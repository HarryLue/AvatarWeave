import { chromium } from '@playwright/test';
const browser = await chromium.launch({ channel: 'chromium', headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: {width: 1440, height: 1080} });
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if(m.type() === 'error') console.log('console error', m.text()); });
try {
  await page.goto('http://127.0.0.1:5174');
  await page.getByText('数字人已就绪', {exact:false}).waitFor({ timeout: 60000 });
  await page.screenshot({ path: 'docs/demo.png', fullPage: true });
  await page.getByRole('button', {name: /开始对话/}).click();
  await page.getByText('正在聆听', {exact:true}).waitFor({ timeout: 45000 });
  await page.getByRole('textbox', {name: '消息'}).fill('你好，请做一个动作');
  await page.getByRole('button', {name:'发送', exact:true}).click();
  await page.getByText(/这是本地演示模式，已收到你的消息/).waitFor({ timeout: 15000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'docs/demo-connected.png', fullPage:true });
  await page.getByRole('button', {name:/结束对话/}).click();
  await page.getByRole('button', {name:/开始对话/}).waitFor();
  // Reconnect verifies that old room listeners and the Live2D instance remain usable.
  await page.getByRole('button', {name:/开始对话/}).click();
  await page.getByText('正在聆听', {exact:true}).waitFor({timeout:45000});
  await page.getByRole('button', {name:/结束对话/}).click();
  await page.evaluate(async moduleUrl => {
    const { AvatarRenderer } = await import(moduleUrl);
    const container = document.createElement('div');
    container.style.cssText = 'width:300px;height:300px';
    const canvas = document.createElement('canvas');
    container.appendChild(canvas); document.body.appendChild(container);
    const avatar = await new AvatarRenderer({canvas, coreUrl:'/avatar-assets/aitutor.min.js', modelUrl:'/avatar-assets/kei_vowels_pro/kei_vowels_pro.model3.json', motionSyncUrl:'/avatar-assets/kei_vowels_pro/kei_vowels_pro.motionsync3.json', onError: e => {throw e;}}).init();
    const context = new AudioContext();
    const oscillator = context.createOscillator();
    const destination = context.createMediaStreamDestination();
    oscillator.connect(destination); oscillator.start();
    await context.resume();
    let frames = 0;
    const onMessage = avatar.sync.onMessage;
    avatar.sync.onMessage = function(event) { frames++; onMessage.call(this, event); };
    avatar.setAudio(destination.stream);
    await new Promise(resolve => setTimeout(resolve, 1000));
    avatar.setAudio(null);
    avatar.setAudio(destination.stream);
    await new Promise(resolve => setTimeout(resolve, 300));
    avatar.dispose();
    oscillator.stop(); await context.close();
    await new Promise(resolve => setTimeout(resolve, 150));
    container.remove();
    if (frames === 0) throw new Error('MotionSync did not consume audio worklet frames');
  }, `/@fs${process.cwd()}/packages/avatar-assets/dist/index.js`);
  if (errors.length) throw new Error(errors.join('\n'));
  console.log('PASS: Live2D render, LiveKit connect, streamed reply, action, disconnect, reconnect, synthetic audio MotionSync frames and teardown; no page errors');
} catch(e) {
  console.log(await page.locator('body').innerText());
  console.log(errors);
  await page.screenshot({ path:'docs/demo-failure.png', fullPage:true });
  throw e;
} finally { await browser.close(); }
