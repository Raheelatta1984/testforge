"""How TestForge launches the browser that recording and execution share.

The dashboard never opens a desktop window. It shows a live screenshot of a
headless Chromium. Launch stays headless on purpose; the picture the user
sees is produced by `Preview`.

Free-tier optimized:
- Minimal args to reduce memory/CPU
- Adaptive preview interval for fast feel without high consumption
- Lower JPEG quality with smart caching
"""

import asyncio
import os
import shutil
import time

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
    """Arguments for `playwright.chromium.launch` optimized for free tier.

    Bundled mode uses the browser installed by `playwright install chromium`
    (the Docker image does this). Set TF_CHROMIUM_PATH to force a binary, which
    is also the fallback when the bundled browser was never downloaded.
    """
    # Fast, low-resource args
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-sync",
        "--disable-translate",
        "--disable-features=Translate,BackForwardCache,AcceptCHFrame",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
        "--safebrowsing-disable-auto-update",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-dev-tools",
    ]
    # Single process reduces memory on free tier (configurable)
    if os.environ.get("TF_BROWSER_SINGLE_PROCESS", "1") == "1":
        args.extend(["--single-process", "--no-zygote"])

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

    Optimized for free tier:
    - Adaptive interval: fast after input (0.15s), slower when idle (0.5s)
    - Lower JPEG quality (configurable) to reduce CPU/bandwidth
    - Skip duplicate frames via hash check
    - On-demand capture after actions
    """

    def __init__(self, page, on_jpeg=None, interval=None):
        self.page = page
        self.on_jpeg = on_jpeg
        # Fast default: 0.18s for snappy feel, configurable via env
        default_interval = float(os.environ.get("TF_PREVIEW_INTERVAL", "0.18"))
        self.base_interval = interval if interval is not None else default_interval
        self.interval = self.base_interval
        self.lock = asyncio.Lock()
        self.latest = None
        self._stopped = asyncio.Event()
        self._task = None
        self._last_activity = time.time()
        self._last_hash = None
        self._jpeg_quality = int(os.environ.get("TF_JPEG_QUALITY", "35"))  # lower = faster, less CPU

    def start(self):
        self._task = asyncio.create_task(self._loop())
        return self

    def touch(self):
        """Mark activity to speed up preview."""
        self._last_activity = time.time()
        self.interval = self.base_interval

    async def _shoot(self, force=False):
        try:
            # Use lower quality for speed
            data = await self.page.screenshot(type="jpeg", quality=self._jpeg_quality)
        except Exception:
            # Page might be closed
            return None

        # Skip duplicate frames to save bandwidth (unless forced)
        if not force and self._last_hash is not None:
            # Quick hash check - compare length and first bytes
            if len(data) == len(self._last_hash) and data[:100] == self._last_hash[:100]:
                # Likely same frame, but still update latest for new clients
                self.latest = data
                return data

        self.latest = data
        self._last_hash = data
        if self.on_jpeg is not None:
            result = self.on_jpeg(data)
            if asyncio.iscoroutine(result):
                await result
        return data

    async def _loop(self):
        while not self._stopped.is_set():
            try:
                # Adaptive interval: fast after activity, slow when idle
                idle = time.time() - self._last_activity
                if idle > 3.0:
                    # Idle for 3s, slow down to save CPU
                    self.interval = min(0.6, self.base_interval * 3)
                elif idle > 1.0:
                    self.interval = min(0.35, self.base_interval * 2)
                else:
                    self.interval = self.base_interval

                async with self.lock:
                    if self._stopped.is_set():
                        break
                    await self._shoot()
            except Exception:
                if self._stopped.is_set():
                    break
            try:
                await asyncio.wait_for(self._stopped.wait(), self.interval)
            except asyncio.TimeoutError:
                pass

    async def capture(self, force=True):
        # Caller must NOT already hold self.lock — asyncio.Lock is not reentrant.
        self.touch()
        async with self.lock:
            return await self._shoot(force=force)

    async def stop(self):
        self._stopped.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 2)
            except Exception:
                self._task.cancel()
