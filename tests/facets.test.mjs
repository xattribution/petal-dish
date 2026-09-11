import assert from 'node:assert/strict';
import {build,defaults,facetZ,zAt} from '../dist/geometry.js';
for(const cfg of [{},{diameter:800,facetAngle:15},{diameter:180,facetAngle:10}]){
 const m=build({...defaults,rearStyle:1,...cfg});
 for(const p of m.parts.filter(p=>p.kind==='panel')){
  const d=p.spec.facet;assert(Math.abs((Math.atan(d[3])-Math.atan(d[2]))*180/Math.PI-m.p.facetAngle)<1e-9);assert(p.angle>=45);
  for(const [x,y,z]of p.mesh.v){const front=zAt(Math.hypot(x,y),m.p),back=facetZ(x,d);assert(front-back>=m.p.thickness-1e-7);assert(z>=back-1e-7&&z<=front+1e-7);}
  for(const face of p.mesh.f){const vs=face.map(i=>p.mesh.v[i]);if(vs.every(([x,,z])=>Math.abs(z-facetZ(x,d))<1e-7)){assert(Math.min(...vs.map(v=>v[0]))>=d[1]-1e-6||Math.max(...vs.map(v=>v[0]))<=d[1]+1e-6,'Rear face crosses fold');}}
 }
 console.log('PASS planar rear, fold angle, minimum wall and parabolic front',cfg);
}
