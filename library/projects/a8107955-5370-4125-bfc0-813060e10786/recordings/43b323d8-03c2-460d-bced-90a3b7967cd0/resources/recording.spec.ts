import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("D365_UAT_Login", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
