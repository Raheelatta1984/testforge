"""Bounded, sequential plans. Portable with step_runtime; no services or timers."""
import asyncio
import copy
from contextlib import AsyncExitStack
import json
import math
import os
import re
import time
from pathlib import Path

try:
    from .step_runtime import interpolate, normalize_steps, resolve, StepClock, StepRuntime
    from .execution_speed import normalize_speed, speed_profile
    from .desktop_runner import DESKTOP_ACTIONS, DESKTOP_FRAMEWORKS, DesktopRunner, validate_custom_app
    from .mobile_runner import MOBILE_ACTIONS, MOBILE_PLATFORMS, ORIENTATIONS, MobileRunner
except ImportError:  # exported companion source
    from testforge_runtime import interpolate, normalize_steps, resolve, StepClock, StepRuntime
    from testforge_speed import normalize_speed, speed_profile
    from testforge_desktop import DESKTOP_ACTIONS, DESKTOP_FRAMEWORKS, DesktopRunner, validate_custom_app
    from testforge_mobile import MOBILE_ACTIONS, MOBILE_PLATFORMS, ORIENTATIONS, MobileRunner

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
DEVICES = {
    "desktop": {"viewport": {"width": 1024, "height": 640}},
    "tablet": {"viewport": {"width": 820, "height": 1180}, "has_touch": True},
    "mobile": {"viewport": {"width": 390, "height": 844}, "has_touch": True},
}
MAX_CASES = 25
MAX_ACTIONS = 2000


def variables(value):
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError("Variables must be an object with at most 100 entries")
    for key, item in value.items():
        if not NAME.fullmatch(key):
            raise ValueError("Invalid variable name")
        if isinstance(item, dict):
            if set(item) != {"env"} or not re.fullmatch(r"TF_SECRET_[A-Z0-9_]{1,64}", str(item['env'])):
                raise ValueError("Secret references must name a TF_SECRET_* environment variable")
        elif not isinstance(item, (str, int, float, bool)) or (isinstance(item, float) and not math.isfinite(item)) or len(str(item)) > 4096:
            raise ValueError("Dataset/variable values must be small scalars or secret references")
    return copy.deepcopy(value)


def resolve_values(value):
    result = {}
    for key, item in value.items():
        if isinstance(item, dict):
            env = item['env']
            if env not in os.environ:
                raise ValueError("Missing secret reference: " + env)
            item = os.environ[env]
        result[key] = item
    return result


def auth_state(alias):
    if not alias:
        return None
    if not re.fullmatch(r"[A-Z0-9_]{1,64}", alias):
        raise ValueError("Auth profile must be an uppercase environment alias")
    raw = os.environ.get('TF_AUTH_STATE_' + alias, '')
    if not raw or len(raw.encode()) > 262144:
        raise ValueError("Auth profile missing or too large: " + alias)
    try:
        payload = json.loads(raw)
        if set(payload) != {'expires_at', 'state'} or not math.isfinite(float(payload['expires_at'])) or float(payload['expires_at']) <= time.time():
            raise ValueError()
        state = payload['state']
        if not isinstance(state, dict) or set(state) != {'cookies', 'origins'}:
            raise ValueError()
        if not isinstance(state['cookies'], list) or not isinstance(state['origins'], list):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError("Invalid or expired auth profile: " + alias) from None
    return state


MAX_DATASET_UPLOAD = 256 * 1024


def parse_dataset_upload(filename, content):
    """Parse a CSV/JSON dataset upload into bounded plan dataset rows.

    CSV cells stay strings except ``env:TF_SECRET_*`` (converted to a secret
    reference, never a stored secret) and JSON rows may carry scalars or the
    same ``{"env": "TF_SECRET_*"}`` references plan variables already use.
    """
    import csv
    import io
    if isinstance(content, bytes):
        if len(content) > MAX_DATASET_UPLOAD:
            raise ValueError("Dataset upload exceeds 256 KiB")
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Dataset upload must be UTF-8 CSV or JSON") from exc
    if not isinstance(content, str) or len(content.encode()) > MAX_DATASET_UPLOAD:
        raise ValueError("Dataset upload exceeds 256 KiB")
    name = str(filename or "").lower()
    rows = None
    if name.endswith(".json") or content.lstrip().startswith(("[", "{")):
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise ValueError("Dataset JSON is not valid JSON") from exc
        if isinstance(payload, dict):
            payload = payload.get("dataset", payload.get("rows"))
        if not isinstance(payload, list):
            raise ValueError("Dataset JSON must be a list of row objects")
        rows = payload
    else:
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise ValueError("Dataset CSV needs a header row")
        rows = []
        for raw_row in reader:
            row = {}
            for key, cell in raw_row.items():
                if key is None:
                    raise ValueError("Dataset CSV has more cells than headers")
                cell = "" if cell is None else cell
                row[str(key).strip()] = {"env": cell[4:]} if cell.startswith("env:TF_SECRET_") else cell
            rows.append(row)
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CASES:
        raise ValueError("Dataset must contain 1–25 rows")
    cleaned = [variables(row) for row in rows]
    columns = []
    for row in cleaned:
        for key in row:
            if key not in columns:
                columns.append(key)
    return {"dataset": cleaned, "rows": len(cleaned), "columns": columns}


def apply_row_overrides(dataset, overrides):
    """Apply per-row overrides (``{"0": {...}}`` or a list) to dataset rows.

    Overrides are validated through the same scalar/secret-reference rules as
    dataset rows, so an override can point at ``TF_SECRET_*`` but can never
    smuggle a new shape into a plan.
    """
    result = [variables(row) for row in dataset]
    if overrides is None:
        return result
    if isinstance(overrides, dict):
        items = []
        for key, patch in overrides.items():
            try:
                index = int(key)
            except (TypeError, ValueError) as exc:
                raise ValueError("Row override keys must be row indexes") from exc
            items.append((index, patch))
    elif isinstance(overrides, list):
        items = list(enumerate(overrides))
    else:
        raise ValueError("Row overrides must be an object or list")
    for index, patch in items:
        if patch is None:
            continue
        if not 0 <= index < len(result) or not isinstance(patch, dict):
            raise ValueError("Row override index out of range")
        result[index] = variables({**result[index], **patch})
    return result


def normalize_plan(raw):
    if not isinstance(raw, dict) or set(raw) - {'version', 'steps', 'setup', 'teardown', 'flows', 'dataset', 'variables', 'matrix', 'retries', 'execution_speed'}:
        raise ValueError("Unknown execution plan fields")
    if raw.get('version', 1) != 1 or type(raw.get('version', 1)) is not int:
        raise ValueError("Unsupported plan version")
    flows = raw.get('flows', {})
    if not isinstance(flows, dict) or len(flows) > 30 or any(not NAME.fullmatch(k) for k in flows):
        raise ValueError("At most 30 named flows")
    count = 0
    def expand(items, stack=()):
        nonlocal count
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("Each step/flow list must have at most 500 entries")
        out = []
        for item in items:
            if isinstance(item, dict) and 'flow' in item:
                name = item['flow']
                if set(item) != {'flow'} or not isinstance(name, str) or name not in flows:
                    raise ValueError("Unknown flow reference")
                if name in stack or len(stack) >= 8:
                    raise ValueError("Recursive flow or flow nesting exceeds eight")
                out.extend(expand(flows[name], (*stack, name)))
            else:
                count += 1
                if count > 500:
                    raise ValueError("Expanded plan exceeds 500 authored steps")
                out.append(item)
        return out
    if sum(len(items) if isinstance(items, list) else 501 for items in flows.values()) > 500:
        raise ValueError('Flow definitions exceed 500 entries')
    for name, items in flows.items():
        count = 0
        expanded = expand(items, (name,))
        if expanded:
            normalize_steps(expanded)
    count = 0
    stages = {}
    for stage in ('setup', 'steps', 'teardown'):
        expanded = expand(raw.get(stage, []))
        stages[stage] = normalize_steps(expanded) if expanded else []
    if not stages['steps']:
        raise ValueError("Plan needs at least one test step")
    dataset = raw.get('dataset', [{}])
    if not isinstance(dataset, list) or not 1 <= len(dataset) <= MAX_CASES:
        raise ValueError("Dataset must contain 1–25 rows")
    dataset = [variables(row) for row in dataset]
    matrix = raw.get('matrix', [{'browser': 'chromium', 'device': 'desktop'}])
    if not isinstance(matrix, list) or not 1 <= len(matrix) <= 6:
        raise ValueError("Matrix must contain 1–6 profiles")
    plan_speed = normalize_speed(raw.get('execution_speed', 1.0))
    profiles = []
    for item in matrix:
        if not isinstance(item, dict) or set(item) - {'browser', 'device', 'auth_profile', 'runner', 'framework', 'platform', 'device_model', 'os_version', 'orientation', 'desktop_app', 'app_profile', 'execution_speed'}:
            raise ValueError("Unknown matrix profile fields")
        browser, device = item.get('browser', 'chromium'), item.get('device', 'desktop')
        if browser not in {'chromium', 'firefox', 'webkit'} or device not in DEVICES:
            raise ValueError("Unsupported browser/device profile")
        alias = item.get('auth_profile', '')
        if not isinstance(alias, str) or (alias and not re.fullmatch(r'[A-Z0-9_]{1,64}', alias)):
            raise ValueError("Invalid auth profile alias")
        runner = str(item.get('runner', 'web')).strip().lower()
        if runner not in {'web', 'windows_desktop', 'mobile_device'}:
            raise ValueError("runner must be web, windows_desktop or mobile_device")
        profile = {'browser': browser, 'device': device, 'auth_profile': alias, 'runner': runner,
                   'execution_speed': normalize_speed(item.get('execution_speed', plan_speed))}
        if runner == 'windows_desktop':
            framework = str(item.get('framework', 'dotnet')).strip().lower()
            if framework not in DESKTOP_FRAMEWORKS:
                raise ValueError("framework must be dotnet, cpp or winforms")
            profile['framework'] = framework
            if item.get('desktop_app') is not None:
                if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", str(item['desktop_app'])):
                    raise ValueError("desktop_app must be a simple app slug")
                profile['desktop_app'] = str(item['desktop_app'])
            if item.get('app_profile') is not None:
                profile['app_profile'] = validate_custom_app(item['app_profile'])
        if runner == 'mobile_device':
            platform = str(item.get('platform', 'android')).strip().lower()
            if platform not in MOBILE_PLATFORMS:
                raise ValueError("platform must be android, ios or zebra")
            orientation = str(item.get('orientation', 'portrait')).strip().lower()
            if orientation not in ORIENTATIONS:
                raise ValueError("orientation must be portrait or landscape")
            profile.update(platform=platform, orientation=orientation,
                           device_model=str(item.get('device_model', 'simulator'))[:80],
                           os_version=str(item.get('os_version', ''))[:40])
        profiles.append(profile)
    retries = raw.get('retries', 0)
    if type(retries) is not int or not 0 <= retries <= 1:
        raise ValueError("Retries must be 0 or 1 (default 0)")
    case_count = len(dataset) * len(profiles)
    downloads = sum(s['repeat_count'] for rows in stages.values() for s in rows if s['action'] == 'download')
    if downloads * case_count * (1 + retries) > 16:
        raise ValueError('Plan allows at most 16 downloads including retries')
    action_count = sum(s['repeat_count'] for rows in stages.values() for s in rows)
    if case_count > MAX_CASES or case_count * action_count * (1 + retries) > MAX_ACTIONS:
        raise ValueError("Plan exceeds 25 cases or 2,000 total actions including retries")
    return {'version': 1, **stages, 'variables': variables(raw.get('variables', {})),
            'dataset': dataset, 'matrix': profiles, 'retries': retries,
            'execution_speed': plan_speed}


async def _run_nonweb_profile(plan, profile, base_variables, output, results,
                              evidence_index, on_progress, input_overrides=None):
    """Sequential desktop/mobile cases: agent/hub when configured, else simulator.

    Evidence deliberately omits typed values and expected/actual payloads (they
    may be secrets); assertion failures are reported as a diagnostic marker,
    exactly as web plan evidence withholds exception messages.
    """
    total = len(plan['dataset']) * len(plan['matrix'])
    pace_ms = int(speed_profile(profile.get('execution_speed', 1.0))['pace_ms'])
    for row_index, row in enumerate(plan['dataset']):
        case = {'case': len(results) + 1, 'row': row_index + 1, **profile, 'status': 'failed', 'attempts': []}
        for attempt in range(plan['retries'] + 1):
            values = {**base_variables, **resolve_values(plan['variables']), **resolve_values(row), **(input_overrides or {})}
            case_dir = output / f"case-{case['case']}-{attempt + 1}"
            case_dir.mkdir(parents=True, exist_ok=True)
            if profile['runner'] == 'windows_desktop':
                adapter = DesktopRunner(app_id=profile.get('desktop_app'), app_profile=profile.get('app_profile'),
                                        framework=profile.get('framework'), artifact_dir=case_dir)
            else:
                adapter = MobileRunner(platform=profile.get('platform', 'android'),
                                       device_model=profile.get('device_model', 'simulator'),
                                       os_version=profile.get('os_version', ''),
                                       orientation=profile.get('orientation', 'portrait'),
                                       artifact_dir=case_dir)
            record = {'attempt': attempt + 1, 'status': 'passed', 'steps': []}
            failed = False
            for stage in ('setup', 'steps', 'teardown'):
                if failed and stage != 'teardown':
                    continue
                for index, step in enumerate(plan[stage]):
                    for repetition in range(step['repeat_count']):
                        evidence_index += 1
                        evidence = {'stage': stage, 'step': index + 1, 'repeat': repetition + 1,
                                    'action': step['action'], 'status': 'passed'}
                        foreign = (DESKTOP_ACTIONS if profile['runner'] == 'mobile_device' else MOBILE_ACTIONS)
                        if step['action'] in foreign:
                            # A mixed plan runs each family on its own profiles.
                            evidence.update(status='skipped', note='step belongs to another runner family')
                            record['steps'].append(evidence)
                            continue
                        resolved_step = {
                            'action': step['action'],
                            'value': step['value'] if step['action'] in {'ui_grid_read', 'table_lookup', 'capture_text'} else interpolate(step.get('value'), values),
                            'selector': resolve(step.get('selector') or {}, values),
                        }
                        clock = StepClock()
                        try:
                            result = await adapter.perform(resolved_step, values)
                        except Exception as exc:
                            failed = True
                            record['status'] = 'failed'
                            evidence.update(status='failed', error_type=type(exc).__name__)
                            if isinstance(exc, AssertionError):
                                evidence['diagnostic'] = 'assertion mismatch (expected/actual withheld from plan evidence)'
                        else:
                            if isinstance(result, dict):
                                for key in ('mode', 'framework', 'window_title', 'platform', 'orientation', 'symbology', 'matched'):
                                    if key in result:
                                        evidence[key] = result[key]
                                shot = result.get('screenshot')
                                if shot:
                                    # Reports resolve screenshots from the run
                                    # root, so re-home the per-case synthetic
                                    # PNG under its unique evidence index.
                                    try:
                                        data = (case_dir / Path(str(shot)).name).read_bytes()
                                        name = f"step-{evidence_index}.png"
                                        (output / name).write_bytes(data)
                                        evidence['screenshot'] = name
                                    except OSError:
                                        evidence['evidence_error'] = 'ScreenshotUnavailable'
                        evidence.update(clock.finish())
                        record['steps'].append(evidence)
                        if pace_ms > 0:
                            await asyncio.sleep(pace_ms / 1000)
                        if evidence['status'] == 'failed':
                            break
                    if failed and stage != 'teardown':
                        break
            case['attempts'].append(record)
            if record['status'] == 'passed':
                case['status'] = 'flaky' if attempt else 'passed'
                break
        results.append(case)
        if on_progress:
            await on_progress(len(results), total, case)
    return evidence_index


async def run_plan(plan, base_variables, artifact_dir, **options):
    # Overall execution ceiling, not an artificial delay or a polling loop.
    async with asyncio.timeout(300):
        return await _run_plan(plan, base_variables, artifact_dir, **options)


async def _run_plan(plan, base_variables, artifact_dir, *, chromium_options=None, on_progress=None, borrowed_browser=None, playwright=None, relaunch=None, evidence=None, input_overrides=None):
    """Caller owns the global browser slot. Never more than one context/browser.

    No retry unless explicitly requested. Teardown runs after setup/test failure.
    Authentication is loaded from operator-provisioned env, never exported.
    """
    from playwright.async_api import async_playwright
    plan = normalize_plan(plan)
    output = Path(artifact_dir)
    output.mkdir(parents=True, exist_ok=True)
    total = len(plan['dataset']) * len(plan['matrix'])
    results = []
    evidence_index = 0
    capture_evidence = evidence
    # Never include exception messages/expected values: they may contain secrets.
    if borrowed_browser and any(p['runner'] == 'web' and p['browser'] != 'chromium' for p in plan['matrix']):
        raise ValueError('Shared batches support Chromium plans only')
    async with AsyncExitStack() as stack:
        has_web = any(p['runner'] == 'web' for p in plan['matrix'])
        pw = playwright or (await stack.enter_async_context(async_playwright()) if has_web else None)
        for profile in plan['matrix']:
            if profile['runner'] != 'web':
                evidence_index = await _run_nonweb_profile(
                    plan, profile, base_variables, output, results, evidence_index,
                    on_progress, input_overrides)
                continue
            options = chromium_options if profile['browser'] == 'chromium' and chromium_options else {'headless': True}
            browser = borrowed_browser or await getattr(pw, profile['browser']).launch(**options)
            single_process = '--single-process' in options.get('args', [])
            fresh = not bool(borrowed_browser)
            try:
                for row_index, row in enumerate(plan['dataset']):
                    case = {'case': len(results) + 1, 'row': row_index + 1, **profile, 'status': 'failed', 'attempts': []}
                    for attempt in range(plan['retries'] + 1):
                        values = {**base_variables, **resolve_values(plan['variables']), **resolve_values(row), **(input_overrides or {})}
                        state = auth_state(profile['auth_profile'])
                        # Chromium --single-process can crash when a closed context
                        # is replaced. Preserve isolation and the process cap: close
                        # BEFORE relaunching, never run browsers in parallel.
                        if single_process and not fresh:
                            if borrowed_browser:
                                if relaunch is None:
                                    raise ValueError('Single-process batch requires a sequential relaunch callback')
                                browser = await relaunch()
                            else:
                                await browser.close()
                                browser = await getattr(pw, profile['browser']).launch(**options)
                        fresh = False
                        context = await browser.new_context(**DEVICES[profile['device']], storage_state=state, accept_downloads=True)
                        record = {'attempt': attempt + 1, 'status': 'passed', 'steps': []}
                        runtime = None
                        try:
                            page = await context.new_page()
                            runtime = StepRuntime(page, output / f"case-{case['case']}-{attempt + 1}", execution_speed=profile.get('execution_speed', plan.get('execution_speed', 1.0)))
                            runtime.artifact_dir.mkdir(exist_ok=True)
                            failed = False
                            for stage in ('setup', 'steps', 'teardown'):
                                if failed and stage != 'teardown':
                                    continue
                                for index, step in enumerate(plan[stage]):
                                    for repetition in range(step['repeat_count']):
                                        evidence_index += 1
                                        evidence = {'stage': stage, 'step': index + 1, 'repeat': repetition + 1,
                                                    'action': step['action'], 'status': 'passed'}
                                        clock = StepClock()
                                        try:
                                            await runtime.perform(step, values)
                                        except Exception as exc:
                                            failed = True
                                            record['status'] = 'failed'
                                            evidence.update(status='failed', error_type=type(exc).__name__)
                                        evidence.update(clock.finish())
                                        try:
                                            # One capture per expanded action, including teardown and retries.
                                            if capture_evidence:
                                                secret_names = {k for k,v in {**plan['variables'], **row}.items() if isinstance(v, dict)}
                                                secret_values = {k:v for k,v in values.items() if k in secret_names or not isinstance(v, (str, int, float, bool))}
                                                evidence['screenshot'] = await capture_evidence(runtime.page, evidence_index, secret_values)
                                        except Exception as exc:
                                            evidence['evidence_error'] = type(exc).__name__
                                        record['steps'].append(evidence)
                                        if evidence['status'] == 'failed':
                                            break
                                    if failed and stage != 'teardown':
                                        break
                        finally:
                            try:
                                if runtime:
                                    await runtime.close()
                            finally:
                                await context.close()
                        case['attempts'].append(record)
                        if record['status'] == 'passed':
                            case['status'] = 'flaky' if attempt else 'passed'
                            break
                    results.append(case)
                    if on_progress:
                        await on_progress(len(results), total, case)
            finally:
                if not borrowed_browser:
                    await browser.close()
    # Flaky is not a clean pass: publication/release gate fails closed.
    return {'status': 'passed' if all(c['status'] == 'passed' for c in results) else 'failed',
            'cases': results, 'total': total, 'sequential': True}
