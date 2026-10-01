import {patch,bounds,printMesh,rotateBed,packParts,zAt} from './mesh.js';
import {solidScope,solid,cylinder,loft} from './solid.js';
import {feedFits,feedDatum,appendFeedParts} from './feed.js';
export {binarySTL,volume,zip,packedPlateMesh,bounds,printMesh,rotateBed,zAt} from './mesh.js';
export const defaults={feedMode:0,feedLegs:3,rodDiameter:6.35,rodClearance:.35,feedPayload:100,phaseUnits:0,autoSecondary:1,secondaryWaves:4,phaseOffset:0,secondaryPosition:.82,backFocus:-20,frequencyGHz:0,diameter:400,fd:.42,thickness:2.4,bedX:220,bedY:220,bedZ:250,margin:8,gap:.4,resolution:5,sectors:0,rows:0,rearStyle:0,facetSize:20,jointClearance:.2,packPlates:1,staggerRings:1,seamJoint:0,clipFit:.05,hubFlat:1,rootThrough:0,mountThrough:1,insertDiameter:5.6};
export const limits={feedMode:[0,2],feedLegs:[3,4],rodDiameter:[0,8],rodClearance:[.15,.7],feedPayload:[1,2000],phaseUnits:[0,1],autoSecondary:[0,1],secondaryWaves:[2,10],phaseOffset:[-150,150],secondaryPosition:[.6,.95],backFocus:[-200,-5],frequencyGHz:[0,100],diameter:[260,1200],fd:[.25,.8],thickness:[1.6,6],bedX:[140,1000],bedY:[140,1000],bedZ:[60,1000],margin:[2,20],gap:[.2,1],resolution:[2,10],sectors:[0,24],rows:[0,8],rearStyle:[0,1],facetSize:[10,30],jointClearance:[.1,.4],packPlates:[0,1],staggerRings:[0,1],seamJoint:[0,2],clipFit:[0,.1],hubFlat:[0,1],rootThrough:[0,1],mountThrough:[0,1],insertDiameter:[5.2,6]};
export const ROOT={seatR:4.8,seatDepth:5};
// Snap clip: a solid trapezoid block with a channel. It snaps straight up over both flange pads from behind:
// its tapered jaws spread over the pads, and a 45° lip at the top of each jaw hooks the pad's chamfered top edge.
// It prints standing on its wide base; every face is 45° or steeper. Pry off with a flat screwdriver under the shell.
// Profile: X across the seam, Y toward the shell, Y = 0 at the bolt axis (pads span Y -5..5, flange bottom -7, shell 7).
export const CLIP={width:10,lip:.3,padChamfer:.5,tip:2.5,root:4,top:6,floor:-8,bottom:-13,fillet:1.5,clear:.4,slotBottom:12};
const clipOuter=(a0,Y)=>a0+CLIP.root+(CLIP.tip-CLIP.root)*(Y-CLIP.floor)/(CLIP.top-CLIP.floor);
export function clipSolid(fit,map){const C=CLIP,a0=5-fit,y0=9.55-a0,parts=[],ext=poly=>loft([-C.width/2,C.width/2].map(U=>poly.map(([X,Y])=>map(X,Y,U))));
 for(const g of [1,-1]){const m=pts=>pts.map(([X,Y])=>[g*X,Y]);
  parts.push(ext(m([[a0,C.floor],[clipOuter(a0,C.floor),C.floor],[clipOuter(a0,C.top),C.top],[a0+.3,C.top],[a0,y0]])));
  parts.push(ext(m([[a0+.02,y0-.02],[a0-C.lip,y0+C.lip],[a0-C.lip,y0+C.lip+.15],[a0+.3,C.top]])));
  parts.push(ext(m([[a0+.02,C.floor+C.fillet],[a0+.02,C.floor-.02],[a0-C.fillet,C.floor-.02]])));}
 const b=Y=>clipOuter(a0,Y);parts.push(ext([[-b(C.floor),C.floor],[b(C.floor),C.floor],[b(C.bottom),C.bottom],[-b(C.bottom),C.bottom]]));
 return parts.reduce((x,y)=>x.union(y));}
// Peak jaw strain while the lip rides over a pad: tapered cantilever from the channel floor, loaded at the lip.
export function clipStrain(p){const C=CLIP,a0=5-p.clipFit,yl=9.55-a0+C.lip,N=400;let d=0,em=0;const h=Y=>clipOuter(a0,Y)-a0;
 for(let i=0;i<N;i++){const Y=C.floor+(yl-C.floor)*(i+.5)/N,x=yl-Y,H=h(Y);d+=x*x/(H**3/12)*(yl-C.floor)/N;}
 const P=(C.lip+p.clipFit)/d;for(let i=0;i<=N;i++){const Y=C.floor+(yl-C.floor)*i/N;em=Math.max(em,6*P*(yl-Y)/h(Y)**2);}return em;}
export const JOINT={revision:12,wall:3,depth:14,boss:5,keyDepth:.8,insertDepth:7,insertMaxLength:6};
const PI=Math.PI,TAU=2*PI;
export function validate(p){for(const[k,[a,b]]of Object.entries(limits))if(!Number.isFinite(p[k])||p[k]<a||p[k]>b)throw Error(`${k} must be between ${a} and ${b}.`);for(const k of ['feedMode','feedLegs','phaseUnits','autoSecondary','sectors','rows','rearStyle','packPlates','staggerRings','seamJoint','hubFlat','rootThrough','mountThrough'])if(!Number.isInteger(p[k]))throw Error(k+' must be a whole-number option.');if(p.sectors&&(p.sectors<6||p.sectors%2))throw Error('Choose automatic or an even petal count from 6 to 24.');if(![0,4,5,6,6.35,8].includes(p.rodDiameter))throw Error('Choose available solid rod stock.');if(p.feedMode&&p.sectors&&p.sectors%p.feedLegs)throw Error('Petal count must be divisible by the rod count.');return p;}
export function backZ(x,y,p){if(!p.rearStyle)return zAt(Math.hypot(x,y),p)-p.thickness;const q=p.facetSize,X=Math.round(x/q)*q,Y=Math.round(y/q)*q;return(2*X*x+2*Y*y-X*X-Y*Y)/(4*p.diameter*p.fd)-p.thickness;}
export const rootBottom=p=>zAt(45,p)-10;
// Flat hub front (default) sits at the height where the petals start (r 45): easier to print, and it is in the feed's shadow.
// Curved follows the parabola. Mount seats are 5 mm below the lowest point of the front around them.
export const hubFace=p=>zAt(45,p);
export const hubSeatFloor=p=>(p.hubFlat?hubFace(p):zAt(25,p))-5;
export const hubMountGrip=p=>hubSeatFloor(p)-(rootBottom(p)-6);
export function rowBounds(p,n,rows,j){const end=p.diameter/2*Math.cos(PI/n);return[45+(end-45)*j/rows,j===rows-1?Infinity:45+(end-45)*(j+1)/rows];}
export function sidePrint(mesh,n){const h=PI/n,c=Math.cos(h),s=Math.sin(h);return printMesh({v:mesh.v.map(([x,y,z])=>[c*x-s*y,-z,s*x+c*y]),f:mesh.f});}
function chooseBed(mesh,p){for(let yaw=0;yaw<180;yaw+=15){const m=rotateBed(mesh,yaw),dim=bounds(m).size;if(dim[0]<=p.bedX-2*p.margin&&dim[1]<=p.bedY-2*p.margin&&dim[2]<=p.bedZ-2)return{mesh:m,dim,yaw};}return null;}
export function plan(p){validate(p);if(Math.min(p.bedX,p.bedY)-2*p.margin<120)throw Error('The 120 mm hub needs more usable bed space.');let best;for(const rows of p.rows?[p.rows]:[1,2,3,4,5,6,7,8])for(const n of p.sectors?[p.sectors]:[6,8,10,12,14,16,18,20,22,24]){if(best&&rows*n>=best.n*best.rows)continue;if(!feedFits(p,n,rows))continue;const h=PI/n,R=p.diameter/2,span=(R-45/Math.cos(h))/rows;if(span<66||45*Math.tan(h)<8.5)continue;let ok=true;for(let j=0;j<rows;j++){const[a,b]=rowBounds(p,n,rows,j),r1=Math.min(R,b/Math.cos(h));if(rows>1&&j<rows-1&&(p.staggerRings?2*b*Math.tan(h/2)<36:2*b*Math.tan(h)<62)){ok=false;break;}const spec={r0:a,r1,a0:-h,a1:h,backFn:(x,y)=>backZ(x,y,p)-17};let m=solidScope(()=>solid(patch(spec,p)).trim([1,0,0],a).mesh());if(!chooseBed(sidePrint(m,n),p)){ok=false;break;}}if(ok)best={n,rows,choices:[]};}if(!best)throw Error('No side-printed layout fits. Use automatic segmentation, a larger print volume, or a smaller dish. Flange joints need enough room for hardware and tools.');return best;}
function clipped(s,p,n,a,b,gap=0){const h=PI/n;
 s=s.trim([Math.sin(h),Math.cos(h),0],gap/2).trim([Math.sin(h),-Math.cos(h),0],gap/2);
 if(p.staggerRings){
  // One shared 2n-sided polygon at each ring boundary. Each petal spans two faces.
  if(a>45.001){let inner=cylinder([0,0,-100],[0,0,p.diameter],p.diameter,32);for(const t of [-h/2,h/2])inner=inner.trim([-Math.cos(t),-Math.sin(t),0],-a-gap/2);s=s.subtract(inner);}else s=s.trim([1,0,0],a+gap/2);
  if(Number.isFinite(b))for(const t of [-h/2,h/2])s=s.trim([-Math.cos(t),-Math.sin(t),0],-b+gap/2);
 }else s=s.trim([1,0,0],a+gap/2).trim([-1,0,0],Number.isFinite(b)?-b+gap/2:-p.diameter);
 return s.intersect(cylinder([0,0,-100],[0,0,p.diameter],p.diameter/2,Math.max(96,Math.ceil(PI*p.diameter/p.resolution))));}
// Integral edge flange: a 3 mm wall with a 45° root gusset and solid screw pads.
function flange(p,frame,start,end,male,h,keyed=true,overlap=0){const {o,e,v}=frame,point=(s,t,z)=>[o[0]+s*e[0]+t*v[0],o[1]+s*e[1]+t*v[1],z],back=s=>{const q=point(s,0,0);return backZ(q[0],q[1],p);},backT=(s,t)=>{const q=point(s,t,0);return backZ(q[0],q[1],p);};
 // Station height. With clips, drop it by the shell's slope across the clip so the jaws and lips clear the shell everywhere.
 const level=s=>p.seamJoint?back(s)-7-Math.abs(backT(s,1)-backT(s,-1))/2*7.6-Math.abs(back(s+1)-back(s-1))/2*5.2:back(s)-7;const span=end-start,keyHalf=keyed?2+(JOINT.keyDepth+.6)/Math.tan(h)+.4+1:0,countBolts=p.seamJoint?(keyed?2*Math.max(1,Math.min(Math.ceil(span/120),Math.floor(span/(4*(keyHalf+7.5))))):Math.max(1,Math.min(2*Math.ceil(span/120),Math.floor(span/34)))):2*Math.max(1,Math.ceil(span/240)),stations=Array.from({length:countBolts},(_,i)=>start+(end-start)*(i+.5)/countBolts),mid=(start+end)/2,cs=male?.18:0;
 const profile=s=>{const d=male&&o[0]===0?Math.min(14,.5+(s-start)*13.5/Math.min((end-start)/4,20*Math.sin(PI/3)/Math.sin(2*h))):14;const outline=male?[[cs,-d],[3,-d],[3+d,0],[3+d,.5],[cs,.5]]:[[cs,-14],[3,-14],[3,-4],[7,0],[7,.5],[cs,.5]];return outline.map(([t,z])=>{const q=point(s,t,0);return point(s,t,backZ(q[0],q[1],p)+z);});};
 const count=Math.max(2,Math.ceil((end-start+2*overlap)/p.resolution)),rings=Array.from({length:count+1},(_,i)=>profile(start-overlap+(end-start+2*overlap)*i/count));let body=loft(rings);
 const prism=(s0,s1,t0,t1,z0,z1)=>loft([s0,s1].map(s=>[[t0,z0],[t1,z0],[t1,z1],[t0,z1]].map(([t,z])=>point(s,t,z))));
 const cuts=[];
 // Print-up in this flange's frame: faces whose normal is -sg·e point down; psi tilts them to 45° or better.
 const upU=e[0]*Math.sin(h)+e[1]*Math.cos(h),upT=v[0]*Math.sin(h)+v[1]*Math.cos(h),sg=upU<0?-1:1;let psi=0;
 for(let d=0;d<=60;d++){const ok=[d,-d].find(x=>-Math.abs(upU)*Math.cos(x*PI/180)-upT*Math.sin(x*PI/180)>=-.68);if(ok!==undefined){psi=ok*PI/180;break;}}
 const padPoly=s=>[[s-sg*(6-5*Math.tan(psi)),0],[s+sg*6,0],[s+sg*6,5],[s-sg*6,5]];
 let pads=null;for(const s of stations){const z=level(s),pad=loft([z-5,z+5].map(zz=>padPoly(s).map(([ss,t])=>point(ss,t,zz))));pads=pads?pads.union(pad):pad;body=body.union(pad);const r=1.7,round=Array.from({length:24},(_,i)=>{const a=TAU*i/24;return[r*Math.cos(a),r*Math.sin(a)];});const roof=[[-r*.707,-r*.707],[r*.707,-r*.707],[r*Math.SQRT2,0],[r*.707,r*.707],[-r*.707,r*.707]];
 const extrude=poly=>loft([-1,9].map(t=>poly.map(([u,w])=>point(s+u,t,z+w))));if(p.seamJoint!==1)cuts.push(extrude(round).union(extrude(roof)));
}
 // Flat seats square to the bolt on both sides: the pad face (t = 5) is exposed through the gusset or fillet.
 // Loose nut and washer; nothing captured. Cut from flanges only (panelSolid), so the shell is never opened.
 let seats=null;for(const s of stations){const z=level(s),R=5,ring=Array.from({length:32},(_,i)=>{const a=TAU*i/32;return[R*Math.cos(a),R*Math.sin(a)];}),roofR=[[-R*.707,-R*.707],[R*.707,-R*.707],[R*Math.SQRT2,0],[R*.707,R*.707],[-R*.707,R*.707]];
 const bore=poly=>loft([5,21].map(t=>poly.map(([u,w])=>point(s+u,t,z+w))));let seat=p.seamJoint!==1?bore(ring).union(bore(roofR)):null;
 if(p.seamJoint){ // clip recess: a slot past the pad for the jaw and its lip, and 45° chamfers on the pad's top (catch) and bottom (lead-in) edges
  const T=5+CLIP.root+.6,wc=CLIP.width/2+CLIP.clear,tan=Math.tan(psi),wc0=wc+Math.max(0,tan)*(T-5);
  const slotPoly=t0=>[[-sg*wc,t0],[sg*(wc0-tan*(t0-5)),t0],[sg*(wc0-tan*(T-5)),T],[-sg*wc,T]];
  const slot=loft([z-CLIP.slotBottom,back(s)+6].map(zz=>slotPoly(5).map(([u,t])=>point(s+u,t,zz)))).union(loft([z+4.2,back(s)+6].map(zz=>slotPoly(4.2).map(([u,t])=>point(s+u,t,zz)))));seat=seat?seat.union(slot):slot;
  const c=CLIP.padChamfer;for(const k of [1,-1])cuts.push(loft([-6.6,6.6].map(u=>[[5.05,k*(5-c-.05)],[5.05,k*5.05],[5-c-.05,k*5.05]].map(([t,w])=>point(s+sg*u,t,z+w)))));}
 seats=seats?seats.union(seat):seat;}
 const z=back(mid)-4.5,depth=JOINT.keyDepth,tip=2,base=tip+(depth+.6)/Math.tan(h)+.4,c=p.jointClearance;
 const key=(clear)=>loft([[-depth-clear,tip+clear,1.3+clear],[.6,base+clear,1.5+clear]].map(([t,w,H])=>[[-w,-H],[w,-H],[w,H],[-w,H]].map(([s,dz])=>point(mid+s,t,z+dz))));
 if(keyed&&male){body=body.union(prism(mid-base-1,mid+base+1,0,3,z-2.5,z+2.5));}else if(keyed){ // mirror matching male key into this flange
 cuts.push(key(c).transform(q=>{const dx=q[0]-o[0],dy=q[1]-o[1],t=dx*v[0]+dy*v[1];return[q[0]-2*t*v[0],q[1]-2*t*v[1],q[2]];}));}
 return{body,seats,pads,keyAddition:male&&keyed?key(0):null,cuts,stations,levels:stations.map(level),key:mid,frame,start,end};}
export function panelSolid(p,n,rows,j,feed=false){const h=PI/n,[a,b]=rowBounds(p,n,rows,j),R=p.diameter/2;const lines=[];if(p.rearStyle)for(let k=-Math.ceil(R/p.facetSize);k<=Math.ceil(R/p.facetSize);k++)for(const normal of [[1,0],[0,1]])lines.push([...normal,(k+.5)*p.facetSize]);let body=clipped(solid(patch({r0:Math.max(1,a-2),r1:R,a0:-h,a1:h,backFn:(x,y)=>backZ(x,y,p),creaseLines:lines},p)),p,n,a,b,p.gap);
 const flanges=[];for(const sign of [-1,1])flanges.push(flange(p,{o:[0,0],e:[Math.cos(h),sign*Math.sin(h)],v:[Math.sin(h),-sign*Math.cos(h)]},Math.max(63,a/Math.cos(p.staggerRings&&j>0?h/2:h)+1),Math.min(R,Number.isFinite(b)?b/Math.cos(p.staggerRings?h/2:h):R)-1,sign===1,h));
 if(p.staggerRings){for(const [r,male] of [[a,false],[b,true]])if((male&&j<rows-1)||(!male&&j>0))for(const t of [-h/2,h/2]){
  const c=Math.cos(t),s=Math.sin(t),L=r*Math.tan(h/2);
  flanges.push(flange(p,{o:[r*c,r*s],e:[-s,c],v:male?[-c,-s]:[c,s]},-L,L,male,h,false,20*Math.tan(h/2)+1));
 }}else{
 if(j>0)flanges.push(flange(p,{o:[a,0],e:[0,1],v:[1,0]},-a*Math.tan(h),a*Math.tan(h),false,h));
 if(j<rows-1)flanges.push(flange(p,{o:[b,0],e:[0,1],v:[-1,0]},-b*Math.tan(h),b*Math.tan(h),true,h));
 }
 // Clip flange bodies first; add keys outside the mating plane afterwards.
 // Seats clear every flange's gusset (a seat near a corner also opens the neighbor's), never the shell or a bolt pad.
 for(const f of flanges){let fb=f.body;for(const g of flanges)if(g.seats)fb=fb.subtract(g.seats);body=body.union(fb.union(f.pads));}
 body=clipped(body,p,n,a,b);
 for(const f of flanges)if(f.keyAddition)body=body.union(f.keyAddition);
 if(j===0){const bottom=rootBottom(p);let boss=solid(patch({r0:43,r1:64/Math.cos(h),a0:-h,a1:h,topFn:(x,y)=>backZ(x,y,p)+.5,backFn:()=>bottom},p)).trim([1,0,0],45).trim([-1,0,0],-63).trim([0,-1,0],-8);body=body.union(boss);
  if(p.rootThrough){ // M4 through bolt: head and washer recessed in a flat seat in the front, nut on the hub's rear face
   const up=[Math.sin(h),Math.cos(h)],across=[Math.cos(h),-Math.sin(h)],drop=r=>[...Array.from({length:32},(_,i)=>{const a=TAU*i/32;return[r*Math.cos(a),r*Math.sin(a)];})],roofR=r=>[[-r*.707,-r*.707],[r*.707,-r*.707],[r*.707,r*.707],[0,r*Math.SQRT2],[-r*.707,r*.707]];
   const bore=(poly,z0,z1)=>loft([z0,z1].map(z=>poly.map(([a,b])=>[52.5+a*across[0]+b*up[0],a*across[1]+b*up[1],z])));
   const seat=zAt(52.5-ROOT.seatR,p)-ROOT.seatDepth,top=zAt(70,p)+5;
   body=body.subtract(bore(drop(2.25),bottom-1,top).union(bore(roofR(2.25),bottom-1,top)).union(bore(drop(ROOT.seatR),seat,top)).union(bore(roofR(ROOT.seatR),seat,top)));
  }else body=body.subtract(cylinder([52.5,0,bottom-1],[52.5,0,bottom+7],p.insertDiameter/2));}
 if(feed){const d=feedDatum(p);const pad=solid(patch({r0:d.r-12,r1:R,a0:-16/d.r,a1:16/d.r,backFn:x=>d.rear+d.slope*(x-d.r)},p));body=body.union(pad);for(const q of d.holes){const x=q.r*Math.cos(q.a),y=q.r*Math.sin(q.a);body=body.subtract(cylinder([x,y,d.rear-15],[x,y,d.front+5],1.7));}}
 for(const f of flanges)for(const cut of f.cuts)body=body.subtract(cut);
 return{body,spec:{a,b,row:j,flanges:flanges.map(({stations,levels,key,frame,start,end})=>({stations,levels,key,frame,start,end})),feedMount:feed}};}
function hubSolid(p,n){const bottom=rootBottom(p)-6,top=rootBottom(p),R=60;const face=hubFace(p);let center=solid(patch({r0:15,r1:R,a0:0,a1:TAU,...(p.hubFlat?{topFn:()=>face}:{}),backFn:()=>bottom},p));for(let i=0;i<n;i++){const a=i*TAU/n;center=center.trim([-Math.cos(a),-Math.sin(a),0],-45+p.gap/2);}let hub=cylinder([0,0,bottom],[0,0,top],R,128).subtract(cylinder([0,0,bottom-1],[0,0,top+1],15,96)).union(center);for(let i=0;i<n;i++){const a=i*TAU/n,x=52.5*Math.cos(a),y=52.5*Math.sin(a);hub=hub.subtract(cylinder([x,y,bottom-1],[x,y,top+1],2.3));}for(let i=0;i<4;i++){const a=PI/4+i*PI/2,x=30*Math.cos(a),y=30*Math.sin(a);hub=hub.subtract(cylinder([x,y,bottom-1],[x,y,p.mountThrough?p.diameter:bottom+7],p.mountThrough?2.3:p.insertDiameter/2));if(p.mountThrough)hub=hub.subtract(cylinder([x,y,hubSeatFloor(p)],[x,y,p.diameter],5,48));}return hub;}
export function build(input){const p=validate({...defaults,...input}),layout=plan(p);return solidScope(()=>{const{n,rows}=layout,parts=[],instances=[],step=TAU/n;const add=(id,name,mesh,qty,angles,kind,row,spec={})=>{const pm=kind==='panel'?sidePrint(mesh,n):printMesh(mesh),choice=chooseBed(pm,p);if(!choice)throw Error(name+' exceeds the print volume after adding joints. Increase print volume or segmentation.');const part={id,name,mesh,print:choice.mesh,output:choice.mesh,dim:choice.dim,qty,kind,row,spec,angle:90,bedRotation:choice.yaw,supportMeshes:[]};parts.push(part);angles.forEach(a=>instances.push({part,a}));return part;};
 for(let j=0;j<rows;j++)for(const mount of [false,true]){const angles=Array.from({length:n},(_,i)=>i).filter(i=>Boolean(p.feedMode&&j===rows-1&&i%(n/p.feedLegs)===0)===mount).map(i=>i*step+(p.staggerRings?(j%2)*PI/n:0));if(!angles.length)continue;const{body,spec}=panelSolid(p,n,rows,j,mount);add(`petal-${j+1}${mount?'-mount':''}`,`Petal ${j+1}${mount?' · rod mount':''}`,body.mesh(),angles.length,angles,'panel',j,spec);}
 add('hub','Hub · clear center',hubSolid(p,n).mesh(),1,[0],'hub',-1);const seams=seamStations(parts);
 if(p.seamJoint)add('seam-clip','Seam clip',clipSolid(p.clipFit,(X,Y,U)=>[X,U,Y]).mesh(),seams+Math.max(2,Math.ceil(seams/10)),[],'clip',-1);const m={p,layout,parts,instances,ringPhases:Array.from({length:rows},(_,j)=>p.staggerRings?(j%2)*PI/n:0),depth:zAt(p.diameter/2,p),focal:p.diameter*p.fd,bolts:n*(2*rows+2*(rows-1))+n};m.bolts=(p.seamJoint===1?0:seams)+n;appendFeedParts(m,{patch,printMesh,bounds});m.plates=p.packPlates?packParts(parts,p):[];return m;});}
export function connectionCoupon(m){return solidScope(()=>{const part=m.parts.find(p=>p.kind==='panel'),f=part.spec.flanges[0],c=Math.cos(PI/m.layout.n),lo=f.stations[0]*c-9,hi=f.key*c+9;const cropped=solid(part.mesh).trim([1,0,0],lo).trim([-1,0,0],-hi).mesh();const clips=m.p.seamJoint?[-.05,0,.05].map(d=>Math.min(.1,Math.max(0,m.p.clipFit+d))).map(fit=>{const mesh=clipSolid(fit,(X,Y,U)=>[X,U,Y]).mesh();return{id:`seam-clip-fit-${fit.toFixed(2)}`,qty:1,mesh,output:printMesh(mesh)};}):[];return[{id:'seam-strip-print-two',qty:2,mesh:cropped,output:sidePrint(cropped,m.layout.n)},...clips];});}
const seamStations=parts=>parts.filter(p=>p.kind==='panel').reduce((sum,p)=>sum+p.qty*p.spec.flanges.reduce((a,f)=>a+f.stations.length,0),0)/2;
export function hardwareSchedule(m){const seams=seamStations(m.parts),sj=m.p.seamJoint,clips=m.parts.find(p=>p.kind==='clip'),rt=!!m.p.rootThrough,mt=!!m.p.mountThrough,n=m.layout.n,inserts=(rt?0:n)+(mt?0:4),screw=Math.ceil(hubMountGrip(m.p)+6.3);return[...(sj?[{item:'seam clip',spec:'printed, in the kit',quantity:clips.qty,note:`${seams} stations plus ${clips.qty-seams} spares. Push straight up over both flanges from behind until both lips snap over the pads; pry off with a flat screwdriver between the clip top and the dish.`}]:[]),...(sj!==1?[{item:'seam screw',spec:'M3 × 16 socket head',quantity:seams,note:(sj?'Optional at any station instead of a clip. ':'')+'Through both flanges from either side; 2–4 threads past the nut.'},{item:'seam nut',spec:'M3 / 5.5 mm AF',quantity:seams,note:'Loose, on the flat seat opposite the head. Hold with a 5.5 mm nut driver or wrench.'},{item:'seam washer',spec:'M3 / 0.5 mm',quantity:2*seams,note:'One under the head, one under the nut.'}]:[]),...(rt?[{item:'root screw',spec:'M4 × 20 socket head',quantity:n,note:'From the front, head and washer in the recessed seat; nut on the hub rear face.'},{item:'root nut',spec:'M4',quantity:n},{item:'root washer',spec:'M4 / 0.8–1 mm',quantity:2*n,note:'One under the head, one under the nut.'}]:[{item:'root screw',spec:'M4 × 12',quantity:n,note:'From the hub rear into the petal insert, with one 1 mm washer; nominal 5 mm entry. The reflecting face stays closed.'},{item:'root washer',spec:'M4 / 1 mm',quantity:n}]),...(inserts?[{item:'blind insert',spec:'M4 / maximum 6 mm long',quantity:inserts,note:`Ø${m.p.insertDiameter} × 7 mm blind pilot; seat 0.5 mm below entry. Match insert vendor pilot specification.`}]:[]),{item:'external mount screw',spec:mt?`M4 × ${Math.ceil((screw+12)/5)*5} for a 12 mm adapter plate`:'M4 / select from actual adapter stack',quantity:4,note:mt?`60 mm BCD; Ø4.6 through the hub. Head behind the adapter; washer and nut in the Ø10 seats on the hub front. Use ${screw} mm plus your adapter thickness, rounded up.`:'60 mm BCD. Target 4–5 mm entry; never bottom out in 7 mm pocket.'},...(mt?[{item:'external mount nut',spec:'M4',quantity:4},{item:'external mount washer',spec:'M4',quantity:8,note:'One under each head and nut.'}]:[])];}
