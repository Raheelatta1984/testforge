import asyncio, os, io, zipfile, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import ARTIFACTS, DEMO_MODE
from app.errors import redact
from app.db import (
    SCHEMA_STATUS, SessionLocal, engine, init_db, repair_schema,
    Project, Recording, RecordingStep, Variable, Run,
)

# Browser-dependent features are optional at boot so the dashboard and its API
# remain usable if the browser runtime is unavailable.
try:
    from app.recorder import RecorderSession
except Exception as exc:
    RecorderSession = None
    print(f"RECORDER UNAVAILABLE: {exc}")

try:
    from app.executor import execute_run as execute_run_task
except Exception as exc:
    execute_run_task = None
    print(f"RUN EXECUTOR UNAVAILABLE: {exc}")

app = FastAPI(title="TestForge Titan ERP")
init_db()
RUN_STREAMS = {}


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

async def broadcast_run(run_id, payload):
    if run_id in RUN_STREAMS:
        for q in list(RUN_STREAMS[run_id]):
            try: q.put_nowait(payload)
            except: pass

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
        "revision": os.environ.get("RENDER_GIT_COMMIT", "local"),
    }

@app.get("/api/projects")
def get_projects():
    with SessionLocal() as db:
        return db.query(Project).order_by(Project.created_at.desc()).all()

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


def _insert_project(name: str, base_url: str) -> Project:
    with SessionLocal() as db:
        project = Project(name=name, base_url=base_url)
        db.add(project)
        db.commit()
        db.refresh(project)
        return project

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
    return report


@app.get("/api/variables")
def get_vars(project_id: str):
    db = SessionLocal()
    vars = db.query(Variable).filter_by(project_id=project_id).all()
    # Hybrid logic: attach recording names as tags
    res = []
    for v in vars:
        res.append({
            "id": v.id, "name": v.name, "value": v.value,
            "tags": [r.name for r in db.query(Recording).all() if any(v.name in str(s.value) for s in r.steps)]
        })
    return res

@app.post("/api/recordings")
def create_rec(body: dict):
    db = SessionLocal()
    r = Recording(project_id=body['project_id'], parent_id=body.get('parent_id'), name=body['name'], start_url=body['start_url'])
    db.add(r); db.commit(); db.refresh(r)
    return r

@app.get("/api/recordings/{rid}/jenkins")
def get_jenkins(rid: str):
    db = SessionLocal()
    r = db.get(Recording, rid)
    script = f"pipeline {{\n  agent any\n  stages {{\n    stage('TestForge ROG') {{\n      steps {{\n"
    for s in r.steps:
        script += f"        echo 'Executing {s.action} on {s.label}'\n"
    script += "      }\n    }\n  }\n}"
    return PlainTextResponse(script)

@app.post("/api/runs")
async def queue_run(body: dict):
    if execute_run_task is None:
        raise HTTPException(status_code=503, detail="Run executor is unavailable")
    target_id = body.get("target_id")
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

        await execute_run_task(rid, on_event, on_frame=on_frame)

    asyncio.create_task(task_wrapper())
    return {"run_id": rid}

@app.get("/api/runs")
def get_runs():
    db = SessionLocal()
    return db.query(Run).order_by(Run.created_at.desc()).all()

@app.get("/api/runs/screenshot/{run_id}/{filename}")
def get_screenshot(run_id: str, filename: str):
    return FileResponse(os.path.join(ARTIFACTS, "runs", run_id, filename))

@app.get("/api/sync/github")
def get_sync():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for root, _, files in os.walk(ARTIFACTS):
            for f in files: z.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), ARTIFACTS))
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip")

@app.websocket("/ws/record/{rid}")
async def ws_rec(ws: WebSocket, rid: str):
    await ws.accept()
    db = SessionLocal(); rec = db.get(Recording, rid); db.close()
    session = RecorderSession(rid, rec.start_url, len(rec.steps), 
                               lambda d: ws.send_json({"type":"frame","data":d}),
                               lambda e: ws.send_json({"type":"step","step":e}))
    await session.start()
    try:
        while True:
            m = await ws.receive_json()
            if m['type'] == 'stop': break
            await session.handle_input(m)
    finally: await session.stop()

@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    q = asyncio.Queue(); RUN_STREAMS.setdefault(run_id, []).append(q)
    try:
        while True:
            evt = await q.get(); await ws.send_json(evt)
    except: pass
    finally: RUN_STREAMS[run_id].remove(q)

@app.post("/api/ai/rephrase")
async def ai_rephrase(body: dict):
    return {"rephrased": f"ROG-Refined: {body.get('text','')}"}

static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=static_path, html=True), name="static")