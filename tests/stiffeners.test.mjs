// Rim band (L, U, triangle) and diamond ribs: closed solids, no face steeper than the plain petal already has, nothing
// in front of the reflecting face, the bands of the outer petals close into one ring, the seam stations keep clear of
// the band, and the ribs are really there. Hardware fit with stiffeners on is covered by the
// structure tests' stiffener cases.
import assert from 'node:assert/strict';
import {build,defaults,volume,zAt} from '../dist/geometry.js';
import {solid,solidScope} from '../dist/solid.js';
import {localPoint} from '../dist/scene.js';
import {rimLean,rimReach,RIM} from '../dist/stiffeners.js';
function closed(mesh,name){const edges=new Map();for(const f of mesh.f)for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],key=Math.min(a,b)+':'+Math.max(a,b),e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}
 assert([...edges.values()].every(([n,w])=>n===2&&w===0),name+' is a closed manifold');assert(volume(mesh)>0,name+' has positive volume');}
// Area of faces in the print mesh (z up, bed at 0) that face down more than 45°, not counting the bed face.
function overhang(out){let area=0;for(const[i,j,k]of out.f){const a=out.v[i],b=out.v[j],d=out.v[k];if(a[2]<.05&&b[2]<.05&&d[2]<.05)continue;
 const u=b.map((x,n)=>x-a[n]),w=d.map((x,n)=>x-a[n]),c=[u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]],l=Math.hypot(...c);if(l>0&&c[2]/l<-Math.SQRT1_2-.02)area+=l/2;}return area;}
const inside=(polys,x,y)=>{let hit=false;for(const poly of polys)for(let i=0,j=poly.length-1;i<poly.length;j=i++){const[a,b]=[poly[i],poly[j]];if((a[1]>y)!==(b[1]>y)&&x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0])hit=!hit;}return hit;};
// The lean: none from 8 petals up; 6 petals lean just enough to keep the band's inner face at 45° in the side print.
assert.equal(rimLean(Math.PI/8),0);assert.equal(rimLean(Math.PI/10),0);{const b=rimLean(Math.PI/6);assert(b>35*Math.PI/180&&b<40*Math.PI/180&&Math.cos(b)*Math.sin(Math.PI/3)<=Math.SQRT1_2);}
assert.equal(defaults.rimBand,1);assert.equal(defaults.ribs,0);
const cases=[{},{rimBand:2,ribs:1},{rimBand:3,ribs:1,seamJoint:1},{rimBand:2,fd:.25,rimDepth:30},{rimBand:3,fd:.25,rimDepth:6},{rimBand:2,fd:.8,ribs:1,rearStyle:1},
 {rimBand:2,ribs:1,diameter:600,sectors:8,bedX:300,bedY:300,bedZ:330},{rimBand:3,ribs:1,diameter:600,sectors:8,rows:2,seamJoint:3},{rimBand:1,ribs:1,feedMode:1,seamJoint:2,mountMode:1}];
for(const c of cases){const m=build({...defaults,...c}),plain=build({...defaults,...c,rimBand:0,ribs:0}),n=m.layout.n,h=Math.PI/n,R=m.p.diameter/2,rows=m.layout.rows;
 assert.deepEqual([m.layout.n,m.layout.rows],[plain.layout.n,plain.layout.rows],'same segmentation');
 for(const part of m.parts.filter(p=>p.kind==='panel')){const base=plain.parts.find(p=>p.id===part.id),label=JSON.stringify(c)+' '+part.id;
  closed(part.mesh,label);closed(part.output,label+' print');
  // no new face steeper than 45° in the side print
  assert(overhang(part.output)<=overhang(base.output)+1,`${label}: ${overhang(part.output).toFixed(1)} mm² steep faces, plain ${overhang(base.output).toFixed(1)}`);
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
  const rimZ=zAt(R,m.p)-m.p.thickness,z=rimZ-m.p.rimDepth/2,b=rimLean(h),T=RIM.wall/Math.cos(b),r=R-m.p.rimDepth/2*Math.tan(b)-T/2,section=ring.raw.slice(z),polys=section.toPolygons().map(p=>p.map(q=>Array.isArray(q)?q:[q.x??q[0],q.y??q[1]]));section.delete();
  let hits=0;const N=720;for(let k=0;k<N;k++){const a=(k+.37)*2*Math.PI/N;if(inside(polys,r*Math.cos(a),r*Math.sin(a)))hits++;}
  assert(hits/N>.97,`${JSON.stringify(c)}: band ring ${(100*hits/N).toFixed(1)}% closed`);});
 console.log('PASS stiffeners',JSON.stringify(c),`${n} × ${rows}`,m.parts.filter(p=>p.kind==='panel').map(p=>`${p.id} +${((volume(p.mesh)-volume(plain.parts.find(q=>q.id===p.id).mesh))/1000).toFixed(1)} cm³`).join(', '));}
