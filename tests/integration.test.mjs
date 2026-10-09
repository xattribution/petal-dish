import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {mountMeshes,mountFrame} from '../dist/mount-meshes.js';
import {mountSolid,mountMesh} from '../dist/mount-fasteners.js';
import {solid,solidScope,cylinder} from '../dist/solid.js';
// Bundled blanks (no fastener holes) match cad/STL/blank; the app cuts the holes for the selected sizes.
assert.deepEqual(Object.keys(mountMeshes).sort(),['base','cheek','cheek-arc','cradle','cradle-dual','upright','upright-arc','yoke','yoke-dual']);
for(const [name,mesh] of Object.entries(mountMeshes)){assert.equal(mesh.source_sha256,createHash('sha256').update(fs.readFileSync(`cad/STL/blank/simple-${name}.stl`)).digest('hex'),'Regenerate bundled mount mesh after CAD changes');assert(['world','cradle'].includes(mesh.frame));assert.equal(mesh.toModel.length,3);}
assert.equal(mountFrame.insert.depth,10);assert.equal(mountFrame.insert.recess,.5);assert.equal(mountFrame.axisZ,94);assert.equal(mountFrame.hubL,75);assert.equal(mountFrame.baseT,14);
const COMPLETE=['base','cheek','cheek-arc','cradle','upright','upright-arc','yoke','yoke-stand','yoke-dual','yoke-dual-stand','cradle-dual','upright-left','cheek-left'];
assert.deepEqual(fs.readdirSync('cad/STL').filter(f=>f.startsWith('simple-')).sort(),COMPLETE.map(n=>`simple-${n}.stl`).sort(),'complete mount STLs');
for(const f of fs.readdirSync('cad/STL/blank'))assert(mountMeshes[f.slice(7,-4)],'Obsolete blank STL '+f);
// At the default sizes the app's cuts reproduce the complete OpenSCAD parts (float32 STL rounding only).
{const read=f=>{const b=fs.readFileSync(f),n=b.readUInt32LE(80),key=new Map(),v=[],faces=[];for(let i=0;i<n;i++){const t=[];for(let k=0;k<3;k++){const o=84+i*50+12+k*12,q=[b.readFloatLE(o),b.readFloatLE(o+4),b.readFloatLE(o+8)],s=q.join();if(!key.has(s)){key.set(s,v.length);v.push(q);}t.push(key.get(s));}faces.push(t);}return{v,f:faces};};
 solidScope(()=>{for(const name of COMPLETE){const body=name.endsWith('-left')?solid(mountMesh(name,defaults)):mountSolid(name,defaults).body,ref=solid(read(`cad/STL/simple-${name}.stl`)),diff=body.subtract(ref).raw.volume()+ref.subtract(body).raw.volume();
  assert(diff<.5,`${name}: app cuts differ from the complete STL by ${diff.toFixed(3)} mm³`);assert.equal(body.raw.genus(),ref.raw.genus(),name+' hole count');}});
 console.log('PASS default-size mount cuts reproduce cad/STL/simple-*.stl');}
// Other sizes: the hole diameters follow the selection.
{const probe=(variant,cfg,a,b,r)=>solidScope(()=>{const {body,src}=mountSolid(variant,{...defaults,...cfg}),M=src.toModel,R=[0,1,2].map(i=>M[i].slice(0,3)),inv=q=>{const w=q.map((x,i)=>x-M[i][3]);return[0,1,2].map(j=>R[0][j]*w[0]+R[1][j]*w[1]+R[2][j]*w[2]);};return body.intersect(cylinder(inv(a),inv(b),r,48)).raw.volume();});
 for(const [m,clear] of [[6,6.6],[8,8.5],[10,10.5]]){assert(probe('upright',{clampBolt:m},[39,0,94],[61,0,94],clear/2-.05)<.01,'M'+m+' elevation hole');assert(probe('upright',{clampBolt:m},[39,0,94],[61,0,94],clear/2+.3)>1,'M'+m+' elevation hole is not oversize');}
 for(const [m,clear] of [[3,3.4],[4,4.5],[5,5.5]]){assert(probe('cradle',{mountBolt:m},[21.2132,62,21.2132],[21.2132,76,21.2132],clear/2-.05)<.01,'M'+m+' hub hole');assert(probe('cradle',{mountBolt:m},[21.2132,62,21.2132],[21.2132,76,21.2132],clear/2+.3)>1,'M'+m+' hub hole is not oversize');
  assert(probe('yoke',{jointBolt:m},[48,32,15],[48,32,23],clear/2-.05)<.01,'M'+m+' joint hole');}
 for(const [m,clear] of [[4,4.5],[6,6.6]])assert(probe('base',{standBolt:m},[31.11,31.11,-1],[31.11,31.11,8],clear/2-.05)<.01,'M'+m+' stand hole');
 // Plain holes instead of the nut pocket, the cheek's head pockets and the stand counterbores.
 const solidAround=(variant,cfg,a,b,r)=>probe(variant,cfg,a,b,r);
 assert(solidAround('base',{},[0,0,1],[0,0,5],6)<20&&solidAround('base',{nutSeat:1},[0,0,1],[0,0,5],6)>200,'azimuth nut pocket or plain hole');
 assert(solidAround('cheek',{},[26.5,0,0],[30,0,0],6)<20&&solidAround('cheek',{headSeat:1},[26.5,0,0],[30,0,0],6)>150,'elevation head pocket or plain hole');
 {const y=28*Math.cos(-20*Math.PI/180),z=28*Math.sin(-20*Math.PI/180);assert(solidAround('cheek-arc',{},[26.5,y,z],[29,y,z],4.5)<20&&solidAround('cheek-arc',{headSeat:1},[26.5,y,z],[29,y,z],4.5)>60,'arc lock head pocket or plain hole');}
 assert(solidAround('yoke-stand',{},[20,45,22.5],[20,45,23.9],5)<20&&solidAround('yoke-stand',{standSeat:1},[20,45,22.5],[20,45,23.9],5)>60,'stand counterbore or plain hole');
 console.log('PASS mount holes follow the selected bolt sizes and seats');}
import {build,defaults,connectionCoupon,clipStrain,hardwareSchedule,binarySTL} from '../dist/geometry.js';
import {bounds} from '../dist/mesh.js';
import {scenePoint,dishPoint,sceneBounds} from '../dist/scene.js';
import {manifest,guide,scadSource} from '../dist/exports.js';
import {mountHardware} from '../dist/mount.js';
fs.mkdirSync('tmp/system-validation',{recursive:true});
for(const cfg of [{mountMode:1,rootThrough:1},{mountMode:1,headSeat:1,nutSeat:1,mountArcLock:1,rootThrough:1,rootSeat:1,mountSeat:1},{mountMode:1,mountBase:0,standSeat:1,headSeat:1},{mountMode:1,mountBase:2,nutSeat:1},{mountMode:1,mountThrough:0},{mountMode:1,mountBase:0,mountArcLock:1},{mountMode:1,mountArcLock:1,elevation:-10},{mountMode:1,mountBase:0,elevation:100},{mountMode:1,seamJoint:1,elevation:-7.5},{mountMode:1,seamJoint:3,elevation:-7.5,mountArcLock:1},{mountMode:1,seamJoint:2,diameter:600,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4,rows:2,azimuth:90,elevation:20},{mountMode:1,feedMode:2,frequencyGHz:24,elevation:90},{mountMode:1,mountBase:2},{mountMode:1,mountBase:2,legDiameter:25.4,legSplay:30,feedMode:3,elevation:-10,azimuth:60},{mountMode:1,mountBase:2,legDiameter:8,legSplay:10,diameter:600,bedX:300,bedY:300,bedZ:300,seamJoint:1,elevation:10}]){
 const m=build(cfg),base=cfg.mountBase??1,arc=cfg.mountArcLock??0,ids=m.parts.filter(p=>p.kind==='mount').map(p=>p.id);
 assert.deepEqual(ids,[...(base?['mount-base']:[]),'mount-yoke','mount-upright','mount-cradle','mount-cheek']);assert.equal(m.mount.base,!!base);assert.equal(m.mount.arcLock,!!arc);
 assert.equal(m.parts.find(p=>p.id==='mount-yoke').spec.variant,base?'yoke':'yoke-stand');assert.equal(m.parts.find(p=>p.id==='mount-cheek').spec.variant,arc?'cheek-arc':'cheek');
 const hw=mountHardware(m),q=item=>hw.find(x=>x.item===item)?.quantity??0;assert.equal(q('mount heat-set insert'),7);assert.equal(q('upright joint screw'),4);assert.equal(q('cheek joint screw'),3);assert.equal(q('arc lock bolt'),arc);assert.equal(q('azimuth nut'),base?1:0);assert.equal(q('stand fixing screw'),base===2?0:4);assert.equal(q('tripod leg'),base===2?3:0);assert.equal(q('leg cross bolt'),base===2?3:0);
 if(base)assert.equal(m.parts.find(p=>p.id==='mount-base').spec.variant,base===2?'base-legs':'base');
 const lowest=Math.min(...m.instances.filter(i=>i.part.kind==='mount').flatMap(i=>i.part.mesh.v.map(v=>scenePoint(m,i,v)[2])));if(base===2)assert(lowest<-30,'leg sockets hang below the base');else assert(Math.abs(lowest-(base?0:14))<1e-3,'lowest mount face');
 // the upright's 2 mm tenon sits in the yoke plate's pocket (plate top z 24, so its lowest point is z 22) and the clamp faces meet at x = 40 for every option
 const at=(id,k,fn)=>fn(...m.instances.filter(i=>i.part.id===id).flatMap(i=>i.part.mesh.v.map(v=>scenePoint(m,i,v)[k])));if(!m.p.azimuth){assert(Math.abs(at('mount-upright',2,Math.min)-22)<1e-3);assert(Math.abs(at('mount-upright',0,Math.min)-40)<1e-3);assert(Math.abs(at('mount-cheek',0,Math.max)-40)<1e-3);}assert(m.mount.maxOverlap<.1);assert(m.mount.sweepStandClearance+1e-5>=m.mount.standClearance);assert(guide(m).includes('Aiming mount'));assert.equal(manifest(m).mount.elevation,m.p.elevation);assert(mountHardware(m).length>0);if(m.p.rootThrough&&m.p.mountThrough)assert(!guide(m).includes('No heat-set inserts are needed.'));
 if(!m.p.mountThrough){const washer=manifest(m).hardware.find(x=>x.item==='external mount washer');assert.equal(washer.quantity,4);assert.equal(washer.spec,'M4 / 1 mm');}
 const assembled=sceneBounds(m),exploded=sceneBounds(m,1);assert(exploded.size.some((x,k)=>x>assembled.size[k]+1),'Exploded camera bounds include separated geometry');
 const count=m.parts.reduce((s,p)=>s+p.qty,0);assert.equal(m.plates.flatMap(p=>p.placements).length,count);
 for(const part of m.parts){assert(bounds(part.output).min[2]>-.00001);if(part.kind==='mount')fs.writeFileSync('tmp/system-validation/'+part.id+'.stl',Buffer.from(binarySTL(part.output)));}
 if(m.p.seamJoint===3)for(const p of m.parts.filter(p=>p.kind==='lever')){assert.equal(m.instances.filter(i=>i.part===p).length,p.installed);assert.equal(p.qty,p.installed+p.spares);}
 if(m.p.seamJoint===1||m.p.seamJoint===2){const p=m.parts.find(p=>p.kind==='clip');assert.equal(m.instances.filter(i=>i.part===p).length,p.installed);assert.equal(p.qty,p.installed+p.spares);assert(Math.abs(p.dim[2]-10)<.001);fs.writeFileSync('tmp/system-validation/clip.stl',Buffer.from(binarySTL(p.output)));}
 const snap=scadSource(m);assert(snap.includes('multmatrix'));assert(snap.includes('mount-cradle'));assert(snap.includes('mount-cheek'));assert.equal(manifest(m).hardware.some(x=>x.item==='azimuth clamp screw'),!!base);assert.equal(manifest(m).mount.base,!!base);
 console.log('PASS integrated mount, clips, source transforms, hardware and plates',cfg);
}
for(const fit of [0,.05,.1]){const c=connectionCoupon(build({seamJoint:1,clipFit:fit}));assert.equal(new Set(c.map(x=>x.id)).size,c.length);assert(c.filter(x=>x.id.startsWith('seam-clip')).every(x=>Math.abs(bounds(x.output).size[2]-10)<.001));}
assert.throws(()=>build({seamJoint:1,clipDetent:2}),/strain/);assert.throws(()=>build({mountMode:1,seamJoint:1,elevation:-10}),/elevation/);
assert(100*clipStrain(defaults)<defaults.clipAllowableStrain);
// Tripod: sockets fit the base for every leg size, bolts scale with the leg, and the leg length answers the dish.
{const tri=cfg=>build({mountMode:1,mountBase:2,...cfg}).mount.tripod,t=tri({});
 assert.equal(t.legs.length,3);assert.deepEqual(t.legs.map(l=>Math.round(l.azimuth)),[90,210,330]);
 for(const l of t.legs){assert(Math.abs(Math.hypot(...l.axis)-1)<1e-9);assert(Math.abs(Math.acos(-l.axis[2])*180/Math.PI-20)<1e-9);assert(l.boreEnd[2]<-.99);}
 assert(t.innerRadius>=11&&t.rootRadius>0);assert.equal(t.engagement,50);assert.equal(t.bolt.size,5);
 assert.equal(tri({legDiameter:8}).bolt.size,3);assert.equal(tri({legDiameter:16}).bolt.size,4);assert.equal(tri({legDiameter:25.4,legSplay:30}).bolt.size,5);
 assert.equal(tri({legBolt:6}).bolt.size,6);assert.equal(tri({legBolt:6}).bolt.hole,6.6);assert.throws(()=>tri({legDiameter:10,legBolt:6}),/leg bolts need legs/);
 assert(tri({legSplay:30}).minLegLength<t.minLegLength,'more splay, shorter legs');
 assert(tri({feedMode:3,feedPayload:500}).minLegLength>t.minLegLength,'a heavier dish needs a wider stance');
 assert.equal(tri({elevation:-10}).poseClear,false);assert.equal(tri({elevation:-10,azimuth:60}).poseClear,true);assert(t.blocked&&t.clearFrom>-10);
 assert(guide(build({mountMode:1,mountBase:2})).includes('Leg sockets'));
 console.log('PASS tripod sockets, bolt sizes, leg length and leg clearance');}
console.log('PASS strain gate, unique coupons and clip orientation');
// Hub-to-mount through bolts: grip from the hub rear to the nut in the front seat, or on top of a collector mast foot.
{const {mountScrew,mountGrip,hubMountGrip,rootBottom}=await import('../dist/geometry.js');
 const plain=build({}),collector=build({feedMode:3}),t=collector.feed.mast,row=m=>hardwareSchedule(m).find(r=>r.item==='external mount screw');
 assert.equal(mountGrip(plain),hubMountGrip(plain.p,plain.layout.n));assert.equal(mountScrew(plain),Math.ceil(mountGrip(plain)+2*.8+3.2+1.5),'M4: two 0.8 mm washers and a 3.2 mm nut');
 assert(Math.abs(mountGrip(collector)-(t.footZ+t.flange-(rootBottom(collector.p)-6)))<1e-9,'collector: hub rear to the top of the foot flange');
 assert(mountScrew(collector)>=mountScrew(plain)+t.flange,'the foot flange adds to the grip');
 assert(row(collector).note.includes('collector mast foot')&&row(collector).spec.includes(`× ${Math.ceil((mountScrew(collector)+12)/5)*5} `));
 assert(guide(collector).includes(`on top of the ${t.flange} mm collector mast foot`)&&guide(collector).includes(`as ${mountScrew(collector)} mm plus`));
 assert(guide(build({rootThrough:1,mountThrough:1,feedMode:1})).includes('The petal roots and hub mount need no heat-set inserts (the feed support has its own M3 inserts).'));
 // the arc lock with the largest clamp bolt: every mount part closed
 const arc=build({mountMode:1,mountArcLock:1,clampBolt:10});for(const part of arc.parts.filter(p=>p.kind==='mount')){const es=new Map();for(const f of part.mesh.f)for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],k=Math.min(a,b)+':'+Math.max(a,b);es.set(k,(es.get(k)||0)+1);}assert([...es.values()].every(n=>n===2),part.id+' closed');}
 console.log('PASS hub mount bolt lengths with and without the collector foot, insert wording, M10 clamp with the arc lock');}
// Elevation clamp sides: left mirrors every sided part, both adds a mirrored arm on each side; hardware doubles.
{const {mountHardware}=await import('../dist/mount.js');
 for(const [mountSides,ids] of [[1,['mount-base','mount-yoke','mount-upright','mount-cradle','mount-cheek']],[2,['mount-base','mount-yoke','mount-upright','mount-upright-left','mount-cradle','mount-cheek','mount-cheek-left']]]){
  const m=build({mountMode:1,mountSides,mountArcLock:1}),parts=m.parts.filter(p=>p.kind==='mount');assert.deepEqual(parts.map(p=>p.id),ids);assert(m.mount.maxOverlap<.1);
  const hw=mountHardware(m),q=item=>hw.find(h=>h.item===item).quantity;assert.equal(q('elevation clamp bolt'),mountSides===2?2:1);assert.equal(q('elevation nut'),mountSides===2?2:1);assert.equal(q('mount heat-set insert'),mountSides===2?14:7);assert.equal(q('arc lock bolt'),1);
  if(mountSides===1){const r=build({mountMode:1,mountArcLock:1}).parts.find(p=>p.id==='mount-upright'),l=parts.find(p=>p.id==='mount-upright');solidScope(()=>{const a=solid(r.mesh).transform(([x,y,z])=>[-x,y,z]),b=solid(l.mesh);assert(Math.abs(a.raw.volume()-b.raw.volume())<.01&&a.subtract(b).raw.volume()<.05,'left upright is the mirror image');});}
  assert(guide(m).includes(mountSides===2?'The elevation clamp is on both sides':'The elevation clamp is on the left'));}
 console.log('PASS elevation clamp on the left or on both sides: parts, mirroring, hardware and guide');}
