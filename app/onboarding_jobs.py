"""Background AI onboarding jobs: loop until every step validates, or the user stops.

A single synchronous preview request cannot wait several minutes for a model to
repair a long script, and a browser cannot stop a request it is waiting on. So
the loop runs in a worker thread: the dashboard starts a job, polls its progress,
and can press STOP at any moment. Stopping ends the loop between two model calls
and returns the best conversion so far, marked incomplete (it can never be saved
as complete).

Guarantees (unchanged from the synchronous onboarding):

* Uploaded or pasted source is never executed. Only data (step objects) comes
  back from the model, and every step is re-validated by app.step_runtime.
* One job runs per process at a time; a second start gets a clear 409.
* The save gate is untouched: a passing draft test is still required.
"""
import threading
import time
import uuid
from collections import OrderedDict

from app.errors import redact

_LOCK = threading.Lock()
_JOBS: "OrderedDict[str, Job]" = OrderedDict()
_ACTIVE: str | None = None
_KEEP = 20                           # finished jobs remembered for polling


class Job:
    def __init__(self, kind, filename):
        self.id = str(uuid.uuid4())
        self.kind = kind
        self.filename = filename
        self.status = "queued"       # queued | running | stopping | passed | stopped | incomplete | failed
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.stop_event = threading.Event()
        self.budget = None
        self.progress = {"phase": "queued", "round": 0, "max_rounds": 0, "calls": 0,
                         "step_fixes": 0, "valid_steps": 0, "proposed_steps": 0,
                         "failing_steps": 0, "percent": 0}
        self.result = None
        self.error = None
        self.onboarding_status = None

    def snapshot(self):
        progress = dict(self.progress)
        if progress.get("proposed_steps"):
            progress["percent"] = round(100.0 * progress.get("valid_steps", 0) / progress["proposed_steps"])
        if self.status == "passed":
            progress["percent"] = 100
        data = {
            "job_id": self.id,
            "kind": self.kind,
            "filename": self.filename,
            "status": self.status,
            "onboarding_status": self.onboarding_status,
            "progress": progress,
            "stop_requested": self.stop_event.is_set(),
            "elapsed_seconds": round(time.time() - self.created_at, 1),
            "done": self.status not in ("queued", "running", "stopping"),
        }
        if self.error:
            data["error"] = redact(self.error)
        if self.result is not None:
            data["result"] = self.result
        return data


def _remember(job):
    _JOBS[job.id] = job
    while len(_JOBS) > _KEEP:
        oldest = next(iter(_JOBS))
        if _JOBS[oldest].status in ("queued", "running", "stopping"):
            break
        _JOBS.pop(oldest)


def active_job():
    with _LOCK:
        return _JOBS.get(_ACTIVE) if _ACTIVE else None


def get(job_id):
    with _LOCK:
        return _JOBS.get(job_id)


def request_stop(job_id):
    job = get(job_id)
    if job is None:
        return None
    if job.status in ("queued", "running"):
        job.stop_event.set()
        job.status = "stopping" if job.status == "running" else job.status
        job.updated_at = time.time()
    return job


def start(kind, prepare, finalize):
    """Run ``prepare()`` and the AI loop in a worker thread.

    ``prepare()`` returns ``(filename, static_result, source, data)``: the static
    validation of the input, the decoded source the model may read (or None when
    the content is binary), and the bytes. ``finalize(result)`` shapes the final
    preview for its input (paste metadata, bundle wrapper...).
    """
    global _ACTIVE
    from app import ai_onboarding
    with _LOCK:
        if _ACTIVE and _JOBS.get(_ACTIVE) and _JOBS[_ACTIVE].status in ("queued", "running", "stopping"):
            raise RuntimeError("An onboarding job is already running; stop it or wait for it to finish")
        job = Job(kind, "")
        _remember(job)
        _ACTIVE = job.id

    def run():
        global _ACTIVE
        try:
            job.status = "running"
            filename, static_result, source = prepare()
            job.filename = filename
            if static_result.get("can_save") or not ai_onboarding.available():
                # Nothing for the model to do (or no key): the static result stands.
                result = static_result
                result["ai"] = {"available": ai_onboarding.available(), "used": False,
                                "reason": ("static validation passed; AI repair was not needed"
                                           if static_result.get("can_save") else
                                           "ANTHROPIC_API_KEY is not set; set it to enable AI smart onboarding")}
                job.status = "passed" if static_result.get("can_save") else "incomplete"
            elif source is None:
                result = static_result
                result["ai"] = {"available": True, "used": False,
                                "reason": "file content is binary; AI onboarding converts text-based scripts"}
                job.status = "incomplete"
            else:
                budget = ai_onboarding.job_budget(should_stop=job.stop_event.is_set)
                job.budget = budget

                def on_progress(info):
                    job.progress.update(info)
                    job.updated_at = time.time()

                result = ai_onboarding.onboard_until_valid(filename, source, static_result, budget, on_progress)
                state = (result.get("ai") or {}).get("onboarding") or {}
                job.onboarding_status = state.get("status")
                if state.get("status") == "passed":
                    job.status = "passed"
                elif state.get("status") == "stopped":
                    job.status = "stopped"
                else:
                    job.status = "incomplete"
                if job.onboarding_status == "failed":
                    job.error = state.get("reason")
            job.result = finalize(result)
        except Exception as exc:       # the worker must always end in a visible state
            job.status = "failed"
            job.error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            job.updated_at = time.time()
            with _LOCK:
                if _ACTIVE == job.id:
                    _ACTIVE = None

    worker = threading.Thread(target=run, name="onboarding-%s" % job.id[:8], daemon=True)
    worker.start()
    return job
