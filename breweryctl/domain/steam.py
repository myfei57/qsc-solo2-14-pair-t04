"""蒸汽热源：锅炉调度、母管压力与安全联锁。

职责边界：
- 按母管压力与用汽单元申报的需求决定哪台锅炉点火、带多大负荷；
- 水位/汽压/火焰越线时执行固定顺序的安全处置（MFT），并留下完整事件序列；
- 安全处置是软件监督层，真正的主燃料切断、安全阀、超低水位硬联锁必须由
  独立的安全仪表系统（SIS/PLC）实现，本模块不替代它们。
"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, elapsed_minutes, format_moment
from ..core.config import Settings
from ..core.errors import (
    ConflictError,
    InterlockError,
    NotFoundError,
    SequenceError,
    ValidationError,
)
from ..core.ids import new_id
from ..core.validators import require_choice, require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .models import (
    AlarmSeverity,
    AlarmStatus,
    BoilerState,
    SafetyCaseStatus,
    SteamBoiler,
    SteamHeader,
    SteamSafetyCase,
    SteamUser,
    SteamUserState,
)

BOILERS = "steam_boilers"
HEADERS = "steam_headers"
USERS = "steam_users"
SAFETY_CASES = "steam_safety_cases"

LOW_WATER = "low_water"
LOW_LOW_WATER = "low_low_water"
OVERPRESSURE = "overpressure"
FLAME_FAILURE = "flame_failure"
HIGH_WATER = "high_water"

LATCHING_CODES = (LOW_LOW_WATER, OVERPRESSURE, FLAME_FAILURE)

CASE_ALARM_CODE = {
    LOW_LOW_WATER: "steam_low_low_water_trip",
    OVERPRESSURE: "steam_overpressure_trip",
    FLAME_FAILURE: "steam_flame_failure_trip",
}

CASE_MESSAGES = {
    LOW_LOW_WATER: "超低水位主燃料切断（MFT）",
    OVERPRESSURE: "汽压越限主燃料切断（MFT）",
    FLAME_FAILURE: "运行中失去火焰，主燃料切断（MFT）",
}

# 每类越线的标准处置顺序，执行结果逐步入 SOE。
TRIP_STEPS: dict[str, tuple[str, str, ...]] = {
    LOW_LOW_WATER: (
        "切断燃料供给（MFT）",
        "停给水泵，禁止向炉膛/炉胆补水",
        "关闭并汽阀，解列蒸汽母管",
        "闭锁本炉自动启动，等待人工复位",
    ),
    OVERPRESSURE: (
        "切断燃料供给（MFT）",
        "关闭并汽阀，解列蒸汽母管",
        "保持安全阀通道可用，禁止再点火",
        "闭锁本炉自动启动，等待人工复位",
    ),
    FLAME_FAILURE: (
        "切断燃料供给（MFT），防止燃料积聚",
        "关闭并汽阀，解列蒸汽母管",
        "进入后吹扫，置换炉膛可燃气体",
        "闭锁本炉自动启动，等待人工复位",
    ),
}


class SteamPlant:
    """管理锅炉、母管、用汽单元以及安全联锁。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock, alarms: AlarmCenter) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.boilers = store.collection(BOILERS)
        self.headers = store.collection(HEADERS)
        self.users = store.collection(USERS)
        self.cases = store.collection(SAFETY_CASES)

    # ------------------------------------------------------------------
    # 资源登记
    # ------------------------------------------------------------------

    def register_boiler(self, brewery_id: str, index: int, header_id: str | None = None) -> dict[str, Any]:
        """登记一台锅炉并挂到指定（或默认）母管。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        if index < 1:
            raise ValidationError("锅炉序号必须为正整数", index=index)
        header = self._resolve_header(header_id, brewery_id)
        code = f"B-{index:02d}"
        if any(item.get("code") == code and item.get("brewery_id") == clean_brewery for item in self.boilers.all()):
            raise ValidationError("锅炉编码已存在", code=code)
        now = format_moment(self.clock.now())
        boiler = SteamBoiler(
            id=new_id("boiler"),
            code=code,
            brewery_id=clean_brewery,
            header_id=header["id"],
            updated_at=now,
        )
        document = self.boilers.put(boiler.id, boiler.to_doc())
        self.store.append_event(
            "steam.boiler_registered",
            {"boiler_id": document["id"], "code": code, "header_id": header["id"]},
        )
        return document

    def register_header(self, brewery_id: str, code: str = "HDR-01") -> dict[str, Any]:
        """登记一条蒸汽母管。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_code = require_text(code, field="code", max_length=40)
        if any(item.get("code") == clean_code and item.get("brewery_id") == clean_brewery for item in self.headers.all()):
            raise ValidationError("母管编码已存在", code=clean_code)
        header = SteamHeader(
            id=new_id("hdr"),
            code=clean_code,
            brewery_id=clean_brewery,
            updated_at=format_moment(self.clock.now()),
        )
        return self.headers.put(header.id, header.to_doc())

    def register_user(self, brewery_id: str, code: str, header_id: str | None = None) -> dict[str, Any]:
        """登记一个用汽单元（如糖化锅、煮沸锅）。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_code = require_text(code, field="code", max_length=40)
        header = self._resolve_header(header_id, clean_brewery)
        if any(item.get("code") == clean_code and item.get("brewery_id") == clean_brewery for item in self.users.all()):
            raise ValidationError("用汽单元编码已存在", code=clean_code)
        user = SteamUser(
            id=new_id("steamuser"),
            code=clean_code,
            brewery_id=clean_brewery,
            header_id=header["id"],
            updated_at=format_moment(self.clock.now()),
        )
        return self.users.put(user.id, user.to_doc())

    # ------------------------------------------------------------------
    # 遥测与需求上报
    # ------------------------------------------------------------------

    def report_boiler(
        self,
        boiler_id: str,
        pressure_bar: float,
        water_level_pct: float,
        flame_on: bool,
    ) -> dict[str, Any]:
        """写入锅炉遥测并立即评估安全联锁与给水泵。"""

        document = self.boilers.require(boiler_id, label="锅炉")
        pressure = require_number(pressure_bar, field="pressure_bar", minimum=0.0, maximum=10.0)
        level = require_number(water_level_pct, field="water_level_pct", minimum=0.0, maximum=100.0)
        flame = bool(flame_on)

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            now = format_moment(self.clock.now())
            if current.get("firing_since"):
                runtime = float(current.get("runtime_min", 0.0)) + elapsed_minutes(
                    str(current.get("updated_at", now)), now
                )
            else:
                runtime = float(current.get("runtime_min", 0.0))
            return merge_documents(
                current,
                [
                    ("pressure_bar", pressure),
                    ("water_level_pct", level),
                    ("flame_on", flame),
                    ("runtime_min", round(max(runtime, 0.0), 3)),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"boiler:{boiler_id}"):
            document = self.boilers.update(boiler_id, mutate)
        self._evaluate_safety(document)
        document = self._auto_feed_pump(self.boilers.require(boiler_id))
        return document

    def report_header_pressure(self, header_id: str, pressure_bar: float) -> dict[str, Any]:
        """更新母管压力并维护压降速率与低压持续时间。"""

        self.headers.require(header_id, label="蒸汽母管")
        pressure = require_number(pressure_bar, field="pressure_bar", minimum=0.0, maximum=10.0)

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            now = format_moment(self.clock.now())
            last_at = current.get("last_reading_at")
            last_pressure = current.get("pressure_bar")
            drop_rate = 0.0
            if last_at and last_pressure is not None:
                minutes = elapsed_minutes(str(last_at), now)
                if minutes > 0:
                    drop_rate = round((float(last_pressure) - pressure) / minutes, 4)
            low_since = current.get("low_pressure_since")
            high_since = current.get("high_pressure_since")
            if pressure < self.settings.steam_stage_on_bar:
                low_since = low_since or now
            else:
                low_since = None
            if pressure > self.settings.steam_stage_off_bar:
                high_since = high_since or now
            else:
                high_since = None
            return merge_documents(
                current,
                [
                    ("previous_reading_at", last_at),
                    ("pressure_bar", pressure),
                    ("pressure_drop_bar_min", drop_rate),
                    ("low_pressure_since", low_since),
                    ("high_pressure_since", high_since),
                    ("last_reading_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"header:{header_id}"):
            return self.headers.update(header_id, mutate)

    def set_demand(
        self,
        user_id: str,
        state: str,
        load_pct: float | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """用汽单元申报需求：idle/demand/peak（糖化+煮沸同时用汽即 peak）。"""

        self.users.require(user_id, label="用汽单元")
        clean_state = require_choice(
            state, field="state", choices=[item.value for item in SteamUserState]
        )
        if clean_state == SteamUserState.IDLE.value:
            load = 0.0
        else:
            load = require_number(
                load_pct if load_pct is not None else (100.0 if clean_state == SteamUserState.PEAK.value else 60.0),
                field="load_pct",
                minimum=1.0,
                maximum=100.0,
            )

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            now = format_moment(self.clock.now())
            previous = current.get("state")
            patch: list[tuple[str, Any]] = [
                ("state", clean_state),
                ("load_pct", load),
                ("updated_at", now),
            ]
            if clean_state == SteamUserState.IDLE.value:
                patch.append(("released_at", now))
                patch.append(("batch_id", None))
            else:
                if previous == SteamUserState.IDLE.value:
                    patch.append(("demanded_at", now))
                if batch_id is not None:
                    patch.append(("batch_id", require_text(batch_id, field="batch_id", max_length=64)))
            return merge_documents(current, patch)

        with self.store.locks.guard(f"steamuser:{user_id}"):
            return self.users.update(user_id, mutate)

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def dispatch(self, header_id: str) -> dict[str, Any]:
        """按需求与母管压力决定启停与负荷，返回本次调度决策。"""

        header = self.headers.require(header_id, label="蒸汽母管")
        boilers = [item for item in self.boilers.all() if item.get("header_id") == header_id]
        users = [item for item in self.users.all() if item.get("header_id") == header_id]

        actions: list[dict[str, Any]] = []
        # 先处理后吹扫到时的炉，避免它们继续占着“在役”名额。
        for boiler in boilers:
            if boiler.get("state") == BoilerState.PURGING.value and self._purge_due(boiler):
                self._finish_purge(boiler["id"])
                actions.append({"boiler_id": boiler["id"], "action": "purge_complete"})
            elif boiler.get("state") == BoilerState.STARTING.value and self._lightoff_due(boiler):
                self._confirm_lightoff(boiler["id"], actions)
        boilers = [self.boilers.get(item["id"]) for item in boilers]
        assert all(boilers)

        pressure = self._header_pressure(header)
        demand_load, peak, active_users = self._demand(users)
        lead = self._by_role(boilers, "lead")
        lags = [item for item in boilers if item.get("role") == "lag"]
        in_service = [item for item in boilers if item.get("state") in (
            BoilerState.STARTING.value,
            BoilerState.FIRING.value,
            BoilerState.STOPPING.value,
        )]

        # 超压：未闭锁的炉全部解列，禁止再点火。
        if pressure is not None and pressure >= self.settings.steam_high_pressure_bar:
            self._raise_header_alarm(
                header, "warning", "steam_header_high_pressure",
                f"母管压力 {pressure:.2f} bar 高于处置线 {self.settings.steam_high_pressure_bar:.2f} bar",
                {"pressure_bar": pressure}, repeat_context={"pressure_bar": pressure},
            )
            for boiler in [item for item in in_service if item.get("state") == BoilerState.FIRING.value]:
                self._shutdown_boiler(boiler["id"], reason="header_high_pressure")
                actions.append({"boiler_id": boiler["id"], "action": "shutdown", "reason": "header_high_pressure"})
            self._clear_header_alarm(header, "steam_header_high_pressure", resolve_below=True)
            return self._dispatch_view(header_id, pressure, demand_load, peak, active_users, actions)

        # 母管高压报警自动解除。
        self._clear_header_alarm(header, "steam_header_high_pressure", resolve_below=True)

        available = self._standby_boilers(boilers)
        target_count = 2 if (peak or demand_load >= 100.0) else 1

        if not lead and not in_service:
            # 完全无需求且无压力不达标信号时不主动启炉。
            if target_count >= 1 and self._needs_steam(header, pressure, demand_load):
                chosen = self._pick_lead(available)
                if chosen is not None:
                    self._start_boiler(chosen["id"], role="lead")
                    actions.append({"boiler_id": chosen["id"], "action": "start", "role": "lead"})
        else:
            # 增加在役炉数：尖峰/满载需求，或低压持续超过延时（单炉供不上的实测信号）。
            low_pressure_pending = (
                pressure is not None
                and pressure < self.settings.steam_stage_on_bar
                and self._low_pressure_long_enough(header)
            )
            want_more = peak or demand_load >= 100.0 or low_pressure_pending
            capacity_short = len(in_service) < max(target_count, 2 if low_pressure_pending else target_count)
            if capacity_short and want_more and available:
                role = "lead" if not lead else "lag"
                chosen = available[0]
                self._start_boiler(chosen["id"], role=role)
                actions.append({"boiler_id": chosen["id"], "action": "start", "role": role})

            # 停备用炉：压力高于停炉线且没有尖峰/满载需求，带回差。
            enough = (
                pressure is None
                or pressure >= self.settings.steam_stage_off_bar
            )
            if enough and not peak and demand_load < 100.0 and len(in_service) > 1:
                lag = self._shutdown_candidate(lags)
                if lag is not None:
                    self._shutdown_boiler(lag["id"], reason="pressure_recovered")
                    actions.append({"boiler_id": lag["id"], "action": "shutdown", "reason": "pressure_recovered"})

        # 在役炉负荷调节。
        for boiler in self.boilers.all():
            if boiler.get("header_id") != header_id or boiler.get("state") != BoilerState.FIRING.value:
                continue
            rate = self._firing_rate(boiler, pressure, demand_load)
            if abs(rate - float(boiler.get("firing_rate_pct", 0.0))) > 0.5:
                self._set_firing_rate(boiler["id"], rate)
                actions.append({"boiler_id": boiler["id"], "action": "modulate", "firing_rate_pct": rate})

        return self._dispatch_view(header_id, pressure, demand_load, peak, active_users, actions)

    # ------------------------------------------------------------------
    # 手动操作
    # ------------------------------------------------------------------

    def start_boiler(self, boiler_id: str) -> dict[str, Any]:
        """操作员手动启动一台炉（仍须满足启动许可）。"""

        boiler = self.boilers.require(boiler_id, label="锅炉")
        role = "lead" if not self._by_role(self._header_boilers(boiler["header_id"]), "lead") else "lag"
        self._start_boiler(boiler_id, role=role, manual=True)
        return self.boilers.require(boiler_id)

    def shutdown_boiler(self, boiler_id: str) -> dict[str, Any]:
        """操作员手动停炉，走正常停炉+后吹扫顺序。"""

        self._shutdown_boiler(boiler_id, reason="operator", manual=True)
        return self.boilers.require(boiler_id)

    def manual_trip(self, boiler_id: str, operator: str, reason_code: str = LOW_LOW_WATER, note: str = "") -> dict[str, Any]:
        """操作员紧急停炉（MFT），按该类型的标准顺序处置并留痕。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        clean_code = require_choice(reason_code, field="reason_code", choices=LATCHING_CODES)
        clean_note = require_text(note or "操作员紧急停炉", field="note", max_length=240)
        boiler = self.boilers.require(boiler_id, label="锅炉")
        if boiler.get("state") == BoilerState.LOCKED_OUT.value:
            raise ConflictError("锅炉已处于闭锁状态", boiler_id=boiler_id)
        case = self._trip(
            boiler,
            clean_code,
            readings={
                "pressure_bar": boiler.get("pressure_bar"),
                "water_level_pct": boiler.get("water_level_pct"),
                "flame_on": boiler.get("flame_on"),
                "manual": True,
                "operator": clean_operator,
                "note": clean_note,
            },
        )
        return case

    def acknowledge_case(self, case_id: str, operator: str) -> dict[str, Any]:
        """确认安全处置事件（不解除闭锁）。"""

        clean_operator = require_text(operator, field="operator", max_length=60)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            status = document.get("status")
            if status != SafetyCaseStatus.ACTIVE.value:
                raise ConflictError("安全事件已确认或已复位", case_id=case_id, status=status)
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("status", SafetyCaseStatus.ACKNOWLEDGED.value),
                    ("acknowledged_at", now),
                    ("acknowledged_by", clean_operator),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"safetycase:{case_id}"):
            return self.cases.update(case_id, mutate)

    def reset_case(self, case_id: str, operator: str, note: str) -> dict[str, Any]:
        """安全条件恢复后人工复位闭锁；条件不满足则拒绝。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        clean_note = require_text(note, field="note", max_length=240)
        case = self.cases.require(case_id, label="安全处置事件")
        boiler = self.boilers.require(str(case["boiler_id"]), label="锅炉")
        self._assert_reset_safe(case, boiler)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") == SafetyCaseStatus.RESET.value:
                raise ConflictError("安全事件已经复位", case_id=case_id)
            now = format_moment(self.clock.now())
            steps = list(document.get("steps", []))
            steps.append(
                {
                    "index": len(steps) + 1,
                    "name": f"{clean_operator} 现场确认后人工复位闭锁",
                    "result": "reset",
                    "at": now,
                }
            )
            return merge_documents(
                document,
                [
                    ("status", SafetyCaseStatus.RESET.value),
                    ("reset_at", now),
                    ("reset_by", clean_operator),
                    ("reset_note", clean_note),
                    ("steps", steps),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"safetycase:{case_id}"):
            case = self.cases.update(case_id, mutate)

        def boiler_mutate(document: dict[str, Any]) -> dict[str, Any]:
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("state", BoilerState.STANDBY.value),
                    ("firing_rate_pct", 0.0),
                    ("flame_on", False),
                    ("feed_pump_on", False),
                    ("header_valve_open", False),
                    ("light_off_at", None),
                    ("firing_since", None),
                    ("purge_due_at", None),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"boiler:{boiler['id']}"):
            self.boilers.update(boiler["id"], boiler_mutate)
        self._clear_boiler_alarm(boiler, CASE_ALARM_CODE[str(case["code"])])
        return case

    def purge_status(self, boiler_id: str) -> dict[str, Any]:
        """查询后吹扫剩余时间。"""

        boiler = self.boilers.require(boiler_id, label="锅炉")
        purge_due = boiler.get("purge_due_at")
        remaining = 0.0
        if boiler.get("state") == BoilerState.PURGING.value and purge_due:
            remaining = max(
                0.0,
                self.settings.steam_post_purge_min
                - elapsed_minutes(str(boiler.get("last_stopped_at", purge_due)), format_moment(self.clock.now())),
            )
        return {
            "boiler_id": boiler_id,
            "state": boiler.get("state"),
            "purging": boiler.get("state") == BoilerState.PURGING.value,
            "purge_due_at": purge_due,
            "remaining_min": round(remaining, 2),
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_boiler(self, boiler_id: str) -> dict[str, Any]:
        return self.boilers.require(boiler_id, label="锅炉")

    def get_header(self, header_id: str) -> dict[str, Any]:
        return self.headers.require(header_id, label="蒸汽母管")

    def active_case(self, boiler_id: str) -> dict[str, Any] | None:
        """返回锅炉未复位的安全处置事件。"""

        items = self.cases.find(
            lambda item: item.get("boiler_id") == boiler_id
            and item.get("status") != SafetyCaseStatus.RESET.value
        )
        if not items:
            return None
        items.sort(key=lambda item: str(item.get("raised_at", "")), reverse=True)
        return items[0]

    def list_cases(self, boiler_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        items = self.cases.all()
        if boiler_id:
            items = [item for item in items if item.get("boiler_id") == boiler_id]
        if status:
            clean = require_choice(
                status, field="status", choices=[item.value for item in SafetyCaseStatus]
            )
            items = [item for item in items if item.get("status") == clean]
        return sorted(items, key=lambda item: str(item.get("raised_at", "")), reverse=True)

    def header_snapshot(self, header_id: str) -> dict[str, Any]:
        """返回母管压力、锅炉与用汽单元的完整画面。"""

        header = self.headers.require(header_id, label="蒸汽母管")
        boilers = [item for item in self.boilers.all() if item.get("header_id") == header_id]
        users = [item for item in self.users.all() if item.get("header_id") == header_id]
        load, peak, active_users = self._demand(users)
        return {
            "header": header,
            "boilers": sorted(boilers, key=lambda item: str(item.get("code"))),
            "users": sorted(users, key=lambda item: str(item.get("code"))),
            "demand_load_pct": load,
            "peak": peak,
            "active_users": active_users,
            "thresholds": {
                "setpoint_bar": self.settings.steam_setpoint_bar,
                "stage_on_bar": self.settings.steam_stage_on_bar,
                "stage_off_bar": self.settings.steam_stage_off_bar,
                "high_pressure_bar": self.settings.steam_high_pressure_bar,
                "trip_pressure_bar": self.settings.steam_trip_pressure_bar,
                "low_water_pct": self.settings.steam_low_water_pct,
                "low_low_water_pct": self.settings.steam_low_low_water_pct,
                "high_water_pct": self.settings.steam_high_water_pct,
            },
        }

    def summary(self) -> dict[str, Any]:
        boilers = self.boilers.all()
        cases = self.cases.all()
        by_state: dict[str, int] = {}
        for item in boilers:
            key = str(item.get("state"))
            by_state[key] = by_state.get(key, 0) + 1
        return {
            "headers": self.headers.count(),
            "boilers": len(boilers),
            "users": self.users.count(),
            "by_state": by_state,
            "active_cases": len(
                [item for item in cases if item.get("status") != SafetyCaseStatus.RESET.value]
            ),
        }

    # ------------------------------------------------------------------
    # 内部：安全联锁
    # ------------------------------------------------------------------

    def _evaluate_safety(self, boiler: dict[str, Any]) -> None:
        """按固定优先级检查越线，命中即 MFT 并闩锁。"""

        if boiler.get("state") == BoilerState.LOCKED_OUT.value:
            return
        pressure = boiler.get("pressure_bar")
        level = boiler.get("water_level_pct")
        flame = bool(boiler.get("flame_on"))

        if level is not None and float(level) <= self.settings.steam_low_low_water_pct:
            self._trip(boiler, LOW_LOW_WATER, readings={"pressure_bar": pressure, "water_level_pct": level, "flame_on": flame})
            return
        if pressure is not None and float(pressure) >= self.settings.steam_trip_pressure_bar:
            self._trip(boiler, OVERPRESSURE, readings={"pressure_bar": pressure, "water_level_pct": level, "flame_on": flame})
            return
        if self._flame_lost(boiler):
            self._trip(boiler, FLAME_FAILURE, readings={"pressure_bar": pressure, "water_level_pct": level, "flame_on": flame})
            return

        # 非闩锁预警：低水位/高水位，恢复正常后自动解除。
        if level is not None and float(level) < self.settings.steam_low_water_pct:
            self._raise_boiler_alarm(
                boiler,
                AlarmSeverity.WARNING.value,
                "steam_low_water",
                f"水位 {float(level):.1f}% 低于低水位线 {self.settings.steam_low_water_pct:.1f}%，限制负荷",
                latching=False,
                context={"water_level_pct": level},
            )
        else:
            self._clear_boiler_alarm(boiler, "steam_low_water")
        if level is not None and float(level) > self.settings.steam_high_water_pct:
            self._raise_boiler_alarm(
                boiler,
                AlarmSeverity.WARNING.value,
                "steam_high_water",
                f"水位 {float(level):.1f}% 高于高水位线 {self.settings.steam_high_water_pct:.1f}%",
                latching=False,
                context={"water_level_pct": level},
            )
        else:
            self._clear_boiler_alarm(boiler, "steam_high_water")

    def _flame_lost(self, boiler: dict[str, Any]) -> bool:
        if boiler.get("state") != BoilerState.FIRING.value or boiler.get("flame_on"):
            return False
        light_off = boiler.get("light_off_at")
        if not light_off:
            return False
        return elapsed_minutes(str(light_off), format_moment(self.clock.now())) >= self.settings.steam_flame_grace_min

    def _trip(self, boiler: dict[str, Any], code: str, readings: dict[str, Any]) -> dict[str, Any]:
        """执行标准 MFT 顺序，记录 SOE，闩锁锅炉。"""

        boiler = self.boilers.require(boiler["id"], label="锅炉")
        if boiler.get("state") == BoilerState.LOCKED_OUT.value and self.active_case(boiler["id"]):
            return self.active_case(boiler["id"])  # type: ignore[return-value]

        now = format_moment(self.clock.now())
        step_names = TRIP_STEPS[code]
        # 失火焰后才需要后吹扫；其余两类不允许在闭锁状态吹扫。
        steps = [
            {"index": index, "name": name, "result": "executed", "at": now}
            for index, name in enumerate(step_names, start=1)
        ]
        purge_due = None
        if code == FLAME_FAILURE:
            from datetime import timedelta

            purge_due = format_moment(self.clock.now() + timedelta(minutes=self.settings.steam_post_purge_min))
        case = SteamSafetyCase(
            id=new_id("case"),
            brewery_id=str(boiler.get("brewery_id")),
            boiler_id=boiler["id"],
            code=code,
            severity=AlarmSeverity.CRITICAL.value,
            reason=CASE_MESSAGES[code],
            readings=dict(readings),
            steps=steps,
            raised_at=now,
            updated_at=now,
        )
        saved_case = self.cases.put(case.id, case.to_doc())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("state", BoilerState.LOCKED_OUT.value),
                    ("role", "none"),
                    ("firing_rate_pct", 0.0),
                    ("flame_on", False),
                    ("feed_pump_on", False),
                    ("header_valve_open", False),
                    ("firing_since", None),
                    ("light_off_at", None),
                    ("purge_due_at", purge_due),
                    ("last_stopped_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"boiler:{boiler['id']}"):
            self.boilers.update(boiler["id"], mutate)
        self._clear_header_roles(boiler["id"])
        self._raise_boiler_alarm(
            boiler,
            AlarmSeverity.CRITICAL.value,
            CASE_ALARM_CODE[code],
            CASE_MESSAGES[code],
            latching=True,
            context={"case_id": case.id, **{k: v for k, v in readings.items() if v is not None}},
        )
        self.store.append_event(
            "steam.safety_trip",
            {"boiler_id": boiler["id"], "case_id": case.id, "code": code, "readings": readings, "steps": len(steps)},
        )
        return saved_case

    def _assert_reset_safe(self, case: dict[str, Any], boiler: dict[str, Any]) -> None:
        if case.get("status") == SafetyCaseStatus.ACTIVE.value:
            raise InterlockError("安全事件尚未确认，禁止复位闭锁", case_id=case["id"])
        level = boiler.get("water_level_pct")
        pressure = boiler.get("pressure_bar")
        if level is None or pressure is None:
            raise InterlockError("缺少最新水位/汽压遥测，无法确认安全状态", boiler_id=boiler["id"])
        code = str(case.get("code"))
        if code == LOW_LOW_WATER and not (
            self.settings.steam_low_water_pct <= float(level) <= self.settings.steam_high_water_pct
        ):
            raise InterlockError(
                "水位尚未恢复到正常区间，禁止复位",
                boiler_id=boiler["id"],
                water_level_pct=level,
                safe_range=[self.settings.steam_low_water_pct, self.settings.steam_high_water_pct],
            )
        if code == OVERPRESSURE and float(pressure) >= self.settings.steam_stage_off_bar:
            raise InterlockError(
                "汽压尚未回落到停炉线以下，禁止复位",
                boiler_id=boiler["id"],
                pressure_bar=pressure,
                safe_below=self.settings.steam_stage_off_bar,
            )
        if code == FLAME_FAILURE and bool(boiler.get("flame_on")):
            raise InterlockError("仍检测到火焰信号，禁止复位", boiler_id=boiler["id"])

    def _auto_feed_pump(self, boiler: dict[str, Any]) -> dict[str, Any]:
        """给水泵只在非闭锁且水位偏低时自动启动，高水位/闭锁立即停泵。"""

        if boiler.get("state") == BoilerState.LOCKED_OUT.value:
            target = False
        else:
            level = boiler.get("water_level_pct")
            if level is None:
                return boiler
            midpoint = (self.settings.steam_low_water_pct + self.settings.steam_high_water_pct) / 2
            target = float(level) < midpoint
        if bool(boiler.get("feed_pump_on")) == target:
            return boiler

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [("feed_pump_on", target), ("updated_at", format_moment(self.clock.now()))],
            )

        with self.store.locks.guard(f"boiler:{boiler['id']}"):
            updated = self.boilers.update(boiler["id"], mutate)
        self.store.append_event(
            "steam.feed_pump",
            {"boiler_id": boiler["id"], "on": target, "water_level_pct": boiler.get("water_level_pct")},
        )
        return updated

    # ------------------------------------------------------------------
    # 内部：启停顺序
    # ------------------------------------------------------------------

    def _start_permissive(self, boiler: dict[str, Any]) -> None:
        if boiler.get("state") == BoilerState.LOCKED_OUT.value:
            raise InterlockError("锅炉处于安全闭锁，禁止启动", boiler_id=boiler["id"])
        if boiler.get("state") not in (BoilerState.STANDBY.value,):
            raise SequenceError(
                "当前锅炉状态不允许启动", boiler_id=boiler["id"], state=boiler.get("state")
            )
        if self.active_case(boiler["id"]):
            raise InterlockLockGuard(boiler["id"])
        level = boiler.get("water_level_pct")
        pressure = boiler.get("pressure_bar")
        if level is None or pressure is None:
            raise InterlockError(
                "缺少最新水位/汽压遥测，不满足启动许可", boiler_id=boiler["id"]
            )
        if not (self.settings.steam_low_water_pct <= float(level) <= self.settings.steam_high_water_pct):
            raise InterlockError(
                "水位不在正常区间，禁止启动",
                boiler_id=boiler["id"],
                water_level_pct=level,
                safe_range=[self.settings.steam_low_water_pct, self.settings.steam_high_water_pct],
            )
        if float(pressure) >= self.settings.steam_stage_off_bar:
            raise InterlockError(
                "炉内压力高于停炉线，禁止启动",
                boiler_id=boiler["id"],
                pressure_bar=pressure,
            )
        if bool(boiler.get("flame_on")):
            raise InterlockError("检测到残火，必须先吹扫", boiler_id=boiler["id"])

    def _start_boiler(self, boiler_id: str, role: str, manual: bool = False) -> dict[str, Any]:
        boiler = self.boilers.require(boiler_id, label="锅炉")
        self._start_permissive(boiler)
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("state", BoilerState.STARTING.value),
                    ("role", role),
                    ("header_valve_open", True),
                    ("flame_on", True),
                    ("firing_rate_pct", 20.0),
                    ("light_off_at", now),
                    ("firing_since", now),
                    ("last_started_at", now),
                    ("cycles", int(document.get("cycles", 0)) + 1),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"boiler:{boiler_id}"):
            updated = self.boilers.update(boiler_id, mutate)
        self._assign_role(boiler_id, role)
        self.store.append_event(
            "steam.boiler_start",
            {"boiler_id": boiler_id, "role": role, "manual": manual},
        )
        return updated

    def _shutdown_boiler(self, boiler_id: str, reason: str, manual: bool = False) -> dict[str, Any]:
        boiler = self.boilers.require(boiler_id, label="锅炉")
        if boiler.get("state") not in (BoilerState.STARTING.value, BoilerState.FIRING.value):
            raise SequenceError(
                "当前锅炉状态不允许停炉", boiler_id=boiler_id, state=boiler.get("state")
            )
        now = format_moment(self.clock.now())
        from datetime import timedelta

        purge_due = format_moment(self.clock.now() + timedelta(minutes=self.settings.steam_post_purge_min))

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("state", BoilerState.PURGING.value),
                    ("firing_rate_pct", 0.0),
                    ("flame_on", False),
                    ("header_valve_open", False),
                    ("firing_since", None),
                    ("light_off_at", None),
                    ("role", "none"),
                    ("purge_due_at", purge_due),
                    ("last_stopped_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"boiler:{boiler_id}"):
            updated = self.boilers.update(boiler_id, mutate)
        self._clear_header_roles(boiler_id)
        self.store.append_event(
            "steam.boiler_shutdown",
            {"boiler_id": boiler_id, "reason": reason, "manual": manual},
        )
        return updated

    def _finish_purge(self, boiler_id: str) -> dict[str, Any]:
        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("state") != BoilerState.PURGING.value:
                raise SequenceError("锅炉不在后吹扫状态", boiler_id=boiler_id)
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("state", BoilerState.STANDBY.value), ("purge_due_at", None), ("updated_at", now)],
            )

        with self.store.locks.guard(f"boiler:{boiler_id}"):
            updated = self.boilers.update(boiler_id, mutate)
        self.store.append_event("steam.purge_complete", {"boiler_id": boiler_id})
        return updated

    def _purge_due(self, boiler: dict[str, Any]) -> bool:
        purge_due = boiler.get("purge_due_at")
        if not purge_due:
            return False
        return format_moment(self.clock.now()) >= str(purge_due)

    def _lightoff_due(self, boiler: dict[str, Any]) -> bool:
        """点火宽限期已过，可确认火焰。"""

        light_off = boiler.get("light_off_at")
        if not light_off:
            return True
        return elapsed_minutes(str(light_off), format_moment(self.clock.now())) >= self.settings.steam_flame_grace_min

    def _confirm_lightoff(self, boiler_id: str, actions: list[dict[str, Any]]) -> None:
        """点火确认：有火焰则进入运行，无火焰判点火失败走 MFT。"""

        boiler = self.boilers.require(boiler_id)
        if boiler.get("flame_on"):
            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                return merge_documents(
                    document,
                    [
                        ("state", BoilerState.FIRING.value),
                        ("firing_rate_pct", 20.0),
                        ("updated_at", format_moment(self.clock.now())),
                    ],
                )

            with self.store.locks.guard(f"boiler:{boiler_id}"):
                self.boilers.update(boiler_id, mutate)
            actions.append({"boiler_id": boiler_id, "action": "lightoff_confirmed"})
            return
        self._trip(
            boiler,
            FLAME_FAILURE,
            readings={
                "pressure_bar": boiler.get("pressure_bar"),
                "water_level_pct": boiler.get("water_level_pct"),
                "flame_on": False,
                "ignition": True,
            },
        )
        actions.append({"boiler_id": boiler_id, "action": "ignition_failure_trip"})

    # ------------------------------------------------------------------
    # 内部：调度辅助
    # ------------------------------------------------------------------

    def _demand(self, users: list[dict[str, Any]]) -> tuple[float, bool, list[str]]:
        active = [item for item in users if item.get("state") != SteamUserState.IDLE.value]
        if not active:
            return 0.0, False, []
        load = min(100.0, sum(float(item.get("load_pct", 0.0)) for item in active))
        peak = any(item.get("state") == SteamUserState.PEAK.value for item in active) or len(active) >= 2
        return load, peak, [str(item.get("code")) for item in active]

    def _needs_steam(self, header: dict[str, Any], pressure: float | None, demand_load: float) -> bool:
        if demand_load > 0:
            return True
        return pressure is not None and pressure < self.settings.steam_stage_on_bar

    def _low_pressure_long_enough(self, header: dict[str, Any]) -> bool:
        since = header.get("low_pressure_since")
        if not since:
            return False
        return elapsed_minutes(str(since), format_moment(self.clock.now())) >= self.settings.steam_stage_delay_min

    def _header_pressure(self, header: dict[str, Any]) -> float | None:
        value = header.get("pressure_bar")
        return float(value) if value is not None else None

    def _standby_boilers(self, boilers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        for boiler in boilers:
            if boiler.get("state") != BoilerState.STANDBY.value:
                continue
            if self.active_case(boiler["id"]):
                continue
            ready.append(boiler)
        # 优先累计运行时间短的炉，均衡磨损。
        ready.sort(key=lambda item: (float(item.get("runtime_min", 0.0)), str(item.get("code"))))
        return ready

    def _pick_lead(self, available: list[dict[str, Any]]) -> dict[str, Any] | None:
        return available[0] if available else None

    def _shutdown_candidate(self, lags: list[dict[str, Any]]) -> dict[str, Any] | None:
        firing = [item for item in lags if item.get("state") in (BoilerState.FIRING.value, BoilerState.STARTING.value)]
        if not firing:
            return None
        firing.sort(key=lambda item: float(item.get("runtime_min", 0.0)), reverse=True)
        return firing[0]

    def _firing_rate(self, boiler: dict[str, Any], pressure: float | None, demand_load: float) -> float:
        if pressure is None:
            rate = max(20.0, demand_load)
        else:
            rate = demand_load + (self.settings.steam_setpoint_bar - pressure) * 200.0
        level = boiler.get("water_level_pct")
        if level is not None and float(level) < self.settings.steam_low_water_pct:
            rate = min(rate, 50.0)
        return round(min(100.0, max(20.0, rate)), 1)

    def _set_firing_rate(self, boiler_id: str, rate: float) -> None:
        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [("firing_rate_pct", rate), ("updated_at", format_moment(self.clock.now()))],
            )

        with self.store.locks.guard(f"boiler:{boiler_id}"):
            self.boilers.update(boiler_id, mutate)

    def _dispatch_view(
        self,
        header_id: str,
        pressure: float | None,
        demand_load: float,
        peak: bool,
        active_users: list[str],
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot = self.header_snapshot(header_id)
        snapshot["demand_load_pct"] = demand_load
        snapshot["peak"] = peak
        snapshot["active_users"] = active_users
        snapshot["pressure_bar"] = pressure
        snapshot["actions"] = actions
        return snapshot

    # ------------------------------------------------------------------
    # 内部：母管角色与告警
    # ------------------------------------------------------------------

    def _header_boilers(self, header_id: str) -> list[dict[str, Any]]:
        return [item for item in self.boilers.all() if item.get("header_id") == header_id]

    def _by_role(self, boilers: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
        for item in boilers:
            if item.get("role") == role and item.get("state") in (
                BoilerState.STARTING.value,
                BoilerState.FIRING.value,
                BoilerState.STOPPING.value,
            ):
                return item
        return None

    def _assign_role(self, boiler_id: str, role: str) -> None:
        document = self.boilers.require(boiler_id)
        header = self.headers.require(str(document["header_id"]), label="蒸汽母管")

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            lags = list(current.get("lag_boiler_ids", []))
            lead_id = current.get("lead_boiler_id")
            if role == "lead":
                lead_id = boiler_id
            elif boiler_id not in lags:
                lags.append(boiler_id)
            return merge_documents(
                current,
                [("lead_boiler_id", lead_id), ("lag_boiler_ids", lags), ("updated_at", format_moment(self.clock.now()))],
            )

        with self.store.locks.guard(f"header:{header['id']}"):
            self.headers.update(header["id"], mutate)

    def _clear_header_roles(self, boiler_id: str) -> None:
        for header in self.headers.all():
            lead = header.get("lead_boiler_id")
            lags = header.get("lag_boiler_ids", [])
            if lead != boiler_id and boiler_id not in lags:
                continue

            def mutate(current: dict[str, Any]) -> dict[str, Any]:
                new_lags = [item for item in current.get("lag_boiler_ids", []) if item != boiler_id]
                new_lead = None if current.get("lead_boiler_id") == boiler_id else current.get("lead_boiler_id")
                # 若被解列的是 lead，把一台 lag 提上来，保持调度连续。
                if new_lead is None and new_lags:
                    new_lead = new_lags.pop(0)
                return merge_documents(
                    current,
                    [
                        ("lead_boiler_id", new_lead),
                        ("lag_boiler_ids", new_lags),
                        ("updated_at", format_moment(self.clock.now())),
                    ],
                )

            with self.store.locks.guard(f"header:{header['id']}"):
                self.headers.update(header["id"], mutate)
            # 同步新 lead 的角色字段（可能是 lag 在役炉被提升）。
            updated = self.headers.get(header["id"])
            if updated and updated.get("lead_boiler_id"):
                lead_id = str(updated["lead_boiler_id"])

                def promote(document: dict[str, Any]) -> dict[str, Any]:
                    if document.get("role") == "lead":
                        return document
                    return merge_documents(
                        document, [("role", "lead"), ("updated_at", format_moment(self.clock.now()))]
                    )

                with self.store.locks.guard(f"boiler:{lead_id}"):
                    self.boilers.update(lead_id, promote)

    def _raise_boiler_alarm(
        self,
        boiler: dict[str, Any],
        severity: str,
        code: str,
        message: str,
        latching: bool,
        context: dict[str, Any],
    ) -> None:
        self.alarms.raise_alarm(
            brewery_id=str(boiler.get("brewery_id")),
            source=f"steam:{boiler['id']}",
            severity=severity,
            code=code,
            message=message,
            latching=latching,
            context=context,
        )

    def _clear_boiler_alarm(self, boiler: dict[str, Any], code: str) -> None:
        for alarm in self.alarms.list_alarms(
            status=AlarmStatus.ACTIVE.value, brewery_id=str(boiler.get("brewery_id"))
        ) + self.alarms.list_alarms(
            status=AlarmStatus.ACKNOWLEDGED.value, brewery_id=str(boiler.get("brewery_id"))
        ):
            if alarm.get("source") == f"steam:{boiler['id']}" and alarm.get("code") == code:
                if alarm.get("status") == AlarmStatus.ACTIVE.value and alarm.get("latching"):
                    # 闩锁告警必须先确认；复位流程中先补确认再解除。
                    self.alarms.acknowledge(str(alarm["id"]), "steam-plant")
                self.alarms.resolve(str(alarm["id"]), "steam-plant", "工艺参数恢复正常，自动解除")

    def _raise_header_alarm(
        self,
        header: dict[str, Any],
        severity: str,
        code: str,
        message: str,
        context: dict[str, Any],
        repeat_context: dict[str, Any] | None = None,
    ) -> None:
        self.alarms.raise_alarm(
            brewery_id=str(header.get("brewery_id")),
            source=f"steam-header:{header['id']}",
            severity=severity,
            code=code,
            message=message,
            latching=False,
            context=context,
        )

    def _clear_header_alarm(self, header: dict[str, Any], code: str, resolve_below: bool) -> None:
        pressure = header.get("pressure_bar")
        if resolve_below and pressure is not None and float(pressure) >= self.settings.steam_high_pressure_bar:
            return
        for alarm in self.alarms.list_alarms(
            status=AlarmStatus.ACTIVE.value, brewery_id=str(header.get("brewery_id"))
        ):
            if alarm.get("source") == f"steam-header:{header['id']}" and alarm.get("code") == code:
                self.alarms.resolve(str(alarm["id"]), "steam-plant", "母管压力恢复正常，自动解除")

    def _resolve_header(self, header_id: str | None, brewery_id: str) -> dict[str, Any]:
        if header_id:
            return self.headers.require(header_id, label="蒸汽母管")
        headers = [item for item in self.headers.all() if item.get("brewery_id") == brewery_id]
        if headers:
            return headers[0]
        return self.register_header(brewery_id)


class InterlockLockGuard(InterlockError):
    """存在未复位安全事件时的启动拒绝。"""

    def __init__(self, boiler_id: str) -> None:
        super().__init__("锅炉存在未复位的安全处置事件，禁止启动", boiler_id=boiler_id)
