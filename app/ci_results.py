"""Machine-readable results for CI: JUnit XML and a JSON summary.

Screenshots and a dashboard are for humans. A pipeline needs a file it already
understands, with a stable exit signal. This module renders stored runs as
JUnit XML (the format Jenkins, GitLab, GitHub Actions and Azure DevOps all
consume) without a new dependency and without trusting stored text: every value
is escaped, every list is bounded, and nothing from the execution log is emitted
unredacted.

Run status mapping is explicit rather than implicit:

===========  ==========================================================
stored       JUnit
===========  ==========================================================
passed       ``<testcase>`` with no child
failed/error ``<failure>`` (a product or automation failure)
cancelled    ``<skipped>`` with the recorded reason
queued/      ``<skipped>`` (it did not produce a verdict)
running
===========  ==========================================================

That distinction matters: a cancelled or queued run must never look like a pass
in a CI summary, and a failed run must never be silently downgraded to skipped.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

MAX_RUNS = 200
MAX_LOG_ENTRIES = 2000
MAX_TEXT = 20_000
FAILED = {"failed", "error", "healed"}
SKIPPED = {"cancelled", "queued", "running", "skipped"}

_UNSAFE_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(value, limit: int = MAX_TEXT) -> str:
    text = str(value if value is not None else "")
    text = _UNSAFE_XML.sub("", text)
    return text[:limit]


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    return str(value)


def _failure_text(run: dict) -> str:
    parts = []
    for entry in (run.get("execution_log") or [])[:MAX_LOG_ENTRIES]:
        if not isinstance(entry, dict) or entry.get("status") not in FAILED:
            continue
        label = entry.get("label") or entry.get("action") or "step"
        parts.append(f"{label}: {entry.get('error') or entry.get('diagnostic') or entry.get('status')}")
        if len(parts) >= 5:
            break
    if not parts:
        if run.get("status") == "healed":
            parts.append("A locator was healed; review the recording and rerun clean before release.")
        else:
            parts.append(run.get("error") or run.get("cancel_reason") or f"Run finished as {run.get('status')}")
    return _clean("\n".join(str(part) for part in parts), 4000)


def _system_out(run: dict) -> str:
    lines = []
    for entry in (run.get("execution_log") or [])[:MAX_LOG_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        if entry.get("stage") == "steps" or entry.get("action"):
            lines.append(f"[{entry.get('order', '-')}] {entry.get('action', '')} · {entry.get('label', '')} "
                         f"→ {entry.get('status', '')}"
                         + (f" ({entry.get('duration_ms')} ms)" if entry.get("duration_ms") is not None else ""))
    return _clean("\n".join(lines), MAX_TEXT)


def _run_testcase(run: dict, classname: str) -> list[str]:
    name = _clean(run.get("recording_name") or run.get("recording_id") or run.get("id") or "run", 300)
    status = str(run.get("status") or "unknown")
    time_value = run.get("elapsed_seconds")
    try:
        time_value = float(time_value) if time_value is not None else 0.0
    except (TypeError, ValueError):
        time_value = 0.0
    attrs = (f"classname={quoteattr(classname)} name={quoteattr(name)} "
             f"time={quoteattr(f'{time_value:.3f}')}")
    if run.get("batch_id"):
        attrs += f" batch={quoteattr(_clean(run['batch_id'], 80))}"
    out = [f"    <testcase {attrs}>"]
    if status in FAILED:
        message = _clean(run.get("error") or f"Run {status}", 500)
        out.append(f"      <failure message={quoteattr(message)} type={quoteattr(status)}>"
                   f"{escape(_failure_text(run))}</failure>")
    elif status in SKIPPED:
        reason = _clean(run.get("cancel_reason") or f"Run was {status} and produced no verdict", 500)
        out.append(f"      <skipped message={quoteattr(reason)}/>")
    system_out = _system_out(run)
    if system_out:
        out.append(f"      <system-out>{escape(system_out)}</system-out>")
    out.append("    </testcase>")
    return out


def junit_xml(runs, suite_name: str = "TestForge", batch_id: str | None = None) -> str:
    """Render runs as one JUnit ``<testsuites>`` document."""
    runs = [run for run in (runs or []) if isinstance(run, dict)][:MAX_RUNS]
    failures = sum(1 for run in runs if str(run.get("status")) in FAILED)
    skipped = sum(1 for run in runs if str(run.get("status")) in SKIPPED)
    total_time = 0.0
    for run in runs:
        try:
            total_time += float(run.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            continue
    classname = f"testforge.batch.{batch_id}" if batch_id else "testforge.run"
    stamp = _iso(datetime.now(timezone.utc))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        f'<testsuites name={quoteattr(_clean(suite_name, 200))} tests="{len(runs)}" '
        f'failures="{failures}" errors="0" skipped="{skipped}" '
        f'time="{total_time:.3f}" timestamp={quoteattr(stamp)}>')
    lines.append(
        f'  <testsuite name={quoteattr(_clean(suite_name, 200))} tests="{len(runs)}" '
        f'failures="{failures}" errors="0" skipped="{skipped}" '
        f'time="{total_time:.3f}" timestamp={quoteattr(stamp)}>')
    for run in runs:
        lines.extend(_run_testcase(run, classname))
    lines.append("  </testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines) + "\n"


def summary(runs, suite_name: str = "TestForge") -> dict:
    """Small JSON payload for a pipeline step or a badge."""
    runs = [run for run in (runs or []) if isinstance(run, dict)][:MAX_RUNS]
    counts = {"total": len(runs), "passed": 0, "failed": 0, "skipped": 0, "other": 0}
    for run in runs:
        status = str(run.get("status") or "")
        if status == "passed":
            counts["passed"] += 1
        elif status in FAILED:
            counts["failed"] += 1
        elif status in SKIPPED:
            counts["skipped"] += 1
        else:
            counts["other"] += 1
    verdict = "failed" if counts["failed"] else ("incomplete" if counts["skipped"] or counts["other"] else "passed")
    return {
        "schema": "testforge-ci-summary/1",
        "suite": _clean(suite_name, 200),
        "generated_at": _iso(datetime.now(timezone.utc)),
        "counts": counts,
        "verdict": verdict,
        "exit_code": 1 if verdict == "failed" else (2 if verdict == "incomplete" else 0),
        "runs": [{"id": run.get("id"), "name": _clean(run.get("recording_name"), 300),
                  "status": run.get("status"), "batch_id": run.get("batch_id"),
                  "elapsed_seconds": run.get("elapsed_seconds")} for run in runs],
        "notes": [
            "A queued, running or cancelled run is 'incomplete', not a pass.",
            "'other' counts a status this version does not know; the pipeline should treat it as incomplete.",
        ],
    }


# ---------------------------------------------------------------------------
# Data access for the routes


def _shape_run(run) -> dict:
    log = list(run.execution_log or [])[:MAX_LOG_ENTRIES]
    recording = getattr(run, "recording", None)
    elapsed = None
    if run.started_at is not None:
        end = run.finished_at or datetime.now(timezone.utc).replace(tzinfo=None)
        start = run.started_at.replace(tzinfo=None) if run.started_at.tzinfo else run.started_at
        finished = end.replace(tzinfo=None) if getattr(end, "tzinfo", None) else end
        try:
            elapsed = max(0.0, round((finished - start).total_seconds(), 3))
        except TypeError:
            elapsed = None
    return {
        "id": run.id,
        "recording_id": run.recording_id,
        "recording_name": recording.name if recording else "",
        "status": run.status,
        "batch_id": run.batch_id,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "elapsed_seconds": elapsed,
        "cancel_reason": run.cancel_reason,
        "execution_log": log,
    }


def load_runs(run_id: str | None = None, batch_id: str | None = None) -> list[dict]:
    """Read runs from the application database. Raises LookupError when absent."""
    from app.db import Run, SessionLocal

    with SessionLocal() as db:
        if run_id:
            row = db.get(Run, run_id)
            if row is None:
                raise LookupError("Run not found")
            return [_shape_run(row)]
        if batch_id:
            rows = (db.query(Run).filter(Run.batch_id == batch_id)
                    .order_by(Run.created_at.asc()).limit(MAX_RUNS).all())
            if not rows:
                raise LookupError("No runs found for that batch")
            return [_shape_run(row) for row in rows]
    raise LookupError("Provide a run or batch id")


def as_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2)
