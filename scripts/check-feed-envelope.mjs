// Export a reproducible size/focal-ratio sweep for independent mesh checks.
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {build,defaults,binarySTL,bounds,volume} from '../dist/geometry.js';
import {feedGeometry,feedDatum} from '../dist/feed.js';
const folder='tmp/feed-envelope';fs.mkdirSync(folder,{recursive:true});
const edgesOnly=process.argv.includes('--edges-only');
const summary=edgesOnly?JSON.parse(fs.readFileSync(`${folder}/summary.json`)).filter(x=>!x.id.startsWith('edge-')):[];
if(!edgesOnly)for(const diameter of [260,400,600,800,1200])for(const fd of [.25,.3,.42,.6,.8])for(const feedMode of [1,2]){
 const id=`${diameter}-${fd}-${feedMode}`,feedLegs=diameter===600||diameter===1200?4:3;
 const cfg={...defaults,diameter,fd,feedMode,feedLegs,rodDiameter:diameter===260?4:diameter===800?8:6.35,bedX:400,bedY:400,bedZ:400,packPlates:0};
 let screen;
 try{screen=feedGeometry(cfg,{n:12,rows:1});}
 catch(e){assert.match(e.message,/Rod geometry is outside|Secondary must be|Returned ray bundle|Rod path crowds/);summary.push({id,diameter,fd,feedMode,status:'rejected',reason:e.message});continue;}
 const m=build(cfg),parts=m.parts.filter(p=>p.kind==='feed'||p.id.endsWith('-mount'));
 for(const p of parts){assert(volume(p.mesh)>0,p.id);assert(Math.abs(bounds(p.output).min[2])<1e-5);if(p.kind==='feed')fs.writeFileSync(`${folder}/${id}-${p.id}.stl`,Buffer.from(binarySTL(p.output)));}
 fs.writeFileSync(`${folder}/${id}.json`,JSON.stringify({parameters:cfg,feed:m.feed,parts:parts.map(p=>({id:p.id,mesh:p.mesh}))}));
 summary.push({id,diameter,fd,feedMode,feedLegs,status:'built',angle:screen.rodAngle,depth:m.depth,cut:screen.cutLength});
 console.log('PASS envelope build',id,'angle',screen.rodAngle.toFixed(2));
}
// Extremes that independently exercise stock clearance, four legs and phase offset.
for(const [index,cfg]of [{feedMode:1,fd:.3,rodDiameter:8,rodClearance:.7,feedLegs:4},{feedMode:1,fd:.8,rodDiameter:4,rodClearance:.15,phaseOffset:100},{feedMode:2,fd:.6,rodDiameter:8,rodClearance:.7,feedLegs:4,frequencyGHz:20},
 {feedMode:1,diameter:260,fd:.8,phaseUnits:1,phaseOffset:-10,frequencyGHz:10,rodDiameter:8,rodClearance:.7},
 {feedMode:1,diameter:1200,fd:.3,rodDiameter:8,feedLegs:4,minimumRise:15.1},
 {feedMode:1,diameter:260,fd:.8,phaseUnits:1,phaseOffset:-10,frequencyGHz:10,rodDiameter:4,rodClearance:.15},
 {feedMode:2,fd:.8,autoSecondary:0,secondaryPosition:.85,rodDiameter:8,rodClearance:.7}].entries()){
 const{minimumRise,...settings}=cfg,parameters={...defaults,...settings,packPlates:0,bedX:400,bedY:400,bedZ:400};
 if(minimumRise)parameters.phaseOffset=parameters.diameter*parameters.fd+8-(feedDatum(parameters).front+4)-minimumRise;
 const m=build(parameters),id=`edge-${index}`;
 for(const part of m.parts.filter(p=>p.kind==='feed'))fs.writeFileSync(`${folder}/${id}-${part.id}.stl`,Buffer.from(binarySTL(part.output)));
 fs.writeFileSync(`${folder}/${id}.json`,JSON.stringify({parameters,feed:m.feed,parts:m.parts.filter(p=>p.kind==='feed'||p.id.endsWith('-mount')).map(p=>({id:p.id,mesh:p.mesh}))}));
 summary.push({id,status:'built',diameter:parameters.diameter,fd:parameters.fd,feedMode:parameters.feedMode,angle:m.feed.rodAngle});
}
fs.writeFileSync(`${folder}/summary.json`,JSON.stringify(summary,null,2));
console.log('PASS',summary.filter(x=>x.status==='built').length,'built;',summary.filter(x=>x.status==='rejected').length,'explicitly rejected configurations');
