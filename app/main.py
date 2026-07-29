import asyncio, os, traceback, csv, io, zipfile, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="TestForge AI Enterprise")

try:
    from app.config import DEMO_MODE, ARTIFACTS
    from app.db import (SessionLocal, init_db, Project, Recording, RecordingStep,
                     Variable, Run, resolve_variables, interpolate)
    from app.recorder import RecorderSession
    from app.executor import execute_run
except Exception as e:
    print("IMPORT ERROR")
    traceback.print_exc()

init_db()
RUN_SUBS = {}

async def emit_run(run_id: str, evt: dict):
    if run_id in RUN_SUBS:
        for q in list(RUN_SUBS[run_id]):
            try: q.put_nowait(evt)
            except: pass

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
        db.add(Variable(scope="project", project_id=p.id, name="base_url", value=p.base_url))
        db.commit()
        return {"id": p.id, "name": p.name}
    finally: db.close()

@app.get("/api/variables")
def list_variables(project_id: str | None = None):
    db = SessionLocal()
    try:
        q = db.query(Variable)
        if project_id: q = q.filter_by(project_id=project_id)
        res = []
        for v in q.all():
            recs = db.query(Recording).all()
            tags = [r.name for r in recs if any(v.name in str(s.value) for s in r.steps)]
            res.append({"id":v.id, "name":v.name, "value":v.value, "associated_recordings": tags})
        return res
    finally: db.close()

@app.post("/api/runs")
def start_run(body: dict):
    db = SessionLocal()
    try:
        run = Run(target_type="recording", target_id=body['target_id'], status="queued")
        db.add(run); db.commit(); db.refresh(run)
        rid = run.id
        async def bg():
            async def on_f(d): await emit_run(rid, {"type":"frame", "data":d})
            async def on_e(e): await emit_run(rid, {"type":"step", **e})
            await execute_run(rid, on_e, on_frame=on_f)
        asyncio.create_task(bg())
        return {"run_id": rid}
    finally: db.close()

@app.get("/api/runs")
def list_runs():
    db = SessionLocal()
    try:
        runs = db.query(Run).order_by(Run.created_at.desc()).limit(20).all()
        return [{"id":r.id, "status":r.status, "created_at":str(r.created_at), "log":r.log} for r in runs]
    finally: db.close()

@app.get("/api/projects/{pid}/recordings")
def list_recs(pid: str):
    db = SessionLocal()
    try:
        recs = db.query(Recording).filter((Recording.project_id==pid)|(Recording.shared==True)).all()
        return [{"id":r.id, "name":r.name, "step_count":len(r.steps), "parent_id":r.parent_id} for r in recs]
    finally: db.close()

@app.websocket("/ws/record/{rid}")
async def ws_record(ws: WebSocket, rid: str):
    await ws.accept()
    db = SessionLocal()
    rec = db.get(Recording, rid)
    session = RecorderSession(rid, rec.start_url, len(rec.steps), lambda d: ws.send_json({"type":"frame","data":d}), lambda e: ws.send_json({"type":"step","step":e}))
    db.close()
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
    q = asyncio.Queue()
    RUN_SUBS.setdefault(run_id, []).append(q)
    try:
        while True:
            evt = await q.get()
            await ws.send_json(evt)
    except: pass
    finally: RUN_SUBS[run_id].remove(q)

static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=static_path, html=True), name="static")