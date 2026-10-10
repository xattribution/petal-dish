// Rim band (L, U, frame) and diamond ribs: closed solids, no new steep face away from the band and none steeper than the
// square wall itself must be (60° at 6 petals, 45° from 8 up), nothing in front of the reflecting face, the bands of the outer petals close into one ring, the seam stations keep clear of
// the band, and the ribs are really there. Hardware fit with stiffeners on is covered by the
// structure tests' stiffener cases.
import assert from 'node:assert/strict';
import {build,defaults,volume,zAt} from '../dist/geometry.js';
import {solid,solidScope} from '../dist/solid.js';
import {localPoint} from '../dist/scene.js';
import {rimReach,RIM} from '../dist/stiffeners.js';
function closed(mesh,name){const edges=new Map();for(const f of mesh.f)for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],key=Math.min(a,b)+':'+Math.max(a,b),e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}
 assert([...edges.values()].every(([n,w])=>n===2&&w===0),name+' is a closed manifold');assert(volume(mesh)>0,name+' has positive volume');}
// Faces of the print mesh (z up, bed at 0) that face down more than 45°, not counting the first 0.5 mm (the bed face and
// the shell's 0.2 mm seam gap there), with how far down
// they face and their radius in the dish (the print mesh keeps the dish mesh's vertex and face order).
function steep(part){const out=part.output,list=[];out.f.forEach(([i,j,k],n)=>{const a=out.v[i],b=out.v[j],d=out.v[k];if(a[2]<.5&&b[2]<.5&&d[2]<.5)return;
 const u=b.map((x,q)=>x-a[q]),w=d.map((x,q)=>x-a[q]),c=[u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]],l=Math.hypot(...c);if(!(l>0)||c[2]/l>=-Math.SQRT1_2-.02)return;
 const m=part.mesh.v,r=Math.hypot(...[0,1].map(q=>(m[i][q]+m[j][q]+m[k][q])/3));list.push({area:l/2,down:Math.asin(Math.min(1,-c[2]/l))*180/Math.PI,r});});return list;}
const sum=list=>list.reduce((s,x)=>s+x.area,0);
const inside=(polys,x,y)=>{let hit=false;for(const poly of polys)for(let i=0,j=poly.length-1;i<poly.length;j=i++){const[a,b]=[poly[i],poly[j]];if((a[1]>y)!==(b[1]>y)&&x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0])hit=!hit;}return hit;};
// Reach of each band: the fillet for L, the 45° foot (frame) and the 90° foot (U) a little further.
assert.equal(rimReach({rimBand:0}),0);assert.equal(rimReach({rimBand:1,rimDepth:14}),RIM.wall+RIM.gusset);assert(rimReach({rimBand:3,rimDepth:14})>RIM.wall+RIM.gusset&&rimReach({rimBand:3,rimDepth:14})<9);assert(rimReach({rimBand:2,rimDepth:14})<9);
assert.equal(defaults.rimBand,3);assert.equal(defaults.ribs,0);
const cases=[{},{rimBand:2,ribs:1},{rimBand:3,ribs:1,seamJoint:1},{rimBand:2,fd:.25,rimDepth:30},{rimBand:3,fd:.25,rimDepth:6},{rimBand:2,fd:.8,ribs:1,rearStyle:1},
 {rimBand:2,ribs:1,diameter:600,sectors:8,bedX:300,bedY:300,bedZ:330},{rimBand:3,ribs:1,diameter:600,sectors:8,rows:2,seamJoint:3},{rimBand:1,ribs:1,feedMode:1,seamJoint:2,mountMode:1}];
for(const c of cases){const m=build({...defaults,...c}),plain=build({...defaults,...c,rimBand:0,ribs:0}),n=m.layout.n,h=Math.PI/n,R=m.p.diameter/2,rows=m.layout.rows;
 assert.deepEqual([m.layout.n,m.layout.rows],[plain.layout.n,plain.layout.rows],'same segmentation');
 for(const part of m.parts.filter(p=>p.kind==='panel')){const base=plain.parts.find(p=>p.id===part.id),label=JSON.stringify(c)+' '+part.id;
  closed(part.mesh,label);closed(part.output,label+' print');
  // away from the band no new face steeper than 45° in the side print; at the band none steeper than the square wall
  // has to be, which faces down at twice the petal's half angle near the top of the print (60° with 6 petals)
  const edge=R-(part.spec.row===rows-1?rimReach(m.p):0)-1,mine=steep(part),plainSteep=steep(base),limit=Math.max(45,360/n)+1.5;
  assert(sum(mine.filter(x=>x.r<edge))<=sum(plainSteep.filter(x=>x.r<edge))+1,`${label}: ${sum(mine.filter(x=>x.r<edge)).toFixed(1)} mm² steep faces away from the band, plain ${sum(plainSteep.filter(x=>x.r<edge)).toFixed(1)}`);
  const worst=Math.max(0,...mine.filter(x=>x.r>=edge&&x.area>.05).map(x=>x.down));assert(worst<=limit,`${label}: band face ${worst.toFixed(1)}° from vertical, limit ${limit}`);
  // nothing in front of the reflecting face
  const front=Math.max(...part.mesh.v.map(([x,y,z])=>z-zAt(Math.hypot(x,y),m.p)));assert(front<.02,`${label}: ${front.toFixed(3)} mm in front of the reflector`);
  // stiffeners add material; a seam keeps its station count, or loses one where the band leaves too little room
  if(m.p.rimBand&&part.spec.row===rows-1||m.p.ribs)assert(volume(part.mesh)>volume(base.mesh)+200,label+' stiffener material');
  part.spec.flanges.forEach((f,k)=>assert(f.stations.length>=base.spec.flanges[k].stations.length-1,label+' station count'));
  // the last station of each radial seam that reaches the rim stays clear of the band
  if(m.p.rimBand&&part.spec.row===rows-1)for(const f of part.spec.flanges.filter(f=>f.frame.o[0]===0&&f.frame.o[1]===0&&f.end>=R-1.5)){const K=f.joint===3?14:f.joint?10:5+4;assert(f.stations.at(-1)<=f.end-rimReach(m.p,h)-K+.6,label+' station clear of the band');}}
 // Assembled, the bands make one ring: the wall's middle, half way down, is solid all the way round.
 if(m.p.rimBand)solidScope(()=>{const outer=m.instances.filter(i=>i.part.kind==='panel'&&i.part.spec.row===rows-1);let ring=null;
  for(const i of outer){const s=solid({v:i.part.mesh.v.map(q=>localPoint(i,q)),f:i.part.mesh.f});ring=ring?ring.union(s):s;}
  const rimZ=zAt(R,m.p)-m.p.thickness,z=rimZ-m.p.rimDepth/2,r=R-RIM.wall/2,section=ring.raw.slice(z),polys=section.toPolygons().map(p=>p.map(q=>Array.isArray(q)?q:[q.x??q[0],q.y??q[1]]));section.delete();
  let hits=0;const N=720;for(let k=0;k<N;k++){const a=(k+.37)*2*Math.PI/N;if(inside(polys,r*Math.cos(a),r*Math.sin(a)))hits++;}
  assert(hits/N>.97,`${JSON.stringify(c)}: band ring ${(100*hits/N).toFixed(1)}% closed`);});
 console.log('PASS stiffeners',JSON.stringify(c),`${n} × ${rows}`,m.parts.filter(p=>p.kind==='panel').map(p=>`${p.id} +${((volume(p.mesh)-volume(plain.parts.find(q=>q.id===p.id).mesh))/1000).toFixed(1)} cm³`).join(', '));}
