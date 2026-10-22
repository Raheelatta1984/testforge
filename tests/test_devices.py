"""The Devices tab catalogue, device-context runs and run validation.

No browser is needed here: the web legs are exercised through FakePlaywright
(context kwargs must reach new_context verbatim) and the simulator leg runs
the real app/mobile_runner.py simulator.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from tests.bootstrap import ensure_test_env
ensure_test_env()
from tests.fakes import FakePage, FakePlaywright
from app import devices
from app.devices_routes import TARGETS, _web_flows, run_simulator_script


class DeviceCatalogue(unittest.TestCase):
    def test_DEV_01_every_profile_carries_viewport_dpr_ua_and_flags(self):
        self.assertEqual([group for group, _ in devices.GROUPS],
                         ["handheld", "android", "ios", "other", "desktop"])
        self.assertEqual(len(devices.DEVICES), 14)
        seen = set()
        for profile in devices.DEVICES:
            self.assertNotIn(profile["id"], seen)
            seen.add(profile["id"])
            viewport = profile["viewport"]
            self.assertIsInstance(viewport["width"], int)
            self.assertIsInstance(viewport["height"], int)
            self.assertGreater(viewport["width"], 0)
            self.assertGreater(viewport["height"], 0)
            self.assertGreater(profile["device_scale_factor"], 0)
            self.assertTrue(profile["user_agent"])
            self.assertIsInstance(profile["is_mobile"], bool)
            self.assertIsInstance(profile["has_touch"], bool)
        groups = {profile["group"] for profile in devices.DEVICES}
        self.assertEqual(groups, {"handheld", "android", "ios", "other", "desktop"})
        # The d-pad feature phone is the deliberate no-touch outlier.
        kaios = devices.get("nokia-8110-kaios")
        self.assertTrue(kaios["is_mobile"])
        self.assertFalse(kaios["has_touch"])

    def test_DEV_02_context_kwargs_match_the_catalogue_and_fail_closed(self):
        kwargs = devices.context_kwargs("iphone-15")
        self.assertEqual(kwargs, {"viewport": {"width": 393, "height": 852},
                                  "device_scale_factor": 3,
                                  "user_agent": devices.get("iphone-15")["user_agent"],
                                  "is_mobile": True, "has_touch": True})
        with self.assertRaises(KeyError):
            devices.context_kwargs("no-such-device")
        self.assertIsNone(devices.get("no-such-device"))

    def test_DEV_03_simulator_platform_follows_the_device_group(self):
        self.assertEqual(devices.simulator_platform("zebra-tc58"), "zebra")
        self.assertEqual(devices.simulator_platform("iphone-15"), "ios")
        self.assertEqual(devices.simulator_platform("pixel-8"), "android")
        self.assertEqual(devices.simulator_platform("desktop-1920"), "android")
        self.assertEqual(devices.simulator_platform("no-such-device"), "android")


class DevicePlanWiring(unittest.TestCase):
    def test_DEV_04_catalogue_ids_are_matrix_devices(self):
        from app.plan_runtime import DEVICES, normalize_plan
        for profile in devices.DEVICES:
            self.assertIn(profile["id"], DEVICES)
        plan = normalize_plan(
            {"steps": [{"action": "navigate", "value": "https://example.test/"}],
             "matrix": [{"browser": "chromium", "device": "galaxy-tab-s9"}]})
        self.assertEqual(plan["matrix"][0]["device"], "galaxy-tab-s9")

    def test_DEV_05_runs_apply_the_profile_verbatim_to_the_context(self):
        from app.plan_runtime import run_plan
        page = FakePage()
        actor = FakePlaywright(page)
        with tempfile.TemporaryDirectory(prefix="tf-device-") as tmp:
            outcome = asyncio.run(run_plan(
                {"steps": [{"action": "navigate", "value": "https://example.test/"}],
                 "matrix": [{"browser": "chromium", "device": "iphone-15"}]},
                {}, tmp, playwright=actor))
        self.assertEqual(outcome["status"], "passed")
        kwargs = actor.browser.kwargs
        self.assertEqual(kwargs["viewport"], {"width": 393, "height": 852})
        self.assertEqual(kwargs["device_scale_factor"], 3)
        self.assertIn("iPhone", kwargs["user_agent"])
        self.assertTrue(kwargs["is_mobile"])
        self.assertTrue(kwargs["has_touch"])

    def test_DEV_06_every_target_flow_is_a_valid_plan(self):
        from app.plan_runtime import normalize_plan
        for target in TARGETS:
            flows = _web_flows(target, "http://127.0.0.1:9/")
            self.assertTrue(flows)
            for label, plan in flows:
                checked = normalize_plan({**plan, "matrix": [
                    {"browser": "chromium", "device": "zebra-tc58"}]})
                first = checked["setup"][0]
                self.assertTrue(first["value"].startswith("http://127.0.0.1:9/"),
                                f"{target}/{label} must navigate an absolute fixture URL")

    def test_DEV_07_the_simulator_leg_replays_the_scan_flow(self):
        outcome = asyncio.run(run_simulator_script("zebra"))
        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(outcome["mode"], "simulator")
        self.assertEqual([step["action"] for step in outcome["steps"]],
                         ["app_launch", "scan_barcode", "assert_mobile_text",
                          "assert_mobile_text"])
        scan = outcome["steps"][1]["detail"]
        self.assertEqual(scan["product"], "TestForge Scanner Demo Item")

    def test_DEV_08_the_new_fixtures_carry_the_asserted_ids(self):
        static = Path("app/static")
        erp = (static / "erp-demo.html").read_text(encoding="utf-8")
        crm = (static / "crm-demo.html").read_text(encoding="utf-8")
        for needle in ("po-supplier", "po-amount", "po-submit", "po-status",
                       "PO-2026-", "AUD"):
            self.assertIn(needle, erp)
        for needle in ("opp-name", "opp-stage", "opp-save", "opp-toast",
                       "OPP-", "Proposal"):
            self.assertIn(needle, crm)
        self.assertTrue((static / "mobile-zebra-demo.html").is_file())
        self.assertTrue((static / "erp-crm-demo.html").is_file())


class DeviceRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.db import init_db
        init_db()
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def test_DEV_09_the_catalogue_lists_groups_targets_and_profiles(self):
        payload = self.client.get("/api/devices").json()
        self.assertEqual([group["id"] for group in payload["groups"]],
                         ["handheld", "android", "ios", "other", "desktop"])
        self.assertEqual(sorted(item["id"] for item in payload["targets"]),
                         ["crm", "erp", "erp-crm", "zebra"])
        self.assertEqual(len(payload["devices"]), 14)
        self.assertEqual(payload["engine"], "chromium")

    def test_DEV_10_unknown_devices_targets_and_busy_runs_fail_closed(self):
        from app import devices_routes
        self.assertEqual(self.client.post(
            "/api/devices/run",
            json={"device_id": "nope", "target": "zebra"}).status_code, 404)
        self.assertEqual(self.client.post(
            "/api/devices/run",
            json={"device_id": "zebra-tc58", "target": "nope"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/devices/run", json="oops").status_code, 400)
        locked = devices_routes._RUN_LOCK.acquire(blocking=False)
        self.assertTrue(locked)
        try:
            response = self.client.post(
                "/api/devices/run",
                json={"device_id": "zebra-tc58", "target": "zebra"})
            self.assertEqual(response.status_code, 429)
        finally:
            devices_routes._RUN_LOCK.release()
