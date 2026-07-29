import os, asyncio, traceback
from datetime import datetime
from playwright.async_api import async_playwright, expect
from app.config import ARTIFACTS
from app.browser import launch_kwargs, video_ok
from app.db import SessionLocal, Run, Recording, resolve_variables, interpolate

execution_lock = asyncio.Semaphore(1)

async def locate(page, selector):
    candidates = [selector.get("primary")] + list(selector.get("fallbacks") or [])
    for sel in [c for c in candidates if c]:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=5000)
            return loc
        except: continue
    raise Exception("Element not found")

async def exec_one(page, step, variables):
    value = interpolate(step.value, variables)
    sel = step.selector or {}
    a = step.action
    if a == "navigate": await page.goto(interpolate(step.url or step.value, variables), wait_until="domcontentloaded")
    elif a == "click": await (await locate(page, sel)).click()
    elif a == "fill": await (await locate(page, sel)).fill(value or "")
    elif a == "press": await page.keyboard.press(value or "Enter")
    elif a == "assert_text": await expect(page.locator("body")).to_contain_text(value or "", timeout=5000)
    elif a == "wait": await page.wait_for_timeout(int(value or 1000))

async def exec_steps(page, steps, variables, on_event, db, depth, log, run_dir):
    if depth > 5: raise Exception("Max Nesting")
    total = len(steps)
    for idx, step in enumerate(steps, 1):
        shot_name = f"step-{idx}.jpg"
        shot_path = os.path.join(run_dir, shot_name)
        percent = int((idx / max(1, total)) * 100)
        entry = {"order": step.order, "action": step.action, "label": step.label or step.action, "status": "running", "percent": percent}
        await on_event(entry)
        try:
            await exec_one(page, step, variables)
            await page.screenshot(path=shot_path, quality=40)
            entry["status"] = "passed"
            entry["screenshot"] = f"/api/runs/screenshot/{run_dir.split('/')[-1]}/{shot_name}"
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
            log.append(entry)
            await on_event(entry)
            raise
        log.append(entry)
        await on_event(entry)

async def execute_run(run_id, on_event, on_frame=None):
    async with execution_lock:
        db = SessionLocal()
        run = db.get(Run, run_id)
        if not run: return
        run.status = "running"; db.commit()
        run_dir = os.path.join(ARTIFACTS, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        log_entries = []
        try:
            target = db.get(Recording, run.target_id)
            variables = resolve_variables(db, project_id=target.project_id)
            async with async_playwright() as p:
                browser = await p.chromium.launch(**launch_kwargs())
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                if video_ok(): 
                    await context.tracing.start(screenshots=True, snapshots=True)
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                if on_frame:
                    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 40})
                    cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(on_frame(p["data"])))
                await exec_steps(page, target.steps, variables, on_event, db, 0, log_entries, run_dir)
                run.status = "passed"
                await browser.close()
        except Exception as e:
            run.status = "failed"
        finally:
            run.log = log_entries
            db.commit(); db.close()
            await on_event({"type": "done", "status": run.status})