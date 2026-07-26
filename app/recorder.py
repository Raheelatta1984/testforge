import asyncio, os
from playwright.async_api import async_playwright

from app.config import ARTIFACTS
from app.browser import launch_kwargs, video_ok
from app.db import SessionLocal, Recording, RecordingStep

INIT_SCRIPT = r"""
(() => {
  if (window.__recorderInstalled) return;
  window.__recorderInstalled = true;
  const css = (el) => {
    if (!(el instanceof Element)) return '';
    if (el.dataset && el.dataset.testid) return `[data-testid="${el.dataset.testid}"]`;
    if (el.id) return `#${CSS.escape(el.id)}`;
    const path = [];
    while (el && el.nodeType === 1 && path.length < 6) {
      let s = el.localName;
      const p = el.parentElement;
      if (p) {
        const same = [...p.children].filter(c => c.localName === el.localName);
        if (same.length > 1) s += `:nth-of-type(${same.indexOf(el) + 1})`;
      }
      path.unshift(s); el = p;
    }
    return path.join(' > ');
  };
  const labelOf = (el) =>
    (el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || el.getAttribute('name'))) || (el.innerText || '').slice(0, 40).trim() || '';
  const send = (p) => { try { window.__recordEvent(JSON.parse(JSON.stringify(p))); } catch(e){} };
  document.addEventListener('click',    e => send({action:'click',   selector:css(e.target), label:labelOf(e.target)}), true);
  document.addEventListener('dblclick', e => send({action:'dblclick',selector:css(e.target), label:labelOf(e.target)}), true);
  document.addEventListener('change', e => {
    const t = e.target;
    if (t.matches && t.matches('input,textarea,select'))
      send({action: t.tagName==='SELECT' ? 'select' : 'fill',
            selector: css(t), value: t.value, label: labelOf(t)});
  }, true);
  document.addEventListener('copy', () => send({action:'copy', value:String(window.getSelection())}), true);
  document.addEventListener('cut',  () => send({action:'cut',  value:String(window.getSelection())}), true);
  document.addEventListener('paste', e => send({action:'paste',
      value:(e.clipboardData ? e.clipboardData.getData('text') : ''),
      selector:css(e.target), label:labelOf(e.target)}), true);
  document.addEventListener('keydown', e => {
    const combo = [e.ctrlKey&&'Control', e.shiftKey&&'Shift', e.altKey&&'Alt', e.key]
                    .filter(Boolean).join('+');
    if (['Enter','Tab','Escape'].includes(e.key) || e.ctrlKey || e.metaKey)
      send({action:'press', value:combo, selector:css(e.target)});
  }, true);
})();
"""

class RecorderSession:
    def __init__(self, recording_id, start_url, start_seq, on_frame, on_event):
        self.recording_id = recording_id
        self.start_url = start_url
        self.seq = start_seq  # Continues from existing steps if resumed!
        self.on_frame = on_frame
        self.on_event = on_event

    async def start(self):
        os.makedirs(f"{ARTIFACTS}/rec/{self.recording_id}", exist_ok=True)
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(**launch_kwargs())
        ctx_args = {
            "viewport": {"width": 1280, "height": 800},
            "permissions": ["clipboard-read", "clipboard-write"],
        }
        if video_ok():
            ctx_args["record_video_dir"] = f"{ARTIFACTS}/rec/{self.recording_id}"
            ctx_args["record_video_size"] = {"width": 1280, "height": 800}
        self.context = await self.browser.new_context(**ctx_args)
        await self.context.add_init_script(INIT_SCRIPT)
        await self.context.expose_binding("__recordEvent", self._on_record_event)
        self.page = await self.context.new_page()

        self.cdp = await self.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_screencast_frame)
        await self.cdp.send("Page.startScreencast",
                            {"format": "jpeg", "quality": 65,
                             "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 1})

        self.page.on("framenavigated", lambda f: asyncio.create_task(
            self._record({"action": "navigate", "url": f.url,
                          "label": f"Navigate to {f.url}"}))
            if f == self.page.main_frame else None)

        await self.page.goto(self.start_url, wait_until="domcontentloaded")

    async def _on_screencast_frame(self, params):
        try: await self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception: pass
        await self.on_frame(params["data"])

    async def _on_record_event(self, source, payload):
        await self._record(payload)

    async def _record(self, payload):
        self.seq += 1
        shot = f"{ARTIFACTS}/rec/{self.recording_id}/step-{self.seq}.jpg"
        try: await self.page.screenshot(path=shot, type="jpeg", quality=55)
        except Exception: shot = None
        step = {"order": self.seq, "action": payload.get("action"),
                "selector": {"primary": payload.get("selector"), "fallbacks": [], "ai_hint": payload.get("label")},
                "value": payload.get("value"), "url": payload.get("url"),
                "label": payload.get("label") or payload.get("action"),
                "screenshot_path": shot}
        db = SessionLocal()
        try:
            db.add(RecordingStep(recording_id=self.recording_id, order=step["order"],
                action=step["action"], selector=step["selector"], value=step["value"],
                url=step["url"], label=step["label"], screenshot_path=shot))
            db.commit()
        finally: db.close()
        await self.on_event(step)

    async def handle_input(self, msg):
        t = msg.get("type")
        if t == "tap":
            x, y = msg["x"], msg["y"]
            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        elif t == "text": await self.page.keyboard.type(msg["text"], delay=30)
        elif t == "key": await self.page.keyboard.press(msg["key"])
        elif t == "scroll": await self.page.mouse.wheel(0, msg.get("deltaY", 300))

    async def stop(self):
        try: await self.cdp.send("Page.stopScreencast")
        except Exception: pass
        video = self.page.video if video_ok() else None
        await self.context.close()
        video_path = None
        if video:
            try: video_path = await video.path()
            except Exception: pass
        await self.browser.close()
        await self._pw.stop()
        db = SessionLocal()
        try:
            rec = db.get(Recording, self.recording_id)
            if rec:
                rec.status = "ready"
                if video_path: rec.video_path = video_path
                db.commit()
        finally: db.close()

ACTIVE = {}