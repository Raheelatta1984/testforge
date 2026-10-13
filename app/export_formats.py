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


def _label_cell(step) -> str:
    """The step label plus its repeat marker, so a table row survives a round trip."""
    label = step.get("label") or step.get("action") or ""
    repeat = step.get("repeat_count") or step.get("repeat") or 1
    if isinstance(repeat, int) and repeat > 1 and "×" not in label:
        return f"{label} ×{repeat}"
    return label


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
    # Desktop/mobile steps recorded outside a plan still get their portable
    # bundles (plan-based bundles are written by write_runners).
    from app.export_templates import write_desktop_bundle, write_mobile_bundle
    write_desktop_bundle(folder, steps)
    write_mobile_bundle(folder, steps)
    # A merged repeat is written into the label as " ×N" so the table can be
    # re-imported with the repeat count intact (steps.json carries it natively).
    azure = [(title, step.get("order"), _label_cell(step),
              step.get("value") or "", json.dumps(step.get("selector") or {}),
              step.get("screenshot") or "") for step in steps]
    _table(folder / "azure-devops.csv", ["Test Case", "Step", "Action", "Input", "Selector", "Screenshot"], azure)
    jira = [(title, step.get("order"), step.get("action"), _label_cell(step),
             json.dumps(step.get("selector") or {}), step.get("value") or "",
             step.get("screenshot") or "") for step in steps]
    _table(folder / "jira.csv", ["Summary", "Step", "Action", "Description", "Selector", "Input", "Screenshot"], jira)
    # The Action cell carries the label (with its repeat marker) exactly like the
    # Azure table; Selector/Input stay structured columns, so onboarding this
    # table reproduces the recording. An unknown column would make Jira's own
    # importer ask for a mapping, so every header here is a familiar one.
    _table(folder / "testcomplete.csv", ["Step", "Action", "Selector", "Input", "Screenshot"],
           [(step.get("order"), _label_cell(step), json.dumps(step.get("selector") or {}),
             step.get("value") or "", step.get("screenshot") or "") for step in steps])
    (folder / "README.md").write_text(
        "# TestForge exports\n\n`recording.json` is the source of truth. "
        "`playwright_test.py` and `playwright_test.js` are sequential Playwright replays. Download ALL resource files together. Python needs playwright; JavaScript/TypeScript use the included package.json (npm install). The TypeScript project runs with npm test and uses one worker, no retries, no video and failure-only screenshots. Jenkinsfile invokes Python "
        "(set TF_VAR_<name> environment variables for placeholders). "
        "The Gherkin feature needs project-specific step definitions. "
        "Azure DevOps, Jira and TestComplete CSV/XLSX files are import templates "
        "that require mapping to the target instance; they are not native proprietary recordings. "
        "Re-import steps.json for a lossless canonical round trip; the Azure DevOps, Jira and "
        "TestComplete tables round-trip too, because the label carries the repeat marker (\" ×N\") "
        "and the Selector column carries the recorded locator. Arbitrary generated runtime code is "
        "not executed by the importer. Screenshots are PNG. Video, when enabled, is WebM, not PNG. "
        "Set TF_EXECUTION_SPEED (0.25-3.0) to pace Python/JS replays exactly like the dashboard slider. "
        "Plans with Windows desktop or mobile profiles also export desktop_test.py/DesktopRunner.cs and "
        "mobile_test.py/mobile_test.js plus a Zebra DataWedge profile; they simulate locally unless "
        "TF_WINDOWS_AGENT_URL/TF_WINDOWS_AGENT_TOKEN or TF_MOBILE_HUB_URL/TF_MOBILE_HUB_TOKEN are set.\n",
        encoding="utf-8")
