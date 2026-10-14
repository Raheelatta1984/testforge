"""Requirement Review: missing acceptance criteria, vague wording and edge cases.

Deterministic and offline. The edge-case table below is shared with the Test
Case Generation agent so both agents agree on what "an edge case" means.
"""
from __future__ import annotations

import re

from .common import MAX_ITEMS, as_text, clip, envelope, finding, lines, scrub

# Terms that make a requirement impossible to test as written.
VAGUE_TERMS = {
    "fast": "Quantify the speed, for example 'responds within 2 seconds for 95% of requests'.",
    "quick": "Quantify the time limit.",
    "user-friendly": "Describe the observable behaviour instead (steps, messages, defaults).",
    "easy": "Describe what the user can do, not how easy it feels.",
    "intuitive": "Describe the observable behaviour instead.",
    "appropriate": "Name the rule that decides what is appropriate.",
    "adequate": "State the threshold or amount.",
    "sufficient": "State the threshold or amount.",
    "reasonable": "State the limit or range.",
    "as needed": "State the condition that triggers the behaviour.",
    "etc": "List every case or state that the list is closed.",
    "and so on": "List every case or state that the list is closed.",
    "robust": "Define the failure conditions the system must survive.",
    "seamless": "Describe the observable steps and the wait/feedback the user sees.",
    "efficient": "Quantify the resource or time limit.",
    "flexible": "State which options or configurations are supported.",
    "some": "Give the count or the exact set.",
    "various": "List the exact set.",
    "several": "Give the exact number.",
    "many": "Give the exact number or range.",
    "soon": "Give a time limit.",
    "approximately": "Give the exact value or the allowed tolerance.",
    "if possible": "Make the behaviour mandatory or state the fallback.",
    "optionally": "State the exact condition under which the option appears.",
}

# (topic, trigger, handled-by-the-requirement pattern, edge cases to consider)
EDGE_RULES = [
    ("authentication", r"\blog ?in\b|\bsign ?in\b|password|credential|\bsession\b|\blogout\b",
     r"lock|attempt|expir|invalid|wrong|timeout|remember",
     ["Wrong password", "Account locked after repeated failures", "Expired session redirects to sign-in",
      "Empty username or password", "Password with leading or trailing spaces"]),
    ("money and quantities", r"\bamount\b|\bprice\b|\btotal\b|quantit|discount|\btax\b|balance|payment|\bfee\b|\bcost\b",
     r"zero|negative|maximum|decimal|round|currency",
     ["Zero value", "Negative value", "Maximum allowed value", "More decimal places than the currency allows",
      "Rounding of totals across line items"]),
    ("dates and time", r"\bdate\b|deadline|schedule|expir|calendar|time ?zone|\bdue\b|\bperiod\b",
     r"past|future|time ?zone|leap|format|range|before|after",
     ["Date in the past", "Leap day (29 February)", "Time-zone boundary near midnight",
      "Start date after end date", "Daylight-saving transition"]),
    ("file upload", r"upload|attach|\bfile\b|document|\bimport\b|\bcsv\b|image",
     r"size|type|format|empty|corrupt|limit|duplicate",
     ["Empty file", "File above the size limit", "Unsupported type or renamed extension",
      "Corrupt file", "Duplicate file name"]),
    ("search and lists", r"search|filter|\blist\b|\btable\b|\bsort|paginat",
     r"no results|empty|page|limit|special",
     ["No results", "Pagination boundary (first, last, exactly one page)",
      "Special characters in the query", "Sort order stable across pages"]),
    ("delete and edit", r"\bdelete\b|\bremove\b|\bcancel\b|\bedit\b|\bupdate\b|\bmodify\b",
     r"confirm|undo|unchanged|concurrent|already|deleted",
     ["Confirmation before a destructive action", "Cancel leaves the data unchanged",
      "Concurrent edit by another user", "Action on an already-deleted record"]),
    ("contact details", r"e-?mail|phone|\baddress\b|contact",
     r"format|valid|duplicate|unicode|length",
     ["Malformed address", "Address with '+' alias or unicode characters", "Duplicate address", "Very long input"]),
    ("roles and permissions", r"\brole\b|permission|\badmin\b|\baccess\b|authori[sz]|approv|\bmanager\b",
     r"denied|forbidden|403|role|permission|unauthori",
     ["User without the role sees a denial, not the data", "Role removed while the user is signed in",
      "Direct URL access without the permission"]),
    ("submit and retries", r"submit|\border\b|checkout|\bbook|reserv|transfer|\bcreate\b",
     r"double|duplicate|retry|timeout|idempot|twice|last",
     ["Double submit (double click)", "Network timeout after submit (is retry safe?)",
      "Two users reserving the last available item"]),
    ("integrations", r"\bapi\b|webhook|integrat|\bsync\b|third.party|external|notification|\bemail sent\b",
     r"unavailable|timeout|retry|fallback|error",
     ["Upstream service unavailable", "Slow upstream response", "Malformed upstream payload"]),
]

PERFORMANCE = re.compile(r"performance|\bload\b|respond|response time|latency|scal|throughput|concurrent users|\bspeed\b|fast", re.I)
SECURITY_TRIGGER = re.compile(r"log ?in|sign ?in|password|payment|card|personal data|\bpii\b|gdpr|health|salary|"
                              r"\bssn\b|token|upload|\bcredit\b|bank", re.I)
SECURITY_COVER = re.compile(r"encrypt|authori[sz]|authenticat|permission|\brole\b|mask|tls|https|audit|"
                            r"rate limit|csrf|xss|sanitis|sanitiz|validat|lock", re.I)
NEGATIVE_COVER = re.compile(r"invalid|error|fail|reject|denied|\bempty\b|missing|blank|duplicate|limit|maximum|"
                            r"minimum|timeout|unauthori[sz]ed|expired|cannot|must not", re.I)
ACCESS_TRIGGER = re.compile(r"screen|page|button|\bform\b|click|display|\bmenu\b|\bdialog\b", re.I)
ACCESS_COVER = re.compile(r"accessib|keyboard|screen reader|aria|wcag|contrast|tab order|focus", re.I)
CRITERIA_MARKERS = re.compile(r"\b(given|when|then|acceptance|criteria|should|must|shall|expected|ac-?\d+)\b", re.I)
OPEN_QUESTION = re.compile(r"\btbd\b|\btbc\b|to be (decided|confirmed)|\bxx+\b|lorem ipsum|\?\s*$", re.I)
ACTOR = re.compile(r"\b(user|customer|admin|administrator|operator|system|manager|guest|member|employee|"
                   r"visitor|client|approver|shopper|agent|service|api)s?\b|\bas an? \w+", re.I)


def _statements(text: str) -> list[str]:
    out = []
    for raw in lines(text):
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)]|\[[ xX]?\])\s*", "", raw).strip()
        if len(line) < 6 or line.startswith("#") or line.endswith(":"):
            continue
        out.append(line)
    return out


def review(text: str, title: str | None = None) -> dict:
    source = scrub(as_text(text, field="requirements"))
    statements = _statements(source)
    findings: list[dict] = []
    questions: list[str] = []
    flat = " ".join(statements) or source

    if not CRITERIA_MARKERS.search(source):
        findings.append(finding(
            "REQ-01", "high", "No acceptance criteria",
            "None of the text uses Given/When/Then, 'should', 'must' or 'shall', so there is nothing to test against.",
            "Add one Given/When/Then scenario per behaviour, including the happy path and at least one failure."))
        questions.append("What are the acceptance criteria (Given/When/Then) for each behaviour?")

    seen_terms = set()
    for term, advice in VAGUE_TERMS.items():
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.I)
        hits = [s for s in statements if pattern.search(s)] if statements else ([source] if pattern.search(source) else [])
        if hits and term not in seen_terms:
            seen_terms.add(term)
            findings.append(finding(
                "REQ-02", "medium", f"Vague term: '{term}'",
                "Found in: " + clip(hits[0], 160), advice))
            questions.append(f"What does '{term}' mean for this feature, in measurable terms?")

    for statement in statements:
        if OPEN_QUESTION.search(statement):
            findings.append(finding("REQ-03", "high", "Open question in the requirement",
                                    clip(statement, 200), "Resolve it before testing or mark the case as blocked."))
            questions.append("Resolve: " + clip(statement, 120))
            break

    for statement in statements:
        if PERFORMANCE.search(statement) and not re.search(r"\d", statement):
            findings.append(finding(
                "REQ-04", "high", "Performance wording without a number",
                clip(statement, 200),
                "State a measurable target (for example 'p95 under 800 ms at 200 concurrent users')."))
            questions.append("What is the measurable performance target for: " + clip(statement, 100))
            break

    if statements and not any(ACTOR.search(s) for s in statements):
        findings.append(finding(
            "REQ-05", "low", "No actor named",
            "No statement says who performs the behaviour (user, admin, system, API).",
            "Start each statement with 'As a <role>' or name the system component."))

    if SECURITY_TRIGGER.search(flat) and not SECURITY_COVER.search(flat):
        findings.append(finding(
            "REQ-06", "high", "Sensitive behaviour without security requirements",
            "The feature touches sign-in, payments, personal or secret data, but no security rule is stated.",
            "State authentication, authorisation, masking/encryption and audit expectations."))
        questions.append("Who may use this feature, and what must be masked, encrypted or logged?")

    if not NEGATIVE_COVER.search(flat):
        findings.append(finding(
            "REQ-07", "medium", "No negative or error behaviour described",
            "Nothing says what happens for invalid, empty, duplicate or unauthorised input.",
            "Add a failure criterion for each input and each dependency that can fail."))
        questions.append("What must happen when the input is invalid, empty or duplicated?")

    if ACCESS_TRIGGER.search(flat) and not ACCESS_COVER.search(flat):
        findings.append(finding(
            "REQ-08", "low", "No accessibility expectations",
            "The requirement describes screens or controls but no keyboard, label or contrast rule.",
            "Add keyboard-only and screen-reader expectations for the new controls."))

    for statement in statements:
        if len(re.findall(r"\band\b", statement, re.I)) >= 3 and re.search(r"should|must|shall", statement, re.I):
            findings.append(finding(
                "REQ-09", "low", "Compound requirement",
                clip(statement, 200), "Split it into one testable requirement per clause."))
            break

    topics = []
    edge_cases = []
    for topic, trigger, handled, cases in EDGE_RULES:
        if not re.search(trigger, flat, re.I):
            continue
        topics.append(topic)
        if re.search(handled, source, re.I):
            continue
        findings.append(finding(
            "REQ-10", "medium", f"Edge cases not described: {topic}",
            "The requirement does not say how the system handles: " + "; ".join(cases) + ".",
            "Add a criterion for the cases that apply, or state explicitly that they are out of scope."))
        edge_cases.extend({"topic": topic, "case": case} for case in cases)
        questions.append(f"How should the system behave for {topic} edge cases ({cases[0].lower()})?")

    if not statements:
        findings.append(finding("REQ-00", "high", "Nothing to review",
                                "The text contains no requirement statements.",
                                "Paste the user story or acceptance criteria."))

    verdict = ("ready" if not any(item["severity"] in ("critical", "high") for item in findings)
               else "needs clarification")
    counts = {"statements": len(statements), "topics": topics, "vague_terms": sorted(seen_terms)}
    summary = (f"Reviewed {len(statements)} statement(s). {len(findings)} finding(s); "
               f"{'ready for test design' if verdict == 'ready' else 'clarify the high-severity items before testing'}.")
    return envelope(
        "requirements", title or "Requirement Review", summary, verdict, findings,
        artifacts={"questions": questions[:MAX_ITEMS], "edge_cases": edge_cases[:MAX_ITEMS], "stats": counts})
