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
    """Pen-test scenarios: attack the credential surface and pin its blast radius."""

    def _no_checkout(self):
        from app import library_store
        original = library_store.git_root
        library_store.git_root = lambda: None
        library_store.invalidate_repo_ref()
        return original

    def _restore(self, original):
        from app import library_store
        library_store.git_root = original
        library_store.invalidate_repo_ref()

    def test_UT_SEC_01_redact_password(self):
        raw = "could not connect to postgresql://qa_user:s3cret@db.internal/testforge"
        cleaned = redact(raw)
        self.assertNotIn("s3cret", cleaned)
        self.assertIn("qa_user:***@", cleaned)

    def test_UT_SEC_02_configuration_surfaces_never_carry_the_token(self):
        """Attack: scrape every configuration surface for the credential.

        `describe()`, `status()` and both `publish_outcome()` payloads must
        serialize without the token even when it is configured and working.
        """
        import json
        from app import github_api, library_store

        token = "github_pat_" + "S" * 18
        original = self._no_checkout()
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN=token)
        try:
            surfaces = {
                "describe": github_api.describe(),
                "status": library_store.status(),
                "outcome_failed": library_store.publish_outcome(False, "push failed"),
                "outcome_ok": library_store.publish_outcome(True),
            }
        finally:
            _env_restore(saved)
            self._restore(original)
        self.assertTrue(surfaces["describe"]["available"], surfaces["describe"])
        blob = json.dumps(surfaces)
        self.assertNotIn(token, blob)

    def test_UT_SEC_03_hostile_github_errors_are_scrubbed_at_the_transport(self):
        """Attack: a GitHub or proxy error body quotes credentials back at us.

        The live token, an unknown `github_pat_…` token and a bare
        `Authorization:` header must all become `***` before `GitHubAPIError`
        leaves `request()`.
        """
        import io
        import json
        import urllib.error
        from unittest import mock
        from app import github_api

        token = "github_pat_" + "S" * 18
        unknown = "github_pat_" + "LEAKED" + "9" * 12
        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_BRANCH="main",
                         TF_GITHUB_TOKEN=token)
        client = github_api.GitHubClient()

        def hostile(request, timeout=None):
            body = json.dumps({"message": f"Bad credentials for Authorization: Bearer {token} and {unknown}"}).encode()
            raise urllib.error.HTTPError(request.full_url, 401, "Bad credentials", {}, io.BytesIO(body))

        def hostile_reason(request, timeout=None):
            raise urllib.error.URLError(f"tunnel failed: Authorization: Bearer {unknown}")

        try:
            with mock.patch("urllib.request.urlopen", hostile):
                with self.assertRaises(github_api.GitHubAPIError) as caught:
                    client.head_sha()
            with mock.patch("urllib.request.urlopen", hostile_reason):
                with self.assertRaises(github_api.GitHubAPIError) as caught2:
                    client.head_sha()
        finally:
            _env_restore(saved)
        for exc in (caught.exception, caught2.exception):
            text = str(exc)
            self.assertNotIn(token, text)
            self.assertNotIn(unknown, text)
            self.assertIn("***", text)

    def test_UT_SEC_04_the_bearer_token_travels_only_to_the_configured_api_host(self):
        """Attack: exfiltrate the token by redirecting where the client sends it.

        The Authorization header is attached only to requests against the
        configured GitHub API base; an API host override moves the whole
        conversation there and nowhere else.
        """
        import json
        from unittest import mock
        from app import github_api

        token = "github_pat_" + "S" * 18
        seen = []

        class _Resp:
            status = 200

            def read(self):
                return json.dumps({"commit": {"sha": "a" * 40}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def spy(request, timeout=None):
            seen.append((request.full_url, request.get_header("Authorization")))
            return _Resp()

        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_BRANCH="main",
                         TF_GITHUB_TOKEN=token)
        try:
            client = github_api.GitHubClient()
            with mock.patch("urllib.request.urlopen", spy):
                client.head_sha()
        finally:
            _env_restore(saved)
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(seen[0][0], "https://api.github.com/repos/acme/qa-library/branches/main")
        self.assertEqual(seen[0][1], f"Bearer {token}")

        seen.clear()
        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_BRANCH="main",
                         TF_GITHUB_TOKEN=token, TF_GITHUB_API="https://ghe.example.test/api/v3")
        try:
            client = github_api.GitHubClient()
            with mock.patch("urllib.request.urlopen", spy):
                client.head_sha()
        finally:
            _env_restore(saved)
        self.assertEqual(len(seen), 1, seen)
        self.assertTrue(seen[0][0].startswith("https://ghe.example.test/api/v3/"), seen)
        self.assertEqual(seen[0][1], f"Bearer {token}")

    def test_UT_SEC_05_publishing_needs_a_named_repository_and_a_token(self):
        """Attack: let a CI runner's stray GITHUB_TOKEN push to a guessed repo.

        Both gates must be shut: a named repository without a token is refused
        with a reason naming TF_GITHUB_TOKEN, and a token without a named
        repository is refused with a reason naming TF_GITHUB_REPO.
        """
        import json
        from app import github_api, library_store

        original = self._no_checkout()
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_TOKEN=None, GITHUB_TOKEN=None, GH_TOKEN=None)
        try:
            info = github_api.describe()
            mode = library_store.publish_mode()
            reason = library_store.publish_disabled_reason() or ""
        finally:
            _env_restore(saved)
            self._restore(original)
        self.assertFalse(info["available"])
        self.assertIn("TF_GITHUB_TOKEN", info["reason"])
        self.assertEqual(mode, "disabled")
        self.assertIn("TF_GITHUB_TOKEN", reason)

        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO=None, TF_GIT_REMOTE=None,
                         TF_GITHUB_URL=None, GITHUB_TOKEN="ghs_ci_runner_token")
        try:
            info = github_api.describe()
        finally:
            _env_restore(saved)
        self.assertFalse(info["available"])
        self.assertIsNone(info["slug"])
        self.assertIn("TF_GITHUB_REPO", info["reason"])
        self.assertNotIn("ghs_ci_runner_token", json.dumps(info))

    def test_UT_SEC_06_a_publish_commits_only_the_library_prefix(self):
        """Attack: smuggle a file outside library/ into the repository tree.

        Every entry the API publisher stages must start with `library/`, a
        sibling file next to the library root must never appear, and the ref
        update must be fast-forward only.
        """
        import tempfile
        from app import github_api

        root = Path(tempfile.mkdtemp(prefix="tf-pentest-"))
        lib = root / "library"
        (lib / "projects" / "demo").mkdir(parents=True)
        (lib / "projects" / "demo" / "project.json").write_text('{"id": "demo"}', encoding="utf-8")
        (lib / "catalog.json").write_text("{}", encoding="utf-8")
        (root / "KEEP.txt").write_text("outside the library", encoding="utf-8")

        class _RecordingClient(FakeGitHubClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.entry_paths = []
                self.ref_force = None

            def create_tree(self, base_sha, entries):
                self.entry_paths = [entry.get("path") for entry in entries]
                return super().create_tree(base_sha, entries)

            def update_ref(self, sha, force=False):
                self.ref_force = force
                return super().update_ref(sha, force)

        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_TOKEN="tok")
        client = _RecordingClient(head="a" * 40, remote={})
        try:
            report = github_api.publish("Save library", lib, client=client)
        finally:
            _env_restore(saved)
        self.assertTrue(report["published"], report)
        self.assertTrue(client.entry_paths, "nothing was staged")
        self.assertTrue(all(p.startswith("library/") for p in client.entry_paths), client.entry_paths)
        self.assertFalse(any("KEEP" in (p or "") for p in client.entry_paths), client.entry_paths)
        self.assertTrue(all(p.startswith("library/") for p in client.remote), client.remote)
        self.assertFalse(client.ref_force, "a publish must never force-update the branch")

    def test_UT_SEC_07_a_dry_run_with_pending_changes_writes_nothing(self):
        """Attack: probe the publish endpoint and cause an unexpected push.

        With changes pending, a dry run must perform reads only: no blob, tree,
        commit or ref call ever reaches the transport.
        """
        import tempfile
        from app import github_api

        root = Path(tempfile.mkdtemp(prefix="tf-pentest-dry-"))
        (root / "catalog.json").write_text("{}", encoding="utf-8")
        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_TOKEN="tok")
        client = FakeGitHubClient(head=None, remote={})
        try:
            report = github_api.publish("Save library", root, client=client, dry_run=True)
        finally:
            _env_restore(saved)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["state"], "pending")
        self.assertEqual(client.blobs_created, 0)
        self.assertIsNone(client.commit_sha)
        self.assertIsNone(client.updated_ref)
        self.assertEqual({method for method, _path in client.calls}, {"GET"}, client.calls)

    def test_UT_SEC_08_a_branch_that_would_alter_the_request_path_is_refused(self):
        """Attack: put path traversal or query syntax in TF_GITHUB_BRANCH.

        The branch name is interpolated into request paths, so a hostile value
        must leave publishing unavailable and no request may be built at all.
        Legal names — including slashes and dots — must still work.
        """
        from unittest import mock
        from app import github_api

        sent = []

        def spy(request, timeout=None):
            sent.append(request.full_url)
            raise AssertionError("no request may be built for a hostile branch")

        for bad in ("main/../../repos/victim/x", "main?per_page=1", "main#frag",
                    "main\nx", "main.lock", ".hidden", "main/../..",
                    "a..b", "main%2e%2e"):
            saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_TOKEN="tok",
                             TF_GITHUB_BRANCH=bad)
            try:
                info = github_api.describe()
                client = github_api.GitHubClient()
                with mock.patch("urllib.request.urlopen", spy):
                    with self.assertRaises(github_api.GitHubAPIError):
                        client.head_sha()
            finally:
                _env_restore(saved)
            self.assertFalse(info["available"], bad)
            self.assertIn("branch", info["reason"].lower(), bad)
            self.assertEqual(sent, [], bad)

        # Whitespace is neutralised rather than refused: `branch()` strips it
        # before it can reach a request path, so padding cannot smuggle anything.
        for good in ("main", "release/2026.10", "user.feature", "  release/x  "):
            saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_TOKEN="tok",
                             TF_GITHUB_BRANCH=good)
            try:
                info = github_api.describe()
            finally:
                _env_restore(saved)
            self.assertTrue(info["available"], (good, info))
        self.assertEqual(info["branch"], "release/x")

        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_TOKEN="tok",
                         TF_GITHUB_BRANCH="main")
        try:
            client = github_api.GitHubClient()
            with mock.patch("urllib.request.urlopen", spy):
                with self.assertRaises(github_api.GitHubAPIError):
                    client.tree("../../users/victim")
        finally:
            _env_restore(saved)
        self.assertEqual(sent, [], "a hostile tree SHA must not reach the transport")

    def test_UT_SEC_09_the_harness_strips_publishing_credentials_before_boot(self):
        """Regression: a developer shell must never publish from a test run.

        tests/harness.py removes every publishing credential from the server
        environment before spawning uvicorn, and this test fails if that guard
        is ever deleted or reordered after the spawn.
        """
        source = (ROOT / "tests" / "harness.py").read_text(encoding="utf-8")
        for name in ("TF_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
                     "TF_GITHUB_REPO", "TF_GIT_REMOTE"):
            self.assertIn(f'"{name}"', source, name)
        strip = source.find("env.pop(secret, None)")
        boot = source.find("subprocess.Popen")
        self.assertNotEqual(strip, -1, "the harness no longer strips publishing credentials")
        self.assertTrue(0 < strip < boot,
                        "credentials must be stripped before the test server starts")

    def test_UT_SEC_10_the_http_surface_never_echoes_the_token(self):
        """Attack: scrape the live HTTP surface while GitHub answers hostilely.

        Every read endpoint must respond without the configured token or an
        unknown `github_pat_…` quoted in a hostile GitHub error body, while the
        redaction marker shows the scrub actually ran.
        """
        import io
        import json
        import urllib.error
        from unittest import mock
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app

        token = "github_pat_" + "S" * 18
        unknown = "github_pat_" + "LEAKED" + "9" * 12
        sent = []

        def hostile(request, timeout=None):
            sent.append(request.full_url)
            body = json.dumps({"message": f"Server said Authorization: Bearer {token} and {unknown}"}).encode()
            raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(body))

        original = self._no_checkout()
        original_pending = library_store.publish_pending
        library_store.publish_pending = lambda: True
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN=token)
        bodies = {}
        try:
            with mock.patch("urllib.request.urlopen", hostile):
                with TestClient(app) as client:
                    for path in ("/api/health", "/api/diagnostics", "/api/library",
                                 "/api/library/publish/plan"):
                        response = client.get(path)
                        self.assertLess(response.status_code, 500, path)
                        bodies[path] = response.text
        finally:
            _env_restore(saved)
            library_store.publish_pending = original_pending
            self._restore(original)
        self.assertTrue(sent, "the hostile GitHub response was never exercised")
        blob = json.dumps(bodies)
        for needle in (token, unknown):
            self.assertNotIn(needle, blob)
        self.assertIn("***", blob, "redaction did not run on the HTTP surface")

    def test_UT_SEC_11_a_failed_push_error_is_scrubbed_before_the_banner(self):
        """Attack: leak the token through the save banner's error detail.

        publish_outcome prints the push error verbatim, so it must come back
        with credential-shaped text replaced by `***` while the useful message
        survives.
        """
        import json
        from app import library_store

        token = "github_pat_" + "S" * 18
        original = self._no_checkout()
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN=token)
        try:
            outcome = library_store.publish_outcome(
                False,
                f"remote rejected the push for {token} via https://qa:{token}@github.com/acme/qa/",
            )
            ok = library_store.publish_outcome(True)
        finally:
            _env_restore(saved)
            self._restore(original)
        blob = json.dumps([outcome, ok])
        self.assertNotIn(token, blob)
        self.assertIn("remote rejected the push", outcome["publish_message"])
        self.assertIn("***", outcome["publish_message"])
        self.assertIn("***", outcome["publish_error"])
        self.assertTrue(outcome["retry_scheduled"])


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
        # Every TestCase in this module, discovered rather than listed: a new
        # class used to be invisible to this check, so its IDs could go
        # undocumented without failing anything.
        import inspect as _inspect
        import sys as _sys
        module = _sys.modules[__name__]
        classes = [
            obj for _name, obj in vars(module).items()
            if _inspect.isclass(obj) and issubclass(obj, unittest.TestCase)
        ]
        for cls in classes:
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



class PublishModeTests(unittest.TestCase):
    """A save must report what really happened to it, and promise only real retries."""

    def _no_checkout(self):
        from app import library_store
        original = library_store.git_root
        library_store.git_root = lambda: None
        library_store.invalidate_repo_ref()
        return original

    def _restore(self, original):
        from app import library_store
        library_store.git_root = original
        library_store.invalidate_repo_ref()

    def test_UT_PUB_01_disabled_publishing_promises_no_retry(self):
        """With publishing off, the message must not say a retry is coming.

        This is the reported bug: the record tab showed "Saved locally; GitHub
        push will retry in 1 minute: GitHub publishing is disabled" — two
        contradictions in one sentence, because no retry loop is started when
        publishing is disabled.
        """
        from app import library_store
        original = self._no_checkout()
        try:
            self.assertEqual(library_store.publish_mode(), "disabled")
            self.assertFalse(library_store.publish_enabled())
            outcome = library_store.publish_outcome(False)
        finally:
            self._restore(original)
        self.assertEqual(outcome["publish_state"], "local-only")
        self.assertFalse(outcome["retry_scheduled"])
        self.assertIsNone(outcome["retry_in_seconds"])
        message = outcome["publish_message"].lower()
        self.assertIn("disabled", message)
        self.assertNotIn("will be retried", message)
        self.assertIn("no .git", outcome["publish_error"].lower())

    def test_UT_PUB_02_failed_push_states_the_retry_interval(self):
        """A real push failure keeps the retry promise, with the configured wait."""
        from app import library_store
        saved = _env_set(TF_LIBRARY_PUBLISH="1")
        original = library_store.git_root
        library_store.git_root = lambda: Path("/nonexistent-checkout")
        try:
            self.assertEqual(library_store.publish_mode(), "checkout")
            outcome = library_store.publish_outcome(False, "remote rejected the push")
            published = library_store.publish_outcome(True)
        finally:
            library_store.git_root = original
            _env_restore(saved)
        self.assertEqual(outcome["publish_state"], "retry-pending")
        self.assertTrue(outcome["retry_scheduled"])
        self.assertEqual(outcome["retry_in_seconds"], library_store.PUBLISH_RETRY_SECONDS)
        self.assertIn("remote rejected the push", outcome["publish_message"])
        self.assertEqual(published["publish_state"], "published")
        self.assertFalse(published["retry_scheduled"])

    def test_UT_PUB_03_api_publisher_commits_and_verifies(self):
        """With no checkout, the REST publisher commits blobs, a tree, a commit and a ref.

        The push is only reported as published after the branch is read back.
        """
        import tempfile
        from app import github_api

        root = Path(tempfile.mkdtemp(prefix="tf-api-lib-"))
        (root / "projects").mkdir()
        (root / "catalog.json").write_text('{"source": "repository", "projects": []}', encoding="utf-8")
        project = root / "projects" / "demo"
        project.mkdir()
        (project / "project.json").write_text('{"id": "demo", "name": "Demo"}', encoding="utf-8")
        (project / "variables.json").write_text("[]", encoding="utf-8")

        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_BRANCH="main",
                         TF_GITHUB_TOKEN="ghp_secret_token_value")
        client = FakeGitHubClient(head="a" * 40, remote={})
        try:
            self.assertTrue(github_api.enabled())
            report = github_api.publish("Save library", root, client=client)
        finally:
            _env_restore(saved)

        self.assertTrue(report["published"], report)
        self.assertEqual(report["added"], ["library/catalog.json", "library/projects/demo/project.json",
                                            "library/projects/demo/variables.json"])
        self.assertEqual(report["files"], 3)
        methods = [method for method, _path in client.calls]
        # 3 blobs, one tree, one commit, one ref update, and two reads of the branch
        self.assertEqual(methods.count("POST"), 5)
        self.assertEqual(methods.count("PATCH"), 1)
        self.assertEqual(client.blobs_created, 3)
        self.assertEqual(client.tree_entries, 3)
        self.assertEqual(client.updated_ref, client.commit_sha)
        self.assertTrue(client.verified_after_push)
        # The blob SHA git would compute, without a git binary.
        expected = github_api.blob_sha(b'{"id": "demo", "name": "Demo"}')
        self.assertIn(expected, client.blob_shas)

    def test_UT_PUB_04_api_publisher_is_a_no_op_when_synced(self):
        """A second publish with nothing changed must not create a commit."""
        import tempfile
        from app import github_api

        root = Path(tempfile.mkdtemp(prefix="tf-api-lib2-"))
        (root / "catalog.json").write_text("{}", encoding="utf-8")
        head = "b" * 40
        remote = {"library/catalog.json": {"sha": github_api.blob_sha(b"{}"), "size": 2}}
        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_TOKEN="tok")
        client = FakeGitHubClient(head=head, remote=remote)
        try:
            report = github_api.publish("Save library", root, client=client)
            dry = github_api.publish("Save library", root, client=client, dry_run=True)
        finally:
            _env_restore(saved)
        self.assertFalse(report["published"])
        self.assertEqual(report["state"], "synced")
        self.assertEqual(client.blobs_created, 0)
        self.assertIsNone(client.commit_sha)
        self.assertTrue(dry["dry_run"])

    def test_UT_PUB_05_api_publisher_bounds_the_push_and_hides_the_token(self):
        """Oversized files are skipped with a reason, and the token never appears."""
        import tempfile
        from app import github_api

        root = Path(tempfile.mkdtemp(prefix="tf-api-lib3-"))
        (root / "small.json").write_text("{}", encoding="utf-8")
        (root / "huge.bin").write_bytes(b"x" * 4096)
        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_TOKEN="ghp_super_secret")
        original_cap = github_api.MAX_FILE_BYTES
        github_api.MAX_FILE_BYTES = 1024
        try:
            collected = github_api.collect_local_files(root)
            failing = FakeGitHubClient(head=None, remote={}, error="Bad credentials for ghp_super_secret")
            with self.assertRaises(github_api.GitHubAPIError) as caught:
                failing.head_sha()
        finally:
            github_api.MAX_FILE_BYTES = original_cap
            _env_restore(saved)
        self.assertIn("library/small.json", collected["files"])
        self.assertNotIn("library/huge.bin", collected["files"])
        self.assertEqual(collected["skipped"][0]["path"], "library/huge.bin")
        self.assertIn("larger than", collected["skipped"][0]["reason"])
        self.assertNotIn("ghp_super_secret", str(caught.exception))
        self.assertIn("***", str(caught.exception))
        # The redactor is what keeps a token out of the dashboard, and it must
        # work without the environment still holding the token.
        self.assertEqual(github_api._safe("Authorization: Bearer ghp_super_secret"),
                         "Authorization: ***")
        self.assertEqual(github_api._safe("push failed for ghp_super_secret"),
                         "push failed for ***")
        # A remote URL can carry the token itself, so the redactor handles that
        # shape too. Built from chr(64) so no credential-shaped literal sits in
        # this file.
        at = chr(64)
        self.assertEqual(
            github_api._safe("fatal: unable to access 'https://qa:" + "ghp_super_secret" + at + "github.com/acme/qa/'"),
            "fatal: unable to access 'https://qa:***" + at + "github.com/acme/qa/'",
        )

    def test_UT_PUB_06_status_reports_api_publishing_instead_of_dashes(self):
        """The GitHub tab shows a branch and a mode when the API can publish."""
        from app import library_store
        original = self._no_checkout()
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="release", TF_GITHUB_TOKEN="tok")
        try:
            info = library_store.status()
        finally:
            _env_restore(saved)
            self._restore(original)
        self.assertEqual(info["publish_mode"], "api")
        self.assertTrue(info["publish_enabled"])
        self.assertEqual(info["git_state"], "api")
        self.assertEqual(info["branch"], "release")
        self.assertTrue(info["api_publish"]["available"])
        self.assertIn("GitHub API", info["git_note"])
        self.assertEqual(info["repository_url"], "https://github.com/acme/qa-library")
        # An empty library is reported as an empty library, never as a git problem.
        self.assertTrue(info["status_reason"] is None or "library tree is empty" in info["status_reason"],
                        info["status_reason"])
        self.assertNotIn("no .git", (info["status_reason"] or "").lower())

    def test_UT_PUB_07_a_stray_token_does_not_enable_publishing(self):
        """CI runners have a GITHUB_TOKEN; that alone must not push anywhere."""
        from app import github_api, library_store
        original = self._no_checkout()
        saved = _env_set(TF_LIBRARY_PUBLISH=None, GITHUB_TOKEN="ghs_actions_token",
                         TF_GITHUB_REPO=None, TF_GIT_REMOTE=None)
        try:
            self.assertFalse(github_api.enabled())
            self.assertEqual(library_store.publish_mode(), "disabled")
            self.assertIsNone(github_api.repo_slug())
        finally:
            _env_restore(saved)
            self._restore(original)


class FakeGitHubClient:
    """In-memory stand-in for the GitHub REST API. No network."""

    def __init__(self, head=None, remote=None, error=None):
        from app import github_api
        self.slug = "acme/qa-library"
        self.branch = "main"
        self.token = "token"
        self.head = head
        self.remote = dict(remote or {})
        self.error = error
        self.calls = []
        self.blobs_created = 0
        self.blob_shas = []
        self.tree_entries = 0
        self.tree_base = None
        self.commit_sha = None
        self.updated_ref = None
        self.verified_after_push = False
        self._github_api = github_api

    def head_sha(self):
        self.calls.append(("GET", "/branches"))
        if self.error:
            # The real transport strips credentials before raising; so does this one.
            raise self._github_api.GitHubAPIError(self._github_api._safe(self.error))
        if self.updated_ref:
            self.verified_after_push = self.head == self.commit_sha
        return self.head

    def tree(self, sha):
        self.calls.append(("GET", "/trees"))
        return dict(self.remote)

    def create_blob(self, data):
        self.calls.append(("POST", "/blobs"))
        self.blobs_created += 1
        sha = self._github_api.blob_sha(data)
        self.blob_shas.append(sha)
        return sha

    def create_tree(self, base_sha, entries):
        self.calls.append(("POST", "/trees"))
        self.tree_entries = len(entries)
        self.tree_base = base_sha
        for entry in entries:
            if entry.get("sha") is None:
                self.remote.pop(entry["path"], None)
            else:
                self.remote[entry["path"]] = {"sha": entry["sha"], "size": 1}
        return "t" * 40

    def create_commit(self, message, tree_sha, parent):
        self.calls.append(("POST", "/commits"))
        self.commit_sha = "c" * 40
        self.commit_message = message
        self.commit_parent = parent
        return self.commit_sha

    def update_ref(self, sha, force=False):
        self.calls.append(("PATCH", "/refs"))
        self.updated_ref = sha
        self.head = sha

    def request(self, method, path, payload=None, accept=None):
        self.calls.append((method, path))
        return 200, {}


class LocalLogTests(unittest.TestCase):
    """The Logs tab fallback for a deployment with no git checkout."""

    def setUp(self):
        import tempfile
        from app import logs_local
        self.logs_local = logs_local
        self.root = Path(tempfile.mkdtemp(prefix="tf-logs-"))
        run = self.root / "20261011-090000"
        run.mkdir()
        (run / "results.md").write_text("| ID | Result |\n| --- | --- |\n| UT-X | passed |\n", encoding="utf-8")
        (run / "harness.log").write_text("line one\nline two\n", encoding="utf-8")
        (self.root / "index.md").write_text(
            "# Harness run index\n\n"
            "| Started | Result | Passed | Failed | Skipped | Revision | Folder |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| 2026-10-11T09:00:00+00:00 | PASS | 90 | 0 | 3 | abc1234 | "
            "[20261011-090000](20261011-090000/results.md) |\n", encoding="utf-8")
        self.saved = _env_set(TF_LOGS_DIR=str(self.root))
        logs_local._CACHE.update({"ts": 0.0, "value": None, "key": None})

    def tearDown(self):
        _env_restore(self.saved)
        self.logs_local._CACHE.update({"ts": 0.0, "value": None, "key": None})

    def test_UT_LOG_04_local_index_and_file_are_served(self):
        index = self.logs_local.index()
        self.assertTrue(index["available"])
        self.assertEqual(index["origin"], "index.md")
        self.assertEqual(index["rows"][0]["folder"], "20261011-090000")
        self.assertEqual(index["rows"][0]["Result"], "PASS")
        body = self.logs_local.read("20261011-090000", "harness.log")
        self.assertTrue(body["ok"])
        self.assertIn("line two", body["text"])
        self.assertFalse(body["truncated"])
        self.assertEqual(self.logs_local.files_in("20261011-090000"), ["harness.log", "results.md"])

    def test_UT_LOG_05_local_reader_refuses_to_escape_and_caps_the_tail(self):
        for folder, name in [("../../etc", "passwd"), ("20261011-090000", "../../secret"),
                             ("..", "index.md"), ("20261011-090000", "results.md.sh")]:
            result = self.logs_local.read(folder, name)
            self.assertFalse(result.get("ok"), f"{folder}/{name} must not be served")
        big = self.root / "20261011-090000" / "unit.log"
        big.write_text("x" * 5000 + "\nTAIL MARKER\n", encoding="utf-8")
        body = self.logs_local.read("20261011-090000", "unit.log", max_bytes=1024)
        self.assertTrue(body["ok"])
        self.assertLessEqual(body["bytes"], 1024)
        self.assertTrue(body["truncated"])
        self.assertIn("TAIL MARKER", body["text"])

    def test_UT_LOG_06_source_falls_back_to_local_and_explains_itself(self):
        """`/api/logs/source` must offer the local reader instead of a dead end."""
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app
        original = library_store.git_root
        library_store.git_root = lambda: None
        library_store.invalidate_repo_ref()
        try:
            with TestClient(app) as client:
                payload = client.get("/api/logs/source").json()
                local_index = client.get("/api/logs/local/index").json()
                body = client.get("/api/logs/local/20261011-090000/results.md")
                escape = client.get("/api/logs/local/..%2F..%2Fetc/passwd")
        finally:
            library_store.git_root = original
            library_store.invalidate_repo_ref()
        self.assertFalse(payload["available"])
        self.assertIn("No git checkout", payload["reason"])
        self.assertTrue(payload["local"]["available"])
        self.assertTrue(any("TF_GITHUB_REPO" in fix for fix in payload["fixes"]))
        self.assertTrue(local_index["available"])
        self.assertEqual(body.status_code, 200)
        self.assertIn("UT-X", body.json()["text"])
        self.assertEqual(escape.status_code, 404)

    def test_UT_LOG_07_repo_coordinates_come_from_configuration(self):
        """No checkout is needed to read committed logs: TF_GITHUB_REPO is enough."""
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app
        original = library_store.git_root
        library_store.git_root = lambda: None
        saved = _env_set(TF_GITHUB_REPO="acme/qa-library", TF_GITHUB_BRANCH="release")
        library_store.invalidate_repo_ref()
        try:
            ref = library_store.repo_ref()
            with TestClient(app) as client:
                payload = client.get("/api/logs/source").json()
                rejected = client.get("/api/logs/source", params={"branch": "../etc"})
        finally:
            _env_restore(saved)
            library_store.git_root = original
            library_store.invalidate_repo_ref()
        self.assertEqual(ref, {"slug": "acme/qa-library", "branch": "release", "available": True,
                               "via": "config", "repository_url": "https://github.com/acme/qa-library"})
        self.assertTrue(payload["available"])
        self.assertEqual(payload["base"], "https://raw.githubusercontent.com/acme/qa-library/release")
        self.assertEqual(payload["index"], "https://raw.githubusercontent.com/acme/qa-library/release/logs/index.md")
        self.assertEqual(rejected.status_code, 422)


class QueueHygieneTests(unittest.TestCase):
    """Old and irrelevant queue entries must leave the queue."""

    def setUp(self):
        from app import library_store, run_queue
        self.run_queue = run_queue
        self.library_store = library_store
        # Through the library, not straight into the database: the run endpoint
        # looks a recording up in library/ before it will queue it.
        project = library_store.create_project("queue-hygiene", "")
        self.project_id = project["id"]
        recording = library_store.create_recording(self.project_id, "queued", "/demo.html")
        self.recording_id = recording["id"]

    def tearDown(self):
        """Leave the queue as it was found.

        The suite shares one database and another scenario asserts that the
        pending queue is empty, so runs this class queued must not outlive it.
        """
        for run_id in self.run_queue.live_run_ids():
            self.run_queue.unregister(run_id)
        with SessionLocal() as db:
            for row in db.query(Run).filter(Run.status.in_(("queued", "running"))).all():
                db.delete(row)
            db.commit()

    def _run(self, status="queued", age_minutes=0.0, recording_id=None, started_at=None):
        from datetime import datetime, timedelta
        with SessionLocal() as db:
            run = Run(recording_id=recording_id or self.recording_id, status=status,
                      started_at=started_at)
            db.add(run); db.commit(); db.refresh(run)
            if age_minutes:
                run.created_at = datetime.utcnow() - timedelta(minutes=age_minutes)
                db.commit()
            return run.id

    def test_UT_QUEUE_01_orphans_are_cancelled_and_live_runs_are_not(self):
        orphan = self._run("queued", age_minutes=10)
        stranded = self._run("running", age_minutes=1)
        fresh = self._run("queued", age_minutes=0)
        live = self._run("queued", age_minutes=10)

        class Worker:
            def done(self):
                return False
        self.run_queue.register(live, Worker())
        try:
            report = self.run_queue.reap_orphans()
        finally:
            self.run_queue.unregister(live)

        with SessionLocal() as db:
            self.assertEqual(db.get(Run, orphan).status, "cancelled")
            self.assertIn("restarted", db.get(Run, orphan).cancel_reason)
            self.assertEqual(db.get(Run, stranded).status, "cancelled")
            # A run queued a moment ago is still being handed to its worker.
            self.assertEqual(db.get(Run, fresh).status, "queued")
            self.assertEqual(db.get(Run, live).status, "queued")
        self.assertEqual(report["cancelled"], 2)

    def test_UT_QUEUE_02_clear_honours_age_and_dry_run(self):
        old = self._run("queued", age_minutes=90)
        new = self._run("queued", age_minutes=1)
        self.run_queue.register(old, type("W", (), {"done": lambda self: False})())
        self.run_queue.register(new, type("W", (), {"done": lambda self: False})())
        try:
            preview = self.run_queue.clear(older_than_minutes=60, include_orphans=False,
                                           include_stale=True, dry_run=True)
            self.assertEqual(preview["matched"], 1)
            self.assertEqual(preview["cancelled"], 0)
            self.assertTrue(preview["dry_run"])
            report = self.run_queue.clear(older_than_minutes=60, include_orphans=False,
                                          include_stale=True)
        finally:
            self.run_queue.unregister(old)
            self.run_queue.unregister(new)
        self.assertEqual(report["cancelled"], 1)
        self.assertEqual(report["by_kind"], {"stale": 1})
        with SessionLocal() as db:
            self.assertEqual(db.get(Run, old).status, "cancelled")
            self.assertEqual(db.get(Run, new).status, "queued")

    def test_UT_QUEUE_03_duplicate_queued_runs_are_reused_not_stacked(self):
        from datetime import datetime as dt
        from fastapi.testclient import TestClient
        import app.main as main
        first = self._run("queued", age_minutes=0, started_at=None)
        duplicate = self.run_queue.duplicate_of(self.recording_id)
        self.assertEqual(duplicate["run_id"], first)
        started = self._run("queued", age_minutes=0, recording_id=self.recording_id, started_at=dt.utcnow())
        self.assertEqual(self.run_queue.duplicate_of(self.recording_id)["run_id"], first)
        self.assertEqual(started != first, True)

        # Through the API: the second RUN press reuses the waiting run.
        original = main.execute_run_task

        async def slow_run(*args, **kwargs):
            await asyncio.sleep(0.05)
        main.execute_run_task = slow_run
        try:
            with TestClient(main.app) as client:
                first_response = client.post("/api/runs", json={"target_id": self.recording_id})
                second = client.post("/api/runs", json={"target_id": self.recording_id})
                forced = client.post("/api/runs", json={"target_id": self.recording_id, "force": True})
        finally:
            main.execute_run_task = original
        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second.json()["deduplicated"], True)
        self.assertEqual(second.json()["run_id"], first_response.json()["run_id"])
        self.assertNotEqual(forced.json()["run_id"], first_response.json()["run_id"])

    def test_UT_QUEUE_04_finished_history_can_be_purged(self):
        from datetime import timedelta
        ancient = self._run("passed", age_minutes=60 * 24 * 40)
        recent = self._run("passed", age_minutes=1)
        queued = self._run("queued", age_minutes=60 * 24 * 40)
        report = self.run_queue.purge_finished(older_than_days=14)
        with SessionLocal() as db:
            self.assertIsNone(db.get(Run, ancient))
            self.assertIsNotNone(db.get(Run, recent))
            # An unfinished run is never purged, only cleared by the reaper.
            self.assertIsNotNone(db.get(Run, queued))
        self.assertEqual(report["purged"], 1)

    def test_UT_QUEUE_05_clear_endpoint_validates_and_reports(self):
        from fastapi.testclient import TestClient
        from app.main import app
        # Queued before boot: the startup reaper owns this one.
        before_boot = self._run("queued", age_minutes=120)
        with TestClient(app) as client:
            with SessionLocal() as db:
                self.assertEqual(db.get(Run, before_boot).status, "cancelled",
                                 "boot must cancel runs a restart left behind")
                self.assertIn("restarted", db.get(Run, before_boot).cancel_reason)
            stale = self._run("queued", age_minutes=120)
            bad = client.post("/api/runs/queue/clear", json={"older_than_minutes": -5})
            bad_text = client.post("/api/runs/queue/clear", json={"older_than_minutes": "soon"})
            # A dry run reports exactly what the real clear would act on.
            preview = client.post("/api/runs/queue/clear", json={"dry_run": True})
            self.assertEqual(preview.json()["matched"], 1)
            self.assertEqual(preview.json()["candidates"][0]["kind"], "orphan")
            status = client.get("/api/runs/queue/status").json()
            self.assertIn("hygiene", status)
            self.assertIn("policy", status["hygiene"])
            self.assertEqual(status["hygiene"]["orphans"], 1)
            cleared = client.post("/api/runs/queue/clear", json={"reason": "Clearing before a demo"})
            after = client.get("/api/runs/queue/status").json()
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad_text.status_code, 422)
        self.assertTrue(preview.json()["dry_run"])
        self.assertEqual(preview.json()["cancelled"], 0)
        self.assertGreaterEqual(cleared.json()["cancelled"], 1)
        self.assertIn("demo", cleared.json()["reason"])
        with SessionLocal() as db:
            self.assertEqual(db.get(Run, stale).status, "cancelled")
        self.assertEqual(after["hygiene"]["clearable"], 0)


class BatchExecutionTests(unittest.TestCase):
    """Adaptive batch execution: one browser, learned budgets, cheap screenshots."""

    def tearDown(self):
        """Batches driven by a stub executor never finish their runs; clean them up."""
        from app import run_queue
        for run_id in run_queue.live_run_ids():
            run_queue.unregister(run_id)
        with SessionLocal() as db:
            for row in db.query(Run).filter(Run.status.in_(("queued", "running"))).all():
                db.delete(row)
            db.commit()

    def _make_recording(self, name, steps, start_url="http://127.0.0.1:8765/demo.html"):
        """A recording in both stores: library/ (what the API selects) and the DB."""
        from app import library_store
        from app.db import RecordingStep
        project = library_store.create_project(name, "")
        recording = library_store.create_recording(project["id"], name, start_url)
        with SessionLocal() as db:
            for order, step in enumerate(steps, 1):
                db.add(RecordingStep(recording_id=recording["id"], order=order,
                                     action=step.get("action"), value=step.get("value"),
                                     label=step.get("label") or step.get("action"),
                                     selector=step.get("selector"),
                                     repeat_count=step.get("repeat", 1)))
            db.commit()
        library_store.export_recording(recording["id"], publish=False)
        return recording["id"], project["id"]

    def _make_batch(self, recording_ids, name="batch"):
        from app.db import Batch
        with SessionLocal() as db:
            batch = Batch(name=name, status="queued", total=len(recording_ids))
            db.add(batch); db.commit(); db.refresh(batch)
            run_ids = []
            for recording_id in recording_ids:
                run = Run(recording_id=recording_id, status="queued", batch_id=batch.id)
                db.add(run); db.commit(); db.refresh(run)
                run_ids.append(run.id)
            return batch.id, run_ids

    STEPS = [
        {"action": "navigate", "value": "/demo.html", "label": "Open"},
        {"action": "click", "label": "Go", "selector": {"primary": "#go", "x": 80, "y": 200}},
    ]

    def test_UT_BATCH_01_one_browser_replays_the_whole_batch(self):
        """Three recordings, one Chromium launch: that is the point of a batch."""
        from app.batch_runner import execute_batch
        from app.db import Batch
        from tests.fakes import BatchFakePlaywright
        ids = [self._make_recording(f"batch-a-{index}", self.STEPS)[0] for index in range(3)]
        batch_id, run_ids = self._make_batch(ids)
        fake = BatchFakePlaywright(known=["#go"])
        events = []

        async def on_event(payload):
            events.append(payload)

        report = asyncio.run(execute_batch(batch_id, on_event, playwright_factory=lambda: fake,
                                           screenshots="none", pacer=AdaptivePacerForTest()))
        self.assertEqual(fake.launches, 1, "a batch must not launch a browser per recording")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["passed"], 3)
        self.assertEqual(report["resources"]["browsers_launched"], 1)
        self.assertEqual(report["savings"]["browser_launches_avoided"], 2)
        self.assertEqual(len(report["results"]), 3)
        self.assertTrue(all(item["seconds"] >= 0 for item in report["results"]))
        with SessionLocal() as db:
            for run_id in run_ids:
                run = db.get(Run, run_id)
                self.assertEqual(run.status, "passed")
                self.assertEqual(run.batch_id, batch_id)
                self.assertIsNotNone(run.started_at)
                self.assertIsNotNone(run.finished_at)
            batch = db.get(Batch, batch_id)
            self.assertEqual(batch.status, "passed")
            self.assertEqual(batch.progress_pct, 100)
            self.assertTrue(batch.report["resources"])
        self.assertTrue(any(event.get("type") == "batch" and event.get("status") == "passed"
                            for event in events))

    def test_UT_BATCH_02_pacing_learns_clamps_and_classifies(self):
        """Learned budgets stay between a floor and the single-run ceiling."""
        from app.batch_runner import DEFAULT_BUDGETS, classify_failure
        pacer = AdaptivePacerForTest()
        for _ in range(4):
            pacer.observe("click", {"primary": "#go"}, 100, True)
        fast = pacer.budgets_for("click", {"primary": "#go"})
        self.assertTrue(fast["learned"])
        self.assertLess(fast["action_ms"], DEFAULT_BUDGETS["action_ms"],
                        "a step that always lands in 100ms must not be given 3s")
        self.assertGreaterEqual(fast["action_ms"], 350)
        for _ in range(4):
            pacer.observe("click", {"primary": "#slow"}, 9000, True)
        slow = pacer.budgets_for("click", {"primary": "#slow"})
        self.assertLessEqual(slow["action_ms"], 10000, "never looser than the ceiling")
        unknown = pacer.budgets_for("click", {"primary": "#never-seen"})
        self.assertEqual(unknown["action_ms"], DEFAULT_BUDGETS["action_ms"])
        self.assertFalse(unknown["learned"])
        escalated = pacer.escalate(fast)
        self.assertTrue(escalated["escalated"])
        self.assertGreater(escalated["action_ms"], fast["action_ms"])
        self.assertEqual(classify_failure("Timeout 400ms exceeded"), "timeout")
        self.assertEqual(classify_failure("net::ERR_NAME_NOT_RESOLVED"), "navigation")
        self.assertEqual(classify_failure("Click has no selector and no coordinates"), "recording")
        self.assertEqual(classify_failure("Unsupported action: dance"), "recording")
        self.assertEqual(classify_failure("element is not visible"), "selector")

    def test_UT_BATCH_03_screenshots_only_where_they_are_worth_it(self):
        """The default batch mode captures a PNG on failure only."""
        from app.batch_runner import execute_batch
        from tests.fakes import BatchFakePlaywright
        good = self._make_recording("batch-shot-good", self.STEPS)[0]
        broken = self._make_recording("batch-shot-broken", [
            {"action": "navigate", "value": "/demo.html"},
            {"action": "click", "label": "Ghost", "selector": {"primary": "#ghost"}},
        ])[0]
        batch_id, _ = self._make_batch([good, broken])
        fake = BatchFakePlaywright(known=["#go"])
        report = asyncio.run(execute_batch(batch_id, None, playwright_factory=lambda: fake,
                                           pacer=AdaptivePacerForTest()))
        shots = [shot for page in fake.pages for shot in page.shots if shot == "png"]
        self.assertEqual(report["screenshots"]["mode"], "failure")
        self.assertEqual(report["screenshots"]["taken"], 1, "only the failed step is captured")
        self.assertEqual(len(shots), 1)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["status"], "partial")
        failed = [result for result in report["results"] if result["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        # The real cause is reported, not a generic "no selector" message.
        self.assertIn("#ghost", failed[0]["error"])

    def test_UT_BATCH_04_transient_failure_is_retried_once(self):
        """A timeout is retried with a relaxed budget; the recording still passes."""
        from app.batch_runner import execute_batch
        from tests.fakes import BatchFakePlaywright
        # No recorded coordinates on purpose: with x/y present the replay falls
        # back to a coordinate click and the timeout never surfaces (UT-REP-03).
        recording_id, _ = self._make_recording("batch-flaky", [
            {"action": "navigate", "value": "/demo.html", "label": "Open"},
            {"action": "click", "label": "Go", "selector": {"primary": "#go"}},
        ])
        batch_id, run_ids = self._make_batch([recording_id])
        fake = BatchFakePlaywright(known=["#go"], flaky={"#go": 1})
        report = asyncio.run(execute_batch(batch_id, None, playwright_factory=lambda: fake,
                                           screenshots="none", retry_transient=True,
                                           pacer=AdaptivePacerForTest()))
        self.assertEqual(report["status"], "passed", report["results"])
        self.assertEqual(report["retries"], 1)
        self.assertEqual(report["pacing"]["escalations"], 1)
        with SessionLocal() as db:
            log = db.get(Run, run_ids[0]).execution_log
        retried = [entry for entry in log if entry.get("retried")]
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["status"], "passed")

    def test_UT_BATCH_05_no_retry_for_a_real_recording_defect(self):
        """A missing selector is not transient: retrying it only costs time."""
        from app.batch_runner import execute_batch
        from tests.fakes import BatchFakePlaywright
        recording_id, _ = self._make_recording("batch-broken", [
            {"action": "navigate", "value": "/demo.html"},
            {"action": "click", "label": "Ghost", "selector": {"primary": "#ghost"}},
        ])
        batch_id, _ = self._make_batch([recording_id])
        fake = BatchFakePlaywright(known=["#go"])
        report = asyncio.run(execute_batch(batch_id, None, playwright_factory=lambda: fake,
                                           screenshots="none", retry_transient=True,
                                           pacer=AdaptivePacerForTest()))
        self.assertEqual(report["retries"], 0)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failed"], 1)

    def test_UT_BATCH_06_plan_groups_origins_shortest_first(self):
        from app.batch_runner import origin_of, plan_order
        items = [
            {"id": "a", "name": "long", "start_url": "https://shop.example/login", "step_count": 9},
            {"id": "b", "name": "short", "start_url": "https://shop.example/cart", "step_count": 2},
            {"id": "c", "name": "other", "start_url": "https://hr.example/", "step_count": 4},
            {"id": "d", "name": "local", "start_url": "/demo.html", "step_count": 1},
        ]
        self.assertEqual(origin_of("/demo.html"), origin_of("http://127.0.0.1:8765/demo.html"))
        plan = plan_order(items)
        # The origin with the most recordings runs together, shortest first inside
        # it, so one warm session covers both shop.example recordings.
        self.assertEqual([item["id"] for item in plan][:2], ["b", "a"])
        self.assertEqual([origin_of(item["start_url"]) for item in plan[:2]],
                         [origin_of("https://shop.example/login")] * 2)
        self.assertEqual(sorted(item["id"] for item in plan), ["a", "b", "c", "d"])

    def test_UT_BATCH_07_batch_endpoints_validate_and_report(self):
        from fastapi.testclient import TestClient
        import app.main as main
        recording_id, project_id = self._make_recording("batch-api", self.STEPS)
        original = main.execute_batch_task
        started = {}

        async def stub_batch(batch_id, on_event=None, **kwargs):
            started["batch_id"] = batch_id
            started["kwargs"] = kwargs
            return {"batch_id": batch_id, "status": "passed"}
        main.execute_batch_task = stub_batch
        try:
            with TestClient(main.app) as client:
                empty = client.post("/api/runs/batch", json={})
                unknown = client.post("/api/runs/batch", json={"recording_ids": ["nope"]})
                bad_mode = client.post("/api/runs/batch",
                                       json={"recording_ids": [recording_id], "screenshots": "every-frame"})
                created = client.post("/api/runs/batch", json={
                    "recording_ids": [recording_id, recording_id], "project_id": project_id,
                    "screenshots": "none", "name": "Nightly"})
                listing = client.get("/api/runs/batches")
                missing = client.get("/api/runs/batch/does-not-exist")
        finally:
            main.execute_batch_task = original
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(bad_mode.status_code, 422)
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        # The same recording twice is one entry, and recording_ids wins over project_id.
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["run_ids"]), 1)
        self.assertEqual(payload["options"]["screenshots"], "none")
        self.assertEqual(started["kwargs"]["screenshots"], "none")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(missing.status_code, 404)

    def test_UT_BATCH_08_cancel_stops_the_rest_of_the_batch(self):
        from fastapi.testclient import TestClient
        import app.main as main
        from app import batch_runner
        ids = [self._make_recording(f"batch-cancel-{index}", self.STEPS)[0] for index in range(3)]
        original = main.execute_batch_task
        hold = asyncio.Event()

        async def stuck_batch(batch_id, on_event=None, **kwargs):
            started["batch_id"] = batch_id
            await asyncio.sleep(0.2)
            return {"batch_id": batch_id, "status": "cancelled"}
        started = {}
        main.execute_batch_task = stuck_batch
        try:
            with TestClient(main.app) as client:
                created = client.post("/api/runs/batch", json={"recording_ids": ids})
                batch_id = created.json()["batch_id"]
                cancelled = client.post(f"/api/runs/batch/{batch_id}/cancel")
                detail = client.get(f"/api/runs/batch/{batch_id}").json()
                again = client.post(f"/api/runs/batch/{batch_id}/cancel")
        finally:
            main.execute_batch_task = original
            batch_runner._CANCELLED.discard(batch_id)
        self.assertEqual(cancelled.json()["ok"], True)
        self.assertGreaterEqual(cancelled.json()["cancelled_runs"], 1)
        self.assertTrue(any(run["status"] == "cancelled" for run in detail["runs"]))
        self.assertEqual(detail["status"], "cancelled")
        _ = hold
        self.assertFalse(again.json()["ok"], "a finished batch cannot be cancelled again")

    def test_UT_BATCH_09_a_second_batch_is_refused_while_one_runs(self):
        """One browser means one batch: the second is refused, not queued behind it."""
        from app import guardrails
        budget = guardrails.batch_budget
        self.assertTrue(budget.acquire())
        try:
            self.assertFalse(budget.acquire())
            self.assertEqual(budget.report()["rejected"], 1)
        finally:
            budget.release()
        self.assertTrue(budget.acquire())
        budget.release()
        self.assertEqual(budget.report()["active"], 0)

    def test_UT_BATCH_10_a_batch_cannot_open_a_second_browser(self):
        """The batch holds the shared budget, so a recording cannot start Chromium too."""
        from app import guardrails
        from app.batch_runner import execute_batch
        from tests.fakes import BatchFakePlaywright
        recording_id, _ = self._make_recording("batch-budget", self.STEPS)
        batch_id, _ = self._make_batch([recording_id])
        fake = BatchFakePlaywright(known=["#go"])
        seen = {}

        async def on_event(payload):
            # Sampled from inside the batch, while the browser slot is held.
            if payload.get("type") == "run" and "active" not in seen:
                seen["active"] = guardrails.browser_budget.report()["active"]
                seen["waiting"] = guardrails.browser_budget.report()["waiting"]

        report = asyncio.run(execute_batch(batch_id, on_event, playwright_factory=lambda: fake,
                                           screenshots="none", pacer=AdaptivePacerForTest()))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(seen["active"], 1, "the batch holds exactly one browser slot")
        self.assertEqual(guardrails.browser_budget.report()["active"], 0,
                         "the slot is released when the batch ends")


def AdaptivePacerForTest():
    """A pacer with no profile file, so tests never touch artifacts/batches."""
    from app.batch_runner import AdaptivePacer
    return AdaptivePacer(path=None, profile={})


if __name__ == "__main__":
    unittest.main()
