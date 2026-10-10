"""GitHub-backed library.

Projects, variables, recordings, and the files a recording depends on live
under ``library/`` in this repository. The dashboard reads that tree, not
leftover database rows. A new project or recording is written here and, unless
publishing is turned off, committed and pushed to the current branch.

``TF_LIBRARY_DIR`` relocates the tree (the harness uses a copy).
``TF_LIBRARY_PUBLISH=0`` writes the files but does not commit or push.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from app.config import logger
from app.db import (
    Project,
    Recording,
    RecordingStep,
    SessionLocal,
    Variable,
    generate_uuid,
)

LOCK = threading.RLock()
_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


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


def publish_enabled() -> bool:
    flag = os.environ.get("TF_LIBRARY_PUBLISH")
    if flag is not None and flag.strip() != "":
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return git_root() is not None


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
        lines.append(f"        echo 'Executing {action or ''} on {label or ''}'")
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
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def list_projects() -> list[dict]:
    rows = []
    for project_id in _iter_project_ids():
        data = _read_project(project_id)
        if data is not None:
            rows.append(_project_payload(data))
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return rows


def get_project(project_id: str) -> dict | None:
    data = _read_project(require_id(project_id, "project id"))
    return _project_payload(data) if data else None


def list_recordings(project_id: str) -> list[dict]:
    require_id(project_id, "project id")
    if _read_project(project_id) is None:
        raise NotFound("Project not found")
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
    return result


def _write_catalog() -> None:
    projects = []
    for project in list_projects():
        project_id = project["id"]
        recordings = []
        for recording in list_recordings(project_id):
            recordings.append({
                "id": recording["id"],
                "name": recording["name"],
                "step_count": recording["step_count"],
                "resources": recording.get("resources") or [],
                "path": recording["repository_path"],
            })
        projects.append({
            "id": project_id,
            "name": project["name"],
            "path": project["repository_path"],
            "recordings": recordings,
            "variables": [item["name"] for item in list_variables(project_id)],
        })
    _dump(library_dir() / "catalog.json", {"source": "repository", "projects": projects})


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
        steps.append(item)
    steps.sort(key=lambda item: item.get("order") or 0)
    return steps


def _write_recording_files(recording_id: str) -> dict:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise NotFound("Recording not found")
        project_id = recording.project_id
        data = {
            "id": recording.id,
            "project_id": project_id,
            "parent_id": recording.parent_id,
            "name": recording.name,
            "start_url": recording.start_url or "",
            "tags": recording.tags or "",
            "status": recording.status or "active",
            "created_at": _iso(recording.created_at),
            "steps": _steps_from_db(recording),
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
        # Copy an absolute artifact into the repository if that is what was stored.
        source = Path(name)
        if not source.is_file():
            with SessionLocal() as db:
                row = db.get(RecordingStep, step["id"]) if step.get("id") else None
                source = Path(row.screenshot_path) if row and row.screenshot_path else source
        if source.is_file() and source.parent != resources:
            (resources / name).write_bytes(source.read_bytes())
            step["screenshot"] = name
    for leftover in resources.glob("step-*.jpg"):
        if leftover.name not in referenced:
            leftover.unlink()
    (resources / "Jenkinsfile").write_text(jenkins_script(data["steps"]), encoding="utf-8")
    _dump(folder / "recording.json", data)
    data["resources"] = _resource_names(project_id, recording_id)
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


def _publish_unlocked(message: str) -> bool:
    if not publish_enabled():
        return False
    root = git_root()
    if root is None:
        raise PublishError("Library directory is not inside a git checkout, so it cannot be pushed")
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if branch == "HEAD":
        raise PublishError("Checkout is detached; refusing to push the library")
    requested = os.environ.get("TF_GIT_BRANCH", "").strip()
    if requested and requested != branch:
        raise PublishError(f"Refusing to push branch {branch} to {requested}")
    relative = Path(os.path.relpath(library_dir().resolve(), root.resolve())).as_posix()
    _run_git(["add", "--", relative], root)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", relative],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if staged.returncode == 0:
        return True
    if staged.returncode != 1:
        detail = (staged.stderr or staged.stdout or "git diff failed").strip().splitlines()
        raise PublishError(detail[-1][:500] if detail else "git diff failed")
    before = _run_git(["rev-parse", "HEAD"], root)
    try:
        _run_git(["commit", "-m", message, "--", relative], root)
        remote = os.environ.get("TF_GIT_REMOTE", "origin")
        _run_git(["push", remote, f"HEAD:{branch}"], root, timeout=90)
    except PublishError:
        head = ""
        try:
            head = _run_git(["rev-parse", "HEAD"], root)
        except PublishError:
            head = ""
        if head and head != before:
            _run_git(["reset", "--mixed", before], root)
        raise
    logger.info("LIBRARY PUBLISHED %s", message)
    return True


def publish_pending() -> bool:
    with LOCK:
        return _publish_unlocked("Save library changes")


def _finish(message: str) -> bool:
    _write_catalog()
    return _publish_unlocked(message)


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
            payload["published"] = _finish(f"Save library project {name}")
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
            payload["published"] = _finish(f"Save library recording {name}")
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


def create_variable(project_id: str, name: str, value: str) -> dict:
    require_id(project_id, "project id")
    with LOCK:
        if _read_project(project_id) is None:
            raise NotFound("Project not found")
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
            published = _finish(f"Save library variable {name}")
            saved = next(item for item in list_variables(project_id) if item["id"] == variable_id)
            saved["published"] = published
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
            published = _finish(f"Save library variable {name or previous[0]}")
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
            _finish(f"Remove library variable {snapshot['name']}")
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
        payload = _write_recording_files(recording_id)
        if publish:
            payload["published"] = _finish(f"Save library recording {payload['name']}")
        else:
            _write_catalog()
            payload["published"] = False
        return payload


def attach_step_image(recording_id: str, order: int, data: bytes) -> str | None:
    if not data:
        return None
    with LOCK:
        with SessionLocal() as db:
            recording = db.get(Recording, recording_id)
            if recording is None:
                return None
            project_id = recording.project_id
        name = f"step-{int(order):03d}.jpg"
        folder = _recording_dir(project_id, recording_id) / "resources"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
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
        db.add(RecordingStep(
            id=step.get("id") or generate_uuid(),
            recording_id=row.id,
            order=int(step.get("order") or 0),
            action=step.get("action"),
            value=step.get("value"),
            label=step.get("label"),
            selector=step.get("selector"),
            screenshot_path=step.get("screenshot"),
        ))


def _materialize_unlocked() -> None:
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


def status() -> dict:
    root = git_root()
    branch = None
    revision = None
    remote_url = "https://github.com/Raheelatta1984/testforge"
    dirty = False
    if root is not None:
        try:
            branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
            revision = _run_git(["rev-parse", "--short", "HEAD"], root)
            remote_url = _https_repo(_run_git(["remote", "get-url", os.environ.get("TF_GIT_REMOTE", "origin")], root))
            relative = Path(os.path.relpath(library_dir().resolve(), root.resolve())).as_posix()
            porcelain = _run_git(["status", "--porcelain", "--", relative], root)
            dirty = bool(porcelain.strip())
        except PublishError as exc:
            logger.info("LIBRARY STATUS %s", exc)
    projects = []
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
        logger.info("LIBRARY LIST %s", exc)
    return {
        "source": "repository",
        "publish_enabled": publish_enabled(),
        "branch": branch,
        "revision": revision,
        "remote": os.environ.get("TF_GIT_REMOTE", "origin"),
        "repository_url": remote_url,
        "library_path": "library",
        "dirty": dirty,
        "projects": projects,
    }


def playback_url(url: str) -> str:
    """Resolve a repository-relative page onto this server. Other hosts stay put."""
    raw = (url or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return f"http://127.0.0.1:{os.environ.get('PORT', '8000')}{raw}"
    return raw
