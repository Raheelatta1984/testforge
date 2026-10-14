"""Automation Code Review: unstable locators, hard waits and other bad practice.

Line-based static checks on Playwright, Selenium, pytest, unittest and
Jest-style scripts. The uploaded source is read as text and is never executed,
matching the rest of the import pipeline.
"""
from __future__ import annotations

import re

from .common import as_text, clip, envelope, finding, lines, scrub

# (id, severity, pattern, title, suggestion)
RULES = [
    ("CR-01", "high", re.compile(r"""(?:xpath\s*=\s*|page\.locator\(|find_element\w*\(|By\.XPATH|\$x\()\s*['"`]?\s*(?:/html|//\*|/\w+(?:\[\d+\])?/|\(//)"""),
     "Absolute or positional XPath", "Use getByRole, getByLabel or a data-testid attribute."),
    ("CR-02", "high", re.compile(r"""['"`]//[\w*]+(?:\[[^\]]*\])*\[\d+\]"""),
     "XPath with a numeric index", "Index-based XPath breaks when one element is added; select by role, text or test id."),
    ("CR-03", "high", re.compile(r"nth-child|nth-of-type|:nth-\w+"),
     "nth-child / nth-of-type selector", "Position-based CSS is layout-dependent; use a stable attribute."),
    ("CR-04", "high", re.compile(r"""['"`](?:\.|#)?(?:css|sc|jsx|makeStyles|Mui|emotion)-[A-Za-z0-9_-]{4,}['"`]|:r[0-9a-z]{1,4}:"""),
     "Generated class or id (CSS-in-JS / framework hash)",
     "Those names change on every build; target data-testid or an accessible name instead."),
    ("CR-05", "high", re.compile(r"time\.sleep\(|\bsleep\(\s*\d|waitForTimeout\(|Thread\.sleep\(|\bwait\s*\(\s*\d{3,}\s*\)"),
     "Hard-coded sleep", "Wait for a condition (expect ... toBeVisible, WebDriverWait, waitForResponse) instead of time."),
    ("CR-06", "critical", re.compile(
        r"""(?i)(?:\b(?:password|passwd|pwd|secret|api[_-]?key|token|client[_-]?secret)\b\s*[:=]\s*['"][^'"\s]{3,}['"]"""
        r"""|(?:password|passwd|pwd|secret|api[_-]?key|token)[^'"\n]{0,40}['"]\s*,\s*['"][^'"\s]{4,}['"])"""),
     "Credential literal in source", "Read it from an environment variable or the Variables vault; rotate the exposed value."),
    ("CR-07", "medium", re.compile(r"""(?:goto|open|visit|navigate)\(\s*['"]https?://"""),
     "Hard-coded absolute URL", "Use baseURL in the config or a project variable so environments can be switched."),
    ("CR-08", "high", re.compile(r"except\s*(?:Exception\s*)?:\s*pass\b|except\s+\w+\s*:\s*\n\s*pass|catch\s*(?:\([^)]*\))?\s*\{\s*\}"),
     "Exception swallowed silently", "Catch the specific error, log it and fail the step; silent passes hide defects."),
    ("CR-09", "medium", re.compile(r"\.only\(|\bfit\(|\bfdescribe\(|@pytest\.mark\.only"),
     "Focused test left in", "A focused test silently skips every other test in the file; remove .only before merging."),
    ("CR-10", "medium", re.compile(r"\.skip\(|\bxit\(|\bxdescribe\(|@pytest\.mark\.skip|@unittest\.skip|test\.skip\("),
     "Skipped test", "Skipped tests are invisible in the pass rate; fix, delete or quarantine with a ticket."),
    ("CR-11", "medium", re.compile(r"page\.\$\(|page\.\$\$\(|element_handle\(|find_element_by_\w+|find_elements_by_\w+|ElementHandle"),
     "Deprecated or handle-based API", "Use locators (page.locator / By.*) which auto-wait and re-resolve."),
    ("CR-12", "medium", re.compile(r"force\s*=\s*True|\{\s*force\s*:\s*true\s*\}"),
     "Forced action hides actionability problems", "Remove force; fix the overlay or the wait that makes the element unready."),
    ("CR-13", "low", re.compile(r"\bprint\(|console\.log\("),
     "Debug output left in test", "Use the test logger or remove before merging."),
    ("CR-14", "low", re.compile(r"expect\([^)]*\)\.toBeTruthy\(\)|assertTrue\(\s*True\s*\)|assert\s+True\b"),
     "Weak assertion", "Assert the actual value (toHaveText, assertEqual) instead of truthiness."),
    ("CR-15", "low", re.compile(r"\.(?:first|last)\b(?!\w)|\.nth\(\s*\d+\s*\)|\[\s*0\s*\]\.click\(|\.first\(\)"),
     "Positional selection", "Narrow the locator with a role or label so the match is unique."),
]

GOOD_LOCATOR = re.compile(r"get_by_role|getByRole|get_by_test_id|getByTestId|get_by_label|getByLabel|"
                          r"get_by_text|getByText|data-testid|data-test|data-qa|By\.ID|By\.NAME|aria-label", re.I)
TEST_START = re.compile(r"^\s*(?:async\s+)?def\s+test_\w+|^\s*(?:test|it)\s*\(\s*['\"`]", re.M)
ASSERTION = re.compile(r"assert|expect\(|should|toBe|toEqual|toHave|toContain|raises|self\.assert|check\(|verify", re.I)


def _tests(text: str):
    starts = [m.start() for m in TEST_START.finditer(text)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        header = text[start:start + 120].strip().splitlines()[0]
        yield header, text[start:end]


def review(source_text: str, filename: str | None = None) -> dict:
    source = as_text(source_text, field="script")
    cleaned = scrub(source)
    source_lines = lines(cleaned)
    findings: list[dict] = []
    counts = {"lines": len(source_lines), "tests": 0, "assertions": 0, "good_locators": 0, "unstable_locators": 0,
              "hard_waits": 0}
    name = (filename or "script").strip()[:120] or "script"

    for number, line in enumerate(source_lines, start=1):
        if not line.strip() or line.lstrip().startswith(("#", "//")):
            continue  # blank lines and comments carry no executable locator
        counts["good_locators"] += len(GOOD_LOCATOR.findall(line))
        for check_id, severity, pattern, title, advice in RULES:
            if pattern.search(line):
                if check_id in ("CR-01", "CR-02", "CR-03", "CR-04", "CR-15"):
                    counts["unstable_locators"] += 1
                if check_id == "CR-05":
                    counts["hard_waits"] += 1
                findings.append(finding(check_id, severity, title, clip(line.strip(), 180),
                                        advice, location=f"{name}:{number}"))

    for header, body in _tests(cleaned):
        counts["tests"] += 1
        found = len(ASSERTION.findall(body))
        counts["assertions"] += found
        if found == 0:
            findings.append(finding("CR-16", "high", "Test without an assertion", clip(header, 160),
                                    "A test that cannot fail proves nothing. Assert the outcome the requirement states.",
                                    location=name))

    if counts["tests"] == 0 and counts["lines"] > 0:
        findings.append(finding("CR-17", "info", "No test functions recognised",
                                "Expected def test_*, test(...) or it(...). The review still ran on every line.",
                                "If this is a helper module, review it together with the test files that use it."))

    # Balance: a script full of good locators is not punished for one stray XPath.
    if counts["good_locators"] >= 5 and counts["unstable_locators"] <= 1:
        findings.append(finding("CR-90", "info", "Good locator practice",
                                f"{counts['good_locators']} role, label or test-id locators found.",
                                "Keep this pattern."))

    summary = (f"Scanned {counts['lines']} line(s) and {counts['tests']} test(s): "
               f"{counts['unstable_locators']} unstable locator(s), {counts['hard_waits']} hard wait(s), "
               f"{counts['assertions']} assertion(s).")
    verdict = "fix before merge" if any(f["severity"] in ("critical", "high") for f in findings) else "acceptable"
    return envelope("code-review", "Automation Code Review", summary, verdict, findings,
                    artifacts={"stats": counts, "file": name})
