import assert from 'node:assert/strict';
import {build,defaults,hardwareSchedule,validate,volume} from '../dist/geometry.js';
import {connectionCatalog} from '../dist/connections.js';
import {manifest,kit} from '../dist/exports.js';
for(const staggerRings of [0,1]){
 const parameters={...defaults,diameter:700,resolution:10,staggerRings,packPlates:1},base=build(parameters),catalog=connectionCatalog(base),target=catalog.find(j=>j.family==='radial');
 assert(catalog.filter(j=>j.family==='radial'||j.family==='ring').every(j=>j.parts.length===2));
 const m=build({...parameters,connections:{families:{ring:{seamJoint:1}},joints:{[target.id]:{seamJoint:2,seamBolt:4}},roots:{0:1},mounts:{1:0}},printSelection:{clip:false}}),changed=connectionCatalog(m);
 const joint=changed.find(j=>j.id===target.id);assert.equal(joint.method,2);assert.equal(joint.size,4);assert.equal(joint.parts.length,2);
 assert(changed.filter(j=>j.family==='ring').every(j=>j.method===1));assert(changed.filter(j=>j.family==='root'&&j.index!==0).every(j=>j.method===0));assert.equal(changed.find(j=>j.id==='root:0').method,1);
 assert(m.parts.filter(p=>p.row===0).length>1);assert(m.parts.every(p=>volume(p.mesh)>0));assert(m.plates.every(p=>p.placements.every(q=>q.part.kind!=='clip')));
 const schedule=hardwareSchedule(m);assert.equal(schedule.filter(h=>h.item==='root screw').reduce((n,h)=>n+h.quantity,0),m.layout.n);assert.equal(schedule.find(h=>h.item==='root nut').quantity,1);assert.equal(schedule.filter(h=>h.item==='external mount screw').reduce((n,h)=>n+h.quantity,0),4);
 assert.equal(manifest(m).connections.length,changed.length);
 const data=await kit(m).arrayBuffer(),view=new DataView(data),names=[];let offset=0;while(view.getUint32(offset,true)===0x04034b50){const n=view.getUint16(offset+26,true),extra=view.getUint16(offset+28,true),size=view.getUint32(offset+18,true);names.push(new TextDecoder().decode(data.slice(offset+30,offset+30+n)));offset+=30+n+extra+size;}assert(names.includes('CONNECTIONS.csv'));assert(!names.some(n=>n.startsWith('seam-clip_qty-')));assert(names.some(n=>n.includes('seam-clip-fit-')));
 console.log('PASS paired mixed seams, individual overrides, root/mount hardware, print exclusion and export map',staggerRings);
}
assert.throws(()=>validate({...defaults,connections:{families:{ring:{seamJoint:8}}}}));assert.throws(()=>validate({...defaults,connections:{joints:{x:{seamBolt:5}}}}));assert.throws(()=>validate({...defaults,clipDetent:2,connections:{families:{ring:{seamJoint:1}}}}),/strain/);
console.log('PASS connection input validation');
