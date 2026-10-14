"""Test Case Generation: positive, negative and boundary scenarios from requirements.

Each acceptance criterion becomes a positive case. Numeric limits ('between 8
and 64 characters', 'at most 500') become boundary cases at min-1, min, max and
max+1. Format words (e-mail, date, phone) and required-field wording become
negative cases. Output is structured JSON plus a spreadsheet-safe CSV.
"""
from __future__ import annotations

import re

from .common import MAX_ITEMS, as_text, clip, csv_text, envelope, finding, scrub
from .requirements import EDGE_RULES, _statements

NUM = r"(\d+(?:\.\d+)?)"
RANGE_RE = re.compile(r"between\s+" + NUM + r"\s+and\s+" + NUM, re.I)
DASH_RANGE_RE = re.compile(r"(?<![\d.\-/:])" + r"(\d{1,6})\s*(?:-|to)\s*(\d{1,6})" + r"(?![\d.\-/:])", re.I)
MIN_RE = re.compile(r"(?:at least|minimum(?: of)?|min(?:imum)?|no less than|not less than|greater than or equal to)\s+"
                    + NUM, re.I)
MAX_RE = re.compile(r"(?:at most|maximum(?: of)?|max(?:imum)?|up to|no more than|not exceed(?:ing)?|"
                    r"less than or equal to)\s+" + NUM, re.I)
UNIT_RE = re.compile(r"characters?|chars?|digits?|items?|days?|years?|%|percent|files?|mb|kb|seconds?|words?", re.I)
REQUIRED_RE = re.compile(r"required|mandatory|must be provided|cannot be empty|not be blank|must not be empty|"
                         r"must enter|must select", re.I)
FORMAT_HINTS = [
    (re.compile(r"e-?mail", re.I), "e-mail address", ["plainaddress", "user@", "user@@example.com", "user@example"]),
    (re.compile(r"\bdate\b", re.I), "date", ["31/02/2030", "not-a-date", "00/00/0000"]),
    (re.compile(r"phone|mobile", re.I), "phone number", ["abc", "+", "12"]),
    (re.compile(r"\bnumber\b|amount|quantity|\bprice\b", re.I), "number", ["abc", "-1", "1e400"]),
    (re.compile(r"\burl\b|website|link", re.I), "URL", ["htp:/broken", "javascript:alert(1)"]),
]
PERMISSION_RE = re.compile(r"only (?:an? )?(admin\w*|manager|approver|owner)|\brole\b|permission|authori[sz]ed", re.I)
FIELD_RE = re.compile(r"^(?:the\s+)?([A-Za-z][A-Za-z /-]{0,40}?)\s+(?:must|should|shall|can|cannot|may|needs?|is|has)\b", re.I)
GENERIC_FIELD = "input"


def _field(criterion: str) -> str:
    match = FIELD_RE.search(criterion)
    if match:
        name = re.sub(r"^(the|a|an)\s+", "", match.group(1).strip().lower())
        if 1 <= len(name) <= 40 and len(name.split()) <= 4 and name not in {"user", "system", "it", "this", "value"} \
                and not re.match(r"(only|all|any|every|no|none)\b", name):
            return name
    return GENERIC_FIELD


def _limits(criterion: str) -> tuple[float | None, float | None, bool]:
    """(minimum, maximum, is_length) found in one criterion."""
    low = high = None
    match = RANGE_RE.search(criterion) or DASH_RANGE_RE.search(criterion)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
    else:
        found_min = MIN_RE.search(criterion)
        found_max = MAX_RE.search(criterion)
        low = float(found_min.group(1)) if found_min else None
        high = float(found_max.group(1)) if found_max else None
    if low is not None and high is not None and low > high:
        low, high = high, low
    unit = UNIT_RE.search(criterion)
    is_length = bool(unit and re.search(r"characters?|chars?|digits?|words?", unit.group(0), re.I))
    return low, high, is_length


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _case(cases, kind, title, steps, data, expected, criterion_no, priority="P2",
          preconditions="Application reachable", technique="equivalence partitioning"):
    """kind is one of positive, negative, boundary; technique says how the case was derived."""
    case_id = f"TC-{len(cases) + 1:03d}"
    cases.append({
        "id": case_id, "type": kind, "priority": priority, "title": title, "technique": technique,
        "preconditions": preconditions, "steps": steps, "test_data": data, "expected": expected,
        "criterion": criterion_no,
    })


def generate(text: str, feature: str | None = None) -> dict:
    source = scrub(as_text(text, field="requirements"))
    criteria = _statements(source)
    findings: list[dict] = []
    cases: list[dict] = []
    feature_name = (feature or "").strip()[:120] or "the feature"
    auth_context = bool(re.search(r"log ?in|sign ?in|account|member|customer", source, re.I))
    precondition = "User is signed in" if auth_context else "Application reachable"

    if not criteria:
        findings.append(finding("TC-00", "high", "No criteria to generate from",
                                "Paste acceptance criteria, one behaviour per line.",
                                "Use 'Given/When/Then' or 'The <field> must ...' sentences."))

    for index, criterion in enumerate(criteria[:MAX_ITEMS], start=1):
        field = _field(criterion)
        low, high, is_length = _limits(criterion)
        _case(cases, "positive", f"{feature_name}: {criterion[:90]}",
              [f"Open {feature_name}.", f"Perform: {criterion}", "Observe the outcome."],
              {}, criterion, index, priority="P1", preconditions=precondition)

        if REQUIRED_RE.search(criterion):
            _case(cases, "negative", f"{field}: empty value is rejected",
                  [f"Leave {field} empty.", "Submit the form."],
                  {field: ""}, f"Submission is blocked and {field} shows a required-field message.", index,
                  priority="P1", preconditions=precondition)

        if low is not None or high is not None:
            unit = "characters" if is_length else "value"
            if low is not None and high is not None:
                points = [
                    (low - 1, "invalid", f"below minimum ({_num(low)} - 1)", "rejected with a validation message"),
                    (low, "boundary", f"at minimum ({_num(low)})", "accepted"),
                    (high, "boundary", f"at maximum ({_num(high)})", "accepted"),
                    (high + 1, "invalid", f"above maximum ({_num(high)} + 1)", "rejected with a validation message"),
                ]
            elif high is not None:
                points = [
                    (high, "boundary", f"at maximum ({_num(high)})", "accepted"),
                    (high + 1, "invalid", f"above maximum ({_num(high)} + 1)", "rejected with a validation message"),
                ]
            else:
                points = [
                    (low, "boundary", f"at minimum ({_num(low)})", "accepted"),
                    (low - 1, "invalid", f"below minimum ({_num(low)} - 1)", "rejected with a validation message"),
                ]
            for value, kind, label, outcome in points:
                if value < 0:
                    continue
                shown = ("a string of " if is_length else "") + _num(value) + (" " + unit if is_length else "")
                _case(cases, "boundary" if kind == "boundary" else "negative", f"{field}: {label}",
                      [f"Enter {shown} into {field}.", "Submit."],
                      {field: ("x" * min(int(value), 5000)) if is_length else _num(value)},
                      f"Input is {outcome}.", index, priority="P1" if kind == "boundary" else "P2",
                      preconditions=precondition, technique="boundary value analysis")

        for pattern, label, samples in FORMAT_HINTS:
            if pattern.search(criterion):
                for sample in samples[:3]:
                    _case(cases, "negative", f"{label}: invalid format '{sample}' is rejected",
                          [f"Enter '{sample}' into the {label} field.", "Submit."],
                          {field if field != GENERIC_FIELD else label: sample},
                          "Submission is blocked with a message that names the format.", index,
                          preconditions=precondition, technique="invalid format class")
                break

        if PERMISSION_RE.search(criterion):
            _case(cases, "negative", f"Unauthorised user cannot perform: {criterion[:70]}",
                  ["Sign in as a user without the required role.", f"Attempt: {criterion}"],
                  {"role": "viewer"}, "Action is denied and no data changes.", index, priority="P1",
                  preconditions="User signed in without the role", technique="permission check")

        if not any(c["criterion"] == index and c["type"] == "negative" for c in cases):
            _case(cases, "negative", f"Invalid input for {field}",
                  [f"Enter an invalid value for {field}.", "Submit."],
                  {field: "@@@ invalid !!"}, "The input is rejected and no record changes.", index,
                  preconditions=precondition, technique="invalid value class")

    for topic, trigger, _handled, edge_list in EDGE_RULES:
        if not criteria:
            break
        flat = " ".join(criteria)
        if re.search(trigger, flat, re.I) and len(cases) < MAX_ITEMS * 4:
            _case(cases, "negative", f"Edge case ({topic}): {edge_list[0].lower()}",
                  [f"Set up the {topic} situation: {edge_list[0].lower()}.", "Perform the main action."],
                  {}, "The system refuses or handles the case as the requirement states; no silent failure.",
                  0, priority="P2", preconditions=precondition, technique="edge case catalogue")

    cases = cases[:MAX_ITEMS * 2]
    counts = {"positive": 0, "negative": 0, "boundary": 0}
    for case in cases:
        counts[case["type"]] = counts.get(case["type"], 0) + 1
    rows = [[c["id"], c["title"], c["type"], c["priority"], c["technique"], c["preconditions"], " | ".join(c["steps"]),
             "; ".join(f"{k}={v}" for k, v in c["test_data"].items()), c["expected"]] for c in cases]
    csv = csv_text(["ID", "Title", "Type", "Priority", "Technique", "Preconditions", "Steps", "Test data", "Expected"], rows)
    summary = (f"Generated {len(cases)} case(s) from {len(criteria)} criterion/criteria: "
               f"{counts['positive']} positive, {counts['negative']} negative, {counts['boundary']} boundary.")
    gherkin = to_gherkin(cases, feature_name)
    return envelope("test-cases", "Test Case Generation", summary,
                    "generated" if cases else "nothing generated", findings,
                    artifacts={"cases": cases, "counts": counts, "csv": csv, "gherkin": gherkin,
                               "criteria": criteria[:MAX_ITEMS]},
                    scored=False)


def _line(text: str, limit: int = 300) -> str:
    """One Gherkin line: whitespace collapsed, so no value can break the file structure."""
    return clip(" ".join(str(text or "").split()), limit)


def _sentence(text: str) -> str:
    return _line(text).rstrip(".")


def to_gherkin(cases: list[dict], feature: str) -> str:
    """Render cases as a Gherkin feature: one plain Scenario per case, tagged by type and priority.

    Scenario Outlines and Examples tables are not used, so each case reads on its own. The steps
    are plain English; a BDD runner needs step definitions bound to the application's locators.
    """
    if not cases:
        return ""
    out = [f"Feature: {_line(feature, 120)}",
           "  # Generated from the acceptance criteria by TestForge QA agents. Review before use.", ""]
    for case in cases:
        out.append(f"  @{case['type']} @{case['priority'].lower()}")
        out.append(f"  Scenario: {case['id']} {_line(case['title'], 200)}")
        precondition = case.get("preconditions") or ""
        if precondition in ("", "Application reachable"):
            precondition = "the application is reachable"
        out.append(f"    Given {_sentence(precondition)}")
        for index, step in enumerate(case.get("steps") or []):
            out.append(f"    {'When' if index == 0 else 'And'} {_sentence(step)}")
        out.append(f"    Then {_sentence(case.get('expected') or 'the outcome matches the requirement')}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
