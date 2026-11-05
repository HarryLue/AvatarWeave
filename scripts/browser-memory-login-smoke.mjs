import { chromium } from '@playwright/test';
import { readFile } from 'node:fs/promises';

// Actual optional development OIDC, not the UI fixture transport. Never log
// credentials, callback queries, cookies, token bodies, or browser storage.
let phase = 'configuration';
let browser;
try {
  const info = JSON.parse(await readFile('.runtime/memory-deployment.json', 'utf8'));
  if (!info.local_auth) throw new Error('Local development OIDC is required');
  const credentials = JSON.parse(await readFile('.runtime/memory-auth/credentials.json', 'utf8'));
  const origin = new URL(info.origin).origin;
  if (!origin.startsWith('https://')) throw new Error('HTTPS is required');
  browser = await chromium.launch({
    channel: 'chromium', headless: true,
    args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
  });
  const context = await browser.newContext();
  const page = await context.newPage();
  phase = 'login_page';
  const response = await page.goto(origin + '/api/v2/auth/login', { waitUntil: 'domcontentloaded' });
  if (response?.status() !== 200 || !new URL(page.url()).pathname.startsWith('/idp/')) {
    throw new Error('OIDC login page did not load');
  }
  phase = 'account_login';
  await page.locator('#username').fill(credentials.username);
  await page.locator('#password').fill(credentials.password);
  await page.locator('#kc-login').click();
  await page.waitForURL(url => url.origin === origin && url.pathname === '/', { timeout: 30000 });
  phase = 'account_identity';
  const identity = await page.request.get(origin + '/api/v2/me');
  if (identity.status() !== 200 || (await identity.json()).data?.state !== 'account') {
    throw new Error('Authenticated identity is unavailable');
  }
  phase = 'account_logout';
  const csrf = (await context.cookies(origin)).find(cookie => cookie.name === '__Host-avatarweave-csrf')?.value;
  const logout = await page.request.post(origin + '/api/v2/auth/logout', {
    headers: { Origin: origin, 'X-CSRF-Token': csrf ?? '' },
  });
  const after = await page.request.get(origin + '/api/v2/me');
  if (logout.status() !== 200 || (await after.json()).data?.state !== 'anonymous') {
    throw new Error('Logout did not revoke the test login');
  }
  console.log(JSON.stringify({ status: 'passed', origin, real_oidc: true, login: true, logout: true, tls_verification: true }));
} catch {
  // Playwright diagnostics can embed filled form values and full callback URLs.
  console.error(JSON.stringify({ status: 'failed', phase }));
  process.exitCode = 1;
} finally {
  await browser?.close();
}
