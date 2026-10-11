import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("Session 11/10/2026, 14:42:14", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
