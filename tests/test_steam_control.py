"""蒸汽热源：母管调度、水位/汽压/火焰联锁与安全处置留痕。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import InterlockError, SequenceError

from .helpers import StepClock, make_app


class SteamPlantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.svc = self.app.registry.steam_service
        self.plant = self.app.registry.steam
        snapshot = self.svc.header_snapshot(self.plant.headers.keys()[0])
        self.header_id = snapshot["header"]["id"]
        self.boilers = {item["code"]: item["id"] for item in snapshot["boilers"]}
        self.users = {item["code"]: item["id"] for item in snapshot["users"]}
        self.b1 = self.boilers["B-01"]
        self.b2 = self.boilers["B-02"]
        self.mash = self.users["MASH"]
        self.boil = self.users["BOIL"]

    def _readings(self, boiler_id: str, pressure: float = 0.72, level: float = 65.0, flame: bool = False) -> None:
        self.svc.report_boiler(boiler_id, pressure, level, flame)

    def _healthy(self) -> None:
        self._readings(self.b1, level=70.0)
        self._readings(self.b2, level=70.0)

    def _fire_one(self, boiler_id: str) -> None:
        self.svc.set_demand(self.mash, "demand", 40)
        self.svc.report_header_pressure(self.header_id, 0.70)
        self.svc.dispatch(self.header_id)
        self.clock.advance(1)
        self._readings(boiler_id, flame=True)
        self.svc.dispatch(self.header_id)

    def test_demand_starts_single_boiler(self) -> None:
        self._healthy()
        self.svc.set_demand(self.mash, "demand", 55)
        self.svc.report_header_pressure(self.header_id, 0.70)
        view = self.svc.dispatch(self.header_id)
        self.assertEqual([("start", "lead")], [(a["action"], a.get("role")) for a in view["actions"]])

    def test_peak_demand_stages_second_boiler(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.set_demand(self.boil, "peak", 100)
        view = self.svc.dispatch(self.header_id)
        self.assertEqual(
            [("start", "lag")], [(a["action"], a.get("role")) for a in view["actions"]]
        )

    def test_low_pressure_stages_after_delay_only(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.report_header_pressure(self.header_id, 0.55)
        view = self.svc.dispatch(self.header_id)
        self.assertEqual([], [a for a in view["actions"] if a["action"] == "start"])
        self.clock.advance(3)
        self.svc.report_header_pressure(self.header_id, 0.55)
        view = self.svc.dispatch(self.header_id)
        self.assertTrue(any(a["action"] == "start" and a.get("role") == "lag" for a in view["actions"]))

    def test_pressure_recovery_stops_lag_with_purge(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.set_demand(self.boil, "demand", 60)
        self.svc.dispatch(self.header_id)
        self.clock.advance(1)
        self._readings(self.b2, flame=True)
        self.svc.dispatch(self.header_id)
        self.assertEqual(2, len([b for b in self.plant.boilers.all() if b["state"] == "firing"]))

        self.svc.report_header_pressure(self.header_id, 0.82)
        view = self.svc.dispatch(self.header_id)
        self.assertTrue(any(a["action"] == "shutdown" for a in view["actions"]))
        stopped = [b for b in self.plant.boilers.all() if b["state"] == "purging"]
        self.assertEqual(1, len(stopped))
        self.clock.advance(6)
        view = self.svc.dispatch(self.header_id)
        self.assertTrue(any(a["action"] == "purge_complete" for a in view["actions"]))

    def test_low_low_water_trips_mft_and_locks(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        document = self.svc.report_boiler(self.b1, 0.72, 25.0, True)
        self.assertEqual("locked_out", document["state"])
        self.assertFalse(document["header_valve_open"])
        self.assertFalse(document["feed_pump_on"])
        self.assertEqual(0.0, document["firing_rate_pct"])

        case = self.plant.active_case(self.b1)
        assert case is not None
        self.assertEqual("low_low_water", case["code"])
        self.assertEqual("active", case["status"])
        names = [step["name"] for step in case["steps"]]
        self.assertEqual("切断燃料供给（MFT）", names[0])
        self.assertTrue(any("给水泵" in name for name in names))
        self.assertTrue(all(step["result"] == "executed" for step in case["steps"]))

        alarms = self.plant.alarms.list_alarms(status="active")
        self.assertTrue(any(a["code"] == "steam_low_low_water_trip" and a["latching"] for a in alarms))
        with self.assertRaises(InterlockError):
            self.svc.start_boiler(self.b1, "op")

    def test_overpressure_trips_boiler(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.report_boiler(self.b1, 0.95, 65.0, True)
        case = self.plant.active_case(self.b1)
        assert case is not None
        self.assertEqual("overpressure", case["code"])
        names = [step["name"] for step in case["steps"]]
        self.assertTrue(any("安全阀" in name for name in names))

    def test_header_high_pressure_shuts_down_firing_boilers(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.report_header_pressure(self.header_id, 0.86)
        view = self.svc.dispatch(self.header_id)
        self.assertTrue(any(a["action"] == "shutdown" for a in view["actions"]))

    def test_flame_failure_requires_grace_and_post_purge(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        # 刚熄火尚在宽限期内，不误跳
        self.svc.report_boiler(self.b1, 0.70, 65.0, False)
        self.assertIsNone(self.plant.active_case(self.b1))
        self.clock.advance(1)
        self.svc.report_boiler(self.b1, 0.70, 65.0, False)
        case = self.plant.active_case(self.b1)
        assert case is not None
        self.assertEqual("flame_failure", case["code"])
        self.assertIsNotNone(self.plant.get_boiler(self.b1)["purge_due_at"])

    def test_ignition_failure_trips_starting_boiler(self) -> None:
        self._healthy()
        self.svc.set_demand(self.mash, "demand", 40)
        self.svc.report_header_pressure(self.header_id, 0.70)
        self.svc.dispatch(self.header_id)
        # 不回报火焰，超过点火宽限后调度判定点火失败
        self.clock.advance(1)
        self._readings(self.b1, flame=False)
        view = self.svc.dispatch(self.header_id)
        self.assertTrue(any(a["action"] == "ignition_failure_trip" for a in view["actions"]))
        self.assertEqual("locked_out", self.plant.get_boiler(self.b1)["state"])

    def test_locked_case_reset_sequence(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.report_boiler(self.b1, 0.72, 25.0, True)
        case = self.plant.active_case(self.b1)
        assert case is not None
        case_id = str(case["id"])

        # 未确认不能复位
        with self.assertRaises(InterlockError):
            self.svc.reset_case(case_id, "op", "恢复")
        self.svc.acknowledge_case(case_id, "op")
        # 水位仍低不能复位
        self.svc.report_boiler(self.b1, 0.50, 40.0, False)
        with self.assertRaises(InterlockError):
            self.svc.reset_case(case_id, "op", "恢复")
        # 水位正常 + 压力回落方可复位
        self.svc.report_boiler(self.b1, 0.50, 60.0, False)
        reset = self.svc.reset_case(case_id, "op", "现场确认补水正常")
        self.assertEqual("reset", reset["status"])
        self.assertEqual("standby", self.plant.get_boiler(self.b1)["state"])
        # 闩锁告警随复位解除
        active = self.plant.alarms.list_alarms(status="active")
        self.assertFalse(any(a["code"] == "steam_low_low_water_trip" for a in active))

    def test_start_permissive_requires_safe_level(self) -> None:
        self.svc.report_boiler(self.b1, 0.72, 40.0, False)
        with self.assertRaises(InterlockError):
            self.svc.start_boiler(self.b1, "op")
        self.svc.report_boiler(self.b1, 0.72, 65.0, False)
        document = self.svc.start_boiler(self.b1, "op")
        self.assertEqual("starting", document["state"])

    def test_low_water_warning_auto_resolves_and_caps_load(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.report_boiler(self.b1, 0.55, 45.0, True)
        self.assertTrue(self.plant.get_boiler(self.b1)["feed_pump_on"])
        codes = [a["code"] for a in self.plant.alarms.list_alarms(status="active")]
        self.assertIn("steam_low_water", codes)
        # 恢复后泵停、告警自动解除
        self.svc.report_boiler(self.b1, 0.70, 70.0, True)
        self.assertFalse(self.plant.get_boiler(self.b1)["feed_pump_on"])
        codes = [a["code"] for a in self.plant.alarms.list_alarms(status="active")]
        self.assertNotIn("steam_low_water", codes)

    def test_manual_trip_records_standard_sequence(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        case = self.svc.manual_trip(self.b1, "班长", reason_code="overpressure", note="听到异常声响")
        self.assertEqual("overpressure", case["code"])
        self.assertTrue(case["readings"]["manual"])
        self.assertEqual("班长", case["readings"]["operator"])

    def test_audit_trail_records_dispatch_and_trip(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.report_boiler(self.b1, 0.72, 25.0, True)
        entries = self.app.registry.audit.history(limit=0)
        actions = {entry["action"] for entry in entries}
        self.assertIn("steam.dispatch", actions)
        self.assertIn("steam.boiler_locked_out", actions)
        locked = [e for e in entries if e["action"] == "steam.boiler_locked_out"][-1]
        self.assertIn("case_code", locked["detail"])

    def test_dispatch_promotes_lag_when_lead_trips(self) -> None:
        self._healthy()
        self._fire_one(self.b1)
        self.svc.set_demand(self.boil, "peak", 100)
        self.svc.dispatch(self.header_id)
        self.clock.advance(1)
        self._readings(self.b2, flame=True)
        self.svc.dispatch(self.header_id)

        self.svc.report_boiler(self.b1, 0.72, 25.0, True)
        self.svc.dispatch(self.header_id)
        header = self.plant.get_header(self.header_id)
        self.assertEqual(self.b2, header["lead_boiler_id"])
        self.assertEqual("lead", self.plant.get_boiler(self.b2)["role"])

    def test_shutdown_from_illegal_state_rejected(self) -> None:
        self._healthy()
        with self.assertRaises(SequenceError):
            self.svc.shutdown_boiler(self.b1, "op")


if __name__ == "__main__":
    unittest.main()
