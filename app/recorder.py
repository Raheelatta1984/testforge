"""Remote-browser recorder.

The browser runs on the server. The dashboard shows `session.latest_jpeg` and
posts clicks / keystrokes back. Every accepted action is stored as a
RecordingStep that the executor can replay.
"""

import asyncio
import os

from playwright.async_api import async_playwright

from app.browser import Preview, explain_launch_error, launch_kwargs
from app.config import ARTIFACTS, logger
from app.db import RecordingStep, SessionLocal

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
  return { selector, tag: el.tagName.toLowerCase() };
}
"""

VIEWPORT = {"width": 1280, "height": 800}
SESSIONS = {}
_LOCK = asyncio.Lock()


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
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.launch(**launch_kwargs())
            self.context = await self.browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=1,
            )
            self.page = await self.context.new_page()
            # Start the screenshot loop only after navigation so it cannot race
            # page.goto on the same Playwright page.
            self.preview = Preview(self.page, on_jpeg=self._on_jpeg)
            if self.start_url:
                await self.page.goto(self.start_url, wait_until="domcontentloaded", timeout=30000)
                self.current_url = self.page.url
                await self._record("navigate", value=self.page.url, label=f"Open {self.page.url}")
            self.preview.start()
            await self.preview.capture()
            self.status = "live"
            logger.info("RECORDER LIVE %s %s", self.recording_id, self.current_url)
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
        async with self.preview.lock:
            if kind == "tap":
                x = int(msg["x"])
                y = int(msg["y"])
                info = None
                try:
                    info = await asyncio.wait_for(self.page.evaluate(_ELEMENT_JS, [x, y]), 5)
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
                await self.page.keyboard.type(text)
                selector = {"primary": (focused or {}).get("selector")} if focused else None
                step = await self._record("type", value=text, selector=selector, label=f"Type: {text}")
            elif kind in ("key", "press"):
                key = str(msg.get("key") or msg.get("value") or "")
                if not key:
                    raise ValueError("Key is empty")
                await self.page.keyboard.press(key)
                step = await self._record("press", value=key, label=f"Press {key}")
            else:
                raise ValueError(f"Unknown input type: {kind}")
            try:
                await self.preview._shoot()
            except Exception:
                pass
            self.current_url = self.page.url
        await self._broadcast({"type": "step", "step": step})
        return step

    async def _record(self, action, value=None, label=None, selector=None):
        self.seq += 1
        with SessionLocal() as db:
            row = RecordingStep(
                recording_id=self.recording_id,
                order=self.seq,
                action=action,
                value=value,
                label=label,
                selector=selector,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return {
                "id": row.id,
                "order": row.order,
                "action": row.action,
                "value": row.value,
                "label": row.label,
                "selector": row.selector,
            }

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

    async def stop(self):
        if self._closed:
            return
        self._closed = True
        self.status = "stopped" if self.status != "error" else self.status
        SESSIONS.pop(self.recording_id, None)
        await self._close_browser()


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
