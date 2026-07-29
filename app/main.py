import asyncio, os, traceback, io, zipfile, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# CRITICAL BOOT ORDER
app = FastAPI(title="TestForge Titan ERP")

try:
    from app.config import ARTIFACTS, DEMO_MODE
    from app.db import (SessionLocal, init_db, Project, Recording, RecordingStep, Variable, Run)
    from app.recorder import RecorderSession
    from app.executor import execute_run_task
except Exception as e:
    print(f"BOOTSTRAP FAILURE: {e}")
    traceback.print_exc()

init_db()
RUN_STREAMS = {}

async def broadcast_run(run_id, payload):
    if run_id in RUN_STREAMS:
        for q in list(RUN_STREAMS[run_id]):
            try: q.put_nowait(payload)
            except: pass

@app.get("/api/projects")
def get_projects():
    db = SessionLocal()
    return db.query(Project).all()

@app.post("/api/projects")
def create_project(body: dict):
    db = SessionLocal()
    p = Project(name=body['name'], base_url=body.get('base_url', ''))
    db.add(p); db.commit(); db.refresh(p)
    return p

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
def queue_run(body: dict):
    db = SessionLocal()
    run = Run(recording_id=body['target_id'], status="queued")
    db.add(run); db.commit(); db.refresh(run)
    rid = run.id
    db.close()
    
    async def task_wrapper():
        async def on_frame(d): await broadcast_run(rid, {"type":"frame", "data":d})
        async def on_evt(e): await broadcast_run(rid, e)
        await execute_run_task(rid, on_evt, on_frame=on_frame)
        
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