"""热源供给：用汽需求调度、水位/汽压越线安全处置与留档。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import InterlockError, LatchError

from .helpers import StepClock, make_app


class SteamControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.svc = self.app.registry.steam_service
        self.plant = self.app.registry.steam
        self.brewery = self.app.registry._default_brewery_id()

    def _boilers(self):
        by_code = {b["code"]: b for b in self.svc.list_boilers(self.brewery)}
        return by_code["BL-01"], by_code["BL-02"]

    def _consumer(self, code: str) -> str:
        for item in self.svc.list_consumers(self.brewery):
            if item["code"] == code:
                return str(item["id"])
        raise KeyError(code)

    # ---------------------------------------------------------------- 调度

    def test_demand_starts_enough_boilers_for_mash_plus_boil(self) -> None:
        b1, b2 = self._boilers()
        self.svc.claim(self._consumer("mash"), "op")
        self.svc.claim(self._consumer("boil"), "op")
        # 叠加 800 kg/h：单炉 600 不够，低联箱压力下两炉都应并入。
        self.svc.report_header(self.brewery, 0.45, "op")
        report = self.svc.dispatch(self.brewery, "op")
        self.assertEqual(1200.0, report["online_capacity_kgh"])
        self.assertEqual(0.0, report["shortfall_kgh"])
        commands = sorted(c["code"] for c in report["commands"])
        self.assertEqual(["BL-01", "BL-02"], commands)
        self.assertEqual("firing", self.svc.get_boiler(b1["id"])["state"])
        self.assertEqual("firing", self.svc.get_boiler(b2["id"])["state"])

    def test_single_consumer_uses_one_boiler(self) -> None:
        self.svc.claim(self._consumer("boil"), "op")  # 500 kg/h
        self.svc.report_header(self.brewery, 0.45, "op")
        report = self.svc.dispatch(self.brewery, "op")
        self.assertEqual(600.0, report["online_capacity_kgh"])
        started = [c for c in report["commands"] if c["command"] == "start"]
        self.assertEqual(1, len(started))
        # 优先级 10 的 1 号炉先并入。
        self.assertEqual("BL-01", started[0]["code"])

    def test_release_then_rebalance_stops_surplus(self) -> None:
        b1, b2 = self._boilers()
        self.svc.claim(self._consumer("mash"), "op")
        self.svc.claim(self._consumer("boil"), "op")
        self.svc.report_header(self.brewery, 0.45, "op")
        self.svc.dispatch(self.brewery, "op")
        self.assertEqual("firing", self.svc.get_boiler(b2["id"])["state"])
        # 只剩煮沸 500，一炉即够；在压力不低时解列多余锅炉。
        self.svc.release(self._consumer("mash"), "op")
        self.svc.report_header(self.brewery, 0.72, "op")
        report = self.svc.dispatch(self.brewery, "op")
        stopped = [c for c in report["commands"] if c["command"] == "stop"]
        self.assertTrue(stopped)
        self.assertEqual(600.0, report["online_capacity_kgh"])

    def test_capacity_shortfall_raises_critical_alarm(self) -> None:
        # 两台都跳闸挂牌后，需求无法被满足。
        b1, b2 = self._boilers()
        self.plant.report_boiler(b1["id"], water_pct=8.0)
        self.plant.report_boiler(b2["id"], water_pct=8.0)
        self.svc.claim(self._consumer("boil"), "op")
        self.svc.report_header(self.brewery, 0.4, "op")
        self.svc.dispatch(self.brewery, "op")
        active = self.app.registry.alarms.list_alarms(status="active")
        codes = {a["code"] for a in active}
        self.assertIn("steam_capacity_shortfall", codes)

    def test_stale_or_missing_level_blocks_auto_start(self) -> None:
        # 注册一台没有水位测点的新炉：即使缺容量也不允许自动点。
        self.plant.register_boiler(self.brewery, "BL-99", 800.0, priority=1)
        b1, b2 = self._boilers()
        # 先让原两炉跳闸，留出缺口。
        self.plant.report_boiler(b1["id"], water_pct=8.0)
        self.plant.report_boiler(b2["id"], water_pct=8.0)
        self.svc.claim(self._consumer("boil"), "op")
        self.svc.report_header(self.brewery, 0.4, "op")
        report = self.svc.dispatch(self.brewery, "op")
        self.assertGreater(report["shortfall_kgh"], 0.0)
        new = next(b for b in self.plant.list_boilers(self.brewery) if b["code"] == "BL-99")
        self.assertEqual("standby", new["state"])

    def test_stale_reading_also_blocks_manual_start(self) -> None:
        b1, _ = self._boilers()
        self.clock.advance(5)  # 超过默认 120 秒新鲜窗口
        with self.assertRaises(InterlockError):
            self.svc.start_boiler(b1["id"], "op")

    # ------------------------------------------------------------ 越线处置

    def test_low_low_water_trip_runs_fixed_order_and_locks(self) -> None:
        _, b2 = self._boilers()
        result = self.svc.report_boiler(b2["id"], "op", water_pct=9.0)
        case = result["interlock"]
        self.assertEqual("low_low_water", case["kind"])
        self.assertTrue(case["tripping"])
        self.assertEqual("locked", case["state"])
        keys = [s["key"] for s in case["steps"]]
        # 切燃料必须先于停燃烧器/给水隔离/挂牌等。
        self.assertEqual(
            ["fuel_cut", "burner_stop", "feedwater_close", "blowdown_open", "steam_isolate", "tag", "inspect"],
            keys,
        )
        automatic = [s for s in case["steps"] if s["automatic"]]
        self.assertTrue(all(s["status"] == "done" for s in automatic))
        self.assertTrue(all(s["actor"] == "system" for s in automatic))
        self.assertTrue(all(s["status"] == "pending" for s in case["steps"] if not s["automatic"]))
        boiler = self.svc.get_boiler(b2["id"])
        self.assertEqual("locked", boiler["state"])
        self.assertTrue(boiler["tagged"])
        self.assertFalse(boiler["burner_on"])
        self.assertTrue(boiler["steam_isolated"])
        self.assertTrue(boiler["blowdown_open"])
        self.assertFalse(boiler["feedwater_open"])

    def test_locked_boiler_cannot_be_started_or_stopped(self) -> None:
        _, b2 = self._boilers()
        self.plant.report_boiler(b2["id"], water_pct=9.0)
        with self.assertRaises(LatchError):
            self.svc.start_boiler(b2["id"], "op")
        with self.assertRaises(LatchError):
            self.svc.stop_boiler(b2["id"], "op")

    def test_reset_requires_manual_steps_and_safe_level(self) -> None:
        _, b2 = self._boilers()
        case = self.plant.report_boiler(b2["id"], water_pct=9.0)["interlock"]
        # 人工步未确认不能复位。
        with self.assertRaises(InterlockError):
            self.svc.reset_case(case["id"], "op")
        for step in case["steps"]:
            if not step["automatic"]:
                self.svc.complete_step(case["id"], step["key"], "op")
        # 水位仍在危险区不能复位。
        self.svc.report_boiler(b2["id"], "op", water_pct=20.0)
        with self.assertRaises(InterlockError):
            self.svc.reset_case(case["id"], "op")
        # 回到正常水位后可以复位摘牌。
        self.svc.report_boiler(b2["id"], "op", water_pct=55.0)
        reset = self.svc.reset_case(case["id"], "op")
        self.assertEqual("reset", reset["state"])
        boiler = self.svc.get_boiler(b2["id"])
        self.assertEqual("standby", boiler["state"])
        self.assertFalse(boiler["tagged"])

    def test_high_steam_pressure_trip(self) -> None:
        _, b2 = self._boilers()
        case = self.plant.report_boiler(b2["id"], steam_bar=1.05)["interlock"]
        self.assertEqual("steam_high", case["kind"])
        self.assertEqual("locked", case["state"])
        boiler = self.svc.get_boiler(b2["id"])
        self.assertFalse(boiler["burner_on"])
        self.assertTrue(boiler["steam_isolated"])
        # 汽压未回落不能复位。
        for step in case["steps"]:
            if not step["automatic"]:
                self.svc.complete_step(case["id"], step["key"], "op")
        self.plant.report_boiler(b2["id"], steam_bar=0.95)
        with self.assertRaises(InterlockError):
            self.svc.reset_case(case["id"], "op")
        self.plant.report_boiler(b2["id"], steam_bar=0.7)
        self.assertEqual("reset", self.svc.reset_case(case["id"], "op")["state"])

    def test_low_water_opens_feed_and_auto_recovers(self) -> None:
        b1, _ = self._boilers()
        case = self.plant.report_boiler(b1["id"], water_pct=22.0)["interlock"]
        self.assertEqual("low_water", case["kind"])
        self.assertFalse(case["tripping"])
        self.assertTrue(self.svc.get_boiler(b1["id"])["feedwater_open"])
        # 水位恢复后处置单自动闭环，执行器复位。
        result = self.plant.report_boiler(b1["id"], water_pct=50.0)
        self.assertEqual("recovered", result["interlock"]["state"])
        self.assertFalse(self.svc.get_boiler(b1["id"])["feedwater_open"])
        # 自动恢复后该炉可以被正常点火。
        self.svc.claim(self._consumer("boil"), "op")
        self.svc.report_header(self.brewery, 0.45, "op")
        self.svc.dispatch(self.brewery, "op")
        self.assertEqual("firing", self.svc.get_boiler(b1["id"])["state"])

    def test_low_water_superseded_by_low_low_trip(self) -> None:
        b1, _ = self._boilers()
        recover_case = self.plant.report_boiler(b1["id"], water_pct=22.0)["interlock"]
        self.assertEqual("low_water", recover_case["kind"])
        # 水位继续跌到低低：新开跳闸单，且锅炉挂牌。
        trip_case = self.plant.report_boiler(b1["id"], water_pct=9.0)["interlock"]
        self.assertEqual("low_low_water", trip_case["kind"])
        self.assertEqual("locked", trip_case["state"])
        self.assertEqual("locked", self.svc.get_boiler(b1["id"])["state"])
        # 原低水位单被标记为被取代但仍保留可追溯；活跃处置单只剩跳闸单。
        active = self.plant.list_cases(boiler_id=b1["id"], state="active")
        self.assertEqual(0, len(active))
        superseded = self.plant.list_cases(boiler_id=b1["id"], state="superseded")
        self.assertEqual(1, len(superseded))
        locked = self.plant.list_cases(boiler_id=b1["id"], state="locked")
        self.assertEqual(1, len(locked))

    def test_no_interlock_inside_safe_band(self) -> None:
        b1, _ = self._boilers()
        self.assertIsNone(self.plant.report_boiler(b1["id"], water_pct=50.0, steam_bar=0.7)["interlock"])

    # ------------------------------------------------------------ 留档

    def test_handling_process_is_recorded(self) -> None:
        _, b2 = self._boilers()
        case = self.svc.report_boiler(b2["id"], "op", water_pct=9.0)["interlock"]
        for step in case["steps"]:
            if not step["automatic"]:
                self.svc.complete_step(case["id"], step["key"], "op")
        self.svc.report_boiler(b2["id"], "op", water_pct=55.0)
        self.svc.reset_case(case["id"], "op")
        actions = [e["action"] for e in self.app.registry.audit.history(limit=0)]
        self.assertIn("steam.interlock_engaged", actions)
        self.assertIn("steam.interlock_step_done", actions)
        self.assertIn("steam.interlock_reset", actions)
        # append-only 日志里保留了自动步骤事实。
        kinds = [ev.get("kind") for ev in self.app.registry.store.events(limit=500)]
        self.assertIn("steam.interlock_step", kinds)
        self.assertIn("steam.interlock_opened", kinds)
        self.assertIn("steam.interlock_reset", kinds)

    def test_dispatch_commands_are_audited(self) -> None:
        self.svc.claim(self._consumer("boil"), "op")
        self.svc.report_header(self.brewery, 0.45, "op")
        self.svc.dispatch(self.brewery, "op")
        actions = [e["action"] for e in self.app.registry.audit.history(limit=0)]
        self.assertIn("steam.boiler_dispatched", actions)
        self.assertIn("steam.dispatched", actions)

    def test_case_persists_across_restart(self) -> None:
        _, b2 = self._boilers()
        case = self.plant.report_boiler(b2["id"], water_pct=9.0)["interlock"]
        case_id = case["id"]
        data_dir = self.app.settings.data_dir
        self.app.close()
        reopened = make_app(data_dir=data_dir, clock=self.clock)
        stored = reopened.registry.steam_service.get_case(case_id)
        self.assertEqual("locked", stored["state"])
        self.assertEqual("low_low_water", stored["kind"])
        reopened.close()


if __name__ == "__main__":
    unittest.main()
