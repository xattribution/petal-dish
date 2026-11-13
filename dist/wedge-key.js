// Wedge-key seams (seamJoint 4): the petal-side station geometry (slot, lugs, V-register) and the printed parts (T-key,
// wedge, the thumb-screw option's drift keeper and thumb nut, fit-test shims), all built with the app's own kernel so
// they follow the parameters. Part meshes are in the station frame of the upper ("male") flange, vertices [X, Y, U]:
// X across the seam into the male petal (the bed-side "female" wall is X -5…0), Y toward the shell, U along the seam.
import {prism,cylinder,loft,hullPoints} from './solid.js';
import {WEDGE,wedgeKey,CLIP} from './params.js';
const W=CLIP.wall,PI=Math.PI;
// Extrude a polygon along one station axis; vertices come out as [X, Y, U]. Mirrored maps are fine: the kernel keeps
// the solid closed and outward-facing.
function ext(poly,axis,a,b){const s=prism(poly,a,b);
 return axis==='U'?s:axis==='Y'?s.transform(([x,y,z])=>[x,z,y]):s.transform(([x,y,z])=>[z,y,x]);}
const rect=(x0,y0,x1,y1)=>[[x0,y0],[x1,y0],[x1,y1],[x0,y1]];
function hull2(points){const p=[...points].sort((a,b)=>a[0]-b[0]||a[1]-b[1]),cross=(o,a,b)=>(a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]),lo=[],hi=[];
 for(const q of p){while(lo.length>1&&cross(lo[lo.length-2],lo[lo.length-1],q)<=1e-12)lo.pop();lo.push(q);}
 for(const q of p.reverse()){while(hi.length>1&&cross(hi[hi.length-2],hi[hi.length-1],q)<=1e-12)hi.pop();hi.push(q);}
 return lo.slice(0,-1).concat(hi.slice(0,-1));}
// Slot section in (U, Y): the rectangle plus, where a ceiling would face down more than ~43° in the side print, a 90°
// gable toward print-up (du, dw: print-up projected into the section, as roofToward does for round bores). The apex is the
// lowest point whose two 45° lines clear every corner, so no edge of the hull is a ceiling.
export function slotSection(du=0,dw=0,su=WEDGE.slotU,sy=WEDGE.slotY,yc=WEDGE.yc){
 const corners=rect(-su/2,yc-sy/2,su/2,yc+sy/2),l=Math.hypot(du,dw);if(l<.66)return corners;
 const up=[du/l,dw/l],across=[-up[1],up[0]],c=[0,yc],rel=corners.map(([u,y])=>[u-c[0],y-c[1]]),
  apex=Math.max(...rel.map(r=>r[0]*up[0]+r[1]*up[1]+Math.abs(r[0]*across[0]+r[1]*across[1])));
 return hull2([...corners,[c[0]+apex*up[0],c[1]+apex*up[1]]]);}
// Extent along the seam (s, from the station) of a slot at station tilt t with a gable either way: the in-section
// print-up of both flanges of a seam lies along ±(cos t, -sin t), so this is the same on both sides of the seam.
export function slotSpan(t){const c=Math.cos(t),sn=Math.sin(t),v=[...slotSection(c,-sn),...slotSection(-c,sn)].map(([U,Y])=>U*c-Y*sn);return[Math.min(...v),Math.max(...v)];}
// Station cuts and additions for one flange (its own station frame F, X into its own petal).
// lug: deepens the wall under the slot by WEDGE.lug.depth with 45° ends; on a flange whose outer face looks down in the
// side print (the upper flange) its outer edge is chamfered 45° below the first `flush` mm, continuing the gusset line.
export function wedgeStation({F,Yb,upU,upT,tilt}){const L=WEDGE.lug,chamfer=upT<-.6,
 ring=(Y,half,xOut)=>[[-.5,-half],[xOut,-half],[xOut,half],[-.5,half]].map(([X,U])=>F(X,Y,U));
 const lug=loft([ring(Yb+1,L.half+1,W),ring(Yb-L.flush,L.half-L.flush,W),ring(Yb-L.depth,L.half-L.depth,chamfer?W-(L.depth-L.flush):W)]);
 const sec=slotSection(Math.cos(tilt)*upU,-Math.sin(tilt)*upU),slot=loft([-1,W+1].map(X=>sec.map(([U,Y])=>F(X,Y,U))));
 return{lug,slot};}
// V-register, in straight segments between the stations. A ridge that follows the shell along the seam cannot slide in
// with the final petal: that petal moves radially, so in its last vh/sin(h) of travel it is still (remaining)·cos h out
// along the seam, which on a seam rising at angle t puts the ridge (remaining)·cos h·sin t off the groove across its
// flanks, more than the flank opening for 8 or more petals. A straight segment tilted β from horizontal is off by only
// (remaining)·cos h·sin β, which the 50° flanks absorb when sin β ≤ tan h / tan 50°; so each segment follows the shell
// slope where that allows, and is flatter (crossing the wall depth) where it does not. Horizontal segments register the
// dish-axis direction, which at a radial seam is the reflector step.
export function registerDims(){const R=WEDGE.ridge,tf=Math.tan(R.flank*PI/180),w0=R.top/2+R.h/tf,wg0=R.top/2+R.clear+R.h/tf;
 return{tf,w0,wg0,mouth:wg0+R.relief,apex:R.h+(R.top/2+R.clear)*tf};}
export function wedgeRegister({point,back,depth,tiltAt,stations,tilts,range:[lo,hi],h,radial,stagger,male,upU,upT}){
 const R=WEDGE.ridge,{tf,w0,wg0,mouth,apex}=registerDims(),slide=radial?1/Math.tan(h):stagger?Math.tan(h/2):0,
  betaMax=Math.asin(Math.min(1,slide>1e-9?.9/(slide*tf):1)),lead=R.h*slide+1,dTop=1,dBot=1.5;
 // free intervals along the seam: clear of every slot (with any gable) by 2 mm, and of the seam ends
 const blocked=stations.map((s,i)=>{const[a,b]=slotSpan(tilts[i]);return[s+a-2,s+b+2];});
 const free=[];let at=lo;for(const[b0,b1]of blocked){if(b0>at)free.push([at,Math.min(b0,hi)]);at=Math.max(at,b1);}if(at<hi)free.push([at,hi]);
 const out=[];
 for(const[sA,sB]of free){if(sB-sA<2*lead+2*R.h+R.minFlat)continue;const sm=(sA+sB)/2,t=tiltAt(sm),beta=Math.sign(t)*Math.min(Math.abs(t),betaMax),cb=Math.cos(beta),sb=Math.sin(beta),band=mouth/cb;
  const fit=L=>{let zmin=-Infinity,zmax=Infinity;for(let k=0;k<=32;k++){const lam=-L+2*L*k/32,s=sm+lam*cb,g=back(s)-lam*sb;zmin=Math.max(zmin,g-(depth(s)-dBot-band));zmax=Math.min(zmax,g-(dTop+band));}return zmin<=zmax?(zmin+zmax)/2:null;};
  let a=0,b=Math.max(0,((sB-sA)/2-mouth*Math.abs(sb))/cb);if(fit(b)===null){for(let i=0;i<30;i++){const m=(a+b)/2;if(fit(m)===null)b=m;else a=m;}b=a;}
  const L=b,zc=fit(L),ridge=L-lead;if(zc===null||ridge-R.h<R.minFlat/2)continue;
  // local (λ along the segment, μ across it in the seam plane, t across the seam into this flange) → petal coordinates
  const P=(lam,mu,tt)=>point(sm+lam*cb-mu*sb,tt,zc+lam*sb+mu*cb),O=P(0,0,0),dir=(lam,mu,tt)=>P(lam,mu,tt).map((x,k)=>x-O[k]);
  const plane=(nl,d)=>{const n=dir(...nl),l=Math.hypot(...n),u=n.map(x=>x/l);return[u,d/Math.hypot(...nl)+u[0]*O[0]+u[1]*O[1]+u[2]*O[2]];};
  const extrude=(poly,l0,l1)=>loft([l0,l1].map(lam=>poly.map(([mu,tt])=>P(lam,mu,tt))));
  if(male){// ridge on the mating face (t < 0), embedded 0.4 mm into the wall, ends tapered 45°
   let s=extrude([[-w0,.4],[-w0,0],[-R.top/2,-R.h],[R.top/2,-R.h],[w0,0],[w0,.4]],-ridge-1,ridge+1);
   for(const[nl,d]of [[[-1,0,1],-ridge],[[1,0,1],-ridge]]){const[u,off]=plane(nl,d);s=s.trim(u,off);}
   out.push({solid:s,s:sm,beta,length:2*ridge,z:zc});
  }else{// groove in the mating face (t > 0): 50° flanks, V bottom, mouth relief for first-layer flare; ends tilted 45°
   // only where a square end would be a ceiling in the side print
   const wr=wg0-R.reliefDepth/tf,poly=[[-mouth,-.5],[-mouth,0],[-wr,R.reliefDepth],[0,apex],[wr,R.reliefDepth],[mouth,0],[mouth,-.5]].reverse();
   let s=extrude(poly,-L-2,L+2);const lu=Math.cos(beta)*upU,k=sign=>sign*lu>=-.6?0:Math.sign(upT)||1;
   for(const[nl,d]of [[[-1,0,k(-1)],-L],[[1,0,k(1)],-L]]){const[u,off]=plane(nl,d);s=s.trim(u,off);}
   out.push({solid:s,s:sm,beta,length:2*L,z:zc});}
 }
 return out;}

// ---- printed parts (station frame [X, Y, U]) ----
const K=WEDGE;
// T-key: shank through both slots, L-shaped head on the male outer face (flush with the shank's -U face, so the key
// prints lying on that face with the pull along the layers), wedge window past the female wall, lead-in chamfers.
export function keySolid(clear=K.clear){const {u:ku,y:ky}=wedgeKey(clear),yc=K.yc,xt=-W-K.c-K.tail,ch=K.chamfer,hh=K.head.half;
 const shank=ext([[xt,yc-ky/2+ch],[xt+ch,yc-ky/2],[W+.01,yc-ky/2],[W+.01,yc+ky/2],[xt+ch,yc+ky/2],[xt,yc+ky/2-ch]],'U',-ku/2,ku/2);
 const head=ext(rect(W,yc-hh,W+K.head.t,yc+hh),'U',-ku/2,-ku/2+K.head.u);
 const window=ext(rect(-W-K.c,yc-K.window.y/2,-W+K.window.inboard,yc+K.window.y/2),'U',-ku,ku);
 return shank.union(head).subtract(window);}
// Wedge geometry along U for a taper 1:T, seated at zero gap: its tapered face touches the key window's trailing corner.
// Along the seam it reaches uTip…uEnd seated, and `travel` mm either way covers print tolerance and seating.
export function wedgeLayout(taper=K.taper){const run=(K.run/K.taper)*taper,tmax=K.tip+run/taper,uTip=wedgeKey().u/2-(K.c-K.tip)*taper;
 return{run,tmax,uTip,uTaperEnd:uTip+run,uPad:uTip+run,uEnd:uTip+run+K.pad.u,draw:run/taper,closes:K.c-K.tip,reserve:tmax-K.c};}
// Wedge: a 2.7 mm plate (prints flat) whose flat face slides on the female outer face; 0.9 mm tip, 1:12 taper, then a
// thumb pad standing 11 mm out from the wall, which meets the key's tail when the 0.4 mm reserve is used up. Optional
// click bumps on the tapered face.
export function wedgeSolid(taper=K.taper,bumps=false){const g=wedgeLayout(taper),x0=-W,r=K.pad.r,px=x0-K.pad.x,arc=(cx,cu,a0)=>Array.from({length:5},(_,i)=>{const a=a0+i*PI/8;return[cx+r*Math.cos(a),cu+r*Math.sin(a)];});
 const poly=[[x0,g.uTip],[x0,g.uEnd],...arc(px+r,g.uEnd-r,PI/2),...arc(px+r,g.uPad+r,PI),[x0-g.tmax,g.uPad],[x0-K.tip,g.uTip]];
 // keep the outline counter-clockwise in (X, U)
 let area=0;for(let i=0;i<poly.length;i++){const a=poly[i],b=poly[(i+1)%poly.length];area+=a[0]*b[1]-b[0]*a[1];}if(area<0)poly.reverse();
 let s=ext(poly,'Y',K.yc-K.wy/2,K.yc+K.wy/2);
 if(bumps)for(let u=g.uTaperEnd-1.5;u>g.uTaperEnd-13;u-=2.5){const x=x0-(K.tip+(u-g.uTip)/taper)+.15;s=s.union(cylinder([x,K.yc-K.wy/2,u],[x,K.yc+K.wy/2,u],.3,12));}
 return s;}
// Thumb-screw option, same slot: an M4 × 20 hex-head bolt from the male side, its head in a hex pocket in a drift keeper
// so it cannot turn (the keeper's boss is the key's section and enters the female slot 1 mm, pulling the walls into
// line), into an M4 nut pressed into a Ø12 knurled thumb nut on the female side (Ø12 is the most that turns clear of the
// shell above the slot). Finger-tight, no tools.
export function keeperSolid(){const {u:ku,y:ky}=wedgeKey(),yc=K.yc,hh=K.head.half,t=K.keeper.t,x1=W+t;
 const body=ext(rect(W,yc-hh,x1,yc+hh),'U',-6,6),r=(X,d)=>[[X,yc-ky/2+d,-ku/2+d],[X,yc-ky/2+d,ku/2-d],[X,yc+ky/2-d,ku/2-d],[X,yc+ky/2-d,-ku/2+d]];
 const boss=loft([r(W+.01,0),r(-K.keeper.boss+.5,0),r(-K.keeper.boss,.5)]);
 return body.union(boss).subtract(cylinder([-3,yc,0],[x1+1,yc,0],K.thumbNut.hole/2,24)).subtract(cylinder([x1-K.keeper.pocketDepth,yc,0],[x1+1,yc,0],K.keeper.headAF/Math.sqrt(3),6));}
export function thumbNutSolid(){const N=K.thumbNut,yc=K.yc,x0=-W-N.washer,x1=x0-N.length;
 return cylinder([x1,yc,0],[x0,yc,0],N.d/2,24).subtract(cylinder([x1-1,yc,0],[x0+1,yc,0],N.hole/2,24)).subtract(cylinder([x1-1,yc,0],[x1+N.nutDepth,yc,0],N.nutAF/Math.sqrt(3),6));}
// Room the station's hardware needs beside the walls, cleared from neighbouring flanges where seams meet (as the clip
// and bolt seats are): on the upper flange the key head or the keeper, on the bed-side flange the key's tail or the
// thumb nut. In the flange's own frame, t from the wall's outer face outward.
// Where it notches a neighbouring flange the cut must have no ceiling in the side print, so the box gets a roof toward
// print-up (up: print-up in the flange's (t, Y, U)), steep enough that every line from its apex to a corner rises 45°,
// trimmed back to the wall's outer face so it never cuts the wall itself.
export function wedgeSeat(F,male,up=[1,0,0]){const yc=K.yc,box=(t1,y0,y1,u)=>{const c=[(W+t1)/2,(y0+y1)/2,0],corners=[];
  for(const t of [W,t1])for(const Y of [y0,y1])for(const U of [-u,u])corners.push([t,Y,U]);
  const d=Math.max(...corners.map(q=>{const r=q.map((x,k)=>x-c[k]),a=r[0]*up[0]+r[1]*up[1]+r[2]*up[2];return a+Math.hypot(...r.map((x,k)=>x-a*up[k]));}));
  const o=F(0,0,0),n=F(1,0,0).map((x,k)=>x-o[k]),w=F(W,0,0);
  return hullPoints([...corners,c.map((x,k)=>x+d*up[k])].map(q=>F(...q))).trim(n,n[0]*w[0]+n[1]*w[1]+n[2]*w[2]);};
 return male?box(W+Math.max(K.head.t,K.keeper.t)+.5,yc-K.head.half-.5,yc+K.head.half+.5,K.head.u-wedgeKey().u/2+.5):box(W+K.thumbNut.washer+K.thumbNut.length+1,yc-K.thumbNut.d/2-.5,yc+K.thumbNut.d/2+.5,K.thumbNut.d/2+.5);}
// Fit-test shim: a flat plate t thick with 1–3 notches on one edge to tell the sizes apart.
export function shimSolid(t,notches){const pts=[[0,0],[24,0],[24,12]];for(let i=1;i<=notches;i++){const x=24-i*5;pts.push([x+1.5,12],[x,10],[x-1.5,12]);}pts.push([0,12]);
 return ext(pts,'U',0,t);}
// Print orientations (all proper rotations of [X, Y, U]):
export const PRINT={key:([X,Y,U])=>[X,Y,U],wedge:([X,Y,U])=>[U,X,Y],keeper:([X,Y,U])=>[U,Y,-X],nut:([X,Y,U])=>[U,Y,-X],shim:([X,Y,U])=>[X,Y,U]};
