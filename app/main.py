import asyncio, os, traceback, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import ARTIFACTS
from app.db import SessionLocal, init_db, Project, Recording, RecordingStep, Variable, Run
from app.recorder import RecorderSession, ACTIVE
from app.executor import execute_run

app = FastAPI(title="TestForge AI Enterprise")
init_db()

RUN_SUBS = {}

async def emit_run(run_id, evt):
    if run_id in RUN_SUBS:
        for q in RUN_SUBS[run_id]:
            try: q.put_nowait(evt)
            except: pass

@app.post("/api/recordings")
async def create_rec(body: dict):
    db = SessionLocal()
    r = Recording(project_id=body['project_id'], name=body['name'], start_url=body['start_url'])
    db.add(r); db.commit(); db.refresh(r)
    return {"id": r.id}

@app.post("/api/runs")
async def start_run(body: dict):
    db = SessionLocal()
    run = Run(target_type="recording", target_id=body['target_id'], status="queued")
    db.add(run); db.commit(); db.refresh(run)
    rid = run.id
    db.close()
    
    async def bg():
        async def on_f(d): await emit_run(rid, {"type":"frame", "data":d})
        async def on_e(e): await emit_run(rid, e)
        await execute_run(rid, on_e, on_frame=on_f)
    
    asyncio.create_task(bg())
    return {"run_id": rid}

@app.get("/api/runs")
def list_runs():
    db = SessionLocal()
    return db.query(Run).order_by(Run.created_at.desc()).all()

@app.websocket("/ws/record/{recording_id}")
async def ws_record(ws: WebSocket, recording_id: str):
    await ws.accept()
    db = SessionLocal()
    rec = db.get(Recording, recording_id)
    session = RecorderSession(recording_id, rec.start_url, 0, 
                               lambda d: ws.send_json({"type":"frame","data":d}),
                               lambda e: ws.send_json({"type":"step","step":e}))
    await session.start()
    try:
        while True:
            msg = await ws.receive_json()
            if msg['type'] == 'stop': break
            await session.handle_input(msg)
    finally:
        await session.stop()

@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    q = asyncio.Queue()
    RUN_SUBS.setdefault(run_id, []).append(q)
    try:
        while True:
            evt = await q.get()
            await ws.send_json(evt)
    finally:
        RUN_SUBS[run_id].remove(q)

os.makedirs("app/static", exist_ok=True)
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")