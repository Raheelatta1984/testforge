import { defineConfig } from '@playwright/test';
const { launchOptions } = require('./testforge_runtime.js');
export default defineConfig({ testMatch: 'recording.spec.ts', workers: 1, fullyParallel: false, retries: 0, reporter: [['list'], ['html', {open: 'never'}]], use: {browserName: 'chromium', launchOptions: launchOptions(), viewport: {width:1024,height:640}, screenshot: 'only-on-failure', video: 'off', trace: 'off'} });
