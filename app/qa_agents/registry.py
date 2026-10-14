"""The nine QA agents: catalogue, sample inputs and dispatch.

`COMPARED_WITH` records how each agent relates to what TestForge already did
before this hub existed, so the dashboard and docs can say plainly what is new,
what extends an existing feature and what the existing feature still does better.
"""
from __future__ import annotations

from . import ai, api_contract, case_design, code_review, defects, flaky, regression, release, requirements, triage
from .common import InputError, as_text

AGENTS = [
    {
        "id": "requirements", "name": "Requirement Review", "icon": "📋", "input": "text", "sources": [],
        "blurb": "Finds missing acceptance criteria, vague wording, unhandled edge cases and open questions.",
        "compared_with": "New. No requirement analysis existed; the AI onboarding converts scripts, it does not review requirements.",
    },
    {
        "id": "test-cases", "name": "Test Case Generation", "icon": "🧪", "input": "text", "sources": [],
        "blurb": "Creates positive, negative and boundary cases from acceptance criteria, with a Jira/Azure-ready CSV.",
        "compared_with": "New. Existing recordings capture what a tester does; nothing derived cases from requirements.",
    },
    {
        "id": "api-contract", "name": "API Contract", "icon": "🔌", "input": "spec", "sources": [],
        "blurb": "Analyses an OpenAPI or Swagger file for validation, security and error-contract gaps and writes contract tests.",
        "compared_with": "Extends the API checks in the recorder: those run against a live page; this reviews the spec before anything runs.",
    },
    {
        "id": "code-review", "name": "Automation Code Review", "icon": "🔍", "input": "code", "sources": [],
        "blurb": "Audits automation scripts for unstable locators, hard waits, hard-coded secrets and tests without assertions.",
        "compared_with": "Complements the static import validator, which checks that a script can be converted safely, not whether it is well written.",
    },
    {
        "id": "failure-triage", "name": "Failure Triage", "icon": "🩺", "input": "errors", "sources": ["run"],
        "blurb": "Classifies failures as Product, Automation, Data or Environment, with the signals behind each call.",
        "compared_with": "Extends the executor's six retry buckets into the four owner categories; the retry logic itself is unchanged.",
    },
    {
        "id": "flaky", "name": "Flaky Test Analysis", "icon": "🌀", "input": "runs", "sources": ["recording"],
        "blurb": "Reads a recording's run history for flip rate, timing, error clusters and time-of-day patterns, then ranks root causes.",
        "compared_with": "Extends the plan runner's 'passed on retry = flaky' status with timing and root-cause analysis. The existing gate is stricter and stays.",
    },
    {
        "id": "defect-drafting", "name": "Defect Drafting", "icon": "🐞", "input": "defect", "sources": ["run"],
        "blurb": "Converts a failed run, logs and evidence into a Jira-ready defect with Markdown and Jira wiki output.",
        "compared_with": "Extends the Jira CSV export (one row per step). This drafts the narrative and severity from the failure.",
    },
    {
        "id": "regression", "name": "Regression Selection", "icon": "🎯", "input": "changes", "sources": ["project"],
        "blurb": "Recommends which existing tests a code change should run, and creates a batch payload for them.",
        "compared_with": "New. Batch runs execute a chosen set; nothing chose the set from a change.",
    },
    {
        "id": "release-summary", "name": "Release Summary", "icon": "🚦", "input": "release", "sources": ["project"],
        "blurb": "Writes a concise stakeholder report with a go/no-go recommendation from the latest result of every test.",
        "compared_with": "Extends the per-run PDF and JSON reports to a release level. Per-run evidence stays in Runs and Artifacts.",
    },
]
BY_ID = {agent["id"]: agent for agent in AGENTS}

SAMPLES = {
    "requirements": {"title": "Checkout", "text": (
        "As a shopper I want to pay for my order quickly.\n"
        "- The checkout must be fast and user-friendly.\n"
        "- Card payment should be accepted for orders up to 5000 USD.\n"
        "- Discount codes may be applied as needed.\n"
        "- TBD: what happens when the card is declined?\n")},
    "test-cases": {"feature": "Registration", "text": (
        "Username must be between 3 and 20 characters.\n"
        "Email must be a valid e-mail address and is required.\n"
        "Password must be at least 8 characters.\n"
        "Only an administrator can delete a user account.\n")},
    "api-contract": {"title": "Orders", "spec": (
        '{"openapi": "3.0.3", "info": {"title": "Orders", "version": "1.0"},'
        ' "servers": [{"url": "http://api.example.test"}],'
        ' "paths": {"/orders/{id}": {"get": {"parameters": [{"name": "id", "in": "path", "required": true,'
        ' "schema": {"type": "string"}}], "responses": {"200": {"description": "ok", "content": {"application/json":'
        ' {"schema": {"type": "object", "properties": {"id": {"type": "string"}, "customerPassword": {"type": "string"}}}}}}}}},'
        ' "/users": {"get": {"responses": {"200": {"description": "ok"}}}}}}')},
    "code-review": {"filename": "test_login.py", "source": (
        "import time\n"
        "from playwright.sync_api import Page\n\n"
        "def test_login(page: Page):\n"
        "    page.goto('https://staging.example.test/login')\n"
        "    page.fill('//div[3]/form/input[2]', 'ada')\n"
        "    page.fill('input[name=password]', 'Sup3rSecret!')\n"
        "    page.click('.css-1x9f2ab')\n"
        "    time.sleep(5)\n\n"
        "def test_dashboard(page):\n"
        "    page.goto('/dashboard')\n")},
    "failure-triage": {"errors": [
        "TimeoutError: locator.click: Timeout 30000ms exceeded. waiting for locator('#submit')",
        "AssertionError: expected 'Order placed' but received 'Something went wrong' (HTTP 500 Internal Server Error)",
        "net::ERR_CONNECTION_REFUSED at https://staging.example.test/api/cart",
        "IntegrityError: duplicate key value violates unique constraint \"users_email_key\"",
    ], "log": ""},
    "flaky": {"name": "Login then logout", "runs": [
        {"status": "passed", "started_at": "2026-10-10T09:00:00Z", "duration_ms": 4200},
        {"status": "failed", "started_at": "2026-10-10T10:00:00Z", "duration_ms": 9800,
         "error": "TimeoutError: waiting for locator('#dashboard') exceeded 5000ms"},
        {"status": "passed", "started_at": "2026-10-10T11:00:00Z", "duration_ms": 4100},
        {"status": "failed", "started_at": "2026-10-11T10:00:00Z", "duration_ms": 10100,
         "error": "TimeoutError: waiting for locator('#dashboard') exceeded 5000ms"},
        {"status": "passed", "started_at": "2026-10-11T12:00:00Z", "duration_ms": 4000},
        {"status": "failed", "started_at": "2026-10-12T10:00:00Z", "duration_ms": 9900,
         "error": "TimeoutError: waiting for locator('#dashboard') exceeded 5000ms"},
    ]},
    "defect-drafting": {"title": "", "recording_name": "Checkout", "environment": {
        "browser": "Chromium 130", "os": "Windows 11", "app_version": "2.4.1", "url": "https://shop.example.test/checkout"},
        "steps": ["Add the blue jacket to the basket", "Open the basket", "Click PLACE ORDER"],
        "expected": "The order confirmation page is shown with an order number.",
        "actual": "The page shows 'Something went wrong' and no order is created.",
        "error": "AssertionError: expected 'Order placed' but received 'Something went wrong' (HTTP 500)",
        "logs": "POST /api/orders 500 Internal Server Error\nuser ada@example.test token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",
        "evidence": ["step-007.png"], "severity": ""},
    "regression": {"changed_files": ["app/static/checkout-components.js", "app/routes/cart.py"],
                   "diff": "", "max_tests": 10, "tests": [
        {"id": "r1", "name": "Checkout pays with card", "tags": ["smoke"], "start_url": "https://shop.example.test/checkout",
         "labels": ["Click PLACE ORDER", "Confirmation shows order number"]},
        {"id": "r2", "name": "Cart keeps quantity", "tags": [], "start_url": "https://shop.example.test/cart",
         "labels": ["Change quantity to 3", "Cart total updates"]},
        {"id": "r3", "name": "Login then logout", "tags": ["critical"], "start_url": "https://shop.example.test/login",
         "labels": ["Sign in", "Sign out"]},
        {"id": "r4", "name": "Search finds products", "tags": [], "start_url": "https://shop.example.test/search",
         "labels": ["Type jacket in search", "Results list appears"]},
    ]},
    "release-summary": {"release": "2.4.1", "window": "Oct 2026 sprint", "runs": [
        {"recording_name": "Login then logout", "status": "passed", "critical": True, "created_at": "2026-10-12T10:00:00Z"},
        {"recording_name": "Checkout pays with card", "status": "failed", "critical": True,
         "error": "AssertionError: expected 'Order placed' but received 'Something went wrong' (HTTP 500)",
         "created_at": "2026-10-12T11:00:00Z"},
        {"recording_name": "Cart keeps quantity", "status": "passed", "created_at": "2026-10-12T11:30:00Z"},
        {"recording_name": "Search finds products", "status": "passed", "created_at": "2026-10-12T12:00:00Z"},
    ], "known_defects": [{"title": "Discount code shows wrong total on mobile", "severity": "major"}],
        "open_risks": ["Payment provider sandbox has intermittent 503s"]},
}


def catalogue() -> dict:
    return {"agents": [dict(agent) for agent in AGENTS], "ai": ai.status(), "modes": ["auto", "rules"]}


def sample(agent_id: str) -> dict:
    if agent_id not in BY_ID:
        raise KeyError(agent_id)
    return SAMPLES.get(agent_id, {})


def _text(payload: dict, key: str, label: str) -> str:
    """Required pasted text: empty input is a 400, not an empty report."""
    text = as_text(payload.get(key), field=label)
    if not text.strip():
        raise InputError(f"Paste the {label} to review (the '{key}' field is empty).")
    return text


def _run(agent_id: str, payload: dict):
    """Return (result, text-to-review-for-AI)."""
    if agent_id == "requirements":
        text = _text(payload, "text", "requirements")
        return requirements.review(text, payload.get("title")), text
    if agent_id == "test-cases":
        text = _text(payload, "text", "requirements")
        return case_design.generate(text, payload.get("feature")), text
    if agent_id == "api-contract":
        spec = _text(payload, "spec", "API specification")
        return api_contract.review(spec, payload.get("title")), spec[:ai.MAX_INPUT_CHARS]
    if agent_id == "code-review":
        source = _text(payload, "source", "script")
        return code_review.review(source, payload.get("filename")), source
    if agent_id == "failure-triage":
        errors = payload.get("errors") or []
        if not isinstance(errors, list):
            raise InputError("errors must be a list of strings")
        result = triage.triage(errors=errors, log=payload.get("log"), context=payload.get("context"))
        return result, "\n".join(str(e) for e in errors)[:ai.MAX_INPUT_CHARS]
    if agent_id == "flaky":
        runs = payload.get("runs") or []
        if not isinstance(runs, list):
            raise InputError("runs must be a list")
        result = flaky.analyse(runs, payload.get("name"))
        return result, str(result.get("artifacts", {}).get("stats", {}))
    if agent_id == "defect-drafting":
        result = defects.draft(payload)
        return result, str(result["artifacts"]["draft"].get("markdown", ""))
    if agent_id == "regression":
        tests = payload.get("tests") or []
        if not isinstance(tests, list):
            raise InputError("tests must be a list")
        diff = as_text(payload.get("diff"), field="diff")  # optional: changed_files alone is enough
        files = payload.get("changed_files") or []
        if not isinstance(files, list):
            raise InputError("changed_files must be a list of paths")
        try:
            max_tests = int(payload.get("max_tests") or 25)
        except (TypeError, ValueError):
            raise InputError("max_tests must be a number") from None
        result = regression.select(tests, diff=diff, changed_files=files, max_tests=max_tests)
        return result, "Changed: " + ", ".join(str(f) for f in files[:50]) + "\n" + diff[:4000]
    if agent_id == "release-summary":
        runs = payload.get("runs") or []
        if not isinstance(runs, list):
            raise InputError("runs must be a list")
        result = release.report(runs, release=payload.get("release", ""), known_defects=payload.get("known_defects"),
                                open_risks=payload.get("open_risks"), window=payload.get("window", ""),
                                critical_names=payload.get("critical_names"))
        return result, result["artifacts"]["markdown"]
    raise KeyError(agent_id)


def run(agent_id: str, payload: dict | None, mode: str = "auto") -> dict:
    if agent_id not in BY_ID:
        raise KeyError(agent_id)
    if mode not in ("auto", "rules"):
        raise InputError("mode must be 'auto' or 'rules'")
    if not isinstance(payload, dict):
        raise InputError("The request body must be a JSON object")
    result, text = _run(agent_id, payload)
    result["agent_name"] = BY_ID[agent_id]["name"]
    return ai.enrich(result, text, mode)
