"""Defect Drafting: turn logs and evidence into a professional, Jira-ready defect report.

The draft is assembled from the facts supplied (steps, expected and actual
behaviour, environment, the relevant log lines and evidence names). Every text
field is scrubbed for credentials and personal data first, and the report
states which fields are still missing rather than inventing them.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .common import as_text, clip, envelope, finding, lines, scrub

SEVERITIES = ("blocker", "critical", "major", "minor", "trivial")
PRIORITY = {"blocker": "Highest", "critical": "Highest", "major": "High", "minor": "Low", "trivial": "Lowest"}
CRITICAL = re.compile(r"data loss|cannot (?:log ?in|sign ?in|pay|check ?out)|payment (?:failed|fails)|crash|"
                      r"security|leak|exposed|unauthori[sz]ed access|\b500\b|corrupt", re.I)
MINOR = re.compile(r"typo|alignment|cosmetic|spacing|colou?r|font|label text|tooltip", re.I)
MAJOR = re.compile(r"error|fail|cannot|unable|incorrect|wrong|missing|not (?:saved|shown|updated)", re.I)


def _steps_from(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip() for line in lines(value)]
    else:
        items = []
        for step in value:
            if isinstance(step, dict):
                action = str(step.get("action") or "").strip()
                label = str(step.get("label") or "").strip()
                val = str(step.get("value") or "").strip()
                text = " ".join(part for part in (action, label) if part)
                if val and action.lower() in ("fill", "type", "select", "input"):
                    text += f' with "{val}"'
                items.append(text)
            else:
                items.append(str(step).strip())
    return [scrub(item)[:300] for item in items if item][:60]


def _area(url: str | None, recording: str | None) -> str:
    if url:
        path = [p for p in urlparse(url).path.split("/") if p]
        if path:
            return path[0].replace("-", " ").replace("_", " ").title()[:40]
    return (recording or "Application")[:40]


# The rest of the hub speaks critical/high/medium/low/info; a defect uses Jira's names. Map the
# hub's names so a reporter's explicit choice is kept rather than silently replaced.
SEVERITY_ALIASES = {"high": "major", "medium": "major", "low": "minor", "info": "trivial"}


def _severity(text: str, given: str | None) -> tuple[str, str]:
    chosen = str(given or "").strip().lower()
    chosen = SEVERITY_ALIASES.get(chosen, chosen)
    if chosen in SEVERITIES:
        return chosen, "given by the reporter"
    if CRITICAL.search(text):
        return "critical", "contains data-loss, security, crash or server-error wording"
    if MINOR.search(text) and not MAJOR.search(text):
        return "minor", "cosmetic wording and no functional failure"
    return "major", "functional failure without a data-loss or security signal"


def _log_excerpt(log: str | None, error: str | None) -> str:
    chunks = []
    if log:
        chunks.extend(lines(scrub(as_text(log, field="log")))[-40:])
    if error:
        chunks.append(scrub(as_text(error, field="error")))
    return clip("\n".join(chunks), 4000)


def draft(data: dict) -> dict:
    title = scrub(str(data.get("title") or "")).strip()
    expected = scrub(str(data.get("expected") or "")).strip()
    actual = scrub(str(data.get("actual") or "")).strip()
    error = data.get("error") or ""
    log = data.get("logs") or ""
    env = data.get("environment") if isinstance(data.get("environment"), dict) else {}
    env = {str(k)[:40]: scrub(str(v))[:200] for k, v in env.items() if v not in (None, "")}
    steps = _steps_from(data.get("steps") if data.get("steps") is not None else data.get("steps_text"))
    evidence = [str(name).strip()[:160] for name in (data.get("evidence") or []) if str(name).strip()][:30]
    recording = scrub(str(data.get("recording_name") or ""))[:120]
    run_id = str(data.get("run_id") or "")[:60]

    if not actual:
        actual = scrub(str(error).strip().splitlines()[0])[:400] if error else ""
    url = env.get("url") or data.get("url")
    area = _area(url, recording)
    if not title:
        # "[Area] <what went wrong>": the summary names the symptom; steps and log carry the rest.
        title = f"[{area}] {clip(actual or 'behaves unexpectedly', 200)}"
    title = clip(scrub(title), 255)

    severity, why = _severity(f"{actual} {error} {log}", data.get("severity"))
    findings: list[dict] = []
    if len(steps) < 2:
        findings.append(finding("DEF-01", "high", "Steps to reproduce missing or too short",
                                f"{len(steps)} step(s) supplied.",
                                "List every step from a clean start, one action per line."))
    if not expected:
        findings.append(finding("DEF-02", "high", "Expected result missing",
                                "A defect without an expected result cannot be verified as fixed.",
                                "State what the requirement says should happen."))
    if not actual:
        findings.append(finding("DEF-03", "high", "Actual result missing",
                                "Describe what happened, with the message or the value that was wrong."))
    if not env:
        findings.append(finding("DEF-04", "medium", "Environment not stated",
                                "Browser, OS, build and URL decide whether a defect reproduces.",
                                "Add environment fields: browser, OS, app version, URL."))
    if not evidence and not log and not error:
        findings.append(finding("DEF-05", "medium", "No evidence attached",
                                "No log lines, error text or screenshot names were provided.",
                                "Attach the failing screenshot, the video or the log excerpt."))
    if re.search(r"doesn'?t work|does not work|is broken|not working|bug", title, re.I):
        findings.append(finding("DEF-06", "medium", "Vague summary",
                                title, "Name the feature, the action and the wrong result in the summary."))
    if run_id:
        findings.append(finding("DEF-07", "info", "Linked execution", f"Run {run_id} is referenced in the report.",
                                "Keep the run id so the evidence can be re-opened."))

    excerpt = _log_excerpt(log, error)
    readiness = "ready to file" if not any(f["severity"] in ("high", "critical") for f in findings) else "incomplete"
    labels = ["testforge", "automation"] + ([severity] if severity else [])
    component = area if area else "Application"

    md = []
    md.append(f"## Summary\n{title}\n")
    md.append(f"**Severity:** {severity.title()} ({why})  \n**Priority:** {PRIORITY[severity]}  \n**Component:** {component}\n")
    if env:
        md.append("## Environment\n" + "\n".join(f"- **{k}:** {v}" for k, v in env.items()) + "\n")
    md.append("## Steps to reproduce\n" + ("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) or "_Not provided._") + "\n")
    md.append("## Expected result\n" + (expected or "_Not provided._") + "\n")
    md.append("## Actual result\n" + (actual or "_Not provided._") + "\n")
    if excerpt:
        md.append("## Log excerpt\n```\n" + excerpt + "\n```\n")
    if evidence:
        md.append("## Attachments\n" + "\n".join(f"- {name}" for name in evidence) + "\n")
    if run_id:
        md.append(f"_Reference: TestForge run {run_id}._\n")
    markdown = "\n".join(md)

    wiki = []
    wiki.append(f"h3. Summary\n{title}\n")
    wiki.append(f"*Severity:* {severity.title()} ({why})\n*Priority:* {PRIORITY[severity]}\n*Component:* {component}\n")
    if env:
        wiki.append("h3. Environment\n" + "\n".join(f"* *{k}:* {v}" for k, v in env.items()) + "\n")
    wiki.append("h3. Steps to reproduce\n" + ("\n".join(f"# {s}" for s in steps) or "_Not provided._") + "\n")
    wiki.append("h3. Expected result\n" + (expected or "_Not provided._") + "\n")
    wiki.append("h3. Actual result\n" + (actual or "_Not provided._") + "\n")
    if excerpt:
        wiki.append("h3. Log excerpt\n{code}\n" + excerpt + "\n{code}\n")
    if evidence:
        wiki.append("h3. Attachments\n" + "\n".join(f"* {name}" for name in evidence) + "\n")
    jira = "\n".join(wiki)

    csv_draft = {"Summary": title, "Issue Type": "Bug", "Priority": PRIORITY[severity],
                 "Labels": " ".join(labels), "Component": component,
                 "Description": "\n".join(md)[:30000]}
    summary = f"Draft defect '{clip(title, 80)}' ({severity}, {readiness})."
    return envelope("defect-drafting", "Defect Drafting", summary, readiness, findings, scored=False,
                    artifacts={"draft": {
                        "summary": title, "severity": severity, "priority": PRIORITY[severity],
                        "component": component, "labels": labels, "environment": env,
                        "steps": steps, "expected": expected, "actual": actual,
                        "evidence": evidence, "run_id": run_id or None, "recording": recording or None,
                        "markdown": markdown, "jira_wiki": jira, "jira_csv_row": csv_draft},
                        "readiness": readiness})
