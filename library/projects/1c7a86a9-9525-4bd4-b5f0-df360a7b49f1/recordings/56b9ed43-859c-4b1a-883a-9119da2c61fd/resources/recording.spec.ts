import { test } from '@playwright/test';
const { replayPlan } = require('./testforge_plan.js');
test("Testing Calculator win11", async ({ playwright }, testInfo) => {
  await replayPlan(playwright, require('./execution-plan.json'), testInfo.outputPath('files'));
});
