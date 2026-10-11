"""Real Chromium regression checks for extended actions (sequential, local only).

Run after installing Chromium:
  TF_TEST_BROWSER=1 python -m unittest tests.test_browser_features -v
The existing TF_CHROMIUM_PATH/TF_CHROMIUM_LIBS overrides are supported.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest

from tests.bootstrap import ensure_test_env
ensure_test_env()
from app.browser import launch_kwargs
from app.step_runtime import StepRuntime, normalize_step
from playwright.async_api import async_playwright

HTML = b'''<!doctype html><html><head><title>Fixture</title></head><body>
<label>Name <input id="name"></label><button id="go" onclick="document.querySelector('#out').textContent='Hello, '+document.querySelector('#name').value">Continue</button>
<div id="out"></div><input type="checkbox" id="check"><input type="radio" id="radio"><button id="disabled" disabled>Disabled</button>
<select id="choice"><option value="a">Alpha</option><option value="b">Beta</option></select>
<input type="file" id="file"><table><tr><td id="cell">42</td></tr></table>
<div id="scroll" style="height:30px;overflow:auto"><div style="height:200px">Scroll</div></div>
<button id="double" ondblclick="this.textContent='Doubled'">Double</button>
<button id="right" oncontextmenu="event.preventDefault();this.textContent='Right'">Right</button>
<button id="dialog" onclick="document.querySelector('#out').textContent=prompt('Your name?') || 'dismissed'">Prompt</button>
<a id="popup" href="/child" target="_blank">Popup</a>
<a id="download" download="fixture.txt" href="data:text/plain,Hello">Download</a>
<iframe id="frame" srcdoc="<input id='inside'><span id='frame-text'>Frame ready</span>"></iframe>
</body></html>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'id':42,'state':'ready'}).encode() if self.path in {'/api','/mock'} else HTML
        self.send_response(200)
        self.send_header('Content-Type','application/json' if self.path in {'/api','/mock'} else 'text/html')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get('TF_TEST_BROWSER') == '1', 'Set TF_TEST_BROWSER=1 with Chromium installed')
class BrowserFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0), Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=5)

    def run_browser(self, scenario):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(**launch_kwargs())
                    context=await browser.new_context(accept_downloads=True)
                    page=await context.new_page(); await page.goto(self.base)
                    runtime=StepRuntime(page,directory); variables={}
                    async def step(action, value='', primary=None, **options):
                        if primary: options['primary']=primary
                        return await runtime.perform(normalize_step({'action':action,'value':value,'selector':options}),variables)
                    try:
                        await scenario(page,runtime,variables,step,Path(directory))
                    finally:
                        await runtime.close(); await context.close(); await browser.close()
        asyncio.run(exercise())

    def test_BR_NEW_01_controls_assertions_and_failure(self):
        async def scenario(page,runtime,variables,step,directory):
            await step('fill','Ada','#name')
            await step('assert_value','Ada','#name')
            await step('click',primary='button',kind='role',name='Continue')
            await step('assert_text','Hello, Ada','#out')
            await step('assert_contains','Ada','#out')
            await step('assert_title','Fixture')
            await step('assert_url',self.base+'/')
            await step('assert_count','2','input[type=checkbox],input[type=radio]')
            await step('check',primary='#check'); await step('check',primary='#check')
            await step('assert_checked',primary='#check')
            await step('uncheck',primary='#check');await step('assert_unchecked',primary='#check')
            await step('check',primary='#radio');await step('assert_checked',primary='#radio')
            await step('select_option','Beta','#choice',by='label');await step('assert_value','b','#choice')
            await step('hover',primary='#go')
            await step('dblclick',primary='#double');await step('assert_text','Doubled','#double')
            await step('right_click',primary='#right');await step('assert_text','Right','#right')
            await step('assert_disabled',primary='#disabled');await step('assert_enabled',primary='#go')
            await step('wait_visible',primary='#go');await step('wait_hidden',primary='#absent')
            await step('scroll','40','#scroll')
            self.assertGreater(await page.locator('#scroll').evaluate('(el)=>el.scrollTop'),0)
            await step('capture_text','answer','#cell');self.assertEqual(variables['answer'],'42')
            with self.assertRaises(AssertionError):
                await step('assert_text','WRONG','#out',timeout_ms=60)
            with self.assertRaises(ValueError):
                await step('fill','{{missing}}','#name')
            self.assertEqual(await page.input_value('#name'),'Ada')
        self.run_browser(scenario)

    def test_BR_NEW_02_frames_popups_dialog_files(self):
        async def scenario(page,runtime,variables,step,directory):
            await step('fill','Inside','#inside',frames=['#frame'])
            await step('assert_value','Inside','#inside',frames=['#frame'])
            await step('open_popup',primary='#popup',alias='child')
            await step('assert_url',self.base+'/child')
            await step('switch_page','main');await step('close_page','child')
            await step('new_page',self.base+'/child',alias='new')
            await step('assert_title','Fixture');await step('close_page','new')
            await step('dialog','Ada','#dialog',message='Your name?',dialog_type='prompt')
            await step('assert_text','Ada','#out')
            await step('dialog',primary='#dialog',decision='dismiss')
            await step('assert_text','dismissed','#out')
            await step('upload_file',primary='#file',filename='fixture.txt',base64=base64.b64encode(b'hello').decode(),mime_type='text/plain')
            self.assertEqual(await page.locator('#file').evaluate('(el)=>el.files[0].name'),'fixture.txt')
            result=await step('download','fixture.txt','#download')
            self.assertEqual((directory/result['download']).read_bytes(),b'Hello')
        self.run_browser(scenario)

    def test_BR_NEW_03_api_capture_and_scoped_network_mock(self):
        async def scenario(page,runtime,variables,step,directory):
            result=await step('api_request',self.base+'/api',status=200,json_path='id',expected=42,capture='id')
            self.assertEqual(result['http_status'],200)
            await step('fill','{{id}}','#name');await step('assert_value','42','#name')
            with self.assertRaises(AssertionError):
                await step('api_request',self.base+'/api',status=403)
            await step('mock_route','**/mock',body='{"mocked":true}')
            self.assertEqual(await page.evaluate("fetch('/mock').then(r=>r.json())"),{'mocked':True})
            await step('clear_routes')
            self.assertEqual((await page.evaluate("fetch('/mock').then(r=>r.json())"))['id'],42)
        self.run_browser(scenario)

    def test_BR_NEW_04_single_and_batch_assertion_failures(self):
        from app import library_store
        from app.db import SessionLocal,Run,Batch,init_db
        init_db()
        from app.executor import execute_run
        from app.batch_runner import execute_batch
        project=library_store.create_project('Browser assertions',self.base)
        rec=library_store.create_recording(project['id'],'Assert failure',self.base,steps=[
            {'action':'navigate','value':self.base},
            {'action':'assert_text','selector':{'primary':'#out','timeout_ms':60},'value':'intentional wrong value'}])
        with SessionLocal() as db:
            single=Run(recording_id=rec['id']);batch=Batch(name='Assertions',status='queued',total=1)
            db.add_all([single,batch]);db.commit()
            child=Run(recording_id=rec['id'],batch_id=batch.id);db.add(child);db.commit()
            single_id,batch_id,child_id=single.id,batch.id,child.id
        async def exercise():
            async def event(_): pass
            await execute_run(single_id,event,display_window=False,screenshots='failure')
            await execute_batch(batch_id,event,display_window=False,screenshots='failure',retry_transient=True)
        asyncio.run(exercise())
        with SessionLocal() as db:
            for rid in (single_id,child_id):
                row=db.get(Run,rid)
                self.assertEqual(row.status,'failed')
                self.assertNotEqual(row.progress_pct,100)
                self.assertEqual(row.execution_log[-1]['action'],'assert_text')
                self.assertFalse(row.execution_log[-1].get('retried'))
                self.assertTrue(row.execution_log[-1].get('screenshot'))
                self.assertFalse(row.execution_log[0].get('screenshot'))

    @unittest.skipUnless(os.environ.get('TF_TEST_NODE_MODULES'), 'Needs Node Playwright in TF_TEST_NODE_MODULES')
    def test_BR_NEW_05_exported_python_javascript_typescript(self):
        import subprocess
        import sys
        from app.export_formats import write_exports
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            steps=[normalize_step(s,i+1) for i,s in enumerate([
                {'action':'navigate','value':self.base},
                {'action':'fill','selector':'#name','value':'Ada'},
                {'action':'click','selector':'#go'},
                {'action':'assert_text','selector':'#out','value':'Hello, Ada'},
                {'action':'check','selector':'#check'},
                {'action':'assert_checked','selector':'#check'},
                {'action':'fill','selector':{'primary':'#inside','frames':['#frame']},'value':'Inside'},
                {'action':'assert_value','selector':{'primary':'#inside','frames':['#frame']},'value':'Inside'},
                {'action':'api_request','value':self.base+'/api','selector':{'json_path':'id','expected':42,'capture':'id'}},
                {'action':'fill','selector':'#name','value':'{{id}}'},
                {'action':'assert_value','selector':'#name','value':'42'},
            ])]
            write_exports(folder,{'name':'Export parity','project_id':'project','id':'recording','steps':steps})
            env={**os.environ,'NODE_PATH':os.environ['TF_TEST_NODE_MODULES']}
            cli=str(Path(env['NODE_PATH'])/'@playwright/test/cli.js')
            commands=[[sys.executable,str(folder/'playwright_test.py')],['node',str(folder/'playwright_test.js')],['node',cli,'test','--config',str(folder/'playwright.config.ts')]]
            for command in commands:
                result=subprocess.run(command,cwd=folder,env=env,capture_output=True,text=True,timeout=45)
                self.assertEqual(result.returncode,0,result.stdout[-2000:]+result.stderr[-2000:])
            steps[-1]['value']='WRONG'
            steps[-1]['selector']['timeout_ms']=60
            (folder/'steps.json').write_text(json.dumps(steps))
            for command in commands:
                result=subprocess.run(command,cwd=folder,env=env,capture_output=True,text=True,timeout=45)
                self.assertNotEqual(result.returncode,0,'An exported incorrect assertion passed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
