import assert from 'node:assert/strict';
import {build,defaults,volume} from '../dist/geometry.js';
const key=(r,a)=>[r*Math.cos(a),r*Math.sin(a)].map(x=>x.toFixed(4)).join(',');
for(const rearStyle of [0,1])for(const staggerRings of [0,1]){
 const cfg={...defaults,diameter:600,sectors:8,rows:3,bedX:400,bedY:400,bedZ:400,rearStyle,staggerRings},models=[0,1].map(jointStyle=>build({...cfg,jointStyle}));
 assert.deepEqual(models[0].ringPhases,models[1].ringPhases,'Joint style must not change ring phase');
 for(const m of models){const holes=new Set();for(const i of m.instances.filter(x=>x.part.kind==='panel'))for(const h of i.part.spec.holes)holes.add(key((h.r0+h.r1)/2,(h.a0+h.a1)/2+i.a));for(const i of m.instances.filter(x=>x.part.kind==='bridge'))for(const h of i.part.spec.holes)assert(holes.has(key((h.r0+h.r1)/2,(h.a0+h.a1)/2+i.a)),'Every plate bore meets a panel bore');
 for(const p of m.parts.filter(p=>p.kind==='bridge'&&p.spec.floorPlane)){const [mx,my,c]=p.spec.floorPlane,normal=Math.hypot(1,mx,my);assert(p.print.f.some(f=>f.every(i=>Math.abs(p.print.v[i][2])<1e-7)),'Export has a flat bed-contact face');for(const [x,y]of p.mesh.v)assert((p.spec.baseFn(x,y)-mx*x-my*y-c)/normal>=3.2-1e-7,'Plate has at least 3.2 mm normal thickness');assert(volume(p.mesh)>0);}}
 console.log('PASS independent staggering, bore mapping and flat plate bases',{rearStyle,staggerRings});
}
