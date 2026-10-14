"""Shared building blocks for the nine QA agents.

Every agent returns the same envelope so the dashboard, the JSON download and
the Markdown copy can render any of them without a special case:

    {
      "agent": "<id>", "title": "...", "summary": "...",
      "verdict": "<agent-specific short label>", "score": 0-100 | None,
      "findings": [finding, ...], "artifacts": {...}, "warnings": [...],
      "engine": "rules" | "rules+ai", "ai": {...}, "generated_at": "<iso>"
    }

Bounds are deliberate. Agents run inside the 512 MB service without a browser,
so every input is capped and every list is truncated with a note rather than
allowed to grow with the input.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

MAX_TEXT_CHARS = 200_000       # largest pasted input any agent accepts
MAX_FINDINGS = 200             # per agent; extra occurrences are summarised
MAX_ITEMS = 150                # generated cases, rows, questions
SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_WEIGHT = {"critical": 25, "high": 12, "medium": 6, "low": 2, "info": 0}


class InputError(ValueError):
    """The request is malformed or outside the agent's bounds (HTTP 400/413)."""


class InputLimitError(InputError):
    """Input is larger than an agent accepts. Routes answer 413 for this type only."""


# ---------------------------------------------------------------------------
# Redaction. Applied to everything an agent echoes back or sends to a model.
_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|authorization)"
                r"(\s*[:=]\s*)(['\"]?)([^\s'\",;&]+)"), r"\1\2\3***"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "***JWT***"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "***KEY***"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"), "***GITHUB-TOKEN***"),
    (re.compile(r"(?i)\b(postgres(?:ql)?|mysql|mssql)(\+\w+)?://[^\s'\"]+"), r"\1://***"),
    # The lookbehind stops a match starting mid-word; without it a long run of letters is quadratic.
    (re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
]


def scrub(text) -> str:
    """Remove credentials, tokens and personal e-mail addresses from text."""
    value = "" if text is None else str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def clip(text, limit: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def as_text(value, limit: int = MAX_TEXT_CHARS, field: str = "input") -> str:
    """Accept a string or a list of strings and return bounded text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(item) for item in value)
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value)
    if len(text) > limit:
        raise InputLimitError(f"{field} is {len(text):,} characters; the limit is {limit:,}. Split it into smaller parts.")
    return text


def lines(text: str) -> list[str]:
    return [line.rstrip() for line in str(text or "").splitlines()]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Findings and scores.
def finding(check_id: str, severity: str, title: str, detail: str = "", suggestion: str = "",
            location: str | None = None) -> dict:
    if severity not in SEVERITIES:
        raise ValueError("unknown severity " + severity)
    item = {"id": check_id, "severity": severity, "title": title, "detail": detail, "suggestion": suggestion}
    if location:
        item["location"] = location
    return item


def cap_findings(items: list[dict]) -> tuple[list[dict], list[str]]:
    """Bound the findings list and say so when it was cut."""
    warnings = []
    if len(items) > MAX_FINDINGS:
        warnings.append(f"Showing the first {MAX_FINDINGS} of {len(items)} findings; fix these and run again.")
        items = items[:MAX_FINDINGS]
    return items, warnings


def score_from(findings: list[dict]) -> int:
    """100 minus a weighted penalty per finding, never below zero."""
    penalty = sum(SEVERITY_WEIGHT.get(item.get("severity"), 0) for item in findings)
    return max(0, 100 - penalty)


def severity_counts(findings: list[dict]) -> dict:
    counts = {severity: 0 for severity in SEVERITIES}
    for item in findings:
        counts[item.get("severity", "info")] = counts.get(item.get("severity", "info"), 0) + 1
    return counts


def envelope(agent: str, title: str, summary: str, verdict: str, findings: list[dict],
             artifacts: dict | None = None, score: int | None = None, warnings: list[str] | None = None,
             scored: bool = True) -> dict:
    """Wrap an agent result. Generators and reports pass scored=False: a 0-100 quality score is
    meaningless for a list of generated cases or a release decision, so it is reported as None."""
    findings, cut = cap_findings(findings)
    return {
        "agent": agent,
        "title": title,
        "summary": summary,
        "verdict": verdict,
        "score": (score_from(findings) if score is None else score) if scored else None,
        "findings": findings,
        "severity_counts": severity_counts(findings),
        "artifacts": artifacts or {},
        "warnings": list(warnings or []) + cut,
        "engine": "rules",
        "ai": {"available": False, "used": False, "model": None, "error": None, "reason": ""},
        "ai_insights": [],
        "generated_at": now_iso(),
    }


def csv_cell(value) -> str:
    """Spreadsheet-safe cell: formulas are neutralised, quotes are doubled."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def csv_text(header: list[str], rows: list[list]) -> str:
    out = [",".join(csv_cell(cell) for cell in header)]
    for row in rows:
        out.append(",".join(csv_cell(cell) for cell in row))
    return "\n".join(out) + "\n"
