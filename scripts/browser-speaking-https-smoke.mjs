import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';

const origin = process.env.SPEAKING_HTTPS_ORIGIN || 'https://111.56.189.4';
const browser = await chromium.launch({ channel: 'chromium', headless: true,
  args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
// Certificate verification remains enabled: a self-signed or invalid certificate must fail.
const context = await browser.newContext({ viewport: { width: 393, height: 852 }, isMobile: true, hasTouch: true });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(`${origin}/speaking/`);
  await page.getByRole('button', { name: '开始我的第一次练习' }).waitFor({ timeout: 60000 });
  assert.match(await page.title(), /开口旅行/);
  assert.equal(await page.evaluate(() => isSecureContext && !!navigator.mediaDevices?.getUserMedia), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const redirect = await context.request.get(`${origin}/speaking`, { maxRedirects: 0 });
  assert.equal(redirect.status(), 308);
  const deepLink = await context.request.get(`${origin}/speaking/history`);
  assert.match(await deepLink.text(), /开口旅行/);
  assert.equal((await context.request.get(`${origin}/speaking/assets/nonexistent-check.js`)).status(), 404);
  const original = await context.request.get(`${origin}/`);
  assert.match(await original.text(), /AvatarWeave/);
  const signup = await context.request.post(`${origin}/api/practice/guest`);
  assert.equal(signup.status(), 201);
  const { token } = await signup.json();
  const headers = { Authorization: `Bearer ${token}` };
  const start = await context.request.post(`${origin}/api/practice/sessions`, { headers });
  assert.equal(start.status(), 201);
  const practice = await start.json();
  try {
    const rtc = new URL(practice.credentials.server_url);
    assert.equal(rtc.protocol, 'wss:');
    assert.equal(rtc.host, new URL(origin).host);
    assert.equal(rtc.pathname.replace(/\/$/, ''), '/realtime');
  } finally {
    const finish = await context.request.post(`${origin}/api/practice/sessions/${practice.id}/finish`, { headers });
    assert.equal(finish.status(), 200);
    assert.equal((await finish.json()).status, 'ended');
  }
  assert.deepEqual(errors, []);
  console.log('PASS: trusted HTTPS, mobile avatar, secure microphone API, speaking routes, original Demo, same-origin API and WSS credentials; zero page errors. No model conversation started.');
} finally {
  await browser.close();
}
