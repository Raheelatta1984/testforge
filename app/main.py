import asyncio, os, io, zipfile, base64, re
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
    Project, Recording, RecordingStep, Variable, Run,
)

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

app = FastAPI(title="TestForge Titan ERP")
init_db()
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
    launch. Same-app links are rewritten to loopback. Relative paths, including
    the sample app, always stay on this process.
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
    own_hosts = {"localhost", "127.0.0.1", "0.0.0.0"}
    if request_host:
        own_hosts.add(request_host)
    if host in own_hosts:
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"http://127.0.0.1:{_server_port(request)}{path}{query}"
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
    }


@app.get("/api/projects")
def get_projects():
    with SessionLocal() as db:
        return [_project_dict(project) for project in db.query(Project).order_by(Project.created_at.desc()).all()]


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
        return _insert_project(name, base_url)
    except SQLAlchemyError as exc:
        # A database created by an older revision can be missing columns.
        # Repair it and try once more before surfacing the failure.
        try:
            if repair_schema():
                return _insert_project(name, base_url)
        except SQLAlchemyError:
            pass
        raise HTTPException(
            status_code=503,
            detail=f"Database rejected the project: {redact(exc)}",
        ) from exc


def _insert_project(name: str, base_url: str) -> dict:
    with SessionLocal() as db:
        project = Project(name=name, base_url=base_url)
        db.add(project)
        db.commit()
        db.refresh(project)
        return _project_dict(project)


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
    """Read-only deployment report: schema state, live columns, and a write probe.

    Used to explain failures such as project creation returning HTTP 500 without
    needing shell access to the host.
    """
    report = {
        "status": "ok",
        "revision": os.environ.get("RENDER_GIT_COMMIT", "local"),
        "schema": dict(SCHEMA_STATUS),
        "tables": {},
        "recorder": "ok" if RecorderSession is not None else f"unavailable: {RECORDER_ERROR}",
        "executor": "ok" if execute_run_task is not None else f"unavailable: {EXECUTOR_ERROR}",
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
    if not report["write_probe"]["ok"]:
        report["status"] = "degraded"
    if RecorderSession is None or execute_run_task is None:
        report["status"] = "degraded"
    return report


@app.get("/api/variables")
def get_vars(project_id: str):
    with SessionLocal() as db:
        variables = db.query(Variable).filter_by(project_id=project_id).all()
        recordings = db.query(Recording).filter_by(project_id=project_id).all()
        loaded = [(recording.name, list(recording.steps)) for recording in recordings]
        result = []
        for variable in variables:
            tags = [
                name for name, steps in loaded
                if any(variable.name in (step.value or "") or variable.name in str(step.selector or "") for step in steps)
            ]
            result.append({
                "id": variable.id,
                "name": variable.name,
                "value": variable.value,
                "is_secret": variable.is_secret,
                "tags": tags,
            })
        return result


@app.post("/api/variables", status_code=201)
def create_var(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Variable name is required")
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=422, detail="Project is required")
    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        variable = Variable(project_id=project_id, name=name, value=body.get("value") or "")
        db.add(variable)
        db.commit()
        db.refresh(variable)
        return {"id": variable.id, "name": variable.name, "value": variable.value, "tags": []}


@app.patch("/api/variables/{vid}")
def patch_var(vid: str, body: dict):
    with SessionLocal() as db:
        variable = db.get(Variable, vid)
        if variable is None:
            raise HTTPException(status_code=404, detail="Variable not found")
        if "value" in body:
            variable.value = "" if body.get("value") is None else str(body.get("value"))
        if isinstance(body.get("name"), str) and body["name"].strip():
            variable.name = body["name"].strip()
        db.commit()
        db.refresh(variable)
        return {"id": variable.id, "name": variable.name, "value": variable.value}


@app.delete("/api/variables/{vid}")
def delete_var(vid: str):
    with SessionLocal() as db:
        variable = db.get(Variable, vid)
        if variable is None:
            raise HTTPException(status_code=404, detail="Variable not found")
        db.delete(variable)
        db.commit()
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
    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        recording = Recording(
            project_id=project_id,
            parent_id=body.get("parent_id") or None,
            name=name.strip()[:255],
            start_url=start_url,
        )
        db.add(recording)
        db.commit()
        db.refresh(recording)
        return _recording_dict(recording)


@app.get("/api/projects/{project_id}/recordings")
def list_recordings(project_id: str):
    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = (
            db.query(Recording)
            .filter_by(project_id=project_id)
            .order_by(Recording.created_at.desc())
            .all()
        )
        return [_recording_dict(row) for row in rows]


@app.get("/api/recordings/{rid}")
def get_recording(rid: str):
    with SessionLocal() as db:
        recording = db.get(Recording, rid)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        return _recording_dict(recording, with_steps=True)


@app.post("/api/recordings/{rid}/session")
async def start_recording_session(rid: str, request: Request):
    if open_session is None:
        raise HTTPException(status_code=503, detail=f"Recorder is unavailable: {RECORDER_ERROR}")
    with SessionLocal() as db:
        recording = db.get(Recording, rid)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        start_url = browser_url(recording.start_url or "", request)
        if start_url != recording.start_url:
            recording.start_url = start_url
            db.commit()
        seq = len(list(recording.steps or []))
    session = await open_session(rid, start_url, seq)
    return {"status": session.status, "error": session.error, "url": session.current_url}


@app.get("/api/recordings/{rid}/session")
def recording_session_status(rid: str):
    session = get_session(rid)
    if session is None:
        return {"status": "absent", "error": None}
    return {"status": session.status, "error": session.error, "url": session.current_url}


@app.get("/api/recordings/{rid}/frame")
def recording_frame(rid: str):
    session = get_session(rid)
    if session is None:
        raise HTTPException(status_code=404, detail="Recording session is not running")
    if session.latest_jpeg:
        return Response(
            content=session.latest_jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
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
    return {"ok": True, "step": step}


@app.post("/api/recordings/{rid}/stop")
async def stop_recording(rid: str):
    session = get_session(rid)
    if session is not None:
        await session.stop()
    return {"ok": True}


@app.get("/api/recordings/{rid}/jenkins")
def get_jenkins(rid: str):
    with SessionLocal() as db:
        recording = db.get(Recording, rid)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        script = "pipeline {\n  agent any\n  stages {\n    stage('TestForge') {\n      steps {\n"
        for step in recording.steps:
            script += f"        echo 'Executing {step.action} on {step.label}'\n"
        script += "      }\n    }\n  }\n}\n"
        return PlainTextResponse(script)


@app.post("/api/runs", status_code=201)
async def queue_run(body: dict):
    if execute_run_task is None:
        raise HTTPException(status_code=503, detail=f"Run executor is unavailable: {EXECUTOR_ERROR}")
    target_id = body.get("target_id") or body.get("recording_id")
    if not target_id:
        raise HTTPException(status_code=422, detail="Recording id is required")
    with SessionLocal() as db:
        recording = db.get(Recording, target_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        run = Run(recording_id=recording.id, status="queued")
        db.add(run)
        db.commit()
        db.refresh(run)
        rid = run.id

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
    return {"run_id": rid}


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
            return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        return Response(status_code=204)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/runs/video/{run_id}")
def get_video(run_id: str):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None or not run.video_path:
            raise HTTPException(status_code=404, detail="No video for this run")
        path = run.video_path
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Video file is missing")
    # Only serve files that live under the artifacts directory.
    artifacts_root = os.path.realpath(ARTIFACTS)
    if not os.path.realpath(path).startswith(artifacts_root + os.sep):
        raise HTTPException(status_code=404, detail="Video file is missing")
    return FileResponse(path)


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


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    queue = asyncio.Queue()
    RUN_STREAMS.setdefault(run_id, []).append(queue)
    for event in list(RUN_BUFFERS.get(run_id, [])):
        queue.put_nowait(event)
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
