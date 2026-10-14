"""Onboarding: turn literal input values into reusable variables and parameters.

Two rules drive this module, both from the way the dashboard is meant to be used:

1. A recording or an imported script should carry **parameters**, not one person's
   data. A literal typed into a field ("Ada", a password, a store number) is a
   candidate to become `{{variable}}`.
2. The decision belongs to the person saving. Candidates are *suggested* here
   (deterministically, offline -- no AI service, no network) and are applied only
   when the caller confirms them.

The extraction is deliberately conservative and explainable: it names candidates
after the field they were typed into, reuses a variable that already holds the
same value, and never touches a step that already carries a `{{placeholder}}` or
whose value is meaningful to the replay (a key name, a captured variable name, a
page alias).
"""
import re

# Actions whose `value` is data the user typed (or picked) and may be replaced by
# a variable. A `save_variable`/`capture_text` value is a variable *name*, a
# `press` value is a key, a `switch_page` value is an alias: none of them is data.
DATA_ACTIONS = {
    "type": "value", "fill": "value", "select_option": "value", "upload_file": None,
    # Windows desktop recordings: text typed into a control and the option picked
    # from a list are data exactly like their browser counterparts.
    "ui_type": "value", "ui_select": "value",
    "navigate": "url", "new_page": "url", "api_request": "url",
    "assert_text": "expected", "assert_contains": "expected", "assert_value": "expected",
    "assert_url": "url", "assert_title": "expected", "assert_count": "count",
    "mock_route": "url",
}
# Values that are URLs or paths are anchored data, not input to parameterise,
# unless they carry a query parameter that clearly is.
URL_ACTIONS = {"navigate", "new_page", "api_request", "mock_route", "assert_url"}

SECRET_WORDS = re.compile(
    r"(?:pass(?:word|code|phrase)?|pwd|pin|otp|token|secret|api[\s_-]?key|access[\s_-]?key|"
    r"credential|client[\s_-]?secret|private[\s_-]?key|bearer|auth)", re.I)
AUTH_CATEGORY = "Auth"
PARAMETER_CATEGORY = "Parameter"

# Structural words only: a field called "Username" must become {{username}}, so the
# words that name a control are dropped and the words that name the data are kept.
STOP_WORDS = {
    "the", "a", "an", "your", "my", "enter", "type", "input", "field", "textbox", "text", "box",
    "value", "button", "link", "tab", "form", "page", "click", "select", "option", "of", "for",
    "with", "and", "or", "to", "on", "in", "into", "id", "css", "xpath", "search", "please",
    "dropdown", "list", "label", "control", "element", "new", "tb", "txt",
}
MAX_VALUE_LENGTH = 120
DATE_LIKE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")
UUID_LIKE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EMAIL_LIKE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
NAME_LIKE = re.compile(r"^[A-Z][A-Za-z'\-]*(?:[ .][A-Z][A-Za-z'\-]*){0,3}$")
NUMBER_LIKE = re.compile(r"^\d{1,6}$")
URL_LIKE = re.compile(r"^(?:https?://|/|\./|\.\./)")
PLACEHOLDER = re.compile(r"\{\{\s*[\w.\-]+\s*\}\}")
SAFE_NAME = re.compile(r"[\w.\-]{1,100}")
QUERY_PAIR = re.compile(r"([?&])([A-Za-z0-9_.\-\[\]]{1,40})=([^&#\s]{1,120})")


def _hint_from_selector(selector):
    """Guess a name from the field the value went into."""
    if not isinstance(selector, dict):
        return ""
    # Desktop controls carry their UIA name and automation id instead of HTML
    # attributes; the field reference is what the recorder falls back to.
    for key in ("label", "placeholder", "aria-label", "name", "field_name", "data-testid", "id",
                "automation_id", "primary", "text"):
        value = selector.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _tokens(text):
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", str(text or ""))
    words = [word for word in cleaned.split() if word]
    words = [word for word in words if word.lower() not in STOP_WORDS and not word.isdigit()]
    return words


def suggest_name(value, hint=""):
    """A short, safe variable name: the field name when there is one, else the shape."""
    tokens = _tokens(hint)
    if tokens:
        candidate = "_".join(token.lower() for token in tokens[:3])
    else:
        candidate = ""
    if not candidate or candidate in STOP_WORDS:
        if EMAIL_LIKE.match(str(value)):
            candidate = "email"
        elif DATE_LIKE.match(str(value)):
            candidate = "date"
        elif NUMBER_LIKE.match(str(value)):
            candidate = "number"
        elif UUID_LIKE.match(str(value)):
            candidate = "identifier"
        elif URL_LIKE.match(str(value)):
            candidate = "url"
        elif NAME_LIKE.match(str(value)) and " " not in str(value):
            candidate = "name"
        elif NAME_LIKE.match(str(value)):
            candidate = "full_name"
        else:
            candidate = "input"
    candidate = re.sub(r"[^\w.\-]+", "_", candidate).strip("_.-") or "input"
    if candidate[0].isdigit():
        candidate = "v_" + candidate
    return candidate[:60]


def _unique(name, taken):
    if name not in taken:
        taken.add(name)
        return name
    index = 2
    while f"{name}_{index}" in taken:
        index += 1
    taken.add(f"{name}_{index}")
    return f"{name}_{index}"


def secret_like(hint, value):
    return bool(SECRET_WORDS.search(str(hint or ""))) or bool(SECRET_WORDS.search(str(value or "")[:40]))


def _category_for(value, hint):
    if secret_like(hint, value):
        return AUTH_CATEGORY
    if NUMBER_LIKE.match(str(value)) or DATE_LIKE.match(str(value)):
        return PARAMETER_CATEGORY
    return "General"


def _step_dict(step):
    """Accept either a SQLAlchemy row or a plain canonical step dict."""
    if isinstance(step, dict):
        return step
    return {
        "id": getattr(step, "id", None),
        "order": getattr(step, "order", None),
        "action": getattr(step, "action", None),
        "value": getattr(step, "value", None),
        "label": getattr(step, "label", None),
        "selector": getattr(step, "selector", None) or {},
    }


def candidates(steps, existing_variables=(), variables_seed=None, limit=60):
    """Literals in `steps` that could become variables, in step order.

    Each candidate is one value (not one step): typing the same value into three
    fields yields one variable with three usages, which is what makes a recording
    reusable. `existing_variables` let a literal be recognised as a value that is
    already a variable, so it is re-pointed instead of duplicated.
    """
    taken = {str(item.get("name")) for item in existing_variables if isinstance(item, dict)}
    taken |= set(variables_seed or {})
    by_value = {}
    for item in existing_variables or []:
        if isinstance(item, dict) and item.get("value") and not item.get("is_secret"):
            by_value.setdefault(str(item["value"]), str(item.get("name")))
    found = {}
    seed_values = {str(k): str(v) for k, v in (variables_seed or {}).items()}
    for raw in steps or []:
        step = _step_dict(raw)
        action = str(step.get("action") or "").lower()
        kind = DATA_ACTIONS.get(action)
        if not kind:
            continue
        value = step.get("value")
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        if not value.strip() or PLACEHOLDER.search(value) or len(value) > MAX_VALUE_LENGTH:
            continue
        hint = _hint_from_selector(step.get("selector"))
        label = str(step.get("label") or "")
        if kind == "url" or action in URL_ACTIONS:
            # The URL itself stays; only its query values are data worth a variable.
            for _sep, key, raw_value in QUERY_PAIR.findall(value):
                if PLACEHOLDER.search(raw_value) or len(raw_value) < 2:
                    continue
                _register(found, taken, by_value, seed_values, raw_value, key, step, hint=key,
                          in_url=True, category=PARAMETER_CATEGORY if NUMBER_LIKE.match(raw_value) else "General")
            continue
        if kind == "expected":
            # An expectation that merely repeats typed data follows that variable:
            # "Hello, Ada" becomes "Hello, {{username}}" instead of a new variable.
            continue
        _register(found, taken, by_value, seed_values, value, value, step,
                  hint=hint or label, in_url=False, category=_category_for(value, hint or label))
    # Expectations are considered after inputs so a name can be reused.
    for raw in steps or []:
        step = _step_dict(raw)
        if DATA_ACTIONS.get(str(step.get("action") or "").lower()) != "expected":
            continue
        value = step.get("value")
        if not isinstance(value, str) or not value.strip() or PLACEHOLDER.search(value):
            continue
        for candidate in found.values():
            if candidate.get("kind") == "input" and candidate["value"] in value and candidate["value"] != value:
                candidate.setdefault("expectations", []).append(
                    {"step_id": step.get("id"), "order": step.get("order"), "before": value})
                break
    ordered = sorted(found.values(), key=lambda item: (item["first_order"], item["value"]))
    return ordered[:limit]


def _register(found, taken, by_value, seed_values, value, key, step, hint, in_url, category):
    """Record one literal, merging a repeat of the same value into one candidate."""
    existing = by_value.get(value) or seed_values.get(value)
    if existing:
        entry = found.get(value)
        if entry is None:
            entry = {"value": value, "name": existing, "reuse": True, "usages": [], "first_order": step.get("order") or 0}
            _finish(entry, hint, category)
            found[value] = entry
        entry["usages"].append(_usage(step, in_url, key))
        return
    entry = found.get(value)
    if entry is None:
        entry = {"value": value, "name": _unique(suggest_name(value, hint), taken),
                 "reuse": False, "usages": [], "first_order": step.get("order") or 0}
        _finish(entry, hint, category)
        found[value] = entry
    entry["usages"].append(_usage(step, in_url, key))


def _finish(entry, hint, category):
    entry["hint"] = str(hint or "")[:120]
    entry["category"] = category
    entry["is_secret"] = secret_like(hint, entry["value"])
    entry["kind"] = "query" if entry["usages"] and entry["usages"][0]["in_url"] else "input"


def _usage(step, in_url, key):
    return {"step_id": step.get("id"), "order": step.get("order"), "action": step.get("action"),
            "label": (step.get("label") or "")[:200], "selector": step.get("selector") or {},
            "in_url": bool(in_url), "query_key": key if in_url and key != step.get("value") else None}


def plan(steps, existing_variables=(), variables_seed=None):
    """The reviewer-facing payload: what would change, and what it becomes."""
    items = candidates(steps, existing_variables, variables_seed)
    by_order = {}
    by_id = {}
    for raw in steps or []:
        step = _step_dict(raw)
        by_order.setdefault(step.get("order"), step)
        if step.get("id"):
            by_id[step["id"]] = step
    for item in items:
        preview = []
        for usage in item["usages"]:
            source = by_id.get(usage.get("step_id")) or by_order.get(usage.get("order")) or {}
            before = source.get("value") or ""
            preview.append({"order": usage["order"], "action": usage["action"],
                            "before": before[:160], "after": replace_in(before, item) [:160]})
        item["preview"] = preview[:5]
        item["usages_count"] = len(item["usages"])
    return {"candidates": items, "count": len(items),
            "summary": (f"{len(items)} input value(s) can become variables; "
                        f"{sum(1 for i in items if i.get('reuse'))} already match an existing variable")}


def replace_in(text, candidate):
    """Replace one candidate's literal inside a step value with {{name}}."""
    if not isinstance(text, str) or not text:
        return text
    value, name = candidate["value"], candidate["name"]
    if value == text:
        return "{{%s}}" % name
    if candidate.get("kind") == "query":
        key = next((usage.get("query_key") for usage in candidate.get("usages") or [] if usage.get("query_key")), None)
        pattern = re.compile(r"([?&])" + re.escape(key or "") + r"=" + re.escape(value) + r"(?=[&#]|$)")
        return pattern.sub(lambda m: f"{m.group(1)}{key}={{{{{name}}}}}", text)
    return text.replace(value, "{{%s}}" % name)


def apply(steps, assignments, existing_variables=()):
    """Rewrite step values for the assignments the reviewer confirmed.

    Returns `(new_steps, applied, reuse)` where `applied` lists the variables to
    create and `reuse` lists literals that were pointed at an existing variable.
    """
    items = {item["value"]: item for item in candidates(steps, existing_variables)}
    by_name = {}
    for item in items.values():
        by_name[item["name"]] = item
    new_steps = []
    applied, reused = [], []
    chosen = []
    for assignment in assignments or []:
        if not isinstance(assignment, dict):
            continue
        name = str(assignment.get("name") or "").strip()
        value = assignment.get("value")
        if value is None:
            continue
        value = str(value)
        if not SAFE_NAME.fullmatch(name):
            raise ValueError(f"'{name}' is not a valid variable name (letters, numbers, dots, dashes, underscores)")
        item = items.get(value) or by_name.get(name)
        if item is None:
            continue
        item = {**item, "name": name,
                "category": str(assignment.get("category") or item.get("category") or "General"),
                "is_secret": bool(assignment.get("is_secret", item.get("is_secret")))}
        if item.get("reuse") and not assignment.get("create"):
            reused.append({"name": name, "value": value})
        else:
            applied.append({"name": name, "value": value, "category": item["category"],
                            "is_secret": item["is_secret"]})
        chosen.append(item)
    for raw in steps or []:
        step = dict(_step_dict(raw))
        value = step.get("value")
        if isinstance(value, str):
            for item in chosen:
                if item["value"] and item["value"] in value:
                    value = replace_in(value, item)
            step["value"] = value
        new_steps.append(step)
    return new_steps, applied, reused
