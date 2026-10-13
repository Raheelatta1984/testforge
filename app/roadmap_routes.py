"""Future-release roadmap API: support matrix, pilots, custom desktop apps
and dataset uploads.  Everything here is bounded and sequential; no route
stores a credential or talks to a real enterprise system."""

import base64
import binascii
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import ARTIFACTS
from app.desktop_runner import DESKTOP_APP_CATALOG, validate_custom_app
from app.plan_runtime import apply_row_overrides, parse_dataset_upload
from app.support_matrix import run_pilot_suite, support_matrix

router = APIRouter()

CUSTOM_APPS_FILE = Path(ARTIFACTS) / "roadmap" / "custom-desktop-apps.json"
MAX_CUSTOM_APPS = 20


def _custom_apps():
    try:
        payload = json.loads(CUSTOM_APPS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except (OSError, ValueError):
        return []


@router.get('/api/roadmap/support-matrix')
def roadmap_support_matrix():
    return support_matrix()


@router.post('/api/roadmap/pilot/run')
def roadmap_pilot_run():
    receipt = run_pilot_suite()
    matrix = support_matrix(receipt=receipt)
    matrix['pilot_receipt'] = {'generated_at': receipt['generated_at'], 'expires_at': receipt['expires_at']}
    return matrix


@router.get('/api/roadmap/desktop-apps')
def roadmap_desktop_apps():
    builtins = [{'id': app['app_id'], 'name': app['window_title'], 'process_name': app['process_name'],
                 'framework': app['framework'], 'kind': app['kind'], 'builtin': True}
                for app in DESKTOP_APP_CATALOG.values()]
    custom = [{**app, 'id': app['app_id'], 'name': app['window_title'], 'builtin': False}
              for app in _custom_apps()]
    return {'apps': builtins + custom}


class DesktopAppIn(BaseModel):
    profile: dict


@router.post('/api/roadmap/desktop-apps', status_code=201)
def roadmap_add_desktop_app(body: DesktopAppIn):
    try:
        app = validate_custom_app(body.profile)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    apps = [existing for existing in _custom_apps() if existing.get('app_id') != app['app_id']]
    apps.append(app)
    if len(apps) > MAX_CUSTOM_APPS:
        raise HTTPException(422, 'At most 20 custom desktop apps')
    CUSTOM_APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_APPS_FILE.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    return {'app': {**app, 'id': app['app_id'], 'name': app['window_title'], 'builtin': False}}


class DatasetParseIn(BaseModel):
    filename: str = 'dataset.csv'
    content: str | None = None
    base64_content: str | None = None


@router.post('/api/plans/dataset/parse')
def plans_dataset_parse(body: DatasetParseIn):
    if body.base64_content is not None:
        try:
            raw = base64.b64decode(body.base64_content, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(422, 'base64_content is not valid base64') from exc
    elif body.content is not None:
        raw = body.content
    else:
        raise HTTPException(422, 'Provide dataset content or base64_content')
    try:
        return parse_dataset_upload(body.filename, raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class DatasetOverridesIn(BaseModel):
    dataset: list
    overrides: dict | list | None = None


@router.post('/api/plans/dataset/apply-overrides')
def plans_dataset_apply_overrides(body: DatasetOverridesIn):
    try:
        dataset = apply_row_overrides(body.dataset, body.overrides)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {'dataset': dataset, 'rows': len(dataset)}
