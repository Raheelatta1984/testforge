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
    from .step_runtime import normalize_steps, StepRuntime
except ImportError:  # exported companion source
    from testforge_runtime import normalize_steps, StepRuntime

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


def normalize_plan(raw):
    if not isinstance(raw, dict) or set(raw) - {'version', 'steps', 'setup', 'teardown', 'flows', 'dataset', 'variables', 'matrix', 'retries'}:
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
    profiles = []
    for item in matrix:
        if not isinstance(item, dict) or set(item) - {'browser', 'device', 'auth_profile'}:
            raise ValueError("Unknown matrix profile fields")
        browser, device = item.get('browser', 'chromium'), item.get('device', 'desktop')
        if browser not in {'chromium', 'firefox', 'webkit'} or device not in DEVICES:
            raise ValueError("Unsupported browser/device profile")
        alias = item.get('auth_profile', '')
        if not isinstance(alias, str) or (alias and not re.fullmatch(r'[A-Z0-9_]{1,64}', alias)):
            raise ValueError("Invalid auth profile alias")
        profiles.append({'browser': browser, 'device': device, 'auth_profile': alias})
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
            'dataset': dataset, 'matrix': profiles, 'retries': retries}


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
    if borrowed_browser and any(p['browser'] != 'chromium' for p in plan['matrix']):
        raise ValueError('Shared batches support Chromium plans only')
    async with AsyncExitStack() as stack:
        pw = playwright or await stack.enter_async_context(async_playwright())
        for profile in plan['matrix']:
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
                            runtime = StepRuntime(page, output / f"case-{case['case']}-{attempt + 1}")
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
                                        try:
                                            await runtime.perform(step, values)
                                        except Exception as exc:
                                            failed = True
                                            record['status'] = 'failed'
                                            evidence.update(status='failed', error_type=type(exc).__name__)
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
