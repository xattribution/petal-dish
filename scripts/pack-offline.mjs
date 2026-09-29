import fs from 'node:fs';
import {build} from 'esbuild';
const wasm=fs.readFileSync('node_modules/manifold-3d/manifold.wasm').toString('base64');
const result=await build({entryPoints:['dist/app.js'],bundle:true,format:'esm',platform:'browser',external:['node:module'],write:false,minify:true,define:{'import.meta.url':'"https://petal.invalid/app.js"'}});
const notice=fs.readFileSync('THIRD-PARTY-NOTICES.md','utf8').replaceAll('*/','* /');
const code=`/* ${notice} */\n globalThis.PETAL_WASM_BASE64="${wasm}";\n(async()=>{${result.outputFiles[0].text}\n})().catch(e=>{const el=document.getElementById('error');el.hidden=false;el.textContent='Could not initialize geometry: '+e.message;console.error(e);});`;
fs.writeFileSync('dist/app.bundle.js',code);
const html=fs.readFileSync('dist/index.html','utf8').replace('<link rel="stylesheet" href="style.css">',()=>`<style>${fs.readFileSync('dist/style.css','utf8')}</style>`).replace('<script src="app.bundle.js"></script>',()=>`<script>${code.replace(/<\/script/gi,'<\\/script')}</script>`).replace('href="./" aria-label="PETAL home"','href="#" aria-label="PETAL home"').replace(/<a class="button quiet" href="petal-offline.html" download>.*?<\/a>/,'<span class="button quiet">Offline edition</span>');
fs.writeFileSync('dist/petal-offline.html',html);console.log('Offline app:',Buffer.byteLength(html),'bytes; embedded solid kernel, no network required.');
