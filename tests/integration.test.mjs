import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {mountMeshes,mountFrame} from '../dist/mount-meshes.js';
assert.deepEqual(Object.keys(mountMeshes).sort(),['base','base-legs','cheek','cheek-arc','cradle','upright','upright-arc','yoke','yoke-stand']);
for(const [name,mesh] of Object.entries(mountMeshes)){assert.equal(mesh.source_sha256,createHash('sha256').update(fs.readFileSync(`cad/STL/simple-${name}.stl`)).digest('hex'),'Regenerate bundled mount mesh after CAD changes');assert(['world','cradle'].includes(mesh.frame));assert.equal(mesh.toModel.length,3);}
assert.equal(mountFrame.insert.depth,10);assert.equal(mountFrame.insert.recess,.5);assert.equal(mountFrame.axisZ,94);assert.equal(mountFrame.hubL,75);assert.equal(mountFrame.baseT,14);
for(const f of fs.readdirSync('cad/STL').filter(f=>f.startsWith('simple-')))assert(mountMeshes[f.slice(7,-4)],'Obsolete mount STL '+f);
import {build,defaults,connectionCoupon,clipStrain,hardwareSchedule,binarySTL} from '../dist/geometry.js';
import {bounds} from '../dist/mesh.js';
import {scenePoint,dishPoint,sceneBounds} from '../dist/scene.js';
import {manifest,guide,scadSource} from '../dist/exports.js';
import {mountHardware} from '../dist/mount.js';
fs.mkdirSync('tmp/system-validation',{recursive:true});
for(const cfg of [{mountMode:1,rootThrough:1},{mountMode:1,mountThrough:0},{mountMode:1,mountBase:0,mountArcLock:1},{mountMode:1,mountArcLock:1,elevation:-10},{mountMode:1,mountBase:0,elevation:100},{mountMode:1,seamJoint:1,elevation:-7.5},{mountMode:1,seamJoint:3,elevation:-7.5,mountArcLock:1},{mountMode:1,seamJoint:2,diameter:600,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4,rows:2,azimuth:90,elevation:20},{mountMode:1,feedMode:2,frequencyGHz:24,elevation:90},{mountMode:1,mountBase:2},{mountMode:1,mountBase:2,legDiameter:25.4,legSplay:30,feedMode:3,elevation:-10,azimuth:60},{mountMode:1,mountBase:2,legDiameter:8,legSplay:10,diameter:600,bedX:300,bedY:300,bedZ:300,seamJoint:1,elevation:10}]){
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
 assert(tri({legSplay:30}).minLegLength<t.minLegLength,'more splay, shorter legs');
 assert(tri({feedMode:3,feedPayload:500}).minLegLength>t.minLegLength,'a heavier dish needs a wider stance');
 assert.equal(tri({elevation:-10}).poseClear,false);assert.equal(tri({elevation:-10,azimuth:60}).poseClear,true);assert(t.blocked&&t.clearFrom>-10);
 assert(guide(build({mountMode:1,mountBase:2})).includes('Leg sockets'));
 console.log('PASS tripod sockets, bolt sizes, leg length and leg clearance');}
console.log('PASS strain gate, unique coupons and clip orientation');
