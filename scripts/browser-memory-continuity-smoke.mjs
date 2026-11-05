import { chromium, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';

// Run write, rotate the operator-owned synthetic login, then read in a fresh
// browser. No real user's cookies or memories are used or logged.
const mode = process.env.MEMORY_CONTINUITY_PHASE ?? 'write';
let browser, page, sessionId, phase = 'fixture', memoryStatus;
try {
  if (!['write', 'read'].includes(mode)) throw new Error('Invalid phase');
  const fixture = JSON.parse(await readFile('.runtime/private-rtc-smoke/profile.json', 'utf8'));
  const origin = new URL(fixture.origin).origin;
  browser = await chromium.launch({ channel: 'chromium', headless: true, args: [
    '--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
    '--autoplay-policy=no-user-gesture-required', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
  ] });
  const context = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1440, height: 1080 } });
  await context.addInitScript(() => {
    const parse = JSON.parse;
    JSON.parse = function (...args) {
      const value = parse.apply(this, args);
      if (value?.version === 1 && value.state === 'failed' && typeof value.source_saved === 'boolean') {
        const code = typeof value.code === 'string' && /^[a-z_]{1,64}$/.test(value.code) ? value.code : 'unknown';
        window.__memoryContinuityFaults = [...(window.__memoryContinuityFaults ?? []), code].slice(-8);
      }
      return value;
    };
  });
  await context.addCookies([
    { name: '__Host-avatarweave-account', value: fixture.token, url: origin, secure: true, httpOnly: true, sameSite: 'Lax' },
    { name: '__Host-avatarweave-csrf', value: fixture.csrf, url: origin, secure: true, httpOnly: false, sameSite: 'Lax' },
  ]);
  page = await context.newPage();
  page.on('response', async response => {
    if (new URL(response.url()).pathname === '/api/v2/sessions' && response.status() === 201) {
      try { sessionId = (await response.json()).data.session_id; } catch { /* No secrets in diagnostics. */ }
    }
  });
  phase = 'account_connection';
  await page.goto(origin, { waitUntil: 'domcontentloaded' });
  await page.getByText('数字人已就绪', { exact: false }).waitFor({ timeout: 60000 });
  await page.getByLabel('本次对话使用记忆', { exact: true }).check();
  await page.getByRole('button', { name: '开始账号对话', exact: true }).click();
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).find(b => b.textContent === '开启麦克风')?.disabled, undefined, { timeout: 35000 });
  await page.locator('.message.agent').first().waitFor({ timeout: 60000 });
  async function send(text) {
    const label = phase;
    phase = label + ':ready';
    await page.getByText('正在聆听', { exact: true }).waitFor({ timeout: 60000 });
    const before = await page.locator('.message.agent').count();
    await page.getByRole('textbox', { name: '消息', exact: true }).fill(text);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    phase = label + ':reply';
    await page.waitForFunction(count => document.querySelectorAll('.message.agent').length > count, before, { timeout: 60000 });
    phase = label + ':settled';
    await page.getByText('正在聆听', { exact: true }).waitFor({ timeout: 60000 });
  }
  const memories = async () => {
    const response = await context.request.get(origin + '/api/v2/memories');
    memoryStatus = response.status();
    if (!response.ok()) return [];
    return (await response.json()).data.items;
  };
  if (mode === 'write') {
    phase = 'persist_name';
    await send('请记住，我叫林小舟。');
    phase = 'persist_name:memory';
    await expect.poll(async () => (await memories()).some(m => m.text.includes('林小舟')), { timeout: 60000, intervals: [2000] }).toBe(true);
    phase = 'coarse_meeting';
    await send('下午有个会议。');
    phase = 'coarse_meeting:memory';
    await expect.poll(async () => (await memories()).filter(m => /会议|开会/.test(m.text)).length, { timeout: 60000, intervals: [2000] }).toBe(1);
    const original = (await memories()).find(m => /会议|开会/.test(m.text));
    phase = 'refine_meeting';
    await send('下午三点有个会议。');
    phase = 'refine_meeting:memory';
    await expect.poll(async () => {
      const meetings = (await memories()).filter(m => /会议|开会/.test(m.text));
      return meetings.length === 1 && meetings[0].memory_id === original.memory_id && meetings[0].version > original.version && meetings[0].valid_time.precision === 'instant' && /三点/.test(meetings[0].text);
    }, { timeout: 60000, intervals: [2000] }).toBe(true);
  } else {
    phase = 'new_login_name_reply';
    await send('还记得我吗？先说我的姓名。');
    await expect.poll(async () => (await page.locator('.message.agent').last().textContent()).includes('林小舟'), { timeout: 10000 }).toBe(true);
  }
  phase = 'end';
  await page.getByRole('button', { name: /结束对话/ }).click();
  sessionId = null;
  console.log(JSON.stringify({ status: 'passed', mode, actual_https_rtc: true,
    same_meeting_id_updated: mode === 'write', fresh_browser_name_recalled: mode === 'read', actual_oidc_flow_tested: false }));
} catch {
  const codes = await page?.evaluate(() => window.__memoryContinuityFaults ?? []).catch(() => []);
  console.error(JSON.stringify({ status: 'failed', mode, phase, memory_status: memoryStatus, codes }));
  process.exitCode = 1;
} finally {
  if (page && sessionId) await page.evaluate(async id => {
    const csrf = document.cookie.split('; ').find(v => v.startsWith('__Host-avatarweave-csrf='))?.split('=').slice(1).join('=') ?? '';
    await fetch(`/api/v2/sessions/${encodeURIComponent(id)}/end`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf) }, body: JSON.stringify({ reason: 'user_end', idempotency_key: crypto.randomUUID() }) });
  }, sessionId).catch(() => {});
  await browser?.close();
}
