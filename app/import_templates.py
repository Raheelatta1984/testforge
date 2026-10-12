"""Downloadable onboarding templates: one per supported format, all importable.

A template is only listed here if the real importer accepts it: `tests/
test_onboarding_formats.py` walks this pack and fails if any template produces an
error diagnostic. Nothing in the pack points at a real system -- the targets are
the reserved `example.test` domain and the built-in demo page -- and every literal
input is a `{{variable}}` placeholder.

Formats covered: canonical CSV/XLSX, natural-language steps in Excel, Azure DevOps
columns, Playwright Python, Playwright JavaScript/TypeScript, BDD/Gherkin, a
Jenkins pipeline with its referenced script, Robot Framework, Selenium IDE,
Cypress, canonical JSON, YAML, Markdown, and a Postman collection.
"""
import base64
import io
import json
from pathlib import PurePosixPath

NOTICE = ("Templates are synthetic: the target is example.test or the built-in demo page and every "
          "literal input is a {{variable}}. Preview each one before you replace the steps with your own.")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

MEDIA_TYPES = {
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".json": "application/json",
    ".xlsx": "xlsx", ".py": "text/x-python", ".js": "text/javascript", ".ts": "text/plain",
    ".feature": "text/plain", ".robot": "text/plain", ".side": "application/json",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".md": "text/markdown", ".txt": "text/plain",
    ".html": "text/html",
}

# --- the templates -------------------------------------------------------------

SIMPLE_STEPS_CSV = """action,selector,value,label,repeat_count
navigate,,{{base_url}},Open the application,1
fill,#name,Ada,Enter the username,1
click,#go,,Continue,1
assert_text,#out,"Hello, Ada",Verify the greeting,1
"""

SIMPLE_STEPS_XLSX = [
    ["action", "selector", "value", "label", "repeat_count"],
    ["navigate", "", "{{base_url}}", "Open the application", 1],
    ["fill", "#name", "Ada", "Enter the username", 1],
    ["click", "#go", "", "Continue", 1],
    ["assert_text", "#out", "Hello, Ada", "Verify the greeting", 1],
]

# "Simple steps in Excel": one natural-language action per row. The importer reads
# the action cell as a step label, so no canonical verb is needed.
SIMPLE_SENTENCES_XLSX = [
    ["#", "Action", "Expected Result"],
    [1, "Open the application", "The sign-in form is displayed"],
    [2, "Enter Ada in the Username field", ""],
    [3, "Click the Continue button", ""],
    [4, "Verify the page contains Welcome", ""],
]

AZURE_DEVOPS_XLSX = [
    ["Test Case", "Step", "Action", "Input", "Selector", "Expected Result", "Priority"],
    ["Login", 1, "Open the application", "{{base_url}}", "", "Sign-in form is displayed", 1],
    ["Login", 2, "Enter {{username}} in the Username field", "{{username}}", "#name", "", 1],
    ["Login", 3, "Click the Continue button", "", "#go", "Dashboard loads", 1],
    ["Login", 4, "Verify the page contains Welcome", "", "", "", 2],
]

# The shape TestForge itself exports (Library -> DOWNLOAD -> azure-devops.xlsx) and
# the shape other recorders produce: the Action cell holds the recorded *label*.
# This one exists to be re-imported, including a repeat marker and a recorded point.
RECORDED_LABELS_XLSX = [
    ["Test Case", "Step", "Action", "Input", "Selector", "Screenshot"],
    ["Login (recorded)", 1, "Open https://example.test/login", "", "", ""],
    ["Login (recorded)", 2, "Click enter using password", "",
     '{"kind": "text", "primary": "enter using password"}', ""],
    ["Login (recorded)", 3, "Click your password", "",
     '{"primary": "#password", "x": 528, "y": 562}', ""],
    ["Login (recorded)", 4, "Type: {{password}}", "{{password}}", '{"primary": "#password"}', ""],
    ["Login (recorded)", 5, "Press Enter", "", "", ""],
    ["Login (recorded)", 6, "Click add to cart \u00d73", "",
     '{"primary": "#add"}', ""],
]

PLAYWRIGHT_PY = '''from playwright.sync_api import expect


def test_login(page):
    page.goto("https://example.test/login")
    page.get_by_label("Username").fill("{{username}}")
    page.get_by_label("Password").fill("{{password}}")
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator("#welcome")).to_have_text("Welcome")
'''

PLAYWRIGHT_TS = """import { test, expect } from '@playwright/test';

test('login', async ({ page }) => {
  await page.goto('https://example.test/login');
  await page.getByLabel('Username').fill('{{username}}');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.locator('#welcome')).toContainText('Welcome');
});
"""

BDD_FEATURE = '''Feature: Login
  Scenario: Sign in with a valid account
    Given I open "https://example.test/login"
    When I fill "#username" with "{{username}}"
    And I fill "#password" with "{{password}}"
    And I click "#sign-in"
    Then "#welcome" should contain "Welcome"
'''

JENKINSFILE = """pipeline {
  agent any
  stages {
    stage('Playwright tests') {
      steps {
        sh 'python playwright_login.py'
      }
    }
  }
}
"""

ROBOT = """*** Settings ***
Documentation     Synthetic login suite. Replace the target with your own fixture.
*** Variables ***
${BASE_URL}       https://example.test/login
${USERNAME}       {{username}}
*** Test Cases ***
Login
    Open Browser    ${BASE_URL}    chromium
    Input Text    id=username    ${USERNAME}
    Click Button    Sign in
    Page Should Contain    Welcome
"""

SELENIUM_SIDE = json.dumps({
    "id": "synthetic-login",
    "version": "2.0",
    "name": "Synthetic login",
    "url": "https://example.test/login",
    "tests": [{
        "id": "login",
        "name": "Login",
        "commands": [
            {"id": "c1", "command": "open", "target": "https://example.test/login", "value": ""},
            {"id": "c2", "command": "type", "target": "id=username", "value": "{{username}}"},
            {"id": "c3", "command": "click", "target": "css=#sign-in", "value": ""},
            {"id": "c4", "command": "assertText", "target": "css=#welcome", "value": "Welcome"},
        ],
    }],
}, indent=2) + "\n"

CYPRESS = """describe('login', () => {
  it('signs in', () => {
    cy.visit('https://example.test/login');
    cy.get('#username').type('{{username}}');
    cy.get('#sign-in').click();
    cy.get('#welcome').should('contain', 'Welcome');
  });
});
"""

STEPS_JSON = json.dumps({
    "name": "Login (canonical JSON)",
    "variables": {"username": "Ada"},
    "steps": [
        {"action": "navigate", "value": "https://example.test/login", "label": "Open the login page"},
        {"action": "fill", "selector": {"kind": "label", "primary": "Username"}, "value": "{{username}}",
         "label": "Enter the username"},
        {"action": "click", "selector": {"kind": "role", "primary": "button", "name": "Sign in"},
         "label": "Click the Sign in button"},
        {"action": "assert_contains", "selector": {"kind": "css", "primary": "body"},
         "value": "Welcome", "label": "Verify the page contains Welcome"},
    ],
}, indent=2) + "\n"

STEPS_YAML = """name: Login (YAML steps)
variables:
  username: Ada
steps:
  - action: navigate
    value: https://example.test/login
  - action: fill
    selector:
      kind: label
      primary: Username
    value: "{{username}}"
  - Click the Sign in button
  - Verify the page contains Welcome
"""

CASES_MARKDOWN = """# Login

Test case: Login smoke

1. Open https://example.test/login
2. Enter {{username}} in the Username field
3. Click the Sign in button
4. Verify the page contains Welcome
"""

POSTMAN = json.dumps({
    "info": {"name": "Store API (synthetic)", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    "item": [{
        "name": "List stores",
        "request": {"method": "GET", "url": {"raw": "https://api.example.test/stores?storeNo=426"}, "header": []},
        "event": [{"listen": "test", "script": {"exec": ["pm.response.to.have.status(200);"]}}],
    }],
}, indent=2) + "\n"

README = """# TestForge onboarding templates

Every file in this pack is a working example of one supported input format. Import
them with **Library -> UPLOAD TEST SCRIPTS -> VALIDATE & PREVIEW** to see the parsed
steps before you replace them with your own.

| File | Format | What to notice |
| --- | --- | --- |
| `simple-steps.csv` | Canonical CSV | One action per row; `selector`, `value` and `label` are optional. |
| `simple-steps.xlsx` | Canonical Excel | The same table in a worksheet; literal cells only (no formulas, one sheet). |
| `simple-steps-in-excel.xlsx` | Plain-English Excel | One sentence per row; no action verb required. |
| `azure-devops-steps.xlsx` | Azure DevOps export | The column names Azure DevOps, TestRail and friends use; update the steps in place and upload. |
| `azure-devops-steps.csv` | Azure DevOps CSV | The same table as CSV. |
| `recorded-labels.xlsx` | A recorded export | The Action cell holds recorded labels (`Click add to cart \u00d73`, `Type: {{password}}`, `Click 528,562`); the Selector column keeps the recorded locator. This is what TestForge's own `azure-devops.xlsx` looks like. |
| `playwright-login.py` | Playwright Python | Straight-line actions and one `test_*` function with the `page` fixture. |
| `playwright-login.spec.ts` | Playwright JS/TS | One standard `test(...)` wrapper, one action per line. |
| `login.feature` | BDD / Cucumber | One scenario using the supported phrases. |
| `Jenkinsfile` + `playwright-login.py` | Jenkins bundle | Upload both together: the pipeline is validated, only its script becomes steps. |
| `login.robot` | Robot Framework | `*** Test Cases ***` keywords; `${VARIABLE}` becomes `{{variable}}`. |
| `login.side` | Selenium IDE | `open/type/click/assertText`; a fixed `pause` is refused on purpose. |
| `login.cy.js` | Cypress | `cy.visit/get/type/click/should`. |
| `login.steps.json` | Canonical JSON | The lossless form: re-import this for an exact round trip. |
| `login-steps.yaml` | YAML | A `steps:` list of actions or sentences. |
| `login-test-cases.md` | Markdown | Numbered natural-language steps. |
| `store-api.postman_collection.json` | Postman | Each request becomes an `api_request` step. |

Rules that apply to every format: no fixed sleeps, no shell commands, no macros, no
custom step definitions, no evaluated code, and nothing is executed during preview.
Use `{{variables}}` for passwords and other inputs; review the input values after
previewing to turn them into project variables.
"""


def _workbook(rows):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "Steps"
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _azure_rows():
    return AZURE_DEVOPS_XLSX


def _xy(rows):
    return "\n".join(",".join(_csv_cell(cell) for cell in row) for row in rows) + "\n"


def _csv_cell(value):
    text = str(value if value is not None else "")
    if any(character in text for character in ',"\n'):
        return '"' + text.replace('"', '""') + '"'
    return text


def build(template_id):
    """Return {filename: (bytes, media type)} for one template, or None."""
    for entry in CATALOG:
        if entry["id"] == template_id:
            files = {}
            for name in entry["files"]:
                content, media = _content(name)
                files[name] = (content, media)
            return files
    return None


def _content(name):
    stem = PurePosixPath(name).stem
    suffix = PurePosixPath(name).suffix.lower()
    if name == "simple-steps.csv":
        return SIMPLE_STEPS_CSV.encode(), MEDIA_TYPES[".csv"]
    if suffix == ".csv" and stem == "azure-devops-steps":
        # Quote every cell so a spreadsheet import cannot run a formula.
        body = "\n".join(",".join(_csv_cell(("'" if str(cell).lstrip().startswith(("=", "+", "-", "@")) else "")
                                       + str(cell)) for cell in row) for row in _azure_rows()) + "\n"
        return body.encode(), MEDIA_TYPES[".csv"]
    if suffix == ".xlsx":
        rows = {"simple-steps.xlsx": SIMPLE_STEPS_XLSX,
                "simple-steps-in-excel.xlsx": SIMPLE_SENTENCES_XLSX,
                "azure-devops-steps.xlsx": _azure_rows(),
                "recorded-labels.xlsx": RECORDED_LABELS_XLSX}.get(name)
        if rows is None:
            raise KeyError(name)
        return _workbook(rows), XLSX_MIME
    static = {
        "playwright-login.py": PLAYWRIGHT_PY, "playwright_login.py": PLAYWRIGHT_PY,
        "playwright-login.spec.ts": PLAYWRIGHT_TS, "login.feature": BDD_FEATURE,
        "Jenkinsfile": JENKINSFILE, "login.robot": ROBOT, "login.side": SELENIUM_SIDE,
        "login.cy.js": CYPRESS, "login.steps.json": STEPS_JSON, "login-steps.yaml": STEPS_YAML,
        "login-test-cases.md": CASES_MARKDOWN, "store-api.postman_collection.json": POSTMAN,
        "README.md": README,
    }
    if name in static:
        return static[name].encode(), MEDIA_TYPES.get(suffix, "text/plain")
    raise KeyError(name)


CATALOG = [
    {"id": "canonical-csv", "name": "Simple steps (CSV)", "format": "CSV",
     "files": ["simple-steps.csv"],
     "description": "The canonical template: action, selector, value, label, repeat_count."},
    {"id": "canonical-xlsx", "name": "Simple steps (Excel)", "format": "Excel .xlsx",
     "files": ["simple-steps.xlsx"],
     "description": "The same table as a single-sheet workbook."},
    {"id": "sentences-xlsx", "name": "Simple steps in Excel (plain English)", "format": "Excel .xlsx",
     "files": ["simple-steps-in-excel.xlsx"],
     "description": "One plain-English step per row; no canonical action column needed."},
    {"id": "azure-devops-xlsx", "name": "Azure DevOps export (.xlsx)", "format": "Excel .xlsx",
     "files": ["azure-devops-steps.xlsx"],
     "description": "Azure DevOps / TestRail column names with natural-language actions."},
    {"id": "recorded-labels", "name": "Re-import a recorded export (.xlsx)", "format": "Excel .xlsx",
     "files": ["recorded-labels.xlsx"],
     "description": "The Action cell holds recorded labels (with a repeat marker and a recorded point), "
                    "like TestForge's own azure-devops.xlsx; Selector/Input stay structured."},
    {"id": "azure-devops-csv", "name": "Azure DevOps export (CSV)", "format": "CSV",
     "files": ["azure-devops-steps.csv"],
     "description": "The same Azure DevOps table as CSV."},
    {"id": "playwright-python", "name": "Playwright Python", "format": "Playwright .py",
     "files": ["playwright-login.py"],
     "description": "Straight-line page actions plus assert_* expectations."},
    {"id": "playwright-ts", "name": "Playwright JavaScript / TypeScript", "format": "Playwright .ts",
     "files": ["playwright-login.spec.ts"],
     "description": "One standard test() wrapper, one action per line."},
    {"id": "bdd", "name": "BDD / Cucumber", "format": "Gherkin .feature",
     "files": ["login.feature"],
     "description": "One scenario using the supported Given/When/Then phrases."},
    {"id": "jenkins", "name": "Jenkins pipeline + its test script", "format": "Jenkinsfile bundle",
     "files": ["Jenkinsfile", "playwright_login.py"],
     "description": "Upload both files in one go; only the referenced script becomes steps."},
    {"id": "robot", "name": "Robot Framework", "format": "Robot .robot",
     "files": ["login.robot"],
     "description": "Test Cases keywords; ${VARIABLE} becomes {{variable}}."},
    {"id": "selenium-ide", "name": "Selenium IDE export", "format": "Selenium .side",
     "files": ["login.side"],
     "description": "open/type/click/assertText commands."},
    {"id": "cypress", "name": "Cypress spec", "format": "Cypress .js",
     "files": ["login.cy.js"],
     "description": "cy.visit/get/type/click/should chains."},
    {"id": "json", "name": "Canonical JSON steps", "format": "JSON",
     "files": ["login.steps.json"],
     "description": "Lossless round trip of canonical steps; also carries variables."},
    {"id": "yaml", "name": "YAML step list", "format": "YAML",
     "files": ["login-steps.yaml"],
     "description": "A steps: list of actions or plain-English lines."},
    {"id": "markdown", "name": "Markdown / plain text steps", "format": "Markdown",
     "files": ["login-test-cases.md"],
     "description": "Numbered natural-language steps with an expected result."},
    {"id": "postman", "name": "Postman collection", "format": "Postman v2",
     "files": ["store-api.postman_collection.json"],
     "description": "Each request becomes an api_request step with its status assertion."},
]


def catalog(include_content=True):
    """The template list; `base64` lets the dashboard preview a template in place."""
    entries = []
    for entry in CATALOG:
        item = {key: entry[key] for key in ("id", "name", "format", "description")}
        item["files"] = list(entry["files"])
        item["bundle"] = len(entry["files"]) > 1
        if include_content:
            files = build(entry["id"]) or {}
            item["content"] = {name: base64.b64encode(data).decode()
                               for name, (data, _media) in files.items()}
        entries.append(item)
    return entries


def zip_pack():
    """A ZIP of the whole pack (plus the README) for offline use."""
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", README)
        for entry in CATALOG:
            for name, (data, _media) in (build(entry["id"]) or {}).items():
                archive.writestr(entry["id"] + "/" + name, data)
    return buffer.getvalue()
