// README screenshot of the real app (docs/snapshots/app.png). Serves dist/ locally and drives it in headless
// Chromium with software WebGL. Needs Playwright: `npm i --no-save playwright && npx playwright install chromium`.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
let chromium;
try{({chromium}=await import('playwright'));}catch{console.error('Install Playwright first: npm i --no-save playwright && npx playwright install chromium');process.exit(1);}
const types={'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json'};
const server=http.createServer((req,res)=>{const file=path.join('dist',decodeURIComponent(new URL(req.url,'http://x').pathname).replace(/^\/$/,'/index.html'));if(!file.startsWith('dist')||!fs.existsSync(file)){res.writeHead(404);res.end();return;}res.writeHead(200,{'Content-Type':types[path.extname(file)]||'application/octet-stream'});fs.createReadStream(file).pipe(res);}).listen(0);
const port=server.address().port,browser=await chromium.launch({args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist']});
const page=await browser.newPage({viewport:{width:1440,height:900},deviceScaleFactor:1});
await page.goto(`http://localhost:${port}/index.html`);await page.waitForSelector('body[data-ready=true]',{timeout:120000});
const settle=()=>page.waitForFunction(()=>!document.getElementById('export-all').disabled&&document.getElementById('fit-badge').textContent==='Fits bed',null,{timeout:120000});
// 400 mm dish, snap clips, prime-focus rods and the aiming mount; Connections tab open so the seam cards show.
for(const [id,value] of [['seamJoint',1],['feedMode',1],['mountMode',1]]){await page.evaluate(([id,value])=>{const x=document.getElementById(id);x.value=String(value);x.dispatchEvent(new Event('input',{bubbles:true}));},[id,value]);await page.waitForTimeout(400);await settle();}
await page.click('[data-category=Connections]');
await page.evaluate(()=>{const c=document.getElementById('canvas');c.dispatchEvent(new KeyboardEvent('keydown',{key:'+'}));});
await page.waitForTimeout(800);
fs.mkdirSync('docs/snapshots',{recursive:true});await page.screenshot({path:'docs/snapshots/app.png'});
// Print plates (docs/snapshots/plates.png): the default dish on 220 mm beds, its petals nested, Print tab open.
await page.click('#reset');await page.waitForTimeout(400);await settle();
await page.click('[data-category=Print]');await page.click('[data-view=layout]');await page.waitForTimeout(1200);
await page.screenshot({path:'docs/snapshots/plates.png'});
await browser.close();server.close();console.log('rendered docs/snapshots/app.png and plates.png');
