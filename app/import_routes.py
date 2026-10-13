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


MAX_OVERRIDE_REASON = 200


def unverified_override(body):
    """The operator's explicit reason for saving without a passing test, or None.

    Deliberately not a boolean the dashboard can send by accident: the override has
    to be asked for by name *and* carry a written reason, because the recording it
    produces is published to the library marked as never having run. Omitting the
    receipt, or sending ``require_test: false``, still refuses -- that gate is
    unchanged (UT-PLAN-12).
    """
    if body.get('allow_untested') is not True:
        return None
    reason = body.get('override_reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            'Saving without a passing test needs a written reason in override_reason '
            '(the recording is published marked "unverified").')
    return reason.strip()[:MAX_OVERRIDE_REASON]


def save_import(body):
    project_id = body.get("project_id")
    name = body.get("name")
    if not isinstance(project_id, str) or not isinstance(name, str) or not name.strip():
        raise ValueError("Select a project and provide a recording name")
    from app import import_drafts
    plan = body.get('execution_plan')
    override = unverified_override(body)
    receipt = None
    tested_plan = None
    if override is None:
        if not body.get('draft_id'):
            raise ValueError('Test the imported steps first; a passing draft_id is required')
        receipt, tested_plan = import_drafts.verified(body['draft_id'], project_id,
            plan or {'steps': body.get('steps')})
        if receipt.get('saved_recording_id'):
            existing = library_store.get_recording(receipt['saved_recording_id'])
            if existing is None:
                raise ValueError('Previously saved recording was deleted; test again')
            return {**existing, 'published': False, 'publish_message': 'Already saved; inspect repository sync status', 'already_saved': True}
    elif body.get('draft_id'):
        # Keep the audit trail even though this receipt did not authorise the save.
        try:
            receipt = import_drafts.read(body['draft_id'])
        except ValueError:
            receipt = None
    # Ordinary imported scripts keep the original single-run/batch representation.
    if tested_plan is not None:
        steps = normalize_steps(tested_plan['steps'] if plan else body.get("steps"))
    elif plan:
        from app.plan_runtime import normalize_plan
        # No receipt means nothing validated this plan yet: expand it here so the
        # saved steps are exactly what a run would execute. Still never executed.
        steps = normalize_steps(normalize_plan(plan)['steps'])
    else:
        steps = normalize_steps(body.get("steps"))
    # No partial imported script is silently saved: clients must supply a reviewed
    # canonical step list. Save never runs or publishes the uploaded source code.
    start_url = next((s["value"] for s in steps if s["action"] == "navigate"), "")
    tags = str(body.get("tags") or "imported")
    if override is not None:
        tags = library_store.mark_unverified(tags)
    result = library_store.create_recording(project_id, name, start_url, steps=steps, tags=tags, execution_plan=plan)
    if override is not None:
        result['unverified'] = True
        result['override_reason'] = override
        result['tested'] = False
        result['draft_status'] = (receipt or {}).get('status')
        result['notice'] = (
            f"Saved without a passing test ({override}). It is published to the library marked "
            f"'{library_store.UNVERIFIED_TAG}'; run it before trusting its result.")
    elif receipt is not None:
        result['tested'] = True
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
    from app import ai_onboarding
    from app.script_import import PASTE_FORMAT_LABELS
    return {"formats": FORMATS, "actions": sorted(ACTIONS), "max_bytes": MAX_BYTES,
            "max_steps": MAX_STEPS, "sequential": True, "executes_source": False,
            "ai_onboarding": {"available": ai_onboarding.available(), "model": ai_onboarding.MODEL},
            "paste": {"supported": True, "endpoint": "/api/imports/preview-text",
                      "max_bytes": MAX_BYTES,
                      "formats": [{"id": key, "label": label} for key, label in PASTE_FORMAT_LABELS],
                      "notice": "Paste or type a script instead of uploading a file. It is processed by the exact same static + AI-powered validation, draft-test and save flow as an upload; the pasted source is never executed."},
            "notice": "Static supported-subset conversion first; when the static validator reports unsupported syntax and an AI key is configured, AI smart onboarding converts the script to canonical steps and re-validates every single step (fix loop). Never code execution. Preview errors that AI could not resolve must still be corrected before saving. GitHub publication follows existing library settings."}


@router.post("/api/imports/preview")
async def preview(request: Request, filename: str, smart: bool = True):
    data = await bounded_body(request, MAX_BYTES)
    from app import ai_onboarding
    if smart and ai_onboarding.available():
        # Smart onboarding keeps the static parse as its first pass, so the same
        # exclusive gate and worker-thread bounds apply to the whole loop.
        return await _exclusive(ai_onboarding.smart_parse_file, filename[:255], data)
    return await _exclusive(parse_file, filename[:255], data)


def _pasted_display_name(content, current, resolved_filename):
    """A human name for a paste whose parser only had the generic file name.

    Uploads are named after their file; a paste has no meaningful file name, so
    a Playwright test title (or ``test_*`` function) names the recording the
    tester is about to create. The dashboard name field stays editable either
    way, and a parser-supplied name (scenario, JSON ``name``...) always wins.
    """
    import re
    from pathlib import PurePosixPath
    stem = PurePosixPath(resolved_filename).stem
    if current and current != stem and current.lower() not in {"pasted", "upload"}:
        return current
    match = re.search(r"""\btest(?:\.only|\.skip)?\s*\(\s*['"]([^'"\n]{1,200})['"]""", content)
    if match:
        return match.group(1).strip()[:200] or current
    match = re.search(r"(?m)^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", content)
    if match:
        return match.group(1).removeprefix("test_").replace("_", " ").strip().capitalize()[:200] or current
    match = re.search(r"(?m)^\s*Scenario:\s*(.+)$", content)
    if match:
        return match.group(1).strip()[:200] or current
    return current


@router.post("/api/imports/preview-text")
async def preview_text(request: Request, smart: bool = True):
    """Paste/type a script: the upload preview, without the file.

    The pasted text is given the filename an upload of the same script would
    have had (explicit format, filename hint, or content detection), encoded
    once, and run through the identical pipeline: static validation first,
    AI smart onboarding (validate -> convert -> re-validate per step) when it
    reports unsupported syntax, the same exclusive gate and size bounds, and
    the same preview result -- so variable review, TEST BEFORE PUBLISHING,
    SAVE TO LIBRARY & PUBLISH and RUN SAVED STEPS all work unchanged.
    Pasted source is never executed, exactly like uploaded source.
    """
    from app.script_import import detect_pasted_filename
    body = await body_json(request)
    content = body.get("content", body.get("script", body.get("text")))
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(422, "Paste or type a script first (the script area is empty)")
    try:
        data = content.encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise HTTPException(422, "The pasted script could not be read as UTF-8 text") from exc
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Pasted script exceeds the 1 MiB import limit")
    filename_hint = body.get("filename", body.get("file_name"))
    if filename_hint is not None and not isinstance(filename_hint, str):
        raise HTTPException(422, "filename must be a file name such as login.spec.ts")
    format_hint = body.get("format", body.get("language"))
    if format_hint is not None and not isinstance(format_hint, str):
        raise HTTPException(422, "format must be one of the paste formats (or auto)")
    try:
        resolved = detect_pasted_filename(content, filename_hint, format_hint)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    use_smart = body.get("smart") if isinstance(body.get("smart"), bool) else smart
    from app import ai_onboarding
    if use_smart and ai_onboarding.available():
        result = await _exclusive(ai_onboarding.smart_parse_file, resolved[:255], data)
    else:
        result = await _exclusive(parse_file, resolved[:255], data)
    result = {**result,
              "input_method": "paste",
              "paste": {"filename": resolved,
                        "format": str(format_hint or "auto").strip().lower() or "auto",
                        "characters": len(content),
                        "lines": content.count("\n") + 1,
                        "smart": bool(use_smart)}}
    result["name"] = _pasted_display_name(content, result.get("name") or "", resolved)
    return result


@router.post("/api/imports/preview-bundle")
async def preview_bundle(request: Request, smart: bool = True):
    body = await body_json(request)
    files = body.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        raise HTTPException(422, "Provide 1–8 files")
    try:
        decoded = [(str(f["name"])[:255], base64.b64decode(f["base64"], validate=True)) for f in files]
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, "Each file needs name and valid base64 fields") from exc
    from app import ai_onboarding
    if smart and ai_onboarding.available():
        return await _exclusive(ai_onboarding.smart_parse_bundle, decoded)
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
        return await import_drafts.start(body.get('project_id'),
                                         body.get('execution_plan') or {'steps': body.get('steps')},
                                         body.get('name'))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/api/imports/tests')
def draft_receipts(project_id: str | None = None):
    """Receipts this instance still holds, so a passing test survives a tab switch.

    The import panel keeps its draft in component state, which React throws away
    when another tab is opened. Without this the user comes back to a disabled
    SAVE button an hour before the receipt actually expires.
    """
    from app import import_drafts
    try:
        if project_id:
            library_store.require_id(project_id, 'project id')
        return {'drafts': import_drafts.recent(project_id), 'ttl_seconds': import_drafts.TTL}
    except library_store.LibraryError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, OSError) as exc:
        # Misconfigured draft storage or an unreadable folder: say so, list nothing.
        raise HTTPException(422, str(exc)) from exc


@router.get('/api/imports/tests/{draft_id}')
def draft_status(draft_id: str, plan: bool = False):
    from app import import_drafts
    try:
        return import_drafts.detail(draft_id, include_plan=plan)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/api/imports/tests/{draft_id}/cancel')
async def cancel_draft(draft_id: str):
    from app import import_drafts
    try:
        return import_drafts.cancel(draft_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/imports/templates")
def templates():
    """Every onboarding template, with its bytes, so the dashboard can preview one."""
    from app.import_templates import NOTICE, catalog
    return {"templates": catalog(), "notice": NOTICE}


@router.get("/api/imports/templates.zip")
def templates_zip():
    from app.import_templates import zip_pack
    return Response(zip_pack(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="testforge-onboarding-templates.zip"'})


@router.get("/api/imports/templates/{template_id}")
def template_file(template_id: str, filename: str | None = None):
    """One template file; a bundle (Jenkinsfile + script) is returned as a ZIP."""
    import io
    import zipfile
    from app.import_templates import build
    files = build(template_id)
    if not files:
        raise HTTPException(404, "Unknown template")
    if filename:
        if filename not in files:
            raise HTTPException(404, "Unknown file in this template")
        data, media = files[filename]
        return Response(data, media_type=("text/plain" if media == "xlsx" else media),
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    if len(files) == 1:
        name, (data, media) = next(iter(files.items()))
        return Response(data, media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if media == "xlsx" else media),
            headers={"Content-Disposition": f'attachment; filename="{name}"'})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, (data, _media) in files.items():
            archive.writestr(name, data)
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{template_id}.zip"'})


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


def variable_plan_for_import(body):
    """Which literals in a preview would become variables, and what they become."""
    from app import variables_ai
    project_id = body.get('project_id')
    steps = body.get('steps')
    if not isinstance(steps, list):
        raise ValueError('Provide the imported steps to review')
    existing = library_store.list_variables(project_id) if isinstance(project_id, str) and project_id else []
    plan = variables_ai.plan(steps, existing, body.get('variables') if isinstance(body.get('variables'), dict) else None)
    return {**plan, 'existing': [{'id': item.get('id'), 'name': item.get('name'),
                                  'category': item.get('category'), 'is_secret': bool(item.get('is_secret'))}
                                 for item in existing]}


@router.post('/api/imports/variable-plan')
async def imports_variable_plan(request: Request):
    """Onboarding preview for an uploaded script: inputs -> variables."""
    body = await body_json(request)
    try:
        return variable_plan_for_import(body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def adopt_import_variables(body):
    """Create the chosen variables and return the substituted steps (nothing saved)."""
    from app import variables_ai
    project_id = body.get('project_id')
    steps = body.get('steps')
    assignments = body.get('assignments')
    if not isinstance(project_id, str) or not isinstance(steps, list):
        raise ValueError('Provide the project and the imported steps')
    if not isinstance(assignments, list) or not assignments:
        raise ValueError('Select at least one value to turn into a variable')
    with library_store.LOCK:
        existing = library_store.list_variables(project_id)
        substituted, to_create, reused = variables_ai.apply(steps, assignments, existing)
        created = []
        if to_create:
            created = library_store.create_variables(project_id, to_create).get('variables') or []
    return {'steps': substituted, 'created_variables': created, 'reused_variables': reused,
            'counts': {'variables_created': len(created), 'variables_reused': len(reused)}}


@router.post('/api/imports/variables')
async def imports_variables(request: Request):
    """Create variables for an uploaded script and hand back the rewritten steps."""
    result = await _exclusive(adopt_import_variables, await body_json(request))
    if result.get('retry_scheduled'):
        from app.main import schedule_publish_retry
        schedule_publish_retry()
    return result


@router.get('/api/plans/catalog')
def plans_catalog():
    """One fast read for the Execution-plan tab: recordings, inputs and variables."""
    try:
        return library_store.plan_catalog()
    except library_store.LibraryError as exc:
        raise HTTPException(404, str(exc)) from exc


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
