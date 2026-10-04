import assert from 'node:assert/strict';
import fs from 'node:fs';
import {build,defaults} from '../dist/geometry.js';
import {assemblyPDF,kit} from '../dist/exports.js';
import {BUILD_ID} from '../dist/version.js';
fs.mkdirSync('tmp/pdfs',{recursive:true});
const cases=[['default',{mountMode:1,seamJoint:1}],['custom',{diameter:600,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4,frequencyGHz:10,rearStyle:1,mountThrough:1,rootThrough:1,hubFlat:0,rows:2,mountMode:1,elevation:20}],['secondary',{feedMode:2,frequencyGHz:24}],['mixed',{diameter:600,bedX:300,bedY:300,bedZ:300,rows:2,sectors:8,connections:{families:{radial:{seamJoint:2,seamBolt:4},ring:{seamJoint:1}},roots:{0:1},mounts:{1:0}},printSelection:{clip:false}}]];
for(const[name,changes]of cases){if(process.argv[2]&&process.argv[2]!==name)continue;const m=build({...defaults,...changes}),pdf=assemblyPDF(m);assert.equal(new TextDecoder().decode(pdf.slice(0,5)),'%PDF-');fs.writeFileSync(`tmp/pdfs/${name}.pdf`,Buffer.from(pdf));fs.writeFileSync(`tmp/pdfs/${name}.json`,JSON.stringify({diameter:m.p.diameter,parts:m.parts.map(p=>({id:p.id,qty:p.qty,printIncluded:p.printIncluded})),feed:m.feed?{legs:m.p.feedLegs,cut:m.feed.cutLength.toFixed(2)}:null,build:BUILD_ID}));console.log('PASS PDF generation',name,pdf.byteLength);}
if(!process.argv[2]){const archive=Buffer.from(await kit(build(defaults)).arrayBuffer());fs.writeFileSync('tmp/pdfs/kit.zip',archive);
console.log('PASS model export includes PDF fixture for ZIP validation');}
