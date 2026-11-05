import { chromium } from '@playwright/test';
import { readFile } from 'node:fs/promises';

// Provision an operator-owned synthetic account first. The protected fixture
// contains temporary cookies, not an OIDC bypass exposed by the application.
let browser, page, sessionId, resumedSessionId, phase = 'fixture';
let passed = false;
try {
  const fixture = JSON.parse(await readFile(process.env.MEMORY_RTC_SMOKE_PROFILE ?? '.runtime/private-rtc-smoke/profile.json', 'utf8'));
  const origin = new URL(fixture.origin).origin;
  browser = await chromium.launch({ channel: 'chromium', headless: true, args: [
    '--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
    '--autoplay-policy=no-user-gesture-required', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    `--use-file-for-fake-audio-capture=${fixture.audio}`,
  ] });
  const context = await browser.newContext({ permissions: ['microphone'], viewport: { width: 1440, height: 1080 } });
  await context.addCookies([
    { name: '__Host-avatarweave-account', value: fixture.token, url: origin, secure: true, httpOnly: true, sameSite: 'Lax' },
    { name: '__Host-avatarweave-csrf', value: fixture.csrf, url: origin, secure: true, httpOnly: false, sameSite: 'Lax' },
  ]);
  page = await context.newPage();
  if (fixture.resume_session_id) {
    sessionId = fixture.resume_session_id;
    await page.addInitScript(id => sessionStorage.setItem('avatarweave.private-session', JSON.stringify({ id, memory_enabled: true })), fixture.resume_session_id);
  }
  page.on('response', async response => {
    if (new URL(response.url()).pathname === '/api/v2/sessions' && response.status() === 201) {
      try { sessionId = (await response.json()).data.session_id; } catch { /* No credential diagnostics. */ }
    }
    if (new URL(response.url()).pathname.endsWith('/resume') && response.status() === 200) {
      try { resumedSessionId = (await response.json()).data.session_id; } catch { /* No credential diagnostics. */ }
    }
  });
  phase = 'account_page';
  await page.goto(origin, { waitUntil: 'domcontentloaded' });
  await page.getByText('数字人已就绪', { exact: false }).waitFor({ timeout: 60000 });
  await page.getByLabel('本次对话使用记忆', { exact: true }).check();
  phase = 'private_connection';
  await page.getByRole('button', { name: '开始账号对话', exact: true }).click();
  await page.getByRole('button', { name: '开启麦克风', exact: true }).waitFor({ state: 'visible' });
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).find(button => button.textContent === '开启麦克风')?.disabled, undefined, { timeout: 35000 });
  phase = 'greeting';
  await page.locator('.message.agent').first().waitFor({ timeout: 60000 });
  phase = 'decoded_audio';
  await page.waitForFunction(() => {
    if (!window.__memoryRtcAudio) {
      const audio = document.querySelector('audio');
      if (!audio?.srcObject || audio.paused) return false;
      const ctx = new AudioContext();
      const analyser = ctx.createAnalyser();
      ctx.createMediaStreamSource(audio.srcObject).connect(analyser);
      void ctx.resume();
      window.__memoryRtcAudio = { ctx, analyser, samples: new Float32Array(analyser.fftSize) };
    }
    const { analyser, samples } = window.__memoryRtcAudio;
    analyser.getFloatTimeDomainData(samples);
    return samples.some(value => Math.abs(value) > 0.001);
  }, undefined, { timeout: 60000 });
  await page.getByText('正在聆听', { exact: true }).waitFor({ timeout: 60000 });
  phase = 'text_reply';
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('这是一条系统连通性测试，请只回复：连接正常。');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.waitForFunction(() => Array.from(document.querySelectorAll('.message.agent')).some(node => node.textContent.includes('连接正常')), undefined, { timeout: 60000 });
  await page.getByText('正在聆听', { exact: true }).waitFor({ timeout: 60000 });
  phase = 'microphone_transcription';
  const usersBefore = await page.locator('.message.user').count();
  const agentsBefore = await page.locator('.message.agent').count();
  await page.getByRole('button', { name: '开启麦克风', exact: true }).click();
  await page.waitForFunction(count => {
    const nodes = document.querySelectorAll('.message.user');
    return nodes.length > count && !nodes[nodes.length - 1].querySelector('.cursor') && nodes[nodes.length - 1].textContent.length > 4;
  }, usersBefore, { timeout: 60000 });
  await page.getByRole('button', { name: '关闭麦克风', exact: true }).click();
  phase = 'speech_reply';
  await page.waitForFunction(count => document.querySelectorAll('.message.agent').length > count, agentsBefore, { timeout: 60000 });
  phase = 'reload_resume';
  const originalSessionId = sessionId;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.getByText('数字人已就绪', { exact: false }).waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: '开始账号对话', exact: true }).click();
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).find(button => button.textContent === '开启麦克风')?.disabled, undefined, { timeout: 35000 });
  if (resumedSessionId !== originalSessionId || sessionId !== originalSessionId) throw new Error('Reload created another session');
  phase = 'resumed_greeting';
  await page.locator('.message.agent').first().waitFor({ timeout: 60000 });
  await page.getByText('正在聆听', { exact: true }).waitFor({ timeout: 60000 });
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('这是一条恢复连接测试，请只回复：恢复正常。');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.waitForFunction(() => Array.from(document.querySelectorAll('.message.agent')).some(node => node.textContent.includes('恢复正常')), undefined, { timeout: 60000 });
  await page.getByRole('button', { name: /结束对话/ }).click();
  passed = true;
  console.log(JSON.stringify({ status: 'passed', real_private_rtc: true, memory_enabled: true, text_reply: true,
    microphone_transcription: true, speech_reply: true, reload_resumed_same_session: true, reload_greeting: true,
    expired_owner_session_resumed: Boolean(fixture.resume_session_id), decoded_non_silent_audio: true, device_hearing_measured: false }));
} catch {
  // Do not leak filled credentials, token payloads or private message text.
  console.error(JSON.stringify({ status: 'failed', phase }));
  process.exitCode = 1;
} finally {
  if (page) {
    await page.evaluate(() => window.__memoryRtcAudio?.ctx.close()).catch(() => {});
    if (!passed && sessionId) await page.evaluate(async id => {
      const csrf = document.cookie.split('; ').find(value => value.startsWith('__Host-avatarweave-csrf='))?.split('=').slice(1).join('=') ?? '';
      await fetch(`/api/v2/sessions/${encodeURIComponent(id)}/end`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf) }, body: JSON.stringify({ reason: 'user_end', idempotency_key: crypto.randomUUID() }) });
    }, sessionId).catch(() => {});
  }
  await browser?.close();
}
