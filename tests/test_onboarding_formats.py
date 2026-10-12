"""Onboarding any format without errors: recorded labels, exports and templates.

The regression this file exists for: a team exports a recording from TestForge's own
Library (azure-devops.xlsx, jira.csv, testcomplete.csv) or brings a step table from
Azure DevOps / TestRail, uploads it, and every row came back as
"Unsupported action: ...". The tables below mirror that input exactly, including the
Harris Farm staging labels that were reported, and assert that onboarding now reports
zero errors -- and that genuinely unsupported input still does.
"""
import asyncio
import io
import json
import unittest

from tests.bootstrap import ensure_test_env
ensure_test_env()
from fastapi.testclient import TestClient
from app.main import app
from app import import_templates
from app.export_formats import write_exports
from app.script_import import parse_bundle, parse_file
from app.step_runtime import StepRuntime, normalize_step, normalize_steps
from tests.fakes import FakePage

# The reported upload, row for row: TestForge's own azure-devops.xlsx export shape,
# where the Action column holds the recorder's label rather than a canonical verb.
REPORTED_LABELS = [
    "Open https://staging.harrisfarm.com.au/password",
    "Click enter using password",
    "Click your password",
    "Type: {{hfm_password}}",
    "Click enter",
    "Click 528,562",
    "Click search for products e.g. bread log in / sign up cart $0.00 0",
    "Type: granny apple",
    "Click search",
    "Type: apple",
    "Press Enter",
    "Press Tab",
    "Click 876,562",
    "Click add to cart",
]


def workbook(rows):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def reported_export():
    rows = [["Test Case", "Step", "Action", "Input", "Selector", "Screenshot"]]
    for index, label in enumerate(REPORTED_LABELS, 1):
        rows.append(["Harris Farm login", index, label, "", "{}", ""])
    return workbook(rows)


def errors(result):
    return [item for item in result["diagnostics"] if item["severity"] == "error"]


class RecordedLabelTests(unittest.TestCase):
    def test_UT_ONB_01_recorded_label_export_onboards_without_errors(self):
        result = parse_file("azure-devops.xlsx", reported_export())
        self.assertEqual(errors(result), [])
        self.assertTrue(result["can_save"], result["diagnostics"])
        self.assertEqual(len(result["steps"]), len(REPORTED_LABELS))
        actions = [step["action"] for step in result["steps"]]
        self.assertEqual(actions[0], "navigate")
        self.assertEqual(actions[-1], "click")
        # The password placeholder survives; it is never treated as a literal.
        typed = [step["value"] for step in result["steps"] if step["action"] == "type"]
        self.assertIn("{{hfm_password}}", typed)
        self.assertIn("granny apple", typed)
        # A coordinate-only label becomes a click on the recorded point.
        coordinate = [step for step in result["steps"] if (step["selector"] or {}).get("x") == 528]
        self.assertEqual(len(coordinate), 1)
        self.assertEqual(coordinate[0]["selector"]["y"], 562)
        # A value with no field named binds to the element the flow clicked last.
        password_type = next(step for step in result["steps"] if step["value"] == "{{hfm_password}}")
        self.assertTrue(password_type["selector"].get("primary"))
        self.assertTrue(any(d["severity"] == "note" for d in result["diagnostics"]))
        # A label that quotes a whole page region keeps only its leading phrase.
        noisy = next(step for step in result["steps"]
                     if "search for products" in str((step["selector"] or {}).get("primary")))
        self.assertLess(len(noisy["selector"]["primary"]), 60)

    def test_UT_ONB_02_own_export_tables_round_trip(self):
        steps = normalize_steps([
            {"action": "navigate", "value": "https://staging.example.test/password",
             "label": "Open https://staging.example.test/password"},
            {"action": "click", "selector": {"kind": "text", "primary": "enter using password"},
             "label": "Click enter using password"},
            {"action": "click", "selector": {"primary": "#Password", "x": 528, "y": 562},
             "label": "Click your password"},
            {"action": "type", "value": "{{hfm_password}}", "selector": {"primary": "#Password"},
             "label": "Type: {{hfm_password}}"},
            {"action": "press", "value": "Enter", "label": "Press Enter"},
            {"action": "click", "selector": {"x": 876, "y": 562}, "label": "Click 876,562"},
            {"action": "click", "selector": {"primary": "#add"}, "label": "Click add to cart",
             "repeat_count": 3},
        ])
        import tempfile
        from pathlib import Path

        def signature(step):
            selector = step["selector"]
            return (step["action"], step["value"], selector.get("primary"), selector.get("x"),
                    selector.get("y"), selector.get("name"), step["repeat_count"])

        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            write_exports(folder, {"id": "r1", "project_id": "p1", "name": "Login smoke", "steps": steps})
            for name in ("azure-devops.xlsx", "azure-devops.csv", "jira.csv", "jira.xlsx",
                         "testcomplete.csv", "testcomplete.xlsx", "steps.json"):
                result = parse_file(name, (folder / name).read_bytes())
                self.assertEqual(errors(result), [], name)
                self.assertEqual([signature(step) for step in result["steps"]],
                                 [signature(step) for step in steps], name)
            # An older Jira export has no Selector column: the locator is rebuilt
            # from the description and the repeat marker still travels in it.
            import csv as _csv
            with (folder / "jira.csv").open(encoding="utf-8-sig", newline="") as handle:
                table = list(_csv.reader(handle))
            keep = [index for index, name in enumerate(table[0]) if name != "Selector"]
            buffer = io.StringIO()
            writer = _csv.writer(buffer)
            for row in table:
                writer.writerow([row[index] for index in keep])
            result = parse_file("jira-legacy.csv", buffer.getvalue().encode("utf-8"))
            self.assertEqual(errors(result), [], result["diagnostics"])
            self.assertEqual(len(result["steps"]), len(steps))
            self.assertEqual(result["steps"][-1]["repeat_count"], 3)

    def test_UT_ONB_03_sentence_and_multiword_values(self):
        table = workbook([
            ["#", "Action", "Expected Result"],
            [1, "Open https://example.test/login", "Sign-in form is displayed"],
            [2, "Enter Ada Lovelace in the Username field", ""],
            [3, "Click the Sign in button", "Dashboard loads"],
        ])
        result = parse_file("sentences.xlsx", table)
        self.assertEqual(errors(result), [])
        # A multi-word value is not truncated at the first word.
        filled = next(step for step in result["steps"] if step["action"] == "fill")
        self.assertEqual(filled["value"], "Ada Lovelace")
        # Each row keeps its own expectation, in sheet order.
        self.assertEqual([step["action"] for step in result["steps"]],
                         ["navigate", "assert_contains", "fill", "click", "assert_contains"])

    def test_UT_ONB_04_unsupported_source_still_fails_closed(self):
        for name, data in (
            ("unknown.csv", b"action,selector,value\nfrobnicate,#x,1\n"),
            ("unknown.xlsx", workbook([["action", "selector", "value"], ["frobnicate", "#x", "1"]])),
            ("unknown.txt", b"1. Frobnicate the widget\n"),
        ):
            result = parse_file(name, data)
            self.assertFalse(result["can_save"], name)
            self.assertTrue(errors(result), name)
        # A fixed wait stays refused, exactly as before this change.
        wait = parse_file("wait.txt", b"1. Open https://example.test\n2. Wait 5 seconds\n")
        self.assertFalse(wait["can_save"])
        self.assertTrue(any("Fixed waits" in item["message"] for item in errors(wait)))


class CoordinateStepTests(unittest.TestCase):
    """A recorded point is a real target: it must normalise, save and replay."""

    def test_UT_ONB_05_coordinate_and_focus_steps_normalise(self):
        step = normalize_step({"action": "click", "selector": {"x": 528, "y": 562}, "label": "Click 528,562"})
        self.assertEqual((step["selector"]["x"], step["selector"]["y"]), (528, 562))
        self.assertNotIn("primary", step["selector"])
        typed = normalize_step({"action": "type", "value": "Ada", "selector": {"primary": None}})
        self.assertEqual(typed["value"], "Ada")
        with self.assertRaises(ValueError):
            normalize_step({"action": "assert_visible", "selector": {"x": 1, "y": 2}})
        with self.assertRaises(ValueError):
            normalize_step({"action": "click", "selector": {"x": "left", "y": 2}})
        with self.assertRaises(ValueError):
            normalize_step({"action": "click", "selector": {}})

    def test_UT_ONB_06_coordinate_click_replays_on_the_point(self):
        page = FakePage()
        runtime = StepRuntime(page)
        asyncio.run(runtime.perform(normalize_step({"action": "click", "selector": {"x": 80, "y": 140}}), {}))
        self.assertEqual(page.clicks, [("mouse", 80.0, 140.0)])
        asyncio.run(runtime.perform(normalize_step({"action": "type", "value": "Ada", "selector": {}}), {}))
        self.assertEqual(page.typed, ["Ada"])


class TemplatePackTests(unittest.TestCase):
    def test_UT_ONB_07_every_template_imports_without_errors(self):
        self.assertGreaterEqual(len(import_templates.CATALOG), 12)
        for entry in import_templates.CATALOG:
            files = import_templates.build(entry["id"])
            self.assertTrue(files, entry["id"])
            payload = list(files.items())
            if len(payload) > 1:
                result = parse_bundle([(name, data) for name, (data, _media) in payload])
            else:
                name, (data, _media) = payload[0]
                result = parse_file(name, data)
            self.assertEqual(errors(result), [], f"{entry['id']}: {result['diagnostics']}")
            self.assertTrue(result["can_save"], entry["id"])
            self.assertGreater(len(result["steps"]), 0, entry["id"])
            # A template is an example, never a live target.
            self.assertNotIn("harrisfarm", json.dumps(result["steps"]).lower())

    def test_UT_ONB_08_template_endpoints(self):
        with TestClient(app) as client:
            listing = client.get("/api/imports/templates")
            self.assertEqual(listing.status_code, 200, listing.text)
            payload = listing.json()
            self.assertTrue(payload["templates"])
            for entry in payload["templates"]:
                self.assertEqual(sorted(entry["content"]), sorted(entry["files"]))
            single = client.get("/api/imports/templates/canonical-csv")
            self.assertEqual(single.status_code, 200)
            self.assertIn("text/csv", single.headers["content-type"])
            spreadsheet = client.get("/api/imports/templates/azure-devops-xlsx")
            self.assertEqual(spreadsheet.status_code, 200)
            self.assertIn("spreadsheetml", spreadsheet.headers["content-type"])
            bundle = client.get("/api/imports/templates/jenkins")
            self.assertEqual(bundle.status_code, 200)
            self.assertIn("zip", bundle.headers["content-type"])
            self.assertEqual(client.get("/api/imports/templates/not-a-template").status_code, 404)
            pack = client.get("/api/imports/templates.zip")
            self.assertEqual(pack.status_code, 200)
            import zipfile
            with zipfile.ZipFile(io.BytesIO(pack.content)) as archive:
                names = archive.namelist()
            self.assertIn("README.md", names)
            self.assertIn("canonical-csv/simple-steps.csv", names)
            self.assertIn("jenkins/Jenkinsfile", names)
            # The listing's own bytes go through the real preview endpoint unchanged.
            entry = next(item for item in payload["templates"] if item["id"] == "azure-devops-xlsx")
            import base64
            preview = client.post("/api/imports/preview?filename=azure-devops-steps.xlsx",
                                  content=base64.b64decode(entry["content"]["azure-devops-steps.xlsx"]))
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertTrue(preview.json()["can_save"], preview.json()["diagnostics"])
            self.assertEqual(errors(preview.json()), [])

    def test_UT_ONB_09_reported_upload_through_the_api(self):
        """The exact reported file, uploaded the way the dashboard uploads it."""
        with TestClient(app) as client:
            response = client.post("/api/imports/preview?filename=azure-devops.xlsx", content=reported_export())
            self.assertEqual(response.status_code, 200, response.text)
            preview = response.json()
            self.assertEqual(errors(preview), [], preview["diagnostics"])
            self.assertTrue(preview["can_save"])
            self.assertEqual(len(preview["steps"]), len(REPORTED_LABELS))
            # The input review still offers the literal values as variables.
            plan = client.post("/api/imports/variable-plan",
                               json={"project_id": "", "steps": preview["steps"]})
            self.assertEqual(plan.status_code, 200, plan.text)
            self.assertTrue(plan.json()["candidates"])


class LabelGrammarUnitTests(unittest.TestCase):
    def test_UT_ONB_10_recorder_labels_map_to_canonical_steps(self):
        cases = {
            "Click add to cart": ("click", ""),
            "Click 528,562": ("click", ""),
            "Type: {{password}}": ("type", "{{password}}"),
            "Press Enter": ("press", "Enter"),
            "Open https://example.test/login": ("navigate", "https://example.test/login"),
            "Right click the row": ("right_click", ""),
            "Mouse over the summary": ("hover", ""),
            "Verify: the page contains Welcome": ("assert_contains", "Welcome"),
        }
        for label, (action, value) in cases.items():
            result = parse_file("one.xlsx", workbook([["Action", "Selector", "Input"], [label, "", ""]]))
            self.assertEqual(errors(result), [], label)
            self.assertTrue(result["can_save"], label)
            step = result["steps"][0]
            self.assertEqual(step["action"], action, label)
            self.assertEqual(step["value"], value, label)
        # A recorded "save what the field holds" step reads the element clicked
        # before it, so it needs that element rather than a bare name.
        saved = parse_file("save.xlsx", workbook([["Action"], ["Click the order number field"],
                                                  ["Save input into order_number"]]))
        self.assertEqual(errors(saved), [])
        self.assertEqual(saved["steps"][1]["action"], "save_variable")
        self.assertEqual(saved["steps"][1]["value"], "order_number")
        alone = parse_file("save-alone.xlsx", workbook([["Action"], ["Save input into order_number"]]))
        self.assertTrue(any("needs the field it reads" in item["message"] for item in errors(alone)))
        # "Select: Express" needs the dropdown the flow clicked before it.
        paired = workbook([["Action"], ["Click the Delivery dropdown"], ["Select: Express"]])
        result = parse_file("pair.xlsx", paired)
        self.assertEqual(errors(result), [])
        self.assertEqual(result["steps"][1]["action"], "select_option")
        self.assertEqual(result["steps"][1]["selector"].get("by"), "label")
