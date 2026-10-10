"""GitHub-backed library with caching and verified current-branch publication.

Projects, variables, recordings, and the files a recording depends on live
under ``library/`` in this repository. The dashboard reads that tree, not
leftover database rows. A new project or recording is written here and, unless
publishing is turned off, committed and pushed to the checked-out branch when possible.

``TF_LIBRARY_DIR`` relocates the tree (the harness uses a copy).
``TF_LIBRARY_PUBLISH=0`` writes the files but does not commit or push.

Performance improvements:
- catalog.json fast path for project listing
- in-memory cache with TTL and mtime checks
- individual steps remain editable
- verified push to the checked-out branch, with a one-minute retry in the API
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import logger
from app import github_api
from app.db import (
    Project,
    Recording,
    RecordingStep,
    SessionLocal,
    Variable,
    generate_uuid,
)

LOCK = threading.RLock()
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

# --- Caching layer for super fast loads ---
_CACHE: dict[str, Any] = {
    "projects": None,  # (mtime, timestamp, data)
    "catalog_mtime": 0,
    "catalog_data": None,
    "catalog_ts": 0,
    "recordings": {},  # project_id -> (mtime, ts, data)
    "variables": {},  # project_id -> (mtime, ts, data)
    "materialize_mtime": 0,
    "materialize_ts": 0,
}
CACHE_TTL_PROJECTS = 10  # seconds
CACHE_TTL_RECORDINGS = 5
CACHE_TTL_CATALOG = 5
CACHE_TTL_MATERIALIZE = 15


class LibraryError(Exception):
    pass


class NotFound(LibraryError):
    pass


class PublishError(LibraryError):
    pass


def require_id(value: str, label: str = "id") -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise LibraryError(f"Unsafe {label}")
    return value


def library_dir() -> Path:
    raw = os.environ.get("TF_LIBRARY_DIR")
    path = Path(raw) if raw else Path(__file__).resolve().parents[1] / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def publish_mode() -> str:
    """How a save can reach GitHub: ``checkout``, ``api`` or ``disabled``.

    ``checkout`` is the normal case — the library sits inside a git working tree
    and is committed and pushed with the git binary. ``api`` covers the hosted
    container, which is built from ``app/`` and ``library/`` only and therefore has
    no ``.git``: there the GitHub REST API commits the same files, provided
    ``TF_GITHUB_REPO`` and a token are configured. ``disabled`` means neither, and
    every message the dashboard shows must say so instead of promising a retry.
    """
    flag = os.environ.get("TF_LIBRARY_PUBLISH")
    if flag is not None and flag.strip() != "":
        if flag.strip().lower() not in {"1", "true", "yes", "on"}:
            return "disabled"
        return "checkout" if git_root() is not None else ("api" if github_api.enabled() else "disabled")
    if git_root() is not None:
        return "checkout"
    return "api" if github_api.enabled() else "disabled"


def publish_enabled() -> bool:
    return publish_mode() != "disabled"


def publish_disabled_reason() -> str | None:
    """Why a save cannot be published, in the words the dashboard should show.

    Composed rather than either/or: a deployment can have no checkout *and* have
    publishing switched off, and reporting only one of the two sends the operator
    looking in the wrong place.
    """
    if publish_mode() != "disabled":
        return None
    parts: list[str] = []
    if git_root() is None:
        parts.append(
            "No .git directory at or above the library folder, so there is no "
            "branch to publish to; the library is served from local files."
        )
    flag = os.environ.get("TF_LIBRARY_PUBLISH")
    if flag is not None and flag.strip().lower() in {"0", "false", "no", "off"}:
        parts.append("Publishing is also switched off with TF_LIBRARY_PUBLISH=0.")
    else:
        api = github_api.describe()
        parts.append(
            api.get("reason")
            or "No GitHub API credentials are configured (TF_GITHUB_REPO with TF_GITHUB_TOKEN)."
        )
    return " ".join(parts)


def git_root() -> Path | None:
    current = library_dir().resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def project_repo_path(project_id: str) -> str:
    return f"library/projects/{project_id}/project.json"


def recording_repo_path(project_id: str, recording_id: str) -> str:
    return f"library/projects/{project_id}/recordings/{recording_id}/recording.json"


def variables_repo_path(project_id: str) -> str:
    return f"library/projects/{project_id}/variables.json"


def jenkins_script(steps) -> str:
    lines = [
        "pipeline {",
        "  agent any",
        "  stages {",
        "    stage('TestForge') {",
        "      steps {",
    ]
    for step in steps or []:
        action = step.get("action") if isinstance(step, dict) else getattr(step, "action", "")
        label = step.get("label") if isinstance(step, dict) else getattr(step, "label", "")
        # Show repeat info in Jenkinsfile
        repeat = 1
        if isinstance(step, dict):
            repeat = step.get("repeat") or (step.get("selector") or {}).get("repeat") or 1
            if isinstance(repeat, dict):
                repeat = repeat.get("times") or 1
        if isinstance(repeat, int) and repeat > 1:
            lines.append(f"        echo 'Executing {action or ''} on {label or ''} x{repeat}'")
        else:
            lines.append(f"        echo 'Executing {action or ''} on {label or ''}'")
        # Handle loop steps
        if action == "loop" or action == "repeat":
            lines.append(f"        echo '{label or 'Repeat block'}'")
    lines.extend(["      }", "    }", "  }", "}", ""])
    return "\n".join(lines)


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat() + "Z"
    if value:
        return str(value)
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").replace("Z", "")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.utcnow().replace(microsecond=0)


def _dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # atomic write to avoid partial files during save
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _project_dir(project_id: str) -> Path:
    return library_dir() / "projects" / require_id(project_id, "project id")


def _recording_dir(project_id: str, recording_id: str) -> Path:
    return _project_dir(project_id) / "recordings" / require_id(recording_id, "recording id")


def _run_git(args: list[str], cwd: Path, timeout: int = 60) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"git {' '.join(args[:2])} timed out") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git failed").strip().splitlines()
        raise PublishError(detail[-1][:500] if detail else "git failed")
    return (result.stdout or "").strip()


def _https_repo(url: str) -> str:
    text = (url or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text[len("git@github.com:"):]
    return text or "https://github.com/Raheelatta1984/testforge"


def _project_payload(data: dict, published: bool | None = None) -> dict:
    project_id = data["id"]
    payload = {
        "id": project_id,
        "name": data.get("name") or "",
        "base_url": data.get("base_url") or "",
        "industry_type": data.get("industry_type") or "Generic",
        "created_at": _iso(data.get("created_at")),
        "source": "repository",
        "repository_path": project_repo_path(project_id),
    }
    if published is not None:
        payload["published"] = published
    return payload


def _step_payload(step: dict) -> dict:
    payload = {
        "id": step.get("id"),
        "order": step.get("order"),
        "action": step.get("action"),
        "value": step.get("value"),
        "label": step.get("label"),
        "selector": step.get("selector"),
    }
    if step.get("screenshot"):
        payload["screenshot"] = step["screenshot"]
    # Preserve repeat info
    if step.get("repeat") is not None:
        payload["repeat"] = step["repeat"]
    # Also check selector for repeat
    sel = step.get("selector") or {}
    if isinstance(sel, dict) and sel.get("repeat") is not None:
        payload["repeat"] = sel.get("repeat")
    if step.get("repeat_count") and step.get("repeat_count") != 1:
        payload["repeat"] = step.get("repeat_count")
    return payload


def _recording_payload(data: dict, *, with_steps: bool, published: bool | None = None) -> dict:
    steps = list(data.get("steps") or [])
    payload = {
        "id": data["id"],
        "project_id": data["project_id"],
        "parent_id": data.get("parent_id"),
        "name": data.get("name") or "",
        "start_url": data.get("start_url") or "",
        "status": data.get("status") or "active",
        "tags": data.get("tags") or "",
        "created_at": _iso(data.get("created_at")),
        "step_count": len(steps),
        "source": "repository",
        "repository_path": recording_repo_path(data["project_id"], data["id"]),
        "resources": list(data.get("resources") or []),
    }
    if published is not None:
        payload["published"] = published
    if with_steps:
        payload["steps"] = [_step_payload(step) for step in steps]
    return payload


def _read_project(project_id: str) -> dict | None:
    path = _project_dir(project_id) / "project.json"
    if not path.is_file():
        return None
    data = _load(path)
    data["id"] = project_id
    return data


def _read_variables(project_id: str) -> list[dict]:
    path = _project_dir(project_id) / "variables.json"
    if not path.is_file():
        return []
    payload = _load(path)
    return payload if isinstance(payload, list) else []


def _resource_names(project_id: str, recording_id: str) -> list[str]:
    folder = _recording_dir(project_id, recording_id) / "resources"
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.iterdir() if path.is_file())


def _read_recording_file(project_id: str, recording_id: str) -> dict | None:
    path = _recording_dir(project_id, recording_id) / "recording.json"
    if not path.is_file():
        return None
    data = _load(path)
    data["id"] = recording_id
    data["project_id"] = project_id
    data["resources"] = _resource_names(project_id, recording_id)
    steps = data.get("steps") or []
    data["steps"] = sorted(steps, key=lambda step: step.get("order") or 0)
    return data


def _iter_project_ids() -> list[str]:
    root = library_dir() / "projects"
    if not root.is_dir():
        return []
    found = []
    for path in root.iterdir():
        if path.is_dir() and _ID.match(path.name) and (path / "project.json").is_file():
            found.append(path.name)
    return found


def _find_recording(recording_id: str) -> dict | None:
    require_id(recording_id, "recording id")
    for project_id in _iter_project_ids():
        data = _read_recording_file(project_id, recording_id)
        if data is not None:
            return data
    return None


# ---- Fast path: catalog.json ----
def _read_catalog_fast() -> dict | None:
    """Read catalog.json with mtime cache for super fast project listing."""
    catalog_path = library_dir() / "catalog.json"
    if not catalog_path.is_file():
        return None
    try:
        mtime = catalog_path.stat().st_mtime
        now = time.time()
        cached_mtime = _CACHE.get("catalog_mtime", 0)
        cached_ts = _CACHE.get("catalog_ts", 0)
        if _CACHE.get("catalog_data") and cached_mtime == mtime and (now - cached_ts) < CACHE_TTL_CATALOG:
            return _CACHE["catalog_data"]
        data = _load(catalog_path)
        _CACHE["catalog_mtime"] = mtime
        _CACHE["catalog_data"] = data
        _CACHE["catalog_ts"] = now
        return data
    except Exception:
        return None


def list_projects_fast() -> list[dict] | None:
    """Super fast project list from catalog.json, no FS scan."""
    catalog = _read_catalog_fast()
    if catalog is None:
        return None
    projects = catalog.get("projects") or []
    rows = []
    for proj in projects:
        pid = proj.get("id")
        if not pid:
            continue
        pdata = _read_project(pid)
        if pdata:
            rows.append(_project_payload(pdata))
        else:
            # If project file missing, skip (don't fallback) - ensures discarded projects disappear
            # Only fallback if we are in fast mode and file just hasn't been materialized yet? 
            # For safety, check if project dir exists
            pdir = _project_dir(pid)
            if pdir.is_dir() and (pdir / "project.json").is_file():
                rows.append(_project_payload(_read_project(pid) or {"id": pid, "name": proj.get("name") or pid}))
            # else skip - project was deleted
            continue
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return rows


def list_projects() -> list[dict]:
    # Try fast path first
    fast = list_projects_fast()
    if fast is not None:
        # Update cache
        _CACHE["projects"] = (time.time(), fast)
        return fast

    # Check memory cache
    now = time.time()
    cached = _CACHE.get("projects")
    if cached:
        ts, data = cached
        if (now - ts) < CACHE_TTL_PROJECTS:
            return data

    rows = []
    for project_id in _iter_project_ids():
        data = _read_project(project_id)
        if data is not None:
            rows.append(_project_payload(data))
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    _CACHE["projects"] = (now, rows)
    return rows


def get_project(project_id: str) -> dict | None:
    data = _read_project(require_id(project_id, "project id"))
    return _project_payload(data) if data else None


def list_recordings(project_id: str) -> list[dict]:
    require_id(project_id, "project id")
    if _read_project(project_id) is None:
        raise NotFound("Project not found")

    # Check cache
    now = time.time()
    rec_cache = _CACHE["recordings"].get(project_id)
    if rec_cache:
        mtime, ts, data = rec_cache
        # Quick mtime check of recordings folder
        folder = _project_dir(project_id) / "recordings"
        try:
            cur_mtime = folder.stat().st_mtime if folder.is_dir() else 0
        except:
            cur_mtime = 0
        if cur_mtime == mtime and (now - ts) < CACHE_TTL_RECORDINGS:
            return data

    # Try catalog fast path
    catalog = _read_catalog_fast()
    if catalog:
        for proj in catalog.get("projects") or []:
            if proj.get("id") == project_id:
                rows = []
                for rec in proj.get("recordings") or []:
                    # Need full data for step_count etc, but catalog has it
                    rows.append({
                        "id": rec.get("id"),
                        "project_id": project_id,
                        "name": rec.get("name") or "",
                        "start_url": "",
                        "status": "active",
                        "created_at": _iso(datetime.utcnow()),
                        "step_count": rec.get("step_count") or 0,
                        "source": "repository",
                        "repository_path": rec.get("path") or recording_repo_path(project_id, rec.get("id")),
                        "resources": rec.get("resources") or [],
                    })
                # Sort by name or keep catalog order
                rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
                # Cache
                folder = _project_dir(project_id) / "recordings"
                try:
                    cur_mtime = folder.stat().st_mtime if folder.is_dir() else 0
                except:
                    cur_mtime = 0
                _CACHE["recordings"][project_id] = (cur_mtime, now, rows)
                # If catalog has step_count, return it (fast), but still try to load full if needed
                # For super fast, return catalog version
                return rows

    folder = _project_dir(project_id) / "recordings"
    rows = []
    if folder.is_dir():
        for path in folder.iterdir():
            if not path.is_dir() or not _ID.match(path.name):
                continue
            data = _read_recording_file(project_id, path.name)
            if data is not None:
                rows.append(_recording_payload(data, with_steps=False))
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    try:
        cur_mtime = folder.stat().st_mtime if folder.is_dir() else 0
    except:
        cur_mtime = 0
    _CACHE["recordings"][project_id] = (cur_mtime, now, rows)
    return rows


def get_recording(recording_id: str) -> dict | None:
    data = _find_recording(recording_id)
    if data is None:
        return None
    return _recording_payload(data, with_steps=True)


def list_variables(project_id: str) -> list[dict]:
    require_id(project_id, "project id")
    if _read_project(project_id) is None:
        raise NotFound("Project not found")

    # Cache check
    now = time.time()
    var_cache = _CACHE["variables"].get(project_id)
    if var_cache:
        mtime, ts, data = var_cache
        var_file = _project_dir(project_id) / "variables.json"
        try:
            cur_mtime = var_file.stat().st_mtime if var_file.is_file() else 0
        except:
            cur_mtime = 0
        if cur_mtime == mtime and (now - ts) < CACHE_TTL_RECORDINGS:
            return data

    recordings = []
    folder = _project_dir(project_id) / "recordings"
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_dir() and _ID.match(path.name):
                data = _read_recording_file(project_id, path.name)
                if data is not None:
                    recordings.append(data)
    result = []
    for variable in _read_variables(project_id):
        name = variable.get("name") or ""
        tags = [
            recording.get("name")
            for recording in recordings
            if any(
                name and (
                    name in (step.get("value") or "")
                    or name in json.dumps(step.get("selector") or {})
                )
                for step in recording.get("steps") or []
            )
        ]
        result.append({
            "id": variable.get("id"),
            "name": name,
            "value": variable.get("value") or "",
            "is_secret": bool(variable.get("is_secret")),
            "category": variable.get("category") or "General",
            "tags": tags,
            "source": "repository",
            "repository_path": variables_repo_path(project_id),
        })
    var_file = _project_dir(project_id) / "variables.json"
    try:
        cur_mtime = var_file.stat().st_mtime if var_file.is_file() else 0
    except:
        cur_mtime = 0
    _CACHE["variables"][project_id] = (cur_mtime, now, result)
    return result


def _write_catalog() -> None:
    # Direct filesystem scan for catalog - never use cached fast path here to avoid stale data
    projects = []
    for project_id in _iter_project_ids():
        pdata = _read_project(project_id)
        if pdata is None:
            continue
        proj_payload = _project_payload(pdata)
        # Direct scan for recordings (fast, no cache)
        recordings = []
        folder = _project_dir(project_id) / "recordings"
        if folder.is_dir():
            for rpath in folder.iterdir():
                if not rpath.is_dir() or not _ID.match(rpath.name):
                    continue
                rdata = _read_recording_file(project_id, rpath.name)
                if rdata is not None:
                    recordings.append({
                        "id": rdata["id"],
                        "name": rdata.get("name") or "",
                        "step_count": len(rdata.get("steps") or []),
                        "resources": _resource_names(project_id, rdata["id"]),
                        "path": recording_repo_path(project_id, rdata["id"]),
                    })
        # Variables
        try:
            vars_list = [item.get("name") for item in _read_variables(project_id) if item.get("name")]
        except Exception:
            vars_list = []
        projects.append({
            "id": project_id,
            "name": proj_payload["name"],
            "path": proj_payload["repository_path"],
            "recordings": recordings,
            "variables": vars_list,
        })
    # Sort by created_at desc if possible
    projects.sort(key=lambda x: x.get("id") or "", reverse=True)
    _dump(library_dir() / "catalog.json", {"source": "repository", "projects": projects})
    # Invalidate caches
    _CACHE["projects"] = None
    _CACHE["catalog_data"] = None
    _CACHE["catalog_mtime"] = 0
    _CACHE["catalog_ts"] = 0


def _write_project_files(project_id: str) -> dict:
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            raise NotFound("Project not found")
        data = {
            "id": project.id,
            "name": project.name,
            "base_url": project.base_url or "",
            "industry_type": project.industry_type or "Generic",
            "created_at": _iso(project.created_at),
        }
        variables = [
            {
                "id": variable.id,
                "name": variable.name,
                "value": variable.value or "",
                "category": variable.category or "General",
                "is_secret": bool(variable.is_secret),
            }
            for variable in project.variables
        ]
    folder = _project_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    _dump(folder / "project.json", data)
    _dump(folder / "variables.json", variables)
    # Invalidate caches
    _CACHE["projects"] = None
    _CACHE["variables"].pop(project_id, None)
    return _project_payload(data)


def _steps_from_db(recording: Recording) -> list[dict]:
    steps = []
    for step in list(recording.steps or []):
        item = {
            "id": step.id,
            "order": step.order,
            "action": step.action,
            "value": step.value,
            "label": step.label,
            "selector": step.selector if isinstance(step.selector, dict) else step.selector,
        }
        if step.screenshot_path:
            item["screenshot"] = Path(step.screenshot_path).name
        # Preserve repeat_count
        if getattr(step, "repeat_count", 1) and step.repeat_count != 1:
            item["repeat"] = step.repeat_count
        else:
            # Check selector for repeat
            sel = item.get("selector") or {}
            if isinstance(sel, dict) and sel.get("repeat"):
                item["repeat"] = sel.get("repeat")
        steps.append(item)
    steps.sort(key=lambda item: item.get("order") or 0)
    return steps


# ---- Step compression: deduplicate repeated actions ----
_REPEAT_SUFFIX = re.compile(r"\s*×\s*\d+$")


def strip_repeat_suffix(label) -> str:
    """Return `label` without a trailing ' ×N' marker, so signatures stay stable."""
    text = (label or "").rstrip()
    match = _REPEAT_SUFFIX.search(text)
    return text[: match.start()] if match else text


def _step_signature(step: dict) -> tuple:
    """Signature for equality check ignoring id/order/screenshot."""
    action = (step.get("action") or "").lower()
    value = step.get("value") or ""
    label = step.get("label") or ""
    selector = step.get("selector") or {}
    if isinstance(selector, dict):
        primary = selector.get("primary") or ""
        # Ignore repeat metadata for comparison
        sig_sel = {k: v for k, v in selector.items() if k not in ("repeat", "repeat_block", "repeat_times")}
        # Use primary + x,y for clicks
        primary = sig_sel.get("primary") or ""
        x = sig_sel.get("x")
        y = sig_sel.get("y")
        sel_sig = (primary, x, y)
    else:
        sel_sig = (str(selector),)
    # For type actions, value matters, for click, selector matters
    if action in ("type", "fill", "text"):
        return (action, sel_sig, value)
    elif action in ("click",):
        return (action, sel_sig, label)
    else:
        return (action, sel_sig, value, label)


# Public alias: the recorder uses the same equality rule as the exporter so a
# step it merges at record time is exactly one `compress_steps` would merge.
step_signature = _step_signature


def compress_steps(steps: list[dict]) -> list[dict]:
    """Compress repeated steps: merge identical consecutive and detect repeating blocks.

    Returns new list with repeat metadata:
    - Single repeat: step with 'repeat' = N
    - Block repeat: original block + loop step with action='loop'
    """
    if not steps:
        return steps

    # First pass: merge identical consecutive steps
    merged: list[dict] = []
    i = 0
    while i < len(steps):
        cur = steps[i]
        cur_sig = _step_signature(cur)
        count = 1
        j = i + 1
        while j < len(steps) and _step_signature(steps[j]) == cur_sig:
            count += 1
            j += 1
        if count > 1:
            new_step = dict(cur)
            new_step["repeat"] = count
            # Update label to show repeat, without stacking a suffix on a step
            # the recorder already merged.
            base_label = strip_repeat_suffix(cur.get("label") or cur.get("action") or "Step")
            new_step["label"] = f"{base_label} ×{count}"
            merged.append(new_step)
            i = j
        else:
            merged.append(dict(cur))
            i += 1

    # Second pass: detect repeating blocks (size 1..5)
    # We already merged singles, but blocks like A,B,A,B should be detected
    compressed: list[dict] = []
    i = 0
    max_block = 5
    while i < len(merged):
        found = False
        # Try block sizes
        for k in range(1, max_block + 1):
            if i + k * 2 > len(merged):
                continue
            block = merged[i:i+k]
            block_sigs = [_step_signature(s) for s in block]
            # Count how many times this block repeats consecutively
            repeat_times = 1
            while True:
                start = i + repeat_times * k
                end = start + k
                if end > len(merged):
                    break
                next_block_sigs = [_step_signature(s) for s in merged[start:end]]
                if next_block_sigs == block_sigs:
                    repeat_times += 1
                else:
                    break
            if repeat_times >= 2:
                # We have block repeated repeat_times times
                # Keep first block
                compressed.extend(block)
                # Add loop step
                loop_step = {
                    "id": generate_uuid(),
                    "order": 0,  # will be reordered later
                    "action": "loop",
                    "value": f"{k}:{repeat_times-1}",
                    "label": f"🔁 Repeat last {k} steps ×{repeat_times-1} (total {repeat_times}×)",
                    "selector": {"repeat_block": k, "repeat_times": repeat_times - 1, "type": "block"},
                    "repeat": None,
                }
                compressed.append(loop_step)
                i += repeat_times * k
                found = True
                break
        if not found:
            compressed.append(merged[i])
            i += 1

    # Reassign order
    for idx, step in enumerate(compressed, 1):
        step["order"] = idx
    return compressed


def _write_recording_files(recording_id: str) -> dict:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise NotFound("Recording not found")
        project_id = recording.project_id
        raw_steps = _steps_from_db(recording)
        # Preserve each editable step and its original execution sequence.
        compressed = raw_steps
        data = {
            "id": recording.id,
            "project_id": project_id,
            "parent_id": recording.parent_id,
            "name": recording.name,
            "start_url": recording.start_url or "",
            "tags": recording.tags or "",
            "status": recording.status or "active",
            "created_at": _iso(recording.created_at),
            "steps": compressed,
        }
    folder = _recording_dir(project_id, recording_id)
    resources = folder / "resources"
    resources.mkdir(parents=True, exist_ok=True)
    referenced = set()
    for step in data["steps"]:
        name = step.get("screenshot")
        if not name:
            continue
        referenced.add(name)
        source = Path(name)
        if not source.is_file():
            with SessionLocal() as db:
                row = db.get(RecordingStep, step["id"]) if step.get("id") else None
                source = Path(row.screenshot_path) if row and row.screenshot_path else source
        if source.is_file() and source.parent != resources:
            (resources / name).write_bytes(source.read_bytes())
            step["screenshot"] = name
    for leftover in list(resources.glob("step-*.jpg")) + list(resources.glob("step-*.png")):
        if leftover.name not in referenced:
            leftover.unlink()
    from app.export_formats import write_exports
    write_exports(resources, data)
    _dump(folder / "recording.json", data)
    data["resources"] = _resource_names(project_id, recording_id)
    # Invalidate cache
    _CACHE["recordings"].pop(project_id, None)
    _CACHE["catalog_data"] = None
    return _recording_payload(data, with_steps=True)


def _discard_project(project_id: str) -> None:
    folder = library_dir() / "projects" / project_id
    if folder.exists():
        shutil.rmtree(folder)
    with SessionLocal() as db:
        row = db.get(Project, project_id)
        if row is not None:
            db.delete(row)
            db.commit()
    _CACHE["projects"] = None
    _CACHE["catalog_data"] = None
    _CACHE["catalog_mtime"] = 0
    _CACHE["catalog_ts"] = 0
    _CACHE["recordings"].pop(project_id, None)
    _CACHE["variables"].pop(project_id, None)


def _publish_current_branch(message: str) -> bool:
    """Publish the library and verify it landed; never merge/rebase or touch main.

    Two mechanisms, one contract:

    * a git checkout — commit ``library/`` on the checked-out branch and push it;
    * no checkout — commit the same files through the GitHub REST API
      (:mod:`app.github_api`), which is what the hosted container has to use.

    A successful push is verified against the remote ref before it is reported.
    Failed pushes keep the local files for the next retry.
    """
    mode = publish_mode()
    if mode == "disabled":
        return False
    if mode == "api":
        return _publish_over_api(message)
    root = git_root()
    if root is None:
        raise PublishError("Library is not inside a git checkout")
    remote = os.environ.get("TF_GIT_REMOTE", "origin")
    branch = _run_git(["symbolic-ref", "--short", "HEAD"], root)
    configured = os.environ.get("TF_GIT_BRANCH")
    if configured and configured != branch:
        raise PublishError(f"Refusing to publish {branch} to {configured}: branch mismatch")
    _run_git(["remote", "get-url", remote], root)
    relative = Path(os.path.relpath(library_dir().resolve(), root.resolve())).as_posix()
    _run_git(["add", "--", relative], root)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", relative], cwd=root)
    if staged.returncode == 1:
        _run_git(["commit", "-m", message, "--", relative], root)
    elif staged.returncode != 0:
        raise PublishError("Unable to inspect staged library changes")
    head = _run_git(["rev-parse", "HEAD"], root)
    remote_head = _run_git(["ls-remote", "--heads", remote, branch], root, timeout=15).split()
    if not remote_head or remote_head[0] != head:
        _run_git(["push", remote, f"HEAD:refs/heads/{branch}"], root, timeout=30)
        remote_head = _run_git(["ls-remote", "--heads", remote, branch], root, timeout=15).split()
        if not remote_head or remote_head[0] != head:
            raise PublishError("Push completed but remote branch did not match local HEAD")
    return True


# Last API push, so the dashboard can show the commit it produced without another
# network round trip. Bounded to one entry.
_LAST_API_PUSH: dict[str, Any] = {}
# Cached remote head for API mode. `status()` must stay cheap: it is read by the
# GitHub tab and by /api/diagnostics, and neither should wait on api.github.com.
_API_HEAD: dict[str, Any] = {"sha": None, "ts": 0.0, "branch": None}
_API_HEAD_TTL = 60.0


def last_api_push() -> dict:
    return dict(_LAST_API_PUSH)


def _remember_api_head(sha: str | None, branch_name: str | None) -> None:
    _API_HEAD.update({"sha": sha, "ts": time.time(), "branch": branch_name})


def cached_api_head() -> str | None:
    if _API_HEAD.get("sha") and (time.time() - float(_API_HEAD.get("ts") or 0)) < _API_HEAD_TTL:
        return str(_API_HEAD["sha"])
    return None


def _publish_over_api(message: str) -> bool:
    """Commit the library through the GitHub REST API and verify the branch ref."""
    try:
        report = github_api.publish(message, library_dir())
    except github_api.GitHubAPIError as exc:
        raise PublishError(str(exc)) from exc
    _LAST_API_PUSH.clear()
    _LAST_API_PUSH.update(report)
    _remember_api_head(report.get("remote_sha") or report.get("local_sha"), report.get("branch"))
    if report.get("published"):
        logger.info("LIBRARY PUBLISHED VIA API %s file(s) -> %s@%s",
                    report.get("total"), report.get("branch"), str(report.get("commit"))[:7])
        return True
    # "Nothing to do" is a success: the remote already matches this instance.
    return report.get("state") == "synced"


def remote_library_status() -> dict:
    """Read the remote ref, not a possibly stale local tracking branch.

    Always carries a `state` and a human-readable `reason`, because a bare
    `synced: false` tells the dashboard nothing it can act on. With no checkout it
    asks the GitHub API instead of giving up, and only reports `no-checkout` when
    there is no way to publish at all.
    """
    root = git_root()
    if root is None:
        if github_api.enabled():
            report = github_api.remote_status(library_root=library_dir())
            _remember_api_head(report.get("remote_sha"), report.get("branch"))
            report.setdefault("branch", github_api.branch())
            return report
        return {
            "synced": False,
            "state": "no-checkout",
            "mode": "disabled",
            "error": "No git checkout",
            "reason": (
                "No .git directory was found at or above the library folder, and no "
                "GitHub API credentials are configured, so there is no branch to "
                "verify or publish to. Mount or copy a git checkout, or set "
                "TF_GITHUB_REPO with TF_GITHUB_TOKEN to publish over the GitHub API."
            ),
        }
    remote = os.environ.get("TF_GIT_REMOTE", "origin")
    try:
        branch = _run_git(["symbolic-ref", "--short", "HEAD"], root)
        local = _run_git(["rev-parse", "HEAD"], root)
        heads = _run_git(["ls-remote", "--heads", remote, branch], root, timeout=15).split()
        remote_sha = heads[0] if heads else None
        relative = Path(os.path.relpath(library_dir().resolve(), root.resolve())).as_posix()
        dirty = bool(_run_git(["status", "--porcelain", "--", relative], root))
        if remote_sha is None:
            state, reason = "no-remote-branch", (
                f"Branch {branch} does not exist on remote {remote} yet. "
                "The first successful push creates it."
            )
        elif dirty:
            state, reason = "dirty", "Library files have changed since the last commit."
        elif remote_sha != local:
            state, reason = "ahead", "Local commits have not reached the remote branch yet."
        else:
            state, reason = "synced", "Remote branch SHA matches local HEAD."
        return {"branch": branch, "local_sha": local, "remote_sha": remote_sha,
                "dirty": dirty, "synced": bool(remote_sha == local and not dirty),
                "state": state, "reason": reason, "mode": "checkout"}
    except PublishError as exc:
        return {
            "synced": False,
            "state": "unreachable",
            "mode": "checkout",
            "branch": None,
            "error": str(exc),
            "reason": f"Could not read remote {remote}: {exc}",
        }


def _publish_unlocked(message: str) -> bool:
    """Publish pending library changes on the current branch."""
    return _publish_current_branch(message)


# The retry loop in app.main waits this long between attempts. It is part of the
# wording the dashboard shows, so the two must not drift apart.
PUBLISH_RETRY_SECONDS = int(os.environ.get("TF_PUBLISH_RETRY_SECONDS", "60") or 60)


def publish_outcome(published: bool, error: str | None = None) -> dict:
    """One honest description of what happened to a save.

    The dashboard used to build this sentence itself and always promised a retry,
    including when publishing was disabled and nothing would ever retry. Every
    caller now returns these fields and the UI prints `publish_message` verbatim.
    """
    mode = publish_mode()
    enabled = mode != "disabled"
    error = error or last_publish_error()
    branch = None
    if mode == "checkout":
        root = git_root()
        try:
            branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root) if root else None
        except (PublishError, OSError):
            # A checkout on an unmounted volume is still a checkout; the branch is
            # simply unknown here, and the message must not fail because of that.
            branch = None
    elif mode == "api":
        branch = github_api.branch()
    where = f"{github_api.repo_slug() or 'the repository'}@{branch}" if branch else "the repository"
    if published:
        state = "published"
        message = f"Published to {where} ({'git push' if mode == 'checkout' else 'GitHub API'})."
        retry = False
    elif not enabled:
        state = "local-only"
        message = (
            "Saved locally. GitHub publishing is disabled in this deployment: no push "
            "is scheduled and no retry will run. " + (publish_disabled_reason() or "")
        ).strip()
        retry = False
    else:
        state = "retry-pending"
        detail = f" ({error})" if error else ""
        message = (
            f"Saved locally. The push to {where} did not complete{detail} and will be "
            f"retried in {PUBLISH_RETRY_SECONDS}s."
        )
        retry = True
    return {
        "published": bool(published),
        "publish_error": None if published else (
            (publish_disabled_reason() if not enabled else error) or "Push not verified"
        ),
        "publish_state": state,
        "publish_mode": mode,
        "publish_enabled": enabled,
        "publish_branch": branch,
        "retry_scheduled": retry,
        "retry_in_seconds": PUBLISH_RETRY_SECONDS if retry else None,
        "publish_message": message,
    }


def publish_pending() -> bool:
    with LOCK:
        return _publish_unlocked("Save library changes")


# Why the most recent publish attempt did not complete. `_finish` swallows the
# PublishError on purpose (the local save succeeded), but the dashboard still has
# to say what went wrong instead of a generic "not verified".
_LAST_PUBLISH_ERROR: dict[str, Any] = {"error": None}


def last_publish_error() -> str | None:
    return _LAST_PUBLISH_ERROR.get("error")


def _finish(message: str) -> bool:
    _write_catalog()
    try:
        published = _publish_unlocked(message)
    except PublishError as exc:
        logger.warning("LIBRARY SAVED LOCALLY; PUSH PENDING: %s", exc)
        _LAST_PUBLISH_ERROR["error"] = str(exc)
        return False
    _LAST_PUBLISH_ERROR["error"] = None if published else "Push completed but the remote branch did not match"
    return published


def create_project(name: str, base_url: str) -> dict:
    with LOCK:
        project_id = None
        try:
            with SessionLocal() as db:
                project = Project(
                    name=name,
                    base_url=base_url or "",
                    created_at=datetime.utcnow().replace(microsecond=0),
                )
                db.add(project)
                db.commit()
                db.refresh(project)
                project_id = project.id
            payload = _write_project_files(project_id)
            payload.update(publish_outcome(_finish(f"Save library project {name} - {base_url}")))
            return payload
        except Exception:
            if project_id:
                _discard_project(project_id)
                try:
                    _write_catalog()
                except Exception:
                    pass
            raise


def create_recording(project_id: str, name: str, start_url: str, parent_id: str | None = None) -> dict:
    require_id(project_id, "project id")
    with LOCK:
        if _read_project(project_id) is None:
            raise NotFound("Project not found")
        _materialize_unlocked()
        recording_id = None
        try:
            with SessionLocal() as db:
                if db.get(Project, project_id) is None:
                    raise NotFound("Project not found")
                recording = Recording(
                    project_id=project_id,
                    parent_id=parent_id or None,
                    name=name.strip()[:255],
                    start_url=start_url,
                    created_at=datetime.utcnow().replace(microsecond=0),
                )
                db.add(recording)
                db.commit()
                db.refresh(recording)
                recording_id = recording.id
            payload = _write_recording_files(recording_id)
            payload.update(publish_outcome(_finish(f"Save library recording {name} - Project {project_id}")))
            return payload
        except Exception:
            if recording_id:
                _delete_recording_files(recording_id)
                with SessionLocal() as db:
                    row = db.get(Recording, recording_id)
                    if row is not None:
                        db.delete(row)
                        db.commit()
                try:
                    _write_catalog()
                except Exception:
                    pass
            raise


def _delete_recording_files(recording_id: str) -> None:
    found = _find_recording(recording_id)
    if found is None:
        return
    folder = _recording_dir(found["project_id"], recording_id)
    if folder.exists():
        shutil.rmtree(folder)
    _CACHE["recordings"].pop(found["project_id"], None)


def create_variable(project_id: str, name: str, value: str) -> dict:
    require_id(project_id, "project id")
    with LOCK:
        if _read_project(project_id) is None:
            raise NotFound("Project not found")
        # Do not rehydrate recordings from a lagging export while recording.
        with SessionLocal() as db:
            present = db.get(Project, project_id) is not None
        if not present:
            _materialize_unlocked()
        variable_id = None
        try:
            with SessionLocal() as db:
                if db.get(Project, project_id) is None:
                    raise NotFound("Project not found")
                variable = Variable(project_id=project_id, name=name, value=value or "")
                db.add(variable)
                db.commit()
                db.refresh(variable)
                variable_id = variable.id
            _write_project_files(project_id)
            published = _finish(f"Save library variable {name} - Project {project_id}")
            saved = next(item for item in list_variables(project_id) if item["id"] == variable_id)
            saved.update(publish_outcome(published))
            return saved
        except Exception:
            if variable_id:
                with SessionLocal() as db:
                    row = db.get(Variable, variable_id)
                    if row is not None:
                        db.delete(row)
                        db.commit()
                try:
                    _write_project_files(project_id)
                    _write_catalog()
                except Exception:
                    pass
            raise


def update_variable(variable_id: str, *, name: str | None = None, value: str | None = None) -> dict:
    require_id(variable_id, "variable id")
    with LOCK:
        with SessionLocal() as db:
            variable = db.get(Variable, variable_id)
        if variable is None:
            _materialize_unlocked()
        with SessionLocal() as db:
            variable = db.get(Variable, variable_id)
            if variable is None:
                raise NotFound("Variable not found")
            previous = (variable.name, variable.value)
            project_id = variable.project_id
            if name:
                variable.name = name
            if value is not None:
                variable.value = value
            db.commit()
        try:
            _write_project_files(project_id)
            published = _finish(f"Save library variable {name or previous[0]} - Project {project_id}")
        except Exception:
            with SessionLocal() as db:
                variable = db.get(Variable, variable_id)
                if variable is not None:
                    variable.name, variable.value = previous
                    db.commit()
                    _write_project_files(project_id)
                    _write_catalog()
            raise
        saved = next(item for item in list_variables(project_id) if item["id"] == variable_id)
        saved["published"] = published
        return saved


def delete_variable(variable_id: str) -> None:
    require_id(variable_id, "variable id")
    with LOCK:
        with SessionLocal() as db:
            missing = db.get(Variable, variable_id) is None
        if missing:
            _materialize_unlocked()
        with SessionLocal() as db:
            variable = db.get(Variable, variable_id)
            if variable is None:
                raise NotFound("Variable not found")
            snapshot = {
                "project_id": variable.project_id,
                "name": variable.name,
                "value": variable.value or "",
                "category": variable.category,
                "is_secret": variable.is_secret,
            }
            db.delete(variable)
            db.commit()
        try:
            _write_project_files(snapshot["project_id"])
            _finish(f"Remove library variable {snapshot['name']} - Project {snapshot['project_id']}")
        except Exception:
            with SessionLocal() as db:
                db.add(Variable(id=variable_id, **snapshot))
                db.commit()
                _write_project_files(snapshot["project_id"])
                _write_catalog()
            raise


def export_recording(recording_id: str, *, publish: bool = False) -> dict | None:
    """Write the recording that is already in the database into the repository."""
    with LOCK:
        with SessionLocal() as db:
            if db.get(Recording, recording_id) is None:
                return None
        try:
            payload = _write_recording_files(recording_id)
        except Exception as e:
            logger.exception("EXPORT RECORDING FAILED %s %s", recording_id, e)
            raise LibraryError(f"Failed to save recording: {e}") from e

        if publish:
            try:
                published = _finish(f"Save library recording {payload['name']} - {payload['id']} - {datetime.utcnow().isoformat()}Z")
                payload.update(publish_outcome(published))
            except PublishError as pe:
                # Even if publish fails, the file is saved locally: report that
                # honestly, with a retry only when a retry will actually happen.
                logger.warning("PUBLISH FAILED %s %s", recording_id, pe)
                payload.update(publish_outcome(False, str(pe)))
                try:
                    _write_catalog()
                except Exception:
                    pass
        else:
            try:
                _write_catalog()
            except Exception as ce:
                logger.warning("CATALOG WRITE FAILED %s", ce)
            payload["published"] = False
        return payload


def compress_recording(recording_id: str, *, publish: bool = True) -> dict:
    """Collapse consecutive repeated steps of a saved recording into one step.

    Rewrites the database rows so the recording, its export and the replay all
    agree on the shorter step list. Recordings made before record-time merging
    keep their original rows until this runs.
    """
    with LOCK:
        with SessionLocal() as db:
            recording = db.get(Recording, recording_id)
            if recording is None:
                raise NotFound("Recording not found")
            rows = sorted(list(recording.steps or []), key=lambda row: row.order or 0)
            before = len(rows)
            steps = [
                {
                    "id": row.id,
                    "order": row.order,
                    "action": row.action,
                    "value": row.value,
                    "label": row.label,
                    "selector": row.selector if isinstance(row.selector, dict) else None,
                    "repeat": row.repeat_count or 1,
                    "screenshot": Path(row.screenshot_path).name if row.screenshot_path else None,
                }
                for row in rows
            ]
        compressed = compress_steps(steps)

        if len(compressed) != before:
            with SessionLocal() as db:
                recording = db.get(Recording, recording_id)
                for row in list(recording.steps or []):
                    db.delete(row)
                db.flush()
                for item in compressed:
                    repeat = item.get("repeat") or 1
                    try:
                        repeat = max(1, min(int(repeat), 100))
                    except (TypeError, ValueError):
                        repeat = 1
                    db.add(RecordingStep(
                        recording_id=recording_id,
                        order=item["order"],
                        action=item.get("action"),
                        value=item.get("value"),
                        label=item.get("label"),
                        selector=item.get("selector"),
                        repeat_count=repeat,
                        screenshot_path=item.get("screenshot"),
                    ))
                db.commit()
            _CACHE["materialize_ts"] = 0

        payload = export_recording(recording_id, publish=publish)
        if payload is None:
            raise NotFound("Recording not found")
        return {
            "recording_id": recording_id,
            "before": before,
            "after": len(compressed),
            "changed": len(compressed) != before,
            "steps": payload.get("steps") or [],
            "published": bool(payload.get("published")),
            "publish_message": payload.get("publish_message"),
            "retry_scheduled": bool(payload.get("retry_scheduled")),
        }


def attach_step_image(recording_id: str, order: int, data: bytes) -> str | None:
    if not data:
        return None
    with LOCK:
        with SessionLocal() as db:
            recording = db.get(Recording, recording_id)
            if recording is None:
                return None
            project_id = recording.project_id
        name = f"step-{int(order):03d}.png"
        folder = _recording_dir(project_id, recording_id) / "resources"
        folder.mkdir(parents=True, exist_ok=True)
        # Atomic write
        tmp = folder / f"{name}.tmp"
        from app.images import compact_png
        tmp.write_bytes(compact_png(data))
        tmp.replace(folder / name)
        return name


def _upsert_project(db, data: dict) -> Project:
    row = db.get(Project, data["id"])
    if row is None:
        row = Project(id=data["id"])
        db.add(row)
    row.name = data.get("name") or "Untitled"
    row.base_url = data.get("base_url") or ""
    row.industry_type = data.get("industry_type") or "Generic"
    row.created_at = _parse_dt(data.get("created_at"))
    return row


def _upsert_recording(db, data: dict) -> None:
    row = db.get(Recording, data["id"])
    if row is None:
        row = Recording(id=data["id"], project_id=data["project_id"], name=data.get("name") or "Recording")
        db.add(row)
    row.project_id = data["project_id"]
    row.parent_id = data.get("parent_id")
    row.name = data.get("name") or "Recording"
    row.start_url = data.get("start_url") or ""
    row.tags = data.get("tags") or ""
    row.status = data.get("status") or "active"
    row.created_at = _parse_dt(data.get("created_at"))
    db.flush()
    for step in list(row.steps or []):
        db.delete(step)
    db.flush()
    for step in data.get("steps") or []:
        # Handle repeat/loop metadata
        repeat_count = 1
        if step.get("repeat"):
            r = step.get("repeat")
            if isinstance(r, int):
                repeat_count = r
            elif isinstance(r, dict):
                repeat_count = r.get("times") or r.get("count") or 1
        # Skip loop steps for DB? Keep them as action=loop
        db.add(RecordingStep(
            id=step.get("id") or generate_uuid(),
            recording_id=row.id,
            order=int(step.get("order") or 0),
            action=step.get("action"),
            value=step.get("value"),
            label=step.get("label"),
            selector=step.get("selector"),
            screenshot_path=step.get("screenshot"),
            repeat_count=repeat_count,
        ))


def _materialize_unlocked() -> None:
    """Materialize with mtime check to avoid unnecessary work."""
    # Check if library dir changed since last materialize
    lib_dir = library_dir()
    try:
        # Get latest mtime of catalog or projects folder
        catalog_path = lib_dir / "catalog.json"
        projects_root = lib_dir / "projects"
        latest_mtime = 0
        if catalog_path.is_file():
            latest_mtime = max(latest_mtime, catalog_path.stat().st_mtime)
        if projects_root.is_dir():
            # Only check top-level mtime for speed
            latest_mtime = max(latest_mtime, projects_root.stat().st_mtime)

        now = time.time()
        if _CACHE["materialize_mtime"] == latest_mtime and (now - _CACHE["materialize_ts"]) < CACHE_TTL_MATERIALIZE:
            return  # No changes, skip

        _CACHE["materialize_mtime"] = latest_mtime
        _CACHE["materialize_ts"] = now
    except Exception:
        pass

    file_projects = set(_iter_project_ids())
    with SessionLocal() as db:
        for project_id in file_projects:
            data = _read_project(project_id)
            if data is None:
                continue
            _upsert_project(db, data)
            db.flush()
            file_vars = {item.get("id") for item in _read_variables(project_id) if item.get("id")}
            for variable in list(db.query(Variable).filter_by(project_id=project_id)):
                if variable.id not in file_vars:
                    db.delete(variable)
            for item in _read_variables(project_id):
                if not item.get("id"):
                    continue
                variable = db.get(Variable, item["id"])
                if variable is None:
                    variable = Variable(id=item["id"], project_id=project_id, name=item.get("name") or "var")
                    db.add(variable)
                variable.project_id = project_id
                variable.name = item.get("name") or variable.name
                variable.value = item.get("value") or ""
                variable.category = item.get("category") or "General"
                variable.is_secret = bool(item.get("is_secret"))
            folder = _project_dir(project_id) / "recordings"
            file_recs = set()
            if folder.is_dir():
                for path in folder.iterdir():
                    if path.is_dir() and _ID.match(path.name):
                        recording = _read_recording_file(project_id, path.name)
                        if recording is not None:
                            file_recs.add(path.name)
                            import sys
                            recorder_module = sys.modules.get("app.recorder")
                            live = recorder_module and recorder_module.get_session(path.name)
                            if not live:
                                _upsert_recording(db, recording)
            for recording in list(db.query(Recording).filter_by(project_id=project_id)):
                if recording.id not in file_recs:
                    db.delete(recording)
        for project in list(db.query(Project)):
            if project.id not in file_projects:
                db.delete(project)
        db.commit()


def materialize_all() -> None:
    with LOCK:
        _materialize_unlocked()


def materialize_recording(recording_id: str) -> bool:
    with LOCK:
        if _find_recording(recording_id) is None:
            return False
        _materialize_unlocked()
        return True


def _library_disk_report() -> dict:
    """What is actually on disk under library/, independent of git."""
    folder = library_dir()
    projects_dir = folder / "projects"
    dirs = [item.name for item in projects_dir.iterdir() if item.is_dir()] if projects_dir.is_dir() else []
    with_project_file = [
        name for name in dirs
        if (projects_dir / name / "project.json").is_file()
    ]
    return {
        "library_dir": str(folder),
        "projects_on_disk": len(with_project_file),
        "project_dirs": sorted(with_project_file),
        "catalog_present": (folder / "catalog.json").is_file(),
    }


def status() -> dict:
    root = git_root()
    mode = publish_mode()
    branch = None
    revision = None
    remote_url = f"https://github.com/{github_api.repo_slug() or github_api.DEFAULT_REPO}"
    dirty = False
    git_state = "ok"
    git_error = None
    git_note = None
    if root is None and mode == "api":
        # No checkout, but the GitHub API can still publish: report the branch and
        # the last verified revision instead of a wall of dashes.
        git_state = "api"
        branch = github_api.branch()
        slug = github_api.repo_slug()
        remote_url = f"https://github.com/{slug}" if slug else remote_url
        head = cached_api_head()
        revision = head[:7] if head else None
        last = last_api_push()
        if last.get("commit"):
            revision = str(last["commit"])[:7]
        git_note = (
            f"No git checkout here: this instance publishes {slug}@{branch} through "
            "the GitHub API. Saves are committed and verified against that branch."
        )
        if revision is None:
            git_note += " The revision is read on the first verify or push."
    elif root is None:
        git_state = "no-checkout"
        git_error = publish_disabled_reason() or (
            "No .git directory at or above the library folder. A container image "
            "built from app/ only has no checkout, so there is no branch to "
            "publish to; the library is served from local files."
        )
    else:
        try:
            branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
            revision = _run_git(["rev-parse", "--short", "HEAD"], root)
            remote_url = _https_repo(_run_git(["remote", "get-url", os.environ.get("TF_GIT_REMOTE", "origin")], root))
            relative = Path(os.path.relpath(library_dir().resolve(), root.resolve())).as_posix()
            porcelain = _run_git(["status", "--porcelain", "--", relative], root)
            dirty = bool(porcelain.strip())
        except PublishError as exc:
            git_state = "unavailable"
            git_error = str(exc)
            logger.info("LIBRARY STATUS %s", exc)
    projects = []
    projects_error = None
    try:
        for project in list_projects():
            recordings = []
            for recording in list_recordings(project["id"]):
                recordings.append({
                    "id": recording["id"],
                    "name": recording["name"],
                    "step_count": recording["step_count"],
                    "resources": recording.get("resources") or [],
                    "repository_path": recording["repository_path"],
                })
            projects.append({
                "id": project["id"],
                "name": project["name"],
                "repository_path": project["repository_path"],
                "variables": [item["name"] for item in list_variables(project["id"])],
                "recordings": recordings,
            })
    except LibraryError as exc:
        projects_error = str(exc)
        logger.info("LIBRARY LIST %s", exc)
    disk = _library_disk_report()
    if projects_error:
        reason = f"Library listing failed: {projects_error}"
    elif git_state == "no-checkout" or git_state == "unavailable":
        reason = git_error
    elif not projects:
        reason = (
            "The library tree is empty. Create a project to add the first one."
            if disk["projects_on_disk"] == 0
            else f"{disk['projects_on_disk']} project(s) are on disk but not listed."
        )
    else:
        reason = None
    api_info = github_api.describe()
    return {
        "source": "repository",
        "publish_enabled": publish_enabled(),
        # How a save reaches GitHub: "checkout" (git push), "api" (REST commit) or
        # "disabled". The dashboard keys its wording off this, so it can never
        # promise a retry that nothing will perform.
        "publish_mode": mode,
        "publish_disabled_reason": publish_disabled_reason(),
        "api_publish": {
            "available": api_info["available"],
            "slug": api_info["slug"],
            "branch": api_info["branch"],
            "reason": api_info["reason"],
        },
        "last_api_push": last_api_push() or None,
        "branch": branch,
        "revision": revision,
        "remote": os.environ.get("TF_GIT_REMOTE", "origin"),
        "repository_url": remote_url,
        "library_path": "library",
        "dirty": dirty,
        "projects": projects,
        # Diagnostics: the dashboard used to render bare dashes with no cause.
        "git_state": git_state,
        "git_error": git_error,
        "git_note": git_note,
        "git_available": git_state in ("ok", "api"),
        "projects_error": projects_error,
        "status_reason": reason,
        **disk,
    }


_REPO_REF: dict[str, Any] = {}


def repo_ref() -> dict:
    """Where the dashboard should read committed logs from, cached.

    Deliberately cheap: two local git calls, no `ls-remote` and no tree walk, so
    asking for it cannot load the server. Returns `slug` (owner/repo) and
    `branch`, which is all a browser needs to build a raw.githubusercontent.com
    URL. Nothing here touches the network.

    Without a checkout the coordinates come from configuration instead
    (`TF_GITHUB_REPO` / `TF_GIT_REMOTE` and `TF_GITHUB_BRANCH` / `TF_LOGS_BRANCH`),
    because reading a public repository needs no token and no git binary. A
    deployment that configures neither reports `available: false` with a reason,
    and the Logs tab falls back to the reports on this instance's own disk.
    """
    cached = _REPO_REF.get("value")
    if cached is not None and (time.time() - _REPO_REF.get("ts", 0)) < 300:
        return cached
    root = git_root()
    slug = None
    branch = None
    via = None
    if root is not None:
        try:
            branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
            url = _https_repo(_run_git(["remote", "get-url", os.environ.get("TF_GIT_REMOTE", "origin")], root))
            match = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+)$", url)
            slug = match.group(1) if match else None
            via = "checkout" if slug and branch else None
        except PublishError as exc:
            logger.info("REPO REF %s", exc)
    if not (slug and branch):
        slug = slug or github_api.repo_slug()
        branch = (os.environ.get("TF_LOGS_BRANCH") or "").strip() or (branch if via == "checkout" else None) \
            or github_api.branch()
        via = "config" if slug else None
    value = {
        "slug": slug,
        "branch": branch,
        "available": bool(slug and branch),
        "via": via,
        "repository_url": f"https://github.com/{slug}" if slug else None,
    }
    _REPO_REF["value"] = value
    _REPO_REF["ts"] = time.time()
    return value


def invalidate_repo_ref() -> None:
    """Drop the cached coordinates, e.g. after a push changed the branch."""
    _REPO_REF.clear()


def playback_url(url: str) -> str:
    """Resolve a repository-relative page onto this server. Other hosts stay put."""
    raw = (url or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return f"http://127.0.0.1:{os.environ.get('PORT', '8000')}{raw}"
    return raw


# ---- Additional fast APIs for requirements 7 ----
def list_all_recordings_fast() -> list[dict]:
    """Super fast list of all recordings from catalog for library tab."""
    catalog = _read_catalog_fast()
    if catalog is None:
        # Fallback to scanning
        all_recs = []
        for proj in list_projects():
            try:
                for rec in list_recordings(proj["id"]):
                    all_recs.append({**rec, "project_name": proj["name"]})
            except NotFound:
                continue
        return all_recs

    all_recs = []
    for proj in catalog.get("projects") or []:
        pname = proj.get("name") or proj.get("id")
        pid = proj.get("id")
        for rec in proj.get("recordings") or []:
            all_recs.append({
                "id": rec.get("id"),
                "project_id": pid,
                "project_name": pname,
                "name": rec.get("name") or "",
                "step_count": rec.get("step_count") or 0,
                "resources": rec.get("resources") or [],
                "repository_path": rec.get("path") or "",
                "source": "repository",
            })
    return all_recs


def push_current_branch(message: str = "Sync library") -> bool:
    """Compatibility wrapper: publish the current checked-out branch only."""
    with LOCK:
        return _publish_current_branch(message)
