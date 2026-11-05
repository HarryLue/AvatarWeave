import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
const root=path.dirname(fileURLToPath(import.meta.url));
const req=createRequire('/tmp/avatarweave-bp-v2-tools/package.json');
const {createCanvas,DOMMatrix,Path2D,ImageData,loadImage}=req('@napi-rs/canvas');
Object.assign(globalThis,{DOMMatrix,Path2D,ImageData});
const pdfjs=await import('/tmp/avatarweave-bp-v2-tools/node_modules/pdfjs-dist/legacy/build/pdf.mjs');
const report=[];
for(const [filename,out] of [[path.resolve(root,'../avatarweave-business-plan.pdf'),'v1-footer-fixed'],[path.join(root,'AvatarWeave-Investor-Deck-V2.pdf'),'v2-cover']]){
 const pdf=await pdfjs.getDocument({data:new Uint8Array(await fs.readFile(filename)),disableFontFace:true,useSystemFonts:false}).promise;
 const page=await pdf.getPage(1),viewport=page.getViewport({scale:1.4});const canvas=createCanvas(viewport.width,viewport.height),ctx=canvas.getContext('2d');
 await page.render({canvasContext:ctx,viewport}).promise;await fs.writeFile(path.join(root,'previews',out+'.png'),canvas.toBuffer('image/png'));
 const text=(await page.getTextContent()).items.map(x=>x.str).join(' ');
 report.push({filename,pages:pdf.numPages,firstPageText:text});
 if(out==='v1-footer-fixed'&&!text.includes('BUSINESS PLAN'))throw new Error('V1 footer text missing');
 if(out==='v2-cover'&&pdf.numPages!==25)throw new Error('Unexpected slide count');
 for(let i=1;i<=pdf.numPages;i++){const pg=await pdf.getPage(i),tx=(await pg.getTextContent()).items.map(x=>x.str).join('');if(!tx.trim())throw new Error('Empty PDF page '+i);}
 await pdf.cleanup();
}
const W=1280,H=720,tile=320,gap=14;const sheet=createCanvas(tile*4+gap*5,180*7+gap*8),cx=sheet.getContext('2d');cx.fillStyle='#cbd6cd';cx.fillRect(0,0,sheet.width,sheet.height);
for(let i=0;i<25;i++){const im=await loadImage(path.join(root,'previews/slide-'+String(i+1).padStart(2,'0')+'.png'));cx.drawImage(im,gap+(i%4)*(tile+gap),gap+Math.floor(i/4)*(180+gap),tile,180);}
await fs.writeFile(path.join(root,'previews/contact-sheet.png'),sheet.toBuffer('image/png'));
await fs.writeFile(path.join(root,'pdf-inspection.json'),JSON.stringify(report,null,2));console.log(report.map(x=>({file:path.basename(x.filename),pages:x.pages})));
