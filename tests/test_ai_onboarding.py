"""AI smart onboarding: validate → AI convert → per-step fix loop regressions.

The Anthropic HTTP layer is faked; everything else (static first pass, prompt
contracts, per-step re-validation with normalize_step, budgets, fallbacks and
the preview endpoints) runs for real.
"""
import base64
import json
import os
import unittest
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()

from fastapi.testclient import TestClient

from app import ai_onboarding
from app.main import app
from app.script_import import parse_bundle, parse_file

# The exact upload that failed before: a TestForge replay runner with runtime
# imports, dict comprehensions, branches and an async-with — all unsupported by
# the static Python subset, and carrying no literal browser steps at all.
RUNNER_PY = '''"""Sequential TestForge replay. pip install playwright; playwright install chromium.

Run from this resource directory. Supply variables as TF_VAR_<name>.

"""

import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright
from testforge_runtime import StepRuntime


async def main():
    here = Path(__file__).resolve().parent
    steps = json.loads((here / "steps.json").read_text())
    variables = {k[7:]: v for k, v in os.environ.items() if k.startswith("TF_VAR_")}
    if (here / 'execution-plan.json').exists():
        from testforge_plan import run_plan
        result = await run_plan(plan, variables, here / 'run-artifacts')
        if result['status'] != 'passed':
            raise RuntimeError('Execution plan failed')
        return
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        runtime = StepRuntime(page, here / "run-artifacts")
        try:
            for step in steps:
                repeat = step.get("repeat_count", step.get("repeat", 1)) or 1
                if not isinstance(repeat, int) or not 1 <= repeat <= 100:
                    raise ValueError("Invalid repeat count")
                for _ in range(repeat):
                    await runtime.perform(step, variables)
        finally:
            await runtime.close()
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
'''

# A script the static subset rejects (imports, loop, helper) that DOES contain
# real browser steps for the AI to extract.
LOOPY_PY = '''import os
from playwright.sync_api import expect

BASE = os.environ["BASE_URL"]
ROWS = ["Ada", "Bob"]

def login(page, user):
    page.goto(BASE + "/login")
    page.get_by_label("Name").fill(user)
    page.get_by_role("button", name="Continue").click()

def test_greeting(page):
    for user in ROWS:
        login(page, user)
        expect(page.locator("#out")).to_have_text("Hello, " + user)
'''

GOOD_STEPS = {
    "name": "Greeting flow",
    "start_url": "{{base_url}}/login",
    "steps": [
        {"action": "navigate", "selector": {}, "value": "{{base_url}}/login", "label": "Open login"},
        {"action": "fill", "selector": {"kind": "label", "primary": "Name"}, "value": "{{username}}", "label": "Enter name"},
        {"action": "click", "selector": {"kind": "role", "primary": "button", "name": "Continue"}, "value": "", "label": "Continue"},
        {"action": "assert_text", "selector": {"kind": "css", "primary": "#out"}, "value": "Hello, {{username}}", "label": "Verify greeting"},
    ],
    "notes": ["Loop over ROWS converted to {{username}}; set it per run."],
}

RUNNER_NOTE = {
    "name": "TestForge replay runner",
    "start_url": "",
    "steps": [],
    "notes": ["This script replays steps.json; upload steps.json together with the script as a bundle."],
}


def api_reply(payload):
    """Wrap a fake model answer in the Anthropic HTTP response shape."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class FakeAI:
    """Scripted model: pops one answer per call and records every prompt."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, url_payload, key):
        self.prompts.append(json.dumps(url_payload))
        if not self.answers:
            raise AssertionError("fake AI received more calls than scripted")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def with_key(fake, system_hint=None):
    """Patch the transport and the key; returns the context manager."""
    return patch.object(ai_onboarding, "_post", fake)


class AiOnboardingTests(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_static_first_pass_still_rejects_the_runner(self):
        result = parse_file("playwright_test (new).py", RUNNER_PY.encode())
        self.assertFalse(result["can_save"])
        self.assertEqual(len([d for d in result["diagnostics"] if d["severity"] == "error"]), 11)

    def test_offboarding_without_key_reports_unavailable(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        result = ai_onboarding.smart_parse_file("playwright_test (new).py", RUNNER_PY.encode())
        self.assertFalse(result["can_save"])
        self.assertFalse(result["ai"]["used"])
        self.assertFalse(result["ai"]["available"])
        self.assertIn("ANTHROPIC_API_KEY", result["ai"]["reason"])

    def test_smart_onboarding_converts_and_revalidates_every_step(self):
        fake = FakeAI([api_reply(GOOD_STEPS)])
        with with_key(fake):
            result = ai_onboarding.smart_parse_file("loopy_test.py", LOOPY_PY.encode())
        self.assertTrue(result["can_save"], result)
        self.assertEqual(len(result["steps"]), 4)
        self.assertEqual([s["action"] for s in result["steps"]],
                         ["navigate", "fill", "click", "assert_text"])
        self.assertTrue(result["ai"]["used"])
        self.assertEqual(result["ai"]["converted_steps"], 4)
        # The static errors became explicit notes; nothing is silently hidden.
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        notes = [d for d in result["diagnostics"] if d["severity"] == "note"]
        self.assertEqual(errors, [])
        self.assertTrue(any("handled by conversion" in d["message"] for d in notes))
        self.assertTrue(any("never executed" in d["message"] for d in notes))
        self.assertFalse(result["source_executed"])
        # The prompt carried the source and the static diagnostics.
        sent = fake.prompts[0]
        self.assertIn("loopy_test.py", sent)
        self.assertIn("Unsupported Python syntax", sent)

    def test_per_step_fix_loop_repairs_invalid_steps(self):
        bad_then_good = {
            "name": "Greeting flow", "start_url": "{{base_url}}/login",
            "steps": [
                {"action": "navigate", "selector": {}, "value": "{{base_url}}/login"},
                # Invalid: unknown action and a non-URL value; the fix call repairs it.
                {"action": "fill_form", "selector": {"kind": "label", "primary": "Name"}, "value": "{{username}}"},
                {"action": "click", "selector": {"kind": "role", "primary": "button", "name": "Continue"}},
                {"action": "assert_text", "selector": {"kind": "css", "primary": "#out"}, "value": "Hello"},
            ],
        }
        fake = FakeAI([
            api_reply(bad_then_good),
            api_reply({"action": "fill", "selector": {"kind": "label", "primary": "Name"},
                       "value": "{{username}}", "label": "Enter name"}),
        ])
        with with_key(fake):
            result = ai_onboarding.smart_parse_file("loopy_test.py", LOOPY_PY.encode())
        self.assertTrue(result["can_save"], result)
        self.assertEqual(len(result["steps"]), 4)
        self.assertEqual(result["ai"]["step_fixes"], 1)
        repaired = [t for t in result["ai"]["trace"] if "repair" in str(t.get("result", ""))]
        self.assertTrue(repaired)
        # The fix prompt contained the validator error and the bad step.
        fix_prompt = fake.prompts[1]
        self.assertIn("fill_form", fix_prompt)
        self.assertIn("Unsupported action", fix_prompt)

    def test_runner_gets_honest_bundle_note_when_ai_reports_no_steps(self):
        fake = FakeAI([api_reply(RUNNER_NOTE)])
        with with_key(fake):
            result = ai_onboarding.smart_parse_file("playwright_test (new).py", RUNNER_PY.encode())
        self.assertFalse(result["can_save"])          # nothing to import: stays blocked
        self.assertEqual(result["steps"], [])
        notes = [d["message"] for d in result["diagnostics"] if d["severity"] == "note"]
        self.assertTrue(any("steps.json" in note for note in notes))

    def test_garbage_ai_falls_back_to_static_errors(self):
        fake = FakeAI([api_reply({"nonsense": True}), api_reply({"still": "nothing"})])
        with with_key(fake):
            result = ai_onboarding.smart_parse_file("loopy_test.py", LOOPY_PY.encode())
        self.assertFalse(result["can_save"])
        self.assertEqual(result["steps"], [])
        self.assertTrue(any(d["severity"] == "error" for d in result["diagnostics"]))
        self.assertEqual(result["ai"]["converted_steps"], 0)

    def test_transport_error_falls_back_to_static_result(self):
        fake = FakeAI([RuntimeError("AI service unavailable: timeout")])
        with with_key(fake):
            result = ai_onboarding.smart_parse_file("loopy_test.py", LOOPY_PY.encode())
        self.assertFalse(result["can_save"])
        self.assertTrue(any(d["severity"] == "error" for d in result["diagnostics"]))
        self.assertIn("failed", json.dumps(result["ai"]["trace"]))

    def test_binary_content_is_refused_politely(self):
        fake = FakeAI([])
        with with_key(fake):
            result = ai_onboarding.smart_parse_file("blob.bin", bytes(range(256)) * 64)
        self.assertFalse(result["can_save"])
        self.assertFalse(result["ai"]["used"])
        self.assertIn("binary", result["ai"]["reason"])

    def test_bundle_script_with_referenced_steps_json(self):
        steps_json = json.dumps({"name": "Runner steps", "steps": [
            {"action": "navigate", "value": "http://example.test"},
            {"action": "fill", "selector": "#name", "value": "Ada"},
            {"action": "click", "selector": "#go"},
        ]}).encode()
        result = parse_bundle([
            ("playwright_test.py", RUNNER_PY.encode()),
            ("steps.json", steps_json),
        ])
        self.assertTrue(result["can_save"], result)
        self.assertEqual(len(result["steps"]), 3)
        self.assertEqual(result["wrapper"], "playwright_test.py")
        self.assertIn("steps.json", result["notice"])

    def test_bundle_unreferenced_data_file_is_refused(self):
        steps_json = json.dumps({"steps": []}).encode()
        with self.assertRaises(Exception):
            parse_bundle([("other.py", LOOPY_PY.encode()), ("steps.json", steps_json)])


class SmartImportEndpointTests(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_preview_endpoint_runs_smart_onboarding(self):
        fake = FakeAI([api_reply(GOOD_STEPS)])
        with with_key(fake), TestClient(app) as client:
            response = client.post(
                "/api/imports/preview?filename=loopy_test.py&smart=true",
                content=LOOPY_PY.encode(),
                headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["can_save"], result)
        self.assertTrue(result["ai"]["used"])

    def test_preview_endpoint_smart_false_keeps_static_behavior(self):
        fake = FakeAI([])
        with with_key(fake), TestClient(app) as client:
            response = client.post(
                "/api/imports/preview?filename=loopy_test.py&smart=false",
                content=LOOPY_PY.encode(),
                headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(result["can_save"])
        self.assertTrue(any(d["severity"] == "error" for d in result["diagnostics"]))
        self.assertEqual(fake.prompts, [])          # the AI was never called

    def test_formats_endpoint_reports_ai_availability(self):
        with TestClient(app) as client:
            data = client.get("/api/imports/formats").json()
        self.assertIn("ai_onboarding", data)
        self.assertTrue(data["ai_onboarding"]["available"])
        self.assertEqual(data["ai_onboarding"]["model"], ai_onboarding.MODEL)


if __name__ == "__main__":
    unittest.main()
