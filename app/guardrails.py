"""Process-wide resource guardrails for the hosted deployment.

The Render free instance has a hard memory ceiling and very little CPU, and it
restarts the service the moment the ceiling is crossed. Every limit in this
module exists because something was previously unbounded:

* Chromium processes were capped for *runs* only. A recording session could open
  a second browser while a run was executing, which is the fastest way to an OOM.
* ``LIVE_FRAMES`` kept the last screenshot of every run for the life of the
  process, and ``RUN_BUFFERS`` kept up to 200 events per run. Both grew forever.
* ``GET /api/sync/github`` zipped the entire artifact tree into RAM in one go.
* ``GET /api/runs`` returned every run with its full log, and the dashboard
  polled it every few seconds.
* Batch execution could claim every recording in the library at once, and the
  Logs tab could be asked to serve an arbitrarily large file from disk.

Everything here is a plain number with an environment override, so an operator
can tune the deployment without reading the call sites.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw.strip()))
    except ValueError:
        return default


# --- Concurrency ------------------------------------------------------------
# One Chromium per instance. The dashboard and every recording share it.
MAX_CONCURRENT_BROWSERS = _int("TF_MAX_BROWSERS", 1)

# --- Live frames ------------------------------------------------------------
# How many runs may keep a last frame in memory at once, and the idle seconds
# after which a frame is dropped even if the cap is not reached.
MAX_LIVE_FRAME_RUNS = _int("TF_MAX_LIVE_FRAME_RUNS", 4)
LIVE_FRAME_TTL = float(os.environ.get("TF_LIVE_FRAME_TTL", "120") or 120)

# --- Run event buffers ------------------------------------------------------
MAX_RUN_BUFFER_RUNS = _int("TF_MAX_RUN_BUFFER_RUNS", 8)
MAX_RUN_BUFFER_EVENTS = _int("TF_MAX_RUN_BUFFER_EVENTS", 60)

# --- Downloads --------------------------------------------------------------
MAX_ZIP_BYTES = _int("TF_MAX_ZIP_BYTES", 64 * 1024 * 1024)

# --- List endpoints ---------------------------------------------------------
DEFAULT_RUN_LIST_LIMIT = _int("TF_RUN_LIST_LIMIT", 25)
MAX_RUN_LIST_LIMIT = _int("TF_RUN_LIST_MAX", 100)

# --- Batch execution --------------------------------------------------------
# A batch replays many recordings through ONE browser. The caps below are what
# keep "many" from becoming an unbounded request: how much work one batch may
# claim, how much of it is kept in memory, and how many batches may exist at once.
MAX_BATCH_RECORDINGS = _int("TF_BATCH_MAX_RECORDINGS", 40)
MAX_CONCURRENT_BATCHES = _int("TF_MAX_BATCHES", 1)
BATCH_LOG_ENTRIES = _int("TF_BATCH_LOG_ENTRIES", 200)
MAX_BATCH_REPORTS = _int("TF_BATCH_REPORTS", 40)
BATCH_LIST_LIMIT = _int("TF_BATCH_LIST_LIMIT", 20)


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Sharing one browser context across same-origin recordings keeps a login warm and
# avoids a context per recording. Turn it off for strict isolation.
BATCH_SHARE_SESSION = _flag("TF_BATCH_SHARE_SESSION", True)
# One escalated retry for a timeout or a network error, never for a missing selector.
BATCH_RETRY_TRANSIENT = _flag("TF_BATCH_RETRY_TRANSIENT", True)

# --- Local log reader -------------------------------------------------------
# Used only when a deployment has no git checkout to read committed logs from.
# Bounded so the Logs tab cannot be turned into a way to read this instance dry.
MAX_LOG_FILE_BYTES = _int("TF_MAX_LOG_FILE_BYTES", 256 * 1024)
MAX_LOG_INDEX_ROWS = _int("TF_MAX_LOG_INDEX_ROWS", 40)


class BrowserBudget:
    """Cap concurrent Chromium processes across recording *and* execution.

    A single semaphore has to cover both, because the two paths previously had
    separate (or no) limits and could run a browser each at the same time.
    """

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._sem: asyncio.Semaphore | None = None
        self._loop = None
        self.active = 0
        self.peak = 0
        self.waiting = 0
        self.acquired = 0

    def _semaphore(self) -> asyncio.Semaphore:
        # asyncio primitives bind to the loop that first uses them, and the test
        # suite opens a fresh loop per case.
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._sem = asyncio.Semaphore(self.limit)
            self._loop = loop
        return self._sem

    @asynccontextmanager
    async def slot(self, label: str):
        """Hold one browser slot for the duration of the block.

        The counters stay correct even when the waiter is cancelled before it
        gets a slot, which matters because a cancelled request must not leave the
        budget permanently short by one.
        """
        self.waiting += 1
        acquired = False
        try:
            async with self._semaphore():
                acquired = True
                self.waiting -= 1
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.acquired += 1
                try:
                    yield
                finally:
                    self.active -= 1
        finally:
            if not acquired:
                self.waiting -= 1

    async def acquire(self, label: str) -> asyncio.Semaphore:
        """Take a slot for a caller whose lifetime spans several methods.

        Returns the semaphore so `release` hands the permit back to the same
        object even if the event loop has moved on. The recorder needs this
        because it launches the browser in `start` and closes it in `stop`.
        """
        sem = self._semaphore()
        self.waiting += 1
        try:
            await sem.acquire()
        except BaseException:
            self.waiting -= 1
            raise
        self.waiting -= 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.acquired += 1
        return sem

    def release(self, sem: asyncio.Semaphore | None) -> None:
        """Give a slot back. Safe to call twice."""
        if sem is None:
            return
        self.active = max(0, self.active - 1)
        sem.release()

    def report(self) -> dict:
        return {
            "limit": self.limit,
            "active": self.active,
            "waiting": self.waiting,
            "peak": self.peak,
            "acquired": self.acquired,
        }


class BoundedFrames:
    """Last live frame per run, with a hard cap on how many runs are kept.

    The previous plain dict never evicted, so a long-lived worker accumulated one
    JPEG per run until the instance was restarted.
    """

    def __init__(self, limit: int, ttl: float):
        self.limit = max(1, int(limit))
        self.ttl = ttl
        self._items: dict[str, tuple[float, bytes]] = {}

    def put(self, run_id: str, data: bytes) -> None:
        import time

        now = time.time()
        self._items.pop(run_id, None)
        self._items[run_id] = (now, data)
        self._evict(now)

    def get(self, run_id: str) -> bytes | None:
        import time

        entry = self._items.get(run_id)
        if entry is None:
            return None
        stamp, data = entry
        if self.ttl and (time.time() - stamp) > self.ttl:
            self._items.pop(run_id, None)
            return None
        return data

    def forget(self, run_id: str) -> None:
        self._items.pop(run_id, None)

    def _evict(self, now: float) -> None:
        if self.ttl:
            for run_id in [k for k, (stamp, _) in self._items.items() if (now - stamp) > self.ttl]:
                self._items.pop(run_id, None)
        while len(self._items) > self.limit:
            oldest = next(iter(self._items))
            self._items.pop(oldest, None)

    def clear(self) -> None:
        self._items.clear()

    def report(self) -> dict:
        return {"runs": len(self._items), "limit": self.limit, "bytes": sum(len(v[1]) for v in self._items.values())}


class BoundedRunBuffers:
    """Replay buffers for recently connected clients, bounded by run count.

    A late client should still see the current run's steps, but keeping every
    run's history in RAM is what turned a busy service into an OOM.
    """

    def __init__(self, max_runs: int, max_events: int):
        self.max_runs = max(1, int(max_runs))
        self.max_events = max(1, int(max_events))
        self._buffers: dict[str, list] = {}

    def append(self, run_id: str, payload: dict) -> list:
        buffer = self._buffers.get(run_id)
        if buffer is None:
            buffer = []
            self._buffers[run_id] = buffer
        else:
            self._buffers.pop(run_id, None)
            self._buffers[run_id] = buffer
        if payload.get("type") == "frame":
            buffer[:] = [item for item in buffer if item.get("type") != "frame"]
        buffer.append(payload)
        if len(buffer) > self.max_events:
            del buffer[: -self.max_events]
        while len(self._buffers) > self.max_runs:
            oldest = next(iter(self._buffers))
            self._buffers.pop(oldest, None)
        return buffer

    def get(self, run_id: str) -> list:
        return list(self._buffers.get(run_id, ()))

    def forget(self, run_id: str) -> None:
        self._buffers.pop(run_id, None)

    def report(self) -> dict:
        return {
            "runs": len(self._buffers),
            "limit": self.max_runs,
            "events": sum(len(value) for value in self._buffers.values()),
            "event_limit": self.max_events,
        }


class BatchBudget:
    """How many batches may execute at once.

    The browser budget already serialises Chromium, so a second batch would only
    sit waiting while holding its run rows in the queue. Refusing it up front is
    cheaper than queueing it, and the caller can tell the user why.
    """

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self.active = 0
        self.peak = 0
        self.rejected = 0

    def acquire(self) -> bool:
        if self.active >= self.limit:
            self.rejected += 1
            return False
        self.active += 1
        self.peak = max(self.peak, self.active)
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)

    def report(self) -> dict:
        return {"limit": self.limit, "active": self.active, "peak": self.peak,
                "rejected": self.rejected}


# Singletons shared by the recorder, the executor and the API.
browser_budget = BrowserBudget(MAX_CONCURRENT_BROWSERS)
live_frames = BoundedFrames(MAX_LIVE_FRAME_RUNS, LIVE_FRAME_TTL)
run_buffers = BoundedRunBuffers(MAX_RUN_BUFFER_RUNS, MAX_RUN_BUFFER_EVENTS)
batch_budget = BatchBudget(MAX_CONCURRENT_BATCHES)


def report() -> dict:
    """Snapshot for /api/diagnostics and /api/health."""
    return {
        "max_concurrent_browsers": MAX_CONCURRENT_BROWSERS,
        "browser": browser_budget.report(),
        "live_frames": live_frames.report(),
        "run_buffers": run_buffers.report(),
        "max_zip_bytes": MAX_ZIP_BYTES,
        "run_list_limit": DEFAULT_RUN_LIST_LIMIT,
        "batch": batch_budget.report(),
        "batch_max_recordings": MAX_BATCH_RECORDINGS,
        "batch_log_entries": BATCH_LOG_ENTRIES,
        "max_log_file_bytes": MAX_LOG_FILE_BYTES,
    }
