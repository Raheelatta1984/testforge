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
    from app.export_templates import write_runners
    write_runners(folder, title, recording.get("execution_plan"))
    # Jenkinsfile invokes the actual replay instead of echoing success.
    repo_resources = "library/projects/{}/recordings/{}/resources".format(
        recording["project_id"], recording["id"])
    (folder / "Jenkinsfile").write_text(
        "pipeline {\n  agent any\n  stages {\n    stage('TestForge replay') {\n"
        "      steps {\n        sh 'cd " + repo_resources + " && python -m pip install -r requirements.txt && python -m playwright install chromium && python playwright_test.py'\n"
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
        "`playwright_test.py` and `playwright_test.js` are sequential Playwright replays. Download ALL resource files together. Python needs playwright; JavaScript/TypeScript use the included package.json (npm install). The TypeScript project runs with npm test and uses one worker, no retries, no video and failure-only screenshots. Jenkinsfile invokes Python "
        "(set TF_VAR_<name> environment variables for placeholders). "
        "The Gherkin feature needs project-specific step definitions. "
        "Azure DevOps, Jira and TestComplete CSV/XLSX files are import templates "
        "that require mapping to the target instance; they are not native proprietary recordings. "
        "Re-import steps.json for a lossless canonical round trip; arbitrary generated runtime code is not executed by the importer. "
        "Screenshots are PNG. Video, when enabled, is WebM, not PNG.\n",
        encoding="utf-8")
