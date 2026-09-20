"""热源供给应用服务：把操作请求落到 SteamPlant 并统一写审计。"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..core.validators import require_text
from ..domain.audit import AuditLog
from ..domain.steam import CONSUMER_KINDS, SteamPlant


class SteamService:
    """锅炉登记、需求申报、调度与越线处置的对外门面。"""

    def __init__(self, plant: SteamPlant, audit: AuditLog) -> None:
        self.plant = plant
        self.audit = audit

    def register_boiler(
        self,
        brewery_id: str,
        code: str,
        rating_kgh: float,
        operator: str,
        priority: int = 100,
        water_pct: float | None = None,
    ) -> dict[str, Any]:
        clean_operator = require_text(operator, field="operator", max_length=60)
        boiler = self.plant.register_boiler(
            brewery_id, code, rating_kgh, priority=int(priority), water_pct=water_pct
        )
        self.audit.record(
            brewery_id,
            None,
            clean_operator,
            "steam.boiler_registered",
            {
                "boiler_id": boiler["id"],
                "code": boiler["code"],
                "rating_kgh": boiler["rating_kgh"],
                "priority": boiler["priority"],
            },
        )
        return boiler

    def register_consumer(
        self,
        brewery_id: str,
        code: str,
        name: str,
        demand_kgh: float,
        operator: str,
        kind: str = "other",
    ) -> dict[str, Any]:
        clean_operator = require_text(operator, field="operator", max_length=60)
        if kind not in CONSUMER_KINDS:
            raise ValidationError("用汽设备类型不合法", kind=kind, allowed=list(CONSUMER_KINDS))
        consumer = self.plant.register_consumer(brewery_id, code, name, demand_kgh, kind=kind)
        self.audit.record(
            brewery_id,
            None,
            clean_operator,
            "steam.consumer_registered",
            {
                "consumer_id": consumer["id"],
                "code": consumer["code"],
                "demand_kgh": consumer["demand_kgh"],
                "kind": kind,
            },
        )
        return consumer

    def claim(self, consumer_id: str, actor: str, batch_id: str | None = None) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        consumer = self.plant.claim_consumer(consumer_id, clean_actor, batch_id=batch_id)
        self.audit.record(
            str(consumer["brewery_id"]),
            batch_id,
            clean_actor,
            "steam.consumer_claimed",
            {
                "consumer_id": consumer_id,
                "code": consumer["code"],
                "demand_kgh": consumer["demand_kgh"],
            },
        )
        return consumer

    def release(self, consumer_id: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        consumer = self.plant.release_consumer(consumer_id, clean_actor)
        self.audit.record(
            str(consumer["brewery_id"]),
            None,
            clean_actor,
            "steam.consumer_released",
            {"consumer_id": consumer_id, "code": consumer["code"]},
        )
        return consumer

    def report_header(self, brewery_id: str, pressure_bar: float, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        header = self.plant.report_header(brewery_id, pressure_bar)
        self.audit.record(
            brewery_id,
            None,
            clean_actor,
            "steam.header_reported",
            {"pressure_bar": header["pressure_bar"]},
        )
        return header

    def report_boiler(
        self,
        boiler_id: str,
        actor: str,
        water_pct: float | None = None,
        steam_bar: float | None = None,
    ) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        result = self.plant.report_boiler(boiler_id, water_pct=water_pct, steam_bar=steam_bar)
        boiler = result["boiler"]
        case = result.get("interlock")
        if case is not None:
            self.audit.record(
                str(boiler["brewery_id"]),
                None,
                clean_actor,
                "steam.interlock_engaged",
                {
                    "boiler_id": boiler_id,
                    "case_id": case["id"],
                    "kind": case["kind"],
                    "state": case["state"],
                    "tripping": case["tripping"],
                    "water_pct": boiler.get("water_pct"),
                    "steam_bar": boiler.get("steam_bar"),
                },
            )
        return result

    def dispatch(self, brewery_id: str, actor: str) -> dict[str, Any]:
        clean_actor = require_text(actor, field="actor", max_length=60)
        report = self.plant.dispatch(brewery_id)
        for command in report["commands"]:
            self.audit.record(
                brewery_id,
                None,
                clean_actor,
                "steam.boiler_dispatched",
                {
                    "boiler_id": command["boiler_id"],
                    "command": command["command"],
                    "reason": command["reason"],
                },
            )
        self.audit.record(
            brewery_id,
            None,
            clean_actor,
            "steam.dispatched",
            {
                "demand_kgh": report["demand"]["total_kgh"],
                "capacity_kgh": report["online_capacity_kgh"],
                "shortfall_kgh": report["shortfall_kgh"],
                "header_bar": report["header_bar"],
                "commands": len(report["commands"]),
            },
        )
        return report

    def start_boiler(self, boiler_id: str, operator: str) -> dict[str, Any]:
        clean_operator = require_text(operator, field="operator", max_length=60)
        boiler = self.plant.start_boiler(boiler_id, clean_operator)
        self.audit.record(
            str(boiler["brewery_id"]),
            None,
            clean_operator,
            "steam.boiler_started",
            {"boiler_id": boiler_id, "code": boiler["code"]},
        )
        return boiler

    def stop_boiler(self, boiler_id: str, operator: str) -> dict[str, Any]:
        clean_operator = require_text(operator, field="operator", max_length=60)
        boiler = self.plant.stop_boiler(boiler_id, clean_operator)
        self.audit.record(
            str(boiler["brewery_id"]),
            None,
            clean_operator,
            "steam.boiler_stopped",
            {"boiler_id": boiler_id, "code": boiler["code"]},
        )
        return boiler

    def complete_step(self, case_id: str, step_key: str, operator: str) -> dict[str, Any]:
        clean_operator = require_text(operator, field="operator", max_length=60)
        case = self.plant.complete_step(case_id, step_key, clean_operator)
        self.audit.record(
            str(case["brewery_id"]),
            None,
            clean_operator,
            "steam.interlock_step_done",
            {"case_id": case_id, "step": step_key},
        )
        return case

    def reset_case(self, case_id: str, operator: str) -> dict[str, Any]:
        clean_operator = require_text(operator, field="operator", max_length=60)
        case = self.plant.reset_case(case_id, clean_operator)
        self.audit.record(
            str(case["brewery_id"]),
            None,
            clean_operator,
            "steam.interlock_reset",
            {"case_id": case_id, "boiler_id": case["boiler_id"]},
        )
        return case

    def get_boiler(self, boiler_id: str) -> dict[str, Any]:
        return self.plant.get_boiler(boiler_id)

    def boiler_snapshot(self, boiler_id: str) -> dict[str, Any]:
        boiler = self.plant.get_boiler(boiler_id)
        cases = self.plant.list_cases(boiler_id=boiler_id)
        return {"boiler": boiler, "cases": cases}

    def demand(self, brewery_id: str) -> dict[str, Any]:
        return self.plant.demand(brewery_id)

    def overview(self, brewery_id: str) -> dict[str, Any]:
        return self.plant.overview(brewery_id)

    def list_boilers(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        return self.plant.list_boilers(brewery_id=brewery_id)

    def list_consumers(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        return self.plant.list_consumers(brewery_id=brewery_id)

    def list_cases(self, boiler_id: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        return self.plant.list_cases(boiler_id=boiler_id, state=state)

    def get_case(self, case_id: str) -> dict[str, Any]:
        return self.plant.get_case(case_id)
