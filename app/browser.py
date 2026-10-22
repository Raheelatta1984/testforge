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

# How often the capture loop re-checks whether anybody is watching while it is
# idle. Checking is free; rendering a frame nobody will see is not.
VIEWER_RECHECK = float(os.environ.get("TF_VIEWER_RECHECK", "0.15"))

# Standard desktop Chrome UA without the `HeadlessChrome` token that causes
# CDN bot managers (Akamai, Cloudflare) to tarpit HTTP/2 navigation streams.
DEFAULT_USER_AGENT = os.environ.get(
    "TF_BROWSER_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


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
        "--disable-blink-features=AutomationControlled",
        "--disable-http2",
    ]
    # Single process reduces memory on free tier (configurable)
    if os.environ.get("TF_BROWSER_SINGLE_PROCESS", "1") == "1":
        args.extend(["--single-process", "--no-zygote"])
    # A Chromium built for another distribution can need one extra flag that this
    # project does not set for the bundled browser (for example
    # --ozone-platform=headless on a serverless build). Only the operator knows
    # which, so the flags stay opt-in and empty by default.
    args.extend(flag for flag in os.environ.get("TF_CHROMIUM_ARGS", "").split() if flag)

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


def trace_ok() -> bool:
    """Whether a run should record a Playwright trace.zip.

    Same rule as video: the bundled browser can always write one, a stand-in
    binary often cannot, and TF_TRACE=0 opts a run out (for example a tight
    loop where per-step screenshots are evidence enough).
    """
    if os.environ.get("TF_CHROMIUM_PATH", "").strip():
        return False
    if os.environ.get("TF_TRACE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return _mode() == "bundled"


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
    - Captures nothing at all while `should_capture` reports no viewer, so a run
      nobody is watching (or one with the live window switched off) spends no
      render time on frames that would be thrown away
    """

    def __init__(self, page, on_jpeg=None, interval=None, should_capture=None,
                 frame_metadata=None):
        self.page = page
        self.on_jpeg = on_jpeg
        # `should_capture` returns False while the live window is switched off or
        # no client is attached. Capturing then is pure waste.
        self.should_capture = should_capture
        # Optional snapshot of the step being replayed. It is read before the
        # screenshot starts, so a frame finishing just as the next step begins
        # cannot be mislabeled as belonging to that next step.
        self.frame_metadata = frame_metadata
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
        self._last_context = None
        self._jpeg_quality = int(os.environ.get("TF_JPEG_QUALITY", "35"))  # lower = faster, less CPU
        self.captures = 0

    def _wanted(self) -> bool:
        """Whether a frame would reach anybody. Defaults to yes."""
        if self.should_capture is None:
            return True
        try:
            return bool(self.should_capture())
        except Exception:
            return True

    def start(self):
        self._task = asyncio.create_task(self._loop())
        return self

    def touch(self):
        """Mark activity to speed up preview."""
        self._last_activity = time.time()
        self.interval = self.base_interval

    async def _shoot(self, force=False):
        page = self.page
        frame_context = None
        if self.frame_metadata is not None:
            try:
                frame_context = self.frame_metadata()
            except Exception:
                frame_context = None
        try:
            # Use lower quality for speed and disable animation/font waits so a
            # slow external stylesheet never stalls the live preview. Read
            # page/context before awaiting the screenshot so the frame retains
            # the step context it began with.
            data = await page.screenshot(
                type="jpeg",
                quality=self._jpeg_quality,
                animations="disabled",
                timeout=3000,
            )
            self.captures += 1
        except Exception:
            # Page might be closed or navigating; the next live frame will retry.
            return None

        # Skip duplicate frames to save bandwidth (unless forced). A step change
        # is meaningful even if the pixels are identical: emit that frame again
        # with its new step tag so the screen/log remain visibly synchronized.
        context_changed = frame_context is not None and frame_context != self._last_context
        if not force and not context_changed and self._last_hash is not None:
            # Quick hash check - compare length and first bytes
            if len(data) == len(self._last_hash) and data[:100] == self._last_hash[:100]:
                self.latest = data
                return data

        self.latest = data
        self._last_hash = data
        self._last_context = frame_context
        if self.on_jpeg is not None:
            result = (self.on_jpeg(data, frame_context)
                      if frame_context is not None else self.on_jpeg(data))
            if asyncio.iscoroutine(result):
                await result
        return data

    async def _loop(self):
        while not self._stopped.is_set():
            try:
                # Nothing to do while the live window is off or nobody is
                # watching: polling again is cheaper than rendering a frame
                # that gets discarded.
                if not self._wanted():
                    # Re-check often: this branch costs a timer, not a render, and
                    # a short interval is what keeps the first frame quick when a
                    # viewer attaches part-way through a run.
                    try:
                        await asyncio.wait_for(self._stopped.wait(), VIEWER_RECHECK)
                    except asyncio.TimeoutError:
                        pass
                    continue
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
        """Stop the capture loop. Never raises: callers rely on it to unwind."""
        self._stopped.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            task.cancel()
