import assert from 'node:assert/strict';
import {build,defaults,hardwareSchedule,structuralDepth,JOINT,zAt,volume} from '../dist/geometry.js';
import {guideSections,guide,reflectorGuide,hardwareCSV,inspectionCSV,manifest,kit} from '../dist/exports.js';
for(const cfg of [{},{diameter:800},{fd:.25,diameter:180},{diameter:600,rows:3,staggerRings:0}]){
 const m=build({...defaults,...cfg}),p=m.p;
 for(const part of m.parts.filter(p=>p.kind==='panel')){
  const s=part.spec;
  for(const seat of s.seats){const r=(seat.r0+seat.r1)/2,a=(seat.a0+seat.a1)/2,x=r*Math.cos(a),y=r*Math.sin(a);assert.equal(structuralDepth(s,p,x,y,s.seats),0,'Ribs do not invade mating datum');}
  for(const h of s.holes){if(h.r<66)continue;const x=h.r*Math.cos(h.a),y=h.r*Math.sin(h.a);assert.equal(structuralDepth(s,p,x,y,s.seats),0,'Ribs clear bolt access');}
 }
 const schedule=hardwareSchedule(m);assert.equal(schedule.reduce((n,r)=>n+r.quantity,0),m.bolts+4);
 assert(schedule.filter(r=>r.role==='mount').every(r=>r.length_mm===null));
 const text=guide(m);assert(text.includes('entire ring'));assert(text.includes('Do not use generic steel-joint'));
 assert.equal(manifest(m).interface.revision,9);
 console.log('PASS structural keep-outs, quantities and instructions',cfg);
}
for(const mode of [0,1,2])for(const cfg of [{},{diameter:800},{diameter:180,fd:.25},{diameter:600,fd:.25,rows:3},{insertDepth:12,insertDiameter:6.5}]){
 const m=build({...defaults,...cfg,fastenerStyle:mode}),rows=hardwareSchedule(m);
 for(const r of rows.filter(r=>r.role!=='mount')){
  assert(r.min_length_mm>0);
  assert(r.max_length_mm===null||r.min_length_mm<=r.max_length_mm,'Nonempty assembly length window');
  if(r.length_mm){assert(r.length_mm>=r.min_length_mm-1e-8);assert(r.max_length_mm===null||r.length_mm<=r.max_length_mm+1e-8);}
  if(mode===2){assert(r.insert_top_below_mouth_mm>=0,'Insert sits inside the cavity');assert(r.engagement_mm===null||r.engagement_mm>=4.4-1e-8);}
 }
 if(!Object.keys(cfg).length)assert(rows.filter(r=>r.role!=='mount').every(r=>r.length_mm!==null),'Default kit uses listed stock screws');
 const body=guideSections(m).find(s=>s.title==='Hardware for this mode').body;
 assert(body.includes(mode===2?'blind inserts':mode===1?'FRONT hex':'FRONT into rear'));
 console.log('PASS per-position screw length and mode-specific guide',mode,cfg);
}
const a=build(defaults),b=build({...defaults,structuralRibs:0,sectors:a.layout.n,rows:a.layout.rows});
assert.equal(a.layout.choices[0].angle,45);assert(a.parts.find(p=>p.kind==='panel').supportMeshes.length>0);
assert(volume(a.parts[0].mesh)>volume(b.parts[0].mesh),'Ribbed design makes its mass tradeoff explicit');
const nut=build({...defaults,nutClearance:.4});assert.deepEqual(nut.parts[0].spec.seats,a.parts[0].spec.seats,'Nut fit must not alter seat clearances');
assert.equal(nut.parts.find(p=>p.kind==='bridge').spec.nuts[0].af,7.8);
assert(reflectorGuide(a).includes('conductive'));assert(hardwareCSV(a).includes('MEASURE ADAPTER'));assert.equal(inspectionCSV(a).trim().split('\n').length,22);
const zipText=new TextDecoder().decode(await kit(a,'').arrayBuffer());
for(const file of ['REFLECTOR.md','HARDWARE.csv','INSPECTION.csv','ASSEMBLY.md','petal-snapshot.scad'])assert(zipText.includes(file));
console.log('PASS reinforced defaults, independent nut fit and engineering kit files');
