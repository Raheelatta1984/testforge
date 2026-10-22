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
import inspect
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright

from app import guardrails
from app.audit_trail import AuditTrail
from app.browser import Preview, explain_launch_error, launch_kwargs, trace_ok, video_ok
from app.config import ARTIFACTS, logger
from app.db import Recording, Run, SessionLocal, interpolate, resolve_variables
from app.guardrails import browser_budget, live_frames
from app.step_runtime import audit_selector, StepClock, StepRuntime, BASIC, normalize_step, interpolate as strict_interpolate
from app.library_store import playback_url

# Per-step body-text excerpt kept for the run audit and the library scenarios.
# It is the only per-step work that is not required to replay, so it is the one
# thing worth switching off when a run must be as cheap as possible.
RUN_EXCERPT = os.environ.get("TF_RUN_EXCERPT", "1").strip().lower() in {"1", "true", "yes", "on"}


# Timeouts for one replayed step. The single-run path uses these defaults; the
# batch path passes budgets learned from previous runs (see app.batch_runner), so
# a step that always lands in 120ms is not given three seconds to fail in.
DEFAULT_BUDGETS = {"navigate_ms": 20000, "action_ms": 3000, "type_delay_ms": 0}


def live_frame(run_id):
    """Last frame for a run, from the bounded store. None once it ages out."""
    return live_frames.get(run_id)


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


def _audit(trail, event, payload=None):
    """Append to the run's audit trail. An audit failure is logged, never fatal:
    the run's verdict must not depend on its own bookkeeping."""
    if trail is None:
        return
    try:
        trail.append(event, payload or {})
    except Exception as exc:
        logger.warning("AUDIT APPEND %s (%s)", event, type(exc).__name__)


def _step_audit(entry):
    """The compact, already-scrubbed projection a step leaves in the trail."""
    return {"order": entry.get("order"), "step_index": entry.get("step_index"),
            "action": entry.get("action"), "label": entry.get("label"),
            "status": entry.get("status"), "duration_ms": entry.get("duration_ms"),
            "error": entry.get("error"), "evidence": bool(entry.get("screenshot"))}


async def _click(page, selector, action_ms: int | None = None):
    """Click by selector, falling back to the recorded coordinates.

    ``action_ms`` is the learned budget for this step; the default is what a single
    run has always used.

    When the selector attempt fails and there are no coordinates to fall back to,
    the original error is reported. It used to be swallowed and replaced with "Click
    has no selector and no coordinates", which is wrong twice over: the step does
    have a selector, and the real cause (a timeout, a detached element) is what the
    operator needs in order to fix the recording.
    """
    selector = selector or {}
    timeout = int(action_ms or DEFAULT_BUDGETS["action_ms"])
    primary = selector.get("primary")
    failure = None
    if primary:
        loc = page.locator(primary)
        try:
            count = await loc.count()
            if count >= 1:
                await loc.first.click(timeout=timeout)
                return
            failure = RuntimeError(f"no element matched {primary}")
        except Exception as exc:
            failure = exc
    if selector.get("x") is not None and selector.get("y") is not None:
        await page.mouse.click(float(selector["x"]), float(selector["y"]))
        return
    if failure is not None:
        raise RuntimeError(f"Click on {primary} failed: {failure}") from failure
    raise RuntimeError("Click has no selector and no coordinates")


async def _focus(page, selector, action_ms: int | None = None):
    try:
        await _click(page, selector, action_ms)
    except Exception:
        if selector and selector.get("x") is not None:
            await page.mouse.click(float(selector["x"]), float(selector["y"]))


async def replay_step(page, step, variables, budgets=None, runtime=None):
    """Replay one recorded step.

    ``budgets`` optionally overrides the per-action timeouts. Anything missing
    falls back to :data:`DEFAULT_BUDGETS`, so callers can pass a partial dict.
    """
    action = (step.get("action") or "").lower()
    raw = step.get("value")
    value = strict_interpolate(raw, variables) if raw else None
    if (step.get("selector") or {}).get("strict") or action not in BASIC | {"text", "key", "key_press", "loop", "repeat"}:
        try:
            checked = normalize_step(step)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if budgets and "timeout_ms" not in checked["selector"]:
            key = "navigate_ms" if action == "navigate" else "action_ms"
            checked["selector"]["timeout_ms"] = max(1, min(30000, int(budgets.get(key, 3000))))
        runner = runtime or StepRuntime(page)
        return await runner.perform(checked, variables)
    selector = step.get("selector") or {}
    budget = dict(DEFAULT_BUDGETS)
    if isinstance(budgets, dict):
        budget.update({key: int(item) for key, item in budgets.items()
                       if key in DEFAULT_BUDGETS and item})
    if runtime is not None and getattr(runtime, "speed_profile", None):
        # Legacy basic steps bypass StepRuntime; the slider still slows typing.
        budget["type_delay_ms"] = int(runtime.speed_profile.get("type_delay_ms") or 0)
    navigate_ms = budget["navigate_ms"]
    action_ms = budget["action_ms"]
    type_delay_ms = budget["type_delay_ms"]

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
        # domcontentloaded, not networkidle: a replay does not need every analytics
        # request to settle, and waiting for them is the slowest part of a run.
        # If slow third-party scripts stall domcontentloaded after navigation has
        # already committed, stop the hung stream and continue rather than failing
        # with `Page.goto: Timeout ... exceeded`.
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=navigate_ms)
        except Exception as exc:
            if "Timeout" not in type(exc).__name__ and "Timeout" not in str(exc):
                raise
            current = getattr(page, "url", None) or ""
            if current and current != "about:blank":
                if hasattr(page, "evaluate"):
                    try:
                        await page.evaluate("() => { try { window.stop(); } catch (_) {} }")
                    except Exception:
                        pass
            else:
                await page.goto(url, wait_until="commit", timeout=min(max(navigate_ms, 1000), 15000))
        return
    if action == "click":
        await _click(page, selector, action_ms)
        return
    if action in ("fill", "type", "text"):
        if selector:
            await _focus(page, selector, action_ms)
        if action == "fill" and selector.get("primary"):
            await page.locator(selector["primary"]).first.fill(value or "", timeout=action_ms)
        else:
            # Fast typing, compatible with FakeKeyboard in tests
            try:
                await page.keyboard.type(value or "", delay=type_delay_ms)
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
        variables[name] = await page.locator(selector["primary"]).first.input_value(timeout=action_ms)
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
                # Bound expansion before allocating or replaying any action.
                if len(expanded) + len(block) * repeat_times + 1 > 2000:
                    raise ValueError("Execution exceeds 2,000 expanded steps")
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

        if len(expanded) + repeat > 2000:
            raise ValueError("Execution exceeds 2,000 expanded steps")
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


async def execute_run(run_id, on_event, on_frame=None, display_window=True, viewer_count=None,
                      screenshots="all", window_enabled=None, execution_speed=None):
    """Replay one recording and optionally stream a viewer-aware live preview.

    ``display_window`` is the initial setting. ``window_enabled`` may be a
    callable so the Runs tab can turn a live single run's preview on or off
    without restarting it. No screenshots are rendered while it is off or while
    nobody is watching; ``viewer_count`` reports attached clients.
    """
    # Plans have their own sequential contexts, but share the SAME global slot.
    with SessionLocal() as db:
        row = db.get(Run, run_id)
        recording = db.get(Recording, row.recording_id) if row else None
        plan = recording.execution_plan if recording else None
        project_id = recording.project_id if recording else None
        from app.secrets_store import sensitive_values
        plan_sensitive = sensitive_values(db, [project_id]) if plan else []
        variable_snapshot = row.variable_snapshot if row else None
        overrides = (row.input_overrides if row.input_overrides is not None else recording.input_defaults or {}) if recording and row else {}
    if plan:
        from app.import_drafts import environment
        from app.plan_runtime import normalize_plan, run_plan
        from app.evidence import callback
        logs = []
        desktop_step_keys = set()

        def step_key(case_number, attempt_number, step):
            return (case_number, attempt_number, step.get('stage'), step.get('step'),
                    step.get('repeat'), step.get('action'))

        def plan_preview_enabled():
            if callable(window_enabled):
                try:
                    return bool(window_enabled())
                except Exception:
                    return bool(display_window)
            return bool(display_window)

        async def desktop_step(event, screenshot_data=None):
            entry = dict(event)
            key = step_key(entry.get('case'), entry.get('attempt'), entry)
            desktop_step_keys.add(key)
            if entry.get('screenshot'):
                entry['screenshot'] = f"/api/runs/screenshot/{run_id}/{Path(entry['screenshot']).name}"
            entry = scrub_entry(entry, plan_sensitive)
            logs.append(entry)
            _save(run_id, status='running', progress_pct=entry.get('percent', 1), execution_log=list(logs))
            await on_event(entry)

            # Per-step PNG evidence is always persisted by plan_runtime. The
            # optional live feed is a small JPEG using the same step identity.
            if screenshot_data and plan_preview_enabled():
                watching = True
                if viewer_count is not None:
                    try:
                        watching = int(viewer_count()) > 0
                    except Exception:
                        watching = True
                if watching:
                    try:
                        from io import BytesIO
                        from PIL import Image
                        with Image.open(BytesIO(screenshot_data)) as opened:
                            image = opened.convert('RGB')
                            image.thumbnail((1024, 640))
                            output = BytesIO()
                            image.save(output, format='JPEG', quality=48, optimize=True)
                            frame = output.getvalue()
                        live_frames.put(run_id, frame)
                        live_path = Path(ARTIFACTS) / 'runs' / run_id / 'live.jpg'
                        live_path.parent.mkdir(parents=True, exist_ok=True)
                        live_path.write_bytes(frame)
                        if on_frame is not None:
                            metadata = {key: entry.get(key) for key in ('order', 'step_index', 'label')}
                            payload = base64.b64encode(frame).decode('ascii')
                            result = on_frame(payload, **metadata)
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        # A thumbnail failure must not cancel a plan whose PNG
                        # step evidence was safely stored.
                        pass

        async def progress(done, total, case):
            for attempt in case['attempts']:
                for step in attempt['steps']:
                    if step_key(case.get('case'), attempt.get('attempt'), step) in desktop_step_keys:
                        continue
                    entry = {**step, 'order': len(logs) + 1,
                             'label': f"Case {done}, row {case.get('row')}, "
                                      f"{case.get('browser') or case.get('runner') or case.get('platform') or 'plan'}"
                                      f"/{case.get('device') or case.get('device_model') or 'target'}, "
                                      f"attempt {attempt['attempt']}",
                             'error': step.get('error_type'),
                             'result': {'case_status': case['status']}}
                    if entry.get('screenshot'):
                        entry['screenshot'] = f"/api/runs/screenshot/{run_id}/{Path(entry['screenshot']).name}"
                    entry = scrub_entry(entry, plan_sensitive)
                    logs.append(entry)
            _audit(trail, 'case', {'case': case.get('case'), 'row': case.get('row'),
                                     'status': case.get('status'), 'browser': case.get('browser'),
                                     'runner': case.get('runner')})
            percent = int(done / total * 100)
            _save(run_id, progress_pct=percent, execution_log=list(logs))
            await on_event({'type': 'status', 'status': 'running', 'percent': percent})
        async with browser_budget.slot('plan:' + run_id):
            plan = normalize_plan(plan)
            if execution_speed is not None:
                # The run-level slider wins over the plan's stored factor.
                from app.execution_speed import normalize_speed
                factor = normalize_speed(execution_speed)
                plan['execution_speed'] = factor
                for profile in plan['matrix']:
                    profile['execution_speed'] = factor
            from app.secrets_store import reveal_map, scrub_entry
            values, _ = environment(project_id, plan, reveal_map(variable_snapshot) if variable_snapshot is not None else None)
            _save(run_id, status='running', started_at=datetime.datetime.utcnow())
            try:
                trail = AuditTrail(Path(ARTIFACTS) / 'runs' / run_id, run_id)
            except Exception:
                trail = None
                logger.warning("AUDIT OPEN %s failed", run_id)
            _audit(trail, 'run_started', {'plan': True,
                                          'cases': len(plan['dataset']) * len(plan['matrix']),
                                          'profiles': [profile.get('browser') or profile.get('runner')
                                                       for profile in plan['matrix']]})
            result = await run_plan(plan, values, Path(ARTIFACTS) / 'runs' / run_id,
                                    chromium_options=launch_kwargs(), on_progress=progress,
                                    on_step=desktop_step,
                                    input_overrides=reveal_map(overrides), evidence=callback(Path(ARTIFACTS) / 'runs' / run_id, plan_sensitive))
            _audit(trail, 'run_finished', {'status': result['status'], 'cases': result.get('total')})
            _save(run_id, status=result['status'], progress_pct=100, finished_at=datetime.datetime.utcnow(), execution_log=logs)
            await on_event({'type': 'done', 'status': result['status'], 'percent': 100})
        return
    display_window = bool(display_window)

    def window_is_enabled():
        if callable(window_enabled):
            try:
                return bool(window_enabled())
            except Exception:
                return display_window
        return display_window

    # Immediate queue display - broadcast queued status right away
    await on_event({"type": "status", "status": "queued", "percent": 0,
                    "message": "Queued, starting immediately...",
                    "display_window": window_is_enabled()})
    _save(run_id, status="queued", progress_pct=0, execution_log=[])

    # One browser slot for the whole run, shared with the recorder: two Chromium
    # processes at once is what took the hosted instance over its memory limit.
    async with browser_budget.slot(f"run:{run_id}"):
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
            from app.secrets_store import reveal_map, sensitive_values, SecretValue, scrub, scrub_entry
            variables = reveal_map(variable_snapshot) if variable_snapshot is not None else resolve_variables(db, recording.project_id)
            variables.update(reveal_map(overrides))
            sensitive = sensitive_values(db, [recording.project_id])
            sensitive.extend(v for v in variables.values() if isinstance(v, SecretValue))
            run.status = "running"
            run.progress_pct = 1
            if getattr(run, "started_at", None) is None:
                run.started_at = datetime.datetime.utcnow()
            db.commit()

        run_dir = os.path.join(ARTIFACTS, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        log_entries = []
        status = "passed"
        error_text = None
        try:
            trail = AuditTrail(run_dir, run_id)
        except Exception:
            trail = None
            logger.warning("AUDIT OPEN %s failed", run_id)
        _audit(trail, "run_started", {"steps": len(steps), "screenshots": screenshots,
                                      "window": window_is_enabled()})

        if not steps:
            status = "error"
            error_text = "This recording has no steps. Record at least one action, then run it again."
            _audit(trail, "run_finished", {"status": status, "steps": 0, "error": "no steps"})
            _save(
                run_id,
                status=status,
                rog_monitor_log=error_text,
                finished_at=datetime.datetime.utcnow(),
                execution_log=[],
            )
            await on_event({"type": "done", "status": status, "error": error_text})
            return

        frame_state = {"order": None, "step_index": None, "label": None}

        def viewers_present() -> bool:
            if not window_is_enabled():
                return False
            if viewer_count is None:
                return True
            try:
                return int(viewer_count()) > 0
            except Exception:
                return True

        async def publish(data: bytes, frame_context=None):
            # Frame callbacks can race with the switch or a websocket disconnect;
            # don't spend encode/broadcast work for a viewer that has gone away.
            if not viewers_present():
                return
            live_frames.put(run_id, data)
            try:
                with open(os.path.join(run_dir, "live.jpg"), "wb") as handle:
                    handle.write(data)
            except OSError:
                pass
            if on_frame is not None:
                payload = base64.b64encode(data).decode("ascii")
                context = frame_context if isinstance(frame_context, dict) else frame_state
                metadata = {key: context.get(key) for key in ("order", "step_index", "label")
                            if context.get(key) is not None}
                try:
                    parameters = inspect.signature(on_frame).parameters.values()
                    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
                    names = {item.name for item in parameters}
                    frame_kwargs = metadata if accepts_kwargs else {
                        key: value for key, value in metadata.items() if key in names
                    }
                except (TypeError, ValueError):
                    frame_kwargs = {}
                result = on_frame(payload, **frame_kwargs) if frame_kwargs else on_frame(payload)
                if inspect.isawaitable(result):
                    await result

        # Broadcast running immediately so the UI can show the monitor and the
        # currently selected window mode before Chromium has finished launching.
        active_window = window_is_enabled()
        await on_event({
            "type": "status",
            "status": "running",
            "percent": 1,
            "message": "Browser launching..." if active_window else "Browser launching (live window off)...",
            "display_window": active_window,
        })
        logger.info(
            "RUN START %s steps=%s (expanded from %s) window=%s",
            run_id, len(steps), len(raw_steps), window_is_enabled(),
        )

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(**launch_kwargs())
                # Smaller viewport for speed on free tier
                vw = int(os.environ.get("TF_VIEWPORT_WIDTH", "1024"))
                vh = int(os.environ.get("TF_VIEWPORT_HEIGHT", "640"))
                context_kwargs = {"viewport": {"width": vw, "height": vh}, "device_scale_factor": 1}
                # Video is evidence, not decoration: it records for every run the
                # browser can write (small size, capped by run retention) unless
                # the operator opts out with TF_ENABLE_VIDEO=0.
                if video_ok() and os.environ.get("TF_ENABLE_VIDEO", "1").strip().lower() not in {"0", "false", "no", "off"}:
                    context_kwargs["record_video_dir"] = run_dir
                    context_kwargs["record_video_size"] = {"width": min(vw, 640), "height": min(vh, 400)}
                context = await browser.new_context(**context_kwargs)
                # A Playwright trace (screenshots, DOM snapshots, sources) for the
                # whole run, kept next to the video. TF_TRACE=0 opts out.
                tracing = False
                if trace_ok() and hasattr(context, "tracing"):
                    try:
                        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                        tracing = True
                    except Exception:
                        tracing = False
                page = await context.new_page()
                # Keep the lightweight preview loop alive so an operator can
                # switch it on mid-run. Its viewer/mode gate means no screenshot
                # is rendered while the window is off or nobody is connected.
                preview = Preview(
                    page,
                    on_jpeg=publish,
                    should_capture=viewers_present,
                    frame_metadata=lambda: dict(frame_state),
                )
                preview.start()
                # Step JPEGs are still the exact post-action evidence and are
                # also pushed to the live view before the step-complete event.
                shot_tasks = set()
                video = page.video
                runtime = StepRuntime(page, run_dir, execution_speed=execution_speed)
                try:
                    total = len(steps)
                    for index, step in enumerate(steps, 1):
                        percent = int((index - 1) / total * 100)
                        clock = StepClock()
                        entry = {
                            "order": step.get("order") or index,
                            "step_index": index,
                            "action": step.get("action"),
                            "label": step.get("label") or step.get("action"),
                            "value": step.get("value"),
                            "selector": audit_selector(step.get("selector")),
                            "status": "running",
                            "percent": percent,
                            "started_at": clock.started_at,
                        }
                        # The same sanitized step identity tags every frame until
                        # the next step starts. This keeps the screen and log in
                        # the same order even when frames and step events overlap.
                        entry = scrub_entry(entry, sensitive)
                        frame_state.update(order=entry.get("order"), step_index=index,
                                           label=entry.get("label"))
                        preview.touch()
                        await on_event(entry)
                        try:
                            # Do not hold the screenshot lock across the action:
                            # the preview loop must be able to show navigation and
                            # other slow actions while Playwright is still running.
                            if (step.get("action") or "").lower() == "log":
                                entry["status"] = "passed"
                                entry["percent"] = int(index / total * 100)
                                entry.update(clock.finish())
                            else:
                                result = await replay_step(page, step, variables, runtime=runtime)
                                # Action finished: stamp before screenshot evidence so
                                # durations compare the speed setting, not encoding time.
                                entry.update(clock.finish())
                                page = runtime.page
                                preview.page = page
                                if result:
                                    entry["result"] = result
                                if screenshots == "all":
                                    try:
                                        from app.evidence import capture_once
                                        async with preview.lock:
                                            shot_name = await capture_once(page, run_dir, index, sensitive)
                                        entry['screenshot'] = f'/api/runs/screenshot/{run_id}/{shot_name}'
                                        if viewers_present():
                                            await publish((Path(run_dir) / shot_name).read_bytes(),
                                                          dict(frame_state))
                                    except Exception as evidence_error:
                                        entry['evidence_error'] = type(evidence_error).__name__
                                if RUN_EXCERPT:
                                    try:
                                        entry["excerpt"] = scrub(await page.inner_text("body"), sensitive)[:300]
                                    except Exception:
                                        pass
                                entry["status"] = "passed"
                                entry["percent"] = int(index / total * 100)
                        except Exception as exc:
                            if "finished_at" not in entry:
                                entry.update(clock.finish())
                            entry["status"] = "failed"
                            entry["error"] = scrub(str(exc), sensitive).splitlines()[0][:500]
                            if screenshots in {"all", "failure"}:
                                try:
                                    from app.evidence import capture_once
                                    async with preview.lock:
                                        shot_name = await capture_once(page, run_dir, index, sensitive)
                                    entry['screenshot'] = f'/api/runs/screenshot/{run_id}/{shot_name}'
                                    if viewers_present():
                                        await publish((Path(run_dir) / shot_name).read_bytes(),
                                                      dict(frame_state))
                                except Exception as evidence_error:
                                    entry['evidence_error'] = type(evidence_error).__name__
                            entry["percent"] = percent
                            entry = scrub_entry(entry, sensitive)
                            _audit(trail, "step", _step_audit(entry))
                            log_entries.append(entry)
                            await on_event(entry)
                            status = "failed"
                            error_text = entry["error"]
                            _save(run_id, status="failed", progress_pct=percent, execution_log=list(log_entries))
                            break
                        entry = scrub_entry(entry, sensitive)
                        _audit(trail, "step", _step_audit(entry))
                        log_entries.append(entry)
                        await on_event(entry)
                        _save(run_id, progress_pct=entry["percent"], execution_log=list(log_entries))
                finally:
                    # Evidence is already persisted; no second encode/capture pass.
                    if shot_tasks:
                        await asyncio.gather(*list(shot_tasks), return_exceptions=True)
                    await preview.stop()
                    try:
                        await runtime.close()
                    except Exception as exc:
                        logger.warning("RUN ACTION CLEANUP %s (%s)", run_id, type(exc).__name__)
                    video_path = None
                    trace_path = None
                    try:
                        # The trace must stop before its context closes; stopping
                        # without a path would discard it.
                        if tracing:
                            await context.tracing.stop(path=str(Path(run_dir) / "trace.zip"))
                            candidate = Path(run_dir) / "trace.zip"
                            if candidate.is_file() and candidate.stat().st_size > 0:
                                trace_path = str(candidate)
                    except Exception:
                        trace_path = None
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
                    if trace_path and os.path.isfile(trace_path):
                        _save(run_id, trace_path=trace_path)
        except Exception as exc:
            status = "error"
            error_text = scrub(explain_launch_error(exc), sensitive)
            logger.warning("RUN FAILED %s (%s)", run_id, type(exc).__name__)

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
        _audit(trail, "run_finished", {"status": status, "steps": len(log_entries),
                                         "elapsed": round(elapsed, 3)})
        _save(run_id, **fields)
        await on_event({
            "type": "done", "status": status, "error": error_text, "log": log_entries,
            "elapsed": elapsed, "display_window": window_is_enabled(),
        })
        logger.info("RUN DONE %s status=%s elapsed=%.1fs", run_id, status, elapsed)
