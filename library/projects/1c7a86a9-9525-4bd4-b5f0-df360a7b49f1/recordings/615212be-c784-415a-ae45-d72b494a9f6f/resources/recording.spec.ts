import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("testcomplete", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
