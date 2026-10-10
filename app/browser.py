"""How TestForge launches the browser that recording and execution share.

The dashboard never opens a desktop window. It shows a live screenshot of a
headless Chromium. Launch stays headless on purpose; the picture the user
sees is produced by `Preview`.
"""

import asyncio
import os
import shutil

from .config import IS_TERMUX


def _mode() -> str:
    mode = os.environ.get("TF_BROWSER_MODE", "auto").lower()
    if mode == "auto":
        return "system" if IS_TERMUX else "bundled"
    return mode


def _system_chromium() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _browser_env() -> dict | None:
    """Libraries for a custom Chromium, without leaking them into Python.

    Putting those directories on the server's own LD_LIBRARY_PATH breaks
    modules such as sqlite3 (undefined symbol sqlite3_trace_v2). Playwright
    only needs them in the browser process.
    """
    libs = os.environ.get("TF_CHROMIUM_LIBS", "").strip()
    if not libs:
        return None
    env = os.environ.copy()
    current = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = libs + (":" + current if current else "")
    return env


def launch_kwargs() -> dict:
    """Arguments for `playwright.chromium.launch`.

    Bundled mode uses the browser installed by `playwright install chromium`
    (the Docker image does this). Set TF_CHROMIUM_PATH to force a binary, which
    is also the fallback when the bundled browser was never downloaded.
    """
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-setuid-sandbox",
        "--disable-gpu",
    ]
    kw = {"headless": True, "args": args}
    browser_env = _browser_env()
    if browser_env is not None:
        kw["env"] = browser_env
    explicit = os.environ.get("TF_CHROMIUM_PATH", "").strip()
    if explicit:
        if not os.path.isfile(explicit):
            raise RuntimeError(f"TF_CHROMIUM_PATH does not exist: {explicit}")
        kw["executable_path"] = explicit
        return kw
    if _mode() == "system":
        exe = _system_chromium()
        if not exe:
            raise RuntimeError("Chromium not found. Install it or set TF_CHROMIUM_PATH.")
        kw["executable_path"] = exe
        kw["args"] = args + ["--single-process", "--no-zygote"]
    return kw


def video_ok() -> bool:
    # A stand-in binary (TF_CHROMIUM_PATH) often cannot write Playwright video.
    if os.environ.get("TF_CHROMIUM_PATH", "").strip():
        return False
    return _mode() == "bundled" and os.environ.get("TF_NO_VIDEO") != "1"


def explain_launch_error(exc: Exception) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else exc.__class__.__name__
    if "Executable doesn't exist" in text or "playwright install" in text:
        return (
            "Chromium is not installed, so the browser window cannot open. "
            "On the server run: playwright install chromium"
        )
    if "Chromium not found" in text or "TF_CHROMIUM_PATH" in text:
        return first
    return first[:400]


class Preview:
    """Keep a fresh JPEG of the page so the dashboard can show the browser.

    CDP screencast was the old path. Playwright only invokes those listeners
    synchronously, and the async handler was never awaited, so the UI received
    no frames at all. A screenshot loop is slower and reliable.
    """

    def __init__(self, page, on_jpeg=None, interval=0.35):
        self.page = page
        self.on_jpeg = on_jpeg
        self.interval = interval
        self.lock = asyncio.Lock()
        self.latest = None
        self._stopped = asyncio.Event()
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def _shoot(self):
        data = await self.page.screenshot(type="jpeg", quality=45)
        self.latest = data
        if self.on_jpeg is not None:
            result = self.on_jpeg(data)
            if asyncio.iscoroutine(result):
                await result
        return data

    async def _loop(self):
        while not self._stopped.is_set():
            try:
                async with self.lock:
                    if self._stopped.is_set():
                        break
                    await self._shoot()
            except Exception:
                # A closed page raises here on shutdown. The next loop sees the stop flag.
                if self._stopped.is_set():
                    break
            try:
                await asyncio.wait_for(self._stopped.wait(), self.interval)
            except asyncio.TimeoutError:
                pass

    async def capture(self):
        # Caller must NOT already hold self.lock — asyncio.Lock is not reentrant.
        async with self.lock:
            return await self._shoot()

    async def stop(self):
        self._stopped.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 2)
            except Exception:
                self._task.cancel()
