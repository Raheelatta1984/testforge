"""Master setup (region, time zone, date/time format, language) and the Windows
desktop recorder's variable onboarding.

* Settings are validated on the server, stored once for the whole instance, and
  a corrupt stored value falls back to its default instead of breaking the page.
* A value typed into a Windows control becomes a reusable variable the same way a
  browser input does, and the replayable plan copy is rewritten along with it.
"""
import os
import time
import unittest
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()

from fastapi.testclient import TestClient

from app import app_settings, library_store, variables_ai
from app.db import AppSetting, SessionLocal
from app.main import app


class MasterSettingsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.__enter__()
        with SessionLocal() as db:
            db.query(AppSetting).delete()
            db.commit()

    def tearDown(self):
        with SessionLocal() as db:
            db.query(AppSetting).delete()
            db.commit()
        self.client.__exit__(None, None, None)

    def test_defaults_and_catalog_are_served(self):
        body = self.client.get("/api/settings").json()
        self.assertEqual(body["settings"], app_settings.DEFAULTS)
        options = body["options"]
        self.assertEqual({item["code"] for item in options["regions"]}, set(app_settings.REGION_BY_CODE))
        self.assertIn("GLOBAL", {item["code"] for item in options["regions"]})
        self.assertEqual([item["id"] for item in options["languages"]], ["en", "de", "fr", "es"])
        self.assertIn("UTC", options["timezones"])
        self.assertIn("Europe/Berlin", options["timezones"])
        self.assertTrue(all("/" in name or name == "UTC" for name in options["timezones"]))

    def test_every_region_default_is_a_valid_choice(self):
        zones = set(app_settings.timezones())
        for region in app_settings.REGIONS:
            with self.subTest(region=region["code"]):
                self.assertIn(region["timezone"], zones)
                self.assertIn(region["date_format"], app_settings.DATE_FORMAT_IDS)
                self.assertIn(region["time_format"], app_settings.TIME_FORMAT_IDS)
                self.assertIn(region["language"], app_settings.LANGUAGE_IDS)

    def test_valid_change_is_stored_once_for_the_whole_instance(self):
        response = self.client.put("/api/settings", json={
            "region": "DE", "timezone": "Europe/Berlin", "date_format": "DD.MM.YYYY",
            "time_format": "24h", "language": "de", "default_windows_version": "windows_10",
            "screen_zoom": "150"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["settings"]["timezone"], "Europe/Berlin")
        # A second client, with no state of its own, sees the same master settings.
        with TestClient(app) as other:
            again = other.get("/api/settings").json()["settings"]
        self.assertEqual(again["region"], "DE")
        self.assertEqual(again["language"], "de")
        self.assertEqual(again["screen_zoom"], "150")

    def test_partial_update_keeps_other_values(self):
        self.client.put("/api/settings", json={"time_format": "12h"})
        self.client.put("/api/settings", json={"language": "fr"})
        settings = self.client.get("/api/settings").json()["settings"]
        self.assertEqual(settings["time_format"], "12h")
        self.assertEqual(settings["language"], "fr")
        self.assertEqual(settings["timezone"], app_settings.DEFAULTS["timezone"])

    def test_invalid_values_are_refused_with_a_reason(self):
        cases = [
            {"timezone": "Mars/Olympus"},
            {"timezone": "posix/Europe/Berlin"},
            {"region": "XX"},
            {"date_format": "YYYY.DD.MM.HH"},
            {"time_format": "25h"},
            {"language": "klingon"},
            {"screen_zoom": "900"},
            {"default_windows_version": "windows_95"},
            {"not_a_setting": "x"},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.client.put("/api/settings", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertTrue(response.json()["detail"])
        self.assertEqual(self.client.get("/api/settings").json()["settings"], app_settings.DEFAULTS)

    def test_non_object_body_is_refused(self):
        self.assertEqual(self.client.put("/api/settings", json=["UTC"]).status_code, 422)

    def test_corrupt_stored_value_falls_back_to_default(self):
        with SessionLocal() as db:
            db.add(AppSetting(key=app_settings.MASTER_KEY,
                              value={"timezone": "Not/AZone", "language": "de", "time_format": "12h"}))
            db.commit()
        settings = self.client.get("/api/settings").json()["settings"]
        self.assertEqual(settings["timezone"], app_settings.DEFAULTS["timezone"])
        self.assertEqual(settings["language"], "de")
        self.assertEqual(settings["time_format"], "12h")


class DesktopVariableOnboardingTests(unittest.TestCase):
    """A value typed on a Windows screen is offered as a variable after STOP, like the browser."""

    def setUp(self):
        self.client = TestClient(app)
        self.client.__enter__()
        self.project = self.client.post("/api/projects", json={"name": "Desktop variables"}).json()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def _record_typed_value(self, value):
        with patch.dict(os.environ, {"TF_WINDOWS_AGENT_URL": "", "TF_WINDOWS_AGENT_TOKEN": ""}):
            start = self.client.post("/api/desktop/recordings", json={
                "project_id": self.project["id"], "name": "Desktop typed",
                "app_id": "calculator", "windows_version": "windows_10"})
            self.assertEqual(start.status_code, 201)
            recording_id = start.json()["recording_id"]
            typed = self.client.post(f"/api/desktop/recordings/{recording_id}/input", json={
                "action": "ui_type", "x": 0.5, "y": 0.2, "value": value, "field_name": "Customer name"})
            self.assertEqual(typed.status_code, 200, typed.text)
            stopped = self.client.post(f"/api/desktop/recordings/{recording_id}/stop", json={})
            self.assertEqual(stopped.status_code, 200)
        return recording_id

    def test_typed_desktop_value_is_proposed_as_a_variable_after_stop(self):
        recording_id = self._record_typed_value("Ada Lovelace")
        plan = self.client.post(f"/api/recordings/{recording_id}/variable-plan", json={})
        self.assertEqual(plan.status_code, 200)
        candidates = plan.json()["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["value"], "Ada Lovelace")
        self.assertEqual(candidates[0]["name"], "customer_name")
        self.assertFalse(candidates[0]["is_secret"])

    def test_applying_the_variable_rewrites_rows_and_the_replayable_plan(self):
        recording_id = self._record_typed_value("Ada Lovelace")
        applied = self.client.post(f"/api/recordings/{recording_id}/variables", json={
            "assignments": [{"name": "customer_name", "value": "Ada Lovelace", "category": "General",
                             "is_secret": False, "create": False}]})
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["counts"]["steps_changed"], 1)
        detail = self.client.get(f"/api/recordings/{recording_id}").json()
        self.assertEqual(detail["steps"][0]["value"], "{{customer_name}}")
        with SessionLocal() as db:
            from app.db import Recording
            plan = db.get(Recording, recording_id).execution_plan
        self.assertEqual(plan["steps"][0]["value"], "{{customer_name}}")
        variables = {item["name"]: item for item in library_store.list_variables(self.project["id"])}
        self.assertEqual(variables["customer_name"]["value"], "Ada Lovelace")
        # The replay resolves {{customer_name}} from the project variable and passes.
        queued = self.client.post("/api/runs", json={"target_id": recording_id, "display_window": False})
        self.assertEqual(queued.status_code, 201, queued.text)
        run_id = queued.json()["run_id"]
        run = {}
        for _ in range(300):
            run = self.client.get(f"/api/runs/{run_id}?full=true").json()
            if run.get("status") not in {"queued", "running"}:
                break
            time.sleep(0.02)
        self.assertEqual(run.get("status"), "passed", run.get("error") or run.get("log"))
        typed = [item for item in run["log"] if item.get("action") == "ui_type"]
        self.assertTrue(typed)
        self.assertNotIn("{{", str(typed[0].get("value", "")))

    def test_desktop_plan_step_is_only_rewritten_when_it_still_holds_the_literal(self):
        plan = {"steps": [{"action": "ui_type", "value": "Ada", "selector": {}},
                          {"action": "ui_type", "value": "Grace", "selector": {}}]}
        changed = library_store._plan_with_values(
            plan, [{"order": 2, "before": "Grace", "after": "{{name}}"}])
        self.assertEqual([step["value"] for step in changed["steps"]], ["Ada", "{{name}}"])
        self.assertEqual([step["value"] for step in plan["steps"]], ["Ada", "Grace"])  # input untouched

    def test_desktop_ui_actions_are_data_actions_and_control_names_are_hints(self):
        self.assertEqual(variables_ai.DATA_ACTIONS["ui_type"], "value")
        self.assertEqual(variables_ai.DATA_ACTIONS["ui_select"], "value")
        self.assertNotIn("ui_click", variables_ai.DATA_ACTIONS)  # a click carries no data
        hint = variables_ai._hint_from_selector({"automation_id": "txtCustomer", "field_name": "Customer name"})
        self.assertEqual(hint, "Customer name")


if __name__ == "__main__":
    unittest.main()
