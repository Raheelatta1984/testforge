"""HTTP surface for the Devices tab.

    GET  /api/devices          the catalogue: groups plus every profile
    POST /api/devices/run      run one deterministic flow on one device profile

A device run has web legs and, for the zebra target, one simulator leg. A web
leg drives the bundled Chromium at the profile's viewport, DPR, user agent and
mobile/touch flags over a deterministic fixture served from loopback; the
simulator leg replays the same scan flow through app/mobile_runner.py. No real
handset, tenant or device hub is involved, and the response says so.
"""
from __future__ import annotations

import asyncio
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app import devices
from app.mobile_runner import MobileRunner

router = APIRouter()

_RUN_LOCK = threading.Lock()

STATIC_DIR = Path(__file__).resolve().parent / "static"

TARGETS = {
    "zebra": {"fixture": "mobile-zebra-demo.html", "label": "Zebra warehouse scan"},
    "erp": {"fixture": "erp-demo.html", "label": "ERP purchase order"},
    "crm": {"fixture": "crm-demo.html", "label": "CRM opportunity"},
    "erp-crm": {"fixture": "erp-crm-demo.html", "label": "ERP/CRM tenant packs"},
}

WEB_ENGINE = "chromium"


def _fixture_server():
    """Serve app/static from loopback on an ephemeral port.

    Device runs must not depend on how this instance is reached (direct port,
    tunnel or preview proxy), so the fixtures get their own loopback server
    for the duration of the run.
    """
    handler = partial(SimpleHTTPRequestHandler, directory=str(STATIC_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Send the run as a JSON object.") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Send the run as a JSON object.")
    return payload


def _web_flows(target: str, base: str) -> list:
    """Deterministic (flow label, plan) pairs for a target; URLs are absolute."""
    css = lambda selector: {"primary": selector}  # noqa: E731 - tiny local helper
    if target == "zebra":
        return [("warehouse scan", {
            "setup": [{"action": "navigate", "value": base + "mobile-zebra-demo.html"}],
            "steps": [
                {"action": "assert_text", "selector": css("#status"), "value": "Ready"},
                {"action": "click", "selector": css("#scan")},
                {"action": "assert_text", "selector": css("#scan_result"),
                 "value": "EAN-13:4006381333931"},
                {"action": "assert_text", "selector": css("#product_name"),
                 "value": "TestForge Scanner Demo Item"},
            ]})]
    if target == "erp":
        return [("purchase order", {
            "setup": [{"action": "navigate", "value": base + "erp-demo.html"}],
            "steps": [
                {"action": "assert_text", "selector": css("#po-status"), "value": "Ready"},
                {"action": "fill", "selector": css("#po-supplier"), "value": "Acme Pty Ltd"},
                {"action": "fill", "selector": css("#po-amount"), "value": "480"},
                {"action": "click", "selector": css("#po-submit")},
                {"action": "assert_text", "selector": css("#po-status"),
                 "value": "PO-2026-001 approved for Acme Pty Ltd: AUD 480.00"},
            ]})]
    if target == "crm":
        return [("opportunity", {
            "setup": [{"action": "navigate", "value": base + "crm-demo.html"}],
            "steps": [
                {"action": "assert_text", "selector": css("#opp-toast"), "value": "Ready"},
                {"action": "fill", "selector": css("#opp-name"), "value": "Globex rollout"},
                {"action": "fill", "selector": css("#opp-stage"), "value": "Proposal"},
                {"action": "click", "selector": css("#opp-save")},
                {"action": "assert_text", "selector": css("#opp-toast"),
                 "value": "Opportunity OPP-0001 saved at stage Proposal"},
            ]})]
    if target == "erp-crm":
        login = lambda user: [  # noqa: E731 - tiny local helper
            {"action": "fill", "selector": css(user), "value": "operator"},
            {"action": "fill", "selector": css('input[name="password"]'), "value": "secret"},
            {"action": "click", "selector": css('[data-testid$="sign-in"]')}]
        return [
            ("dynamics 365 f&o", {
                "setup": [{"action": "navigate",
                           "value": base + "erp-crm-demo.html?pack=ms_dynamics_fo"}],
                "steps": login('[data-testid="d365fo-username"]') + [
                    {"action": "fill", "selector": css('[data-testid="d365fo-sales-order"]'),
                     "value": "SO-1001"},
                    {"action": "click", "selector": css('[data-testid="d365fo-submit"]')},
                    {"action": "assert_text", "selector": css('[data-testid="d365fo-result"]'),
                     "value": "Sales order SO-1001 confirmed in Dynamics 365 F&O"},
                ]}),
            ("salesforce", {
                "setup": [{"action": "navigate",
                           "value": base + "erp-crm-demo.html?pack=salesforce_crm"}],
                "steps": login('[data-testid="sf-username"]') + [
                    {"action": "fill", "selector": css('[data-testid="sf-opportunity"]'),
                     "value": "OPP-1001"},
                    {"action": "click", "selector": css('[data-testid="sf-save"]')},
                    {"action": "assert_text", "selector": css('[data-testid="sf-toast"]'),
                     "value": "Opportunity OPP-1001 saved at stage Qualification"},
                ]}),
        ]
    raise KeyError(target)


def _first_failure(outcome: dict) -> dict | None:
    for case in outcome.get("cases") or []:
        for attempt in case.get("attempts") or []:
            for step in attempt.get("steps") or []:
                if step.get("status") == "failed":
                    return {"stage": step.get("stage"), "step": step.get("step"),
                            "action": step.get("action"),
                            "error_type": step.get("error_type")}
    return None


def _web_leg(label: str, plan: dict, device_id: str, tmp: Path) -> dict:
    from app.plan_runtime import run_plan
    plan = {**plan, "matrix": [{"browser": WEB_ENGINE, "device": device_id}]}
    try:
        outcome = asyncio.run(run_plan(plan, {}, tmp / label.replace(" ", "-")))
    except Exception as exc:
        return {"leg": "web", "flow": label, "status": "error",
                "error_type": type(exc).__name__}
    leg = {"leg": "web", "flow": label, "status": outcome.get("status"),
           "cases": outcome.get("total")}
    failure = _first_failure(outcome)
    if failure:
        leg["failure"] = failure
    return leg


SIMULATOR_SCRIPT = [
    {"action": "app_launch", "value": "warehouse.app"},
    {"action": "scan_barcode",
     "selector": {"symbology": "EAN-13", "barcode_data": "4006381333931"}},
    {"action": "assert_mobile_text", "selector": {"accessibility_id": "product_name"},
     "value": "TestForge Scanner Demo Item"},
    {"action": "assert_mobile_text", "selector": {"accessibility_id": "status"},
     "value": "Item found: TestForge Scanner Demo Item"},
]


async def run_simulator_script(platform: str) -> dict:
    """The zebra scan flow through app/mobile_runner.py (simulator, no hub)."""
    runner = MobileRunner(mode="simulator", platform=platform,
                          device_model=f"{platform} simulator")
    performed = []
    try:
        for step in SIMULATOR_SCRIPT:
            result = await runner.perform(step, {})
            performed.append({"action": step["action"], "status": "passed",
                              "detail": {key: result.get(key) for key in
                                         ("product", "status_text", "app", "barcode_data")
                                         if result.get(key)}})
    except (AssertionError, ValueError) as exc:
        performed.append({"action": step["action"], "status": "failed",
                          "error_type": type(exc).__name__})
        return {"leg": "simulator", "flow": "warehouse scan", "status": "failed",
                "platform": platform, "mode": "simulator", "steps": performed}
    return {"leg": "simulator", "flow": "warehouse scan", "status": "passed",
            "platform": platform, "mode": "simulator", "steps": performed}


def _run_device(device_id: str, target: str) -> dict:
    import tempfile
    started = time.monotonic()
    server, thread = _fixture_server()
    try:
        base = f"http://127.0.0.1:{server.server_port}/"
        legs = []
        with tempfile.TemporaryDirectory(prefix="tf-device-") as tmp:
            for label, plan in _web_flows(target, base):
                legs.append(_web_leg(label, plan, device_id, Path(tmp)))
        if target == "zebra":
            legs.append(asyncio.run(
                run_simulator_script(devices.simulator_platform(device_id))))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    status = "passed" if all(leg["status"] == "passed" for leg in legs) else "failed"
    return {"status": status, "legs": legs,
            "elapsed_seconds": round(time.monotonic() - started, 3)}


@router.get("/api/devices")
def list_devices():
    return {"groups": [{"id": group_id, "name": name} for group_id, name in devices.GROUPS],
            "targets": [{"id": target_id, **spec} for target_id, spec in TARGETS.items()],
            "engine": WEB_ENGINE,
            "devices": devices.DEVICES,
            "note": ("Emulation profiles: a run drives the bundled Chromium at the profile's "
                     "viewport, DPR, user agent and mobile/touch flags, plus the deterministic "
                     "simulator for the zebra scan. No real handset, tenant or device hub.")}


@router.post("/api/devices/run")
async def run_device(request: Request):
    body = await _body(request)
    device_id = body.get("device_id") or body.get("device")
    target = body.get("target")
    profile = devices.get(device_id)
    if profile is None:
        raise HTTPException(404, f"Unknown device: {device_id}")
    if target not in TARGETS:
        raise HTTPException(400, f"Unknown target: {target}. "
                                 f"Choose one of {', '.join(sorted(TARGETS))}.")
    if not _RUN_LOCK.acquire(blocking=False):
        raise HTTPException(429, "Another device run is using the browser. "
                                 "Device runs are sequential; retry when it finishes.")
    try:
        outcome = await asyncio.to_thread(_run_device, profile["id"], target)
    finally:
        _RUN_LOCK.release()
    return {"device": profile, "target": target, "engine": WEB_ENGINE,
            "status": outcome["status"], "legs": outcome["legs"],
            "elapsed_seconds": outcome["elapsed_seconds"],
            "simulator": target == "zebra",
            "note": ("Web legs ran in the bundled Chromium at this profile; the zebra run adds "
                     "a simulator leg from app/mobile_runner.py. No real device was touched.")}
