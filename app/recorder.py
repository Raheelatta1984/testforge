"""Remote-browser recorder optimized for speed and smooth saving.

The browser runs on the server. The dashboard shows `session.latest_jpeg` and
posts clicks / keystrokes back. Every accepted action is stored as a
RecordingStep that the executor can replay.

Optimizations:
- Fast viewport (1024x640) for lower CPU on free tier, configurable
- Adaptive preview with touch() for instant feedback
- Debounced export to avoid heavy I/O on every step
- Consecutive identical actions collapse into one step carrying a repeat count
- Save never raises: the browser always closes and the recording always persists
"""

import asyncio
import os
import re
import time

from playwright.async_api import async_playwright

from app.browser import Preview, explain_launch_error, launch_kwargs
from app.config import ARTIFACTS, logger
from app.guardrails import browser_budget
from app.db import RecordingStep, SessionLocal, resolve_variables, interpolate, Variable, VAR_REGEX
from app import library_store

# Element under the click, plus a CSS selector stable enough to replay.
_ELEMENT_JS = """
([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el || el.nodeType !== 1) return null;
  const cssEscape = (window.CSS && CSS.escape)
    ? (value) => CSS.escape(value)
    : (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
  const attr = (node, name) => {
    const value = node.getAttribute(name);
    return value ? String(value).slice(0, 120) : "";
  };
  function selectorFor(node) {
    if (!node || node.nodeType !== 1) return null;
    if (node.id) return "#" + cssEscape(node.id);
    const testid = attr(node, "data-testid");
    if (testid) return '[data-testid="' + cssEscape(testid) + '"]';
    const name = attr(node, "name");
    if (name) return node.tagName.toLowerCase() + '[name="' + cssEscape(name) + '"]';
    const aria = attr(node, "aria-label");
    if (aria) return node.tagName.toLowerCase() + '[aria-label="' + aria.replace(/"/g, '\\\\"') + '"]';
    const placeholder = attr(node, "placeholder");
    if (placeholder) return node.tagName.toLowerCase() + '[placeholder="' + placeholder.replace(/"/g, '\\\\"') + '"]';
    const parts = [];
    let cur = node;
    while (cur && cur.nodeType === 1 && parts.length < 6 && cur.tagName !== "HTML") {
      if (cur.id) { parts.unshift("#" + cssEscape(cur.id)); break; }
      let index = 1;
      let sib = cur;
      while ((sib = sib.previousElementSibling)) {
        if (sib.tagName === cur.tagName) index += 1;
      }
      parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  }
  const text = (el.innerText || el.value || attr(el, "aria-label") || attr(el, "placeholder") || "").trim().slice(0, 80);
  return { selector: selectorFor(el), tag: el.tagName.toLowerCase(), text };
}
"""

_FOCUSED_JS = """
() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  const cssEscape = (window.CSS && CSS.escape)
    ? (value) => CSS.escape(value)
    : (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
  let selector = null;
  if (el.id) selector = "#" + cssEscape(el.id);
  else if (el.getAttribute("name")) selector = el.tagName.toLowerCase() + '[name="' + cssEscape(el.getAttribute("name")) + '"]';
  else if (el.getAttribute("placeholder")) selector = el.tagName.toLowerCase() + '[placeholder="' + el.getAttribute("placeholder").replace(/"/g, '\\\\"') + '"]';
  if (!selector) {
    const parts = [];
    let cur = el;
    while (cur && cur.tagName !== "HTML" && parts.length < 6) {
      let index = 1;
      let sib = cur;
      while ((sib = sib.previousElementSibling)) if (sib.tagName === cur.tagName) index++;
      parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
      cur = cur.parentElement;
    }
    selector = parts.join(" > ");
  }
  return { selector, tag: el.tagName.toLowerCase() };
}
"""

# Smaller viewport for free tier speed, configurable
VIEWPORT_WIDTH = int(os.environ.get("TF_VIEWPORT_WIDTH", "1024"))
VIEWPORT_HEIGHT = int(os.environ.get("TF_VIEWPORT_HEIGHT", "640"))
VIEWPORT = {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
SESSIONS = {}
_LOCK = asyncio.Lock()

# Grace period for the debounced export on save. It must be generous: cutting an
# export short is what loses a recording. The caller performs an authoritative
# export afterwards, so nothing depends on this window being tight.
EXPORT_FLUSH_TIMEOUT = float(os.environ.get("TF_EXPORT_FLUSH_TIMEOUT", "10"))

# Consecutive identical actions collapse into one step with a repeat count.
# Set TF_MERGE_REPEAT_STEPS=0 to keep one row per action instead.
MERGE_REPEAT_STEPS = os.environ.get("TF_MERGE_REPEAT_STEPS", "1").strip().lower() in {
    "1", "true", "yes", "on",
}

# Upper bound on a merged repeat count, matching the executor's replay cap.
MAX_REPEAT = 100

# Actions whose repeats are NOT interchangeable, so each one keeps its own row.
# `save_variable` snapshots whatever the input holds at that moment: two
# consecutive captures of the same field usually hold different values, and
# folding them together would claim otherwise.
MERGE_EXCLUDED_ACTIONS = frozenset({"save_variable", "navigate"})


def get_session(recording_id):
    return SESSIONS.get(recording_id)


class RecorderSession:
    def __init__(self, recording_id, start_url, start_seq):
        self.recording_id = recording_id
        self.start_url = start_url
        self.seq = start_seq
        self.status = "starting"
        self.error = None
        self.current_url = start_url
        self.latest_jpeg = None
        self.listeners = []
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.preview = None
        self._ready = asyncio.Event()
        self._closed = False
        # For smooth saving
        self._pending_export = False
        self._export_task = None
        self._save_logs = []  # for debugging save issues
        self._image_tasks = set()
        # Permit for this session's Chromium, from the budget shared with runs.
        self._browser_slot = None

    def add_listener(self, callback):
        self.listeners.append(callback)

    def remove_listener(self, callback):
        self.listeners = [item for item in self.listeners if item is not callback]

    async def _broadcast(self, payload):
        for callback in list(self.listeners):
            try:
                result = callback(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self.remove_listener(callback)

    async def start(self):
        os.makedirs(os.path.join(ARTIFACTS, "rec", self.recording_id), exist_ok=True)
        try:
            # Wait for the browser budget before launching. Without this a
            # recording session and a run could each hold a Chromium and take the
            # instance over its memory limit.
            self._browser_slot = await browser_budget.acquire(f"rec:{self.recording_id}")
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.launch(**launch_kwargs())
            self.context = await self.browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
                # Speed: disable animations, reduced motion
                reduced_motion="reduce",
            )
            self.page = await self.context.new_page()
            self.preview = Preview(self.page, on_jpeg=self._on_jpeg)
            if self.start_url:
                await self.page.goto(self.start_url, wait_until="domcontentloaded", timeout=30000)
                self.current_url = self.page.url
                await self._record("navigate", value=self.page.url, label=f"Open {self.page.url}", force_new=True)
            self.preview.start()
            await self.preview.capture(force=True)
            self.status = "live"
            logger.info("RECORDER LIVE %s %s viewport=%sx%s", self.recording_id, self.current_url, VIEWPORT_WIDTH, VIEWPORT_HEIGHT)
        except Exception as exc:
            self.status = "error"
            self.error = explain_launch_error(exc)
            logger.exception("RECORDER FAILED %s", self.recording_id)
            await self._broadcast({"type": "error", "message": self.error})
            await self._close_browser()
        finally:
            self._ready.set()

    async def _on_jpeg(self, data: bytes):
        self.latest_jpeg = data

    async def wait_ready(self, timeout=30):
        await asyncio.wait_for(self._ready.wait(), timeout)
        if self.status == "error":
            raise RuntimeError(self.error or "Browser failed to start")
        if self.page is None:
            raise RuntimeError("Browser is not running")

    async def handle_input(self, msg):
        await self.wait_ready()
        kind = msg.get("type")
        # Touch preview for fast feedback
        if self.preview:
            self.preview.touch()

        async with self.preview.lock:
            if kind == "tap":
                x = int(msg["x"])
                y = int(msg["y"])
                info = None
                try:
                    info = await asyncio.wait_for(self.page.evaluate(_ELEMENT_JS, [x, y]), 3)
                except Exception:
                    info = None
                await self.page.mouse.click(x, y)
                selector = {
                    "primary": (info or {}).get("selector"),
                    "x": x,
                    "y": y,
                    "text": (info or {}).get("text"),
                    "tag": (info or {}).get("tag"),
                }
                text = (selector.get("text") or "").strip()
                label = f"Click {text}" if text else f"Click {x},{y}"
                step = await self._record("click", selector=selector, label=label)
            elif kind == "text":
                text = str(msg.get("text") or "")
                if not text:
                    raise ValueError("Text is empty")
                focused = None
                try:
                    focused = await self.page.evaluate(_FOCUSED_JS)
                except Exception:
                    focused = None
                with SessionLocal() as db:
                    from app.db import Recording
                    recording = db.get(Recording, self.recording_id)
                    variables = resolve_variables(db, recording.project_id)
                missing = [name for name in VAR_REGEX.findall(text) if name not in variables]
                if missing:
                    raise ValueError("Unknown variable: " + ", ".join(missing))
                resolved = interpolate(text, variables)
                await self.page.keyboard.type(resolved, delay=0)
                selector = {"primary": (focused or {}).get("selector")} if focused else None
                step = await self._record("type", value=text, selector=selector, label=f"Type: {text}")
            elif kind in ("key", "press"):
                key = str(msg.get("key") or msg.get("value") or "")
                if not key:
                    raise ValueError("Key is empty")
                await self.page.keyboard.press(key)
                step = await self._record("press", value=key, label=f"Press {key}")
            elif kind == "save_variable":
                name = str(msg.get("name") or "").strip()
                if not name or not re.fullmatch(r"[\w.-]{1,100}", name):
                    raise ValueError("Variable name must be 1–100 letters, numbers, dots, dashes or underscores")
                focused = await self.page.evaluate(_FOCUSED_JS)
                selector = (focused or {}).get("selector")
                if not selector:
                    raise ValueError("Focus an input with an id, name or placeholder before saving its value")
                actual = await self.page.evaluate("() => document.activeElement.value")
                with SessionLocal() as db:
                    from app.db import Recording
                    recording = db.get(Recording, self.recording_id)
                    row = db.query(Variable).filter(Variable.project_id == recording.project_id, Variable.name == name).first()
                    project_id = recording.project_id
                    if row and not msg.get("existing"):
                        raise ValueError(f"Variable {name} already exists; choose update existing")
                    if not row and msg.get("existing"):
                        raise ValueError(f"Variable {name} does not exist")
                    variable_id = row.id if row else None
                if variable_id:
                    await asyncio.to_thread(library_store.update_variable, variable_id, value=str(actual))
                else:
                    await asyncio.to_thread(library_store.create_variable, project_id, name, str(actual))
                step = await self._record("save_variable", value=name, selector={"primary": selector}, label=f"Save input into {name}")
            else:
                raise ValueError(f"Unknown input type: {kind}")
            try:
                # Screenshot after the action, not the preceding frame. Persist off the
                # input path so screenshots and exports do not stall the next click.
                png = await self.page.screenshot(type="png", animations="disabled")
                task = asyncio.create_task(self._attach_image(step, png))
                self._image_tasks.add(task)
                task.add_done_callback(self._image_tasks.discard)
                self.preview.touch()
            except Exception as exc:
                logger.warning("STEP SCREENSHOT FAILED %s %s", self.recording_id, exc)
            self.current_url = self.page.url
        await self._broadcast({"type": "step", "step": step})
        return step

    async def _merge_into_previous(self, candidate):
        """Fold `candidate` into the previous row when it repeats it exactly.

        Returns the updated step payload, or None when the action is distinct and
        needs its own row. Equality is `library_store.step_signature`, the same
        rule the exporter's `compress_steps` uses, so what the recorder merges and
        what the library compresses can never disagree.
        """
        if self.seq < 1:
            return None
        try:
            with SessionLocal() as db:
                row = (
                    db.query(RecordingStep)
                    .filter(
                        RecordingStep.recording_id == self.recording_id,
                        RecordingStep.order == self.seq,
                    )
                    .first()
                )
                if row is None:
                    return None
                previous = {
                    "action": row.action,
                    "value": row.value,
                    "label": library_store.strip_repeat_suffix(row.label),
                    "selector": row.selector,
                }
                if library_store.step_signature(previous) != library_store.step_signature(candidate):
                    return None
                count = (row.repeat_count or 1) + 1
                if count > MAX_REPEAT:
                    return None
                row.repeat_count = count
                db.commit()
                db.refresh(row)
                return {
                    "id": row.id,
                    "order": row.order,
                    "action": row.action,
                    "value": row.value,
                    "label": row.label,
                    "selector": row.selector,
                    "repeat": row.repeat_count,
                    "screenshot": os.path.basename(row.screenshot_path) if row.screenshot_path else None,
                    "merged": True,
                }
        except Exception as exc:
            # A merge is an optimisation; never let it cost the user their action.
            logger.warning("MERGE SKIPPED %s %s", self.recording_id, exc)
            return None

    async def _record(self, action, value=None, label=None, selector=None, force_new=False):
        """Persist the action, folding it into the previous row when it repeats it.

        Consecutive identical actions become one step carrying `repeat = N`, which
        the replay expands back into N actions. `force_new` keeps an action on its
        own row even when the previous one matches.
        """
        candidate = {"action": action, "value": value, "label": label, "selector": selector}
        if not force_new and MERGE_REPEAT_STEPS and (action or "").lower() not in MERGE_EXCLUDED_ACTIONS:
            merged = await self._merge_into_previous(candidate)
            if merged is not None:
                logger.info(
                    "RECORDER MERGE %s order=%s action=%s repeat=%s",
                    self.recording_id, merged["order"], action, merged["repeat"],
                )
                self._schedule_export()
                return merged

        self.seq += 1

        try:
            with SessionLocal() as db:
                row = RecordingStep(
                    recording_id=self.recording_id,
                    order=self.seq,
                    action=action,
                    value=value,
                    label=label,
                    selector=selector,
                    repeat_count=1,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                step = {
                    "id": row.id,
                    "order": row.order,
                    "action": row.action,
                    "value": row.value,
                    "label": row.label,
                    "selector": row.selector,
                    "repeat": 1,
                }
        except Exception as e:
            logger.exception("RECORD FAILED %s %s", self.recording_id, e)
            raise RuntimeError(f"Failed to save step: {e}") from e

        # Schedule debounced export for smooth saving (not on every step immediately)
        self._schedule_export()
        return step

    async def _attach_image(self, step, png):
        try:
            name = await asyncio.to_thread(library_store.attach_step_image, self.recording_id, step["order"], png)
            if name:
                with SessionLocal() as db:
                    row = db.get(RecordingStep, step["id"])
                    if row:
                        row.screenshot_path = name
                        db.commit()
                step["screenshot"] = name
                self._schedule_export()
        except Exception as exc:
            logger.warning("SCREENSHOT ATTACH FAILED %s %s", self.recording_id, exc)

    def _schedule_export(self):
        """Debounced export to avoid heavy I/O on every step."""
        if self._export_task and not self._export_task.done():
            self._pending_export = True
            return

        async def _do_export():
            try:
                # Small debounce
                await asyncio.sleep(0.5)
                while True:
                    self._pending_export = False
                    try:
                        # Run in thread to avoid blocking event loop
                        await asyncio.to_thread(library_store.export_recording, self.recording_id, publish=False)
                        logger.info("RECORDER EXPORT OK %s seq=%s", self.recording_id, self.seq)
                    except Exception as e:
                        logger.warning("RECORDER EXPORT FAILED %s %s", self.recording_id, e)
                        self._save_logs.append(f"{time.time()}: export failed {e}")
                    if not self._pending_export:
                        break
                    # If another export was requested during this one, loop again after short delay
                    await asyncio.sleep(0.3)
            finally:
                self._export_task = None

        self._export_task = asyncio.create_task(_do_export())

    async def _close_browser(self):
        if self.preview is not None:
            await self.preview.stop()
            self.preview = None
        for closer in (self.context, self.browser):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception:
                pass
        self.context = None
        self.browser = None
        self.page = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        # Hand the browser permit back last, once the process is really gone.
        if self._browser_slot is not None:
            slot, self._browser_slot = self._browser_slot, None
            browser_budget.release(slot)

    async def _flush_export(self):
        """Wait for the debounced export without ever raising at the caller.

        `asyncio.wait_for` cancels the awaited task and waits for that
        cancellation to finish before raising TimeoutError, and the export task
        clears `self._export_task` in its own `finally`. Re-reading the attribute
        after the wait therefore sees `None` — which is exactly how SAVE died with
        `AttributeError: 'NoneType' object has no attribute 'cancel'`. Hold the
        task in a local, and shield it so a slow export is never cut off
        mid-write: a cancelled export is a lost recording.
        """
        task = self._export_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), EXPORT_FLUSH_TIMEOUT)
            return
        except asyncio.TimeoutError:
            # Leave the export running; the caller performs an authoritative
            # export next, and both writes are serialized by the library lock.
            self._save_logs.append(
                f"{time.time()}: export still running after {EXPORT_FLUSH_TIMEOUT}s; left to finish"
            )
            logger.warning(
                "RECORDER EXPORT SLOW %s (left running after %.0fs)",
                self.recording_id, EXPORT_FLUSH_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._save_logs.append(f"{time.time()}: export failed {exc}")
            logger.warning("RECORDER EXPORT FAILED %s %s", self.recording_id, exc)

    async def stop(self):
        """Close the session. Never raises: the browser must always be released."""
        if self._closed:
            return
        self._closed = True
        self.status = "stopped" if self.status != "error" else self.status
        SESSIONS.pop(self.recording_id, None)

        try:
            # Do not close before queued screenshot writes finish.
            if self._image_tasks:
                await asyncio.gather(*list(self._image_tasks), return_exceptions=True)
            # Ensure the debounced export has landed before the session goes away.
            await self._flush_export()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._save_logs.append(f"{time.time()}: stop failed {exc}")
            logger.exception("RECORDER STOP FAILED %s", self.recording_id)
        finally:
            await self._close_browser()

        # Final save logs
        logger.info("RECORDER STOPPING %s seq=%s logs=%s", self.recording_id, self.seq, self._save_logs[-5:])


async def open_session(recording_id, start_url, start_seq) -> RecorderSession:
    async with _LOCK:
        existing = SESSIONS.get(recording_id)
        if existing and existing.status in ("starting", "live"):
            return existing
        if existing:
            await existing.stop()
        session = RecorderSession(recording_id, start_url, start_seq)
        SESSIONS[recording_id] = session
    asyncio.create_task(session.start())
    return session
