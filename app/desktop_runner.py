"""Windows desktop runner: remote UIA agent bridge + deterministic simulator.

Two execution modes share one validated step contract:

* **Remote Windows Agent Mode** (``TF_WINDOWS_AGENT_URL`` +
  ``TF_WINDOWS_AGENT_TOKEN``): each step is POSTed as JSON to the agent's
  ``/commands`` endpoint over HTTPS (plain HTTP is only accepted for a
  loopback agent under test).  The token is sent as a bearer credential and
  is never returned, logged or written to disk by this module.  An agent may
  answer with ``screenshot_base64`` (bounded to 512 KiB) which is kept in
  memory as per-step evidence.
* **Deterministic Local Simulation Mode** (default everywhere else): steps
  run against an in-process simulated UI Automation control tree seeded from
  a catalog of testable desktop applications (Calculator, Settings, Notepad,
  Microsoft Access/VBA, Visual FoxPro, a COBOL terminal, an AS/400 terminal,
  Oracle desktop and generic .NET/C++/WinForms apps).  It renders synthetic
  PNG window screenshots, so CI, draft tests (``POST /api/imports/test``) and
  Linux deployments can verify desktop plans with no Windows machine.

This module is dependency-free so portable exports can copy it verbatim as
``testforge_desktop.py``.  It never starts a process, never uses eval/exec
and runs steps strictly sequentially.
"""

import ast
import base64
import binascii
import hashlib
import json
import os
import re
import struct
import urllib.request
import zlib

DESKTOP_ACTIONS = {
    "window_attach", "ui_click", "ui_double_click", "ui_right_click", "ui_type",
    "ui_select", "ui_check", "ui_menu_invoke", "ui_grid_read",
    "assert_control_text", "assert_control_state", "wait_window_ready",
}
DESKTOP_FRAMEWORKS = {"dotnet", "cpp", "winforms"}
DESKTOP_LOCATOR_KEYS = {
    "automation_id", "control_type", "name", "class_name", "framework_id",
    "window_title", "process_name", "row", "column", "checked",
}
# Application kinds a tester may register as a custom desktop target.  The
# first three are the roadmap frameworks; the rest are the custom/legacy
# applications operators asked to be able to add (Access/VBA, FoxPro, COBOL,
# AS/400, Oracle desktop) plus a free-form custom target.
DESKTOP_APP_KINDS = {
    "calculator", "settings", "notepad", "custom", "access_vba", "foxpro",
    "cobol_terminal", "as400_terminal", "oracle_desktop", "dotnet", "cpp",
    "winforms", "winforms_legacy",
}
MAX_SCREENSHOT_BYTES = 512 * 1024


def _control(automation_id, name, text="", state="enabled", control_type="Edit", class_name=""):
    return {
        "automation_id": automation_id, "name": name, "text": str(text),
        "state": state, "control_type": control_type, "class_name": class_name,
        "checked": False, "selected": "",
    }


def _app(app_id, title, process, class_name, framework, kind, controls):
    return {
        "app_id": app_id, "window_title": title, "process_name": process,
        "class_name": class_name, "framework": framework, "kind": kind,
        "controls": controls,
    }


# Seeded simulated applications.  Every control is addressable by the same
# structured UIA locators a real FlaUI/pywinauto script would use.
DESKTOP_APP_CATALOG = {
    "calculator": _app("calculator", "Calculator", "CalculatorApp.exe", "Windows.UI.Core.CoreWindow", "dotnet", "calculator", [
        _control("CalculatorResults", "Display is 0", "0", control_type="Text", class_name="TextBlock"),
        _control("num2Button", "Two", "2", control_type="Button"),
        _control("num3Button", "Three", "3", control_type="Button"),
        _control("plusButton", "Plus", "+", control_type="Button"),
        _control("equalButton", "Equals", "=", control_type="Button"),
        _control("clearButton", "Clear", "C", control_type="Button"),
    ]),
    "settings": _app("settings", "Settings", "SystemSettings.exe", "Windows.UI.Core.CoreWindow", "dotnet", "settings", [
        _control("SettingsSearchBox", "Search settings", "", control_type="Edit", class_name="TextBox"),
        _control("NetworkToggle", "Wi-Fi", "Off", control_type="CheckBox"),
        _control("SettingsStatus", "Status", "Ready", control_type="Text"),
    ]),
    "notepad": _app("notepad", "Untitled - Notepad", "notepad.exe", "Notepad", "cpp", "notepad", [
        _control("15", "Text Editor", "", control_type="Document", class_name="Edit"),
        _control("FileMenu", "File", "File", control_type="MenuItem"),
    ]),
    "custom_dotnet": _app("custom_dotnet", "Custom .NET Application", "CustomApp.exe", "WindowsForms10.Window", "dotnet", "dotnet", [
        _control("txtName", "Name", "", control_type="Edit"),
        _control("btnSave", "Save", "Save", control_type="Button"),
        _control("lblStatus", "Status", "Ready", control_type="Text"),
        _control("gridOrders", "Orders", "Order 1001 | Ada | Open", control_type="DataGrid"),
    ]),
    "custom_cpp": _app("custom_cpp", "Custom C++ Application", "NativeApp.exe", "AfxFrameOrView140", "cpp", "cpp", [
        _control("1001", "Name", "", control_type="Edit", class_name="Edit"),
        _control("1002", "Save", "Save", control_type="Button", class_name="Button"),
        _control("1003", "Status", "Ready", control_type="Text", class_name="Static"),
    ]),
    "winforms_legacy": _app("winforms_legacy", "Legacy WinForms Application", "LegacyApp.exe", "WindowsForms10.Window.8.app.0", "winforms", "winforms_legacy", [
        _control("txtCustomer", "Customer", "", control_type="Edit"),
        _control("chkActive", "Active", "Active", control_type="CheckBox"),
        _control("cmbStatus", "Status", "Draft", control_type="ComboBox"),
        _control("btnSubmit", "Submit", "Submit", control_type="Button"),
        _control("lblResult", "Result", "Ready", control_type="Text"),
        _control("gridCustomers", "Customers", "1001 | Ada | Active", control_type="DataGrid"),
    ]),
    "access_vba": _app("access_vba", "Microsoft Access - Orders.accdb", "MSACCESS.EXE", "OMain", "winforms", "access_vba", [
        _control("txtOrderId", "Order ID", "", control_type="Edit"),
        _control("btnRunMacro", "Run VBA Macro", "Run VBA Macro", control_type="Button"),
        _control("lblMacroResult", "Macro result", "Ready", control_type="Text"),
        _control("gridOrders", "Orders datasheet", "1001 | Ada | Shipped", control_type="DataGrid"),
    ]),
    "foxpro": _app("foxpro", "Microsoft Visual FoxPro", "vfp9.exe", "_screen", "cpp", "foxpro", [
        _control("txtCommand", "Command Window", "", control_type="Edit"),
        _control("lblFoxResult", "Result", "Ready", control_type="Text"),
    ]),
    "cobol_terminal": _app("cobol_terminal", "COBOL Terminal - CUSTINQ", "cobolrun.exe", "ConsoleWindowClass", "cpp", "cobol_terminal", [
        _control("fldCustomer", "CUSTOMER NUMBER", "", control_type="Edit"),
        _control("lblBalance", "BALANCE", "0.00", control_type="Text"),
        _control("btnInquire", "Inquire", "Inquire", control_type="Button"),
    ]),
    "as400_terminal": _app("as400_terminal", "AS/400 - WRKACTJOB", "acslaunch_win-64.exe", "SunAwtFrame", "cpp", "as400_terminal", [
        _control("fldCommand", "Command line", "", control_type="Edit"),
        _control("lblJobStatus", "Job status", "Ready", control_type="Text"),
        _control("btnEnter", "Enter", "Enter", control_type="Button"),
    ]),
    "oracle_desktop": _app("oracle_desktop", "Oracle Forms - Order Entry", "ifrun60.exe", "OraForms", "cpp", "oracle_desktop", [
        _control("txtOrderNumber", "Order Number", "", control_type="Edit"),
        _control("btnQuery", "Query", "Query", control_type="Button"),
        _control("lblOrderStatus", "Order Status", "Ready", control_type="Text"),
    ]),
}


def validate_custom_app(profile):
    """Validate a custom desktop application profile; returns a clean copy."""
    if not isinstance(profile, dict):
        raise ValueError("desktop app profile must be an object")
    unknown = set(profile) - {"app_id", "name", "window_title", "process_name", "class_name", "framework", "kind", "controls"}
    if unknown:
        raise ValueError("Unknown desktop app profile fields: " + ", ".join(sorted(unknown)))
    kind = str(profile.get("kind") or "custom").strip().lower()
    if kind not in DESKTOP_APP_KINDS:
        raise ValueError("Unsupported desktop app kind: " + kind)
    framework = str(profile.get("framework") or ("winforms" if kind in {"winforms", "winforms_legacy", "access_vba"} else "dotnet")).strip().lower()
    if framework not in DESKTOP_FRAMEWORKS:
        raise ValueError("framework must be dotnet, cpp or winforms")
    app_id = str(profile.get("app_id") or profile.get("name") or "custom").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", app_id):
        # A friendly display name ("Orders (Access)") becomes a slug rather
        # than an error, so operators can register apps by name.
        app_id = re.sub(r"[^a-z0-9]+", "-", app_id).strip("-")[:64] or "custom"
    title = str(profile.get("window_title") or profile.get("name") or "Custom Application")[:200]
    controls = profile.get("controls") or []
    if not isinstance(controls, list) or len(controls) > 50:
        raise ValueError("desktop app controls must be a list of at most 50")
    cleaned = []
    for item in controls:
        if not isinstance(item, dict):
            raise ValueError("desktop app controls must be objects")
        cleaned.append(_control(
            str(item.get("automation_id") or item.get("name") or "control")[:120],
            str(item.get("name") or item.get("automation_id") or "Control")[:120],
            str(item.get("text") or "")[:500],
            str(item.get("state") or "enabled")[:40],
            str(item.get("control_type") or "Edit")[:60],
            str(item.get("class_name") or "")[:120],
        ))
    if not cleaned:
        cleaned = [
            _control("txtInput", "Input", "", control_type="Edit"),
            _control("btnOk", "OK", "OK", control_type="Button"),
            _control("lblStatus", "Status", "Ready", control_type="Text"),
        ]
    return _app(app_id, title, str(profile.get("process_name") or "CustomApp.exe")[:120],
                str(profile.get("class_name") or "")[:120], framework, kind, cleaned)


def catalog_with_custom(custom_profiles=None):
    catalog = {key: _clone_app(app) for key, app in DESKTOP_APP_CATALOG.items()}
    for profile in custom_profiles or []:
        app = validate_custom_app(profile)
        catalog[app["app_id"]] = app
    return catalog


def _clone_app(app):
    return {**app, "controls": [dict(c) for c in app["controls"]]}


def _locator_key(selector):
    return tuple(str(selector.get(k) or "") for k in
                 ("automation_id", "control_type", "name", "class_name", "framework_id", "window_title"))


def validate_desktop_selector(selector, action):
    """Validate the structured UIA locator carried by a desktop step."""
    if not isinstance(selector, dict):
        raise ValueError("Desktop locator must be an object")
    if action == "window_attach":
        if not (selector.get("window_title") or selector.get("process_name") or selector.get("class_name")):
            raise ValueError("window_attach needs window_title, process_name or class_name")
        return
    if action == "wait_window_ready":
        return  # the attached window (or a window_title) is enough
    if action == "ui_menu_invoke":
        if not (selector.get("automation_id") or selector.get("name") or selector.get("class_name")):
            return  # the menu path itself may be the step value
        return
    if not any(selector.get(k) for k in ("automation_id", "name", "class_name", "control_type")):
        raise ValueError(action + " needs a UIA locator (automation_id, name, class_name or control_type)")


def _safe_eval(expression):
    """Evaluate the Calculator's tiny arithmetic; anything else is refused."""
    tree = ast.parse(expression, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return walk(node.operand) if isinstance(node.op, ast.UAdd) else -walk(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = walk(node.left), walk(node.right)
            return {ast.Add: left + right, ast.Sub: left - right,
                    ast.Mult: left * right, ast.Div: left / right}[type(node.op)]
        raise ValueError("unsupported expression")

    result = walk(tree)
    return str(int(result)) if float(result).is_integer() else str(round(result, 8))


def synthetic_png(title, lines, width=640, height=360):
    """A deterministic PNG 'window screenshot' rendered without any imaging library."""
    digest = hashlib.sha256((title + "|" + "|".join(lines)).encode()).digest()
    accent = (40 + digest[0] % 120, 90 + digest[1] % 100, 180)
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if y < 36:  # title bar
                row.extend(accent)
            elif 48 <= y < 330 and 16 <= x < width - 16 and (y - 48) % 28 < 18:
                shade = 235 if ((y - 48) // 28) % 2 == 0 else 215
                row.extend((shade, shade, shade + 5 if shade < 250 else 255))
            else:
                row.extend((24, 28, 38))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    return png


class DesktopSimulator:
    """In-process simulated UI Automation tree. One app attached at a time."""

    def __init__(self, app_id=None, app_profile=None, framework=None, artifact_dir=None):
        self.catalog = catalog_with_custom([app_profile] if app_profile else [])
        self.artifact_dir = artifact_dir
        self.app = None
        self.framework = framework
        self.last_result = None
        self.screenshot_count = 0
        if app_id:
            self.attach({"window_title": self.catalog[app_id]["window_title"]} if app_id in self.catalog else {"window_title": app_id})

    # -- attachment ---------------------------------------------------------
    def attach(self, selector, value=""):
        wanted_title = str(selector.get("window_title") or value or "").lower()
        wanted_process = str(selector.get("process_name") or "").lower()
        wanted_class = str(selector.get("class_name") or "").lower()
        for app in self.catalog.values():
            if wanted_title and wanted_title not in app["window_title"].lower():
                continue
            if wanted_process and wanted_process != app["process_name"].lower():
                continue
            if wanted_class and wanted_class != app["class_name"].lower():
                continue
            self.app = _clone_app(app)
            self.framework = self.framework or app["framework"]
            return {"window_title": app["window_title"], "process_name": app["process_name"],
                    "framework": app["framework"], "app_id": app["app_id"], "ready": True}
        raise ValueError("No simulated window matches window_attach (title/process/class): "
                         + (wanted_title or wanted_process or wanted_class or "?"))

    def _require_app(self):
        if self.app is None:
            # A plan that starts with a control action attaches the generic app,
            # mirroring a runner that attaches to the already-open window.
            self.attach({"window_title": "Custom .NET Application"})
        return self.app

    def _find(self, selector):
        app = self._require_app()
        for control in app["controls"]:
            if selector.get("automation_id") and control["automation_id"] != selector["automation_id"]:
                continue
            if selector.get("name") and control["name"] != selector["name"]:
                continue
            if selector.get("class_name") and control["class_name"] != selector["class_name"]:
                continue
            if selector.get("control_type") and control["control_type"] != selector["control_type"]:
                continue
            return control
        # Custom/generic targets: a first reference materialises the control, as
        # a real tree would simply contain it. Seeded catalog controls above are
        # matched strictly first, so assertions on seeded text stay meaningful.
        control = _control(str(selector.get("automation_id") or selector.get("name") or "control")[:120],
                           str(selector.get("name") or selector.get("automation_id") or "Control")[:120],
                           "", control_type=str(selector.get("control_type") or "Edit")[:60],
                           class_name=str(selector.get("class_name") or "")[:120])
        app["controls"].append(control)
        return control

    # -- evidence -----------------------------------------------------------
    def screenshot_bytes(self):
        app = self._require_app()
        lines = [f"{c['name']}: {c['text']}" for c in app["controls"][:10]]
        return synthetic_png(app["window_title"], lines)

    def save_screenshot(self, directory, index):
        from pathlib import Path
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"desktop-step-{index}.png"
        (folder / name).write_bytes(self.screenshot_bytes())
        return name

    # -- steps ----------------------------------------------------------------
    def perform(self, step, variables=None):
        variables = variables if variables is not None else {}
        action = str(step.get("action") or "").lower()
        if action not in DESKTOP_ACTIONS:
            raise ValueError("Unsupported desktop action: " + action)
        selector = step.get("selector") or {}
        validate_desktop_selector(selector, action)
        value = "" if step.get("value") is None else str(step.get("value"))
        self.last_result = None
        if action == "window_attach":
            result = {"action": action, **self.attach(selector, value)}
        elif action == "wait_window_ready":
            if selector.get("window_title") and self.app is None:
                self.attach(selector, value)
            app = self._require_app()
            result = {"action": action, "window_title": app["window_title"], "ready": True}
        elif action in {"ui_click", "ui_double_click", "ui_right_click"}:
            control = self._find(selector)
            result = {"action": action, "control": control["name"], "clicked": True}
            app = self._require_app()
            if app["kind"] == "calculator":
                display = next(c for c in app["controls"] if c["automation_id"] == "CalculatorResults")
                if control["automation_id"] == "equalButton":
                    try:
                        display["text"] = _safe_eval(display["text"])
                    except Exception:
                        display["text"] = "Error"
                    display["name"] = "Display is " + display["text"]
                    result.update(control="Equals", text=display["text"])
                elif control["automation_id"] == "clearButton":
                    display["text"] = "0"
                elif control["text"] and display["text"] in {"0", "Error"}:
                    display["text"] = control["text"]
                elif control["text"]:
                    display["text"] += control["text"]
            if control["name"] in {"Save", "Submit", "Run VBA Macro", "Query", "Inquire", "Enter", "OK"}:
                for candidate in app["controls"]:
                    if candidate["control_type"] == "Text" and candidate is not control:
                        candidate["text"] = "Saved" if control["name"] in {"Save", "Submit", "OK"} else "Completed"
                        break
            result["text"] = control["text"]
        elif action == "ui_type":
            control = self._find(selector)
            control["text"] = value
            if control["control_type"] == "Text" or control["automation_id"] == "CalculatorResults":
                control["name"] = control["name"].split(" is ")[0] + " is " + value if control["automation_id"] == "CalculatorResults" else control["name"]
            result = {"action": action, "control": control["name"], "text": value}
        elif action == "ui_select":
            control = self._find(selector)
            control["selected"] = value
            control["text"] = value
            result = {"action": action, "control": control["name"], "selected": value}
        elif action == "ui_check":
            control = self._find(selector)
            wanted = value.strip().lower()
            control["checked"] = wanted not in {"false", "0", "no", "unchecked", "off"}
            control["state"] = "checked" if control["checked"] else "unchecked"
            result = {"action": action, "control": control["name"], "checked": control["checked"]}
        elif action == "ui_menu_invoke":
            path = value or str(selector.get("name") or "")
            if not path:
                raise ValueError("ui_menu_invoke needs a menu path value such as File>Save")
            result = {"action": action, "menu": path, "invoked": True}
            app = self._require_app()
            if path.lower().endswith("save"):
                for candidate in app["controls"]:
                    if candidate["control_type"] == "Text":
                        candidate["text"] = "Saved"
                        break
        elif action == "ui_grid_read":
            control = self._find(selector)
            name = value or "grid_text"
            if not re.fullmatch(r"[\w.-]{1,100}", name):
                raise ValueError("ui_grid_read value must be a simple variable name")
            variables[name] = control["text"]
            result = {"action": action, "control": control["name"], "captured": name, "text": control["text"]}
        elif action == "assert_control_text":
            control = self._find(selector)
            actual = control["text"]
            self.last_result = {"assertion": action, "expected": value, "actual": actual, "matched": actual == value}
            if actual != value:
                raise AssertionError(f"assert_control_text: expected {value!r}, actual {actual!r}")
            result = dict(self.last_result)
        elif action == "assert_control_state":
            control = self._find(selector)
            actual = "checked" if control.get("checked") else control["state"]
            if value.strip().lower() in {"checked", "unchecked"}:
                actual = "checked" if control.get("checked") else "unchecked"
            self.last_result = {"assertion": action, "expected": value, "actual": actual,
                                "matched": actual.lower() == value.strip().lower()}
            if not self.last_result["matched"]:
                raise AssertionError(f"assert_control_state: expected {value!r}, actual {actual!r}")
            result = dict(self.last_result)
        else:  # pragma: no cover - action set checked above
            raise ValueError("Unsupported desktop action: " + action)
        self.screenshot_count += 1
        result.setdefault("framework", (self.app or {}).get("framework", self.framework))
        result.setdefault("window_title", (self.app or {}).get("window_title"))
        self.last_result = result
        return result


def agent_configured(environ=None):
    env = os.environ if environ is None else environ
    return bool(env.get("TF_WINDOWS_AGENT_URL") and env.get("TF_WINDOWS_AGENT_TOKEN"))


class RemoteWindowsAgent:
    """Authenticated HTTPS bridge to a Windows UIA/FlaUI/WinAppDriver worker."""

    def __init__(self, url=None, token=None, timeout=15):
        self.url = (url if url is not None else os.environ.get("TF_WINDOWS_AGENT_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("TF_WINDOWS_AGENT_TOKEN", "")
        self.timeout = timeout
        if not self.url or not self.token:
            raise ValueError("Remote Windows agent needs TF_WINDOWS_AGENT_URL and TF_WINDOWS_AGENT_TOKEN")
        from urllib.parse import urlparse
        parsed = urlparse(self.url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("TF_WINDOWS_AGENT_URL must be HTTPS (HTTP only for a loopback agent)")

    def dispatch(self, step):
        body = json.dumps({"step": step}).encode()
        request = urllib.request.Request(
            self.url + "/commands", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - validated https/loopback
            payload = json.loads(response.read(MAX_SCREENSHOT_BYTES + 65536))
        if not isinstance(payload, dict):
            raise ValueError("Windows agent returned a non-object response")
        encoded = payload.pop("screenshot_base64", None)
        if encoded:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("Windows agent returned invalid screenshot data") from exc
            if len(data) > MAX_SCREENSHOT_BYTES:
                raise ValueError("Windows agent screenshot exceeds 512 KiB")
            payload["screenshot_bytes"] = len(data)
        payload.pop("token", None)
        return payload


class DesktopRunner:
    """Facade used by plan execution: agent when configured, else simulator."""

    def __init__(self, mode="auto", app_id=None, app_profile=None, framework=None, artifact_dir=None):
        if mode not in {"auto", "agent", "simulator"}:
            raise ValueError("desktop mode must be auto, agent or simulator")
        self.mode = mode
        self.simulator = DesktopSimulator(app_id=app_id, app_profile=app_profile,
                                          framework=framework, artifact_dir=artifact_dir)
        self.artifact_dir = artifact_dir
        self._agent = None

    @property
    def live_configured(self):
        return agent_configured()

    def _use_agent(self):
        return self.mode == "agent" or (self.mode == "auto" and self.live_configured)

    async def perform(self, step, variables=None):
        import asyncio
        if self._use_agent():
            if self._agent is None:
                self._agent = RemoteWindowsAgent()
            result = await asyncio.to_thread(self._agent.dispatch, step)
            result.setdefault("mode", "agent")
            return result
        result = self.simulator.perform(step, variables)
        result = {**result, "mode": "simulator"}
        if self.artifact_dir:
            result["screenshot"] = self.simulator.save_screenshot(
                self.artifact_dir, self.simulator.screenshot_count)
        return result
