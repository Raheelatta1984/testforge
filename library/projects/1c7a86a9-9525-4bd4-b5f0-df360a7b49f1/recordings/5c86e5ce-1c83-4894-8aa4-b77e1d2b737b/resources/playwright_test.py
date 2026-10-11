"""Generated TestForge Playwright replay. Install playwright and chromium first."""
import os
import re
from playwright.sync_api import sync_playwright

STEPS = [{'id': 'bb15b6dc-b592-47f8-908b-eb5156b8b122', 'order': 1, 'action': 'navigate', 'value': 'https://hfm-wholesale-app-dev.ashysand-8cabb4df.australiaeast.azurecontainerapps.io/daily-orders?storeNo=426&date=2026-10-08', 'label': 'Open https://hfm-wholesale-app-dev.ashysand-8cabb4df.australiaeast.azurecontainerapps.io/daily-orders?storeNo=426&date=2026-10-08', 'selector': None}, {'id': 'ed15d2b4-eebf-4173-9f5d-daa5dbc7b5d0', 'order': 2, 'action': 'click', 'value': None, 'label': 'Click 123,322', 'selector': {'primary': 'div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > form:nth-of-type(1) > label:nth-of-type(1) > input:nth-of-type(1)', 'x': 123, 'y': 322, 'text': '', 'tag': 'input'}, 'screenshot': 'step-002.png'}, {'id': '1646ff6f-e63f-4cb7-b097-9bc7b8e19431', 'order': 3, 'action': 'type', 'value': 'Ratta', 'label': 'Type: Ratta', 'selector': {'primary': 'div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > form:nth-of-type(1) > label:nth-of-type(1) > input:nth-of-type(1)'}, 'screenshot': 'step-003.png'}, {'id': '34480860-9401-4fee-a0b8-155abb94183b', 'order': 4, 'action': 'click', 'value': None, 'label': 'Click 115,416', 'selector': {'primary': 'div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(1) > form:nth-of-type(1) > label:nth-of-type(2) > input:nth-of-type(1)', 'x': 115, 'y': 416, 'text': '', 'tag': 'input'}, 'screenshot': 'step-004.png'}]

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
