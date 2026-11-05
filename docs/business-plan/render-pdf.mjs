// Run from repository root. The optional local CJK font is used only for PDF embedding.
import { chromium } from '@playwright/test';
import { readFile, access, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
if (process.argv.includes('--v2')) { await import('./v2/render.mjs'); process.exit(0); }
const root = new URL('./', import.meta.url);
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
try {
  const page = await browser.newPage({viewport:{width:1280,height:1000},deviceScaleFactor:1});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(new URL('business-plan.html',root).href);
  const font=process.env.BP_CJK_FONT || '/tmp/avatarweave-NotoSansCJKsc-Regular.otf';
  await access(font);
  const data=(await readFile(font)).toString('base64');
  await page.addStyleTag({content:`@font-face{font-family:BPChinese;src:url(data:font/otf;base64,${data})}body{font-family:BPChinese,sans-serif}`});
  await page.evaluate(()=>document.fonts.ready);
  assert.equal(await page.locator('h2').count(),15);
  assert.equal(await page.locator('img').evaluateAll(imgs=>imgs.every(i=>i.complete&&i.naturalWidth>0)),true);
  assert.equal(errors.length,0);
  // Chromium footer templates do not inherit the page's embedded CJK font.
  // Use a portable ASCII footer; the main document retains its embedded Chinese font.
  await page.pdf({path:fileURLToPath(new URL('avatarweave-business-plan.pdf',root)),format:'A4',printBackground:true,preferCSSPageSize:true,displayHeaderFooter:true,headerTemplate:'<span></span>',footerTemplate:'<div style="font-family:Arial,sans-serif;font-size:8px;width:100%;padding:0 17mm;color:#60736b;display:flex;justify-content:space-between"><span>AVATARWEAVE | BUSINESS PLAN | 2026.11</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'});
  await page.screenshot({path:fileURLToPath(new URL('assets/document-preview.png',root)),fullPage:false});
  await page.setViewportSize({width:390,height:844});
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
  assert.equal(overflow,false,'Mobile document overflow');
  await writeFile(new URL('validation.json',root),JSON.stringify({sections:15,imagesLoaded:true,javascriptErrors:errors,mobileOverflow:overflow,pdfGenerated:true},null,2)+'\n');
  console.log('PDF, preview and document validation saved.');
} finally {await browser.close();}
