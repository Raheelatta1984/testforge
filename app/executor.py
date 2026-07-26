import os
from datetime import datetime
from playwright.async_api import async_playwright, expect

from app.config import ARTIFACTS
from app.browser import launch_kwargs, video_ok
from app.db import (SessionLocal, Run, Recording, Scenario,
                 resolve_variables, interpolate)

class StepFailure(Exception):
    pass

async def locate(page, selector):
    candidates = [selector.get("primary")] + list(selector.get("fallbacks") or [])
    last_err = None
    for sel in [c for c in candidates if c]:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=3000)
            return loc
        except Exception as e:
            last_err = e
    raise StepFailure(f"No selector matched {candidates}: {last_err}")

async def exec_one(page, step, variables):
    value = interpolate(step.value, variables)
    sel = step.selector or {}
    a = step.action
    if a == "navigate":
        await page.goto(interpolate(step.url, variables) or value or "about:blank",
                        wait_until="domcontentloaded")
    elif a == "click":
        await (await locate(page, sel)).click()
    elif a == "dblclick":
        await (await locate(page, sel)).dblclick()
    elif a == "fill":
        await (await locate(page, sel)).fill(value or "")
    elif a == "select":
        await (await locate(page, sel)).select_option(value)
    elif a == "press":
        await page.keyboard.press(value or "Enter")
    elif a == "copy":
        await page.keyboard.press("Control+C")
    elif a == "cut":
        await page.keyboard.press("Control+X")
    elif a == "paste":
        await (await locate(page, sel)).fill(value or "")
    elif a == "assert_text":
        await expect(page.locator("body")).to_contain_text(value or "", timeout=5000)
    elif a == "assert_visible":
        await expect(await locate(page, sel)).to_be_visible()
    elif a == "wait":
        await page.wait_for_timeout(int(value or 1000))
    elif a == "scroll":
        await page.mouse.wheel(0, int(value or 400))

async def exec_steps(page, steps, variables, on_event, db, depth, log, run_dir):
    if depth > 5:
        raise StepFailure("Recording nesting too deep (possible loop)")
    for idx, step in enumerate(steps, 1):
        shot_path = f"{run_dir}/step-{idx}.jpg"
        entry = {"order": step.order, "action": step.action,
                 "label": step.label or step.action, "status": "running", "screenshot": None}
        await on_event({"type": "step", **entry})
        try:
            if step.action == "call_recording":
                sub = db.get(Recording, step.ref_recording_id)
                if not sub:
                    raise StepFailure("Referenced recording not found")
                sub_vars = {**variables,
                            **resolve_variables(db, recording_id=sub.id),
                            **(step.variable_overrides or {})}
                await exec_steps(page, sub.steps, sub_vars, on_event, db, depth + 1, log, run_dir)
            else:
                await exec_one(page, step, variables)
            
            try:
                await page.screenshot(path=shot_path, type="jpeg", quality=65)
                entry["screenshot"] = f"/api/runs/screenshot/{run_dir.split('/')[-1]}/step-{idx}.jpg"
            except Exception:
                pass

            entry["status"] = "passed"
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = str(e)[:500]
            try:
                await page.screenshot(path=shot_path, type="jpeg", quality=65)
                entry["screenshot"] = f"/api/runs/screenshot/{run_dir.split('/')[-1]}/step-{idx}.jpg"
            except Exception:
                pass
            log.append(entry)
            await on_event({"type": "step", **entry})
            raise
        log.append(entry)
        await on_event({"type": "step", **entry})

async def execute_run(run_id, on_event):
    db = SessionLocal()
    run = db.get(Run, run_id)
    run.status = "running"; db.commit()
    run_dir = f"{ARTIFACTS}/runs/{run_id}"
    os.makedirs(run_dir, exist_ok=True)
    log_entries = []
    try:
        if run.target_type == "recording":
            target = db.get(Recording, run.target_id)
            steps, project_id, rec_id = target.steps, target.project_id, target.id
        else:
            target = db.get(Scenario, run.target_id)
            steps, project_id, rec_id = target.steps, target.project_id, None
        variables = resolve_variables(db, project_id=project_id, recording_id=rec_id)

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs())
            ctx_args = {"viewport": {"width": 1280, "height": 800},
                        "permissions": ["clipboard-read", "clipboard-write"]}
            if video_ok():
                ctx_args["record_video_dir"] = run_dir
                ctx_args["record_video_size"] = {"width": 1280, "height": 800}
            ctx = await browser.new_context(**ctx_args)
            page = await ctx.new_page()
            try:
                await exec_steps(page, steps, variables, on_event, db, 0, log_entries, run_dir)
                run.status = "passed"
            except Exception:
                run.status = "failed"
            video = page.video if video_ok() else None
            await ctx.close()
            if video:
                try: 
                    run.video_path = await video.path()
                except Exception: 
                    run.video_path = None
            await browser.close()
    except Exception as e:
        run.status = "error"; run.error = str(e)[:1000]
    finally:
        run.log = log_entries
        run.finished_at = datetime.utcnow()
        db.commit()
        await on_event({"type": "done", "status": run.status,
                        "log": log_entries, "has_video": bool(run.video_path)})
        db.close()