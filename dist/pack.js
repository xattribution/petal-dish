// Plate packing. Every part stands on the bed in its print orientation. Its space on a plate is described per 4 mm
// height band by a footprint on a grid, grown by half the gap between parts (p.plateGap, 6 mm by default). Two parts clash only where their footprints meet in
// the same band, so parts that lean the same way, like side-printed petals, can nest ("spoon") far closer than their
// bounding boxes allow. Identical parts are packed as nested stacks; everything else as rectangles (guillotine cut).
// Plate coordinates: the usable area (bed less the margin) is W × H with its corner at 0; a placement's x, y move the
// part's yaw-rotated print mesh so that the bed center is the origin, as before.
export const PACK={gap:6,band:4};
export const gapOf=p=>p?.plateGap??PACK.gap;
const EPS=1e-6,PI=Math.PI;
function boundsOf(v){const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];for(const q of v)for(let k=0;k<3;k++){if(q[k]<min[k])min[k]=q[k];if(q[k]>max[k])max[k]=q[k];}return{min,max,size:max.map((x,k)=>x-min[k])};}
function usedVertices(mesh){const used=new Uint8Array(mesh.v.length);for(const f of mesh.f)for(const i of f)used[i]=1;return mesh.v.filter((_,i)=>used[i]);}
const turn=(v,yaw)=>{const c=Math.cos(yaw*PI/180),s=Math.sin(yaw*PI/180);return v.map(([x,y,z])=>[c*x-s*y,s*x+c*y,z]);};
// Grid cell (mm): 1 mm up to a 320 mm bed, then 1.5 mm up to 480 mm and 3 mm beyond, to bound the work. The default
// gap (6 mm) is a whole number of cells at each size, so its half-gap growth is exact; other gaps round up.
export const packCell=p=>{const m=Math.max(p.bedX,p.bedY);return m<=320?1:m<=480?1.5:3;};
const usable=p=>({W:p.bedX-2*p.margin,H:p.bedY-2*p.margin});
const cellRange=(a,b)=>{const i=Math.floor(a+EPS);return[i,Math.max(i,Math.ceil(b-EPS)-1)];};

// Footprint of a mesh turned by `yaw` degrees on a grid of `c` mm, dilated by half the gap, per height band.
// rows[k][j]: Int16Array of [x0, x1] cell pairs (inclusive) for band k, grid row j. Cell (0,0) has its corner at
// (b.min - r·c) in the turned mesh frame. pitch[0|1]: the shift in cells along x or y at which two copies stop clashing.
const footprints=new WeakMap();
export function footprint(mesh,yaw,c,gap=PACK.gap){
 let byKey=footprints.get(mesh);if(!byKey)footprints.set(mesh,byKey=new Map());const key=yaw+':'+c+':'+gap;if(byKey.has(key))return byKey.get(key);
 const all=turn(mesh.v,yaw),b=boundsOf(usedVertices({v:all,f:mesh.f})),h=PACK.band,r=Math.ceil(gap/2/c-EPS);
 const nx0=Math.max(1,Math.ceil(b.size[0]/c-EPS)),ny0=Math.max(1,Math.ceil(b.size[1]/c-EPS)),nb=Math.max(1,Math.ceil(b.size[2]/h-EPS)),nx=nx0+2*r,ny=ny0+2*r;
 const V=all.map(([x,y,z])=>[(x-b.min[0])/c+r,(y-b.min[1])/c+r,z-b.min[2]]),grids=Array.from({length:nb},()=>new Uint8Array(nx*ny));
 const clipZ=(poly,z,above)=>{const out=[];for(let i=0;i<poly.length;i++){const P=poly[i],Q=poly[(i+1)%poly.length],pi=above?P[2]>=z:P[2]<=z,qi=above?Q[2]>=z:Q[2]<=z;if(pi)out.push(P);if(pi!==qi){const t=(z-P[2])/(Q[2]-P[2]);out.push([P[0]+t*(Q[0]-P[0]),P[1]+t*(Q[1]-P[1]),z]);}}return out;};
 // a convex polygon's cells, row by row: the x range of the polygon inside each row strip
 const fill=(g,poly)=>{let y0=Infinity,y1=-Infinity;for(const q of poly){if(q[1]<y0)y0=q[1];if(q[1]>y1)y1=q[1];}const[r0,r1]=cellRange(y0,y1);
  for(let j=Math.max(0,r0);j<=Math.min(ny-1,r1);j++){let a=Infinity,e=-Infinity;
   for(let i=0;i<poly.length;i++){const P=poly[i],Q=poly[(i+1)%poly.length],dy=Q[1]-P[1];let t0=0,t1=1;
    if(Math.abs(dy)<1e-12){if(P[1]<j-EPS||P[1]>j+1+EPS)continue;}else{let ta=(j-P[1])/dy,tb=(j+1-P[1])/dy;if(ta>tb)[ta,tb]=[tb,ta];t0=Math.max(0,ta);t1=Math.min(1,tb);if(t0>t1+EPS)continue;}
    const xa=P[0]+t0*(Q[0]-P[0]),xb=P[0]+t1*(Q[0]-P[0]);if(xa<a)a=xa;if(xb<a)a=xb;if(xa>e)e=xa;if(xb>e)e=xb;}
   if(a>e)continue;const[c0,c1]=cellRange(a,e);for(let x=Math.max(0,c0);x<=Math.min(nx-1,c1);x++)g[j*nx+x]=1;}};
 // surfaces inside each band, clipped to it; each face is also listed under the band middles it crosses
 const crossing=Array.from({length:nb},()=>[]);
 for(const f of mesh.f){const T=[V[f[0]],V[f[1]],V[f[2]]],zmin=Math.min(T[0][2],T[1][2],T[2][2]),zmax=Math.max(T[0][2],T[1][2],T[2][2]);
  for(let k=Math.max(0,Math.floor(zmin/h));k<=Math.min(nb-1,Math.floor(zmax/h));k++){const mid=(k+.5)*h;if(zmin<mid&&zmax>mid)crossing[k].push(T);
   // a face wholly inside the band needs no clipping
   if(zmin>=k*h&&zmax<=(k+1)*h){fill(grids[k],T);continue;}let poly=clipZ(T,k*h,true);if(!poly.length)continue;poly=clipZ(poly,(k+1)*h,false);if(poly.length)fill(grids[k],poly);}}
 // solid interiors: the cross-section at each band's middle, filled even-odd at row centers
 for(let k=0;k<nb;k++){const rows=Array.from({length:ny},()=>[]),z=(k+.5)*h;
  for(const T of crossing[k]){const pts=[];for(let i=0;i<3;i++){const P=T[i],Q=T[(i+1)%3];if((P[2]-z)*(Q[2]-z)<0){const t=(z-P[2])/(Q[2]-P[2]);pts.push([P[0]+t*(Q[0]-P[0]),P[1]+t*(Q[1]-P[1])]);}}
   if(pts.length!==2)continue;const[A,B]=pts,lo=Math.min(A[1],B[1]),hi=Math.max(A[1],B[1]);
   for(let j=Math.max(0,Math.ceil(lo-.5));j<=Math.min(ny-1,Math.floor(hi-.5));j++){const yc=j+.5;if(yc<lo||yc>=hi)continue;rows[j].push(A[0]+(yc-A[1])/(B[1]-A[1])*(B[0]-A[0]));}}
  for(let j=0;j<ny;j++){const xs=rows[j].sort((p,q)=>p-q);for(let i=0;i+1<xs.length;i+=2){const[c0,c1]=cellRange(xs[i],xs[i+1]);for(let x=Math.max(0,c0);x<=Math.min(nx-1,c1);x++)grids[k][j*nx+x]=1;}}}
 // grow by r cells (square), then store runs and the stacking pitch
 const tmp=new Uint8Array(nx*ny),rows=[],pitch=[0,0];
 for(const g of grids){tmp.fill(0);
  for(let j=0;j<ny;j++){let last=-Infinity;for(let x=0;x<nx;x++){if(g[j*nx+x])last=x;if(x-last<=r)tmp[j*nx+x]=1;}last=Infinity;for(let x=nx-1;x>=0;x--){if(g[j*nx+x])last=x;if(last-x<=r)tmp[j*nx+x]=1;}}
  g.fill(0);for(let x=0;x<nx;x++){let last=-Infinity;for(let j=0;j<ny;j++){if(tmp[j*nx+x])last=j;if(j-last<=r)g[j*nx+x]=1;}last=Infinity;for(let j=ny-1;j>=0;j--){if(tmp[j*nx+x])last=j;if(last-j<=r)g[j*nx+x]=1;}}
  const band=[];for(let j=0;j<ny;j++){const runs=[];for(let x=0;x<nx;x++){if(!g[j*nx+x])continue;let e=x;while(e+1<nx&&g[j*nx+e+1])e++;runs.push(x,e);x=e;}band.push(runs.length?Int16Array.from(runs):null);if(runs.length)pitch[0]=Math.max(pitch[0],runs[runs.length-1]-runs[0]+1);}
  for(let x=0;x<nx;x++){let lo=-1,hi=-1;for(let j=0;j<ny;j++)if(g[j*nx+x]){if(lo<0)lo=j;hi=j;}if(lo>=0)pitch[1]=Math.max(pitch[1],hi-lo+1);}
  rows.push(band);}
 const fp={b,c,r,nx,ny,nb,rows,pitch};byKey.set(key,fp);return fp;
}
// Cell origin of a placement's footprint on the plate grid.
const origin=(fp,x,y,W,H)=>[Math.round((x+fp.b.min[0]+W/2)/fp.c)-fp.r,Math.round((y+fp.b.min[1]+H/2)/fp.c)-fp.r];
// Do two placed footprints meet in any shared band? (ax, ay) and (bx, by) are their cell origins.
export function clash(A,ax,ay,B,bx,by){
 if(ax>=bx+B.nx||bx>=ax+A.nx||ay>=by+B.ny||by>=ay+A.ny)return false;
 const nb=Math.min(A.nb,B.nb),j0=Math.max(ay,by),j1=Math.min(ay+A.ny,by+B.ny);
 for(let k=0;k<nb;k++){const RA=A.rows[k],RB=B.rows[k];for(let J=j0;J<j1;J++){const ra=RA[J-ay],rb=RB[J-by];if(!ra||!rb)continue;
  let i=0,m=0;while(i<ra.length&&m<rb.length){const a0=ra[i]+ax,a1=ra[i+1]+ax,b0=rb[m]+bx,b1=rb[m+1]+bx;if(a1<b0)i+=2;else if(b1<a0)m+=2;else return true;}}}
 return false;
}
const snap=(v,c)=>Math.ceil(v/c-EPS)*c;
// Rectangle packing (guillotine cut, best area fit). Each item offers options {w, h, ew, eh, place(x0, y0)}: w and h are
// its size plus the gap rounded up to whole cells, which keeps every corner on the grid; ew and eh are the exact size plus
// the gap. An item fits a free rectangle when its exact size does, so the last one in a row may use the part of a cell the
// rounding would waste, and nothing reaches past the usable area (W + gap with the trailing gap).
function guillotine(items,W,H,c,gap){
 const plates=[],Wg=W+gap,Hg=H+gap;
 for(const item of items){let best;
  for(let bi=0;bi<=plates.length;bi++){const free=bi===plates.length?[{x:0,y:0,w:Wg,h:Hg}]:plates[bi].free;
   for(let fi=0;fi<free.length;fi++)for(const o of item.options){const r=free[fi];if(o.ew>r.w+EPS||o.eh>r.h+EPS)continue;
    const score=bi*1e9+o.w*o.h+.01*r.w*r.h+.001*Math.min(r.w-o.w,r.h-o.h);if(!best||score<best.score)best={bi,fi,o,r,score};}
   if(best)break;}
  if(!best)throw Error('No packed plate fits '+item.name);
  const{bi,fi,o,r}=best;if(bi===plates.length)plates.push({free:[r],placements:[]});const plate=plates[bi];plate.free.splice(fi,1);
  if(r.w-o.w>EPS)plate.free.push({x:r.x+o.w,y:r.y,w:r.w-o.w,h:r.h});if(r.h-o.h>EPS)plate.free.push({x:r.x,y:r.y+o.h,w:Math.min(o.w,r.w),h:r.h-o.h});
  plate.placements.push(...o.place(r.x,r.y));}
 return plates.map(({placements})=>({placements}));
}
// One copy at (x0, y0) in usable-area coordinates (the corner of its turned bounding box).
const placeAt=(part,yaw,b,x0,y0,W,H)=>({part,yaw,x:x0-W/2-b.min[0],y:y0-H/2-b.min[1],bounds:[x0,y0,b.size[0],b.size[1]]});
const singles=new WeakMap();
function singleItem(part,p,c){const{W,H}=usable(p),gap=gapOf(p),key=W+':'+H+':'+c+':'+gap,memo=singles.get(part.output)?.[key];if(memo)return{name:part.name,size:Math.max(...part.dim.slice(0,2)),options:memo.map(o=>({w:o.w,h:o.h,ew:o.b.size[0]+gap,eh:o.b.size[1]+gap,place:(x,y)=>[placeAt(part,o.yaw,o.b,x,y,W,H)]}))};
 const options=Array.from({length:12},(_,i)=>{const yaw=i*15,b=boundsOf(usedVertices({v:turn(part.output.v,yaw),f:part.output.f}));return{yaw,b,w:snap(b.size[0]+gap,c),h:snap(b.size[1]+gap,c)};})
  .filter(o=>o.b.size[0]<=W+EPS&&o.b.size[1]<=H+EPS);
 singles.set(part.output,{...singles.get(part.output),[key]:options});return singleItem(part,p,c);}
// Extent of a footprint across the stacking axis, per band and per line along it: lo/hi cell (-1 where empty).
function profile(fp,axis){const key='prof'+axis;if(fp[key])return fp[key];const n=axis?fp.nx:fp.ny,lo=new Int16Array(fp.nb*n).fill(-1),hi=new Int16Array(fp.nb*n).fill(-1);
 fp.rows.forEach((band,k)=>band.forEach((runs,j)=>{if(!runs)return;if(axis){for(let i=0;i<runs.length;i+=2)for(let x=runs[i];x<=runs[i+1];x++){const q=k*n+x;if(lo[q]<0)lo[q]=j;hi[q]=j;}}else{const q=k*n+j;lo[q]=runs[0];hi[q]=runs[runs.length-1];}}));
 return fp[key]={lo,hi,n};}
// Shift (cells) along `axis` at which B, placed after A with their bounding-box corners aligned across the axis, stops
// clashing with A.
function pitchBetween(A,B,axis){const a=profile(A,axis),b=profile(B,axis);let t=1;
 for(let k=0;k<Math.min(A.nb,B.nb);k++)for(let i=0;i<Math.min(a.n,b.n);i++){const h=a.hi[k*a.n+i],l=b.lo[k*b.n+i];if(h>=0&&l>=0&&h-l+1>t)t=h-l+1;}
 return t;}
// Nested stacks: copies of one part, or of parts with the same shape (a petal and its rod-mount variant), shifted along
// x or y by the largest pitch between any two members, at yaw 0 or 90.
function stackOptions(members,p,c){const{W,H}=usable(p),gap=gapOf(p),out=[];
 for(const yaw of [0,90]){const fps=members.map(part=>footprint(part.output,yaw,c,gap)),b=fps[0].b;
  for(const axis of [0,1]){let t=0;for(const A of fps)for(const B of fps)t=Math.max(t,pitchBetween(A,B,axis));t*=c;
   const len=Math.max(...fps.map(fp=>fp.b.size[axis])),other=Math.max(...fps.map(fp=>fp.b.size[1-axis])),room=axis?H:W,otherRoom=axis?W:H;
   if(other>otherRoom+EPS||len>room+EPS)continue;const kmax=1+Math.floor((room-len)/t+EPS);if(kmax<2||t>=len+gap-EPS)continue;
   out.push({yaw,axis,t,len,other,kmax,b});}}
 return out;}
function stackItem(copies,o,p,c){const{W,H}=usable(p),gap=gapOf(p),k=copies.length,span=o.len+(k-1)*o.t,ew=(o.axis?o.other:span)+gap,eh=(o.axis?span:o.other)+gap,w=snap(ew,c),h=snap(eh,c);
 return{name:copies[0].name,size:Math.max(w,h),options:[{w,h,ew,eh,place:(x,y)=>copies.map((part,i)=>{const b=footprint(part.output,o.yaw,c,gap).b;return placeAt(part,o.yaw,b,x+(o.axis?0:i*o.t),y+(o.axis?i*o.t:0),W,H);})}]};}
const byQty=parts=>parts.flatMap(part=>Array.from({length:part.qty},()=>part));
function finish(plates){const copies=new Map();for(const plate of plates)for(const x of plate.placements){const i=(copies.get(x.part.id??x.part.name)||0)+1;copies.set(x.part.id??x.part.name,i);x.copy=`${x.part.id??x.part.name}:${i}`;}return plates;}
// Automatic packing: rectangles of single parts, or nested stacks of repeated parts when that needs fewer plates.
// `c` overrides the grid cell (plan() ranks layouts on a 2 mm grid, then confirms the winner on the normal one).
export function packParts(parts,p,c=packCell(p)){
 const{W,H}=usable(p),sort=items=>items.sort((a,b)=>b.size-a.size);
 const plain=guillotine(sort(byQty(parts).map(part=>singleItem(part,p,c))),W,H,c,gapOf(p));
 // stackable groups: same kind and row, and the same printed size to the millimeter
 const groups=new Map();for(const part of parts){if(!part.qty)continue;const key=[part.kind,part.row,...part.dim.map(Math.round)].join(':');groups.set(key,[...(groups.get(key)||[]),part]);}
 const stackable=[...groups.values()].filter(g=>g.reduce((n,part)=>n+part.qty,0)>1).map(members=>{const opts=stackOptions(members,p,c);if(!opts.length)return null;
  // the option that gives each copy the least plate area at its fullest stack
  const best=opts.map(o=>({...o,score:(o.len+(o.kmax-1)*o.t+gapOf(p))*(o.other+gapOf(p))/o.kmax})).sort((a,b)=>a.score-b.score)[0];return{members,o:best};}).filter(Boolean);
 if(!stackable.length)return finish(plain);
 const inStack=new Set(stackable.flatMap(g=>g.members)),kmax=Math.max(...stackable.map(g=>g.o.kmax));let best=plain;
 for(let cap=kmax;cap>=2&&cap>kmax-6;cap--)for(const balanced of [true,false]){
  const items=byQty(parts.filter(part=>!inStack.has(part))).map(part=>singleItem(part,p,c));
  for(const{members,o}of stackable){const copies=byQty(members),q=copies.length,k=Math.min(cap,o.kmax),n=Math.ceil(q/k);let at=0;
   for(let i=0;i<n;i++){const size=balanced?Math.floor(q/n)+(i<q%n?1:0):Math.min(k,q-i*k),chunk=copies.slice(at,at+size);at+=size;items.push(size>1?stackItem(chunk,o,p,c):singleItem(chunk[0],p,c));}}
  let plates;try{plates=guillotine(sort(items),W,H,c,gapOf(p));}catch{continue;}
  if(plates.length<best.length)best=plates;}
 return finish(best);
}
// TPU springs can't share a bed with rigid parts on a single-material printer: pack them on their own plates.
export const packAll=(parts,p)=>[...packParts(parts.filter(x=>x.printIncluded!==false&&!x.flex),p),...packParts(parts.filter(x=>x.printIncluded!==false&&x.flex),p)];
export function plateRows(plates){return plates.flatMap((plate,i)=>plate.placements.map(x=>({copy:x.copy,part:x.part.id,plate:i+1,x:x.x,y:x.y,yaw:x.yaw})));}
// Check one placement against the bed and against the others on its plate.
function placed(part,yaw,x,y,p,trusted){const{W,H}=usable(p),c=packCell(p),fp=trusted?null:footprint(part.output,yaw,c,gapOf(p)),b=fp?fp.b:boundsOf(usedVertices({v:turn(part.output.v,yaw),f:part.output.f})),x0=b.min[0]+x+W/2,y0=b.min[1]+y+H/2;
 return{fp,o:fp&&origin(fp,x,y,W,H),inside:x0>=-1e-5&&y0>=-1e-5&&x0+b.size[0]<=W+1e-5&&y0+b.size[1]<=H+1e-5&&b.size[2]<=p.bedZ-2+1e-5,bounds:[x0,y0,b.size[0],b.size[1]]};}
// Manual arrangement: validate rows {copy, plate, x, y, yaw} and build plates. `trusted` skips the clearance check, for
// rows the engine has already checked.
export function manualPlates(parts,p,rows,trusted=false){
 const expected=new Map(parts.flatMap(part=>Array.from({length:part.qty},(_,i)=>[`${part.id}:${i+1}`,part]))),seen=new Set(),plates=[];
 if(!Array.isArray(rows)||rows.length!==expected.size)throw Error('Every printed copy must appear exactly once.');
 for(const r of rows){const part=expected.get(r.copy);if(!part||seen.has(r.copy))throw Error('Unknown or duplicate printed copy.');seen.add(r.copy);
  if(!Number.isInteger(r.plate)||r.plate<1||r.plate>expected.size)throw Error('Plate number must be from 1 to '+expected.size+'.');
  if(![r.x,r.y,r.yaw].every(Number.isFinite))throw Error('Placement coordinates and rotation must be finite numbers.');
  const yaw=((r.yaw%360)+360)%360;let{x,y}=r;
  // typed positions move to the nearest grid cell (at most half a cell), so the clearance check below is exact
  if(!trusted){const{W,H}=usable(p),c=packCell(p),b=footprint(part.output,yaw,c,gapOf(p)).b,on=(v,m)=>{const g=Math.round((v+m)/c)*c-m;return Math.abs(g-v)<1e-6?v:g;};x=on(x,b.min[0]+W/2);y=on(y,b.min[1]+H/2);}
  const here=placed(part,yaw,x,y,p,trusted);
  if(!here.inside)throw Error(`${r.copy} is outside the usable print volume on plate ${r.plate}.`);
  const plate=plates[r.plate-1]??(plates[r.plate-1]={placements:[]});
  if(!trusted)for(const q of plate.placements)if(clash(here.fp,...here.o,q.fp,...q.o))throw Error(`${r.copy} overlaps the ${gapOf(p)} mm clearance of ${q.copy} on plate ${r.plate}.`);
  if(plate.placements.some(q=>Boolean(q.part.flex)!==Boolean(part.flex)))throw Error(`${r.copy} is a ${part.flex?'TPU':'rigid'} part; keep TPU springs on their own plates.`);
  plate.placements.push({part,copy:r.copy,yaw,x,y,bounds:here.bounds,fp:here.fp,o:here.o});}
 // Keep intentional empty plates so the requested plate numbers remain stable.
 return Array.from({length:plates.length},(_,i)=>plates[i]||{placements:[]}).map(plate=>({placements:plate.placements.map(({fp,o,...x})=>x)}));
}
// Move one printed copy onto plate `target` (0-based; plates.length means a new plate), as near the point (hx, hy)
// (bed-centered mm) as it fits, keeping every other placement. Tries the copy's own rotation, then turned 180° and 90°,
// then the others in 15° steps, within a time budget (ms). If no spot is free, the target plate is repacked with the copy added, as long as it all still fits on one
// plate. Returns the new plates, or null when the copy does not fit there.
export function movePlacement(plates,copy,target,hx,hy,p,budget=1500){
 if(!Number.isInteger(target)||target<0||target>plates.length||!Number.isFinite(hx)||!Number.isFinite(hy))return null;
 const{W,H}=usable(p),c=packCell(p),from=plates.findIndex(pl=>pl.placements.some(x=>x.copy===copy)),start=Date.now();if(from<0)return null;
 const moving=plates[from].placements.find(x=>x.copy===copy),others=target<plates.length?plates[target].placements.filter(x=>x.copy!==copy):[];
 if(others.some(q=>Boolean(q.part.flex)!==Boolean(moving.part.flex)))return null;
 const Wc=Math.floor(W/c+EPS),Hc=Math.floor(H/c+EPS),occupied=new Map();
 // per band: prefix sums along each row of the cells the other parts' grown footprints cover
 const bandSums=k=>{if(occupied.has(k))return occupied.get(k);const g=new Uint8Array(Wc*Hc);
  for(const q of others){const fp=footprint(q.part.output,q.yaw,c,gapOf(p)),[ox,oy]=origin(fp,q.x,q.y,W,H);if(k>=fp.nb)continue;
   fp.rows[k].forEach((runs,j)=>{const Y=oy+j;if(!runs||Y<0||Y>=Hc)return;for(let i=0;i<runs.length;i+=2)for(let X=Math.max(0,runs[i]+ox);X<=Math.min(Wc-1,runs[i+1]+ox);X++)g[Y*Wc+X]=1;});}
  const s=new Int32Array((Wc+1)*Hc);for(let Y=0;Y<Hc;Y++)for(let X=0;X<Wc;X++)s[Y*(Wc+1)+X+1]=s[Y*(Wc+1)+X]+g[Y*Wc+X];occupied.set(k,s);return s;};
 const maxBand=Math.max(0,...others.map(q=>footprint(q.part.output,q.yaw,c,gapOf(p)).nb));
 const fits=(fp,ox,oy)=>{for(let k=0;k<Math.min(fp.nb,maxBand);k++){const s=bandSums(k);for(let j=0;j<fp.ny;j++){const runs=fp.rows[k][j],Y=oy+j;if(!runs||Y<0||Y>=Hc)continue;
  for(let i=0;i<runs.length;i+=2){const x0=Math.max(0,runs[i]+ox),x1=Math.min(Wc-1,runs[i+1]+ox);if(x1>=x0&&s[Y*(Wc+1)+x1+1]-s[Y*(Wc+1)+x0]>0)return false;}}}return true;};
 // its own rotation, then turned half way round, then a quarter either way, then the rest in 15° steps
 const yaws=[...new Set([0,180,90,270,...Array.from({length:24},(_,i)=>i*15)].map(d=>(moving.yaw+d)%360))];
 for(const yaw of yaws){if(Date.now()-start>budget)break;const fp=footprint(moving.part.output,yaw,c,gapOf(p)),b=fp.b;if(b.size[2]>p.bedZ-2+EPS||b.size[0]>W+EPS||b.size[1]>H+EPS)continue;
  // candidate corners on the cell grid, nearest the drop point first
  const want=[Math.round((hx+W/2-b.size[0]/2)/c),Math.round((hy+H/2-b.size[1]/2)/c)],xmax=Math.floor((W-b.size[0])/c+EPS),ymax=Math.floor((H-b.size[1])/c+EPS);if(xmax<0||ymax<0)continue;
  const step=yaw===moving.yaw?1:2,nx=Math.floor(xmax/step)+1,ny=Math.floor(ymax/step)+1,dist=new Float64Array(nx*ny),order=new Uint32Array(nx*ny);
  for(let i=0;i<nx;i++)for(let j=0;j<ny;j++){dist[i*ny+j]=(i*step-want[0])**2+(j*step-want[1])**2;order[i*ny+j]=i*ny+j;}order.sort((a,b)=>dist[a]-dist[b]);
  for(let n=0;n<order.length;n++){const X=Math.floor(order[n]/ny)*step,Y=order[n]%ny*step;if(!(n&1023)&&Date.now()-start>budget)break;if(!fits(fp,X-fp.r,Y-fp.r))continue;const x=X*c-W/2-b.min[0],y=Y*c-H/2-b.min[1];return relocate(plates,from,target,moving,{...moving,yaw,x,y,bounds:[X*c,Y*c,b.size[0],b.size[1]]});}}
 // no free spot: repack the target plate with the copy added
 try{const group=new Map();for(const q of [...others,moving])group.set(q.part,(group.get(q.part)||0)+1);
  const packed=packParts([...group].map(([part,qty])=>({...part,qty,output:part.output,id:part.id,name:part.name,part})),p);
  if(packed.length===1){const ids=new Map();const placements=packed[0].placements.map(x=>{const real=x.part.part;const pool=ids.get(real)??ids.set(real,[...others,moving].filter(q=>q.part===real).map(q=>q.copy)).get(real);return{...x,part:real,copy:pool.shift()};});
   return relocate(plates,from,target,moving,null,placements);}}catch{}
 return null;
}
function relocate(plates,from,target,moving,next,replace){
 const out=plates.map(pl=>({placements:pl.placements.filter(x=>x.copy!==moving.copy)}));
 if(target>=out.length)out.push({placements:[]});
 if(replace)out[target].placements=replace;else out[target].placements.push(next);
 return out.filter(pl=>pl.placements.length);
}
