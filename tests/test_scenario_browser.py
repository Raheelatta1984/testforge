"""Real Chromium: ordered cross-recording login/logout, encrypted inputs, evidence/PDF reuse."""
import io
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cryptography.fernet import Fernet
from tests.bootstrap import ensure_test_env
ensure_test_env()
from fastapi.testclient import TestClient
from app.main import app
from app import library_store, evidence
from app.db import Run, SessionLocal, resolve_variables

HTML=b'''<!doctype html><html><body><h1>Isolated login fixture</h1>
<input id="user" style="position:absolute;top:45px;left:20px">
<input id="password" type="password" style="position:absolute;top:80px;left:20px;width:260px;height:30px">
<button id="login" style="position:absolute;top:130px" onclick="document.cookie='session=fixture;path=/;SameSite=Lax';localStorage.setItem('user',document.querySelector('#user').value);document.querySelector('#state').textContent='Signed in';document.querySelector('#echo').textContent=document.querySelector('#password').value;">Login</button>
<button id="logout" style="position:absolute;top:165px" onclick="document.cookie='session=;Max-Age=0;path=/';document.querySelector('#state').textContent='Signed out'">Logout</button>
<p id="state" style="position:absolute;top:200px"></p><p id="echo" style="position:absolute;top:240px"></p>
<script>document.querySelector('#state').textContent=document.cookie.includes('session=fixture')?'Signed in':'Signed out';</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(HTML)));self.end_headers();self.wfile.write(HTML)
    def log_message(self,*args):pass


@unittest.skipUnless(os.environ.get('TF_TEST_BROWSER')=='1','Requires explicit Chromium opt-in')
class ScenarioBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=5)
    def wait(self,client,path):
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            result=client.get(path).json()
            if result.get('status') not in {'queued','running'}:return result
            time.sleep(.025)
        self.fail('Execution did not complete')
    def setup_library(self):
        project=library_store.create_project('D365 synthetic','')
        other=library_store.create_project('Separate recording scope','')
        library_store.create_variable(project['id'],'username','persistent-user',category='Parameter')
        library_store.create_variable(project['id'],'password','persistent-password',is_secret=True)
        login=library_store.create_recording(project['id'],'Login',self.base,steps=[
            {'action':'navigate','value':self.base},
            {'action':'fill','value':'{{username}}','selector':{'primary':'#user'}},
            {'action':'fill','value':'{{password}}','selector':{'primary':'#password'}},
            {'action':'click','selector':{'primary':'#login'}},
            {'action':'assert_text','value':'Signed in','selector':{'primary':'#state'}}])
        logout=library_store.create_recording(other['id'],'Logout',self.base,steps=[
            {'action':'navigate','value':self.base+'/logout'},
            {'action':'assert_text','value':'Signed in','selector':{'primary':'#state'}},
            {'action':'click','selector':{'primary':'#logout'}},
            {'action':'assert_text','value':'Signed out','selector':{'primary':'#state'}}])
        return project,login,logout

    def test_BR_SCENE_01_shared_session_secrets_one_capture_per_step_and_pdf(self):
        with patch.dict(os.environ,{'TF_VARIABLE_ENCRYPTION_KEYS':Fernet.generate_key().decode()}),patch('app.batch_runner.MAX_RSS_MB',0),TestClient(app) as client:
            project,login,logout=self.setup_library();pid=project['id']
            source={'name':'Validate Application login & logout','plans':[{'name':'Sequential session','entries':[{'recording_id':login['id'],'variables':{'username':'plan-user'}},{'recording_id':logout['id']}]}]}
            saved=client.post(f'/api/projects/{pid}/scenarios',json=source)
            self.assertEqual(saved.status_code,201,saved.text);scene=saved.json();plan=scene['plans'][0]
            with patch('app.evidence.capture_once',wraps=evidence.capture_once) as capture:
                queued=client.post(f"/api/projects/{pid}/scenarios/{scene['id']}/plans/{plan['id']}/run",json={'entry_overrides':{'0':{'username':'one-off-user','password':'one-off-sensitive-value'}}})
                self.assertEqual(queued.status_code,201,queued.text);batch=queued.json()
                result=self.wait(client,'/api/runs/batch/'+batch['batch_id'])
                self.assertEqual(result['status'],'passed',result)
                self.assertEqual([r['recording_id'] for r in result['report']['results']],[login['id'],logout['id']])
                self.assertEqual(result['report']['resources']['contexts_opened'],1)
                self.assertEqual(result['report']['resources']['browsers_launched'],1)
                self.assertEqual(capture.call_count,9)
                reports=[]
                for rid in batch['run_ids']:
                    run=client.get('/api/runs/'+rid).json()
                    self.assertNotIn('one-off-sensitive-value',json.dumps(run))
                    self.assertTrue(all(step.get('screenshot','').endswith('.jpg') for step in run['log']),run['log'])
                    report=client.get(f'/api/runs/{rid}/report.pdf');self.assertEqual(report.status_code,200,report.text[:80]);reports.append(report.content)
                response=client.get(f"/api/runs/batch/{batch['batch_id']}/report.pdf")
                self.assertEqual(response.status_code,200,response.text[:80]);self.assertTrue(response.content.startswith(b'%PDF'))
                self.assertEqual(capture.call_count,9,'PDFs must never recapture/re-execute steps')
                self.assertNotIn(b'one-off-sensitive-value',response.content)
                # A known password input is magenta-masked in the original JPEG.
                shot=client.get(f"/api/runs/screenshot/{batch['run_ids'][0]}/step-3.jpg")
                from PIL import Image
                image=Image.open(io.BytesIO(shot.content)).convert('RGB');red,green,blue=image.getpixel((50,95))
                self.assertGreater(red,190);self.assertLess(green,60);self.assertGreater(blue,190)
            with SessionLocal() as db:
                self.assertEqual(str(resolve_variables(db,pid)['password']),'persistent-password')
                self.assertNotIn('one-off-sensitive-value',json.dumps(db.get(Run,batch['run_ids'][0]).input_overrides))
            # A single recording uses the same input precedence and evidence/report path.
            run=client.post('/api/runs',json={'target_id':login['id'],'variables':{'username':'single-user'},'display_window':False}).json()
            result=self.wait(client,'/api/runs/'+run['run_id']);self.assertEqual(result['status'],'passed',result)
            self.assertEqual(len(result['log']),5)
            self.assertEqual(client.get(f"/api/runs/{run['run_id']}/report.pdf").status_code,200)

    def test_BR_SCENE_02_failure_stops_following_recordings(self):
        with patch.dict(os.environ,{'TF_VARIABLE_ENCRYPTION_KEYS':Fernet.generate_key().decode()}),patch('app.batch_runner.MAX_RSS_MB',0),TestClient(app) as client:
            project,login,logout=self.setup_library();pid=project['id']
            # Logout checks for a session before one exists: must fail and never run login.
            saved=client.post(f'/api/projects/{pid}/scenarios',json={'name':'Fail closed','plans':[{'name':'Wrong order','entries':[{'recording_id':logout['id']},{'recording_id':login['id']}]}]}).json()
            result=client.post(f"/api/projects/{pid}/scenarios/{saved['id']}/plans/{saved['plans'][0]['id']}/run",json={}).json()
            batch=self.wait(client,'/api/runs/batch/'+result['batch_id'])
            self.assertEqual(batch['status'],'failed',batch)
            self.assertEqual(batch['skipped'],1)
            later=client.get('/api/runs/'+result['run_ids'][1]).json()
            self.assertEqual(later['status'],'cancelled');self.assertEqual(later['log'],[])

    def test_BR_SCENE_03_dataset_precedence_and_execution_overrides_for_plans(self):
        with TestClient(app) as client:
            project=library_store.create_project('Plan parameter precedence','')
            library_store.create_variable(project['id'],'username','project-user',category='Parameter')
            plan={'dataset':[{'username':'row-user','expected':'row-user'}], 'steps':[
                {'action':'navigate','value':self.base},
                {'action':'fill','value':'{{username}}','selector':{'primary':'#user'}},
                {'action':'assert_value','value':'{{expected}}','selector':{'primary':'#user'}}]}
            rec=library_store.create_recording(project['id'],'AI-authored parameter fixture',self.base,execution_plan=plan)
            for overrides in [{},{'username':'one-off','expected':'one-off'}]:
                queued=client.post('/api/runs',json={'target_id':rec['id'],'variables':overrides,'display_window':False}).json()
                result=self.wait(client,'/api/runs/'+queued['run_id'])
                self.assertEqual(result['status'],'passed',result)
                self.assertTrue(all(step.get('screenshot') for step in result['log']))
            with SessionLocal() as db:
                self.assertEqual(resolve_variables(db,project['id'])['username'],'project-user')
