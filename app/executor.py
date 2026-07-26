import os, asyncio, traceback
from datetime import datetime
from playwright.async_api import async_playwright
from app.config import ARTIFACTS
from app.browser import launch_kwargs
from app.db import SessionLocal, Run, Recording, resolve_variables, interpolate

execution_lock = asyncio.Semaphore(1)

async def execute_run(run_id, on_event, on_frame=None):
    async with execution_lock:
        # Open fresh session for background task
        db = SessionLocal()
        try:
            run = db.get(Run, run_id)
            if not run: return
            
            run.status = "running"
            db.commit()
            
            run_dir = f"{ARTIFACTS}/runs/{run_id}"
            os.makedirs(run_dir, exist_ok=True)
            log_entries = []

            target = db.get(Recording, run.target_id)
            steps = target.steps
            variables = resolve_variables(db, project_id=target.project_id)

            async with async_playwright() as p:
                # Desktop Support: We launch with NO-HEADLESS to use the Xvfb desktop
                browser = await p.chromium.launch(headless=False, args=["--no-sandbox"])
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()

                cdp = await context.new_cdp_session(page)
                if on_frame:
                    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 40})
                    cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(on_frame(p["data"])))

                for idx, step in enumerate(steps, 1):
                    entry = {"order": step.order, "action": step.action, "label": step.label or step.action, "status": "running"}
                    await on_event({"type": "step", **entry, "percent": int((idx/len(steps))*100)})
                    
                    try:
                        # Execution Logic
                        if step.action == "navigate": await page.goto(interpolate(step.url or step.value, variables))
                        elif step.action == "click": await page.locator(step.selector['primary']).click()
                        elif step.action == "fill": await page.locator(step.selector['primary']).fill(interpolate(step.value, variables))
                        
                        shot_name = f"step-{idx}.jpg"
                        await page.screenshot(path=f"{run_dir}/{shot_name}", quality=30)
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
                run.log = log_entries
                db.commit()
                await browser.close()
        except Exception:
            run.status = "error"
            run.error = str(traceback.format_exc())
            db.commit()
        finally:
            await on_event({"type": "done", "status": run.status})
            db.close()