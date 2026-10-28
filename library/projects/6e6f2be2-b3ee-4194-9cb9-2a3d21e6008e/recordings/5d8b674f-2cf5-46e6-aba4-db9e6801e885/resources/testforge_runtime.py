"""Portable, sequential Playwright action runtime. Also shipped with Python exports.

No eval/exec, plugins, arbitrary filesystem access, sleeps or paid services.
Locators use Playwright's actionability/expect waits, returning as soon as ready.
"""
import base64
import binascii
import datetime
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import expect

try:  # package context; portable exports copy these modules next to this one
    from app.execution_speed import normalize_speed, scaled_timeout, speed_profile
    from app.desktop_runner import DESKTOP_ACTIONS, DESKTOP_LOCATOR_KEYS, validate_desktop_selector
    from app.mobile_runner import (DEVICE_KEYS, MOBILE_ACTIONS, MOBILE_LOCATOR_KEYS,
                                   NETWORK_MODES, ORIENTATIONS, validate_barcode,
                                   validate_mobile_selector)
    from app import desktop_runner as _desktop_module
    from app import mobile_runner as _mobile_module
except ImportError:  # pragma: no cover - exercised by exported bundles
    from testforge_speed import normalize_speed, scaled_timeout, speed_profile
    from testforge_desktop import DESKTOP_ACTIONS, DESKTOP_LOCATOR_KEYS, validate_desktop_selector
    from testforge_mobile import (DEVICE_KEYS, MOBILE_ACTIONS, MOBILE_LOCATOR_KEYS,
                                  NETWORK_MODES, ORIENTATIONS, validate_barcode,
                                  validate_mobile_selector)
    import testforge_desktop as _desktop_module
    import testforge_mobile as _mobile_module

MAX_STEPS = 500
# A vendor export (Azure DevOps, TestRail, Zephyr, HP ALM) carries 15-30 metadata
# columns next to the steps; the row cap above still bounds the parse.
MAX_COLUMNS = 40
MAX_REPEAT = 100
MAX_TEXT = 65536
MAX_FILE = 512 * 1024
VARIABLE = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")
BASIC = {"navigate", "click", "type", "fill", "press", "save_variable"}
ACTIONS = BASIC | {
    "check", "uncheck", "select_option", "hover", "dblclick", "right_click",
    "drag_to", "scroll", "assert_text", "assert_contains", "assert_value",
    "assert_visible", "assert_hidden", "assert_enabled", "assert_disabled",
    "assert_checked", "assert_unchecked", "assert_count", "assert_url", "assert_title",
    "wait_visible", "wait_hidden", "capture_text", "new_page", "switch_page",
    "close_page", "open_popup", "dialog", "upload_file", "download",
    "api_request", "mock_route", "clear_routes",
    # Scoped web-table helpers and wait-aware autocomplete (roadmap R03/R04).
    "select_suggestion", "pick_suggestion", "table_lookup", "assert_table_cell",
} | DESKTOP_ACTIONS | MOBILE_ACTIONS
NO_LOCATOR = {"navigate", "press", "assert_url", "assert_title", "new_page", "switch_page",
              "close_page", "api_request", "mock_route", "clear_routes"}
KINDS = {"css", "role", "label", "text", "test_id", "placeholder"}
# A recorded step can address a point on the page instead of an element: the browser
# recorder stores x/y for every click, and coordinate-only labels ("Click 528,562")
# come back through the importer. Only the pointer actions may.
POINTER_ACTIONS = {"click", "dblclick", "right_click", "hover"}
# A recorder `type` step keeps no selector when the field was already focused;
# replaying it is "type into whatever has focus", which is a real action, not a
# malformed one.
FOCUS_ACTIONS = {"type", "fill"}


def interpolate(text, variables):
    def replace(match):
        name = match.group(1)
        if name not in variables or variables[name] is None:
            raise ValueError("Missing variable: " + name)
        return str(variables[name])
    return VARIABLE.sub(replace, str(text or ""))


def resolve(value, variables):
    if isinstance(value, str):
        return interpolate(value, variables)
    if isinstance(value, dict):
        return {k: resolve(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, variables) for v in value]
    return value


def _locator_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("Locator must be an object")
    if spec.get("kind", "css") not in KINDS:
        raise ValueError("Unknown locator kind")
    if not isinstance(spec.get("primary"), str) or not spec["primary"].strip():
        raise ValueError("A nonempty locator.primary is required")
    if "exact" in spec and not isinstance(spec["exact"], bool):
        raise ValueError("locator.exact must be a boolean")
    frames = spec.get("frames", [])
    if not isinstance(frames, list) or len(frames) > 5 or any(not isinstance(f, str) or not f.strip() for f in frames):
        raise ValueError("frames must contain at most five nonempty CSS frame selectors")


OPTION_KEYS = {
    "drag_to": {"target"}, "select_option": {"by"},
    "new_page": {"alias"}, "open_popup": {"alias"},
    "dialog": {"decision", "message", "dialog_type"},
    "upload_file": {"filename", "base64", "mime_type"},
    "api_request": {"method", "headers", "body", "status", "json_path", "capture", "expected"},
    "mock_route": {"status", "content_type", "body", "abort"},
    "select_suggestion": {"suggestion", "type_text"},
    "pick_suggestion": {"suggestion", "type_text"},
    "table_lookup": {"row_text", "column", "row", "header"},
    "assert_table_cell": {"row_text", "column", "row", "header"},
    **{action: set(DESKTOP_LOCATOR_KEYS) for action in DESKTOP_ACTIONS},
    **{action: set(MOBILE_LOCATOR_KEYS) | {"symbology", "barcode_data", "direction", "distance", "duration_ms", "key", "mode", "orientation", "app_id", "bundle_id"} for action in MOBILE_ACTIONS},
}
COMMON_KEYS = {"schema_version", "primary", "kind", "name", "exact", "frames", "page", "strict", "timeout_ms", "x", "y", "text", "tag"}


def _target_spec(selector, action):
    """Validate that a step targets an element, a point, or the focused element.

    Order matters: a step that names an element is validated exactly as before, so
    nothing about an ordinary locator changes. Only when no usable selector is
    present does a recorded coordinate pair (pointer actions) or a
    type-into-the-focused-element step become acceptable. Anything else keeps the
    original message.
    """
    primary = selector.get("primary")
    if isinstance(primary, str) and primary.strip():
        _locator_spec(selector)
        return
    x, y = selector.get("x"), selector.get("y")
    if x is not None or y is not None:
        if action not in POINTER_ACTIONS:
            raise ValueError(action + " cannot target screen coordinates; provide a locator")
        for axis, value in (("x", x), ("y", y)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 20000:
                raise ValueError("Coordinate " + axis + " must be a number from 0 to 20000")
        selector["x"], selector["y"] = x, y
        selector.pop("primary", None)
        return
    if action in FOCUS_ACTIONS:
        selector.pop("primary", None)
        return
    raise ValueError("A nonempty locator.primary is required")


def audit_selector(options):
    """Do not copy potentially sensitive/large payloads into every run log/event."""
    return {k: ("[omitted]" if k in {"headers", "body", "base64"} else v)
            for k, v in (options or {}).items()}


def normalize_step(raw, order=1):
    if not isinstance(raw, dict):
        raise ValueError("Step must be an object")
    action = str(raw.get("action", "")).strip().lower()
    if action not in ACTIONS:
        raise ValueError("Unsupported action: " + action)
    value = raw.get("value", "")
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError("value must be a scalar; put action options in selector")
    value = str(value)
    if len(value) > MAX_TEXT:
        raise ValueError("Step value is too large")
    selector = raw.get("selector") or {}
    if isinstance(selector, str):
        selector = {"primary": selector}
    if not isinstance(selector, dict):
        raise ValueError("selector must be a CSS string or an object")
    # JSON round-trip makes a detached, serializable structure and rejects objects.
    selector = json.loads(json.dumps(selector, allow_nan=False))
    if len(json.dumps(selector)) > MAX_FILE * 2:
        raise ValueError("Step options are too large")
    unknown = set(selector) - COMMON_KEYS - OPTION_KEYS.get(action, set())
    if unknown:
        raise ValueError("Unsupported action options: " + ", ".join(sorted(unknown)))
    if selector.get("schema_version", 1) != 1 or isinstance(selector.get("schema_version", 1), bool):
        raise ValueError("Only action schema_version 1 is supported")
    selector["schema_version"] = 1
    selector["strict"] = True
    if action in DESKTOP_ACTIONS:
        # Desktop steps carry a structured UIA locator instead of a web locator.
        validate_desktop_selector(selector, action)
        if action in {"assert_control_text", "assert_control_state"} and "value" not in raw:
            raise ValueError(action + " requires an expected value")
        if action == "ui_menu_invoke" and not value and not selector.get("name"):
            raise ValueError("ui_menu_invoke requires a menu path value or a name locator")
        if action == "ui_grid_read" and value and not re.fullmatch(r"[\w.-]{1,100}", value):
            raise ValueError("ui_grid_read value must be a simple variable name")
    elif action in MOBILE_ACTIONS:
        validate_mobile_selector(selector, action)
        if action == "scan_barcode":
            validate_barcode(selector, value)
        elif action == "device_key":
            key = value or str(selector.get("key") or "")
            if key not in DEVICE_KEYS:
                raise ValueError("device_key must be Enter, Tab, F1-F12, Escape, Backspace or Scan")
        elif action == "rotate_orientation":
            if (value or str(selector.get("orientation") or "")).lower() not in ORIENTATIONS:
                raise ValueError("rotate_orientation value must be portrait or landscape")
        elif action == "toggle_network_mode":
            if (value or str(selector.get("mode") or "")).lower() not in NETWORK_MODES:
                raise ValueError("toggle_network_mode has an unsupported network mode")
        elif action == "swipe":
            if (value or str(selector.get("direction") or "up")).lower() not in {"up", "down", "left", "right"}:
                raise ValueError("swipe value must be up, down, left or right")
        elif action in {"assert_mobile_text"} and "value" not in raw:
            raise ValueError("assert_mobile_text requires an expected value")
    elif action not in NO_LOCATOR:
        _target_spec(selector, action)
    if action == "select_option" and selector.get("by", "value") not in {"value", "label"}:
        raise ValueError("select_option.by must be value or label")
    if action == "drag_to":
        _locator_spec(selector.get("target"))
    timeout = selector.get("timeout_ms", 3000)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30000:
        raise ValueError("timeout_ms must be an integer from 1 to 30000")
    count = raw.get("repeat_count", raw.get("repeat", 1))
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_REPEAT:
        raise ValueError("repeat_count must be an integer from 1 to 100")
    if action in {"assert_count", "scroll"} and not VARIABLE.search(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(action + " requires an integer value") from exc
        if action == "assert_count" and number < 0:
            raise ValueError("Count cannot be negative")
    if action in {"navigate", "new_page", "api_request"} and not VARIABLE.search(value):
        check_url(value)
    if action in {"capture_text", "save_variable", "switch_page", "close_page"}:
        if not re.fullmatch(r"[\w.-]{1,100}", value):
            raise ValueError(action + " requires a simple variable/page name")
    if action == "new_page" or action == "open_popup":
        if not re.fullmatch(r"[\w.-]{1,100}", str(selector.get("alias", ""))):
            raise ValueError("A simple page alias is required")
    if action == "api_request":
        if str(selector.get("method", "GET")).upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError("Unsupported HTTP method")
        status = selector.get("status", 200)
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("API status must be an integer HTTP status")
        if not isinstance(selector.get("headers", {}), dict):
            raise ValueError("API headers must be an object")
        if selector.get("capture") and not re.fullmatch(r"[\w.-]{1,100}", str(selector["capture"])):
            raise ValueError("API capture must be a simple variable name")
    if action == "upload_file":
        upload_buffer(selector)
    if action == "mock_route":
        if not value or len(value) > 1000:
            raise ValueError("A bounded URL pattern is required")
        if len(str(selector.get("body", ""))) > MAX_TEXT:
            raise ValueError("Mock response is too large")
        status = selector.get("status", 200)
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise ValueError("Mock status must be an integer HTTP status")
        if "abort" in selector and not isinstance(selector["abort"], bool):
            raise ValueError("Mock abort must be boolean")
    if action == "dialog":
        if selector.get("decision", "accept") not in {"accept", "dismiss"}:
            raise ValueError("Dialog decision must be accept or dismiss")
    if action in {"select_suggestion", "pick_suggestion"}:
        if not value and not selector.get("suggestion"):
            raise ValueError(action + " requires the suggestion text as its value")
        for key in ("suggestion", "type_text"):
            if key in selector and (not isinstance(selector[key], str) or len(selector[key]) > 500):
                raise ValueError("Suggestion options must be short strings")
    if action in {"table_lookup", "assert_table_cell"}:
        if "row_text" not in selector and "row" not in selector:
            raise ValueError(action + " requires selector.row_text (or a numeric selector.row)")
        if "column" not in selector:
            raise ValueError(action + " requires selector.column (a header name or zero-based index)")
        if action == "table_lookup" and not re.fullmatch(r"[\w.-]{1,100}", value):
            raise ValueError("table_lookup value must be the destination variable name")
        if action == "assert_table_cell" and "value" not in raw:
            raise ValueError("assert_table_cell requires an expected value")
    return {"order": order, "action": action, "value": value,
            "selector": selector, "label": str(raw.get("label") or action)[:500],
            "repeat_count": count}


def normalize_steps(steps):
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise ValueError("Supply between 1 and 500 steps")
    result = []
    for i, step in enumerate(steps, 1):
        try:
            result.append(normalize_step(step, i))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"Step {i}: {exc}") from exc
    if sum(s["repeat_count"] for s in result) > 2000:
        raise ValueError("Expanded recording exceeds 2000 actions")
    return result


def check_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only HTTP(S) URLs without embedded credentials are supported")
    return url


async def _goto_resilient(page, url: str, timeout_ms: int) -> None:
    """Navigate with domcontentloaded, recovering if slow scripts stall the event."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        if "Timeout" not in type(exc).__name__ and "Timeout" not in str(exc):
            raise
        current = getattr(page, "url", None) or ""
        if current and current != "about:blank":
            if hasattr(page, "evaluate"):
                try:
                    await page.evaluate("() => { try { window.stop(); } catch (_) {} }")
                except Exception:
                    pass
            return
        await page.goto(url, wait_until="commit", timeout=min(max(timeout_ms, 1000), 15000))


def upload_buffer(options):
    name = options.get("filename", "")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", name):
        raise ValueError("Upload needs a safe filename, not a server path")
    encoded = options.get("base64", "")
    if not isinstance(encoded, str) or len(encoded) > MAX_FILE * 2:
        raise ValueError("Upload fixture exceeds 512 KiB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Upload fixture must contain valid base64") from exc
    if len(data) > MAX_FILE:
        raise ValueError("Upload fixture exceeds 512 KiB")
    return {"name": name, "mimeType": str(options.get("mime_type", "application/octet-stream")), "buffer": data}


def locator(page, spec):
    target = page
    for frame in spec.get("frames", []):
        target = target.frame_locator(frame)
    kind, primary = spec.get("kind", "css"), spec["primary"]
    exact = spec.get("exact", True)
    if kind == "role":
        return target.get_by_role(primary, **({"name": spec["name"], "exact": exact} if "name" in spec else {}))
    if kind == "test_id":
        return target.get_by_test_id(primary)
    if kind in {"label", "text", "placeholder"}:
        return getattr(target, "get_by_" + kind)(primary, exact=exact)
    return target.locator(primary)


def iso_utc(moment):
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (naive values are UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StepClock:
    """Wall-clock stamps for one step: create it when the step starts, call ``finish()`` when the action returns.

    ``started_at``/``finished_at`` are UTC ISO-8601 strings with millisecond precision, so they
    survive the JSON execution log and sort as text. ``duration_ms`` uses a monotonic clock, so a
    wall-clock adjustment mid-run cannot skew it. Finish is taken when the action returns, before
    screenshot evidence is encoded, so durations compare the speed setting, not image work.
    """

    def __init__(self):
        self._monotonic = time.perf_counter()
        self.started_at = iso_utc(datetime.datetime.now(datetime.timezone.utc))

    def finish(self):
        return {
            "started_at": self.started_at,
            "finished_at": iso_utc(datetime.datetime.now(datetime.timezone.utc)),
            "duration_ms": round((time.perf_counter() - self._monotonic) * 1000, 1),
        }


def json_field(data, path):
    for part in str(path or "").split(".") if path else []:
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


class StepRuntime:
    """One runtime per recording, with no tasks when idle and no parallel runs."""
    def __init__(self, page, artifact_dir=None, speed=None, execution_speed=None):
        self.page = page
        self.pages = {"main": page}
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None
        self.routes = []
        self.last_result = None
        # 1.0 reproduces the historical behaviour exactly (no added pace).
        self.speed_profile = speed_profile(execution_speed if execution_speed is not None else speed)
        self._desktop = None
        self._mobile = None

    def _timeout(self, opts, default=3000):
        return scaled_timeout((opts or {}).get("timeout_ms", default), self.speed_profile)

    async def _pace(self):
        pace = int(self.speed_profile.get("pace_ms") or 0)
        if pace > 0:
            import asyncio
            await asyncio.sleep(pace / 1000)

    def _assertion_failed(self, action, expected, actual):
        """Record expected-vs-actual diagnostics, then raise a scrub-able error.

        Callers (executor/plan runtimes) scrub known secret values out of the
        message before it is persisted or broadcast, so a failed password
        assertion cannot leak the secret through its diagnostic.
        """
        self.last_result = {"assertion": action, "expected": expected, "actual": actual, "matched": False}
        return AssertionError(f"{action}: expected {expected!r}, actual {actual!r}")

    def _scope(self, opts):
        scope = self.page
        for frame in opts.get("frames", []):
            scope = scope.frame_locator(frame)
        return scope

    async def _table_cell(self, opts, timeout):
        """Scoped row/cell lookup inside one table locator (never page-global)."""
        table = locator(self.page, opts)
        column = opts.get("column")
        if str(column).lstrip("-").isdigit():
            column_index = int(column)
        else:
            headers = table.locator("th")
            column_index = -1
            for index in range(await headers.count()):
                text = (await headers.nth(index).inner_text(timeout=timeout)).strip()
                if text.lower() == str(column).strip().lower():
                    column_index = index
                    break
            if column_index < 0:
                raise AssertionError(f"table column not found: expected header {column!r}, "
                                     f"actual headers available in the scoped table")
        if "row" in opts and str(opts["row"]).lstrip("-").isdigit():
            row = table.locator("tr").nth(int(opts["row"]))
        else:
            row = table.locator("tr").filter(has_text=str(opts.get("row_text") or "")).first
        cell = row.locator("td").nth(column_index)
        return (await cell.inner_text(timeout=timeout)).strip()

    async def close(self):
        for page, pattern, handler in self.routes:
            if not page.is_closed():
                await page.unroute(pattern, handler)
        self.routes.clear()
        for alias, page in list(self.pages.items()):
            if alias != "main" and not page.is_closed():
                await page.close()
        self.page = self.pages["main"]

    async def perform(self, step, variables):
        action = step["action"]
        # Resolve before any side effect, including locators and request headers.
        value = interpolate(step.get("value"), variables) if action not in {"capture_text", "save_variable", "switch_page", "close_page"} else str(step.get("value") or "")
        opts = resolve(step.get("selector") or {}, variables)
        timeout = self._timeout(opts)
        self.last_result = None
        if action in DESKTOP_ACTIONS:
            # Web recordings may carry desktop steps; they run against the
            # deterministic simulator here, and against the Windows agent /
            # simulator pair when a plan declares runner="windows_desktop".
            if self._desktop is None:
                self._desktop = _desktop_module.DesktopSimulator()
            result = self._desktop.perform({"action": action, "value": value, "selector": opts}, variables)
            self.last_result = result
            await self._pace()
            return result
        if action in MOBILE_ACTIONS:
            if self._mobile is None:
                self._mobile = _mobile_module.MobileSimulator()
            result = self._mobile.perform({"action": action, "value": value, "selector": opts}, variables)
            self.last_result = result
            await self._pace()
            return result
        if opts.get("page"):
            if opts["page"] not in self.pages:
                raise ValueError("Unknown page: " + opts["page"])
            self.page = self.pages[opts["page"]]
        page = self.page
        loc = locator(page, opts) if action not in NO_LOCATOR and opts.get("primary") else None
        if action == "navigate":
            await _goto_resilient(page, check_url(value), self._timeout(opts, 20000))
        elif action in POINTER_ACTIONS and loc is None:
            # A recorded coordinate click has no element to wait for; it clicks the
            # point the operator actually clicked.
            x, y = opts["x"], opts["y"]
            button = "right" if action == "right_click" else "left"
            if action == "hover":
                await page.mouse.move(x, y)
            elif action == "dblclick":
                await page.mouse.dblclick(x, y, button=button)
            else:
                await page.mouse.click(x, y, button=button)
        elif action in {"click", "check", "uncheck", "hover", "dblclick"}:
            await getattr(loc, action)(timeout=timeout)
        elif action == "right_click":
            await loc.click(button="right", timeout=timeout)
        elif action == "fill":
            if loc is None:
                await page.keyboard.insert_text(value)
            else:
                await loc.fill(value, timeout=timeout)
        elif action == "type":
            delay = int(self.speed_profile.get("type_delay_ms") or 0)
            if loc is None:
                await page.keyboard.type(value, delay=delay)
            else:
                await loc.press_sequentially(value, delay=delay, timeout=timeout)
        elif action == "press":
            if opts.get("primary"):
                await locator(page, opts).press(value, timeout=timeout)
            else:
                await page.keyboard.press(value)
        elif action == "select_option":
            await loc.select_option(**({"label": value} if opts.get("by") == "label" else {"value": value}), timeout=timeout)
        elif action == "drag_to":
            await loc.drag_to(locator(page, opts["target"]), timeout=timeout)
        elif action == "scroll":
            await loc.scroll_into_view_if_needed(timeout=timeout)
            await loc.evaluate("(el, y) => el.scrollBy(0, y)", int(value), timeout=timeout)
        elif action.startswith("assert_") and action != "assert_table_cell":
            target = page if action in {"assert_url", "assert_title"} else loc
            methods = {"assert_text": "to_have_text", "assert_contains": "to_contain_text",
                       "assert_value": "to_have_value", "assert_visible": "to_be_visible",
                       "assert_hidden": "to_be_hidden", "assert_enabled": "to_be_enabled",
                       "assert_disabled": "to_be_disabled", "assert_checked": "to_be_checked",
                       "assert_count": "to_have_count", "assert_url": "to_have_url", "assert_title": "to_have_title"}
            assertion = expect(target)
            try:
                if action == "assert_unchecked":
                    await assertion.not_to_be_checked(timeout=timeout)
                else:
                    args = [int(value)] if action == "assert_count" else ([value] if action in {"assert_text", "assert_contains", "assert_value", "assert_url", "assert_title"} else [])
                    await getattr(assertion, methods[action])(*args, timeout=timeout)
            except AssertionError as exc:
                # Re-read the actual state once (short, bounded) so reports can
                # show expected-vs-actual instead of an opaque timeout string.
                actual = None
                try:
                    if action == "assert_url":
                        actual = page.url
                    elif action == "assert_title":
                        actual = await page.title()
                    elif action == "assert_count":
                        actual = await loc.count()
                    elif action in {"assert_value", "save_variable"}:
                        actual = await loc.input_value(timeout=500)
                    elif action in {"assert_visible", "assert_hidden"}:
                        actual = "visible" if await loc.is_visible() else "hidden"
                    elif action in {"assert_checked", "assert_unchecked"}:
                        actual = "checked" if await loc.is_checked() else "unchecked"
                    elif action in {"assert_enabled", "assert_disabled"}:
                        actual = "enabled" if await loc.is_enabled() else "disabled"
                    else:
                        actual = (await loc.inner_text(timeout=500)).strip()
                except Exception:
                    actual = None
                expected = value if action not in {"assert_visible", "assert_enabled", "assert_checked"} else action.removeprefix("assert_")
                if action in {"assert_hidden", "assert_disabled", "assert_unchecked"}:
                    expected = {"assert_hidden": "hidden", "assert_disabled": "disabled", "assert_unchecked": "unchecked"}[action]
                raise self._assertion_failed(action, expected, actual) from exc
            self.last_result = {"assertion": action, "expected": value, "actual": value, "matched": True}
        elif action in {"select_suggestion", "pick_suggestion"}:
            # Wait-aware autocomplete: type, then wait for the suggestion whose
            # text matches and click it. No fixed sleeps anywhere.
            suggestion = str(opts.get("suggestion") or value)
            await loc.fill(str(opts.get("type_text", value)), timeout=timeout)
            scope = self._scope(opts)
            candidates = []
            try:
                candidates.append(scope.get_by_role("option", name=suggestion, exact=False))
            except Exception:
                pass
            candidates.append(scope.locator('[role="option"], [role="listbox"] *').filter(has_text=suggestion))
            candidates.append(scope.locator("li, div, span").filter(has_text=suggestion))
            picked = False
            for candidate in candidates:
                try:
                    first = candidate.first
                    await first.wait_for(state="visible", timeout=min(timeout, 3000))
                    await first.click(timeout=timeout)
                    picked = True
                    break
                except Exception:
                    continue
            if not picked:
                raise self._assertion_failed(action, suggestion, "no matching suggestion appeared")
            self.last_result = {"assertion": action, "selected": suggestion, "matched": True}
        elif action == "table_lookup":
            variables[value] = await self._table_cell(opts, timeout)
            self.last_result = {"captured": value, "text": variables[value]}
        elif action == "assert_table_cell":
            actual = await self._table_cell(opts, timeout)
            if actual != value:
                raise self._assertion_failed(action, value, actual)
            self.last_result = {"assertion": action, "expected": value, "actual": actual, "matched": True}
        elif action in {"wait_visible", "wait_hidden"}:
            await loc.wait_for(state=action.removeprefix("wait_"), timeout=timeout)
        elif action in {"save_variable", "capture_text"}:
            variables[value] = await (loc.input_value(timeout=timeout) if action == "save_variable" else loc.inner_text(timeout=timeout))
        elif action == "new_page":
            alias = opts["alias"]
            if alias in self.pages or len(self.pages) >= 4:
                raise ValueError("Page alias exists or four-page limit reached")
            new = await page.context.new_page()
            self.pages[alias] = new
            self.page = new
            await _goto_resilient(new, check_url(value), timeout)
        elif action == "open_popup":
            alias = opts["alias"]
            if alias in self.pages or len(self.pages) >= 4:
                raise ValueError("Page alias exists or four-page limit reached")
            async with page.expect_popup(timeout=timeout) as pending:
                await loc.click(timeout=timeout)
            self.page = self.pages[alias] = await pending.value
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        elif action == "switch_page":
            if value not in self.pages or self.pages[value].is_closed():
                raise ValueError("Unknown or closed page: " + value)
            self.page = self.pages[value]
        elif action == "close_page":
            if value == "main" or value not in self.pages:
                raise ValueError("Only a known secondary page may be closed")
            await self.pages.pop(value).close()
            self.page = self.pages["main"]
        elif action == "dialog":
            # Register before clicking; the handler resolves the dialog to unblock
            # the action. Task is explicitly awaited, not an unowned listener.
            import asyncio
            async def handle():
                dialog = await page.wait_for_event("dialog", timeout=timeout)
                message, kind = dialog.message, dialog.type
                if opts.get("decision", "accept") == "dismiss":
                    await dialog.dismiss()
                else:
                    await dialog.accept(prompt_text=value)
                if "message" in opts and message != opts["message"]:
                    raise AssertionError("Dialog message did not match")
                if "dialog_type" in opts and kind != opts["dialog_type"]:
                    raise AssertionError("Dialog type did not match")
            task = asyncio.create_task(handle())
            try:
                # Yield to register the event waiter; no timed delay.
                await asyncio.sleep(0)
                await loc.click(timeout=timeout)
                await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        elif action == "upload_file":
            await loc.set_input_files(upload_buffer(opts), timeout=timeout)
        elif action == "download":
            if not self.artifact_dir:
                raise ValueError("A run artifact directory is required for downloads")
            async with page.expect_download(timeout=timeout) as pending:
                await loc.click(timeout=timeout)
            download = await pending.value
            try:
                if value and download.suggested_filename != value:
                    raise AssertionError("Download filename did not match")
                path = await download.path()
                if not path or Path(path).stat().st_size > MAX_FILE:
                    raise ValueError("Download missing or exceeds 512 KiB")
                # Never trust browser filenames as paths; copies occur only on demand.
                import uuid
                name = "download-" + uuid.uuid4().hex + ".bin"
                await download.save_as(str(self.artifact_dir / name))
                self.last_result = {"download": name, "bytes": Path(path).stat().st_size}
            finally:
                await download.delete()
        elif action == "api_request":
            response = await page.context.request.fetch(
                check_url(value), method=opts.get("method", "GET").upper(),
                headers=opts.get("headers", {}), data=opts.get("body"),
                timeout=timeout, fail_on_status_code=False, max_redirects=0)
            try:
                if response.status != opts.get("status", 200):
                    raise AssertionError(f"HTTP status: expected {opts.get('status', 200)}, got {response.status}")
                if "json_path" in opts or "capture" in opts or "expected" in opts:
                    content = await response.body()
                    if len(content) > MAX_FILE:
                        raise ValueError("API response exceeds 512 KiB")
                    actual = json_field(json.loads(content), opts.get("json_path"))
                    if "expected" in opts and actual != opts["expected"]:
                        raise AssertionError("API JSON value did not match expected value")
                    if opts.get("capture"):
                        variables[opts["capture"]] = actual
                self.last_result = {"http_status": response.status}
            finally:
                await response.dispose()
        elif action == "mock_route":
            if len(self.routes) >= 10:
                raise ValueError("At most ten network mocks per recording")
            async def handler(route):
                if opts.get("abort"):
                    await route.abort()
                else:
                    await route.fulfill(status=int(opts.get("status", 200)),
                                        content_type=opts.get("content_type", "application/json"),
                                        body=str(opts.get("body", "{}")))
            await page.route(value, handler)
            self.routes.append((page, value, handler))
        elif action == "clear_routes":
            for owner, pattern, handler in self.routes:
                if not owner.is_closed():
                    await owner.unroute(pattern, handler)
            self.routes.clear()
        else:
            raise ValueError("Unsupported action: " + action)
        await self._pace()
        return self.last_result
