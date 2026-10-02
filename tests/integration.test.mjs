import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {mountMeshes} from '../dist/mount-meshes.js';
for(const [name,mesh] of Object.entries(mountMeshes))assert.equal(mesh.source_sha256,createHash('sha256').update(fs.readFileSync(`cad/STL/simple-${name}.stl`)).digest('hex'),'Regenerate bundled mount mesh after CAD changes');
import {build,defaults,connectionCoupon,clipStrain,hardwareSchedule,binarySTL} from '../dist/geometry.js';
import {bounds} from '../dist/mesh.js';
import {scenePoint,dishPoint,sceneBounds} from '../dist/scene.js';
import {manifest,guide,scadSource} from '../dist/exports.js';
import {mountHardware} from '../dist/mount.js';
fs.mkdirSync('tmp/system-validation',{recursive:true});
for(const cfg of [{mountMode:1},{mountMode:1,mountThrough:0},{mountMode:1,seamJoint:1,elevation:-7.5},{mountMode:1,seamJoint:2,diameter:600,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4,rows:2,azimuth:90,elevation:20},{mountMode:1,feedMode:2,frequencyGHz:24,elevation:90}]){
 const m=build(cfg);assert.equal(m.parts.filter(p=>p.kind==='mount').length,3);assert(m.mount.maxOverlap<.1);assert(m.mount.sweepStandClearance+1e-5>=m.mount.standClearance);assert(guide(m).includes('Aiming mount'));assert.equal(manifest(m).mount.elevation,m.p.elevation);assert(mountHardware(m).length>0);
 if(!m.p.mountThrough){const washer=manifest(m).hardware.find(x=>x.item==='external mount washer');assert.equal(washer.quantity,4);assert.equal(washer.spec,'M4 / 1 mm');}
 const assembled=sceneBounds(m),exploded=sceneBounds(m,1);assert(exploded.size.some((x,k)=>x>assembled.size[k]+1),'Exploded camera bounds include separated geometry');
 const count=m.parts.reduce((s,p)=>s+p.qty,0);assert.equal(m.plates.flatMap(p=>p.placements).length,count);
 for(const part of m.parts){assert(bounds(part.output).min[2]>-.00001);if(part.kind==='mount')fs.writeFileSync('tmp/system-validation/'+part.id+'.stl',Buffer.from(binarySTL(part.output)));}
 if(m.p.seamJoint){const p=m.parts.find(p=>p.kind==='clip');assert.equal(m.instances.filter(i=>i.part===p).length,p.installed);assert.equal(p.qty,p.installed+p.spares);assert(Math.abs(p.dim[2]-10)<.001);fs.writeFileSync('tmp/system-validation/clip.stl',Buffer.from(binarySTL(p.output)));}
 const snap=scadSource(m);assert(snap.includes('multmatrix'));assert(snap.includes('mount-cradle'));assert(manifest(m).hardware.some(x=>x.item==='azimuth clamp screw'));
 console.log('PASS integrated mount, clips, source transforms, hardware and plates',cfg);
}
for(const fit of [0,.05,.1]){const c=connectionCoupon(build({seamJoint:1,clipFit:fit}));assert.equal(new Set(c.map(x=>x.id)).size,c.length);assert(c.filter(x=>x.id.startsWith('seam-clip')).every(x=>Math.abs(bounds(x.output).size[2]-10)<.001));}
assert.throws(()=>build({seamJoint:1,clipDetent:2}),/strain/);assert.throws(()=>build({mountMode:1,seamJoint:1,elevation:-10}),/elevation/);
assert(100*clipStrain(defaults)<defaults.clipAllowableStrain);
console.log('PASS strain gate, unique coupons and clip orientation');
