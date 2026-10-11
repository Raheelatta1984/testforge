"""Onboard any test-script format the market uses into canonical steps.

The dashboard already converted a strict Playwright/CSV/BDD subset. Real teams
bring everything else: plain-text step lists, Azure DevOps and TestRail exports,
Selenium IDE projects, Postman collections, Robot Framework suites, Cypress specs,
Java/C# Selenium classes, Word documents, PDFs, YAML and legacy .xls sheets.
This module detects the format from the file (or from the content when the
extension is unknown), then converts it **statically**.

Nothing here executes, imports, evaluates or downloads anything: no subprocess,
no eval/exec, no macros, no remote includes, no AI service. Unsupported syntax is
reported as a diagnostic, never silently dropped -- the same rule the original
parser followed.
"""
import csv
import io
import json
import re
import zipfile
from pathlib import PurePosixPath

import defusedxml.ElementTree as SafeET

from app.script_import import (
    ImportProblem,
    MAX_STEPS,
    MAX_UNPACKED,
    parse_table,
)

# --- what each format is called, in the words the dashboard shows -------------
LABELS = {
    "csv": "CSV / TSV / semicolon or pipe table",
    "xlsx": "Excel workbook (.xlsx, .xlsm, .xls)",
    "json": "Canonical JSON steps, Selenium IDE .side, Postman collection",
    "yaml": "YAML steps or step list",
    "py": "Playwright Python",
    "js": "Playwright JavaScript / TypeScript / Cypress",
    "java": "Selenium Java",
    "cs": "Selenium C# / .NET",
    "feature": "Gherkin / Cucumber / SpecFlow",
    "robot": "Robot Framework",
    "xml": "Canonical step XML, JUnit/TestNG suite names",
    "docx": "Word document (.docx/.doc)",
    "pdf": "PDF test script",
    "text": "Plain text, Markdown, numbered steps, log or test-case notes",
    "jenkins": "Jenkins pipeline + its referenced script",
}

# Extensions handled by this module, mapped to the parser key.
TEXT_EXTENSIONS = {
    ".txt": "text", ".text": "text", ".step": "text", ".steps": "text", ".tst": "text",
    ".log": "text", ".md": "text", ".markdown": "text", ".mdown": "text", ".rst": "text",
    ".asciidoc": "text", ".adoc": "text", ".1st": "text",
    ".yaml": "yaml", ".yml": "yaml",
    ".robot": "robot", ".resource": "robot",
    ".xml": "xml", ".side": "json", ".har": "json",
    ".java": "java", ".cs": "cs", ".spec.js": "js", ".cy.js": "js", ".cys": "js",
    ".html": "html", ".htm": "html", ".doc": "docx", ".docx": "docx", ".xhtml": "html", ".rtf": "rtf",
    ".pdf": "pdf", ".xls": "xls", ".xlsm": "xlsx", ".xltx": "xlsx", ".xltm": "xlsx",
    ".xlsx": "xlsx", ".csv": "csv", ".tsv": "csv", ".psv": "csv", ".tab": "csv",
    ".json": "json", ".jsonc": "json", ".py": "py", ".js": "js", ".ts": "js", ".mjs": "js",
    ".tsx": "js", ".jsx": "js", ".feature": "feature", ".bdd": "feature", ".gherkin": "feature",
    ".groovy": "jenkins", ".jenkins": "jenkins", ".jenkinsfile": "jenkins",
}

# Header names actually used by Azure DevOps, TestRail, Zephyr, Xray, TestLink,
# QTest, TestComplete, HP ALM, Postman and Cucumber-Studio exports.
HEADER_ALIASES = {
    "action": {"action", "actions", "keyword", "keywords", "operation", "command", "step action",
               "action step", "test step", "teststep", "step", "steps", "step type", "control",
               "interaction", "user action", "activity"},
    "selector": {"selector", "locator", "element locator", "object", "object name", "element",
                 "element name", "target", "object repository", "ui object", "web element",
                 "xpath", "css", "by", "identifier", "field", "field name", "control name",
                 "screen element", "page object", "test object"},
    "value": {"value", "values", "input", "input data", "input value", "test data", "data",
              "data value", "parameter", "parameter value", "test input", "text", "content",
              "argument", "arguments", "arg", "key", "keys", "payload", "sample data"},
    "label": {"label", "description", "step description", "test step description", "step name",
              "summary", "comment", "comments", "notes", "note", "action description",
              "instruction", "instructions", "details", "case step", "step details"},
    "expected": {"expected", "expected result", "expected results", "expected output",
                 "expected result / output", "verification", "verify", "assertion", "assertions",
                 "expected value", "expected behaviour", "expected behavior", "result",
                 "expected outcome", "acceptance criteria"},
    "repeat": {"repeat", "repeat_count", "repeat count", "times", "iterations", "iteration", "loop"},
    "order": {"order", "no", "number", "step no", "step number", "step #", "#", "seq", "sequence",
              "id", "index", "test step number"},
    "case": {"test case", "testcase", "test cases", "case", "case name", "scenario", "scenario name",
             "test case name", "tc", "tc id", "test case id", "title", "test name", "test"},
    "precondition": {"precondition", "preconditions", "pre-condition", "pre-conditions",
                     "pre requisite", "prerequisite", "prerequisites", "setup", "given"},
    # Columns that are real spreadsheet metadata rather than steps: accepted, ignored,
    # and named in a note so nothing disappears without being mentioned.
    "ignore": {"priority", "severity", "status", "owner", "assigned to", "assignee", "author",
               "created by", "created", "created on", "created date", "updated", "updated on",
               "module", "component", "requirement", "requirement id", "reference", "ref",
               "sprint", "release", "milestone", "folder", "suite", "test suite", "tags", "tag",
               "labels", "environment", "env", "browser", "platform", "device", "automation",
               "automation type", "type", "design", "design step", "duration", "time", "notes "},
}

HEADER_LOOKUP = {alias: field for field, aliases in HEADER_ALIASES.items() for alias in aliases}

# English keywords used by the step parser, per canonical action.
NAV_WORDS = r"(?:open|navigate|go|goto|launch|browse|visit|load|access|start|use)"
CLICK_WORDS = r"(?:click|tap|press|hit|select|choose|pick|activate)"
INPUT_WORDS = r"(?:enter|type|input|fill|populate|set|provide|supply|write|key in|put)"
ASSERT_WORDS = r"(?:verify|assert|validate|confirm|ensure|check|expect|make sure|see that|test that)"
QUOTED = r"(?:\"[^\"]+\"|'[^']+'|\u201c[^\u201d]+\u201d|\u2018[^\u2019]+\u2019)"
VARIABLE = r"\{\{\s*[\w.\-]+\s*\}\}"
VARIABLE_RE = re.compile(VARIABLE)

# 300 is the oldest width this schema has used for a step label; long URLs became
# labels, so imported labels are clipped with the same helper the recorder uses.
from app.db import fit_text  # noqa: E402  (kept at the bottom of the import block)


def label(text, action):
    return fit_text(str(text or action))


def clean_value(text):
    """Strip quoting, bullets and trailing punctuation from a captured value."""
    value = str(text or "").strip()
    value = re.sub(r"^[\-\u2022\s]+", "", value)
    for opening, closing in (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019")):
        if len(value) > 1 and value.startswith(opening) and value.endswith(closing):
            value = value[1:-1]
            break
    value = value.strip()
    value = re.sub(r"\{\{\s*([\w.\-]+)\s*\}\}", r"{{\1}}", value)
    if value.endswith(".") and value.count(".") == 1 and not re.search(r"\.[a-z]{2,}$", value):
        value = value[:-1]
    return value.strip()


def strip_numbering(line):
    """Remove the step numbering a real test script puts in front of every line."""
    text = line.strip()
    text = re.sub(r"^\|\s*", "", text)
    text = re.sub(r"\s*\|\s*$", "", text)
    text = re.sub(r"^[\[\(]?\s*(?:step|test step|tc|case|scenario)\s*#?\s*\d+(?:\.\d+)*\s*[\]\)]?\s*[:\-.)]?\s*", "", text, flags=re.I)
    text = re.sub(r"^[\[\(]\s*\d+\s*[\]\)]\s*", "", text)
    text = re.sub(r"^\d+(?:\.\d+)*\s*[:\-.)]\s*", "", text)
    text = re.sub(r"^[\-\*\u2022\u00b7\u25aa>]+\s*", "", text)
    text = re.sub(r"^\[[ xX]\]\s*", "", text)
    text = re.sub(r"^(?:given|when|then|and|but)\s+", "", text, flags=re.I)
    return text.strip()


# --- locators ----------------------------------------------------------------

CSS_SHAPE = re.compile(r"(?:^#|^\.|^\[|^[a-zA-Z*][\w-]*[#.\[>:]|^[a-zA-Z*]$|^[\w-]+\s*[>+~])")
XPATH_HINT = re.compile(r"^(?://|\(//|/html|\.//|xpath=)", re.I)


def _attr_css(tag, name, value, contains=False):
    tag = tag if tag and tag != "*" else ""
    quoted = '"%s"' % value.replace('"', "")
    return f"{tag}[{name}*={quoted}]" if contains else f"{tag}[{name}={quoted}]"


def xpath_to_locator(expression):
    """Translate the everyday Selenium XPath subset to a CSS or text locator.

    Enterprise suites are full of `//input[@id='txtUser']`. Anything beyond this
    bounded set is refused with a message, because a partially translated locator
    that silently matches the wrong element is worse than a clear error.
    """
    raw = expression.strip()
    raw = re.sub(r"^xpath\s*=\s*", "", raw, flags=re.I)
    raw = re.sub(r"^\(//(.+)\)\[(\d+)\]$", r"//\1", raw)
    if not raw.startswith("//"):
        if raw.startswith("/"):
            # Absolute document paths (/html/body/div[3]/form/input) resolve to a
            # descendant chain, which CSS can express directly.
            parts = [p for p in raw.strip("/").split("/") if p and p != "html" and p != "body"]
            if not parts:
                raise ImportProblem("Absolute XPath has no element to select")
            chain = []
            for part in parts:
                match = re.fullmatch(r"([\w-]+|\*)(?:\[(\d+)\])?", part)
                if not match:
                    raise ImportProblem(f"XPath step '{part}' is not a plain element/index; convert it to a CSS selector")
                piece = "" if match.group(1) == "*" else match.group(1)
                if match.group(2) and match.group(2) != "1":
                    piece += f":nth-of-type({match.group(2)})"
                chain.append(piece or "*")
            return {"kind": "css", "primary": " ".join(chain)}
        raise ImportProblem("Unsupported locator; use a CSS selector, text, id, name or a simple XPath")
    body = raw[2:]
    index = None
    if re.search(r"\)\[\d+\]$", expression):
        index = re.search(r"\)\[(\d+)\]$", expression).group(1)
    for pattern, handler in (
        (r"^([\w*-]+)?\[@id\s*=\s*['\"]([^'\"]+)['\"]\]$", lambda m: {"kind": "css", "primary": "#" + m.group(2)}),
        (r"^([\w*-]+)?\[contains\(@id\s*,\s*['\"]([^'\"]+)['\"]\)\]$", lambda m: {"kind": "css", "primary": _attr_css(m.group(1), "id", m.group(2), True)}),
        (r"^([\w*-]+)?\[@name\s*=\s*['\"]([^'\"]+)['\"]\]$", lambda m: {"kind": "css", "primary": _attr_css(m.group(1), "name", m.group(2))}),
        (r"^([\w*-]+)?\[@data-testid\s*=\s*['\"]([^'\"]+)['\"]\]$", lambda m: {"kind": "test_id", "primary": m.group(2)}),
        (r"^([\w*-]+)?\[contains\(@class\s*,\s*['\"]([^'\"]+)['\"]\)\]$", lambda m: {"kind": "css", "primary": _attr_css(m.group(1), "class", m.group(2), True)}),
        (r"^([\w*-]+)?\[@class\s*=\s*['\"]([^'\"]+)['\"]\]$", lambda m: {"kind": "css", "primary": _attr_css(m.group(1), "class", m.group(2))}),
        (r"^([\w*-]+)?\[@(aria-label|placeholder|title|alt|value|href)\s*=\s*['\"]([^'\"]+)['\"]\]$",
         lambda m: {"kind": "css", "primary": _attr_css(m.group(1), m.group(2), m.group(3))}),
        (r"^([\w*-]+)?\[\s*(?:normalize-space\(\s*)?text\(\)\s*\)?\s*=\s*['\"]([^'\"]+)['\"]\s*\]$",
         lambda m: {"kind": "text", "primary": m.group(2)}),
        (r"^([\w*-]+)?\[contains\((?:\s*text\(\)\s*|\.)?\s*,\s*['\"]([^'\"]+)['\"]\)\]$",
         lambda m: {"kind": "text", "primary": m.group(2)}),
        (r"^([\w*-]+)?\[@type\s*=\s*['\"]([^'\"]+)['\"]\]$", lambda m: {"kind": "css", "primary": _attr_css(m.group(1), "type", m.group(2))}),
        (r"^([\w*-]+)$", lambda m: {"kind": "css", "primary": m.group(1) if m.group(1) != "*" else "body"}),
    ):
        match = re.fullmatch(pattern, body)
        if match:
            spec = handler(match)
            if index and index != "1":
                spec["primary"] = f"{spec['primary']}:nth-of-type({index})"
            return spec
    raise ImportProblem(
        "XPath '%s' is more advanced than the importer translates; convert it to a CSS selector "
        "or a label/text locator" % expression.strip()[:120]
    )


def field_locator(name, role=None):
    """Turn a field or control name from a plain-English step into a locator."""
    text = clean_value(name)
    text = re.sub(r"\s+(?:field|input|box|textbox|button|link|tab|menu|icon|option|item|checkbox|radio|dropdown|list|combobox|window|dialog|popup|control|element)$", "", text, flags=re.I)
    text = text.strip()
    if not text:
        raise ImportProblem("A field or control name is required")
    if VARIABLE_RE.fullmatch(text):
        raise ImportProblem("A variable cannot be used as a locator; name the field or control")
    lowered = text.lower()
    if lowered.startswith(("xpath=", "//", "(//", "/html")) or XPATH_HINT.match(text):
        return xpath_to_locator(text)
    if lowered.startswith("css="):
        return {"kind": "css", "primary": text[4:].strip()}
    if lowered.startswith("xpath="):
        return xpath_to_locator(text[6:])
    if lowered.startswith("text="):
        return {"kind": "text", "primary": text[5:].strip()}
    if lowered.startswith("testid=") or lowered.startswith("test-id=") or lowered.startswith("data-testid="):
        return {"kind": "test_id", "primary": text.split("=", 1)[1].strip()}
    if lowered.startswith("id=") or lowered.startswith("name="):
        key, _, value = text.partition("=")
        return {"kind": "css", "primary": f'[{key.strip().lower()}="{value.strip().strip(chr(34))}"]'}
    if lowered.startswith("linktext=") or lowered.startswith("link="):
        return {"kind": "text", "primary": text.split("=", 1)[1].strip()}
    if CSS_SHAPE.search(text) and " " not in text.rstrip():
        return {"kind": "css", "primary": text}
    if role and role.lower() in {"button", "link", "tab", "menuitem", "checkbox", "radio", "combobox",
                                 "textbox", "heading", "option", "listitem", "switch", "searchbox"}:
        return {"kind": "role", "primary": role.lower(), "name": text, "exact": False}
    if role and role.lower() in {"input", "field"}:
        return {"kind": "label", "primary": text, "exact": False}
    if role and role.lower() in {"text", "label", "message", "value", "content", "heading"}:
        return {"kind": "text", "primary": text, "exact": False}
    return {"kind": "text", "primary": text, "exact": False}


def _role_for(noun):
    return (noun or "").strip().lower() or None


# --- smart tables ------------------------------------------------------------

def _split_smart(text):
    """Split a table line by the delimiter it actually uses."""
    line = text.rstrip("\n")
    if line.count("\t") >= 1:
        return [cell.strip() for cell in line.split("\t")]
    if line.count("|") >= 2:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return cells
    if line.count(";") >= 2:
        return [cell.strip() for cell in next(csv.reader([line], delimiter=";"))]
    if line.count(",") >= 1:
        return [cell.strip() for cell in next(csv.reader([line]))]
    return [line.strip()]


def _normalise_header(cell):
    text = str(cell or "").strip().lower()
    text = text.replace("_", " ").replace("/", " / ")
    text = re.sub(r"[^a-z0-9 #./]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_table_smart(rows, preview, source="table"):
    """Parse a table whose headers come from a real export, not our template.

    Azure DevOps, TestRail, Zephyr, Xray, TestLink, QTest, TestComplete, HP ALM
    and Cucumber Studio all name the same columns differently. Anything that is
    not a known column is still an error: a header we do not understand must not
    silently become an ignored column.
    """
    rows = [list(row) for row in rows]
    header_index = None
    for index, row in enumerate(rows[:10]):
        normalised = [_normalise_header(cell) for cell in row]
        if any(cell in HEADER_LOOKUP and HEADER_LOOKUP[cell] == "action" for cell in normalised):
            header_index = index
            break
    if header_index is None:
        return False
    header = [_normalise_header(cell) for cell in rows[header_index]]
    fields = []
    unknown = []
    for cell in header:
        field = HEADER_LOOKUP.get(cell)
        if field:
            fields.append(field)
        elif cell:
            fields.append(None)
            unknown.append(str(cell))
        else:
            fields.append(None)
    if unknown:
        preview.error(header_index + 1, "Unknown columns: " + ", ".join(sorted(set(unknown))) +
                      " (rename them to action/selector/value/label/expected result)")
        return True
    if "action" not in fields and not ({"selector", "value"} & set(fields)):
        return False
    if any(field == "ignore" for field in fields):
        ignored = sorted({header[i] for i, field in enumerate(fields) if field == "ignore" and header[i]})
        if ignored:
            preview.note(header_index + 1, f"{source}: metadata columns accepted and not imported: {', '.join(ignored)}")
    if "case" in fields:
        names = []
        for row in rows[header_index + 1:]:
            values = dict(zip(fields, [cell for cell in row] + [None] * len(fields)))
            if values.get("case") and str(values["case"]).strip():
                names.append(str(values["case"]).strip())
        if len({name for name in names}) > 1:
            preview.error(header_index + 1, "Multiple test cases in one sheet must be uploaded separately "
                                           "(or keep the test case column empty)")
            return True
        if names:
            preview.name = names[0][:200]
    for offset, row in enumerate(rows[header_index + 1:], header_index + 2):
        if not any(cell not in (None, "") and str(cell).strip() for cell in row):
            continue
        values = dict(zip(fields, [cell for cell in row] + [None] * (len(fields) - len(row))))
        _table_row(preview, values, offset)
    return True


def _table_row(preview, values, line):
    """One export row: an action, or an object/data/expected-result triple."""
    action = clean_value(values.get("action") or "")
    selector = clean_value(values.get("selector") or "")
    value = clean_value(values.get("value") if values.get("value") is not None else values.get("label") or "")
    description = clean_value(values.get("label") or "")
    expected = clean_value(values.get("expected") or "")
    repeat = values.get("repeat")
    precondition = clean_value(values.get("precondition") or "")
    if precondition:
        preview.note(line, f"precondition noted, not imported as a step: {precondition[:120]}")
    if not action and not selector and not value and not expected:
        return
    if not action:
        # Object + data is the HP ALM / TestComplete shape: no verb column.
        if selector and value:
            preview.add({"action": "fill", "selector": field_locator(selector), "value": value,
                         "label": description or f"Fill {selector}"}, line)
        elif selector:
            preview.add({"action": "click", "selector": field_locator(selector),
                         "label": description or f"Click {selector}"}, line)
        elif expected:
            preview.add({"action": "assert_contains", "selector": {"kind": "css", "primary": "body"},
                         "value": expected, "label": description or "Verify page content"}, line)
        else:
            preview.error(line, "Row has no action and no object; add an action column or an object name")
        return
    keyword = action.lower().strip()
    known = {"navigate", "click", "type", "fill", "press", "check", "uncheck", "select_option",
             "hover", "dblclick", "assert_text", "assert_contains", "assert_value", "assert_visible",
             "assert_hidden", "assert_enabled", "assert_disabled", "assert_checked", "assert_unchecked",
             "assert_count", "assert_url", "assert_title", "wait_visible", "wait_hidden", "capture_text"}
    if keyword not in known:
        # A real export may use English instead of canonical verbs ("Click", "Enter text").
        handled = parse_english_line(
            preview, " ".join(part for part in [action, selector, value] if part), line)
        if handled:
            if expected:
                preview.add({"action": "assert_contains",
                             "selector": {"kind": "css", "primary": "body"}, "value": expected,
                             "label": "Verify expected result"}, line)
            return
        preview.error(line, f"Unsupported action '{action}'; use a canonical action or a plain-English step")
        return
    row = {"action": keyword, "value": ""}
    if selector:
        try:
            row["selector"] = field_locator(selector)
        except ImportProblem as exc:
            preview.error(line, exc)
            return
    needs_locator = keyword not in {"navigate", "press", "assert_url", "assert_title"}
    if needs_locator and not selector:
        preview.error(line, f"{keyword} needs a selector or object name")
        return
    if keyword in {"navigate", "press", "assert_url", "assert_title", "assert_count", "assert_text",
                   "assert_contains", "assert_value"} or value:
        row["value"] = value or expected if keyword.startswith("assert") else value
    if keyword.startswith("assert") and not row["value"] and expected:
        row["value"] = expected
    if keyword in {"assert_visible", "assert_hidden", "assert_enabled", "assert_disabled",
                   "assert_checked", "assert_unchecked", "click", "hover", "dblclick", "check", "uncheck",
                   "wait_visible", "wait_hidden"}:
        row["value"] = ""
    row["label"] = description or action
    if repeat not in (None, "", "1", 1):
        try:
            row["repeat_count"] = int(float(str(repeat)))
        except (TypeError, ValueError):
            preview.error(line, "repeat_count must be a whole number")
            return
    if expected and not keyword.startswith("assert"):
        preview.add(row, line)
        preview.add({"action": "assert_contains", "selector": {"kind": "css", "primary": "body"},
                     "value": expected, "label": "Verify expected result"}, line)
        return
    preview.add(row, line)


# --- plain English steps -----------------------------------------------------

def _english_rules():
    quoted = QUOTED
    return [
        # Fixed waits are refused: this project's rule is no fixed sleeps.
        (re.compile(rf"^(?:wait|pause|sleep)\s+(?:for\s+)?\d+\s*(?:s|sec|secs|second|seconds|ms|milliseconds|minutes?|mins?)?$", re.I),
         "fixed_wait"),
        (re.compile(rf"^(?:wait|wait\s+for)\s+(?:the\s+)?(?P<field>.+?)\s+to\s+(?:appear|be\s+visible|be\s+displayed|load|be\s+present|be\s+enabled)$", re.I),
         "wait_visible"),
        (re.compile(rf"^(?:wait|wait\s+for)\s+(?:the\s+)?(?P<field>.+?)\s+to\s+(?:disappear|be\s+hidden|close|not\s+be\s+present)$", re.I),
         "wait_hidden"),
        (re.compile(rf"^(?:upload|attach|choose\s+file)\s+(?P<value>.+?)\s+(?:to|into|in)\s+(?:the\s+)?(?P<field>.+)$", re.I),
         "upload"),
        (re.compile(rf"^(?:capture|store|save|read|remember|fetch)\s+(?:the\s+)?(?P<field>.+?)(?:\s+(?:value|text|content))?\s+(?:as|into|in)\s+(?P<value>[\w.\-{{}}]+)$", re.I),
         "capture_text"),
        (re.compile(rf"^(?:press|hit|send|strike)\s+(?:the\s+)?(?P<value>enter|return|tab|escape|esc|space|spacebar|backspace|delete|del|up|down|left|right|home|end|page\s*up|page\s*down|f\d{{1,2}}|control\+[a-z]|ctrl\+[a-z])?(?:\s+key)?$", re.I),
         "press"),
        (re.compile(rf"^(?:uncheck|untick|deselect|clear)\s+(?:the\s+)?(?P<field>.+?)(?:\s+checkbox|\s+check\s*box)?$", re.I),
         "uncheck"),
        (re.compile(rf"^(?:check|tick)\s+(?:the\s+)?(?P<field>.+?)(?:\s+checkbox|\s+check\s*box)?$", re.I),
         "check"),
        (re.compile(rf"^(?:select|choose|pick)\s+(?P<value>{quoted}|\S+)\s+from\s+(?:the\s+)?(?P<field>.+?)(?:\s+(?:dropdown|list|menu|combo\s*box|combobox|select|field))?$", re.I),
         "select_option"),
        (re.compile(rf"^(?:set|enter|type|provide)\s+(?:the\s+)?(?P<field>.+?)\s+(?:as|to|=)\s+(?P<value>{quoted}|\S.*)$", re.I),
         "type_by_field"),
        (re.compile(rf"^(?:fill|populate)\s+(?:in\s+)?(?:the\s+)?(?P<field>.+?)\s+(?:with|using)\s+(?P<value>{quoted}|.+)$", re.I),
         "type_by_field"),
        (re.compile(rf"^{INPUT_WORDS}\s+(?:the\s+value\s+)?(?P<value>{quoted}|\S+)\s+(?:in|into|to|on)\s+(?:the\s+)?(?P<field>.+)$", re.I),
         "type_by_field"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?the\s+(?:url|address|location)\s+(?:is|equals?|contains?|includes?)\s+(?P<value>.+)$", re.I),
         "assert_url"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?the\s+(?:page\s+)?title\s+(?:is|equals?|contains?|includes?)\s+(?P<value>.+)$", re.I),
         "assert_title"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?the\s+(?:page|screen|form|table|app(?:lication)?)\s+"
                    rf"(?:contains?|shows?|displays?|has)\s+(?P<value>.+)$", re.I),
         "assert_page_contains"),
        (re.compile(rf"^(?:the\s+)?(?P<field>.+?)\s+should\s+(?:contain|include)\s+(?P<value>.+)$", re.I),
         "assert_contains"),
        (re.compile(rf"^(?:the\s+)?(?P<field>.+?)\s+should\s+(?:equal|be\s+equal\s+to|be|show|display|match)\s+(?P<value>.+)$", re.I),
         "assert_equals"),
        (re.compile(rf"^(?:the\s+)?(?P<field>.+?)\s+should\s+(?:not\s+be\s+visible|be\s+hidden|disappear)$", re.I),
         "assert_hidden"),
        (re.compile(rf"^(?:the\s+)?(?P<field>.+?)\s+should\s+(?:be\s+visible|be\s+displayed|appear|be\s+(?:present|shown))$", re.I),
         "assert_visible"),
        (re.compile(rf"^(?:the\s+)?(?P<field>.+?)\s+should\s+be\s+(?P<state>enabled|disabled|checked|unchecked|selected)$", re.I),
         "assert_state_phrase"),
        (re.compile(rf"^the\s+(?:page\s+)?(?:message|text|label|heading|error|banner)\s+(?P<value>.+?)\s+is\s+(?:displayed|shown|visible)$", re.I),
         "assert_text_visible"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?(?:the\s+)?(?P<field>.+?)\s+(?:should\s+)?is\s+"
                    rf"(?P<state>visible|displayed|hidden|enabled|disabled|checked|unchecked|present|not\s+present|absent|empty|not\s+empty)$", re.I),
         "assert_state_phrase"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?(?:the\s+)?(?P<field>.+?)\s+(?:should\s+)?"
                    rf"(?:contains?|includes?)\s+(?P<value>.+)$", re.I),
         "assert_contains"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?(?:the\s+)?(?P<field>.+?)\s+(?:should\s+)?"
                    rf"(?:equals?|is|matches|has\s+value|has\s+text)\s+(?P<value>.+)$", re.I),
         "assert_equals"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?(?P<field>.+?)\s+(?:is\s+)?(?:displayed|visible|shown|present)$", re.I),
         "assert_visible"),
        (re.compile(rf"^{ASSERT_WORDS}\s+(?:that\s+)?(?P<value>.+?)\s+(?:is|are)\s+(?:displayed|visible|shown)$", re.I),
         "assert_text_visible"),
        (re.compile(rf"^(?:double[\s-]?click|dblclick)\s+(?:on\s+)?(?:the\s+)?(?P<field>.+)$", re.I),
         "dblclick"),
        (re.compile(rf"^(?:hover|mouse\s+over|move\s+to)\s+(?:over\s+)?(?:the\s+)?(?P<field>.+)$", re.I),
         "hover"),
        (re.compile(rf"^{NAV_WORDS}\s+(?:the\s+)?(?:url\s+|page\s+|application\s+|app\s+|site\s+|website\s+|link\s+)?"
                    rf"(?P<value>https?://\S+|www\.\S+|/\S*|\S+\.\w{{2,5}}(?:/\S*)?|\S*\{{\{{\w+\}}\}}\S*)$", re.I),
         "navigate"),
        (re.compile(rf"^{NAV_WORDS}\s+(?:the\s+)?(?:url|page|application|app|site|website|browser|screen)$", re.I),
         "navigate_bare"),
        # "click the Login button" / "click Login" / "tap on 'Continue' link"
        (re.compile(rf"^{CLICK_WORDS}\s+(?:on\s+)?(?:the\s+)?(?P<field>.+?)(?:\s+(?P<role>button|link|tab|menu|menuitem|icon|option|item|checkbox|radio|switch|heading|dropdown))?$", re.I),
         "click"),
    ]


ENGLISH_RULES = _english_rules()


def parse_english_line(preview, raw, line_no, source="text"):
    """Convert one plain-English step line into a canonical step. True when handled."""
    text = strip_numbering(raw)
    if not text:
        return True
    if re.fullmatch(r"(?:note|comment|remark|hint|tip|info|assumption|precondition|given|background)\b[\s:].*", text, re.I):
        preview.note(line_no, f"{source}: {text[:200]}")
        return True
    for pattern, kind in ENGLISH_RULES:
        match = pattern.fullmatch(text)
        if not match:
            continue
        groups = match.groupdict()
        try:
            if kind == "fixed_wait":
                preview.error(line_no, "Fixed waits are not imported (this project runs without fixed sleeps); "
                                       "use a wait_visible / wait_hidden step instead")
                return True
            if kind in {"check", "uncheck", "hover", "dblclick"}:
                preview.add({"action": kind, "selector": field_locator(groups["field"]),
                             "label": text}, line_no)
                return True
            if kind == "click":
                preview.add({"action": "click", "selector": field_locator(groups["field"], groups.get("role")),
                             "label": text}, line_no)
                return True
            if kind == "dblclick":
                preview.add({"action": "dblclick", "selector": field_locator(groups["field"]), "label": text}, line_no)
                return True
            if kind == "press":
                key = clean_value(groups.get("value") or "")
                if not key:
                    preview.error(line_no, "Name the key to press, e.g. 'press Enter' or 'press the Tab key'")
                    return True
                preview.add({"action": "press", "value": key.title().replace("Control+", "Control+"),
                             "label": text}, line_no)
                return True
            if kind == "select_option":
                preview.add({"action": "select_option", "value": clean_value(groups["value"]),
                             "selector": {**field_locator(groups["field"], "combobox"), "by": "label"},
                             "label": text}, line_no)
                return True
            if kind == "type_by_field":
                preview.add({"action": "fill", "value": clean_value(groups["value"]),
                             "selector": field_locator(groups["field"], "input"), "label": text}, line_no)
                return True
            if kind in {"assert_contains", "assert_equals"}:
                target = clean_value(groups.get("field") or "")
                expected = clean_value(groups.get("value") or "")
                action = "assert_contains" if kind == "assert_contains" else "assert_text"
                if not expected:
                    preview.error(line_no, "An expected value is required for the assertion")
                    return True
                preview.add({"action": action, "value": expected,
                             "selector": field_locator(target) if target else {"kind": "css", "primary": "body"},
                             "label": text}, line_no)
                return True
            if kind == "assert_page_contains":
                preview.add({"action": "assert_contains", "value": clean_value(groups["value"]),
                             "selector": {"kind": "css", "primary": "body"}, "label": text}, line_no)
                return True
            if kind in {"assert_visible", "assert_hidden", "assert_state_phrase"}:
                state = (groups.get("state") or "").strip().lower()
                action = {"visible": "assert_visible", "displayed": "assert_visible",
                          "hidden": "assert_hidden", "enabled": "assert_enabled",
                          "disabled": "assert_disabled", "checked": "assert_checked",
                          "selected": "assert_checked", "unchecked": "assert_unchecked"}.get(state)
                if action is None:
                    if kind == "assert_visible":
                        action = "assert_visible"
                    elif kind == "assert_hidden":
                        action = "assert_hidden"
                    else:
                        preview.error(line_no, f"Unsupported state '{state}'")
                        return True
                preview.add({"action": action, "selector": field_locator(groups["field"]), "label": text}, line_no)
                return True
            if kind == "assert_text_visible":
                preview.add({"action": "assert_visible",
                             "selector": {"kind": "text", "primary": clean_value(groups["value"]), "exact": False},
                             "label": text}, line_no)
                return True
            if kind in {"assert_url", "assert_title"}:
                expected = clean_value(groups["value"])
                preview.add({"action": kind, "value": expected, "label": text}, line_no)
                return True
            if kind == "navigate":
                preview.add({"action": "navigate", "value": _navigate_value(groups["value"]), "label": text}, line_no)
                return True
            if kind == "navigate_bare":
                preview.add({"action": "navigate", "value": "{{base_url}}", "label": text}, line_no)
                preview.note(line_no, "No URL in this step; imported as {{base_url}} -- set base_url in the Variables tab")
                return True
            if kind == "capture_text":
                name = clean_value(groups["value"])
                if not re.fullmatch(r"[\w.\-]{1,100}", name):
                    preview.error(line_no, f"'{name}' is not a valid variable name")
                    return True
                preview.add({"action": "capture_text", "value": name,
                             "selector": field_locator(groups["field"]), "label": text}, line_no)
                return True
            if kind == "wait_visible" or kind == "wait_hidden":
                preview.add({"action": kind, "selector": field_locator(groups["field"]), "label": text}, line_no)
                return True
            if kind == "upload":
                preview.error(line_no, "File uploads must carry the file contents; add this step in the "
                                       "Actions panel instead so the fixture is stored with the step")
                return True
        except ImportProblem as exc:
            preview.error(line_no, exc)
            return True
    return False


def parse_english(preview, text, source="text", require_all=True):
    """Parse a plain-text test script: numbered steps, bullets, actions, notes."""
    pending_expected = None
    handled = 0
    seen = 0
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped in {"---", "***", "___"} or set(stripped) <= {"-", "=", "_", "|", "+", " "}:
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            preview.note(line_no, f"{source}: table row kept as a note; upload the table itself to import it as steps")
            continue
        if stripped.startswith(("#", "//", "//!", "<!--")) and not stripped.startswith("### "):
            preview.note(line_no, f"{source}: {stripped[:200]}")
            continue
        # "Expected result: ..." pairs with the step above (Azure DevOps style).
        expected = re.fullmatch(r"(?:expected\s+result|expected|expected output|result)\s*[:\-]\s*(.+)", stripped, re.I)
        if expected:
            if pending_expected is not None:
                value = clean_value(expected.group(1))
                if value:
                    preview.add({"action": "assert_contains", "value": value,
                                 "selector": {"kind": "css", "primary": "body"},
                                 "label": "Verify expected result"}, line_no)
                pending_expected = None
            else:
                preview.note(line_no, f"{source}: expected result without a step: {stripped[:160]}")
            continue
        header = re.fullmatch(r"(?:test\s*case|test|scenario|case|title|feature|objective|description)\s*[:\-]\s*(.+)", stripped, re.I)
        if header:
            preview.name = clean_value(header.group(1))[:200] or preview.name
            continue
        if re.fullmatch(r"(?:pre-?conditions?|pre-?requisites?|assumptions?|test\s+data|environment|priority|severity|tags?)\s*[:\-]\s*(.*)", stripped, re.I):
            preview.note(line_no, f"{source}: {stripped[:200]}")
            continue
        if re.fullmatch(r"(?:steps?|test steps?|test case)\s*:?", stripped, re.I):
            continue
        seen += 1
        if parse_english_line(preview, stripped, line_no, source):
            handled += 1
            pending_expected = True
        else:
            preview.error(line_no, "Not a supported step: '%s'. Rewrite it as one action, e.g. "
                                   "'Click the Login button', 'Enter {{user}} in the Username field', "
                                   "'Verify the page contains Welcome'" % stripped[:120])
    if require_all and seen and handled == 0:
        preview.error(1, "No supported steps were found in this file")
    return handled


# --- specific market formats -------------------------------------------------

SIDE_COMMANDS = {
    "open": ("navigate", None), "click": ("click", None), "clickat": ("click", None),
    "clickandwait": ("click", None), "mouseover": ("hover", None), "doubleclick": ("dblclick", None),
    "type": ("type", "value"), "sendkeys": ("type", "value"), "typekeys": ("type", "value"),
    "check": ("check", None), "uncheck": ("uncheck", None),
    "select": ("select_option", "value"), "selectoption": ("select_option", "value"),
    "selectlabel": ("select_option", "value"), "selectbytext": ("select_option", "value"),
    "selectvalue": ("select_option", "value"),
    "asserttext": ("assert_contains", "value"), "verifytext": ("assert_contains", "value"),
    "asserttitle": ("assert_title", "value"), "verifytitle": ("assert_title", "value"),
    "assertlocation": ("assert_url", "value"), "verifylocation": ("assert_url", "value"),
    "asserturl": ("assert_url", "value"),
    "assertelementpresent": ("assert_visible", None), "verifyelementpresent": ("assert_visible", None),
    "assertelementnotpresent": ("assert_hidden", None), "verifyelementnotpresent": ("assert_hidden", None),
    "assertvalue": ("assert_value", "value"), "verifyvalue": ("assert_value", "value"),
    "waitforelementvisible": ("wait_visible", None),
    "waitforelementnotvisible": ("wait_hidden", None),
    "selectframe": ("switch_frame", None), "storetext": ("capture_text", "value"),
    "storetitle": ("capture_title", "value"),
}
SIDE_IGNORED = {"pause", "waitforpageload", "echo", "comment", "setwindowsize", "setwindowsize",
                "setspeed", "settimeout", "store", "storevalue", "storeattribute", "storerandom",
                "runscript", "executescript", "waitforcondition", "waitfortext", "waitforvalue",
                "assertspeed", "verifychecked"}


def parse_selenium_side(preview, obj):
    """Selenium IDE .side project: the first test's commands become steps."""
    tests = obj.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ImportProblem("A .side file must contain a tests list")
    usable = [t for t in tests if isinstance(t, dict) and isinstance(t.get("commands"), list)]
    if not usable:
        raise ImportProblem("The .side file has no commands to import")
    if len(usable) > 1:
        preview.note(1, "This .side project holds %d tests; the first one is imported. "
                        "Export one test per file to import the others." % len(usable))
    test = usable[0]
    if test.get("name"):
        preview.name = str(test["name"])[:200]
    for index, command in enumerate(test["commands"], 1):
        if not isinstance(command, dict):
            preview.error(index, "Command must be an object")
            continue
        name = str(command.get("command") or "").strip().lower()
        target = str(command.get("target") or "").strip()
        value = str(command.get("value") or "").strip()
        if name == "pause":
            preview.error(index, "Fixed sleeps are not imported (this project runs without fixed sleeps); "
                                 "replace the pause with a wait_visible / wait_hidden step")
            continue
        if name in SIDE_IGNORED or name.startswith("store"):
            preview.note(index, f"Selenium command '{name}' skipped: no browser step")
            continue
        if name not in SIDE_COMMANDS:
            preview.error(index, f"Unsupported Selenium IDE command: {name}")
            continue
        action, value_source = SIDE_COMMANDS[name]
        if action in {"switch_frame", "capture_title"}:
            preview.note(index, f"Selenium command '{name}' has no canonical equivalent yet; "
                                f"add it with the Actions panel")
            continue
        row = {"action": action, "value": value if value_source else "",
               "label": f"{name}: {target or value}"[:500]}
        if action == "navigate":
            row["value"] = _navigate_value(target or value)
        elif action == "press":
            row["value"] = value or target
        elif target and action not in {"assert_url", "assert_title", "assert_value"}:
            row["selector"] = field_locator(target)
        if value_source == "value" and value:
            row["value"] = value
        if action in {"assert_url", "assert_title"} and not row["value"]:
            row["value"] = value or target
        preview.add(row, index)
    return True


POSTMAN_ASSERT_STATUS = re.compile(r"to\.have\.status\(\s*(\d{3})\s*\)")
POSTMAN_ASSERT_JSON = re.compile(r"pm\.response\.json\(\)\s*\.\s*([\w.\[\]]+)[^;]*?to\.(?:eql|equal|deep\.equal)\(\s*['\"]([^'\"]*)['\"]\s*\)")


def _postman_requests(items, depth=0):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("item"), list):
            yield from _postman_requests(item["item"], depth + 1)
            continue
        request = item.get("request")
        if isinstance(request, dict):
            yield item


def parse_postman(preview, obj):
    """Postman collection v2: each request becomes an api_request step."""
    items = list(_postman_requests(obj.get("item")))
    if not items:
        raise ImportProblem("This JSON is not a Postman collection with requests")
    if obj.get("info", {}).get("name"):
        preview.name = str(obj["info"]["name"])[:200]
    for index, item in enumerate(items, 1):
        request = item["request"]
        url = request.get("url")
        raw = url.get("raw") if isinstance(url, dict) else str(url or "")
        raw = str(raw or "").strip()
        if not raw:
            preview.error(index, "Request has no URL")
            continue
        script = " ".join(
            " ".join(event.get("script", {}).get("exec") or [])
            for event in (item.get("event") or []) if isinstance(event, dict)
        )
        selector = {"method": str(request.get("method") or "GET").upper()}
        status = POSTMAN_ASSERT_STATUS.search(script)
        if status:
            selector["status"] = int(status.group(1))
        expected = POSTMAN_ASSERT_JSON.search(script)
        if expected:
            selector["json_path"] = expected.group(1).strip(".")
            selector["expected"] = expected.group(2)
        body = request.get("body")
        if isinstance(body, dict) and body.get("mode") in {"raw", "urlencoded"} and body.get("raw"):
            selector["body"] = str(body["raw"])
        headers = {}
        for header in request.get("header") or []:
            if isinstance(header, dict) and header.get("key") and not header.get("disabled"):
                headers[str(header["key"])] = str(header.get("value") or "")
        if headers:
            selector["headers"] = headers
        preview.add({"action": "api_request", "value": raw,
                     "selector": selector, "label": str(item.get("name") or raw)[:500]}, index)


JUNIT_TAGS = ("testcase", "test", "step")


def parse_xml(preview, data):
    """Canonical <step> XML, or JUnit/TestNG case names parsed as English steps."""
    try:
        root = SafeET.fromstring(data)
    except Exception as exc:  # defusedxml raises its own errors for hostile input
        raise ImportProblem(f"XML could not be read safely: {type(exc).__name__}") from exc
    steps = [node for node in root.iter() if str(node.tag).split("}")[-1] == "step"]
    if steps:
        for index, node in enumerate(steps, 1):
            attributes = {str(k): str(v) for k, v in node.attrib.items()}
            if not attributes.get("action"):
                preview.error(index, "Every <step> needs an action attribute")
                continue
            row = {"action": attributes.get("action"), "value": attributes.get("value", ""),
                   "label": attributes.get("label", ""), "selector": {}}
            for key in ("selector", "primary", "locator", "kind", "name", "by"):
                if attributes.get(key):
                    row["selector"][key if key != "locator" else "primary"] = attributes[key]
            if attributes.get("selector") or attributes.get("locator"):
                row["selector"].pop("selector", None)
            if not row["selector"] and node.text and str(node.text).strip():
                row["selector"] = {"primary": str(node.text).strip()}
            if isinstance(row["selector"].get("primary"), str) and attributes.get("kind") == "text":
                row["selector"]["exact"] = False
            preview.add(row, index)
        return True
    cases = []
    for node in root.iter():
        tag = str(node.tag).split("}")[-1]
        if tag in JUNIT_TAGS and node.attrib.get("name"):
            cases.append(str(node.attrib["name"]))
    if not cases:
        raise ImportProblem("No <step> elements and no test case names found in this XML")
    preview.note(1, "XML carries no step detail, so each test case name is read as a step. "
                    "Anything unclear is reported below.")
    for index, name in enumerate(cases[:MAX_STEPS], 1):
        if not parse_english_line(preview, name, index, "xml"):
            preview.error(index, f"Test case name '{name[:120]}' is not a step; import the real script for this case")
    return True


ROBOT_VARIABLE = re.compile(r"\$\{([^}]+)\}")

# A relative target in any format anchors to the project's base_url variable,
# which is how the canonical template already expresses "open the application".
def _navigate_value(raw):
    value = clean_value(raw)
    if value.startswith(("/", "./", "../")) and not value.startswith("//"):
        return "{{base_url}}" + (value[1:] if value.startswith("/.") else value)
    return value


def _robot_value(text):
    text = ROBOT_VARIABLE.sub(r"{{\1}}", str(text or ""))
    return clean_value(text)


def parse_robot(preview, text):
    """Robot Framework suite: Test Cases keywords become steps."""
    keywords = {
        "open browser": ("navigate", "url"),
        "go to": ("navigate", "url"),
        "new page": ("navigate", "url"),
        "click element": ("click", "locator"),
        "click button": ("click", "locator"),
        "click link": ("click", "locator"),
        "click": ("click", "locator"),
        "input text": ("fill", "locator_value"),
        "input password": ("fill", "locator_value"),
        "input": ("fill", "locator_value"),
        "fill text": ("fill", "locator_value"),
        "select from list by label": ("select_option", "locator_value"),
        "select from list by value": ("select_option", "locator_value"),
        "select radio button": ("check", "locator"),
        "select checkbox": ("check", "locator"),
        "unselect checkbox": ("uncheck", "locator"),
        "press keys": ("press", "optional_press"),
        "page should contain": ("assert_contains", "body_value"),
        "page should contain element": ("assert_visible", "locator"),
        "page should not contain": ("assert_hidden", "body_value"),
        "location should be": ("assert_url", "value"),
        "location should contain": ("assert_url", "value"),
        "title should be": ("assert_title", "value"),
        "element should be visible": ("assert_visible", "locator"),
        "element should not be visible": ("assert_hidden", "locator"),
        "element should be enabled": ("assert_enabled", "locator"),
        "element should be disabled": ("assert_disabled", "locator"),
        "element text should be": ("assert_text", "locator_value"),
        "element should contain": ("assert_contains", "locator_value"),
        "wait until element is visible": ("wait_visible", "locator"),
        "wait until element is not visible": ("wait_hidden", "locator"),
        "capture page screenshot": ("noop", None),
        "close browser": ("noop", None),
        "close all browsers": ("noop", None),
        "maximize browser window": ("noop", None),
    }
    variables = {}
    section = None
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.strip().startswith("***"):
            title = line.strip().strip("*").strip().lower()
            section = "variables" if title.startswith("variable") else ("cases" if "test" in title else "other")
            continue
        if section == "variables":
            cells = [cell.strip() for cell in re.split(r"\s{2,}|\t", line.strip()) if cell.strip()]
            if len(cells) >= 2:
                name = ROBOT_VARIABLE.sub("", cells[0]).strip() or cells[0].strip()
                variables[re.sub(r"[^\w.\-]+", "_", name)] = cells[1]
            continue
        if section != "cases":
            continue
        cells = [cell.strip() for cell in re.split(r"\s{2,}|\t", line.strip()) if cell.strip()]
        if not cells:
            continue
        if line.startswith(" ") or line.startswith("\t"):
            # A continuation/settings line inside a test case ([Tags], [Teardown]).
            if cells[0].startswith("["):
                preview.note(line_no, f"Robot setting {cells[0]} noted, not imported")
                continue
        if len(cells) == 1 and line.strip() == cells[0] and not line.startswith(" "):
            preview.name = cells[0][:200]
            continue
        keyword = cells[0].strip().lower()
        arguments = cells[1:]
        if keyword.startswith("[") or keyword in {"given", "when", "then", "and", "but"}:
            continue
        if keyword not in keywords:
            if len(cells) >= 2:
                handled = parse_english_line(preview, " ".join(cells), line_no, "robot")
                if handled:
                    continue
            preview.error(line_no, f"Unsupported Robot keyword '{cells[0]}'; convert it to a canonical step "
                                   f"or a plain-English line")
            continue
        action, shape = keywords[keyword]
        if action == "noop":
            preview.note(line_no, f"Robot keyword '{cells[0]}' has no browser step; noted only")
            continue
        row = {"action": action, "value": "", "label": " ".join(cells)[:500]}
        try:
            if shape == "url":
                row["value"] = _navigate_value(_robot_value(arguments[0] if arguments else ""))
            elif shape == "value":
                row["value"] = _robot_value(arguments[0] if arguments else "")
            elif shape == "body_value":
                row["value"] = _robot_value(" ".join(arguments))
                row["selector"] = {"kind": "css", "primary": "body"}
            elif shape == "locator":
                row["selector"] = field_locator(_robot_value(arguments[0] if arguments else ""))
            elif shape == "locator_value":
                row["selector"] = field_locator(_robot_value(arguments[0] if arguments else ""))
                row["value"] = _robot_value(" ".join(arguments[1:]))
                if action == "select_option":
                    row["selector"]["by"] = "value" if "value" in keyword else "label"
            elif shape == "optional_press":
                row["value"] = _robot_value(" ".join(arguments[1:]) or (arguments[0] if arguments else "Enter"))
                if len(arguments) >= 2:
                    row["selector"] = field_locator(_robot_value(arguments[0]))
                    row["action"] = "press"
            if action == "select_option" and not row["value"]:
                preview.error(line_no, "select_option needs a value")
                continue
            preview.add(row, line_no)
        except ImportProblem as exc:
            preview.error(line_no, exc)
    if variables:
        preview.suggest_variables(variables)
    return True


CYPRESS_TARGET = re.compile(r"cy\.(?:get|contains)\(\s*(['\"])(.*?)\1\s*\)")
CYPRESS_VISIT = re.compile(r"cy\.visit\(\s*(['\"])(.*?)\1")
CYPRESS_COMMAND = re.compile(
    r"\.(click|dblclick|type|clear|check|uncheck|focus|hover|trigger|select|should|contains|rightclick)\s*\(([^)]*)\)")
CYPRESS_URL = re.compile(r"cy\.url\(\)\s*\.should\(\s*(['\"])(include|contain|eq|equal)\1\s*,\s*(['\"])(.*?)\3")
CYPRESS_TITLE = re.compile(r"cy\.title\(\)\s*\.should\(\s*(['\"])(include|contain|eq|equal)\1\s*,\s*(['\"])(.*?)\3")
CYPRESS_SHOULD = re.compile(r"(?:be\.visible|exist|not\.exist|contain|have\.text|have\.value|be\.checked|be\.disabled|be\.enabled)")


def parse_cypress(preview, text, source="cypress"):
    """Cypress spec subset: cy.visit / cy.get / cy.contains chains and assertions."""
    current = None
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("/*") or line.startswith("*"):
            continue
        found = False
        visit = CYPRESS_VISIT.search(line)
        if visit:
            preview.add({"action": "navigate", "value": _navigate_value(visit.group(2)),
                         "label": f"Open {visit.group(2)}"}, line_no)
            found = True
        target = CYPRESS_TARGET.search(line)
        if target:
            current = (target.group(2), "contains" if "contains(" in target.group(0) else "get")
            found = True
        url_assert = CYPRESS_URL.search(line)
        if url_assert:
            preview.add({"action": "assert_url", "value": clean_value(url_assert.group(4)),
                         "label": line[:500]}, line_no)
            found = True
        title_assert = CYPRESS_TITLE.search(line)
        if title_assert:
            preview.add({"action": "assert_title", "value": clean_value(title_assert.group(4)),
                         "label": line[:500]}, line_no)
            found = True
        for command, arguments in CYPRESS_COMMAND.findall(line):
            if command == "contains":
                # cy.get('button').contains('Sign in').click(): the contained text
                # becomes the target for whatever is chained after it.
                literal = re.search(r"['\"]([^'\"]*)['\"]", arguments)
                if literal:
                    current = (clean_value(literal.group(1)), "contains")
                    found = True
                continue
            if command == "should" and target is None and current is None:
                continue
            if command == "should":
                mapping = {"be.visible": "assert_visible", "exist": "assert_visible",
                           "not.exist": "assert_hidden", "contain": "assert_contains",
                           "have.text": "assert_text", "have.value": "assert_value",
                           "be.checked": "assert_checked", "be.disabled": "assert_disabled",
                           "be.enabled": "assert_enabled"}
                for key, action in mapping.items():
                    if key in arguments:
                        row = {"action": action, "value": "", "label": line[:500]}
                        if key in {"contain", "have.text", "have.value"}:
                            literal = re.search(r"['\"]([^'\"]*)['\"]", arguments.split(",")[-1])
                            row["value"] = clean_value(literal.group(1)) if literal else ""
                        if current:
                            row["selector"] = ({"kind": "text", "primary": current[0], "exact": False}
                                               if current[1] == "contains" else field_locator(current[0]))
                        elif key in {"contain", "have.text"}:
                            row["selector"] = {"kind": "css", "primary": "body"}
                        else:
                            preview.error(line_no, f"'{arguments}' has no element to assert on")
                            found = True
                            break
                        preview.add(row, line_no)
                        found = True
                        break
                continue
            if current is None:
                preview.error(line_no, f"cy.{command}() has no preceding cy.get()/cy.contains() target")
                found = True
                continue
            selector = ({"kind": "text", "primary": current[0], "exact": False} if current[1] == "contains"
                        else field_locator(current[0]))
            action = {"click": "click", "dblclick": "dblclick", "type": "type", "clear": "fill",
                      "check": "check", "uncheck": "uncheck", "focus": "click", "hover": "hover",
                      "trigger": "click", "select": "select_option", "rightclick": "click"}.get(command)
            literal = re.search(r"['\"]([^'\"]*)['\"]", arguments)
            if action == "type":
                preview.add({"action": "type", "value": clean_value(literal.group(1)) if literal else "",
                             "selector": selector, "label": line[:500]}, line_no)
            elif action == "fill":
                preview.add({"action": "fill", "value": "", "selector": selector, "label": line[:500]}, line_no)
            elif action == "select_option":
                preview.add({"action": "select_option", "value": clean_value(literal.group(1)) if literal else "",
                             "selector": {**selector, "by": "label"}, "label": line[:500]}, line_no)
            elif action:
                preview.add({"action": action, "selector": selector, "label": line[:500]}, line_no)
            else:
                preview.error(line_no, f"Unsupported Cypress command: {command}")
            found = True
        if not found:
            if re.match(r"^(?:describe|it|before|after|beforeEach|afterEach|context)\s*\(", line):
                name = re.search(r"['\"]([^'\"]+)['\"]", line)
                if name and re.match(r"^(?:it|describe)\s*\(", line):
                    preview.name = clean_value(name.group(1))[:200]
                continue
            if re.match(r"^(?:import|const|let|require)\b", line) or re.match(r"^\}\)?\s*;?$", line):
                continue
            preview.error(line_no, "Unsupported Cypress statement: " + line[:160])
    return True


JAVA_LOCATOR = re.compile(r"By\.(id|name|className|cssSelector|xpath|linkText|partialLinkText|tagName|tag)"
                          r"\(\s*(['\"])(.*?)\2\s*\)", re.I)
SELENIUM_VALUE = re.compile(r"\.(sendKeys|type|SendKeys|Type)\(\s*(['\"])(.*?)\2\s*\)")


def parse_selenium_code(preview, text, language="java"):
    """Selenium Java/C# subset. Line oriented, static, no compilation."""
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("*") or line.startswith("/*"):
            continue
        if re.search(r"\bThread\.sleep\s*\(|\bSleep\s*\(", line):
            preview.error(line_no, "Fixed sleeps are not imported (this project runs without fixed sleeps)")
            continue
        if re.match(r"^(?:package|import|public|private|protected|class|namespace|using|\[|@|\})", line):
            continue
        if not re.search(r"(?:driver|Driver|page|Page)\s*\.", line):
            continue
        try:
            current = None
            locator = JAVA_LOCATOR.search(line)
            if locator:
                current = field_locator(locator.group(3))
            navigation = re.search(r"(?:get|navigate\(\)\s*\.\s*to|GoToUrl|Navigate\(\)\.GoToUrl)\(\s*(['\"])(.*?)\1\s*\)", line)
            if navigation and ("driver.get" in line or "Navigate" in line or "to(" in line):
                preview.add({"action": "navigate", "value": _navigate_value(navigation.group(2)),
                             "label": f"Open {navigation.group(2)}"}, line_no)
                continue
            if locator and re.search(r"\.click\s*\(|\.Click\s*\(", line):
                preview.add({"action": "click", "selector": current, "label": f"Click {current['primary']}"}, line_no)
                continue
            typed = SELENIUM_VALUE.search(line)
            if locator and typed:
                preview.add({"action": "type", "value": clean_value(typed.group(3)), "selector": current,
                             "label": f"Type {typed.group(3)}"}, line_no)
                continue
            if locator and re.search(r"\.clear\s*\(|\.Clear\s*\(", line):
                preview.add({"action": "fill", "value": "", "selector": current, "label": "Clear field"}, line_no)
                continue
            if re.search(r"selectByVisibleText|SelectByText|selectByText", line):
                literal = re.search(r"\(\s*(['\"])(.*?)\1\s*\)", line)
                if current and literal:
                    preview.add({"action": "select_option", "value": clean_value(literal.group(2)),
                                 "selector": {**current, "by": "label"}, "label": "Select option"}, line_no)
                    continue
            assertion = re.search(r"(?:assertEquals|AreEqual|assertThat|TextShouldBe)\(\s*(?:[^,]+,\s*)?(['\"])(.*?)\1", line)
            if assertion:
                expected = clean_value(assertion.group(2))
                if locator:
                    preview.add({"action": "assert_text", "value": expected, "selector": current,
                                 "label": f"Assert text {expected}"}, line_no)
                else:
                    preview.add({"action": "assert_contains", "value": expected,
                                 "selector": {"kind": "css", "primary": "body"},
                                 "label": f"Assert page contains {expected}"}, line_no)
                continue
            if re.search(r"assertTrue|IsTrue|assertVisible|Displayed", line) and locator:
                preview.add({"action": "assert_visible", "selector": current, "label": "Assert visible"}, line_no)
                continue
            if re.search(r"switchTo\(\)\.frame|SwitchTo\(\)\.Frame", line):
                preview.note(line_no, "Frame switching has no canonical step yet; add it with the Actions panel")
                continue
            preview.error(line_no, "Unsupported Selenium statement: " + line[:160] +
                          " (only driver.get, findElement+click/sendKeys, Select, simple asserts are imported)")
        except ImportProblem as exc:
            preview.error(line_no, exc)
    return True


ROBOT_SECTION_HINT = re.compile(r"\*\*\*\s*test\s*cases?\s*\*\*\*", re.I)
GHERKIN_HINT = re.compile(r"^\s*(?:Feature|Scenario|Background)\s*:", re.M)
PLAYWRIGHT_PY_HINT = re.compile(r"(?:from\s+playwright|def\s+test_|sync_playwright|expect\()")
PLAYWRIGHT_JS_HINT = re.compile(r"(?:from\s+['\"]@playwright/test['\"]|require\(['\"]@playwright/test['\"]\)|await\s+page\.|page\.getBy)")
CYPRESS_HINT = re.compile(r"cy\s*\.\s*(?:visit|get|contains|url|title|intercept|window)\s*\(", re.M)
SELENIUM_JAVA_HINT = re.compile(r"(?:By\.|WebDriver|driver\.findElement|ChromeDriver)", re.M)
SELENIUM_CS_HINT = re.compile(r"(?:IWebDriver|IWebElement|FindElement\s*\(|OpenQA\.Selenium)", re.M)
ROBOT_PLAIN_HINT = re.compile(r"^{}\s*$|^\[(?:Documentation|Tags|Setup|Teardown)\]", re.M)


def sniff(text):
    """Detect what a text body is. Deterministic, offline, and explainable."""
    sample = text[:200_000]
    stripped = sample.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            json.loads(stripped)
            return {"format": "json", "why": "the body parses as JSON"}
        except ValueError:
            pass
    if stripped.startswith("<?xml") or stripped.startswith("<"):
        return {"format": "xml", "why": "the body starts with XML markup"}
    if stripped.startswith("pipeline {") or re.search(r"\bstage\s*\(\s*['\"]", sample):
        return {"format": "jenkins", "why": "the body declares a Jenkins pipeline"}
    if ROBOT_SECTION_HINT.search(sample):
        return {"format": "robot", "why": "the body has a '*** Test Cases ***' section"}
    if GHERKIN_HINT.search(sample):
        return {"format": "feature", "why": "the body uses Gherkin Feature/Scenario keywords"}
    if CYPRESS_HINT.search(sample):
        return {"format": "js", "why": "the body calls cy.* commands (Cypress)"}
    if PLAYWRIGHT_JS_HINT.search(sample):
        return {"format": "js", "why": "the body uses the Playwright JavaScript API"}
    if PLAYWRIGHT_PY_HINT.search(sample):
        return {"format": "py", "why": "the body uses the Playwright Python API"}
    if SELENIUM_CS_HINT.search(sample):
        return {"format": "cs", "why": "the body uses the Selenium .NET API"}
    if SELENIUM_JAVA_HINT.search(sample):
        return {"format": "java", "why": "the body uses the Selenium Java API"}
    if re.match(r"^\s*(?:---\s*$|\w[\w.\- ()]*\s*:\s*(?:$|\r?\n|\s+-|\s+\S))", sample) and re.search(r"^\s{2,}\w", sample, re.M):
        return {"format": "yaml", "why": "the body looks like YAML mappings"}
    lines = [line for line in sample.splitlines() if line.strip()]
    if lines:
        first = lines[0]
        cells = _split_smart(first)
        if len(cells) >= 2 and any(_normalise_header(cell) in HEADER_LOOKUP for cell in cells):
            return {"format": "table", "why": "the first row is a table header with test columns"}
        for index, line in enumerate(lines[:40]):
            if line.count("|") >= 2 and index + 1 < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[index + 1] or ""):
                return {"format": "table", "why": "the body is a Markdown/pipe table"}
            if line.count("|") >= 2 and any(_normalise_header(cell) in HEADER_LOOKUP for cell in _split_smart(line)):
                return {"format": "table", "why": "the body holds a pipe table with test columns"}
    return {"format": "text", "why": "no machine format matched, so it is read as a plain-English step list"}


def parse_yaml(preview, data):
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise ImportProblem("YAML support is not installed on this server (pip install PyYAML)") from exc
    try:
        loaded = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ImportProblem("YAML could not be read: " + str(exc)[:200]) from exc
    if loaded is None:
        raise ImportProblem("The YAML file is empty")
    if isinstance(loaded, dict) and "execution_plan" in loaded:
        from app.plan_runtime import normalize_plan
        plan = normalize_plan(loaded["execution_plan"])
        preview.execution_plan = plan
        steps = plan["steps"]
    elif isinstance(loaded, dict):
        steps = loaded.get("steps") or loaded.get("test") or loaded.get("tests")
    else:
        steps = loaded
    if isinstance(steps, str):
        parse_english(preview, steps, "yaml")
        return True
    if isinstance(steps, list) and steps and isinstance(steps[0], str):
        parse_english(preview, "\n".join(str(item) for item in steps), "yaml")
        return True
    if isinstance(loaded, dict):
        if loaded.get("name"):
            preview.name = str(loaded["name"])[:200]
        variables = loaded.get("variables")
        if isinstance(variables, dict):
            preview.suggest_variables({str(k): str(v) for k, v in variables.items()})
    if not isinstance(steps, list):
        raise ImportProblem("YAML must contain a steps list, a step-name list or a string of steps")
    for index, step in enumerate(steps, 1):
        if isinstance(step, str):
            if not parse_english_line(preview, step, index, "yaml"):
                preview.error(index, f"Unknown step: {step[:120]}")
        else:
            preview.add(step, index)
    return True


def parse_xls(preview, data):
    """Legacy .xls (BIFF) sheets, the format old test-management exports still use."""
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise ImportProblem("Legacy .xls support is not installed on this server (pip install xlrd)") from exc
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise ImportProblem("Legacy .xls could not be read: " + type(exc).__name__) from exc
    try:
        if book.nsheets != 1:
            raise ImportProblem("Use one worksheet per upload; multiple sheets must not be silently combined")
        sheet = book.sheet_by_index(0)
        if sheet.nrows > MAX_STEPS + 1 or sheet.ncols > 12:
            raise ImportProblem("Excel is limited to 500 steps and 12 columns")
        rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)]
    finally:
        book.release_resources()
    if not parse_table_smart(rows, preview, "xls"):
        parse_table(rows, preview)
    return True


def parse_docx(preview, data):
    """Word document: tables first (step tables), then numbered paragraphs."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) > 200 or sum(item.file_size for item in archive.infolist()) > MAX_UNPACKED:
            raise ImportProblem("Word document is too large when expanded")
        if any(name.endswith("vbaProject.bin") for name in names):
            raise ImportProblem("Macros are not accepted")
        if "word/document.xml" not in names:
            raise ImportProblem("This is not a readable Word (.docx) document; save it as .docx or .txt")
        xml = archive.read("word/document.xml")
    root = SafeET.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    tables, paragraphs = [], []
    for node in root.iter():
        tag = str(node.tag)
        if tag == namespace + "tbl":
            rows = []
            for row in node.iter(namespace + "tr"):
                cells = ["".join(text.text or "" for text in cell.iter(namespace + "t"))
                         for cell in row.iter(namespace + "tc")]
                rows.append([cell.strip() for cell in cells])
            tables.append(rows)
        elif tag == namespace + "p":
            text = "".join(part.text or "" for part in node.iter(namespace + "t")).strip()
            if text:
                paragraphs.append(text)
    for rows in tables:
        if len(rows) > 1 and parse_table_smart(rows, preview, "docx"):
            return True
    if not paragraphs:
        raise ImportProblem("The Word document has no readable text or tables")
    preview.note(1, "Read %d paragraphs from the Word document" % len(paragraphs))
    parse_english(preview, "\n".join(paragraphs), "docx")
    return True


def parse_pdf(preview, data):
    """PDF test scripts: extract the text layer, then read it as steps."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise ImportProblem("PDF support is not installed on this server (pip install pypdf)") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ImportProblem("PDF could not be read: " + type(exc).__name__) from exc
    if getattr(reader, "is_encrypted", False):
        raise ImportProblem("Encrypted PDFs are not accepted; export an unprotected copy")
    pages = reader.pages[:30]
    if not pages:
        raise ImportProblem("The PDF has no pages")
    text = "\n".join((page.extract_text() or "") for page in pages)
    if not text.strip():
        raise ImportProblem("The PDF has no text layer (it looks like a scan); export the test script as text")
    preview.note(1, "Read the text layer of %d PDF page(s); scanned images are not converted" % len(pages))
    if not parse_table_smart([_split_smart(line) for line in text.splitlines() if line.strip()], preview, "pdf"):
        parse_english(preview, text, "pdf")
    return True


# --- dispatch ----------------------------------------------------------------

def parse_extra(preview, filename, data):
    """Handle the formats the strict parser does not. Returns the detected format or None."""
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    stem = PurePosixPath(filename.replace("\\", "/")).stem.lower()
    key = None
    if stem == "jenkinsfile":
        return None                      # the Jenkins bundle path owns this
    for candidate, value in TEXT_EXTENSIONS.items():
        if filename.lower().endswith(candidate):
            key = value
            break
    if key is None:
        key = "xlsx" if suffix in {".xlsx", ".xlsm"} else None
    if key in {"json", "py", "js", "feature", "jenkins", "csv", "xlsx"}:
        if suffix == ".har" and key == "json":
            parse_har(preview, data)
            preview.detected = {"format": "har", "why": "browser network capture converted to API steps"}
            return "har"
        return None                      # handled by script_import already
    if key == "xls":
        parse_xls(preview, data)
        preview.detected = {"format": "xls", "why": "legacy Excel workbook (.xls)"}
        return "xls"
    if key == "docx":
        parse_docx(preview, data)
        preview.detected = {"format": "docx", "why": "Word document tables or paragraphs"}
        return "docx"
    if key == "pdf":
        parse_pdf(preview, data)
        preview.detected = {"format": "pdf", "why": "PDF text layer"}
        return "pdf"
    if key == "xml":
        parse_xml(preview, data)
        preview.detected = {"format": "xml", "why": "canonical step XML or test case names"}
        return "xml"
    if key == "yaml":
        parse_yaml(preview, _decode(data))
        preview.detected = {"format": "yaml", "why": "YAML step list"}
        return "yaml"
    if key == "robot":
        parse_robot(preview, _decode(data))
        preview.detected = {"format": "robot", "why": "Robot Framework suite"}
        return "robot"
    if key in {"java", "cs"}:
        parse_selenium_code(preview, _decode(data), key)
        preview.detected = {"format": key, "why": "Selenium %s subset" % ("Java" if key == "java" else "C#/NET")}
        return key
    if key == "html":
        parse_html(preview, _decode(data))
        preview.detected = {"format": "html", "why": "HTML page or legacy Selenium HTML suite"}
        return "html"
    if key == "rtf":
        parse_english(preview, _rtf_text(_decode(data)), "rtf")
        preview.detected = {"format": "rtf", "why": "rich-text document converted to plain text"}
        return "rtf"
    if key == "text":
        source = _decode(data)
        detected = sniff(source)
        preview.detected = detected
        _parse_by_sniff(preview, source, detected["format"], filename)
        return detected["format"]
    return None                      # unknown extension: parse_file sniffs the content


def _rtf_text(source):
    """Crude but safe Rich Text -> plain text: drop control words, keep the words."""
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", "replace"), source)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", text)
    text = text.replace("{", "").replace("}", "")
    return "\n".join(line.strip() for line in text.splitlines())


def parse_html(preview, source):
    """Legacy Selenium HTML suite tables, or the text of an HTML page."""
    rows = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", source, re.I | re.S):
        parsed = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.I | re.S):
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip()
                     for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)]
            if any(cells):
                parsed.append(cells)
        rows.extend(parsed)
    if rows and any(HEADER_LOOKUP.get(_normalise_header(cell)) == "action" for cell in rows[0]) and \
            any("target" in _normalise_header(cell) or "locator" in _normalise_header(cell) or
                "selector" in _normalise_header(cell) for cell in rows[0]):
        header = [_normalise_header(cell) for cell in rows[0]]
        commands = []
        for row in rows[1:]:
            values = dict(zip(header, [cell for cell in row] + [None] * len(header)))
            commands.append({"command": values.get("action") or values.get("command") or "comment",
                             "target": values.get("target") or values.get("locator") or values.get("selector") or "",
                             "value": values.get("value") or ""})
        return parse_selenium_side(preview, {"tests": [{"name": preview.name, "commands": commands}]})
    text = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", source, flags=re.I | re.S)
    # Headings name the page rather than describe an action: keep them as notes so
    # a heading does not read as a step the importer failed to understand.
    headings = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", heading)).strip()
                for heading in re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", source, re.I | re.S)]
    headings = [label for label in headings if label]
    if headings:
        preview.note(1, f"html: page heading(s) treated as page furniture, not steps: {', '.join(headings[:5])[:200]}")
    text = re.sub(r"<h[1-6][^>]*>.*?</h[1-6]>", "\n", text, flags=re.I | re.S)
    text = re.sub(r"</(?:p|div|li|tr|h[1-6]|ul|ol|table|section|article|header|footer|form|dt|dd|pre|figure|main|nav)>"
                  r"|<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return parse_english(preview, text, "html")


def parse_har(preview, data):
    """Browser network capture: the first document load plus its API traffic.

    A HAR is a recording of what the browser did, so it makes an honest first
    draft: one navigate step for the first document, one api_request step per
    distinct JSON/XHR call. Binary and third-party asset traffic is ignored.
    """
    try:
        captured = json.loads(_decode(data))
    except ValueError as exc:
        raise ImportProblem("HAR could not be read as JSON") from exc
    entries = ((captured or {}).get("log") or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        raise ImportProblem("This JSON is not a HAR capture with log.entries")
    document = None
    seen = set()
    api = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        url = str(request.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        mime = str((entry.get("response") or {}).get("content", {}).get("mimeType") or "").lower()
        if document is None and str(request.get("method", "GET")).upper() == "GET" and (
                not mime or "html" in mime or mime.startswith("text/plain")):
            document = url
            continue
        if any(part in url for part in (".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".woff", ".ico", ".gif", ".map")):
            continue
        key = (request.get("method"), url)
        if key in seen:
            continue
        seen.add(key)
        step = {"action": "api_request", "value": url,
                "selector": {"method": str(request.get("method") or "GET").upper()},
                "label": f"{request.get('method')} {url[:200]}"}
        headers = {str(h.get("name")): str(h.get("value") or "")
                   for h in (request.get("headers") or []) if isinstance(h, dict) and h.get("name")}
        headers.pop("Cookie", None)
        if headers:
            step["selector"]["headers"] = headers
        api.append(step)
    if document:
        preview.add({"action": "navigate", "value": document, "label": f"Open {document[:200]}"}, 1)
    if not document and not api:
        raise ImportProblem("The HAR capture holds no page load or API call to convert")
    for index, step in enumerate(api[:100], len(preview.steps) + 1):
        preview.add(step, index)
    if len(api) > 100:
        preview.note(1, "Only the first 100 API calls were imported")
    preview.note(1, "A HAR is a capture, not an assertion: add the checks this flow needs before publishing")
    return True


def _decode(data):
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ImportProblem("The file is not readable text in UTF-8, UTF-16 or Windows-1252")


def _parse_by_sniff(preview, source, kind, filename):
    """Route a sniffed body to the parser that already understands that shape."""
    if kind == "json":
        from app.script_import import parse_file
        inner = parse_file("sniffed.json", source.encode())
        _absorb(preview, inner)
        preview.detected = {"format": "json", "why": "the text body is JSON"}
        return
    if kind == "feature":
        from app.script_import import parse_file
        inner = parse_file("sniffed.feature", source.encode())
        _absorb(preview, inner)
        preview.detected = {"format": "feature", "why": "Gherkin keywords"}
        return
    if kind == "py":
        from app.script_import import PythonParser
        PythonParser(preview).parse(source)
        preview.detected = {"format": "py", "why": "Playwright Python API"}
        return
    if kind == "js":
        if CYPRESS_HINT.search(source):
            parse_cypress(preview, source)
        else:
            from app.script_import import parse_javascript
            parse_javascript(source, preview)
        preview.detected = {"format": "js", "why": "Playwright/Cypress JavaScript"}
        return
    if kind == "java":
        parse_selenium_code(preview, source, "java")
        preview.detected = {"format": "java", "why": "Selenium Java API"}
        return
    if kind == "cs":
        parse_selenium_code(preview, source, "cs")
        preview.detected = {"format": "cs", "why": "Selenium .NET API"}
        return
    if kind == "robot":
        parse_robot(preview, source)
        preview.detected = {"format": "robot", "why": "Robot Framework sections"}
        return
    if kind == "jenkins":
        from app.script_import import parse_file
        inner = parse_file("Jenkinsfile.txt", source.encode())
        _absorb(preview, inner)
        preview.detected = {"format": "jenkins", "why": "Jenkins pipeline"}
        return
    if kind == "xml":
        parse_xml(preview, source.encode())
        preview.detected = {"format": "xml", "why": "XML markup"}
        return
    if kind == "yaml":
        parse_yaml(preview, source)
        preview.detected = {"format": "yaml", "why": "YAML mappings"}
        return
    if kind == "table":
        rows = [_split_smart(line) for line in source.splitlines() if line.strip()]
        rows = [row for row in rows if not set("".join(row)) <= {"-", ":", "|", " "}]
        if parse_table_smart(rows, preview, "text"):
            preview.detected = {"format": "table", "why": "table header with test columns"}
            return
    parse_english(preview, source, "text")
    preview.detected = {"format": "text", "why": "plain-English step list"}


def _absorb(preview, inner):
    """Merge a nested parse (used when a .txt body turns out to be JSON/Gherkin)."""
    for step in inner.get("steps") or []:
        preview.steps.append(step)
    for diagnostic in inner.get("diagnostics") or []:
        preview.diagnostics.append(diagnostic)
    if inner.get("start_url") and not preview.start_url:
        preview.start_url = inner["start_url"]
    if inner.get("name"):
        preview.name = inner["name"]
    if inner.get("execution_plan"):
        preview.execution_plan = inner["execution_plan"]
