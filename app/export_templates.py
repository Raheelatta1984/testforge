"""On-demand portable exports; no services or scheduled jobs."""
from pathlib import Path
import json

PYTHON = '''"""Sequential TestForge replay. pip install playwright; playwright install chromium.
Run from this resource directory. Supply variables as TF_VAR_<name>.
"""
import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright
from testforge_runtime import StepRuntime, interpolate

async def main():
    here = Path(__file__).resolve().parent
    steps = json.loads((here / "steps.json").read_text())
    variables = {k[7:]: v for k, v in os.environ.items() if k.startswith("TF_VAR_")}
    if (here / 'execution-plan.json').exists():
        from testforge_plan import run_plan
        plan = json.loads((here / 'execution-plan.json').read_text())
        opts = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
        if os.environ.get('TF_CHROMIUM_PATH'):
            opts['executable_path'] = os.environ['TF_CHROMIUM_PATH']
        if os.environ.get('TF_CHROMIUM_LIBS'):
            opts['env'] = {**os.environ, 'LD_LIBRARY_PATH': os.environ['TF_CHROMIUM_LIBS']}
        result = await run_plan(plan, variables, here / 'run-artifacts', chromium_options=opts)
        (here / 'plan-result.json').write_text(json.dumps(result, indent=2))
        if result['status'] != 'passed':
            raise RuntimeError('Execution plan failed or was flaky; inspect plan-result.json')
        return
    async with async_playwright() as pw:
        launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if os.environ.get("TF_CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["TF_CHROMIUM_PATH"]
        if os.environ.get("TF_CHROMIUM_LIBS"):
            launch["env"] = {**os.environ, "LD_LIBRARY_PATH": os.environ["TF_CHROMIUM_LIBS"]}
        browser = await pw.chromium.launch(**launch)
        context = await browser.new_context(viewport={"width": 1024, "height": 640})
        page = await context.new_page()
        output = here / "run-artifacts"
        output.mkdir(exist_ok=True)
        runtime = StepRuntime(page, output)
        try:
            total = 0
            for step in steps:
                repeat = step.get("repeat_count", step.get("repeat", 1)) or 1
                if not isinstance(repeat, int) or not 1 <= repeat <= 100:
                    raise ValueError("Invalid repeat count")
                for _ in range(repeat):
                    total += 1
                    if total > 2000:
                        raise ValueError("Expanded action limit exceeded")
                    selector = step.get("selector") or {}
                    if not selector.get("strict") and step["action"] == "click" and not selector.get("primary"):
                        await runtime.page.mouse.click(selector["x"], selector["y"])
                    elif not selector.get("strict") and step["action"] in ("type", "fill") and not selector.get("primary"):
                        await runtime.page.keyboard.type(interpolate(step.get("value"), variables), delay=0)
                    else:
                        await runtime.perform(step, variables)
        finally:
            await runtime.close()
            await context.close()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
'''

JAVASCRIPT = '''// npm install; node playwright_test.js. Supply TF_VAR_<name> variables.
const pw = require('@playwright/test');
const { chromium } = pw;
const { replay, launchOptions } = require('./testforge_runtime.js');
const path = require('node:path');
(async () => {
  if(require('node:fs').existsSync(path.join(__dirname,'execution-plan.json'))){
    const {replayPlan}=require('./testforge_plan.js');
    await replayPlan(pw,require('./execution-plan.json'),path.join(__dirname,'run-artifacts'));
    return;
  }
  const browser = await chromium.launch(launchOptions());
  try {
    const context = await browser.newContext({viewport:{width:1024,height:640}});
    const page = await context.newPage();
    await replay(page, require('./steps.json'), path.join(__dirname,'run-artifacts'));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
'''


def write_runners(folder, title, plan=None):
    base = Path(__file__).parent
    if plan:
        from app.plan_runtime import normalize_plan
        (folder / 'execution-plan-source.json').write_text(json.dumps({'name': title, 'execution_plan': plan}, indent=2), encoding='utf-8')
        (folder / 'execution-plan.json').write_text(json.dumps(normalize_plan(plan), indent=2), encoding='utf-8')
        (folder / 'testforge_plan.py').write_text((base / 'plan_runtime.py').read_text(), encoding='utf-8')
        (folder / 'testforge_plan.js').write_text((base / 'export_plan.js').read_text(), encoding='utf-8')
    else:
        for name in ('execution-plan.json', 'execution-plan-source.json', 'testforge_plan.py', 'testforge_plan.js'):
            (folder / name).unlink(missing_ok=True)
    license_path = base.parent / "LICENSE"
    if license_path.is_file():
        (folder / "LICENSE").write_text(license_path.read_text(), encoding="utf-8")
    (folder / "testforge_runtime.py").write_text((base / "step_runtime.py").read_text(), encoding="utf-8")
    (folder / "testforge_runtime.js").write_text((base / "export_runtime.js").read_text(), encoding="utf-8")
    (folder / "requirements.txt").write_text("playwright==1.49.1\n", encoding="utf-8")
    (folder / "playwright_test.py").write_text(PYTHON, encoding="utf-8")
    (folder / "playwright_test.js").write_text(JAVASCRIPT, encoding="utf-8")
    (folder / 'recorded_page.py').write_text(
        '"""Reusable locator facade; steps.json is the single source of truth.\n'
        'Example: await RecordedPage(page).locator(2, variables).fill("Ada").\n'
        'For plans this covers main test steps; setup/teardown remain in execution-plan.json.\n"""\n'
        'import json\nfrom pathlib import Path\nfrom testforge_runtime import locator, resolve\n'
        'class RecordedPage:\n'
        '    def __init__(self, page):\n        self.page = page\n'
        '        self.steps = json.loads(Path(__file__).with_name("steps.json").read_text())\n'
        '    def locator(self, order, variables=None):\n'
        '        step = next(s for s in self.steps if s["order"] == order)\n'
        '        return locator(self.page, resolve(step["selector"], variables or {}))\n', encoding='utf-8')
    (folder / 'recorded_page.js').write_text(
        "const { locator, resolve } = require('./testforge_runtime.js');\n"
        "class RecordedPage { constructor(page) { this.page=page; this.steps=require('./steps.json'); }\n"
        " locator(order,variables={}) { const step=this.steps.find(s=>s.order===order); if(!step) throw new Error('Unknown step'); return locator(this.page,resolve(step.selector,variables)); } }\n"
        "module.exports={RecordedPage};\n", encoding='utf-8')
    (folder / "package.json").write_text(json.dumps({"private": True, "scripts": {"test": "playwright test"},
        "devDependencies": {"@playwright/test": "1.49.1"}}, indent=2) + "\n", encoding="utf-8")
    (folder / "playwright.config.ts").write_text(
        "import { defineConfig } from '@playwright/test';\n"
        "const { launchOptions } = require('./testforge_runtime.js');\n"
        "export default defineConfig({ testMatch: 'recording.spec.ts', workers: 1, fullyParallel: false, "
        "retries: 0, reporter: [['list'], ['html', {open: 'never'}]], "
        "use: {browserName: 'chromium', launchOptions: launchOptions(), viewport: {width:1024,height:640}, "
        "screenshot: 'only-on-failure', video: 'off', trace: 'off'} });\n", encoding="utf-8")
    if plan:
        spec = ("import { test } from '@playwright/test';\n"
                "const { replayPlan } = require('./testforge_plan.js');\n"
                "test(" + json.dumps(title) + ", async ({ playwright }, testInfo) => {\n"
                "  await replayPlan(playwright, require('./execution-plan.json'), testInfo.outputPath('files'));\n"
                "});\n")
    else:
        spec = ("import { test } from '@playwright/test';\n"
                "const { replay } = require('./testforge_runtime.js');\n"
                "test(" + json.dumps(title) + ", async ({ page }, testInfo) => {\n"
                "  await replay(page, require('./steps.json'), testInfo.outputPath('files'));\n"
                "});\n")
    (folder / "recording.spec.ts").write_text(spec, encoding='utf-8')
