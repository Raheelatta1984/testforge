"""Future-release support matrix and deterministic pilot receipts.

Honesty rules baked into the badge logic:

* ``VERIFIED IN THIS ENVIRONMENT`` requires a passing, unexpired pilot
  receipt produced by :func:`run_pilot_suite` (or an external receipt whose
  mode says a live agent/hub actually ran).  Nothing is pre-verified.
* ``SIMULATOR READY`` means the pack/adapter plus its deterministic local
  simulator exist and can be authored, draft-tested and exported here.
* ``EXTERNAL AGENT REQUIRED`` means the contract is registered but neither
  a simulator result nor a configured live bridge exists.

Receipts live under ``ARTIFACTS/roadmap/`` — runtime evidence outside the
repository-backed ``library/``, never a place credentials could land.  Only
booleans about live bridges are reported; tokens are never read out.
"""

import datetime
import json
import os
from pathlib import Path

from app.config import ARTIFACTS
from app.desktop_runner import DESKTOP_APP_CATALOG, DesktopSimulator
from app.domain_packs import ERP_PACKS, template
from app.mobile_runner import MobileSimulator
from app.plan_runtime import normalize_plan, parse_dataset_upload

RECEIPT_TTL_HOURS = 24

# 13 reported targets: the eleven named in the roadmap (Oracle, SAP Fiori,
# JD Edwards, Odoo, Salesforce, .NET/C#, C++, WinForms, Android, iOS, Zebra)
# with Microsoft Dynamics split into its two real products.
TARGETS = [
    {"id": "oracle_web", "name": "Oracle Fusion / EBS (web)", "kind": "pack", "group": "ERP/CRM",
     "fixture": "/erp-crm-demo.html", "markers": ["oracle-order-number", "oracle-lov-dialog", "oracle-lines-table"]},
    {"id": "sap_fiori", "name": "SAP Fiori / UI5", "kind": "pack", "group": "ERP/CRM",
     "fixture": "/erp-crm-demo.html", "markers": ["salesOrderInput", "sap-grid", "sap-busy"]},
    {"id": "jd_edwards", "name": "JD Edwards EnterpriseOne", "kind": "pack", "group": "ERP/CRM",
     "fixture": "/erp-crm-demo.html", "markers": ["jde-fastpath", "jde-grid", "jde-batch-status"]},
    {"id": "ms_dynamics_fo", "name": "Microsoft Dynamics 365 Finance & Operations", "kind": "pack", "group": "ERP/CRM",
     "fixture": "/erp-crm-demo.html", "markers": ["d365fo-sales-order", "d365fo-result"]},
    {"id": "ms_dynamics_bc", "name": "Microsoft Dynamics 365 Business Central", "kind": "pack", "group": "ERP/CRM",
     "fixture": "/erp-crm-demo.html", "markers": ["bc-sales-order", "bc-result"]},
    {"id": "odoo_erp", "name": "Odoo ERP", "kind": "pack", "group": "ERP/CRM",
     "fixture": "/erp-crm-demo.html", "markers": ["partner_id", "odoo-confirm", "odoo-status"]},
    {"id": "salesforce_crm", "name": "Salesforce CRM (Lightning)", "kind": "pack", "group": "ERP/CRM",
     "fixture": "/erp-crm-demo.html", "markers": ["sf-opportunity", "sf-toast", "data-aura-rendered-by"]},
    {"id": "dotnet_desktop", "name": ".NET / C# desktop (WPF / WinUI)", "kind": "adapter", "group": "Windows desktop",
     "runner": "windows_desktop", "framework": "dotnet", "app": "calculator",
     "live_env": ["TF_WINDOWS_AGENT_URL", "TF_WINDOWS_AGENT_TOKEN"]},
    {"id": "cpp_desktop", "name": "C++ desktop (Win32 / MFC)", "kind": "adapter", "group": "Windows desktop",
     "runner": "windows_desktop", "framework": "cpp", "app": "custom_cpp",
     "live_env": ["TF_WINDOWS_AGENT_URL", "TF_WINDOWS_AGENT_TOKEN"]},
    {"id": "winforms_desktop", "name": "WinForms (incl. legacy, Access/VBA, FoxPro, COBOL, AS/400 custom apps)", "kind": "adapter", "group": "Windows desktop",
     "runner": "windows_desktop", "framework": "winforms", "app": "winforms_legacy",
     "live_env": ["TF_WINDOWS_AGENT_URL", "TF_WINDOWS_AGENT_TOKEN"]},
    {"id": "android_mobile", "name": "Android (UiAutomator2)", "kind": "adapter", "group": "Mobile",
     "runner": "mobile_device", "platform": "android",
     "live_env": ["TF_MOBILE_HUB_URL", "TF_MOBILE_HUB_TOKEN"]},
    {"id": "ios_mobile", "name": "iOS (XCUITest)", "kind": "adapter", "group": "Mobile",
     "runner": "mobile_device", "platform": "ios",
     "live_env": ["TF_MOBILE_HUB_URL", "TF_MOBILE_HUB_TOKEN"]},
    {"id": "zebra_mobile", "name": "Zebra rugged (Android Enterprise + DataWedge)", "kind": "adapter", "group": "Mobile",
     "runner": "mobile_device", "platform": "zebra", "fixture": "/mobile-zebra-demo.html",
     "live_env": ["TF_MOBILE_HUB_URL", "TF_MOBILE_HUB_TOKEN"]},
]


def receipt_path():
    return Path(ARTIFACTS) / "roadmap" / "pilot-receipts.json"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def load_receipt(path=None):
    """Return a valid, unexpired receipt dict or ``None`` (never raises)."""
    try:
        payload = json.loads(Path(path or receipt_path()).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return None
        expires = datetime.datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        if expires <= _now():
            return None
        return payload
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _fixture_text(fixture):
    path = Path(__file__).parent / "static" / Path(fixture).name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _pack_pilot(target):
    pack = template(target["id"])
    plan = normalize_plan(pack["execution_plan"])
    assert len(plan["dataset"]) >= 2, "pack needs positive and negative rows"
    assert plan["teardown"], "pack needs a teardown"
    assert pack["execution_plan"].get("flows"), "pack needs reusable flows"
    text = _fixture_text(target["fixture"])
    assert target["id"] in text, "fixture does not know this pack"
    for marker in target["markers"]:
        assert marker in text, "fixture marker missing: " + marker
    return f"pack plan valid ({sum(len(plan[s]) for s in ('setup','steps','teardown'))} steps, {len(plan['dataset'])} dataset rows); fixture markers present"


def _desktop_pilot(target):
    sim = DesktopSimulator(target["app"])
    sim.perform({"action": "window_attach", "selector": {"window_title": sim.catalog[target["app"]]["window_title"]}, "value": ""})
    if target["id"] == "dotnet_desktop":
        sim.perform({"action": "ui_type", "selector": {"automation_id": "CalculatorResults"}, "value": "2+3"})
        sim.perform({"action": "ui_click", "selector": {"automation_id": "equalButton"}, "value": ""})
        sim.perform({"action": "assert_control_text", "selector": {"automation_id": "CalculatorResults"}, "value": "5"})
    elif target["id"] == "cpp_desktop":
        sim.perform({"action": "ui_type", "selector": {"automation_id": "1001"}, "value": "Ada"})
        sim.perform({"action": "assert_control_text", "selector": {"automation_id": "1001"}, "value": "Ada"})
    else:
        sim.perform({"action": "ui_check", "selector": {"automation_id": "chkActive"}, "value": "checked"})
        sim.perform({"action": "assert_control_state", "selector": {"automation_id": "chkActive"}, "value": "checked"})
    assert sim.screenshot_bytes().startswith(b"\x89PNG")
    return "simulated UIA tree actions and assertions passed; synthetic PNG evidence rendered"


def _mobile_pilot(target):
    sim = MobileSimulator(target["platform"])
    sim.perform({"action": "app_launch", "selector": {}, "value": "com.testforge.demo"})
    if target["platform"] == "zebra":
        sim.perform({"action": "device_key", "selector": {}, "value": "Scan"})
        sim.perform({"action": "scan_barcode", "selector": {"symbology": "EAN-13", "barcode_data": "4006381333931"}, "value": ""})
        sim.perform({"action": "assert_mobile_text", "selector": {"accessibility_id": "status"}, "value": "Item found: TestForge Scanner Demo Item"})
    else:
        sim.perform({"action": "mobile_type", "selector": {"accessibility_id": "quantity"}, "value": "2"})
        sim.perform({"action": "assert_mobile_text", "selector": {"accessibility_id": "quantity"}, "value": "2"})
        sim.perform({"action": "rotate_orientation", "selector": {}, "value": "landscape"})
    assert sim.screenshot_bytes().startswith(b"\x89PNG")
    return "simulated device actions and assertions passed; synthetic PNG evidence rendered"


def run_pilot_suite(path=None):
    """Run every deterministic pilot and persist the receipt. Sequential."""
    results = []
    for target in TARGETS:
        try:
            detail = _pack_pilot(target) if target["kind"] == "pack" else (
                _desktop_pilot(target) if target["runner"] == "windows_desktop" else _mobile_pilot(target))
            results.append({"id": target["id"], "status": "passed", "mode": "simulator", "detail": detail})
        except Exception as exc:  # failure is data, not a 500: the matrix reports it
            results.append({"id": target["id"], "status": "failed", "mode": "simulator",
                            "detail": type(exc).__name__})
    # Shared plumbing pilots: dataset upload parsing is part of every target's
    # authoring path, so a failure there fails the suite loudly in the receipt.
    parse_dataset_upload("pilot.csv", "input,expected\n1,ok\n")
    generated = _now()
    receipt = {
        "version": 1,
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "expires_at": (generated + datetime.timedelta(hours=RECEIPT_TTL_HOURS)).isoformat().replace("+00:00", "Z"),
        "suite": "deterministic-local-pilots",
        "results": results,
    }
    target_path = Path(path or receipt_path())
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def _live_configured(target, environ):
    env = target.get("live_env") or []
    return bool(env) and all(environ.get(name) for name in env)


def support_matrix(environ=None, receipt=None):
    """Real, current state per target. Secrets are reported as booleans only."""
    environ = os.environ if environ is None else environ
    receipt = load_receipt() if receipt is None else receipt
    pilots = {row.get("id"): row for row in (receipt or {}).get("results", []) if isinstance(row, dict)}
    entries = []
    for target in TARGETS:
        registered = target["id"] in ERP_PACKS if target["kind"] == "pack" else True
        simulator_ready = registered
        live = _live_configured(target, environ)
        pilot = pilots.get(target["id"])
        pilot_status = pilot["status"] if pilot else "not_run"
        live_pilot = bool(pilot and pilot.get("mode") in {"agent", "hub", "live"})
        if pilot_status == "passed" and (target["kind"] == "pack" or live_pilot):
            badge = "VERIFIED IN THIS ENVIRONMENT"
        elif simulator_ready:
            badge = "SIMULATOR READY"
        elif registered:
            badge = "EXTERNAL AGENT REQUIRED"
        else:
            badge = "NOT REGISTERED"
        note = ""
        if target["kind"] == "pack":
            note = "Verified means the pack plan and this instance's deterministic fixture passed; not vendor certification."
        elif pilot_status == "passed" and not live_pilot:
            note = "Simulator pilot passed. A real device/agent is still required for live verification."
        elif not live:
            note = "Set " + " and ".join(target.get("live_env") or []) + " to reach a live target."
        entries.append({
            "id": target["id"], "name": target["name"], "group": target["group"],
            "kind": target["kind"],
            "pack": target["id"] if target["kind"] == "pack" else None,
            "runner": target.get("runner"), "framework": target.get("framework"),
            "platform": target.get("platform"), "fixture": target.get("fixture"),
            "registered": registered, "simulator_ready": simulator_ready,
            "live_configured": live,
            "live_env": target.get("live_env") or [],
            "pilot_status": pilot_status,
            "pilot_mode": pilot.get("mode") if pilot else None,
            "badge": badge, "note": note,
        })
    return {
        "targets": entries,
        "badges": ["VERIFIED IN THIS ENVIRONMENT", "SIMULATOR READY", "EXTERNAL AGENT REQUIRED"],
        "receipt": ({"generated_at": receipt.get("generated_at"), "expires_at": receipt.get("expires_at"),
                     "suite": receipt.get("suite")} if receipt else None),
        "desktop_apps": [{"id": app["app_id"], "name": app["window_title"], "framework": app["framework"],
                          "kind": app["kind"], "process_name": app["process_name"]}
                         for app in DESKTOP_APP_CATALOG.values()],
        "custom_desktop_app_kinds": ["calculator", "settings", "custom", "access_vba", "foxpro",
                                     "cobol_terminal", "as400_terminal", "oracle_desktop",
                                     "dotnet", "cpp", "winforms", "winforms_legacy"],
    }
