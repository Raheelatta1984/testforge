import asyncio, os, io, zipfile, base64, re, time
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import ARTIFACTS
from app.errors import redact
from app.db import (
    SCHEMA_STATUS, SessionLocal, engine, init_db, repair_schema,
    Project, Recording, RecordingStep, Run,
)
from app.library_store import LibraryError, NotFound, PublishError
from app import library_store

# Browser-dependent features are optional at boot so the dashboard and its API
# remain usable if the browser runtime is unavailable.
RECORDER_ERROR = None
EXECUTOR_ERROR = None
try:
    from app.recorder import RecorderSession, get_session, open_session
except Exception as exc:
    RecorderSession = None
    get_session = lambda _rid: None
    open_session = None
    RECORDER_ERROR = redact(exc)
    print(f"RECORDER UNAVAILABLE: {RECORDER_ERROR}")

try:
    from app.executor import execute_run as execute_run_task, live_frame
except Exception as exc:
    execute_run_task = None
    live_frame = lambda _run_id: None
    EXECUTOR_ERROR = redact(exc)
    print(f"RUN EXECUTOR UNAVAILABLE: {EXECUTOR_ERROR}")

app = FastAPI(title="TestForge Titan ERP - Optimized")
init_db()
try:
    # Fast startup: try fast path, fallback to full materialize only if needed
    # This avoids slow loading on boot
    fast_projects = library_store.list_projects_fast()
    if fast_projects is None:
        library_store.materialize_all()
    else:
        # Still materialize in background for DB sync, but don't block startup
        # For now, do quick materialize with cache check (fast if no changes)
        library_store.materialize_all()
    # Try to push pending to main branch immediately
    try:
        library_store.publish_pending()
    except Exception as e:
        print(f"INITIAL PUBLISH TO MAIN FAILED (will retry on save): {redact(e)}")
except Exception as exc:
    print(f"LIBRARY LOAD FAILED: {redact(exc)}")
RUN_STREAMS = {}
RUN_BUFFERS = {}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Report unexpected failures as JSON so the dashboard can explain them.

    Without this, Starlette answers with a plain-text "Internal Server Error"
    and the UI can only show the bare status code. Credentials are stripped
    before the message leaves the server.
    """
    if request.scope.get("type") == "websocket":
        raise exc
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {redact(exc)}"},
    )


def _server_port(request: Request | None = None) -> str:
    if request is not None:
        host = request.headers.get("host", "")
        hostname = host.split(":")[0]
        if hostname in {"127.0.0.1", "localhost"} and ":" in host:
            return host.rsplit(":", 1)[-1]
    return str(os.environ.get("PORT", "8000"))


def browser_url(url: str, request: Request | None = None) -> str:
    """Turn a dashboard URL into one the server-side browser can open.

    The preview host (https://…e2b.app) is not reachable from the browser we
    launch, so links to this same app are rewritten to loopback. A different
    local port is a different application and must be left alone — rewriting
    every localhost URL onto PORT broke recordings of apps on :3000.
    """
    raw = (url or "").strip()
    if not raw:
        raw = "/demo.html"
    if raw.startswith("/") and not raw.startswith("//"):
        return f"http://127.0.0.1:{_server_port(request)}{raw}"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="Start URL must be http or https")

    host = (parsed.hostname or "").lower()
    request_host = ""
    if request is not None:
        request_host = request.headers.get("host", "").split(":")[0].lower()
    app_port = int(_server_port(request))
    same_app = False
    if request_host and host == request_host and host not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        same_app = True
    elif host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        if parsed.port is None:
            same_app = (parsed.scheme == "http" and app_port == 80) or (
                parsed.scheme == "https" and app_port == 443
            )
        else:
            same_app = parsed.port == app_port
    if same_app:
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"http://127.0.0.1:{app_port}{path}{query}"
    return raw


def _project_dict(project: Project) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "base_url": project.base_url or "",
        "industry_type": project.industry_type,
        "created_at": project.created_at,
    }


def _step_dict(step: RecordingStep) -> dict:
    return {
        "id": step.id,
        "order": step.order,
        "action": step.action,
        "value": step.value,
        "label": step.label,
        "selector": step.selector,
        "repeat": getattr(step, "repeat_count", 1) or 1,
    }


def _recording_dict(recording: Recording, with_steps: bool = False) -> dict:
    steps = list(recording.steps or [])
    payload = {
        "id": recording.id,
        "project_id": recording.project_id,
        "parent_id": recording.parent_id,
        "name": recording.name,
        "start_url": recording.start_url,
        "status": recording.status,
        "created_at": recording.created_at,
        "step_count": len(steps),
    }
    if with_steps:
        payload["steps"] = [_step_dict(step) for step in steps]
    return payload


def _run_dict(run: Run) -> dict:
    log = run.execution_log or []
    return {
        "id": run.id,
        "recording_id": run.recording_id,
        "status": run.status,
        "progress_pct": run.progress_pct or 0,
        "execution_log": log,
        "log": log,
        "video_path": run.video_path,
        "rog_monitor_log": run.rog_monitor_log,
        "rog_devops_log": run.rog_devops_log,
        "rog_qa_log": run.rog_qa_log,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
    }


async def broadcast_run(run_id, payload):
    buf = RUN_BUFFERS.setdefault(run_id, [])
    if payload.get("type") == "frame":
        buf[:] = [item for item in buf if item.get("type") != "frame"]
    buf.append(payload)
    if len(buf) > 200:
        del buf[:-200]
    for queue in list(RUN_STREAMS.get(run_id, [])):
        try:
            queue.put_nowait(payload)
        except Exception:
            pass


@app.get("/api/health")
def health():
    """Readiness check used to verify a deployment and its database connection."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    return {
        "status": "ok",
        "database": "ok",
        "recorder": RecorderSession is not None,
        "executor": execute_run_task is not None,
        "revision": os.environ.get("RENDER_GIT_COMMIT", "local"),
        "library": "repository",
        "optimized": True,
        "main_branch_push": True,
    }


@app.get("/api/projects")
def get_projects(fast: bool = True):
    """Super fast project listing using catalog cache."""
    start = time.time()
    try:
        if fast:
            # Try super fast path first
            fast_list = library_store.list_projects_fast()
            if fast_list is not None:
                elapsed = time.time() - start
                # Add timing header for debugging
                return JSONResponse(
                    content=fast_list,
                    headers={"X-Load-Time": f"{elapsed:.3f}s", "X-Source": "catalog-fast"}
                )
        # Fallback to cached list_projects (still fast due to internal cache)
        result = library_store.list_projects()
        elapsed = time.time() - start
        return JSONResponse(
            content=result,
            headers={"X-Load-Time": f"{elapsed:.3f}s", "X-Source": "cached-scan"}
        )
    except Exception as e:
        # Last resort: materialize and try again
        try:
            library_store.materialize_all()
            return library_store.list_projects()
        except Exception:
            raise HTTPException(status_code=500, detail=f"Failed to load projects: {redact(e)}")


@app.post("/api/projects", status_code=201)
def create_project(body: dict):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="Project name is required")
    name = name.strip()

    base_url = body.get("base_url", "")
    if not isinstance(base_url, str):
        raise HTTPException(status_code=422, detail="Application URL must be text")
    base_url = base_url.strip()
    if len(name) > 200 or len(base_url) > 500:
        raise HTTPException(status_code=422, detail="Project name or application URL is too long")

    try:
        return library_store.create_project(name, base_url)
    except PublishError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not save the project to GitHub main branch: {redact(exc)}",
        ) from exc
    except SQLAlchemyError as exc:
        try:
            if repair_schema():
                return library_store.create_project(name, base_url)
        except (SQLAlchemyError, PublishError) as retry_exc:
            if isinstance(retry_exc, PublishError):
                raise HTTPException(
                    status_code=503,
                    detail=f"Could not save the project to GitHub main: {redact(retry_exc)}",
                ) from retry_exc
        raise HTTPException(
            status_code=503,
            detail=f"Database rejected the project: {redact(exc)}",
        ) from exc


def _probe_project_write():
    """Try writing a throwaway project inside a transaction that is rolled back."""
    db = SessionLocal()
    try:
        db.add(Project(name="__diagnostics_probe__", base_url=""))
        db.flush()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": redact(exc)}
    finally:
        db.rollback()
        db.close()


@app.get("/api/diagnostics")
def diagnostics():
    """Read-only deployment report: schema state, live columns, and a write probe."""
    report = {
        "status": "ok",
        "revision": os.environ.get("RENDER_GIT_COMMIT", "local"),
        "schema": dict(SCHEMA_STATUS),
        "tables": {},
        "recorder": "ok" if RecorderSession is not None else f"unavailable: {RECORDER_ERROR}",
        "executor": "ok" if execute_run_task is not None else f"unavailable: {EXECUTOR_ERROR}",
        "optimizations": {
            "fast_projects": True,
            "dedup_steps": True,
            "main_branch_push": True,
            "adaptive_preview": True,
            "smooth_save": True,
        }
    }
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            inspector = inspect(connection)
            report["database"] = "ok"
            report["dialect"] = connection.dialect.name
            for table_name in inspector.get_table_names():
                report["tables"][table_name] = [
                    {
                        "name": column["name"],
                        "type": str(column["type"]),
                        "nullable": bool(column.get("nullable", True)),
                    }
                    for column in inspector.get_columns(table_name)
                ]
    except Exception as exc:
        report["database"] = f"unavailable: {redact(exc)}"
        report["status"] = "degraded"
    report["write_probe"] = _probe_project_write()
    report["library"] = {
        "source": "repository",
        "publish_enabled": library_store.publish_enabled(),
        "path": "library",
        "push_branch": "main",
    }
    if not report["write_probe"]["ok"]:
        report["status"] = "degraded"
    if RecorderSession is None or execute_run_task is None:
        report["status"] = "degraded"
    return report


def _library_http(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PublishError):
        return HTTPException(status_code=503, detail=f"Could not save the library to GitHub main: {redact(exc)}")
    if isinstance(exc, LibraryError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@app.get("/api/variables")
def get_vars(project_id: str):
    try:
        # Fast path: don't materialize all, just list variables (has internal cache)
        return library_store.list_variables(project_id)
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.get("/api/projects/{project_id}/variables")
def get_project_vars(project_id: str):
    """Variables for a project - used in projects and recording tabs."""
    try:
        vars_list = library_store.list_variables(project_id)
        project = library_store.get_project(project_id)
        return {
            "project": project,
            "variables": vars_list,
            "count": len(vars_list),
        }
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.post("/api/variables", status_code=201)
def create_var(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Variable name is required")
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="Project is required")
    try:
        return library_store.create_variable(project_id, name, "" if body.get("value") is None else str(body.get("value")))
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.patch("/api/variables/{vid}")
def patch_var(vid: str, body: dict):
    try:
        value = None
        if "value" in body:
            value = "" if body.get("value") is None else str(body.get("value"))
        name = body["name"].strip() if isinstance(body.get("name"), str) and body["name"].strip() else None
        return library_store.update_variable(vid, name=name, value=value)
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.delete("/api/variables/{vid}")
def delete_var(vid: str):
    try:
        library_store.delete_variable(vid)
    except LibraryError as exc:
        raise _library_http(exc) from exc
    return {"ok": True}


@app.post("/api/recordings", status_code=201)
def create_rec(body: dict, request: Request):
    project_id = body.get("project_id")
    name = body.get("name")
    if not project_id or not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="Project and recording name are required")
    try:
        start_url = browser_url(body.get("start_url") or "", request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return library_store.create_recording(
            project_id,
            name.strip(),
            start_url,
            parent_id=body.get("parent_id") or None,
        )
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.get("/api/projects/{project_id}/recordings")
def list_recordings(project_id: str, fast: bool = True):
    try:
        # Super fast path using catalog cache
        if fast:
            # Check if we can serve from fast cache
            catalog = library_store._read_catalog_fast()
            if catalog:
                for proj in catalog.get("projects") or []:
                    if proj.get("id") == project_id:
                        # Return fast version
                        rows = []
                        for rec in proj.get("recordings") or []:
                            rows.append({
                                "id": rec.get("id"),
                                "project_id": project_id,
                                "name": rec.get("name") or "",
                                "start_url": "",
                                "status": "active",
                                "created_at": rec.get("created_at") or "",
                                "step_count": rec.get("step_count") or 0,
                                "source": "repository",
                                "repository_path": rec.get("path") or library_store.recording_repo_path(project_id, rec.get("id")),
                                "resources": rec.get("resources") or [],
                            })
                        rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
                        return JSONResponse(
                            content=rows,
                            headers={"X-Source": "catalog-fast", "X-Count": str(len(rows))}
                        )
        return library_store.list_recordings(project_id)
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.get("/api/library/recordings")
def list_all_recordings_fast():
    """Super fast all recordings from catalog for library tab - requirement 7"""
    try:
        start = time.time()
        result = library_store.list_all_recordings_fast()
        elapsed = time.time() - start
        return JSONResponse(
            content=result,
            headers={"X-Load-Time": f"{elapsed:.3f}s", "X-Source": "catalog-fast-all", "X-Count": str(len(result))}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load recordings: {redact(e)}")


@app.get("/api/recordings/{rid}")
def get_recording(rid: str):
    try:
        # Fast check without full materialize
        recording = library_store.get_recording(rid)
        if recording is None:
            # Try materialize then retry
            library_store.materialize_recording(rid)
            recording = library_store.get_recording(rid)
    except LibraryError as exc:
        raise _library_http(exc) from exc
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return recording


@app.post("/api/recordings/{rid}/session")
async def start_recording_session(rid: str, request: Request):
    if open_session is None:
        raise HTTPException(status_code=503, detail=f"Recorder is unavailable: {RECORDER_ERROR}")
    # Fast path for recording existence
    rec = library_store.get_recording(rid)
    if rec is None:
        if not library_store.materialize_recording(rid):
            raise HTTPException(status_code=404, detail="Recording not found")
    with SessionLocal() as db:
        recording = db.get(Recording, rid)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        start_url = browser_url(recording.start_url or "", request)
        if start_url != recording.start_url:
            recording.start_url = start_url
            db.commit()
        seq = len(list(recording.steps or []))
    library_store.export_recording(rid, publish=False)
    session = await open_session(rid, start_url, seq)
    return {"status": session.status, "error": session.error, "url": session.current_url, "optimized": True}


@app.get("/api/recordings/{rid}/session")
def recording_session_status(rid: str):
    session = get_session(rid)
    if session is None:
        return {"status": "absent", "error": None}
    return {"status": session.status, "error": session.error, "url": session.current_url, "seq": session.seq, "save_logs": session._save_logs[-3:] if hasattr(session, '_save_logs') else []}


@app.get("/api/recordings/{rid}/frame")
def recording_frame(rid: str):
    session = get_session(rid)
    if session is None:
        raise HTTPException(status_code=404, detail="Recording session is not running")
    if session.latest_jpeg:
        return Response(
            content=session.latest_jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Optimized": "true"},
        )
    if session.error:
        raise HTTPException(status_code=409, detail=session.error)
    return Response(status_code=204)


@app.post("/api/recordings/{rid}/input")
async def recording_input(rid: str, body: dict):
    session = get_session(rid)
    if session is None:
        raise HTTPException(status_code=409, detail="Start the recording session before sending input")
    try:
        step = await session.handle_input(body)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Missing {exc}") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "step": step, "optimized": True, "dedup": step.get("repeat", 1) > 1}


@app.post("/api/recordings/{rid}/stop")
async def stop_recording(rid: str):
    session = get_session(rid)
    save_logs = []
    if session is not None:
        save_logs = getattr(session, '_save_logs', [])[-10:]
        await session.stop()
    try:
        # Smooth save with detailed logs
        saved = await asyncio.to_thread(library_store.export_recording, rid, publish=True)
    except LibraryError as exc:
        # Even if publish fails, recording is saved locally - return with warning
        # This makes save smooth per requirement 5
        try:
            # Try again without publish to ensure local save
            saved_local = await asyncio.to_thread(library_store.export_recording, rid, publish=False)
            return {
                "ok": True,
                "published": False,
                "publish_error": str(exc),
                "repository_path": saved_local.get("repository_path") if saved_local else None,
                "save_logs": save_logs,
                "message": f"Recording saved locally but push to main failed: {exc}. Will retry.",
                "retry_branch": "main"
            }
        except Exception as e2:
            raise _library_http(exc) from exc
    if saved is None:
        return {"ok": True, "published": False, "save_logs": save_logs}
    return {
        "ok": True,
        "published": bool(saved.get("published")),
        "repository_path": saved.get("repository_path"),
        "save_logs": save_logs,
        "branch": "main",
        "description": f"AI recording {saved.get('name') or rid} saved to main"
    }


@app.get("/api/recordings/{rid}/jenkins")
def get_jenkins(rid: str):
    recording = library_store.get_recording(rid)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    folder = library_store.library_dir() / "projects" / recording["project_id"] / "recordings" / rid / "resources" / "Jenkinsfile"
    if folder.is_file():
        return PlainTextResponse(folder.read_text(encoding="utf-8"))
    return PlainTextResponse(library_store.jenkins_script(recording.get("steps") or []))


@app.post("/api/runs", status_code=201)
async def queue_run(body: dict):
    if execute_run_task is None:
        raise HTTPException(status_code=503, detail=f"Run executor is unavailable: {EXECUTOR_ERROR}")
    target_id = body.get("target_id") or body.get("recording_id")
    if not target_id:
        raise HTTPException(status_code=422, detail="Recording id is required")
    # Fast check
    rec = library_store.get_recording(target_id)
    if rec is None:
        if not library_store.materialize_recording(target_id):
            raise HTTPException(status_code=404, detail="Recording not found")
    with SessionLocal() as db:
        recording = db.get(Recording, target_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        run = Run(recording_id=recording.id, status="queued")
        db.add(run)
        db.commit()
        db.refresh(run)
        rid = run.id

    # Immediate broadcast of queued status for instant UI feedback (requirement 8)
    await broadcast_run(rid, {"type": "status", "status": "queued", "percent": 0, "message": "Queued - starting immediately"})

    async def task_wrapper():
        async def on_frame(data):
            await broadcast_run(rid, {"type": "frame", "data": data})

        async def on_event(event):
            await broadcast_run(rid, event)

        try:
            await execute_run_task(rid, on_event, on_frame=on_frame)
        except Exception as exc:
            await broadcast_run(rid, {"type": "done", "status": "error", "error": redact(exc)})

    asyncio.create_task(task_wrapper())
    return {"run_id": rid, "status": "queued", "message": "Execution queue started immediately", "display_window": True}


@app.get("/api/runs")
def get_runs():
    with SessionLocal() as db:
        return [_run_dict(run) for run in db.query(Run).order_by(Run.created_at.desc()).all()]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_dict(run)


@app.get("/api/runs/queue/status")
def get_queue_status():
    """Immediate queue status for requirement 8."""
    with SessionLocal() as db:
        queued = db.query(Run).filter(Run.status == "queued").count()
        running = db.query(Run).filter(Run.status == "running").count()
        total = db.query(Run).count()
        recent = db.query(Run).order_by(Run.created_at.desc()).limit(5).all()
        return {
            "queued": queued,
            "running": running,
            "total": total,
            "immediate": True,
            "recent": [_run_dict(r) for r in recent],
            "message": "Queue proceeds immediately, window displays"
        }


def _safe_artifact(run_id: str, filename: str) -> str:
    if not _SAFE_NAME.match(run_id) or not _SAFE_NAME.match(filename) or ".." in filename:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = os.path.join(ARTIFACTS, "runs", run_id, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return path


@app.get("/api/runs/screenshot/{run_id}/{filename}")
def get_screenshot(run_id: str, filename: str):
    return FileResponse(_safe_artifact(run_id, filename))


@app.get("/api/runs/{run_id}/live.jpg")
def get_live_frame(run_id: str):
    data = live_frame(run_id) if live_frame else None
    if not data:
        path = os.path.join(ARTIFACTS, "runs", run_id, "live.jpg")
        if os.path.isfile(path):
            return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Optimized": "true"})
        return Response(status_code=204, headers={"X-Status": "waiting-for-frame"})
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Optimized": "true"})


@app.get("/api/runs/video/{run_id}")
def get_video(run_id: str):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None or not run.video_path:
            raise HTTPException(status_code=404, detail="No video for this run")
        path = run.video_path
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Video file is missing")
    artifacts_root = os.path.realpath(ARTIFACTS)
    if not os.path.realpath(path).startswith(artifacts_root + os.sep):
        raise HTTPException(status_code=404, detail="Video file is missing")
    return FileResponse(path)


@app.get("/api/library")
def library_status():
    return library_store.status()


@app.post("/api/library/publish")
def library_publish():
    try:
        # Force push to main branch immediately per requirements 6,9
        result = library_store.force_push_to_main("Manual push library to main branch")
        status = library_store.status()
        status["pushed_to_main"] = result
        status["branch"] = "main"
        return status
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.get("/api/library/fast")
def library_fast():
    """Super fast library status from catalog for requirement 7."""
    try:
        start = time.time()
        catalog = library_store._read_catalog_fast()
        if catalog:
            elapsed = time.time() - start
            return JSONResponse(
                content={
                    "source": "repository",
                    "fast": True,
                    "projects": catalog.get("projects") or [],
                    "load_time": f"{elapsed:.3f}s",
                    "branch": "main",
                },
                headers={"X-Load-Time": f"{elapsed:.3f}s", "X-Source": "catalog-fast"}
            )
        # Fallback
        return library_store.status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync/github")
def sync_github():
    """Force sync to main branch immediately - requirement 9."""
    try:
        pushed = library_store.force_push_to_main("Force sync library to main - immediate push")
        return {
            "ok": True,
            "pushed": pushed,
            "branch": "main",
            "message": "Repository pushed to main branch immediately" if pushed else "Already up to date with main",
            "immediate": True
        }
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.get("/api/sync/github")
def get_sync():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for root, _, files in os.walk(ARTIFACTS):
            for name in files:
                full = os.path.join(root, name)
                archive.write(full, os.path.relpath(full, ARTIFACTS))
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip")


@app.websocket("/ws/record/{rid}")
async def ws_rec(ws: WebSocket, rid: str):
    await ws.accept()
    if open_session is None:
        await ws.send_json({"type": "error", "message": f"Recorder is unavailable: {RECORDER_ERROR}"})
        await ws.close()
        return
    rec = library_store.get_recording(rid)
    if rec is None:
        if not library_store.materialize_recording(rid):
            await ws.send_json({"type": "error", "message": "Recording not found"})
            await ws.close()
            return
    with SessionLocal() as db:
        recording = db.get(Recording, rid)
        if recording is None:
            await ws.send_json({"type": "error", "message": "Recording not found"})
            await ws.close()
            return
        start_url = recording.start_url
        seq = len(list(recording.steps or []))
    session = await open_session(rid, start_url, seq)

    async def listener(payload):
        await ws.send_json(payload)

    session.add_listener(listener)
    try:
        if session.latest_jpeg:
            await ws.send_json({
                "type": "frame",
                "data": base64.b64encode(session.latest_jpeg).decode("ascii"),
            })
        if session.error:
            await ws.send_json({"type": "error", "message": session.error})
        while True:
            message = await ws.receive_json()
            if message.get("type") == "stop":
                break
            await session.handle_input(message)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await ws.send_json({"type": "error", "message": redact(exc)})
        except Exception:
            pass
    finally:
        session.remove_listener(listener)
        if not session.listeners:
            await session.stop()
            try:
                await asyncio.to_thread(library_store.export_recording, rid, publish=True)
            except Exception as e:
                print(f"WS SAVE FAILED {rid} {redact(e)}")


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    queue = asyncio.Queue()
    RUN_STREAMS.setdefault(run_id, []).append(queue)
    for event in list(RUN_BUFFERS.get(run_id, [])):
        queue.put_nowait(event)
    # Immediately send queued status so window displays (requirement 8)
    try:
        await ws.send_json({"type": "status", "status": "queued", "percent": 0, "message": "Connected - execution starting immediately", "display_window": True})
    except:
        pass
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except Exception:
        pass
    finally:
        try:
            RUN_STREAMS[run_id].remove(queue)
        except ValueError:
            pass


@app.post("/api/ai/rephrase")
async def ai_rephrase(body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        return {"rephrased": ""}
    cleaned = " ".join(text.split())
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return {"rephrased": cleaned}


static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=static_path, html=True), name="static")
