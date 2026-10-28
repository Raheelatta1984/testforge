import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("Session 28 Oct 2026 4:48:42 PM", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
