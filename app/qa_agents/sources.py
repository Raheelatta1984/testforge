"""Build agent inputs from what TestForge already stores: runs, recordings and projects.

Reads only. Nothing here starts a browser or writes to the library. Every list
is bounded so a large project cannot make a request expensive.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app import library_store
from app.db import Recording, Run
from app.errors import redact

MAX_RUNS = 500
MAX_RECORDINGS = 300
MAX_STEPS = 80
ERROR_FIELDS = ("error", "message", "detail")


class SourceNotFound(LookupError):
    pass


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    return str(value)


def _tags(raw) -> list[str]:
    return [t for t in re.split(r"[,\s]+", str(raw or "")) if t][:20]


def _failed_entries(log: list) -> list[dict]:
    out = []
    for entry in log or []:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").lower()
        if status in ("failed", "error") or entry.get("error"):
            out.append(entry)
    return out


def _entry_error(entry: dict) -> str:
    for key in ERROR_FIELDS:
        if entry.get(key):
            return redact(str(entry[key]))
    result = entry.get("result")
    if isinstance(result, dict) and result.get("error"):
        return redact(str(result["error"]))
    return ""


def run_context(db, run_id: str) -> dict:
    run = db.get(Run, run_id)
    if run is None:
        raise SourceNotFound("Run not found")
    recording = run.recording
    log = list(run.execution_log or [])
    failed = _failed_entries(log)
    failed_ids = {id(entry) for entry in failed}
    cut = next((i for i, e in enumerate(log) if id(e) in failed_ids), len(log))
    before = [e for e in log[:cut] if isinstance(e, dict)]
    return {
        "id": run.id, "status": run.status, "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "recording_id": run.recording_id,
        "recording_name": recording.name if recording else "",
        "recording_purpose": (recording.purpose or "") if recording else "",
        "start_url": (recording.start_url or "") if recording else "",
        "tags": _tags(recording.tags) if recording else [],
        "failed": [{"label": redact(str(e.get("label") or e.get("action") or ""))[:300],
                    "action": str(e.get("action") or "")[:60],
                    "error": _entry_error(e)[:800],
                    "screenshot": str(e.get("screenshot") or "")[-120:]} for e in failed[:20]],
        "steps_before_failure": [{"action": str(e.get("action") or ""), "label": redact(str(e.get("label") or ""))[:300],
                                  "value": redact(str(e.get("value") or ""))[:200] if str(e.get("action") or "").lower()
                                  not in ("fill", "type", "input") else "<entered value>"}
                                 for e in before if e.get("action")][-30:],
        "log_tail": [redact(str(e.get("label") or e.get("action") or "")) + " -> " + str(e.get("status") or "")
                     for e in log[-25:] if isinstance(e, dict)],
    }


def triage_payload(db, run_id: str) -> dict:
    ctx = run_context(db, run_id)
    errors = [item["error"] or item["label"] for item in ctx["failed"] if (item["error"] or item["label"])]
    if not errors:
        errors = [f"Run finished with status {ctx['status']} and no failing step was recorded."]
    return {"errors": errors, "context": {"run_id": ctx["id"], "recording": ctx["recording_name"],
                                          "step": (ctx["failed"][0]["label"] if ctx["failed"] else "")}}


def defect_payload(db, run_id: str) -> dict:
    ctx = run_context(db, run_id)
    first = ctx["failed"][0] if ctx["failed"] else {"label": "", "error": "", "screenshot": ""}
    steps = [s["label"] or s["action"] for s in ctx["steps_before_failure"]]
    if first.get("label"):
        steps.append(first["label"])
    return {
        "title": "", "recording_name": ctx["recording_name"], "run_id": ctx["id"], "steps": steps,
        "expected": (ctx["recording_purpose"] or "The recorded step completes as it did when recorded."),
        "actual": (first.get("error") or f"Run ended with status {ctx['status']}.").splitlines()[0][:400],
        "error": first.get("error") or "", "logs": "\n".join(ctx["log_tail"]),
        "environment": {"url": ctx["start_url"], "execution": "TestForge browser run",
                        "run_started": ctx["started_at"] or ""},
        "evidence": [first["screenshot"]] if first.get("screenshot") else [],
    }


def flaky_payload(db, recording_id: str, limit: int = 100) -> dict:
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise SourceNotFound("Recording not found")
    limit = max(1, min(int(limit or 100), 200))
    rows = (db.query(Run).filter(Run.recording_id == recording_id, Run.status.in_(["passed", "failed", "error"]))
            .order_by(Run.created_at.desc()).limit(limit).all())
    runs = []
    for run in reversed(rows):
        log = list(run.execution_log or [])
        failed = _failed_entries(log)
        duration = None
        if run.started_at and run.finished_at:
            duration = round((run.finished_at - run.started_at).total_seconds() * 1000, 1)
        runs.append({"id": run.id, "status": run.status, "started_at": _iso(run.started_at or run.created_at),
                     "duration_ms": duration,
                     "error": _entry_error(failed[0]) if failed else ""})
    return {"runs": runs, "name": recording.name}


def _recording_tests(project_id: str) -> list[dict]:
    tests = []
    for row in library_store.list_recordings(project_id)[:MAX_RECORDINGS]:
        detail = library_store.get_recording(row.get("id")) or {}
        labels = [str(s.get("label") or s.get("action") or "")[:200] for s in (detail.get("steps") or [])[:MAX_STEPS]]
        tests.append({"id": row.get("id"), "name": row.get("name") or "", "tags": _tags(row.get("tags")),
                      "start_url": row.get("start_url") or "", "labels": labels})
    return tests


def regression_payload(project_id: str) -> dict:
    return {"tests": _recording_tests(project_id)}


def release_payload(db, project_id: str, since: str | None = None, until: str | None = None,
                    batch_id: str | None = None) -> dict:
    try:
        library_store.list_recordings(project_id)  # raises NotFound for an unknown project
    except library_store.NotFound:
        raise SourceNotFound("Project not found") from None
    query = (db.query(Run, Recording).join(Recording, Run.recording_id == Recording.id)
             .filter(Recording.project_id == project_id))
    if batch_id:
        query = query.filter(Run.batch_id == batch_id)
    if since:
        query = query.filter(Run.created_at >= _parse_day(since))
    if until:
        query = query.filter(Run.created_at <= _parse_day(until, end=True))
    rows = query.order_by(Run.created_at.desc()).limit(MAX_RUNS).all()
    runs = []
    for run, recording in reversed(rows):
        log = list(run.execution_log or [])
        failed = _failed_entries(log)
        tags = _tags(recording.tags)
        runs.append({"id": run.id, "recording_id": recording.id, "recording_name": recording.name,
                     "status": run.status, "created_at": _iso(run.created_at), "tags": tags,
                     "critical": bool({"smoke", "critical", "p1"} & {t.lower() for t in tags}),
                     "error": _entry_error(failed[0]) if failed else ""})
    return {"runs": runs, "window": " to ".join(x for x in (since, until) if x) or "all runs"}


def _parse_day(text: str, end: bool = False) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(text)[:19] if len(str(text)) > 10 else str(text)[:10])
    except ValueError:
        raise ValueError("Dates must be ISO format, for example 2026-10-01") from None
    if end and len(str(text)) <= 10:
        stamp = stamp.replace(hour=23, minute=59, second=59)
    return stamp
