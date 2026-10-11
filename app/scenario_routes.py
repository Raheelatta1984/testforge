"""Project -> high-level scenario -> named plan -> ordered repository links."""
import copy
import json
import uuid
from fastapi import APIRouter, HTTPException, Request
from app import library_store
from app.import_routes import body_json, _exclusive
from app.secrets_store import protect_overrides, public_map, MASK, protected, freeze_inputs, plan_secret_names

router = APIRouter()


def folder(project_id):
    library_store.require_id(project_id, 'project id')
    if not library_store.get_project(project_id):
        raise ValueError('Project not found')
    return library_store.library_dir() / 'projects' / project_id / 'scenarios'


def load(project_id, scenario_id):
    library_store.require_id(scenario_id, 'scenario id')
    path = folder(project_id) / (scenario_id + '.json')
    if not path.is_file():
        raise ValueError('Scenario not found')
    return json.loads(path.read_text())


def public(data):
    data = copy.deepcopy(data)
    for plan in data['plans']:
        for entry in plan['entries']:
            entry['variables'] = public_map(entry.get('variables'))
    return data


def recording_defaults(recording):
    source = library_store._read_recording_file(recording['project_id'], recording['id']) or {}
    return source.get('input_defaults') or {}


def save(project_id, body, scenario_id=None):
    with library_store.LOCK:
        root = folder(project_id)
        previous = load(project_id, scenario_id) if scenario_id else None
        if previous and body.get('revision') != previous['revision']:
            raise ValueError('Scenario changed; reload before saving')
        if not isinstance(body.get('name'), str) or not 1 <= len(body['name'].strip()) <= 200:
            raise ValueError('Scenario name must contain 1–200 characters')
        plans = body.get('plans', [])
        if not isinstance(plans, list) or not 1 <= len(plans) <= 20:
            raise ValueError('Supply 1–20 named plans')
        output = []
        old_plans = {p['id']: p for p in previous['plans']} if previous else {}
        for plan in plans:
            if not isinstance(plan, dict) or not isinstance(plan.get('name'), str) or not 1 <= len(plan['name'].strip()) <= 200:
                raise ValueError('Every plan needs a name')
            entries = plan.get('entries')
            if not isinstance(entries, list) or not 1 <= len(entries) <= 40:
                raise ValueError('Every plan needs 1–40 ordered recording links')
            plan_id = plan.get('id') or str(uuid.uuid4())
            library_store.require_id(plan_id, 'plan id')
            old_entries = old_plans.get(plan_id, {}).get('entries', [])
            links = []
            for index, entry in enumerate(entries):
                rec = library_store.get_recording(entry.get('recording_id', ''))
                if not rec:
                    raise ValueError('Linked recording does not exist')
                if rec.get('execution_plan'):
                    raise ValueError('Link ordinary recordings; nested matrix plans use isolated contexts and cannot preserve a login session')
                values = entry.get('variables', {})
                if not isinstance(values, dict):
                    raise ValueError('Entry variables must be an object')
                old = next((e for e in old_entries if e.get('id') == entry.get('id') and e['recording_id'] == rec['id']), {})
                entry_id = entry.get('id') or str(uuid.uuid4())
                library_store.require_id(entry_id, 'entry id')
                kept = {k: v for k, v in old.get('variables', {}).items() if values.get(k) == MASK}
                if any(v == MASK and k not in kept for k, v in values.items()):
                    raise ValueError('A masked value can only preserve its existing entry; re-enter or inherit the project secret')
                changes = {k: v for k, v in values.items() if v != MASK}
                sealed = protect_overrides([project_id, rec['project_id']], changes, [k for k,v in {**recording_defaults(rec), **old.get('variables', {})}.items() if protected(v)])
                links.append({'id': entry_id, 'recording_id': rec['id'], 'name': rec['name'], 'variables': {**kept, **sealed}})
            if len({e['id'] for e in links}) != len(links):
                raise ValueError('Duplicate entry id')
            output.append({'id': plan_id, 'name': plan['name'].strip(), 'entries': links})
        if len({p['id'] for p in output}) != len(output):
            raise ValueError('Duplicate plan id')
        data = {'id': scenario_id or str(uuid.uuid4()), 'project_id': project_id, 'name': body['name'].strip(),
                'revision': (previous['revision'] if previous else 0) + 1, 'plans': output}
        root.mkdir(parents=True, exist_ok=True)
        library_store._dump(root / (data['id'] + '.json'), data)
        result = public(data)
        result.update(library_store.publish_outcome(library_store._finish('Save high-level scenario ' + data['name'])))
        return result


def prepare(project_id, scenario_id, plan_id, overrides):
    data = load(project_id, scenario_id)
    plan = next((p for p in data['plans'] if p['id'] == plan_id), None)
    if plan is None:
        raise ValueError('Plan not found')
    if not isinstance(overrides, dict) or set(overrides) - {str(i) for i in range(len(plan['entries']))}:
        raise ValueError('Overrides must use zero-based recording-entry indexes')
    selected = []
    total = 0
    for i, entry in enumerate(plan['entries']):
        library_store.materialize_recording(entry['recording_id'])
        rec = library_store.get_recording(entry['recording_id'])
        if not rec or rec.get('execution_plan'):
            raise ValueError('A linked recording was removed or is no longer an ordinary recording')
        steps = rec.get('steps') or []
        if not steps or any(s['action'] in {'loop', 'repeat'} for s in steps):
            raise ValueError('Linked recordings need concrete steps; expand legacy loop groups first')
        total += sum(int(s.get('repeat') or s.get('repeat_count') or 1) for s in steps)
        if total > 2000:
            raise ValueError('Scenario plan exceeds 2,000 expanded steps')
        transient = protect_overrides([project_id, rec['project_id']], overrides.get(str(i), {}), [k for k,v in {**recording_defaults(rec), **entry.get('variables', {})}.items() if protected(v)])
        base, inputs = freeze_inputs([rec['project_id'], project_id], {**recording_defaults(rec), **entry.get('variables', {})}, transient)
        selected.append({'id': rec['id'], 'name': rec['name'], 'project_id': rec['project_id'],
                         'start_url': rec.get('start_url', ''), 'step_count': len(steps),
                         'snapshot_steps': steps, 'variable_snapshot': base, 'input_overrides': inputs})
    return selected, {'scenario_id': data['id'], 'scenario_name': data['name'], 'scenario_revision': data['revision'],
                      'plan_id': plan['id'], 'plan_name': plan['name'], 'project_id': project_id}


@router.get('/api/projects/{project_id}/scenarios')
def scenarios(project_id: str):
    try:
        root = folder(project_id)
        return [public(json.loads(p.read_text())) for p in sorted(root.glob('*.json'))]
    except (ValueError, library_store.LibraryError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/api/projects/{project_id}/scenarios', status_code=201)
async def create(project_id: str, request: Request):
    return await _exclusive(save, project_id, await body_json(request))


@router.put('/api/projects/{project_id}/scenarios/{scenario_id}')
async def update(project_id: str, scenario_id: str, request: Request):
    return await _exclusive(save, project_id, await body_json(request), scenario_id)


@router.post('/api/projects/{project_id}/scenarios/{scenario_id}/plans/{plan_id}/run', status_code=201)
async def execute(project_id: str, scenario_id: str, plan_id: str, request: Request):
    from app.main import start_batch
    body = await body_json(request)
    return await start_batch({'scenario_id': scenario_id, 'scenario_project_id': project_id, 'plan_id': plan_id,
                              'entry_overrides': body.get('entry_overrides', {}), 'screenshots': 'all',
                              'display_window': False, 'share_session': True, 'retry_transient': False})


@router.get('/api/recordings/{recording_id}/inputs')
def inputs(recording_id: str):
    from app.db import Recording, SessionLocal
    library_store.materialize_recording(recording_id)
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if not recording:
            raise HTTPException(404, 'Recording not found')
        return {'defaults': public_map(recording.input_defaults), 'project_id': recording.project_id,
                'project_variables': library_store.list_variables(recording.project_id)}


def save_inputs(recording_id, body):
    from app.db import Recording, SessionLocal
    library_store.materialize_recording(recording_id)
    with library_store.LOCK, SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if not recording:
            raise ValueError('Recording not found')
        values = body.get('variables', {})
        if not isinstance(values, dict):
            raise ValueError('variables must be an object')
        previous = recording.input_defaults or {}
        if any(v == MASK and k not in previous for k, v in values.items()):
            raise ValueError('Cannot create a masked value without an existing default')
        kept = {k: v for k, v in previous.items() if values.get(k) == MASK}
        changes = protect_overrides([recording.project_id], {k: v for k, v in values.items() if v != MASK}, set(k for k,v in previous.items() if protected(v)) | plan_secret_names(recording.execution_plan))
        recording.input_defaults = {**kept, **changes}
        db.commit()
        result = library_store.export_recording(recording_id, publish=True)
        return {'defaults': public_map(recording.input_defaults), 'publish_message': result.get('publish_message')}


@router.put('/api/recordings/{recording_id}/inputs')
async def update_inputs(recording_id: str, request: Request):
    return await _exclusive(save_inputs, recording_id, await body_json(request))
