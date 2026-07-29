import os, asyncio, traceback
from datetime import datetime
from playwright.async_api import async_playwright, expect
from app.config import ARTIFACTS
from app.browser import launch_kwargs, video_ok
from app.db import SessionLocal, Run, Recording, resolve_variables, interpolate

execution_lock = asyncio.Semaphore(1)

async def locate(page, selector):
    if not selector or 'primary' not in selector: return page.locator("body")
    candidates = [selector.get("primary")] + list(selector.get("fallbacks") or [])
    for sel in [c for c in candidates if c]:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=3000)
            return loc
        except: continue
    return page.locator("body")

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
            steps = target.steps
            variables = resolve_variables(db, project_id=target.project_id)
            async with async_playwright() as p:
                browser = await p.chromium.launch(**launch_kwargs())
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                if on_frame:
                    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 40})
                    cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(on_frame(p["data"])))

                for idx, step in enumerate(steps, 1):
                    percent = int((idx / len(steps)) * 100)
                    entry = {"order": step.order, "action": step.action, "label": step.label, "status": "running", "percent": percent}
                    await on_event(entry)
                    
                    try:
                        if step.action == "navigate": await page.goto(interpolate(step.url or step.value, variables))
                        elif step.action == "click": 
                            loc = await locate(page, step.selector)
                            await loc.click()
                        elif step.action == "fill":
                            loc = await locate(page, step.selector)
                            await loc.fill(interpolate(step.value, variables))
                        
                        shot_name = f"step-{idx}.jpg"
                        await page.screenshot(path=os.path.join(run_dir, shot_name), quality=30)
                        entry["status"] = "passed"
                        entry["screenshot"] = f"/api/runs/screenshot/{run_id}/{shot_name}"
                    except Exception as e:
                        entry["status"] = "failed"
                        entry["error"] = str(e)
                    
                    log_entries.append(entry)
                    await on_event(entry)

                run.status = "passed" if all(s["status"]=="passed" for s in log_entries) else "failed"
                await browser.close()
        except Exception as e:
            run.status = "error"
        finally:
            run.log = log_entries
            db.commit(); db.close()
            await on_event({"type": "done", "status": run.status})