"""On-demand report/download endpoints. No report worker or periodic rendering."""
import base64
import html
import json
import os
from pathlib import Path
import tempfile
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.background import BackgroundTask

from app import library_store
from app.config import ARTIFACTS
from app.db import Run, SessionLocal
from app.guardrails import MAX_ZIP_BYTES

router = APIRouter()


def _run(run_id):
    with SessionLocal() as db:
        row = db.get(Run, run_id)
        if row is None:
            raise HTTPException(404, "Run not found")
        return {"id": row.id, "recording_id": row.recording_id, "status": row.status,
                "progress_pct": row.progress_pct, "created_at": str(row.created_at),
                "finished_at": str(row.finished_at or ""), "steps": row.execution_log or []}


@router.get("/api/runs/{run_id}/report.json")
def json_report(run_id: str):
    return Response(json.dumps(_run(run_id), ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="testforge-report.json"'})


@router.get("/api/runs/{run_id}/report.html")
def html_report(run_id: str):
    data = _run(run_id)
    esc = lambda value: html.escape(str(value if value is not None else ""), quote=True)
    rows = []
    # Offline self-contained images, bounded across the entire report. No
    # screenshot creation, URL fetch, markdown evaluation or external scripts.
    image_budget = 4 * 1024 * 1024
    directory = (Path(ARTIFACTS) / "runs" / run_id).resolve()
    for step in data["steps"][:2000]:
        image = ""
        name = Path(str(step.get("screenshot") or "")).name
        if name and name.endswith((".png", ".jpg")):
            path = directory / name
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= image_budget:
                content = path.read_bytes()
                if len(content) <= image_budget:
                    image_budget -= len(content)
                    image = '<img alt="Step screenshot" src="data:image/' + ('jpeg' if name.endswith('.jpg') else 'png') + ';base64,' + base64.b64encode(content).decode() + '">'
        rows.append('<tr><td>' + esc(step.get("order")) + '</td><td>' + esc(step.get("action")) +
                    '</td><td>' + esc(step.get("status")) + '</td><td><strong>' + esc(step.get("label")) +
                    '</strong><pre>' + esc(step.get("error") or json.dumps(step.get("result") or {}, ensure_ascii=False)) +
                    '</pre>' + ('<p>Retried transient failure</p>' if step.get("retried") else '') + image + '</td></tr>')
    document = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>TestForge report</title>'
    document += '<style>body{font:16px system-ui;margin:2rem;max-width:1100px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:.6rem;text-align:left;vertical-align:top}pre{white-space:pre-wrap}img{max-width:100%;max-height:360px}small{color:#555}</style>'
    document += '<h1>TestForge run report</h1><p>Run ' + esc(data["id"]) + ' · <strong>' + esc(data["status"]) + '</strong> · ' + esc(data["progress_pct"]) + '%</p>'
    document += '<p>Created ' + esc(data["created_at"]) + ' · Finished ' + esc(data["finished_at"]) + '</p>'
    document += '<table><thead><tr><th>Step</th><th>Action</th><th>Outcome</th><th>Evidence</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'
    document += '<p><small>Sequential execution. Only screenshots captured by the run policy are included; embedded images are capped at 4 MiB. Sensitive request payloads are omitted from this HTML report. A passed run only verifies authored actions and assertions.</small></p></html>'
    return HTMLResponse(document, headers={"Content-Disposition": 'attachment; filename="testforge-report.html"',
        "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})


@router.get("/api/recordings/{rid}/resources.zip")
def resources_zip(rid: str):
    recording = library_store.get_recording(rid)
    if recording is None:
        raise HTTPException(404, "Recording not found")
    directory = library_store.library_dir() / "projects" / recording["project_id"] / "recordings" / rid / "resources"
    files = [p for p in directory.iterdir() if p.is_file() and not p.is_symlink()] if directory.is_dir() else []
    if len(files) > 600 or sum(p.stat().st_size for p in files) > MAX_ZIP_BYTES - 128 * 1024:
        raise HTTPException(413, "Resource bundle exceeds the artifact export limit")
    fd, name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        # STORE avoids CPU-heavy compression. Stream the completed file to the
        # client, not an in-memory BytesIO copy of every screenshot.
        with zipfile.ZipFile(name, "w", compression=zipfile.ZIP_STORED) as archive:
            for file in files:
                archive.write(file, arcname=file.name)
                if archive.fp.tell() > MAX_ZIP_BYTES:
                    raise HTTPException(413, "Resource bundle exceeds the artifact export limit")
        return FileResponse(name, filename="testforge-resources.zip", media_type="application/zip",
                            background=BackgroundTask(os.unlink, name))
    except BaseException:
        os.unlink(name)
        raise


def pdf_response(kind, identity, download):
    from app.pdf_reports import generate
    try:
        path, meta = generate(kind, identity)
    except ValueError as exc:
        raise HTTPException(410 if 'expired' in str(exc) else 409, str(exc)) from exc
    return FileResponse(path, media_type='application/pdf', filename=path.name,
                        content_disposition_type='attachment' if download else 'inline',
                        headers={'Cache-Control': 'private, no-store'})


@router.get('/api/runs/{run_id}/report.pdf')
def run_pdf(run_id: str, download: bool = False):
    return pdf_response('run', run_id, download)


@router.get('/api/runs/batch/{batch_id}/report.pdf')
def batch_pdf(batch_id: str, download: bool = False):
    return pdf_response('batch', batch_id, download)


@router.get('/api/reports/{kind}/{identity}')
def report_status(kind: str, identity: str):
    from app.pdf_reports import metadata, repository_path
    try:
        meta = metadata(kind, identity) or {'remote_status': 'not_generated'}
        # Where the committed report lives, so the dashboard can link to the
        # repository copy rather than only the expiring release asset.
        path = repository_path(kind, identity)
        reference = library_store.repo_ref()
        slug, branch = reference.get('slug'), reference.get('branch')
        return {**meta, 'repository_path': path,
                'repository_url': f'https://github.com/{slug}/blob/{branch}/{path}' if slug and branch else None}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/api/reports-maintenance')
def report_maintenance():
    from app.pdf_reports import root, remote_enabled
    path = root() / 'maintenance-status.tmp'
    return {'github_enabled': remote_enabled(), 'retention_hours': 48, 'interval_seconds': 3600,
            'last_sweep': json.loads(path.read_text()) if path.is_file() else None}
