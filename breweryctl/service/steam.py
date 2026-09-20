"""蒸汽热源调度服务：把操作员/上层系统请求落到领域组件并写审计。"""

from __future__ import annotations

from typing import Any

from ..core.validators import require_number, require_text
from ..domain.audit import AuditLog
from ..domain.steam import SteamPlant


class SteamService:
    """封装热源域的操作入口，所有关键动作都写不可变审计。"""

    def __init__(self, plant: SteamPlant, audit: AuditLog) -> None:
        self.plant = plant
        self.audit = audit

    def register_boiler(self, brewery_id: str, index: int, header_id: str | None = None) -> dict[str, Any]:
        document = self.plant.register_boiler(brewery_id, index, header_id=header_id)
        self.audit.record(
            str(document.get("brewery_id")),
            None,
            "steam-plant",
            "steam.boiler_registered",
            {"boiler_id": document["id"], "code": document["code"], "header_id": document["header_id"]},
        )
        return document

    def register_user(self, brewery_id: str, code: str, header_id: str | None = None) -> dict[str, Any]:
        document = self.plant.register_user(brewery_id, code, header_id=header_id)
        self.audit.record(
            str(document.get("brewery_id")),
            None,
            "steam-plant",
            "steam.user_registered",
            {"user_id": document["id"], "code": document["code"], "header_id": document["header_id"]},
        )
        return document

    def report_boiler(
        self,
        boiler_id: str,
        pressure_bar: float,
        water_level_pct: float,
        flame_on: bool,
        actor: str = "scada",
    ) -> dict[str, Any]:
        document = self.plant.report_boiler(boiler_id, pressure_bar, water_level_pct, flame_on)
        case = self.plant.active_case(boiler_id)
        action = "steam.boiler_locked_out" if case else "steam.boiler_report"
        detail: dict[str, Any] = {
            "boiler_id": boiler_id,
            "pressure_bar": document.get("pressure_bar"),
            "water_level_pct": document.get("water_level_pct"),
            "flame_on": document.get("flame_on"),
            "state": document.get("state"),
            "feed_pump_on": document.get("feed_pump_on"),
        }
        if case:
            detail["case_id"] = case["id"]
            detail["case_code"] = case["code"]
        self.audit.record(str(document.get("brewery_id")), None, actor, action, detail)
        return document

    def report_header_pressure(self, header_id: str, pressure_bar: float, actor: str = "scada") -> dict[str, Any]:
        document = self.plant.report_header_pressure(header_id, pressure_bar)
        self.audit.record(
            str(document.get("brewery_id")),
            None,
            actor,
            "steam.header_report",
            {
                "header_id": header_id,
                "pressure_bar": document.get("pressure_bar"),
                "drop_bar_min": document.get("pressure_drop_bar_min"),
            },
        )
        return document

    def set_demand(
        self,
        user_id: str,
        state: str,
        load_pct: float | None = None,
        batch_id: str | None = None,
        actor: str = "brewing",
    ) -> dict[str, Any]:
        document = self.plant.set_demand(user_id, state, load_pct=load_pct, batch_id=batch_id)
        self.audit.record(
            str(document.get("brewery_id")),
            batch_id,
            actor,
            "steam.demand_set",
            {"user_id": user_id, "state": state, "load_pct": document.get("load_pct")},
        )
        return document

    def dispatch(self, header_id: str, actor: str = "scheduler") -> dict[str, Any]:
        view = self.plant.dispatch(header_id)
        self.audit.record(
            str(view["header"].get("brewery_id")),
            None,
            actor,
            "steam.dispatch",
            {
                "header_id": header_id,
                "pressure_bar": view.get("pressure_bar"),
                "demand_load_pct": view.get("demand_load_pct"),
                "peak": view.get("peak"),
                "actions": view.get("actions"),
            },
        )
        return view

    def start_boiler(self, boiler_id: str, operator: str) -> dict[str, Any]:
        document = self.plant.start_boiler(boiler_id)
        self.audit.record(
            str(document.get("brewery_id")),
            None,
            require_text(operator, field="operator", max_length=60),
            "steam.boiler_started",
            {"boiler_id": boiler_id, "role": document.get("role")},
        )
        return document

    def shutdown_boiler(self, boiler_id: str, operator: str) -> dict[str, Any]:
        document = self.plant.shutdown_boiler(boiler_id)
        self.audit.record(
            str(document.get("brewery_id")),
            None,
            require_text(operator, field="operator", max_length=60),
            "steam.boiler_shutdown",
            {"boiler_id": boiler_id},
        )
        return document

    def manual_trip(self, boiler_id: str, operator: str, reason_code: str, note: str = "") -> dict[str, Any]:
        case = self.plant.manual_trip(boiler_id, operator, reason_code=reason_code, note=note)
        self.audit.record(
            str(case.get("brewery_id")),
            None,
            require_text(operator, field="operator", max_length=60),
            "steam.manual_trip",
            {"boiler_id": boiler_id, "case_id": case["id"], "reason_code": case["code"], "note": note},
        )
        return case

    def acknowledge_case(self, case_id: str, operator: str) -> dict[str, Any]:
        document = self.plant.acknowledge_case(case_id, operator)
        self.audit.record(
            str(document.get("brewery_id")),
            None,
            require_text(operator, field="operator", max_length=60),
            "steam.case_acknowledged",
            {"case_id": case_id, "boiler_id": document["boiler_id"], "code": document["code"]},
        )
        return document

    def reset_case(self, case_id: str, operator: str, note: str) -> dict[str, Any]:
        document = self.plant.reset_case(case_id, operator, note)
        self.audit.record(
            str(document.get("brewery_id")),
            None,
            require_text(operator, field="operator", max_length=60),
            "steam.case_reset",
            {"case_id": case_id, "boiler_id": document["boiler_id"], "code": document["code"], "note": note},
        )
        return document

    def case_detail(self, case_id: str) -> dict[str, Any]:
        cases = self.plant.list_cases()
        for item in cases:
            if item.get("id") == case_id:
                return item
        from ..core.errors import NotFoundError

        raise NotFoundError("安全处置事件不存在", case_id=case_id)

    def list_cases(self, boiler_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return self.plant.list_cases(boiler_id=boiler_id, status=status)

    def header_snapshot(self, header_id: str) -> dict[str, Any]:
        return self.plant.header_snapshot(header_id)

    def purge_status(self, boiler_id: str) -> dict[str, Any]:
        return self.plant.purge_status(boiler_id)

    def summary(self) -> dict[str, Any]:
        return self.plant.summary()

    def default_header_id(self, brewery_id: str) -> str:
        header = self.plant._resolve_header(None, brewery_id)
        return str(header["id"])
