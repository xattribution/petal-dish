import {feedMesh,feedCylinder,feedTransform,feedPuckBody,feedContinuousSocket,feedSocketBody,feedIntegralSocket,collectorBodies} from './feed-solids.js';
import {mountChoice} from './connections.js';
import {finBolt,finWidth,bowlBack} from './feed-solids.js';
export const FEED={revision:7,engagement:18,minEngagement:16,maxEngagement:19,puckRadius:22};
const FEED_TAU=2*Math.PI;
const fz=(r,p)=>r*r/(4*p.diameter*p.fd);
export function feedDatum(p){const r=p.diameter/2-20;return{r,front:fz(r,p),rear:fz(r,p)-p.thickness-3,slope:r/(2*p.diameter*p.fd),holes:[]};}
export function feedFits(p,n,rows){if(!p.feedMode)return true;if(n%p.feedLegs)return false;const d=feedDatum(p),r0=45+(rows-1)*(p.diameter/2-45)/rows;return d.r-23>Math.max(78,r0+8)&&d.r*Math.sin(Math.PI/n)>27;}
function hyperbola(p,ratio){
 const f=p.diameter*p.fd,g=p.backFocus,zv=f*ratio,zc=(f+g)/2,c=(f-g)/2,a=zv-zc,b2=c*c-a*a;
 if(!(g<0&&a>0&&a<c&&b2>0))throw Error('Secondary vertex must lie between the two-focus midpoint and primary focus.');
 const surface=r=>zc+a*Math.sqrt(1+r*r/b2),R=p.diameter/2,depth=fz(R,p);let lo=0,hi=R;for(let i=0;i<70;i++){const r=(lo+hi)/2;if(surface(r)<f+(depth-f)*r/R)lo=r;else hi=r;}
 const intercept=(lo+hi)/2,radius=intercept*1.04,edgeZ=surface(radius),backZ=zv+10,returnRadius=intercept*(fz(60,p)-g)/(surface(intercept)-g);
 const profile=Array.from({length:41},(_,i)=>{const r=radius*i/40;return[r,surface(r)];});
 return{primaryFocus:f,backFocus:g,vertex:zv,center:zc,c,a,b2,interceptRadius:intercept,radius,edgeZ,backZ,returnRadius,surface,ratio,profile};
}
export function cassegrainGeometry(p){
 let s,target=null;if(p.autoSecondary&&p.frequencyGHz>0){target=Math.max(56,p.secondaryWaves*299.792458/p.frequencyGHz);if(target>p.diameter*.25)throw Error('Requested wavelength-sized secondary exceeds 25% of dish diameter. Use prime focus, a larger dish, or manual secondary geometry for an explicitly experimental layout.');let lo=Math.max(.5001,(p.diameter*p.fd+p.backFocus)/(2*p.diameter*p.fd)+.001),hi=.98;for(let i=0;i<60;i++){const mid=(lo+hi)/2;if(hyperbola(p,mid).radius*2>target)lo=mid;else hi=mid;}s=hyperbola(p,(lo+hi)/2);}else s=hyperbola(p,p.secondaryPosition);
 if(s.radius<28-1e-7||s.radius>p.diameter*.125+1e-7)throw Error('Secondary must be at least 56 mm across and no more than 25% of the dish diameter for this compact support.');
 if(s.returnRadius>13.5)throw Error('Returned ray bundle does not clear the 30 mm hub with 1.5 mm radial margin. Move the rear focus toward the vertex.');
 return{...s,targetDiameter:target};
}
// Gregorian collector: a concave ellipsoidal bowl just beyond the prime focus reflects the dish's rays down to a
// receive insert below the focus. Foci: F1 = the dish focus, F2 = the insert's phase center.
//  - The bowl's rim catches the dish's rim ray (plus a 3° spill margin), so it re-shapes itself with D and f/D.
//  - The insert sits where its cone of collection (half-angle θ) just reaches the bowl rim, so the bowl fills its beam.
//  - The insert must hide inside the bowl's own shadow: dish rays within the bowl's radius never arrive, so the cone
//    below F1 that they would have filled is dark (Wade, "Multiple reflector dish antennas": the feed's blockage angle
//    must be smaller than the subreflector's). The bowl is the smallest one that satisfies this, or N wavelengths
//    across when a frequency is set, whichever is larger.
//  - The insert cup stands on a tube mast from the hub inside the same dark cone; the rods socket into the bowl.
export const GREGORIAN={wall:3,lip:1.5,spill:3,cupWall:2.5,cupFloor:3,flange:6,footR:38,coax:6,maxFraction:.3};
const deg=r=>r*180/Math.PI;
export function gregorianGeometry(p){
 if(!p.hubFlat)throw Error('The insert mast seats on a flat hub front. Set Hub front to Flat; the hub sits in the bowl’s shadow, so nothing is lost.');
 for(let i=0;i<4;i++)if(!mountChoice(p,i))throw Error('The insert mast bolts through the hub’s four mount holes. Set Hub to mount to through bolts.');
 const G=GREGORIAN,f=p.diameter*p.fd,R=p.diameter/2,psi0=2*Math.atan(R/(2*f)),psiE=psi0+G.spill*Math.PI/180,theta=p.collectorAngle*Math.PI/180;
 const lambda=p.frequencyGHz>0?299.792458/p.frequencyGHz:null,offset=p.phaseUnits===1?(lambda===null?NaN:p.phaseOffset*lambda):p.phaseOffset;
 if(!Number.isFinite(offset))throw Error('Enter a frequency for a phase-center offset specified in wavelengths.');
 if(theta>=psiE-1e-6)throw Error(`The insert must collect over a narrower cone than this dish’s ${deg(psi0).toFixed(1)}° rim angle. Lower the insert half-angle or choose a deeper dish.`);
 const insertR=p.collectorDiameter/2+G.cupWall,above=Math.max(0,-offset)+2;
 const shadowAngle=re=>2*Math.atan((re+G.lip)/(2*f)),depthFor=re=>re*(1/Math.tan(theta)-1/Math.tan(psiE));
 const clearance=re=>(depthFor(re)-above)*Math.tan(shadowAngle(re))-(insertR+1);
 let lo=0,hi=R;for(let i=0;i<70;i++){const mid=(lo+hi)/2;if(clearance(mid)<0)lo=mid;else hi=mid;}
 const minRadius=Math.max(hi,28),re=!p.autoSecondary?p.bowlDiameter/2:Math.max(minRadius,lambda?p.secondaryWaves*lambda/2:0);
 if(re<minRadius-1e-6)throw Error(`A ${p.bowlDiameter} mm bowl cannot hide a ${p.collectorDiameter} mm insert collecting ±${p.collectorAngle}°. Use at least ${Math.ceil(2*minRadius)} mm, a smaller insert or a narrower angle.`);
 if(2*re>G.maxFraction*p.diameter+1e-6)throw Error(`This layout needs a ${Math.ceil(2*re)} mm bowl, over ${100*G.maxFraction}% of the dish. Use a smaller insert, a narrower insert angle${lambda&&re>minRadius+1e-6?', fewer bowl wavelengths':''} or a larger dish.`);
 const delta=depthFor(re),g=f-delta,c=delta/2,zc=(f+g)/2,te=re/Math.sin(psiE),ze=f+te*Math.cos(psiE),a=(te+Math.hypot(re,ze-g))/2,b2=a*a-c*c,zv=zc+a;
 // Polar form about F1 (angle from +z): t(ψ) = b² / (a + c cos ψ); ψ = 0 is the bowl vertex.
 const profile=Array.from({length:97},(_,i)=>{const psi=psiE*i/96,t=b2/(a+c*Math.cos(psi));return[t*Math.sin(psi),f+t*Math.cos(psi)];});
 // Ellipse magnification (1+e)/(1−e): every ray keeps tan(ψ/2) / tan(θ/2) constant, so the dish rim reaches the insert inside its edge.
 const magnification=Math.tan(psiE/2)/Math.tan(theta/2),rimInsertAngle=deg(2*Math.atan(Math.tan(psi0/2)/magnification));
 return{primaryFocus:f,insertFocus:g,center:zc,a,c,b:Math.sqrt(b2),vertex:zv,radius:re,edgeZ:ze,rimAngle:deg(psi0),edgeAngle:deg(psiE),insertAngle:p.collectorAngle,rimInsertAngle,
  shadowAngle:deg(shadowAngle(re)),outerRadius:re+G.lip,blockage:((re+G.lip)/R)**2,magnification,equivalentFD:magnification*p.fd,insertRadius:insertR,offset,cupRim:g-offset,
  apexToInsert:zv-g,profile,minDiameter:2*minRadius,waves:lambda?2*re/lambda:null,nearField:lambda?2*p.collectorDiameter**2/lambda:null};
}
// Collector rod seat: the bowl wall thickens where each rod goes in, between the flat top and the rim. Nothing hangs
// below the rim. The rod runs just above the back cone, enters at the rim and ends inside the thickened wall:
//  - its bore keeps 1.5 mm of wall over the reflecting face and 1.5 mm under the flat top;
//  - the cross bolt sits where the rod clears the back cone, so its washers bear on flat faces in open air;
//  - the seat is the rod collar swept at 45° up to the flat top, so in print (bowl on its top) it stands on the bed
//    and every face under it is 45° or steeper.
export const SEAT={wall:2.5,floor:1.5,gap:.5,front:1.5,roof:1,mouth:1,washerGap:.5,minEngagement:8};
export function collectorSeat(p,bowl,datum,rodD){
 const rb=(rodD+p.rodClearance)/2,ha=rb+SEAT.wall,hb=ha,washer=rodD>=6?7:5,k=Math.max(.8,Math.min(2,ha-washer/2-.25)),re=bowl.radius;
 const cap=bowl.vertex+GREGORIAN.wall,front=bowl.profile,end=SEAT.floor+SEAT.gap,target=Math.max(10,Math.ceil(1.6*rodD));
 const back=bowlBack(bowl.profile,GREGORIAN.wall,cap),backAt=r=>{if(r>re)return-Infinity;for(let i=1;i<back.length;i++)if(back[i][0]>=r){const[r0,z0]=back[i-1],[r1,z1]=back[i];return z0+(z1-z0)*(r-r0)/(r1-r0||1);}return back.at(-1)[1];};
 // (a, b): a along the rod's outward-up normal in its radial plane, b tangential. Chamfered square.
 const section=[[ha,hb-k],[ha-k,hb],[-(ha-k),hb],[-ha,hb-k],[-ha,-(hb-k)],[-(ha-k),-hb],[ha-k,-hb],[ha,-(hb-k)]];
 // One seat for a given rod angle, mouth radius and engagement: the lowest axis that clears everything, or null.
 const place=(alpha,rm,engagement)=>{const c=Math.cos(alpha),s=Math.sin(alpha),t=s/c,ax=[c,-s],u1=[s,c];
  const rEnd=rm-engagement*c,rBolt=rm-engagement/2*c,rB=rEnd-end*c,L=(rm-rB)/c;
  // axis height at r: zr + (re − r)·tan α; every clearance only improves as the axis rises, so bisect for the lowest
  const ok=zr=>{const zB=zr+(re-rB)*t,apex=[rB-rb*Math.SQRT2*u1[0],zB-rb*Math.SQRT2*u1[1]];
   // the bore apex (toward the reflecting face) keeps SEAT.front of wall over the face
   for(const[r,f]of front){const w=[r-apex[0],f-apex[1]],q=Math.max(SEAT.floor,Math.min(L,w[0]*ax[0]+w[1]*ax[1]));if(Math.hypot(w[0]-q*ax[0],w[1]-q*ax[1])<SEAT.front)return false;}
   // the washer circle on each flat side face (b = ±hb) sits in open air, clear of the bowl's back
   const zb=zr+(re-rBolt)*t;for(let j=0;j<48;j++){const a=j*Math.PI/24,r=rBolt+washer/2*(Math.cos(a)*c+Math.sin(a)*s),z=zb+washer/2*(-Math.cos(a)*s+Math.sin(a)*c);if(z<backAt(Math.hypot(r,hb))+SEAT.washerGap)return false;}
   // the rod leaving the mouth clears the rim
   for(const zc of [bowl.edgeZ,bowl.edgeZ+GREGORIAN.wall]){const w=[re-rm,zc-(zr-(rm-re)*t)],q=Math.max(0,w[0]*ax[0]+w[1]*ax[1]);if(Math.hypot(w[0]-q*ax[0],w[1]-q*ax[1])<rodD/2+.5)return false;}
   return true;};
  let lo=bowl.edgeZ-60,hi=cap+60;if(!ok(hi))return null;for(let j=0;j<60;j++){const mid=(lo+hi)/2;if(ok(mid))hi=mid;else lo=mid;}
  const zB=hi+(re-rB)*t;if(zB+rb*c>cap-SEAT.roof)return null;   // the rod end must stay under the flat top
  return{topRadius:rB,upperZ:zB,engagement,length:L,bolt:(rBolt-rB)/c,end,mouthRadius:rm};};
 // Keep the mouth as close to the rim as the bowl allows, and the engagement as long as fits (down to 8 mm).
 const fit=alpha=>{for(let m=SEAT.mouth;m<=30;m+=.5)for(let e=target;e>=SEAT.minEngagement;e-=.5){const q=place(alpha,re+m,e);if(q)return q;}return null;};
 let alpha=Math.atan2(bowl.edgeZ-datum.rear,datum.r-re),seat=null;
 for(let i=0;i<30;i++){seat=fit(alpha);if(!seat)break;const next=Math.atan2(seat.upperZ-datum.rear,datum.r-seat.topRadius);if(Math.abs(next-alpha)<1e-9)break;alpha=next;}
 if(!seat)throw Error('The rods cannot reach into this bowl’s wall. Use a narrower insert angle or a larger bowl.');
 return{...seat,alpha,rb,ha,hb,chamfer:k,cap,section,washer,...SEAT};
}
// Insert mast: a user tube from a foot bolted on the hub front to the insert cup. Heights along the dish axis.
export function mastGeometry(p,bowl){
 const G=GREGORIAN,bore=(p.mastDiameter+.4)/2,footZ=fz(45,p),footTop=footZ+G.flange,footSocket=Math.max(25,1.6*p.mastDiameter),cupSocket=Math.max(20,1.2*p.mastDiameter);
 const cupDepth=Math.min(25,Math.max(8,.4*p.collectorDiameter)),cupRim=bowl.cupRim,cupFloorTop=cupRim-cupDepth,tubeTop=cupFloorTop-G.cupFloor,cupBottom=tubeTop-cupSocket,footSocketTop=footTop+footSocket;
 if(tubeTop<footTop+8)throw Error('The insert would sit inside the hub. Widen the insert half-angle or use a smaller insert.');
 // Deep dishes leave no room for a tube: the cup then stands on a printed column on the foot (one part).
 const coaxR=Math.max(1.5,Math.min(G.coax,bore-1.5)),socketAF=(w,b=bore)=>2*b+2*w,tubed=cupBottom>=footSocketTop+5;
 return{tube:tubed,diameter:p.mastDiameter,bore,coaxR,lower:[0,0,footTop],upper:[0,0,tubeTop],cutLength:tubed?tubeTop-footTop:0,footZ,footTop,footSocket,footSocketTop,cupSocket,cupBottom,tubeTop,cupFloorTop,cupRim,cupDepth,
  footAF:[socketAF(5),socketAF(3)],cupAF:[socketAF(3),socketAF(4.5)],flange:G.flange,flangeRadius:G.footR};
}
export function feedGeometry(p,layout){
 if(!p.feedMode)return null;const d=feedDatum(p),lambda=p.frequencyGHz>0?299.792458/p.frequencyGHz:null,secondary=p.feedMode===2?cassegrainGeometry(p):null,bowl=p.feedMode===3?gregorianGeometry(p):null,mast=bowl?mastGeometry(p,bowl):null;
 const offset=bowl?bowl.offset:p.feedMode!==1?0:p.phaseUnits===1?(lambda===null?NaN:p.phaseOffset*lambda):p.phaseOffset;if(!Number.isFinite(offset))throw Error('Enter a frequency for a phase-center offset specified in wavelengths.');
 // Collector rods end in sockets just outside the bowl rim, where no ray reaches (behind or beyond the bowl).
 const seat=bowl?collectorSeat(p,bowl,d,p.rodDiameter||8):null,lowerZ=d.rear,topRadius=seat?seat.topRadius:18;
 const requiredFace=secondary?lowerZ+(secondary.edgeZ+10-lowerZ)*(d.r-topRadius)/(d.r-secondary.radius-7)-8:0;
 const stemHeight=secondary?Math.max(30,5*Math.ceil((requiredFace-secondary.backZ)/5)):0;
 const carrierFace=bowl?bowl.vertex:secondary?secondary.backZ+stemHeight:p.diameter*p.fd-offset,upperZ=seat?seat.upperZ:carrierFace+8;
 const distance=Math.hypot(d.r-topRadius,upperZ-lowerZ),ux=(d.r-topRadius)/distance,uz=(upperZ-lowerZ)/distance;
 // The lower datum lies under the petal. Solve the actual curved-face crossing,
 // rather than borrowing the former separate shoe's fixed 22 mm entrance.
 let lo=0,hi=distance;for(let i=0;i<64;i++){const t=(lo+hi)/2;if(lowerZ+uz*t<fz(d.r-ux*t,p))lo=t;else hi=t;}
 const lowerEntrance=(lo+hi)/2,upperEntrance=seat?seat.length:22,upperEngagement=seat?seat.engagement:FEED.engagement;
 const cut=distance-lowerEntrance-upperEntrance+FEED.engagement+upperEngagement;
 if(upperZ-lowerZ<15||distance<60||cut>600)throw Error('Rod geometry is outside the compact fitting envelope: require at least 15 mm rise, 60 mm span and at most 600 mm cut.');
 // Conservative single-rod cantilever screening, NOT a full truss/load model.
 // E=69 GPa; full entered payload acts transversely on one rod; 1 mm or lambda/50 target.
 const targetDeflection=Math.min(1,lambda===null?1:lambda/50),load=p.feedPayload/1000*9.80665,L=distance-lowerEntrance-upperEntrance;
 const deflection=diameter=>load*L**3/(3*69000*(Math.PI*diameter**4/64));
 const diameter=p.rodDiameter||[4,5,6,6.35,8].find(d=>deflection(d)<=targetDeflection);
 if(!diameter)throw Error('No 4–8 mm solid aluminum rod meets the screening deflection budget. Reduce payload/span or engineer a tubular support.');
 const phase=p.staggerRings?((layout.rows-1)%2)*Math.PI/layout.n:0,legs=Array.from({length:p.feedLegs},(_,i)=>{const angle=phase+i*FEED_TAU/p.feedLegs;const lower=[d.r*Math.cos(angle),d.r*Math.sin(angle),lowerZ],upper=[topRadius*Math.cos(angle),topRadius*Math.sin(angle),upperZ],axis=upper.map((v,k)=>(v-lower[k])/distance);return{number:i+1,angle,lower,upper,lowerRodEnd:lower.map((v,k)=>v+axis[k]*(lowerEntrance-FEED.engagement)),upperRodEnd:upper.map((v,k)=>v-axis[k]*(upperEntrance-upperEngagement)),lowerFaceCrossing:lower.map((v,k)=>v+axis[k]*lowerEntrance),pivotDistance:distance,lowerEntrance,upperEntrance,cutLength:cut};});
 if(secondary){const rr=secondary.radius+diameter/2+1,z=lowerZ+(upperZ-lowerZ)*(d.r-rr)/(d.r-topRadius);if(z<secondary.edgeZ+3+diameter/2+1)throw Error('Rod path crowds the secondary edge. Change secondary position, rear focus or dish geometry.');}
 const warnings=['Prototype: no wind, payload or RF performance rating. Small radial screws require physical slip and warm-creep tests.'];
 if(p.feedLegs===4)warnings.push('Fit the fourth rod last without preload; never use it to pull a warped petal into alignment.');
 if(!lambda)warnings.push('Frequency unspecified: wavelength-dependent dimensions and RF tolerances are unavailable.');
 if(p.feedMode===1&&offset===0)warnings.push('Zero phase-center offset is a placeholder; specify the actual feed datum.');
 if(deflection(diameter)>targetDeflection)warnings.push('Selected rod exceeds the conservative deflection screening budget; use larger stock or reduce payload.');
 if(secondary){warnings.push('Hyperbolic ray geometry is not electromagnetic validation. A frequency-specific rear feed remains required.');if(lambda&&2*secondary.radius/lambda<5)warnings.push('Secondary is below five wavelengths: diffraction requires full-wave or hybrid electromagnetic analysis.');}
 if(bowl){warnings.push('Ellipsoidal ray geometry is not an electromagnetic validation; measure the assembled collector against a reference antenna.');
  if(bowl.waves!==null&&bowl.waves<5)warnings.push(`The bowl is ${bowl.waves.toFixed(1)} wavelengths across. Below about 5 λ diffraction dominates and much of the bowl’s gain is lost; prime focus will outperform it at this frequency.`);
  else if(bowl.waves!==null&&bowl.waves<10)warnings.push(`The bowl is ${bowl.waves.toFixed(1)} wavelengths across. Dual reflectors work best above about 10 λ; expect some diffraction loss.`);
  if(bowl.nearField!==null&&bowl.apexToInsert<bowl.nearField)warnings.push(`The bowl is inside the insert’s near field at this frequency (${bowl.apexToInsert.toFixed(0)} mm, wants ${bowl.nearField.toFixed(0)} mm). Use a smaller insert.`);
  if(bowl.blockage>.05)warnings.push(`The bowl shades ${(100*bowl.blockage).toFixed(1)}% of the aperture. A smaller or narrower-beam insert lets it shrink.`);}
 return{mode:p.feedMode===1?'prime-focus':p.feedMode===2?'cassegrain':'gregorian-collector',bowl,mast,seat,upperEngagement,topRadius,legs,datum:d,carrierFace,upperZ,lowerZ,cutLength:cut,lowerEntrance,upperEntrance,pivotDistance:distance,rodAngle:Math.atan2(upperZ-lowerZ,d.r-topRadius)*180/Math.PI,phase,secondary,stemHeight,offset,rodDiameter:diameter,lambda,targetDeflection,screenDeflection:deflection(diameter),surfaceRmsBudget:lambda?lambda/40:null,warnings};
}
const transform=(mesh,fn)=>({v:mesh.v.map(fn),f:mesh.f});
const flip=mesh=>transform(mesh,([x,y,z])=>[x,-y,-z]);
function socketFrame(A,B,azimuth=0){const dx=B[0]-A[0],dz=B[2]-A[2],L=Math.hypot(dx,dz),u=dx/L,w=dz/L;return([x,y,z])=>{const X=A[0]+w*x+u*z,Y=A[1]+y;return[Math.cos(azimuth)*X-Math.sin(azimuth)*Y,Math.sin(azimuth)*X+Math.cos(azimuth)*Y,A[2]-u*x+w*z];};}
export function petalRodSocket(p,layout,backFn){const g=feedGeometry(p,layout);return feedIntegralSocket(p,g,layout.n,backFn);}
export function appendFeedParts(m,api){
 if(!m.p.feedMode)return;const{patch,printMesh,bounds}=api,p=m.p,g=feedGeometry(p,m.layout),d=g.datum;m.feed=g;
 const add=(id,name,mesh,qty,angles=[0],printing=null)=>{const pm=printing||printMesh(mesh),dim=bounds(pm).size;if(dim[0]>p.bedX-2*p.margin||dim[1]>p.bedY-2*p.margin||dim[2]>p.bedZ-2)throw Error(name+' does not fit the print volume.');const part={id,name,mesh,print:pm,output:pm,supportMeshes:[],dim,qty,kind:'feed',row:-1,angle:0,bedRotation:0,spec:{holes:[]}};m.parts.push(part);for(const a of angles)m.instances.push({part,a});return part;};
 const angles=g.legs.map(x=>x.angle);
 if(g.bowl){
  // Build each collector part in its local frame for Float32 precision, then lift it into place after meshing.
  const {bowl,foot,cup}=collectorBodies(p,g),lift=(mesh,z)=>transform(mesh,([x,y,q])=>[x,y,q+z]),mm=g.mast;
  const bm=lift(feedMesh(bowl),g.bowl.vertex);add('feed-bowl','Collector · bowl with rod sockets',bm,1,[0],printMesh(flip(bm)));
  if(mm.tube){const fm=lift(feedMesh(foot),mm.footZ);add('feed-mast-foot','Collector · mast foot',fm,1,[0]);
   const cm=lift(feedMesh(cup),mm.tubeTop);add('feed-insert-cup','Collector · insert cup',cm,1,[0]);}
  else{const pm=lift(feedMesh(foot),mm.footZ);add('feed-insert-pedestal','Collector · insert pedestal',pm,1,[0]);}
  return;
 }
 const sleeve=feedSocketBody(g.rodDiameter);
 let puck=feedPuckBody(0,Boolean(g.secondary)),cuts=g.secondary?[]:[feedCylinder([0,0,-19],[0,0,0],16,64)];
 const core=puck,upperSocket=feedContinuousSocket(g.rodDiameter,p.rodClearance,-1);
 // Load carrier nuts outward/upward, away from its center and secondary stem.
 for(const a of angles){const fn=socketFrame([18,0,8],[d.r,0,g.lowerZ-g.carrierFace],a);puck=puck.union(core.union(feedTransform(sleeve,fn)).hull());cuts.push(...upperSocket.cuts.map(c=>feedTransform(c,fn)));}
 cuts.push(feedCylinder([0,0,g.secondary?g.secondary.backZ-g.carrierFace-1:-20],[0,0,20],g.secondary?2.3:6,32));
 for(let i=0;i<p.feedLegs;i++){const a=g.phase+(i+.5)*FEED_TAU/p.feedLegs,x=12*Math.cos(a),y=12*Math.sin(a);cuts.push(feedCylinder([x,y,-1],[x,y,17],1.7,20));}
 if(g.secondary)puck=puck.union(feedCylinder([0,0,g.secondary.backZ-g.carrierFace],[0,0,1],9,32));
 for(const c of cuts)puck=puck.subtract(c);
 // Build the carrier in local coordinates to keep Float32 loft input precise
 // even for large focus offsets. Add its assembled height after meshing.
 const pm=transform(feedMesh(puck),([x,y,z])=>[x,y,z+g.carrierFace]);add('feed-puck','Feed · slim lobed carrier',pm,1,[0],printMesh(flip(pm)));
 if(g.secondary){const s=g.secondary,back=(x,y)=>{const r=Math.hypot(x,y),t=Math.min(1,Math.max(0,(r-10)/5));return Math.max(s.surface(r)+3,s.backZ*(1-t)+(s.surface(r)+3)*t);};const h={r0:0,r1:2.8,a0:0,a1:FEED_TAU},front=(x,y)=>s.surface(Math.hypot(x,y)),mesh=patch({r0:0,r1:s.radius,a0:0,a1:FEED_TAU,levels:[front,(x,y)=>front(x,y)+.3*(back(x,y)-front(x,y)),back],layers:[{},{holes:[h]}],gridBoxes:[h],extraR:[10,15]},p);add('secondary-reflector','Secondary · 3 mm shell / center boss',mesh,1,[0],printMesh(flip(mesh)));}
}
function collectorManifest(m){const g=m.feed,b=g.bowl,t=g.mast;return{layout:'Gregorian: concave ellipsoidal bowl beyond the prime focus, insert below it',primary_focus_z_mm:b.primaryFocus,insert_focus_z_mm:b.insertFocus,bowl_vertex_z_mm:b.vertex,bowl_rim_z_mm:b.edgeZ,bowl_diameter_mm:2*b.radius,bowl_minimum_diameter_mm:b.minDiameter,bowl_wavelengths:b.waves,
 ellipse:{surface:'polar about the prime focus: t(psi) = b^2 / (a + c cos psi), psi from the dish axis',a_mm:b.a,b_mm:b.b,c_mm:b.c},rim_angle_degrees:b.rimAngle,bowl_edge_angle_degrees:b.edgeAngle,insert_half_angle_degrees:b.insertAngle,dish_rim_at_insert_degrees:b.rimInsertAngle,shadow_cone_half_angle_degrees:b.shadowAngle,
 aperture_blockage_fraction:b.blockage,magnification:b.magnification,equivalent_f_over_D:b.equivalentFD,insert_diameter_mm:m.p.collectorDiameter,insert_cup_rim_z_mm:b.cupRim,phase_center_offset_mm:b.offset,bowl_apex_to_insert_mm:b.apexToInsert,insert_near_field_mm:b.nearField,
 mast:t.tube?{tube_diameter_mm:t.diameter,cut_length_mm:t.cutLength,lower_end_z_mm:t.footTop,upper_end_z_mm:t.tubeTop}:{printed_pedestal:true,top_z_mm:t.tubeTop}};}
export function feedManifest(m){if(!m.feed)return{enabled:false};const g=m.feed,s=g.secondary;return{enabled:true,revision:FEED.revision,mode:g.mode,legs:m.p.feedLegs,rod_diameter_mm:g.rodDiameter,stock:'smooth solid aluminum rod (screen assumes E = 69 GPa)',cut_length_mm:g.cutLength,socket_end_span_mm:g.pivotDistance,nominal_insertion_each_end_mm:18,carrier_engagement_range_mm:[16,19],petal_attachment:'integrated underside through-bore and short M3 heat-set insert side screw',petal_bore_roof:'45-degree shoulders and 0.8 mm bridge relative to petal print-up',carrier_lower_face_z_mm:g.carrierFace,phase_center_offset_mm:g.offset,frequency_GHz:m.p.frequencyGHz||null,wavelength_mm:g.lambda,surface_rms_screen_mm:g.surfaceRmsBudget,rod_deflection_screen_mm:g.screenDeflection,rod_deflection_budget_mm:g.targetDeflection,payload_input_g:m.p.feedPayload,puck_core_diameter_mm:44,lower_bore_entrance_mm:g.lowerEntrance,upper_bore_entrance_mm:g.upperEntrance,adapter_bolt_circle_mm:24,adapter_bolts:`${m.p.feedLegs} × M3 midway between rods; feed-specific adapter required`,legs_geometry:g.legs,collector:g.bowl?collectorManifest(m):null,secondary:s?{primary_focus_z:s.primaryFocus,rear_focus_z:s.backFocus,vertex_z:s.vertex,vertex_ratio:s.ratio,diameter_mm:2*s.radius,target_diameter_mm:s.targetDiameter,front_surface:'z = center + a * sqrt(1 + r^2 / b^2)',center:s.center,a:s.a,b:Math.sqrt(s.b2),back_boss_z:s.backZ,return_bundle_radius_mm:s.returnRadius}:null,hardware:feedHardware(m),warnings:g.warnings};}
export function rodCSV(m){if(!m.feed)return'';const g=m.feed;return 'leg,stock,diameter_mm,datum_span_mm,lower_face_crossing_mm,upper_bore_entrance_mm,lower_insertion_mark_mm,upper_insertion_mark_mm,cut_length_mm,cut_length_inches,azimuth_degrees,rod_elevation_degrees,lower_rod_end_z_mm,upper_rod_end_z_mm\n'+m.feed.legs.map(l=>[l.number,'smooth solid aluminum',m.feed.rodDiameter,l.pivotDistance.toFixed(3),g.lowerEntrance.toFixed(3),g.upperEntrance.toFixed(3),FEED.engagement,+g.upperEngagement.toFixed(2),l.cutLength.toFixed(3),(l.cutLength/25.4).toFixed(4),(l.angle*180/Math.PI).toFixed(3),m.feed.rodAngle.toFixed(3),l.lowerRodEnd[2].toFixed(3),l.upperRodEnd[2].toFixed(3)].join(',')).join('\n')+'\n'+(g.mast?.tube?['mast','tube with the insert cable inside',g.mast.diameter,'','','','','',g.mast.cutLength.toFixed(3),(g.mast.cutLength/25.4).toFixed(4),'',90,g.mast.footTop.toFixed(3),g.mast.tubeTop.toFixed(3)].join(',')+'\n':'');}
export function feedHardware(m){if(!m.feed)return[];const n=m.p.feedLegs;if(m.feed.bowl)return collectorHardware(m);return[
 {item:'petal rod-retention screw',spec:'M3 × 12',quantity:n,note:'Headless side set screw into a short M3 insert; rounded tip against solid rod.'},
 {item:'petal retention insert',spec:'short M3 / maximum 4 mm / compatible with 4.2 mm pilot',quantity:n,note:'Heat-set from the side, recessed 0.5 mm. Test insert fit before printing the dish.'},
 {item:'carrier rod-retention screw',spec:'M3 × 10',quantity:n,note:'Real M3 hex nut in carrier side pocket.'},
 {item:'carrier retention nut',spec:'M3 hex / 5.5 mm AF / 2.4 mm thickness',quantity:n,note:'Carrier pocket allows 5.8 mm AF.'},
 ...(m.feed.secondary?[{item:'secondary screw',spec:`M4 × ${m.feed.stemHeight+25}`,quantity:1,note:`${m.feed.stemHeight+16} mm puck/stem stack + 4 mm metal washer/spacer stack; nominal 5 mm entry.`},{item:'M4 washer',spec:'1 mm thick',quantity:4,note:'4 mm total metal spacer/washer stack'},{item:'secondary insert',spec:'short M4 / maximum 6 mm / compatible with 5.6 mm pilot',quantity:1,note:'7 mm blind pocket; insert 0.5 mm below boss face'}]:[])];}
function collectorHardware(m){const g=m.feed,n=m.p.feedLegs,bolt=finBolt(g.rodDiameter),W=finWidth(g.rodDiameter),width=W.hi-W.lo,len=Math.ceil((width+(bolt.size===3?1:.6)+bolt.size*1.4+1)/2)*2,t=g.mast;return[
 {item:'petal rod-retention screw',spec:'M3 × 12',quantity:n,note:'Headless side set screw into a short M3 insert; rounded tip against solid rod.'},
 {item:'petal retention insert',spec:'short M3 / maximum 4 mm / compatible with 4.2 mm pilot',quantity:n,note:'Heat-set from the side, recessed 0.5 mm. Test insert fit before printing the dish.'},
 {item:'bowl rod bolt',spec:`M${bolt.size} × ${len} socket head`,quantity:n,note:`Across each rod seat's flat faces (${width.toFixed(1)} mm) and through the rod. Fit the rod, then drill Ø${bolt.size+.2} through it using the seat's cross hole as the guide.`},
 {item:'bowl rod nut',spec:`M${bolt.size} nyloc`,quantity:n},{item:'bowl rod washer',spec:`M${bolt.size}`,quantity:2*n,note:'One under the head, one under the nut, on the flat fin faces.'},
 ...(t.tube?[{item:'insert mast tube',spec:`Ø${t.diameter} mm tube, cut ${t.cutLength.toFixed(1)} mm`,quantity:1,note:'Aluminum or rigid conduit. The insert cable runs inside it and out through the hub center.'},
  {item:'mast set screw',spec:'M3 × 8 cup point',quantity:2,note:'One in the mast foot, one in the insert cup, each into a short M3 insert.'},
  {item:'mast retention insert',spec:'short M3 / maximum 4 mm / compatible with 4.2 mm pilot',quantity:2,note:'Heat-set into the rib on each socket.'}]:[])];}
export function feedHardwareCSV(m){const quote=x=>'"'+String(x??'').replaceAll('"','""')+'"';return 'item,specification,quantity,grip_mm,note\n'+feedHardware(m).map(x=>[x.item,x.spec,x.quantity,x.grip_mm?.toFixed(3),x.note].map(quote).join(',')).join('\n')+'\n';}
export function feedGuide(m){if(!m.feed)return'';const g=m.feed;if(g.bowl)return collectorGuide(m);return `# PETAL compact rod support · ${g.mode}\n\nAccessory revision 7: rods fasten directly into the underside of the selected outer petals. There are no rim shoes, backing plates or paired mounting bolts. Regenerate the mount petals, carrier and rod cuts as a matching kit; earlier rods and petals do not interchange. Seam/hub interface remains revision 12. Changing rod count may change segmentation.\n\n## Rods and attachment\n\n${m.p.feedLegs} × Ø${g.rodDiameter} mm smooth SOLID aluminum rods; nominal cut ${g.cutLength.toFixed(2)} mm each. The petal has an angled through-hole and a small continuous underside saddle, chamfered into the shell. A side M3×12 headless set screw bears on the rod through a short M3 heat-set insert (maximum 4 mm long, compatible with the 4.2 mm pilot). The carrier retains its hex nuts. No printed threads or extra petal mounting bolts.\n\nRod elevation is ${g.rodAngle.toFixed(2)}°. The lower face crossing is solved from the dish curvature, not a fixed shoe height. Cut = datum span − lower face crossing − upper entrance + 36 mm. Mark 18 mm from each rod end. At the petal, align the mark with the center of the front opening; the through-hole has no floor and the rear end may protrude. This insertion mark is not a claim of 18 mm continuous plastic bearing. At the carrier, retain 16–19 mm engagement without bottoming the rod. RODS.csv gives the actual angles, positions and cuts. Trial-fit long stock before final trimming.\n\n## Printing and assembly\n\nThe integrated saddle is designed for the petal's existing side-print orientation. Its compact circular tapers blend into the underside. The rod bore has 45° roof shoulders and a 0.8 mm bridge aligned to print-up. The side screw and insert pilot are circular, nearly vertical in the print orientation. Keep this exported orientation. Nominal diametral rod clearance is ${m.p.rodClearance.toFixed(2)} mm; print one mount petal and verify fit first. The circular lower arc locates the rod; the short roof relief clears the full specified diameter.\n\nHeat-set the short M3 insert from the side, recessed 0.5 mm; then insert the rod through the petal and fit the side screw. Keep the screw loose while aligning the carrier with a height/centering jig. Fit the fourth rod last without preload. Tighten progressively and mark the rods to reveal slip. Petal inserts and carrier hex nuts are distinct items in FEED-HARDWARE.csv.\n\nThe existing carrier prints top-face down and still needs slicer review/localized support for circular bores and hex-nut roofs. The new petal socket's support-free geometry does not certify the carrier or secondary as support-free. PCTG and ASA still require a fit/slip/temperature test of the printed joint; resin data alone does not rate the assembled part. Use locally solid socket walls and a suitable material profile. Avoid hollow tubing with point screws unless separately engineered for crushing and stiffness.\n\nThe carrier has ${m.p.feedLegs} M3 adapter holes on 24 mm BCD and ${g.secondary?'a central M4 clearance hole':'a 12 mm cable opening'}. A feed-specific adapter and its hardware remain separate. Strain-relieve the cable along a rod. Measure height/centering/tilt before and after tightening and warm soaking.\n\n## Frequency and optical meaning\n\n${g.lambda?`Frequency ${m.p.frequencyGHz} GHz gives wavelength ${g.lambda.toFixed(3)} mm. Surface RMS screening target λ/40 = ${g.surfaceRmsBudget.toFixed(3)} mm, including printing, assembly and metallization errors. This is a design budget, not measured accuracy. The rod screening budget is min(1 mm, λ/50) = ${g.targetDeflection.toFixed(3)} mm.`:'Frequency is unknown; enter it to enable wavelength-based dimensions and accuracy guidance.'}\n\nFrequency alone does not move the focus of a fixed parabola. Prime focus remains f = D × f/D = ${m.focal.toFixed(3)} mm. A phase-center offset can be entered in mm or wavelengths ONLY when supported by the chosen feed design; scaling a generic unknown feed is not valid. The puck lower-face datum is z=${g.carrierFace.toFixed(3)} mm; phase-center offset is ${g.offset.toFixed(3)} mm along +z.\n\n${g.secondary?`The secondary is a convex hyperboloid toward the main dish: vertex z=${g.secondary.vertex.toFixed(3)} mm, diameter ${(2*g.secondary.radius).toFixed(3)} mm, rear focus z=${g.secondary.backFocus.toFixed(3)} mm. Automatic mode targets the entered secondary wavelength count (minimum 56 mm mechanical diameter), solves its position and then recalculates rod lengths and angles. Above 25% dish diameter it rejects the combination; choose prime focus or a larger dish. These are engineering screening limits, not optimized RF dimensions. The 3 mm reflective shell has a central 10 mm boss with one blind Ø5.6×7 mm insert pocket. Install a short M4 insert at least 0.5 mm recessed, maximum 6 mm long; use M4×${g.stemHeight+25} with a 4 mm metal washer/spacer stack through the ${g.stemHeight+16} mm puck/stem stack. Verify actual engagement and leave the reflective face intact. Secondary prints with its boss down; support the surrounding back shell in the slicer. The rear RF feed, phase center, polarization and illumination must be designed separately.`:'Place the actual feed PHASE CENTER at the focus, not automatically its mouth or flange. A zero offset remains unverified. The feed points toward the main dish.'}\n\nAuto rod sizing screens 4, 5, 6, 6.35 and 8 mm SOLID aluminum using E=69 GPa and a deliberately conservative single cantilever with the entire entered ${m.p.feedPayload} g payload applied transversely over the exposed span: δ=FL³/(3EI). Selected-rod result ${g.screenDeflection.toFixed(3)} mm. Include feed/secondary, puck, adapter and cable loads in the payload input; wind, joint compliance, resonance and creep are excluded. This is not an allowable-load calculation or an RF guarantee. Manual diameter overrides retain an over-budget warning.\n\n${g.warnings.map(w=>'- '+w).join('\n')}\n\nCoat the dish-facing secondary surface with continuous bonded aluminum/copper foil or a verified conductive coating; ordinary metallic paint is not sufficient evidence of RF conductivity. Keep datums, bores and the boss back clean. Surface seams/wrinkles count toward the RF error budget. Test one assembled leg for slip and creep, then the complete assembly at several elevations before committing to a full outdoor installation.\n`;}
function collectorGuide(m){const g=m.feed,b=g.bowl,t=g.mast,f=x=>x.toFixed(1),bolt=finBolt(g.rodDiameter);return `# PETAL Gregorian collector

The dish gathers, the bowl folds the focus back down, and the insert sits under the bowl looking up. Rays from the dish converge on the prime focus F1 (z = ${f(b.primaryFocus)} mm), cross it, strike the concave bowl just beyond it and reflect down to the second focus F2 (z = ${f(b.insertFocus)} mm). Put the insert's phase center at F2. The insert, its cup and the mast all stand inside the bowl's shadow, so the only aperture blockage is the bowl itself: ${(100*b.blockage).toFixed(1)}%.

## How the bowl is shaped

The bowl is a section of a prolate ellipsoid whose two foci are F1 and F2 (the Gregorian condition). Any ray through one focus reflects through the other, so the fold is exact for every ray from the dish. About F1, the surface is t(ψ) = b² / (a + c·cos ψ) with a = ${b.a.toFixed(3)}, b = ${b.b.toFixed(3)} and c = ${b.c.toFixed(3)} mm; ψ is measured from the dish axis.

It is re-solved on every build from the dish diameter and f/D:

- Rim: the bowl edge sits at the dish rim angle ${f(b.rimAngle)}° plus 3° spill margin (${f(b.edgeAngle)}°), so every ray from the dish lands on the bowl.
- Focus spacing: F1 to F2 is ${f(b.primaryFocus-b.insertFocus)} mm, set so the rim ray reaches the insert at ±${b.insertAngle}°.
- Size: the smallest bowl whose shadow still covers the insert cup, or N wavelengths when a frequency is set, or the entered diameter. This one is Ø${f(2*b.radius)} mm (minimum Ø${f(b.minDiameter)})${b.waves?`, ${b.waves.toFixed(1)} λ`:''}.

A deeper dish or a narrower insert angle spreads F1 and F2 further apart, so a smaller bowl still shades the insert. A larger insert needs a larger bowl.

Magnification M = (1 + e) / (1 − e) = ${b.magnification.toFixed(2)}, with eccentricity e = c / a = ${(b.c/b.a).toFixed(3)}. The insert sees the system as an f/D ${b.equivalentFD.toFixed(2)} dish: the dish rim arrives at ±${f(b.rimInsertAngle)}° and the bowl edge at ±${b.insertAngle}°. Choose an insert whose −10 dB beam edge is near ±${b.insertAngle}°.

## Parts

- Collector bowl, Ø${f(2*b.radius)} mm, 3 mm shell. The wall thickens into ${m.p.feedLegs} rod seats between the flat top and the rim, so nothing hangs below the rim. Prints on its flat top with the reflecting face up; no supports.
${t.tube?`- Mast foot: bolts to the hub front through the four mount holes. Prints flange down.
- Insert cup: Ø${m.p.collectorDiameter} mm bore, ${f(t.cupDepth)} mm deep, on the mast tube. Prints socket down.
- Mast tube: Ø${t.diameter} mm, cut ${f(t.cutLength)} mm. Aluminum or rigid conduit; the insert cable runs inside it.`:`- Insert pedestal: one printed column from the hub front to the insert cup (this dish is too deep for a tube). Prints flange down.`}

## Assembly

1. Heat-set the M3 inserts: one in each petal rod socket${t.tube?' and one in the rib of the mast foot and insert cup':''}.
2. Bolt the ${t.tube?'mast foot':'pedestal'} to the hub front with the four mount through bolts; the nuts sit on top of the ${t.flange} mm flange.
${t.tube?`3. Feed the insert cable through the tube, seat the tube in the foot and the cup, and snug both set screws.
4. Seat the insert in the cup with its phase center ${f(b.offset)} mm above the cup rim (z = ${f(b.insertFocus)} mm).`:`3. Feed the insert cable up through the hub center and pedestal.
4. Seat the insert in the cup with its phase center ${f(b.offset)} mm above the cup rim (z = ${f(b.insertFocus)} mm).`}
5. Mark each of the ${m.p.feedLegs} × Ø${g.rodDiameter} mm rods (cut ${f(g.cutLength)} mm) 18 mm from the petal end and ${+g.upperEngagement.toFixed(1)} mm from the bowl end. Fit them into the petals, then push them into the bowl's rod seats up to the marks. Center the bowl over the hub, drill each rod Ø${bolt.size+.2} through the seat's cross hole, then fit the M${bolt.size} bolts with a washer on each flat face and a nyloc.
6. Line the bowl's concave face with bonded aluminum or copper foil, seams pressed flat.

## Checks

- Heights run along the dish axis from the paraboloid vertex; the hub front is at z = ${f(t.footZ)} mm.
- Bowl vertex z = ${f(b.vertex)} mm, rim z = ${f(b.edgeZ)} mm.
- Bowl apex to insert: ${f(b.apexToInsert)} mm.${b.nearField?` The insert's far field starts at ${f(b.nearField)} mm.`:''}
- Shadow cone half-angle from the prime focus: ${f(b.shadowAngle)}°. Keep everything under the bowl inside it.

${g.warnings.map(w=>'- '+w).join('\n')}
`;}
