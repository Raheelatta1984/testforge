"""Generated TestForge Playwright replay. Install playwright and chromium first."""
import os
import re
from playwright.sync_api import sync_playwright

STEPS = [{'id': 'e1194874-fedd-447c-ad74-dea50efb8998', 'order': 1, 'action': 'navigate', 'value': 'https://hfm-wholesale-app-dev.ashysand-8cabb4df.australiaeast.azurecontainerapps.io/daily-orders?storeNo=426&date=2026-10-08', 'label': 'Open https://hfm-wholesale-app-dev.ashysand-8cabb4df.australiaeast.azurecontainerapps.io/daily-orders?storeNo=426&date=2026-10-08', 'selector': None}, {'id': '35cd514c-664e-4dfe-a7f6-cd0d00dd148e', 'order': 2, 'action': 'click', 'value': None, 'label': 'Click 369,311', 'selector': {'primary': 'div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > form:nth-of-type(1) > label:nth-of-type(1) > input:nth-of-type(1)', 'x': 369, 'y': 311, 'text': '', 'tag': 'input'}, 'screenshot': 'step-002.png'}, {'id': 'd73fd711-28d2-4561-b1e7-2e8aeee1377a', 'order': 3, 'action': 'type', 'value': 'Sa', 'label': 'Type: Sa', 'selector': {'primary': 'div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > form:nth-of-type(1) > label:nth-of-type(1) > input:nth-of-type(1)'}, 'screenshot': 'step-003.png'}, {'id': '3369afad-e77d-4501-9015-ce101e2a17da', 'order': 4, 'action': 'click', 'value': None, 'label': 'Click 288,395', 'selector': {'primary': 'div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > form:nth-of-type(1) > label:nth-of-type(2) > input:nth-of-type(1)', 'x': 288, 'y': 395, 'text': '', 'tag': 'input'}}]

def value(text):
    def replace(match):
        name = match.group(1)
        env = "TF_VAR_" + name
        if env not in os.environ:
            raise KeyError("Missing variable: " + env)
        return os.environ[env]
    return re.sub(r"\{\{\s*([\w.\-]+)\s*\}\}", replace, text or "")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1024, "height": 640})
    variables = {}
    try:
        for step in STEPS:
            action = step["action"]
            target = step.get("selector") or {}
            selector = target.get("primary")
            text = value(step.get("value"))
            if action == "navigate":
                page.goto(text)
            elif action == "click":
                if selector:
                    page.locator(selector).first.click()
                else:
                    page.mouse.click(target["x"], target["y"])
            elif action in ("type", "fill"):
                if selector:
                    page.locator(selector).first.click()
                if action == "fill" and selector:
                    page.locator(selector).first.fill(text)
                else:
                    page.keyboard.type(text)
            elif action == "press":
                page.keyboard.press(text)
            elif action == "save_variable":
                variables[text] = page.locator(selector).first.input_value()
                os.environ["TF_VAR_" + text] = variables[text]
            else:
                raise ValueError("Unsupported action: " + action)
    finally:
        browser.close()
