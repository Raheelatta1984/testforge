import asyncio, os, traceback, csv, io, zipfile, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="TestForge AI Enterprise")

try:
    from app.config import ARTIFACTS
    from app.db import SessionLocal, init_db, Project, Recording, RecordingStep, Variable, Run
    from app.recorder import RecorderSession
    from app.executor import execute_run
except Exception as e:
    print(f"Import Error: {e}")

init_db()
RUN_SUBS = {}

async def emit_run(run_id, evt):
    if run_id in RUN_SUBS:
        for q in RUN_SUBS[run_id]:
            try: q.put_nowait(evt)
            except: pass

@app.get("/api/runs")
def list_runs():
    db = SessionLocal()
    runs = db.query(Run).order_by(Run.created_at.desc()).all()
    return [{"id": r.id, "status": r.status, "target_id": r.target_id, "created_at": str(r.created_at), "log": r.log} for r in runs]

@app.get("/api/runs/screenshot/{run_id}/{filename}")
def get_shot(run_id: str, filename: str):
    return FileResponse(f"{ARTIFACTS}/runs/{run_id}/{filename}")

@app.get("/api/sync/github-bundle")
def github_sync_bundle():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zipf:
        runs_dir = f"{ARTIFACTS}/runs"
        if os.path.exists(runs_dir):
            for root, _, files in os.walk(runs_dir):
                for f in files:
                    zipf.write(os.path.join(root, f), os.path.relpath(os.path.join(root, f), ARTIFACTS))
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=Logs_{timestamp}.zip"})

@app.post("/api/runs")
async def start_run(body: dict):
    db = SessionLocal()
    run = Run(target_type="recording", target_id=body['target_id'], status="queued")
    db.add(run); db.commit(); db.refresh(run)
    rid = run.id
    db.close()
    
    async def on_f(d): await emit_run(rid, {"type":"frame", "data":d})
    async def on_e(e): await emit_run(rid, {"type":"step", **e})
    await execute_run(rid, on_e, on_frame=on_f)
    return {"run_id": rid}

@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    q = asyncio.Queue()
    RUN_SUBS.setdefault(run_id, []).append(q)
    try:
        while True:
            evt = await q.get()
            await ws.send_json(evt)
    except: pass
    finally: RUN_SUBS[run_id].remove(q)

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
    finally: await session.stop()

os.makedirs("app/static", exist_ok=True)
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")