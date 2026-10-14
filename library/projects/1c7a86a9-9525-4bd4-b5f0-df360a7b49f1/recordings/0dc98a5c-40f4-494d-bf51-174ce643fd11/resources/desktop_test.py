"""Sequential desktop replay: simulator by default, Windows agent when
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
