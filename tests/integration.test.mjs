import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {mountMeshes,mountFrame} from '../dist/mount-meshes.js';
assert.deepEqual(Object.keys(mountMeshes).sort(),['base','cheek','cheek-arc','cradle','upright','upright-arc','yoke','yoke-stand']);
for(const [name,mesh] of Object.entries(mountMeshes)){assert.equal(mesh.source_sha256,createHash('sha256').update(fs.readFileSync(`cad/STL/simple-${name}.stl`)).digest('hex'),'Regenerate bundled mount mesh after CAD changes');assert(['world','cradle'].includes(mesh.frame));assert.equal(mesh.toModel.length,3);}
assert.equal(mountFrame.axisZ,94);assert.equal(mountFrame.hubL,75);assert.equal(mountFrame.baseT,14);
for(const f of fs.readdirSync('cad/STL').filter(f=>f.startsWith('simple-')))assert(mountMeshes[f.slice(7,-4)],'Obsolete mount STL '+f);
import {build,defaults,connectionCoupon,clipStrain,hardwareSchedule,binarySTL} from '../dist/geometry.js';
import {bounds} from '../dist/mesh.js';
import {scenePoint,dishPoint,sceneBounds} from '../dist/scene.js';
import {manifest,guide,scadSource} from '../dist/exports.js';
import {mountHardware} from '../dist/mount.js';
fs.mkdirSync('tmp/system-validation',{recursive:true});
for(const cfg of [{mountMode:1},{mountMode:1,mountThrough:0},{mountMode:1,mountBase:0,mountArcLock:1},{mountMode:1,mountArcLock:1,elevation:-10},{mountMode:1,mountBase:0,elevation:100},{mountMode:1,seamJoint:1,elevation:-7.5},{mountMode:1,seamJoint:3,elevation:-7.5,mountArcLock:1},{mountMode:1,seamJoint:2,diameter:600,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4,rows:2,azimuth:90,elevation:20},{mountMode:1,feedMode:2,frequencyGHz:24,elevation:90}]){
 const m=build(cfg),base=cfg.mountBase??1,arc=cfg.mountArcLock??0,ids=m.parts.filter(p=>p.kind==='mount').map(p=>p.id);
 assert.deepEqual(ids,[...(base?['mount-base']:[]),'mount-yoke','mount-upright','mount-cradle','mount-cheek']);assert.equal(m.mount.base,!!base);assert.equal(m.mount.arcLock,!!arc);
 assert.equal(m.parts.find(p=>p.id==='mount-yoke').spec.variant,base?'yoke':'yoke-stand');assert.equal(m.parts.find(p=>p.id==='mount-cheek').spec.variant,arc?'cheek-arc':'cheek');
 const hw=mountHardware(m),q=item=>hw.find(x=>x.item===item)?.quantity??0;assert.equal(q('mount heat-set insert'),7);assert.equal(q('upright joint screw'),4);assert.equal(q('cheek joint screw'),3);assert.equal(q('arc lock bolt'),arc);assert.equal(q('azimuth nut'),base);assert.equal(q('stand fixing screw'),4);
 const lowest=Math.min(...m.instances.filter(i=>i.part.kind==='mount').flatMap(i=>i.part.mesh.v.map(v=>scenePoint(m,i,v)[2])));assert(Math.abs(lowest-(base?0:14))<1e-3,'lowest mount face');
 // the upright foot sits on the yoke top (z 24) and the clamp faces meet at x = 40 for every option
 const at=(id,k,fn)=>fn(...m.instances.filter(i=>i.part.id===id).flatMap(i=>i.part.mesh.v.map(v=>scenePoint(m,i,v)[k])));if(!m.p.azimuth){assert(Math.abs(at('mount-upright',2,Math.min)-24)<1e-3);assert(Math.abs(at('mount-upright',0,Math.min)-40)<1e-3);assert(Math.abs(at('mount-cheek',0,Math.max)-40)<1e-3);}assert(m.mount.maxOverlap<.1);assert(m.mount.sweepStandClearance+1e-5>=m.mount.standClearance);assert(guide(m).includes('Aiming mount'));assert.equal(manifest(m).mount.elevation,m.p.elevation);assert(mountHardware(m).length>0);
 if(!m.p.mountThrough){const washer=manifest(m).hardware.find(x=>x.item==='external mount washer');assert.equal(washer.quantity,4);assert.equal(washer.spec,'M4 / 1 mm');}
 const assembled=sceneBounds(m),exploded=sceneBounds(m,1);assert(exploded.size.some((x,k)=>x>assembled.size[k]+1),'Exploded camera bounds include separated geometry');
 const count=m.parts.reduce((s,p)=>s+p.qty,0);assert.equal(m.plates.flatMap(p=>p.placements).length,count);
 for(const part of m.parts){assert(bounds(part.output).min[2]>-.00001);if(part.kind==='mount')fs.writeFileSync('tmp/system-validation/'+part.id+'.stl',Buffer.from(binarySTL(part.output)));}
 if(m.p.seamJoint){const p=m.parts.find(p=>p.kind==='clip');assert.equal(m.instances.filter(i=>i.part===p).length,p.installed);assert.equal(p.qty,p.installed+p.spares);assert(Math.abs(p.dim[2]-10)<.001);fs.writeFileSync('tmp/system-validation/clip.stl',Buffer.from(binarySTL(p.output)));}
 const snap=scadSource(m);assert(snap.includes('multmatrix'));assert(snap.includes('mount-cradle'));assert(snap.includes('mount-cheek'));assert.equal(manifest(m).hardware.some(x=>x.item==='azimuth clamp screw'),!!base);assert.equal(manifest(m).mount.base,!!base);
 console.log('PASS integrated mount, clips, source transforms, hardware and plates',cfg);
}
for(const fit of [0,.05,.1]){const c=connectionCoupon(build({seamJoint:1,clipFit:fit}));assert.equal(new Set(c.map(x=>x.id)).size,c.length);assert(c.filter(x=>x.id.startsWith('seam-clip')).every(x=>Math.abs(bounds(x.output).size[2]-10)<.001));}
assert.throws(()=>build({seamJoint:1,clipDetent:2}),/strain/);assert.throws(()=>build({mountMode:1,seamJoint:1,elevation:-10}),/elevation/);
assert(100*clipStrain(defaults)<defaults.clipAllowableStrain);
console.log('PASS strain gate, unique coupons and clip orientation');
