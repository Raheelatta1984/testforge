"""Paste/type-a-script import: the upload pipeline without the file.

The endpoint must behave exactly like POST /api/imports/preview for the same
script -- static validation first, AI smart onboarding on unsupported syntax,
never executing the source -- with detection that also understands bare
Playwright snippets (no imports, no test wrapper) that a file upload would
have identified by its extension.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()

from fastapi.testclient import TestClient

from app import ai_onboarding
from app.main import app
from app.script_import import MAX_BYTES, detect_pasted_filename

PYTHON = """from playwright.sync_api import expect

def test_greeting(page):
    page.goto("http://example.test")
    page.get_by_label("Name").fill("Ada")
    page.get_by_role("button", name="Continue").click()
    expect(page.locator("#out")).to_have_text("Hello, Ada")
"""
BARE_PYTHON = (
    'page.goto("http://example.test")\n'
    'page.get_by_label("Name").fill("Ada")\n'
    'page.get_by_role("button", name="Continue").click()\n'
)
TYPESCRIPT = """import { test, expect } from '@playwright/test';
test('greeting', async ({ page }) => {
  await page.goto('http://example.test');
  await page.getByLabel('Name').fill('Ada');
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.locator('#out')).toHaveText('Hello, Ada');
});
"""
LOOPY_PY = '''import os
from playwright.sync_api import expect

BASE = os.environ["BASE_URL"]

def test_greeting(page):
    for user in ["Ada"]:
        page.goto(BASE + "/login")
        expect(page.locator("#out")).to_have_text("Hello, " + user)
'''
GOOD_STEPS = {
    "name": "Greeting flow",
    "start_url": "http://example.test/login",
    "steps": [
        {"action": "navigate", "selector": {}, "value": "http://example.test/login", "label": "Open login"},
        {"action": "assert_text", "selector": {"kind": "css", "primary": "#out"},
         "value": "Hello, Ada", "label": "Verify greeting"},
    ],
    "notes": [],
}


def api_reply(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class FakeAI:
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


class PasteDetectionTests(unittest.TestCase):
    def test_detects_playwright_python_full_and_bare(self):
        self.assertEqual(detect_pasted_filename(PYTHON), "pasted.py")
        self.assertEqual(detect_pasted_filename(BARE_PYTHON), "pasted.py")

    def test_detects_playwright_and_cypress_javascript(self):
        self.assertEqual(detect_pasted_filename(TYPESCRIPT), "pasted.js")
        self.assertEqual(detect_pasted_filename("cy.visit('http://example.test');\ncy.get('#go').click();"), "pasted.js")
        self.assertEqual(detect_pasted_filename("await page.goto('http://example.test');"), "pasted.js")

    def test_detects_other_pasted_formats(self):
        self.assertEqual(detect_pasted_filename('{"steps": []}'), "pasted.json")
        self.assertEqual(detect_pasted_filename("action,selector,value\nclick,#go,"), "pasted.csv")
        self.assertEqual(detect_pasted_filename("Feature: F\nScenario: S\n Given I open \"http://example.test\""), "pasted.feature")
        self.assertEqual(detect_pasted_filename("*** Test Cases ***\nLogin\n    Open Browser    http://example.test"), "pasted.robot")
        self.assertEqual(detect_pasted_filename("steps:\n  - action: click\n    selector: '#go'"), "pasted.yaml")
        self.assertEqual(detect_pasted_filename("1. Open http://example.test\n2. Click #go"), "pasted.txt")

    def test_explicit_format_and_filename_win(self):
        self.assertEqual(detect_pasted_filename("anything", format_hint="typescript"), "pasted.ts")
        self.assertEqual(detect_pasted_filename("anything", filename="login.spec.ts"), "login.spec.ts")
        self.assertEqual(detect_pasted_filename(BARE_PYTHON, filename="login_flow"), "login_flow.py")
        self.assertEqual(detect_pasted_filename("x", filename="../../etc/passwd.py"), "passwd.py")
        with self.assertRaises(ValueError):
            detect_pasted_filename("x", format_hint="cobol")


class PastePreviewEndpointTests(unittest.TestCase):
    def post(self, client, payload, **kwargs):
        return client.post("/api/imports/preview-text", json=payload, **kwargs)

    def test_paste_playwright_python_processes_like_an_upload(self):
        with TestClient(app) as client:
            response = self.post(client, {"content": PYTHON})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["can_save"], result)
        self.assertEqual([s["action"] for s in result["steps"]],
                         ["navigate", "fill", "click", "assert_text"])
        self.assertEqual(result["input_method"], "paste")
        self.assertEqual(result["paste"]["filename"], "pasted.py")
        self.assertEqual(result["name"], "Greeting")
        self.assertFalse(result["source_executed"])
        self.assertEqual(result["execution"], "sequential")

    def test_paste_bare_snippet_and_typescript(self):
        with TestClient(app) as client:
            bare = self.post(client, {"content": BARE_PYTHON}).json()
            typed = self.post(client, {"content": TYPESCRIPT, "format": "typescript"}).json()
            auto_js = self.post(client, {"content": TYPESCRIPT}).json()
        self.assertTrue(bare["can_save"], bare)
        self.assertEqual(bare["paste"]["filename"], "pasted.py")
        self.assertTrue(typed["can_save"], typed)
        self.assertEqual(typed["paste"]["filename"], "pasted.ts")
        self.assertEqual(typed["name"], "greeting")
        self.assertTrue(auto_js["can_save"], auto_js)

    def test_paste_json_csv_and_gherkin(self):
        cases = [
            ({"content": '{"name":"My steps","steps":[{"action":"navigate","value":"http://example.test"}]}'},
             "pasted.json", "My steps"),
            ({"content": "action,selector,value\nnavigate,,http://example.test\nclick,#go,\n"},
             "pasted.csv", None),
            ({"content": 'Feature: Greeting\nScenario: Say hello\n Given I open "http://example.test"\n'
                          ' When I click "#go"\n Then "#out" should equal "Hello"\n'},
             "pasted.feature", "Say hello"),
        ]
        with TestClient(app) as client:
            for payload, filename, name in cases:
                with self.subTest(filename=filename):
                    result = self.post(client, payload).json()
                    self.assertTrue(result["can_save"], result)
                    self.assertEqual(result["paste"]["filename"], filename)
                    if name:
                        self.assertEqual(result["name"], name)

    def test_paste_rejects_empty_oversize_and_unknown_format(self):
        with TestClient(app) as client:
            self.assertEqual(self.post(client, {"content": "   "}).status_code, 422)
            self.assertEqual(self.post(client, {"script": ""}).status_code, 422)
            self.assertEqual(self.post(client, {"content": "x" * (MAX_BYTES + 1)}).status_code, 413)
            self.assertEqual(self.post(client, {"content": "x", "format": "cobol"}).status_code, 422)
            self.assertEqual(self.post(client, {"content": 42}).status_code, 422)

    def test_pasted_source_is_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "owned"
            code = f'import pathlib\npathlib.Path({str(sentinel)!r}).write_text("owned")\npage.click("#x")\n'
            with TestClient(app) as client:
                result = self.post(client, {"content": code}).json()
            self.assertFalse(result["can_save"], result)
            self.assertFalse(sentinel.exists())

    def test_formats_endpoint_advertises_paste(self):
        with TestClient(app) as client:
            data = client.get("/api/imports/formats").json()
        self.assertTrue(data["paste"]["supported"])
        self.assertEqual(data["paste"]["endpoint"], "/api/imports/preview-text")
        self.assertIn("python", [row["id"] for row in data["paste"]["formats"]])


class PasteAiOnboardingTests(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_paste_runs_ai_smart_onboarding_when_static_fails(self):
        fake = FakeAI([api_reply(GOOD_STEPS)])
        with patch.object(ai_onboarding, "_post", fake), TestClient(app) as client:
            response = client.post("/api/imports/preview-text", json={"content": LOOPY_PY})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["can_save"], result)
        self.assertTrue(result["ai"]["used"])
        self.assertEqual(result["input_method"], "paste")
        self.assertIn("Unsupported Python syntax", fake.prompts[0])

    def test_paste_smart_false_keeps_static_behaviour(self):
        fake = FakeAI([])
        with patch.object(ai_onboarding, "_post", fake), TestClient(app) as client:
            response = client.post("/api/imports/preview-text?smart=false", json={"content": LOOPY_PY})
        result = response.json()
        self.assertFalse(result["can_save"])
        self.assertTrue(any(d["severity"] == "error" for d in result["diagnostics"]))
        self.assertEqual(fake.prompts, [])


class PasteDashboardTests(unittest.TestCase):
    """Static dashboard contract: the area exists and feeds the shared flow."""

    def test_dashboard_has_paste_area_wired_to_preview_text(self):
        source = (Path(__file__).resolve().parents[1]
                  / "app/static/import-components.js").read_text()
        for needle in ["PASTE / TYPE SCRIPT", "Paste or type a test script",
                       "Pasted script format", "PROCESS PASTED SCRIPT",
                       "/api/imports/preview-text", "PYTHON EXAMPLE",
                       "input_method==='paste'"]:
            self.assertIn(needle, source)
        # The upload path the browser test drives is still there, unchanged.
        self.assertIn("Upload test scripts", source)
        self.assertIn("VALIDATE & PREVIEW", source)
        # Both input modes share the one downstream flow.
        self.assertIn("TEST BEFORE PUBLISHING", source)
        self.assertIn("SAVE TO LIBRARY & PUBLISH", source)


if __name__ == "__main__":
    unittest.main()
