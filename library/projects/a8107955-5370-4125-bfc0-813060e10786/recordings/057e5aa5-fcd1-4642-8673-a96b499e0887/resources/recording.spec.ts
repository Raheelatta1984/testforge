import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("Session 12/10/2026, 8:22:44 am", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
