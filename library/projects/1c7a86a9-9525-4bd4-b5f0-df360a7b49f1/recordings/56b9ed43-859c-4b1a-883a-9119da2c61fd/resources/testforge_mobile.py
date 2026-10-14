"""Mobile & rugged-device runner: Appium/lab bridge + deterministic simulator.

Two execution modes share one validated step contract:

* **Connected Device / Appium Lab Mode** (``TF_MOBILE_HUB_URL`` +
  ``TF_MOBILE_HUB_TOKEN``): each step is POSTed as JSON to the hub.  Zebra
  ``scan_barcode`` steps are dispatched as a DataWedge-style broadcast-intent
  payload (see :func:`datawedge_payload`).  The token is a bearer credential
  only; it is never returned or persisted by this module.
* **Deterministic Local Mobile/Zebra Simulator** (default elsewhere): an
  in-process handheld state machine (current app, elements, orientation,
  network mode, last barcode) plus the interactive fixture
  ``app/static/mobile-zebra-demo.html``.  Warehouse/retail barcode flows can
  be authored, draft-tested, replayed and reported on any host.

Dependency-free so portable exports can copy it as ``testforge_mobile.py``.
Sequential only; no process is started and no code is evaluated.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import struct
import urllib.request
import zlib

MOBILE_ACTIONS = {
    "app_launch", "tap_element", "long_press", "swipe", "mobile_type",
    "device_key", "rotate_orientation", "toggle_network_mode", "scan_barcode",
    "assert_mobile_text", "assert_mobile_visible",
}
MOBILE_PLATFORMS = {"android", "ios", "zebra"}
MOBILE_LOCATOR_KEYS = {
    "accessibility_id", "resource_id", "ios_predicate", "ios_class_chain",
    "xpath", "text",
}
BARCODE_SYMBOLOGIES = {"EAN-13", "CODE-128", "QR", "GS1-128"}
DEVICE_KEYS = {"Enter", "Tab", "Escape", "Backspace", "Scan", "SCAN_TRIGGER",
               *(f"F{i}" for i in range(1, 13))}
ORIENTATIONS = {"portrait", "landscape"}
NETWORK_MODES = {"online", "offline", "wifi_on", "wifi_off", "airplane_on",
                 "airplane_off", "data_on", "data_off"}
MAX_SCREENSHOT_BYTES = 512 * 1024

# Deterministic product lookup for the simulator.  Keys are barcode payloads,
# never credentials or personal data.
PRODUCTS = {
    "4006381333931": "TestForge Scanner Demo Item",
    "TF-QR-001": "TestForge QR Demo Item",
    "1234567890128": "Warehouse Demo Carton",
}


def validate_mobile_selector(selector, action):
    if not isinstance(selector, dict):
        raise ValueError("Mobile locator must be an object")
    if action in {"app_launch", "device_key", "rotate_orientation", "toggle_network_mode", "swipe"}:
        return
    if action == "scan_barcode":
        return  # symbology/barcode_data are validated by validate_barcode
    if not any(selector.get(k) for k in MOBILE_LOCATOR_KEYS):
        raise ValueError(action + " needs a mobile locator (accessibility_id, resource_id, "
                                 "ios_predicate, ios_class_chain, xpath or text)")


def _ean13_ok(data):
    if not re.fullmatch(r"\d{13}", data or ""):
        return False
    digits = [int(c) for c in data]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits[:12]))
    return (10 - total % 10) % 10 == digits[12]


def validate_barcode(selector, value):
    selector = selector or {}
    symbology = str(selector.get("symbology") or "CODE-128").upper()
    if symbology not in BARCODE_SYMBOLOGIES:
        raise ValueError("scan_barcode symbology must be one of " + ", ".join(sorted(BARCODE_SYMBOLOGIES)))
    data = str(selector.get("barcode_data") or value or "")
    if not data or len(data) > 256:
        raise ValueError("scan_barcode needs barcode_data (the step value or selector.barcode_data)")
    if symbology == "EAN-13" and not _ean13_ok(data):
        raise ValueError("EAN-13 barcode_data must be 13 digits with a valid check digit")
    if symbology == "GS1-128" and not (data.startswith("(01)") or data.startswith("01")):
        raise ValueError("GS1-128 barcode_data must start with the (01) application identifier")
    if symbology in {"CODE-128", "QR"} and not all(32 <= ord(c) <= 126 for c in data):
        raise ValueError(symbology + " barcode_data must be printable ASCII")
    return symbology, data


def datawedge_payload(symbology, barcode_data, profile_name="TestForge"):
    """The DataWedge intent-injection payload dispatched for a Zebra scan."""
    return {
        "platform": "zebra",
        "intent": {
            "action": "android.intent.action.MAIN",
            "broadcast": True,
            "datawedge_api": "com.symbol.datawedge.api.SOFTWARE_TRIGGER" ,
            "extras": {
                "com.symbol.datawedge.profile": profile_name,
                "com.symbol.datawedge.data_string": barcode_data,
                "com.symbol.datawedge.label_type": symbology,
            },
        },
        "symbology": symbology,
        "barcode_data": barcode_data,
    }


def datawedge_profile(profile_name="TestForge"):
    """A portable DataWedge profile manifest exported next to mobile runners."""
    return {
        "profile_name": profile_name,
        "profile_enabled": True,
        "intent_output": {
            "enabled": True,
            "intent_action": "com.testforge.DATAWEDGE_SCAN",
            "intent_delivery": "broadcast",
        },
        "barcode_input": {
            "enabled": True,
            "symbologies": sorted(BARCODE_SYMBOLOGIES),
        },
        "keystroke_output": {"enabled": False},
        "note": ("Import into DataWedge on an authorised Zebra device. The Lab "
                 "bridge injects barcode_data as an intent extra; no real "
                 "device is claimed by the local simulator."),
    }


def _locator_key(selector):
    return tuple(str(selector.get(k) or "") for k in sorted(MOBILE_LOCATOR_KEYS))


def synthetic_png(title, lines, width=360, height=640):
    digest = hashlib.sha256((title + "|" + "|".join(lines)).encode()).digest()
    accent = (30, 120 + digest[1] % 80, 90)
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if y < 54:
                row.extend(accent)
            elif 70 <= y < 560 and 14 <= x < width - 14 and (y - 70) % 34 < 22:
                shade = 238 if ((y - 70) // 34) % 2 == 0 else 218
                row.extend((shade, shade, shade))
            else:
                row.extend((18, 22, 30))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    return png


class MobileSimulator:
    """In-process Android/iOS/Zebra handheld state machine."""

    def __init__(self, platform="android", device_model="simulator", os_version="",
                 orientation="portrait", artifact_dir=None):
        if platform not in MOBILE_PLATFORMS:
            raise ValueError("platform must be android, ios or zebra")
        self.platform = platform
        self.device_model = str(device_model or "simulator")[:80]
        self.os_version = str(os_version or "")[:40]
        if orientation not in ORIENTATIONS:
            raise ValueError("orientation must be portrait or landscape")
        self.orientation = orientation
        self.artifact_dir = artifact_dir
        self.app = None
        self.network = "online"
        self.last_barcode = None
        self.screenshot_count = 0
        self.last_result = None
        # Seeded elements are keyed by any locator that names them; a first
        # reference to another locator materialises a visible element, exactly
        # like the generic screens of the demo fixture.
        self.elements = {}
        self._seed("home_title", "Warehouse Home" if platform == "zebra" else "Home")
        self._seed("scan_result", "")
        self._seed("product_name", "")
        self._seed("quantity", "1")
        self._seed("status", "Ready")

    def _seed(self, key, text):
        self.elements[key] = {"text": str(text), "visible": True, "key": key}

    def _find(self, selector):
        for key_field in ("accessibility_id", "resource_id", "text", "ios_predicate", "ios_class_chain", "xpath"):
            wanted = selector.get(key_field)
            if wanted and str(wanted) in self.elements:
                return self.elements[str(wanted)]
        key = "|".join(str(selector.get(k) or "") for k in sorted(MOBILE_LOCATOR_KEYS)) or "element"
        if key not in self.elements:
            label = str(selector.get("text") or selector.get("accessibility_id") or "Element")
            self.elements[key] = {"text": label if selector.get("text") else "", "visible": True, "key": key}
        return self.elements[key]

    def screenshot_bytes(self):
        lines = [f"{e['key']}: {e['text']}" for e in list(self.elements.values())[:10]]
        title = f"{self.platform} {self.app or 'home'} ({self.orientation})"
        return synthetic_png(title, lines)

    def save_screenshot(self, directory, index):
        from pathlib import Path
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"mobile-step-{index}.png"
        (folder / name).write_bytes(self.screenshot_bytes())
        return name

    def perform(self, step, variables=None):
        variables = variables if variables is not None else {}
        action = str(step.get("action") or "").lower()
        if action not in MOBILE_ACTIONS:
            raise ValueError("Unsupported mobile action: " + action)
        selector = step.get("selector") or {}
        validate_mobile_selector(selector, action)
        value = "" if step.get("value") is None else str(step.get("value"))
        self.last_result = None
        if action == "app_launch":
            self.app = value or str(selector.get("app_id") or selector.get("bundle_id") or "demo.app")
            result = {"action": action, "app": self.app, "platform": self.platform, "launched": True}
        elif action in {"tap_element", "long_press"}:
            element = self._find(selector)
            result = {"action": action, "element": element["key"], "text": element["text"],
                      "long_press": action == "long_press"}
            if element["key"] == "scan_result" or "scan" in element["key"].lower():
                result["hint"] = "Use scan_barcode to inject a hardware scan"
        elif action == "swipe":
            direction = (value or str(selector.get("direction") or "up")).lower()
            if direction not in {"up", "down", "left", "right"}:
                raise ValueError("swipe value must be up, down, left or right")
            result = {"action": action, "direction": direction}
        elif action == "mobile_type":
            element = self._find(selector)
            element["text"] = value
            result = {"action": action, "element": element["key"], "text": value}
        elif action == "device_key":
            key = value or str(selector.get("key") or "")
            if key not in DEVICE_KEYS and not re.fullmatch(r"F([1-9]|1[0-2])", key or ""):
                raise ValueError("device_key must be Enter, Tab, F1-F12, Escape, Backspace or Scan")
            result = {"action": action, "key": key}
            if key in {"Scan", "SCAN_TRIGGER"}:
                result["hint"] = "Hardware scan trigger armed; scan_barcode injects the payload"
        elif action == "rotate_orientation":
            wanted = (value or str(selector.get("orientation") or "")).lower()
            if wanted not in ORIENTATIONS:
                raise ValueError("rotate_orientation value must be portrait or landscape")
            self.orientation = wanted
            result = {"action": action, "orientation": wanted}
        elif action == "toggle_network_mode":
            mode = (value or str(selector.get("mode") or "")).lower()
            if mode not in NETWORK_MODES:
                raise ValueError("toggle_network_mode value must be one of " + ", ".join(sorted(NETWORK_MODES)))
            self.network = "offline" if mode in {"offline", "wifi_off", "airplane_on", "data_off"} else "online"
            result = {"action": action, "mode": mode, "network": self.network}
        elif action == "scan_barcode":
            symbology, data = validate_barcode(selector, value)
            product = PRODUCTS.get(data)
            self.last_barcode = {"symbology": symbology, "barcode_data": data}
            self.elements["scan_result"]["text"] = f"{symbology}:{data}"
            self.elements["product_name"]["text"] = product or ""
            self.elements["status"]["text"] = f"Item found: {product}" if product else f"Scanned {symbology} code"
            result = {"action": action, **self.last_barcode, "product": product,
                      "scan_result": self.elements["scan_result"]["text"],
                      "status_text": self.elements["status"]["text"],
                      "datawedge": datawedge_payload(symbology, data) if self.platform == "zebra" else None}
        elif action == "assert_mobile_text":
            element = self._find(selector)
            actual = element["text"]
            self.last_result = {"assertion": action, "expected": value, "actual": actual, "matched": actual == value}
            if actual != value:
                raise AssertionError(f"assert_mobile_text: expected {value!r}, actual {actual!r}")
            result = dict(self.last_result)
        elif action == "assert_mobile_visible":
            element = self._find(selector)
            actual = "visible" if element["visible"] else "hidden"
            self.last_result = {"assertion": action, "expected": "visible", "actual": actual,
                                "matched": element["visible"]}
            if not element["visible"]:
                raise AssertionError(f"assert_mobile_visible: expected 'visible', actual {actual!r}")
            result = dict(self.last_result)
        else:  # pragma: no cover
            raise ValueError("Unsupported mobile action: " + action)
        self.screenshot_count += 1
        result.setdefault("platform", self.platform)
        result.setdefault("orientation", self.orientation)
        self.last_result = result
        return result


def hub_configured(environ=None):
    env = os.environ if environ is None else environ
    return bool(env.get("TF_MOBILE_HUB_URL") and env.get("TF_MOBILE_HUB_TOKEN"))


class MobileHub:
    """Authenticated Appium/WebDriver lab bridge."""

    def __init__(self, url=None, token=None, timeout=15):
        self.url = (url if url is not None else os.environ.get("TF_MOBILE_HUB_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("TF_MOBILE_HUB_TOKEN", "")
        self.timeout = timeout
        if not self.url or not self.token:
            raise ValueError("Mobile hub needs TF_MOBILE_HUB_URL and TF_MOBILE_HUB_TOKEN")
        from urllib.parse import urlparse
        parsed = urlparse(self.url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("TF_MOBILE_HUB_URL must be HTTPS (HTTP only for a loopback hub)")

    def dispatch(self, step):
        selector = step.get("selector") or {}
        payload = {"step": step}
        if step.get("action") == "scan_barcode":
            symbology, data = validate_barcode(selector, step.get("value"))
            payload["datawedge"] = datawedge_payload(symbology, data)
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.url + "/commands", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - validated https/loopback
            result = json.loads(response.read(MAX_SCREENSHOT_BYTES + 65536))
        if not isinstance(result, dict):
            raise ValueError("Mobile hub returned a non-object response")
        encoded = result.pop("screenshot_base64", None)
        if encoded:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("Mobile hub returned invalid screenshot data") from exc
            if len(data) > MAX_SCREENSHOT_BYTES:
                raise ValueError("Mobile hub screenshot exceeds 512 KiB")
            result["screenshot_bytes"] = len(data)
        result.pop("token", None)
        return result


class MobileRunner:
    """Facade used by plan execution: hub when configured, else simulator."""

    def __init__(self, mode="auto", platform="android", device_model="simulator",
                 os_version="", orientation="portrait", artifact_dir=None):
        if mode not in {"auto", "hub", "simulator"}:
            raise ValueError("mobile mode must be auto, hub or simulator")
        self.mode = mode
        self.simulator = MobileSimulator(platform, device_model, os_version, orientation, artifact_dir)
        self.artifact_dir = artifact_dir
        self._hub = None

    @property
    def live_configured(self):
        return hub_configured()

    def _use_hub(self):
        return self.mode == "hub" or (self.mode == "auto" and self.live_configured)

    async def perform(self, step, variables=None):
        import asyncio
        if self._use_hub():
            if self._hub is None:
                self._hub = MobileHub()
            result = await asyncio.to_thread(self._hub.dispatch, step)
            result.setdefault("mode", "hub")
            return result
        result = self.simulator.perform(step, variables)
        result = {**result, "mode": "simulator"}
        if self.artifact_dir:
            result["screenshot"] = self.simulator.save_screenshot(
                self.artifact_dir, self.simulator.screenshot_count)
        return result
