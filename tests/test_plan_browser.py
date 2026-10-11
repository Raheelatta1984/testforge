"""Opt-in real Chromium checks: drafts, fixtures, data, profiles and domain packs."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()
from tests.test_browser_features import Handler, HTML
from app.browser import launch_kwargs
from app.plan_runtime import run_plan
from app.domain_packs import PACKS, template
from app import library_store
from app.db import Batch, Recording, Run, SessionLocal
from app.main import app
from fastapi.testclient import TestClient


class PlanHandler(Handler):
    flaky_count=0
    def do_GET(self):
        if self.path.startswith('/domain-demo.html'):
            body=Path('app/static/domain-demo.html').read_bytes()
        else:
            body=HTML.replace(b'</body>', b'<p id="auth"></p><script>document.querySelector("#auth").textContent=document.cookie;</script></body>')
        if self.path.startswith('/flaky'):
            PlanHandler.flaky_count+=1
            if PlanHandler.flaky_count==1: body=body.replace(b'Hello, ',b'First attempt, ')
        self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)


@unittest.skipUnless(os.environ.get('TF_TEST_BROWSER')=='1','Requires explicit browser opt-in')
class PlanBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),PlanHandler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=5)

    def wait(self,client,path):
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            value=client.get(path).json()
            if value.get('status') not in {'queued','running'}: return value
            time.sleep(.025)
        self.fail('Browser job did not finish')

    def plan(self):
        return {'setup':[{'action':'navigate','value':self.base}],
            'flows':{'greet':[{'action':'fill','selector':{'primary':'#name'},'value':'{{user}}'}, {'action':'click','selector':{'primary':'#go'}}]},
            'steps':[{'flow':'greet'},{'action':'assert_text','selector':{'primary':'#out'},'value':'Hello, {{user}}'}],
            'teardown':[{'action':'fill','selector':{'primary':'#name'},'value':''},{'action':'assert_value','selector':{'primary':'#name'},'value':''}],
            'dataset':[{'user':'Ada'},{'user':'Grace'}]}

    def test_BR_PLAN_01_draft_then_publish_and_replay_and_python_export(self):
        project=library_store.create_project('Real draft test','')
        body={'project_id':project['id'],'name':'Data and fixtures','execution_plan':self.plan()}
        with TestClient(app) as client, patch.object(library_store,'_finish',return_value=False) as publish:
            response=client.post('/api/imports/test',json=body)
            self.assertEqual(response.status_code,202,response.text)
            draft=self.wait(client,'/api/imports/tests/'+response.json()['id'])
            self.assertEqual(draft['status'],'passed',draft)
            self.assertEqual(draft['result']['total'],2)
            publish.assert_not_called()
            self.assertEqual(library_store.list_recordings(project['id']),[])
            saved=client.post('/api/imports/save',json={**body,'draft_id':draft['id']})
            self.assertEqual(saved.status_code,201,saved.text);self.assertEqual(publish.call_count,1)
            recording=saved.json()
            run=client.post('/api/runs',json={'recording_id':recording['id'],'display_window':False}).json()
            result=self.wait(client,'/api/runs/'+run['run_id'])
            self.assertEqual(result['status'],'passed',result)
            self.assertEqual(len(result['log']),12)
            self.assertEqual(client.get('/api/runs/'+run['run_id']+'/report.html').status_code,200)
            plain=library_store.create_recording(project['id'],'Ordinary neighbour',self.base,steps=[{'action':'navigate','value':self.base}])
            other_project=library_store.create_project('Isolated second project','')
            other=library_store.create_recording(other_project['id'],'Other account scope',self.base,steps=[{'action':'navigate','value':self.base}])
            batch=client.post('/api/runs/batch',json={'recording_ids':[plain['id'],recording['id'],other['id']]}).json()
            result=self.wait(client,'/api/runs/batch/'+batch['batch_id'])
            self.assertEqual(result['status'],'passed',result)
            self.assertGreaterEqual(result['report']['resources']['browsers_launched'],1)
            self.assertEqual(result['report']['total'],3)
        folder=library_store.library_dir()/'projects'/project['id']/'recordings'/recording['id']/'resources'
        self.assertTrue(os.environ.get('TF_TEST_NODE_MODULES'),'Export parity requires TF_TEST_NODE_MODULES')
        env={**os.environ,'NODE_PATH':os.environ['TF_TEST_NODE_MODULES']}
        cli=str(Path(env['NODE_PATH'])/'@playwright/test/cli.js')
        commands=[[sys.executable,str(folder/'playwright_test.py')],['node',str(folder/'playwright_test.js')],['node',cli,'test','--config',str(folder/'playwright.config.ts')]]
        for command in commands:
            completed=subprocess.run(command,cwd=folder,env=env,capture_output=True,text=True,timeout=45)
            self.assertEqual(completed.returncode,0,completed.stdout[-2000:]+completed.stderr[-2000:])
        plan=json.loads((folder/'execution-plan.json').read_text());plan['steps'][-1]['value']='wrong'
        plan['steps'][-1]['selector']['timeout_ms']=100
        (folder/'execution-plan.json').write_text(json.dumps(plan))
        for command in commands:
            completed=subprocess.run(command,cwd=folder,env=env,capture_output=True,text=True,timeout=45)
            self.assertNotEqual(completed.returncode,0,'Incorrect exported plan passed')

    def test_BR_PLAN_02_device_matrix_auth_and_secret_references(self):
        plan=self.plan();plan['dataset']=[{'user':{'env':'TF_SECRET_PERSON'}}]
        plan['steps'].append({'action':'assert_text','selector':{'primary':'#auth'},'value':'fixture=synthetic'})
        plan['matrix']=[{'device':device,'auth_profile':'FIXTURE'} for device in ('desktop','tablet','mobile')]
        state={'expires_at':time.time()+300,'state':{'cookies':[{'name':'fixture','value':'synthetic','domain':'127.0.0.1','path':'/','expires':time.time()+300,'httpOnly':False,'secure':False,'sameSite':'Lax'}],'origins':[]}}
        with patch.dict(os.environ,{'TF_AUTH_STATE_FIXTURE':json.dumps(state),'TF_SECRET_PERSON':'Synthetic person'}),tempfile.TemporaryDirectory() as folder:
            result=asyncio.run(run_plan(plan,{},folder,chromium_options=launch_kwargs()))
        self.assertEqual(result['status'],'passed',result);self.assertEqual(result['total'],3)
        self.assertNotIn('Synthetic person',json.dumps(result))
        self.assertNotIn('fixture=synthetic',json.dumps(result))

    def test_BR_PLAN_03_teardown_after_failure_and_explicit_retry_gate(self):
        plan=self.plan();plan['dataset']=[{'user':'Ada'}];plan['retries']=1
        plan['steps'][-1]={'action':'assert_text','selector':{'primary':'#out','timeout_ms':100},'value':'incorrect'}
        with tempfile.TemporaryDirectory() as folder:
            result=asyncio.run(run_plan(plan,{},folder,chromium_options=launch_kwargs()))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['cases'][0]['attempts']),2)
        for attempt in result['cases'][0]['attempts']:
            self.assertEqual(attempt['steps'][-1]['stage'],'teardown')
            self.assertEqual(attempt['steps'][-1]['status'],'passed')

        PlanHandler.flaky_count=0
        plan['setup'][0]['value']=self.base+'/flaky'
        plan['steps'][-1]['value']='Hello, Ada'
        with tempfile.TemporaryDirectory() as folder:
            flaky=asyncio.run(run_plan(plan,{},folder,chromium_options=launch_kwargs()))
        self.assertEqual(flaky['cases'][0]['status'],'flaky')
        self.assertEqual(flaky['status'],'failed','Flaky is not a clean pass for publication/CI')

    def test_BR_PLAN_04_all_synthetic_domain_packs(self):
        for slug in PACKS:
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as folder:
                plan=template(slug)['execution_plan'];plan['variables']={'demo_url':self.base+'/domain-demo.html'}
                result=asyncio.run(run_plan(plan,{},folder,chromium_options=launch_kwargs()))
                self.assertEqual(result['status'],'passed',result)
                self.assertEqual(result['total'],2)


if __name__=='__main__': unittest.main()
