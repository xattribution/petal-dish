import fs from 'node:fs';
const read=name=>fs.readFileSync('dist/'+name,'utf8');
const code=['geometry.js','viewer.js','exports.js','app.js'].map(name=>read(name).replace(/^import .*?;\n/gm,'').replace(/^export /gm,'')).join('\n');
const kernel=JSON.stringify(read('kernel.scad')).replace(/</g,'\\u003c');
const html=read('index.html').replace('<link rel="stylesheet" href="style.css">',()=>`<style>${read('style.css')}</style>`).replace('<script type="module" src="app.js"></script>',()=>`<script>window.PETAL_SCAD=${kernel};\n${code.replace(/<\/script/gi,'<\\/script')}\n</script>`).replace('href="./" aria-label="PETAL home"','href="#" aria-label="PETAL home"').replace(/<a class="button quiet" href="petal-offline.html" download>.*?<\/a>/,'<span class="button quiet">Offline edition</span>');
fs.writeFileSync('dist/petal-offline.html',html);
console.log('Standalone HTML:',Buffer.byteLength(html),'bytes');
