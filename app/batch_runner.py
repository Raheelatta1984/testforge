"""Adaptive batch execution: many recordings, one browser, as little CPU as possible.

Running recordings one at a time pays the same fixed costs over and over: a
Chromium launch (~1-2s and ~120MB), a fresh context, a cold cache, and a full-size
screenshot after every single step whether anybody looks at it or not. On a 512MB
instance that is what makes a ten-recording suite feel slow and heavy.

A batch pays those costs once:

* **One browser for the whole batch.** A single slot of the shared browser budget is
  held for the batch, so recording and single runs still cannot open a second
  Chromium. Per-recording contexts are closed as soon as the recording finishes,
  which keeps resident memory flat instead of climbing.
* **Learned step budgets.** :class:`AdaptivePacer` records how long each kind of
  step actually took and derives the next timeout from that (median x safety,
  clamped). A click that always lands in 120ms is not given three seconds to fail,
  so a broken recording fails fast instead of stalling the batch; a slow one keeps
  the budget it needs. The profile survives restarts in ``artifacts/batches``.
* **Origin grouping and a shared session.** Recordings that start on the same host
  run back to back in one context, so a login performed by the first recording is
  still valid for the rest. Shortest-first inside a group gives feedback early.
* **Screenshots only when they are worth something.** The default batch mode
  captures a PNG when a step fails; ``changes`` captures only when the page moved;
  ``none`` captures nothing. Step screenshots are the single largest CPU and disk
  cost of a replay.
* **One escalated retry for transient failures.** A timeout or a network error is
  retried once with a relaxed budget, and only for that step. A missing selector is
  a real failure and is not retried, because retrying it just costs time.
* **A memory circuit breaker.** Resident memory is sampled between recordings; past
  ``TF_BATCH_MAX_RSS_MB`` the browser is closed and relaunched to hand the memory
  back before the platform kills the service.

Nothing here imports a second automation stack: steps are replayed by
``app.executor.replay_step``, the same function a single run uses.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from app import guardrails
from app.config import ARTIFACTS, logger
from app.db import Batch, Recording, Run, SessionLocal, resolve_variables
from app.errors import redact
from app.executor import _expand_steps_with_repeat, _persist_shot, _step_dict, replay_step
from app.browser import explain_launch_error, launch_kwargs
from app.guardrails import browser_budget
from app.library_store import playback_url

# --- Tunables ---------------------------------------------------------------
SCREENSHOT_MODES = ("none", "failure", "changes", "all")
DEFAULT_SCREENSHOTS = os.environ.get("TF_BATCH_SCREENSHOTS", "failure").strip().lower()
if DEFAULT_SCREENSHOTS not in SCREENSHOT_MODES:
    DEFAULT_SCREENSHOTS = "failure"
MAX_BATCH_RECORDINGS = guardrails.MAX_BATCH_RECORDINGS
MAX_LOG_ENTRIES = guardrails.BATCH_LOG_ENTRIES
MAX_RSS_MB = float(os.environ.get("TF_BATCH_MAX_RSS_MB", "420") or 420)
BATCH_DIR = os.path.join(ARTIFACTS, "batches")
PACING_FILE = os.path.join(BATCH_DIR, "pacing.json")
PROFILE_MAX_KEYS = 400

DEFAULT_BUDGETS = {"navigate_ms": 20000, "action_ms": 3000, "type_delay_ms": 5}
CEILINGS = {"navigate_ms": 30000, "action_ms": 10000}
FLOORS = {"navigate_ms": 1500, "action_ms": 350}

# Failures worth one retry with a relaxed budget.
TRANSIENT_KINDS = {"timeout", "navigation"}

_CANCELLED: set[str] = set()


# --- Failure triage ---------------------------------------------------------
def classify_failure(text: str) -> str:
    """Bucket an error so the batch knows whether a retry could possibly help."""
    message = (text or "").lower()
    if not message:
        return "unknown"
    if "unsupported action" in message or "no selector and no coordinates" in message:
        return "recording"
    if "timeout" in message or "exceeded" in message or "waiting for" in message:
        return "timeout"
    if "net::" in message or "err_" in message or "navigation" in message or "dns" in message:
        return "navigation"
    if "not visible" in message or "not found" in message or "no element" in message or "strict mode" in message:
        return "selector"
    if "target page" in message or "browser has been closed" in message or "crashed" in message:
        return "browser"
    return "other"


# --- Learned pacing ---------------------------------------------------------
class AdaptivePacer:
    """Turn observed step durations into the next step's timeout budget.

    The estimate is an exponential moving average multiplied by a safety factor,
    clamped between a floor (never so tight that a healthy click fails) and the
    hard ceiling the single-run path already uses (never looser than today).
    """

    SAFETY = float(os.environ.get("TF_PACER_SAFETY", "4") or 4)
    ALPHA = 0.35

    def __init__(self, path: str | None = PACING_FILE, profile: dict | None = None):
        self.path = path
        self.profile: dict[str, dict] = dict(profile or {})
        self.hits = 0
        self.misses = 0
        self.escalations = 0
        if profile is None and path:
            self.load()

    # --- persistence --------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
            profile = raw.get("profile") if isinstance(raw, dict) else None
            if isinstance(profile, dict):
                self.profile = {str(key): value for key, value in list(profile.items())[:PROFILE_MAX_KEYS]
                                if isinstance(value, dict)}
        except (OSError, ValueError):
            self.profile = {}

    def save(self) -> bool:
        if not self.path:
            # An in-memory pacer (unit tests, a read-only image) has nowhere to
            # write, and that is not an error.
            return False
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # Bounded: oldest entries go first, so the profile cannot grow forever.
            items = sorted(self.profile.items(), key=lambda kv: kv[1].get("seen", 0), reverse=True)
            payload = {
                "version": 1,
                "saved_at": datetime.datetime.utcnow().isoformat() + "Z",
                "profile": dict(items[:PROFILE_MAX_KEYS]),
            }
            Path(self.path).write_text(json.dumps(payload), encoding="utf-8")
            return True
        except OSError as exc:
            logger.warning("PACING PROFILE NOT SAVED: %s", exc)
            return False

    # --- keys and budgets ---------------------------------------------------
    @staticmethod
    def key(action: str, selector: dict | None) -> str:
        selector = selector if isinstance(selector, dict) else {}
        target = selector.get("primary") or ""
        if not target:
            target = f"@{selector.get('x')},{selector.get('y')}" if selector.get("x") is not None else "-"
        return f"{(action or 'step').lower()}:{str(target)[:80]}"

    def _estimate(self, key: str, fallback: int, floor: int, ceiling: int) -> int:
        entry = self.profile.get(key)
        if not entry or not entry.get("n"):
            return fallback
        ema = float(entry.get("ms") or 0)
        if ema <= 0:
            return fallback
        budget = int(ema * self.SAFETY)
        return max(floor, min(ceiling, budget))

    def budgets_for(self, action: str, selector: dict | None) -> dict:
        """Timeouts for one step, learned where there is history, default otherwise."""
        action = (action or "").lower()
        key = self.key(action, selector)
        if action == "navigate":
            navigate = self._estimate(key, DEFAULT_BUDGETS["navigate_ms"],
                                      FLOORS["navigate_ms"], CEILINGS["navigate_ms"])
            return {"navigate_ms": navigate, "action_ms": DEFAULT_BUDGETS["action_ms"],
                    "type_delay_ms": DEFAULT_BUDGETS["type_delay_ms"], "learned": key in self.profile}
        action_ms = self._estimate(key, DEFAULT_BUDGETS["action_ms"],
                                   FLOORS["action_ms"], CEILINGS["action_ms"])
        return {"navigate_ms": DEFAULT_BUDGETS["navigate_ms"], "action_ms": action_ms,
                "type_delay_ms": DEFAULT_BUDGETS["type_delay_ms"], "learned": key in self.profile}

    @staticmethod
    def escalate(budgets: dict) -> dict:
        """Relax a budget for the one automatic retry of a transient failure.

        Tripling is bounded by the same ceiling the single-run path uses, so a
        retry can never wait longer than a normal run would.
        """
        relaxed = dict(budgets)
        for name in ("navigate_ms", "action_ms"):
            ceiling = CEILINGS.get(name, DEFAULT_BUDGETS[name] * 3)
            relaxed[name] = int(min(ceiling, max(int(budgets.get(name, 0)) * 3,
                                                 DEFAULT_BUDGETS[name])))
        relaxed["type_delay_ms"] = max(int(budgets.get("type_delay_ms", 5)), 15)
        relaxed["escalated"] = True
        return relaxed

    def observe(self, action: str, selector: dict | None, elapsed_ms: float, ok: bool) -> None:
        key = self.key(action, selector)
        entry = self.profile.get(key) or {"ms": 0.0, "n": 0, "fails": 0, "seen": 0}
        previous = float(entry.get("ms") or 0)
        entry["ms"] = round(elapsed_ms if not entry.get("n") else
                            previous * (1 - self.ALPHA) + max(0.0, elapsed_ms) * self.ALPHA, 1)
        entry["n"] = int(entry.get("n") or 0) + 1
        if not ok:
            entry["fails"] = int(entry.get("fails") or 0) + 1
        entry["seen"] = time.time()
        self.profile[key] = entry
        if ok:
            self.hits += 1
        else:
            self.misses += 1

    def seed_from_history(self, runs: list[dict]) -> int:
        """Warm the profile from finished runs already in the database.

        Execution log entries carry the step and, when a previous batch timed them,
        a ``ms`` value. Seeding means the first batch on a fresh deployment is not
        blind, and a redeploy does not throw away what was learned.
        """
        seeded = 0
        for run in runs:
            for entry in run.get("execution_log") or []:
                elapsed = entry.get("ms")
                if not isinstance(elapsed, (int, float)) or elapsed <= 0:
                    continue
                self.observe(entry.get("action") or "step", entry.get("selector"),
                             float(elapsed), entry.get("status") == "passed")
                seeded += 1
        return seeded

    def report(self) -> dict:
        learned = {key: value for key, value in self.profile.items() if value.get("n")}
        return {
            "keys": len(self.profile),
            "learned": len(learned),
            "hits": self.hits,
            "misses": self.misses,
            "escalations": self.escalations,
            "safety": self.SAFETY,
            "slowest": sorted(
                ({"key": key, "ms": value.get("ms"), "runs": value.get("n")}
                 for key, value in learned.items()),
                key=lambda item: item["ms"] or 0, reverse=True,
            )[:5],
        }


# --- Planning ---------------------------------------------------------------
def origin_of(url: str) -> str:
    """Scheme+host+port of a start URL, so same-origin recordings can share a session."""
    raw = playback_url(url or "")
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "unknown"
    if not parsed.netloc:
        return "local"
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def plan_order(items: list[dict]) -> list[dict]:
    """Group recordings by origin, shortest first inside a group.

    Grouping keeps one warm context per host; shortest-first means the first
    results land quickly instead of after the longest recording in the batch.
    """
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = origin_of(item.get("start_url") or "")
        groups.setdefault(key, []).append(item)
    ordered: list[dict] = []
    for key in sorted(groups, key=lambda name: (-len(groups[name]), name)):
        ordered.extend(sorted(groups[key], key=lambda item: (item.get("step_count") or 0,
                                                             item.get("name") or "")))
    return ordered


# --- Resource sampling ------------------------------------------------------
def sample_resources() -> dict:
    """Resident memory and CPU time of this process. Two /proc reads, no psutil."""
    sample: dict = {"rss_mb": None, "cpu_seconds": None}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    sample["rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                    break
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/self/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().split()
        ticks = os.sysconf("SC_CLK_TCK") or 100
        sample["cpu_seconds"] = round((int(fields[13]) + int(fields[14])) / ticks, 2)
    except (OSError, ValueError, IndexError):
        pass
    return sample


def _now():
    return datetime.datetime.utcnow()


def _save_batch(batch_id: str, **fields) -> None:
    with SessionLocal() as db:
        row = db.get(Batch, batch_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()


def _save_run(run_id: str, **fields) -> None:
    with SessionLocal() as db:
        row = db.get(Run, run_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()


def request_cancel(batch_id: str) -> None:
    """Ask a running batch to stop after the recording it is on."""
    _CANCELLED.add(batch_id)
    if len(_CANCELLED) > 50:
        for stale in list(_CANCELLED)[: len(_CANCELLED) - 50]:
            _CANCELLED.discard(stale)


def cancel_requested(batch_id: str) -> bool:
    return batch_id in _CANCELLED


def load_batch_runs(batch_id: str) -> list[dict]:
    """The runs of a batch with everything the replay needs, in one database pass."""
    with SessionLocal() as db:
        runs = db.query(Run).filter(Run.batch_id == batch_id).all()
        items = []
        for run in runs:
            recording = db.get(Recording, run.recording_id)
            if recording is None:
                items.append({
                    "run_id": run.id, "recording_id": run.recording_id, "name": None,
                    "start_url": None, "project_id": None, "steps": [], "missing": True,
                })
                continue
            items.append({
                "run_id": run.id,
                "recording_id": recording.id,
                "name": recording.name,
                "start_url": recording.start_url,
                "project_id": recording.project_id,
                "steps": [_step_dict(step) for step in recording.steps],
                "variables": resolve_variables(db, recording.project_id),
                "missing": False,
            })
        return items


def write_report(batch_id: str, report: dict) -> str | None:
    """Persist the batch report next to the run artifacts, and prune old ones."""
    try:
        os.makedirs(BATCH_DIR, exist_ok=True)
        path = os.path.join(BATCH_DIR, f"{batch_id}.json")
        Path(path).write_text(json.dumps(report, default=str), encoding="utf-8")
        keep = guardrails.MAX_BATCH_REPORTS
        reports = sorted(
            (item for item in Path(BATCH_DIR).glob("*.json") if item.name != "pacing.json"),
            key=lambda item: item.stat().st_mtime, reverse=True,
        )
        for old in reports[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
        return path
    except OSError as exc:
        logger.warning("BATCH REPORT NOT WRITTEN %s %s", batch_id, exc)
        return None


def _signature(page, step: dict) -> str:
    """Cheap fingerprint of 'did the page change', used by the `changes` mode."""
    url = getattr(page, "url", "") or ""
    return f"{url}|{step.get('action')}|{step.get('selector')}|{step.get('value')}"


async def execute_batch(batch_id: str, on_event=None, *, playwright_factory=None,
                        pacer: AdaptivePacer | None = None, screenshots: str | None = None,
                        share_session: bool | None = None, retry_transient: bool | None = None,
                        display_window: bool = False, viewer_count=None) -> dict:
    """Replay every recording of a batch through one browser.

    ``playwright_factory`` exists so the unit tests can drive the whole batch
    without Chromium, exactly as they drive ``execute_run``.
    """
    factory = playwright_factory
    if factory is None:
        from playwright.async_api import async_playwright as factory  # noqa: F401
    mode = (screenshots or DEFAULT_SCREENSHOTS).lower()
    if mode not in SCREENSHOT_MODES:
        mode = DEFAULT_SCREENSHOTS
    pacer = pacer or AdaptivePacer()
    share = guardrails.BATCH_SHARE_SESSION if share_session is None else bool(share_session)
    retry = guardrails.BATCH_RETRY_TRANSIENT if retry_transient is None else bool(retry_transient)

    async def emit(payload: dict) -> None:
        if on_event is None:
            return
        try:
            result = on_event(payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.warning("BATCH EVENT FAILED %s", exc)

    started = time.time()
    resources_before = sample_resources()
    items = load_batch_runs(batch_id)
    plan = plan_order(items)
    # Merge into the options the API recorded, so what it selected (and what it
    # could not find) stays visible in the batch row.
    with SessionLocal() as db:
        row = db.get(Batch, batch_id)
        options = dict(row.options or {}) if row is not None else {}
    options.update({"screenshots": mode, "share_session": share,
                    "retry_transient": retry, "display_window": bool(display_window),
                    "pacing_path": pacer.path})
    _save_batch(batch_id, status="running", started_at=_now(), total=len(plan), options=options)
    await emit({"type": "batch", "batch_id": batch_id, "status": "running", "total": len(plan),
                "message": f"Batch started: {len(plan)} recording(s), one browser"})

    results: list[dict] = []
    counters = {"passed": 0, "failed": 0, "skipped": 0, "done": 0}
    peak_rss = resources_before.get("rss_mb")
    launches = 1
    relaunches = 0
    contexts_opened = 0
    screenshots_taken = 0
    screenshots_skipped = 0
    retries = 0
    steps_executed = 0
    batch_status = "passed"
    failure_text = None

    def progress() -> int:
        return int(counters["done"] / len(plan) * 100) if plan else 100

    # One slot for the entire batch: recording and single runs cannot start a
    # second Chromium while a batch holds the browser.
    async with browser_budget.slot(f"batch:{batch_id}"):
        browser = None
        context = None
        page = None
        context_origin = None
        shot_tasks: set = set()
        try:
            async with factory() as playwright:
                browser = await playwright.chromium.launch(**launch_kwargs())
                viewport = {"width": int(os.environ.get("TF_VIEWPORT_WIDTH", "1024")),
                            "height": int(os.environ.get("TF_VIEWPORT_HEIGHT", "640"))}
                for position, item in enumerate(plan, 1):
                    if cancel_requested(batch_id):
                        counters["skipped"] += len(plan) - position + 1
                        for remaining in plan[position - 1:]:
                            _save_run(remaining["run_id"], status="cancelled",
                                      cancel_reason="Batch cancelled before this recording started.",
                                      finished_at=_now())
                            results.append({"run_id": remaining["run_id"], "recording_id": remaining["recording_id"],
                                            "name": remaining.get("name"), "status": "cancelled",
                                            "steps": 0, "seconds": 0.0})
                        batch_status = "cancelled"
                        await emit({"type": "batch", "batch_id": batch_id, "status": "cancelled",
                                    "message": "Batch cancelled; the remaining recordings were not started."})
                        break

                    run_id = item["run_id"]
                    name = item.get("name") or item["recording_id"]
                    if item.get("missing"):
                        counters["failed"] += 1
                        counters["done"] += 1
                        _save_run(run_id, status="error", cancel_reason=None,
                                  rog_monitor_log="Recording not found", finished_at=_now())
                        results.append({"run_id": run_id, "recording_id": item["recording_id"],
                                        "name": name, "status": "error", "error": "Recording not found",
                                        "steps": 0, "seconds": 0.0})
                        _save_batch(batch_id, done=counters["done"], failed=counters["failed"],
                                    progress_pct=progress())
                        await emit({"type": "run", "batch_id": batch_id, "run_id": run_id, "status": "error",
                                    "name": name, "error": "Recording not found"})
                        continue

                    # Memory circuit breaker: hand the browser's memory back before
                    # the platform decides for us.
                    sample = sample_resources()
                    if sample.get("rss_mb") and MAX_RSS_MB and sample["rss_mb"] > MAX_RSS_MB:
                        try:
                            if context is not None:
                                await context.close()
                            if browser is not None:
                                await browser.close()
                            browser = await playwright.chromium.launch(**launch_kwargs())
                            context = None
                            page = None
                            context_origin = None
                            launches += 1
                            relaunches += 1
                            await emit({"type": "batch", "batch_id": batch_id, "status": "running",
                                        "message": f"Memory guard: browser relaunched at {sample['rss_mb']}MB"})
                        except Exception as exc:
                            logger.warning("BATCH MEMORY GUARD FAILED %s", redact(exc))

                    origin = origin_of(item.get("start_url") or "")
                    need_context = context is None or (not share and True) or (share and context_origin != origin)
                    if need_context:
                        if context is not None:
                            try:
                                await context.close()
                            except Exception:
                                pass
                        context = await browser.new_context(viewport=viewport, device_scale_factor=1)
                        contexts_opened += 1
                        context_origin = origin
                        page = await context.new_page()
                    elif page is None:
                        page = await context.new_page()

                    recording_started = time.time()
                    steps = await _expand_steps_with_repeat(item["steps"])
                    variables = dict(item.get("variables") or {})
                    log_entries: list[dict] = []
                    status = "passed"
                    error_text = None
                    _save_run(run_id, status="running", started_at=_now(), progress_pct=1,
                              execution_log=[], cancel_reason=None)
                    await emit({"type": "run", "batch_id": batch_id, "run_id": run_id, "status": "running",
                                "name": name, "index": position, "total": len(plan),
                                "percent": progress()})

                    if not steps:
                        status = "error"
                        error_text = "This recording has no steps."
                    run_dir = os.path.join(ARTIFACTS, "runs", run_id)
                    os.makedirs(run_dir, exist_ok=True)
                    last_signature = None
                    total_steps = len(steps)
                    for index, step in enumerate(steps, 1):
                        action = (step.get("action") or "").lower()
                        selector = step.get("selector") if isinstance(step.get("selector"), dict) else {}
                        budgets = pacer.budgets_for(action, selector)
                        attempt_started = time.time()
                        entry = {
                            "order": step.get("order") or index,
                            "action": step.get("action"),
                            "label": step.get("label") or step.get("action"),
                            "value": step.get("value"),
                            "selector": step.get("selector"),
                            "status": "running",
                            "percent": int((index - 1) / total_steps * 100) if total_steps else 100,
                        }
                        try:
                            if action == "log":
                                entry["status"] = "passed"
                            else:
                                await replay_step(page, step, variables, budgets=budgets)
                                steps_executed += 1
                            elapsed_ms = (time.time() - attempt_started) * 1000
                            pacer.observe(action, selector, elapsed_ms, True)
                            entry["ms"] = round(elapsed_ms, 1)
                            entry["budget_ms"] = budgets.get("navigate_ms") if action == "navigate" else budgets.get("action_ms")
                            entry["status"] = "passed"
                            if mode in ("all", "changes"):
                                signature = _signature(page, step)
                                changed = signature != last_signature
                                last_signature = signature
                                if changed or mode == "all":
                                    await _capture(page, run_dir, index, entry, shot_tasks)
                                    screenshots_taken += 1
                                else:
                                    screenshots_skipped += 1
                        except Exception as exc:
                            elapsed_ms = (time.time() - attempt_started) * 1000
                            kind = classify_failure(str(exc))
                            pacer.observe(action, selector, elapsed_ms, False)
                            retried = False
                            if retry and kind in TRANSIENT_KINDS and not budgets.get("escalated"):
                                retries += 1
                                pacer.escalations += 1
                                relaxed = pacer.escalate(budgets)
                                retry_started = time.time()
                                try:
                                    await replay_step(page, step, variables, budgets=relaxed)
                                    steps_executed += 1
                                    pacer.observe(action, selector, (time.time() - retry_started) * 1000, True)
                                    entry["status"] = "passed"
                                    entry["ms"] = round((time.time() - attempt_started) * 1000, 1)
                                    entry["retried"] = True
                                    retried = True
                                except Exception as retry_exc:
                                    error_text = str(retry_exc).splitlines()[0][:500]
                            if not retried:
                                entry["status"] = "failed"
                                entry["kind"] = kind
                                entry["error"] = error_text or str(exc).splitlines()[0][:500]
                                entry["ms"] = round(elapsed_ms, 1)
                                status = "failed"
                                error_text = entry["error"]
                                if mode in ("failure", "changes", "all"):
                                    try:
                                        await _capture(page, run_dir, index, entry, shot_tasks)
                                        screenshots_taken += 1
                                    except Exception:
                                        pass
                                log_entries.append(entry)
                                break
                        log_entries.append(entry)
                        if len(log_entries) > MAX_LOG_ENTRIES:
                            del log_entries[: len(log_entries) - MAX_LOG_ENTRIES]
                        _save_run(run_id, progress_pct=entry.get("percent"), execution_log=list(log_entries))

                    elapsed = time.time() - recording_started
                    if status == "passed":
                        counters["passed"] += 1
                    else:
                        counters["failed"] += 1
                    counters["done"] += 1
                    # progress_pct is NOT NULL: only set it when the run completed,
                    # the same way the single-run path does.
                    finished_fields = {
                        "status": status,
                        "execution_log": list(log_entries),
                        "finished_at": _now(),
                        "rog_monitor_log": (f"Batch step failed: {error_text[:600]}" if error_text else None),
                        "rog_qa_log": (f"Batch: {len(log_entries)} step(s) in {elapsed:.2f}s "
                                       f"({(len(log_entries) / elapsed if elapsed else 0):.1f} steps/s)"),
                        "rog_devops_log": f"Batch {batch_id}: shared browser, screenshots={mode}",
                    }
                    if status == "passed":
                        finished_fields["progress_pct"] = 100
                    _save_run(run_id, **finished_fields)
                    result = {"run_id": run_id, "recording_id": item["recording_id"], "name": name,
                              "status": status, "error": error_text, "steps": len(log_entries),
                              "seconds": round(elapsed, 3),
                              "steps_per_second": round(len(log_entries) / elapsed, 2) if elapsed else None}
                    results.append(result)
                    _save_batch(batch_id, done=counters["done"], passed=counters["passed"],
                                failed=counters["failed"], skipped=counters["skipped"],
                                progress_pct=progress())
                    await emit({"type": "run", "batch_id": batch_id, "run_id": run_id, "status": status,
                                "name": name, "error": error_text, "seconds": result["seconds"],
                                "percent": progress(), "index": position, "total": len(plan)})

                    # A closed page per recording keeps memory flat when sessions
                    # are not shared; the context is reused for the next origin.
                    if not share and page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = None

                if shot_tasks:
                    await asyncio.gather(*list(shot_tasks), return_exceptions=True)
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
        except Exception as exc:
            batch_status = "error"
            failure_text = explain_launch_error(exc)
            logger.exception("BATCH FAILED %s", batch_id)
            for item in plan:
                with SessionLocal() as db:
                    row = db.get(Run, item["run_id"])
                    if row is not None and row.status in ("queued", "running"):
                        row.status = "error"
                        row.rog_monitor_log = failure_text[:600]
                        row.finished_at = _now()
                        db.commit()

    if batch_status not in ("cancelled", "error"):
        if counters["failed"] and counters["passed"]:
            batch_status = "partial"
        elif counters["failed"]:
            batch_status = "failed"
        else:
            batch_status = "passed"

    elapsed_total = time.time() - started
    pacer.save()
    resources_after = sample_resources()
    if resources_after.get("rss_mb"):
        peak_rss = max(peak_rss or 0, resources_after["rss_mb"])
    report = {
        "batch_id": batch_id,
        "status": batch_status,
        "error": failure_text,
        "total": len(plan),
        "passed": counters["passed"],
        "failed": counters["failed"],
        "skipped": counters["skipped"],
        "seconds": round(elapsed_total, 3),
        "results": results,
        "throughput": {
            "recordings_per_minute": round(counters["done"] / elapsed_total * 60, 2) if elapsed_total else None,
            "steps_per_second": round(steps_executed / elapsed_total, 2) if elapsed_total else None,
            "steps_executed": steps_executed,
        },
        "resources": {
            "browsers_launched": launches,
            "browser_relaunches": relaunches,
            "contexts_opened": contexts_opened,
            "rss_mb_before": resources_before.get("rss_mb"),
            "rss_mb_after": resources_after.get("rss_mb"),
            "rss_mb_peak": peak_rss,
            "cpu_seconds_before": resources_before.get("cpu_seconds"),
            "cpu_seconds_after": resources_after.get("cpu_seconds"),
            "memory_guard_mb": MAX_RSS_MB,
        },
        "screenshots": {
            "mode": mode,
            "taken": screenshots_taken,
            "skipped": screenshots_skipped,
        },
        "pacing": pacer.report(),
        "retries": retries,
        # One launch per recording is what a batch replaces; the saving is that
        # cost times the recordings that did not have to pay it.
        "savings": {
            "browser_launches_avoided": max(0, len(plan) - launches),
            "note": "A batch launches Chromium once instead of once per recording.",
        },
    }
    _save_batch(batch_id, status=batch_status, done=counters["done"], passed=counters["passed"],
                failed=counters["failed"], skipped=counters["skipped"],
                progress_pct=100 if batch_status in ("passed", "partial", "failed") else progress(),
                report=report, error=failure_text, finished_at=_now())
    write_report(batch_id, report)
    _CANCELLED.discard(batch_id)
    await emit({"type": "batch", "batch_id": batch_id, "status": batch_status, "percent": 100,
                "seconds": report["seconds"], "passed": counters["passed"], "failed": counters["failed"],
                "message": f"Batch {batch_status} in {report['seconds']}s"})
    logger.info("BATCH DONE %s status=%s total=%s passed=%s failed=%s elapsed=%.2fs",
                batch_id, batch_status, len(plan), counters["passed"], counters["failed"], elapsed_total)
    return report


async def _capture(page, run_dir: str, index: int, entry: dict, shot_tasks: set) -> None:
    """Take the step PNG now and encode/write it off the replay path."""
    name = f"step-{index}.png"
    png = await page.screenshot(type="png", animations="disabled")
    task = asyncio.create_task(_persist_shot(run_dir, name, png))
    shot_tasks.add(task)
    task.add_done_callback(shot_tasks.discard)
    entry["screenshot"] = f"/api/runs/screenshot/{os.path.basename(run_dir)}/{name}"
