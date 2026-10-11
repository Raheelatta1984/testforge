"""Scenarios, encrypted inputs, single-capture PDFs and remote retention contracts."""
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock, patch
import uuid
from cryptography.fernet import Fernet
from tests.bootstrap import ensure_test_env
ensure_test_env()
from fastapi.testclient import TestClient
from app.main import app
from app import library_store, pdf_reports, scenario_routes
from app.db import Batch, Recording, Run, SessionLocal, Variable, resolve_variables
from app.secrets_store import decrypt, encrypt, MASK, protected, scrub_entry


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS': Fernet.generate_key().decode(), 'TF_REPORTS_GITHUB':'0'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.project = library_store.create_project('D365 synthetic scenario fixture', '')
        self.pid = self.project['id']

    def recording(self, name, steps=None):
        return library_store.create_recording(self.pid, name, '', steps=steps or [{'action':'navigate','value':'http://fixture.test'}])

    def scenario(self, recordings, values=None):
        return scenario_routes.save(self.pid, {'name':'Validate Application login & logout', 'plans':[{'name':'Login then logout',
            'entries':[{'recording_id':r['id'], 'variables':values or {}} for r in recordings]}]})

    def test_UT_SCENE_01_encrypted_variable_api_storage_reuse_and_rotation(self):
        value = 'synthetic-password-that-must-not-leak'
        with TestClient(app) as client:
            response = client.post('/api/variables', json={'project_id':self.pid,'name':'password','value':value,'is_secret':True,'category':'Parameter'})
            self.assertEqual(response.status_code,201,response.text)
            data=response.json(); self.assertEqual(data['value'],''); self.assertTrue(data['is_secret'])
            self.assertEqual(data['category'],'Parameter')
            with SessionLocal() as db:
                stored = db.get(Variable,data['id']).value
                self.assertTrue(protected(stored)); self.assertNotIn(value,stored)
                self.assertEqual(str(resolve_variables(db,self.pid)['password']), value)
                self.assertEqual(db.get(Variable,data['id']).value,stored)
            response=client.patch('/api/variables/'+data['id'],json={'is_secret':True})
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotIn(value,response.text)
            self.assertEqual(client.patch('/api/variables/'+data['id'],json={'is_secret':False}).status_code,422)
            old=os.environ['TF_VARIABLE_ENCRYPTION_KEYS']; new=Fernet.generate_key().decode()
            with patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS':new+','+old}):
                self.assertEqual(decrypt(stored),value)
                client.patch('/api/variables/'+data['id'],json={'value':'rotated-synthetic'})
            with patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS':new}), SessionLocal() as db:
                self.assertEqual(str(resolve_variables(db,self.pid)['password']),'rotated-synthetic')
            text=''.join(p.read_text() for p in (library_store.library_dir()/'projects'/self.pid).rglob('*.json'))
            self.assertNotIn(value,text); self.assertNotIn('rotated-synthetic',text)

    def test_UT_SCENE_02_missing_wrong_keys_and_ciphertext_injection_fail_closed(self):
        with patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS':''}), TestClient(app) as client:
            result=client.post('/api/variables',json={'project_id':self.pid,'name':'password','value':'not-allowed','is_secret':True})
            self.assertEqual(result.status_code,422)
        sealed=encrypt('private')
        with patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS':Fernet.generate_key().decode()}), self.assertRaises(ValueError):
            decrypt(sealed)
        with self.assertRaises(ValueError): library_store.create_variable(self.pid,'foo',sealed)
        self.assertEqual(scrub_entry({'status':'passed','action':'fill','value':'passed'},['passed']),
                         {'status':'passed','action':'fill','value':'[redacted]'})

    def test_UT_SCENE_03_order_repeat_links_snapshot_and_masked_defaults(self):
        library_store.create_variable(self.pid,'password','project-secret',is_secret=True)
        login=self.recording('Login', [{'action':'navigate','value':'http://fixture.test'}, {'action':'fill','value':'{{password}}','selector':{'primary':'#password'}}])
        logout=self.recording('Logout')
        scenario=self.scenario([login,logout,login], {'password':'linked-secret'})
        plan=scenario['plans'][0]
        self.assertEqual(plan['entries'][0]['variables']['password'],MASK)
        selected,meta=scenario_routes.prepare(self.pid,scenario['id'],plan['id'],{'0':{'password':'one-off-secret'}})
        self.assertEqual([r['id'] for r in selected],[login['id'],logout['id'],login['id']])
        self.assertEqual(decrypt(selected[0]['input_overrides']['password']),'one-off-secret')
        self.assertEqual(decrypt(selected[2]['input_overrides']['password']),'linked-secret')
        self.assertEqual(len(selected[0]['snapshot_steps']),2)
        source=scenario_routes.load(self.pid,scenario['id'])
        self.assertNotIn('linked-secret',json.dumps(source)); self.assertNotIn('one-off-secret',json.dumps(source))
        # Stable entry IDs preserve encrypted defaults even after drag/reorder.
        plan['entries'].reverse()
        saved=scenario_routes.save(self.pid,scenario,scenario['id'])
        self.assertEqual(saved['revision'],2)
        with self.assertRaises(ValueError): scenario_routes.save(self.pid,scenario,scenario['id'])

    def test_UT_SCENE_04_explicit_batch_order_one_session_and_no_duplicate_actions(self):
        from app.batch_runner import execute_batch
        from tests.fakes import BatchFakePlaywright
        login=self.recording('Long login',[{'action':'navigate','value':'http://fixture.test'}, {'action':'click','selector':{'primary':'#go'}}])
        logout=self.recording('Short logout')
        scenario=self.scenario([login,logout,login]); plan=scenario['plans'][0]
        selected,meta=scenario_routes.prepare(self.pid,scenario['id'],plan['id'],{})
        with SessionLocal() as db:
            batch=Batch(status='queued',total=3,options={'ordered':True,'scenario':meta});db.add(batch);db.commit();bid=batch.id
            for index,item in enumerate(selected):
                db.add(Run(recording_id=item['id'],batch_id=bid,sequence=index,status='queued',snapshot_steps=item['snapshot_steps']))
            db.commit()
        fake=BatchFakePlaywright(known=['#go'])
        with patch('app.batch_runner.MAX_RSS_MB',0):
            result=asyncio.run(execute_batch(bid,None,playwright_factory=lambda:fake,screenshots='all',retry_transient=False,share_session=True))
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual([r['recording_id'] for r in result['results']],[login['id'],logout['id'],login['id']])
        self.assertEqual(result['resources']['contexts_opened'],1)
        self.assertEqual(sum(len(p.shots) for p in fake.pages),5)

    def test_UT_SCENE_05_recording_inputs_persist_mask_and_seal_after_definition_deleted(self):
        var=library_store.create_variable(self.pid,'password','project-secret',is_secret=True)
        rec=self.recording('Login')
        saved=scenario_routes.save_inputs(rec['id'],{'variables':{'password':'default-secret'}})
        self.assertEqual(saved['defaults'],{'password':MASK})
        library_store.delete_variable(var['id'])
        saved=scenario_routes.save_inputs(rec['id'],{'variables':{'password':'replacement-secret'}})
        self.assertEqual(saved['defaults'],{'password':MASK})
        with SessionLocal() as db:
            self.assertEqual(decrypt(db.get(Recording,rec['id']).input_defaults['password']),'replacement-secret')
        scene=self.scenario([rec],{'password':'linked-after-delete'})
        self.assertEqual(scene['plans'][0]['entries'][0]['variables']['password'],MASK)

    def test_UT_SCENE_06_pdfs_use_existing_jpeg_and_cache_after_completion(self):
        from PIL import Image
        from app.config import ARTIFACTS
        rec=self.recording('Evidence')
        with SessionLocal() as db:
            row=Run(recording_id=rec['id'],status='passed',finished_at=datetime.utcnow(),execution_log=[]);db.add(row);db.commit();rid=row.id
            directory=Path(ARTIFACTS)/'runs'/rid;directory.mkdir(parents=True)
            Image.new('RGB',(100,60),'navy').save(directory/'step-1.jpg',quality=35)
            row.execution_log=[{'order':1,'action':'fill','label':'Login password','value':'[redacted]','status':'passed','screenshot':f'/api/runs/screenshot/{rid}/step-1.jpg'}];db.commit()
        path,meta=pdf_reports.generate('run',rid)
        self.assertTrue(path.read_bytes().startswith(b'%PDF'));self.assertEqual(meta['steps'],1)
        self.assertIn(b'/DCTDecode',path.read_bytes(),'JPEG should remain DCT, not be re-encoded')
        stamp=path.stat().st_mtime_ns
        with patch('app.pdf_reports.draw',side_effect=AssertionError('duplicate rendering')), TestClient(app) as client:
            self.assertEqual(client.get(f'/api/runs/{rid}/report.pdf').status_code,200)
            self.assertIn('attachment',client.get(f'/api/runs/{rid}/report.pdf?download=true').headers['content-disposition'])
        self.assertEqual(path.stat().st_mtime_ns,stamp)
        pdf_reports.sweep(now=meta['expires_at']+1,force=True)
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ValueError,'expired'): pdf_reports.generate('run',rid)

    def test_UT_SCENE_07_remote_oldest_first_expiry_hourly_and_restart_discovery(self):
        now=time.time(); calls=[]
        def asset(age):
            return {'id':int(age),'name':f'tf-{int(now-age)}-run-{uuid.uuid4()}.pdf'}
        assets=[asset(49*3600),asset(1),asset(52*3600)]
        class Store:
            def release(self):return {'id':1}
            def assets(self,_):return assets
            def delete(self,identity):calls.append(identity)
        with patch.dict(os.environ,{'TF_REPORTS_GITHUB':'1'}),patch('app.pdf_reports.ReleaseStore',Store):
            pdf_reports.sweep(now=now,force=True)
            self.assertEqual(calls,[52*3600,49*3600])
            pdf_reports.sweep(now=now+100)
            self.assertEqual(len(calls),2,'must not poll GitHub more than hourly')

    def test_UT_SCENE_08_release_transport_consumes_status_payload_contract(self):
        with patch('app.pdf_reports.github_api.GitHubClient') as client:
            instance=client.return_value;instance.slug='fixture/repo';instance.token='synthetic-token';instance.branch='fixture'
            instance.request.side_effect=[(200,{'id':3,'upload_url':'https://uploads.github.com/repos/fixture/repo/releases/3/assets{?name,label}'}),(200,[])]
            store=pdf_reports.ReleaseStore();release=store.release();self.assertEqual(release['id'],3)
            self.assertEqual(store.assets(release),[])

    def test_UT_SCENE_09_invalid_scenarios_and_override_types_are_rejected(self):
        rec=self.recording('Fixture')
        for body in [{'name':'x','plans':[]},{'name':'','plans':[{}]}, {'name':'x','plans':[{'name':'p','entries':[{'recording_id':rec['id'],'variables':{'pw':MASK}}]}]}]:
            with self.subTest(body=body),self.assertRaises(ValueError):scenario_routes.save(self.pid,body)
        scene=self.scenario([rec]); plan=scene['plans'][0]
        for overrides in [{'9':{}},{'0':{'bad':[]}}, {'0':{'bad':float('nan')}}]:
            with self.subTest(overrides=overrides),self.assertRaises(ValueError):scenario_routes.prepare(self.pid,scene['id'],plan['id'],overrides)

    def test_UT_SCENE_10_single_run_overrides_are_sealed_without_changing_defaults(self):
        library_store.create_variable(self.pid,'password','persistent-secret',is_secret=True)
        rec=self.recording('Fixture')
        with patch('app.main.execute_run_task',new=AsyncMock()),TestClient(app) as client:
            response=client.post('/api/runs',json={'target_id':rec['id'],'variables':{'password':'one-off'},'display_window':False})
            self.assertEqual(response.status_code,201,response.text)
            with SessionLocal() as db:
                row=db.get(Run,response.json()['run_id'])
                self.assertTrue(protected(row.input_overrides['password']))
                self.assertEqual(decrypt(row.input_overrides['password']),'one-off')
                self.assertEqual(str(resolve_variables(db,self.pid)['password']),'persistent-secret')

    def test_UT_SCENE_11_queued_project_values_are_frozen_separately_from_plan_overrides(self):
        from app.secrets_store import freeze_inputs, reveal_map
        var=library_store.create_variable(self.pid,'username','before',category='Parameter')
        base,overrides=freeze_inputs([self.pid], {'extra':'default'}, {'extra':'once'})
        library_store.update_variable(var['id'],value='after')
        self.assertEqual(reveal_map(base)['username'],'before')
        self.assertNotIn('username',overrides,'Project defaults must not override plan datasets')
        self.assertEqual(overrides,{'extra':'once'})
        rec=self.recording('Frozen fixture')
        scene=self.scenario([rec]);plan=scene['plans'][0]
        selected,_=scenario_routes.prepare(self.pid,scene['id'],plan['id'],{})
        library_store.update_variable(var['id'],value='later')
        self.assertEqual(selected[0]['variable_snapshot']['username'],'after')

    def test_UT_SCENE_12_remote_upload_once_and_only_summary_committed(self):
        rec=self.recording('Remote report fixture')
        with SessionLocal() as db:
            run=Run(recording_id=rec['id'],status='passed',finished_at=datetime.utcnow(),execution_log=[])
            db.add(run);db.commit();rid=run.id
        with patch.dict(os.environ, {'TF_REPORTS_GITHUB':'1'}),patch('app.pdf_reports.ReleaseStore') as remote,patch.object(library_store,'_finish',return_value=True):
            remote.return_value.upload.return_value={'id':123,'browser_download_url':'https://github.com/fixture/repo/releases/download/reports/fixture.pdf'}
            pdf_reports.finalize('run',rid);pdf_reports.finalize('run',rid)
            self.assertEqual(remote.return_value.upload.call_count,1)
            summary=library_store.library_dir()/'reports'/f'run-{rid}.json'
            self.assertTrue(summary.is_file());data=json.loads(summary.read_text())
            self.assertEqual(data['asset_id'],123);self.assertNotIn('runs',data)
            self.assertEqual(list(summary.parent.glob('*.pdf')),[])
            self.assertTrue(pdf_reports.metadata('run',rid)['summary_published'])

    def test_UT_SCENE_13_secret_redaction_precedes_error_truncation(self):
        from app.executor import execute_run
        from tests.fakes import FakePage, FakePlaywright
        secret='long-sensitive-password-'*40
        library_store.create_variable(self.pid,'password',secret,is_secret=True)
        rec=self.recording('Long secret failure')
        with SessionLocal() as db:
            run=Run(recording_id=rec['id'],status='queued');db.add(run);db.commit();rid=run.id
        events=[]
        async def event(payload):events.append(payload)
        fake=FakePlaywright(FakePage())
        with patch('app.executor.async_playwright',lambda:fake),patch('app.executor.replay_step',new=AsyncMock(side_effect=RuntimeError('Expected '+secret))):
            asyncio.run(execute_run(rid,event,display_window=False))
        self.assertNotIn(secret[:100],json.dumps(events))
        self.assertIn('[redacted]',json.dumps(events))
        with SessionLocal() as db:
            self.assertNotIn(secret[:100],json.dumps(db.get(Run,rid).execution_log))

    def test_UT_SCENE_14_unconfigured_keyring_is_reported_before_a_save_is_attempted(self):
        """A missing keyring must be discoverable, not only discovered by failing.

        The dashboard disables "Encrypt at rest" from this report, so it has to
        say which variable to set and how to produce a key - and it must never
        carry key material itself.
        """
        from app import secrets_store
        live = os.environ['TF_VARIABLE_ENCRYPTION_KEYS']
        with patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS': ''}), TestClient(app) as client:
            report = client.get('/api/health').json()['encryption']
            self.assertEqual((report['configured'], report['ready'], report['keys']), (False, False, 0))
            self.assertEqual(report['variable'], 'TF_VARIABLE_ENCRYPTION_KEYS')
            self.assertIn('Fernet.generate_key()', report['generate_command'])
            # The save path and the probe must blame the same thing.
            refused = client.post('/api/variables', json={'project_id': self.pid, 'name': 'password',
                                                          'value': 'not-allowed', 'is_secret': True})
            self.assertEqual(refused.status_code, 422)
            for message in (report['reason'], refused.json()['detail']):
                self.assertIn('TF_VARIABLE_ENCRYPTION_KEYS', message)
                self.assertIn('Fernet.generate_key()', message)
        with patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS': 'not-a-fernet-key'}):
            broken = secrets_store.status()
            self.assertEqual((broken['configured'], broken['ready']), (True, False))
            self.assertIn('not a usable keyring', broken['reason'])
        with patch.dict(os.environ, {'TF_VARIABLE_ENCRYPTION_KEYS': f'{Fernet.generate_key().decode()},{live}'}):
            rotating = secrets_store.status()
            self.assertEqual((rotating['configured'], rotating['ready'], rotating['keys']), (True, True, 2))
            self.assertIsNone(rotating['reason'])
            self.assertNotIn(live, json.dumps(rotating))

    def test_UT_SCENE_15_demo_tab_is_registered_and_its_fixtures_are_served(self):
        """The Demo tab must exist in the nav and everything it points at must resolve.

        It is the only route a new user has to recording and replay without a
        real application, so a stale script tag or a renamed fixture silently
        removes that route.
        """
        static = Path(library_store.__file__).resolve().parent / 'static'
        dashboard = (static / 'index.html').read_text()
        self.assertIn('/demo-components.js', dashboard)
        self.assertIn('["demo","🧪"]', dashboard)
        self.assertIn('tab === "demo" && e(DemoTab', dashboard)
        # Every tab needs a hover/assistive name: the bar is icon-only.
        self.assertIn("title: k.toUpperCase()", dashboard)
        component = (static / 'demo-components.js').read_text()
        self.assertIn('function DemoTab', component)
        for fixture in ('/demo.html', '/domain-demo.html'):
            self.assertTrue((static / fixture.lstrip('/')).is_file(), fixture)
            self.assertIn(fixture, component)
        with TestClient(app) as client:
            for path in ('/demo-components.js', '/demo.html', '/domain-demo.html'):
                self.assertEqual(client.get(path).status_code, 200, path)
            packs = client.get('/api/imports/packs').json()
            self.assertEqual({p['id'] for p in packs},
                             {'commerce', 'banking', 'insurance', 'hrms', 'travel'})
            # The tab prints a one-line blurb per pack; an unexplained pack is a bug.
            for pack in packs:
                self.assertIn(f"  {pack['id']}:", component)
                self.assertEqual(client.get(f"/api/imports/packs/{pack['id']}").status_code, 200)
