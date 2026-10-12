import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("Session 13/10/2026, 08:03:59", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
