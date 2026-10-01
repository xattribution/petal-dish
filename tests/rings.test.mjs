import assert from 'node:assert/strict';
import {build,backZ,rootBottom} from '../dist/geometry.js';
import {solid,solidScope,cylinder} from '../dist/solid.js';
const turn=(v,a)=>[Math.cos(a)*v[0]-Math.sin(a)*v[1],Math.sin(a)*v[0]+Math.cos(a)*v[1],v[2]],distance=(a,b)=>Math.hypot(...a.map((v,i)=>v-b[i]));
for(const cfg of [{diameter:600},{diameter:800,rows:3,sectors:8,bedX:300,bedY:300,bedZ:300},{diameter:600,rearStyle:1},{diameter:600,feedMode:1},{diameter:600,feedMode:1,feedLegs:4}]){
 const m=build(cfg),h=Math.PI/m.layout.n;
 for(let row=1;row<m.layout.rows;row++){
 const holes=j=>m.instances.filter(i=>i.part.kind==='panel'&&i.part.row===j).flatMap(i=>i.part.spec.flanges.filter(f=>Math.hypot(...f.frame.o)>1).flatMap(f=>f.stations.map((s,k)=>{const q=[f.frame.o[0]+s*f.frame.e[0],f.frame.o[1]+s*f.frame.e[1],0];q[2]=f.levels[k];return{q:turn(q,i.a),normal:turn([...f.frame.v,0],i.a),r:Math.hypot(...f.frame.o)};})));
 const a=holes(row-1),b=holes(row),r=Math.max(...a.map(x=>x.r));
 for(const x of a.filter(x=>Math.abs(x.r-r)<.001)){const match=b.filter(y=>distance(x.q,y.q)<.15);assert.equal(match.length,1,'each cross-ring screw has exactly one matching bore');assert(distance(x.normal,match[0].normal.map(v=>-v))<1e-6,'opposed bore axes');}
 // Neighboring radial seams differ by half a sector; no four-panel corner.
 assert(Math.abs(Math.abs(m.ringPhases[row]-m.ringPhases[row-1])-h)<1e-9);
 }
 if(m.feed){const panels=m.instances.filter(i=>i.part.spec.feedMount);assert.deepEqual(panels.map(i=>i.a),m.feed.legs.map(l=>l.angle));}
 console.log('PASS phased ring bore alignment, T-junction spacing and feed registration',cfg);
}
for(const mountThrough of [0,1]){const m=build({mountThrough});solidScope(()=>{const hub=solid(m.parts.find(p=>p.kind==='hub').mesh),bottom=rootBottom(m.p)-6;for(let i=0;i<4;i++){const a=Math.PI/4+i*Math.PI/2,x=30*Math.cos(a),y=30*Math.sin(a),v=hub.intersect(cylinder([x,y,bottom-1],[x,y,100],2.25)).raw.volume();if(mountThrough)assert(v<.001);else assert(v>1);}})}
console.log('PASS all four mount bores: full-depth versus blind');
