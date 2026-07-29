import asyncio, os
from playwright.async_api import async_playwright
from app.config import ARTIFACTS
from app.browser import launch_kwargs
from app.db import SessionLocal, Recording, RecordingStep

class RecorderSession:
    def __init__(self, recording_id, start_url, start_seq, on_frame, on_event):
        self.recording_id = recording_id
        self.start_url = start_url
        self.seq = start_seq
        self.on_frame = on_frame
        self.on_event = on_event
        self._pw = None

    async def start(self):
        os.makedirs(os.path.join(ARTIFACTS, "rec", self.recording_id), exist_ok=True)
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(**launch_kwargs())
        self.context = await self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = await self.context.new_page()
        self.cdp = await self.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 50})
        await self.page.goto(self.start_url)

    async def _on_frame(self, params):
        await self.on_frame(params["data"])
        await self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    async def handle_input(self, msg):
        t = msg.get("type")
        if t == "tap":
            await self.page.mouse.click(msg['x'], msg['y'])
            await self._record("click", label=f"Click {msg['x']},{msg['y']}")
        elif t == "text":
            await self.page.keyboard.type(msg['text'])
            await self._record("fill", value=msg['text'], label=f"Type: {msg['text']}")

    async def _record(self, action, value=None, label=None):
        self.seq += 1
        db = SessionLocal()
        db.add(RecordingStep(recording_id=self.recording_id, order=self.seq, action=action, value=value, label=label))
        db.commit(); db.close()
        await self.on_event({"order": self.seq, "action": action, "label": label})

    async def stop(self):
        await self.browser.close()
        await self._pw.stop()