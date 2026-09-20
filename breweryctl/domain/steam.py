"""热源供给：蒸汽锅炉调度与水位/汽压越线安全处置。

该模块是监督层（supervisory）：它根据用汽需求决定点/停哪台锅炉，在水位或
汽压越线时按固定安全顺序生成处置单并执行自动步骤。真实的火焰检测、给水泵、
安全阀等最终保护必须由独立的硬接线安全联锁承担，软件这一层给出的是可留档、
可追溯、可被操作员确认的顺序逻辑，不能替代硬件保护回路。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, InterlockError, LatchError, NotFoundError, SequenceError, ValidationError
from ..core.ids import new_id, slugify
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .models import (
    Boiler,
    BoilerState,
    InterlockCase,
    InterlockKind,
    InterlockState,
    InterlockStep,
    StepStatus,
    SteamConsumer,
    SteamHeader,
)

BOILERS = "boilers"
STEAM_CONSUMERS = "steam_consumers"
STEAM_HEADERS = "steam_headers"
INTERLOCKS = "steam_interlocks"

CONSUMER_KINDS = ("mash", "boil", "cip", "other")

# 自动步骤在锅炉台账上对应的执行器变更，None 表示纯记录/通知步骤。
# 键与处置模板中的 step key 对齐。
_ACTION_EFFECT: dict[str, dict[str, bool]] = {
    "fuel_cut": {"burner_on": False},
    "burner_stop": {"burner_on": False},
    "feedwater_close": {"feedwater_open": False},
    "blowdown_open": {"blowdown_open": True},
    "blowdown_close": {"blowdown_open": False},
    "steam_isolate": {"steam_isolated": True},
    "steam_restore": {"steam_isolated": False},
    "feedwater_open": {"feedwater_open": True},
}


@dataclass(frozen=True)
class _PlanStep:
    key: str
    label: str
    automatic: bool


# 各越线类型的固定安全处置顺序。跳闸类（tripping）执行完自动步后挂牌锁炉。
_PLANS: dict[str, dict[str, Any]] = {
    InterlockKind.LOW_LOW_WATER.value: {
        "tripping": True,
        "severity": "critical",
        "alarm_code": "boiler_llw_trip",
        "message": "锅炉低低水位，切燃料停炉并挂牌",
        "steps": [
            _PlanStep("fuel_cut", "立即切断燃料", True),
            _PlanStep("burner_stop", "停燃烧器（严禁低水位硬烧）", True),
            _PlanStep("feedwater_close", "隔离给水，防止冷水进干烧炉体", True),
            _PlanStep("blowdown_open", "保持排污/泄压通道，确认无蒸汽喷出", True),
            _PlanStep("steam_isolate", "关闭主蒸汽阀，解列该炉", True),
            _PlanStep("tag", "现场挂『禁止点火』牌并人工确认", False),
            _PlanStep("inspect", "查明低水位原因（泄漏/给水泵/液位计）后复位", False),
        ],
    },
    InterlockKind.STEAM_HIGH.value: {
        "tripping": True,
        "severity": "critical",
        "alarm_code": "boiler_steam_hh_trip",
        "message": "锅炉汽压越上限，切燃料泄压并挂牌",
        "steps": [
            _PlanStep("fuel_cut", "立即切断燃料", True),
            _PlanStep("burner_stop", "停燃烧器、降低热负荷", True),
            _PlanStep("blowdown_open", "确认对空排汽/安全阀动作正常", True),
            _PlanStep("steam_isolate", "关闭主蒸汽阀，解列该炉", True),
            _PlanStep("tag", "确认汽压回落到安全区后挂牌复位", False),
        ],
    },
    InterlockKind.HIGH_HIGH_WATER.value: {
        "tripping": True,
        "severity": "critical",
        "alarm_code": "boiler_hhw_trip",
        "message": "锅炉高高水位（满水），停炉隔离防蒸汽带水",
        "steps": [
            _PlanStep("fuel_cut", "立即切断燃料", True),
            _PlanStep("burner_stop", "停燃烧器", True),
            _PlanStep("feedwater_close", "关闭给水阀", True),
            _PlanStep("blowdown_open", "排污放水至正常水位", True),
            _PlanStep("steam_isolate", "关闭主蒸汽阀，防止带水进入联箱", True),
            _PlanStep("tag", "确认恢复正常水位后挂牌复位", False),
        ],
    },
    InterlockKind.LOW_WATER.value: {
        "tripping": False,
        "severity": "warning",
        "alarm_code": "boiler_low_water",
        "message": "锅炉低水位，自动补给水并减负荷",
        "steps": [
            _PlanStep("feedwater_open", "启动给水泵补水", True),
            _PlanStep("burner_load", "降低燃烧负荷，监视水位回升", False),
        ],
    },
    InterlockKind.HIGH_WATER.value: {
        "tripping": False,
        "severity": "warning",
        "alarm_code": "boiler_high_water",
        "message": "锅炉高水位，停止给水并排污",
        "steps": [
            _PlanStep("feedwater_close", "关闭给水阀", True),
            _PlanStep("blowdown_open", "短时排污放水", True),
        ],
    },
}

# 恢复型处置单判定是否已回到安全区。
_RECOVER_KEY = {
    InterlockKind.LOW_WATER.value: "low",
    InterlockKind.HIGH_WATER.value: "high",
}


class SteamPlant:
    """管理蒸汽用户需求、锅炉台账、调度与越线处置。"""

    def __init__(
        self,
        store: FileStore,
        settings: Settings,
        clock: Clock,
        alarms: AlarmCenter,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.boilers = store.collection(BOILERS)
        self.consumers = store.collection(STEAM_CONSUMERS)
        self.headers = store.collection(STEAM_HEADERS)
        self.interlocks = store.collection(INTERLOCKS)

    # ------------------------------------------------------------------ 台账

    def register_boiler(
        self,
        brewery_id: str,
        code: str,
        rating_kgh: float,
        priority: int = 100,
        water_pct: float | None = None,
    ) -> dict[str, Any]:
        """登记一台锅炉；初始水位给定时视为一条新鲜测点。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_code = slugify(require_text(code, field="code", max_length=32)).upper()
        rating = require_number(rating_kgh, field="rating_kgh", minimum=1.0, maximum=100_000.0)
        if any(item.get("code") == clean_code for item in self._boilers_of(clean_brewery)):
            raise ValidationError("锅炉编号已存在", code=clean_code)
        now = format_moment(self.clock.now())
        boiler = Boiler(
            id=new_id("boiler"),
            code=clean_code,
            brewery_id=clean_brewery,
            rating_kgh=rating,
            priority=int(priority),
            water_pct=None if water_pct is None else self._safe_water(water_pct),
            last_reading_at=now if water_pct is not None else None,
            updated_at=now,
        )
        document = self.boilers.put(boiler.id, boiler.to_doc())
        self.store.append_event(
            "steam.boiler_registered",
            {"boiler_id": document["id"], "code": clean_code, "rating_kgh": rating},
        )
        return document

    def register_consumer(
        self,
        brewery_id: str,
        code: str,
        name: str,
        demand_kgh: float,
        kind: str = "other",
    ) -> dict[str, Any]:
        """登记一个用汽设备及其额定耗汽量（kg/h）。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_code = slugify(require_text(code, field="code", max_length=32))
        if kind not in CONSUMER_KINDS:
            raise ValidationError("用汽设备类型不合法", kind=kind, allowed=list(CONSUMER_KINDS))
        clean_name = require_text(name, field="name", max_length=80)
        demand = require_number(demand_kgh, field="demand_kgh", minimum=0.0, maximum=100_000.0)
        existing = self.consumers.find(
            lambda item: item.get("brewery_id") == clean_brewery and item.get("code") == clean_code
        )
        if existing:
            raise ValidationError("用汽设备编号已存在", code=clean_code)
        now = format_moment(self.clock.now())
        consumer = SteamConsumer(
            id=new_id("steamc"),
            code=clean_code,
            brewery_id=clean_brewery,
            name=clean_name,
            demand_kgh=demand,
            updated_at=now,
        )
        # kind 不进数据类（保持模型精简），但台账里保留便于筛选。
        document = consumer.to_doc()
        document["kind"] = kind
        return self.consumers.put(consumer.id, document)

    # ------------------------------------------------------------------ 需求

    def claim_consumer(
        self,
        consumer_id: str,
        actor: str,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """用汽设备投入（例如糖化与煮沸同时用汽）。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("active"):
                raise ConflictError("该用汽设备已在供汽", consumer_id=consumer_id)
            return merge_documents(
                document,
                [
                    ("active", True),
                    ("batch_id", batch_id),
                    ("claimed_at", now),
                    ("claimed_by", clean_actor),
                    ("updated_at", now),
                ],
            )

        return self.consumers.update(consumer_id, mutate)

    def release_consumer(self, consumer_id: str, actor: str) -> dict[str, Any]:
        """用汽设备退出。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("active", False),
                    ("batch_id", None),
                    ("claimed_at", None),
                    ("claimed_by", clean_actor),
                    ("updated_at", now),
                ],
            )

        return self.consumers.update(consumer_id, mutate)

    def demand(self, brewery_id: str) -> dict[str, Any]:
        """汇总厂区当前瞬时用汽需求。"""

        active = [
            item
            for item in self.consumers.all()
            if item.get("brewery_id") == brewery_id and item.get("active")
        ]
        total = round(sum(float(item.get("demand_kgh", 0.0)) for item in active), 3)
        return {
            "brewery_id": brewery_id,
            "total_kgh": total,
            "active_consumers": [
                {
                    "id": item["id"],
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "demand_kgh": item.get("demand_kgh"),
                    "batch_id": item.get("batch_id"),
                }
                for item in sorted(active, key=lambda item: str(item.get("code")))
            ],
        }

    def report_header(self, brewery_id: str, pressure_bar: float) -> dict[str, Any]:
        """登记联箱（分汽缸）最新汽压测点。"""

        pressure = require_number(pressure_bar, field="pressure_bar", minimum=0.0, maximum=10.0)
        now = format_moment(self.clock.now())
        header = SteamHeader(brewery_id=brewery_id, pressure_bar=pressure, updated_at=now)
        return self.headers.put(brewery_id, header.to_doc())

    # ------------------------------------------------------------------ 测点

    def report_boiler(
        self,
        boiler_id: str,
        water_pct: float | None = None,
        steam_bar: float | None = None,
    ) -> dict[str, Any]:
        """上报锅炉水位与汽压；越线时立即按安全顺序处置。

        返回包含处置结果的视图。低低/高高水位与高高压优先于普通越限；
        普通低水位/高水位只在锅炉没有跳闸处置单时干预。
        """

        boiler = self.boilers.require(boiler_id, label="锅炉")
        water = self._safe_water(water_pct) if water_pct is not None else boiler.get("water_pct")
        steam = (
            require_number(steam_bar, field="steam_bar", minimum=0.0, maximum=10.0)
            if steam_bar is not None
            else boiler.get("steam_bar")
        )
        now = format_moment(self.clock.now())
        updated = merge_documents(
            boiler,
            [
                ("water_pct", water),
                ("steam_bar", steam),
                ("last_reading_at", now),
                ("updated_at", now),
            ],
        )
        updated = self.boilers.put(boiler_id, updated)
        case = self._evaluate_limits(updated)
        return {"boiler": self.boilers.require(boiler_id), "interlock": case}

    def _evaluate_limits(self, boiler: dict[str, Any]) -> dict[str, Any] | None:
        water = boiler.get("water_pct")
        steam = boiler.get("steam_bar")
        kind: str | None = None
        trigger = 0.0
        threshold = 0.0
        if water is not None:
            w = float(water)
            if w <= self.settings.steam_low_low_water_pct:
                kind, trigger, threshold = (
                    InterlockKind.LOW_LOW_WATER.value,
                    w,
                    self.settings.steam_low_low_water_pct,
                )
            elif w >= self.settings.steam_high_high_water_pct:
                kind, trigger, threshold = (
                    InterlockKind.HIGH_HIGH_WATER.value,
                    w,
                    self.settings.steam_high_high_water_pct,
                )
        if kind is None and steam is not None and float(steam) >= self.settings.steam_header_trip_bar:
            kind, trigger, threshold = (
                InterlockKind.STEAM_HIGH.value,
                float(steam),
                self.settings.steam_header_trip_bar,
            )
        if kind is not None:
            # 跳闸类：同型活跃处置单已存在则不重复开单；非同型活跃单也不再叠加。
            if self._active_case(str(boiler["id"]), tripping_only=True):
                return self._active_case(str(boiler["id"]))
            return self.open_case(boiler, kind, trigger, threshold)
        # 恢复型越限：存在跳闸处置单时不干预（跳闸优先级最高）。
        if water is not None and not self._active_case(str(boiler["id"])):
            w = float(water)
            if w < self.settings.steam_low_water_pct:
                return self.open_case(
                    boiler, InterlockKind.LOW_WATER.value, w, self.settings.steam_low_water_pct
                )
            if w > self.settings.steam_high_water_pct:
                return self.open_case(
                    boiler, InterlockKind.HIGH_WATER.value, w, self.settings.steam_high_water_pct
                )
        # 工艺量回到安全区：自动闭环恢复型处置单。
        return self._auto_recover(boiler)

    # ------------------------------------------------------------- 处置单

    def open_case(
        self,
        boiler: dict[str, Any],
        kind: str,
        trigger_value: float,
        threshold: float,
    ) -> dict[str, Any]:
        """开出处置单并立即执行全部自动步骤。"""

        plan = _PLANS.get(kind)
        if plan is None:
            raise ValidationError("越线类型不合法", kind=kind)
        now = format_moment(self.clock.now())
        steps = [
            InterlockStep(
                order=index,
                key=step.key,
                label=step.label,
                automatic=step.automatic,
            ).to_doc()
            for index, step in enumerate(plan["steps"], start=1)
        ]
        case = InterlockCase(
            id=new_id("slock"),
            boiler_id=str(boiler["id"]),
            brewery_id=str(boiler["brewery_id"]),
            kind=kind,
            tripping=bool(plan["tripping"]),
            trigger_value=round(float(trigger_value), 3),
            threshold=round(float(threshold), 3),
            reason=plan["message"],
            steps=steps,
            raised_at=now,
            updated_at=now,
        )
        document = self.interlocks.put(case.id, case.to_doc())
        # 更高优先级处置单开出时，关闭该炉既有的活跃恢复型处置单，保留留档。
        self._supersede_prior(str(boiler["id"]), case.id)
        self.alarms.raise_alarm(
            brewery_id=str(boiler["brewery_id"]),
            source=f"boiler:{boiler['id']}",
            severity=plan["severity"],
            code=plan["alarm_code"],
            message=f"{boiler.get('code')} {plan['message']}（{trigger_value:.2f}/阈值 {threshold:.2f}）",
            latching=bool(plan["tripping"]),
            context={
                "boiler_id": boiler["id"],
                "case_id": case.id,
                "kind": kind,
                "tripping": bool(plan["tripping"]),
                "trigger_value": round(float(trigger_value), 3),
                "threshold": round(float(threshold), 3),
            },
        )
        self.store.append_event(
            "steam.interlock_opened",
            {
                "case_id": case.id,
                "boiler_id": boiler["id"],
                "kind": kind,
                "tripping": bool(plan["tripping"]),
                "trigger_value": round(float(trigger_value), 3),
                "threshold": round(float(threshold), 3),
            },
        )
        # 立即执行自动步骤（同型已挂牌场景下 _evaluate_limits 已先拦截，这里顺序执行）。
        document = self._run_automatic_steps(document)
        return document

    def _run_automatic_steps(self, case: dict[str, Any]) -> dict[str, Any]:
        """按顺序把自动步骤标记完成，并把执行器动作落到锅炉台账。"""

        boiler = self.boilers.require(case["boiler_id"], label="锅炉")
        now = format_moment(self.clock.now())
        steps = [dict(step) for step in case.get("steps", [])]
        fired_actions: list[str] = []
        for step in steps:
            if not step.get("automatic") or step.get("status") != StepStatus.PENDING.value:
                continue
            effect = _ACTION_EFFECT.get(str(step.get("key")))
            detail: dict[str, Any] = {"automatic": True}
            if effect is not None:
                boiler = self._apply_boiler(str(boiler["id"]), list(effect.items()))
                detail["applied"] = effect
            step["status"] = StepStatus.DONE.value
            step["done_at"] = now
            step["actor"] = "system"
            step["detail"] = detail
            fired_actions.append(str(step.get("key")))
            self.store.append_event(
                "steam.interlock_step",
                {
                    "case_id": case["id"],
                    "boiler_id": case["boiler_id"],
                    "step": step.get("key"),
                    "automatic": True,
                    "applied": effect,
                },
            )

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            patch: list[tuple[str, Any]] = [("steps", steps), ("updated_at", now)]
            if case.get("tripping") and all(
                not s.get("automatic") or s.get("status") == StepStatus.DONE.value for s in steps
            ):
                # 跳闸类自动步执行完即挂牌锁炉，等待人工步确认与复位。
                patch.append(("state", InterlockState.LOCKED.value))
                patch.append(("locked_at", now))
            return merge_documents(document, patch)

        document = self.interlocks.update(str(case["id"]), mutate)
        if case.get("tripping"):
            self._apply_boiler(
                str(case["boiler_id"]),
                [
                    ("state", BoilerState.LOCKED.value),
                    ("tagged", True),
                    ("burner_on", False),
                ],
            )
        if fired_actions:
            self.store.append_event(
                "steam.interlock_actions",
                {"case_id": case["id"], "actions": fired_actions},
            )
        return self.interlocks.require(str(case["id"]))

    def complete_step(self, case_id: str, step_key: str, operator: str) -> dict[str, Any]:
        """操作员确认完成一个人工步骤。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("state") == InterlockState.RESET.value:
                raise ConflictError("处置单已复位", case_id=case_id)
            steps = [dict(step) for step in document.get("steps", [])]
            target = next((step for step in steps if step.get("key") == step_key), None)
            if target is None:
                raise NotFoundError("处置步骤不存在", case_id=case_id, step=step_key)
            if target.get("status") == StepStatus.DONE.value:
                raise ConflictError("该步骤已完成", step=step_key)
            if target.get("automatic"):
                raise SequenceError("自动步骤由系统执行，无需人工确认", step=step_key)
            target["status"] = StepStatus.DONE.value
            target["done_at"] = now
            target["actor"] = clean_operator
            detail = dict(target.get("detail", {}))
            detail["automatic"] = False
            target["detail"] = detail
            return merge_documents(document, [("steps", steps), ("updated_at", now)])

        with self.store.locks.guard(f"interlock:{case_id}"):
            document = self.interlocks.update(case_id, mutate)
        self.store.append_event(
            "steam.interlock_step",
            {"case_id": case_id, "step": step_key, "automatic": False, "operator": clean_operator},
        )
        return document

    def reset_case(self, case_id: str, operator: str) -> dict[str, Any]:
        """跳闸锅炉满足复位条件后人工摘牌复位。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        case = self.interlocks.require(case_id, label="处置单")
        if not case.get("tripping"):
            raise SequenceError("非跳闸处置单无需复位，等待自动恢复即可", case_id=case_id)
        if case.get("state") == InterlockState.RESET.value:
            raise ConflictError("处置单已复位", case_id=case_id)
        pending_manual = [
            str(step.get("key"))
            for step in case.get("steps", [])
            if not step.get("automatic") and step.get("status") != StepStatus.DONE.value
        ]
        if pending_manual:
            raise InterlockError(
                "仍有现场人工步骤未确认，禁止复位",
                case_id=case_id,
                pending=pending_manual,
            )
        boiler = self.boilers.require(str(case["boiler_id"]), label="锅炉")
        self._assert_reset_safe(case, boiler)
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("state", InterlockState.RESET.value),
                    ("reset_at", now),
                    ("reset_by", clean_operator),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"interlock:{case_id}"):
            document = self.interlocks.update(case_id, mutate)
        # 摘牌、恢复待命；主蒸汽阀与排污阀回到安全初始位。
        self._apply_boiler(
            str(case["boiler_id"]),
            [
                ("state", BoilerState.STANDBY.value),
                ("tagged", False),
                ("steam_isolated", False),
                ("blowdown_open", False),
            ],
        )
        self.alarms.raise_alarm(
            brewery_id=str(case["brewery_id"]),
            source=f"boiler:{case['boiler_id']}",
            severity="info",
            code="boiler_trip_reset",
            message=f"{clean_operator} 已复位处置单 {case_id}",
            latching=False,
            context={"case_id": case_id, "boiler_id": case["boiler_id"], "operator": clean_operator},
        )
        self.store.append_event(
            "steam.interlock_reset",
            {"case_id": case_id, "boiler_id": case["boiler_id"], "operator": clean_operator},
        )
        return document

    def _assert_reset_safe(self, case: dict[str, Any], boiler: dict[str, Any]) -> None:
        """复位前再次核对工艺量已在安全区，且有新鲜测点。"""

        kind = case.get("kind")
        reading_at = boiler.get("last_reading_at")
        if reading_at is None:
            raise InterlockError("缺少水位/汽压测点，禁止复位", boiler_id=boiler["id"])
        if self._reading_is_stale(str(reading_at)):
            raise InterlockError("测点已过期，禁止凭旧数据复位", boiler_id=boiler["id"], last=reading_at)
        water = boiler.get("water_pct")
        steam = boiler.get("steam_bar")
        if kind in (InterlockKind.LOW_LOW_WATER.value, InterlockKind.HIGH_HIGH_WATER.value):
            if water is None:
                raise InterlockError("缺少水位测点", boiler_id=boiler["id"])
            w = float(water)
            if not (
                self.settings.steam_low_water_pct < w < self.settings.steam_high_water_pct
            ):
                raise InterlockError(
                    "水位尚未回到正常区，禁止复位",
                    boiler_id=boiler["id"],
                    water_pct=w,
                    safe_low=self.settings.steam_low_water_pct,
                    safe_high=self.settings.steam_high_water_pct,
                )
        if kind == InterlockKind.STEAM_HIGH.value:
            if steam is None or float(steam) >= self.settings.steam_header_high_bar:
                raise InterlockError(
                    "汽压尚未回落到安全区，禁止复位",
                    boiler_id=boiler["id"],
                    steam_bar=steam,
                    safe_below=self.settings.steam_header_high_bar,
                )

    def _auto_recover(self, boiler: dict[str, Any]) -> dict[str, Any] | None:
        """恢复型处置单在工艺量回到安全区后自动闭环。"""

        active_case = self._open_case_for(str(boiler["id"]))
        if active_case is None or active_case.get("tripping"):
            return None
        kind = str(active_case.get("kind"))
        side = _RECOVER_KEY.get(kind)
        water = boiler.get("water_pct")
        if water is None or side is None:
            return None
        w = float(water)
        recovered = (
            (side == "low" and w >= self.settings.steam_low_water_pct)
            or (side == "high" and w <= self.settings.steam_high_water_pct)
        )
        if not recovered:
            return active_case
        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [("state", InterlockState.RECOVERED.value), ("recovered_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"interlock:{active_case['id']}"):
            document = self.interlocks.update(str(active_case["id"]), mutate)
        # 恢复后把补水/排污执行器收回到安全初始位。
        self._apply_boiler(
            str(boiler["id"]),
            [("feedwater_open", False), ("blowdown_open", False)],
        )
        self.store.append_event(
            "steam.interlock_recovered",
            {"case_id": active_case["id"], "boiler_id": boiler["id"], "water_pct": w},
        )
        return document

    def _open_case_for(self, boiler_id: str) -> dict[str, Any] | None:
        items = self.interlocks.find(
            lambda item: item.get("boiler_id") == boiler_id
            and item.get("state") in (InterlockState.ACTIVE.value, InterlockState.LOCKED.value)
        )
        items.sort(key=lambda item: str(item.get("raised_at")))
        return items[-1] if items else None

    def _supersede_prior(self, boiler_id: str, new_case_id: str) -> None:
        """把该炉既有的活跃（恢复型）处置单标记为被更高优先级单取代。"""

        now = format_moment(self.clock.now())
        for item in self.interlocks.find(
            lambda doc: doc.get("boiler_id") == boiler_id
            and doc.get("state") == InterlockState.ACTIVE.value
        ):
            if str(item.get("id")) == new_case_id:
                continue

            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                return merge_documents(
                    document,
                    [
                        ("state", InterlockState.SUPERSEDED.value),
                        ("recovered_at", now),
                        ("updated_at", now),
                    ],
                )

            self.interlocks.update(str(item["id"]), mutate)
            self.store.append_event(
                "steam.interlock_superseded",
                {"case_id": item["id"], "boiler_id": boiler_id, "by_case": new_case_id},
            )

    def _active_case(self, boiler_id: str, tripping_only: bool = False) -> dict[str, Any] | None:
        case = self._open_case_for(boiler_id)
        if case is None:
            return None
        if tripping_only and not case.get("tripping"):
            return None
        return case

    # ------------------------------------------------------------------ 调度

    def start_boiler(self, boiler_id: str, operator: str) -> dict[str, Any]:
        """手动点火（受安全联锁约束）。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        boiler = self.boilers.require(boiler_id, label="锅炉")
        self._assert_can_fire(boiler)
        document = self._apply_boiler(
            boiler_id,
            [
                ("state", BoilerState.FIRING.value),
                ("burner_on", True),
                ("online_since", format_moment(self.clock.now())),
            ],
        )
        self.store.append_event(
            "steam.boiler_command",
            {"boiler_id": boiler_id, "command": "start", "operator": clean_operator},
        )
        return document

    def stop_boiler(self, boiler_id: str, operator: str) -> dict[str, Any]:
        """手动停炉解列（跳闸挂牌的锅炉不能用此命令摘除）。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        boiler = self.boilers.require(boiler_id, label="锅炉")
        if boiler.get("state") == BoilerState.LOCKED.value:
            raise LatchError("锅炉跳闸挂牌中，必须走复位流程", boiler_id=boiler_id)
        document = self._apply_boiler(
            boiler_id,
            [
                ("state", BoilerState.STANDBY.value),
                ("burner_on", False),
                ("online_since", None),
            ],
        )
        self.store.append_event(
            "steam.boiler_command",
            {"boiler_id": boiler_id, "command": "stop", "operator": clean_operator},
        )
        return document

    def dispatch(self, brewery_id: str) -> dict[str, Any]:
        """按当前用汽需求与联箱汽压决定点/停锅炉。

        - 在线容量不足且联箱压力偏低（或无压力测点）时，按优先级补点待命炉；
        - 在线容量明显冗余且压力不低时，停掉最晚并入的锅炉（保留最小冗余）；
        - 跳闸挂牌、水位不在安全区或测点过期的锅炉不参与自动投切。
        """

        demand_view = self.demand(brewery_id)
        target = demand_view["total_kgh"]
        header = self.headers.get(brewery_id)
        pressure = None
        pressure_fresh = False
        if header and header.get("pressure_bar") is not None:
            pressure = float(header["pressure_bar"])
            pressure_fresh = not self._reading_is_stale(str(header.get("updated_at")))

        boilers = self._boilers_of(brewery_id)
        firing = [b for b in boilers if b.get("state") == BoilerState.FIRING.value]
        firing.sort(key=lambda b: (int(b.get("priority", 100)), str(b.get("code"))))
        firing_capacity = sum(float(b.get("rating_kgh", 0.0)) for b in firing)
        pressure_low = pressure is None or not pressure_fresh or pressure < self.settings.steam_header_low_bar
        pressure_high = pressure is not None and pressure_fresh and pressure > self.settings.steam_header_high_bar
        # 供给不吃紧：压力不低于目标（无新鲜测点时保守地不允许自动解列）。
        supply_relaxed = pressure is not None and pressure_fresh and pressure >= self.settings.steam_header_target_bar

        commands: list[dict[str, Any]] = []
        deficit = round(target - firing_capacity, 3)
        started: list[str] = []
        if pressure_low and deficit > 0:
            candidates = [
                b
                for b in boilers
                if b.get("state") == BoilerState.STANDBY.value and self._firing_ready(b)
            ]
            candidates.sort(key=lambda b: (int(b.get("priority", 100)), str(b.get("code"))))
            for candidate in candidates:
                if firing_capacity >= target:
                    break
                fired = self.start_boiler(str(candidate["id"]), "dispatch")
                started.append(str(candidate["id"]))
                firing_capacity += float(candidate["rating_kgh"])
                commands.append(
                    {
                        "boiler_id": candidate["id"],
                        "code": candidate.get("code"),
                        "command": "start",
                        "reason": "demand_shortfall" if pressure is not None else "no_pressure_signal",
                    }
                )

        # 冗余停炉：供给不吃紧、本轮没有刚启动新炉时解列多余锅炉。
        # 以需求为基准做迟滞：压力偏高时停后仍满足需求即可，否则要求 20% 余量，
        # 避免在需求边界上频繁启停。
        stopped: list[str] = []
        if supply_relaxed and not started:
            online = [b for b in self._boilers_of(brewery_id) if b.get("state") == BoilerState.FIRING.value]
            online.sort(key=lambda b: (int(b.get("priority", 100)), str(b.get("code"))), reverse=True)
            required_when_idle = target * (1.0 if pressure_high else 1.2)
            for candidate in online:
                rating = float(candidate["rating_kgh"])
                remaining = firing_capacity - rating
                if remaining < required_when_idle:
                    break
                self.stop_boiler(str(candidate["id"]), "dispatch")
                stopped.append(str(candidate["id"]))
                firing_capacity -= float(candidate["rating_kgh"])
                commands.append(
                    {
                        "boiler_id": candidate["id"],
                        "code": candidate.get("code"),
                        "command": "stop",
                        "reason": "capacity_surplus" if not pressure_high else "header_high",
                    }
                )

        final_capacity = sum(
            float(b.get("rating_kgh", 0.0))
            for b in self._boilers_of(brewery_id)
            if b.get("state") == BoilerState.FIRING.value
        )
        shortfall = round(max(target - final_capacity, 0.0), 3)
        if shortfall > 0:
            self.alarms.raise_alarm(
                brewery_id=brewery_id,
                source=f"steam:{brewery_id}",
                severity="critical",
                code="steam_capacity_shortfall",
                message=f"可供蒸汽 {final_capacity:.0f} kg/h 低于需求 {target:.0f} kg/h",
                latching=False,
                context={
                    "demand_kgh": target,
                    "capacity_kgh": final_capacity,
                    "shortfall_kgh": shortfall,
                    "locked_boilers": [
                        b.get("code")
                        for b in self._boilers_of(brewery_id)
                        if b.get("state") == BoilerState.LOCKED.value
                    ],
                },
            )
        self.store.append_event(
            "steam.dispatch",
            {
                "brewery_id": brewery_id,
                "demand_kgh": target,
                "capacity_kgh": final_capacity,
                "header_bar": pressure,
                "header_fresh": pressure_fresh,
                "started": started,
                "stopped": stopped,
                "shortfall_kgh": shortfall,
            },
        )
        return {
            "brewery_id": brewery_id,
            "demand": demand_view,
            "header_bar": pressure,
            "header_fresh": pressure_fresh,
            "online_capacity_kgh": round(final_capacity, 3),
            "shortfall_kgh": shortfall,
            "commands": commands,
        }

    def _assert_can_fire(self, boiler: dict[str, Any]) -> None:
        if boiler.get("state") == BoilerState.LOCKED.value:
            raise LatchError("锅炉跳闸挂牌中，禁止点火", boiler_id=boiler["id"])
        if boiler.get("state") == BoilerState.FIRING.value:
            raise ConflictError("锅炉已在运行", boiler_id=boiler["id"])
        if self._open_case_for(str(boiler["id"])) is not None:
            raise InterlockError("锅炉存在未闭环的越线处置单，禁止点火", boiler_id=boiler["id"])
        if not self._firing_ready(boiler):
            raise InterlockError(
                "不满足点火条件：需要新鲜且处于安全水位的测点",
                boiler_id=boiler["id"],
                water_pct=boiler.get("water_pct"),
                last_reading_at=boiler.get("last_reading_at"),
                safe_low=self.settings.steam_low_water_pct,
                safe_high=self.settings.steam_high_water_pct,
            )

    def _firing_ready(self, boiler: dict[str, Any]) -> bool:
        """锅炉是否具备自动/手动点火的安全前提。"""

        if boiler.get("state") != BoilerState.STANDBY.value:
            return False
        if self._open_case_for(str(boiler["id"])) is not None:
            return False
        water = boiler.get("water_pct")
        reading_at = boiler.get("last_reading_at")
        if water is None or reading_at is None:
            return False
        if self._reading_is_stale(str(reading_at)):
            return False
        w = float(water)
        return self.settings.steam_low_water_pct <= w <= self.settings.steam_high_water_pct

    # ------------------------------------------------------------------ 查询

    def get_boiler(self, boiler_id: str) -> dict[str, Any]:
        return self.boilers.require(boiler_id, label="锅炉")

    def list_boilers(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        items = self.boilers.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        return sorted(items, key=lambda item: (int(item.get("priority", 100)), str(item.get("code"))))

    def list_consumers(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        items = self.consumers.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        return sorted(items, key=lambda item: str(item.get("code")))

    def list_cases(
        self,
        boiler_id: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self.interlocks.all()
        if boiler_id:
            items = [item for item in items if item.get("boiler_id") == boiler_id]
        if state:
            items = [item for item in items if item.get("state") == state]
        return sorted(items, key=lambda item: str(item.get("raised_at")), reverse=True)

    def get_case(self, case_id: str) -> dict[str, Any]:
        return self.interlocks.require(case_id, label="处置单")

    def overview(self, brewery_id: str) -> dict[str, Any]:
        """热源供给总览，供控制台与首页使用。"""

        boilers = self._boilers_of(brewery_id)
        demand_view = self.demand(brewery_id)
        states: dict[str, int] = {}
        for item in boilers:
            key = str(item.get("state"))
            states[key] = states.get(key, 0) + 1
        online = sum(
            float(item.get("rating_kgh", 0.0)) for item in boilers if item.get("state") == BoilerState.FIRING.value
        )
        header = self.headers.get(brewery_id)
        active_cases = [
            item
            for item in self.interlocks.find(
                lambda doc: doc.get("brewery_id") == brewery_id
                and doc.get("state") in (InterlockState.ACTIVE.value, InterlockState.LOCKED.value)
            )
        ]
        return {
            "boilers": len(boilers),
            "by_state": states,
            "firing_kgh": round(online, 3),
            "demand": demand_view,
            "header": header,
            "active_cases": sorted(
                (
                    {
                        "id": item["id"],
                        "boiler_id": item.get("boiler_id"),
                        "kind": item.get("kind"),
                        "state": item.get("state"),
                        "tripping": item.get("tripping"),
                    }
                    for item in active_cases
                ),
                key=lambda item: str(item["id"]),
            ),
            "thresholds": {
                "low_water_pct": self.settings.steam_low_water_pct,
                "low_low_water_pct": self.settings.steam_low_low_water_pct,
                "high_water_pct": self.settings.steam_high_water_pct,
                "high_high_water_pct": self.settings.steam_high_high_water_pct,
                "header_target_bar": self.settings.steam_header_target_bar,
                "header_low_bar": self.settings.steam_header_low_bar,
                "header_high_bar": self.settings.steam_header_high_bar,
                "header_trip_bar": self.settings.steam_header_trip_bar,
            },
        }

    # ------------------------------------------------------------------ 内部

    def _boilers_of(self, brewery_id: str) -> list[dict[str, Any]]:
        return self.boilers.find(lambda item: item.get("brewery_id") == brewery_id)

    def _safe_water(self, value: Any) -> float:
        return require_number(value, field="water_pct", minimum=0.0, maximum=100.0)

    def _reading_is_stale(self, moment_text: str) -> bool:
        from ..core.clock import elapsed_minutes, parse_moment

        try:
            age_sec = elapsed_minutes(moment_text, format_moment(self.clock.now())) * 60.0
        except ValidationError:
            # 无法解析的时间戳按过期处理，禁止拿不可信测点去投炉。
            return True
        return age_sec > float(self.settings.steam_reading_stale_sec)

    def _apply_boiler(self, boiler_id: str, patch: list[tuple[str, Any]]) -> dict[str, Any]:
        now = format_moment(self.clock.now())
        fields = list(patch)
        fields.append(("updated_at", now))

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(document, fields)

        with self.store.locks.guard(f"boiler:{boiler_id}"):
            return self.boilers.update(boiler_id, mutate)
