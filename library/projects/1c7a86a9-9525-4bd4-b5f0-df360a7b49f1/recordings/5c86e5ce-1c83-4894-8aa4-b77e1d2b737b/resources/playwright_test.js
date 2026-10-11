// Generated TestForge Playwright JavaScript replay. npm install playwright
const { chromium } = require('playwright');
const steps = require('./steps.json');
const vars = {};
function value(text) {
  return String(text ?? '').replace(/\{\{\s*([\w.\-]+)\s*\}\}/g, (_match, name) => {
    if (name in vars) return vars[name];
    if (!(`TF_VAR_${name}` in process.env)) throw new Error(`Missing TF_VAR_${name}`);
    return process.env[`TF_VAR_${name}`];
  });
}
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1024, height: 640 } });
  try {
    for (const step of steps) {
      const selector = (step.selector || {}).primary;
      const text = value(step.value);
      if (step.action === 'navigate') await page.goto(text);
      else if (step.action === 'click') {
        if (selector) await page.locator(selector).first().click();
        else await page.mouse.click(step.selector.x, step.selector.y);
      } else if (step.action === 'fill' && selector) await page.locator(selector).first().fill(text);
      else if (step.action === 'type' || step.action === 'fill') {
        if (selector) await page.locator(selector).first().click();
        await page.keyboard.type(text);
      } else if (step.action === 'press') await page.keyboard.press(text);
      else if (step.action === 'save_variable') vars[text] = await page.locator(selector).first().inputValue();
      else throw new Error(`Unsupported action: ${step.action}`);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
