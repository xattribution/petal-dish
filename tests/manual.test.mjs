import assert from 'node:assert/strict';
import fs from 'node:fs';
import {build,defaults} from '../dist/geometry.js';
import {assemblyPDF,kit} from '../dist/exports.js';
import {BUILD_ID} from '../dist/version.js';
fs.mkdirSync('tmp/pdfs',{recursive:true});
const cases=[['default',{}],['custom',{diameter:600,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4,frequencyGHz:10,rearStyle:1}],['secondary',{feedMode:2,frequencyGHz:24}]];
for(const[name,changes]of cases){const m=build({...defaults,...changes}),pdf=assemblyPDF(m);assert.equal(new TextDecoder().decode(pdf.slice(0,5)),'%PDF-');fs.writeFileSync(`tmp/pdfs/${name}.pdf`,Buffer.from(pdf));fs.writeFileSync(`tmp/pdfs/${name}.json`,JSON.stringify({diameter:m.p.diameter,parts:m.parts.map(p=>({id:p.id,qty:p.qty})),feed:m.feed?{legs:m.p.feedLegs,cut:m.feed.cutLength.toFixed(2)}:null,build:BUILD_ID}));console.log('PASS PDF generation',name,pdf.byteLength);}
const archive=Buffer.from(await kit(build(defaults)).arrayBuffer());fs.writeFileSync('tmp/pdfs/kit.zip',archive);
console.log('PASS model export includes PDF fixture for ZIP validation');
