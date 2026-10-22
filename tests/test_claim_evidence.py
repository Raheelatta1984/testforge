"""The evidence behind the capability matrix, and the capabilities it under-claimed.

Three separate honesty problems live in this file:

1. **The deployed report used to contradict CI.** Market claims cite files,
   symbols, routes and tests. CI verifies them against a full checkout and
   passes; the Docker image only shipped ``app/``, so the same audit inside the
   container failed 17 probes and the hosted report published those claims as
   "unverified". These tests rebuild an image-shaped tree from the Dockerfile
   and re-run the audit against it.
2. **Capabilities that existed were rated as missing.** HTTP steps, network
   mocks, uploads/downloads and frame chains were implemented in both runtimes
   while the matrix still said otherwise. Claiming less than you do is still a
   false claim, so the ratings carry tests that fail if the code regresses.
3. **New capabilities must arrive with their evidence.** Visual baselines and
   bounded parallel execution are tested where they live, not only in the
   matrix.
"""
import asyncio
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.bootstrap import ensure_test_env

ensure_test_env()

ROOT = Path(__file__).resolve().parents[1]
COPY_RE = re.compile(r"^COPY\s+(\S+)\s+(\S+)\s*$", re.MULTILINE)


def _copy_specs() -> list[tuple[str, str]]:
    """``(source, destination)`` for every COPY in the Dockerfile."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    return COPY_RE.findall(text)


def _ignored() -> set[str]:
    rows = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(line)
    return set(rows)


def _dest(src: str, dest: str) -> str:
    dest = dest.replace("\\", "/")
    if dest in (".", "./"):
        return src
    if dest.startswith("./"):
        dest = dest[2:]
    return dest.rstrip("/")


def _build_image_tree(target: Path) -> Path:
    """Materialise an image-shaped copy of the repository (what Docker ships)."""
    ignored = _ignored()
    for src, dest in _copy_specs():
        top = src.replace("\\", "/").strip("/").split("/")[0]
        if top in ignored or src in ignored:
            continue
        source = ROOT / src
        if not source.exists():
            continue
        destination = target / _dest(src.replace("\\", "/").strip("/"), dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
    return target


class DockerClaimEvidenceTests(unittest.TestCase):
    """Every path a claim cites must exist inside the deployed image."""

    def test_CE_01_probes_outside_app_are_shipped_by_the_dockerfile(self):
        from app import market_intel
        shipped_tops = {src.replace("\\", "/").strip("/").split("/")[0]
                        for src, _ in _copy_specs()}
        shipped_tops |= {src.replace("\\", "/").strip("/")
                         for src, _ in _copy_specs() if "/" not in src}
        ignored = _ignored()
        missing = []
        for cap in market_intel.capabilities():
            for probe in (cap.get("self") or {}).get("evidence") or []:
                relative = str(probe.get("path") or "").replace("\\", "/").strip("/")
                if not relative or probe.get("kind") == "route":
                    continue
                top = relative.split("/")[0]
                if top not in shipped_tops and relative not in shipped_tops:
                    missing.append(f"{cap['id']}: {relative}")
                elif top in ignored or relative in ignored:
                    missing.append(f"{cap['id']}: {relative} (excluded by .dockerignore)")
        self.assertEqual(missing, [], "a claim cites evidence the image does not ship")

    def test_CE_02_the_audit_passes_against_an_image_shaped_tree(self):
        """The container's own audit must agree with CI's audit."""
        from app import market_intel
        tmp = Path(tempfile.mkdtemp(prefix="tf-image-"))
        try:
            _build_image_tree(tmp)
            receipt = market_intel.audit_claims(root=tmp)
            unverified = [row["id"] for row in receipt["results"] if not row["verified"]]
            self.assertEqual(unverified, [],
                             "the deployed report would publish these as unverified")
            self.assertTrue(receipt["summary"]["passed"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_CE_03_the_evidence_bundle_stays_small(self):
        """Shipping evidence must not quietly cost tens of megabytes."""
        evidence = 0
        for src, _dest in _copy_specs():
            top = src.split("/")[0]
            if top not in {"tests", ".github", "deploy", "tools"} and src not in {
                    "Dockerfile", "docker-compose.yml"}:
                continue
            path = ROOT / src
            if path.is_dir():
                evidence += sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            elif path.is_file():
                evidence += path.stat().st_size
        self.assertGreater(evidence, 0, "the evidence bundle disappeared from the image")
        self.assertLess(evidence, 4 * 1024 * 1024,
                        "shipping claim evidence grew past what a small image should carry")


class RuntimeCapabilityTests(unittest.TestCase):
    """Capabilities the matrix under-claimed: HTTP, mocks, files, frames.

    Each one has to exist in BOTH runtimes - the Python replay runtime and the
    JavaScript runtime shipped with an exported project - or a recording that
    replays locally cannot be exported, which is not a capability.
    """

    @classmethod
    def setUpClass(cls):
        cls.python_runtime = (ROOT / "app" / "step_runtime.py").read_text(encoding="utf-8")
        cls.js_runtime = (ROOT / "app" / "export_runtime.js").read_text(encoding="utf-8")

    def test_CE_10_http_step_exists_in_both_runtimes_with_assertions(self):
        from app.step_runtime import ACTIONS, normalize_step
        self.assertIn("api_request", ACTIONS)
        for needle in ("api_request", "json_path", "capture"):
            self.assertIn(needle, self.python_runtime)
            self.assertIn(needle, self.js_runtime)
        step = normalize_step({"action": "api_request", "value": "https://api.example.test/v1/orders",
                               "selector": {"method": "POST", "status": 201, "json_path": "id",
                                            "expected": 42, "capture": "order_id",
                                            "headers": {"Accept": "application/json"}}})
        self.assertEqual(step["action"], "api_request")
        self.assertEqual(step["selector"]["status"], 201)
        for bad in ({"method": "CONNECT"}, {"status": 99}, {"status": True},
                    {"capture": "not a variable!"}, {"headers": []}):
            with self.assertRaises(ValueError):
                normalize_step({"action": "api_request", "value": "https://api.example.test/x",
                                "selector": bad})

    def test_CE_11_network_mocks_exist_in_both_runtimes_and_are_bounded(self):
        from app.step_runtime import ACTIONS, MAX_TEXT, normalize_step
        for action in ("mock_route", "clear_routes"):
            self.assertIn(action, ACTIONS)
            self.assertIn(action, self.python_runtime)
            self.assertIn(action, self.js_runtime)
        # Both runtimes cap the number of live mocks; an unbounded route table is
        # a memory leak with extra steps.
        self.assertIn("At most ten network mocks", self.python_runtime)
        self.assertIn("Network mock limit reached", self.js_runtime)
        step = normalize_step({"action": "mock_route", "value": "**/api/orders",
                               "selector": {"status": 503, "body": "{}",
                                            "content_type": "application/json"}})
        self.assertEqual(step["selector"]["status"], 503)
        normalize_step({"action": "mock_route", "value": "**/api/orders",
                        "selector": {"abort": True}})
        for bad in ("", "x" * 1001):
            with self.assertRaises(ValueError):
                normalize_step({"action": "mock_route", "value": bad, "selector": {}})
        with self.assertRaises(ValueError):
            normalize_step({"action": "mock_route", "value": "**/api",
                            "selector": {"body": "x" * (MAX_TEXT + 1)}})
        with self.assertRaises(ValueError):
            normalize_step({"action": "mock_route", "value": "**/api", "selector": {"status": 700}})

    def test_CE_12_uploads_and_downloads_are_first_class_steps(self):
        from app.step_runtime import ACTIONS, MAX_FILE, normalize_step
        for action in ("upload_file", "download"):
            self.assertIn(action, ACTIONS)
            self.assertIn(action, self.python_runtime)
            self.assertIn(action, self.js_runtime)
        # Upload fixtures are validated, not trusted: filename and size caps.
        with self.assertRaises(ValueError):
            normalize_step({"action": "upload_file", "value": "",
                            "selector": {"primary": "#file", "filename": "../../secret",
                                         "base64": "YQ=="}})
        step = normalize_step({"action": "upload_file", "value": "",
                               "selector": {"primary": "#file", "filename": "invoice.pdf",
                                            "base64": "YQ==", "mime_type": "application/pdf"}})
        self.assertEqual(step["selector"]["filename"], "invoice.pdf")
        # Downloads assert the filename and enforce the same 512 KiB evidence cap.
        self.assertIn("Download filename did not match", self.python_runtime)
        self.assertIn("Download filename did not match", self.js_runtime)
        self.assertLessEqual(MAX_FILE, 512 * 1024)

    def test_CE_13_frame_chains_and_page_aliases_are_authorable(self):
        from app.step_runtime import normalize_step
        self.assertIn("frames", self.python_runtime)
        self.assertIn("frames", self.js_runtime)
        step = normalize_step({"action": "fill", "value": "Inside",
                               "selector": {"primary": "#inside", "frames": ["#outer", "#inner"]}})
        self.assertEqual(step["selector"]["frames"], ["#outer", "#inner"])
        # Bounded: five frames, and a badly typed chain is refused.
        with self.assertRaises(ValueError):
            normalize_step({"action": "click", "selector": {"primary": "#a", "frames": ["#1"] * 6}})
        with self.assertRaises(ValueError):
            normalize_step({"action": "click", "selector": {"primary": "#a", "frames": "#1"}})
        # Popups: an alias is required and is matched strictly.
        popup = normalize_step({"action": "open_popup", "value": "Click details",
                                "selector": {"primary": "#details", "alias": "details"}})
        self.assertEqual(popup["selector"]["alias"], "details")
        with self.assertRaises(ValueError):
            normalize_step({"action": "open_popup", "value": "x", "selector": {"primary": "#d",
                                                                              "alias": "bad alias!"}})


class VisualRegressionTests(unittest.TestCase):
    """Deterministic baselines: store, bounded diff, honest verdict."""

    @staticmethod
    def _image(width=120, height=80, colour=(20, 20, 20), marker=None):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (width, height), colour)
        if marker:
            ImageDraw.Draw(image).rectangle(marker, fill=(240, 240, 240))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_CE_20_identical_images_pass(self):
        from app.visual_regression import compare
        payload = compare(self._image(), self._image())
        self.assertEqual(payload["verdict"], "pass")
        self.assertEqual(payload["ratio"], 0.0)
        self.assertIsNone(payload["bbox"])
        self.assertEqual(payload["baseline"]["sha256"], payload["candidate"]["sha256"])

    def test_CE_21_a_changed_region_fails_and_is_located(self):
        from app.visual_regression import compare
        payload = compare(self._image(), self._image(marker=(10, 10, 40, 40)))
        self.assertEqual(payload["verdict"], "fail")
        self.assertGreater(payload["ratio"], 0.005)
        box = payload["bbox"]
        self.assertTrue(box and box[0] <= 12 and box[2] >= 38, f"bbox {box} should cover the change")

    def test_CE_22_threshold_and_tolerance_are_respected(self):
        from app.visual_regression import compare, normalise_threshold
        candidate = self._image(marker=(0, 0, 5, 5))
        self.assertEqual(compare(self._image(), candidate, threshold=0.9)["verdict"], "pass")
        self.assertEqual(compare(self._image(), candidate, threshold=0.0)["verdict"], "fail")
        self.assertEqual(normalise_threshold("nonsense"), normalise_threshold(None))
        self.assertEqual(normalise_threshold(99), 0.5)
        self.assertEqual(normalise_threshold(-1), 0.0)
        # A one-value JPEG-ish wobble must not fail the gate.
        self.assertEqual(compare(self._image(), self._image(colour=(28, 28, 28)))["verdict"], "pass")

    def test_CE_23_size_mismatch_is_reported_not_hidden(self):
        from app.visual_regression import compare
        payload = compare(self._image(width=120), self._image(width=60, height=40,
                                                             colour=(200, 200, 200)))
        self.assertTrue(payload["resized"])
        self.assertEqual(payload["verdict"], "fail")
        self.assertEqual(payload["baseline"]["width"], 120)
        self.assertEqual(payload["candidate"]["width"], 60)

    def test_CE_24_bounds_are_enforced(self):
        from app import visual_regression as vr
        with self.assertRaises(ValueError):
            vr.compare(b"x" * (vr.MAX_IMAGE_BYTES + 1), b"x")
        from PIL import Image
        huge = Image.new("RGB", (3000, 3000))
        buffer = io.BytesIO()
        huge.save(buffer, format="PNG")
        with self.assertRaises(ValueError):
            vr.compare(buffer.getvalue(), buffer.getvalue())

    def test_CE_25_store_promotes_lists_compares_and_deletes(self):
        from app import visual_regression as vr
        from app.config import ARTIFACTS
        root = Path(tempfile.mkdtemp(prefix="tf-visual-"))
        try:
            store = vr.BaselineStore(root)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "step-1.jpg").write_bytes(self._image())
            (run_dir / "step-2.jpg").write_bytes(self._image(marker=(0, 0, 30, 30)))
            (run_dir / "notes.txt").write_text("not evidence")
            promoted = store.promote(run_dir, "recording-1", "1024x640", run_id="run-1")
            self.assertEqual(len(promoted), 2, "only step screenshots become baselines")
            self.assertEqual(store.list("recording-1")[0]["step_index"], 1)

            # Same run, same baselines: passes.
            clean = store.compare_run(run_dir, "recording-1", "1024x640", run_id="run-1")
            self.assertEqual(clean["verdict"], "pass")
            self.assertEqual(clean["passed"], 2)

            # A regression in step 2 fails the gate and names the step.
            (run_dir / "step-2.jpg").write_bytes(self._image(marker=(0, 0, 60, 60)))
            dirty = store.compare_run(run_dir, "recording-1", "1024x640", run_id="run-1")
            self.assertEqual(dirty["verdict"], "fail")
            self.assertEqual([row["step_index"] for row in dirty["results"]
                              if row["verdict"] == "fail"], [2])

            # A recording with no baseline is 'unknown', never 'pass'.
            other = root / "runs" / "run-2"
            other.mkdir(parents=True)
            (other / "step-1.jpg").write_bytes(self._image())
            unknown = store.compare_run(other, "recording-2", "1024x640")
            self.assertEqual(unknown["results"][0]["status"], "missing_baseline")
            self.assertEqual(unknown["verdict"], "fail")

            key = promoted[0]["key"]
            self.assertTrue(store.image(key))
            self.assertTrue(store.delete(key))
            self.assertFalse(store.delete(key))
            self.assertIsNone(store.image(key))
            # A hostile key resolves to nothing rather than escaping the store.
            self.assertIsNone(store.image("../../etc/passwd"))
            self.assertIsNone(store.meta("..%2f..%2fetc%2fpasswd"))
            with self.assertRaises(ValueError):
                store.promote(run_dir, "bad id!", "1024x640")
            with self.assertRaises(ValueError):
                store.save("not-a-key", b"x" * 10)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertTrue(str(ARTIFACTS))

    def test_CE_26_baseline_keys_are_deterministic(self):
        from app.visual_regression import baseline_key
        self.assertEqual(baseline_key("rec-1", 3, "1024x640"),
                         baseline_key("rec-1", 3, "1024x640"))
        self.assertNotEqual(baseline_key("rec-1", 3, "1024x640"),
                            baseline_key("rec-1", 4, "1024x640"))
        self.assertNotEqual(baseline_key("rec-1", 3, "1024x640"),
                            baseline_key("rec-1", 3, "800x600"))
        self.assertRegex(baseline_key("rec-1", 3), r"^[0-9a-f]{32}$")


class VisualEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)
        cls.root = Path(tempfile.mkdtemp(prefix="tf-visual-api-"))
        cls.run_dir = cls.root / "runs" / "run-api"
        cls.run_dir.mkdir(parents=True)
        from PIL import Image, ImageDraw
        for index in (1, 2):
            image = Image.new("RGB", (100, 60), (30, 30, 30))
            if index == 2:
                ImageDraw.Draw(image).rectangle((0, 0, 50, 30), fill=(250, 250, 250))
            image.save(cls.run_dir / f"step-{index}.jpg", format="JPEG")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def _patch_artifacts(self):
        return patch.dict(os.environ, {"TF_ARTIFACTS": str(self.root)}), \
            patch("app.visual_regression.ARTIFACTS", str(self.root)), \
            patch("app.visual_routes.ARTIFACTS", str(self.root))

    def test_CE_30_endpoints_promote_compare_and_clean_up(self):
        from app import visual_regression as vr
        env, art_routes, art_module = self._patch_artifacts()
        with env, art_routes, art_module:
            status = self.client.get("/api/visual/status").json()
            self.assertIn("baselines", status)
            promoted = self.client.post("/api/visual/baselines",
                                        json={"recording_id": "rec-api", "run_id": "run-api",
                                              "viewport": "1024x640"})
            self.assertEqual(promoted.status_code, 201)
            listed = self.client.get("/api/visual/baselines", params={"recording_id": "rec-api"}).json()
            self.assertEqual(listed["count"], 2)
            key = listed["baselines"][0]["key"]
            self.assertEqual(self.client.get(f"/api/visual/baselines/{key}").status_code, 200)
            # Compare the same evidence: it matches the baselines it promoted.
            clean = self.client.post("/api/visual/compare",
                                     json={"recording_id": "rec-api", "run_id": "run-api",
                                           "viewport": "1024x640"}).json()
            self.assertEqual(clean["verdict"], "pass")
            # Now move the page and the gate has to fail.
            from PIL import Image, ImageDraw
            shifted = Image.new("RGB", (100, 60), (30, 30, 30))
            ImageDraw.Draw(shifted).rectangle((0, 0, 60, 50), fill=(250, 250, 250))
            shifted.save(self.run_dir / "step-2.jpg", format="JPEG")
            dirty = self.client.post("/api/visual/compare",
                                     json={"recording_id": "rec-api", "run_id": "run-api",
                                           "viewport": "1024x640"}).json()
            self.assertEqual(dirty["verdict"], "fail")
            self.assertEqual(dirty["failed"], 1)
            for row in listed["baselines"]:
                self.client.delete(f"/api/visual/baselines/{row['key']}")
            self.assertEqual(self.client.get("/api/visual/baselines",
                                             params={"recording_id": "rec-api"}).json()["count"], 0)
            self.assertEqual(self.client.post("/api/visual/compare",
                                              json={"recording_id": "rec-api",
                                                    "run_id": "nope"}).status_code, 404)
            self.assertEqual(self.client.post("/api/visual/baselines",
                                              json={"run_id": "run-api"}).status_code, 400)


class PoolTests(unittest.TestCase):
    """Bounded, fair, cancellable parallelism."""

    def test_CE_40_pool_width_is_clamped(self):
        from app.bounded_pool import pool_width
        self.assertEqual(pool_width(4, ceiling=8), 4)
        self.assertEqual(pool_width(9, ceiling=8), 8)
        self.assertEqual(pool_width(5, ceiling=8, budget=2), 2)
        self.assertEqual(pool_width(0), 1)
        self.assertEqual(pool_width("nonsense"), 1)
        self.assertEqual(pool_width(None, default=3), 3)

    def test_CE_41_the_pool_never_exceeds_its_width(self):
        from app.bounded_pool import BoundedPool
        live = 0
        peak = 0

        async def worker(item):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                await asyncio.sleep(0.005 if item % 2 else 0.02)
                return item
            finally:
                live -= 1

        async def scenario():
            pool = BoundedPool(3)
            results = await pool.map(range(12), worker)
            return pool, results

        pool, results = asyncio.run(scenario())
        self.assertLessEqual(peak, 3)
        self.assertEqual([row["value"] for row in results], list(range(12)))
        self.assertEqual(pool.report()["completed"], 12)
        self.assertLessEqual(pool.report()["peak"], 3)

    def test_CE_42_one_failure_does_not_stop_the_pool(self):
        from app.bounded_pool import BoundedPool

        async def worker(item):
            await asyncio.sleep(0.001)
            if item == 3:
                raise RuntimeError("step blew up")
            return item * 2

        async def scenario():
            return await BoundedPool(2).map(range(6), worker)

        results = asyncio.run(scenario())
        self.assertEqual(results[3]["ok"], False)
        self.assertIn("RuntimeError", results[3]["error"])
        self.assertEqual([row["value"] for row in results if row["ok"]], [0, 2, 4, 8, 10])

    def test_CE_43_cancellation_is_fair_and_reports_every_item(self):
        from app.bounded_pool import BoundedPool

        async def worker(item):
            await asyncio.sleep(0.05)
            return item

        async def scenario():
            pool = BoundedPool(2)
            task = asyncio.ensure_future(pool.map(range(8), worker))
            await asyncio.sleep(0.01)
            pool.cancel()
            return await task

        results = asyncio.run(scenario())
        self.assertEqual(len(results), 8)
        self.assertTrue(all(row["cancelled"] for row in results[2:]),
                        "work that never started is reported cancelled, not lost")
        self.assertTrue(any(row["ok"] for row in results[:2]))

    def test_CE_44_execution_pool_respects_the_browser_budget(self):
        from app import guardrails, run_pool
        with patch.object(guardrails, "MAX_CONCURRENT_BROWSERS", 1):
            self.assertEqual(run_pool.configured_width(), 1)
            with patch.dict(os.environ, {"TF_MAX_CONCURRENT_RUNS": "6"}):
                self.assertEqual(run_pool.configured_width(), 1)
        with patch.object(guardrails, "MAX_CONCURRENT_BROWSERS", 4):
            with patch.dict(os.environ, {"TF_MAX_CONCURRENT_RUNS": "6"}):
                self.assertEqual(run_pool.configured_width(), 4)
            with patch.dict(os.environ, {"TF_MAX_CONCURRENT_RUNS": "2"}):
                self.assertEqual(run_pool.configured_width(), 2)

    def test_CE_45_execution_pool_runs_bounds_and_cancels(self):
        from app import run_pool
        live = 0
        peak = 0
        done = []

        async def unit(index):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                await asyncio.sleep(0.001 if index < 3 else 0.5)
                done.append(index)
            finally:
                live -= 1

        async def scenario():
            pool = run_pool.ExecutionPool(width_fn=lambda: 3)
            with guardrails_patch():
                for index in range(6):
                    pool.submit(unit(index), name=f"unit-{index}")
                await asyncio.sleep(0.05)
                cancelled = pool.cancel_all()
                # Cancellation lands on the next loop turn; wait for it so the
                # counters describe what actually happened.
                await asyncio.sleep(0.05)
                report = pool.report()
            await asyncio.sleep(0.05)
            return report, cancelled

        def guardrails_patch():
            from app import guardrails
            return patch.object(guardrails, "MAX_CONCURRENT_BROWSERS", 3)

        report, cancelled = asyncio.run(scenario())
        self.assertLessEqual(peak, 3)
        self.assertEqual(done, [0, 1, 2], "fast units finish, slow ones are cancelled")
        self.assertGreater(cancelled, 0)
        self.assertEqual(report["width"], 3)
        self.assertEqual(report["browser_budget"], 3)
        self.assertGreaterEqual(report["cancelled"], 1)
        self.assertGreaterEqual(report["completed"], 3)

    def test_CE_46_the_queue_status_exposes_the_pool(self):
        from fastapi.testclient import TestClient
        from app.main import app
        payload = TestClient(app).get("/api/runs/queue/status").json()
        self.assertIn("pool", payload)
        self.assertEqual(payload["pool"]["width"], 1,
                         "the hosted default stays sequential")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class CiRunnerTests(unittest.TestCase):
    """`tf` - the packaged CI runner the matrix claimed was only a roadmap."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tf-cli-"))
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _opener(routes, calls):
        """A fake urlopen: routes maps (method, path-suffix) to (status, payload)."""

        def open(method, url, data, headers, timeout):
            calls.append({"method": method, "url": url, "data": data, "headers": headers})
            for (want_method, suffix), payload in routes.items():
                if want_method == method and url.endswith(suffix):
                    status, body = payload
                    return status, body if isinstance(body, bytes) else json.dumps(body).encode()
            return 404, b'{"detail":"not found"}'

        return open

    def _plan(self, payload):
        path = self.root / "plan.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_CE_50_a_single_recording_plan_queues_waits_and_collects(self):
        from tools import tf
        routes = {
            ("POST", "/api/runs"): (201, {"run_id": "run-1", "status": "queued"}),
            ("GET", "/api/runs/run-1"): (200, {"status": "passed", "passed": 3, "failed": 0,
                                               "skipped": 0, "cancelled": 0, "error": 0,
                                               "total": 3}),
            ("GET", "/api/runs/run-1/junit.xml"): (200, b"<testsuite name='run-1'/>"),
            ("GET", "/api/runs/run-1/report.json"): (200, {"run_id": "run-1", "status": "passed"}),
        }
        junit_path = self.root / "out" / "results.xml"
        real = tf.TestForgeClient  # captured before the patch replaces the name

        def factory(base=None, token=None, timeout=None, opener=None):
            return real(base=base, token=token, opener=self._opener(routes, self.calls))
        with patch.object(tf, "TestForgeClient", factory):
            code = tf.main(["--base", "http://tf.test", "--token", "t" * 40, "run",
                            "--plan", self._plan({"recording_id": "rec-1",
                                                  "screenshots": "failure"}),
                            "--wait", "--junit", str(junit_path), "--json",
                            str(self.root / "report.json"), "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(junit_path.read_text(), "<testsuite name='run-1'/>")
        self.assertEqual(json.loads((self.root / "report.json").read_text())["run_id"], "run-1")
        queued = [call for call in self.calls if call["method"] == "POST"]
        self.assertEqual(json.loads(queued[0]["data"].decode())["recording_id"], "rec-1")
        self.assertEqual(queued[0]["headers"]["Authorization"], "Bearer " + "t" * 40)

    def test_CE_51_a_multi_recording_plan_becomes_a_batch_and_can_fail(self):
        from tools import tf
        routes = {
            ("POST", "/api/runs/batch"): (201, {"batch_id": "batch-1", "status": "queued"}),
            ("GET", "/api/runs/batch/batch-1"): (200, {"status": "failed", "counters": {
                "passed": 1, "failed": 2, "skipped": 0, "cancelled": 0, "error": 0, "total": 3}}),
            ("GET", "/api/runs/batch/batch-1/junit.xml"): (200, b"<testsuite name='batch-1'/>"),
            ("GET", "/api/runs/batch/batch-1"): (200, {"status": "failed"}),
        }
        real = tf.TestForgeClient  # captured before the patch replaces the name

        def factory(base=None, token=None, timeout=None, opener=None):
            return real(base=base, token=token, opener=self._opener(routes, self.calls))

        with patch.object(tf, "TestForgeClient", factory):
            code = tf.main(["--base", "http://tf.test", "run", "--plan",
                            self._plan({"recording_ids": ["a", "b", "c"], "name": "nightly"}),
                            "--wait", "--junit", str(self.root / "b.xml"), "--quiet"])
        self.assertEqual(code, 1, "a failed suite must fail the pipeline")
        posted = [call for call in self.calls if call["method"] == "POST"]
        self.assertEqual(json.loads(posted[0]["data"].decode())["recording_ids"], ["a", "b", "c"])

    def test_CE_52_waiting_gives_up_with_a_pipeline_error_not_a_test_failure(self):
        from tools import tf
        client = tf.TestForgeClient(base="http://tf.test",
                                    opener=self._opener({("GET", "/api/runs/run-9"):
                                                         (200, {"status": "running"})}, self.calls))
        sleeps = []
        with self.assertRaises(tf.TestForgeError):
            client.wait({"kind": "run", "run_id": "run-9"}, timeout=1.0, interval=0.5,
                        sleep=lambda seconds: sleeps.append(seconds))
        self.assertGreater(len(sleeps), 0)

    def test_CE_53_usage_and_transport_errors_exit_two(self):
        from tools import tf
        self.assertEqual(tf.main(["run", "--plan", str(self.root / "missing.json")]), 2)
        self.assertEqual(tf.main(["run", "--plan", self._plan({})]), 2)
        bad = self.root / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        self.assertEqual(tf.main(["run", "--plan", str(bad)]), 2)
        with patch.object(tf.TestForgeClient, "request",
                          side_effect=tf.TestForgeError("cannot reach http://tf.test")):
            self.assertEqual(tf.main(["run", "--plan", self._plan({"recording_id": "a"}),
                                      "--quiet"]), 2)

    def test_CE_54_status_junit_and_report_commands(self):
        from tools import tf
        routes = {
            ("GET", "/api/runs/run-1"): (200, {"status": "passed"}),
            ("GET", "/api/runs/run-1/junit.xml"): (200, b"<testsuite/>"),
            ("GET", "/api/runs/run-1/report.json"): (200, {"run_id": "run-1"}),
            ("GET", "/api/runs/run-2"): (200, {"status": "failed"}),
        }
        real = tf.TestForgeClient  # captured before the patch replaces the name

        def factory(base=None, token=None, timeout=None, opener=None):
            return real(base=base, token=token, opener=self._opener(routes, self.calls))
        out = self.root / "one.xml"
        with patch.object(tf, "TestForgeClient", factory):
            self.assertEqual(tf.main(["junit", "run-1", "-o", str(out), "--quiet"]), 0)
            self.assertEqual(tf.main(["report", "run-1", "-o",
                                      str(self.root / "one.json"), "--quiet"]), 0)
            self.assertEqual(tf.main(["status", "run-1", "--quiet"]), 0)
            self.assertEqual(tf.main(["status", "run-2", "--quiet"]), 1)
        self.assertIn("<testsuite", out.read_text())
        self.assertEqual(json.loads((self.root / "one.json").read_text())["run_id"], "run-1")
