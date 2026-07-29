import asyncio, os, traceback, csv, io, zipfile, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# --- 1. APP INSTANCE (MUST BE AT TOP) ---
app = FastAPI(title="TestForge AI Enterprise")

# --- 2. CONFIG & IMPORTS ---
try:
    from app.config import DEMO_MODE, ARTIFACTS
    from app.db import (SessionLocal, init_db, Project, Recording, RecordingStep,
                     Variable, Run, resolve_variables, interpolate)
    from app.recorder import RecorderSession
    from app.executor import execute_run
except Exception as e:
    print("CRITICAL IMPORT ERROR")
    traceback.print_exc()
    raise e

init_db()
RUN_SUBS = {}

async def emit_run(run_id: str, evt: dict):
    if run_id in RUN_SUBS:
        for q in list(RUN_SUBS[run_id]):
            try: q.put_nowait(evt)
            except: pass

# --- 3. ENDPOINTS ---
@app.get("/api/projects")
def list_projects():
    db = SessionLocal()
    try: return [{"id": p.id, "name": p.name, "base_url": p.base_url} for p in db.query(Project).all()]
    finally: db.close()

@app.post("/api/projects")
def create_project(body: dict):
    db = SessionLocal()
    try:
        p = Project(name=body['name'], base_url=body.get('base_url', ''))
        db.add(p); db.commit(); db.refresh(p)
        return {"id": p.id, "name": p.name}
    finally: db.close()

@app.get("/api/variables")
def list_variables(project_id: str | None = None):
    db = SessionLocal()
    try:
        q = db.query(Variable)
        if project_id: q = q.filter(Variable.project_id == project_id)
        result = []
        for v in q.all():
            vd = {"id":v.id, "name":v.name, "value":v.value}
            recs = db.query(Recording).all()
            vd["associated_recordings"] = [r.name for r in recs if any(v.name in str(s.value) for s in r.steps)]
            result.append(vd)
        return result
    finally: db.close()

@app.get("/api/sync/github-bundle")
def github_sync_bundle():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zipf:
        runs_dir = os.path.join(ARTIFACTS, "runs")
        if os.path.exists(runs_dir):
            for root, _, files in os.walk(runs_dir):
                for f in files: zipf.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), ARTIFACTS))
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip")

@app.post("/api/runs")
def start_run(body: dict):
    db = SessionLocal()
    try:
        run = Run(target_type=body['target_type'], target_id=body['target_id'], status="queued")
        db.add(run); db.commit(); db.refresh(run)
        rid = run.id
        async def bg():
            async def on_f(d): await emit_run(rid, {"type":"frame", "data":d})
            await execute_run(rid, lambda e: emit_run(rid, e), on_frame=on_f)
        asyncio.create_task(bg())
        return {"run_id": rid}
    finally: db.close()

@app.get("/api/runs")
def list_runs():
    db = SessionLocal()
    try:
        runs = db.query(Run).order_by(Run.created_at.desc()).limit(20).all()
        result = []
        for r in runs:
            baseline = []
            rec = db.get(Recording, r.target_id)
            if rec: baseline = [{"order": s.order, "action": s.action, "label": s.label} for s in rec.steps]
            result.append({"id": r.id, "status": r.status, "created_at": str(r.created_at), "log": r.log, "baseline": baseline})
        return result
    finally: db.close()

@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    q = asyncio.Queue()
    RUN_SUBS.setdefault(run_id, []).append(q)
    try:
        while True:
            evt = await q.get()
            await ws.send_json(evt)
            if evt.get("type") == "done": break
    except: pass
    finally: RUN_SUBS[run_id].remove(q)

@app.websocket("/ws/record/{recording_id}")
async def ws_record(ws: WebSocket, recording_id: str):
    await ws.accept()
    db = SessionLocal()
    rec = db.get(Recording, recording_id)
    url, seq = (rec.start_url, len(rec.steps)) if rec else ("about:blank", 0)
    db.close()
    async def on_f(d): await ws.send_json({"type":"frame", "data":d})
    async def on_e(s): await ws.send_json({"type":"step", "step":s})
    session = RecorderSession(recording_id, url, seq, on_f, on_e)
    await session.start()
    try:
        while True:
            msg = await ws.receive_json()
            if msg['type'] == 'stop': break
            await session.handle_input(msg)
    finally: await session.stop()

# --- 4. STATIC FILES ---
static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(static_path):
    app.mount("/", StaticFiles(directory=static_path, html=True), name="static")