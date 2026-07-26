import asyncio, os, traceback, csv, io, zipfile, datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --- 1. CONFIG & IMPORTS ---
try:
    from app.config import DEMO_MODE, ARTIFACTS
    from app.db import (SessionLocal, init_db, Project, Recording, RecordingStep,
                     Variable, Scenario, ScenarioStep, Run, resolve_variables, interpolate)
    from app.recorder import RecorderSession, ACTIVE
    from app.executor import execute_run
except Exception as e:
    print("CRITICAL IMPORT ERROR IN main.py:")
    traceback.print_exc()
    raise e

# --- 2. APP INITIALIZATION ---
app = FastAPI(title="TestForge AI Enterprise")
init_db()

RUN_SUBS: dict[str, list[asyncio.Queue]] = {}

async def emit_run(run_id: str, evt: dict):
    for q in list(RUN_SUBS.get(run_id, [])):
        try: q.put_nowait(evt)
        except asyncio.QueueFull: pass

# --- 3. SERIALIZERS ---
def proj_dict(p): return {"id": p.id, "name": p.name, "base_url": p.base_url}
def step_dict(s):
    return {"id": s.id, "order": s.order, "action": s.action,
            "selector": s.selector, "value": s.value, "url": s.url,
            "label": s.label, "screenshot_path": s.screenshot_path}
def rec_dict(r, with_steps=False):
    d = {"id": r.id, "project_id": r.project_id, "parent_id": r.parent_id, "name": r.name,
         "description": r.description, "start_url": r.start_url,
         "shared": r.shared, "status": r.status, "has_video": bool(r.video_path),
         "step_count": len(r.steps)}
    if with_steps: d["steps"] = [step_dict(s) for s in r.steps]
    return d
def var_dict(v):
    return {"id": v.id, "scope": v.scope, "project_id": v.project_id,
            "recording_id": v.recording_id, "name": v.name,
            "value": "••••••" if v.is_secret else v.value, "is_secret": v.is_secret}

def run_dict(r, db=None):
    baseline = []
    if r.target_type == "recording" and db:
        rec = db.get(Recording, r.target_id)
        if rec and rec.steps:
            baseline = [step_dict(s) for s in rec.steps]
    return {"id": r.id, "target_type": r.target_type, "target_id": r.target_id,
            "mode": r.mode, "status": r.status, "log": r.log or [],
            "baseline": baseline, "error": r.error, "has_video": bool(r.video_path),
            "created_at": str(r.created_at), "finished_at": str(r.finished_at)}

# --- 4. ENDPOINTS ---

@app.get("/api/projects")
def list_projects():
    db = SessionLocal()
    try: return [proj_dict(p) for p in db.query(Project).all()]
    finally: db.close()

@app.post("/api/projects")
def create_project(body: dict):
    db = SessionLocal()
    try:
        p = Project(name=body['name'], base_url=body.get('base_url', ''))
        db.add(p); db.commit(); db.refresh(p)
        db.add(Variable(scope="project", project_id=p.id, name="base_url", value=p.base_url))
        db.commit()
        return proj_dict(p)
    finally: db.close()

@app.get("/api/sync/github-bundle")
def github_sync_bundle():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zipf:
        runs_dir = f"{ARTIFACTS}/runs"
        if os.path.exists(runs_dir):
            for root, _, files in os.walk(runs_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    arc_path = os.path.relpath(full_path, ARTIFACTS)
                    zipf.write(full_path, arc_path)
    buf.seek(0)
    return Response(buf.read(), media_type="application/zip", 
                    headers={"Content-Disposition": f"attachment; filename=TestForge_Sync_{timestamp}.zip"})

@app.get("/api/projects/{pid}/recordings")
def list_recordings(pid: str):
    db = SessionLocal()
    try:
        recs = db.query(Recording).filter((Recording.project_id == pid) | (Recording.shared == True)).all()
        return [rec_dict(r) for r in recs]
    finally: db.close()

@app.post("/api/recordings")
def create_recording(body: dict):
    db = SessionLocal()
    try:
        r = Recording(project_id=body['project_id'], parent_id=body.get('parent_id'), 
                      name=body['name'], start_url=body['start_url'], shared=body.get('shared', False), status="recording")
        db.add(r); db.commit(); db.refresh(r)
        return {"id": r.id}
    finally: db.close()

@app.get("/api/recordings/{rid}")
def get_recording(rid: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        return rec_dict(r, with_steps=True)
    finally: db.close()

@app.patch("/api/recordings/{rid}")
def patch_recording(rid: str, body: dict):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if "shared" in body: r.shared = body["shared"]
        db.commit()
        return rec_dict(r)
    finally: db.close()

@app.get("/api/recordings/{rid}/export/jenkins")
def export_gherkin(rid: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        feature = f"Feature: {r.name}\n  Scenario: Automated UI flow\n    Given I launch URL '{r.start_url}'\n"
        for s in r.steps:
            if s.action == "click": feature += f"    When I click '{s.label}'\n"
            elif s.action == "fill": feature += f"    And I enter '{s.value}' into '{s.label}'\n"
        return PlainTextResponse(feature)
    finally: db.close()

@app.get("/api/variables")
def list_variables(project_id: str | None = None):
    db = SessionLocal()
    try:
        q = db.query(Variable)
        if project_id: q = q.filter((Variable.scope == "global") | (Variable.project_id == project_id))
        result = []
        for v in q.all():
            vd = var_dict(v)
            recs = db.query(Recording).all()
            vd["associated_recordings"] = [r.name for r in recs if any(v.name in (step.value or "") for step in r.steps)]
            result.append(vd)
        return result
    finally: db.close()

@app.post("/api/variables")
def create_variable(body: dict):
    db = SessionLocal()
    try:
        v = Variable(**body)
        db.add(v); db.commit(); db.refresh(v)
        return var_dict(v)
    finally: db.close()

@app.patch("/api/variables/{vid}")
def patch_variable(vid: str, body: dict):
    db = SessionLocal()
    try:
        v = db.get(Variable, vid)
        if "value" in body: v.value = body["value"]
        db.commit()
        return var_dict(v)
    finally: db.close()

@app.delete("/api/variables/{vid}")
def delete_variable(vid: str):
    db = SessionLocal()
    try:
        v = db.get(Variable, vid)
        if v: db.delete(v); db.commit()
        return {"ok": True}
    finally: db.close()

@app.post("/api/runs")
def start_run(body: dict):
    db = SessionLocal()
    try:
        run = Run(target_type=body['target_type'], target_id=body['target_id'], mode="script")
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
        db_runs = db.query(Run).order_by(Run.created_at.desc()).limit(50).all()
        return [run_dict(r, db) for r in db_runs]
    finally: db.close()

@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str):
    db = SessionLocal()
    try:
        r = db.get(Run, run_id)
        return run_dict(r, db)
    finally: db.close()

@app.get("/api/runs/screenshot/{run_id}/{filename}")
def run_screenshot(run_id: str, filename: str):
    return FileResponse(f"{ARTIFACTS}/runs/{run_id}/{filename}")

@app.get("/api/runs/video/{run_id}")
def run_video(run_id: str):
    db = SessionLocal()
    try:
        r = db.get(Run, run_id)
        if r.video_path: return FileResponse(r.video_path)
        raise HTTPException(404)
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
    finally: 
        if q in RUN_SUBS.get(run_id, []): RUN_SUBS[run_id].remove(q)

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
    ACTIVE[recording_id] = session
    await session.start()
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "stop": break
            await session.handle_input(msg)
    except: pass
    finally:
        await session.stop()
        ACTIVE.pop(recording_id, None)

@app.post("/api/ai/suggest-step")
def ai_suggest_step(body: dict):
    import random
    return {"suggestion": random.choice(["Click Login", "Validate Text", "Submit Form"])}

@app.post("/api/ai/suggest-variables")
def ai_suggest_variables(body: dict):
    return {"suggestions": [{"name": "email", "value": "test@user.com"}]}

# --- 5. STATIC FILES ---
os.makedirs("app/static", exist_ok=True)
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")