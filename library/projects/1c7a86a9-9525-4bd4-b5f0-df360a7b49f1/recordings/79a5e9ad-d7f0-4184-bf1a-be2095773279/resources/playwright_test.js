// npm install; node playwright_test.js. Supply TF_VAR_<name> variables.
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
