import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';

const url = process.env.PRACTICE_SMOKE_URL || 'http://127.0.0.1:5175/speaking/';
const artifacts = process.env.PRACTICE_SMOKE_ARTIFACTS || '/tmp';
const pipeline = process.env.PRACTICE_SMOKE_MODE === 'pipeline';
const finishOnly = process.env.PRACTICE_SMOKE_FINISH_ONLY === '1';
const browser = await chromium.launch({ channel: 'chromium', headless: true,
  args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] });
const context = await browser.newContext({ viewport: { width: 393, height: 852 }, isMobile: true, hasTouch: true, permissions: ['microphone'] });
await context.addInitScript(() => {
  // Reproduce Safari's abnormal close event during an intentional signal shutdown.
  const NativeWebSocket = window.WebSocket;
  window.__forced1006Count = 0;
  window.WebSocket = class extends NativeWebSocket {
    constructor(...args) {
      super(...args);
      this.addEventListener('close', event => {
        if (!window.__requestedPracticeFinish) return;
        Object.defineProperty(event, 'code', { value: 1006 });
        Object.defineProperty(event, 'wasClean', { value: false });
        window.__forced1006Count++;
      });
    }
  };
});
const page = await context.newPage();
const errors = [];
const signalErrors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('console', message => {
  if (message.type() === 'error' && /error reading from signal stream|WS closed unexpectedly/.test(message.text())) signalErrors.push(message.text());
});
try {
  await page.goto(url);
  const start = page.getByRole('button', { name: '开始我的第一次练习' });
  await start.waitFor({ timeout: 60000 });
  await page.screenshot({ path: `${artifacts}/practice-home-mobile.png`, fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await start.click();
  await page.getByText('轮到你了，慢慢说', { exact: true }).waitFor({ timeout: 45000 });
  if (pipeline) await page.locator('.practice-stage[data-audio-ready="true"]').waitFor({ timeout: 45000 });
  await page.getByLabel('输入英语消息').fill("I'd like to check in. My reservation is under Alex Chen.");
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.getByText('Welcome to Willow Hotel!', { exact: false }).waitFor({ timeout: 15000 });
  if (!finishOnly) await page.getByRole('button', { name: '给我一点提示' }).click();
  if (!pipeline && !finishOnly) await page.getByText('你可以试着说：', { exact: false }).waitFor({ timeout: 15000 });
  else if (pipeline && !finishOnly) {
    await page.getByRole('button', { name: '麦克风已开启', exact: true }).click();
    for (const [text, completed] of [["I'd like to check in. My reservation is under Alex Chen.", 1], ["I'm staying for two nights.", 2], ['What time is breakfast?', 3]]) {
      await page.getByText('轮到你了，慢慢说', { exact: true }).waitFor({ timeout: 45000 });
      await page.getByLabel('输入英语消息').fill(text);
      await page.getByRole('button', { name: '发送', exact: true }).click();
      await page.locator('.progress-label b').filter({ hasText: `${completed}/3` }).waitFor({ timeout: 45000 });
    }
  }
  await page.screenshot({ path: `${artifacts}/practice-call-mobile.png`, fullPage: true });
  let releasePoll;
  const pollGate = new Promise(resolve => { releasePoll = resolve; });
  let pollCaptured;
  const pollReady = new Promise(resolve => { pollCaptured = resolve; });
  let holdPoll = true;
  await page.route(/\/api\/practice\/sessions\/[^/]+$/, async route => {
    if (route.request().method() !== 'GET' || !holdPoll) return route.continue();
    holdPoll = false;
    const response = await route.fetch();
    const snapshot = await response.json();
    pollCaptured();
    await pollGate;
    return route.fulfill({ json: snapshot });
  });
  await Promise.race([pollReady, new Promise((_, reject) => setTimeout(() => reject(new Error('status poll not captured')), 10000))]);
  let failReportOnce = true;
  await page.route('**/api/practice/sessions/*/report', async route => {
    if (failReportOnce) {
      failReportOnce = false;
      return route.fulfill({ status: 503, json: { detail: '反馈暂时无法生成，练习记录已保存，请重试' } });
    }
    return route.continue();
  });
  let finishCount = 0;
  page.on('request', request => { if (request.url().endsWith('/finish')) finishCount++; });
  const finished = page.waitForResponse(response => response.url().endsWith('/finish'));
  await page.evaluate(() => { window.__requestedPracticeFinish = true; });
  await page.getByRole('button', { name: pipeline ? '完成练习，看看反馈 ↗' : '结束并查看反馈', exact: true }).click();
  await finished;
  releasePoll();
  await page.getByRole('button', { name: '重试生成反馈' }).waitFor({ timeout: 20000 });
  await page.getByRole('button', { name: '重试生成反馈' }).click();
  await page.getByRole('heading', { name: '这次练得怎么样' }).waitFor({ timeout: 55000 });
  assert.ok(await page.evaluate(() => window.__forced1006Count > 0), 'the close regression must exercise a 1006 event');
  await page.screenshot({ path: `${artifacts}/practice-review-mobile.png`, fullPage: true });
  await page.getByRole('button', { name: '查看练习记录', exact: true }).click();
  await page.getByRole('button', { name: /酒店入住练习/ }).waitFor();
  await page.reload();
  await page.getByRole('button', { name: '练习记录' }).click();
  const record = page.getByRole('button', { name: /酒店入住练习/ });
  await record.waitFor();
  assert.equal(await record.count(), 1);
  await record.click();
  await page.getByRole('heading', { name: '这次练得怎么样' }).waitFor();
  assert.deepEqual(errors, []);
  assert.deepEqual(signalErrors, [], 'intentional shutdown must not emit signal errors');
  assert.equal(finishCount, 1, 'a delayed status poll must not duplicate finish');

  const denied = await browser.newContext({ viewport: { width: 393, height: 852 } });
  const deniedPage = await denied.newPage();
  await deniedPage.route('**/api/practice/scenario', async route => {
    const response = await route.fetch();
    return route.fulfill({ json: { ...await response.json(), mode: 'pipeline' } });
  });
  let createdSessions = 0;
  deniedPage.on('request', request => {
    if (request.url().endsWith('/api/practice/sessions') && request.method() === 'POST') createdSessions++;
  });
  await deniedPage.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('麦克风权限被拒绝', 'NotAllowedError'); };
  });
  await deniedPage.goto(url);
  await deniedPage.getByRole('button', { name: '开始我的第一次练习' }).click({ timeout: 60000 });
  await deniedPage.getByRole('alert').filter({ hasText: '麦克风权限被拒绝' }).waitFor();
  assert.equal(createdSessions, 0, 'Denied microphone must not consume a practice');
  await denied.close();
  console.log(`PASS: ${finishOnly ? 'finish-only regression' : 'complete practice'}, real room, mobile avatar, text streams, persisted finish, delayed-poll race, Safari-like 1006 shutdown, report retry, history reload, microphone denial; zero page or signal errors`);
} finally {
  await browser.close();
}
