import asyncio, os, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from app.config import DEMO_MODE
    from app.db import (SessionLocal, init_db, Project, Recording, RecordingStep,
                     Variable, Scenario, ScenarioStep, Run)
    from app.recorder import RecorderSession, ACTIVE
    from app.executor import execute_run
except Exception as e:
    print("CRITICAL IMPORT ERROR IN main.py:")
    traceback.print_exc()
    raise e

app = FastAPI(title="TestForge")
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
            "label": s.label, "screenshot_path": s.screenshot_path,
            "ref_recording_id": s.ref_recording_id, "variable_overrides": s.variable_overrides}
def rec_dict(r, with_steps=False):
    d = {"id": r.id, "project_id": r.project_id, "name": r.name,
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
                       "selector": st.selector, "value": st.value, "expected_result": st.expected_result,
                       "ref_recording_id": st.ref_recording_id, "variable_overrides": st.variable_overrides}
                      for st in s.steps]
    return d
def run_dict(r):
    return {"id": r.id, "target_type": r.target_type, "target_id": r.target_id,
            "mode": r.mode, "status": r.status, "log": r.log or [],
            "error": r.error, "has_video": bool(r.video_path),
            "has_transcript": bool(r.agent_transcript),
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
        return proj_dict(p)
    finally: db.close()

class RecordingCreate(BaseModel): project_id: str; name: str; start_url: str; shared: bool = False

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
        r = Recording(project_id=body.project_id, name=body.name, start_url=body.start_url, shared=body.shared, status="recording")
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

class RecordingPatch(BaseModel): name: str | None = None; shared: bool | None = None; description: str | None = None

@app.patch("/api/recordings/{rid}")
def patch_recording(rid: str, body: RecordingPatch):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if not r: raise HTTPException(404)
        for k, v in body.dict(exclude_none=True).items(): setattr(r, k, v)
        db.commit()
        return rec_dict(r)
    finally: db.close()

@app.delete("/api/recordings/{rid}")
def delete_recording(rid: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if r: db.delete(r); db.commit()
        return {"ok": True}
    finally: db.close()

class DuplicateReq(BaseModel): target_project_id: str

@app.post("/api/recordings/{rid}/duplicate")
def duplicate_recording(rid: str, body: DuplicateReq):
    db = SessionLocal()
    try:
        src = db.get(Recording, rid)
        if not src: raise HTTPException(404)
        copy = Recording(project_id=body.target_project_id, name=src.name + " (copy)", start_url=src.start_url, description=src.description, status="ready")
        db.add(copy); db.flush()
        for s in src.steps:
            db.add(RecordingStep(recording_id=copy.id, order=s.order, action=s.action, selector=s.selector, value=s.value, url=s.url, label=s.label, ref_recording_id=s.ref_recording_id, variable_overrides=s.variable_overrides))
        db.commit()
        return {"id": copy.id}
    finally: db.close()

@app.get("/api/recordings/{rid}/video")
def recording_video(rid: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if r and r.video_path and os.path.exists(r.video_path): return FileResponse(r.video_path, media_type="video/webm")
        raise HTTPException(404)
    finally: db.close()

class StepPatch(BaseModel): value: str | None = None; label: str | None = None; primary: str | None = None

@app.patch("/api/steps/{step_id}")
def patch_step(step_id: str, body: StepPatch):
    db = SessionLocal()
    try:
        s = db.get(RecordingStep, step_id)
        if not s: raise HTTPException(404)
        if body.value is not None: s.value = body.value
        if body.label is not None: s.label = body.label
        if body.primary is not None:
            sel = dict(s.selector or {}); sel["primary"] = body.primary; s.selector = sel
        db.commit()
        return step_dict(s)
    finally: db.close()

@app.delete("/api/steps/{step_id}")
def delete_step(step_id: str):
    db = SessionLocal()
    try:
        s = db.get(RecordingStep, step_id)
        if s: db.delete(s); db.commit()
        return {"ok": True}
    finally: db.close()

@app.post("/api/steps/{step_id}/move")
def move_step(step_id: str, direction: str):
    db = SessionLocal()
    try:
        s = db.get(RecordingStep, step_id)
        if not s: raise HTTPException(404)
        sib = (db.query(RecordingStep).filter(RecordingStep.recording_id == s.recording_id, RecordingStep.order == (s.order - 1 if direction == "up" else s.order + 1)).first())
        if sib: s.order, sib.order = sib.order, s.order; db.commit()
        return {"ok": True}
    finally: db.close()

class StepCreate(BaseModel): action: str; value: str | None = None; url: str | None = None; label: str | None = None; ref_recording_id: str | None = None; variable_overrides: dict = {}

@app.post("/api/recordings/{rid}/steps")
def add_step(rid: str, body: StepCreate):
    db = SessionLocal()
    try:
        rec = db.get(Recording, rid)
        if not rec: raise HTTPException(404)
        nxt = (max([s.order for s in rec.steps]) + 1) if rec.steps else 1
        s = RecordingStep(recording_id=rid, order=nxt, action=body.action, value=body.value, url=body.url, label=body.label, ref_recording_id=body.ref_recording_id, variable_overrides=body.variable_overrides or None)
        db.add(s); db.commit(); db.refresh(s)
        return step_dict(s)
    finally: db.close()

@app.get("/api/steps/{step_id}/screenshot")
def step_screenshot(step_id: str):
    db = SessionLocal()
    try:
        s = db.get(RecordingStep, step_id)
        if s and s.screenshot_path and os.path.exists(s.screenshot_path): return FileResponse(s.screenshot_path, media_type="image/jpeg")
        raise HTTPException(404)
    finally: db.close()

class VariableCreate(BaseModel): scope: str; project_id: str | None = None; recording_id: str | None = None; name: str; value: str = ""; is_secret: bool = False

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

class ScenarioGenerate(BaseModel): project_id: str; source_text: str

SCENARIO_TOOL = {
    "name": "save_scenarios",
    "description": "Save generated QA scenarios",
    "input_schema": {"type": "object", "properties": {"scenarios": {"type": "array",
        "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "action": {"enum": ["navigate", "click", "fill", "press", "assert_text", "wait"]},
                "selector_hint": {"type": "string"}, "value": {"type": "string"}, "expected_result": {"type": "string"}},
                "required": ["action"]}}}},
        "required": ["title", "steps"]}}},
    "required": ["scenarios"]}

async def llm_scenarios(text: str) -> list:
    import anthropic
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-sonnet-4-5", max_tokens=4096,
        tools=[SCENARIO_TOOL], tool_choice={"type": "tool", "name": "save_scenarios"},
        messages=[{"role": "user", "content": f"Senior QA: turn this user story into UI test scenarios (happy path + negative). Use {{variable}} placeholders.\n\n{text}"}])
    return next(b.input for b in resp.content if b.type == "tool_use")["scenarios"]

def demo_scenarios(text: str) -> list:
    kw = (text[:60] or "flow").strip()
    return [{"title": f"Happy path — {kw}", "steps": [{"action": "navigate", "value": "{{base_url}}", "expected_result": "Page loads"}, {"action": "fill", "selector_hint": "main input", "value": "{{test_input}}", "expected_result": "Value accepted"}, {"action": "press", "value": "Enter", "expected_result": "Form submitted"}, {"action": "assert_text", "value": "{{test_input}}", "expected_result": "Entry visible"}]}, {"title": f"Negative — empty submit — {kw}", "steps": [{"action": "navigate", "value": "{{base_url}}", "expected_result": "Page loads"}, {"action": "press", "value": "Enter", "expected_result": "Validation blocks"}]}]

@app.post("/api/scenarios")
async def generate_scenario(body: ScenarioGenerate):
    scenarios = (demo_scenarios(body.source_text) if DEMO_MODE else await llm_scenarios(body.source_text))
    db = SessionLocal()
    try:
        created = []
        for sc in scenarios:
            row = Scenario(project_id=body.project_id, title=sc["title"], source_text=body.source_text[:4000], status="ready")
            db.add(row); db.flush()
            for i, st in enumerate(sc["steps"], 1):
                db.add(ScenarioStep(scenario_id=row.id, order=i, action=st["action"], selector={"primary": None, "fallbacks": [], "ai_hint": st.get("selector_hint")}, value=st.get("value"), expected_result=st.get("expected_result")))
            created.append(scen_dict(row, with_steps=True))
        db.commit()
        return {"demo_mode": DEMO_MODE, "created": created}
    finally: db.close()

@app.get("/api/projects/{pid}/scenarios")
def list_scenarios(pid: str):
    db = SessionLocal()
    try: return [scen_dict(s) for s in db.query(Scenario).filter_by(project_id=pid).all()]
    finally: db.close()

@app.get("/api/scenarios/{sid}")
def get_scenario(sid: str):
    db = SessionLocal()
    try:
        s = db.get(Scenario, sid)
        if not s: raise HTTPException(404)
        return scen_dict(s, with_steps=True)
    finally: db.close()

class RunCreate(BaseModel): target_type: str; target_id: str; mode: str = "script"

@app.post("/api/runs")
async def start_run(body: RunCreate):
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

@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    db = SessionLocal()
    try:
        r = db.get(Run, run_id)
        if not r: raise HTTPException(404)
        return run_dict(r)
    finally: db.close()

@app.get("/api/runs/{run_id}/video")
def run_video(run_id: str):
    db = SessionLocal()
    try:
        r = db.get(Run, run_id)
        if r and r.video_path and os.path.exists(r.video_path): return FileResponse(r.video_path, media_type="video/webm")
        raise HTTPException(404)
    finally: db.close()

@app.get("/api/runs/{run_id}/transcript")
def run_transcript(run_id: str):
    db = SessionLocal()
    try:
        r = db.get(Run, run_id)
        if not r: raise HTTPException(404)
        return {"mode": r.mode, "transcript": r.agent_transcript or []}
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
    db.close()
    q: asyncio.Queue = asyncio.Queue(maxsize=120)
    async def on_frame(data):
        if q.full():
            try: q.get_nowait()
            except asyncio.QueueEmpty: pass
        q.put_nowait({"type": "frame", "data": data})
    async def on_event(step): q.put_nowait({"type": "step", "step": step})
    session = RecorderSession(recording_id, start_url, on_frame, on_event)
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

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
