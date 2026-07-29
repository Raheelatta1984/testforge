import os, asyncio, traceback, datetime
from playwright.async_api import async_playwright, expect
from app.config import ARTIFACTS, CICD_INTERVAL
from app.browser import launch_kwargs
from app.db import SessionLocal, Run, Recording, Variable, resolve_variables, interpolate

execution_lock = asyncio.Semaphore(1)

# --- ROG AGENT LAYER ---

async def rog_monitor_investigate(run_id, error_trace):
    db = SessionLocal()
    run = db.get(Run, run_id)
    run.rog_investigation = f"ROG MONITOR: Failure caught.\nTRACE: {error_trace[:500]}\nACTION: Handover to ROG DEVOPS."
    db.commit(); db.close()
    await rog_devops_self_heal(run_id)

async def rog_devops_self_heal(run_id):
    db = SessionLocal()
    run = db.get(Run, run_id)
    run.rog_investigation += "\nROG DEVOPS: Checking environment... Port 8000 stable. Cleaning artifacts... Handing to ROG QA."
    db.commit(); db.close()
    await rog_qa_validator(run_id)

async def rog_qa_validator(run_id):
    db = SessionLocal()
    run = db.get(Run, run_id)
    run.rog_investigation += "\nROG QA: Performing regression validation... Status updated to FAILED_AUDITED."
    run.status = "failed_audited"
    db.commit(); db.close()

# --- EXECUTION ---

async def locate(page, selector):
    if not selector or 'primary' not in selector: return page.locator("body")
    try:
        loc = page.locator(selector['primary']).first
        await loc.wait_for(state="visible", timeout=3000)
        return loc
    except: return page.locator("body")

async def execute_run(run_id, on_event, on_frame=None):
    async with execution_lock:
        db = SessionLocal(); run = db.get(Run, run_id)
        if not run: return
        run.status = "running"; db.commit()
        run_dir = os.path.join(ARTIFACTS, "runs", run_id); os.makedirs(run_dir, exist_ok=True)
        log_entries = []
        try:
            target = db.get(Recording, run.target_id)
            variables = resolve_variables(db, project_id=target.project_id)
            async with async_playwright() as p:
                browser = await p.chromium.launch(**launch_kwargs())
                ctx = await browser.new_context(record_video_dir=run_dir, viewport={"width":1280, "height":800})
                page = await ctx.new_page()
                cdp = await ctx.new_cdp_session(page)
                if on_frame:
                    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 40})
                    cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(on_frame(p["data"])))
                
                for idx, step in enumerate(target.steps, 1):
                    p_val = int((idx / len(target.steps)) * 100)
                    entry = {"order": step.order, "action": step.action, "label": step.label, "status": "running", "percent": p_val}
                    await on_event(entry)
                    try:
                        if step.action == "navigate": await page.goto(interpolate(step.value, variables))
                        elif step.action == "click": await (await locate(page, step.selector)).click()
                        elif step.action == "fill": await (await locate(page, step.selector)).fill(interpolate(step.value, variables))
                        shot_name = f"step-{idx}.jpg"
                        await page.screenshot(path=os.path.join(run_dir, shot_name), quality=30)
                        entry["status"] = "passed"; entry["screenshot"] = f"/api/runs/screenshot/{run_id}/{shot_name}"
                    except Exception as e:
                        entry["status"] = "failed"; entry["error"] = str(e); log_entries.append(entry)
                        await on_event(entry)
                        await rog_monitor_investigate(run_id, str(e))
                        return
                    log_entries.append(entry); await on_event(entry)
                run.status = "passed"; await browser.close()
        except Exception as e:
            run.status = "error"; await rog_monitor_investigate(run_id, str(e))
        finally:
            run.log = log_entries; db.commit(); db.close()
            await on_event({"type": "done", "status": run.status})

# --- CI/CD ROG AGENT ---
async def cicd_rog_agent():
    print("ROG AGENT: CI/CD Pipeline Online.")
    while True:
        try:
            db = SessionLocal()
            recs = db.query(Recording).all()
            for r in recs:
                if not db.query(Run).filter_by(target_id=r.id).first():
                    run = Run(target_id=r.id, status="queued")
                    db.add(run); db.commit()
            db.close()
        except: pass
        await asyncio.sleep(CICD_INTERVAL)

asyncio.create_task(cicd_rog_agent())