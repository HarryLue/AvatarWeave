import {chromium} from '@playwright/test';
import {writeFile,mkdir,readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const root=new URL('./',import.meta.url);
await mkdir(new URL('previews/',root),{recursive:true});
const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
try{
 const page=await browser.newPage({viewport:{width:1400,height:900},deviceScaleFactor:1});
 await page.goto(new URL('deck.html',root).href);await page.evaluate(()=>document.fonts.ready);
 const check=await page.evaluate(()=>{
  const problems=[];
  document.querySelectorAll('.txt').forEach(e=>{const r=document.createRange();r.selectNodeContents(e);const text=r.getBoundingClientRect(),box=e.getBoundingClientRect();if(text.bottom>box.bottom+4||text.right>box.right+4)problems.push({slide:e.closest('.slide').dataset.slide,text:e.innerText,bottom:Math.round(text.bottom-box.bottom),right:Math.round(text.right-box.right)});});
  return {slides:document.querySelectorAll('.slide').length,imagesLoaded:[...document.images].every(i=>i.complete&&i.naturalWidth>0),textOverflow:problems};
 });
 await page.pdf({path:fileURLToPath(new URL('AvatarWeave-Investor-Deck-V2.pdf',root)),width:'13.333333in',height:'7.5in',preferCSSPageSize:true,printBackground:true,displayHeaderFooter:false,margin:{top:0,right:0,bottom:0,left:0}});
 for(let i=0;i<check.slides;i++)await page.locator('.slide').nth(i).screenshot({path:fileURLToPath(new URL('previews/slide-'+String(i+1).padStart(2,'0')+'.png',root))});
 await writeFile(new URL('validation.json',root),JSON.stringify(check,null,2));
 console.log(JSON.stringify(check));
}finally{await browser.close()}
