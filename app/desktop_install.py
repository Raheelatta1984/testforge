"""Install a custom desktop application on the Windows target, before recording it.

Testers asked to install their own build and then record against it in the same
session. This module is that pre-recording step: it validates an installer
descriptor, sends it to the configured Windows agent, records what the agent
reported, and registers the application so the desktop recorder can launch it.

Two modes, one contract — the same split the desktop runner already uses:

* **Agent mode** (``TF_WINDOWS_AGENT_URL`` + ``TF_WINDOWS_AGENT_TOKEN``): the
  install command is POSTed to the agent's ``/commands`` endpoint as an
  ``app_install`` action over HTTPS with a bearer token. The dashboard host never
  runs an installer and never touches a Windows file system.
* **Simulator mode** (default): nothing is installed anywhere. The request is
  validated, a deterministic install log is produced and the application profile
  is registered against the simulator's control tree, so the whole
  install-then-record flow can be rehearsed on Linux CI.

Nothing here uses ``eval``/``exec`` or a shell. The command sent to the agent is
built from a template for the installer kind, or checked against an allowlist
when the operator supplies one, and shell metacharacters are rejected outright.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import ARTIFACTS
from app.desktop_runner import (
    DESKTOP_APP_KINDS,
    DESKTOP_FRAMEWORKS,
    DesktopRunner,
    agent_configured,
    validate_custom_app,
)

INSTALLS_FILE = Path(ARTIFACTS) / "roadmap" / "desktop-installs.json"
MAX_INSTALLS = 20
MAX_INSTALL_LOG_CHARS = 4096
MAX_COMMAND_CHARS = 500

# Installer kinds and the command template each one gets by default. `{installer}`
# is always quoted by this module, never by the operator.
INSTALLER_KINDS = {
    "msi": 'msiexec.exe /i "{installer}" /qn /norestart',
    "exe": '"{installer}" /S',
    "msix": 'Add-AppxPackage -Path "{installer}"',
    "winget": "winget install --id {installer} --silent --accept-package-agreements --accept-source-agreements",
    "choco": "choco install {installer} -y",
    "portable": 'xcopy "{installer}" "C:\\Program Files\\{name}\\" /E /I /Y',
}
PACKAGE_KINDS = {"winget", "choco"}
FILE_KINDS = {"msi", "exe", "msix", "portable"}
FILE_EXTENSIONS = {
    "msi": (".msi", ".msp"),
    "exe": (".exe",),
    "msix": (".msix", ".msixbundle", ".appx", ".appxbundle"),
    "portable": (".zip", ".exe", ".7z"),
}
# Anything a shell would interpret. Rejected in every operator-supplied string
# that ends up in a command, because the agent runs what we send.
FORBIDDEN = re.compile(r"[&|;`$<>]")
WINDOWS_PATH = re.compile(r"^[A-Za-z]:\\[^\x00-\x1f\"<>|?*]{0,400}$")
PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,100}$")
HTTPS_URL = re.compile(r"^https://[^\s\"<>|]{4,500}$")
# A custom command must start with a known installer, a package manager, or a
# quoted absolute Windows path.
COMMAND_PREFIX = re.compile(
    r'^(msiexec(\.exe)?|winget(\.exe)?|choco(\.exe)?|Add-AppxPackage|"[A-Za-z]:\\[^"]{1,300}")',
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    value = value or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean(value, limit: int, what: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        raise ValueError(f"{what} is limited to {limit} characters")
    if FORBIDDEN.search(text):
        raise ValueError(f"{what} may not contain shell metacharacters (& | ; ` $ < >)")
    return text


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")[:64]
    if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", slug):
        raise ValueError("Derive the application id from a name with letters or digits")
    return slug


def default_command(kind: str, installer: str, name: str, extra_args: str = "") -> str:
    """The command for an installer kind, with the operator's extras appended."""
    template = INSTALLER_KINDS.get(kind)
    if template is None:
        raise ValueError("installer_kind must be one of " + ", ".join(sorted(INSTALLER_KINDS)))
    command = template.format(installer=installer, name=name)
    if extra_args:
        command = f"{command} {extra_args}"
    if len(command) > MAX_COMMAND_CHARS:
        raise ValueError(f"The install command is limited to {MAX_COMMAND_CHARS} characters")
    return command


def validate_install_request(payload: dict) -> dict:
    """Validate an install request; returns a clean, bounded copy.

    Raises ValueError with a sentence an operator can act on.
    """
    if not isinstance(payload, dict):
        raise ValueError("The install request must be an object")
    unknown = set(payload) - {
        "name", "app_id", "installer_kind", "installer", "install_args", "install_command",
        "expected_process", "expected_window_title", "framework", "kind", "version",
        "windows_version", "controls", "uninstall_command",
    }
    if unknown:
        raise ValueError("Unknown install fields: " + ", ".join(sorted(unknown)))

    name = _clean(payload.get("name"), 120, "The application name")
    if not name:
        raise ValueError("Enter the application name so it can be registered")
    app_id = _slug(payload.get("app_id") or name)

    kind = str(payload.get("installer_kind") or "").strip().lower()
    if kind not in INSTALLER_KINDS:
        raise ValueError("installer_kind must be one of " + ", ".join(sorted(INSTALLER_KINDS)))

    raw_installer = re.sub(r"\s+", " ", str(payload.get("installer") or "")).strip()
    if FORBIDDEN.search(raw_installer):
        raise ValueError("The installer reference may not contain shell metacharacters (& | ; ` $ < >)")
    if kind in PACKAGE_KINDS:
        if not PACKAGE_ID.fullmatch(raw_installer):
            raise ValueError(f"A {kind} package id must be letters, digits, dots, dashes or underscores")
        installer = raw_installer
    else:
        if not (WINDOWS_PATH.fullmatch(raw_installer) or HTTPS_URL.fullmatch(raw_installer)):
            raise ValueError(
                "The installer must be an absolute Windows path (C:\\Installers\\AppSetup.msi) "
                "or an https URL the Windows agent can download"
            )
        if raw_installer.startswith("C:\\") or "\\" in raw_installer[:3]:
            suffix = next((ext for ext in FILE_EXTENSIONS[kind]
                           if raw_installer.lower().endswith(ext)), None)
            if suffix is None and not HTTPS_URL.fullmatch(raw_installer):
                raise ValueError(
                    f"A {kind} installer must be one of: " + ", ".join(FILE_EXTENSIONS[kind]))
        installer = raw_installer[:500]
    if FORBIDDEN.search(installer):
        raise ValueError("The installer reference may not contain shell metacharacters")

    install_args = _clean(payload.get("install_args"), 300, "The extra install arguments")
    install_command = _clean(payload.get("install_command"), MAX_COMMAND_CHARS, "The install command")
    if install_command and not COMMAND_PREFIX.match(install_command):
        raise ValueError(
            "A custom install command must start with msiexec, winget, choco, Add-AppxPackage "
            'or a quoted absolute Windows path such as "C:\\Installers\\Setup.exe"'
        )
    command = install_command or default_command(kind, installer, name, install_args)

    framework = str(payload.get("framework") or "dotnet").strip().lower()
    if framework not in DESKTOP_FRAMEWORKS:
        raise ValueError("framework must be dotnet, cpp or winforms")
    app_kind = str(payload.get("kind") or "custom").strip().lower()
    if app_kind not in DESKTOP_APP_KINDS:
        raise ValueError("Unsupported desktop app kind: " + app_kind)

    from app.desktop_runner import WINDOWS_VERSIONS
    windows_version = str(payload.get("windows_version") or "windows_11").strip().lower()
    if windows_version not in WINDOWS_VERSIONS:
        raise ValueError("Choose Windows 10 or Windows 11")

    controls = payload.get("controls") or []
    if not isinstance(controls, list) or len(controls) > 50:
        raise ValueError("controls must be a list of at most 50 desktop controls")

    uninstall_command = _clean(payload.get("uninstall_command"), MAX_COMMAND_CHARS, "The uninstall command")
    if uninstall_command and not COMMAND_PREFIX.match(uninstall_command):
        raise ValueError("A custom uninstall command must start with msiexec, winget, choco or a quoted path")

    return {
        "app_id": app_id,
        "name": name,
        "installer_kind": kind,
        "installer": installer,
        "install_args": install_args,
        "command": command,
        "expected_process": _clean(payload.get("expected_process"), 120, "The process name") or f"{app_id}.exe",
        "expected_window_title": _clean(payload.get("expected_window_title"), 200, "The window title") or name,
        "framework": framework,
        "app_kind": app_kind,
        "version": _clean(payload.get("version"), 60, "The version"),
        "windows_version": windows_version,
        "controls": controls,
        "uninstall_command": uninstall_command,
    }


RECEIPT_FIELDS = ("app_id", "name", "installer_kind", "installer", "command", "mode",
                  "status", "exit_code", "installed_at", "duration_ms", "verified",
                  "verified_at", "controls", "error", "install_log")


def public_receipt(record: dict) -> dict:
    """The bounded install summary the dashboard and the session state may show."""
    receipt = {key: record.get(key) for key in RECEIPT_FIELDS if key in record}
    log = receipt.get("install_log")
    if isinstance(log, list):
        receipt["install_log"] = [str(line)[:400] for line in log[:12]]
    return receipt


def _read_installs() -> list[dict]:
    try:
        payload = json.loads(INSTALLS_FILE.read_text(encoding="utf-8"))
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    except (OSError, ValueError):
        return []


def _write_installs(rows: list[dict]) -> None:
    INSTALLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = INSTALLS_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.replace(temporary, INSTALLS_FILE)


def _register_profile(request: dict, mode: str, status: str, installed_at: str) -> dict:
    """Register the application so the recorder can launch it after the install."""
    profile = validate_custom_app({
        "app_id": request["app_id"],
        "name": request["name"],
        "window_title": request["expected_window_title"],
        "process_name": request["expected_process"],
        "framework": request["framework"],
        "kind": request["app_kind"],
        "controls": request["controls"],
    })
    try:
        from app.roadmap_routes import save_custom_app
        save_custom_app(profile)
    except Exception:
        # Registration is what makes the app selectable after the install, so a
        # failure here must surface rather than leave a receipt for a missing app.
        raise
    return {
        **profile,
        "install_mode": mode,
        "install_status": status,
        "installed_at": installed_at,
    }


def _log_lines(result: dict, mode: str, request: dict) -> list[str]:
    """The install log, bounded, from whatever the target reported."""
    lines: list[str] = []
    raw = result.get("log") if isinstance(result, dict) else None
    if isinstance(raw, str):
        lines.extend(part for part in raw.splitlines() if part.strip())
    elif isinstance(raw, list):
        lines.extend(str(part) for part in raw if str(part).strip())
    if not lines:
        lines = [f"{mode}: install command dispatched for {request['name']}"]
    total = 0
    bounded: list[str] = []
    for line in lines:
        line = line.strip()[:400]
        if total + len(line) > MAX_INSTALL_LOG_CHARS:
            bounded.append("… install log truncated")
            break
        bounded.append(line)
        total += len(line)
    return bounded


def _simulated_install(request: dict) -> dict:
    """A deterministic install, so the flow is testable without a Windows host."""
    return {
        "mode": "simulator",
        "exit_code": 0,
        "installed": True,
        "log": [
            "simulator: no Windows agent is configured, so nothing was installed on a real machine",
            f"simulator: would run {request['command']}",
            f"simulator: registered {request['expected_process']} with window "
            f"\"{request['expected_window_title']}\"",
            "simulator: the recorded steps run against the deterministic control tree",
        ],
    }


def _agent_install(request: dict) -> dict:
    from app.desktop_runner import RemoteWindowsAgent
    agent = RemoteWindowsAgent()
    step = {
        "action": "app_install",
        "selector": {
            "app_id": request["app_id"],
            "window_title": request["expected_window_title"],
            "process_name": request["expected_process"],
        },
        "value": request["command"],
        "label": f"Install {request['name']}",
    }
    context = {
        "windows_version": request["windows_version"],
        "app_id": request["app_id"],
        "installer_kind": request["installer_kind"],
        "installer": request["installer"],
        "install_command": request["command"],
        "uninstall_command": request["uninstall_command"] or None,
        "version": request["version"] or None,
    }
    result = agent.dispatch(step, context=context, include_screenshot=False)
    if not isinstance(result, dict):
        raise ValueError("The Windows agent returned an unreadable install result")
    return {"mode": "agent", **result}


def install_application(payload: dict) -> dict:
    """Validate, install (or simulate) and register one desktop application."""
    request = validate_install_request(payload)
    existing = _read_installs()
    if len([row for row in existing if row.get("app_id") != request["app_id"]]) >= MAX_INSTALLS:
        raise ValueError(f"At most {MAX_INSTALLS} installed desktop applications; remove one first")

    mode = "agent" if agent_configured() else "simulator"
    started = time.perf_counter()
    error = None
    try:
        # Blocking by design: the routes that call this are sync, so FastAPI runs
        # them in a thread pool and the event loop keeps serving the dashboard.
        result = _agent_install(request) if mode == "agent" else _simulated_install(request)
    except Exception as exc:
        result = {"mode": mode, "exit_code": None, "installed": False, "log": [str(exc)[:400]]}
        error = f"{type(exc).__name__}: {exc}"[:400]
    duration_ms = round((time.perf_counter() - started) * 1000, 1)

    exit_code = result.get("exit_code")
    installed = bool(result.get("installed", exit_code in (0, None) and error is None))
    status = "installed" if installed and error is None else "failed"
    installed_at = _iso()
    profile = _register_profile(request, mode, status, installed_at)

    record = {
        "app_id": request["app_id"],
        "name": request["name"],
        "installer_kind": request["installer_kind"],
        "installer": request["installer"],
        "install_args": request["install_args"],
        "command": request["command"],
        "uninstall_command": request["uninstall_command"],
        "expected_process": request["expected_process"],
        "expected_window_title": request["expected_window_title"],
        "framework": request["framework"],
        "kind": request["app_kind"],
        "version": request["version"],
        "windows_version": request["windows_version"],
        "mode": mode,
        "status": status,
        "exit_code": exit_code,
        "installed_at": installed_at,
        "duration_ms": duration_ms,
        "install_log": _log_lines(result, mode, request),
        "error": error,
        "controls": len(profile.get("controls") or []),
    }
    rows = [row for row in existing if row.get("app_id") != request["app_id"]]
    rows.append(record)
    _write_installs(rows)
    return {"install": record, "app": {**profile, "id": profile["app_id"],
                                      "name": profile["window_title"], "builtin": False}}


def installed_apps() -> list[dict]:
    """Installed applications, newest first, for the recorder's install panel."""
    rows = _read_installs()
    return sorted(rows, key=lambda row: str(row.get("installed_at") or ""), reverse=True)


def get_install(app_id: str) -> dict | None:
    if not isinstance(app_id, str) or not app_id:
        return None
    return next((row for row in _read_installs() if row.get("app_id") == app_id), None)


def verify_application(app_id: str) -> dict:
    """Launch and close the installed app once, to prove it opens before recording."""
    record = get_install(app_id)
    if record is None:
        raise ValueError("Install that application before verifying it")
    if record.get("status") != "installed":
        raise ValueError("That installation failed; install it again before verifying")
    mode = "agent" if agent_configured() else "simulator"
    runner = DesktopRunner(mode=mode, app_id=app_id,
                           app_profile={"app_id": app_id,
                                        "name": record["name"],
                                        "window_title": record["expected_window_title"],
                                        "process_name": record["expected_process"],
                                        "framework": record["framework"],
                                        "kind": record["kind"]},
                           framework=record["framework"],
                           windows_version=record.get("windows_version") or "windows_11")
    selector = {"app_id": app_id, "window_title": record["expected_window_title"],
                "process_name": record["expected_process"]}
    launch = asyncio.run(runner.perform({"action": "window_launch", "selector": selector, "value": ""}, {}))
    asyncio.run(runner.perform({"action": "window_close", "selector": {"app_id": app_id}, "value": ""}, {}))
    verified = bool(launch.get("ready") or launch.get("launched"))
    controls = len((launch.get("controls") or [])) or record.get("controls") or 0
    rows = _read_installs()
    for row in rows:
        if row.get("app_id") == app_id:
            row["verified"] = verified
            row["verified_at"] = _iso()
    _write_installs(rows)
    return {"app_id": app_id, "verified": verified, "mode": mode,
            "window_title": launch.get("window_title"), "controls": controls,
            "verified_at": _iso()}


def remove_application(app_id: str) -> dict | None:
    """Forget an install record and unregister the profile it created."""
    rows = _read_installs()
    kept = [row for row in rows if row.get("app_id") != app_id]
    if len(kept) == len(rows):
        return None
    _write_installs(kept)
    try:
        from app.roadmap_routes import remove_custom_app
        remove_custom_app(app_id)
    except Exception:
        pass
    return {"app_id": app_id, "removed": True}


# --- routes -----------------------------------------------------------------
#
# Registered from app/main.py alongside the other desktop routes. Every handler
# is sync on purpose: the install dispatch is a blocking HTTPS call, and FastAPI
# runs sync handlers in a thread pool.

from fastapi import APIRouter, HTTPException  # noqa: E402  (kept next to the routes)

router = APIRouter()


@router.get("/api/desktop/installs")
def desktop_installs():
    """Installed desktop applications, and which mode an install would use."""
    return {
        "installs": installed_apps(),
        "mode": "agent" if agent_configured() else "simulator",
        "installer_kinds": sorted(INSTALLER_KINDS),
        "limit": MAX_INSTALLS,
    }


@router.post("/api/desktop/installs", status_code=201)
def desktop_install(body: dict):
    """Install (or simulate installing) a desktop app so it can be recorded next."""
    try:
        return install_application(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/desktop/installs/{app_id}/verify")
def desktop_install_verify(app_id: str):
    """Open and close the installed app once, before the recording starts."""
    try:
        return verify_application(app_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/api/desktop/installs/{app_id}")
def desktop_install_remove(app_id: str):
    removed = remove_application(app_id)
    if removed is None:
        raise HTTPException(404, "That application has no install record")
    return removed
