// Breakaway rim support: print only (the assembled dish is unchanged), on the outer ring only, one closed file with the
// petal, joined to it only by its tines, standing on the bed, no taller or longer than the petal, and printable: no face
// steeper than 45° except the flat undersides of the tines (bridges of about 1 mm).
import assert from 'node:assert/strict';
import {build,defaults,volume,bounds} from '../dist/geometry.js';
import {solid,solidScope} from '../dist/solid.js';
import {rimSupport,SUPPORT} from '../dist/rim-support.js';
function closed(mesh,name){const edges=new Map();for(const f of mesh.f)for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],key=Math.min(a,b)+':'+Math.max(a,b),e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}
 assert([...edges.values()].every(([n,w])=>n===2&&w===0),name+' is a closed manifold');assert(volume(mesh)>0,name+' has positive volume');}
// downward faces of a print mesh above the first 0.5 mm: [area, how far down from horizontal-facing (degrees)]
function downward(mesh){const out=[];for(const[i,j,k]of mesh.f){const a=mesh.v[i],b=mesh.v[j],d=mesh.v[k];if(Math.min(a[2],b[2],d[2])<.5)continue;
 const u=b.map((x,q)=>x-a[q]),w=d.map((x,q)=>x-a[q]),c=[u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]],l=Math.hypot(...c);if(!(l>0))continue;
 if(c[2]/l<-Math.SQRT1_2-.01)out.push([l/2,Math.asin(Math.min(1,-c[2]/l))*180/Math.PI]);}return out;}
assert.equal(defaults.rimSupport,1);
const cases=[{},{sectors:8},{diameter:600,sectors:12,bedX:300,bedY:300,bedZ:330},{diameter:800,rows:2,bedX:300,bedY:300,bedZ:330},{rimBand:0},{rimBand:2,rimLip:3,ribs:3},
 {supportWall:6,supportGap:.6,tinePitch:10,tineWidth:1},{fd:.25,rimDepth:30},{fd:.8,seamJoint:1},{feedMode:1,diameter:600,bedX:300,bedY:300,bedZ:330}];
for(const c of cases){const m=build({...defaults,...c}),plain=build({...defaults,...c,rimSupport:0}),rows=m.layout.rows,label=JSON.stringify(c);
 assert.deepEqual([m.layout.n,rows],[plain.layout.n,plain.layout.rows],label+' same segmentation');
 for(const part of m.parts.filter(p=>p.kind==='panel')){const base=plain.parts.find(p=>p.id===part.id),name=label+' '+part.id,outer=part.row===rows-1;
  // the dish is the same: the support exists only in the printed file
  assert.equal(part.mesh.v.length,base.mesh.v.length,name+' same dish mesh');assert(Math.abs(volume(part.mesh)-volume(base.mesh))<1e-6,name+' same dish volume');
  if(!outer){assert.equal(part.supportMeshes.length,0,name+' inner ring has no rim support');assert.equal(part.output,part.print);continue;}
  const sup=part.supportMeshes[0],info=part.rimSupport;assert(sup&&info,name+' has a rim support');
  closed(part.output,name+' print file');closed(sup,name+' support');
  // the petal sits exactly as without the support; the file is the petal and the support joined
  assert.equal(part.print.v.length,base.print.v.length);const bo=bounds(part.output),bp=bounds(part.print),bs=bounds(sup);
  assert(Math.abs(bo.min[2])<1e-4&&Math.abs(bs.min[2])<1e-4&&Math.abs(bp.min[2])<1e-4,name+' petal and support stand on the bed');
  assert(bs.max[2]<=bp.max[2]+1e-6,name+' support no taller than the petal');
  assert(Math.abs(part.dim[0]-base.dim[0])<.25||Math.abs(part.dim[1]-base.dim[1])<.25,name+' support stays inside the rim outline along the bed');
  assert(Math.max(...part.dim)<=Math.max(...base.dim)+.25,name+' no longer than the petal');
  const grow=part.dim[0]+part.dim[1]-base.dim[0]-base.dim[1];assert(grow<=m.p.supportGap+m.p.supportWall+.6,`${name}: footprint grows ${grow.toFixed(2)} mm`);
  // joined only by the tines: their overlap with the petal is tiny, and the wall without them stays a gap away
  solidScope(()=>{const P=solid(part.print),S=solid(sup),both=P.union(S),overlap=P.intersect(S).raw.volume(),per=overlap/info.tines;
   assert(per>.02&&per<.3,`${name}: ${per.toFixed(3)} mm³ of each tine inside the rim`);
   assert(Math.abs(both.raw.volume()-volume(part.output))<1,name+' file is petal ∪ support');
   assert.equal(both.raw.decompose().filter(x=>x.volume()>0).length,1,name+' one connected print');
   // the wall alone, in the petal's frame, moved toward the petal by almost the gap, does not reach it
   const n=m.layout.n,wall=rimSupport(m.p,n,part.mesh,{tines:false}).solid.transform(q=>[q[0],q[1],q[2]-(m.p.supportGap-.03)]);
   assert(wall.intersect(solid(part.mesh)).raw.volume()<1e-6,name+' wall clear of the rim');});
  // tines from just above the bed to near the top, never further apart than the setting
  assert(info.tines>=Math.ceil((info.height-3)/m.p.tinePitch)&&info.tines>=2,`${name}: ${info.tines} tines over ${info.height.toFixed(0)} mm`);
  // printable: nothing steeper than 45° but the tines' flat undersides, which are small
  const down=downward(sup),slanted=down.filter(([a,deg])=>deg<89.5&&a>.01),flat=down.filter(([,deg])=>deg>=89.5).reduce((s,[a])=>s+a,0);
  assert.equal(slanted.length,0,`${name}: ${slanted.length} support faces steeper than 45°, worst ${Math.max(0,...slanted.map(x=>x[1])).toFixed(1)}°`);
  assert(flat<=info.tines*(SUPPORT.root+3)*(1.2+m.p.tineWidth),`${name}: ${flat.toFixed(1)} mm² of flat underside`);
  // the support carries no weight of its own on the petal: it stands on a foot on the bed
  const foot=sup.f.reduce((s,[i,j,k])=>{const a=sup.v[i],b=sup.v[j],d=sup.v[k];if(Math.max(a[2],b[2],d[2])>1e-4)return s;return s+Math.abs((b[0]-a[0])*(d[1]-a[1])-(b[1]-a[1])*(d[0]-a[0]))/2;},0);
  assert(foot>=m.p.supportWall*SUPPORT.bow*.9,`${name}: ${foot.toFixed(1)} mm² foot`);}
 console.log('PASS rim support',label,`${m.layout.n} × ${rows}`,m.parts.filter(p=>p.rimSupport).map(p=>`${p.id}: ${p.rimSupport.tines} tines, ${(p.rimSupport.volume/1000).toFixed(1)} cm³`).join(', '),`plates ${plain.plates.length} → ${m.plates.length}`);}
