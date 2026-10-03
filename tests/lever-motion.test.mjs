import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {build,LEVER_PARTS} from '../dist/geometry.js';
import {leverMeshes} from '../dist/lever-meshes.js';
import {solid,solidScope} from '../dist/solid.js';
import {localPoint} from '../dist/scene.js';

assert.equal(leverMeshes.source_sha256,createHash('sha256').update(fs.readFileSync('cad/seam-lever.scad')).digest('hex'));
for(const seamBolt of [3,4])for(const diameter of [400,600]){
 const m=build({seamJoint:3,seamBolt,diameter,bedX:300,bedY:300,bedZ:300});
 solidScope(()=>{
  const dish=m.instances.filter(i=>i.part.kind==='panel'||i.part.kind==='hub').map(i=>solid(i.part.mesh).transform(q=>localPoint(i,q))).reduce((a,b)=>a.union(b));
  const instances=m.instances.filter(i=>i.part.id==='seam-lever-lever');
  for(const ins of instances)for(const [name] of LEVER_PARTS){
   const opened=leverMeshes['M'+seamBolt][name].open;
   assert(solid(opened).transform(q=>localPoint(ins,q)).intersect(dish).raw.volume()<.05,`M${seamBolt}, ${diameter}: open ${name} clears dish`);
  }
  // A cam follows its flange tangent as it turns; the draw bar and spring allow its pivot to translate.
  // Check that tangent motion, rather than rotating the cam about a fixed closed-position pivot.
  const set=leverMeshes['M'+seamBolt],mesh=set.lever.installed,pose=set.motion;
  const pivot=pose.wall+pose.closedRadius;
  for(const angle of [15,30,45,60,75]){
   const a=angle*Math.PI/180,c=Math.cos(a),s=Math.sin(a);
   const relative=mesh.v.map(([x,y,z])=>[c*(x-pivot)-s*(y-pose.holeY),s*(x-pivot)+c*(y-pose.holeY),z]);
   const radius=-Math.min(...relative.map(q=>q[0]));
   assert(radius>=pose.openRadius-.001&&radius<=pose.peakRadius+.001,'Cam draw stays inside spring travel');
   const posed={v:relative.map(([x,y,z])=>[x+pose.wall+radius,y+pose.holeY,z]),f:mesh.f};
   for(const ins of instances)assert(solid(posed).transform(q=>localPoint(ins,q)).intersect(dish).raw.volume()<.05,`M${seamBolt}, ${diameter}: lever at ${angle} clears dish`);
  }
 });
 console.log('PASS open lever set and sampled cam motion', {seamBolt,diameter});
}
