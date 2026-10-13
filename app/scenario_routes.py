"""Project -> high-level scenario -> named plan -> ordered repository links."""
import copy
import re
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


_VARIABLE_REFERENCE = re.compile(r'\{\{\s*([A-Za-z_][\w.-]{0,99})\s*\}\}')


def _referenced_names(steps) -> set:
    """Names of every {{variable}} a recording's steps use, in value, label or locator."""
    names = set()
    for step in steps:
        for text in (step.value or '', step.label or '', json.dumps(step.selector or {}, ensure_ascii=False)):
            names.update(_VARIABLE_REFERENCE.findall(text))
    return names


@router.get('/api/recordings/{recording_id}/inputs')
def inputs(recording_id: str):
    from app.db import Recording, SessionLocal
    # Only fall back to the (locked, file-scanning) materialize when the row is
    # missing: a recording that is already in the database opens instantly, even
    # while a background publish is holding the library lock.
    with SessionLocal() as db:
        found = db.get(Recording, recording_id) is not None
    if not found:
        library_store.materialize_recording(recording_id)
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if not recording:
            raise HTTPException(404, 'Recording not found')
        used = _referenced_names(recording.steps or [])
        defaults = public_map(recording.input_defaults)
        project_id = recording.project_id
    variables = library_store.list_variables_current(project_id)
    # The screen lists what this recording uses first, then everything else the
    # project holds, so a variable saved from an AI recording is visible at once.
    variables.sort(key=lambda item: (item['name'] not in used, item['name'].lower()))
    return {'defaults': defaults, 'project_id': project_id,
            'project_variables': variables, 'used_names': sorted(used),
            'missing_names': sorted(used - {item['name'] for item in variables})}


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
        # Local write only: the GitHub push runs in the background so SAVE returns
        # at once (see update_inputs). The next publish carries these defaults too.
        library_store.export_recording(recording_id, publish=False)
        return {'defaults': public_map(recording.input_defaults), 'publish_state': 'pending',
                'publish_message': 'Saved. Publishing to the repository runs in the background.'}


@router.post('/api/recordings/{recording_id}/variable-plan')
def recording_variable_plan(recording_id: str):
    """Propose the variables a finished recording should carry.

    Called by the recorder right after SAVE: every literal that was typed is
    listed with the variable name it would get, grouped by value, with a preview
    of the step it changes. Nothing is written until the user confirms.
    """
    from app import variables_ai
    from app.db import Recording, RecordingStep, SessionLocal
    with SessionLocal() as db:
        # Steps are authoritative in the database right after a recording: the
        # file snapshot can lag behind the final debounced export.
        row = db.get(Recording, recording_id)
        if row is None:
            library_store.materialize_recording(recording_id)
            row = db.get(Recording, recording_id)
        if row is None:
            raise HTTPException(404, 'Recording not found')
        project_id = row.project_id
        name = row.name
        steps = sorted(db.query(RecordingStep).filter_by(recording_id=recording_id).all(),
                       key=lambda step: step.order or 0)
        if not steps:
            steps = sorted(list(row.steps or []), key=lambda step: step.order or 0)
    existing = library_store.list_variables(project_id) if project_id else []
    result = variables_ai.plan(steps, existing)
    result['recording'] = {'id': recording_id, 'name': name, 'step_count': len(steps)}
    result['existing'] = [{'id': item.get('id'), 'name': item.get('name'),
                           'category': item.get('category'), 'is_secret': bool(item.get('is_secret'))}
                          for item in existing]
    return result


@router.post('/api/recordings/{recording_id}/variables')
def adopt_recording_variables(recording_id: str, payload: dict):
    """End-of-recording onboarding: turn typed literals into reusable variables.

    The recorder sends what was typed (its proposal); the server reads the stored
    steps and rewrites the ones that really hold those literals. Applying twice is
    safe -- a step that already carries ``{{name}}`` is left alone.
    """
    try:
        result = library_store.apply_variables_to_recording(recording_id, payload.get('assignments'))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except library_store.NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if result.get('retry_scheduled'):
        from app.main import schedule_publish_retry
        schedule_publish_retry()
    return result


@router.put('/api/recordings/{recording_id}/inputs')
async def update_inputs(recording_id: str, request: Request):
    result = await _exclusive(save_inputs, recording_id, await body_json(request))
    from app.main import spawn_background_publish
    spawn_background_publish()
    return result
