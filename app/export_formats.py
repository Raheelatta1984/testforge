"""Portable, explicit exports of the canonical (uncompressed) recording steps.

These are interoperability artifacts, not native Jenkins/Azure/Jira/TestComplete
recordings. Import templates need mapping to the destination project's schema.
"""
import csv
import json
from pathlib import Path


def _cell(value):
    text = str(value if value is not None else "")
    # Prevent spreadsheet formula execution in both CSV and XLSX consumers.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _table(path: Path, headers, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows([_cell(cell) for cell in row] for row in rows)
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for XLSX recording exports") from exc
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Steps"
    sheet.append(headers)
    for row in rows:
        sheet.append([_cell(cell) for cell in row])
    workbook.save(path.with_suffix(".xlsx"))


def write_exports(folder: Path, recording: dict) -> None:
    steps = sorted(recording.get("steps") or [], key=lambda step: step.get("order") or 0)
    title = recording.get("name") or "TestForge recording"
    # Playwright Python runner; values may contain {{variable}} and are read
    # from TF_VAR_<name> at runtime, never substituted into code literals.
    source = '''"""Generated TestForge Playwright replay. Install playwright and chromium first."""
import os
import re
from playwright.sync_api import sync_playwright

STEPS = ''' + repr(steps) + '''

def value(text):
    def replace(match):
        name = match.group(1)
        env = "TF_VAR_" + name
        if env not in os.environ:
            raise KeyError("Missing variable: " + env)
        return os.environ[env]
    return re.sub(r"\\{\\{\\s*([\\w.\\-]+)\\s*\\}\\}", replace, text or "")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1024, "height": 640})
    variables = {}
    try:
        for step in STEPS:
            action = step["action"]
            target = step.get("selector") or {}
            selector = target.get("primary")
            text = value(step.get("value"))
            if action == "navigate":
                page.goto(text)
            elif action == "click":
                if selector:
                    page.locator(selector).first.click()
                else:
                    page.mouse.click(target["x"], target["y"])
            elif action in ("type", "fill"):
                if selector:
                    page.locator(selector).first.click()
                if action == "fill" and selector:
                    page.locator(selector).first.fill(text)
                else:
                    page.keyboard.type(text)
            elif action == "press":
                page.keyboard.press(text)
            elif action == "save_variable":
                variables[text] = page.locator(selector).first.input_value()
                os.environ["TF_VAR_" + text] = variables[text]
            else:
                raise ValueError("Unsupported action: " + action)
    finally:
        browser.close()
'''
    (folder / "playwright_test.py").write_text(source, encoding="utf-8")
    (folder / "playwright_test.js").write_text(r'''// Generated TestForge Playwright JavaScript replay. npm install playwright
const { chromium } = require('playwright');
const steps = require('./steps.json');
const vars = {};
function value(text) {
  return String(text ?? '').replace(/\{\{\s*([\w.\-]+)\s*\}\}/g, (_match, name) => {
    if (name in vars) return vars[name];
    if (!(`TF_VAR_${name}` in process.env)) throw new Error(`Missing TF_VAR_${name}`);
    return process.env[`TF_VAR_${name}`];
  });
}
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1024, height: 640 } });
  try {
    for (const step of steps) {
      const selector = (step.selector || {}).primary;
      const text = value(step.value);
      if (step.action === 'navigate') await page.goto(text);
      else if (step.action === 'click') {
        if (selector) await page.locator(selector).first().click();
        else await page.mouse.click(step.selector.x, step.selector.y);
      } else if (step.action === 'fill' && selector) await page.locator(selector).first().fill(text);
      else if (step.action === 'type' || step.action === 'fill') {
        if (selector) await page.locator(selector).first().click();
        await page.keyboard.type(text);
      } else if (step.action === 'press') await page.keyboard.press(text);
      else if (step.action === 'save_variable') vars[text] = await page.locator(selector).first().inputValue();
      else throw new Error(`Unsupported action: ${step.action}`);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
''', encoding="utf-8")
    # Jenkinsfile invokes the actual replay instead of echoing success.
    repo_resources = "library/projects/{}/recordings/{}/resources".format(
        recording["project_id"], recording["id"])
    (folder / "Jenkinsfile").write_text(
        "pipeline {\n  agent any\n  stages {\n    stage('TestForge replay') {\n"
        "      steps {\n        sh 'cd " + repo_resources + " && python -m pip install playwright && python -m playwright install chromium && python playwright_test.py'\n"
        "      }\n    }\n  }\n}\n", encoding="utf-8")
    feature = ["Feature: " + title.replace("\n", " "), "  Scenario: Replay recorded steps"]
    for step in steps:
        label = (step.get("label") or step.get("action") or "step").replace("\n", " ").replace('"', "'")
        feature.append('    When I perform step %s "%s"' % (step.get("order"), label))
    (folder / "recording.feature").write_text("\n".join(feature) + "\n", encoding="utf-8")
    (folder / "steps.json").write_text(json.dumps(steps, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    azure = [(title, step.get("order"), step.get("label") or step.get("action"),
              step.get("value") or "", json.dumps(step.get("selector") or {}),
              step.get("screenshot") or "") for step in steps]
    _table(folder / "azure-devops.csv", ["Test Case", "Step", "Action", "Input", "Selector", "Screenshot"], azure)
    jira = [(title, step.get("order"), step.get("action"), step.get("label") or "",
             step.get("value") or "", step.get("screenshot") or "") for step in steps]
    _table(folder / "jira.csv", ["Summary", "Step", "Action", "Description", "Input", "Screenshot"], jira)
    _table(folder / "testcomplete.csv", ["Step", "Action", "Selector", "Input", "Screenshot"],
           [(step.get("order"), step.get("action"), json.dumps(step.get("selector") or {}),
             step.get("value") or "", step.get("screenshot") or "") for step in steps])
    (folder / "README.md").write_text(
        "# TestForge exports\n\n`recording.json` is the source of truth. "
        "`playwright_test.py` and `playwright_test.js` are runnable Playwright replays; Jenkinsfile invokes Python "
        "(set TF_VAR_<name> environment variables for placeholders). "
        "The Gherkin feature needs project-specific step definitions. "
        "Azure DevOps, Jira and TestComplete CSV/XLSX files are import templates "
        "that require mapping to the target instance; they are not native proprietary recordings. "
        "Screenshots are PNG. Video, when enabled, is WebM, not PNG.\n",
        encoding="utf-8")
