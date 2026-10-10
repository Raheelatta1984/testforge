"""Run-queue hygiene: orphans, staleness, duplicates and explicit clearing.

The queue used to be write-only. `POST /api/runs` inserted a row with
``status="queued"`` and handed the work to an asyncio task; nothing ever revisited
that row. Two things followed:

* A restart (a deploy, an OOM kill, a crash) leaves every queued and running row
  exactly as it was. No task exists any more, so those runs can never finish, yet
  the dashboard counts them as pending forever. They are *orphans*.
* Pressing RUN twice queues the same recording twice, and with a browser budget of
  one the second run waits behind the first for no reason.

This module owns the answer to "what is actually still going to run?". It is plain
synchronous database work with no Playwright import, so the API, the boot hook and
the periodic sweeper all share one implementation and the unit tests can call it
directly.
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta

from app.config import logger
from app.db import Run, SessionLocal

# --- Tunables ---------------------------------------------------------------
# A run that has no worker of its own is only an orphan once it is older than this,
# because the row is committed a moment before the task is created.
ORPHAN_GRACE_SECONDS = float(os.environ.get("TF_QUEUE_ORPHAN_GRACE", "120") or 120)
# Queued for longer than this without starting means it is behind something that
# will not give up the browser. It is reported as stale so it can be cleared.
STALE_MINUTES = float(os.environ.get("TF_QUEUE_STALE_MINUTES", "30") or 30)
# Finished runs are history. They stay for this long unless purged explicitly.
RETENTION_DAYS = float(os.environ.get("TF_RUN_RETENTION_DAYS", "14") or 14)
# How often the background sweeper reaps orphans.
SWEEP_SECONDS = float(os.environ.get("TF_QUEUE_SWEEP_SECONDS", "300") or 300)

UNFINISHED = ("queued", "running", "investigating")
LOCK = threading.RLock()

# run_id -> the worker that owns it. Bounded, and entries are removed when the
# worker finishes, so a long-lived process does not accumulate one per run.
_LIVE: "dict[str, object]" = {}
_LIVE_MAX = 500


def register(run_id: str, worker: object) -> None:
    """Claim a run for this process. Only a claimed run may stay unfinished."""
    with LOCK:
        _LIVE[run_id] = worker
        if len(_LIVE) > _LIVE_MAX:
            for stale in list(_LIVE)[: len(_LIVE) - _LIVE_MAX]:
                _LIVE.pop(stale, None)


def unregister(run_id: str) -> None:
    with LOCK:
        _LIVE.pop(run_id, None)


def is_live(run_id: str) -> bool:
    """True while a worker in *this* process still owns the run."""
    with LOCK:
        worker = _LIVE.get(run_id)
    if worker is None:
        return False
    done = getattr(worker, "done", None)
    if callable(done):
        try:
            return not done()
        except Exception:
            return True
    return True


def live_run_ids() -> list[str]:
    with LOCK:
        return list(_LIVE)


def _utcnow() -> datetime:
    return datetime.utcnow()


def _age_seconds(row: Run, now: datetime | None = None) -> float:
    """Age of a run in seconds. Naive timestamps in this schema are UTC."""
    now = now or _utcnow()
    created = row.created_at
    if created is None:
        return 0.0
    if created.tzinfo is not None:
        created = created.replace(tzinfo=None)
    age = (now - created).total_seconds()
    return max(0.0, age)


def _classify(row: Run, now: datetime) -> tuple[str, str] | None:
    """Why this unfinished run is a candidate for clearing, or None to keep it."""
    age = _age_seconds(row, now)
    if not is_live(row.id):
        if age >= ORPHAN_GRACE_SECONDS or row.status == "running":
            return "orphan", (
                "No worker in this process owns it. It was queued before the "
                "service restarted and can never finish."
            )
        return None
    if row.status == "queued" and age >= STALE_MINUTES * 60:
        return "stale", f"Queued for {int(age // 60)} minutes without starting."
    return None


def snapshot(limit: int = 50) -> dict:
    """What the queue looks like right now, for the dashboard and the API."""
    now = _utcnow()
    with SessionLocal() as db:
        unfinished = db.query(Run).filter(Run.status.in_(UNFINISHED)).all()
        queued = [row for row in unfinished if row.status == "queued"]
        running = [row for row in unfinished if row.status == "running"]
        orphans, stale = [], []
        for row in unfinished:
            verdict = _classify(row, now)
            if verdict is None:
                continue
            item = {
                "id": row.id,
                "recording_id": row.recording_id,
                "status": row.status,
                "age_seconds": round(_age_seconds(row, now), 1),
                "kind": verdict[0],
                "reason": verdict[1],
            }
            (orphans if verdict[0] == "orphan" else stale).append(item)
        seen: dict[str, int] = {}
        for row in queued:
            seen[row.recording_id] = seen.get(row.recording_id, 0) + 1
        duplicates = sum(count - 1 for count in seen.values() if count > 1)
        oldest = max((_age_seconds(row, now) for row in queued), default=0.0)
        listed = sorted(unfinished, key=lambda row: row.created_at or now, reverse=True)[:limit]
        return {
            "queued": len(queued),
            "running": len(running),
            "pending": len(unfinished),
            "orphans": len(orphans),
            "stale": len(stale),
            "clearable": len(orphans) + len(stale),
            "duplicates": duplicates,
            "oldest_queued_seconds": round(oldest, 1),
            "live_workers": len(live_run_ids()),
            "items": [
                {
                    "id": row.id,
                    "recording_id": row.recording_id,
                    "status": row.status,
                    "age_seconds": round(_age_seconds(row, now), 1),
                    "live": is_live(row.id),
                }
                for row in listed
            ],
            "orphan_items": orphans[:limit],
            "stale_items": stale[:limit],
            "policy": {
                "orphan_grace_seconds": ORPHAN_GRACE_SECONDS,
                "stale_minutes": STALE_MINUTES,
                "retention_days": RETENTION_DAYS,
                "sweep_seconds": SWEEP_SECONDS,
            },
        }


def find_clearable(*, older_than_minutes: float | None = None,
                   include_orphans: bool = True, include_stale: bool = True,
                   include_duplicates: bool = False, cancel_running: bool = False,
                   limit: int = 500) -> list[dict]:
    """Unfinished runs that should leave the queue, each with the reason why.

    Nothing is written here, so the API can offer a dry run with the exact same
    answer the real clear would act on.
    """
    now = _utcnow()
    floor_seconds = 0.0 if older_than_minutes is None else max(0.0, float(older_than_minutes) * 60)
    found: list[dict] = []
    with SessionLocal() as db:
        statuses = list(UNFINISHED) if cancel_running else ["queued", "investigating"]
        rows = db.query(Run).filter(Run.status.in_(statuses)).all()
        first_queued: dict[str, str] = {}
        for row in sorted(rows, key=lambda item: item.created_at or now):
            if row.status == "queued":
                first_queued.setdefault(row.recording_id, row.id)
        for row in rows:
            age = _age_seconds(row, now)
            if age < floor_seconds:
                continue
            verdict = _classify(row, now)
            kind = verdict[0] if verdict else None
            reason = verdict[1] if verdict else None
            if kind is None and include_duplicates and row.status == "queued":
                if first_queued.get(row.recording_id) not in (None, row.id):
                    kind, reason = "duplicate", (
                        f"An older queued run for the same recording is already waiting "
                        f"({first_queued[row.recording_id][:8]})."
                    )
            if kind is None:
                continue
            if kind == "orphan" and not include_orphans:
                continue
            if kind == "stale" and not include_stale:
                continue
            if row.status == "running" and not cancel_running:
                continue
            found.append({
                "id": row.id,
                "recording_id": row.recording_id,
                "status": row.status,
                "age_seconds": round(age, 1),
                "kind": kind,
                "reason": reason,
            })
            if len(found) >= limit:
                break
    return found


def cancel(run_ids: list[str], reason: str, status: str = "cancelled") -> int:
    """Move runs out of the queue. Returns how many rows changed."""
    if not run_ids:
        return 0
    now = _utcnow()
    changed = 0
    with SessionLocal() as db:
        for row in db.query(Run).filter(Run.id.in_(list(run_ids))).all():
            if row.status not in UNFINISHED:
                continue
            worker = _LIVE.get(row.id)
            row.status = status
            row.cancel_reason = reason[:500]
            row.finished_at = now
            db.commit()
            changed += 1
            # A worker that is still alive must be told to stop, otherwise the
            # row says cancelled while a browser keeps replaying it.
            cancel_it = getattr(worker, "cancel", None)
            if callable(cancel_it):
                try:
                    cancel_it()
                except Exception as exc:
                    logger.warning("QUEUE CANCEL WORKER FAILED %s %s", row.id, exc)
    return changed


def purge_finished(*, older_than_days: float | None = None, keep_recent: int = 0,
                   limit: int = 1000) -> dict:
    """Delete finished run history. Bounded so one call cannot lock the database."""
    days = RETENTION_DAYS if older_than_days is None else float(older_than_days)
    cutoff = _utcnow() - timedelta(days=max(0.0, days))
    with SessionLocal() as db:
        query = db.query(Run).filter(Run.status.notin_(list(UNFINISHED)), Run.created_at < cutoff)
        rows = query.order_by(Run.created_at.asc()).limit(limit).all()
        ids = [row.id for row in rows]
        for row in rows:
            db.delete(row)
        db.commit()
        remaining = db.query(Run).count()
    return {"purged": len(ids), "cutoff": cutoff.isoformat() + "Z", "runs_left": remaining, "run_ids": ids[:50]}


def clear(*, older_than_minutes: float | None = None, include_orphans: bool = True,
          include_stale: bool = True, include_duplicates: bool = False,
          cancel_running: bool = False, purge_finished_days: float | None = None,
          reason: str | None = None, dry_run: bool = False) -> dict:
    """One call that answers "clear the irrelevant and old queue entries"."""
    candidates = find_clearable(
        older_than_minutes=older_than_minutes,
        include_orphans=include_orphans,
        include_stale=include_stale,
        include_duplicates=include_duplicates,
        cancel_running=cancel_running,
    )
    text = reason or _default_reason(candidates)
    report: dict = {
        "dry_run": bool(dry_run),
        "candidates": candidates,
        "matched": len(candidates),
        "cancelled": 0,
        "reason": text,
        "by_kind": {},
    }
    for item in candidates:
        report["by_kind"][item["kind"]] = report["by_kind"].get(item["kind"], 0) + 1
    if not dry_run:
        report["cancelled"] = cancel([item["id"] for item in candidates], text)
    if purge_finished_days is not None and not dry_run:
        report["purge"] = purge_finished(older_than_days=purge_finished_days)
    elif purge_finished_days is not None:
        with SessionLocal() as db:
            cutoff = _utcnow() - timedelta(days=max(0.0, float(purge_finished_days)))
            report["purge"] = {
                "purged": 0,
                "would_purge": db.query(Run).filter(
                    Run.status.notin_(list(UNFINISHED)), Run.created_at < cutoff
                ).count(),
                "cutoff": cutoff.isoformat() + "Z",
                "dry_run": True,
            }
    report["queue"] = snapshot(limit=10)
    return report


def _default_reason(candidates: list[dict]) -> str:
    kinds = sorted({item["kind"] for item in candidates})
    if not kinds:
        return "Cleared from the execution queue."
    return "Cleared from the execution queue (" + ", ".join(kinds) + ")."


def reap_orphans(reason: str | None = None) -> dict:
    """Cancel runs nobody can finish any more. Called at boot and by the sweeper."""
    text = reason or (
        "Cancelled by queue hygiene: the service restarted while this run was "
        "queued, so no worker owns it."
    )
    candidates = find_clearable(include_orphans=True, include_stale=True, cancel_running=True)
    ids = [item["id"] for item in candidates]
    cancelled = cancel(ids, text) if ids else 0
    if cancelled:
        logger.info("QUEUE REAPED %s run(s): %s", cancelled, ", ".join(item["kind"] for item in candidates[:10]))
    return {"cancelled": cancelled, "run_ids": ids[:50], "reason": text}


def duplicate_of(recording_id: str) -> dict | None:
    """An unfinished, not-yet-started queued run for the same recording."""
    with SessionLocal() as db:
        row = (
            db.query(Run)
            .filter(Run.recording_id == recording_id, Run.status == "queued", Run.started_at.is_(None))
            .order_by(Run.created_at.asc())
            .first()
        )
        if row is None:
            return None
        return {"run_id": row.id, "status": row.status, "age_seconds": round(_age_seconds(row), 1)}


_sweeper: asyncio.Task | None = None


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await asyncio.to_thread(reap_orphans)
        except Exception as exc:
            logger.warning("QUEUE SWEEP FAILED: %s", exc)


def start_sweeper() -> bool:
    """Start the periodic reaper once per process. Returns True if it started."""
    global _sweeper
    if SWEEP_SECONDS <= 0:
        return False
    if _sweeper is not None and not _sweeper.done():
        return False
    try:
        _sweeper = asyncio.get_running_loop().create_task(_sweep_loop())
    except RuntimeError:
        return False
    return True
