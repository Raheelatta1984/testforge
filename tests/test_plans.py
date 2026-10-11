"""Pure and API checks for isolated drafts and bounded sequential plans."""
import asyncio
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock, patch

from tests.bootstrap import ensure_test_env
ensure_test_env()
from fastapi.testclient import TestClient
from app.main import app
from app import library_store, import_drafts
from app.db import Recording, SessionLocal, resolve_variables
from app.plan_runtime import normalize_plan, resolve_values, auth_state
from app.script_import import parse_file

STEP = {'action': 'navigate', 'value': 'http://example.test'}
PASS = {'status': 'passed', 'cases': [], 'total': 1, 'sequential': True}


class PlanTests(unittest.TestCase):
    def test_UT_PLAN_01_flows_fixtures_datasets_and_bounds(self):
        plan = normalize_plan({'flows': {'open': [STEP]}, 'steps': [{'flow':'open'}],
            'dataset':[{'user':'Ada'}, {'user':'Grace'}], 'teardown':[STEP],
            'matrix':[{'device':'mobile'}, {'device':'desktop'}]})
        self.assertEqual(plan['steps'][0]['action'], 'navigate')
        self.assertEqual(plan['retries'], 0)
        for raw in [
            {'steps':[{'flow':'a'}], 'flows':{'a':[{'flow':'a'}]}},
            {'steps':[STEP], 'dataset':[{}]*26},
            {'steps':[dict(STEP,repeat_count=100)], 'dataset':[{}]*21},
            {'steps':[STEP], 'retries':2},
            {'steps':[STEP], 'matrix':[{'browser':'unknown'}]},
            {'steps':[STEP], 'matrix':[{'device':'unknown'}]},
            {'steps':[STEP], 'variables':{'token':{'env':'GITHUB_TOKEN'}}},
            {'steps':[STEP], 'parallel':True},
        ]:
            with self.subTest(raw=raw), self.assertRaises(ValueError): normalize_plan(raw)

    def test_UT_PLAN_02_secret_and_auth_references_fail_closed(self):
        with patch.dict(os.environ, {'TF_SECRET_TEST':'fixture-secret'}):
            self.assertEqual(resolve_values({'pw':{'env':'TF_SECRET_TEST'}})['pw'],'fixture-secret')
        with self.assertRaises(ValueError): resolve_values({'pw':{'env':'TF_SECRET_MISSING_TEST'}})
        for value in ['', '{}', json.dumps({'expires_at':0,'state':{'cookies':[],'origins':[]}})]:
            with patch.dict(os.environ, {'TF_AUTH_STATE_TEST': value}), self.assertRaises(ValueError): auth_state('TEST')
        value=json.dumps({'expires_at':time.time()+60,'state':{'cookies':[],'origins':[]}})
        with patch.dict(os.environ, {'TF_AUTH_STATE_TEST':value}): self.assertEqual(auth_state('TEST')['cookies'],[])
        project=library_store.create_project('Reference-only project','')
        library_store.create_variable(project['id'],'pw','env:TF_SECRET_TEST')
        with patch.dict(os.environ, {'TF_SECRET_TEST':'not-stored'}), SessionLocal() as db:
            self.assertEqual(resolve_variables(db,project['id'])['pw'],'not-stored')
        files=(library_store.library_dir()/'projects'/project['id']).rglob('*.json')
        self.assertNotIn('not-stored', ''.join(p.read_text() for p in files))

    def wait(self, client, draft_id):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=client.get('/api/imports/tests/'+draft_id).json()
            if state.get('status') not in {'queued','running'}: return state
            time.sleep(.01)
        self.fail('Mock draft did not finish')

    def test_UT_PLAN_03_draft_isolation_binding_and_idempotent_save(self):
        project=library_store.create_project('Draft isolation','')
        body={'project_id':project['id'],'name':'Tested import','steps':[STEP]}
        with TestClient(app) as client, patch.object(import_drafts,'run_plan',AsyncMock(return_value=PASS)), patch.object(library_store,'_finish',return_value=False) as publish:
            start=client.post('/api/imports/test',json=body)
            self.assertEqual(start.status_code,202,start.text)
            draft=self.wait(client,start.json()['id']);self.assertEqual(draft['status'],'passed')
            publish.assert_not_called()
            self.assertEqual(library_store.list_recordings(project['id']),[])
            with SessionLocal() as db: self.assertEqual(db.query(Recording).filter_by(project_id=project['id']).count(),0)
            wrong={**body,'steps':[{'action':'navigate','value':'http://different.test'}],'draft_id':draft['id']}
            self.assertEqual(client.post('/api/imports/save',json=wrong).status_code,422)
            saved=client.post('/api/imports/save',json={**body,'draft_id':draft['id']})
            self.assertEqual(saved.status_code,201,saved.text)
            again=client.post('/api/imports/save',json={**body,'draft_id':draft['id']})
            self.assertEqual(again.json()['id'],saved.json()['id'])
            self.assertEqual(publish.call_count,1)
            self.assertNotIn(library_store.library_dir().resolve(),import_drafts.directory().parents)

    def test_UT_PLAN_04_failed_draft_and_changed_secret_block_publication(self):
        project=library_store.create_project('Draft failures','')
        raw={'steps':[STEP],'variables':{'pw':{'env':'TF_SECRET_TEST'}}}
        body={'project_id':project['id'],'name':'Fail','execution_plan':raw}
        with TestClient(app) as client, patch.dict(os.environ,{'TF_SECRET_TEST':'one'}), patch.object(import_drafts,'run_plan',AsyncMock(return_value={**PASS,'status':'failed'})):
            draft=self.wait(client,client.post('/api/imports/test',json=body).json()['id'])
            self.assertEqual(client.post('/api/imports/save',json={**body,'draft_id':draft['id']}).status_code,422)
        with TestClient(app) as client, patch.dict(os.environ,{'TF_SECRET_TEST':'one'}), patch.object(import_drafts,'run_plan',AsyncMock(return_value=PASS)):
            draft=self.wait(client,client.post('/api/imports/test',json=body).json()['id'])
            with patch.dict(os.environ,{'TF_SECRET_TEST':'two'}):
                self.assertEqual(client.post('/api/imports/save',json={**body,'draft_id':draft['id']}).status_code,422)
            state=import_drafts.read(draft['id']);state['created_at']=time.time()-4000;import_drafts.write(state)
            self.assertEqual(client.get('/api/imports/tests/'+draft['id']).status_code,404)

    def test_UT_PLAN_05_opt_in_engines_and_safe_paths(self):
        project=library_store.create_project('Browser policy','')
        with TestClient(app) as client:
            response=client.post('/api/imports/test',json={'project_id':project['id'],'execution_plan':{'steps':[STEP],'matrix':[{'browser':'firefox'}]}})
            self.assertEqual(response.status_code,422,response.text)
            self.assertEqual(client.get('/api/imports/tests/not-a-uuid').status_code,404)
        with patch.object(import_drafts,'ARTIFACTS',str(library_store.library_dir())):
            with self.assertRaisesRegex(ValueError,'outside'): import_drafts.directory()

    def test_UT_PLAN_06_json_plan_and_round_trip(self):
        project=library_store.create_project('Plan roundtrip','')
        plan=normalize_plan({'steps':[STEP],'dataset':[{'name':'Ada'},{'name':'Grace'}], 'matrix':[{'browser':'firefox'}]})
        preview=parse_file('plan.json',json.dumps({'name':'Plan','execution_plan':plan}).encode())
        self.assertTrue(preview['can_save'],preview)
        self.assertEqual(len(preview['execution_plan']['dataset']),2)
        saved=library_store.create_recording(project['id'],'Plan','',steps=plan['steps'],execution_plan=plan)
        library_store.materialize_recording(saved['id'])
        with SessionLocal() as db: self.assertEqual(db.get(Recording,saved['id']).execution_plan,plan)
        detail=library_store.get_recording(saved['id'])
        self.assertEqual(detail['execution_plan'],plan)
        self.assertIn('execution-plan.json',detail['resources'])
        with TestClient(app) as client:
            response=client.post('/api/runs/batch',json={'recording_ids':[saved['id']]})
            self.assertEqual(response.status_code,422,response.text)

    def test_UT_PLAN_07_domain_packs_are_synthetic_bounded_plans(self):
        with TestClient(app) as client:
            packs=client.get('/api/imports/packs').json(); self.assertEqual(len(packs),5)
            for pack in packs:
                result=client.get('/api/imports/packs/'+pack['id']).json()
                plan=normalize_plan(result['execution_plan'])
                self.assertEqual(len(plan['dataset']),2)
                self.assertTrue(plan['teardown'])
            self.assertEqual(client.get('/api/imports/packs/unknown').status_code,404)

    def test_UT_PLAN_08_concurrent_draft_is_refused_and_cancellable(self):
        project=library_store.create_project('Single draft worker','')
        async def wait_forever(*args,**kwargs): await asyncio.Event().wait()
        with TestClient(app) as client, patch.object(import_drafts,'run_plan',wait_forever):
            body={'project_id':project['id'],'steps':[STEP]}
            first=client.post('/api/imports/test',json=body).json()
            self.assertEqual(client.post('/api/imports/test',json=body).status_code,409)
            self.assertEqual(client.post('/api/imports/tests/'+first['id']+'/cancel').status_code,200)
            self.assertEqual(self.wait(client,first['id'])['status'],'cancelled')

    def test_UT_PLAN_09_generated_outputs_and_symlinks_never_publish(self):
        import tempfile
        from app.github_api import collect_local_files
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'library';root.mkdir()
            (root/'recording.json').write_text('{}')
            for name in ('node_modules','run-artifacts','test-results','playwright-report','__pycache__'):
                (root/name).mkdir();(root/name/'secret.json').write_text('not-source')
            (root/'plan-result.json').write_text('not-source')
            outside=Path(directory)/'outside.json';outside.write_text('private')
            (root/'linked.json').symlink_to(outside)
            result=collect_local_files(root)
            self.assertEqual(len(result['files']),1,result)
            self.assertTrue(next(iter(result['files'])).endswith('/recording.json'))

    def test_UT_PLAN_10_plan_source_is_preserved_and_step_edits_blocked(self):
        project=library_store.create_project('Immutable tested plans','')
        source={'flows':{'open':[STEP]},'steps':[{'flow':'open'}]}
        compiled=normalize_plan(source)
        saved=library_store.create_recording(project['id'],'Reusable source','',steps=compiled['steps'],execution_plan=source)
        detail=library_store.get_recording(saved['id'])
        self.assertEqual(detail['execution_plan']['flows'],source['flows'])
        with SessionLocal() as db:
            step_id=db.get(Recording,saved['id']).steps[0].id
        with TestClient(app) as client:
            self.assertEqual(client.patch('/api/recordings/'+saved['id']+'/steps/'+step_id,json={'value':'changed'}).status_code,422)
            self.assertEqual(client.post('/api/recordings/'+saved['id']+'/steps',json=STEP).status_code,422)
            self.assertEqual(client.post('/api/recordings/'+saved['id']+'/steps/compress').status_code,422)
        folder=library_store.library_dir()/'projects'/project['id']/'recordings'/saved['id']/'resources'
        self.assertEqual(json.loads((folder/'execution-plan-source.json').read_text())['execution_plan'],source)
        compile((folder/'recorded_page.py').read_text(),'recorded_page.py','exec')
        self.assertIn('class RecordedPage',(folder/'recorded_page.js').read_text())

    def test_UT_PLAN_11_autosave_read_cannot_rollback_recorder_write(self):
        from app.db import Project
        with SessionLocal() as writer:
            row=Project(name='Transaction isolation regression',base_url='')
            writer.add(row);writer.flush();project_id=row.id
            with SessionLocal() as reader:
                # A second session must not see or roll back this uncommitted row.
                self.assertIsNone(reader.get(Project,project_id))
            writer.commit()
        with SessionLocal() as verify:
            self.assertIsNotNone(verify.get(Project,project_id))
            verify.delete(verify.get(Project,project_id));verify.commit()

    def test_UT_PLAN_12_import_save_cannot_bypass_test_gate(self):
        project=library_store.create_project('Mandatory test gate','')
        with TestClient(app) as client, patch.object(library_store,'_finish') as publish:
            response=client.post('/api/imports/save',json={'project_id':project['id'],'name':'Untested','steps':[STEP],'require_test':False})
            self.assertEqual(response.status_code,422,response.text)
            self.assertIn('draft_id',response.json()['detail'])
            publish.assert_not_called()
            self.assertEqual(library_store.list_recordings(project['id']),[])


if __name__=='__main__': unittest.main()
