import os, asyncio, traceback
from datetime import datetime
from playwright.async_api import async_playwright, expect
from app.config import ARTIFACTS
from app.browser import launch_kwargs, video_ok
from app.db import SessionLocal, Run, Recording, Scenario, resolve_variables, interpolate

# Semaphore ensures we don't crash Render's memory by running too many browsers
execution_lock = asyncio.Semaphore(1)

async def locate(page, selector):
    candidates = [selector.get("primary")] + list(selector.get("fallbacks") or [])
    for sel in [c for c in candidates if c]:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=5000)
            return loc
        except: continue
    raise Exception(f"Element not found")

async def execute_run(run_id, on_event, on_frame=None):
    async with execution_lock:
        db = SessionLocal()
        run = db.get(Run, run_id)
        if not run: return
        
        run.status = "running"
        db.commit()
        
        run_dir = f"{ARTIFACTS}/runs/{run_id}"
        os.makedirs(run_dir, exist_ok=True)
        log_entries = []

        try:
            target = db.get(Recording, run.target_id) if run.target_type == "recording" else db.get(Scenario, run.target_id)
            steps = target.steps
            variables = resolve_variables(db, project_id=target.project_id)

            async with async_playwright() as p:
                browser = await p.chromium.launch(**launch_kwargs())
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()

                cdp = await context.new_cdp_session(page)
                if on_frame:
                    cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(on_frame(p["data"])))
                    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 50})

                for idx, step in enumerate(steps, 1):
                    entry = {"order": step.order, "action": step.action, "label": step.label or step.action, "status": "running"}
                    await on_event({"type": "step", **entry, "percent": int((idx/len(steps))*100)})
                    
                    try:
                        # Logic for actions
                        if step.action == "navigate": await page.goto(interpolate(step.url or step.value, variables))
                        elif step.action == "click": (await locate(page, step.selector)).click()
                        elif step.action == "fill": (await locate(page, step.selector)).fill(interpolate(step.value, variables))
                        
                        shot_name = f"step-{idx}.jpg"
                        await page.screenshot(path=f"{run_dir}/{shot_name}")
                        entry["status"] = "passed"
                        entry["screenshot"] = f"/api/runs/screenshot/{run_id}/{shot_name}"
                    except Exception as e:
                        entry["status"] = "failed"
                        entry["error"] = str(e)
                        log_entries.append(entry)
                        break
                    
                    log_entries.append(entry)
                    await on_event({"type": "step", **entry})

                run.status = "passed" if all(s["status"]=="passed" for s in log_entries) else "failed"
                await browser.close()
        except Exception as e:
            run.status = "error"
            run.error = str(traceback.format_exc())
        finally:
            run.log = log_entries
            run.finished_at = datetime.utcnow()
            db.commit()
            db.close()
            await on_event({"type": "done", "status": run.status})