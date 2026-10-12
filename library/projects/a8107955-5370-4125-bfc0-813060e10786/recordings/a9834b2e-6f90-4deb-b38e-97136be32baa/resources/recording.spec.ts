import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("Session 12/10/2026, 12:22:31", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
