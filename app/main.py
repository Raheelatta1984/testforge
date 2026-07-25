import os

# Auto-create static directory & fallback index if missing (prevents startup crashes)
os.makedirs("app/static", exist_ok=True)
index_path = "app/static/index.html"
if not os.path.exists(index_path):
    with open(index_path, "w") as f:
        f.write("""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>TestForge</title></head>
<body style="background:#020617; color:#f8fafc; font-family:sans-serif; text-align:center; padding-top:50px;">
  <h1>⚒️ TestForge is booting up...</h1>
  <p>If you see this, app/static/index.html was missing from GitHub. Please commit the full UI file.</p>
</body>
</html>""")

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")lue": {"type": "string"}, "expected_result": {"type": "string"}},
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
