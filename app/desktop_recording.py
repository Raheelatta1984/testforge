"""Interactive Windows-desktop recording sessions.

The dashboard host does not pretend to be a Windows VM. Without a configured
Windows agent this module renders and records against the deterministic desktop
simulator. With TF_WINDOWS_AGENT_URL/TOKEN it delegates launch, UIA actions and
screenshots to the authenticated Windows agent. Only one interactive recorder
is held by this module at a time; callers coordinate it with browser recording.
"""
from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from app import library_store
from app.db import Recording, RecordingStep, SessionLocal
from app.desktop_runner import (
    DESKTOP_APP_CATALOG,
    DesktopRunner,
    WINDOWS_VERSIONS,
    agent_configured,
    validate_custom_app,
)
from app.step_runtime import normalize_step

MAX_DESKTOP_STEPS = 500
MAX_SCREENSHOT_BYTES = 512 * 1024
INTERACTIVE_ACTIONS = {
    "ui_click", "ui_double_click", "ui_right_click", "ui_type", "ui_select", "ui_check",
}
ACTIVE_SESSIONS: dict[str, "DesktopRecordingSession"] = {}
_SESSION_LOCK = asyncio.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    value = value or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _db_time(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def registered_apps() -> list[dict]:
    """Return built-in and user-registered desktop profiles for the recorder UI."""
    rows = [
        {"id": app["app_id"], "name": app["window_title"],
         "process_name": app["process_name"], "framework": app["framework"],
         "kind": app["kind"], "builtin": True}
        for app in DESKTOP_APP_CATALOG.values()
    ]
    try:
        # Reuse the exact bounded profiles exposed in Future planning.
        from app.roadmap_routes import _custom_apps
        custom = _custom_apps()
    except Exception:
        custom = []
    rows.extend({**app, "id": app["app_id"], "name": app["window_title"], "builtin": False}
                for app in custom if isinstance(app, dict) and app.get("app_id"))
    return rows


def _application(app_id: str) -> tuple[dict, bool]:
    if not isinstance(app_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", app_id):
        raise ValueError("Choose a registered desktop application")
    builtin = DESKTOP_APP_CATALOG.get(app_id)
    if builtin is not None:
        return {**builtin, "controls": [dict(control) for control in builtin["controls"]]}, True
    try:
        from app.roadmap_routes import _custom_apps
        custom = _custom_apps()
    except Exception:
        custom = []
    profile = next((item for item in custom if item.get("app_id") == app_id), None)
    if profile is None:
        raise ValueError("That desktop application is not registered")
    return validate_custom_app(profile), False


def _profile_selector(profile: dict) -> dict:
    return {key: profile.get(key) for key in ("app_id", "window_title", "process_name", "class_name")
            if profile.get(key)}


def _plan_shell(profile: dict, *, builtin: bool, windows_version: str) -> dict:
    matrix_profile = {
        "runner": "windows_desktop",
        "framework": profile["framework"],
        "desktop_app": profile["app_id"],
        "windows_version": windows_version,
    }
    if not builtin:
        matrix_profile["app_profile"] = profile
    return {
        "version": 1,
        "matrix": [matrix_profile],
        "setup": [{"action": "window_launch", "selector": _profile_selector(profile),
                   "value": "", "label": "Launch " + profile["window_title"]}],
        "steps": [],
        "teardown": [{"action": "window_close", "selector": {"app_id": profile["app_id"]},
                       "value": "", "label": "Close " + profile["window_title"]}],
        "variables": {},
        "dataset": [{}],
        "retries": 0,
        "execution_speed": 1.0,
    }


def _normalise_screenshot(data: bytes) -> bytes:
    """Convert agent images to a bounded PNG; persisted evidence is one image/step."""
    if not data:
        raise ValueError("The Windows target did not return a screenshot for this step")
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise ValueError("Windows screenshot exceeds the 512 KiB evidence limit")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
            image.thumbnail((1280, 800))
            while True:
                output = io.BytesIO()
                palette = image.quantize(colors=256, method=Image.Quantize.FASTOCTREE,
                                         dither=Image.Dither.NONE)
                palette.save(output, format="PNG", compress_level=3)
                payload = output.getvalue()
                if len(payload) <= MAX_SCREENSHOT_BYTES:
                    return payload
                width, height = image.size
                if width <= 320 or height <= 200:
                    raise ValueError("Windows screenshot cannot be reduced below the evidence limit")
                image = image.resize((max(320, int(width * .8)), max(200, int(height * .8))))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Windows target returned an unreadable screenshot") from exc


def _matched_control(result: dict) -> dict | None:
    for key in ("matched_control", "control_info", "element"):
        candidate = result.get(key)
        if isinstance(candidate, dict):
            return candidate
    candidate = result.get("control")
    return candidate if isinstance(candidate, dict) else None


def _short_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:120]


class DesktopRecordingSession:
    """One app, one version target, one live frame and an append-only step log."""

    def __init__(self, recording_id: str, profile: dict, *, builtin: bool,
                 windows_version: str, purpose: str = ""):
        self.recording_id = recording_id
        self.profile = profile
        self.builtin = builtin
        self.windows_version = windows_version
        self.purpose = purpose
        self.mode = "agent" if agent_configured() else "simulator"
        self.status = "starting"
        self.started_at = _iso()
        self.latest_frame: bytes | None = None
        self.step_count = 0
        self.error: str | None = None
        self.runner: DesktopRunner | None = None
        self._action_lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self.status in {"starting", "live"}

    def public_state(self) -> dict:
        return {
            "active": self.active,
            "recording_id": self.recording_id,
            "status": self.status,
            "mode": self.mode,
            "app_id": self.profile["app_id"],
            "app_name": self.profile["window_title"],
            "windows_version": self.windows_version,
            "purpose": self.purpose,
            "started_at": self.started_at,
            "step_count": self.step_count,
            "error": self.error,
        }

    async def start(self) -> dict:
        self.runner = DesktopRunner(
            mode=self.mode,
            app_id=self.profile["app_id"],
            app_profile=None if self.builtin else self.profile,
            framework=self.profile["framework"],
            windows_version=self.windows_version,
        )
        command = {"action": "window_launch", "selector": _profile_selector(self.profile), "value": ""}
        result = await self.runner.perform(command, {}, capture_screenshot=True)
        frame = result.pop("_screenshot_data", None)
        if frame is None and self.mode == "simulator":
            frame = self.runner.simulator.screenshot_bytes()
        if frame is None:
            raise ValueError("The Windows agent must return screenshot_base64 for window_launch")
        self.latest_frame = _normalise_screenshot(frame)
        self.status = "live"
        return {**self.public_state(), "frame": base64.b64encode(self.latest_frame).decode("ascii")}

    def _target(self, x: float, y: float, field_name: str) -> dict:
        if self.mode == "simulator" and self.runner is not None:
            hit = self.runner.simulator.control_at(x, y)
            if hit:
                # Prefer the actual UIA identity. Keep the relative point too as
                # a recovery path if an app build changes its generated ID.
                return {
                    "automation_id": hit.get("automation_id"),
                    "name": hit.get("name"),
                    "control_type": hit.get("control_type"),
                    "class_name": hit.get("class_name"),
                    "x": x, "y": y,
                }
        name = _short_name(field_name)
        if not name:
            name = f"Screen target {round(x * 100)}% x {round(y * 100)}%"
        return {"field_name": name, "x": x, "y": y, "coordinate_fallback": True}

    async def input(self, body: dict) -> dict:
        if not self.active or self.runner is None:
            raise RuntimeError("Desktop recording is not active")
        action = str(body.get("action") or "").strip().lower()
        if action not in INTERACTIVE_ACTIONS:
            raise ValueError("Choose click, double-click, right-click, type, select or check")
        raw_x, raw_y = body.get("x"), body.get("y")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (raw_x, raw_y)):
            raise ValueError("Click the Windows screen to choose a target location")
        x, y = float(raw_x), float(raw_y)
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError("Screen coordinates must be between 0 and 1")
        value = body.get("value", "")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError("Step value must be plain text")
        value = str(value)
        if len(value) > 4096:
            raise ValueError("Step value is limited to 4096 characters")
        field_name = body.get("field_name", "")
        if not isinstance(field_name, str):
            raise ValueError("Field reference name must be text")

        async with self._action_lock:
            if not self.active or self.runner is None:
                raise RuntimeError("Desktop recording is no longer active")
            if self.step_count >= MAX_DESKTOP_STEPS:
                raise ValueError("A desktop recording can contain at most 500 steps")
            order = self.step_count + 1
            selector = self._target(x, y, field_name)
            raw_step = {"action": action, "value": value, "selector": selector,
                        "label": f"{action.replace('ui_', '').replace('_', ' ').title()} · " +
                                 _short_name(field_name or selector.get("name") or selector.get("field_name") or "desktop control")}
            step = normalize_step(raw_step, order)
            started = _utc_now()
            started_clock = time.perf_counter()
            result = await self.runner.perform(step, {}, capture_screenshot=True)
            finished = _utc_now()
            duration_ms = round((time.perf_counter() - started_clock) * 1000, 1)
            frame = result.pop("_screenshot_data", None)
            if frame is None and self.mode == "simulator":
                frame = self.runner.simulator.screenshot_bytes()
            if frame is None:
                raise ValueError("The Windows agent did not return a screenshot for this step")
            frame = _normalise_screenshot(frame)

            # Prefer the actual UIA response. The simulator's hit-test has
            # already resolved exact metadata into this action's selector.
            matched = _matched_control(result)
            if not matched and any(selector.get(key) for key in
                                   ("automation_id", "name", "control_type", "class_name")):
                matched = {key: selector[key] for key in
                           ("automation_id", "name", "control_type", "class_name", "framework_id")
                           if selector.get(key)}
            if matched:
                exact = {key: matched[key] for key in
                         ("automation_id", "name", "control_type", "class_name", "framework_id")
                         if matched.get(key)}
                if exact:
                    selector = {**exact, "x": x, "y": y}
                    step["selector"] = selector
                    if matched.get("name"):
                        step["label"] = f"{action.replace('ui_', '').replace('_', ' ').title()} · {_short_name(matched['name'])}"
            elif result.get("control") and isinstance(result.get("control"), str) and self.mode == "agent":
                # A readable control name but no AutomationId still gets a stable
                # named locator; coordinates remain the last-resort fallback.
                actual_name = _short_name(result["control"])
                selector = {"name": actual_name, "field_name": actual_name,
                            "x": x, "y": y}
                step["selector"] = selector
                step["label"] = f"{action.replace('ui_', '').replace('_', ' ').title()} · {actual_name}"
                matched = {"name": actual_name}

            screenshot_name = await asyncio.to_thread(
                library_store.attach_step_image, self.recording_id, order, frame)
            if not screenshot_name:
                raise RuntimeError("Could not save the desktop step screenshot")
            with SessionLocal() as db:
                recording = db.get(Recording, self.recording_id)
                if recording is None:
                    raise RuntimeError("Desktop recording no longer exists")
                plan = recording.execution_plan or _plan_shell(
                    self.profile, builtin=self.builtin, windows_version=self.windows_version)
                plan = {**plan, "steps": list(plan.get("steps") or []) + [{
                    "action": step["action"], "value": step["value"],
                    "selector": step["selector"], "label": step["label"],
                }]}
                from app.plan_runtime import normalize_plan
                recording.execution_plan = normalize_plan(plan)
                screenshot_path = (library_store._recording_dir(recording.project_id, self.recording_id)
                                   / "resources" / screenshot_name)
                row = RecordingStep(
                    recording_id=self.recording_id,
                    order=order,
                    action=step["action"],
                    value=step["value"],
                    label=step["label"],
                    selector=step["selector"],
                    screenshot_path=str(screenshot_path),
                    started_at=_db_time(started),
                    finished_at=_db_time(finished),
                    duration_ms=duration_ms,
                    repeat_count=1,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                saved_step = {
                    "id": row.id,
                    "order": row.order,
                    "action": row.action,
                    "value": row.value,
                    "label": row.label,
                    "selector": row.selector,
                    "started_at": _iso(started),
                    "finished_at": _iso(finished),
                    "duration_ms": duration_ms,
                    "screenshot": f"/api/recordings/{self.recording_id}/steps/{row.id}/screenshot",
                }
            self.step_count = order
            self.latest_frame = frame
            library_store._CACHE["plan_catalog"] = None
            return {
                "step": saved_step,
                "frame": base64.b64encode(frame).decode("ascii"),
                "status": self.status,
                "mode": self.mode,
                "windows_version": self.windows_version,
                "matched_control": matched,
            }

    async def stop(self) -> dict:
        async with self._action_lock:
            if self.status == "stopped":
                return self.public_state()
            self.status = "stopping"
            close_error = None
            try:
                if self.runner is not None:
                    command = {"action": "window_close",
                               "selector": {"app_id": self.profile["app_id"]}, "value": ""}
                    await self.runner.perform(command, {}, capture_screenshot=False)
            except Exception as exc:
                close_error = type(exc).__name__
            finally:
                self.runner = None
                self.latest_frame = None
                self.status = "stopped"
                if close_error:
                    self.error = "Windows close command failed: " + close_error
                ACTIVE_SESSIONS.pop(self.recording_id, None)
            return self.public_state()


async def start_session(recording_id: str, app_id: str, windows_version: str,
                        purpose: str = "") -> dict:
    """Start one app screen, refusing overlap with another desktop session."""
    if windows_version not in WINDOWS_VERSIONS:
        raise ValueError("Choose Windows 10 or Windows 11")
    profile, builtin = _application(app_id)
    async with _SESSION_LOCK:
        other = next((session for session in ACTIVE_SESSIONS.values() if session.active), None)
        if other is not None and other.recording_id != recording_id:
            raise RuntimeError("Only one Windows desktop recording can be open at a time")
        session = DesktopRecordingSession(recording_id, profile, builtin=builtin,
                                          windows_version=windows_version, purpose=purpose)
        ACTIVE_SESSIONS[recording_id] = session
        try:
            return await session.start()
        except Exception:
            # A launch can succeed even when the first screenshot fails. Make a
            # best-effort close so a rejected start never leaves the app behind.
            try:
                await session.stop()
            except Exception:
                ACTIVE_SESSIONS.pop(recording_id, None)
            raise


def get_session(recording_id: str | None = None) -> DesktopRecordingSession | None:
    if recording_id:
        session = ACTIVE_SESSIONS.get(recording_id)
        return session if session and session.active else None
    return next((session for session in ACTIVE_SESSIONS.values() if session.active), None)


def active_recording_ids() -> set[str]:
    return {recording_id for recording_id, session in ACTIVE_SESSIONS.items() if session.active}


async def stop_session(recording_id: str) -> dict | None:
    session = ACTIVE_SESSIONS.get(recording_id)
    if session is None:
        return None
    return await session.stop()


def frame_for(recording_id: str) -> bytes | None:
    session = ACTIVE_SESSIONS.get(recording_id)
    return session.latest_frame if session and session.active else None
