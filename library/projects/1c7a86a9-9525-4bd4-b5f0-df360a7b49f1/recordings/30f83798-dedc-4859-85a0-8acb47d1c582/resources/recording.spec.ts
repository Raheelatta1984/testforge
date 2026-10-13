import { test } from '@playwright/test';
const { replay } = require('./testforge_runtime.js');
test("HarrisFarm Shopify Website", async ({ page }, testInfo) => {
  await replay(page, require('./steps.json'), testInfo.outputPath('files'));
});
