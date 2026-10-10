"""Replay a saved recording in a fresh browser and stream the picture back.

Run rows are keyed by `recording_id`. Progress lives in `progress_pct` and the
step log in `execution_log`. Those names match the models — an older revision
of this file wrote `target_id` / `log` / `rog_investigation`, which do not
exist, so every run died before a browser opened.
"""

import asyncio
import base64
import datetime
import os

from playwright.async_api import async_playwright

from app.browser import Preview, explain_launch_error, launch_kwargs, video_ok
from app.config import ARTIFACTS, logger
from app.db import Recording, Run, SessionLocal, interpolate, resolve_variables
from app.library_store import playback_url

execution_lock = asyncio.Semaphore(1)
LIVE_FRAMES = {}


def live_frame(run_id):
    return LIVE_FRAMES.get(run_id)


def _step_dict(step):
    return {
        "order": step.order,
        "action": step.action,
        "label": step.label,
        "value": step.value,
        "selector": step.selector if isinstance(step.selector, dict) else None,
    }


def _save(run_id, **fields):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        db.commit()


async def _click(page, selector):
    selector = selector or {}
    primary = selector.get("primary")
    if primary:
        loc = page.locator(primary)
        try:
            count = await loc.count()
            if count >= 1:
                await loc.first.click(timeout=4000)
                return
        except Exception:
            pass
    if selector.get("x") is not None and selector.get("y") is not None:
        await page.mouse.click(float(selector["x"]), float(selector["y"]))
        return
    raise RuntimeError("Click has no selector and no coordinates")


async def _focus(page, selector):
    try:
        await _click(page, selector)
    except Exception:
        if selector and selector.get("x") is not None:
            await page.mouse.click(float(selector["x"]), float(selector["y"]))


async def replay_step(page, step, variables):
    action = (step.get("action") or "").lower()
    raw = step.get("value")
    value = interpolate(raw, variables) if raw else None
    selector = step.get("selector") or {}
    if action == "navigate":
        url = playback_url(value or "")
        if not url:
            raise RuntimeError("Navigate step has no URL")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return
    if action == "click":
        await _click(page, selector)
        return
    if action in ("fill", "type", "text"):
        if selector:
            await _focus(page, selector)
        if action == "fill" and selector.get("primary"):
            await page.locator(selector["primary"]).first.fill(value or "", timeout=4000)
        else:
            await page.keyboard.type(value or "")
        return
    if action in ("press", "key", "key_press"):
        await page.keyboard.press(value or selector.get("key") or "Enter")
        return
    raise RuntimeError(f"Unsupported action: {action}")


async def execute_run(run_id, on_event, on_frame=None):
    async with execution_lock:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                return
            recording = db.get(Recording, run.recording_id)
            if recording is None:
                run.status = "error"
                run.rog_monitor_log = "Recording not found"
                run.finished_at = datetime.datetime.utcnow()
                db.commit()
                await on_event({"type": "done", "status": "error", "error": "Recording not found"})
                return
            steps = [_step_dict(step) for step in recording.steps]
            start_url = recording.start_url
            variables = resolve_variables(db, recording.project_id)
            run.status = "running"
            run.progress_pct = 0
            db.commit()

        run_dir = os.path.join(ARTIFACTS, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        log_entries = []
        status = "passed"
        error_text = None

        if not steps:
            status = "error"
            error_text = "This recording has no steps. Record at least one action, then run it again."
            _save(
                run_id,
                status=status,
                rog_monitor_log=error_text,
                finished_at=datetime.datetime.utcnow(),
                execution_log=[],
            )
            await on_event({"type": "done", "status": status, "error": error_text})
            return

        def publish(data: bytes):
            LIVE_FRAMES[run_id] = data
            try:
                with open(os.path.join(run_dir, "live.jpg"), "wb") as handle:
                    handle.write(data)
            except OSError:
                pass
            if on_frame is not None:
                payload = base64.b64encode(data).decode("ascii")
                result = on_frame(payload)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(**launch_kwargs())
                context_kwargs = {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 1}
                if video_ok():
                    context_kwargs["record_video_dir"] = run_dir
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()
                preview = Preview(page, on_jpeg=publish).start()
                video = page.video
                try:
                    total = len(steps)
                    for index, step in enumerate(steps, 1):
                        percent = int(index / total * 100)
                        entry = {
                            "order": step.get("order") or index,
                            "action": step.get("action"),
                            "label": step.get("label") or step.get("action"),
                            "status": "running",
                            "percent": percent,
                        }
                        await on_event(entry)
                        try:
                            async with preview.lock:
                                await replay_step(page, step, variables)
                                shot_name = f"step-{index}.jpg"
                                shot_path = os.path.join(run_dir, shot_name)
                                try:
                                    await page.screenshot(path=shot_path, type="jpeg", quality=45)
                                    entry["screenshot"] = f"/api/runs/screenshot/{run_id}/{shot_name}"
                                except Exception:
                                    pass
                                try:
                                    entry["excerpt"] = (await page.inner_text("body"))[:400]
                                except Exception:
                                    pass
                            entry["status"] = "passed"
                            entry["percent"] = percent
                        except Exception as exc:
                            entry["status"] = "failed"
                            entry["error"] = str(exc).splitlines()[0][:500]
                            entry["percent"] = percent
                            log_entries.append(entry)
                            await on_event(entry)
                            status = "failed"
                            error_text = entry["error"]
                            _save(run_id, status="failed", progress_pct=percent, execution_log=list(log_entries))
                            break
                        log_entries.append(entry)
                        await on_event(entry)
                        _save(run_id, progress_pct=percent, execution_log=list(log_entries))
                finally:
                    await preview.stop()
                    video_path = None
                    try:
                        await context.close()
                        if video is not None:
                            video_path = await video.path()
                    except Exception:
                        video_path = None
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    if video_path and os.path.isfile(video_path):
                        _save(run_id, video_path=video_path)
        except Exception as exc:
            status = "error"
            error_text = explain_launch_error(exc)
            logger.exception("RUN FAILED %s", run_id)

        fields = {
            "status": status,
            "execution_log": log_entries,
            "finished_at": datetime.datetime.utcnow(),
            "progress_pct": 100 if status == "passed" else None,
        }
        # Don't wipe a progress value already stored on failure.
        if fields["progress_pct"] is None:
            fields.pop("progress_pct")
        if error_text:
            fields["rog_monitor_log"] = f"Execution failed: {error_text[:800]}"
            fields["rog_devops_log"] = "Browser session closed. The run was not retried automatically."
            fields["rog_qa_log"] = "QA: marked failed. Open the step log and screenshots."
        else:
            fields["rog_qa_log"] = "QA: every recorded step completed."
        _save(run_id, **fields)
        await on_event({"type": "done", "status": status, "error": error_text, "log": log_entries})
