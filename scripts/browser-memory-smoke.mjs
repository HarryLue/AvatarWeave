import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';

const baseUrl = process.env.MEMORY_UI_BASE_URL ?? 'http://127.0.0.1:5188';
const probeDirectory = await mkdtemp(`${process.cwd()}/.runtime/private-file-probe-`);
try {
  const probe = `${probeDirectory}/credentials.json`;
  await writeFile(probe, '{"synthetic":true}', { mode: 0o600 });
  for (const privatePath of [`${process.cwd()}/.env`, probe]) {
    const result = await fetch(new URL(`/@fs${privatePath}`, baseUrl));
    assert.ok([403, 404].includes(result.status), 'Development server exposed a private configuration path');
    await result.body?.cancel();
  }
} finally { await rm(probeDirectory, { recursive: true }); }

// Explicit fixture transport for UI. Python tests check actual API/SQL/provider behavior.
const browser = await chromium.launch({ channel: 'chromium', headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
const errors = [], writes = [];
page.on('pageerror', error => errors.push(error.message));
let memory = { memory_id: 'synthetic-memory', version: 1, text: '喜欢茉莉花茶', kind: 'preference', state: 'current', pinned: false, label: 'user_report', valid_time: { precision: 'unknown' }, evidence: [] };
let loop = { loop_id: 'synthetic-loop', version: 1, title: '下周面试', event_time: { precision: 'unknown' }, status: 'planned', user_allows_followup: false };
let people = [];
let reflection = { reflection_id: 'reflection-fixture', version: 1, text: '你可能喜欢安静的休息方式', state: 'needs_confirmation' };
const preferences = { preference_version: 1, long_term_memory_enabled: true, raw_transcript_days: 90, retain_evidence_capsules: true, allow_proactive_followup: false, allow_anniversary_recall: false, cross_companion_share: 'none', permitted_shared_predicates: [] };
await page.route('**/api/**', async route => {
  const request = route.request(), path = new URL(request.url()).pathname, body = request.postDataJSON();
  if (request.method() !== 'GET') writes.push({ path, body });
  let data;
  if (path === '/api/health') data = { pipeline: { mode: 'mock' } };
  else if (path === '/api/avatars') data = { default_avatar: 'kei', avatars: [{ id: 'kei', name: 'Kei', draggable: true, actions: {}, assets: { core_url: '/avatar-assets/aitutor.min.js', model_url: '/avatar-assets/kei_vowels_pro/kei_vowels_pro.model3.json' } }] };
  else if (path === '/api/tts/voices') data = { enabled: false, voices: [], default_voice: null, capabilities: { voice_selection: false, preview: false }, provider: 'fixture', provider_label: 'fixture', notice: '' };
  else if (path === '/api/token') { await route.fulfill({ status: 401, json: {} }); return; }
  else if (path === '/api/v2/me') data = { state: 'account', companion_ids: ['companion'] };
  else if (path === '/api/v2/memory-preferences') data = preferences;
  else if (path === '/api/v2/memories') data = { items: memory ? [memory] : [], next_cursor: null };
  else if (path.endsWith('/pin')) { memory = { ...memory, pinned: body.pinned, version: memory.version + 1 }; data = memory; }
  else if (path === '/api/v2/memories/synthetic-memory' && request.method() === 'PATCH') { memory = { ...memory, text: body.correction_text, version: memory.version + 1 }; data = memory; }
  else if (path === '/api/v2/memories/synthetic-memory' && request.method() === 'DELETE') { memory = null; data = { deletion_job_id: 'delete-fixture' }; }
  else if (path.endsWith('/evidence')) data = { ...memory, evidence: [{ quote: '我喜欢茉莉花茶。' }] };
  else if (path === '/api/v2/timeline') data = { items: memory ? [{ ...memory, title: memory.text, period: memory.valid_time, status: 'reported' }] : [], next_cursor: null };
  else if (path === '/api/v2/open-loops') data = { items: [loop], next_cursor: null };
  else if (path === '/api/v2/entities' && request.method() === 'GET') data = { items: people, next_cursor: null };
  else if (path === '/api/v2/entities') { data = { entity_id: 'person-fixture', version: 1, canonical_name: body.canonical_name, aliases: body.aliases }; people = [data]; }
  else if (path === '/api/v2/entities/person-fixture' && request.method() === 'DELETE') { people = []; data = { deletion_job_id: 'person-delete-fixture' }; }
  else if (path === '/api/v2/entities/person-fixture') { data = { ...people[0], version: 2, canonical_name: body.canonical_name, aliases: body.aliases }; people = [data]; }
  else if (path === '/api/v2/reflections') data = { items: [reflection], next_cursor: null };
  else if (path === '/api/v2/reflections/reflection-fixture') { reflection = { ...reflection, state: body.action === 'confirm' ? 'confirmed' : 'rejected' }; data = reflection; }
  else if (path === '/api/v2/open-loops/synthetic-loop') { loop = { ...loop, status: body.status, version: loop.version + 1 }; data = loop; }
  else if (path === '/api/v2/exports') data = { export_id: 'export-fixture', state: 'ready', download_handle: 'export-fixture', conflicts: [] };
  else if (path.startsWith('/api/v2/deletion-jobs/')) data = { physical_cleanup_complete: false };
  else if (path === '/api/v2/sessions') data = { session_id: 'session-fixture', memory_enabled: body.memory_enabled, rtc: null };
  else if (path.endsWith('/end')) data = { session_id: 'session-fixture', status: 'ended' };
  else { await route.fulfill({ status: 404, json: {} }); return; }
  await route.fulfill({ json: path.startsWith('/api/v2/') ? { status: 'ok', data, error: null, trace_id: 'fixture' } : data });
});
try {
  await page.goto(baseUrl);
  await page.getByText('喜欢茉莉花茶', { exact: true }).waitFor();
  await page.getByRole('button', { name: '置顶', exact: true }).click();
  await page.getByRole('button', { name: '取消置顶', exact: true }).waitFor();
  await page.getByRole('button', { name: '纠正', exact: true }).click();
  await page.getByLabel('更正后的信息').fill('喜欢绿茶');
  await page.getByRole('button', { name: '保存纠正', exact: true }).click();
  await page.getByText('喜欢绿茶', { exact: true }).waitFor();
  await page.getByRole('button', { name: '人生时间线', exact: true }).click();
  await page.getByText('喜欢绿茶', { exact: true }).waitFor();
  await page.getByRole('button', { name: '未结束的事项', exact: true }).click();
  await page.getByRole('button', { name: '标记完成', exact: true }).click();
  await page.getByText('已完成', { exact: true }).waitFor();
  await page.getByRole('button', { name: '重要人物', exact: true }).click();
  await page.getByLabel('人物姓名', { exact: true }).fill('李云');
  await page.getByLabel('日常称呼，用顿号分隔', { exact: true }).fill('妈妈');
  await page.getByRole('button', { name: '确认新人物', exact: true }).click();
  await page.getByText('李云', { exact: true }).waitFor();
  await page.getByRole('button', { name: '修改人物称呼', exact: true }).click();
  await page.getByLabel('人物姓名', { exact: true }).fill('李芸');
  await page.getByRole('button', { name: '保存人物称呼', exact: true }).click();
  await page.getByText('李芸', { exact: true }).waitFor();
  await page.getByRole('button', { name: '删除人物', exact: true }).click();
  await page.getByRole('button', { name: '确认删除人物', exact: true }).click();
  await page.getByText('人物及相关内容已停止提供。', { exact: true }).waitFor();
  await page.getByRole('button', { name: '待确认推测', exact: true }).click();
  await page.getByRole('button', { name: '填写我的确认', exact: true }).click();
  await page.getByLabel('我希望记住的表述', { exact: true }).fill('我休息时喜欢安静');
  await page.getByRole('button', { name: '确认我的表述', exact: true }).click();
  await page.getByText('已按你的表述保存', { exact: true }).waitFor();
  await page.getByRole('button', { name: '导出与恢复', exact: true }).click();
  await page.getByRole('button', { name: '创建导出文件', exact: true }).click();
  assert.ok((await page.getByRole('link', { name: '下载我的记忆' }).getAttribute('href')).endsWith('/export-fixture/download'));
  await page.getByRole('button', { name: '事实与偏好' }).click();
  await page.getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await page.getByRole('button', { name: '查看最近删除进度' }).click();
  await page.getByText('内容已停止提供；物理文件和备份清理仍在核验。', { exact: true }).waitFor();
  await page.getByText('数字人已就绪', { exact: false }).waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: '开始账号对话' }).click();
  await page.getByText('账号会话已建立，私人语音尚未启用。', { exact: true }).waitFor();
  assert.equal(writes.find(w => w.path === '/api/v2/sessions').body.memory_enabled, false);
  assert.ok(writes.some(w => w.path.endsWith('/session-fixture/end')));
  await page.screenshot({ path: '.runtime/memory-ui-smoke.png', fullPage: true });
  const renderer = await page.evaluate(async url => {
    const { AvatarRenderer } = await import(url);
    const canvas = document.createElement('canvas'); canvas.width = 300; canvas.height = 400; document.body.append(canvas);
    const avatar = await new AvatarRenderer({ canvas, coreUrl: '/avatar-assets/aitutor.min.js', modelUrl: '/avatar-assets/kei_vowels_pro/kei_vowels_pro.model3.json' }).init();
    const events = [], controller = new AbortController();
    await avatar.executeTracked({ type: 'motion', name: '', index: 3 }, controller.signal, state => events.push(state));
    if (!events.includes('started') || !events.includes('completed')) throw new Error('Finite tracked motion did not finish');
    const cancelled = [], stop = new AbortController();
    const task = avatar.executeTracked({ type: 'motion', name: '', index: 3 }, stop.signal, state => cancelled.push(state));
    setTimeout(() => stop.abort(), 300); await task;
    if (!cancelled.includes('cancelled') || cancelled.includes('completed')) throw new Error('Cancellation was misreported');
    avatar.dispose(); canvas.remove(); return { completed: events, cancelled };
  }, `/@fs${process.cwd()}/packages/renderer/src/index.ts`);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ status: 'passed', private_files_blocked: true, ui_transport: 'fixture', actual_live2d_renderer: true, renderer, device_audio_playback: false }));
} finally { await browser.close(); }
