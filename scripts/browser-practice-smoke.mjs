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
  const startBounds = await start.boundingBox();
  assert.ok(startBounds && startBounds.y + startBounds.height <= 852, 'start must fit the first mobile screen');
  // A pending practice from another page must be recoverable instead of a dead-end 409.
  await page.waitForFunction(() => !!localStorage.getItem('avatarweave.practice.guest'));
  const pending = await page.evaluate(async () => {
    const response = await fetch('/api/practice/sessions', {method: 'POST', headers: {
      Authorization: `Bearer ${localStorage.getItem('avatarweave.practice.guest')}`,
    }});
    return {status: response.status};
  });
  assert.equal(pending.status, 201);
  await start.click();
  await page.getByRole('button', {name: '前往未完成练习'}).click({timeout: 15000});
  await page.locator('.state').filter({hasText: '直接开口，我在听'}).waitFor({timeout: 45000});
  await page.getByRole('button', {name: '结束并查看反馈', exact: true}).click();
  await page.getByRole('heading', {name: '这次练得怎么样'}).waitFor({timeout: 20000});
  await page.getByRole('button', {name: '重新开始练习'}).click();
  await page.locator('.state').filter({hasText: '直接开口，我在听'}).waitFor({ timeout: 45000 });
  for (const viewport of [{width: 375, height: 667}, {width: 393, height: 852}]) {
    await page.setViewportSize(viewport);
    await page.waitForTimeout(150);
    for (const selector of ['.mic', '.help-row', '.transcripts']) {
      const bounds = await page.locator(selector).boundingBox();
      assert.ok(bounds && bounds.y >= 0 && bounds.y + bounds.height <= viewport.height + 1, `${selector} must stay in the mobile viewport`);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }
  if (pipeline) await page.locator('.practice-stage[data-audio-ready="true"]').waitFor({ timeout: 45000 });
  if (pipeline) await page.getByRole('button', {name: '键盘输入', exact: true}).click();
  await page.getByLabel('输入英语消息').fill("I'd like to check in. My reservation is under Jamie Chen.");
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.getByText('Welcome to Willow Hotel!', { exact: false }).waitFor({ timeout: 15000 });
  if (!finishOnly) await page.getByRole('button', { name: '给我一点提示' }).click();
  if (!pipeline && !finishOnly) {
    await page.getByText('你可以试着说：', { exact: false }).waitFor({ timeout: 15000 });
    await page.getByRole('button', {name: '给我完整例句'}).click();
    await page.getByText('你可以试着说：').filter({hasText: 'Jamie Chen'}).waitFor({timeout: 15000});
    await page.getByRole('button', {name: '再听一遍', exact: true}).click();
    await page.getByText('请再说一遍。', {exact: true}).waitFor();
    await page.locator('.mission-details summary').click();
    await page.getByText('预订姓名：Jamie Chen', {exact: false}).waitFor();
    await page.locator('.mission-details summary').click();
  }
  else if (pipeline && !finishOnly) {
    await page.locator('.state').filter({hasText: '直接开口，我在听'}).waitFor({timeout: 45000});
    const beforeReplay = await page.locator('.bubble.agent').count();
    const replayText = await page.locator('.bubble.agent p').last().innerText();
    await page.getByRole('button', {name: '再听一遍', exact: true}).click();
    await page.waitForFunction(count => document.querySelectorAll('.bubble.agent').length > count, beforeReplay);
    await page.locator('.state').filter({hasText: '直接开口，我在听'}).waitFor({timeout: 45000});
    assert.equal(await page.locator('.bubble.agent p').last().innerText(), replayText, 'replay must speak the actual previous text');
    await page.getByRole('button', { name: '麦克风已开启', exact: true }).click();
    for (const [text, completed] of [["I'd like to check in. My reservation is under Jamie Chen.", 1], ["I'm staying for two nights.", 2], ['What time is breakfast?', 3]]) {
      await page.locator('.state').filter({hasText: '麦克风已关闭，点击开启'}).waitFor({ timeout: 45000 });
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
  assert.equal(finishCount, 1, 'a delayed status poll must not duplicate finish');
  // The same conflict must have a one-click destination from a completed report.
  const nextPending = await page.evaluate(async () => {
    const response = await fetch('/api/practice/sessions', {method: 'POST', headers: {
      Authorization: `Bearer ${localStorage.getItem('avatarweave.practice.guest')}`,
    }});
    return response.status;
  });
  assert.equal(nextPending, 201);
  const homeTab = await context.newPage();
  await homeTab.goto(url);
  await homeTab.getByRole('button', {name: '前往未完成练习'}).waitFor({timeout: 15000});
  await homeTab.close();
  await page.getByRole('button', {name: '再练一次'}).click();
  await page.getByRole('button', {name: '前往未完成练习'}).click({timeout: 15000});
  await page.locator('.state').filter({hasText: '直接开口，我在听'}).waitFor({timeout: 45000});
  await page.getByRole('button', {name: '结束并查看反馈', exact: true}).click();
  await page.getByRole('heading', {name: '这次练得怎么样'}).waitFor({timeout: 20000});
  await page.getByRole('button', { name: '查看练习记录', exact: true }).click();
  await page.getByRole('button', { name: /酒店入住练习/ }).first().waitFor();
  await page.reload();
  await page.getByRole('button', { name: '练习记录' }).click();
  const allRecords = page.getByRole('button', { name: /酒店入住练习/ });
  const record = allRecords.first();
  await record.waitFor();
  assert.equal(await allRecords.count(), 3);
  await record.click();
  await page.getByRole('heading', { name: '这次练得怎么样' }).waitFor();
  assert.deepEqual(errors, []);
  assert.deepEqual(signalErrors, [], 'intentional shutdown must not emit signal errors');
  assert.equal(finishCount, 2, 'each of the two separate practices must finish exactly once');

  if (!pipeline) {
    const resumeContext = await browser.newContext({viewport: {width: 393, height: 852}, isMobile: true, hasTouch: true});
    const originalTab = await resumeContext.newPage();
    await originalTab.goto(url);
    await originalTab.getByRole('button', {name: '开始我的第一次练习'}).click({timeout: 60000});
    await originalTab.locator('.state').filter({hasText: '直接开口，我在听'}).waitFor({timeout: 45000});
    await originalTab.getByLabel('输入英语消息').fill('My reservation is under Jamie Chen.');
    await originalTab.getByRole('button', {name: '发送', exact: true}).click();
    await originalTab.locator('.bubble.agent').first().waitFor();
    const resumedTab = await resumeContext.newPage();
    await resumedTab.goto(url);
    const [resumeResponse] = await Promise.all([
      resumedTab.waitForResponse(response => response.url().endsWith('/resume')),
      resumedTab.getByRole('button', {name: '回到上次练习'}).click({timeout: 15000}),
    ]);
    assert.equal(resumeResponse.status(), 200);
    await resumedTab.locator('.state').filter({hasText: '直接开口，我在听'}).waitFor({timeout: 45000});
    await resumedTab.getByText('My reservation is under Jamie Chen.', {exact: true}).waitFor();
    assert.equal(await resumedTab.getByRole('heading', {name: '这次练得怎么样'}).count(), 0);
    const replyCount = await resumedTab.locator('.bubble.agent').count();
    await resumedTab.getByLabel('输入英语消息').fill('I am staying for two nights.');
    await resumedTab.getByRole('button', {name: '发送', exact: true}).click();
    await resumedTab.getByText('I am staying for two nights.', {exact: true}).waitFor();
    await resumedTab.waitForFunction(count => document.querySelectorAll('.bubble.agent').length > count, replyCount);
    const records = await resumedTab.evaluate(async () => (await fetch('/api/practice/sessions', {headers: {
      Authorization: `Bearer ${localStorage.getItem('avatarweave.practice.guest')}`,
    }})).json());
    assert.equal(records.length, 1, 'resume must not create a new practice');
    assert.equal(records[0].status, 'active');
    await resumedTab.getByRole('button', {name: '结束并查看反馈', exact: true}).click();
    await resumedTab.getByRole('heading', {name: '这次练得怎么样'}).waitFor({timeout: 20000});
    await resumeContext.close();
  }

  const staleContext = await browser.newContext({viewport: {width: 393, height: 852}});
  await staleContext.addInitScript(() => localStorage.setItem('avatarweave.practice.guest', 'stale-ui-test'));
  const stalePage = await staleContext.newPage();
  let staleEnded = false;
  let unwantedReports = 0;
  const staleRecord = {id: 'stale', status: 'active', goals: [], help_count: 0, report: null,
    created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 60000).toISOString()};
  await stalePage.route('**/api/practice/quota', route => route.fulfill({json: {limit: 50, used: 1, remaining: 49, attempts_remaining: 249, resets_at: new Date().toISOString()}}));
  await stalePage.route('**/api/practice/sessions', route => route.fulfill({json: [{...staleRecord, status: staleEnded ? 'ended' : 'active'}]}));
  await stalePage.route('**/api/practice/sessions/stale/resume', route => {
    staleEnded = true;
    return route.fulfill({status: 410, json: {detail: '上次练习已经结束或超时，请重新开始一场练习'}});
  });
  stalePage.on('request', request => { if (request.url().endsWith('/report')) unwantedReports++; });
  await stalePage.goto(url);
  await stalePage.getByRole('button', {name: '回到上次练习'}).click({timeout: 60000});
  await stalePage.getByRole('alert').filter({hasText: '上次练习已经结束或超时'}).waitFor();
  assert.equal(await stalePage.locator('.review-page').count(), 0, 'a stale resume must never jump to a report');
  assert.equal(unwantedReports, 0);
  await stalePage.getByRole('button', {name: '再来一次酒店入住'}).waitFor();
  await staleContext.close();

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

  const exhausted = await browser.newContext({ viewport: { width: 393, height: 852 } });
  const exhaustedPage = await exhausted.newPage();
  await exhaustedPage.route('**/api/practice/quota', route => route.fulfill({json: {
    limit: 5, used: 5, remaining: 0, attempts_remaining: 20, resets_at: new Date().toISOString(),
  }}));
  await exhaustedPage.goto(url);
  const limitButton = exhaustedPage.getByRole('button', {name: '今天的次数已用完'});
  await limitButton.waitFor({timeout: 60000});
  assert.equal(await limitButton.isDisabled(), true, 'Exhausted quota must be visible before a failing POST');
  await exhausted.close();
  console.log(`PASS: ${finishOnly ? 'finish-only regression' : 'complete practice'}, real room, mobile avatar, text streams, persisted finish, delayed-poll race, Safari-like 1006 shutdown, report retry, history reload, microphone denial; zero page or signal errors`);
} finally {
  await browser.close();
}
