"""One smoke plan, three engines: chromium, firefox and webkit for real.

The execution matrix already records a browser per profile and the runner
launches exactly that engine (`getattr(pw, profile['browser'])` in
app/plan_runtime.py). This test proves it by running the same smoke plan on
each engine, sequentially, against a local fixture server.

Run locally (an engine whose binary is missing is skipped, honestly):
  TF_TEST_BROWSER=1 python -m unittest tests.test_multi_browser -v

Run in CI (a missing engine fails the build instead of skipping, so the
multi-browser job can never pass on Chromium alone):
  TF_TEST_BROWSER=1 TF_REQUIRE_ENGINES=1 python -m unittest tests.test_multi_browser -v
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from tests.bootstrap import ensure_test_env
ensure_test_env()
from tests.test_browser_features import Handler
from app.plan_runtime import normalize_plan, run_plan

ENGINES = ("chromium", "firefox", "webkit")


def engine_binary(engine):
    """Absolute path of the installed engine binary, or None when missing."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            path = getattr(pw, engine).executable_path
    except Exception:
        return None
    return path if path and os.path.isfile(path) else None


class MatrixContract(unittest.TestCase):
    """The matrix contract needs no browser, so it runs in the unit job."""

    def test_matrix_accepts_exactly_the_three_supported_engines(self):
        base = {"steps": [{"action": "navigate", "value": "https://example.test/"}]}
        for engine in ENGINES:
            plan = normalize_plan({**base, "matrix": [{"browser": engine}]})
            self.assertEqual(plan["matrix"][0]["browser"], engine)
        with self.assertRaises(ValueError):
            normalize_plan({**base, "matrix": [{"browser": "lynx"}]})

    def test_missing_engine_binaries_are_reported_not_hidden(self):
        self.assertIsInstance(engine_binary("no-such-engine"), type(None))


@unittest.skipUnless(os.environ.get("TF_TEST_BROWSER") == "1", "Requires explicit browser opt-in")
class MultiBrowserSmoke(unittest.TestCase):
    ENGINES = ENGINES

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        missing = [engine for engine in cls.ENGINES if engine_binary(engine) is None]
        if missing and os.environ.get("TF_REQUIRE_ENGINES") == "1":
            raise AssertionError(
                "TF_REQUIRE_ENGINES=1 but these engine binaries are missing: "
                + ", ".join(missing))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def smoke_plan(self, engine):
        return {
            "setup": [{"action": "navigate", "value": self.base}],
            "flows": {"greet": [
                {"action": "fill", "selector": {"primary": "#name"}, "value": "Ada"},
                {"action": "click", "selector": {"primary": "#go"}}]},
            "steps": [{"flow": "greet"},
                      {"action": "assert_text", "selector": {"primary": "#out"},
                       "value": "Hello, Ada"}],
            "matrix": [{"browser": engine, "device": "desktop"}],
        }

    def test_MB_SMOKE_01_the_same_plan_passes_on_every_installed_engine(self):
        ran = []
        for engine in self.ENGINES:
            with self.subTest(engine=engine):
                binary = engine_binary(engine)
                if binary is None:
                    self.skipTest(f"{engine} is not installed here; "
                                  "CI installs it and TF_REQUIRE_ENGINES=1 forbids this skip")
                started = time.monotonic()
                with tempfile.TemporaryDirectory(prefix=f"tf-{engine}-") as tmp:
                    result = asyncio.run(run_plan(self.smoke_plan(engine), {}, tmp))
                elapsed = time.monotonic() - started
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["cases"][0]["browser"], engine)
                ran.append(f"{engine} ({elapsed:.1f}s via {binary})")
        if ran:
            print("\nMulti-browser smoke passed on: " + "; ".join(ran))
        else:
            print("\nMulti-browser smoke skipped: no engine binaries installed here; "
                  "CI installs them and TF_REQUIRE_ENGINES=1 forbids this skip")
