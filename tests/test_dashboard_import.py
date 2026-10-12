"""Opt-in browser UI check against an isolated, publishing-disabled TestForge.

TF_TEST_APP_URL=http://127.0.0.1:8000 python -m unittest tests.test_dashboard_import -v
Do NOT point this mutating test at production or a publishing-enabled instance.
"""
import asyncio
import json
import os
from pathlib import Path
import time
import unittest

from tests.bootstrap import ensure_test_env
ensure_test_env()
from app.browser import launch_kwargs
from playwright.async_api import async_playwright


def recorded_label_workbook(base):
    """The workbook shape that was reported as "Unsupported action" (an export of a
    recording, with the recorder's labels in the Action column). The point is that
    onboarding it reports no error and can become a runnable recording."""
    import io
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.append(["Test Case", "Step", "Action", "Input", "Selector", "Screenshot"])
    rows = [
        ["Recorded login", 1, "Open " + base + "/demo.html", "", "", ""],
        ["Recorded login", 2, "Click 80,140", "", '{"x": 80, "y": 140}', ""],
        ["Recorded login", 3, "Type: Ada", "", "", ""],
        ["Recorded login", 4, "Click #go", "", '{"primary": "#go"}', ""],
        ["Recorded login", 5, "Verify the page contains Hello, Ada", "", "", ""],
    ]
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


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
                # The dashboard ties every project to a company/entity, so step 1
                # creates one before step 2 can submit. Both submit buttons exist
                # on the page, so each is addressed by its own label.
                entity_name='Import entity '+str(int(time.time()))
                await page.get_by_label('Company or entity name').fill(entity_name)
                await page.get_by_role('button',name='CREATE COMPANY / ENTITY',exact=True).click()
                project_name='Import validation '+str(int(time.time()))
                await page.locator('#project-name').fill(project_name)
                await page.locator('#project-url').fill(base+'/demo.html')
                # Waits for the new entity to appear in the select itself.
                await page.locator('#project-entity').select_option(label=entity_name)
                await page.get_by_role('button',name='CREATE PROJECT',exact=True).click()
                await page.get_by_text(project_name,exact=True).first.click()
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


    def test_BR_UI_03_recorded_label_export_onboards_and_runs(self):
        """The reported upload: a recorded export whose Action column holds labels."""
        async def exercise():
            base=os.environ['TF_TEST_APP_URL'].rstrip('/')
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(**launch_kwargs())
                page=await browser.new_page(viewport={'width':1280,'height':900})
                errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
                self.assertFalse((await (await page.request.get(base+'/api/health')).json())['library_publish_enabled'])
                await page.goto(base)
                if await page.locator('#project-entity option').count() <= 1:
                    await page.get_by_label('Company or entity name').fill('Recorded export entity')
                    await page.get_by_role('button',name='CREATE COMPANY / ENTITY',exact=True).click()
                project_name='Recorded export '+str(time.time_ns())
                await page.locator('#project-name').fill(project_name)
                await page.locator('#project-entity').select_option(index=1)
                await page.get_by_role('button',name='CREATE PROJECT',exact=True).click()
                await page.get_by_text(project_name,exact=True).first.click()
                await page.get_by_text('📚',exact=True).click()
                await page.get_by_text('UPLOAD TEST SCRIPTS',exact=True).click()
                data=recorded_label_workbook(base)
                await page.get_by_label('Upload test scripts').set_input_files(
                    {'name':'azure-devops.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':data})
                async with page.expect_response(lambda r:'/api/imports/preview?' in r.url) as pending:
                    await page.get_by_role('button',name='VALIDATE & PREVIEW',exact=True).click()
                preview=await (await pending.value).json()
                self.assertEqual([d for d in preview['diagnostics'] if d['severity']=='error'],[])
                self.assertTrue(preview['can_save'],preview['diagnostics'])
                self.assertEqual(len(preview['steps']),5)
                # The dashboard must say it is ready, not ask for corrections.
                await page.get_by_text('ready to save',exact=False).wait_for()
                self.assertEqual(await page.get_by_text('corrections required',exact=False).count(),0)
                await page.get_by_label('Imported recording name').fill('Recorded export login')
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/test')) as pending:
                    await page.get_by_role('button',name='TEST BEFORE PUBLISHING',exact=True).click()
                draft=await (await pending.value).json()
                async with asyncio.timeout(60):
                    while True:
                        result=await (await page.request.get(base+'/api/imports/tests/'+draft['id'])).json()
                        if result['status'] not in {'queued','running'}: break
                        await asyncio.sleep(0.1)
                self.assertEqual(result['status'],'passed',result)
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/save')) as pending:
                    await page.get_by_role('button',name='SAVE TO LIBRARY & PUBLISH',exact=True).click()
                saved=await (await pending.value).json()
                self.assertEqual(saved['step_count'],5)
                self.assertFalse(saved['published'])
                async with page.expect_response(lambda r:r.url.endswith('/api/runs') and r.request.method=='POST') as pending:
                    await page.get_by_role('button',name='RUN SAVED STEPS',exact=True).click()
                run=await (await pending.value).json()
                async with asyncio.timeout(60):
                    while True:
                        result=await (await page.request.get(base+'/api/runs/'+run['run_id'])).json()
                        if result['status'] in {'passed','failed','error'}: break
                        await asyncio.sleep(0.1)
                self.assertEqual(result['status'],'passed',result)
                # The template picker previews a pack file through the same parser,
                # so a team can see the steps before replacing them.
                await page.get_by_text('📚',exact=True).click()
                await page.get_by_text('UPLOAD TEST SCRIPTS',exact=True).click()
                await page.get_by_label('Onboarding template').select_option('recorded-labels')
                async with page.expect_response(lambda r:'/api/imports/preview?' in r.url) as pending:
                    await page.get_by_role('button',name='PREVIEW THIS TEMPLATE',exact=True).click()
                template=await (await pending.value).json()
                self.assertEqual([d for d in template['diagnostics'] if d['severity']=='error'],[])
                self.assertTrue(template['can_save'])
                self.assertGreaterEqual(len(template['steps']),6)
                await page.get_by_text('ready to save',exact=False).wait_for()
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

    def test_BR_UI_04_passing_receipt_survives_and_unverified_override_is_explicit(self):
        """PASS must unlock SAVE (and keep it unlocked), FAIL must not -- unless asked.

        Three reported problems, one flow: a passing draft test that left SAVE
        greyed out with no explanation, no way to bank a script when the test
        could not pass, and a Library tab that only listed the selected project so
        recordings made elsewhere looked unsaved.
        """
        async def exercise():
            base=os.environ['TF_TEST_APP_URL'].rstrip('/')
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(**launch_kwargs())
                page=await browser.new_page(viewport={'width':1280,'height':900})
                errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
                self.assertFalse((await (await page.request.get(base+'/api/health')).json())['library_publish_enabled'])
                await page.goto(base)

                async def new_project(label):
                    if await page.locator('#project-entity option').count() <= 1:
                        await page.get_by_label('Company or entity name').fill('Receipt entity '+str(time.time_ns()))
                        await page.get_by_role('button',name='CREATE COMPANY / ENTITY',exact=True).click()
                    name=label+' '+str(time.time_ns())
                    await page.locator('#project-name').fill(name)
                    await page.locator('#project-entity').select_option(index=1)
                    await page.get_by_role('button',name='CREATE PROJECT',exact=True).click()
                    await page.get_by_text(name,exact=True).first.click()
                    return name

                async def upload(steps_csv, recording_name):
                    await page.get_by_text('📚',exact=True).click()
                    await page.get_by_text('UPLOAD TEST SCRIPTS',exact=True).click()
                    await page.get_by_label('Upload test scripts').set_input_files(
                        {'name':'flow.csv','mimeType':'text/csv','buffer':steps_csv.encode()})
                    async with page.expect_response(lambda r:'/api/imports/preview?' in r.url) as pending:
                        await page.get_by_role('button',name='VALIDATE & PREVIEW',exact=True).click()
                    preview=await (await pending.value).json()
                    self.assertTrue(preview['can_save'],preview['diagnostics'])
                    await page.get_by_label('Imported recording name').fill(recording_name)
                    return preview

                async def test_and_wait():
                    async with page.expect_response(lambda r:r.url.endswith('/api/imports/test')) as pending:
                        await page.get_by_role('button',name='TEST BEFORE PUBLISHING',exact=True).click()
                    draft=await (await pending.value).json()
                    async with asyncio.timeout(60):
                        while True:
                            result=await (await page.request.get(base+'/api/imports/tests/'+draft['id'])).json()
                            if result['status'] not in {'queued','running'}: break
                            await asyncio.sleep(0.1)
                    await page.get_by_text('Draft test: '+result['status'],exact=False).wait_for()
                    return result

                project=await new_project('Receipt gate')
                save_button=page.get_by_role('button',name='SAVE TO LIBRARY & PUBLISH',exact=True)

                # --- a FAILING test keeps SAVE locked, and now says why ---------
                await upload('action,selector,value,label\n'
                             f'navigate,,{base}/demo.html,Open\n'
                             'fill,#name,Ada,Name\nclick,#go,,Go\n'
                             'assert_text,#out,"Goodbye, Ada",Wrong expectation\n',
                             'Failing greeting')
                self.assertTrue(await save_button.is_disabled())
                await page.get_by_text('nothing has been tested yet',exact=False).wait_for()
                failed=await test_and_wait()
                self.assertEqual(failed['status'],'failed',failed)
                self.assertTrue(await save_button.is_disabled())
                # The old single sentence ("a passing test is required") is what
                # made a working gate look broken; the cause is named instead.
                await page.get_by_text("finished as 'failed'",exact=False).wait_for()
                await page.get_by_text('SAVE WITHOUT A PASSING TEST',exact=False).click()
                unverified_save=page.get_by_role('button',name='SAVE AS UNVERIFIED',exact=True)
                self.assertTrue(await unverified_save.is_disabled())
                await page.get_by_label('Reason for saving unverified').fill('Target environment is down')
                self.assertTrue(await unverified_save.is_disabled())   # the tick is still required
                await page.get_by_label('Acknowledge this recording is unverified').check()
                self.assertFalse(await unverified_save.is_disabled())
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/save')) as pending:
                    await unverified_save.click()
                response=await pending.value
                self.assertEqual(response.status,201,await response.text())
                override=await response.json()
                self.assertTrue(override['unverified'])
                self.assertFalse(override['tested'])
                self.assertIn('unverified',override['tags'])
                self.assertEqual(override['override_reason'],'Target environment is down')
                # ...and the library says so on the card, not only in the payload.
                await page.get_by_text('UNVERIFIED',exact=True).first.wait_for()

                # --- a PASSING test unlocks SAVE, and survives leaving the tab --
                await upload('action,selector,value,label\n'
                             f'navigate,,{base}/demo.html,Open\n'
                             'fill,#name,Ada,Name\nclick,#go,,Go\n'
                             'assert_text,#out,"Hello, Ada",Greeting\n',
                             'Passing greeting')
                passed=await test_and_wait()
                self.assertEqual(passed['status'],'passed',passed)
                self.assertFalse(await save_button.is_disabled())
                await page.get_by_text('receipt valid for',exact=False).wait_for()
                # Opening another tab unmounts this panel. The receipt is a server
                # object with an hour to live, so coming back must not lose it.
                await page.get_by_text('📁',exact=True).click()
                await page.get_by_text('📚',exact=True).click()
                await page.get_by_text('UPLOAD TEST SCRIPTS',exact=True).click()
                await page.get_by_text('Restored your last import',exact=False).wait_for()
                self.assertFalse(await save_button.is_disabled())
                async with page.expect_response(lambda r:r.url.endswith('/api/imports/save')) as pending:
                    await save_button.click()
                response=await pending.value
                self.assertEqual(response.status,201,await response.text())
                saved=await response.json()
                self.assertTrue(saved['tested'])
                self.assertFalse(saved.get('unverified'))
                self.assertEqual(saved['step_count'],4)

                # --- the Library lists every project, not just the selected one --
                other = await new_project('Other project')
                projects=await (await page.request.get(base+'/api/projects?fast=true')).json()
                other_id=next(row['id'] for row in projects if row['name']==other)
                other_rec=await page.request.post(base+'/api/imports/save',data=json.dumps({
                    'project_id':other_id,
                    'name':'Elsewhere recording','tags':'imported','allow_untested':True,
                    'override_reason':'listing check',
                    'steps':[{'action':'navigate','value':base+'/demo.html'},
                             {'action':'assert_text','selector':{'primary':'#out'},'value':'Hello, Ada'}]}))
                self.assertEqual(other_rec.status,201,await other_rec.text())
                await page.get_by_text(project,exact=True).first.click()
                await page.get_by_text('📚',exact=True).click()
                await page.get_by_text('Passing greeting',exact=True).wait_for()
                # Both projects are on screen while one is selected.
                self.assertGreaterEqual(await page.get_by_text('Elsewhere recording',exact=True).count(),1)
                self.assertGreaterEqual(await page.get_by_text('across ',exact=False).count(),1)
                await page.get_by_label('Filter by project').select_option(index=0)   # All projects
                self.assertGreaterEqual(await page.get_by_text('Failing greeting',exact=True).count(),1)
                await page.get_by_label('Search the library').fill('Passing greeting')
                self.assertEqual(await page.get_by_text('Elsewhere recording',exact=True).count(),0)
                await page.screenshot(path=str(Path(os.environ['TF_TEST_ROOT'])/'dashboard-receipt-gate.png'))
                self.assertEqual(errors,[])
                await browser.close()
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
