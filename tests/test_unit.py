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

    def test_UT_REP_10_relative_navigate_uses_this_server(self):
        saved = _env_set(PORT="8765")
        try:
            page = FakePage()
            asyncio.run(replay_step(page, {"action": "navigate", "value": "/demo.html"}, {}))
            self.assertEqual(page.gotos[0][0], "http://127.0.0.1:8765/demo.html")
            other = FakePage()
            asyncio.run(replay_step(other, {"action": "navigate", "value": "http://127.0.0.1:3000/app"}, {}))
            self.assertEqual(other.gotos[0][0], "http://127.0.0.1:3000/app")
        finally:
            _env_restore(saved)

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


class LibraryStoreTests(unittest.TestCase):
    def test_UT_LIB_01_project_is_written_to_the_repository_tree(self):
        from app import library_store

        payload = library_store.create_project("Catalog", "https://example.com")
        self.assertEqual(payload["source"], "repository")
        self.assertFalse(payload["published"])
        path = Path(os.environ["TF_LIBRARY_DIR"]) / "projects" / payload["id"] / "project.json"
        self.assertTrue(path.is_file())
        self.assertIn(payload["id"], [item["id"] for item in library_store.list_projects()])

    def test_UT_LIB_02_database_only_projects_are_hidden(self):
        from app import library_store
        from app.db import Project, SessionLocal

        with SessionLocal() as db:
            db.add(Project(id="db-only-project", name="Not in git", base_url=""))
            db.commit()
        self.assertNotIn("Not in git", [item["name"] for item in library_store.list_projects()])
        library_store.materialize_all()
        with SessionLocal() as db:
            self.assertIsNone(db.get(Project, "db-only-project"))

    def test_UT_LIB_03_recording_writes_its_jenkins_resource(self):
        from app import library_store

        project = library_store.create_project("Rec", "")
        recording = library_store.create_recording(project["id"], "Flow", "/demo.html")
        jenkins = (
            Path(os.environ["TF_LIBRARY_DIR"])
            / "projects" / project["id"] / "recordings" / recording["id"] / "resources" / "Jenkinsfile"
        )
        self.assertTrue(jenkins.is_file())
        self.assertIn("pipeline", jenkins.read_text(encoding="utf-8"))
        self.assertTrue(str(recording["repository_path"]).endswith("recording.json"))

    def test_UT_LIB_04_variables_roundtrip_in_the_repository(self):
        import json
        from app import library_store

        project = library_store.create_project("Vars", "")
        created = library_store.create_variable(project["id"], "user", "Ada")
        updated = library_store.update_variable(created["id"], value="Grace")
        self.assertEqual(updated["value"], "Grace")
        raw = json.loads((Path(os.environ["TF_LIBRARY_DIR"]) / "projects" / project["id"] / "variables.json").read_text())
        self.assertEqual(raw[0]["value"], "Grace")
        library_store.delete_variable(created["id"])
        self.assertEqual(library_store.list_variables(project["id"]), [])

    def test_UT_LIB_05_rejects_a_path_escape(self):
        from app import library_store

        with self.assertRaises(library_store.LibraryError):
            library_store.require_id("../etc")

    def test_UT_LIB_06_publish_pushes_only_library_files(self):
        import shutil
        import subprocess
        import tempfile
        from app import library_store

        tmp = Path(tempfile.mkdtemp(prefix="tf-git-"))
        bare = tmp / "remote.git"
        repo = tmp / "repo"
        try:
            subprocess.check_call(["git", "init", "--bare", "-b", "library-test", str(bare)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call(["git", "init", "-b", "library-test", str(repo)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call(["git", "config", "user.email", "qa@testforge.local"], cwd=repo)
            subprocess.check_call(["git", "config", "user.name", "TestForge QA"], cwd=repo)
            (repo / "KEEP.txt").write_text("do not touch\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "KEEP.txt"], cwd=repo)
            subprocess.check_call(["git", "commit", "-m", "init"], cwd=repo, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "branch", "-M", "library-test"], cwd=repo)
            subprocess.check_call(["git", "remote", "add", "origin", str(bare)], cwd=repo)
            subprocess.check_call(["git", "push", "-u", "origin", "HEAD"], cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            saved = _env_set(TF_LIBRARY_DIR=str(repo / "library"), TF_LIBRARY_PUBLISH="1", TF_GIT_REMOTE="origin")
            try:
                payload = library_store.create_project("Pushed", "https://example.com")
                self.assertTrue(payload["published"])
                self.assertTrue(library_store.remote_library_status()["synced"])
                mismatch = _env_set(TF_GIT_BRANCH="main")
                try:
                    with self.assertRaises(library_store.PublishError):
                        library_store.publish_pending()
                finally:
                    _env_restore(mismatch)
                clone = tmp / "clone"
                subprocess.check_call(
                    ["git", "clone", "--branch", "library-test", str(bare), str(clone)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                cloned = clone / "library" / "projects" / payload["id"] / "project.json"
                self.assertTrue(cloned.is_file())
                self.assertEqual((clone / "KEEP.txt").read_text(encoding="utf-8"), "do not touch\n")
                status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
                self.assertEqual(status.strip(), "")
            finally:
                _env_restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_UT_LIB_07_failed_push_keeps_local_project_for_retry(self):
        import shutil
        import subprocess
        import tempfile
        from app import library_store
        from app.db import Project, SessionLocal

        tmp = Path(tempfile.mkdtemp(prefix="tf-git-fail-"))
        repo = tmp / "repo"
        try:
            subprocess.check_call(["git", "init", "-b", "library-test", str(repo)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.check_call(["git", "config", "user.email", "qa@testforge.local"], cwd=repo)
            subprocess.check_call(["git", "config", "user.name", "TestForge QA"], cwd=repo)
            (repo / "README").write_text("init\n", encoding="utf-8")
            subprocess.check_call(["git", "add", "README"], cwd=repo)
            subprocess.check_call(["git", "commit", "-m", "init"], cwd=repo, stdout=subprocess.DEVNULL)
            subprocess.check_call(["git", "remote", "add", "origin", str(tmp / "missing.git")], cwd=repo)
            saved = _env_set(TF_LIBRARY_DIR=str(repo / "library"), TF_LIBRARY_PUBLISH="1")
            try:
                project = library_store.create_project("Nope", "")
                self.assertFalse(project["published"])
                self.assertIn(project["id"], [p["id"] for p in library_store.list_projects()])
                with SessionLocal() as db:
                    self.assertEqual(db.query(Project).filter_by(name="Nope").count(), 1)
            finally:
                _env_restore(saved)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_UT_LIB_08_materialize_loads_a_file_only_recording(self):
        import json
        from app import library_store
        from app.db import Recording, SessionLocal

        root = Path(os.environ["TF_LIBRARY_DIR"]) / "projects" / "file-only"
        rec = root / "recordings" / "file-only-rec"
        (rec / "resources").mkdir(parents=True)
        (root / "project.json").write_text(json.dumps({
            "id": "file-only",
            "name": "File only",
            "base_url": "/demo.html",
            "industry_type": "Generic",
            "created_at": "2026-10-10T00:00:00Z",
        }), encoding="utf-8")
        (root / "variables.json").write_text("[]\n", encoding="utf-8")
        (rec / "recording.json").write_text(json.dumps({
            "id": "file-only-rec",
            "project_id": "file-only",
            "name": "From git",
            "start_url": "/demo.html",
            "steps": [{"id": "file-only-1", "order": 1, "action": "navigate", "value": "/demo.html", "label": "Open"}],
        }), encoding="utf-8")
        (rec / "resources" / "Jenkinsfile").write_text("pipeline {}\n", encoding="utf-8")
        library_store.materialize_all()
        with SessionLocal() as db:
            row = db.get(Recording, "file-only-rec")
            self.assertIsNotNone(row)
            self.assertEqual(row.name, "From git")
            self.assertEqual(len(list(row.steps)), 1)
        self.assertEqual(library_store.get_recording("file-only-rec")["source"], "repository")


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
        for cls in (BootTests, VariableTests, UrlTests, BrowserLaunchTests, SecurityTests, ReplayTests, RunTests, LibraryStoreTests):
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



class WorkflowRegressionTests(unittest.TestCase):
    """Record/save/edit/export regressions that do not require Chromium."""

    def test_UT_FLOW_01_repeated_action_merges_distinct_actions_keep_order(self):
        """Consecutive identical actions become one step carrying a repeat count.

        Distinct actions still get their own row, in execution order.
        """
        from app import library_store
        from app.db import RecordingStep, SessionLocal
        from app.recorder import RecorderSession

        project = library_store.create_project("Workflow steps", "")
        rec = library_store.create_recording(project["id"], "steps", "/demo.html")
        session = RecorderSession(rec["id"], "", 0)
        async def exercise():
            first = await session._record("click", selector={"x": 1, "y": 2}, label="click")
            second = await session._record("click", selector={"x": 1, "y": 2}, label="click")
            third = await session._record("click", selector={"x": 1, "y": 2}, label="click")
            # Identical repeat: the same row, with the count bumped.
            self.assertEqual(second["id"], first["id"])
            self.assertEqual(third["id"], first["id"])
            self.assertEqual([first["order"], second["order"], third["order"]], [1, 1, 1])
            self.assertEqual(third["repeat"], 3)
            # A different action keeps its own row after the merged one.
            fourth = await session._record("press", value="Enter", label="Press Enter")
            self.assertNotEqual(fourth["id"], first["id"])
            self.assertEqual(fourth["order"], 2)
            await session._attach_image(third, b"\x89PNG\r\n\x1a\n")
            await session.stop()
        asyncio.run(exercise())
        exported = library_store.export_recording(rec["id"])
        self.assertEqual([step["order"] for step in exported["steps"]], [1, 2])
        self.assertEqual(exported["steps"][0]["repeat"], 3)
        self.assertEqual(exported["steps"][0]["screenshot"], "step-001.png")
        self.assertNotIn("×", exported["steps"][0]["label"])
        with SessionLocal() as db:
            self.assertEqual(db.query(RecordingStep).filter_by(recording_id=rec["id"]).count(), 2)

    def test_UT_FLOW_07_merged_repeat_replays_every_occurrence(self):
        """A merged step must still perform the action as many times as recorded."""
        from app.executor import _expand_steps_with_repeat
        steps = [
            {"order": 1, "action": "click", "label": "Next", "selector": {"primary": "#next"}, "repeat": 3},
            {"order": 2, "action": "press", "value": "Enter", "label": "Press Enter", "repeat": 1},
        ]
        expanded = asyncio.run(_expand_steps_with_repeat(steps))
        self.assertEqual(len(expanded), 4)
        self.assertEqual([item["action"] for item in expanded], ["click", "click", "click", "press"])

    def test_UT_FLOW_08_save_variable_is_never_merged(self):
        """Two captures of the same field hold different values, so both stay."""
        from app import library_store
        from app.db import RecordingStep, SessionLocal
        from app.recorder import RecorderSession
        project = library_store.create_project("Capture order", "")
        rec = library_store.create_recording(project["id"], "capture-order", "/demo.html")
        session = RecorderSession(rec["id"], "", 0)
        async def exercise():
            first = await session._record("save_variable", value="captured",
                                          selector={"primary": "#input"}, label="Save input into captured")
            second = await session._record("save_variable", value="captured",
                                           selector={"primary": "#input"}, label="Save input into captured")
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual([first["order"], second["order"]], [1, 2])
            await session.stop()
        asyncio.run(exercise())
        with SessionLocal() as db:
            self.assertEqual(db.query(RecordingStep).filter_by(recording_id=rec["id"]).count(), 2)

    def test_UT_FLOW_09_compress_existing_recording(self):
        """A recording saved before merging can be collapsed on demand."""
        from fastapi.testclient import TestClient
        from app import library_store
        from app.db import RecordingStep, SessionLocal
        from app.main import app
        project = library_store.create_project("Legacy repeats", "")
        rec = library_store.create_recording(project["id"], "legacy", "/demo.html")
        with SessionLocal() as db:
            for order in range(1, 5):
                db.add(RecordingStep(recording_id=rec["id"], order=order, action="click",
                                     label="Next", selector={"primary": "#next"}))
            db.commit()
        with TestClient(app) as client:
            response = client.post(f"/api/recordings/{rec['id']}/steps/compress")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["before"], 4)
            self.assertEqual(payload["after"], 1)
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["steps"][0]["repeat"], 4)
            detail = client.get(f"/api/recordings/{rec['id']}").json()
            self.assertEqual(len(detail["steps"]), 1)
            self.assertEqual(detail["steps"][0]["repeat"], 4)

    def test_UT_FLOW_02_stop_without_session_and_edit_with_screenshot(self):
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app
        from app.db import RecordingStep, SessionLocal
        project = library_store.create_project("Edit steps", "")
        rec = library_store.create_recording(project["id"], "edit", "/demo.html")
        with SessionLocal() as db:
            row = RecordingStep(recording_id=rec["id"], order=1, action="type", value="before", label="Before")
            db.add(row); db.commit(); db.refresh(row)
            step_id = row.id
        with TestClient(app) as client:
            response = client.post(f"/api/recordings/{rec['id']}/stop")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            response = client.patch(f"/api/recordings/{rec['id']}/steps/{step_id}", json={"value": "{{user}}", "label": "After"})
            self.assertEqual(response.status_code, 200)
            response = client.get(f"/api/recordings/{rec['id']}")
            self.assertEqual(response.json()["steps"][0]["value"], "{{user}}")
            self.assertEqual(client.get(f"/api/recordings/{rec['id']}/steps/{step_id}/screenshot").status_code, 404)
            self.assertEqual(client.get("/api/runs/queue/status").json()["pending"], 0)

    def test_UT_FLOW_03_export_templates_and_formula_safety(self):
        from app import library_store
        from openpyxl import load_workbook
        project = library_store.create_project("Exports", "")
        rec = library_store.create_recording(project["id"], "Export", "/demo.html")
        from app.db import RecordingStep, SessionLocal
        with SessionLocal() as db:
            db.add(RecordingStep(recording_id=rec["id"], order=1, action="type", value="=1+2", label="Formula"))
            db.commit()
        library_store.export_recording(rec["id"])
        folder = Path(os.environ["TF_LIBRARY_DIR"]) / "projects" / project["id"] / "recordings" / rec["id"] / "resources"
        for name in ("Jenkinsfile", "playwright_test.py", "playwright_test.js", "recording.feature", "azure-devops.csv", "azure-devops.xlsx", "jira.csv", "jira.xlsx", "testcomplete.csv", "testcomplete.xlsx"):
            self.assertTrue((folder / name).is_file(), name)
        sheet = load_workbook(folder / "azure-devops.xlsx").active
        self.assertEqual(sheet["D2"].value, "'=1+2")
        import py_compile
        py_compile.compile(str(folder / "playwright_test.py"), doraise=True)

    def test_UT_FLOW_04_variable_capture_and_replay_value(self):
        from app import library_store
        from app.db import SessionLocal, resolve_variables
        from app.recorder import RecorderSession
        project = library_store.create_project("Vars replay", "")
        rec = library_store.create_recording(project["id"], "vars", "/demo.html")
        library_store.create_variable(project["id"], "user", "actual-value")

        class Keyboard:
            def __init__(self): self.typed = []
            async def type(self, text, delay=0): self.typed.append(text)
        class Page:
            def __init__(self): self.keyboard = Keyboard(); self.url = "/demo.html"
            async def evaluate(self, script): return {"selector": "#user", "tag": "input"}
            async def screenshot(self, **kwargs): return b"\x89PNG\r\n\x1a\n"
        class Preview:
            def __init__(self): self.lock = asyncio.Lock()
            def touch(self): pass
            async def stop(self): pass
        session = RecorderSession(rec["id"], "", 0)
        session.page = Page(); session.preview = Preview(); session._ready.set()
        async def exercise():
            step = await session.handle_input({"type": "text", "text": "{{user}}"})
            self.assertEqual(session.page.keyboard.typed, ["actual-value"])
            self.assertEqual(step["value"], "{{user}}")
            await session.stop()
        asyncio.run(exercise())
        with SessionLocal() as db:
            self.assertEqual(resolve_variables(db, project["id"])["user"], "actual-value")

    def test_UT_FLOW_05_capture_new_and_existing_variable(self):
        from app import library_store
        from app.db import SessionLocal, RecordingStep, resolve_variables
        from app.recorder import RecorderSession
        project = library_store.create_project("Captured variables", "")
        rec = library_store.create_recording(project["id"], "capture", "/demo.html")

        class Page:
            url = "/demo.html"
            current = "alpha"
            async def evaluate(self, script):
                return self.current if "document.activeElement.value" in script else {"selector": "#input"}
            async def screenshot(self, **kwargs): return b"\x89PNG\r\n\x1a\n"
        class Preview:
            def __init__(self): self.lock = asyncio.Lock()
            def touch(self): pass
            async def stop(self): pass
        session = RecorderSession(rec["id"], "", 0)
        session.page = Page(); session.preview = Preview(); session._ready.set()
        async def exercise():
            await session._record("click", selector={"primary": "#input"}, label="Select input")
            await session.handle_input({"type": "save_variable", "name": "captured", "existing": False})
            session.page.current = "beta"
            await session.handle_input({"type": "save_variable", "name": "captured", "existing": True})
            await session.stop()
        asyncio.run(exercise())
        with SessionLocal() as db:
            self.assertEqual(resolve_variables(db, project["id"])["captured"], "beta")
            self.assertEqual([s.order for s in db.query(RecordingStep).filter_by(recording_id=rec["id"]).order_by(RecordingStep.order)], [1, 2, 3])

    def test_UT_FLOW_06_compact_png_preserves_format_and_never_grows(self):
        import io
        from PIL import Image
        from app.images import compact_png
        image = Image.new("RGB", (128, 128), "#19405c")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        original = buffer.getvalue()
        smaller = compact_png(original)
        self.assertTrue(smaller.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLessEqual(len(smaller), len(original))
        self.assertEqual(Image.open(io.BytesIO(smaller)).size, (128, 128))


class SaveCrashTests(unittest.TestCase):
    def test_UT_SAVE_01_stop_survives_a_slow_export(self):
        """SAVE must not die with 'NoneType' object has no attribute 'cancel'.

        stop() waited 2s for the debounced export and then cancelled
        `self._export_task`. But asyncio.wait_for cancels and re-awaits the task
        before raising TimeoutError, and the task's own finally clears that
        attribute first -- so the cancel hit None, the exception escaped stop(),
        the save endpoint returned 500 and the browser was never closed.
        """
        import time
        from app import library_store, recorder
        project = library_store.create_project("Slow export", "")
        rec = library_store.create_recording(project["id"], "slow", "/demo.html")
        session = recorder.RecorderSession(rec["id"], "", 0)

        original_export = library_store.export_recording
        original_timeout = recorder.EXPORT_FLUSH_TIMEOUT
        calls = []
        closed = []

        def blocking_export(recording_id, publish=False):
            calls.append(recording_id)
            time.sleep(0.8)          # longer than the flush budget below
            return original_export(recording_id, publish=publish)

        async def exercise():
            library_store.export_recording = blocking_export
            recorder.EXPORT_FLUSH_TIMEOUT = 0.2
            async def fake_close():
                closed.append(True)
            session._close_browser = fake_close
            try:
                session._schedule_export()
                await asyncio.sleep(0.05)
                await session.stop()          # used to raise AttributeError
                # The export is left to finish rather than cut off mid-write.
                await asyncio.sleep(1.0)
            finally:
                recorder.EXPORT_FLUSH_TIMEOUT = original_timeout
                library_store.export_recording = original_export

        asyncio.run(exercise())
        self.assertTrue(calls, "the export must have started")
        self.assertTrue(closed, "the browser must always be closed")
        self.assertEqual(session.status, "stopped")

    def test_UT_SAVE_02_stop_closes_the_browser_even_if_the_export_raises(self):
        from app import library_store, recorder
        project = library_store.create_project("Broken export", "")
        rec = library_store.create_recording(project["id"], "broken", "/demo.html")
        session = recorder.RecorderSession(rec["id"], "", 0)

        original_export = library_store.export_recording
        closed = []

        def exploding_export(recording_id, publish=False):
            raise RuntimeError("disk on fire")

        async def exercise():
            library_store.export_recording = exploding_export
            async def fake_close():
                closed.append(True)
            session._close_browser = fake_close
            try:
                session._schedule_export()
                await asyncio.sleep(0.7)      # let the debounced task fail
                await session.stop()
            finally:
                library_store.export_recording = original_export

        asyncio.run(exercise())
        self.assertTrue(closed, "the browser must always be closed")
        self.assertTrue(any("export failed" in line for line in session._save_logs))


class PreviewCostTests(unittest.TestCase):
    def test_UT_RUN_03_no_frame_is_rendered_without_a_viewer(self):
        """The live window must not burn CPU for a run nobody is watching."""
        from app.browser import Preview

        class Page:
            def __init__(self):
                self.shots = 0
            async def screenshot(self, **kwargs):
                self.shots += 1
                return b"\xff\xd8" + b"0" * 200

        viewers = {"count": 0}
        page = Page()
        preview = Preview(page, on_jpeg=None, interval=0.01,
                          should_capture=lambda: viewers["count"] > 0)

        async def exercise():
            preview.start()
            await asyncio.sleep(0.3)
            unwatched = page.shots
            viewers["count"] = 1
            for _ in range(40):            # give the loop time to notice
                await asyncio.sleep(0.05)
                if page.shots:
                    break
            watched = page.shots
            await preview.stop()
            return unwatched, watched

        unwatched, watched = asyncio.run(exercise())
        self.assertEqual(unwatched, 0, "no viewer means no capture")
        self.assertGreater(watched, 0, "capture resumes as soon as somebody watches")
        self.assertGreater(preview.captures, 0)

    def test_UT_RUN_04_run_records_the_live_window_choice(self):
        """The Runs tab toggle reaches the queue and the run record."""
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import RUN_WINDOWS, app
        project = library_store.create_project("Window toggle", "")
        rec = library_store.create_recording(project["id"], "toggle", "/demo.html")
        with TestClient(app) as client:
            response = client.post("/api/runs", json={"target_id": rec["id"], "display_window": False})
            self.assertEqual(response.status_code, 201)
            payload = response.json()
            run_id = payload["run_id"]
            self.assertFalse(payload["display_window"])
            self.assertFalse(RUN_WINDOWS[run_id])
            self.assertFalse(client.get(f"/api/runs/{run_id}").json()["display_window"])

            response = client.post("/api/runs", json={"target_id": rec["id"]})
            self.assertEqual(response.status_code, 201)
            self.assertTrue(response.json()["display_window"], "the window defaults to on")


class LibraryDiagnosticsTests(unittest.TestCase):
    def test_UT_GIT_01_status_explains_a_missing_git_checkout(self):
        """A deployment with no checkout must say so, not show bare dashes."""
        from app import library_store
        original = library_store.git_root
        library_store.git_root = lambda: None
        try:
            info = library_store.status()
            verification = library_store.remote_library_status()
        finally:
            library_store.git_root = original
        self.assertEqual(info["git_state"], "no-checkout")
        self.assertFalse(info["git_available"])
        self.assertIsNone(info["branch"])
        self.assertIsNone(info["revision"])
        self.assertFalse(info["publish_enabled"])
        self.assertIn("No .git", info["status_reason"])
        self.assertEqual(verification["state"], "no-checkout")
        self.assertIn("No .git", verification["reason"])

    def test_UT_GIT_02_status_reports_what_is_on_disk(self):
        from app import library_store
        info = library_store.status()
        self.assertIn("projects_on_disk", info)
        self.assertIn("project_dirs", info)
        self.assertIn("catalog_present", info)
        self.assertIn("library_dir", info)
        # Whatever the checkout state, the listing is explained rather than silent.
        if info["git_available"] and not info["projects_error"] and info["projects"]:
            self.assertIsNone(info["status_reason"])



class ExecutorWindowTests(unittest.TestCase):
    """Drive the real execute_run against a fake browser.

    This exercises the live-window switch and the viewer gate end to end without
    needing Chromium.
    """

    def _recording_with_steps(self, name):
        from app.db import Project, Recording, RecordingStep
        with SessionLocal() as db:
            project = Project(name=name, base_url="")
            db.add(project); db.commit(); db.refresh(project)
            recording = Recording(project_id=project.id, name=name, start_url="http://127.0.0.1/demo.html")
            db.add(recording); db.commit(); db.refresh(recording)
            db.add(RecordingStep(recording_id=recording.id, order=1, action="navigate",
                                 value="/demo.html", label="Open"))
            db.add(RecordingStep(recording_id=recording.id, order=2, action="click",
                                 label="Go", selector={"primary": "#go", "x": 1, "y": 2}))
            db.commit()
            run = Run(recording_id=recording.id, status="queued")
            db.add(run); db.commit(); db.refresh(run)
            return run.id

    def _drive(self, display_window, viewers):
        import app.executor as executor
        from tests.fakes import FakePage, FakePlaywright
        page = FakePage(known=["#go"])
        fake = FakePlaywright(page)
        frames = []
        events = []

        async def on_event(event):
            events.append(event)

        async def on_frame(payload):
            frames.append(payload)

        run_id = self._recording_with_steps(f"window-{display_window}-{viewers}")
        original = executor.async_playwright
        executor.async_playwright = lambda: fake
        try:
            asyncio.run(execute_run(
                run_id, on_event, on_frame=on_frame,
                display_window=display_window, viewer_count=lambda: viewers,
            ))
        finally:
            executor.async_playwright = original
        with SessionLocal() as db:
            status = db.get(Run, run_id).status
        return status, page, frames, events

    def test_UT_RUN_05_window_off_replays_without_rendering_a_frame(self):
        import os
        from app.config import ARTIFACTS
        status, page, frames, events = self._drive(display_window=False, viewers=1)
        self.assertEqual(status, "passed", "the replay must still succeed headless")
        self.assertEqual([s for s in page.shots if s == "jpeg"], [], "no screencast frames")
        self.assertIn("png", page.shots, "step screenshots are still taken")
        self.assertEqual(frames, [], "no frame is pushed to the dashboard")
        self.assertFalse(events[-1]["display_window"])
        # Screenshots are written detached from the replay path, so the run must
        # not finish before they are on disk -- otherwise the audit links 404.
        with SessionLocal() as db:
            log = db.query(Run).order_by(Run.created_at.desc()).first().execution_log
        for entry in log:
            if entry.get("screenshot"):
                name = entry["screenshot"].rsplit("/", 2)
                path = os.path.join(ARTIFACTS, "runs", name[-2], name[-1])
                self.assertTrue(os.path.isfile(path), f"{path} was never written")

    def test_UT_RUN_06_window_on_with_a_viewer_streams_frames(self):
        status, page, frames, events = self._drive(display_window=True, viewers=1)
        self.assertEqual(status, "passed")
        self.assertGreater(len([s for s in page.shots if s == "jpeg"]), 0)
        self.assertTrue(events[-1]["display_window"])

    def test_UT_RUN_07_window_on_but_nobody_watching_renders_nothing(self):
        status, page, frames, events = self._drive(display_window=True, viewers=0)
        self.assertEqual(status, "passed")
        self.assertEqual([s for s in page.shots if s == "jpeg"], [],
                         "an unwatched run must not pay for frames")

    def test_UT_RUN_08_merged_repeat_executes_every_occurrence(self):
        """A step recorded as x3 performs three actions on replay."""
        from app.db import Project, Recording, RecordingStep
        import app.executor as executor
        from tests.fakes import FakePage, FakePlaywright
        with SessionLocal() as db:
            project = Project(name="repeat-exec", base_url="")
            db.add(project); db.commit(); db.refresh(project)
            recording = Recording(project_id=project.id, name="repeat", start_url="http://127.0.0.1/demo.html")
            db.add(recording); db.commit(); db.refresh(recording)
            db.add(RecordingStep(recording_id=recording.id, order=1, action="navigate",
                                 value="/demo.html", label="Open"))
            db.add(RecordingStep(recording_id=recording.id, order=2, action="click",
                                 label="Next", selector={"primary": "#next", "x": 5, "y": 6},
                                 repeat_count=3))
            db.commit()
            run = Run(recording_id=recording.id, status="queued")
            db.add(run); db.commit(); db.refresh(run)
            run_id = run.id

        page = FakePage(known=["#next"])
        fake = FakePlaywright(page)

        async def on_event(event):
            pass

        original = executor.async_playwright
        executor.async_playwright = lambda: fake
        try:
            asyncio.run(execute_run(run_id, on_event, display_window=False,
                                    viewer_count=lambda: 0))
        finally:
            executor.async_playwright = original
        with SessionLocal() as db:
            self.assertEqual(db.get(Run, run_id).status, "passed")
        self.assertEqual(len([c for c in page.clicks if c[1] == "#next"]), 3,
                         "repeat_count must be honoured on replay")



class GuardrailTests(unittest.TestCase):
    """The limits that keep the hosted instance inside its memory ceiling."""

    def test_UT_MEM_01_browser_budget_serialises_and_counts(self):
        from app.guardrails import BrowserBudget
        budget = BrowserBudget(1)
        order = []

        async def worker(name, delay):
            async with budget.slot(name):
                order.append("in:" + name)
                await asyncio.sleep(delay)
                order.append("out:" + name)

        async def exercise():
            await asyncio.gather(worker("a", 0.05), worker("b", 0.01))

        asyncio.run(exercise())
        # With one permit, b cannot start until a has finished.
        self.assertEqual(order, ["in:a", "out:a", "in:b", "out:b"])
        report = budget.report()
        self.assertEqual(report["active"], 0, "permits must be handed back")
        self.assertEqual(report["waiting"], 0)
        self.assertEqual(report["peak"], 1, "never more than the limit")

    def test_UT_MEM_02_cancelled_waiter_does_not_leak_a_permit(self):
        from app.guardrails import BrowserBudget
        budget = BrowserBudget(1)

        async def exercise():
            held = await budget.acquire("holder")
            pending = asyncio.create_task(budget.acquire("waiter"))
            await asyncio.sleep(0.02)
            self.assertEqual(budget.report()["waiting"], 1)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            budget.release(held)

        asyncio.run(exercise())
        self.assertEqual(budget.report()["waiting"], 0)
        self.assertEqual(budget.report()["active"], 0)

    def test_UT_MEM_03_live_frames_are_bounded_and_expire(self):
        from app.guardrails import BoundedFrames
        frames = BoundedFrames(limit=2, ttl=0.05)
        frames.put("r1", b"a" * 100)
        frames.put("r2", b"b" * 100)
        frames.put("r3", b"c" * 100)
        self.assertIsNone(frames.get("r1"), "oldest frame must be evicted")
        self.assertEqual(frames.get("r3"), b"c" * 100)
        self.assertEqual(frames.report()["runs"], 2)
        import time as _time
        _time.sleep(0.08)
        self.assertIsNone(frames.get("r2"), "frames must age out even under the cap")
        frames.put("r4", b"d")
        frames.forget("r4")
        self.assertIsNone(frames.get("r4"))

    def test_UT_MEM_04_run_buffers_are_bounded(self):
        from app.guardrails import BoundedRunBuffers
        buffers = BoundedRunBuffers(max_runs=2, max_events=3)
        for index in range(10):
            buffers.append("run-a", {"type": "step", "order": index})
        self.assertEqual([item["order"] for item in buffers.get("run-a")], [7, 8, 9],
                         "the event cap must hold and keep the newest events")
        # A frame replaces the previous frame rather than accumulating.
        buffers.append("run-a", {"type": "frame", "data": "one"})
        buffers.append("run-a", {"type": "frame", "data": "two"})
        self.assertEqual([i for i in buffers.get("run-a") if i["type"] == "frame"][-1]["data"], "two")
        self.assertEqual(len([i for i in buffers.get("run-a") if i["type"] == "frame"]), 1)
        # Now exceed the run cap: the oldest run must go.
        buffers.append("run-b", {"type": "status"})
        buffers.append("run-c", {"type": "status"})
        self.assertEqual(buffers.report()["runs"], 2, "run cap must hold")
        self.assertEqual(buffers.get("run-a"), [], "oldest run must be evicted")
        self.assertEqual(buffers.get("run-c"), [{"type": "status"}])
        buffers.forget("run-b")
        self.assertEqual(buffers.get("run-b"), [])

    def test_UT_MEM_05_recorder_and_executor_share_one_budget(self):
        """A recording session and a run must not each hold a Chromium."""
        import app.executor as executor
        import app.recorder as recorder
        self.assertIs(executor.browser_budget, recorder.browser_budget)

    def test_UT_MEM_06_two_runs_never_hold_a_browser_at_once(self):
        import app.executor as executor
        from app.guardrails import BrowserBudget
        from tests.fakes import FakePage, FakePlaywright
        from app.db import Project, Recording, RecordingStep

        with SessionLocal() as db:
            project = Project(name="budget-runs", base_url="")
            db.add(project); db.commit(); db.refresh(project)
            run_ids = []
            for index in range(2):
                recording = Recording(project_id=project.id, name=f"b{index}",
                                      start_url="http://127.0.0.1/demo.html")
                db.add(recording); db.commit(); db.refresh(recording)
                db.add(RecordingStep(recording_id=recording.id, order=1, action="navigate",
                                     value="/demo.html", label="Open"))
                db.commit()
                run = Run(recording_id=recording.id, status="queued")
                db.add(run); db.commit(); db.refresh(run)
                run_ids.append(run.id)

        # Track how many fake browsers are open simultaneously.
        concurrent = {"now": 0, "peak": 0}
        original_launch = None

        class CountingPlaywright(FakePlaywright):
            async def launch(self, **kwargs):
                concurrent["now"] += 1
                concurrent["peak"] = max(concurrent["peak"], concurrent["now"])
                try:
                    return await super().launch(**kwargs)
                finally:
                    concurrent["now"] -= 1

        original_budget = executor.browser_budget
        executor.browser_budget = BrowserBudget(1)
        original_pw = executor.async_playwright
        executor.async_playwright = lambda: CountingPlaywright(FakePage(known=[]))

        async def on_event(event):
            await asyncio.sleep(0.02)

        async def exercise():
            await asyncio.gather(*[
                executor.execute_run(rid, on_event, display_window=False,
                                     viewer_count=lambda: 0)
                for rid in run_ids
            ])

        try:
            asyncio.run(exercise())
        finally:
            executor.browser_budget = original_budget
            executor.async_playwright = original_pw
        self.assertEqual(concurrent["peak"], 1, "two runs must not open two browsers")


class RunListGuardrailTests(unittest.TestCase):
    def _make_runs(self, count):
        from app.db import Project, Recording, RecordingStep
        with SessionLocal() as db:
            project = Project(name="run-list", base_url="")
            db.add(project); db.commit(); db.refresh(project)
            ids = []
            for index in range(count):
                recording = Recording(project_id=project.id, name=f"l{index}",
                                      start_url="http://127.0.0.1/demo.html")
                db.add(recording); db.commit(); db.refresh(recording)
                db.add(RecordingStep(recording_id=recording.id, order=1, action="navigate",
                                     value="/demo.html", label="Open",
                                     selector={"primary": "#x" * 50}))
                db.commit()
                ids.append(recording.id)
            return ids

    def test_UT_API_01_run_list_is_bounded_and_light_by_default(self):
        from fastapi.testclient import TestClient
        from app import guardrails
        from app.main import app
        self._make_runs(3)
        with TestClient(app) as client:
            rows = client.get("/api/runs").json()
            self.assertLessEqual(len(rows), guardrails.DEFAULT_RUN_LIST_LIMIT)
            for row in rows:
                self.assertNotIn("execution_log", row,
                                 "the polled list must not carry every step")
                self.assertIn("step_count", row)
            heavy = client.get("/api/runs?limit=2&full=true").json()
            self.assertEqual(len(heavy), 2, "limit must be honoured")
            self.assertIn("execution_log", heavy[0])
            capped = client.get("/api/runs?limit=100000").json()
            self.assertLessEqual(len(capped), guardrails.MAX_RUN_LIST_LIMIT)


class LogsSourceTests(unittest.TestCase):
    def test_UT_LOG_01_logs_are_pointed_at_github_not_this_server(self):
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app
        original = library_store.repo_ref
        library_store.repo_ref = lambda: {"slug": "acme/testforge", "branch": "main", "available": True}
        try:
            with TestClient(app) as client:
                payload = client.get("/api/logs/source").json()
        finally:
            library_store.repo_ref = original
        self.assertTrue(payload["available"])
        self.assertEqual(payload["base"], "https://raw.githubusercontent.com/acme/testforge/main")
        self.assertEqual(payload["index"],
                         "https://raw.githubusercontent.com/acme/testforge/main/logs/index.md")
        # Coordinates only: the service must not read or serve the log bodies.
        self.assertNotIn("content", payload)
        self.assertNotIn("files", payload)

    def test_UT_LOG_02_logs_source_rejects_a_bad_branch(self):
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app
        original = library_store.repo_ref
        library_store.repo_ref = lambda: {"slug": "acme/testforge", "branch": "main", "available": True}
        try:
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/logs/source?branch=..%2Fetc").status_code, 422)
                self.assertEqual(client.get("/api/logs/source?branch=main").status_code, 200)
        finally:
            library_store.repo_ref = original

    def test_UT_LOG_03_logs_source_explains_a_missing_checkout(self):
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app
        original = library_store.repo_ref
        library_store.repo_ref = lambda: {"slug": None, "branch": None, "available": False}
        try:
            with TestClient(app) as client:
                payload = client.get("/api/logs/source").json()
        finally:
            library_store.repo_ref = original
        self.assertFalse(payload["available"])
        self.assertIn("checkout", payload["reason"].lower())


class ArtifactExportTests(unittest.TestCase):
    def test_UT_MEM_07_artifact_export_refuses_to_buffer_the_world(self):
        import os
        from fastapi.testclient import TestClient
        from app import guardrails
        from app.config import ARTIFACTS
        from app.main import app
        folder = os.path.join(ARTIFACTS, "runs", "zip-guard")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "big.bin"), "wb") as handle:
            handle.write(b"x" * 4096)
        original = guardrails.MAX_ZIP_BYTES
        guardrails.MAX_ZIP_BYTES = 1024
        try:
            with TestClient(app) as client:
                response = client.get("/api/sync/github")
                self.assertEqual(response.status_code, 413)
                self.assertIn("exceed", response.json()["detail"])
        finally:
            guardrails.MAX_ZIP_BYTES = original

    def test_UT_MEM_08_artifact_export_streams_within_the_cap(self):
        import io
        import os
        import zipfile
        from fastapi.testclient import TestClient
        from app import guardrails
        from app.config import ARTIFACTS
        from app.main import app
        folder = os.path.join(ARTIFACTS, "runs", "zip-ok")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "small.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello")
        original = guardrails.MAX_ZIP_BYTES
        guardrails.MAX_ZIP_BYTES = 64 * 1024 * 1024
        try:
            with TestClient(app) as client:
                response = client.get("/api/sync/github")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "application/zip")
                archive = zipfile.ZipFile(io.BytesIO(response.content))
                self.assertIn("runs/zip-ok/small.txt", archive.namelist())
        finally:
            guardrails.MAX_ZIP_BYTES = original



if __name__ == "__main__":
    unittest.main()
