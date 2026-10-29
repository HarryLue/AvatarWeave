// node --env-file=.env scripts/browser-asr-smoke.mjs /absolute/path/input-with-trailing-silence.wav
import { chromium } from '@playwright/test';
import { resolve } from 'node:path';
import assert from 'node:assert/strict';
if (!process.argv[2]) throw new Error('Pass a WAV microphone fixture containing speech followed by at least 10 seconds of silence');
const browser = await chromium.launch({ channel: 'chromium', headless:true, args:[
  '--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
  '--autoplay-policy=no-user-gesture-required', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
  `--use-file-for-fake-audio-capture=${resolve(process.argv[2])}`,
] });
const page = await browser.newPage({permissions:['microphone'], viewport:{width:1440,height:1080}});
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:5174');
  await page.getByText('数字人已就绪',{exact:false}).waitFor({timeout:60000});
  if(process.env.API_ACCESS_KEY) await page.locator('.connection-settings input').fill(process.env.API_ACCESS_KEY);
  await page.getByRole('button',{name:/开始对话/}).click();
  await page.locator('.message.agent').first().waitFor({timeout:60000});
  await page.getByText('正在聆听',{exact:true}).waitFor({timeout:60000});
  await page.evaluate(()=>{
    window.__asrFrames=[];
    const record=()=>{
      const frame=Array.from(document.querySelectorAll('.message.user')).map(node=>({text:node.lastElementChild.textContent.replace('▍',''),pending:!!node.querySelector('.cursor')}));
      if(frame.length && JSON.stringify(frame)!==JSON.stringify(window.__asrFrames.at(-1))) window.__asrFrames.push(frame);
    };
    window.__asrObserver=new MutationObserver(record);
    window.__asrObserver.observe(document.querySelector('.messages'),{childList:true,subtree:true,characterData:true});
  });
  await page.getByRole('button',{name:'开启麦克风',exact:true}).click();
  await page.waitForFunction(()=>{
    const users=document.querySelectorAll('.message.user');
    return users.length && Array.from(users).some(node=>!node.querySelector('.cursor') && node.lastElementChild.textContent.length>4);
  },undefined,{timeout:60000});
  await page.getByRole('button',{name:'关闭麦克风',exact:true}).click();
  await page.waitForTimeout(1200);
  const frames=await page.evaluate(()=>window.__asrFrames);
  const count=await page.locator('.message.user').count();
  assert.equal(count,1,`one spoken utterance must create one bubble: ${JSON.stringify(frames)}`);
  assert.ok(frames.some(frame=>frame.some(message=>message.pending)),`no interim ASR state observed: ${JSON.stringify(frames)}`);
  assert.equal(frames.at(-1)[0].pending,false,'final transcript must close the same bubble');
  assert.deepEqual(errors,[]);
  await page.screenshot({path:'docs/asr-streaming.png',fullPage:true});
  await page.getByRole('button',{name:/结束对话/}).click();
  console.log(JSON.stringify({status:'passed',user_bubbles:count,distinct_transcript_updates:frames.length,interim_visible:true,final_visible:true}));
} catch(error) {
  await page.screenshot({path:'docs/asr-streaming-failure.png',fullPage:true});
  console.log(await page.locator('.error').allTextContents());
  throw error;
} finally {await browser.close();}
