"""Failure Triage: classify failures as Product, Automation, Data or Environment.

Each error is scored against weighted signals for the four categories. The
highest score wins; the matched signals are returned as the rationale so an
engineer can disagree with a specific reason rather than a black box. The
existing executor bucket (app/batch_runner.classify_failure, used for retry
decisions) is reported alongside, not replaced.
"""
from __future__ import annotations

import re

from .common import MAX_ITEMS, as_text, clip, envelope, finding, scrub

CATEGORIES = ("product", "automation", "data", "environment")
OWNER = {
    "product": "Development team: reproduce on the feature, then file a defect (Defect Drafting agent).",
    "automation": "QA automation: fix the locator, wait or script logic; re-record the step if the UI changed.",
    "data": "Test-data owner: refresh or reset the fixture, account or inventory the test depends on.",
    "environment": "Platform / DevOps: check service health, DNS, certificates, capacity; re-run once stable.",
}

# (category, weight, label, pattern)
SIGNALS = [
    ("environment", 3, "connection refused", re.compile(r"ECONNREFUSED|connection refused", re.I)),
    ("environment", 3, "DNS / host not found", re.compile(r"ENOTFOUND|getaddrinfo|name_not_resolved|DNS", re.I)),
    ("environment", 3, "network error (net::ERR_*)", re.compile(r"net::ERR_|ERR_CONNECTION|ERR_NETWORK", re.I)),
    ("environment", 3, "socket reset / hang-up", re.compile(r"ECONNRESET|socket hang up|connection reset", re.I)),
    ("environment", 3, "gateway error 502/504", re.compile(r"\b50[24]\b|bad gateway|gateway time-?out", re.I)),
    ("environment", 2, "service unavailable 503", re.compile(r"\b503\b|service unavailable|maintenance", re.I)),
    ("environment", 2, "TLS / certificate problem", re.compile(r"certificate|\bSSL\b|\bTLS\b|handshake", re.I)),
    ("environment", 2, "rate limited (429)", re.compile(r"\b429\b|too many requests|rate limit", re.I)),
    ("environment", 3, "resource exhaustion", re.compile(r"out of memory|\bOOM\b|no space left|disk full|EMFILE", re.I)),
    ("environment", 2, "browser or target crashed", re.compile(r"target crashed|browser has been closed|crashed", re.I)),
    ("data", 3, "duplicate record", re.compile(r"duplicate key|already exists|unique constraint|already (?:used|redeemed|booked|registered)", re.I)),
    ("data", 3, "referential integrity", re.compile(r"foreign key|IntegrityError|violates", re.I)),
    ("data", 3, "insufficient stock or balance", re.compile(r"insufficient (?:balance|funds|stock|inventory|quantity)|out of stock|sold out", re.I)),
    ("data", 2, "record or account not found", re.compile(r"no such record|record not found|user not found|account not found|no records? (?:found|match)", re.I)),
    ("data", 2, "credentials rejected", re.compile(r"invalid credentials|wrong password|bad credentials", re.I)),
    ("data", 2, "expired fixture", re.compile(r"expired (?:token|code|session|voucher|coupon)|test data|fixture|dataset", re.I)),
    ("data", 1, "not eligible for the scenario", re.compile(r"not eligible|no longer valid", re.I)),
    ("automation", 4, "strict-mode locator ambiguity", re.compile(r"strict mode violation", re.I)),
    ("automation", 4, "element detached or stale", re.compile(r"not attached to the DOM|detached|stale element", re.I)),
    ("automation", 3, "locator or selector not found", re.compile(r"no such element|unable to locate|locator (?:resolved|not)|waiting for (?:locator|selector|element)|selector .* not found", re.I)),
    ("automation", 3, "covered by another element", re.compile(r"intercepts pointer events|element is covered", re.I)),
    ("automation", 2, "element not visible or not enabled", re.compile(r"not visible|not enabled|outside of the viewport", re.I)),
    ("automation", 3, "script error in test code", re.compile(r"TypeError|ReferenceError|AttributeError|NameError|SyntaxError|ModuleNotFoundError|ImportError|KeyError|IndexError|is not a function|cannot read propert", re.I)),
    ("automation", 2, "fixture or helper missing in test code", re.compile(r"fixture '[^']+' not found|step not found|unknown variable", re.I)),
    ("automation", 1, "generic timeout", re.compile(r"timeout|timed out|exceeded", re.I)),
    ("product", 3, "assertion on expected vs actual", re.compile(r"AssertionError|expected .* (?:to|but)|to (?:equal|be|contain|have) .* received|assert .* ==", re.I)),
    ("product", 4, "server error 500", re.compile(r"\b500\b|internal server error|unhandled exception", re.I)),
    ("product", 3, "wrong result returned", re.compile(r"returned (?:wrong|incorrect|unexpected)|incorrect (?:total|value|result)|calculation", re.I)),
    ("product", 2, "uncaught application error", re.compile(r"uncaught (?:exception|error|typeerror)|unhandled (?:rejection|promise)", re.I)),
    ("product", 1, "forbidden for an allowed user", re.compile(r"\b403\b|forbidden|permission denied", re.I)),
    ("product", 2, "should-behaviour not met", re.compile(r"should (?:have|be|show|display|redirect|contain)", re.I)),
]
SPLIT = re.compile(r"\n(?=\s*(?:\S*Error\b|\S*Exception\b|AssertionError|TimeoutError|FAILED|✘|✗|Failed\b|Error:))")
EXECUTOR_BUCKET_MAP = {"recording": "automation", "selector": "automation", "timeout": "automation",
                       "navigation": "environment", "browser": "environment", "other": None}


def classify(text: str) -> dict:
    """Classify one error. Returns category, confidence, scores and matched signals."""
    body = str(text or "")
    scores = {category: 0 for category in CATEGORIES}
    matched: list[tuple[str, str]] = []
    for category, weight, label, pattern in SIGNALS:
        if pattern.search(body):
            scores[category] += weight
            matched.append((category, label))
    total = sum(scores.values())
    if total == 0:
        return {"category": "automation", "confidence": 0.2, "scores": scores, "signals": [],
                "rationale": "No decisive signal. Scripts are the most common cause of unexplained failures, "
                             "so the error is filed as automation; verify manually."}
    best = max(CATEGORIES, key=lambda category: (scores[category], -CATEGORIES.index(category)))
    share = scores[best] / total
    runner_up = sorted(scores.values(), reverse=True)[1]
    confidence = round(min(0.95, 0.35 + 0.6 * share - (0.15 if scores[best] - runner_up <= 1 else 0)), 2)
    signals = [label for category, label in matched if category == best]
    rationale = "Matched " + ", ".join(signals) + "."
    if scores[best] - runner_up <= 1 and runner_up:
        rationale += " A second category also scored; treat as mixed and check the evidence."
    return {"category": best, "confidence": max(0.2, confidence), "scores": scores, "signals": signals,
            "rationale": rationale}


def split_errors(text: str) -> list[str]:
    chunks = [chunk.strip() for chunk in SPLIT.split(str(text or "")) if chunk.strip()]
    return chunks or []


def triage(errors: list[str] | None = None, log: str | None = None, context: dict | None = None) -> dict:
    items = [scrub(as_text(item, field="error")) for item in (errors or []) if str(item or "").strip()]
    if log:
        items.extend(scrub(chunk) for chunk in split_errors(as_text(log, field="log")))
    items = [item for item in items if item.strip()][:MAX_ITEMS]
    context = context or {}
    findings: list[dict] = []
    rows = []
    totals = {category: 0.0 for category in CATEGORIES}
    try:
        from app.batch_runner import classify_failure  # the executor's own retry bucket
    except Exception:  # pragma: no cover - the runner needs Playwright; triage must not
        classify_failure = None

    for index, text in enumerate(items, start=1):
        result = classify(text)
        bucket = classify_failure(text) if classify_failure else None
        totals[result["category"]] += result["confidence"]
        mapped = EXECUTOR_BUCKET_MAP.get(bucket) if bucket else None
        # None = the executor expressed no opinion ("other"), so there is nothing to agree or disagree with.
        agrees = None if mapped is None else mapped == result["category"]
        rows.append({"index": index, "error": clip(text, 600), "category": result["category"],
                     "confidence": result["confidence"], "rationale": result["rationale"],
                     "signals": result["signals"], "owner": OWNER[result["category"]],
                     "executor_bucket": bucket, "executor_agrees": agrees})
        if result["confidence"] < 0.5:
            findings.append(finding("TRI-01", "low", f"Low-confidence classification (error {index})",
                                    clip(text, 180), "Attach the trace or screenshot and classify manually."))

    if not rows:
        findings.append(finding("TRI-00", "medium", "No failure text to triage",
                                "Provide the error messages or the failing log lines.",
                                "Paste the error text, or start the triage from a failed run."))
        verdict, dominant = "nothing to triage", None
    else:
        total = sum(totals.values()) or 1.0
        dominant = max(CATEGORIES, key=lambda c: totals[c])
        share = totals[dominant] / total
        verdict = dominant if share >= 0.5 else "mixed"
        if dominant == "product" and share >= 0.5:
            findings.append(finding("TRI-02", "high", "Probable product defect",
                                    f"{round(share * 100)}% of the weighted evidence points at the application.",
                                    OWNER["product"]))
        if dominant == "environment" and share >= 0.5:
            findings.append(finding("TRI-03", "medium", "Environment instability",
                                    "Re-run after the environment is healthy before reporting a defect.",
                                    OWNER["environment"]))

    counts = {category: sum(1 for row in rows if row["category"] == category) for category in CATEGORIES}
    summary = (f"{len(rows)} failure(s) triaged: " + ", ".join(f"{counts[c]} {c}" for c in CATEGORIES) +
               (f". Dominant: {verdict}." if rows else "."))
    return envelope("failure-triage", "Failure Triage", summary, verdict, findings,
                    artifacts={"classifications": rows, "counts": counts,
                               "context": {k: scrub(str(v))[:200] for k, v in context.items()
                                           if k in ("recording", "step", "run_id", "browser", "environment")}},
                    scored=False)
