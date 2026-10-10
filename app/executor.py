"""Replay a saved recording in a fresh browser and stream the picture back.

Optimized for:
- Immediate queue processing and display
- Fast execution on free tier (lower viewport, no video by default for speed)
- Handling repeat/loop steps (deduplicated recordings)
- Smooth live preview, and no preview at all when the window is switched off
- No frame is rendered unless somebody is watching it
"""

import asyncio
import base64
import datetime
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

from app.browser import Preview, explain_launch_error, launch_kwargs, video_ok
from app.config import ARTIFACTS, logger
from app.db import Recording, Run, SessionLocal, interpolate, resolve_variables
from app.library_store import playback_url

execution_lock = asyncio.Semaphore(1)
LIVE_FRAMES = {}
RUN_QUEUE = []  # for immediate display
RUN_QUEUE_LOCK = asyncio.Lock()

# Per-step body-text excerpt kept for the run audit and the library scenarios.
# It is the only per-step work that is not required to replay, so it is the one
# thing worth switching off when a run must be as cheap as possible.
RUN_EXCERPT = os.environ.get("TF_RUN_EXCERPT", "1").strip().lower() in {"1", "true", "yes", "on"}


async def _persist_shot(directory, name, data):
    """Palette-compress and write a step screenshot off the replay path."""
    try:
        from app.images import compact_png

        optimized = await asyncio.to_thread(compact_png, data)
        await asyncio.to_thread(Path(os.path.join(directory, name)).write_bytes, optimized)
    except Exception as exc:
        logger.warning("RUN SCREENSHOT FAILED %s %s", name, exc)


def live_frame(run_id):
    return LIVE_FRAMES.get(run_id)


def _step_dict(step):
    return {
        "order": step.order,
        "action": step.action,
        "label": step.label,
        "value": step.value,
        "selector": step.selector if isinstance(step.selector, dict) else None,
        "repeat": getattr(step, "repeat_count", 1) or 1,
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
                await loc.first.click(timeout=3000)  # faster timeout
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

    # Handle repeat metadata
    repeat = step.get("repeat") or 1
    if isinstance(repeat, dict):
        repeat = repeat.get("times") or repeat.get("count") or 1

    # Loop action is handled at higher level, but support here as no-op if encountered alone
    if action in ("loop", "repeat"):
        # This is a synthetic repeat marker - actual repetition handled in execute_run loop
        return

    if action == "navigate":
        url = playback_url(value or "")
        if not url:
            raise RuntimeError("Navigate step has no URL")
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)  # faster timeout
        return
    if action == "click":
        await _click(page, selector)
        return
    if action in ("fill", "type", "text"):
        if selector:
            await _focus(page, selector)
        if action == "fill" and selector.get("primary"):
            await page.locator(selector["primary"]).first.fill(value or "", timeout=3000)
        else:
            # Fast typing, compatible with FakeKeyboard in tests
            try:
                await page.keyboard.type(value or "", delay=5)
            except TypeError:
                await page.keyboard.type(value or "")
        return
    if action in ("press", "key", "key_press"):
        await page.keyboard.press(value or selector.get("key") or "Enter")
        return
    if action == "save_variable":
        name = raw
        if not name or not selector.get("primary"):
            raise RuntimeError("Save variable needs a name and input selector")
        variables[name] = await page.locator(selector["primary"]).first.input_value(timeout=3000)
        return
    raise RuntimeError(f"Unsupported action: {action}")


async def _expand_steps_with_repeat(steps):
    """Expand steps handling repeat and loop markers for execution.

    Returns flat list of steps to execute, with repeat info resolved.
    For block repeats (loop action), repeats previous k steps.
    """
    expanded = []
    i = 0
    while i < len(steps):
        step = steps[i]
        action = (step.get("action") or "").lower()

        if action in ("loop", "repeat") and step.get("selector"):
            # Block repeat
            sel = step.get("selector") or {}
            block_size = sel.get("repeat_block") or sel.get("block_size") or 0
            repeat_times = sel.get("repeat_times") or sel.get("times") or 0
            if block_size > 0 and repeat_times > 0:
                # Find last block_size steps from expanded (not including loop markers)
                # We need to get the last block_size original steps
                # For simplicity, use steps history before this loop marker
                # Look back in original steps list for last block_size non-loop steps
                block = []
                j = i - 1
                while len(block) < block_size and j >= 0:
                    prev = steps[j]
                    if (prev.get("action") or "").lower() not in ("loop", "repeat"):
                        block.insert(0, prev)
                    j -= 1
                # Repeat block
                for _ in range(repeat_times):
                    expanded.extend(block)
                # Log that we repeated
                expanded.append({
                    "order": step.get("order"),
                    "action": "log",
                    "label": f"🔁 Repeated last {block_size} steps ×{repeat_times}",
                    "value": None,
                    "selector": None,
                    "repeat": 1,
                })
            i += 1
            continue

        # Normal step with repeat count
        repeat = step.get("repeat") or 1
        if isinstance(repeat, dict):
            repeat = repeat.get("times") or 1
        try:
            repeat = int(repeat)
        except:
            repeat = 1
        repeat = max(1, min(repeat, 100))  # cap at 100 to avoid abuse

        for r in range(repeat):
            # For repeated identical steps, keep same step but add repeat index to label if >1
            if repeat > 1 and r > 0:
                s = dict(step)
                s["label"] = f"{step.get('label') or step.get('action')} ({r+1}/{repeat})"
                expanded.append(s)
            else:
                expanded.append(step)
        i += 1
    return expanded


async def execute_run(run_id, on_event, on_frame=None, display_window=True, viewer_count=None):
    """Replay one recording.

    `display_window=False` skips the live screencast entirely: no preview loop,
    no frames, no per-frame CPU. `viewer_count` is a callable returning how many
    clients are watching; while it reports zero the preview renders nothing.
    """
    display_window = bool(display_window)
    # Immediate queue display - broadcast queued status right away
    await on_event({"type": "status", "status": "queued", "percent": 0, "message": "Queued, starting immediately..."})
    _save(run_id, status="queued", progress_pct=0, execution_log=[])

    async with execution_lock:
        start_time = time.time()
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
            raw_steps = [_step_dict(step) for step in recording.steps]
            # Expand repeats for execution
            steps = await _expand_steps_with_repeat(raw_steps)
            start_url = recording.start_url
            variables = resolve_variables(db, recording.project_id)
            run.status = "running"
            run.progress_pct = 1
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

        def viewers_present() -> bool:
            if not display_window:
                return False
            if viewer_count is None:
                return True
            try:
                return int(viewer_count()) > 0
            except Exception:
                return True

        # Broadcast running immediately so UI shows window
        await on_event({
            "type": "status",
            "status": "running",
            "percent": 1,
            "message": "Browser launching..." if display_window else "Browser launching (live window off)...",
            "display_window": display_window,
        })
        logger.info(
            "RUN START %s steps=%s (expanded from %s) window=%s",
            run_id, len(steps), len(raw_steps), display_window,
        )

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(**launch_kwargs())
                # Smaller viewport for speed on free tier
                vw = int(os.environ.get("TF_VIEWPORT_WIDTH", "1024"))
                vh = int(os.environ.get("TF_VIEWPORT_HEIGHT", "640"))
                context_kwargs = {"viewport": {"width": vw, "height": vh}, "device_scale_factor": 1}
                # Video disabled for speed unless explicitly enabled (saves CPU)
                if video_ok() and os.environ.get("TF_ENABLE_VIDEO", "0") == "1":
                    context_kwargs["record_video_dir"] = run_dir
                    context_kwargs["record_video_size"] = {"width": min(vw, 640), "height": min(vh, 400)}
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()
                # The Preview always exists so replay and capture share one lock;
                # its loop only runs when the live window is switched on.
                preview = Preview(
                    page,
                    on_jpeg=publish if display_window else None,
                    should_capture=viewers_present,
                )
                if display_window:
                    preview.start()
                shot_tasks = set()
                video = page.video
                try:
                    total = len(steps)
                    for index, step in enumerate(steps, 1):
                        percent = int((index - 1) / total * 100)
                        entry = {
                            "order": step.get("order") or index,
                            "action": step.get("action"),
                            "label": step.get("label") or step.get("action"),
                            "value": step.get("value"),
                            "selector": step.get("selector"),
                            "status": "running",
                            "percent": percent,
                        }
                        # Immediate broadcast for queue display
                        await on_event(entry)
                        try:
                            async with preview.lock:
                                # Skip log actions
                                if (step.get("action") or "").lower() == "log":
                                    entry["status"] = "passed"
                                    entry["percent"] = int(index / total * 100)
                                else:
                                    await replay_step(page, step, variables)
                                    shot_name = f"step-{index}.png"
                                    try:
                                        # The capture must happen now, while the page
                                        # still shows this step's result. Encoding and
                                        # writing it does not, so they run detached.
                                        png = await page.screenshot(type="png", animations="disabled")
                                        task = asyncio.create_task(_persist_shot(run_dir, shot_name, png))
                                        shot_tasks.add(task)
                                        task.add_done_callback(shot_tasks.discard)
                                        entry["screenshot"] = f"/api/runs/screenshot/{run_id}/{shot_name}"
                                    except Exception:
                                        pass
                                    if RUN_EXCERPT:
                                        try:
                                            entry["excerpt"] = (await page.inner_text("body"))[:300]
                                        except Exception:
                                            pass
                                    entry["status"] = "passed"
                                    entry["percent"] = int(index / total * 100)
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
                        _save(run_id, progress_pct=entry["percent"], execution_log=list(log_entries))
                finally:
                    # Screenshots still being written must land before the browser
                    # closes, or the audit shows links to files that do not exist.
                    if shot_tasks:
                        await asyncio.gather(*list(shot_tasks), return_exceptions=True)
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

        elapsed = time.time() - start_time
        fields = {
            "status": status,
            "execution_log": log_entries,
            "finished_at": datetime.datetime.utcnow(),
            "progress_pct": 100 if status == "passed" else None,
        }
        if fields["progress_pct"] is None:
            fields.pop("progress_pct")
        if error_text:
            fields["rog_monitor_log"] = f"Execution failed: {error_text[:800]}"
            fields["rog_devops_log"] = f"Browser session closed after {elapsed:.1f}s. The run was not retried automatically."
            fields["rog_qa_log"] = "QA: marked failed. Open the step log and screenshots."
        else:
            fields["rog_qa_log"] = f"QA: every recorded step completed in {elapsed:.1f}s."
        _save(run_id, **fields)
        await on_event({
            "type": "done", "status": status, "error": error_text, "log": log_entries,
            "elapsed": elapsed, "display_window": display_window,
        })
        logger.info("RUN DONE %s status=%s elapsed=%.1fs", run_id, status, elapsed)
