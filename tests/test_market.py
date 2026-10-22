"""Market position, step catalogue, impact map, JUnit results and the explorer.

The tests that matter here are the honesty ones: every TestForge "yes" must
still have evidence, an audit must fail when a probe rots, and an unverified
explorer finding must never be presented as a confirmed defect.
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.bootstrap import ensure_test_env

ensure_test_env()

from fastapi.testclient import TestClient

from app import ci_results, explorer, impact_map, library_store, market_intel, market_report, step_catalog
from app.main import app


_LIBRARY = []


def make_library():
    """A tiny project with two recordings, isolated in the temp library.

    Created once per process: a second call would duplicate every step and make
    an occurrence count meaningless.
    """
    if _LIBRARY:
        return _LIBRARY[0]
    step_catalog._CACHE.update({"at": 0.0, "key": None, "payload": None})
    impact_map._CACHE.update({"at": 0.0, "key": None, "payload": None})
    project = library_store.create_project("Market test project", "https://shop.example.test")
    checkout = library_store.create_recording(
        project["id"], "Checkout pays with card", "https://shop.example.test/checkout",
        steps=[
            {"action": "navigate", "value": "https://shop.example.test/checkout", "label": "Open https://shop.example.test/checkout"},
            {"action": "click", "selector": {"kind": "test_id", "primary": "place-order"}, "value": "", "label": "Click Place order"},
            {"action": "click", "selector": {"kind": "test_id", "primary": "place-order"}, "value": "", "label": "Click Place order"},
        ], tags="smoke")
    login = library_store.create_recording(
        project["id"], "Sign in and out", "https://shop.example.test/login",
        steps=[
            {"action": "navigate", "value": "https://shop.example.test/login", "label": "Open https://shop.example.test/login"},
            {"action": "type", "selector": {"kind": "css", "primary": "input[name=email]"}, "value": "ada@example.test", "label": "Type Ada@Example.test"},
            {"action": "click", "selector": {"kind": "role", "primary": "Sign in"}, "value": "", "label": "Click Sign in"},
        ], tags="critical")
    _LIBRARY.append((project, checkout, login))
    return project, checkout, login


class MarketMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.full = market_intel.matrix()

    def test_the_matrix_is_structurally_valid_and_complete(self):
        problems = market_intel.validate()
        self.assertEqual(problems, [], problems)
        self.assertEqual(len(self.full["capabilities"]), 42)
        self.assertGreaterEqual(len(self.full["tools"]), 10)
        self.assertTrue(any(tool.get("self") for tool in self.full["tools"]))
        # Every capability carries a rating for every competitor, with no blanks.
        competitor_ids = [tool["id"] for tool in self.full["tools"] if not tool.get("self")]
        for capability in self.full["capabilities"]:
            for tool_id in competitor_ids:
                self.assertIn(capability["ratings"].get(tool_id),
                              ("yes", "partial", "no", "unknown"),
                              f"{capability['id']} / {tool_id}")

    def test_every_testforge_yes_claim_has_evidence(self):
        for capability in self.full["capabilities"]:
            if capability["self"]["claimed"] == "yes":
                self.assertTrue(capability["self"]["evidence"],
                                f"{capability['id']} claims yes without evidence")

    def test_claim_audit_passes_and_every_probe_resolves(self):
        audit = market_intel.audit_claims()
        self.assertTrue(audit["summary"]["passed"],
                        json.dumps([row["id"] for row in audit["results"] if not row["verified"]]))
        self.assertEqual(audit["summary"]["failed"], 0)
        self.assertRegex(audit["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(audit["results"]), len(self.full["capabilities"]))

    def test_audit_fails_when_the_evidence_is_not_there(self):
        with tempfile.TemporaryDirectory() as empty:
            receipt = market_intel.audit_claims(root=Path(empty), persist=False)
        self.assertFalse(receipt["summary"]["passed"])
        self.assertGreater(receipt["summary"]["failed"], 0)
        self.assertEqual(receipt["summary"]["structural_problems"], 0)

    def test_unknown_is_never_counted_as_a_competitor_win(self):
        data = market_intel.load()
        synthetic = copy.deepcopy(data)
        capability = synthetic["capabilities"][0]
        capability["id"] = "synthetic-gap-check"
        capability["ratings"] = {}
        capability["unknown"] = [tool["id"] for tool in synthetic["tools"] if not tool.get("self")]
        capability["self"] = {"rating": "no", "note": "not implemented", "evidence": []}
        self.assertEqual(market_intel.validate(synthetic), [])
        rows = market_intel.gaps(synthetic, audit_by_id={})
        self.assertNotIn("synthetic-gap-check", [row["id"] for row in rows])

    def test_scoreboard_maths_matches_the_ratings(self):
        for row in self.full["scoreboard"]:
            known = row["yes"] + row["partial"] + row["no"]
            self.assertEqual(known, row["known"])
            expected = round(100 * (row["yes"] + 0.5 * row["partial"]) / known, 1) if known else None
            self.assertEqual(row["score"], expected)

    def test_gaps_and_differentiators_are_disjoint_and_explained(self):
        gap_ids = {row["id"] for row in self.full["gaps"]}
        differentiator_ids = {row["id"] for row in self.full["differentiators"]}
        self.assertFalse(gap_ids & differentiator_ids)
        for gap in self.full["gaps"]:
            self.assertIn(gap["our_rating"], ("partial", "no", "unverified", "unknown"))
            self.assertTrue(gap["leaders"])
            self.assertTrue(gap["roadmap"])
        for row in self.full["differentiators"]:
            self.assertTrue(row["note"])

    def test_market_endpoints_answer(self):
        for url in ("/api/market/matrix", "/api/market/competitors", "/api/market/capabilities",
                    "/api/market/gaps", "/api/market/differentiators", "/api/market/summary",
                    "/api/market/audit", "/api/market/evidence"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"{url}: {response.text[:200]}")
        self.assertEqual(self.client.get("/api/market/evidence?capability=not-a-capability").status_code, 404)

    def test_audit_endpoint_persists_a_receipt(self):
        response = self.client.post("/api/market/audit")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["summary"]["passed"])
        self.assertTrue(payload.get("receipt_path"))
        self.assertTrue(Path(payload["receipt_path"]).is_file())

    def test_html_report_is_self_contained_and_escaped(self):
        response = self.client.get("/api/market/report.html")
        self.assertEqual(response.status_code, 200)
        document = response.text
        self.assertIn("<style>", document)
        self.assertNotIn("<script", document.lower())
        self.assertNotIn("http://", document.replace("http://www.w3.org", ""))
        self.assertIn("Capability matrix", document)
        self.assertIn("Evidence appendix", document)
        self.assertIn("frame-ancestors 'none'", response.headers.get("content-security-policy", ""))
        self.assertIn('X-Content-Type-Options', response.headers)
        self.assertEqual(market_report._esc("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")

    def test_markdown_report_has_tables_and_the_audit_receipt(self):
        response = self.client.get("/api/market/report.md")
        self.assertEqual(response.status_code, 200)
        document = response.text
        self.assertIn("| Capability |", document)
        self.assertIn("Claim audit", document)
        receipt = market_intel.audit_claims()["receipt_sha256"]
        self.assertIn(receipt, document)


class StepCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project, cls.checkout, cls.login = make_library()
        step_catalog.build(force=True)

    def test_the_step_catalogue_normalises_volatile_values(self):
        payload = step_catalog.catalog()
        self.assertGreater(payload["source"]["recordings"], 0)
        phrases = [entry["phrase"] for entry in payload["entries"]]
        self.assertIn("open <url>", phrases)
        self.assertIn("click place order", phrases)
        # The same step repeated inside one recording is one definition.
        place_order = next(entry for entry in payload["entries"] if entry["phrase"] == "click place order")
        self.assertEqual(place_order["occurrences"], 2)
        self.assertIn("place-order", place_order["selector"])
        self.assertTrue(place_order["placeholders"] == [])

    def test_step_search_is_deterministic_and_ranked(self):
        first = step_catalog.search("click sign in")
        second = step_catalog.search("click sign in")
        self.assertEqual(first["results"], second["results"])
        self.assertGreaterEqual(len(first["results"]), 1)
        self.assertEqual(first["results"][0]["phrase"], "click sign in")
        self.assertIn("sign", first["results"][0]["matched"])
        self.assertEqual(step_catalog.search("   ")["results"], [])

    def test_resolve_step_binds_ambiguous_and_unbound_phrases(self):
        bound = step_catalog.resolve_step("click place order")
        self.assertEqual(bound["status"], "bound")
        self.assertEqual(bound["best"]["phrase"], "click place order")
        unbound = step_catalog.resolve_step("approve a vendor invoice in the general ledger")
        self.assertEqual(unbound["status"], "unbound")
        self.assertTrue(unbound["advice"].startswith("No existing definition"))
        with self.assertRaises(ValueError):
            step_catalog.resolve_step("   ")

    def test_catalogue_endpoints_respect_bounds_and_scope(self):
        client = TestClient(app)
        catalog = client.get("/api/steps/catalog?limit=5")
        self.assertEqual(catalog.status_code, 200)
        self.assertLessEqual(len(catalog.json()["entries"]), 5)
        search = client.get("/api/steps/search?q=place%20order")
        self.assertEqual(search.status_code, 200)
        self.assertTrue(search.json()["results"])
        resolved = client.post("/api/steps/resolve", json={"phrase": "click place order"})
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["status"], "bound")
        self.assertEqual(client.post("/api/steps/resolve", json={}).status_code, 400)
        self.assertEqual(client.get("/api/steps/catalog?limit=0").status_code, 400)


class ImpactMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project, cls.checkout, cls.login = make_library()

    def test_impact_analysis_selects_tests_with_reasons_and_a_batch_payload(self):
        impact_map.build_map()
        result = impact_map.analyse(changed_files=["app/static/checkout-components.js"], max_tests=10)
        names = [row["name"] for row in result["selected"]]
        self.assertIn("Checkout pays with card", names)
        row = next(item for item in result["selected"] if item["name"] == "Checkout pays with card")
        self.assertTrue(row["reasons"])
        self.assertIn("checkout", row["matched_terms"])
        self.assertEqual(result["batch_payload"]["recording_ids"], [item["id"] for item in result["selected"]])
        self.assertGreaterEqual(result["reduction_pct"], 0)

    def test_priority_tags_always_run_and_unknown_changes_answer_honestly(self):
        result = impact_map.analyse(changed_files=["app/static/nothing-matches-this-file.js"],
                                    include_priorities=False)
        self.assertEqual(result["selected"], [])
        self.assertTrue(result["unmatched_hint"])
        strict = impact_map.analyse(changed_files=["app/static/nothing-matches-this-file.js"])
        self.assertTrue(strict["selected"])
        self.assertTrue(all("always-run tag" in " ".join(row["reasons"]) for row in strict["selected"]))
        priority = impact_map.analyse(labels=["Sign in"])
        self.assertTrue(any("always-run tag" in " ".join(row["reasons"]) for row in priority["selected"]))

    def test_an_empty_change_set_is_rejected(self):
        with self.assertRaises(ValueError):
            impact_map.analyse()

    def test_impact_endpoints_and_map(self):
        client = TestClient(app)
        payload = client.get("/api/impact/map").json()
        self.assertIn("nodes", payload)
        self.assertIn("edges", payload)
        response = client.post("/api/impact/analyse", json={"changed_files": ["app/static/checkout-components.js"]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["selected"])
        self.assertEqual(client.post("/api/impact/analyse", json={}).status_code, 400)
        self.assertEqual(client.post("/api/impact/analyse", json={"diff": "x" * 10}).status_code, 400)


FAST_PAGE = explorer.Page(
    url="https://shop.example.test/checkout", status=200,
    headers={"content-type": "text/html", "content-security-policy": "default-src 'self'",
             "x-content-type-options": "nosniff"},
    body="<html><head><title>Checkout</title></head><body>"
         "<a href='/logout'>Sign out</a><a href='https://other.example.test/x'>Partner</a>"
         "<button></button><div id='dup'></div><div id='dup'></div>"
         "<img src='https://shop.example.test/missing.png'></body></html>",
    elapsed_ms=120,
    links=["https://shop.example.test/logout", "https://other.example.test/x"],
    ids=["dup", "dup"],
    interactive=[{"tag": "button", "type": "", "id": "", "name": "", "aria_label": "", "title": "",
                  "alt": "", "placeholder": "", "text": "", "hidden": False},
                 {"tag": "a", "href": "/logout", "text": "Sign out"}],
    images=[{"src": "https://shop.example.test/missing.png", "alt": "", "ok": False}],
    title="Checkout", mode="fake",
)

BROKEN_PAGE = explorer.Page(
    url="https://shop.example.test/checkout", status=500,
    headers={"content-type": "text/html"},
    body="<html><head><title>Boom</title></head><body>Traceback (most recent call last): KeyError 'cart'"
         "<input type='password' name='pin'><form method='get' action='/login'>"
         "<input type='password' name='password'></form></body></html>",
    elapsed_ms=4200,
    links=["https://shop.example.test/logout", "https://shop.example.test/checkout"],
    ids=[], title="Boom", mode="fake",
    interactive=[{"tag": "input", "type": "password", "id": "", "name": "password", "aria_label": "",
                  "title": "", "alt": "", "placeholder": "", "text": "", "hidden": False}],
    forms=[{"action": "/login", "method": "get", "fields": [{"name": "password", "type": "password"}]}],
    images=[], console=["error: checkout failed to render"], page_errors=["TypeError: cart is undefined"],
    failed_requests=[{"url": "https://shop.example.test/api/cart", "error": "net::ERR_FAILED"}],
)


class FakeFetcher:
    """Deterministic stand-in for a browser or HTTP client."""

    mode = "fake"

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise explorer.FetchError("page not available")
        return copy.deepcopy(self.pages[url])


class ExplorerTests(unittest.TestCase):
    def test_unsafe_targets_are_rejected(self):
        for value in ("file:///etc/passwd", "ftp://example.test", "javascript:alert(1)",
                      "https://user:secret@shop.example.test/"):
            with self.assertRaises(explorer.ExplorerError):
                explorer.validate_target(value, allow_private=True)
        with self.assertRaises(explorer.ExplorerError):
            explorer.validate_target("http://127.0.0.1:8000/", allow_private=False)
        self.assertEqual(explorer.validate_target("https://shop.example.test", allow_private=False),
                         "https://shop.example.test/")

    def test_explorer_finds_defects_and_verifies_them_independently(self):
        fetcher = FakeFetcher({"https://shop.example.test/checkout": BROKEN_PAGE})
        report = explorer.Explorer("https://shop.example.test/checkout", fetcher=fetcher,
                                   max_pages=1, max_depth=0, max_verifications=6).scan()
        checks = {finding["check"] for finding in report["findings"]}
        for expected in ("http-status", "password-exposure", "error-leak", "console-error",
                         "page-error", "network-failure"):
            self.assertIn(expected, checks)
        slow = explorer.Page(url="https://shop.example.test/reports", status=200, elapsed_ms=5200,
                             headers={"content-type": "text/html", "content-security-policy": "default-src 'self'",
                                      "x-content-type-options": "nosniff"},
                             body="<html><head><title>Reports</title></head><body><a href='/x'>X</a></body></html>",
                             links=["https://shop.example.test/x"], title="Reports", mode="fake")
        slow_report = explorer.Explorer("https://shop.example.test/reports",
                                        fetcher=FakeFetcher({"https://shop.example.test/reports": slow}),
                                        max_pages=1, max_depth=0, max_verifications=1).scan()
        self.assertIn("slow-page", {finding["check"] for finding in slow_report["findings"]})
        self.assertGreater(report["counts"]["verified"], 0)
        self.assertTrue(all(finding["attempts"] >= 1 for finding in report["findings"]))
        # Destruction is never followed, and the crawl stays on one origin.
        self.assertNotIn("https://shop.example.test/logout", fetcher.calls)
        self.assertNotIn("https://other.example.test/x", fetcher.calls)
        self.assertRegex(report["receipt"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["receipt"]["findings"], report["counts"]["total"])

    def test_a_finding_that_does_not_reproduce_is_not_verified(self):
        class FlakyFetcher(FakeFetcher):
            def __init__(self):
                super().__init__({"https://shop.example.test/checkout": BROKEN_PAGE})
                self.count = 0

            def fetch(self, url):
                self.count += 1
                page = copy.deepcopy(BROKEN_PAGE if self.count == 1 else FAST_PAGE)
                page.url = url
                return page

        report = explorer.Explorer("https://shop.example.test/checkout", fetcher=FlakyFetcher(),
                                   max_pages=1, max_depth=0, max_verifications=4).scan()
        status_500 = [finding for finding in report["findings"] if finding["check"] == "http-status"]
        self.assertTrue(status_500)
        # The second fetch answered 200, so the 500 is a real but non-reproducing
        # observation: it must be reported unverified, not filed as a defect.
        self.assertFalse(status_500[0]["verified"])
        self.assertGreater(report["counts"]["unverified"], 0)
        markdown = explorer.defect_report(report)
        self.assertIn("NOT verified", markdown)
        self.assertIn(report["receipt"]["sha256"], markdown)

    def test_scan_endpoint_is_bounded_and_reports_safely(self):
        client = TestClient(app)
        capabilities = client.get("/api/explorer/capabilities")
        self.assertEqual(capabilities.status_code, 200)
        self.assertTrue(capabilities.json()["deterministic"])
        self.assertFalse(capabilities.json()["model_required"])
        rejected = client.post("/api/explorer/scan", json={"url": "http://127.0.0.1:9/"})
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("private", rejected.json()["detail"].lower())
        with patch("app.intel_routes.explorer.Explorer") as fake:
            fake.return_value.scan.return_value = {"target": "https://shop.example.test/", "counts": {"total": 0}}
            ok = client.post("/api/explorer/scan", json={"url": "https://shop.example.test/", "max_pages": 99})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(fake.call_args.kwargs["max_pages"], explorer.MAX_PAGES)


class JUnitResultsTests(unittest.TestCase):
    RUNS = [
        {"id": "r1", "recording_name": "Checkout <pays> & confirms", "status": "failed",
         "elapsed_seconds": 2.5, "batch_id": "b1",
         "execution_log": [{"order": 1, "action": "click", "label": "Pay", "status": "failed",
                            "error": "expected 'Order placed' but received 'Something went wrong'"}]},
        {"id": "r2", "recording_name": "Sign in", "status": "passed", "elapsed_seconds": 1.0},
        {"id": "r3", "recording_name": "Cancelled test", "status": "cancelled",
         "cancel_reason": "Batch cancelled before this recording started."},
        {"id": "r4", "recording_name": "Still queued", "status": "queued"},
    ]

    def test_junit_xml_is_escaped_and_counts_every_status(self):
        document = ci_results.junit_xml(self.RUNS, suite_name="Suite & <Co>")
        self.assertTrue(document.startswith('<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertIn('tests="4"', document)
        self.assertIn('failures="1"', document)
        self.assertIn('skipped="2"', document)
        self.assertIn("Checkout &lt;pays&gt; &amp; confirms", document)
        self.assertIn("expected &apos;Order placed&apos;".replace("&apos;", "'"), document)
        self.assertNotIn("<script>", document)
        self.assertIn("<skipped", document)
        # A queued or cancelled run is never reported as a pass.
        self.assertEqual(document.count("<testcase "), 4)
        self.assertEqual(document.count("<failure "), 1)

    def test_summary_exit_codes_distinguish_failure_from_incomplete(self):
        self.assertEqual(ci_results.summary(self.RUNS)["exit_code"], 1)
        only_skip = [run for run in self.RUNS if run["status"] in ("cancelled", "queued")]
        self.assertEqual(ci_results.summary(only_skip)["verdict"], "incomplete")
        self.assertEqual(ci_results.summary(only_skip)["exit_code"], 2)
        passed = [run for run in self.RUNS if run["status"] == "passed"]
        self.assertEqual(ci_results.summary(passed)["exit_code"], 0)

    def test_junit_endpoints_serve_xml(self):
        client = TestClient(app)
        with patch("app.intel_routes.ci_results.load_runs", return_value=self.RUNS):
            run = client.get("/api/runs/abc/junit.xml")
            batch = client.get("/api/runs/batch/b1/junit.xml")
            results = client.get("/api/runs/abc/results.json")
        self.assertEqual(run.status_code, 200)
        self.assertIn("application/xml", run.headers["content-type"])
        self.assertIn("<testsuites", run.text)
        self.assertEqual(batch.status_code, 200)
        self.assertIn('classname="testforge.batch.b1"', batch.text)
        self.assertEqual(results.status_code, 200)
        self.assertEqual(results.json()["exit_code"], 1)
        with patch("app.intel_routes.ci_results.load_runs", side_effect=LookupError("Run not found")):
            self.assertEqual(client.get("/api/runs/missing/junit.xml").status_code, 404)


class McpMarketToolsTests(unittest.TestCase):
    def test_mcp_exposes_and_answers_the_new_tools(self):
        from app.qa_agents import mcp_server, registry

        tools = {tool["name"]: tool for tool in mcp_server._tools(list(registry.BY_ID))}
        for name in ("market_position", "market_gaps", "resolve_step", "step_search", "impact_analysis"):
            self.assertIn(name, tools)
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        position = mcp_server._call_tool("market_position", {})
        self.assertFalse(position["isError"])
        payload = json.loads(position["content"][0]["text"])
        self.assertIn("audit_passed", payload)
        self.assertIn("open_gaps", payload)
        gaps = mcp_server._call_tool("market_gaps", {})
        self.assertFalse(gaps["isError"])
        self.assertIn("gaps", json.loads(gaps["content"][0]["text"]))
        resolved = mcp_server._call_tool("resolve_step", {"phrase": "click place order"})
        self.assertFalse(resolved["isError"])
        self.assertIn(json.loads(resolved["content"][0]["text"])["status"], ("bound", "ambiguous", "unbound"))
        searched = mcp_server._call_tool("step_search", {"query": "place order"})
        self.assertFalse(searched["isError"])
        impact = mcp_server._call_tool("impact_analysis",
                                       {"changed_files": ["app/static/checkout-components.js"]})
        self.assertFalse(impact["isError"])
        self.assertIn("selected", json.loads(impact["content"][0]["text"]))
        self.assertTrue(mcp_server._call_tool("resolve_step", {})["isError"])


class AuditCliTests(unittest.TestCase):
    def test_the_audit_gate_exit_codes(self):
        import contextlib
        import io
        from tools import market_audit

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            self.assertEqual(market_audit.main(["--structure-only"]), 0)
            self.assertEqual(market_audit.main(["--json"]), 0)
        self.assertIn("PASS", buffer.getvalue())
        with tempfile.TemporaryDirectory() as empty:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(market_audit.main(["--root", empty, "--json"]), 1)
            self.assertIn('"passed": false', buffer.getvalue().replace("False", "false").lower())


class UiWiringTests(unittest.TestCase):
    def test_the_dashboard_serves_and_wires_the_new_tabs(self):
        index = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        document = index.read_text(encoding="utf-8")
        self.assertIn('/market-components.js', document)
        self.assertIn('tab === "market" && e(MarketTab', document)
        self.assertIn('tab === "intel" && e(IntelTab', document)
        components = (index.parent / "market-components.js").read_text(encoding="utf-8")
        for token in ("/api/market/matrix", "/api/market/audit", "/api/steps/resolve",
                      "/api/impact/analyse", "/api/explorer/scan", "/api/market/report.html"):
            self.assertIn(token, components)
        client = TestClient(app)
        self.assertEqual(client.get("/market-components.js").status_code, 200)


class NoDowngradeTests(unittest.TestCase):
    """The enhancement must not have replaced anything that already existed."""

    def test_existing_surfaces_still_answer(self):
        client = TestClient(app)
        for url in ("/api/health", "/api/agents", "/api/library/fast", "/api/roadmap/support-matrix",
                    "/api/imports/formats", "/api/dev/kit", "/api/runs"):
            response = client.get(url)
            self.assertIn(response.status_code, (200, 204), f"{url} -> {response.status_code}")

    def test_the_nine_agents_and_the_old_mcp_tools_are_unchanged(self):
        from app.qa_agents import mcp_server, registry

        self.assertEqual(len(registry.AGENTS), 9)
        names = {tool["name"] for tool in mcp_server._tools(list(registry.BY_ID))}
        for name in ("qa_list_agents", "qa_get_sample", "qa_run_agent", "qa_run_from_source"):
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
