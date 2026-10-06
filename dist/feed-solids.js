import {box,cylinder,loft,revolve,prism,hullPoints} from './solid.js';
import {octFrustum,insertScrewCuts,teardrop,tube} from './sockets.js';
export const feedMesh=s=>s.mesh();
export const feedCylinder=cylinder,feedBox=box;
export const feedTransform=(s,fn)=>s.transform(fn);
export function feedPuckBody(face,secondary=false){const h=16;return loft([[0,21],[1,22],[h-1,22],[h,21]].map(([z,r])=>Array.from({length:64},(_,i)=>{const a=i*Math.PI/32;return[r*Math.cos(a),r*Math.sin(a),face+z];})));}
export function feedSocketBody(diameter){const ro=diameter/2+2.5;
 return loft([[0,ro-.7,.8],[2,ro,1.8],[7,ro,4.7],[12,ro,5.6],[17,ro,4.7],[20.5,ro,1.8],[22,ro-.7,.8]].map(([z,r,bulge])=>Array.from({length:48},(_,i)=>{const a=i*Math.PI/24,s=Math.sin(a),positive=Math.max(0,s);return[(r+3.5*positive)*Math.cos(a),r*s+bulge*positive*positive,z];})));}
export function feedContinuousSocket(diameter,clearance,loadingDirection=1){const ro=diameter/2+2.5;return{ro,cuts:[
 feedCylinder([0,0,2],[0,0,120],(diameter+clearance)/2,48),
 feedCylinder([0,0,12],[0,120,12],1.7,24),
 feedCylinder([0,ro+5.6,12],[0,120,12],3.3,32),
 feedCylinder([0,ro+.6,12],[0,ro+3.2,12],5.8/Math.sqrt(3),6),
 feedBox([60*loadingDirection,ro+1.9,12],[60,1.3,3.45])
 ]};}
// Two small circular tapers join the rod bearing and side insert to the shell.
// Only the bore follows the rod angle; the exterior follows the dish underside.
export function feedIntegralSocket(p,g,n,backFn){
 const h=Math.PI/n,up=[Math.sin(h),Math.cos(h),0],A=[g.datum.r,0,g.lowerZ],B=[g.topRadius??18,0,g.upperZ],axis=B.map((v,k)=>(v-A[k])/g.pivotDistance),dot=axis.reduce((s,v,k)=>s+v*up[k],0),v0=up.map((v,k)=>v-dot*axis[k]),vl=Math.hypot(...v0),V=v0.map(v=>v/vl);
 let T=[axis[1]*V[2]-axis[2]*V[1],axis[2]*V[0]-axis[0]*V[2],axis[0]*V[1]-axis[1]*V[0]];if(T[2]<0)T=T.map(v=>-v);
 const at=(t,w,l)=>A.map((a,k)=>a+T[k]*t+V[k]*w+axis[k]*l),r=(g.rodDiameter+p.rodClearance)/2,depth=3+r+1.2;
 const collar=(cx,cy,inner,d)=>{const outer=inner+(d+.3)/.95;return loft([[inner,-d],[outer,.3],[outer,.5],[outer*.75,.5],[outer*.5,.5],[outer*.25,.5],[.1,.5]].map(([radius,z])=>Array.from({length:63},(_,i)=>{const a=(i+.17)*2*Math.PI/63,x=cx+radius*Math.cos(a),y=cy+radius*Math.sin(a);return[x,y,backFn(x,y)+z];})));};
 const earY=-(r+3.6),earDepth=3+2.1*Math.hypot(1,g.datum.slope)+1.4;
 const body=collar(A[0],0,r+.7,depth).union(collar(A[0],earY,2.7,earDepth)).intersect(cylinder([0,0,-p.diameter],[0,0,p.diameter],p.diameter/2-.1,128));
 // Circular bearing with a short 0.8 mm bridge at the print-up roof.
 // The 45-degree shoulders enclose the complete nominal clearance circle.
 const roof=r*Math.SQRT2-.4,poly=[...Array.from({length:37},(_,i)=>{const a=(135+270*i/36)*Math.PI/180;return[r*Math.cos(a),r*Math.sin(a)];}),[.4,roof],[-.4,roof]];
 const bore=loft([-40,g.lowerEntrance+60].map(l=>poly.map(([t,w])=>at(t,w,l))));
 const pilotEnd=-r-2.5,shoulderEnd=-r-.5,C=at(0,0,1.3);
 const screw=loft([[-100,2.1],[pilotEnd,2.1],[shoulderEnd,1.7],[0,1.7]].map(([y,radius])=>Array.from({length:48},(_,i)=>{const a=i*Math.PI/24;return[C[0]+radius*Math.cos(a),y,C[2]+radius*Math.sin(a)];})));
 return{body,cuts:[bore,screw],frame:{A,axis,T,V,screwL:1.3,insertAxis:[0,1,0],pilotEnd},depth};
}

// Collector rod seat bolt: M3 for 6 mm rods and up, M2 below. Width across the seat's flat side faces.
export const finBolt=rodD=>rodD>=6?{size:3,hole:1.7}:{size:2,hole:1.2};
export const finWidth=(rodD,clearance=.35)=>{const hb=(rodD+clearance)/2+2.5;return{lo:-hb,hi:hb,ro:hb};};
// Area of faces that would print steeper than 45° when +z points down to the bed, ignoring the bed face at z = bed.
function overhangArea(s,bed){const {v,f}=s.rawMesh();let area=0;for(const[i,j,k]of f){const a=v[i],b=v[j],d=v[k];if(a[2]>bed-.01&&b[2]>bed-.01&&d[2]>bed-.01)continue;
 const u=[b[0]-a[0],b[1]-a[1],b[2]-a[2]],w=[d[0]-a[0],d[1]-a[1],d[2]-a[2]],n=[u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]],l=Math.hypot(...n);if(l>0&&n[2]/l>Math.SQRT1_2+.01)area+=l/2;}return area;}
// Collector bowl back, as [r, z] from the vertex out to the rim: the reflecting face plus `wall`, never shallower than
// 45° (it is the underside in print), capped by the flat top at z = top that sits on the bed.
export function bowlBack(front,wall,top){
 const back=new Array(front.length);back[front.length-1]=front.at(-1)[1]+wall;for(let i=front.length-2;i>=0;i--)back[i]=Math.max(front[i][1]+wall,back[i+1]+(front[i+1][0]-front[i][0]));
 const rear=[];for(let i=0;i<front.length;i++){rear.push([front[i][0],Math.min(back[i],top)]);if(i<front.length-1&&back[i]>top&&back[i+1]<top){const k=(back[i]-top)/(back[i]-back[i+1]);rear.push([front[i][0]+k*(front[i+1][0]-front[i][0]),top]);}}
 return rear;
}
// Gregorian collector solids, each in its own local frame (z along the dish axis):
//  bowl: origin at the bowl vertex; prints flat top down, reflecting face up, so it needs no supports.
//  foot: origin on the hub front; prints flange down.   cup: origin at the tube's top end; prints socket down.
export function collectorBodies(p,g){
 const b=g.bowl,m=g.mast,wall=3,Z0=b.vertex,front=b.profile.map(([r,z])=>[r,z-Z0]);
 const top=wall,rear=bowlBack(front,wall,top);
 let bowl=revolve([...front,...[...rear].reverse()],160);
 // Everything below the reflecting face stays clear: subtract that cavity from the finished part.
 const edge=front.at(-1),cavity=revolve([[0,edge[1]-80],[edge[0]+.01,edge[1]-80],[edge[0]+.01,edge[1]],...[...front].reverse()],160);
 // Rod seats (see collectorSeat): the rod collar swept at 45° up to the flat top, plus a small flare into the rim
 // under the seat's mouth. The side faces stay flat where the cross bolt's washers sit.
 const Q=g.seat,c=Math.cos(Q.alpha),sn=Math.sin(Q.alpha),Bx=Q.topRadius,Bz=Q.upperZ-Z0,re=b.radius,ze=b.edgeZ-Z0,bolt=finBolt(g.rodDiameter);
 const rb=(g.rodDiameter+p.rodClearance)/2,ha=rb+Q.wall,hb=ha,k=Math.max(.8,Math.min(2,ha-(g.rodDiameter>=6?3.5:2.5)-.25));
 const section=[[ha,hb-k],[ha-k,hb],[-(ha-k),hb],[-ha,hb-k],[-ha,-(hb-k)],[-(ha-k),-hb],[ha-k,-hb],[ha,-(hb-k)]],lower=section.filter(([a])=>a<0).concat([[0,hb],[0,-hb]]);
 const P=(t,a=0,y=0)=>[Bx+t*c+a*sn,y,Bz-t*sn+a*c],sec=(t,pts=section)=>pts.map(([a,y])=>P(t,a,y)),sweep=q=>q[2]>=top?q:[q[0]-(top-q[2]),q[1],top];
 const core=hullPoints([...sec(0),...sec(Q.length),...sec(0).map(sweep),...sec(Q.length).map(sweep)]).trim([0,0,-1],-top);
 // The flare runs from the rim to the underside of the seat's mouth; it is left out where it would reach the washers.
 const flareAt=(hb+4)/re,anchors=[],along=q=>(q[0]-Bx)*c-(q[2]-Bz)*sn;for(const ph of [-flareAt,0,flareAt])for(const z of [ze+.4,ze+wall-.4])anchors.push([(re-.4)*Math.cos(ph),(re-.4)*Math.sin(ph),z]);   // just inside the rim wall
 const flare=anchors.every(q=>along(q)>=Q.bolt+Q.washer/2+.5)?hullPoints([...anchors,...sec(Q.length-.3,lower.map(([a,y])=>[a*.97,y*.97]))]).trim([0,0,-1],-top):null;
 const cuts=[teardrop(P(Q.floor),P(80),rb,[0,0,-1],48),teardrop(P(Q.bolt,0,-hb-1),P(Q.bolt,0,hb+1),bolt.hole,[0,0,-1])];
 const assemble=seat=>{let out=bowl;for(const a of g.legs.map(l=>l.angle)){const turn=q=>[Math.cos(a)*q[0]-Math.sin(a)*q[1],Math.sin(a)*q[0]+Math.cos(a)*q[1],q[2]];
  out=out.union(seat.transform(turn));for(const cut of cuts)out=out.subtract(cut.transform(turn));}return out.subtract(cavity);};
 // The core is printable by construction. Keep the flare only when the finished bowl has no face under 45° in print
 // (print-down is +z here; the flat top on the bed is exempt).
 if(flare){const withFlare=assemble(core.union(flare));if(overhangArea(withFlare,top)<1){bowl=withFlare;}else bowl=assemble(core);}else bowl=assemble(core);
 // Mast foot on the hub front: flange on the four mount bolts, tapered socket, M3 set screw into an insert on a rib.
 const F=m.flange,ribY=m.bore+6.5,rib=(z0,z1)=>prism([[-4.5,0],[4.5,0],[4.5,ribY],[-4.5,ribY]],z0,z1),ring=(z,rr)=>Array.from({length:96},(_,i)=>{const a=i*Math.PI/48;return[rr*Math.cos(a),rr*Math.sin(a),z];});
 const cupR=b.insertRadius,rimZ=3+m.cupDepth,cupBody=(z,fl)=>revolve(fl>0?[[0,z-fl],[cupR-fl,z-fl],[cupR,z],[cupR,z+rimZ],[0,z+rimZ]]:[[0,z-.02],[cupR,z-.02],[cupR,z+rimZ],[0,z+rimZ]],96),coaxCone=z=>loft([[z-.5,m.bore+.5],[z+m.bore-m.coaxR,m.coaxR]].map(([zz,rr])=>ring(zz,rr)));
 if(!m.tube){// Pedestal: flange, a solid tapered column with the coax hole, and the cup on a 45° flare.
  const H=m.tubeTop-m.footZ,col=Math.max(m.footAF[1],2*m.coaxR+8),fl=Math.max(0,cupR-col/2+.3);
  const ped0=tube(m.flangeRadius,0,F,96).union(octFrustum(Math.max(m.footAF[0],col+4),col,F-.01,H-.01));let ped=ped0.union(cupBody(H,fl));
  for(const c of [tube(m.coaxR,-1,H+3.01),tube(p.collectorDiameter/2+.2,H+3,H+rimZ+1,96)])ped=ped.subtract(c);
  for(let i=0;i<4;i++){const a=Math.PI/4+i*Math.PI/2;ped=ped.subtract(cylinder([30*Math.cos(a),30*Math.sin(a),-1],[30*Math.cos(a),30*Math.sin(a),F+1],2.3,32));}
  return{bowl,foot:ped,cup:null};}
 let foot=tube(m.flangeRadius,0,F,96).union(octFrustum(m.footAF[0],m.footAF[1],F-.01,F+m.footSocket)).union(rib(F-.01,F+m.footSocket));
 for(const c of [tube(m.bore,F,F+m.footSocket+1),tube(m.coaxR,-1,F+.01),...insertScrewCuts(m.bore,F+m.footSocket/2,ribY)])foot=foot.subtract(c);
 for(let i=0;i<4;i++){const a=Math.PI/4+i*Math.PI/2;foot=foot.subtract(cylinder([30*Math.cos(a),30*Math.sin(a),-1],[30*Math.cos(a),30*Math.sin(a),F+1],2.3,32));}
 // Insert cup on the tube top: tapered socket below (narrow end down), 45° flare into the cup, cup above.
 const S=m.cupSocket,apothem=z=>(m.cupAF[0]+(m.cupAF[1]-m.cupAF[0])*(z+S)/S)/2;
  let fl=Math.max(0,cupR-apothem(0));for(let i=0;i<4;i++)fl=Math.max(0,cupR-apothem(-fl)+.3);
 let cup=octFrustum(m.cupAF[0],m.cupAF[1],-S,-.01).union(rib(-S,-.01)).union(cupBody(0,fl));
 for(const c of [tube(m.bore,-S-1,0),coaxCone(0),tube(m.coaxR,0,3.01),tube(p.collectorDiameter/2+.2,3,rimZ+1,96),...insertScrewCuts(m.bore,-S/2,ribY)])cup=cup.subtract(c);
 return{bowl,foot,cup};
}
