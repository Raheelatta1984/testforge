"""Unit tests for the recording and replay fixes.

These call the TestForge library directly. They do not open a browser.
Scenario IDs match tests/SCENARIOS.md.
"""

import asyncio
import os
import unittest
from pathlib import Path

from tests.bootstrap import ensure_test_env

ensure_test_env()

from app.browser import _browser_env, explain_launch_error, launch_kwargs, video_ok
from app.config import CICD_INTERVAL, IS_TERMUX
from app.db import Run, SessionLocal, apply_variables, interpolate
from app.errors import redact
from app.executor import execute_run, replay_step
from app.main import browser_url
from fastapi import HTTPException
from tests.fakes import FakePage

ROOT = Path(__file__).resolve().parents[1]


class Req:
    def __init__(self, host):
        self.headers = {"host": host}


def _env_set(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return saved


def _env_restore(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class BootTests(unittest.TestCase):
    def test_UT_BOOT_01_modules_import(self):
        from app.executor import execute_run as runner
        from app.recorder import RecorderSession, open_session

        self.assertTrue(callable(runner))
        self.assertTrue(callable(open_session))
        self.assertTrue(callable(getattr(RecorderSession, "start")))

    def test_UT_BOOT_02_config_contract(self):
        self.assertIsInstance(IS_TERMUX, bool)
        self.assertIsInstance(CICD_INTERVAL, int)
        self.assertGreater(CICD_INTERVAL, 0)
        self.assertIs(interpolate, apply_variables)

    def test_UT_BOOT_03_run_columns_match_the_model(self):
        # The executor used to write target_id / log / rog_investigation.
        # Those names are not columns, so every run died on import or on save.
        self.assertTrue(hasattr(Run, "recording_id"))
        self.assertTrue(hasattr(Run, "execution_log"))
        self.assertTrue(hasattr(Run, "progress_pct"))
        self.assertTrue(hasattr(Run, "rog_monitor_log"))
        self.assertFalse(hasattr(Run, "target_id"))
        self.assertFalse(hasattr(Run, "rog_investigation"))


class VariableTests(unittest.TestCase):
    def test_UT_VAR_01_known_keys(self):
        text = interpolate("Hello {{user}} from {{env}}", {"user": "Ada", "env": "qa"})
        self.assertEqual(text, "Hello Ada from qa")

    def test_UT_VAR_02_unknown_key_kept(self):
        self.assertEqual(interpolate("Hello {{missing}}", {}), "Hello {{missing}}")

    def test_UT_VAR_03_empty_text(self):
        self.assertIsNone(interpolate(None, {"user": "Ada"}))
        self.assertEqual(interpolate("", {"user": "Ada"}), "")

    def test_UT_VAR_04_inner_whitespace(self):
        self.assertEqual(interpolate("{{ user }}", {"user": "Ada"}), "Ada")


class UrlTests(unittest.TestCase):
    def setUp(self):
        self._saved = _env_set(PORT="8765")

    def tearDown(self):
        _env_restore(self._saved)

    def test_UT_URL_01_blank_sample_app(self):
        self.assertEqual(browser_url(""), "http://127.0.0.1:8765/demo.html")
        self.assertEqual(browser_url(None), "http://127.0.0.1:8765/demo.html")

    def test_UT_URL_02_relative_loopback(self):
        self.assertEqual(browser_url("/demo.html"), "http://127.0.0.1:8765/demo.html")

    def test_UT_URL_03_scheme_less_https(self):
        self.assertEqual(browser_url("example.com/login"), "https://example.com/login")

    def test_UT_URL_04_external_unchanged(self):
        url = "https://example.com/app?q=1"
        self.assertEqual(browser_url(url), url)

    def test_UT_URL_05_file_scheme_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            browser_url("file:///tmp/secret.html")
        self.assertEqual(caught.exception.status_code, 422)

    def test_UT_URL_06_preview_host_rewritten(self):
        request = Req("8000-sandbox.e2b.app")
        got = browser_url("https://8000-sandbox.e2b.app/demo.html?x=1", request)
        self.assertEqual(got, "http://127.0.0.1:8765/demo.html?x=1")

    def test_UT_URL_07_other_local_port_preserved(self):
        url = "http://127.0.0.1:3000/app"
        self.assertEqual(browser_url(url, Req("127.0.0.1:8765")), url)
        self.assertEqual(browser_url("http://localhost:5173/"), "http://localhost:5173/")

    def test_UT_URL_08_query_preserved(self):
        request = Req("127.0.0.1:8765")
        got = browser_url("http://127.0.0.1:8765/demo.html?name=Ada", request)
        self.assertEqual(got, "http://127.0.0.1:8765/demo.html?name=Ada")


class BrowserLaunchTests(unittest.TestCase):
    def test_UT_BRW_01_libs_do_not_leak(self):
        saved = _env_set(TF_CHROMIUM_LIBS="/tmp/tf-fake-libs", LD_LIBRARY_PATH=os.environ.get("LD_LIBRARY_PATH"))
        before = os.environ.get("LD_LIBRARY_PATH")
        try:
            isolated = _browser_env()
            self.assertIn("/tmp/tf-fake-libs", isolated["LD_LIBRARY_PATH"])
            self.assertEqual(os.environ.get("LD_LIBRARY_PATH"), before)
            kwargs = launch_kwargs()
            self.assertEqual(os.environ.get("LD_LIBRARY_PATH"), before)
            self.assertIn("env", kwargs)
        finally:
            _env_restore(saved)

    def test_UT_BRW_02_missing_binary_raises(self):
        saved = _env_set(TF_CHROMIUM_PATH="/tmp/tf-missing-chromium", TF_CHROMIUM_LIBS=None)
        try:
            with self.assertRaises(RuntimeError) as caught:
                launch_kwargs()
            self.assertIn("TF_CHROMIUM_PATH", str(caught.exception))
        finally:
            _env_restore(saved)

    def test_UT_BRW_03_install_hint(self):
        message = explain_launch_error(RuntimeError(
            "Executable doesn't exist at /opt/ms-playwright/chromium\n"
            "Please run the following command: playwright install chromium"
        ))
        self.assertIn("playwright install chromium", message)
        self.assertNotIn("/opt/ms-playwright", message)

    def test_UT_BRW_04_video_disabled_for_override(self):
        saved = _env_set(TF_CHROMIUM_PATH="/tmp/tf-missing-chromium")
        try:
            self.assertFalse(video_ok())
        finally:
            _env_restore(saved)


class SecurityTests(unittest.TestCase):
    def test_UT_SEC_01_redact_password(self):
        raw = "could not connect to postgresql://qa_user:s3cret@db.internal/testforge"
        cleaned = redact(raw)
        self.assertNotIn("s3cret", cleaned)
        self.assertIn("qa_user:***@", cleaned)


class ReplayTests(unittest.TestCase):
    def test_UT_REP_01_navigate_interpolates(self):
        page = FakePage()
        asyncio.run(replay_step(page, {"action": "navigate", "value": "https://example.com/{{user}}"}, {"user": "ada"}))
        self.assertEqual(page.gotos[0][0], "https://example.com/ada")
        self.assertEqual(page.gotos[0][1], "domcontentloaded")

    def test_UT_REP_02_click_selector(self):
        page = FakePage(known={"#go"})
        asyncio.run(replay_step(page, {"action": "click", "selector": {"primary": "#go", "x": 1, "y": 2}}, {}))
        self.assertEqual(page.clicks, [("locator", "#go")])

    def test_UT_REP_03_click_coordinate_fallback(self):
        page = FakePage()
        asyncio.run(replay_step(page, {"action": "click", "selector": {"primary": "#missing", "x": 80, "y": 140}}, {}))
        self.assertEqual(page.clicks, [("mouse", 80.0, 140.0)])

    def test_UT_REP_04_click_without_target(self):
        page = FakePage()
        with self.assertRaises(RuntimeError):
            asyncio.run(replay_step(page, {"action": "click", "selector": {}}, {}))

    def test_UT_REP_05_type_interpolates(self):
        page = FakePage(known={"#name"})
        asyncio.run(replay_step(
            page,
            {"action": "type", "value": "{{user}}", "selector": {"primary": "#name"}},
            {"user": "Ada"},
        ))
        self.assertEqual(page.typed, ["Ada"])
        self.assertEqual(page.clicks[0], ("locator", "#name"))

    def test_UT_REP_06_fill_replaces(self):
        page = FakePage(known={"#name"})
        asyncio.run(replay_step(
            page,
            {"action": "fill", "value": "Ada", "selector": {"primary": "#name"}},
            {},
        ))
        self.assertEqual(page.fills, [("#name", "Ada")])

    def test_UT_REP_07_press_key(self):
        page = FakePage()
        asyncio.run(replay_step(page, {"action": "press", "value": "Enter"}, {}))
        self.assertEqual(page.pressed, ["Enter"])

    def test_UT_REP_08_unsupported_action(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(replay_step(FakePage(), {"action": "hover"}, {}))

    def test_UT_REP_09_navigate_without_url(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(replay_step(FakePage(), {"action": "navigate", "value": ""}, {}))


class RunTests(unittest.TestCase):
    def test_UT_RUN_01_no_steps_fails_closed(self):
        from app.db import Project, Recording

        with SessionLocal() as db:
            project = Project(name="unit-empty", base_url="")
            db.add(project)
            db.commit()
            db.refresh(project)
            recording = Recording(project_id=project.id, name="empty", start_url="http://127.0.0.1/demo.html")
            db.add(recording)
            db.commit()
            db.refresh(recording)
            run = Run(recording_id=recording.id, status="queued")
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id

        events = []

        async def on_event(event):
            events.append(event)

        asyncio.run(execute_run(run_id, on_event))
        with SessionLocal() as db:
            saved = db.get(Run, run_id)
            self.assertEqual(saved.status, "error")
            self.assertIn("no steps", (saved.rog_monitor_log or "").lower())
        self.assertEqual(events[-1]["status"], "error")

    def test_UT_RUN_02_missing_recording(self):
        with SessionLocal() as db:
            run = Run(recording_id="missing-recording", status="queued")
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id

        events = []

        async def on_event(event):
            events.append(event)

        asyncio.run(execute_run(run_id, on_event))
        with SessionLocal() as db:
            saved = db.get(Run, run_id)
            self.assertEqual(saved.status, "error")
            self.assertIn("Recording not found", saved.rog_monitor_log or "")
        self.assertEqual(events[-1]["error"], "Recording not found")


class CatalogTests(unittest.TestCase):
    def test_UT_DOC_01_catalog_ids_match(self):
        import json

        documented = (ROOT / "tests" / "SCENARIOS.md").read_text()
        library = json.loads((ROOT / "tests" / "scenarios.json").read_text())
        missing = []
        for scenario in library["scenarios"]:
            if scenario["id"] not in documented:
                missing.append(scenario["id"])
            for covered in scenario.get("also_covers", []):
                if covered not in documented:
                    missing.append(covered)
        for name in dir(unittest.TestCase):
            pass
        for cls in (BootTests, VariableTests, UrlTests, BrowserLaunchTests, SecurityTests, ReplayTests, RunTests):
            for name in dir(cls):
                if name.startswith("test_UT_") and name.split("_", 1)[0] == "test":
                    scenario_id = name.split("_", 1)[1]
                    # test_UT_BOOT_01_... -> UT-BOOT-01
                    parts = name[len("test_"):].split("_")
                    # UT BOOT 01 rest...
                    scenario_id = "-".join(parts[:3])
                    if scenario_id not in documented:
                        missing.append(scenario_id)
        self.assertFalse(missing, "IDs missing from tests/SCENARIOS.md: " + ", ".join(missing))


if __name__ == "__main__":
    unittest.main(verbosity=2)
