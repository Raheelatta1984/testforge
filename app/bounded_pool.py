"""A bounded, cancellation-fair worker pool for TestForge execution.

Everything TestForge runs is deliberate about resources: one Chromium per
instance, a queue in front of it, a memory guard inside a batch. Parallelism has
to fit inside that discipline or it is worse than useless on a 512MB instance,
so this module exists to make the *bounding* explicit and testable rather than
sprinkled through the callers:

* **Bounded width.** No more than ``width`` items run at once, and the width is
  clamped by :func:`pool_width` against a hard ceiling and whatever the shared
  browser budget can actually serve. Width 1 reproduces the historical
  sequential behaviour exactly.
* **Fair start order.** Items start in the order they were submitted. A slow
  item cannot starve the queue behind it, and a caller that asked for ten things
  sees the first one begin immediately.
* **Fair cancellation.** :meth:`BoundedPool.cancel` stops new items from
  starting, cancels what is in flight, and still returns one result per item so
  the caller can report every recording as cancelled instead of losing them.
* **Error isolation.** A worker that raises is reported per item. One failure
  never aborts the rest of the batch and never escapes as an exception.

Standard library only, and no browser import: the pool is exercised by unit
tests with ordinary coroutines.
"""
from __future__ import annotations

import asyncio

# A deployment is free to ask for more; the ceiling is what keeps a small
# instance alive. Raising it is an explicit, documented operator decision.
MAX_WIDTH = 32


def pool_width(requested=None, ceiling: int = MAX_WIDTH, budget: int | None = None,
               default: int = 1) -> int:
    """Clamp a requested width into ``[1, min(ceiling, budget)]``.

    ``budget`` is the shared resource budget (browser slots, worker slots). A
    budget of ``None`` means the caller owns no shared budget and only the
    ceiling applies. Non-numeric or negative input degrades to ``default``
    rather than raising: a mistyped environment variable must not stop a batch.
    """
    try:
        value = int(default if requested is None else requested)
    except (TypeError, ValueError):
        value = int(default)
    value = max(1, value)
    value = min(value, max(1, int(ceiling)))
    if budget is not None:
        value = min(value, max(1, int(budget)))
    return value


class BoundedPool:
    """Run ``worker(item)`` over ``items`` with at most ``width`` in flight."""

    def __init__(self, width: int = 1, *, name: str = "pool", ceiling: int = MAX_WIDTH):
        self.width = pool_width(width, ceiling=ceiling)
        self.name = name
        self.started = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self.peak = 0
        self._cancel = False

    # --- control -----------------------------------------------------------
    def cancel(self) -> None:
        """Stop starting work and cancel what is already running."""
        self._cancel = True

    @property
    def cancelling(self) -> bool:
        return self._cancel

    def report(self) -> dict:
        return {"name": self.name, "width": self.width, "started": self.started,
                "completed": self.completed, "failed": self.failed,
                "cancelled": self.cancelled, "peak": self.peak}

    # --- execution ---------------------------------------------------------
    async def map(self, items, worker):
        """Return one result per item, in submission order.

        Each result is ``{"ok": bool, "value": ..., "error": str|None,
        "cancelled": bool, "index": int}``. Nothing raises: a worker exception
        becomes ``ok=False`` with the redacted-safe exception text.
        """
        items = list(items)
        results: list[dict | None] = [None] * len(items)
        slots = asyncio.Semaphore(self.width)
        in_flight = 0

        async def guarded(index: int, item):
            nonlocal in_flight
            acquired = counted = False
            try:
                await slots.acquire()
                acquired = True
                if self._cancel:
                    return (index, {"ok": False, "value": None, "error": None,
                                    "cancelled": True, "index": index})
                self.started += 1
                in_flight += 1
                counted = True
                self.peak = max(self.peak, in_flight)
                value = await worker(item)
                return (index, {"ok": True, "value": value, "error": None,
                                "cancelled": False, "index": index})
            except asyncio.CancelledError:
                self.cancelled += 1
                return (index, {"ok": False, "value": None, "error": "cancelled",
                                "cancelled": True, "index": index})
            except Exception as exc:  # noqa: BLE001 - one bad item must not end the batch
                return (index, {"ok": False, "value": None,
                                "error": f"{type(exc).__name__}: {exc}",
                                "cancelled": False, "index": index})
            finally:
                if counted:
                    in_flight -= 1
                if acquired:
                    slots.release()

        # Every item gets a task immediately and waits on the semaphore: asyncio
        # wakes semaphore waiters first-in-first-out, so start order stays fair
        # without a scheduler of our own.
        tasks = {asyncio.ensure_future(guarded(index, item)) for index, item in enumerate(items)}
        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks = set(pending)
                for task in done:
                    index, payload = task.result()
                    results[index] = payload
                    if payload["cancelled"]:
                        self.cancelled += 1
                    elif payload["ok"]:
                        self.completed += 1
                    else:
                        self.failed += 1
                if self._cancel and tasks:
                    for task in tasks:
                        task.cancel()
        except asyncio.CancelledError:
            self._cancel = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return [row or {"ok": False, "value": None, "error": "cancelled",
                        "cancelled": True, "index": index}
                for index, row in enumerate(results)]


async def map_bounded(items, worker, width: int = 1, *, name: str = "pool",
                      ceiling: int = MAX_WIDTH) -> list[dict]:
    """Convenience wrapper: run one bounded pass and return the results."""
    pool = BoundedPool(width, name=name, ceiling=ceiling)
    return await pool.map(items, worker)
