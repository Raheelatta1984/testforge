"""Sequential TestForge replay. pip install playwright; playwright install chromium.
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
