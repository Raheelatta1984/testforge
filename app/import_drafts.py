"""Private, bounded draft tests. Never put unapproved drafts in library/ or DB."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import time
import uuid

from app.config import ARTIFACTS
from app import library_store
from app.browser import launch_kwargs
from app.db import Project, SessionLocal, resolve_variables
from app.guardrails import browser_budget
from app.plan_runtime import normalize_plan, resolve_values, auth_state, run_plan

KEY = secrets.token_bytes(32)  # receipts fail closed after restart
TTL = 3600
MAX_DRAFTS = 10
MAX_DRAFT_BYTES = 32 * 1024 * 1024
_task = None
_active = None


def directory():
    root = (Path(ARTIFACTS) / 'import-drafts').resolve()
    library = library_store.library_dir().resolve()
    if root == library or library in root.parents or root in library.parents:
        raise ValueError('Draft storage must be outside the published library')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def path_for(draft_id):
    try:
        if str(uuid.UUID(draft_id)) != draft_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid draft id') from None
    return directory() / draft_id


def write(state):
    if _active and _active['id'] == state['id'] and _active is not state:
        _active.update(state)
    folder = path_for(state['id'])
    folder.mkdir(exist_ok=True, mode=0o700)
    tmp = folder / 'state.tmp'
    tmp.write_text(json.dumps(state, allow_nan=False), encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(folder / 'state.json')


def read(draft_id):
    if _active and _active['id'] == draft_id:
        state = _active.copy()
    else:
        path = path_for(draft_id) / 'state.json'
        if not path.is_file():
            raise ValueError('Draft not found; test the steps again')
        state = json.loads(path.read_text())
    if time.time() - state['created_at'] > TTL:
        raise ValueError('Draft expired; test the steps again')
    if state['status'] in {'queued', 'running'} and not (_task and not _task.done() and _active and _active['id'] == draft_id):
        state = {**state, 'status': 'interrupted', 'error': 'Server restarted; test again'}
    return state


def public(state):
    return {k: state[k] for k in ('id', 'status', 'percent', 'created_at', 'result', 'error', 'saved_recording_id') if k in state}


def environment(project_id, plan, project_values=None):
    plan = normalize_plan(plan)
    library_store.require_id(project_id, 'project id')
    # No writes or rehydration of recordings in preview/test paths.
    with SessionLocal() as db:
        if db.get(Project, project_id) is None:
            raise ValueError('Select an existing project')
        values = resolve_variables(db, project_id) if project_values is None else project_values
    allowed = set(os.environ.get('TF_ALLOWED_TEST_BROWSERS', 'chromium').split(','))
    if any(p['browser'] not in allowed for p in plan['matrix']):
        raise ValueError('Browser disabled by resource policy; operator must explicitly enable/install it')
    resolved = [resolve_values(plan['variables']), *[resolve_values(row) for row in plan['dataset']]]
    states = [auth_state(p['auth_profile']) for p in plan['matrix']]
    binding = json.dumps([project_id, plan, values, resolved, states], sort_keys=True, allow_nan=False, default=lambda v: {'encrypted': v.ciphertext}).encode()
    digest = hmac.new(KEY, binding, hashlib.sha256).hexdigest()
    return values, digest


async def start(project_id, raw_plan):
    global _task, _active
    if _task and not _task.done():
        raise RuntimeError('A draft test is already queued/running; no parallel draft worker is started')
    plan = normalize_plan(raw_plan)
    values, fingerprint = environment(project_id, plan)
    root = directory()
    folders = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime)
    sizes = {folder: sum(p.stat().st_size for p in folder.rglob('*') if p.is_file() and not p.is_symlink()) for folder in folders}
    total = sum(sizes.values())
    remaining = len(folders)
    for folder in folders:
        if time.time() - folder.stat().st_mtime > TTL or remaining >= MAX_DRAFTS or total > MAX_DRAFT_BYTES:
            shutil.rmtree(folder)
            total -= sizes[folder]
            remaining -= 1
    state = {'id': str(uuid.uuid4()), 'project_id': project_id, 'plan': plan,
             'fingerprint': fingerprint, 'created_at': time.time(), 'status': 'queued', 'percent': 0}
    write(state)
    _active = state

    async def work():
        try:
            async with asyncio.timeout(300), browser_budget.slot('draft:' + state['id']):
                if time.time() - state['created_at'] > TTL:
                    raise ValueError('Draft expired while queued')
                state['status'] = 'running'
                # Validate references again in case the job waited for a recorder.
                current_values, current_fingerprint = environment(project_id, plan)
                if not hmac.compare_digest(fingerprint, current_fingerprint):
                    raise ValueError('Inputs changed while queued')
                async def progress(done, total, case):
                    state['percent'] = int(done / total * 100)
                result = await run_plan(plan, current_values, path_for(state['id']) / 'files',
                                        chromium_options=launch_kwargs(), on_progress=progress)
                state.update(status=result['status'], result=result, percent=100)
        except asyncio.CancelledError:
            state.update(status='cancelled', error='Draft test cancelled')
        except Exception as exc:
            # Never leak Playwright call logs or target/auth data into receipts.
            state.update(status='error', error=type(exc).__name__ + ': check browser installation, profile configuration and authored steps')
        finally:
            write(state)
    _task = asyncio.create_task(work())
    return public(state)


def cancel(draft_id):
    state = read(draft_id)
    if _active and _active['id'] == draft_id and _task and not _task.done():
        _task.cancel()
    return public(state)


def verified(draft_id, project_id, raw_plan):
    state = read(draft_id)
    plan = normalize_plan(raw_plan)
    _, fingerprint = environment(project_id, plan)
    if state['status'] != 'passed' or state['project_id'] != project_id or not hmac.compare_digest(state['fingerprint'], fingerprint):
        raise ValueError('A passing test of these exact steps, dataset, profiles and variables is required before publication')
    return state, plan
