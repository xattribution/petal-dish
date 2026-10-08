import {jointKey,seamChoice,rootChoice,mountChoice} from './connections.js';
import {patch,bounds,printMesh,rotateBed,packAll,packParts,zAt} from './mesh.js';
import {solidScope,solid,cylinder,loft,sphere,prism} from './solid.js';
import {defaults,limits,CLIP,clipOuter,boltY,usesClips,usesLevers,seamHoles,SEAM_BOLT,seamBolt,clipStrain,JOINT,validate,FASTENER,insertPilot,stockLength,stockDown} from './params.js';
export {defaults,limits,CLIP,boltY,usesClips,usesLevers,seamHoles,SEAM_BOLT,seamBolt,clipStrain,JOINT,validate} from './params.js';
import {appendMount} from './mount.js';
import {affine,rz} from './scene.js';
import {feedFits,petalRodSocket,appendFeedParts} from './feed.js';
import {leverMeshes} from './lever-meshes.js';
export {binarySTL,volume,packedPlateMesh,bounds,printMesh,rotateBed,zAt,packAll} from './mesh.js';
export const ROOT={seatR:4.8,seatDepth:5};
// Root and hub-to-mount fastener geometry for the selected sizes (M4 gives the revision-12 interface exactly).
export const rootHole=p=>{const F=FASTENER[p.rootBolt];return{r:F.clear/2,seatR:F.washer/2+.3,hubR:F.clear/2+.05,pilot:insertPilot(p,p.rootBolt),depth:F.insert+1};};
export const mountHole=p=>{const F=FASTENER[p.mountBolt];return{r:F.clear/2+.05,seatR:F.washer/2+.5,pilot:insertPilot(p,p.mountBolt),depth:F.insert+1};};
// The detent axis sits outside the wall so the clip's bump reaches exactly the detent depth into the wall.
const detentX=d=>CLIP.wall+CLIP.detentR-d;
export function clipSolid(fit,detent,map){const C=CLIP,a0=5-fit,parts=[],ext=poly=>loft([-C.width/2,C.width/2].map(U=>poly.map(([X,Y])=>map(X,Y,U))));
 for(const g of [1,-1]){const m=pts=>pts.map(([X,Y])=>[g*X,Y]);
  parts.push(ext(m([[a0,C.floor],[clipOuter(a0,C.floor),C.floor],[clipOuter(a0,C.top),C.top],[a0,C.top]])));
  parts.push(ext(m([[a0+.02,C.floor+C.fillet],[a0+.02,C.floor-.02],[a0-C.fillet,C.floor-.02]])));}
 const b=Y=>clipOuter(a0,Y);parts.push(ext([[-b(C.floor),C.floor],[b(C.floor),C.floor],[b(C.bottom),C.bottom],[-b(C.bottom),C.bottom]]));
 for(const g of [1,-1]){const X=g*(a0+C.detentR-detent);parts.push(cylinder(map(X,C.detentY,-C.width/2),map(X,C.detentY,C.width/2),C.detentR,64).intersect(ext([[g*(a0-detent-.1),C.detentY-C.detentR-.1],[g*(a0+.3),C.detentY-C.detentR-.1],[g*(a0+.3),C.detentY+C.detentR+.1],[g*(a0-detent-.1),C.detentY+C.detentR+.1]])));}
 return parts.reduce((x,y)=>x.union(y));}
// Station frame from the exported flange spec: (X across the seam, Y toward the shell, U along the seam) -> dish coordinates.
export function stationFrame(f,i){const{o,e,v}=f.frame,s=f.stations[i],z=f.levels[i],a=f.tilts[i],c=Math.cos(a),sn=Math.sin(a);return(X,Y,U)=>{const ss=s+U*c-Y*sn;return[o[0]+ss*e[0]+X*v[0],o[1]+ss*e[1]+X*v[1],z+U*sn+Y*c];};}
const PI=Math.PI,TAU=2*PI;
export function backZ(x,y,p){if(!p.rearStyle)return zAt(Math.hypot(x,y),p)-p.thickness;const q=p.facetSize,X=Math.round(x/q)*q,Y=Math.round(y/q)*q;return(2*X*x+2*Y*y-X*X-Y*Y)/(4*p.diameter*p.fd)-p.thickness;}
export const rootBottom=p=>zAt(45,p)-10;
// Optional flat hub front: easier to print, and it is in the feed's shadow. It sits level with the highest petal edge,
// at the hub's corners (r 45 / cos(π/n)), so the hub is never below a petal. Along each edge, where the petals are lower,
// a chamfer runs down to meet them (see hubTop). Curved follows the parabola.
// Mount seats are 5 mm below the lowest point of the front around them.
export const hubFace=(p,n)=>p.hubFlat?zAt(45/Math.cos(PI/n),p):zAt(45,p);
// Through-bolt seats on the dish front. Recessed: 5 mm below the surface. Plain: a flat spot face at the lowest point
// under the washer, so it only cuts where the surface slopes (nothing on the flat hub).
export const hubSeatFloor=(p,n)=>(p.hubFlat?hubFace(p,n):zAt(30-mountHole(p).seatR,p))-(p.mountSeat?0:5);
export const rootSeatFloor=p=>zAt(52.5-rootHole(p).seatR,p)-(p.rootSeat?0:ROOT.seatDepth);
export const hubMountGrip=(p,n)=>hubSeatFloor(p,n)-(rootBottom(p)-6);
// Hub-to-mount through bolt, before the adapter plate: from the hub rear to the nut, which sits in the front seat or, with
// a collector, on top of the mast foot's flange (the foot rests on the hub front). Plus a washer under the head and one
// under the nut, the nut, and 1.5 mm of thread past it. The adapter plate's thickness is added by the user.
const WASHER_T={3:.5,4:.8,5:1,6:1.6};
export const mountGrip=m=>{const t=m.feed?.mast;return t?t.footZ+t.flange-(rootBottom(m.p)-6):hubMountGrip(m.p,m.layout.n);};
export const mountScrew=m=>Math.ceil(mountGrip(m)+2*WASHER_T[m.p.mountBolt]+FASTENER[m.p.mountBolt].nutH+1.5);
// Flat hub front with edge chamfers: level at H inside, falling linearly over `w` to the petal edge height at each edge.
// Hub edge line: the petals' inner edge (45) less half the gap and the optional hub clearance (hubFloat). The chamfer
// reaches the petal's edge height exactly at the hub edge.
export const hubEdge=p=>45-p.gap/2-(p.hubFloat||0);
export function hubTop(p,n){const H=hubFace(p,n),w=Math.max(2,H-zAt(45,p)),e=hubEdge(p),c=Array.from({length:n},(_,i)=>[Math.cos(i*TAU/n),Math.sin(i*TAU/n)]);
 return{H,w,lines:c.map(([x,y])=>[x,y,e-w]),fn:(x,y)=>{let k=0,best=-Infinity;for(let i=0;i<n;i++){const s=x*c[i][0]+y*c[i][1];if(s>best){best=s;k=i;}}
  const d=e-best;if(d>=w)return H;const t=-x*c[k][1]+y*c[k][0],edge=zAt(Math.hypot(45,t),p);return edge+(H-edge)*Math.max(0,d)/w;}};}
export function rowBounds(p,n,rows,j){const end=p.diameter/2*Math.cos(PI/n);return[45+(end-45)*j/rows,j===rows-1?Infinity:45+(end-45)*(j+1)/rows];}
export function sidePrint(mesh,n){const h=PI/n,c=Math.cos(h),s=Math.sin(h);return printMesh({v:mesh.v.map(([x,y,z])=>[c*x-s*y,-z,s*x+c*y]),f:mesh.f});}
function chooseBed(mesh,p){for(let yaw=0;yaw<180;yaw+=15){const m=rotateBed(mesh,yaw),dim=bounds(m).size;if(dim[0]<=p.bedX-2*p.margin&&dim[1]<=p.bedY-2*p.margin&&dim[2]<=p.bedZ-2)return{mesh:m,dim,yaw};}return null;}
// Segmentation. Automatic petal count and rings either use the largest petals that fit the bed (fewest pieces and
// seams; segmentGoal 0) or the layout that packs onto the fewest shared print beds (segmentGoal 1), which may use more,
// smaller petals. Plate counts are estimated from each ring's side-printed footprint plus the hub.
export function petalFootprints(p,n,rows){if(!feedFits(p,n,rows))return null;const h=PI/n,R=p.diameter/2,span=(R-45/Math.cos(h))/rows;if(span<66||45*Math.tan(h)<8.5)return null;const result=[];
 for(let j=0;j<rows;j++){const[a,b]=rowBounds(p,n,rows,j),r1=Math.min(R,b/Math.cos(h));if(rows>1&&j<rows-1&&(p.staggerRings?2*b*Math.tan(h/2)<36:2*b*Math.tan(h)<62))return null;const spec={r0:Math.max(1,a-2),r1,a0:-h,a1:h,backFn:(x,y)=>backZ(x,y,p)-17};
  // inner edge as clipped() cuts it: with staggered rings an outer petal's inner edge is a V that reaches r = a
  const m=solidScope(()=>{let s=solid(patch(spec,p));if(p.staggerRings&&a>45.001){let inner=cylinder([0,0,-100],[0,0,p.diameter],p.diameter,32);for(const t of [-h/2,h/2])inner=inner.trim([-Math.cos(t),-Math.sin(t),0],-a);s=s.subtract(inner);}else s=s.trim([1,0,0],a);return s.rawMesh();});const choice=chooseBed(sidePrint(m,n),p);if(!choice)return null;result.push(choice);}
 return result;}
const HUB_FOOTPRINT=(()=>{const k=32,v=[],f=[];for(let i=0;i<k;i++){const a=i*TAU/k;v.push([60*Math.cos(a),60*Math.sin(a),0],[60*Math.cos(a),60*Math.sin(a),16]);}for(let i=0;i<k;i++){const j=(i+1)%k;f.push([2*i,2*j,2*j+1],[2*i,2*j+1,2*i+1]);if(i>0&&i<k-1)f.push([0,2*j,2*i],[1,2*i+1,2*j+1]);}return{v,f};})();
export function estimatePlates(p,n,footprints,cell){const parts=[{name:'hub',output:HUB_FOOTPRINT,dim:[120,120,16],qty:1},...footprints.map((c,j)=>({name:'petal '+(j+1),output:c.mesh,dim:c.dim,qty:n}))];return packParts(parts,p,cell).length;}
export function plan(p){validate(p);if(Math.min(p.bedX,p.bedY)-2*p.margin<120)throw Error('The 120 mm hub needs more usable bed space.');
 const rowsList=p.rows?[p.rows]:[1,2,3,4,5,6,7,8],nList=p.sectors?[p.sectors]:[6,8,10,12,14,16];
 // The real petals (flanges, root boss, ring flanges) are checked whenever the estimate comes within 15 mm of the bed.
 const real=(n,rows)=>solidScope(()=>Array.from({length:rows},(_,j)=>chooseBed(sidePrint(panelSolid(p,n,rows,j,false).body.rawMesh(),n),p))),
  fitsReal=(n,rows)=>real(n,rows).every(Boolean),near=fp=>fp.some(c=>c.dim[0]>p.bedX-2*p.margin-15||c.dim[1]>p.bedY-2*p.margin-15||c.dim[2]>p.bedZ-2-15);
 // Fewest pieces first; for the same count, fewer rings.
 const order=rowsList.flatMap(rows=>nList.map(n=>({n,rows}))).sort((x,y)=>x.n*x.rows-y.n*y.rows||x.rows-y.rows);let best;
 for(const c of order){const fp=petalFootprints(p,c.n,c.rows);if(!fp||near(fp)&&!fitsReal(c.n,c.rows))continue;best={n:c.n,rows:c.rows,choices:[]};break;}
 if(!best){if(p.feedMode){let fits=false;try{plan({...p,feedMode:0});fits=true;}catch{}if(fits)throw Error(`No petal count fits both your printer and ${p.feedLegs} rods: the petal count must be a multiple of the rod count${p.sectors?'':', and petals must stay wide enough at the hub'}. Try ${p.feedLegs===3?4:3} rods${p.sectors?' or automatic petals':''}, or a larger print volume.`);}
  throw Error(`No side-printed layout fits.${p.sectors||p.rows?' Use automatic segmentation,':' Use'} a larger print volume or a smaller dish. Flange joints need enough room for hardware and tools.`);}
 if(p.segmentGoal===1&&(!p.sectors||!p.rows)){
  // Rank candidates on coarse footprints (the packer's per-height footprints on a 2 mm grid), then confirm the winner at
  // full detail.
  const coarse={...p,resolution:Math.max(p.resolution,12)},ranked=[];
  for(const rows of rowsList.filter(r=>r<=best.rows+2))for(const n of nList){if(rows*n>3*best.n*best.rows)continue;const fp=petalFootprints(coarse,n,rows);if(fp)ranked.push({n,rows,plates:estimatePlates(p,n,fp,2)});}
  const least=Math.min(...ranked.map(c=>c.plates)),close=ranked.filter(c=>c.plates<=least+1||c.n===best.n&&c.rows===best.rows);let pick=null;
  for(const c of close){const fp=petalFootprints(p,c.n,c.rows);if(!fp)continue;const plates=estimatePlates(p,c.n,fp,2);if(!pick||plates<pick.plates||plates===pick.plates&&(c.n*c.rows<pick.n*pick.rows||c.n*c.rows===pick.n*pick.rows&&c.rows<pick.rows))pick={n:c.n,rows:c.rows,plates};}
  // A different layout has to earn its extra petals: check both with the real petals before switching.
  if(pick&&!(pick.n===best.n&&pick.rows===best.rows)){const fp=real(pick.n,pick.rows),base=real(best.n,best.rows);
   if(fp.every(Boolean)){const b=estimatePlates(p,pick.n,fp);if(!base.every(Boolean))return{n:pick.n,rows:pick.rows,choices:[],estimatedPlates:b};const a=estimatePlates(p,best.n,base);return b<a?{n:pick.n,rows:pick.rows,choices:[],estimatedPlates:b}:{...best,estimatedPlates:a};}
   return best;}
  if(pick)return{...best,estimatedPlates:pick.plates};}
 return best;}
function clipped(s,p,n,a,b,gap=0){const h=PI/n;
 s=s.trim([Math.sin(h),Math.cos(h),0],gap/2).trim([Math.sin(h),-Math.cos(h),0],gap/2);
 if(p.staggerRings){
  // One shared 2n-sided polygon at each ring boundary. Each petal spans two faces.
  if(a>45.001){let inner=cylinder([0,0,-100],[0,0,p.diameter],p.diameter,32);for(const t of [-h/2,h/2])inner=inner.trim([-Math.cos(t),-Math.sin(t),0],-a-gap/2);s=s.subtract(inner);}else s=s.trim([1,0,0],a+gap/2);
  if(Number.isFinite(b))for(const t of [-h/2,h/2])s=s.trim([-Math.cos(t),-Math.sin(t),0],-b+gap/2);
 }else s=s.trim([1,0,0],a+gap/2).trim([-1,0,0],Number.isFinite(b)?-b+gap/2:-p.diameter);
 return s.intersect(cylinder([0,0,-100],[0,0,p.diameter],p.diameter/2,Math.max(96,Math.ceil(PI*p.diameter/p.resolution))));}
// Integral edge flange: a 3 mm wall with a 45° root gusset and solid screw pads.
function flange(p,frame,start,end,male,h,overlap=0){const {o,e,v}=frame,point=(s,t,z)=>[o[0]+s*e[0]+t*v[0],o[1]+s*e[1]+t*v[1],z],back=s=>{const q=point(s,0,0);return backZ(q[0],q[1],p);},backT=(s,t)=>{const q=point(s,t,0);return backZ(q[0],q[1],p);};
 // Station height. With clips, drop it by the shell's slope across the clip so the jaws clear the shell everywhere.
 // With clips the station is tilted to follow the shell along the seam; it drops only for the shell's slope across the seam.
 const tilt=s=>p.seamJoint?Math.atan((back(s+1)-back(s-1))/2):0,level=s=>p.seamJoint?back(s)-(7+Math.max(0,CLIP.top+.25-7+Math.abs(backT(s,1)-backT(s,-1))/2*7.6))/Math.cos(tilt(s)):back(s)-7;
 const frameAt=s=>{const z=level(s),a=tilt(s),c=Math.cos(a),sn=Math.sin(a);return(X,Y,U)=>point(s+U*c-Y*sn,X,z+U*sn+Y*c);},hy=boltY(p);const ramp=p.seamJoint&&o[0]===0&&o[1]===0?Math.min((end-start)/4,20*Math.sin(PI/3)/Math.sin(2*h))+6:0,span=end-start-ramp,countBolts=p.seamJoint?Math.max(1,Math.min(Math.round(span/40),Math.floor(span/34))):2*Math.max(1,Math.ceil(span/240)),stations=Array.from({length:countBolts},(_,i)=>start+ramp+span*(i+.5)/countBolts),mid=(start+end)/2,cs=0,W=p.seamJoint?CLIP.wall:3,D=14+(p.seamJoint?(sl=>Math.max(0,5*sl+7.6*Math.abs(sl)))((backT((start+end)/2,1)-backT((start+end)/2,-1))/2):0),dAt=s=>male&&o[0]===0?Math.min(D,.5+(s-start)*(D-.5)/Math.min((end-start)/4,20*Math.sin(PI/3)/Math.sin(2*h))):D;
 const profile=s=>{const d=dAt(s);const outline=male?[[cs,-d],[W,-d],[W+d,0],[W+d,.5],[cs,.5]]:[[cs,-d],[W,-d],[W,-4],[W+4,0],[W+4,.5],[cs,.5]];return outline.map(([t,z])=>{const q=point(s,t,0);return point(s,t,backZ(q[0],q[1],p)+z);});};
 const count=Math.max(2,Math.ceil((end-start+2*overlap)/p.resolution)),rings=Array.from({length:count+1},(_,i)=>profile(start-overlap+(end-start+2*overlap)*i/count));let body=loft(rings);
 // An end face whose outward normal (along ±e) points down more than 45° in the side print is cut back at the smallest
 // angle toward print-up that fixes it (the male flange's thin ramped start on the upper side, for example).
 {const uu=e[0]*Math.sin(h)+e[1]*Math.cos(h),ut=v[0]*Math.sin(h)+v[1]*Math.cos(h),sv=ut<0?-1:1;
  for(const[sEnd,dir]of [[start-overlap,-1],[end+overlap,1]]){if(dir*uu>=-.68)continue;let a=0;while(a<1.2&&dir*uu*Math.cos(a)+Math.abs(ut)*Math.sin(a)<-.68)a+=.01;
   const n=[dir*e[0]*Math.cos(a)+sv*v[0]*Math.sin(a),dir*e[1]*Math.cos(a)+sv*v[1]*Math.sin(a),0],P=point(sEnd,sv<0?W+dAt(sEnd)+1:0,0);body=body.trim(n.map(x=>-x),-(n[0]*P[0]+n[1]*P[1]));}}
 const prism=(s0,s1,t0,t1,z0,z1)=>loft([s0,s1].map(s=>[[t0,z0],[t1,z0],[t1,z1],[t0,z1]].map(([t,z])=>point(s,t,z))));
 const cuts=[];
 // Print-up in this flange's frame: faces whose normal is -sg·e point down; psi tilts them to 45° or better.
 const upU=e[0]*Math.sin(h)+e[1]*Math.cos(h),upT=v[0]*Math.sin(h)+v[1]*Math.cos(h),sg=upU<0?-1:1;let psi=0;
 for(let d=0;d<=60;d++){const ok=[d,-d].find(x=>-Math.abs(upU)*Math.cos(x*PI/180)-upT*Math.sin(x*PI/180)>=-.68);if(ok!==undefined){psi=ok*PI/180;break;}}
 // Teardrop roof for a bolt hole along t at station s, (u, w) cross-section: the apex points to print-up projected into
 // that section (cos(tilt)·upU along u, -sin(tilt)·upU along w), so tilted clip stations print without support too.
 const roofToward=(r,s)=>{const a=tilt(s),du=Math.cos(a)*upU,dw=-Math.sin(a)*upU,l=Math.hypot(du,dw),c=l>1e-9?du/l:1,sn=l>1e-9?dw/l:0;return[[-r*.707,-r*.707],[r*.707,-r*.707],[r*Math.SQRT2,0],[r*.707,r*.707],[-r*.707,r*.707]].map(([x,y])=>[c*x-sn*y,sn*x+c*y]);};
 const SB=seamBolt(p),pw=SB.padW,padPoly=s=>[[s-sg*(pw-5*Math.tan(psi)),0],[s+sg*(pw+2),0],[s+sg*(pw+2),3],[s+sg*pw,5],[s-sg*pw,5]];
 const padCount=count,padEnvelope=p.seamJoint?null:loft(Array.from({length:padCount+1},(_,i)=>{
  const ss=start-overlap+(end-start+2*overlap)*i/padCount;
  return [[-.02,-dAt(ss)+.1],[5.02,-dAt(ss)+.1],[5.02,.4],[-.02,.4]].map(([t,dz])=>{const q=point(ss,t,0);return point(ss,t,backZ(q[0],q[1],p)+dz);});
 }));
 let pads=null;for(const s of stations){const z=level(s),F=frameAt(s),ct=Math.cos(tilt(s)),Yb=(back(s)-dAt(s)-z)*ct,Ys=(back(s)-z)*ct;if(!p.seamJoint){
  // A full-depth curved bearing land joins the shell and flange without a notch.
  // Its print-down end keeps the support-safe ramp; the other end blends into
  // the 3 mm wall with a 2 mm chamfer. Bolt/washer datums remain unchanged.
  const pad=loft([-p.diameter,p.diameter].map(zz=>padPoly(s).map(([ss,t])=>point(ss,t,zz)))).intersect(padEnvelope);
  pads=pads?pads.union(pad):pad;body=body.union(pad);
 }else{
  // two low ridges either side of the clip, from the flange bottom to the shell: a face toward the clip, a flat 1 mm top,
  // and a ramp back to the wall. Each sloped face is as steep as this station's print direction allows (45° or better):
  // print-up along the station's U axis is cos(tilt)·upU, across the wall (t) upT.
  const R=CLIP.ridge,u0=CLIP.width/2+CLIP.clear,ct=Math.cos(tilt(s)),slope=(sign)=>{let best=-9,run=0;for(let a=0;a<=80;a++){const r=a*PI/180,val=sign*ct*upU*Math.cos(r)+upT*Math.sin(r);if(val>=-.68)return Math.tan(r);if(val>best){best=val;run=Math.tan(r);}}return run;};
  for(const k of [1,-1]){const run=Math.min(R.maxRamp,R.h*slope(k)),lean=Math.min(R.maxRamp,R.h*slope(-k));
   const ridge=loft([Yb+.5,Ys-.3].map(Y=>[[k*u0,W-.02],[k*(u0+lean+R.top+run),W-.02],[k*(u0+lean+R.top),W+R.h],[k*(u0+lean),W+R.h]].map(([u,t])=>F(t,Y,u))));pads=pads?pads.union(ridge):ridge;}
}
  const r=SB.holeR,round=Array.from({length:24},(_,i)=>{const a=TAU*i/24;return[r*Math.cos(a),r*Math.sin(a)];});const roof=roofToward(r,s);
  const extrude=poly=>loft([-1,9].map(t=>poly.map(([u,w])=>F(t,hy+w,u))));if(seamHoles(p))cuts.push(extrude(round).union(extrude(roof)));
 }
 // Flat seats square to the bolt on both sides: the wall/pad face (t = 5) is exposed through the gusset or fillet.
 // Loose nut and washer; nothing captured. Cut from flanges only (panelSolid), so the shell is never opened.
 let seats=null;const windows=[];for(const s of stations){const z=level(s),F=frameAt(s),Yb=(back(s)-dAt(s)-z)*Math.cos(tilt(s)),R=SB.seatR,ring=Array.from({length:32},(_,i)=>{const a=TAU*i/32;return[R*Math.cos(a),R*Math.sin(a)];}),roofR=roofToward(R,s);
 const bore=poly=>loft([5,21].map(t=>poly.map(([u,w])=>F(t,hy+w,u))));let seat=seamHoles(p)?bore(ring).union(bore(roofR)):null;
 if(p.seamJoint){ // clip recess
  // the 45° gusset (and the bed-side fillet) is cut clean through over the clip and its ridges; the cut end is tilted (psi) to print without support
  const T=W+D+2,wc=CLIP.width/2+CLIP.clear+CLIP.ridge.top+CLIP.ridge.maxRamp+CLIP.ridge.h*1.7+.6,tan=Math.tan(psi),wc0=wc+Math.max(0,tan)*(T-W);
  const slotPoly=[[-sg*wc,W],[sg*wc0,W],[sg*(wc0-tan*(T-W)),T],[-sg*wc,T]];
  windows.push(loft([-30,25].map(Y=>slotPoly.map(([u,t])=>F(t,Y,u)))));
  // the clip's own envelope is also cleared from neighboring flanges (corners where seams meet)
  {const e=CLIP.width/2+CLIP.clear,t1=W+CLIP.root+.8;seat=(x=>seat?seat.union(x):x)(loft([-30,25].map(Y=>[[-e,W],[e,W],[e,t1],[-e,t1]].map(([u,t])=>F(t,Y,u)))));}
  // cylindrical groove across the wall for the clip's bump: 0.1 mm deeper and as wide as the clip plus its clearance
  {const w=CLIP.width/2+CLIP.clear,X=detentX(p.clipDetent);cuts.push(cylinder(F(X,CLIP.detentY,-w),F(X,CLIP.detentY,w),CLIP.detentR+.1,64));}
  // lead-in chamfer on the wall's bottom edge
  const c=Math.max(.6,p.clipDetent+.2);cuts.push(loft([-CLIP.width/2-CLIP.clear,CLIP.width/2+CLIP.clear].map(u=>[[W+.05,Yb-.05],[W+.05,Yb+c],[W-c,Yb-.05]].map(([t,Y])=>F(t,Y,u)))));}
 seats=seats?seats.union(seat):seat;}
 return{body,seats,windows,pads,cuts,stations,levels:stations.map(level),tilts:stations.map(tilt),key:mid,frame,start,end};}
// Every flange on a petal: [frame, start, end, male, half-angle, overlap]. Shared by panelSolid and petalSignature.
function flangeFrames(p,n,rows,j){const h=PI/n,[a,b]=rowBounds(p,n,rows,j),R=p.diameter/2,defs=[];
 for(const sign of [-1,1])defs.push([{o:[0,0],e:[Math.cos(h),sign*Math.sin(h)],v:[Math.sin(h),-sign*Math.cos(h)]},Math.max(63,a/Math.cos(p.staggerRings&&j>0?h/2:h)+1),Math.min(R,Number.isFinite(b)?b/Math.cos(p.staggerRings?h/2:h):R)-1,sign===1,h]);
 if(p.staggerRings){for(const [r,male] of [[a,false],[b,true]])if((male&&j<rows-1)||(!male&&j>0))for(const t of [-h/2,h/2]){
  const c=Math.cos(t),s=Math.sin(t),L=r*Math.tan(h/2);
  defs.push([{o:[r*c,r*s],e:[-s,c],v:male?[-c,-s]:[c,s]},-L,L,male,h,20*Math.tan(h/2)+1]);
 }}else{
 if(j>0)defs.push([{o:[a,0],e:[0,1],v:[1,0]},-a*Math.tan(h),a*Math.tan(h),false,h]);
 if(j<rows-1)defs.push([{o:[b,0],e:[0,1],v:[-1,0]},-b*Math.tan(h),b*Math.tan(h),true,h]);
 }
 return defs;}
// Petals with the same root fastener, rod mount and seam choices are identical solids.
const petalSignature=(p,n,rows,j,mount,angle)=>JSON.stringify([mount,j===0?p.rootThrough:null,flangeFrames(p,n,rows,j).map(([frame,start,end])=>{const c=seamChoice(p,frame,start,end,angle);return[c.seamJoint,c.seamBolt];})]);
export function panelSolid(p,n,rows,j,feed=false,angle=0){const h=PI/n,[a,b]=rowBounds(p,n,rows,j),R=p.diameter/2;const lines=[];if(p.rearStyle)for(let k=-Math.ceil(R/p.facetSize);k<=Math.ceil(R/p.facetSize);k++)for(const normal of [[1,0],[0,1]])lines.push([...normal,(k+.5)*p.facetSize]);let body=clipped(solid(patch({r0:Math.max(1,a-2),r1:R,a0:-h,a1:h,backFn:(x,y)=>backZ(x,y,p),creaseLines:lines},p)),p,n,a,b,p.gap);
 const makeFlange=(frame,start,end,...args)=>{const choice=seamChoice(p,frame,start,end,angle),f=flange({...p,...choice},frame,start,end,...args);return {...f,id:choice.id,family:choice.family,joint:choice.seamJoint,bolt:choice.seamBolt};};const flanges=flangeFrames(p,n,rows,j).map(([frame,start,end,...args])=>makeFlange(frame,start,end,...args));
 // Clip flange bodies to the seam planes (flat mating faces).
 // Seats clear every flange's gusset (a seat near a corner also opens the neighbor's), never the shell or a bolt pad.
 for(const f of flanges){let fb=f.body;for(const g of flanges)if(g.seats)fb=fb.subtract(g.seats);for(const w of f.windows)fb=fb.subtract(w);body=body.union(f.pads?fb.union(f.pads):fb);}
 body=clipped(body,p,n,a,b);
 if(j===0){const bottom=rootBottom(p);let boss=solid(patch({r0:43,r1:64/Math.cos(h),a0:-h,a1:h,topFn:(x,y)=>backZ(x,y,p)+.5,backFn:()=>bottom},p)).trim([1,0,0],45).trim([-1,0,0],-63).trim([0,-1,0],-8);body=body.union(boss);
  const RH=rootHole(p);
  // holes along the dish axis lie horizontal in the side print: round plus a 45° roof toward print-up
  const up=[Math.sin(h),Math.cos(h)],across=[Math.cos(h),-Math.sin(h)],drop=r=>[...Array.from({length:32},(_,i)=>{const a=TAU*i/32;return[r*Math.cos(a),r*Math.sin(a)];})],roofR=r=>[[-r*.707,-r*.707],[r*.707,-r*.707],[r*.707,r*.707],[0,r*Math.SQRT2],[-r*.707,r*.707]];
  const bore=(poly,z0,z1)=>loft([z0,z1].map(z=>poly.map(([a,b])=>[52.5+a*across[0]+b*up[0],a*across[1]+b*up[1],z])));
  if(p.rootThrough){ // through bolt: head and washer on a flat seat in the front (recessed or a plain spot face), nut on the hub's rear face
   const seat=rootSeatFloor(p),top=zAt(70,p)+5;
   body=body.subtract(bore(drop(RH.r),bottom-1,top).union(bore(roofR(RH.r),bottom-1,top)).union(bore(drop(RH.seatR),seat,top)).union(bore(roofR(RH.seatR),seat,top)));
  }else body=body.subtract(bore(drop(RH.pilot/2),bottom-1,bottom+RH.depth).union(bore(roofR(RH.pilot/2),bottom-1,bottom+RH.depth)));}
 if(feed){const socket=petalRodSocket(p,{n,rows},(x,y)=>backZ(x,y,p));body=body.union(socket.body);for(const cut of socket.cuts)body=body.subtract(cut);}

 for(const f of flanges)for(const cut of f.cuts)body=body.subtract(cut);
 return{body,spec:{a,b,row:j,flanges:flanges.map(({stations,levels,tilts,key,frame,start,end,id,family,joint,bolt})=>({stations,levels,tilts,key,frame,start,end,id,family,joint,bolt})),feedMount:feed,rootThrough:p.rootThrough}};}
// Each petal and the hub is built in its own scope, so its intermediate solids are freed before the next one starts.
const meshedPanel=(...args)=>solidScope(()=>{const{body,spec}=panelSolid(...args);return{mesh:body.mesh(),spec};});
export function hubSolid(p,n){const bottom=rootBottom(p)-6,top=rootBottom(p),R=60,flat=hubTop(p,n);let center=solid(patch({r0:15,r1:R,a0:0,a1:TAU,...(p.hubFlat?{topFn:flat.fn,creaseLines:flat.lines,extraR:Array.from({length:41},(_,i)=>38+i*.5)}:{}),backFn:()=>bottom},p));const e=hubEdge(p)/Math.cos(PI/n);center=center.intersect(prism(Array.from({length:n},(_,i)=>{const a=(i+.5)*TAU/n;return[e*Math.cos(a),e*Math.sin(a)];}),bottom-1,p.diameter));let hub=cylinder([0,0,bottom],[0,0,top],R,128).subtract(cylinder([0,0,bottom-1],[0,0,top+1],15,96)).union(center);
 // every hole in one subtraction: the cuts are disjoint, so their union is cheap and the hub is cut once
 const cuts=[];for(let i=0;i<n;i++){const a=i*TAU/n,x=52.5*Math.cos(a),y=52.5*Math.sin(a);cuts.push(cylinder([x,y,bottom-1],[x,y,top+1],rootHole(p).hubR+(p.hubFloat||0)));}const MH=mountHole(p);for(let i=0;i<4;i++){const a=PI/4+i*PI/2,x=30*Math.cos(a),y=30*Math.sin(a);let c=cylinder([x,y,bottom-1],[x,y,mountChoice(p,i)?p.diameter:bottom+MH.depth],mountChoice(p,i)?MH.r:MH.pilot/2);if(mountChoice(p,i)&&!(p.mountSeat&&p.hubFlat))c=c.union(cylinder([x,y,hubSeatFloor(p,n)],[x,y,p.diameter],MH.seatR,48));cuts.push(c);}return hub.subtract(cuts.reduce((a,b)=>a.union(b)));}
// The dish's solids do not depend on the aiming mount, its pose or the plate options, so a change to only those reuses
// the last build's segmentation, petal meshes and hub mesh (one dish is kept).
const POSE_KEYS=new Set(['azimuth','elevation','mountMode','mountBase','mountArcLock','clampBolt','jointBolt','standBolt','legBolt','headSeat','nutSeat','standSeat','legDiameter','legSplay','packPlates','printSelection']);
let dishMemo={key:null,map:new Map()};
// print orientation of a reused petal or hub mesh (the same output object also keeps the packer's cached footprints)
const bedChoices=new WeakMap();
function dishPart(p,name,fn){const key=JSON.stringify(Object.keys(p).filter(k=>!POSE_KEYS.has(k)).sort().map(k=>[k,p[k]]));if(dishMemo.key!==key)dishMemo={key,map:new Map()};if(!dishMemo.map.has(name))dishMemo.map.set(name,fn());return dishMemo.map.get(name);}
export function build(input){const p=validate({...defaults,...input}),layout=dishPart(p,'plan',()=>plan(p));return solidScope(()=>{const{n,rows}=layout,parts=[],instances=[],step=TAU/n;const add=(id,name,mesh,qty,angles,kind,row,spec={})=>{let choice=bedChoices.get(mesh);if(!choice){const pm=kind==='panel'?sidePrint(mesh,n):kind==='clip'?printMesh({v:mesh.v.map(([x,y,z])=>[x,z,-y]),f:mesh.f}):printMesh(mesh);choice=chooseBed(pm,p);if(choice&&(kind==='panel'||kind==='hub'))bedChoices.set(mesh,choice);}if(!choice)throw Error(name+' exceeds the print volume after adding joints. Increase print volume or segmentation.');const part={id,name,mesh,print:choice.mesh,output:choice.mesh,dim:choice.dim,qty,kind,row,spec,angle:90,bedRotation:choice.yaw,supportMeshes:[]};parts.push(part);angles.forEach(a=>instances.push({part,a}));return part;};
 if(p.connections){ for(let j=0;j<rows;j++){const groups=new Map();for(let i=0;i<n;i++){const angle=i*step+(p.staggerRings?(j%2)*PI/n:0),mount=Boolean(p.feedMode&&j===rows-1&&i%(n/p.feedLegs)===0),local={...p,rootThrough:rootChoice(p,i)},signature=petalSignature(local,n,rows,j,mount,angle);if(groups.has(signature))groups.get(signature).angles.push(angle);else groups.set(signature,{...dishPart(p,`panel:${j}:${signature}`,()=>meshedPanel(local,n,rows,j,mount,angle)),angles:[angle],mount});}let v=0;for(const group of groups.values()){v++;add(`petal-${j+1}-variant-${v}`,`Petal ${j+1} · variant ${v}${group.mount?' · rod mount':''}`,group.mesh,group.angles.length,group.angles,'panel',j,group.spec);}}
}else{ for(let j=0;j<rows;j++)for(const mount of [false,true]){const angles=Array.from({length:n},(_,i)=>i).filter(i=>Boolean(p.feedMode&&j===rows-1&&i%(n/p.feedLegs)===0)===mount).map(i=>i*step+(p.staggerRings?(j%2)*PI/n:0));if(!angles.length)continue;const{mesh,spec}=dishPart(p,`panel:${j}:${mount}`,()=>meshedPanel(p,n,rows,j,mount));add(`petal-${j+1}${mount?'-mount':''}`,`Petal ${j+1}${mount?' · rod mount':''}`,mesh,angles.length,angles,'panel',j,spec);}
}
 add('hub','Hub · clear center',dishPart(p,'hub',()=>solidScope(()=>hubSolid(p,n).mesh())),1,[0],'hub',-1);const seams=seamStations(parts);
 const m={p,layout,parts,instances,ringPhases:Array.from({length:rows},(_,j)=>p.staggerRings?(j%2)*PI/n:0),depth:zAt(p.diameter/2,p),focal:p.diameter*p.fd,bolts:0};
 if(p.connections){installConnections(m,add);}else{
 if(usesClips(p))add('seam-clip','Seam clip',clipSolid(p.clipFit,p.clipDetent,(X,Y,U)=>[X,U,Y]).mesh(),seams+Math.max(2,Math.ceil(seams/10)),[],'clip',-1);m.bolts=(p.seamJoint===0||p.seamJoint===2?seams:0)+n;
 // One clip or one lever set per seam station, shown installed; stations are shared by two petals, so pair them once.
 const fitted=usesClips(p)?[parts.find(q=>q.kind==='clip')]:usesLevers(p)?addLevers(p,parts,seams):[];
 if(fitted.length){const placed=[],panels=instances.filter(i=>i.part.kind==='panel');
  // A lever set sits on the +X side of the station; where that side meets another flange (ring junctions) it is turned to -X.
  const collides=usesLevers(p)?leverProbe(panels):null,leverSet=collides?fitted.map(part=>solid(part.mesh)).reduce((x,y)=>x.union(y)):null;
  for(const ins of panels)for(const f of ins.part.spec.flanges)for(let k=0;k<f.stations.length;k++){const F=stationFrame(f,k),center=rz(F(0,0,0),ins.a);if(placed.some(q=>Math.hypot(...q.map((x,j)=>x-center[j]))<.15))continue;placed.push(center);
   // Where neither side has room (some ring junctions), that station takes a seam bolt instead; every station has the hole.
   let g=1;if(collides&&collides(leverSet,F,ins.a,1)){g=-1;if(collides(leverSet,F,ins.a,-1)){m.boltStations=(m.boltStations||0)+1;continue;}}
   for(const part of fitted)instances.push({part,a:ins.a,matrix:part.kind==='clip'?affine(([x,u,y])=>F(x,y,u)):affine(([x,y,u])=>F(g*x,y,g*u))});}if(placed.length!==seams)throw Error('Seam stations do not pair across the seams. Change mesh or segmentation.');
  const installed=placed.length-(m.boltStations||0),spares=Math.max(2,Math.ceil(installed/10));if(!installed)throw Error('No seam station has room for a lever. Use bolts, clips or Both.');for(const part of fitted){if(part.kind==='lever')part.qty=installed+spares;part.installed=installed;part.spares=part.qty-installed;}m.bolts+=m.boltStations||0;}
}
 appendFeedParts(m,{patch,printMesh,bounds});appendMount(m);m.rootScrew=`M${p.rootBolt} × ${rootScrew(p)}`;for(const part of parts)part.printIncluded=p.printSelection?.[part.kind]!==false;m.plates=p.packPlates?packAll(parts,p):[];return m;});}
// Lever sets are checked against only the petals whose bounds they reach, one petal at a time (petals meet
// only at faces, so their overlaps add), each test in its own scope so temporary solids are freed at once.
// Testing every station against a union of the whole dish was slow and exhausted WASM memory on large dishes.
function leverProbe(panels){const base=new Map(),items=panels.map(i=>{if(!base.has(i.part))base.set(i.part,solid(i.part.mesh));const s=base.get(i.part).transform(q=>rz(q,i.a)),{min,max}=s.raw.boundingBox();return{s,min,max};});
 return (set,F,a,g)=>solidScope(()=>{const shape=set.transform(q=>rz(F(g*q[0],q[1],g*q[2]),a)),b=shape.raw.boundingBox(),near=items.filter(x=>x.min.every((v,k)=>v<=b.max[k]+.01)&&x.max.every((v,k)=>v>=b.min[k]-.01));let overlap=0;for(const x of near)if((overlap+=shape.intersect(x.s).raw.volume())>.05)return true;return false;});}
function installConnections(m,add){
 const {p,parts,instances}=m,panels=instances.filter(i=>i.part.kind==='panel'),stations=new Map();
 for(const ins of panels)for(const f of ins.part.spec.flanges)for(let k=0;k<f.stations.length;k++){const F=stationFrame(f,k),center=rz(F(0,0,0),ins.a),key=center.map(v=>Math.round(v*10)).join(',');if(!stations.has(key))stations.set(key,[]);stations.get(key).push({ins,f,k,F});}
 for(const pair of stations.values())if(pair.length!==2||pair[0].f.joint!==pair[1].f.joint||pair[0].f.bolt!==pair[1].f.bolt)throw Error('Connection choices do not match across a seam.');
 const counts={clip:0,lever:{3:0,4:0},bolts:{3:0,4:0}},fitted={};for(const pair of stations.values()){const f=pair[0].f;if(f.joint===1||f.joint===2)counts.clip++;if(f.joint===3)counts.lever[f.bolt]++;if(f.joint===0||f.joint===2)counts.bolts[f.bolt]++;}
 if(counts.clip)fitted.clip=[add('seam-clip','Seam clip',clipSolid(p.clipFit,p.clipDetent,(X,Y,U)=>[X,U,Y]).mesh(),counts.clip+Math.max(2,Math.ceil(counts.clip/10)),[],'clip',-1)];
 const both=counts.lever[3]&&counts.lever[4];for(const size of [3,4])if(counts.lever[size])fitted[size]=addLevers({...p,seamBolt:size},parts,counts.lever[size],both?'-M'+size:'');
 const collides=counts.lever[3]||counts.lever[4]?leverProbe(panels):null,leverSets={};for(const size of [3,4])if(fitted[size])leverSets[size]=fitted[size].map(part=>solid(part.mesh)).reduce((a,b)=>a.union(b));
 for(const pair of stations.values()){const {ins,f,F}=pair[0],set=f.joint===1||f.joint===2?fitted.clip:f.joint===3?fitted[f.bolt]:null;if(!set)continue;let g=1;if(f.joint===3){const shape=leverSets[f.bolt];if(collides(shape,F,ins.a,1)){g=-1;if(collides(shape,F,ins.a,-1)){counts.lever[f.bolt]--;counts.bolts[f.bolt]++;m.boltStations=(m.boltStations||0)+1;continue;}}}for(const part of set)instances.push({part,a:ins.a,matrix:part.kind==='clip'?affine(([x,u,y])=>F(x,y,u)):affine(([x,y,u])=>F(g*x,y,g*u))});}
 for(const [key,set]of Object.entries(fitted)){const installed=key==='clip'?counts.clip:counts.lever[key],spares=Math.max(2,Math.ceil(installed/10));for(const part of set){part.installed=installed;part.spares=spares;part.qty=installed+spares;}}
 m.connectionCounts=counts;m.bolts=m.layout.n+counts.bolts[3]+counts.bolts[4];
}
// Seam lever (cad/seam-lever.scad): four printed parts per station, bundled for the selected bolt size.
export const LEVER_PARTS=[['lever','Seam lever · lever'],['bar','Seam lever · draw bar'],['keeper','Seam lever · keeper'],['spring','Seam lever · spring (TPU)']];
function addLevers(p,parts,seams,suffix=""){const set=leverMeshes['M'+p.seamBolt],qty=seams+Math.max(2,Math.ceil(seams/10));return LEVER_PARTS.map(([key,name])=>{const src=set[key],mesh={v:src.installed.v.map(q=>[...q]),f:src.installed.f},choice=chooseBed(printMesh({v:src.print.v.map(q=>[...q]),f:src.print.f}),p);if(!choice)throw Error(name+' exceeds the print volume.');const part={id:'seam-lever-'+key+suffix,name:name+suffix,mesh,print:choice.mesh,output:choice.mesh,dim:choice.dim,qty,kind:'lever',row:-1,spec:{hole_d:set.hole_d,material:key==='spring'?'TPU 95A':'PCTG or PETG',source_sha256:leverMeshes.source_sha256},flex:key==='spring',angle:90,bedRotation:choice.yaw,supportMeshes:[]};parts.push(part);return part;});}
export function connectionCoupon(m){if(m.connectionCounts)return mixedCoupons(m);return solidScope(()=>{const part=m.parts.find(p=>p.kind==='panel'),f=part.spec.flanges[0],c=Math.cos(PI/m.layout.n),lo=f.stations[0]*c-9,hi=f.key*c+9;const cropped=solid(part.mesh).trim([1,0,0],lo).trim([-1,0,0],-hi).mesh();const clips=usesClips(m.p)?[...new Set([Math.max(0,m.p.clipFit-.05),m.p.clipFit,Math.min(.1,m.p.clipFit+.05)].map(x=>+x.toFixed(3)))].map(fit=>{const mesh=clipSolid(fit,m.p.clipDetent,(X,Y,U)=>[X,U,Y]).mesh();return{id:`seam-clip-fit-${fit.toFixed(2)}`,qty:1,mesh,output:printMesh({v:mesh.v.map(([x,y,z])=>[x,z,-y]),f:mesh.f})};}):[];return[{id:'seam-strip-print-two',qty:2,mesh:cropped,output:sidePrint(cropped,m.layout.n)},...clips];});}
const seamStations=parts=>parts.filter(p=>p.kind==='panel').reduce((sum,p)=>sum+p.qty*p.spec.flanges.reduce((a,f)=>a+f.stations.length,0),0)/2;
// Root and hub-to-mount hardware for the selected sizes. Through bolts: nut and washers; blind: heat-set inserts.
// Root screw: through bolts span the recessed front seat, two washers up to 1 mm and the nut, plus 1.5 mm; blind screws
// enter the petal insert through one 1 mm washer.
export const rootScrew=p=>{const F=FASTENER[p.rootBolt];return p.rootThrough?stockLength(rootSeatFloor(p)-(rootBottom(p)-6)+2+F.nutH+1.5):stockDown(6+1+F.insert-.5);};
function rootMountHardware(m,screw){const p=m.p,n=m.layout.n,rt=!!p.rootThrough,mt=!!p.mountThrough,R=p.rootBolt,M=p.mountBolt,FR=FASTENER[R],FM=FASTENER[M],RH=rootHole(p),MH=mountHole(p),foot=m.feed?.mast?m.feed.mast.flange:0;
 const inserts=[];if(!rt)inserts.push([R,n]);if(!mt)inserts.push([M,4]);
 const insertRows=[...new Set(inserts.map(([s])=>s))].map(s=>({item:'blind insert',spec:`M${s} / maximum ${FASTENER[s].insert} mm long`,quantity:inserts.filter(([x])=>x===s).reduce((a,[,q])=>a+q,0),note:`Ø${insertPilot(p,s)} × ${FASTENER[s].insert+1} mm blind pilot; seat 0.5 mm below entry. Match insert vendor pilot specification.`}));
 return[...(rt?[{item:'root screw',spec:`M${R} × ${rootScrew(p)} socket head`,quantity:n,note:`From the front, head and washer ${p.rootSeat?'on the flat spot face, standing above the reflecting face':'in the recessed seat'}; nut on the hub rear face.`},{item:'root nut',spec:`M${R}`,quantity:n},{item:'root washer',spec:`M${R} / 0.8–1 mm, ${FR.washer} mm OD`,quantity:2*n,note:'One under the head, one under the nut.'}]
   :[{item:'root screw',spec:`M${R} × ${rootScrew(p)}`,quantity:n,note:`From the hub rear into the petal insert, with one 1 mm washer; about ${rootScrew(p)-7} mm entry. The reflecting face stays closed.`},{item:'root washer',spec:`M${R} / 1 mm`,quantity:n}]),
  ...insertRows,
  {item:'external mount screw',spec:mt?`M${M} × ${Math.ceil((screw+12)/5)*5} for a 12 mm adapter plate`:(p.mountMode?`M${M} × ${stockDown(12+1+FM.insert-.5)} for the 12 mm cradle + 1 mm washer`:`M${M} / select from actual adapter stack`),quantity:4,
   note:mt?`60 mm BCD; Ø${(2*MH.r).toFixed(1)} through the hub. Head behind the adapter; ${foot?`washer and nut on top of the ${foot} mm collector mast foot, which these bolts also clamp`:p.mountSeat?'washer and nut on the hub front':`washer and nut in the Ø${2*MH.seatR} seats on the hub front`}. Use ${screw} mm plus your adapter thickness, rounded up.`:`60 mm BCD. Target ${FM.insert-2}–${FM.insert-1} mm entry; never bottom out in the ${FM.insert+1} mm pocket.`},
  ...(mt?[{item:'external mount nut',spec:`M${M}`,quantity:4},{item:'external mount washer',spec:`M${M}`,quantity:8,note:'One under each head and nut.'}]:[{item:'external mount washer',spec:`M${M} / 1 mm`,quantity:4,note:'One under each screw head; include it when calculating insert engagement.'}])];}
function legacyHardwareSchedule(m){const seams=seamStations(m.parts),sj=m.p.seamJoint,clips=m.parts.find(p=>p.kind==='clip'),rt=!!m.p.rootThrough,mt=!!m.p.mountThrough,n=m.layout.n,inserts=(rt?0:n)+(mt?0:4),screw=mountScrew(m);const SB=seamBolt(m.p),levers=m.parts.find(p=>p.id==='seam-lever-lever');return[...(clips?[{item:'seam clip',spec:'printed, in the kit',quantity:clips.qty,note:`${seams} stations plus ${clips.qty-seams} spares. Push straight up over both flanges from behind until both bumps click into the grooves; remove by springing one jaw outward with a flat screwdriver.`}]:[]),...(levers?[{item:'seam lever set',spec:`printed, in the kit: lever, draw bar and keeper (PCTG/PETG) plus a TPU 95A spring, for Ø${levers.spec.hole_d} holes`,quantity:levers.qty,note:`${levers.installed} stations plus ${levers.spares} spares${m.boltStations?` (the other ${m.boltStations} stations take bolts)`:''}. Bar through both flanges, keeper and spring onto its neck from behind, lever onto the cross pin, then flip the handle down to clamp. No screws.`}]:[]),...(sj===0||sj===2||m.boltStations?[{item:'seam screw',spec:SB.screw,quantity:sj===3?m.boltStations:seams,note:(sj===3?`At the ${m.boltStations} ring-junction stations with no room for a lever. `:sj?'Optional at any station instead of a clip. ':'')+'Through both flanges from either side; a few threads past the nut.'},{item:'seam nut',spec:SB.nut,quantity:sj===3?m.boltStations:seams,note:`Loose, on the flat seat opposite the head. Hold with a ${SB.driver} mm nut driver or wrench.`},{item:'seam washer',spec:sj>=2?SB.smallWasher:SB.washer,quantity:2*(sj===3?m.boltStations:seams),note:sj>=2?'One under the head, one under the nut. Small washers keep clear of the clip groove above the bolt.':'One under the head, one under the nut.'}]:[]),...rootMountHardware(m,screw)];}

function mixedHardwareSchedule(m){const {p,connectionCounts:c}=m,n=m.layout.n,result=[];
 for(const size of [3,4])if(c.bolts[size]){const SB=seamBolt({...p,seamBolt:size});result.push({item:'seam screw',spec:SB.screw,quantity:c.bolts[size],note:'Match M'+size+' holes; Both stations allow clips instead.'},{item:'seam nut',spec:SB.nut,quantity:c.bolts[size]},{item:'seam washer',spec:SB.smallWasher,quantity:2*c.bolts[size]});}
 for(const part of m.parts.filter(p=>p.kind==='clip'||p.id.startsWith('seam-lever-lever')))result.push({item:part.kind==='clip'?'seam clip':'seam lever set',spec:part.kind==='clip'?'printed':`printed / Ø${part.spec.hole_d} hole / TPU spring`,quantity:part.qty,note:`${part.installed} installed + ${part.spares} spares`});
 for(const type of [0,1]){const rootCount=Array.from({length:n},(_,i)=>rootChoice(p,i)).filter(x=>x===type).length,mountCount=Array.from({length:4},(_,i)=>mountChoice(p,i)).filter(x=>x===type).length;
  if(rootCount){const rows=hardwareSchedule({...m,connectionCounts:null,p:{...p,rootThrough:type,mountThrough:1}}).filter(x=>x.item.startsWith('root'));for(const row of rows)result.push({...row,quantity:row.quantity*rootCount/n});}
  if(mountCount){const rows=hardwareSchedule({...m,connectionCounts:null,p:{...p,rootThrough:1,mountThrough:type}}).filter(x=>x.item.startsWith('external mount'));for(const row of rows)result.push({...row,quantity:row.quantity*mountCount/4});}
  if(type===0&&rootCount+mountCount){const inserts=[[p.rootBolt,rootCount],[p.mountBolt,mountCount]].filter(([,q])=>q);for(const s of new Set(inserts.map(([x])=>x)))result.push({item:'blind insert',spec:`M${s} / maximum ${FASTENER[s].insert} mm long`,quantity:inserts.filter(([x])=>x===s).reduce((a,[,q])=>a+q,0),note:`Ø${insertPilot(p,s)} × ${FASTENER[s].insert+1} mm blind pilot; seat 0.5 mm below entry.`});}
 }return result;}

function mixedCoupons(m){return solidScope(()=>{const seen=new Set(),result=[],panels=m.instances.filter(i=>i.part.kind==='panel');let clips=false;
 for(const ins of panels)for(const f of ins.part.spec.flanges){const signature=f.joint+'-M'+f.bolt;if(seen.has(signature))continue;seen.add(signature);clips||=f.joint===1||f.joint===2;const id=jointKey(f.frame,f.start,f.end,ins.a),world=rz(stationFrame(f,Math.floor(f.stations.length/2))(0,0,0),ins.a);let member=0;
 for(const other of panels)for(const g of other.part.spec.flanges){if(jointKey(g.frame,g.start,g.end,other.a)!==id)continue;const k=g.stations.findIndex((_,index)=>Math.hypot(...rz(stationFrame(g,index)(0,0,0),other.a).map((v,i)=>v-world[i]))<.15);if(k<0)throw Error('Fit strips cannot find the matching station.');const t=g.stations[k],{o,e,v}=g.frame,center=[o[0]+t*e[0],o[1]+t*e[1]],dot=(a,b)=>a[0]*b[0]+a[1]*b[1];
 const crop=solid(other.part.mesh).trim([...e,0],dot(center,e)-12).trim([-e[0],-e[1],0],-dot(center,e)-12).trim([...v,0],dot(center,v)-16).trim([-v[0],-v[1],0],-dot(center,v)-16).mesh();
 result.push({id:'seam-'+signature+'-strip-'+(member++?'B':'A'),qty:1,mesh:crop,output:sidePrint(crop,m.layout.n)});
 }if(member!==2)throw Error('Fit strips must contain both sides of a seam.');
 }
 if(clips)for(const fit of [...new Set([Math.max(0,m.p.clipFit-.05),m.p.clipFit,Math.min(.1,m.p.clipFit+.05)].map(x=>+x.toFixed(3)))]){const mesh=clipSolid(fit,m.p.clipDetent,(X,Y,U)=>[X,U,Y]).mesh();result.push({id:`seam-clip-fit-${fit.toFixed(2)}`,qty:1,mesh,output:printMesh({v:mesh.v.map(([x,y,z])=>[x,z,-y]),f:mesh.f})});}return result;
 });}
export function hardwareSchedule(m){const rows=m.connectionCounts?mixedHardwareSchedule(m):legacyHardwareSchedule(m);return rows.map(row=>{const kind=row.item==='seam clip'?'clip':row.item==='seam lever set'?'lever':null;return kind&&m.p.printSelection?.[kind]===false?{...row,spec:row.spec.replace('in the kit','reuse existing parts'),note:(row.note||'')+' Reuse compatible existing parts; excluded from print plates and STL kit.'}:row;});}
