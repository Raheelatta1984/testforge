"""Opt-in browser UI check against an isolated, publishing-disabled TestForge.

TF_TEST_APP_URL=http://127.0.0.1:8000 python -m unittest tests.test_dashboard_import -v
Do NOT point this mutating test at production or a publishing-enabled instance.
"""
import asyncio
import os
from pathlib import Path
import time
import unittest

from tests.bootstrap import ensure_test_env
ensure_test_env()
from app.browser import launch_kwargs
from playwright.async_api import async_playwright


@unittest.skipUnless(os.environ.get('TF_TEST_APP_URL'), 'Needs isolated TF_TEST_APP_URL')
class DashboardImport(unittest.TestCase):
    def test_BR_UI_01_import_preview_save_execute_report(self):
        async def exercise():
            base=os.environ['TF_TEST_APP_URL'].rstrip('/')
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(**launch_kwargs())
                page=await browser.new_page(viewport={'width':1280,'height':900})
                errors=[]; page.on('pageerror',lambda err:errors.append(str(err)))
                # Do not mutate an instance which could publish to a real branch.
                health=await page.request.get(base+'/api/health')
                self.assertFalse((await health.json())['library_publish_enabled'])
                await page.goto(base)
                project_name='Import validation '+str(int(time.time()))
                await page.locator('#project-name').fill(project_name)
                await page.locator('#project-url').fill(base+'/demo.html')
                await page.locator('button[type=submit]').click()
                await page.get_by_text(project_name,exact=True).click()
                await page.get_by_text('📚',exact=True).click()
                await page.get_by_text('UPLOAD TEST SCRIPTS',exact=True).click()
                data=('action,selector,value\n'
                      f'navigate,,{base}/demo.html\n'
                      'fill,#name,Ada\nclick,#go,\nassert_text,#out,"Hello, Ada"\n').encode()
                await page.get_by_label('Upload test scripts').set_input_files({'name':'greeting.csv','mimeType':'text/csv','buffer':data})
                await page.get_by_role('button',name='VALIDATE & PREVIEW',exact=True).click()
                await page.get_by_label('Imported recording name').fill('Imported greeting')
                self.assertFalse(await page.get_by_role('button',name='SAVE TO LIBRARY & PUBLISH',exact=True).is_enabled())
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/test')) as pending:
                    await page.get_by_role('button',name='TEST BEFORE PUBLISHING',exact=True).click()
                draft=await (await pending.value).json()
                async with asyncio.timeout(30):
                    while True:
                        result=await (await page.request.get(base+'/api/imports/tests/'+draft['id'])).json()
                        if result['status'] not in {'queued','running'}: break
                        await asyncio.sleep(0.1)
                self.assertEqual(result['status'],'passed',result)
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/save')) as pending:
                    await page.get_by_role('button',name='SAVE TO LIBRARY & PUBLISH',exact=True).click()
                saved=await (await pending.value).json()
                self.assertEqual(saved['step_count'],4)
                self.assertFalse(saved['published'])
                async with page.expect_response(lambda r:r.url.endswith('/api/runs') and r.request.method=='POST') as pending:
                    await page.get_by_role('button',name='RUN SAVED STEPS',exact=True).click()
                run=await (await pending.value).json()
                self.assertFalse(run['display_window'])
                async with asyncio.timeout(30):
                    while True:
                        result=await (await page.request.get(base+'/api/runs/'+run['run_id'])).json()
                        if result['status'] in {'passed','failed','error'}:
                            break
                        await asyncio.sleep(0.1)  # test observes completion; no replay delay
                self.assertEqual(result['status'],'passed',result)
                self.assertEqual(result['progress_pct'],100)
                self.assertEqual(result['log'][-1]['action'],'assert_text')
                report=await page.request.get(base+'/api/runs/'+run['run_id']+'/report.html')
                self.assertEqual(report.status,200)
                self.assertIn('assert_text',await report.text())
                # Advanced plan UI: local pack, two dataset rows, test before save.
                await page.get_by_text('📚',exact=True).click()
                await page.get_by_text('UPLOAD TEST SCRIPTS',exact=True).click()
                await page.get_by_label('Synthetic domain pack').select_option('banking')
                await page.get_by_label('Imported recording name').wait_for()
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/test')) as pending:
                    await page.get_by_role('button',name='TEST BEFORE PUBLISHING',exact=True).click()
                draft=await (await pending.value).json()
                async with asyncio.timeout(30):
                    while True:
                        result=await (await page.request.get(base+'/api/imports/tests/'+draft['id'])).json()
                        if result['status'] not in {'queued','running'}: break
                        await asyncio.sleep(.1)
                self.assertEqual(result['status'],'passed',result)
                self.assertEqual(result['result']['total'],2)
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/save')) as pending:
                    await page.get_by_role('button',name='SAVE TO LIBRARY & PUBLISH',exact=True).click()
                plan_saved=await (await pending.value).json()
                self.assertEqual(len(plan_saved['execution_plan']['dataset']),2)
                self.assertIn('submit',plan_saved['execution_plan']['flows'])
                await page.screenshot(path=str(Path(os.environ['TF_TEST_ROOT'])/'dashboard-import.png'))
                self.assertEqual(errors,[])
                await browser.close()
        asyncio.run(exercise())


    def test_BR_UI_02_scenario_inputs_secrets_and_pdf(self):
        async def exercise():
            base=os.environ['TF_TEST_APP_URL'].rstrip('/')
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(**launch_kwargs())
                page=await browser.new_page(viewport={'width':1280,'height':900})
                errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
                health=await (await page.request.get(base+'/api/health')).json()
                self.assertFalse(health['library_publish_enabled'])
                await page.goto(base)
                name='Scenario UI '+str(time.time_ns())
                await page.locator('#project-name').fill(name)
                async with page.expect_response(lambda r:r.url.endswith('/api/projects') and r.request.method=='POST') as pending:
                    await page.get_by_role('button',name='CREATE PROJECT',exact=True).click()
                project=await (await pending.value).json();pid=project['id']
                await page.get_by_text(name,exact=True).click()
                await page.get_by_text('🔤',exact=True).click()
                await page.get_by_label('Variable name',exact=True).fill('password')
                self.assertEqual(await page.get_by_label('Variable value',exact=True).get_attribute('type'),'password')
                await page.get_by_label('Variable value',exact=True).fill('synthetic-ui-secret')
                async with page.expect_response(lambda r:r.url.endswith('/api/variables') and r.request.method=='POST') as pending:
                    await page.get_by_role('button',name='SAVE INPUT',exact=True).click()
                result=await (await pending.value).json();self.assertTrue(result['is_secret']);self.assertEqual(result['value'],'')
                await page.get_by_label('Variable name',exact=True).fill('username')
                await page.get_by_label('Variable value',exact=True).fill('persistent-user')
                await page.get_by_label('Input category').select_option('Parameter')
                async with page.expect_response(lambda r:r.url.endswith('/api/variables') and r.request.method=='POST'):
                    await page.get_by_role('button',name='SAVE INPUT',exact=True).click()
                ids=[]
                steps=[[
                    {'action':'navigate','value':base+'/demo.html'},
                    {'action':'fill','value':'{{username}}','selector':{'primary':'#name'}},
                    {'action':'click','selector':{'primary':'#go'}}],
                    [{'action':'assert_text','value':'Hello, one-off-user','selector':{'primary':'#out'}}]]
                for index,actions in enumerate(steps):
                    response=await page.request.post(base+'/api/recordings',data={'project_id':pid,'name':['Login input fixture','Logout assertion fixture'][index],'start_url':base+'/demo.html'})
                    self.assertEqual(response.status,201);rec=await response.json();ids.append(rec['id'])
                    for action in actions:
                        response=await page.request.post(base+f"/api/recordings/{rec['id']}/steps",data=action)
                        self.assertEqual(response.status,201,await response.text())
                await page.get_by_text('📚',exact=True).click()
                await page.get_by_role('button',name='NEW HIGH-LEVEL SCENARIO',exact=True).click()
                await page.get_by_label('Scenario name').fill('Validate login and logout')
                await page.get_by_label('Plan 1 name').fill('Ordered flow')
                await page.get_by_role('button',name='LINK RECORDING',exact=True).click()
                await page.get_by_label('Recording 1.1',exact=True).select_option(ids[0])
                await page.get_by_role('button',name='LINK RECORDING',exact=True).click()
                await page.get_by_label('Recording 1.2',exact=True).select_option(ids[1])
                await page.get_by_label('username',exact=True).first.fill('plan-user')
                async with page.expect_response(lambda r:r.url.endswith(f'/api/projects/{pid}/scenarios') and r.request.method=='POST') as pending:
                    await page.get_by_role('button',name='SAVE SCENARIO',exact=True).click()
                saved=await (await pending.value).json();self.assertEqual(saved['plans'][0]['entries'][0]['variables']['username'],'plan-user')
                await page.get_by_role('button',name='REVIEW INPUTS & RUN',exact=True).click()
                dialog=page.get_by_role('dialog',name='Plan execution inputs')
                await dialog.get_by_label('username',exact=True).first.fill('one-off-user')
                async with page.expect_response(lambda r:'/plans/' in r.url and r.url.endswith('/run')) as pending:
                    await page.get_by_role('button',name='EXECUTE ORDERED PLAN',exact=True).click()
                batch=await (await pending.value).json()
                async with asyncio.timeout(30):
                    while True:
                        result=await (await page.request.get(base+'/api/runs/batch/'+batch['batch_id'])).json()
                        if result['status'] not in {'queued','running'}:break
                        await asyncio.sleep(.1)
                self.assertEqual(result['status'],'passed',result)
                await page.get_by_role('link',name='VIEW PDF',exact=True).wait_for()
                report=await page.request.get(base+await page.get_by_role('link',name='VIEW PDF',exact=True).get_attribute('href'))
                self.assertEqual(report.status,200);self.assertTrue((await report.body()).startswith(b'%PDF'))
                values=await (await page.request.get(base+'/api/variables?project_id='+pid)).json()
                self.assertEqual(next(v['value'] for v in values if v['name']=='username'),'persistent-user')
                self.assertEqual(next(v['value'] for v in values if v['name']=='password'),'')
                self.assertEqual(errors,[])
                await browser.close()
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
