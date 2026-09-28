import assert from 'node:assert/strict';
import {build,defaults,connectionCoupon,volume,bounds,JOINT} from '../dist/geometry.js';
import {kit,manifest} from '../dist/exports.js';

function closed(mesh){
 const edges=new Map();
 for(const f of mesh.f){
  const [a,b,c]=f.map(i=>mesh.v[i]),u=b.map((x,i)=>x-a[i]),v=c.map((x,i)=>x-a[i]);
  assert(Math.hypot(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])>1e-9,'Nondegenerate coupon faces');
  for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],key=[Math.min(a,b),Math.max(a,b)].join(':');const e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}
 }
 for(const [count,winding] of edges.values())assert(count===2&&winding===0,'Closed, consistently wound coupon');
 assert(volume(mesh)>0);
}
for(const cfg of [{},{rearStyle:1},{diameter:180,thickness:1.6,jointClearance:.4},{diameter:800,rows:3}]){
 const m=build({...defaults,...cfg}),coupon=connectionCoupon(m);
 assert.equal(coupon.length,3);
 const holes=new Set();
 for(const p of coupon){
  closed(p.mesh);closed(p.output);
  assert(Math.abs(bounds(p.output).min[2])<1e-8);
  assert(Math.abs(volume(p.mesh)-volume(p.output))<1e-5);
  const size=bounds(p.output).size;assert(size[0]<m.p.bedX-2*m.p.margin&&size[1]<m.p.bedY-2*m.p.margin&&size[2]<m.p.bedZ-2);
  const s=p.spec;
  const inside=h=>h.r>s.r0&&h.r<s.r1&&h.a>s.a0&&h.a<s.a1;
  const key=h=>[h.r*Math.cos(h.a+p.assemblyRotation),h.r*Math.sin(h.a+p.assemblyRotation)].map(x=>x.toFixed(4)).join(',');
  if(p.id!=='coupon-plate'){for(const h of s.holes.filter(inside))holes.add(key(h));}
  else for(const h of s.holes)assert(holes.has(key(h)),'Both plate bores match cropped panel bores');
 }
 for(const p of m.parts.filter(p=>p.kind==='bridge'))for(const h of p.spec.nuts){
  const s=p.spec;
  const wall=Math.min(h.r-s.r0,s.r1-h.r,h.r*Math.sin(h.a-s.a0),h.r*Math.sin(s.a1-h.a))-h.w/2;
  assert(wall>2.7,'Nut pocket has a conservative 2.7 mm edge ligament');
  const x=h.r*Math.cos(h.a),y=h.r*Math.sin(h.a),i=s.nuts.indexOf(h);
  assert(s.baseFn(x,y)-s.nutSeats[i]>=JOINT.bearingWall,'Plate nut bearing wall');
 }
 assert.equal(manifest(m).interface.revision,9);
 console.log('PASS reinforced plate margins and printable joint coupon',cfg);
}
const m=build(defaults),archive=await kit(m,'// test kernel').arrayBuffer();
const names=new TextDecoder().decode(archive);
for(const name of ['FIT_TEST/coupon-left.stl','FIT_TEST/coupon-right.stl','FIT_TEST/coupon-plate.stl','FIT_TEST/README.md'])assert(names.includes(name),'ZIP includes '+name);
assert.equal(connectionCoupon(build({...defaults,jointStyle:0})).length,0);
console.log('PASS fit-test archive and legacy exclusion');
