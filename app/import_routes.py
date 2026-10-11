"""Import preview/confirmation and action authoring; bounded and on demand."""
import asyncio
import base64
import binascii
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func

from app import library_store
from app.db import Recording, RecordingStep, SessionLocal
from app.script_import import FORMATS, MAX_BYTES, ImportProblem, parse_bundle, parse_file
from app.step_runtime import ACTIONS, MAX_STEPS, normalize_step, normalize_steps

router = APIRouter()
# Parsing work never starts an unbounded task queue. No browser or network work
# happens in preview. Two simultaneous imports are refused, not run in parallel.
_gate = asyncio.Lock()


async def bounded_body(request, limit=MAX_BYTES * 2):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, "Import request exceeds the size limit")
        data.extend(chunk)
    return bytes(data)


async def body_json(request):
    try:
        data = json.loads(await bounded_body(request))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Expected a bounded JSON object")


def save_import(body):
    project_id = body.get("project_id")
    name = body.get("name")
    if not isinstance(project_id, str) or not isinstance(name, str) or not name.strip():
        raise ValueError("Select a project and provide a recording name")
    if not body.get('draft_id'):
        raise ValueError('Test the imported steps first; a passing draft_id is required')
    from app import import_drafts
    receipt, tested_plan = import_drafts.verified(body['draft_id'], project_id,
        body.get('execution_plan') or {'steps': body.get('steps')})
    if receipt.get('saved_recording_id'):
        existing = library_store.get_recording(receipt['saved_recording_id'])
        if existing is None:
            raise ValueError('Previously saved recording was deleted; test again')
        return {**existing, 'published': False, 'publish_message': 'Already saved; inspect repository sync status', 'already_saved': True}
    # Ordinary imported scripts keep the original single-run/batch representation.
    plan = body.get('execution_plan')
    steps = normalize_steps(tested_plan['steps'] if plan else body.get("steps"))
    # No partial imported script is silently saved: clients must supply a reviewed
    # canonical step list. Save never runs or publishes the uploaded source code.
    start_url = next((s["value"] for s in steps if s["action"] == "navigate"), "")
    result = library_store.create_recording(project_id, name, start_url, steps=steps, tags=str(body.get("tags") or "imported"), execution_plan=plan)
    if receipt:
        receipt['saved_recording_id'] = result['id']
        import_drafts.write(receipt)
    return result


async def _exclusive(work, *args):
    if _gate.locked():
        raise HTTPException(409, "An import is already being processed; try again when it finishes")
    async with _gate:
        try:
            task = asyncio.create_task(asyncio.to_thread(work, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A disconnected caller must not release the gate while its
                # parser/file writer is still running in a worker thread.
                await task
                raise
        except library_store.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, TypeError, binascii.Error, library_store.LibraryError) as exc:
            raise HTTPException(422, str(exc)) from exc


@router.get("/api/imports/formats")
def formats():
    return {"formats": FORMATS, "actions": sorted(ACTIONS), "max_bytes": MAX_BYTES,
            "max_steps": MAX_STEPS, "sequential": True, "executes_source": False,
            "notice": "Static supported-subset conversion, not arbitrary code execution. Preview errors must be corrected before saving. GitHub publication follows existing library settings."}


@router.post("/api/imports/preview")
async def preview(request: Request, filename: str):
    data = await bounded_body(request, MAX_BYTES)
    return await _exclusive(parse_file, filename[:255], data)


@router.post("/api/imports/preview-bundle")
async def preview_bundle(request: Request):
    body = await body_json(request)
    files = body.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        raise HTTPException(422, "Provide 1–8 files")
    try:
        decoded = [(str(f["name"])[:255], base64.b64decode(f["base64"], validate=True)) for f in files]
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, "Each file needs name and valid base64 fields") from exc
    return await _exclusive(parse_bundle, decoded)


@router.post("/api/imports/save", status_code=201)
async def save(request: Request):
    body = await body_json(request)
    result = await _exclusive(save_import, body)
    if result.get("retry_scheduled"):
        from app.main import schedule_publish_retry
        schedule_publish_retry()
    return result




@router.post('/api/imports/test', status_code=202)
async def test_draft(request: Request):
    from app import import_drafts
    body = await body_json(request)
    try:
        return await import_drafts.start(body.get('project_id'), body.get('execution_plan') or {'steps': body.get('steps')})
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/api/imports/tests/{draft_id}')
def draft_status(draft_id: str):
    from app import import_drafts
    try:
        return import_drafts.public(import_drafts.read(draft_id))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/api/imports/tests/{draft_id}/cancel')
async def cancel_draft(draft_id: str):
    from app import import_drafts
    try:
        return import_drafts.cancel(draft_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/imports/template.csv")
def template():
    # Quoted options use standard CSV encoding; no spreadsheet formulas/macros.
    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["action", "selector", "value", "label", "repeat_count"])
    writer.writerow(["navigate", "", "{{base_url}}", "Open application", 1])
    writer.writerow(["fill", "#name", "Ada", "Enter name", 1])
    writer.writerow(["click", "#go", "", "Continue", 1])
    writer.writerow(["assert_text", "#out", "Hello, Ada", "Verify greeting", 1])
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="testforge-template.csv"'})


def append_step(rid, body):
    # No live-session edits: changing step order under a recorder would lose data.
    from app.recorder import get_session
    if get_session(rid):
        raise ValueError("Stop the recording session before adding a step")
    with library_store.LOCK:
        if not library_store.materialize_recording(rid):
            raise library_store.NotFound("Recording not found")
        with SessionLocal() as db:
            recording = db.get(Recording, rid)
            if recording is None:
                raise library_store.NotFound("Recording not found")
            if recording.execution_plan:
                raise ValueError('Re-import and test an edited plan; do not append steps to its snapshot')
            count = db.query(RecordingStep).filter_by(recording_id=rid).count()
            if count >= MAX_STEPS:
                raise ValueError("Recording has reached the step limit")
            order = (db.query(func.max(RecordingStep.order)).filter_by(recording_id=rid).scalar() or 0) + 1
            step = normalize_step(body, order)
            db.add(RecordingStep(recording_id=rid, **step))
            db.commit()
        return library_store.export_recording(rid, publish=True)


@router.post("/api/recordings/{rid}/steps", status_code=201)
async def add_step(rid: str, request: Request):
    result = await _exclusive(append_step, rid, await body_json(request))
    if result.get("retry_scheduled"):
        from app.main import schedule_publish_retry
        schedule_publish_retry()
    return result


def update_metadata(rid, body):
    import re
    with library_store.LOCK:
        if not library_store.materialize_recording(rid):
            raise library_store.NotFound("Recording not found")
        with SessionLocal() as db:
            recording = db.get(Recording, rid)
            if recording is None:
                raise library_store.NotFound("Recording not found")
            if "name" in body:
                if not isinstance(body["name"], str) or not body["name"].strip():
                    raise ValueError("Recording name cannot be empty")
                recording.name = body["name"].strip()[:255]
            if "tags" in body:
                if not isinstance(body["tags"], str):
                    raise ValueError("Tags must be comma-separated text")
                tags = sorted(set(t.lower() for t in re.split(r"[\s,]+", body["tags"].strip()) if t))
                if len(tags) > 20 or any(not re.fullmatch(r"[\w.-]{1,50}", t) for t in tags) or len(",".join(tags)) > 500:
                    raise ValueError("Use at most 20 simple tags (500 characters total)")
                recording.tags = ",".join(tags)
            if "parent_id" in body:
                parent = body["parent_id"] or None
                visited = {rid}
                current = parent
                while current:
                    if current in visited or len(visited) > 100:
                        raise ValueError("Parent relationship would create a cycle or exceed the depth limit")
                    visited.add(current)
                    ancestor = db.get(Recording, current)
                    if ancestor is None or ancestor.project_id != recording.project_id:
                        raise ValueError("Parent must belong to the same project")
                    current = ancestor.parent_id
                recording.parent_id = parent
            db.commit()
        return library_store.export_recording(rid, publish=True)


@router.patch("/api/recordings/{rid}/metadata")
async def edit_metadata(rid: str, request: Request):
    result = await _exclusive(update_metadata, rid, await body_json(request))
    if result.get("retry_scheduled"):
        from app.main import schedule_publish_retry
        schedule_publish_retry()
    return result


@router.get('/api/imports/packs')
def packs():
    from app.domain_packs import PACKS
    return [{'id': key, 'name': value[0]} for key, value in PACKS.items()]


@router.get('/api/imports/packs/{slug}')
def pack(slug: str):
    from app.domain_packs import template
    try:
        result = template(slug)
        # This URL is consumed by the server-side browser, not fetched by the UI.
        result['demo_url'] = library_store.playback_url('/domain-demo.html')
        return result
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
