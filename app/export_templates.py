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
from testforge_runtime import StepRuntime

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
        runtime = StepRuntime(page, output, execution_speed=os.environ.get("TF_EXECUTION_SPEED") or None)
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
                    # One code path for recordings and imported steps: the runtime
                    # itself handles locators, recorded coordinates and typing into
                    # the focused element.
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


DESKTOP_TEST_PY = '''"""Sequential desktop replay: simulator by default, Windows agent when
TF_WINDOWS_AGENT_URL and TF_WINDOWS_AGENT_TOKEN are set (TF_DESKTOP_MODE=agent
forces the bridge, =simulator forces the local simulated UIA tree)."""
import asyncio
import json
import os
from pathlib import Path
from testforge_desktop import DesktopRunner
from testforge_runtime import interpolate, resolve

async def main():
    here = Path(__file__).resolve().parent
    variables = {k[7:]: v for k, v in os.environ.items() if k.startswith("TF_VAR_")}
    plan_file = here / "execution-plan.json"
    if plan_file.exists():
        from testforge_plan import normalize_plan, resolve_values
        plan = normalize_plan(json.loads(plan_file.read_text()))
        for profile in plan["matrix"]:
            if profile.get("runner") != "windows_desktop":
                continue
            for row in plan["dataset"]:
                values = {**variables, **resolve_values(plan["variables"]), **resolve_values(row)}
                runner = DesktopRunner(os.environ.get("TF_DESKTOP_MODE", "auto"),
                                       app_id=profile.get("desktop_app"),
                                       app_profile=profile.get("app_profile"),
                                       framework=profile.get("framework"),
                                       artifact_dir=here / "run-artifacts")
                from testforge_mobile import MOBILE_ACTIONS
                for stage in ("setup", "steps", "teardown"):
                    for step in plan[stage]:
                        if step["action"] in MOBILE_ACTIONS:
                            continue  # another runner family owns this step
                        resolved = {"action": step["action"],
                                    "value": step["value"] if step["action"] == "ui_grid_read" else interpolate(step.get("value"), values),
                                    "selector": resolve(step.get("selector") or {}, values)}
                        await runner.perform(resolved, values)
        print("desktop plan passed (sequential)")
        return
    steps = json.loads((here / "desktop-steps.json").read_text())["steps"]
    runner = DesktopRunner(os.environ.get("TF_DESKTOP_MODE", "auto"), artifact_dir=here / "run-artifacts")
    for step in steps:
        await runner.perform({"action": step["action"],
                              "value": step["value"] if step["action"] == "ui_grid_read" else interpolate(step.get("value"), variables),
                              "selector": resolve(step.get("selector") or {}, variables)}, variables)
    print("desktop steps passed (sequential)")

if __name__ == "__main__":
    asyncio.run(main())
'''

MOBILE_TEST_PY = '''"""Sequential mobile replay: simulator by default, Appium/DataWedge hub when
TF_MOBILE_HUB_URL and TF_MOBILE_HUB_TOKEN are set (TF_MOBILE_MODE=hub forces
the bridge, =simulator forces the local device simulator)."""
import asyncio
import json
import os
from pathlib import Path
from testforge_mobile import MobileRunner
from testforge_runtime import interpolate, resolve

async def main():
    here = Path(__file__).resolve().parent
    variables = {k[7:]: v for k, v in os.environ.items() if k.startswith("TF_VAR_")}
    plan_file = here / "execution-plan.json"
    if plan_file.exists():
        from testforge_plan import normalize_plan, resolve_values
        plan = normalize_plan(json.loads(plan_file.read_text()))
        for profile in plan["matrix"]:
            if profile.get("runner") != "mobile_device":
                continue
            for row in plan["dataset"]:
                values = {**variables, **resolve_values(plan["variables"]), **resolve_values(row)}
                runner = MobileRunner(os.environ.get("TF_MOBILE_MODE", "auto"),
                                      platform=profile.get("platform", "android"),
                                      device_model=profile.get("device_model", "simulator"),
                                      os_version=profile.get("os_version", ""),
                                      orientation=profile.get("orientation", "portrait"),
                                      artifact_dir=here / "run-artifacts")
                from testforge_desktop import DESKTOP_ACTIONS
                for stage in ("setup", "steps", "teardown"):
                    for step in plan[stage]:
                        if step["action"] in DESKTOP_ACTIONS:
                            continue  # another runner family owns this step
                        resolved = {"action": step["action"], "value": interpolate(step.get("value"), values),
                                    "selector": resolve(step.get("selector") or {}, values)}
                        await runner.perform(resolved, values)
        print("mobile plan passed (sequential)")
        return
    steps = json.loads((here / "mobile-steps.json").read_text())["steps"]
    runner = MobileRunner(os.environ.get("TF_MOBILE_MODE", "auto"),
                          platform=os.environ.get("TF_MOBILE_PLATFORM", "android"),
                          artifact_dir=here / "run-artifacts")
    for step in steps:
        await runner.perform({"action": step["action"], "value": interpolate(step.get("value"), variables),
                              "selector": resolve(step.get("selector") or {}, variables)}, variables)
    print("mobile steps passed (sequential)")

if __name__ == "__main__":
    asyncio.run(main())
'''

MOBILE_TEST_JS = '''// Sequential mobile replay against the deterministic simulator.
// Live Appium/DataWedge execution uses mobile_test.py with TF_MOBILE_HUB_URL.
const fs = require('node:fs');
const path = require('node:path');
const { MobileSimulator } = require('./testforge_mobile.js');
// Local interpolation so this simulator runner needs no Playwright install.
function resolve(value, vars) {
  if (typeof value === 'string') return value.replace(/\\{\\{\\s*([\\w.-]+)\\s*\\}\\}/g, (_, name) => {
    const v = Object.prototype.hasOwnProperty.call(vars, name) ? vars[name] : process.env['TF_VAR_' + name];
    if (v === undefined || v === null) throw new Error('Missing variable: ' + name);
    return String(v);
  });
  if (Array.isArray(value)) return value.map(item => resolve(item, vars));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolve(v, vars)]));
  return value;
}
(async () => {
  const vars = Object.fromEntries(Object.entries(process.env).filter(([k]) => k.startsWith('TF_VAR_')).map(([k, v]) => [k.slice(7), v]));
  const planFile = path.join(__dirname, 'execution-plan.json');
  const plan = fs.existsSync(planFile) ? JSON.parse(fs.readFileSync(planFile)) : null;
  const DESKTOP_ACTIONS = ['window_attach','ui_click','ui_double_click','ui_right_click','ui_type','ui_select','ui_check','ui_menu_invoke','ui_grid_read','assert_control_text','assert_control_state','wait_window_ready'];
  const runSteps = (sim, steps) => { for (const step of steps) { if (DESKTOP_ACTIONS.includes(step.action)) continue; sim.perform({action: step.action, value: resolve(step.value || '', vars), selector: resolve(step.selector || {}, vars)}, vars); } };
  if (plan) {
    for (const profile of plan.matrix) {
      if (profile.runner !== 'mobile_device') continue;
      for (const row of plan.dataset) runSteps(new MobileSimulator(profile.platform || 'android'), [...plan.setup, ...plan.steps, ...plan.teardown]);
    }
  } else {
    runSteps(new MobileSimulator(process.env.TF_MOBILE_PLATFORM || 'android'), JSON.parse(fs.readFileSync(path.join(__dirname, 'mobile-steps.json'))).steps);
  }
  console.log('mobile steps passed (simulator, sequential)');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''

DESKTOP_RUNNER_CS = '''// TestForge Windows desktop bridge client (.NET 6+).
// Reads desktop-steps.json and posts each step, in order, to the authenticated
// Windows UIA agent named by TF_WINDOWS_AGENT_URL / TF_WINDOWS_AGENT_TOKEN.
// Without an agent it exits with guidance; local simulation is desktop_test.py.
// dotnet run --project .  (place this file alone in a console project folder)
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

var url = Environment.GetEnvironmentVariable("TF_WINDOWS_AGENT_URL");
var token = Environment.GetEnvironmentVariable("TF_WINDOWS_AGENT_TOKEN");
if (string.IsNullOrWhiteSpace(url) || string.IsNullOrWhiteSpace(token))
{
    Console.Error.WriteLine("Set TF_WINDOWS_AGENT_URL and TF_WINDOWS_AGENT_TOKEN, or run desktop_test.py for the local simulator.");
    return 2;
}
var steps = JsonDocument.Parse(await File.ReadAllTextAsync("desktop-steps.json")).RootElement.GetProperty("steps");
using var http = new HttpClient { BaseAddress = new Uri(url) };
http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
foreach (var step in steps.EnumerateArray())
{
    var payload = JsonSerializer.Serialize(new { step });
    using var response = await http.PostAsync("/commands", new StringContent(payload, Encoding.UTF8, "application/json"));
    response.EnsureSuccessStatusCode();
    Console.WriteLine($"step ok: {step.GetProperty("action").GetString()}");
}
Console.WriteLine("desktop steps passed (Windows agent, sequential)");
return 0;
'''


# pytest entry point for the exported recording. It calls the same replay as
# `python playwright_test.py`, so steps, plans and variables behave identically; a failed step
# raises inside main() and fails the test.
PYTEST_WRAPPER = '''"""pytest entry point for this recording.

Install and run: pip install -r requirements.txt && python -m playwright install chromium && pytest -q
Supply TF_VAR_<name> environment variables for recorded variables.
"""
import asyncio

from playwright_test import main


def test_recording_replays():
    asyncio.run(main())
'''


def _flatten_plan_steps(plan):
    try:
        from app.plan_runtime import normalize_plan
        normalized = normalize_plan(plan)
    except Exception:
        return [], []
    steps = [step for stage in ('setup', 'steps', 'teardown') for step in normalized[stage]]
    return steps, normalized['matrix']


def write_desktop_bundle(folder, steps, matrix=None):
    from app.desktop_runner import DESKTOP_ACTIONS
    selected = [step for step in steps if step.get('action') in DESKTOP_ACTIONS]
    has_profile = any((profile or {}).get('runner') == 'windows_desktop' for profile in (matrix or []))
    if not selected and not has_profile:
        return False
    folder = Path(folder)
    (folder / 'desktop-steps.json').write_text(json.dumps({'steps': selected}, indent=2), encoding='utf-8')
    (folder / 'desktop_test.py').write_text(DESKTOP_TEST_PY, encoding='utf-8')
    (folder / 'DesktopRunner.cs').write_text(DESKTOP_RUNNER_CS, encoding='utf-8')
    (folder / 'DESKTOP.md').write_text(
        '# Windows desktop replay\n\nSequential only. `python desktop_test.py` uses the deterministic '
        'simulated UIA tree unless `TF_WINDOWS_AGENT_URL` and `TF_WINDOWS_AGENT_TOKEN` name an '
        'authenticated Windows agent (FlaUI/UIA/WinAppDriver worker). `DesktopRunner.cs` is the '
        'equivalent C# agent client for .NET shops. No credentials are stored in this bundle.\n', encoding='utf-8')
    return True


def write_mobile_bundle(folder, steps, matrix=None):
    from app.mobile_runner import MOBILE_ACTIONS, datawedge_profile
    selected = [step for step in steps if step.get('action') in MOBILE_ACTIONS]
    has_profile = any((profile or {}).get('runner') == 'mobile_device' for profile in (matrix or []))
    if not selected and not has_profile:
        return False
    folder = Path(folder)
    (folder / 'mobile-steps.json').write_text(json.dumps({'steps': selected}, indent=2), encoding='utf-8')
    (folder / 'mobile_test.py').write_text(MOBILE_TEST_PY, encoding='utf-8')
    (folder / 'mobile_test.js').write_text(MOBILE_TEST_JS, encoding='utf-8')
    (folder / 'zebra-datawedge-profile.json').write_text(json.dumps(datawedge_profile(), indent=2), encoding='utf-8')
    (folder / 'MOBILE.md').write_text(
        '# Mobile / Zebra replay\n\nSequential only. `python mobile_test.py` (or `node mobile_test.js`) '
        'uses the deterministic device simulator unless `TF_MOBILE_HUB_URL` and `TF_MOBILE_HUB_TOKEN` '
        'name an authenticated Appium lab. Zebra scans are dispatched as DataWedge broadcast-intent '
        'payloads; import `zebra-datawedge-profile.json` into DataWedge on authorised devices. '
        'No credentials are stored in this bundle.\n', encoding='utf-8')
    return True


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
    # Canonical-runtime companions: speed profile plus desktop/mobile runners.
    (folder / "testforge_speed.py").write_text((base / "execution_speed.py").read_text(), encoding="utf-8")
    (folder / "testforge_desktop.py").write_text((base / "desktop_runner.py").read_text(), encoding="utf-8")
    (folder / "testforge_mobile.py").write_text((base / "mobile_runner.py").read_text(), encoding="utf-8")
    (folder / "testforge_desktop.js").write_text((base / "export_desktop.js").read_text(), encoding="utf-8")
    (folder / "testforge_mobile.js").write_text((base / "export_mobile.js").read_text(), encoding="utf-8")
    if plan:
        plan_steps, matrix = _flatten_plan_steps(plan)
        write_desktop_bundle(folder, plan_steps, matrix)
        write_mobile_bundle(folder, plan_steps, matrix)
    (folder / "requirements.txt").write_text("playwright==1.49.1\npytest==8.3.3\n", encoding="utf-8")
    (folder / "playwright_test.py").write_text(PYTHON, encoding="utf-8")
    (folder / "test_recording.py").write_text(PYTEST_WRAPPER, encoding="utf-8")
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
