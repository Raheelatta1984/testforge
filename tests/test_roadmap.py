"""Roadmap: ERP/CRM packs, Windows desktop adapter, Mobile/Zebra adapter,
execution speed, dataset upload, support matrix, export parity and redaction."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()
from fastapi.testclient import TestClient
from app.main import app
import app.support_matrix as support_matrix_module
from app.support_matrix import run_pilot_suite, support_matrix
from app.desktop_runner import (DESKTOP_APP_CATALOG, DesktopRunner, DesktopSimulator,
                                RemoteWindowsAgent, validate_custom_app)
from app.domain_packs import ERP_PACKS, catalog, template
from app.execution_speed import normalize_speed, scaled_timeout, speed_profile
from app.mobile_runner import MobileRunner, MobileSimulator, datawedge_profile
from app.plan_runtime import (apply_row_overrides, normalize_plan, parse_dataset_upload,
                              run_plan)
from app.recorder import _ELEMENT_JS, _FOCUSED_JS
from app.step_runtime import StepRuntime, normalize_step

ERP_SLUGS = {'oracle_web', 'sap_fiori', 'jd_edwards', 'ms_dynamics_fo', 'ms_dynamics_bc', 'odoo_erp', 'salesforce_crm'}
VALID_EAN = '4006381333931'


def desktop_plan(**overrides):
    plan = {
        'matrix': [{'runner': 'windows_desktop', 'framework': 'dotnet', 'desktop_app': 'calculator'}],
        'setup': [{'action': 'window_attach', 'selector': {'window_title': 'Calculator'}, 'value': ''}],
        'steps': [
            {'action': 'ui_type', 'selector': {'automation_id': 'CalculatorResults'}, 'value': '{{expr}}'},
            {'action': 'ui_click', 'selector': {'automation_id': 'equalButton'}, 'value': ''},
            {'action': 'assert_control_text', 'selector': {'automation_id': 'CalculatorResults'}, 'value': '{{expected}}'},
        ],
        'teardown': [{'action': 'ui_click', 'selector': {'automation_id': 'clearButton'}, 'value': ''}],
        'dataset': [{'expr': '2+3', 'expected': '5'}],
    }
    plan.update(overrides)
    return plan


def mobile_plan(**overrides):
    plan = {
        'matrix': [{'runner': 'mobile_device', 'platform': 'zebra', 'device_model': 'TC58', 'os_version': '13'}],
        'steps': [
            {'action': 'app_launch', 'selector': {}, 'value': 'com.testforge.warehouse'},
            {'action': 'device_key', 'selector': {}, 'value': 'F2'},
            {'action': 'scan_barcode', 'selector': {'symbology': 'EAN-13', 'barcode_data': VALID_EAN}, 'value': ''},
            {'action': 'assert_mobile_text', 'selector': {'accessibility_id': 'status'}, 'value': 'Item found: TestForge Scanner Demo Item'},
            {'action': 'rotate_orientation', 'selector': {}, 'value': 'landscape'},
        ],
    }
    plan.update(overrides)
    return plan


class ErpPackTests(unittest.TestCase):
    def test_catalog_keeps_five_synthetic_and_adds_seven_erp_packs(self):
        ids = [entry['id'] for entry in catalog()]
        self.assertEqual(len(ids), 12)
        self.assertEqual(set(ids[:5]), {'commerce', 'banking', 'insurance', 'hrms', 'travel'})
        self.assertTrue(ERP_SLUGS <= {entry['id'] for entry in catalog() if entry['group'] == 'erp_crm'})

    def test_every_erp_pack_is_a_complete_bounded_plan(self):
        fixture = (Path(__file__).parent.parent / 'app/static/erp-crm-demo.html').read_text(encoding='utf-8')
        for slug in ERP_SLUGS:
            with self.subTest(slug=slug):
                pack = template(slug)
                self.assertEqual(pack['demo_path'], '/erp-crm-demo.html')
                raw = pack['execution_plan']
                self.assertTrue(raw['flows'] and raw['setup'] and raw['teardown'])
                self.assertEqual(len(raw['dataset']), 2)
                self.assertIn('No record found', raw['dataset'][1]['expected'])
                self.assertEqual(raw['variables']['erp_user'], {'env': 'TF_SECRET_ERP_USERNAME'})
                self.assertEqual(raw['variables']['erp_password'], {'env': 'TF_SECRET_ERP_PASSWORD'})
                serialized = json.dumps(raw)
                self.assertNotIn('TF_SECRET_ERP_USERNAME=' , serialized)
                normalised = normalize_plan(raw)
                actions = {step['action'] for step in normalised['steps']}
                self.assertTrue({'assert_text', 'assert_value', 'assert_visible'} <= actions | {s['action'] for s in normalised['setup']})
                self.assertIn(slug, fixture)

    def test_erp_fixture_models_enterprise_dom_patterns(self):
        fixture = (Path(__file__).parent.parent / 'app/static/erp-crm-demo.html').read_text(encoding='utf-8')
        for marker in ('oracle-frame', 'oracle-lov-dialog', 'oracle-lines-frame', 'data-sap-ui-id',
                       'sap-grid', 'jde-fastpath', 'jde-grid', 'd365fo-sales-order', 'bc-sales-order',
                       'partner_id', 'odoo-status', 'sf-toast', 'data-aura-rendered-by', 'tenant-select'):
            self.assertIn(marker, fixture)
        mobile = (Path(__file__).parent.parent / 'app/static/mobile-zebra-demo.html').read_text(encoding='utf-8')
        for marker in ('scan_result', 'DataWedge', 'EAN-13', 'GS1-128'):
            self.assertIn(marker, mobile)

    def test_packs_api_serves_erp_demo_urls(self):
        with TestClient(app) as client:
            packs = client.get('/api/imports/packs').json()
            self.assertEqual(len(packs), 12)
            detail = client.get('/api/imports/packs/sap_fiori').json()
            self.assertIn('/erp-crm-demo.html', detail['demo_url'])
            self.assertEqual(detail['execution_plan']['variables']['erp_user'], {'env': 'TF_SECRET_ERP_USERNAME'})


class DesktopTests(unittest.TestCase):
    def test_calculator_and_assertion_diagnostics(self):
        sim = DesktopSimulator('calculator')
        sim.perform({'action': 'window_attach', 'selector': {'window_title': 'Calculator'}, 'value': ''})
        sim.perform({'action': 'ui_type', 'selector': {'automation_id': 'CalculatorResults'}, 'value': '2+3'})
        sim.perform({'action': 'ui_click', 'selector': {'automation_id': 'equalButton'}, 'value': ''})
        result = sim.perform({'action': 'assert_control_text', 'selector': {'automation_id': 'CalculatorResults'}, 'value': '5'})
        self.assertEqual(result['actual'], '5')
        with self.assertRaises(AssertionError) as caught:
            sim.perform({'action': 'assert_control_text', 'selector': {'automation_id': 'CalculatorResults'}, 'value': '9'})
        self.assertIn("expected '9', actual '5'", str(caught.exception))
        self.assertTrue(sim.screenshot_bytes().startswith(b'\x89PNG'))

    def test_catalog_covers_custom_and_legacy_apps(self):
        for app_id in ('calculator', 'settings', 'access_vba', 'foxpro', 'cobol_terminal',
                       'as400_terminal', 'oracle_desktop', 'custom_dotnet', 'custom_cpp', 'winforms_legacy'):
            self.assertIn(app_id, DESKTOP_APP_CATALOG)
        custom = validate_custom_app({'name': 'Orders (Access)', 'kind': 'access_vba'})
        self.assertEqual(custom['framework'], 'winforms')
        with self.assertRaises(ValueError):
            validate_custom_app({'name': 'Nope', 'kind': 'mainframe-tso'})
        with self.assertRaises(ValueError):
            normalize_plan({'steps': [{'action': 'click', 'selector': {'primary': '#a'}, 'value': ''}],
                            'matrix': [{'runner': 'windows_desktop', 'framework': 'qt'}]})

    def test_desktop_step_schema_validated(self):
        step = normalize_step({'action': 'ui_grid_read', 'selector': {'automation_id': 'gridOrders'}, 'value': 'orders'})
        self.assertEqual(step['action'], 'ui_grid_read')
        for raw in ({'action': 'ui_click', 'selector': {}, 'value': ''},
                    {'action': 'window_attach', 'selector': {}, 'value': ''},
                    {'action': 'assert_control_text', 'selector': {'automation_id': 'a'}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_step(raw)

    def test_desktop_plan_runs_sequentially_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_plan(desktop_plan(), {}, tmp))
            self.assertEqual(result['status'], 'passed')
            steps = result['cases'][0]['attempts'][0]['steps']
            self.assertTrue(all(step['status'] == 'passed' for step in steps))
            self.assertTrue(any(step.get('screenshot', '').endswith('.png') for step in steps))
            self.assertTrue(list(Path(tmp).glob('step-*.png')))

    def test_desktop_execution_streams_timed_screenshots_in_step_order(self):
        async def exercise(tmp):
            events = []
            async def on_step(event, screenshot):
                events.append((event, screenshot))
            plan = desktop_plan(
                setup=[{'action': 'window_launch', 'selector': {'app_id': 'calculator'}, 'value': ''}],
                teardown=[{'action': 'window_close', 'selector': {'app_id': 'calculator'}, 'value': ''}],
            )
            result = await run_plan(plan, {}, tmp, on_step=on_step)
            return result, events
        with tempfile.TemporaryDirectory() as tmp:
            result, events = asyncio.run(exercise(tmp))
            self.assertEqual(result['status'], 'passed')
            self.assertEqual([event['action'] for event, _ in events],
                             ['window_launch', 'ui_type', 'ui_click', 'assert_control_text', 'window_close'])
            for event, screenshot in events:
                with self.subTest(action=event['action']):
                    self.assertTrue(event['started_at'])
                    self.assertTrue(event['finished_at'])
                    self.assertGreaterEqual(event['duration_ms'], 0)
                    self.assertTrue(event['screenshot'].endswith('.png'))
                    self.assertTrue(screenshot.startswith(b'\x89PNG'))
                    self.assertTrue((Path(tmp) / event['screenshot']).is_file())

    def test_desktop_failure_evidence_withholds_expected_values(self):
        plan = desktop_plan(dataset=[{'expr': '2+3', 'expected': 'top-secret-expected'}])
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_plan(plan, {}, tmp))
        self.assertEqual(result['status'], 'failed')
        evidence = json.dumps(result)
        self.assertNotIn('top-secret-expected', evidence)
        self.assertIn('assertion mismatch', evidence)

    def test_agent_requires_https_and_token_never_returned(self):
        with self.assertRaises(ValueError):
            RemoteWindowsAgent('http://198.51.100.9:9000', 'x' * 20)
        agent = RemoteWindowsAgent('https://windows.example.test', 'agent-token-value')
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                captured['response_limit'] = limit
                return b'{"ok": true}'

        def fake_open(request, timeout=0):
            captured['auth'] = request.headers.get('Authorization')
            captured['body'] = json.loads(request.data)
            return Response()

        with patch('app.desktop_runner.urllib.request.urlopen', fake_open):
            result = agent.dispatch({'action': 'ui_click', 'selector': {'name': 'OK'}, 'value': ''},
                                   include_screenshot=True)
        self.assertEqual(captured['auth'], 'Bearer agent-token-value')
        self.assertTrue(captured['body']['capture_screenshot'])
        self.assertGreater(captured['response_limit'], 512 * 1024)
        self.assertNotIn('agent-token-value', json.dumps(result))

    def test_desktop_runner_facade_uses_simulator_without_agent(self):
        async def go():
            runner = DesktopRunner(mode='simulator', app_id='winforms_legacy')
            await runner.perform({'action': 'window_attach', 'selector': {'window_title': 'Legacy WinForms Application'}, 'value': ''}, {})
            result = await runner.perform({'action': 'ui_select', 'selector': {'automation_id': 'cmbStatus'}, 'value': 'Posted'}, {})
            await runner.perform({'action': 'assert_control_text', 'selector': {'automation_id': 'cmbStatus'}, 'value': 'Posted'}, {})
            return result
        self.assertEqual(asyncio.run(go())['mode'], 'simulator')


class DesktopRecordingApiTests(unittest.TestCase):
    def test_desktop_capture_is_live_timed_and_exclusive_with_browser(self):
        with patch.dict(os.environ, {"TF_WINDOWS_AGENT_URL": "", "TF_WINDOWS_AGENT_TOKEN": ""}):
            with TestClient(app) as client:
                project = client.post('/api/projects', json={'name': 'Desktop capture API'}).json()
                with patch('app.main._active_browser_recording_ids', return_value={'browser-session'}):
                    blocked = client.post('/api/desktop/recordings', json={
                        'project_id': project['id'], 'name': 'Desktop blocked',
                        'app_id': 'calculator', 'windows_version': 'windows_10',
                    })
                self.assertEqual(blocked.status_code, 409)
                start = client.post('/api/desktop/recordings', json={
                    'project_id': project['id'], 'name': 'Calculator proof',
                    'purpose': 'Confirm desktop evidence is captured',
                    'app_id': 'calculator', 'windows_version': 'windows_10',
                })
                self.assertEqual(start.status_code, 201)
                session = start.json()
                self.assertEqual(session['mode'], 'simulator')
                self.assertTrue(session['active'])
                self.assertTrue(session['frame'])
                recording_id = session['recording_id']

                browser = client.post('/api/recordings', json={
                    'project_id': project['id'], 'name': 'Exclusive browser', 'start_url': '/demo.html',
                    'interactive_session': True,
                })
                self.assertEqual(browser.status_code, 409)

                captured = client.post(f'/api/desktop/recordings/{recording_id}/input', json={
                    'action': 'ui_click', 'x': 0.80, 'y': 0.33,
                    'field_name': 'Two button', 'value': '',
                })
                self.assertEqual(captured.status_code, 200)
                step = captured.json()['step']
                self.assertEqual(step['selector']['automation_id'], 'num2Button')
                self.assertEqual(captured.json()['matched_control']['name'], 'Two')
                self.assertTrue(step['started_at'])
                self.assertTrue(step['finished_at'])
                self.assertGreaterEqual(step['duration_ms'], 0)
                self.assertTrue(step['screenshot'].endswith('/screenshot'))
                image = client.get(step['screenshot'])
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.headers['content-type'], 'image/png')
                self.assertTrue(image.content.startswith(b'\x89PNG'))

                live = client.get(f'/api/recordings/{recording_id}').json()
                self.assertEqual(live['purpose'], 'Confirm desktop evidence is captured')
                self.assertEqual(len(live['steps']), 1)
                self.assertEqual(live['source'], 'live')

                stopped = client.post(f'/api/desktop/recordings/{recording_id}/stop', json={})
                self.assertEqual(stopped.status_code, 200)
                self.assertTrue(stopped.json()['window_closed'])
                self.assertEqual(stopped.json()['step_count'], 1)
                self.assertFalse(client.get('/api/desktop/recording/session').json()['active'])

                queued = client.post('/api/runs', json={'target_id': recording_id, 'display_window': False})
                self.assertEqual(queued.status_code, 201)
                self.assertTrue(queued.json()['display_window'])
                run_id = queued.json()['run_id']
                for _ in range(100):
                    run = client.get(f'/api/runs/{run_id}?full=true').json()
                    if run.get('status') not in {'queued', 'running'}:
                        break
                    time.sleep(0.02)
                self.assertEqual(run['status'], 'passed')
                self.assertEqual(run['execution_target'], 'windows_desktop')
                self.assertGreaterEqual(run['elapsed_seconds'], 0)
                self.assertTrue(run['started_at'])
                self.assertTrue(run['last_step_finished_at'])
                self.assertGreaterEqual(len(run['log']), 3)  # launch, captured step, close
                for item in run['log']:
                    self.assertTrue(item['started_at'])
                    self.assertTrue(item['finished_at'])
                    self.assertIn('duration_ms', item)
                    self.assertTrue(item['screenshot'].endswith('.png'))

                browser = client.post('/api/recordings', json={
                    'project_id': project['id'], 'name': 'Browser after desktop', 'start_url': '/demo.html',
                    'interactive_session': True,
                })
                self.assertEqual(browser.status_code, 201)
                self.assertEqual(client.post(f"/api/recordings/{browser.json()['id']}/stop", json={}).status_code, 200)


class MobileTests(unittest.TestCase):
    def test_zebra_scan_and_locators(self):
        sim = MobileSimulator('zebra')
        sim.perform({'action': 'app_launch', 'selector': {}, 'value': 'com.testforge.warehouse'})
        result = sim.perform({'action': 'scan_barcode', 'selector': {'symbology': 'EAN-13', 'barcode_data': VALID_EAN}, 'value': ''})
        self.assertEqual(result['product'], 'TestForge Scanner Demo Item')
        self.assertEqual(result['datawedge']['intent']['extras']['com.symbol.datawedge.data_string'], VALID_EAN)
        with self.assertRaises(ValueError):
            sim.perform({'action': 'scan_barcode', 'selector': {'symbology': 'EAN-13', 'barcode_data': '1234567890123'}, 'value': ''})
        with self.assertRaises(ValueError):
            sim.perform({'action': 'scan_barcode', 'selector': {'symbology': 'GS1-128', 'barcode_data': 'XYZ'}, 'value': ''})
        self.assertTrue(sim.screenshot_bytes().startswith(b'\x89PNG'))

    def test_mobile_step_schema_validated(self):
        self.assertEqual(normalize_step({'action': 'device_key', 'selector': {}, 'value': 'F12'})['value'], 'F12')
        for raw in ({'action': 'device_key', 'selector': {}, 'value': 'F13'},
                    {'action': 'rotate_orientation', 'selector': {}, 'value': 'upside-down'},
                    {'action': 'tap_element', 'selector': {}, 'value': ''},
                    {'action': 'toggle_network_mode', 'selector': {}, 'value': '5g'}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_step(raw)

    def test_mobile_plan_runs_with_screenshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_plan(mobile_plan(), {}, tmp))
            self.assertEqual(result['status'], 'passed')
            self.assertTrue(list(Path(tmp).glob('step-*.png')))

    def test_mixed_plan_skips_foreign_steps_per_profile(self):
        plan = mobile_plan(matrix=[{'runner': 'windows_desktop', 'framework': 'dotnet', 'desktop_app': 'calculator'},
                                   {'runner': 'mobile_device', 'platform': 'zebra'}])
        plan['setup'] = [{'action': 'window_attach', 'selector': {'window_title': 'Calculator'}, 'value': ''}]
        plan['dataset'] = [{}]
        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(run_plan(plan, {}, tmp))
        self.assertEqual(result['status'], 'passed')
        desktop_steps = result['cases'][0]['attempts'][0]['steps']
        self.assertTrue(any(step['status'] == 'skipped' for step in desktop_steps))

    def test_mobile_runner_and_datawedge_manifest(self):
        manifest = datawedge_profile()
        self.assertEqual(manifest['intent_output']['intent_delivery'], 'broadcast')
        self.assertIn('EAN-13', manifest['barcode_input']['symbologies'])
        with self.assertRaises(ValueError):
            normalize_plan({'steps': [{'action': 'app_launch', 'selector': {}, 'value': 'a'}],
                            'matrix': [{'runner': 'mobile_device', 'platform': 'windows'}]})


class SpeedTests(unittest.TestCase):
    def test_speed_normalisation_and_profiles(self):
        self.assertEqual(normalize_speed('2'), 2.0)
        self.assertEqual(normalize_speed({'factor': 0.5}), 0.5)
        self.assertEqual(speed_profile(1.0)['pace_ms'], 0)
        self.assertGreater(speed_profile(0.5)['pace_ms'], 0)
        self.assertLess(speed_profile(2.0)['timeout_scale'], 1)
        self.assertGreater(speed_profile(0.5)['timeout_scale'], 1)
        self.assertEqual(scaled_timeout(3000, speed_profile(2.0)), 1500)
        for bad in (0, 3.5, 'fast', True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_speed(bad)

    def test_plan_speed_validation(self):
        plan = normalize_plan({'steps': [{'action': 'click', 'selector': {'primary': '#a'}, 'value': ''}],
                               'execution_speed': 0.5})
        self.assertEqual(plan['execution_speed'], 0.5)
        self.assertEqual(plan['matrix'][0]['execution_speed'], 0.5)
        with self.assertRaises(ValueError):
            normalize_plan({'steps': [{'action': 'click', 'selector': {'primary': '#a'}, 'value': ''}],
                            'execution_speed': 9})

    def test_run_api_rejects_bad_speed(self):
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/runs', json={'target_id': 'x', 'execution_speed': 9}).status_code, 422)

    def test_step_runtime_scales_timeouts(self):
        runtime = StepRuntime(None, execution_speed=2.0)
        self.assertEqual(runtime._timeout({'timeout_ms': 3000}), 1500)
        self.assertEqual(StepRuntime(None)._timeout({'timeout_ms': 3000}), 3000)


class DatasetTests(unittest.TestCase):
    def test_csv_and_json_uploads(self):
        parsed = parse_dataset_upload('rows.csv', 'input,expected\n1001,Open\n')
        self.assertEqual(parsed['dataset'], [{'input': '1001', 'expected': 'Open'}])
        secret = parse_dataset_upload('rows.csv', 'input,pin\n1001,env:TF_SECRET_PIN\n')
        self.assertEqual(secret['dataset'][1 - 1]['pin'], {'env': 'TF_SECRET_PIN'})
        parsed = parse_dataset_upload('rows.json', json.dumps([{'input': '1'}]))
        self.assertEqual(parsed['rows'], 1)
        with self.assertRaises(ValueError):
            parse_dataset_upload('rows.csv', '')

    def test_row_overrides_via_api(self):
        with TestClient(app) as client:
            parsed = client.post('/api/plans/dataset/parse', json={'filename': 'd.csv', 'content': 'input,expected\n1,Open\n'})
            self.assertEqual(parsed.status_code, 200)
            applied = client.post('/api/plans/dataset/apply-overrides',
                                  json={'dataset': parsed.json()['dataset'], 'overrides': {'0': {'expected': 'Closed'}}})
            self.assertEqual(applied.json()['dataset'][0]['expected'], 'Closed')
        self.assertEqual(apply_row_overrides([{'a': '1'}], [{'a': '2'}]), [{'a': '2'}])


class SupportMatrixTests(unittest.TestCase):
    def test_matrix_has_thirteen_targets_and_no_fake_verification(self):
        matrix = support_matrix(environ={}, receipt=None)
        self.assertEqual(len(matrix['targets']), 13)
        names = {target['id'] for target in matrix['targets']}
        self.assertTrue({'oracle_web', 'sap_fiori', 'jd_edwards', 'odoo_erp', 'salesforce_crm',
                         'ms_dynamics_fo', 'ms_dynamics_bc', 'dotnet_desktop', 'cpp_desktop',
                         'winforms_desktop', 'android_mobile', 'ios_mobile', 'zebra_mobile'} <= names)
        with patch.object(support_matrix_module, 'load_receipt', lambda: None):
            fresh = support_matrix(environ={})
        self.assertNotIn('VERIFIED IN THIS ENVIRONMENT', {target['badge'] for target in fresh['targets']})

    def test_pilot_receipt_verifies_packs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = run_pilot_suite(Path(tmp) / 'receipt.json')
            matrix = support_matrix(environ={}, receipt=receipt)
        badges = {target['id']: target['badge'] for target in matrix['targets']}
        for slug in ERP_SLUGS:
            self.assertEqual(badges[slug], 'VERIFIED IN THIS ENVIRONMENT', slug)
        for target in ('dotnet_desktop', 'cpp_desktop', 'winforms_desktop', 'android_mobile', 'ios_mobile', 'zebra_mobile'):
            self.assertEqual(badges[target], 'SIMULATOR READY', target)
        self.assertTrue(all(row['status'] == 'passed' for row in receipt['results']))

    def test_matrix_api_and_custom_desktop_apps(self):
        with TestClient(app) as client:
            matrix = client.get('/api/roadmap/support-matrix')
            self.assertEqual(matrix.status_code, 200)
            self.assertEqual(len(matrix.json()['targets']), 13)
            apps = client.get('/api/roadmap/desktop-apps').json()['apps']
            self.assertTrue(any(app['id'] == 'calculator' for app in apps))
            created = client.post('/api/roadmap/desktop-apps',
                                  json={'profile': {'name': 'Orders Custom', 'kind': 'access_vba'}})
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()['app']['kind'], 'access_vba')
            self.assertEqual(client.post('/api/roadmap/desktop-apps',
                                         json={'profile': {'name': 'Bad', 'kind': 'nope'}}).status_code, 422)

    def test_secrets_never_appear_in_matrix(self):
        env = {'TF_WINDOWS_AGENT_URL': 'https://windows.example.test', 'TF_WINDOWS_AGENT_TOKEN': 'do-not-leak-me',
               'TF_MOBILE_HUB_URL': 'https://hub.example.test', 'TF_MOBILE_HUB_TOKEN': 'do-not-leak-me-either'}
        blob = json.dumps(support_matrix(environ=env, receipt={'results': []}))
        self.assertNotIn('do-not-leak', blob)
        self.assertTrue(any(t['live_configured'] for t in support_matrix(environ=env, receipt={'results': []})['targets']))


class WebHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_lookup_is_scoped_to_the_table(self):
        class Cell:
            def __init__(self, text): self._text = text
            async def inner_text(self, timeout=None): return self._text

        class Row:
            def __init__(self, cells): self._cells = cells
            def locator(self, kind): return self
            def nth(self, index): return Cell(self._cells[index])

        class Headers:
            def __init__(self, texts): self._texts = texts
            async def count(self): return len(self._texts)
            def nth(self, index): return Cell(self._texts[index])

        class Table:
            def locator(self, kind):
                if kind == 'th': return Headers(['Order', 'Status'])
                return self

            def filter(self, has_text=None):
                self.first = Row(['5001001', 'Confirmed'])
                return self

        runtime = StepRuntime(None)
        with patch('app.step_runtime.locator', return_value=Table()):
            text = await runtime._table_cell({'primary': 'table', 'row_text': '5001001', 'column': 'Status'}, 100)
        self.assertEqual(text, 'Confirmed')


class RecorderAndExportTests(unittest.TestCase):
    def test_recorder_prefers_stable_enterprise_attributes(self):
        for script in (_ELEMENT_JS, _FOCUSED_JS):
            self.assertLess(script.index('data-testid'), script.index('data-sap-ui-id'))
            self.assertLess(script.index('data-sap-ui-id'), script.index('data-field'))
            self.assertIn('stableId', script)
            # A stable id beats placeholder copy (LB-REC-02 records #name, not
            # input[placeholder="Your name"]): placeholder text changes with
            # copy/i18n, while stableId() already filters generated ids.
            if 'stableId(node.id)' in script:
                self.assertLess(script.index('stableId(node.id)'),
                                script.index('const placeholder'))
            else:
                self.assertLess(script.index('stableId(el.id)'),
                                script.index('getAttribute("placeholder")'))

    def test_new_actions_are_canonical_everywhere(self):
        self.assertEqual(normalize_step({'action': 'table_lookup', 'selector': {'primary': 'table', 'row_text': 'Ada', 'column': 'Status'}, 'value': 'status'})['action'], 'table_lookup')
        self.assertEqual(normalize_step({'action': 'pick_suggestion', 'selector': {'primary': '#x'}, 'value': 'Acme'})['action'], 'pick_suggestion')
        runtime_js = (Path(__file__).parent.parent / 'app/export_runtime.js').read_text(encoding='utf-8')
        for marker in ('pick_suggestion', 'table_lookup', 'assert_table_cell', 'expected', 'DESKTOP_ACTIONS', 'MOBILE_ACTIONS'):
            self.assertIn(marker, runtime_js)

    def test_export_bundles_for_desktop_and_mobile_plans(self):
        from app.export_formats import write_exports
        plan = {
            'matrix': [{'runner': 'windows_desktop', 'framework': 'dotnet', 'desktop_app': 'calculator'},
                       {'runner': 'mobile_device', 'platform': 'zebra'}],
            'setup': [{'action': 'window_attach', 'selector': {'window_title': 'Calculator'}, 'value': ''}],
            'steps': [
                {'action': 'ui_type', 'selector': {'automation_id': 'CalculatorResults'}, 'value': '2+3'},
                {'action': 'ui_click', 'selector': {'automation_id': 'equalButton'}, 'value': ''},
                {'action': 'assert_control_text', 'selector': {'automation_id': 'CalculatorResults'}, 'value': '5'},
                {'action': 'scan_barcode', 'selector': {'symbology': 'EAN-13', 'barcode_data': VALID_EAN}, 'value': ''},
            ],
            'dataset': [{'expr': 'x'}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_exports(folder, {'id': 'r1', 'project_id': 'p1', 'name': 'Bundles', 'steps': [], 'execution_plan': plan})
            for name in ('desktop_test.py', 'DesktopRunner.cs', 'desktop-steps.json', 'mobile_test.py',
                         'mobile_test.js', 'zebra-datawedge-profile.json', 'testforge_desktop.py',
                         'testforge_mobile.py', 'testforge_speed.py', 'testforge_desktop.js', 'testforge_mobile.js'):
                self.assertTrue((folder / name).is_file(), name)
            env = {**os.environ, 'PYTHONPATH': str(folder)}
            ran = subprocess.run([os.sys.executable, 'desktop_test.py'], cwd=folder, env=env,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn('desktop plan passed', ran.stdout)
            if shutil.which('node'):
                ran = subprocess.run(['node', 'mobile_test.js'], cwd=folder, capture_output=True, text=True, timeout=60)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertIn('mobile steps passed', ran.stdout)


if __name__ == '__main__':
    unittest.main()
