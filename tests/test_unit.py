"""Unit tests for the recording and replay fixes.

These call the TestForge library directly. They do not open a browser.
Scenario IDs match tests/SCENARIOS.md.
"""

import asyncio
import os
import re
import unittest
from unittest.mock import patch
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


class _GitHubResponse:
    """Stand-in for what ``urlopen`` hands back, with the headers we read."""

    def __init__(self, payload=None, status=200, headers=None):
        import json
        self._body = json.dumps(payload if payload is not None else {}).encode()
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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

    def test_UT_BRW_05_chromium_args_are_opt_in(self):
        """A custom Chromium can need one extra flag; the default adds none."""
        saved = _env_set(TF_CHROMIUM_ARGS=None, TF_CHROMIUM_LIBS=None, TF_CHROMIUM_PATH=None)
        try:
            self.assertNotIn("--ozone-platform=headless", launch_kwargs()["args"])
        finally:
            _env_restore(saved)
        saved = _env_set(TF_CHROMIUM_ARGS="--ozone-platform=headless --disable-gpu-compositing",
                         TF_CHROMIUM_LIBS="/tmp/tf-fake-libs", TF_CHROMIUM_PATH=None)
        try:
            kwargs = launch_kwargs()
            self.assertEqual(kwargs["args"][-2:], ["--ozone-platform=headless", "--disable-gpu-compositing"])
            # The extra flag belongs to the browser process, never to the server.
            self.assertNotIn("ozone", os.environ.get("LD_LIBRARY_PATH") or "")
            self.assertIn("/tmp/tf-fake-libs", kwargs["env"]["LD_LIBRARY_PATH"])
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


class RateLimitTests(unittest.TestCase):
    """The `@rate_limit` decorator, which guards the cheap HTTP surface.

    The wrapper needs the incoming `Request`, but FastAPI decides what to pass
    a route handler from the handler's own signature. When the wrapper's
    signature stopped matching what FastAPI saw, every decorated route —
    including `/api/health`, the deployment's `healthCheckPath` — answered
    ``TypeError: health() missing 1 required positional argument: 'request'``
    and Render rolled the deploy back.
    """

    def test_UT_RATE_01_decorated_routes_stay_callable(self):
        """Deployment regression: the health probe must answer 200."""
        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200, health.text)
            self.assertEqual(health.json()["status"], "ok")
            # A query parameter on a decorated route still reaches the
            # handler: fast=false must take the slow listing path, which is
            # not the default.
            listed = client.get("/api/projects?fast=false")
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.headers.get("X-Source"), "cached-scan")

    def test_UT_RATE_02_the_wrapper_is_transparent_and_throttles(self):
        """Sync and async handlers work unchanged and the bucket 429s."""
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from app.rate_limiter import _buckets, get_config, rate_limit, set_config

        calls = []
        sub = FastAPI()

        @rate_limit
        def sync_handler(fast: bool = True):
            calls.append(("sync", fast))
            return {"fast": fast}

        @rate_limit
        async def async_handler(request: Request):
            calls.append(("async", request.client.host if request.client else None))
            return {"ok": True}

        sub.add_api_route("/sync", sync_handler, methods=["GET"])
        sub.add_api_route("/async", async_handler, methods=["GET"])

        saved = get_config()
        try:
            set_config(enabled=1, requests=2, period=60)
            _buckets.clear()
            with TestClient(sub) as client:
                # A handler that never asked for `request` must not be handed
                # one, the query parameter must survive the wrapper, and the
                # async handler must be awaited.
                self.assertEqual(client.get("/sync?fast=false").json(), {"fast": False})
                self.assertEqual(client.get("/async").json(), {"ok": True})
                over = client.get("/sync")
                self.assertEqual(over.status_code, 429, over.text)
                self.assertEqual(over.json()["detail"], "Rate limit exceeded")
        finally:
            set_config(enabled=saved["enabled"],
                       requests=saved["requests_per_period"],
                       period=saved["period_seconds"])
            _buckets.clear()
        # Exactly the two allowed calls ran; the 429 never reached the handler.
        self.assertEqual(calls, [("sync", False), ("async", "testclient")])


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
            asyncio.run(replay_step(FakePage(), {"action": "unknown_action"}, {}))

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


    def test_UT_LIB_09_rescue_brings_back_a_database_only_recording(self):
        """A recording whose files are gone is exported before anything deletes it.

        This is the crash the Record tab's RESCUE button repairs: the row is in
        the database but no `recording.json` backs it, and the next materialize
        would drop it as database-only.
        """
        import shutil

        from app import library_store

        project = library_store.create_project("Rescue audit", "")
        recording = library_store.create_recording(project["id"], "Lost flow", "/demo.html", steps=[
            {"order": 1, "action": "navigate", "value": "https://example.com/", "label": "Open"},
        ])
        folder = (Path(os.environ["TF_LIBRARY_DIR"]) / "projects" / project["id"]
                  / "recordings" / recording["id"])
        self.assertTrue((folder / "recording.json").is_file())
        shutil.rmtree(folder)                      # the crash: the tree lost the recording

        report = library_store.library_health()
        self.assertIn(recording["id"], [item["id"] for item in report["database_only_recordings"]])
        self.assertFalse(report["healthy"])

        result = library_store.rescue_library(apply=True)
        self.assertEqual([item["id"] for item in result["rescued"]], [recording["id"]])
        self.assertTrue((folder / "recording.json").is_file())
        self.assertEqual(library_store.get_recording(recording["id"])["steps"][0]["action"], "navigate")
        after = library_store.library_health()
        self.assertEqual(after["database_only_recordings"], [])
        self.assertTrue(after["healthy"])


class EntityTests(unittest.TestCase):
    """Entity/company is a first-class object a project is created against."""

    def test_UT_ENT_01_a_project_without_a_chosen_entity_is_still_tied_to_one(self):
        from app import library_store

        project = library_store.create_project("Legacy file", "https://example.com")
        self.assertEqual(project["entity_id"], library_store.DEFAULT_ENTITY_ID)
        self.assertFalse(project["entity_assigned"])
        self.assertTrue(project["entity_name"])
        again = library_store.get_project(project["id"])
        self.assertEqual(again["entity_id"], library_store.DEFAULT_ENTITY_ID)

    def test_UT_ENT_02_an_entity_is_created_before_the_project_that_uses_it(self):
        from app import library_store

        entity = library_store.create_entity("Harris Farm Markets", code="HFM", industry="Retail")
        self.assertEqual(entity["id"], "harris-farm-markets")
        self.assertEqual(entity["code"], "HFM")
        self.assertIn(entity["id"], [row["id"] for row in library_store.list_entities()])
        project = library_store.create_project("Store replen", "", entity_id=entity["id"])
        self.assertEqual(project["entity_name"], "Harris Farm Markets")
        self.assertTrue(project["entity_assigned"])
        counted = next(row for row in library_store.list_entities() if row["id"] == entity["id"])
        self.assertIn(project["id"], counted["project_ids"])
        # A project cannot be created against an entity that does not exist.
        with self.assertRaises(library_store.NotFound):
            library_store.create_project("Nowhere", "", entity_id="no-such-entity")

    def test_UT_ENT_03_names_are_unique_including_the_implicit_bucket(self):
        from app import library_store

        library_store.create_entity("Acme Corp")
        with self.assertRaises(library_store.LibraryError):
            library_store.create_entity("acme corp")
        with self.assertRaises(library_store.LibraryError):
            library_store.create_entity("  ")
        with self.assertRaises(library_store.LibraryError):
            library_store.create_entity(library_store.DEFAULT_ENTITY_NAME)
        with self.assertRaises(library_store.LibraryError):
            library_store.create_entity("x" * (library_store.MAX_ENTITY_NAME + 1))

    def test_UT_ENT_04_renaming_and_removing_an_entity_keeps_projects_tied(self):
        from app import library_store

        entity = library_store.create_entity("Gamma Retail")
        project = library_store.create_project("Portal", "", entity_id=entity["id"])
        renamed = library_store.update_entity(entity["id"], name="Gamma Holdings")
        self.assertEqual(renamed["name"], "Gamma Holdings")
        self.assertEqual(library_store.get_project(project["id"])["entity_name"], "Gamma Holdings")
        with self.assertRaises(library_store.LibraryError):
            library_store.delete_entity(entity["id"])          # members must be moved
        removed = library_store.delete_entity(entity["id"], reassign_to=library_store.DEFAULT_ENTITY_ID)
        self.assertEqual(removed["reassigned_projects"], [project["id"]])
        moved = library_store.get_project(project["id"])
        self.assertEqual(moved["entity_id"], library_store.DEFAULT_ENTITY_ID)
        self.assertFalse(moved["entity_assigned"])
        # The fallback bucket itself is immutable.
        with self.assertRaises(library_store.LibraryError):
            library_store.delete_entity(library_store.DEFAULT_ENTITY_ID)
        with self.assertRaises(library_store.LibraryError):
            library_store.update_entity(library_store.DEFAULT_ENTITY_ID, name="Nobody")

    def test_UT_ENT_05_registry_lives_in_the_library_tree(self):
        import json

        from app import library_store

        entity = library_store.create_entity("Beta Foods", notes="second tenant")
        path = Path(os.environ["TF_LIBRARY_DIR"]) / "entities.json"
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["source"], "repository")
        row = next(item for item in raw["entities"] if item["id"] == entity["id"])
        self.assertEqual(row["name"], "Beta Foods")
        self.assertEqual(row["notes"], "second tenant")
        # Entities are listed before any project exists, last in the ordering
        # only when they are the fallback bucket.
        ids = [item["id"] for item in library_store.list_entities()]
        self.assertLess(ids.index(entity["id"]), ids.index(library_store.DEFAULT_ENTITY_ID))


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


class QAAgentHubTests(unittest.TestCase):
    """The QA agent hub: nine agents, the rules engine, the optional AI layer and the routes.

    The agents are deterministic functions of their input. The AI layer is optional and
    is replaced by a stub here so no test reaches the network.
    """

    AGENT_IDS = ("requirements", "test-cases", "api-contract", "code-review", "failure-triage",
                 "flaky", "defect-drafting", "regression", "release-summary")

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def test_UT_AGT_01_catalogue_lists_the_nine_agents_and_routes(self):
        from app.main import app
        from app.qa_agents import registry

        self.assertEqual([a["id"] for a in registry.catalogue()["agents"]], list(self.AGENT_IDS))
        paths = {getattr(route, "path", "") for route in app.routes}
        for path in ("/api/agents", "/api/agents/{agent_id}/sample",
                     "/api/agents/{agent_id}/run", "/api/agents/{agent_id}/from-source"):
            self.assertIn(path, paths)

    def test_UT_AGT_02_every_sample_runs_on_rules_with_the_common_envelope(self):
        from app.qa_agents import registry

        envelope_keys = {"agent", "title", "summary", "verdict", "score", "findings",
                         "severity_counts", "artifacts", "warnings", "engine", "ai", "ai_insights", "generated_at"}
        for agent_id in self.AGENT_IDS:
            with self.subTest(agent=agent_id):
                result = registry.run(agent_id, registry.sample(agent_id), "rules")
                self.assertTrue(envelope_keys <= set(result), set(result))
                self.assertEqual(result["engine"], "rules")
                self.assertEqual(result["ai_insights"], [])
                self.assertFalse(result["ai"]["used"])

    def test_UT_AGT_03_requirement_review_flags_vague_and_sensitive_wording(self):
        from app.qa_agents import registry

        result = registry.run("requirements", {"text": "The checkout must be fast and user-friendly.\n"
                                                        "Users pay by card; TBD: what happens when it is declined?"}, "rules")
        ids = {f["id"] for f in result["findings"]}
        self.assertTrue({"REQ-02", "REQ-04", "REQ-03"} <= ids, ids)
        with self.assertRaises(ValueError):
            registry.run("requirements", {"text": "   "}, "rules")

    def test_UT_AGT_04_test_cases_use_boundaries_and_do_not_leak_required_fields(self):
        from app.qa_agents import case_design

        out = case_design.generate("Username must be between 3 and 20 characters.\n"
                                   "Email is required.")
        cases = out["artifacts"]["cases"]
        lengths = {len(c["test_data"]["username"]) for c in cases if "username" in c["test_data"]
                   and c["technique"] == "boundary value analysis"}
        self.assertEqual(lengths, {2, 3, 20, 21})
        titles = [c["title"] for c in cases]
        self.assertFalse(any(t.startswith("username: empty value") for t in titles), titles)
        self.assertTrue(any(t.startswith("email: empty value") for t in titles), titles)
        self.assertEqual(set(out["artifacts"]["counts"]), {"positive", "negative", "boundary"})
        self.assertIsNone(out["score"])

    def test_UT_AGT_05_api_contract_numbers_tests_once_and_reads_swagger_and_yaml(self):
        from app.qa_agents import api_contract

        spec = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"},
                "servers": [{"url": "https://api.example.test"}], "security": [],
                "paths": {"/a/{id}": {"get": {"operationId": "getA", "parameters": [
                              {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                          "responses": {"200": {"description": "ok"}, "400": {"description": "bad"}}}},
                          "/b": {"get": {"operationId": "getB", "responses": {"200": {"description": "ok"}}}}}}
        import json
        out = api_contract.review(json.dumps(spec), "T")
        ids = [t["id"] for t in out["artifacts"]["contract_tests"]]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertTrue(all(i.startswith("AC-API-") for i in ids))
        swagger = {"swagger": "2.0", "info": {"title": "S", "version": "1"}, "host": "x.test",
                   "schemes": ["https"], "paths": {"/c": {"get": {"responses": {"200": {"description": "ok"}}}}}}
        self.assertEqual(api_contract.review(json.dumps(swagger))["summary"].split()[0:2], ["Reviewed", "1"])
        try:
            import yaml  # noqa: F401
        except ImportError:
            return
        yaml_spec = "openapi: 3.0.3\ninfo: {title: Y, version: '1'}\npaths: {}\n"
        self.assertIn("Reviewed", api_contract.review(yaml_spec)["summary"])

    def test_UT_AGT_06_code_review_catches_credentials_and_waits_but_not_env_references(self):
        from app.qa_agents import code_review

        bad = ("def test_login(page):\n"
               "    page.fill('input[name=password]', 'Sup3rSecret!')\n"
               "    time.sleep(5)\n"
               "    assert page.title()\n")
        ids = {f["id"] for f in code_review.review(bad, "test_login.py")["findings"]}
        self.assertIn("CR-06", ids)
        self.assertIn("CR-05", ids)
        clean = ("def test_login(page):\n"
                 "    page.fill('input[name=password]', os.environ['TF_PASSWORD'])\n"
                 "    assert page.title()\n")
        self.assertNotIn("CR-06", {f["id"] for f in code_review.review(clean, "test_login.py")["findings"]})

    def test_UT_AGT_07_triage_classifies_and_reports_executor_disagreement_honestly(self):
        from app.qa_agents import triage

        self.assertEqual(triage.classify("TimeoutError: waiting for locator('#submit') exceeded 5000ms")["category"],
                         "automation")
        self.assertEqual(triage.classify("AssertionError: expected 'Order placed' but received 'Something went wrong' "
                                         "(HTTP 500)")["category"], "product")
        out = triage.triage(errors=["AssertionError: expected 1 but got 2 (HTTP 500)"])
        row = out["artifacts"]["classifications"][0]
        self.assertIsNone(row["executor_agrees"])  # the executor said "other": no opinion, so no agreement claim
        for item in out["artifacts"]["classifications"]:
            self.assertGreaterEqual(item["confidence"], 0.2)
            self.assertLessEqual(item["confidence"], 0.95)

    def test_UT_AGT_08_flaky_timeouts_are_timing_not_locator_faults(self):
        from app.qa_agents import flaky

        runs = []
        for i in range(6):
            failed = i % 2 == 1
            runs.append({"id": f"r{i}", "status": "failed" if failed else "passed",
                         "started_at": f"2026-10-0{i + 1}T10:00:00+00:00",
                         "duration_ms": 9900 if failed else 4100,
                         "error": "TimeoutError: waiting for locator('#btn') exceeded 5000ms" if failed else ""})
        out = flaky.analyse(runs, "Login")
        causes = [h["cause"] for h in out["artifacts"]["hypotheses"]]
        self.assertIn("Timing / race condition", causes)
        self.assertNotIn("Unstable locator or DOM churn", causes)
        short = flaky.analyse(runs[:2], "Login")
        self.assertEqual(short["verdict"], "insufficient history")

    def test_UT_AGT_09_defect_draft_scores_severity_scrubs_and_titles_cleanly(self):
        from app.qa_agents import defects

        out = defects.draft({"actual": "The page shows 'Something went wrong' (HTTP 500)",
                             "steps": ["Open basket", "Click PLACE ORDER"], "expected": "Order placed",
                             "logs": "POST /api/orders token=abc123secret user jane.doe@corp.com"})
        draft = out["artifacts"]["draft"]
        self.assertEqual(draft["severity"], "critical")
        self.assertNotIn("when", draft["summary"].split("]")[-1])
        self.assertNotIn("abc123secret", draft["markdown"])
        self.assertNotIn("jane.doe", draft["markdown"])
        self.assertIsNone(out["score"])
        # A reporter's explicit choice is kept; the hub's "low" maps to Jira's "minor".
        given = defects.draft({"actual": "Label is misaligned", "severity": "low"})
        self.assertEqual(given["artifacts"]["draft"]["severity"], "minor")
        self.assertEqual(defects.draft({"actual": "x", "severity": "critical"})["artifacts"]["draft"]["severity"],
                         "critical")

    def test_UT_AGT_10_regression_selects_the_changed_area_and_smoke_tests(self):
        from app.qa_agents import regression

        tests = [{"id": "t1", "name": "Checkout pays with card", "tags": ["smoke"], "start_url": "https://s.test/checkout"},
                 {"id": "t2", "name": "Cart keeps quantity", "tags": [], "start_url": "https://s.test/cart"},
                 {"id": "t3", "name": "Search finds products", "tags": [], "start_url": "https://s.test/search"}]
        out = regression.select(tests, changed_files=["app/static/checkout-components.js"], max_tests=10)
        selected = [t["id"] for t in out["artifacts"]["selected"]]
        self.assertIn("t1", selected)
        self.assertNotIn("t3", selected)
        self.assertEqual(out["artifacts"]["batch_payload"],
                         {"recording_ids": selected, "name": "Regression selection", "screenshots": "failure"})

    def test_UT_AGT_11_release_decision_follows_the_documented_policy(self):
        from app.qa_agents import release

        def run(rid, status, critical=False, error=""):
            return {"id": rid, "recording_id": rid, "recording_name": rid, "status": status,
                    "created_at": "2026-10-05T10:00:00+00:00", "tags": ["smoke"] if critical else [],
                    "critical": critical, "error": error}

        self.assertEqual(release.report([])["artifacts"]["decision"], "INSUFFICIENT EVIDENCE")
        self.assertEqual(release.report([run("a", "passed", True), run("b", "failed", True,
                                                                       "AssertionError: HTTP 500")])
                         ["artifacts"]["decision"], "NO-GO")
        all_green = [run(f"r{i}", "passed") for i in range(20)]
        self.assertEqual(release.report(all_green)["artifacts"]["decision"], "GO")
        ninety = [run(f"s{i}", "passed") for i in range(9)] + [run("s9", "failed", False, "Timeout")]
        self.assertEqual(release.report(ninety)["artifacts"]["decision"], "CONDITIONAL GO")

    def test_UT_AGT_12_ai_layer_never_changes_findings_and_never_sends_secrets(self):
        import json
        from unittest import mock
        from app.qa_agents import ai, registry

        text = "Login with password=Hunter2 and mail jane.doe@corp.com. The app shows a total."
        saved = _env_set(ANTHROPIC_API_KEY="sk-test-0123456789abcdefABCD", TF_AGENT_AI=None)
        try:
            rules = registry.run("requirements", {"text": text}, "rules")
            with mock.patch.object(ai, "_post", side_effect=AssertionError("network used")):
                self.assertEqual(registry.run("requirements", {"text": text}, "rules")["engine"], "rules")
            reply = {"content": [{"type": "text", "text": json.dumps({"insights": ["Check refunds."]})}]}
            sent = {}

            def capture(payload):
                sent["body"] = json.dumps(payload)
                return reply

            with mock.patch.object(ai, "_post", side_effect=capture):
                added = registry.run("requirements", {"text": text}, "auto")
            self.assertEqual(added["engine"], "rules+ai")
            self.assertEqual(added["ai_insights"], ["Check refunds."])
            self.assertEqual(added["findings"], rules["findings"])
            self.assertNotIn("Hunter2", sent["body"])
            self.assertNotIn("jane.doe", sent["body"])
            with mock.patch.object(ai, "_post", return_value={"content": [{"type": "text", "text": "no json"}]}):
                bad = registry.run("requirements", {"text": text}, "auto")
            self.assertEqual(bad["engine"], "rules")
            self.assertIn("JSON", bad["ai"]["error"])
            with mock.patch.object(ai, "_post", side_effect=RuntimeError("boom")):
                failed = registry.run("requirements", {"text": text}, "auto")
            self.assertEqual(failed["engine"], "rules")
            self.assertEqual(failed["findings"], rules["findings"])
            self.assertIn("RuntimeError", failed["ai"]["error"])
        finally:
            _env_restore(saved)

    def test_UT_AGT_13_routes_return_400_404_and_413_by_error_type(self):
        big = {"input": {"text": "x" * 200_001}}
        self.assertEqual(self.client.post("/api/agents/requirements/run", json=big).status_code, 413)
        self.assertEqual(self.client.post("/api/agents/requirements/run",
                                          json={"input": {"text": ""}}).status_code, 400)
        self.assertEqual(self.client.post("/api/agents/unknown/run", json={"input": {}}).status_code, 404)
        self.assertEqual(self.client.post("/api/agents/requirements/run",
                                          json={"input": {"text": "x"}, "mode": "magic"}).status_code, 400)
        self.assertEqual(self.client.post("/api/agents/requirements/run", data="[1,2]",
                                          headers={"content-type": "application/json"}).status_code, 400)
        self.assertEqual(self.client.post("/api/agents/requirements/from-source",
                                          json={"source": {"type": "run", "run_id": "x"}}).status_code, 400)
        self.assertEqual(self.client.get("/api/agents/release-summary/sample").status_code, 200)

    def test_UT_AGT_14_from_source_reads_stored_runs_recordings_and_projects(self):
        from datetime import datetime, timedelta
        from app import library_store
        from app.db import Recording, Run

        project = library_store.create_project("QA hub unit project", "https://shop.example.test",
                                               entity_name="QA hub unit company")
        recording = library_store.create_recording(project["id"], "Checkout pays with card",
                                                   "https://shop.example.test/checkout", tags="smoke",
                                                   publish=False)
        now = datetime.utcnow()
        with SessionLocal() as db:
            rows = []
            for i, status in enumerate(["passed", "failed", "passed", "failed"]):
                run = Run(recording_id=recording["id"], status=status,
                          created_at=now - timedelta(minutes=10 - i),
                          execution_log=[{"action": "click", "label": "PLACE ORDER", "status": status,
                                          "error": "AssertionError: expected 'Order placed' (HTTP 500)"
                                          if status == "failed" else ""}])
                db.add(run)
                rows.append(run)
            db.commit()
            latest_failed = rows[-1].id
            run_ids = [r.id for r in rows]
        triage = self.client.post("/api/agents/failure-triage/from-source",
                                  json={"source": {"type": "run", "run_id": latest_failed}})
        self.assertEqual(triage.status_code, 200, triage.text)
        self.assertEqual(triage.json()["artifacts"]["classifications"][0]["category"], "product")
        flaky = self.client.post("/api/agents/flaky/from-source",
                                 json={"source": {"type": "recording", "recording_id": recording["id"]}})
        self.assertEqual(flaky.status_code, 200, flaky.text)
        self.assertEqual(flaky.json()["artifacts"]["stats"]["runs"], 4)
        release = self.client.post("/api/agents/release-summary/from-source",
                                   json={"source": {"type": "project", "project_id": project["id"]}})
        self.assertEqual(release.status_code, 200, release.text)
        self.assertEqual(release.json()["artifacts"]["decision"], "NO-GO")
        regression = self.client.post("/api/agents/regression/from-source",
                                      json={"source": {"type": "project", "project_id": project["id"]},
                                            "input": {"changed_files": ["app/routes/checkout.py"]}})
        self.assertEqual(regression.status_code, 200, regression.text)
        self.assertEqual(self.client.post("/api/agents/failure-triage/from-source",
                                          json={"source": {"type": "project", "project_id": project["id"]}}).status_code,
                         400)
        self.assertEqual(self.client.post("/api/agents/release-summary/from-source",
                                          json={"source": {"type": "project", "project_id": "no-such"}}).status_code,
                         404)
        self.assertEqual(len(run_ids), 4)

    def test_UT_AGT_15_scrubbing_long_input_stays_linear(self):
        import time
        from app.qa_agents.common import scrub

        started = time.monotonic()
        scrub("x" * 200_000)
        self.assertLess(time.monotonic() - started, 2.0, "scrub() became super-linear on long words")

    def test_UT_AGT_16_dashboard_adds_the_tab_without_removing_any(self):
        index = (ROOT / "app" / "static" / "index.html").read_text()
        self.assertIn('<script src="/qa-agents-components.js"></script>', index)
        self.assertIn('[\"qa-agents\", \"🧰\", \"QA Agents\"]', index)
        self.assertIn('tab === \"qa-agents\" && e(QAAgentsTab', index)
        for key in ("demo", "projects", "variables", "record", "desktop-record", "plan", "library", "runs",
                    "comparison", "artifacts", "logs", "github", "azure", "devdeploy", "settings", "future"):
            self.assertIn('["%s", ' % key, index, "existing tab %r must remain" % key)

    def test_UT_AGT_17_component_classes_are_in_the_committed_stylesheet(self):
        """A class missing from vendor/tailwind.css renders as nothing: check every literal one.

        Classes reach the DOM through helper functions as well as className props, so every
        string literal in the component is scanned for tokens shaped like utility classes.
        """
        css = (ROOT / "app" / "static" / "vendor" / "tailwind.css").read_text()
        html = (ROOT / "app" / "static" / "index.html").read_text()
        source = (ROOT / "app" / "static" / "qa-agents-components.js").read_text()
        utility = re.compile(r"^(?:md:|hover:|disabled:)?(?:bg|border|text|p[xytblr]?|m[xytblr]?|gap|space-[xy]|"
                             r"flex|grid|rounded|font|items|justify|w|h|min-[wh]|max-[wh]|shadow|opacity|leading|"
                             r"tracking|overflow|z|top|left|right|bottom|inset|col-span|self|shrink|cursor|"
                             r"transition|whitespace|break|list|columns|ring|scale|sticky|fixed|absolute|relative)(?:-|$)")
        class_token = re.compile(r"[a-z][a-z0-9:/\[\].%-]*")
        literal = set()
        for string in re.findall(r"\"([^\"\n]*)\"|`([^`\n]*)`", source):
            for text in string:
                tokens = re.sub(r"\$\{[^}]*\}", " ", text).split()
                # Only class lists: every token class-shaped and at least one a utility. Filenames,
                # MIME types, element ids and copy are skipped.
                if tokens and all(class_token.fullmatch(t) for t in tokens) and any(utility.match(t) for t in tokens):
                    literal.update(tokens)
        missing = []
        for token in sorted(literal):
            escaped = re.sub(r"([\\/.:\[\]%])", r"\\\1", token)
            if not re.search(r"\." + re.escape(escaped) + r"(?![A-Za-z0-9_\-\\])", css) and token not in html:
                missing.append(token)
        self.assertFalse(missing, "classes with no rule in tailwind.css: " + ", ".join(missing))


    def test_UT_AGT_18_test_cases_emit_valid_gherkin_without_outlines(self):
        from app.qa_agents import case_design
        from app.qa_agents.registry import SAMPLES

        result = case_design.generate(SAMPLES["test-cases"]["text"] + "\nPassword must not be shown\nin logs.",
                                      "Registration\nwith breaks")
        gherkin = result["artifacts"]["gherkin"]
        lines = gherkin.splitlines()
        self.assertTrue(lines[0].startswith("Feature: Registration with breaks"), lines[0])
        self.assertNotIn("Scenario Outline", gherkin)
        self.assertNotIn("Examples:", gherkin)
        self.assertNotIn("|", gherkin, "no data tables: each scenario reads on its own")
        scenarios = [line for line in lines if line.lstrip().startswith("Scenario:")]
        self.assertEqual(len(scenarios), len(result["artifacts"]["cases"]))
        for case in result["artifacts"]["cases"]:
            self.assertTrue(any(line.strip().startswith("Scenario: " + case["id"] + " ") for line in lines))
        keywords = [line.strip().split(" ", 1)[0] for line in lines if line.strip()]
        for index, word in enumerate(keywords):
            if word == "Scenario:":
                self.assertEqual(keywords[index + 1], "Given", "every scenario starts with Given")
        self.assertEqual(gherkin.count("Then "), len(scenarios))
        self.assertTrue(all(line.strip() for line in gherkin.split("\n") if line.startswith("    ")),
                        "step lines are single-line and non-empty")
        self.assertEqual(case_design.to_gherkin([], "Empty"), "")
        self.assertIn("gherkin", result["artifacts"])

    def _fake_ollama(self, reply):
        """A local HTTP server that records the request and answers with `reply` (JSON)."""
        import http.server
        import json
        import threading

        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
                data = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1], seen

    def test_UT_AGT_19_ollama_provider_runs_locally_and_never_sends_secrets(self):
        import json
        from unittest import mock
        from app.qa_agents import ai, registry

        text = "Login with password=Hunter2 and mail jane.doe@corp.com. The app shows a total."
        port, seen = self._fake_ollama({"message": {"role": "assistant", "content": json.dumps(
            {"insights": ["Check refunds.", "   "]})}})
        local = "127.0.0.1,localhost"
        saved = _env_set(TF_AGENT_AI="1", TF_AGENT_AI_PROVIDER="ollama", ANTHROPIC_API_KEY="sk-test-0123456789abcdefABCD",
                         NO_PROXY=local, no_proxy=local)
        try:
            with mock.patch.object(ai, "OLLAMA_URL", f"http://127.0.0.1:{port}"), \
                    mock.patch.object(ai, "OLLAMA_MODEL", "test-model"):
                self.assertEqual(ai.provider(), "ollama", "an explicit provider wins over the cloud key")
                status = ai.status()
                self.assertEqual((status["available"], status["provider"], status["model"]),
                                 (True, "ollama", "test-model"))
                with mock.patch.object(ai, "_post", side_effect=AssertionError("cloud used")):
                    result = registry.run("requirements", {"text": text}, "auto")
                rules = registry.run("requirements", {"text": text}, "rules")
        finally:
            _env_restore(saved)
        self.assertEqual(result["engine"], "rules+ai")
        self.assertEqual(result["ai_insights"], ["Check refunds."])
        self.assertEqual(result["findings"], rules["findings"])
        self.assertEqual((result["ai"]["provider"], result["ai"]["model"], result["ai"]["used"]),
                         ("ollama", "test-model", True))
        self.assertEqual(seen["path"], "/api/chat")
        self.assertEqual(seen["body"]["model"], "test-model")
        self.assertIs(seen["body"]["stream"], False)
        self.assertEqual(seen["body"]["format"], "json")
        sent = json.dumps(seen["body"])
        for secret in ("Hunter2", "jane.doe", "sk-test-0123456789"):
            self.assertNotIn(secret, sent)

    def test_UT_AGT_20_local_model_failures_keep_rules_and_provider_switches_work(self):
        import socket
        from unittest import mock
        from app.qa_agents import ai, registry

        text = "The checkout must be fast. TBD: declined cards?"
        local = "127.0.0.1,localhost"
        with socket.socket() as probe:  # a port nothing listens on
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        rules = registry.run("requirements", {"text": text}, "rules")
        saved = _env_set(TF_AGENT_AI="1", TF_AGENT_AI_PROVIDER="ollama", ANTHROPIC_API_KEY=None,
                         NO_PROXY=local, no_proxy=local)
        try:
            with mock.patch.object(ai, "OLLAMA_URL", f"http://127.0.0.1:{closed}"):
                unreachable = registry.run("requirements", {"text": text}, "auto")
            port, _ = self._fake_ollama({"message": {"content": "no json here"}})
            with mock.patch.object(ai, "OLLAMA_URL", f"http://127.0.0.1:{port}"):
                malformed = registry.run("requirements", {"text": text}, "auto")
            for failed in (unreachable, malformed):
                self.assertEqual(failed["engine"], "rules")
                self.assertEqual(failed["ai_insights"], [])
                self.assertEqual(failed["findings"], rules["findings"])
            self.assertIn("unreachable", unreachable["ai"]["error"])
            self.assertIn("JSON", malformed["ai"]["error"])
            os.environ["TF_AGENT_AI"] = "0"
            self.assertIsNone(ai.provider())
            self.assertFalse(ai.status()["available"])
            os.environ["TF_AGENT_AI"] = "1"
            os.environ["TF_AGENT_AI_PROVIDER"] = "bogus"
            self.assertIsNone(ai.provider())
            self.assertIn("must be", ai.status()["reason"])
            os.environ.pop("TF_AGENT_AI_PROVIDER")
            os.environ["ANTHROPIC_API_KEY"] = "sk-test-0123456789abcdefABCD"
            self.assertEqual(ai.provider(), "anthropic")
            self.assertEqual(ai.status()["provider"], "anthropic")
        finally:
            _env_restore(saved)

    def test_UT_AGT_21_mcp_server_speaks_json_rpc_and_reports_tool_errors(self):
        import json
        from app.qa_agents import mcp_server

        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = [t["name"] for t in tools]
        # The four agent tools stay exactly where they were; the market and
        # library-intelligence tools are additive and read-only.
        self.assertEqual(names[:4], ["qa_list_agents", "qa_get_sample", "qa_run_agent", "qa_run_from_source"])
        self.assertEqual(names[4:], ["market_position", "market_gaps", "resolve_step", "step_search",
                                     "impact_analysis"])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools), "every tool is read-only")
        self.assertEqual(tuple(tools[2]["inputSchema"]["properties"]["agent"]["enum"]), self.AGENT_IDS)

        def call(name, arguments):
            reply = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                       "params": {"name": name, "arguments": arguments}})
            return reply["result"]["isError"], reply["result"]["content"][0]["text"]

        error, text = call("qa_list_agents", {})
        self.assertFalse(error)
        self.assertEqual(len(json.loads(text)["agents"]), 9)
        error, text = call("qa_run_agent", {"agent": "requirements", "input": {"text": "The checkout must be fast."},
                                            "mode": "rules"})
        self.assertFalse(error)
        self.assertEqual(json.loads(text)["engine"], "rules")
        self.assertTrue(call("qa_run_agent", {"agent": "nope", "input": {}})[0])
        self.assertTrue(call("qa_run_agent", {"agent": "requirements", "input": {"text": "x"}, "mode": "magic"})[0])
        self.assertTrue(call("qa_run_from_source", {"agent": "release-summary",
                                                    "source": {"type": "run", "run_id": "x"}})[0])
        self.assertTrue(call("qa_run_from_source", {"agent": "requirements", "source": {"type": "run"}})[0])
        self.assertTrue(call("qa_run_agent", {"agent": "requirements", "input": "not an object"})[0])
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "bogus"})["error"]["code"], -32601)
        self.assertEqual(mcp_server.handle(5)["error"]["code"], -32600)
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                            "params": {}})["error"]["code"], -32602)
        batch = mcp_server.handle([{"jsonrpc": "2.0", "id": 5, "method": "ping"},
                                   {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual([r["id"] for r in batch], [5])

    def test_UT_AGT_22_stdio_server_writes_only_protocol_lines_to_stdout(self):
        import json
        import subprocess
        import sys
        import tempfile

        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "qa_run_agent", "arguments": {"agent": "requirements", "mode": "rules",
                                                      "input": {"text": "The checkout must be fast."}}}},
            "not json",
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DATABASE_URL="sqlite:///" + os.path.join(tmp, "mcp.db"),
                       TF_ARTIFACTS=os.path.join(tmp, "artifacts"), TF_LIBRARY_DIR=os.path.join(tmp, "library"))
            os.makedirs(env["TF_ARTIFACTS"], exist_ok=True)
            os.makedirs(env["TF_LIBRARY_DIR"], exist_ok=True)
            payload = "\n".join(m if isinstance(m, str) else json.dumps(m) for m in messages) + "\n"
            proc = subprocess.run([sys.executable, "-m", "app.qa_agents.mcp_server"], input=payload,
                                  capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            self.assertEqual(len(replies), 5, "one reply per request; the notification gets none")
            self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "testforge-qa-agents")
            self.assertEqual(replies[2]["result"]["content"][0]["type"], "text")
            self.assertEqual(replies[3]["error"]["code"], -32700)
            self.assertEqual(replies[4]["id"], 4)
            probe = subprocess.run([sys.executable, "-c",
                                    "import sys, app.qa_agents; print('app.config' in sys.modules)"],
                                   capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)
            self.assertEqual(probe.stdout.strip(), "False", "importing the package must not import the app")

class DockerfileTests(unittest.TestCase):
    """The build context must contain every path the Dockerfile copies."""

    def test_UT_IMG_01_copy_sources_survive_dockerignore(self):
        """No .dockerignore entry strips a local COPY source.

        BuildKit refuses the build with "failed to compute cache key ... not
        found" when a COPY source is excluded from the context. That is what a
        deploy does today if `docs` returns to .dockerignore while the Dockerfile
        still ships docs/ for the Azure tab (GET /api/docs/azure-guide).
        """
        dockerfile = (ROOT / "Dockerfile").read_text()
        patterns = [
            line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        def excluded(path):
            # Last match wins, as in dockerignore: `docs` then `!docs/keep` keeps
            # docs/keep. Entries are literal names here (globs like `*.py[cod]`
            # never name a COPY root); an entry that matches the path or a parent
            # directory of it removes that whole tree from the context.
            verdict = False
            for pattern in patterns:
                negated = pattern.startswith("!")
                name = pattern.lstrip("!").strip().strip("/").rstrip("/")
                if not name:
                    continue
                hit = path == name or path.startswith(name + "/")
                if hit:
                    verdict = not negated
            return verdict

        ignored = []
        for raw in dockerfile.splitlines():
            line = raw.strip()
            if line.split()[:1] != ["COPY"]:
                continue
            words = [w for w in line.split()[1:] if not w.startswith("--")]
            for src in words[:-1]:  # every token but the destination
                if src.startswith(("http://", "https://")):
                    continue
                src = src.lstrip("./")
                if excluded(src):
                    ignored.append(src)
        self.assertFalse(
            ignored,
            "Dockerfile COPYs paths that .dockerignore removes from the build "
            "context (the build fails with 'failed to compute cache key ... not "
            "found'): " + ", ".join(ignored),
        )

    def test_UT_IMG_02_image_binds_the_port_used_by_browser_replay(self):
        """The web process and the server-side browser must agree on PORT.

        In a Render-style container PORT=10000 used to be converted to a
        loopback URL while the fixed Docker CMD kept uvicorn on 8000. The sample
        page then failed with net::ERR_CONNECTION_REFUSED. The runtime CMD must
        bind the injected port, and URL normalization must use that same value.
        """
        import json
        dockerfile = (ROOT / "Dockerfile").read_text()
        cmd_line = next(line.strip() for line in dockerfile.splitlines()
                        if line.strip().startswith("CMD "))
        command = json.loads(cmd_line[len("CMD "):])
        self.assertEqual(command[:2], ["sh", "-c"])
        self.assertIn("${PORT:-8000}", command[2])
        self.assertIn("--host 0.0.0.0", command[2])
        self.assertIn("--workers 1", command[2])
        with patch.dict(os.environ, {"PORT": "10000"}):
            self.assertEqual(
                browser_url("/demo.html", Req("testforge.example")),
                "http://127.0.0.1:10000/demo.html",
            )


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
        for name in ("Jenkinsfile", "playwright_test.py", "test_recording.py", "playwright_test.js", "recording.feature", "azure-devops.csv", "azure-devops.xlsx", "jira.csv", "jira.xlsx", "testcomplete.csv", "testcomplete.xlsx"):
            self.assertTrue((folder / name).is_file(), name)
        sheet = load_workbook(folder / "azure-devops.xlsx").active
        self.assertEqual(sheet["D2"].value, "'=1+2")
        import py_compile
        py_compile.compile(str(folder / "playwright_test.py"), doraise=True)
        # pytest entry point: collected by pytest, calls the same replay, pinned in requirements.
        self.assertIn("def test_recording_replays", (folder / "test_recording.py").read_text())
        self.assertIn("from playwright_test import main", (folder / "test_recording.py").read_text())
        self.assertIn("pytest==", (folder / "requirements.txt").read_text())

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

    def test_UT_FLOW_10_running_a_saved_recording_inside_a_live_session(self):
        """An existing recording can be replayed into the session being recorded.

        This is the "execute another recording while recording" path: the steps
        drive the live browser and, with record on, land in the open recording as
        ordinary editable steps.
        """
        from app import library_store
        from app.db import RecordingStep, SessionLocal
        from app.recorder import RecorderSession
        from tests.fakes import FakePage

        project = library_store.create_project("Live replay", "")
        source = library_store.create_recording(project["id"], "Login flow", "https://example.com/", steps=[
            {"order": 1, "action": "navigate", "value": "https://example.com/login", "label": "Open login"},
            {"order": 2, "action": "click", "selector": {"primary": "#user"}, "label": "Click user"},
            {"order": 3, "action": "type", "value": "{{user}}", "selector": {"primary": "#user"}, "label": "Type user"},
            {"order": 4, "action": "press", "value": "Enter", "label": "Press Enter"},
        ])
        target = library_store.create_recording(project["id"], "Rescued session", "https://example.com/")

        class Preview:
            def __init__(self): self.lock = asyncio.Lock(); self.page = None; self.touches = 0
            def touch(self): self.touches += 1
            async def stop(self): pass

        page = FakePage(known={"#user"})
        preview = Preview(); preview.page = page
        session = RecorderSession(target["id"], "", 0)
        session.page = page; session.preview = preview
        session._ready.set(); session.status = "live"

        async def exercise():
            result = await session.replay(source["steps"], {"user": "Ada"},
                                          record=True, source=source)
            await session.stop()
            return result

        result = asyncio.run(exercise())
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual([result["executed"], result["recorded"]], [4, 4])
        self.assertIsNone(result["failed_at"])
        self.assertEqual(page.gotos[0][0], "https://example.com/login")
        self.assertIn(("locator", "#user"), page.clicks)
        self.assertEqual(page.typed, ["Ada"])
        self.assertEqual(page.pressed, ["Enter"])
        self.assertTrue(preview.touches, "the live window must be kept awake while replaying")

        with SessionLocal() as db:
            rows = db.query(RecordingStep).filter_by(recording_id=target["id"]).order_by(RecordingStep.order).all()
            self.assertEqual([row.order for row in rows], [1, 2, 3, 4])
            self.assertEqual(rows[2].value, "{{user}}")
        exported = library_store.get_recording(target["id"])
        self.assertEqual([step["order"] for step in exported["steps"]], [1, 2, 3, 4])

    def test_UT_FLOW_11_a_failed_live_replay_reports_its_step(self):
        """A replay that cannot finish says which step failed and keeps the rest."""
        from app import library_store
        from app.db import RecordingStep, SessionLocal
        from app.executor import replay_step as real_replay_step
        from app.recorder import RecorderSession
        from tests.fakes import FakePage

        project = library_store.create_project("Broken live replay", "")
        source = library_store.create_recording(project["id"], "Half flow", "https://example.com/", steps=[
            {"order": 1, "action": "navigate", "value": "https://example.com/step", "label": "Open step"},
            {"order": 2, "action": "click", "selector": {"primary": "#ghost"}, "label": "Click ghost"},
        ])
        target = library_store.create_recording(project["id"], "Target", "https://example.com/")

        class Preview:
            def __init__(self): self.lock = asyncio.Lock(); self.page = None
            def touch(self): pass
            async def stop(self): pass

        page = FakePage()
        preview = Preview(); preview.page = page
        session = RecorderSession(target["id"], "", 0)
        session.page = page; session.preview = preview
        session._ready.set(); session.status = "live"

        calls = []

        async def flaky_replay(page_, step, variables, **kwargs):
            calls.append(step.get("action"))
            if step.get("action") == "click":
                raise RuntimeError("Timeout 3000ms exceeded waiting for #ghost")
            return await real_replay_step(page_, step, variables, **kwargs)

        async def exercise():
            with patch("app.executor.replay_step", flaky_replay):
                result = await session.replay(source["steps"], {}, record=True, source=source)
            await session.stop()
            return result

        result = asyncio.run(exercise())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_at"], 2)
        self.assertEqual(result["executed"], 1)
        self.assertIn("#ghost", result["error"] or "")
        self.assertEqual(calls, ["navigate", "click"])
        with SessionLocal() as db:
            rows = db.query(RecordingStep).filter_by(recording_id=target["id"]).all()
            self.assertEqual(len(rows), 1, "only the step that ran is recorded")

    def test_UT_FLOW_12_a_closed_session_refuses_to_replay(self):
        from app import library_store
        from app.recorder import RecorderSession
        from tests.fakes import FakePage

        project = library_store.create_project("Closed replay", "")
        source = library_store.create_recording(project["id"], "Anything", "https://example.com/", steps=[
            {"order": 1, "action": "press", "value": "Enter", "label": "Press Enter"},
        ])

        class Preview:
            def __init__(self): self.lock = asyncio.Lock(); self.page = None
            def touch(self): pass
            async def stop(self): pass

        page = FakePage()
        preview = Preview(); preview.page = page
        session = RecorderSession(source["id"], "", 0)
        session.page = page; session.preview = preview
        session._ready.set(); session.status = "live"; session._closed = True

        async def exercise():
            with self.assertRaises(RuntimeError):
                await session.replay(source["steps"], {}, record=False, source=source)

        asyncio.run(exercise())
        self.assertEqual(page.pressed, [], "nothing may run in a closed session")

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

        def blocking_export(recording_id, publish=False, include_exports=True):
            calls.append(recording_id)
            time.sleep(0.8)          # longer than the flush budget below
            return original_export(recording_id, publish=publish, include_exports=include_exports)

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

        def exploding_export(recording_id, publish=False, include_exports=True):
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


class SchemaDriftTests(unittest.TestCase):
    """A column narrower than the model must not cost the user a recorded step.

    The live failure: recording a D365 staging URL
    (`https://harrisfarm-uat.sandbox.operations.dynamics.com/?cmp=hfm&mi=FUS_StoreReplenTable`)
    redirects the browser to a `login.microsoftonline.com/.../oauth2/v2.0/authorize?...`
    URL of several hundred characters. The recorder labelled the navigate step
    `Open <url>` and the insert failed with
    `psycopg2.errors.StringDataRightTruncation: value too long for type character
    varying(300)`, which the dashboard reported as `Failed to save step`. The
    deployed database still carried the `varchar(300)` width from an older
    revision, and `create_all()` never changes a column that already exists.
    """

    def test_UT_DB_01_long_labels_are_clipped_to_the_column_width(self):
        from app.db import LABEL_LIMIT, RecordingStep, fit_text

        self.assertEqual(LABEL_LIMIT, 500)
        self.assertIsNone(fit_text(None), "a nullable column keeps its NULL")
        self.assertEqual(fit_text("Open the page"), "Open the page")
        clipped = fit_text("u" * 900)
        self.assertEqual(len(clipped), LABEL_LIMIT)
        self.assertTrue(clipped.endswith("…"), "the clip stays visible, not silent")
        # The guard sits on the model, so the API editor, the library importer and
        # the compressor are covered as well -- not only the recorder.
        row = RecordingStep(recording_id="r", order=1, action="navigate",
                            label="Open " + "u" * 900)
        self.assertEqual(len(row.label), LABEL_LIMIT)

    def test_UT_DB_02_a_long_navigate_label_keeps_the_whole_url_in_value(self):
        from app import library_store
        from app.db import LABEL_LIMIT
        from app.recorder import RecorderSession

        url = ("https://login.microsoftonline.com/9f/oauth2/v2.0/authorize?client_id=00000007"
               "&redirect_uri=" + "https%3A%2F%2Fharrisfarm-uat.sandbox.operations.dynamics.com%2F" * 9
               + "&response_mode=form_post&state=abc")
        self.assertGreater(len(url), LABEL_LIMIT, "the scenario needs an over-long URL")
        project = library_store.create_project("Long labels", "")
        rec = library_store.create_recording(
            project["id"], "D365_UAT_Login",
            "https://harrisfarm-uat.sandbox.operations.dynamics.com/?cmp=hfm&mi=FUS_StoreReplenTable",
        )
        session = RecorderSession(rec["id"], "", 0)
        saved = {}

        async def exercise():
            saved.update(await session._record("navigate", value=url, label=f"Open {url}",
                                               force_new=True))
            await session.stop()

        asyncio.run(exercise())
        self.assertEqual(saved["value"], url, "the URL the replay needs is stored whole")
        self.assertEqual(len(saved["label"]), LABEL_LIMIT)
        self.assertTrue(saved["label"].startswith("Open https://login.microsoftonline.com/"))
        self.assertTrue(saved["label"].endswith("…"))
        exported = library_store.export_recording(rec["id"])
        self.assertEqual(exported["steps"][0]["value"], url)

        # Compressing such a step must keep the repeat marker inside the column.
        clicks = [{"order": order, "action": "click", "label": "c" * 700,
                   "selector": {"primary": "#next"}} for order in (1, 2)]
        compressed = library_store.compress_steps(clicks)
        self.assertEqual(len(compressed), 1)
        self.assertEqual(compressed[0]["repeat"], 2)
        self.assertTrue(compressed[0]["label"].endswith(" ×2"))
        self.assertLessEqual(len(compressed[0]["label"]), LABEL_LIMIT)

    def test_UT_DB_03_a_narrow_legacy_column_is_widened(self):
        """The repair widens only what is narrower than the model declares."""
        from sqlalchemy.dialects.postgresql import TEXT, VARCHAR, dialect
        from app.db import RecordingStep, _widen_narrow_columns

        class Inspector:
            def __init__(self, columns):
                self.columns = columns

            def get_columns(self, table):
                return self.columns

        class Connection:
            """Records the DDL a real Postgres connection would run."""

            def __init__(self):
                self.dialect = dialect()
                self.sql = []

            def execute(self, statement):
                self.sql.append(str(statement))

        table = RecordingStep.__table__
        self.assertEqual(table.c.label.type.length, 500)
        connection = Connection()
        repairs = []
        _widen_narrow_columns(
            Inspector([
                {"name": "label", "type": VARCHAR(300), "nullable": True},     # legacy width
                {"name": "action", "type": VARCHAR(100), "nullable": True},    # matches the model
                {"name": "value", "type": TEXT(), "nullable": True},           # already unbounded
                {"name": "selector", "type": VARCHAR(500), "nullable": True},  # matches the model
            ]),
            connection, table, repairs,
        )
        self.assertEqual(connection.sql,
                         ["ALTER TABLE recording_steps ALTER COLUMN label TYPE VARCHAR(500)"])
        self.assertEqual(repairs, ["recording_steps.label: widened VARCHAR(300) -> VARCHAR(500)"])

        # SQLite ignores a VARCHAR length, so nothing may be altered there.
        class SqliteConnection:
            dialect = type("Dialect", (), {"name": "sqlite"})()

            def execute(self, statement):
                raise AssertionError("SQLite stores any length; no ALTER is needed")

        _widen_narrow_columns(Inspector([{"name": "label", "type": VARCHAR(300), "nullable": True}]),
                              SqliteConnection(), table, [])

    def test_UT_DB_04_the_recorder_repairs_the_schema_and_saves_the_step(self):
        """A rejected insert is retried once after the repair, never dropped."""
        from sqlalchemy.exc import ProgrammingError
        from app import library_store, recorder
        from app.db import RecordingStep, SessionLocal

        project = library_store.create_project("Schema repair", "")
        rec = library_store.create_recording(project["id"], "retry", "/demo.html")
        session = recorder.RecorderSession(rec["id"], "", 0)
        real_session = recorder.SessionLocal
        original_repair = recorder.repair_schema
        attempts, repairs = [], []

        class RefusingSession:
            """A session whose commit is rejected the way the narrow column rejects it."""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def add(self, row):
                self.row = row

            def commit(self):
                raise ProgrammingError(
                    "INSERT INTO recording_steps (recording_id, \"order\", action, label) VALUES (...)",
                    {},
                    Exception("value too long for type character varying(300)"),
                )

        def flaky_session():
            attempts.append(1)
            return RefusingSession() if len(attempts) == 1 else real_session()

        async def exercise():
            step = await session._record("navigate", value="https://example.com/login",
                                         label="Open https://example.com/login", force_new=True)
            await session.stop()
            return step

        try:
            recorder.SessionLocal = flaky_session
            recorder.repair_schema = lambda: repairs.append(True) or True
            step = asyncio.run(exercise())
        finally:
            recorder.SessionLocal = real_session
            recorder.repair_schema = original_repair

        self.assertEqual(len(attempts), 2, "the rejected insert must be attempted again")
        self.assertTrue(repairs, "the schema repair must run before the retry")
        self.assertEqual(step["label"], "Open https://example.com/login")
        with SessionLocal() as db:
            rows = db.query(RecordingStep).filter_by(recording_id=rec["id"]).all()
        self.assertEqual(len(rows), 1, "exactly one row, the retry does not duplicate")

    def test_UT_DB_05_a_saved_step_is_not_read_back(self):
        """`Could not refresh instance` must not fail a save that committed.

        The recorder used to `refresh()` the row after the commit -- a second
        SELECT by primary key. A row that a concurrent library write had replaced
        made that SELECT return nothing, and the dashboard showed
        `Failed to save step: Could not refresh instance '<RecordingStep at 0x...>'`
        even though the step was already stored.
        """
        from sqlalchemy.orm import Session
        from app import library_store
        from app.db import RecordingStep, SessionLocal
        from app.recorder import RecorderSession

        project = library_store.create_project("No read back", "")
        rec = library_store.create_recording(project["id"], "no-read-back", "/demo.html")
        session = RecorderSession(rec["id"], "", 0)
        original = Session.refresh

        def refuse(*_args, **_kwargs):
            raise AssertionError("a written step must not be read back to save it")

        async def exercise():
            step = await session._record("click", label="Click name", selector={"primary": "#name"})
            await session.stop()
            return step

        Session.refresh = refuse
        try:
            step = asyncio.run(exercise())
        finally:
            Session.refresh = original
        self.assertEqual(step["label"], "Click name")
        self.assertTrue(step["id"])
        with SessionLocal() as db:
            rows = db.query(RecordingStep).filter_by(recording_id=rec["id"]).all()
        self.assertEqual([row.id for row in rows], [step["id"]], "the id returned is the stored row")

    def test_UT_DB_06_an_over_long_recording_url_is_rejected_before_the_insert(self):
        """A value the column cannot hold is a 422, not a `value too long` error."""
        from fastapi.testclient import TestClient
        from app import library_store
        from app.main import app

        project = library_store.create_project("Bounded fields", "")
        with TestClient(app) as client:
            response = client.post("/api/recordings", json={
                "project_id": project["id"], "name": "D365",
                "start_url": "https://example.com/?q=" + "x" * 1200,
            })
            self.assertEqual(response.status_code, 422)
            self.assertIn("too long", response.json()["detail"])
            accepted = client.post("/api/recordings", json={
                "project_id": project["id"], "name": "D365",
                "start_url": "https://harrisfarm-uat.sandbox.operations.dynamics.com/?cmp=hfm&mi=FUS_StoreReplenTable",
            })
            self.assertEqual(accepted.status_code, 201)

    def test_UT_DB_07_a_step_is_kept_even_when_the_column_cannot_be_widened(self):
        """A table the service may not ALTER still gets the action, not an error."""
        from sqlalchemy.exc import ProgrammingError
        from app import library_store, recorder
        from app.db import LABEL_LIMIT, RecordingStep, SessionLocal

        url = ("https://login.microsoftonline.com/9f/oauth2/v2.0/authorize?client_id=00000007"
               "&redirect_uri=" + "https%3A%2F%2Fharrisfarm-uat.sandbox.operations.dynamics.com%2F" * 9
               + "&state=abc")
        project = library_store.create_project("Unowned table", "")
        rec = library_store.create_recording(project["id"], "narrow", "/demo.html")
        session = recorder.RecorderSession(rec["id"], "", 0)
        real_session = recorder.SessionLocal
        original_repair = recorder.repair_schema
        attempts, repairs = [], []

        class NarrowSession:
            """The real session, plus the `varchar(300)` column nobody widened."""

            def __init__(self):
                self._db = real_session()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._db.__exit__(*exc)

            def add(self, row):
                self.row = row
                self._db.add(row)

            def commit(self):
                if len(self.row.label or "") > 300:
                    self._db.rollback()
                    raise ProgrammingError(
                        "INSERT INTO recording_steps (recording_id, \"order\", action, label) VALUES (...)",
                        {},
                        Exception("value too long for type character varying(300)"),
                    )
                self._db.commit()

        def narrow_session():
            attempts.append(1)
            return NarrowSession()

        async def exercise():
            step = await session._record("navigate", value=url, label=f"Open {url}", force_new=True)
            await session.stop()
            return step

        try:
            recorder.SessionLocal = narrow_session
            recorder.repair_schema = lambda: repairs.append(True) or True
            step = asyncio.run(exercise())
        finally:
            recorder.SessionLocal = real_session
            recorder.repair_schema = original_repair

        self.assertEqual(len(attempts), 3, "insert, repaired insert, shortened label")
        self.assertTrue(repairs, "the repair is still attempted first")
        self.assertEqual(step["value"], url, "the URL the replay needs survives")
        self.assertLessEqual(len(step["label"]), recorder.FALLBACK_LABEL_LIMIT)
        self.assertLess(recorder.FALLBACK_LABEL_LIMIT, LABEL_LIMIT)
        with SessionLocal() as db:
            rows = db.query(RecordingStep).filter_by(recording_id=rec["id"]).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].value, url)


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
            self.assertTrue(response.json()["display_window"], "the API defaults to on")

    def test_UT_RUN_10_active_single_run_window_can_be_toggled(self):
        """The top-level Runs switch updates a live single run without requeueing."""
        from fastapi.testclient import TestClient
        from app import library_store
        from app.db import Run, SessionLocal
        import app.main as main

        project = library_store.create_project("Mid-run window", "")
        rec = library_store.create_recording(project["id"], "mid-run", "/demo.html")
        with SessionLocal() as db:
            run = Run(recording_id=rec["id"], status="queued")
            db.add(run); db.commit(); db.refresh(run)
            run_id = run.id
        main._remember_run_window(run_id, False)
        try:
            with TestClient(main.app) as client:
                response = client.post(f"/api/runs/{run_id}/window", json={"display_window": True})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["display_window"])
                self.assertTrue(main.RUN_WINDOWS[run_id])
                self.assertEqual(main.guardrails.run_buffers.get(run_id)[-1], {
                    "type": "window", "display_window": True,
                })
                with SessionLocal() as db:
                    db.get(Run, run_id).status = "running"
                    db.commit()
                response = client.post(f"/api/runs/{run_id}/window", json={"display_window": False})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(main.RUN_WINDOWS[run_id])
                response = client.post(f"/api/runs/{run_id}/window", json={"display_window": "yes"})
                self.assertEqual(response.status_code, 422)
                with SessionLocal() as db:
                    db.get(Run, run_id).status = "passed"
                    db.commit()
                response = client.post(f"/api/runs/{run_id}/window", json={"display_window": False})
                self.assertEqual(response.status_code, 409)
        finally:
            main.RUN_WINDOWS.pop(run_id, None)

    def test_UT_RUN_11_runs_preview_controls_are_at_top_and_step_tagged(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        runs = html[html.index("function Runs("):html.index("function QueueHygiene(")]
        self.assertLess(runs.index('id: "runs-live"'), runs.index('id: "runs-queue"'))
        self.assertIn("/window`, {display_window: next}", runs)
        self.assertIn("step_index: message.step_index", runs)
        self.assertIn("e(ScreenViewer", runs)


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
        self.assertEqual(page.shots, ["jpeg", "jpeg"], "one evidence JPEG per step, no extra screencast")
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
        self.assertEqual(page.shots, ["jpeg", "jpeg"], "one JPEG per step only")
        self.assertEqual(frames, [], "unwatched evidence is not broadcast")

    def test_UT_RUN_09_live_frames_stream_during_actions_and_follow_steps(self):
        import time
        import app.executor as executor
        from tests.fakes import FakePage, FakePlaywright

        page = FakePage(known=["#go"])
        fake = FakePlaywright(page)
        frames, events = [], []
        async def on_event(event):
            event = dict(event, at=time.monotonic())
            events.append(event)
        async def on_frame(payload, **metadata):
            frames.append({**metadata, "at": time.monotonic()})

        run_id = self._recording_with_steps("live-step-sync")
        original_playwright = executor.async_playwright
        original_replay = executor.replay_step
        async def slow_replay(*args, **kwargs):
            await asyncio.sleep(0.35)
            return await original_replay(*args, **kwargs)
        executor.async_playwright = lambda: fake
        executor.replay_step = slow_replay
        enabled = {"value": False}
        try:
            async def exercise():
                task = asyncio.create_task(execute_run(
                    run_id, on_event, on_frame=on_frame, display_window=False,
                    window_enabled=lambda: enabled["value"], viewer_count=lambda: 1,
                ))
                await asyncio.sleep(0.05)
                enabled["value"] = True
                await task
            asyncio.run(exercise())
        finally:
            executor.async_playwright = original_playwright
            executor.replay_step = original_replay

        first_done = next(event["at"] for event in events
                          if event.get("step_index") == 1 and event.get("status") == "passed")
        live_during_first = [frame for frame in frames
                             if frame.get("step_index") == 1 and frame["at"] < first_done]
        self.assertTrue(live_during_first, "the preview loop should stream before step 1 completes")
        self.assertEqual({frame.get("step_index") for frame in frames}, {1, 2})
        self.assertEqual(next(frame for frame in frames if frame.get("step_index") == 2).get("label"), "Go")

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

    def _refused_write(self, code=403, message="Resource not accessible by personal access token"):
        """A push whose reads work and whose writes GitHub refuses, through the real transport."""
        import io
        import json
        import tempfile
        import urllib.error
        from unittest import mock
        from app import github_api

        root = Path(tempfile.mkdtemp(prefix="tf-refused-"))
        (root / "catalog.json").write_text("{}", encoding="utf-8")

        def transport(request, timeout=None):
            method = (request.get_method() or "GET").upper()
            if method not in ("GET", "HEAD", "OPTIONS"):
                body = json.dumps({"message": message}).encode()
                raise urllib.error.HTTPError(request.full_url, code, "Forbidden", {}, io.BytesIO(body))
            if "/branches/" in request.full_url:
                return _GitHubResponse({"commit": {"sha": "a" * 40}})
            return _GitHubResponse({"tree": []})

        token = "github_pat_" + "S" * 18
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN=token)
        return root, transport, saved, token

    def test_UT_PUB_08_a_refused_write_says_so_and_promises_no_retry(self):
        """The reported bug: a 403 was reported as "will be retried in 60s".

        "Resource not accessible by personal access token" is the token being
        refused, not an outage. Waiting a minute cannot change GitHub's answer,
        so the save must come back refused, with the fix named, and the retry
        loop must never be started for it.
        """
        import urllib.request
        from unittest import mock
        from app import github_api, library_store

        root, transport, saved, token = self._refused_write()
        original = self._no_checkout()
        client = github_api.GitHubClient()
        try:
            with mock.patch.object(urllib.request, "urlopen", transport):
                with self.assertRaises(github_api.GitHubAPIError) as caught:
                    github_api.publish("Save library", root, client=client)
        finally:
            _env_restore(saved)
            self._restore(original)
        exc = caught.exception
        self.assertEqual(exc.status, 403)
        self.assertTrue(exc.permanent, "a permissions 403 must not be treated as transient")
        self.assertIn("Contents", exc.hint)
        self.assertIn("fine-grained", exc.hint)
        self.assertNotIn(token, str(exc) + (exc.hint or ""))

        # ...and the dashboard sentence built from it.
        original_write = library_store._write_catalog
        original_publish = library_store._publish_unlocked
        library_store._write_catalog = lambda: None

        def refuse(_message):
            error = library_store.PublishError(str(exc))
            error.status, error.hint, error.permanent = exc.status, exc.hint, exc.permanent
            raise error

        library_store._publish_unlocked = refuse
        original_error = dict(library_store._LAST_PUBLISH_ERROR)
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN=token)
        try:
            self.assertFalse(library_store._finish("Save library recording demo"))
            outcome = library_store.publish_outcome(False)
            blocked = library_store.publish_blocked()
        finally:
            _env_restore(saved)
            library_store._write_catalog = original_write
            library_store._publish_unlocked = original_publish
            library_store._LAST_PUBLISH_ERROR.update(original_error)
        self.assertTrue(blocked)
        self.assertEqual(outcome["publish_state"], "refused")
        self.assertFalse(outcome["retry_scheduled"])
        self.assertIsNone(outcome["retry_in_seconds"])
        message = outcome["publish_message"]
        self.assertNotIn("will be retried", message)
        self.assertIn("refused", message)
        self.assertIn("Contents", outcome["publish_hint"] or "")

    def test_UT_PUB_09_a_rate_limit_is_still_worth_retrying(self):
        """A 403 that means "slow down" must keep the retry promise."""
        from app import library_store

        original = self._no_checkout()
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN="github_pat_" + "S" * 18)
        error = library_store.PublishError(
            "GitHub 403 for POST /repos/acme/qa-library/git/blobs: "
            "API rate limit exceeded for user ID 1"
        )
        error.status = 403
        error.hint = None
        error.permanent = False
        original_write = library_store._write_catalog
        original_publish = library_store._publish_unlocked
        original_error = dict(library_store._LAST_PUBLISH_ERROR)
        library_store._write_catalog = lambda: None

        def refuse(_message):
            raise error

        library_store._publish_unlocked = refuse
        try:
            self.assertFalse(library_store._finish("Save library recording demo"))
            outcome = library_store.publish_outcome(False)
        finally:
            _env_restore(saved)
            library_store._write_catalog = original_write
            library_store._publish_unlocked = original_publish
            library_store._LAST_PUBLISH_ERROR.update(original_error)
            self._restore(original)
        self.assertEqual(outcome["publish_state"], "retry-pending")
        self.assertTrue(outcome["retry_scheduled"])
        self.assertIn("retried in", outcome["publish_message"])

    def test_UT_PUB_10_access_report_names_the_missing_permission(self):
        """The GitHub tab can ask what the token may do, without pushing anything."""
        import urllib.request
        from unittest import mock
        from app import github_api

        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN="github_pat_" + "S" * 18,
                         TF_GITHUB_API="https://api.github.com")
        github_api._ACCESS_CACHE.update({"key": None, "ts": 0.0, "value": None})

        def read_only(request, timeout=None):
            return _GitHubResponse(
                {"permissions": {"pull": True, "push": False, "admin": False},
                 "private": True, "default_branch": "main"},
                headers={"X-OAuth-Scopes": ""},
            )

        try:
            with mock.patch.object(urllib.request, "urlopen", read_only):
                report = github_api.access_report(force=True)
        finally:
            _env_restore(saved)
        self.assertEqual(report["slug"], "acme/qa-library")
        self.assertEqual(report["state"], "read-only")
        self.assertFalse(report["can_write"])
        self.assertTrue(report["permissions"]["pull"])
        self.assertIn("Contents", report["fix"])
        self.assertIn("fine-grained", report["fix"])

        def writable(request, timeout=None):
            return _GitHubResponse({"permissions": {"pull": True, "push": True}, "private": False},
                                   headers={"X-OAuth-Scopes": "repo, read:org"})

        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN="ghp_" + "S" * 30)
        try:
            with mock.patch.object(urllib.request, "urlopen", writable):
                ok = github_api.access_report(force=True)
        finally:
            _env_restore(saved)
        self.assertEqual(ok["state"], "ok")
        self.assertTrue(ok["can_write"])
        self.assertEqual(ok["scopes"], ["repo", "read:org"])
        self.assertEqual(ok["token_kind"], "classic")
        self.assertIsNone(ok["fix"])

    def test_UT_PUB_11_the_diagnosis_endpoint_never_leaks_the_token(self):
        """`GET /api/github/access` reports the refusal without echoing the secret."""
        import urllib.request
        from unittest import mock
        from fastapi.testclient import TestClient
        from app import github_api
        from app.main import app

        token = "github_pat_" + "S" * 18
        original = self._no_checkout()
        saved = _env_set(TF_LIBRARY_PUBLISH=None, TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN=token)
        github_api._ACCESS_CACHE.update({"key": None, "ts": 0.0, "value": None})

        def hostile(request, timeout=None):
            return _GitHubResponse({"permissions": {"pull": True, "push": False}},
                                   headers={"X-OAuth-Scopes": ""})

        try:
            with mock.patch.object(urllib.request, "urlopen", hostile):
                with TestClient(app) as client:
                    response = client.get("/api/github/access?force=1")
        finally:
            _env_restore(saved)
            self._restore(original)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        self.assertNotIn(token, body)
        payload = response.json()
        self.assertFalse(payload["can_write"])
        self.assertIn("Contents", payload["fix"])

    def test_UT_PUB_12_a_refused_git_push_is_not_retried_either(self):
        """A checkout gets git's prose, not a status code — it must be recognised too."""
        from app import library_store

        original = library_store.git_root
        library_store.git_root = lambda: Path("/nonexistent-checkout")
        saved = _env_set(TF_LIBRARY_PUBLISH="1", TF_GITHUB_REPO="acme/qa-library",
                         TF_GITHUB_BRANCH="main", TF_GITHUB_TOKEN="ghp_" + "S" * 30)
        original_write = library_store._write_catalog
        original_publish = library_store._publish_unlocked
        original_error = dict(library_store._LAST_PUBLISH_ERROR)
        library_store._write_catalog = lambda: None

        def denied(_message):
            # Exactly what git prints for a token that may not write: a 403 is
            # reported as "Repository not found" on a repository it cannot see.
            raise library_store.PublishError(
                "fatal: unable to access 'https://github.com/acme/qa-library/': "
                "The requested URL returned error: 403"
            )

        library_store._publish_unlocked = denied
        try:
            self.assertFalse(library_store._finish("Save library recording demo"))
            outcome = library_store.publish_outcome(False)
            self.assertTrue(library_store.publish_blocked())
        finally:
            _env_restore(saved)
            library_store.git_root = original
            library_store._write_catalog = original_write
            library_store._publish_unlocked = original_publish
            library_store._LAST_PUBLISH_ERROR.update(original_error)
        self.assertEqual(outcome["publish_state"], "refused")
        self.assertFalse(outcome["retry_scheduled"])
        self.assertIn("repo", outcome["publish_hint"] or "",
                      "a classic-token hint must name the scope it is missing")
        self.assertNotIn("will be retried", outcome["publish_message"])


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

    @patch.dict(os.environ, {"TF_BROWSER_SINGLE_PROCESS": "0"})
    def test_UT_BATCH_01_one_browser_replays_the_whole_batch(self):
        """Normal Chromium supports replacing isolated contexts in one launch.

        Single-process mode needs sequential relaunches on context changes;
        real mixed-project coverage lives in test_plan_browser.
        """
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
        """The low-level failure policy uses one JPEG (the UI/API enforce all)."""
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
        shots = [shot for page in fake.pages for shot in page.shots if shot == "jpeg"]
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
        self.assertEqual(payload["options"]["screenshots"], "all", "API enforces one JPEG per step")
        self.assertEqual(started["kwargs"]["screenshots"], "all")
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


class AzureGuideTests(unittest.TestCase):
    """The Azure tab reads the deployment guide that ships in docs/."""

    def test_AZ_01_the_guide_is_served_as_markdown(self):
        from app import main as app_main
        payload = app_main.azure_deployment_guide()
        self.assertEqual(payload["source"], "docs/azure-devops-deployment-guide.md")
        self.assertTrue(payload["markdown"].startswith("# "), "the guide must start with its title heading")
        self.assertIn("## 1. Target state", payload["markdown"])
        self.assertGreater(payload["bytes"], 10000, "a stub or empty guide must fail the test, not ship")
        self.assertFalse(payload["truncated"])
        self.assertLessEqual(len(payload["markdown"].encode("utf-8")), app_main.DOCS_GUIDE_MAX_BYTES)

    def test_AZ_02_repeated_reads_are_cached(self):
        from app import main as app_main
        self.assertIs(app_main.azure_deployment_guide(), app_main.azure_deployment_guide())

    def test_AZ_03_a_deployment_without_the_guide_says_where_to_read_it(self):
        from app import main as app_main
        original = app_main._guide_path
        app_main._guide_path = lambda: "/nonexistent/azure-devops-deployment-guide.md"
        try:
            with self.assertRaises(HTTPException) as caught:
                app_main.azure_deployment_guide()
        finally:
            app_main._guide_path = original
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("github.com", caught.exception.detail, "the error must point at the guide in the repository")

    def test_AZ_04_the_guide_is_azure_only(self):
        """Nothing in the Azure tab may describe a different hosting platform.

        The guide used to run a side-by-side comparison with the free demo, which
        made the tab unreadable on its own and meant any change to that other
        deployment was a change to the Azure document. The one permitted match is
        the environment variable the app itself reads for its revision
        (`os.environ["RENDER_GIT_COMMIT"]` in app/main.py) — that name is code, not
        a platform reference, and renaming it is an app change.
        """
        from app import main as app_main
        markdown = app_main.azure_deployment_guide()["markdown"]
        stray = [line.strip() for line in markdown.splitlines()
                 if "render" in line.lower() and "RENDER_GIT_COMMIT" not in line]
        self.assertFalse(stray, "the Azure guide must stay about Azure; found: " + " | ".join(stray[:3]))
        lowered = markdown.lower()
        for topic in ("azure database for postgresql", "azure-pipelines.yml", "docker compose",
                      "azure repos", "key vault"):
            self.assertIn(topic, lowered, "the guide must actually cover " + topic)


class FrontendBootTests(unittest.TestCase):
    """The dashboard must actually paint something.

    One throw during the first render unmounts the whole React tree. The
    operator sees a blank screen - no tabs, no message, nothing to click - while
    the API behind it answers every request with 200, so nothing server-side
    looks wrong. That is exactly how the guide-snapshot bug presented: a
    `getSnapshot` that returned a fresh `Set` on every call spun
    `useSyncExternalStore` into "Maximum update depth exceeded" and took all
    thirteen tabs with it. These checks pin the invariants that keep a single
    component failure from becoming an unusable app.
    """

    STATIC = ROOT / "app" / "static"
    COMPONENT_FILES = ("tf-settings.js", "variable-review.js", "settings-components.js", "import-components.js",
                       "scenario-components.js", "demo-components.js", "future-components.js",
                       "desktop-install-components.js", "devdeploy-components.js", "qa-agents-components.js",
                       "market-components.js")
    # Allocations that are never Object.is-stable, so returning one from a
    # getSnapshot guarantees an infinite render loop.
    FRESH_RETURN = re.compile(r"return\s+(?:new\b|[{[])")

    def frontend(self):
        """Every script the browser runs, in load order, plus the raw page."""
        index = (self.STATIC / "index.html").read_text()
        inline = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index, re.S))
        components = [(name, (self.STATIC / name).read_text()) for name in self.COMPONENT_FILES]
        # The component files are loaded by <script src> before the inline app
        # script, so all of it shares one global scope at runtime.
        return index, "\n".join(body for _name, body in components) + "\n" + inline

    @staticmethod
    def block_after(source, start):
        """The {...} body that opens at or after `start`, brace-matched."""
        open_at = source.index("{", start)
        depth = 0
        for i in range(open_at, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    return source[open_at + 1:i]
        raise AssertionError("unbalanced braces reading a function body")

    def snapshot_functions(self, blob):
        """Names passed as getSnapshot to useSyncExternalStore, with their bodies."""
        found = {}
        for subscribe, get_snapshot in re.findall(
            r"useSyncExternalStore\s*\(\s*([^,()]+)\s*,\s*([^,()]+)", blob
        ):
            name = get_snapshot.strip()
            if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                continue  # an inline arrow; nothing to look up
            definition = re.search(r"(?:const|let|var)\s+" + re.escape(name) + r"\s*=", blob) or \
                re.search(r"function\s+" + re.escape(name) + r"\s*\(", blob)
            self.assertIsNotNone(definition, "getSnapshot %r is used but never defined" % name)
            found[name] = (subscribe.strip(), self.block_after(blob, definition.end()))
        self.assertTrue(found, "no useSyncExternalStore reads found - the check has gone stale")
        return found

    def test_UT_UI_01_getsnapshot_returns_a_cached_value(self):
        """No getSnapshot allocates, or React re-renders forever.

        `useSyncExternalStore` compares snapshots with Object.is and re-renders
        whenever they differ. A reader that builds a new Set/Map/object/array on
        every call therefore always looks "changed": React re-renders, re-reads,
        sees a new instance again, and loops until it aborts with "Maximum
        update depth exceeded" (minified React error #185) and unmounts the
        tree. Returning a primitive is fine - primitives compare by value.
        """
        _index, blob = self.frontend()
        offenders = []
        for name, (_subscribe, body) in self.snapshot_functions(blob).items():
            if self.FRESH_RETURN.search(body):
                offenders.append(name)
        self.assertFalse(
            offenders,
            "these getSnapshot functions return a freshly allocated value, which "
            "makes useSyncExternalStore loop until the whole app unmounts "
            "(blank screen): " + ", ".join(sorted(offenders)) + ". Cache the value "
            "against the raw store contents and return the same instance until "
            "they change.",
        )

    def test_UT_UI_02_a_cached_snapshot_is_invalidated_by_a_write(self):
        """Caching must not freeze the store: a write still has to re-render.

        The other half of UT-UI-01. A reader that returned one permanent object
        would also stop the update loop - by never updating at all, so a
        dismissed guide note would stay on screen. The cached reader has to key
        its cache on the stored value, and the writer has to notify subscribers.
        """
        _index, blob = self.frontend()
        reader = self.snapshot_functions(blob)["readHiddenTags"][1]
        self.assertRegex(reader, r"localStorage\.getItem", "the reader must still read the store")
        self.assertRegex(
            reader, r"if\s*\([^)]*===\s*hiddenTagsRaw\s*\)\s*return\s+hiddenTagsCache",
            "readHiddenTags must return the cached Set while the stored string is unchanged",
        )
        writer = self.block_after(blob, blob.index("const hideGuideTag"))
        self.assertRegex(writer, r"localStorage\.setItem", "dismissing a note must persist")
        self.assertRegex(writer, r"guideListeners\.forEach", "dismissing a note must notify subscribers")

    def test_UT_UI_03_a_writer_never_mutates_the_cached_snapshot(self):
        """hideGuideTag copies before it adds.

        Mutating the instance React is holding would both corrupt that snapshot
        and hide the change from the Object.is comparison, so the note would
        look dismissed in the store while staying on screen.
        """
        _index, blob = self.frontend()
        writer = self.block_after(blob, blob.index("const hideGuideTag"))
        self.assertNotRegex(
            writer, r"(?<!new Set\()readHiddenTags\(\)\s*;",
            "hideGuideTag must not take the cached Set directly",
        )
        self.assertRegex(
            writer, r"new Set\(\s*readHiddenTags\(\)\s*\)",
            "hideGuideTag must copy the cached snapshot before adding to it",
        )

    def test_UT_UI_04_a_component_crash_cannot_blank_the_whole_app(self):
        """The root render sits inside an error boundary.

        Without one, a throw in any component - a bad snapshot, a null field
        from the API, a typo - unmounts everything below the root and the
        operator gets a blank screen with no clue what happened. With one, the
        failure is named on screen and the rest of the deployment is still up.
        """
        index, blob = self.frontend()
        # assertRegex would echo the whole 140 KiB bundle on failure, so these
        # are plain booleans with a short message instead.
        self.assertTrue(re.search(r"class\s+ErrorBoundary\s+extends\s+React\.Component", blob),
                        "the dashboard must define `class ErrorBoundary extends React.Component`")
        self.assertIn("getDerivedStateFromError", blob, "the boundary must catch render errors")
        self.assertIn("componentDidCatch", blob, "the boundary must report what it caught")
        # createRoot(document.getElementById("root")).render(...) - the lazy
        # group has to step over the parens inside the argument.
        root_render = re.search(r"createRoot\(.*?\)\.render\((.*)\);", index)
        self.assertIsNotNone(root_render, "could not find the createRoot(...).render(...) call")
        self.assertRegex(
            root_render.group(1), r"e\(\s*ErrorBoundary\s*,",
            "the root render must be wrapped in ErrorBoundary, or a single "
            "component failure blanks every tab",
        )

    def test_UT_UI_05_every_component_rendered_is_defined(self):
        """No `e(Component, ...)` names something that was never defined.

        The other classic blank screen: an undefined component is a
        ReferenceError at the first render, which kills the tree just as
        thoroughly as a crash does. Every capitalised component passed to
        `e(...)` across the page and the three component files must be defined
        in one of them.
        """
        _index, blob = self.frontend()
        defined = set(re.findall(r"function\s+([A-Z][A-Za-z0-9_$]*)\s*\(", blob))
        defined |= set(re.findall(r"(?:const|let|var)\s+([A-Z][A-Za-z0-9_$]*)\s*=", blob))
        defined |= set(re.findall(r"class\s+([A-Z][A-Za-z0-9_$]*)\s", blob))
        referenced = set(re.findall(r"\be\(\s*([A-Z][A-Za-z0-9_$]*)\s*[,)]", blob))
        missing = sorted(referenced - defined)
        self.assertFalse(
            missing,
            "these components are rendered but never defined, so the first "
            "render throws a ReferenceError and the app shows a blank screen: "
            + ", ".join(missing),
        )

    def test_UT_FLOW_13_recorder_goto_commits_fast_and_survives_page_goto_timeout(self):
        """AI recording must commit navigation fast and never die on `Page.goto: Timeout 30000ms exceeded`."""
        from app import library_store
        from app.db import RecordingStep, SessionLocal
        from app.recorder import RecorderSession
        from tests.fakes import FakePage, FakePlaywright

        project = library_store.create_project("Fast recorder goto", "")
        rec = library_store.create_recording(project["id"], "Kroger learning", "https://www.kroger.com/")

        class SlowExternalPage(FakePage):
            def __init__(self):
                super().__init__(url="about:blank")
                self.load_states = []
                self.stopped = False

            async def goto(self, url, wait_until=None, timeout=None):
                self.gotos.append((url, wait_until, timeout))
                raise RuntimeError("Page.goto: Timeout 30000ms exceeded.")

            async def wait_for_load_state(self, state="domcontentloaded", timeout=None):
                self.load_states.append((state, timeout))

            async def evaluate(self, script, *args):
                if "window.stop()" in str(script):
                    self.stopped = True
                return None

        page = SlowExternalPage()
        fake_pw = FakePlaywright(page)

        class FakeAsyncPW:
            async def start(self):
                return fake_pw

            async def stop(self):
                return None

        session = RecorderSession(rec["id"], "https://www.kroger.com/", 0)

        async def exercise():
            with patch("app.recorder.async_playwright", return_value=FakeAsyncPW()):
                await session.start()
                self.assertEqual(session.status, "live")
                self.assertIsNone(session.error)
                self.assertTrue(page.stopped, "window.stop() must halt a hung external navigation")
                self.assertEqual(page.gotos[0][1], "commit", "recorder must use fast commit navigation")
                self.assertIsNotNone(session.latest_jpeg, "an initial frame must be captured before going live")
                await session.stop()

        asyncio.run(exercise())
        with SessionLocal() as db:
            steps = db.query(RecordingStep).filter_by(recording_id=rec["id"]).all()
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0].action, "navigate")
            self.assertEqual(steps[0].value, "https://www.kroger.com/")

    def test_UT_REP_11_replay_step_recovers_from_domcontentloaded_timeout_after_commit(self):
        """A replay navigate step whose page already committed recovers if domcontentloaded times out."""
        from tests.fakes import FakePage

        class PartiallyLoadedPage(FakePage):
            def __init__(self):
                super().__init__(url="https://www.kroger.com/")
                self.stopped = False

            async def goto(self, url, wait_until=None, timeout=None):
                self.gotos.append((url, wait_until, timeout))
                if wait_until == "domcontentloaded":
                    raise RuntimeError("Page.goto: Timeout 20000ms exceeded.")

            async def evaluate(self, script, *args):
                if "window.stop()" in str(script):
                    self.stopped = True

        page = PartiallyLoadedPage()
        asyncio.run(replay_step(page, {"action": "navigate", "value": "https://www.kroger.com/"}, {}))
        self.assertEqual(page.gotos[0][1], "domcontentloaded")
        self.assertTrue(page.stopped)


def AdaptivePacerForTest():
    """A pacer with no profile file, so tests never touch artifacts/batches."""
    from app.batch_runner import AdaptivePacer
    return AdaptivePacer(path=None, profile={})


if __name__ == "__main__":
    unittest.main()
