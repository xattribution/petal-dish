const PI=Math.PI,TAU=2*PI;
export const zAt=(r,p)=>r*r/(4*p.diameter*p.fd);
const unique=a=>[...new Set(a.map(x=>+x.toFixed(9)))].sort((a,b)=>a-b);
const spaced=(a,b,n)=>Array.from({length:n+1},(_,i)=>a+(b-a)*i/n);
export function hole(r,a,w=4.6){return {r0:r-w/2,r1:r+w/2,a0:a-w/(2*r),a1:a+w/(2*r),r,a,w,shape:"round"};}
const boreSamples=[-1,-.75,-1/Math.sqrt(3),-.25,0,.25,1/Math.sqrt(3),.75,1];
function boreCuts(h,axis){return h.shape?boreSamples.map(t=>axis==='r'?h.r+t*h.w/2:h.a+t*h.w/(2*h.r)):[h[axis+'0'],h[axis+'1']];}
function boreGroups(boxes){const groups=[];for(const h of boxes.filter(h=>h.shape)){let g=groups.find(g=>Math.abs(g[0].r-h.r)<1e-6&&Math.abs(g[0].a-h.a)<1e-6);if(!g){g=[];groups.push(g);}if(!g.some(x=>Math.abs(x.w-h.w)<1e-6))g.push(h);}return groups.map(g=>g.sort((a,b)=>a.w-b.w));}
function warpBores(x,y,groups){const r=Math.hypot(x,y),a=Math.atan2(y,x);for(const g of groups){const h=g[0],da=Math.atan2(Math.sin(a-h.a),Math.cos(a-h.a)),u=r-h.r,v=h.r*da,q=Math.max(Math.abs(u),Math.abs(v)),outer=g.at(-1).w/2,limit=outer*1.15;if(q>=limit||q<1e-10)continue;const angle=Math.atan2(v,u),cs=Math.cos(angle),sn=Math.sin(angle),point=b=>{const radius=b.shape==='hex'?b.af/2/Math.max(...Array.from({length:6},(_,i)=>Math.cos(angle-i*PI/3))):b.w/2;return[radius*cs,radius*sn];};let p0=[0,0],q0=0,p1,q1;for(const b of g){p1=point(b);q1=b.w/2;if(q<=q1)break;p0=p1;q0=q1;}if(q>q1){p1=[Math.cos(h.a)*(x-h.r*Math.cos(h.a))+Math.sin(h.a)*(y-h.r*Math.sin(h.a)),-Math.sin(h.a)*(x-h.r*Math.cos(h.a))+Math.cos(h.a)*(y-h.r*Math.sin(h.a))];q1=limit;}const t=(q-q0)/(q1-q0),U=p0[0]+t*(p1[0]-p0[0]),V=p0[1]+t*(p1[1]-p0[1]);return[h.r*Math.cos(h.a)+U*Math.cos(h.a)-V*Math.sin(h.a),h.r*Math.sin(h.a)+U*Math.sin(h.a)+V*Math.cos(h.a)];}return[x,y];}
export function nutPocket(h,af=7.4){return {...hole(h.r,h.a,af/Math.cos(PI/6)),shape:'hex',af};}
export function splitLines(mesh,lines){let v=mesh.v.map(v=>[...v]),faces=mesh.f;const eps=1e-6;for(const [a,b,c]of lines){const d=v.map(p=>a*p[0]+b*p[1]-c),cuts=new Map(),next=[];const hit=(u,w)=>{const key=Math.min(u,w)+':'+Math.max(u,w);if(!cuts.has(key)){const t=d[u]/(d[u]-d[w]);cuts.set(key,v.length);v.push(v[u].map((x,k)=>x+(v[w][k]-x)*t));}return cuts.get(key);};for(const f of faces){const ds=f.map(i=>d[i]);if(Math.min(...ds)>=-eps||Math.max(...ds)<=eps){next.push(f);continue;}for(const sign of [1,-1]){const poly=[];for(let j=0;j<3;j++){const u=f[j],w=f[(j+1)%3];if(sign*d[u]>=-eps)poly.push(u);if((d[u]>eps&&d[w]<-eps)||(d[u]<-eps&&d[w]>eps))poly.push(hit(u,w));}for(let j=1;j<poly.length-1;j++)next.push([poly[0],poly[j],poly[j+1]]);}}faces=next;}return {v,f:faces};}
export function patch(spec,p){
 if(spec.layers)return layeredPatch(spec,p);
 const {r0,r1,a0,a1,holes=[],thickness=p.thickness,offset=0}=spec;
 const rr=unique([...spaced(r0,r1,Math.max(1,Math.ceil((r1-r0)/p.resolution))),...(spec.extraR||[]),...holes.flatMap(h=>boreCuts(h,'r')).filter(r=>r>r0&&r<r1)]);
 const aa=unique([...spaced(a0,a1,Math.max(2,Math.ceil((a1-a0)*r1/p.resolution))),...holes.flatMap(h=>boreCuts(h,'a')).filter(a=>a>a0&&a<a1)]);
 const nr=rr.length,na=aa.length,full=Math.abs(a1-a0-TAU)<1e-7,cols=full?na-1:na;
 const groups=boreGroups(holes),v=[];for(let l=0;l<2;l++)for(const r of rr)for(let j=0;j<cols;j++){const a=aa[j];v.push([...warpBores(r*Math.cos(a),r*Math.sin(a),groups),l]);}
 const id=(i,j,l)=>l*nr*cols+i*cols+(j%cols);
 const active=Array.from({length:nr-1},(_,i)=>Array.from({length:na-1},(_,j)=>!holes.some(h=>(rr[i]+rr[i+1])/2>h.r0&&(rr[i]+rr[i+1])/2<h.r1&&(aa[j]+aa[j+1])/2>h.a0&&(aa[j]+aa[j+1])/2<h.a1)));
 const is=(i,j)=>{if(full)j=(j+na-1)%(na-1);return i>=0&&i<nr-1&&j>=0&&j<na-1&&active[i][j];};
 const f=[];
 for(let i=0;i<nr-1;i++)for(let j=0;j<na-1;j++)if(active[i][j]){
  const q=[id(i,j,0),id(i+1,j,0),id(i+1,j+1,0),id(i,j+1,0)],b=q.map(x=>x+nr*cols);
  f.push([q[0],q[1],q[2]],[q[0],q[2],q[3]],[b[2],b[1],b[0]],[b[3],b[2],b[0]]);
  [[0,1,i,j-1],[1,2,i+1,j],[2,3,i,j+1],[3,0,i-1,j]].forEach(([s,t,ni,nj])=>{if(!is(ni,nj))f.push([q[s],b[s],b[t]],[q[s],b[t],q[t]]);});
 }
 const split=spec.creaseLines?.length?splitLines({v,f},spec.creaseLines):{v,f};const tops=split.v.map(([x,y])=>spec.topFn?spec.topFn(x,y):zAt(Math.hypot(x,y),p)+offset),floor=spec.flatBottom?Math.min(...tops)-thickness:null;
 return {v:split.v.map(([x,y,w],i)=>{const bottom=spec.backFn?spec.backFn(x,y):spec.flatBottom?floor:tops[i]-thickness;return[x,y,tops[i]*(1-w)+bottom*w];}),f:split.f};
}
export function bounds(mesh){const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];const used=new Set(mesh.f.flat());for(const i of used)for(let k=0;k<3;k++){min[k]=Math.min(min[k],mesh.v[i][k]);max[k]=Math.max(max[k],mesh.v[i][k]);}return{min,max,size:max.map((x,k)=>x-min[k])};}
export function printMesh(mesh,tilt=0){const c=Math.cos(tilt),s=Math.sin(tilt);let m={v:mesh.v.map(([x,y,z])=>[c*x+s*z,y,-s*x+c*z]),f:mesh.f};const b=bounds(m);m.v=m.v.map(v=>v.map((x,k)=>x-(k===2?b.min[k]:(b.min[k]+b.max[k])/2)));return m;}
export function rotateBed(mesh,degrees){const c=Math.cos(degrees*PI/180),s=Math.sin(degrees*PI/180);return{v:mesh.v.map(([x,y,z])=>[c*x-s*y,s*x+c*y,z]),f:mesh.f};}
export function mergeMeshes(meshes){const v=[],f=[];for(const mesh of meshes){const off=v.length;for(const point of mesh.v)v.push(point);for(const face of mesh.f)f.push(face.map(i=>i+off));}return{v,f};}
export function binarySTL(mesh){mesh=compactMesh({v:mesh.v.map(p=>p.map(Math.fround)),f:mesh.f});const ab=new ArrayBuffer(84+mesh.f.length*50),d=new DataView(ab);new Uint8Array(ab,0,80).set(new TextEncoder().encode('PETAL / millimetres / procedural mesh'));d.setUint32(80,mesh.f.length,true);let k=84;for(const face of mesh.f){const [a,b,c]=face.map(i=>mesh.v[i]),u=b.map((x,i)=>x-a[i]),v=c.map((x,i)=>x-a[i]);let norm=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]],len=Math.hypot(...norm);norm=norm.map(x=>x/(len||1));for(const x of [...norm,...a,...b,...c]){d.setFloat32(k,x,true);k+=4;}d.setUint16(k,0,true);k+=2;}return ab;}
export function volume(mesh){let sum=0;for(const [i,j,k]of mesh.f){const a=mesh.v[i],b=mesh.v[j],c=mesh.v[k];sum+=a[0]*(b[1]*c[2]-b[2]*c[1])+a[1]*(b[2]*c[0]-b[0]*c[2])+a[2]*(b[0]*c[1]-b[1]*c[0]);}return sum/6;}
export function packParts(parts,p){
 const W=p.bedX-2*p.margin,H=p.bedY-2*p.margin,gap=6,plates=[];
 const options=new Map(parts.map(part=>[part,Array.from({length:12},(_,i)=>{const yaw=i*15,b=bounds(rotateBed(part.output,yaw));return{yaw,b,w:b.size[0]+gap,h:b.size[1]+gap};}).filter(o=>o.w<=W+gap+1e-6&&o.h<=H+gap+1e-6)]));
 const items=parts.flatMap(part=>Array.from({length:part.qty},()=>part)).sort((a,b)=>Math.max(...b.dim.slice(0,2))-Math.max(...a.dim.slice(0,2)));
 for(const part of items){
  let best;
  for(let bi=0;bi<=plates.length;bi++){
   const free=bi===plates.length?[{x:0,y:0,w:W+gap,h:H+gap}]:plates[bi].free;
   for(let fi=0;fi<free.length;fi++)for(const o of options.get(part)){
    const r=free[fi];if(o.w>r.w+1e-6||o.h>r.h+1e-6)continue;
    const score=bi*1e9+o.w*o.h+.01*r.w*r.h+.001*Math.min(r.w-o.w,r.h-o.h);
    if(!best||score<best.score)best={bi,fi,o,r,score};
   }
   if(best)break;
  }
  if(!best)throw Error('No packed plate fits '+part.name);
  const {bi,fi,o,r}=best;if(bi===plates.length)plates.push({free:[r],placements:[]});
  const plate=plates[bi];plate.free.splice(fi,1);
  if(r.w-o.w>1e-6)plate.free.push({x:r.x+o.w,y:r.y,w:r.w-o.w,h:r.h});
  if(r.h-o.h>1e-6)plate.free.push({x:r.x,y:r.y+o.h,w:o.w,h:r.h-o.h});
  plate.placements.push({part,yaw:o.yaw,x:r.x-W/2-o.b.min[0],y:r.y-H/2-o.b.min[1],bounds:[r.x,r.y,o.w-gap,o.h-gap]});
 }
 const copies=new Map();for(const plate of plates)for(const x of plate.placements){const i=(copies.get(x.part.id)||0)+1;copies.set(x.part.id,i);x.copy=`${x.part.id}:${i}`;}
 return plates.map(({placements})=>({placements}));
}
// TPU springs can't share a bed with rigid parts on a single-material printer: pack them on their own plates.
export const packAll=(parts,p)=>[...packParts(parts.filter(x=>x.printIncluded!==false&&!x.flex),p),...packParts(parts.filter(x=>x.printIncluded!==false&&x.flex),p)];
export function packedPlateMesh(plate){return mergeMeshes(plate.placements.map(({part,yaw,x,y})=>{const mesh=rotateBed(part.output,yaw);return{v:mesh.v.map(v=>[v[0]+x,v[1]+y,v[2]]),f:mesh.f};}));}
export function layeredPatch(spec,p){const boxes=spec.gridBoxes||[],rr=unique([...spaced(spec.r0,spec.r1,Math.max(1,Math.ceil((spec.r1-spec.r0)/p.resolution))),...(spec.extraR||[]),...boxes.flatMap(h=>boreCuts(h,'r')).filter(r=>r>spec.r0&&r<spec.r1)]),aa=unique([...spaced(spec.a0,spec.a1,Math.max(2,Math.ceil((spec.a1-spec.a0)*spec.r1/p.resolution))),...boxes.flatMap(h=>boreCuts(h,'a')).filter(a=>a>spec.a0&&a<spec.a1)]),nr=rr.length,na=aa.length,full=Math.abs(spec.a1-spec.a0-TAU)<1e-7,nc=full?na-1:na,nl=spec.layers.length,id=(i,j,l)=>l*nr*nc+i*nc+(j%nc),groups=boreGroups(boxes),v=[];
 for(let l=0;l<=nl;l++)for(const r of rr)for(let j=0;j<nc;j++)v.push([...warpBores(r*Math.cos(aa[j]),r*Math.sin(aa[j]),groups),l]);const active=spec.layers.map(layer=>Array.from({length:nr-1},(_,i)=>Array.from({length:na-1},(_,j)=>{const r=(rr[i]+rr[i+1])/2,a=(aa[j]+aa[j+1])/2;return (!layer.regions||layer.regions.some(h=>inRect(r,a,h)))&&!(layer.exclude||[]).some(h=>inRect(r,a,h))&&!(layer.holes||[]).some(h=>inRect(r,a,h));}))),is=(i,j,l)=>{if(full)j=(j+na-1)%(na-1);return l>=0&&l<nl&&i>=0&&i<nr-1&&j>=0&&j<na-1&&active[l][i][j];},f=[];
 for(let l=0;l<nl;l++)for(let i=0;i<nr-1;i++)for(let j=0;j<na-1;j++)if(is(i,j,l)){const b=[id(i,j,l),id(i+1,j,l),id(i+1,j+1,l),id(i,j+1,l)],q=b.map(x=>x+nr*nc);if(!is(i,j,l+1))f.push([q[0],q[1],q[2]],[q[0],q[2],q[3]]);if(!is(i,j,l-1))f.push([b[2],b[1],b[0]],[b[3],b[2],b[0]]);for(const[s,t,ni,nj]of[[0,1,i,j-1],[1,2,i+1,j],[2,3,i,j+1],[3,0,i-1,j]])if(!is(ni,nj,l))f.push([q[s],b[s],b[t]],[q[s],b[t],q[t]]);}
 const split=spec.creaseLines?.length?splitLines({v,f},spec.creaseLines):{v,f},levels=[...spec.levels];if(levels[0]===null){const [mx,my]=spec.floorSlope||[0,0];let floor=Infinity;for(const[x,y]of split.v)floor=Math.min(floor,levels[1](x,y)-mx*x-my*y);floor-=spec.floorDepth*Math.hypot(1,mx,my);spec.floorPlane=[mx,my,floor];levels[0]=(x,y)=>mx*x+my*y+floor;}
 const mesh={v:split.v.map(([x,y,w])=>{const i=Math.min(nl-1,Math.floor(w)),t=w-i;return[x,y,levels[i](x,y)*(1-t)+levels[i+1](x,y)*t];}),f:split.f};return compactMesh(mesh);}
// Crease intersections can land within numerical noise of a bore-grid vertex.
// Weld those coincident points before repairing flat triangles; otherwise a
// vanishing edge can cause an endless split/repair cycle.
export function compactMesh(mesh,precision=1e6){
 const used=new Map(),positions=new Map(),v=[];
 let f=mesh.f.map(face=>face.map(i=>{
  if(!used.has(i)){
   const point=mesh.v[i],key=point.map(x=>Math.round(x*precision)).join(',');
   if(!positions.has(key)){positions.set(key,v.length);v.push(point);}
   used.set(i,positions.get(key));
  }
  return used.get(i);
 })).filter(face=>new Set(face).size===3);
 // Collapse sub-resolution connected edges across quantization-cell boundaries.
 const parent=v.map((_,i)=>i),root=i=>{while(parent[i]!==i){parent[i]=parent[parent[i]];i=parent[i];}return i;};
 for(const face of f)for(let j=0;j<3;j++){const a=face[j],b=face[(j+1)%3];if(Math.hypot(...v[a].map((x,k)=>x-v[b][k]))<Math.max(.00001,1/precision))parent[root(b)]=root(a);}
 f=f.map(face=>face.map(root)).filter(face=>new Set(face).size===3);
 try{const repaired=repairFlatTriangles({v,f}),pairs=new Map(),drop=new Set();
 // Float32 Boolean seams can leave coincident, oppositely wound zero-volume
 // triangle pairs. Cancel both, preserving the surrounding closed surface.
 repaired.f.forEach((face,i)=>{const key=[...face].sort((a,b)=>a-b).join(':');if(pairs.has(key)){const j=pairs.get(key),other=repaired.f[j],k=other.indexOf(face[0]);if(other[(k+1)%3]===face[2]){drop.add(i);drop.add(j);pairs.delete(key);}}else pairs.set(key,i);});
 const result={v:repaired.v,f:repaired.f.filter((_,i)=>!drop.has(i))},edges=new Map();
 for(const face of result.f)for(let j=0;j<3;j++){const a=face[j],b=face[(j+1)%3],key=Math.min(a,b)+':'+Math.max(a,b),e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}
 if([...edges.values()].some(([count,winding])=>count!==2||winding!==0))throw Error('Mesh has an unresolved seam. Change mesh spacing or segmentation and regenerate; no printable export was produced.');
 return removeNumericalIslands(result);}catch(e){if(precision>1e4)return compactMesh(mesh,precision/10);throw e;}
}
// Boolean intersections can leave a closed, flat tetrahedron with Float32-scale
// volume. Remove only tiny closed islands; never discard a substantive component.
function removeNumericalIslands(mesh){const parent=mesh.v.map((_,i)=>i),root=i=>{while(parent[i]!==i){parent[i]=parent[parent[i]];i=parent[i];}return i;};for(const face of mesh.f)for(const i of face)parent[root(i)]=root(face[0]);const groups=new Map();for(const face of mesh.f){const id=root(face[0]),group=groups.get(id)||{count:0,volume:0};const [a,b,c]=face.map(i=>mesh.v[i]);group.count++;group.volume+=(a[0]*(b[1]*c[2]-b[2]*c[1])+a[1]*(b[2]*c[0]-b[0]*c[2])+a[2]*(b[0]*c[1]-b[1]*c[0]))/6;groups.set(id,group);}if(groups.size<2)return mesh;const discarded=new Set([...groups].filter(([,g])=>g.count<=12&&Math.abs(g.volume)<.00001).map(([id])=>id));return discarded.size?{v:mesh.v,f:mesh.f.filter(face=>!discarded.has(root(face[0])))}:mesh;}
function repairFlatTriangles(mesh){const {v}=mesh,f=[...mesh.f],edges=new Map(),key=(a,b)=>Math.min(a,b)+':'+Math.max(a,b),add=(face,i)=>{for(let j=0;j<3;j++){const k=key(face[j],face[(j+1)%3]);if(!edges.has(k))edges.set(k,new Set());edges.get(k).add(i);}},remove=(face,i)=>{for(let j=0;j<3;j++)edges.get(key(face[j],face[(j+1)%3]))?.delete(i);},area=face=>{const [a,b,c]=face.map(i=>v[i]),u=b.map((x,k)=>x-a[k]),w=c.map((x,k)=>x-a[k]);return Math.hypot(u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]);};f.forEach(add);const queue=f.map((_,i)=>i);for(let q=0;q<queue.length;q++){const i=queue[q],face=f[i];if(!face||area(face)>1e-9)continue;if(q>mesh.f.length*4)throw Error("Mesh repair did not converge. Change the mesh spacing and regenerate.");let j=0,dist=-1;for(let k=0;k<3;k++){const d=v[face[k]].reduce((s,x,t)=>s+(x-v[face[(k+1)%3]][t])**2,0);if(d>dist){dist=d;j=k;}}const a=face[j],b=face[(j+1)%3],mid=face[(j+2)%3],adj=[...edges.get(key(a,b))].find(k=>k!==i&&f[k]);if(adj===undefined)throw Error('Cannot repair collapsed mesh edge');const other=f[adj],k=other.findIndex((x,k)=>key(x,other[(k+1)%3])===key(a,b)),u=other[k],w=other[(k+1)%3],tip=other[(k+2)%3];remove(face,i);remove(other,adj);f[i]=null;f[adj]=[u,mid,tip];add(f[adj],adj);const ni=f.length;f.push([mid,w,tip]);add(f[ni],ni);queue.push(adj,ni);}return {v,f:f.filter(Boolean)};}
// Exact triangle slices produce a piecewise-linear support envelope, including
// shallow rear seats. X breakpoints preserve the actual underside profile.

const inRect=(r,a,h)=>r>h.r0&&r<h.r1&&a>h.a0&&a<h.a1;

// Manual overrides retain a stable identity for every physical copy. Coordinates
// are the output mesh origin relative to the bed centre; only in-plane yaw changes.
export function plateRows(plates){return plates.flatMap((plate,i)=>plate.placements.map(x=>({copy:x.copy,part:x.part.id,plate:i+1,x:x.x,y:x.y,yaw:x.yaw})));}
export function manualPlates(parts,p,rows){
 const expected=new Map(parts.flatMap(part=>Array.from({length:part.qty},(_,i)=>[`${part.id}:${i+1}`,part]))),seen=new Set(),plates=[];
 if(!Array.isArray(rows)||rows.length!==expected.size)throw Error('Every printed copy must appear exactly once.');
 for(const r of rows){const part=expected.get(r.copy);if(!part||seen.has(r.copy))throw Error('Unknown or duplicate printed copy.');seen.add(r.copy);
  if(!Number.isInteger(r.plate)||r.plate<1||r.plate>expected.size)throw Error('Plate number must be from 1 to '+expected.size+'.');
  if(![r.x,r.y,r.yaw].every(Number.isFinite))throw Error('Placement coordinates and rotation must be finite numbers.');
  const yaw=((r.yaw%360)+360)%360,b=bounds(rotateBed(part.output,yaw)),W=p.bedX-2*p.margin,H=p.bedY-2*p.margin,x=b.min[0]+r.x+W/2,y=b.min[1]+r.y+H/2,w=b.size[0],h=b.size[1];
  if(x<-.00001||y<-.00001||x+w>W+.00001||y+h>H+.00001||b.max[2]>p.bedZ-2+.00001)throw Error(`${r.copy} is outside the usable print volume on plate ${r.plate}.`);
  const plate=plates[r.plate-1]??(plates[r.plate-1]={placements:[]});
  for(const q of plate.placements){const[X,Y,A,B]=q.bounds;if(!(x>=X+A+5.99999||X>=x+w+5.99999||y>=Y+B+5.99999||Y>=y+h+5.99999))throw Error(`${r.copy} overlaps the 6 mm clearance of ${q.copy} on plate ${r.plate}.`);}
  if(plate.placements.some(q=>Boolean(q.part.flex)!==Boolean(part.flex)))throw Error(`${r.copy} is a ${part.flex?'TPU':'rigid'} part; keep TPU springs on their own plates.`);
  plate.placements.push({part,copy:r.copy,yaw,x:r.x,y:r.y,bounds:[x,y,w,h]});
 }
 // Keep intentional empty plates so the requested plate numbers remain stable.
 return Array.from({length:plates.length},(_,i)=>plates[i]||{placements:[]});
}
