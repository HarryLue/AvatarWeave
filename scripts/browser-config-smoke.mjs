import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';

const browser = await chromium.launch({channel:'chromium',headless:true,args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page = await browser.newPage();
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
try {
  const response=await page.request.get('http://127.0.0.1:8090/api/avatars');
  assert.equal(response.status(),200);
  const document=await response.json();
  const original=document.avatars[0];
  const second={...original,id:'another-avatar',name:'配置中的另一个数字人',default_action:'custom',actions:{custom:{label:'配置中的动作',type:'motion',name:'Other',index:7}}};
  await page.route('**/api/avatars',route=>route.fulfill({json:{...document,avatars:[original,second]}}));
  await page.route('**/api/tts/voices',route=>route.fulfill({json:{provider:'external',provider_label:'外部语音',enabled:true,default_voice:'fr-voice',capabilities:{voice_selection:true,preview:false},notice:'配置中的提示',voices:[{id:'fr-voice',name:'法语声音',language:'fr',language_label:'法语',gender:'',description:'来自外部服务'},{id:'ja-voice',name:'日语声音',language:'ja',language_label:'日语',gender:'',description:'另一种语言'}]}}));
  await page.goto('http://127.0.0.1:5174');
  await page.getByText('数字人已就绪',{exact:false}).waitFor({timeout:60000});
  assert.equal(await page.getByLabel('语音音色').locator('optgroup').count(),2);
  assert.equal(await page.getByRole('button',{name:'试听',exact:true}).count(),0);
  assert.match(await page.locator('.voice-picker').innerText(),/外部语音/);
  await page.getByLabel('数字人',{exact:true}).selectOption('another-avatar');
  await page.getByText('数字人已就绪',{exact:false}).waitFor({timeout:60000});
  assert.equal(await page.locator('.stage-footer strong').innerText(),second.name);
  await page.getByTitle('配置中的动作',{exact:true}).waitFor();
  assert.deepEqual(errors,[]);
  console.log('PASS: provider-neutral labels, arbitrary language groups, capability-based controls, configurable avatar assets/name/actions; no page errors');
} finally { await browser.close(); }
