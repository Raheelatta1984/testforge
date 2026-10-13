import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("Session 14/10/2026, 01:45:44", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
