"""Reusable locator facade; steps.json is the single source of truth.
Example: await RecordedPage(page).locator(2, variables).fill("Ada").
For plans this covers main test steps; setup/teardown remain in execution-plan.json.
"""
import json
from pathlib import Path
from testforge_runtime import locator, resolve
class RecordedPage:
    def __init__(self, page):
        self.page = page
        self.steps = json.loads(Path(__file__).with_name("steps.json").read_text())
    def locator(self, order, variables=None):
        step = next(s for s in self.steps if s["order"] == order)
        return locator(self.page, resolve(step["selector"], variables or {}))
