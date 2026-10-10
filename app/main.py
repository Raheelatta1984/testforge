import asyncio, os, tempfile, zipfile, base64, re, time
from datetime import datetime
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app import guardrails, logs_local, run_queue
from app.config import ARTIFACTS
from app.errors import redact
from app.db import (
    SCHEMA_STATUS, SessionLocal, engine, init_db, repair_schema,
    Batch, Project, Recording, RecordingStep, Run,
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

# Batch execution is optional for the same reason: it drives the same browser
# stack, and the dashboard must stay usable when that stack is missing.
BATCH_ERROR = None
try:
    from app import batch_runner
    from app.batch_runner import execute_batch as execute_batch_task
except Exception as exc:
    batch_runner = None
    execute_batch_task = None
    BATCH_ERROR = redact(exc)
    print(f"BATCH EXECUTOR UNAVAILABLE: {BATCH_ERROR}")

app = FastAPI(title="TestForge Titan ERP - Optimized")
init_db()
try:
    # Both branches of the old fast path called materialize_all(), so the branch
    # was decoration and boot walked the whole library tree twice over. The
    # catalog alone is enough to serve the dashboard; the database is filled in
    # on the first request that actually needs it.
    if library_store.list_projects_fast() is None:
        library_store.materialize_all()
    # Network publication runs in the retry worker, never on the startup path.
except Exception as exc:
    print(f"LIBRARY LOAD FAILED: {redact(exc)}")
RUN_STREAMS = {}
# run_id -> whether that run was queued with the live window on. Bounded, so a
# long-lived worker does not accumulate one entry per run forever.
RUN_WINDOWS = {}
RUN_WINDOWS_MAX = 200


def _remember_run_window(run_id: str, display_window: bool) -> None:
    RUN_WINDOWS[run_id] = display_window
    if len(RUN_WINDOWS) > RUN_WINDOWS_MAX:
        for stale in list(RUN_WINDOWS)[:-RUN_WINDOWS_MAX]:
            RUN_WINDOWS.pop(stale, None)
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
        "screenshot": os.path.basename(step.screenshot_path) if step.screenshot_path else None,
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


def _run_dict(run: Run, with_log: bool = True) -> dict:
    """Serialise a run.

    `execution_log` holds every step with its selector, excerpt and screenshot
    path, so it dominates the payload. The list endpoint leaves it out by
    default; the dashboard polls that list every few seconds.
    """
    log = (run.execution_log or []) if with_log else []
    payload = {
        "id": run.id,
        "recording_id": run.recording_id,
        "status": run.status,
        "progress_pct": run.progress_pct or 0,
        "step_count": len(run.execution_log or []),
        "video_path": run.video_path,
        "rog_monitor_log": run.rog_monitor_log,
        "rog_devops_log": run.rog_devops_log,
        "rog_qa_log": run.rog_qa_log,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "batch_id": run.batch_id,
        # Set when queue hygiene cancelled a run: without it the row reads
        # "cancelled" and gives the operator no way to know why.
        "cancel_reason": run.cancel_reason,
        "display_window": RUN_WINDOWS.get(run.id, True),
    }
    if with_log:
        payload["execution_log"] = log
        payload["log"] = log
    return payload


async def broadcast_run(run_id, payload):
    # Bounded: an unbounded buffer per run is a slow leak that only shows up as
    # an OOM restart days later.
    guardrails.run_buffers.append(run_id, payload)
    for queue in list(RUN_STREAMS.get(run_id, [])):
        try:
            queue.put_nowait(payload)
        except Exception:
            pass


@app.on_event("startup")
async def boot_hygiene():
    """Two things must happen before the dashboard starts polling.

    * A restart leaves every `queued` and `running` row behind with no worker to
      finish it. Those runs are cancelled here so the queue shows what will really
      happen instead of a pending count that never moves.
    * The publish retry loop and the queue sweeper start once per process.
    """
    if library_store.publish_enabled():
        schedule_publish_retry()
    try:
        reaped = await asyncio.to_thread(run_queue.reap_orphans)
        if reaped.get("cancelled"):
            print(f"QUEUE HYGIENE: cancelled {reaped['cancelled']} orphaned run(s) at boot")
    except Exception as exc:
        print(f"QUEUE HYGIENE FAILED: {redact(exc)}")
    run_queue.start_sweeper()


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
        "batch_executor": execute_batch_task is not None,
        "revision": os.environ.get("RENDER_GIT_COMMIT", "local"),
        "library": "repository",
        "optimized": True,
        "library_publish_enabled": library_store.publish_enabled(),
        "library_publish_mode": library_store.publish_mode(),
        # Cheap, allocation-free, and it is what Render's health check hits.
        "guardrails": guardrails.report(),
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
            detail=f"Could not save the project to GitHub branch: {redact(exc)}",
        ) from exc
    except SQLAlchemyError as exc:
        try:
            if repair_schema():
                return library_store.create_project(name, base_url)
        except (SQLAlchemyError, PublishError) as retry_exc:
            if isinstance(retry_exc, PublishError):
                raise HTTPException(
                    status_code=503,
                    detail=f"Could not save the project to GitHub branch: {redact(retry_exc)}",
                ) from retry_exc
        raise HTTPException(
            status_code=503,
            detail=f"Database rejected the project: {redact(exc)}",
        ) from exc


def _memory_report() -> dict:
    """Resident memory of this process, so a slow leak is visible before the OOM."""
    report = {"rss_bytes": None, "rss_mb": None, "limit_bytes": None}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    report["rss_bytes"] = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass
    if report["rss_bytes"]:
        report["rss_mb"] = round(report["rss_bytes"] / (1024 * 1024), 1)
    try:
        with open("/sys/fs/cgroup/memory.max", "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
            if raw.isdigit():
                report["limit_bytes"] = int(raw)
    except OSError:
        pass
    return report


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
        "batch_executor": "ok" if execute_batch_task is not None else f"unavailable: {BATCH_ERROR}",
        "optimizations": {
            "fast_projects": True,
            "individual_steps": True,
            "library_publish_enabled": library_store.publish_enabled(),
            "library_publish_mode": library_store.publish_mode(),
            "adaptive_preview": True,
            "adaptive_batch_execution": execute_batch_task is not None,
            "queue_hygiene": True,
            "smooth_save": True,
        },
        "guardrails": guardrails.report(),
        "memory": _memory_report(),
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
    library_status = library_store.status()
    report["library"] = {
        "source": "repository",
        "publish_enabled": library_status.get("publish_enabled"),
        "publish_mode": library_status.get("publish_mode"),
        "publish_disabled_reason": library_status.get("publish_disabled_reason"),
        "api_publish": library_status.get("api_publish"),
        "last_api_push": library_status.get("last_api_push"),
        "path": "library",
        "push_branch": library_status.get("branch"),
        "git_state": library_status.get("git_state"),
    }
    report["queue"] = run_queue.snapshot(limit=5)
    report["logs"] = {
        "local_dir": str(logs_local.logs_dir()),
        "local_available": logs_local.available(),
        "repo_ref": library_store.repo_ref(),
    }
    if not report["write_probe"]["ok"]:
        report["status"] = "degraded"
    if RecorderSession is None or execute_run_task is None or execute_batch_task is None:
        report["status"] = "degraded"
    return report


def _library_http(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PublishError):
        return HTTPException(status_code=503, detail=f"Could not save the library to GitHub branch: {redact(exc)}")
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
        seq = max((step.order for step in recording.steps), default=0)
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
        return Response(status_code=204)
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
    return {"ok": True, "step": step, "optimized": True, "dedup": False}


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
        # Even if publish fails, the recording is saved locally: report that with
        # the real outcome instead of failing the save.
        try:
            saved_local = await asyncio.to_thread(library_store.export_recording, rid, publish=False)
        except Exception:
            raise _library_http(exc) from exc
        outcome = await asyncio.to_thread(library_store.publish_outcome, False, str(exc))
        if outcome["retry_scheduled"]:
            schedule_publish_retry()
        name = (saved_local or {}).get("name") or rid
        return {
            "ok": True,
            "repository_path": (saved_local or {}).get("repository_path"),
            "save_logs": save_logs,
            "branch": outcome.get("publish_branch"),
            "retry_branch": outcome.get("publish_branch"),
            "description": f"AI recording {name} finished and was saved locally",
            "message": outcome["publish_message"],
            **outcome,
        }
    if saved is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    # A retry is only promised when something will perform one. With publishing
    # disabled the old wording ("will retry in 1 minute") described a loop that
    # was never started, so the dashboard waited for a push that could not happen.
    if saved.get("retry_scheduled") or (not saved.get("published") and library_store.publish_enabled()):
        schedule_publish_retry()
    name = saved.get("name") or rid
    published = bool(saved.get("published"))
    return {
        "ok": True,
        "published": published,
        "repository_path": saved.get("repository_path"),
        "save_logs": save_logs,
        "branch": saved.get("publish_branch") or library_store.status().get("branch"),
        "publish_error": saved.get("publish_error"),
        "publish_state": saved.get("publish_state"),
        "publish_mode": saved.get("publish_mode"),
        "publish_enabled": saved.get("publish_enabled"),
        "retry_scheduled": bool(saved.get("retry_scheduled")),
        "retry_in_seconds": saved.get("retry_in_seconds"),
        "publish_message": saved.get("publish_message"),
        "message": saved.get("publish_message"),
        "description": (
            f"AI recording {name} finished and was published to "
            f"{saved.get('publish_branch') or 'the repository'}"
            if published else f"AI recording {name} finished and was saved locally"
        ),
    }


# Only one retry loop per worker. Failed pushes stay visible as unsynced;
# no request blocks for the one-minute backoff.
_publish_retry_task = None


def schedule_publish_retry():
    global _publish_retry_task
    if not library_store.publish_enabled():
        return
    if _publish_retry_task and not _publish_retry_task.done():
        return

    async def retry():
        while True:
            await asyncio.sleep(max(5, library_store.PUBLISH_RETRY_SECONDS))
            try:
                await asyncio.to_thread(library_store.publish_pending)
                verified = await asyncio.to_thread(library_store.remote_library_status)
                if verified.get("synced"):
                    return
            except Exception as exc:
                from app.config import logger
                logger.warning("LIBRARY RETRY FAILED: %s", redact(exc))

    _publish_retry_task = asyncio.create_task(retry())


@app.get("/api/recordings/{rid}/steps/{step_id}/screenshot")
def recording_step_screenshot(rid: str, step_id: str):
    with SessionLocal() as db:
        step = db.get(RecordingStep, step_id)
        if not step or step.recording_id != rid or not step.screenshot_path:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        recording = db.get(Recording, rid)
        path = library_store.library_dir() / "projects" / recording.project_id / "recordings" / rid / "resources" / os.path.basename(step.screenshot_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(path, media_type="image/png" if path.suffix.lower() == ".png" else "image/jpeg")


@app.patch("/api/recordings/{rid}/steps/{step_id}")
async def edit_recording_step(rid: str, step_id: str, body: dict):
    allowed = {"navigate", "click", "type", "fill", "press", "save_variable"}
    with SessionLocal() as db:
        step = db.get(RecordingStep, step_id)
        if not step or step.recording_id != rid:
            raise HTTPException(status_code=404, detail="Step not found")
        if "action" in body:
            if body["action"] not in allowed:
                raise HTTPException(status_code=422, detail="Unsupported action")
            step.action = body["action"]
        if "value" in body:
            step.value = None if body["value"] is None else str(body["value"])
        if "label" in body:
            step.label = str(body["label"] or "")[:500]
        if "selector" in body:
            if body["selector"] is not None and not isinstance(body["selector"], dict):
                raise HTTPException(status_code=422, detail="Selector must be an object")
            step.selector = body["selector"]
        db.commit()
        result = _step_dict(step)
    saved = await asyncio.to_thread(library_store.export_recording, rid, publish=True) or {}
    for key in ("published", "publish_message", "publish_state", "publish_error",
                "retry_scheduled", "retry_in_seconds", "publish_enabled"):
        result[key] = saved.get(key)
    result["published"] = bool(result.get("published"))
    if saved.get("retry_scheduled"):
        schedule_publish_retry()
    return result


@app.post("/api/recordings/{rid}/steps/compress")
async def compress_recording_steps(rid: str):
    """Collapse repeated steps of an existing recording into one step per action.

    New recordings already merge as they are recorded; this covers recordings
    saved before that, and lets a user re-collapse after manual edits.
    """
    try:
        result = await asyncio.to_thread(library_store.compress_recording, rid)
    except LibraryError as exc:
        raise _library_http(exc) from exc
    if result.get("retry_scheduled"):
        schedule_publish_retry()
    return result


@app.get("/api/recordings/{rid}/resources/{filename}")
def download_recording_resource(rid: str, filename: str):
    if not _SAFE_NAME.fullmatch(filename) or ".." in filename:
        raise HTTPException(status_code=404, detail="Resource not found")
    recording = library_store.get_recording(rid)
    if recording is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    folder = library_store.library_dir() / "projects" / recording["project_id"] / "recordings" / rid / "resources"
    path = folder / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Resource not found")
    return FileResponse(path, filename=filename)


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
    # The Runs tab toggle. Off means no live screencast at all: the replay is
    # identical, only the picture is not produced.
    display_window = True if body.get("display_window") is None else bool(body.get("display_window"))
    # Fast check
    rec = library_store.get_recording(target_id)
    if rec is None:
        if not library_store.materialize_recording(target_id):
            raise HTTPException(status_code=404, detail="Recording not found")
    # Queue hygiene: with one browser a second queued run of the same recording
    # only waits behind the first. Reuse it unless the caller insists.
    if not body.get("force"):
        existing = await asyncio.to_thread(run_queue.duplicate_of, target_id)
        if existing:
            _remember_run_window(existing["run_id"], display_window)
            return {
                "run_id": existing["run_id"],
                "status": "queued",
                "deduplicated": True,
                "queued_for_seconds": existing["age_seconds"],
                "message": (
                    "This recording is already queued and has not started yet, so the "
                    "existing run was reused instead of queueing a second one."
                ),
                "display_window": display_window,
            }
    with SessionLocal() as db:
        recording = db.get(Recording, target_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        run = Run(recording_id=recording.id, status="queued")
        db.add(run)
        db.commit()
        db.refresh(run)
        rid = run.id
    _remember_run_window(rid, display_window)

    # Immediate broadcast of queued status for instant UI feedback (requirement 8)
    await broadcast_run(rid, {"type": "status", "status": "queued", "percent": 0, "message": "Queued - starting immediately"})

    def viewer_count():
        return len(RUN_STREAMS.get(rid, ()))

    async def task_wrapper():
        async def on_frame(data):
            await broadcast_run(rid, {"type": "frame", "data": data})

        async def on_event(event):
            await broadcast_run(rid, event)

        try:
            await execute_run_task(
                rid, on_event, on_frame=on_frame,
                display_window=display_window, viewer_count=viewer_count,
            )
        except Exception as exc:
            from datetime import datetime
            with SessionLocal() as db:
                row = db.get(Run, rid)
                if row:
                    row.status = "error"
                    row.rog_monitor_log = redact(exc)
                    row.finished_at = datetime.utcnow()
                    db.commit()
            await broadcast_run(rid, {"type": "done", "status": "error", "error": redact(exc)})

    task = asyncio.create_task(task_wrapper())
    # The queue reaper only cancels runs that nobody owns, so ownership has to be
    # recorded the moment the worker exists and dropped the moment it finishes.
    run_queue.register(rid, task)
    task.add_done_callback(lambda _done: run_queue.unregister(rid))
    return {
        "run_id": rid,
        "status": "queued",
        "deduplicated": False,
        "message": "Execution queue started immediately",
        "display_window": display_window,
    }


# --- Adaptive batch execution ---------------------------------------------
# A batch replays many recordings through ONE browser, with step budgets learned
# from previous runs and screenshots only where they are worth the CPU. See
# app/batch_runner.py for why each of those is the cheap choice.


class _BatchHandle:
    """Ownership token for every run of a batch.

    The queue reaper asks this whether a run still has a worker, and asks it to
    stop a run that is being cleared. For a batch, "stop" means "do not start the
    next recording", not "kill the task": killing it mid-replay would leave a
    browser open.
    """

    def __init__(self, task, batch_id):
        self.task = task
        self.batch_id = batch_id

    def done(self):
        return self.task is None or self.task.done()

    def cancel(self):
        if batch_runner is not None:
            batch_runner.request_cancel(self.batch_id)


def _select_batch_recordings(body: dict) -> tuple[list[dict], list[str]]:
    """Resolve what a batch should replay: explicit ids, one project, or everything."""
    wanted = body.get("recording_ids")
    project_id = body.get("project_id")
    selected: list[dict] = []
    unknown: list[str] = []

    if isinstance(wanted, (list, tuple)) and wanted:
        for item in wanted:
            if not isinstance(item, str) or not item.strip():
                continue
            recording_id = item.strip()
            recording = library_store.get_recording(recording_id)
            if recording is None:
                library_store.materialize_recording(recording_id)
                recording = library_store.get_recording(recording_id)
            if recording is None:
                unknown.append(recording_id)
                continue
            selected.append({
                "id": recording["id"],
                "name": recording.get("name") or recording["id"],
                "start_url": recording.get("start_url") or "",
                "project_id": recording.get("project_id"),
                "step_count": recording.get("step_count") or len(recording.get("steps") or []),
            })
    elif project_id:
        for recording in library_store.list_recordings(str(project_id)):
            selected.append({
                "id": recording["id"],
                "name": recording.get("name") or recording["id"],
                "start_url": recording.get("start_url") or "",
                "project_id": project_id,
                "step_count": recording.get("step_count") or 0,
            })
    elif body.get("all"):
        for recording in library_store.list_all_recordings_fast():
            selected.append({
                "id": recording["id"],
                "name": recording.get("name") or recording["id"],
                "start_url": recording.get("start_url") or "",
                "project_id": recording.get("project_id"),
                "step_count": recording.get("step_count") or 0,
            })

    # Same recording twice in one batch is a mistake, not a feature.
    seen: set[str] = set()
    unique = []
    for item in selected:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
    return unique, unknown


def _batch_dict(batch: Batch, runs=None, with_report: bool = True, full: bool = False) -> dict:
    payload = {
        "id": batch.id,
        "name": batch.name,
        "status": batch.status,
        "total": batch.total or 0,
        "done": batch.done or 0,
        "passed": batch.passed or 0,
        "failed": batch.failed or 0,
        "skipped": batch.skipped or 0,
        "progress_pct": batch.progress_pct or 0,
        "options": batch.options or {},
        "error": batch.error,
        "created_at": batch.created_at,
        "started_at": batch.started_at,
        "finished_at": batch.finished_at,
    }
    if with_report:
        payload["report"] = batch.report or {}
    if runs is not None:
        payload["runs"] = [_run_dict(run, with_log=full) for run in runs]
    return payload


@app.post("/api/runs/batch", status_code=201)
async def start_batch(body: dict | None = None):
    """Queue one adaptive batch execution.

    Body: ``recording_ids`` (list) or ``project_id`` or ``all`` to select what to
    replay, plus ``name``, ``display_window``, ``screenshots``
    (``none``/``failure``/``changes``/``all``), ``share_session``,
    ``retry_transient``. One browser serves the whole batch, so a second batch is
    refused while one is executing rather than queued behind it.
    """
    if execute_batch_task is None:
        raise HTTPException(status_code=503, detail=f"Batch executor is unavailable: {BATCH_ERROR}")
    body = body or {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be an object")
    if not (body.get("recording_ids") or body.get("project_id") or body.get("all")):
        raise HTTPException(
            status_code=422,
            detail="Select recordings for the batch: recording_ids, project_id or all=true",
        )
    selected, unknown = await asyncio.to_thread(_select_batch_recordings, body)
    if not selected:
        detail = "No recordings matched."
        if unknown:
            detail = f"No recordings matched. Unknown recording id(s): {', '.join(unknown[:10])}"
        raise HTTPException(status_code=404, detail=detail)
    cap = guardrails.MAX_BATCH_RECORDINGS
    if len(selected) > cap:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{len(selected)} recordings were selected but a batch is limited to {cap}. "
                "Split the batch or raise TF_BATCH_MAX_RECORDINGS."
            ),
        )
    if not guardrails.batch_budget.acquire():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Another batch is already executing (limit {guardrails.batch_budget.limit}). "
                "Wait for it to finish or cancel it first."
            ),
        )

    screenshots = str(body.get("screenshots") or batch_runner.DEFAULT_SCREENSHOTS).lower()
    if screenshots not in batch_runner.SCREENSHOT_MODES:
        guardrails.batch_budget.release()
        raise HTTPException(
            status_code=422,
            detail=f"screenshots must be one of {', '.join(batch_runner.SCREENSHOT_MODES)}",
        )
    display_window = bool(body.get("display_window"))
    share_session = (guardrails.BATCH_SHARE_SESSION
                     if body.get("share_session") is None else bool(body.get("share_session")))
    retry_transient = (guardrails.BATCH_RETRY_TRANSIENT
                       if body.get("retry_transient") is None else bool(body.get("retry_transient")))
    name = body.get("name")
    name = str(name).strip()[:200] if isinstance(name, str) and name.strip() else None

    plan = batch_runner.plan_order(selected)
    started_batch = False
    try:
        with SessionLocal() as db:
            batch = Batch(name=name, status="queued", total=len(plan),
                          options={"screenshots": screenshots, "share_session": share_session,
                                   "retry_transient": retry_transient, "display_window": display_window,
                                   "selected": len(selected), "unknown": unknown[:20]})
            db.add(batch)
            db.commit()
            db.refresh(batch)
            batch_id = batch.id
            run_ids = []
            for item in plan:
                run = Run(recording_id=item["id"], status="queued", batch_id=batch_id)
                db.add(run)
                db.commit()
                db.refresh(run)
                run_ids.append(run.id)
                _remember_run_window(run.id, display_window)
            started_batch = True

        stream_key = f"batch:{batch_id}"

        async def on_event(payload):
            await broadcast_run(stream_key, payload)
            run_id = payload.get("run_id")
            if run_id:
                await broadcast_run(run_id, payload)

        def viewer_count():
            return len(RUN_STREAMS.get(stream_key, ()))

        async def batch_wrapper():
            try:
                await execute_batch_task(
                    batch_id, on_event,
                    display_window=display_window, viewer_count=viewer_count,
                    screenshots=screenshots, share_session=share_session,
                    retry_transient=retry_transient,
                )
            except Exception as exc:
                from datetime import datetime
                with SessionLocal() as db:
                    row = db.get(Batch, batch_id)
                    if row is not None and row.status not in ("passed", "partial", "failed", "cancelled"):
                        row.status = "error"
                        row.error = redact(exc)[:800]
                        row.finished_at = datetime.utcnow()
                        db.commit()
                await broadcast_run(stream_key, {"type": "batch", "batch_id": batch_id,
                                                 "status": "error", "error": redact(exc)})
            finally:
                for run_id in run_ids:
                    run_queue.unregister(run_id)
                guardrails.batch_budget.release()

        task = asyncio.create_task(batch_wrapper())
        handle = _BatchHandle(task, batch_id)
        for run_id in run_ids:
            run_queue.register(run_id, handle)
        await broadcast_run(stream_key, {
            "type": "batch", "batch_id": batch_id, "status": "queued", "total": len(plan),
            "message": f"Batch queued: {len(plan)} recording(s) through one browser",
        })
        return {
            "batch_id": batch_id,
            "status": "queued",
            "total": len(plan),
            "run_ids": run_ids,
            "unknown_recording_ids": unknown,
            "plan": [{"recording_id": item["id"], "name": item.get("name"),
                      "start_url": item.get("start_url"), "step_count": item.get("step_count")}
                     for item in plan],
            "options": {"screenshots": screenshots, "share_session": share_session,
                        "retry_transient": retry_transient, "display_window": display_window},
            "message": (
                f"Batch of {len(plan)} recording(s) queued. One browser for the whole batch, "
                f"screenshots on {screenshots}."
            ),
        }
    except Exception:
        if not started_batch:
            guardrails.batch_budget.release()
        raise


@app.get("/api/runs/batches")
def list_batches(limit: int | None = None):
    """Recent batches, newest first, without the full report by default."""
    cap = guardrails.BATCH_LIST_LIMIT if limit is None else limit
    cap = max(1, min(int(cap), guardrails.MAX_RUN_LIST_LIMIT))
    with SessionLocal() as db:
        rows = db.query(Batch).order_by(Batch.created_at.desc()).limit(cap).all()
        return [_batch_dict(row, with_report=False) for row in rows]


@app.get("/api/runs/batch/{batch_id}")
def get_batch(batch_id: str, full: bool = False):
    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found")
        runs = db.query(Run).filter(Run.batch_id == batch_id).order_by(Run.created_at.asc()).all()
        return _batch_dict(batch, runs=runs, full=full)


@app.post("/api/runs/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """Stop a batch: the recording in flight finishes, the rest are not started."""
    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found")
        if batch.status in ("passed", "partial", "failed", "cancelled", "error"):
            return {"ok": False, "batch_id": batch_id, "status": batch.status,
                    "message": f"This batch already finished ({batch.status})."}
    if batch_runner is not None:
        batch_runner.request_cancel(batch_id)
    with SessionLocal() as db:
        rows = db.query(Run).filter(Run.batch_id == batch_id, Run.status == "queued").all()
        cancelled = 0
        for row in rows:
            row.status = "cancelled"
            row.cancel_reason = "Batch cancelled before this recording started."
            row.finished_at = datetime.utcnow()
            cancelled += 1
        batch = db.get(Batch, batch_id)
        if batch is not None and batch.status == "queued":
            batch.status = "cancelled"
            batch.finished_at = datetime.utcnow()
            batch.skipped = cancelled
        db.commit()
        status = batch.status if batch else "cancelled"
    await broadcast_run(f"batch:{batch_id}", {
        "type": "batch", "batch_id": batch_id, "status": "cancelled",
        "message": f"Batch cancelled; {cancelled} recording(s) were not started.",
    })
    return {"ok": True, "batch_id": batch_id, "status": status, "cancelled_runs": cancelled,
            "message": "Batch cancellation requested. The recording in flight finishes first."}


@app.get("/api/runs")
def get_runs(limit: int | None = None, full: bool = False):
    """Recent runs, newest first.

    Bounded on purpose: returning every run ever recorded with its full log is
    what made the dashboard's 3-second poll grow without limit.
    """
    cap = guardrails.DEFAULT_RUN_LIST_LIMIT if limit is None else limit
    cap = max(1, min(int(cap), guardrails.MAX_RUN_LIST_LIMIT))
    with SessionLocal() as db:
        rows = db.query(Run).order_by(Run.created_at.desc()).limit(cap).all()
        return [_run_dict(run, with_log=full) for run in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_dict(run)


@app.get("/api/runs/queue/status")
def get_queue_status():
    """Immediate queue status, including what in it is never going to run."""
    with SessionLocal() as db:
        queued = db.query(Run).filter(Run.status == "queued").count()
        running = db.query(Run).filter(Run.status == "running").count()
        total = db.query(Run).count()
        recent = db.query(Run).order_by(Run.created_at.desc()).limit(5).all()
        payload = {
            "queued": queued,
            "running": running,
            "passed": db.query(Run).filter(Run.status == "passed").count(),
            "failed": db.query(Run).filter(Run.status.in_(["failed", "error"])).count(),
            "cancelled": db.query(Run).filter(Run.status == "cancelled").count(),
            "pending": queued + running,
            "passed_pct": round(100 * db.query(Run).filter(Run.status == "passed").count() / total, 1) if total else 0,
            "total": total,
            "immediate": True,
            "recent": [_run_dict(r) for r in recent],
            "message": "Queue proceeds immediately, window displays"
        }
    # Orphans and stale entries: runs the dashboard would otherwise count as
    # pending forever.
    payload["hygiene"] = run_queue.snapshot(limit=20)
    payload["clearable"] = payload["hygiene"]["clearable"]
    return payload


@app.post("/api/runs/queue/clear")
async def clear_run_queue(body: dict | None = None):
    """Clear irrelevant and old entries out of the execution queue.

    Body (all optional):

    * ``older_than_minutes`` - only touch runs queued at least this long.
    * ``include_orphans`` (default true) - runs no worker owns, e.g. left behind by a restart.
    * ``include_stale`` (default true) - queued longer than ``TF_QUEUE_STALE_MINUTES``.
    * ``include_duplicates`` (default false) - extra queued runs of a recording already waiting.
    * ``cancel_running`` (default false) - also stop a run that is executing now.
    * ``purge_finished_days`` - delete finished history older than this many days.
    * ``dry_run`` (default false) - report what would be cleared and change nothing.
    """
    body = body or {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be an object")
    older_than = body.get("older_than_minutes")
    if older_than is not None:
        try:
            older_than = float(older_than)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="older_than_minutes must be a number") from None
        if older_than < 0:
            raise HTTPException(status_code=422, detail="older_than_minutes cannot be negative")
    purge_days = body.get("purge_finished_days")
    if purge_days is not None:
        try:
            purge_days = float(purge_days)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="purge_finished_days must be a number") from None
        if purge_days < 0:
            raise HTTPException(status_code=422, detail="purge_finished_days cannot be negative")
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(status_code=422, detail="reason must be text")
    report = await asyncio.to_thread(
        run_queue.clear,
        older_than_minutes=older_than,
        include_orphans=True if body.get("include_orphans") is None else bool(body.get("include_orphans")),
        include_stale=True if body.get("include_stale") is None else bool(body.get("include_stale")),
        include_duplicates=bool(body.get("include_duplicates")),
        cancel_running=bool(body.get("cancel_running")),
        purge_finished_days=purge_days,
        reason=(reason.strip()[:200] if reason else None),
        dry_run=bool(body.get("dry_run")),
    )
    report["ok"] = True
    report["message"] = (
        f"{report['matched']} run(s) would be cleared."
        if report["dry_run"] else
        f"{report['cancelled']} run(s) cleared from the queue."
    )
    return report


@app.post("/api/runs/queue/sweep")
async def sweep_run_queue():
    """Run the orphan reaper now instead of waiting for the periodic sweep."""
    report = await asyncio.to_thread(run_queue.reap_orphans)
    report["ok"] = True
    report["message"] = (
        f"{report['cancelled']} orphaned run(s) cancelled."
        if report["cancelled"] else "No orphaned runs to clear."
    )
    return report


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
        return Response(status_code=204, headers={
            "Cache-Control": "no-store",
            # The Runs tab reads this so a switched-off window is not reported as
            # a stalled browser.
            "X-Status": "window-disabled" if RUN_WINDOWS.get(run_id) is False else "waiting-for-frame",
        })
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
async def library_status():
    info = await asyncio.to_thread(library_store.status)
    info["remote_verification"] = await asyncio.to_thread(library_store.remote_library_status)
    if info["publish_enabled"] and not info["remote_verification"].get("synced"):
        schedule_publish_retry()
    return info


@app.post("/api/library/publish")
def library_publish(message: str | None = None):
    """Push the library now, by git or by the GitHub API, and verify it landed."""
    try:
        result = library_store.push_current_branch(
            (message or "Manual push library").strip()[:200] or "Manual push library"
        )
        status = library_store.status()
        status["pushed"] = result
        status["remote_verification"] = library_store.remote_library_status()
        status.update(library_store.publish_outcome(bool(result)))
        if status["retry_scheduled"]:
            schedule_publish_retry()
        return status
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.get("/api/library/publish/plan")
async def library_publish_plan():
    """What the next push would change, without pushing anything.

    In API mode this compares the local library with the remote tree, so the
    dashboard can show "3 files to publish" before the operator commits to it.
    """
    mode = library_store.publish_mode()
    if mode == "disabled":
        return {"available": False, "mode": mode,
                "reason": library_store.publish_disabled_reason()}
    if mode == "api":
        from app import github_api
        collected = await asyncio.to_thread(github_api.collect_local_files, library_store.library_dir())
        try:
            client = github_api.GitHubClient()
            head = await asyncio.to_thread(client.head_sha)
            remote = await asyncio.to_thread(client.tree, head) if head else {}
        except github_api.GitHubAPIError as exc:
            return {"available": False, "mode": mode, "error": str(exc),
                    "files": len(collected["files"]), "bytes": collected["bytes"],
                    "skipped": collected["skipped"],
                    "reason": f"Could not read the remote branch: {exc}"}
        diff = github_api.plan(collected["files"], remote)
        return {
            "available": True, "mode": mode, "slug": client.slug, "branch": client.branch,
            "remote_sha": head, "files": len(collected["files"]), "bytes": collected["bytes"],
            "skipped": collected["skipped"], **diff,
        }
    verification = await asyncio.to_thread(library_store.remote_library_status)
    return {"available": True, "mode": mode, "branch": verification.get("branch"),
            "remote_sha": verification.get("remote_sha"),
            "synced": verification.get("synced"), "state": verification.get("state"),
            "reason": verification.get("reason"), "dirty": verification.get("dirty")}


@app.get("/api/library/fast")
def library_fast():
    """Super fast library status from catalog for requirement 7."""
    try:
        start = time.time()
        catalog = library_store._read_catalog_fast()
        if catalog:
            elapsed = time.time() - start
            # repo_ref() and publish_mode() are two local git calls and an env
            # read. status() walks every project, which is exactly what a "fast"
            # endpoint must not do, and the dashboard polls this one.
            ref = library_store.repo_ref()
            mode = library_store.publish_mode()
            return JSONResponse(
                content={
                    "source": "repository",
                    "fast": True,
                    "projects": catalog.get("projects") or [],
                    "load_time": f"{elapsed:.3f}s",
                    "branch": ref.get("branch"),
                    "slug": ref.get("slug"),
                    "repository_url": ref.get("repository_url"),
                    "publish_mode": mode,
                    "publish_enabled": mode != "disabled",
                    "publish_disabled_reason": library_store.publish_disabled_reason(),
                },
                headers={"X-Load-Time": f"{elapsed:.3f}s", "X-Source": "catalog-fast"}
            )
        # Fallback
        return library_store.status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync/github")
def sync_github():
    """Push and verify the library against GitHub, by checkout or by API."""
    mode = library_store.publish_mode()
    if mode == "disabled":
        # Not an error the caller can retry away: say exactly what is missing.
        raise HTTPException(status_code=503, detail=library_store.publish_disabled_reason())
    try:
        pushed = library_store.push_current_branch("Sync library")
        verification = library_store.remote_library_status()
        outcome = library_store.publish_outcome(bool(pushed))
        synced = bool(verification.get("synced"))
        return {
            "ok": synced,
            "pushed": pushed,
            "mode": mode,
            "branch": outcome.get("publish_branch") or verification.get("branch"),
            "repository_url": f"https://github.com/{verification.get('slug')}" if verification.get("slug")
                              else library_store.status().get("repository_url"),
            "commit": (library_store.last_api_push() or {}).get("commit"),
            "verification": verification,
            "publish_message": outcome["publish_message"],
            "message": (
                f"Repository push verified on {outcome.get('publish_branch') or 'the branch'} "
                f"({'git push' if mode == 'checkout' else 'GitHub API'})."
                if synced else outcome["publish_message"]
            ),
            "retry_scheduled": outcome["retry_scheduled"],
            "immediate": True,
        }
    except LibraryError as exc:
        raise _library_http(exc) from exc


@app.get("/api/sync/github")
def get_sync():
    """Download the artifact tree, streamed from disk.

    This used to build the whole archive in a BytesIO and return `buf.read()`,
    so a single request could hold every screenshot and video the service has
    ever produced in RAM. That is a one-request OOM. Build to a temp file and
    stream it, and refuse to grow past a cap.
    """
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    path = handle.name
    total = 0
    try:
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for root, _, files in os.walk(ARTIFACTS):
                for name in files:
                    full = os.path.join(root, name)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    if total + size > guardrails.MAX_ZIP_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Artifact export would exceed "
                                f"{guardrails.MAX_ZIP_BYTES // (1024 * 1024)}MB. "
                                "Download a single run instead."
                            ),
                        )
                    total += size
                    archive.write(full, os.path.relpath(full, ARTIFACTS))
    except HTTPException:
        handle.close()
        os.unlink(path)
        raise
    except Exception as exc:
        handle.close()
        os.unlink(path)
        raise HTTPException(status_code=500, detail=f"Could not build the export: {redact(exc)}") from exc
    handle.close()
    return FileResponse(path, media_type="application/zip", filename="testforge-artifacts.zip",
                        background=BackgroundTask(os.unlink, path))


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
        seq = max((step.order for step in recording.steps), default=0)
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
                saved = await asyncio.to_thread(library_store.export_recording, rid, publish=True)
                if saved and not saved.get("published"):
                    schedule_publish_retry()
            except Exception as e:
                print(f"WS SAVE FAILED {rid} {redact(e)}")


@app.websocket("/ws/batches/{batch_id}")
async def ws_batch(ws: WebSocket, batch_id: str):
    """Live batch progress. Same bounded replay buffer as a single run."""
    await ws.accept()
    key = f"batch:{batch_id}"
    queue = asyncio.Queue()
    RUN_STREAMS.setdefault(key, []).append(queue)
    for event in guardrails.run_buffers.get(key):
        queue.put_nowait(event)
    try:
        await ws.send_json({"type": "batch", "batch_id": batch_id, "status": "connected",
                            "message": "Watching batch progress"})
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except Exception:
        pass
    finally:
        queues = RUN_STREAMS.get(key)
        if queues is not None:
            try:
                queues.remove(queue)
            except ValueError:
                pass
            if not queues:
                RUN_STREAMS.pop(key, None)


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    queue = asyncio.Queue()
    RUN_STREAMS.setdefault(run_id, []).append(queue)
    for event in guardrails.run_buffers.get(run_id):
        queue.put_nowait(event)
    # Immediately send queued status so window displays (requirement 8)
    try:
        await ws.send_json({
            "type": "status", "status": "queued", "percent": 0,
            "message": "Connected - execution starting immediately",
            "display_window": RUN_WINDOWS.get(run_id, True),
        })
    except:
        pass
    try:
        while True:
            event = await queue.get()
            await ws.send_json(event)
    except Exception:
        pass
    finally:
        queues = RUN_STREAMS.get(run_id)
        if queues is not None:
            try:
                queues.remove(queue)
            except ValueError:
                pass
            if not queues:
                RUN_STREAMS.pop(run_id, None)


@app.get("/api/logs/source")
async def logs_source(branch: str | None = None):
    """Where to read harness logs, so the dashboard can fetch them from GitHub.

    Returns coordinates only. The logs themselves are fetched by the browser
    straight from raw.githubusercontent.com, which keeps them off this instance
    entirely - no disk read, no bandwidth, no memory.
    """
    ref = library_store.repo_ref()
    wanted = (branch or ref.get("branch") or "").strip()
    # Every slash-separated segment must start alphanumerically, so "../etc" and
    # friends cannot be smuggled into the raw.githubusercontent.com URL.
    safe_branch = re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*", wanted
    )
    if wanted and (not safe_branch or ".." in wanted):
        raise HTTPException(status_code=422, detail="Branch name is not valid")
    slug = ref.get("slug")
    if not slug:
        # No coordinates: say why, and offer what this instance does have. The tab
        # used to stop here, so a deployment without a checkout showed no logs at
        # all even when the harness had written reports to its own disk.
        local = await asyncio.to_thread(logs_local.source_report,
                                        "No git checkout here, so the committed log location is unknown.")
        return {
            "available": False,
            "reason": (
                "No git checkout here, so the committed log location is unknown. "
                "Set TF_GITHUB_REPO (with TF_GITHUB_BRANCH) to read the committed logs "
                "from GitHub, or mount a checkout."
            ),
            "slug": None,
            "branch": None,
            "base": None,
            "index": None,
            "via": None,
            "local": local,
            "fixes": [
                "TF_GITHUB_REPO=owner/name and TF_GITHUB_BRANCH=main - reads the committed logs from GitHub with no checkout",
                "mount or copy a git checkout above the library folder",
                "TF_LOGS_DIR=/path/to/logs - serve the harness reports stored on this instance",
            ],
        }
    base = f"https://raw.githubusercontent.com/{slug}/{wanted}"
    return {
        "available": True,
        "slug": slug,
        "branch": wanted,
        "base": base,
        "via": ref.get("via"),
        "repository_url": ref.get("repository_url"),
        "index": f"{base}/logs/index.md",
        "api": f"https://api.github.com/repos/{slug}/contents/logs?ref={wanted}",
        # The local reader stays available as a fallback: raw.githubusercontent.com
        # is blocked on some networks, and a branch that has not been pushed yet has
        # no committed logs to read.
        "local": {
            "available": await asyncio.to_thread(logs_local.available),
            "logs_dir": str(logs_local.logs_dir()),
            "endpoints": {"index": "/api/logs/local/index", "file": "/api/logs/local/{folder}/{name}"},
        },
        "note": "Fetched by your browser directly from GitHub. This service never reads or serves the logs.",
    }


@app.get("/api/logs/local/index")
def logs_local_index(limit: int | None = None):
    """Harness reports stored on this instance.

    Only used when the GitHub coordinates are unavailable or unreachable. Bounded
    by TF_MAX_LOG_INDEX_ROWS and cached, so the Logs tab cannot be turned into a
    directory walk on every poll.
    """
    if limit is not None and not str(limit).isdigit():
        raise HTTPException(status_code=422, detail="limit must be a positive number")
    return logs_local.index(limit=int(limit) if limit else None)


@app.get("/api/logs/local/{folder}/{name}")
def logs_local_file(folder: str, name: str, tail: int | None = None):
    """The tail of one local log file, capped at TF_MAX_LOG_FILE_BYTES."""
    result = logs_local.read(folder, name, max_bytes=tail)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "Log file not found")
    return result


@app.get("/api/logs/local/{folder}")
def logs_local_folder(folder: str):
    """Which log files a local harness run has."""
    files = logs_local.files_in(folder)
    if not files:
        raise HTTPException(status_code=404, detail="No log files found for this run")
    return {"folder": folder, "files": files, "logs_dir": str(logs_local.logs_dir())}


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
