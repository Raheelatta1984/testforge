"""The bounded worker pool that every execution unit passes through.

Runs and batches used to be bare ``asyncio.create_task`` calls. Nothing bounded
how many were in flight beyond the shared browser budget, nothing guaranteed a
fair start order, and cancelling "the queue" meant cancelling database rows and
hoping the tasks noticed. This module is the missing layer:

* **One gate for every unit.** A single run and a whole batch both take a slot,
  so the number of concurrent executions is one number an operator can set:
  ``TF_MAX_CONCURRENT_RUNS``, itself clamped by the shared browser budget
  (``TF_MAX_BROWSERS``) because every unit needs a browser.
* **Fair start order.** Slots are handed out first-come, first-served, so a run
  submitted first starts first instead of racing the event loop.
* **Fair cancellation.** :meth:`ExecutionPool.cancel_all` stops what the pool
  owns; work that has not started yet never starts.
* **Sequential by default.** The width defaults to 1, which is exactly the
  behaviour the hosted instance has always had. Parallelism is opt-in and only
  meaningful where the machine (and the browser budget) can pay for it.

No browser import, no database import: the pool only schedules awaitables.
"""
from __future__ import annotations

import asyncio
import os

from app import guardrails
from app.bounded_pool import pool_width

# Hard ceiling for the deployment knob. Two dozen concurrent Chromium processes
# on one host is not a testing tool, it is an outage with extra steps.
MAX_WORKERS = 32
DEFAULT_WIDTH = 1


def configured_width() -> int:
    """The width the operator asked for, clamped to what the machine can serve."""
    requested = os.environ.get("TF_MAX_CONCURRENT_RUNS", DEFAULT_WIDTH)
    return pool_width(requested, ceiling=MAX_WORKERS,
                      budget=guardrails.MAX_CONCURRENT_BROWSERS)


class ExecutionPool:
    """Bound concurrent execution units; report and cancel them as a group."""

    def __init__(self, width_fn=None, ceiling: int = MAX_WORKERS):
        self._width_fn = width_fn or configured_width
        self.ceiling = ceiling
        self._gate: asyncio.Semaphore | None = None
        self._gate_width = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task] = set()
        self._in_flight = 0
        self.started = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self.peak = 0

    # --- configuration -----------------------------------------------------
    def width(self) -> int:
        """Current width: the operator's request, clamped by the ceiling."""
        return pool_width(self._width_fn(), ceiling=self.ceiling,
                          budget=guardrails.MAX_CONCURRENT_BROWSERS)

    def _current_gate(self) -> asyncio.Semaphore:
        """The gate for this loop, rebuilt when the configured width changes.

        Tasks already waiting on an older gate keep it; the next submission uses
        the new one. Widths change when an operator edits the environment and the
        process is not restarted, which is rare and transient by definition.
        """
        loop = asyncio.get_event_loop()
        width = self.width()
        if self._gate is None or self._loop is not loop or self._gate_width != width:
            self._gate = asyncio.Semaphore(width)
            self._gate_width = width
            self._loop = loop
        return self._gate

    # --- submission --------------------------------------------------------
    def submit(self, work, *, name: str | None = None) -> asyncio.Task:
        """Schedule ``work`` (a coroutine or a zero-argument callable)."""
        gate = self._current_gate()

        async def guarded():
            await gate.acquire()
            self.started += 1
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
            try:
                await (work() if callable(work) else work)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            except Exception:  # noqa: BLE001 - the run/batch wrapper owns reporting
                self.failed += 1
            else:
                self.completed += 1
            finally:
                self._in_flight -= 1
                gate.release()

        task = asyncio.ensure_future(guarded())
        try:
            task.set_name(name or "execution")
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            pass
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- control -----------------------------------------------------------
    def live(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    def cancel_all(self) -> int:
        """Cancel every unit the pool owns. Returns how many were cancelled."""
        count = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                count += 1
        return count

    def report(self) -> dict:
        return {
            "width": self.width(),
            "ceiling": self.ceiling,
            "browser_budget": guardrails.MAX_CONCURRENT_BROWSERS,
            "live": self.live(),
            "in_flight": self._in_flight,
            "started": self.started,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "peak": self.peak,
        }


# The one pool the API submits runs and batches to.
execution_pool = ExecutionPool()
