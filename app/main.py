import asyncio, os, traceback, csv, io
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from app.config import DEMO_MODE, ARTIFACTS
    from app.db import (SessionLocal, init_db, Project, Recording, RecordingStep,
                     Variable, Scenario, ScenarioStep, Run)
    from app.recorder import RecorderSession, ACTIVE
    from app.executor import execute_run
except Exception as e:
    print("CRITICAL IMPORT ERROR IN main.py:")
    traceback.print_exc()
    raise e

app = FastAPI(title="TestForge AI Enterprise")
init_db()

RUN_SUBS: dict[str, list[asyncio.Queue]] = {}

async def emit_run(run_id: str, evt: dict):
    for q in list(RUN_SUBS.get(run_id, [])):
        try: q.put_nowait(evt)
        except asyncio.QueueFull: pass

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
def scen_dict(s, with_steps=False):
    d = {"id": s.id, "project_id": s.project_id, "title": s.title,
         "status": s.status, "step_count": len(s.steps)}
    if with_steps:
        d["steps"] = [{"id": st.id, "order": st.order, "action": st.action,
                       "selector": st.selector, "value": st.value, "expected_result": st.expected_result}
                      for st in s.steps]
    return d
def run_dict(r):
    return {"id": r.id, "target_type": r.target_type, "target_id": r.target_id,
            "mode": r.mode, "status": r.status, "log": r.log or [],
            "error": r.error, "has_video": bool(r.video_path),
            "created_at": str(r.created_at), "finished_at": str(r.finished_at)}

class ProjectCreate(BaseModel): name: str; base_url: str = ""

@app.get("/api/projects")
def list_projects():
    db = SessionLocal()
    try: return [proj_dict(p) for p in db.query(Project).all()]
    finally: db.close()

@app.post("/api/projects")
def create_project(body: ProjectCreate):
    db = SessionLocal()
    try:
        p = Project(name=body.name, base_url=body.base_url)
        db.add(p); db.commit(); db.refresh(p)
        db.add(Variable(scope="project", project_id=p.id, name="base_url", value=body.base_url or "https://example.com"))
        db.add(Variable(scope="project", project_id=p.id, name="test_user", value="qa.user@enterprise.com"))
        db.commit()
        return proj_dict(p)
    finally: db.close()

@app.post("/api/ai/rephrase")
async def ai_rephrase(body: dict):
    text = body.get("text", "")
    return {"rephrased": f"Given environment is ready, When user executes '{text}', Then system validates successfully."}

@app.post("/api/ai/suggest-variables")
def ai_suggest_variables(body: dict):
    return {"suggestions": [{"name": "username", "value": "qa_admin"}, {"name": "password", "value": "Secure2026!"}]}

@app.post("/api/ai/suggest-step")
def ai_suggest_step(body: dict):
    import random
    return {"suggestion": random.choice(["Click search button", "Assert welcome message visible", "Fill password securely", "Submit form"])}

class RecordingCreate(BaseModel): project_id: str; parent_id: str | None = None; name: str; start_url: str; shared: bool = False

@app.get("/api/projects/{pid}/recordings")
def list_recordings(pid: str):
    db = SessionLocal()
    try:
        recs = db.query(Recording).filter((Recording.project_id == pid) | (Recording.shared == True)).all()
        return [rec_dict(r) for r in recs]
    finally: db.close()

@app.post("/api/recordings")
def create_recording(body: RecordingCreate):
    db = SessionLocal()
    try:
        r = Recording(project_id=body.project_id, parent_id=body.parent_id, name=body.name, start_url=body.start_url, shared=body.shared, status="recording")
        db.add(r); db.commit(); db.refresh(r)
        return {"id": r.id}
    finally: db.close()

@app.get("/api/recordings/{rid}")
def get_recording(rid: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if not r: raise HTTPException(404)
        return rec_dict(r, with_steps=True)
    finally: db.close()

@app.delete("/api/recordings/{rid}")
def delete_recording(rid: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if r: db.delete(r); db.commit()
        return {"ok": True}
    finally: db.close()

@app.get("/api/recordings/{rid}/export/{fmt}")
def export_recording(rid: str, fmt: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if not r: raise HTTPException(404)
        steps = r.steps
        if fmt == "jenkins":
            feature = f"Feature: {r.name}\n  Scenario: Automated UI flow\n    Given I launch URL '{r.start_url}'\n"
            for s in steps:
                val = s.value or ""
                if s.action == "click": feature += f"    When I click element '{s.label or s.action}'\n"
                elif s.action == "fill": feature += f"    And I enter '{val}' into '{s.label or s.action}'\n"
            feature += "    Then test completes successfully\n"
            return PlainTextResponse(feature, media_type="text/plain", headers={"Content-Disposition": f"attachment; filename={r.name}.feature"})
        elif fmt == "csv":
            output = io.StringIO(); writer = csv.writer(output)
            writer.writerow(["Order", "Action", "Selector", "Value", "Label"])
            for s in steps: writer.writerow([s.order, s.action, (s.selector or {}).get("primary", ""), s.value or "", s.label or ""])
            return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={r.name}.csv"})
        raise HTTPException(400, "Invalid format")
    finally: db.close()

@app.get("/api/runs/screenshot/{run_id}/{filename}")
def run_screenshot(run_id: str, filename: str):
    path = f"{ARTIFACTS}/runs/{run_id}/{filename}"
    if os.path.exists(path): return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(404)

@app.get("/api/runs/video/{run_id}")
def run_video(run_id: str):
    db = SessionLocal()
    try:
        r = db.get(Run, run_id)
        if r and r.video_path and os.path.exists(r.video_path): return FileResponse(r.video_path, media_type="video/webm")
        raise HTTPException(404)
    finally: db.close()

class VariableCreate(BaseModel): scope: str; project_id: str | None = None; name: str; value: str = ""; is_secret: bool = False

@app.get("/api/variables")
def list_variables(project_id: str | None = None):
    db = SessionLocal()
    try:
        q = db.query(Variable)
        if project_id: q = q.filter((Variable.scope == "global") | (Variable.project_id == project_id))
        return [var_dict(v) for v in q.all()]
    finally: db.close()

@app.post("/api/variables")
def create_variable(body: VariableCreate):
    db = SessionLocal()
    try:
        v = Variable(**body.dict())
        db.add(v); db.commit(); db.refresh(v)
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

class RunCreate(BaseModel): target_type: str; target_id: str; mode: str = "script"

@app.post("/api/runs")
def start_run(body: RunCreate):
    db = SessionLocal()
    try:
        run = Run(target_type=body.target_type, target_id=body.target_id, mode=body.mode)
        db.add(run); db.commit(); db.refresh(run)
        rid = run.id
    finally: db.close()
    asyncio.create_task(execute_run(rid, lambda e: emit_run(rid, e)))
    return {"run_id": rid}

@app.get("/api/runs")
def list_runs():
    db = SessionLocal()
    try: return [run_dict(r) for r in db.query(Run).order_by(Run.created_at.desc()).limit(50).all()]
    finally: db.close()

@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    db = SessionLocal()
    r = db.get(Run, run_id); snapshot = run_dict(r) if r else None
    db.close()
    await ws.send_json({"type": "snapshot", "run": snapshot})
    if snapshot and snapshot["status"] not in ("queued", "running"): return
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    RUN_SUBS.setdefault(run_id, []).append(q)
    try:
        while True:
            evt = await q.get(); await ws.send_json(evt)
            if evt.get("type") == "done": break
    except WebSocketDisconnect: pass
    finally:
        if q in RUN_SUBS.get(run_id, []): RUN_SUBS[run_id].remove(q)

@app.websocket("/ws/record/{recording_id}")
async def ws_record(ws: WebSocket, recording_id: str):
    await ws.accept()
    db = SessionLocal()
    rec = db.get(Recording, recording_id)
    start_url = rec.start_url if rec else "about:blank"
    start_seq = len(rec.steps) if rec and rec.steps else 0
    db.close()
    q: asyncio.Queue = asyncio.Queue(maxsize=120)
    async def on_frame(data):
        if q.full():
            try: q.get_nowait()
            except asyncio.QueueEmpty: pass
        q.put_nowait({"type": "frame", "data": data})
    async def on_event(step): q.put_nowait({"type": "step", "step": step})
    session = RecorderSession(recording_id, start_url, start_seq, on_frame, on_event)
    ACTIVE[recording_id] = session
    await session.start()
    async def sender():
        while True: await ws.send_json(await q.get())
    send_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "stop": break
            await session.handle_input(msg)
    except WebSocketDisconnect: pass
    finally:
        send_task.cancel()
        try: await session.stop()
        except Exception: pass
        ACTIVE.pop(recording_id, None)
        try: await ws.send_json({"type": "stopped"})
        except Exception: pass

os.makedirs("app/static", exist_ok=True)
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")