"""HTTP surface for the deterministic visual baseline gate.

    GET  /api/visual/status                     store summary, threshold, caps
    GET  /api/visual/baselines                  baselines (optionally one recording)
    POST /api/visual/baselines                  promote a run's step screenshots
    GET  /api/visual/baselines/{key}            one baseline image
    DELETE /api/visual/baselines/{key}          drop one baseline
    POST /api/visual/compare                    compare a run against its baselines

Read-only apart from promote/delete, which only write inside
``ARTIFACTS/visual``. No browser is started and no screenshot is taken here: the
evidence the gate compares is the step evidence a run already captured.
"""


from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import visual_regression as vr
from app.config import ARTIFACTS

router = APIRouter()
MAX_BODY_BYTES = 64 * 1024


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, f"Request is larger than {MAX_BODY_BYTES // 1024} KB")
    if not raw.strip():
        return {}
    import json
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Request body must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def _store() -> vr.BaselineStore:
    return vr.BaselineStore()


def _run_dir(run_id: str) -> Path:
    """Resolve a run's evidence directory, refusing anything that is not an id."""
    if not vr.SAFE_ID_RE.match(str(run_id or "")):
        raise HTTPException(400, "Invalid run id")
    directory = Path(ARTIFACTS) / "runs" / str(run_id)
    if not directory.is_dir():
        raise HTTPException(404, "No evidence directory for this run")
    return directory


def _recording_id(payload: dict) -> str:
    recording_id = str(payload.get("recording_id") or "").strip()
    if not vr.SAFE_ID_RE.match(recording_id):
        raise HTTPException(400, "recording_id is required")
    return recording_id


def _steps(payload: dict) -> list[int] | None:
    raw = payload.get("steps")
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > vr.MAX_STEPS_COMPARED:
        raise HTTPException(400, "steps must be a bounded list of step indexes")
    out = []
    for value in raw:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            raise HTTPException(400, "steps must contain integers") from None
    return out


@router.get("/api/visual/status")
def visual_status():
    return _store().report()


@router.get("/api/visual/baselines")
def visual_baselines(recording_id: str | None = None, limit: int = 500):
    try:
        limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        limit = 500
    rows = _store().list(recording_id)
    return {"count": len(rows), "baselines": rows[:limit]}


@router.post("/api/visual/baselines", status_code=201)
async def visual_promote(request: Request):
    payload = await _body(request)
    recording_id = _recording_id(payload)
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(400, "run_id is required")
    viewport = str(payload.get("viewport") or "").strip()[:40]
    store = _store()
    saved = store.promote(_run_dir(run_id), recording_id, viewport,
                          run_id=run_id, steps=_steps(payload))
    if not saved:
        raise HTTPException(404, "That run has no step screenshots to promote")
    return {"promoted": len(saved), "baselines": saved}


@router.get("/api/visual/baselines/{key}")
def visual_baseline_image(key: str):
    store = _store()
    data = store.image(key)
    if not data:
        raise HTTPException(404, "No baseline with that key")
    target = store.image_path(key)
    return FileResponse(target, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@router.delete("/api/visual/baselines/{key}")
def visual_baseline_delete(key: str):
    if not _store().delete(key):
        raise HTTPException(404, "No baseline with that key")
    return {"deleted": key}


@router.post("/api/visual/compare")
async def visual_compare(request: Request):
    payload = await _body(request)
    recording_id = _recording_id(payload)
    run_id = str(payload.get("run_id") or "").strip()
    if not run_id:
        raise HTTPException(400, "run_id is required")
    viewport = str(payload.get("viewport") or "").strip()[:40]
    threshold = vr.normalise_threshold(payload.get("threshold"))
    store = _store()
    steps = _steps(payload)
    report = store.compare_run(_run_dir(run_id), recording_id, viewport,
                               threshold, run_id=run_id)
    if steps is not None:
        wanted = set(steps)
        report["results"] = [row for row in report["results"]
                             if int(row.get("step_index", -1)) in wanted]
        report["steps"] = len(report["results"])
    return report
