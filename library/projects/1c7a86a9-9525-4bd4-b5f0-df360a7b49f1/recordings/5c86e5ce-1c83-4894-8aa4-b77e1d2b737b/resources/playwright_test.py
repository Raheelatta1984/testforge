"""Generated TestForge Playwright replay. Install playwright and chromium first."""
import os
import re
from playwright.sync_api import sync_playwright

STEPS = []

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
