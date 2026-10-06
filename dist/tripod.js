// Tripod base: the printed azimuth base with three splayed, tapered leg sockets on its underside.
// Model frame (cad/simple-mount.scad): Z up, base bottom at Z 0, +Y = boresight at azimuth 0. Leg 1 points along +Y.
// Each socket is an octagonal frustum around its leg axis; the flats face the tangential direction, so one cross bolt
// through the leg bears on two flat faces. The base prints top face down, so the sockets rise from the bed side,
// leaning out by the splay (at most 30°), with a round blind bore, its floor at the base, and a teardrop bolt hole.
import {cylinder,loft} from './solid.js';
import {octagon,teardrop} from './sockets.js';
import {mountMesh} from './mount-fasteners.js';
import {FASTENER} from './params.js';
export const TRIPOD={embed:4,top:3,rimR:55,nutClear:11,clearance:.4,minEngagement:35,engagement:2.5,tipWall:[3,.15],rootWall:[5,.25],stability:1.5,ground:20,legMargin:3,density:{print:1.24,aluminum:2.7}};
const BOLTS=[[14,3],[20,4],[Infinity,5]],NYLOC={3:4,4:5,5:5,6:6};   // Auto: leg Ø below -> M size; nyloc heights (ISO 10511)
export function tripodGeometry(p){
 const T=TRIPOD,d=p.legDiameter,s=p.legSplay*Math.PI/180,bore=d+T.clearance,boreR=bore/2,E=Math.max(T.minEngagement,T.engagement*d);
 const tipWall=Math.max(T.tipWall[0],T.tipWall[1]*d),rootWall=Math.max(T.rootWall[0],T.rootWall[1]*d),afRoot=bore+2*rootWall,afTip=bore+2*tipWall,z0=T.embed;
 // Local t runs along the leg from the root center (Z = embed). The bore floor's highest edge sits 1 mm below the base
 // bottom; the open end is at S. Above the root the socket carries on at full width into the base and is cut off flat
 // at Z = top, inside the plate, so no end face is left exposed under the base.
 const bolt=p.legBolt||BOLTS.find(([lim])=>d<lim)[1],{clear:hole,washer}=FASTENER[bolt],nut=NYLOC[bolt];
 if(hole>d/2+1e-9)throw Error(`M${bolt} leg bolts need legs of at least Ø${2*hole} mm. Pick a smaller leg bolt.`);
 const c=(z0+1+boreR*Math.sin(s))/Math.cos(s),S=c+E,boltAt=S-E/2,af=t=>t<0?afRoot:afRoot+(afTip-afRoot)*t/S;
 const R0=afRoot/2/Math.cos(Math.PI/8),t0=-((T.top-z0+R0*Math.sin(s))/Math.cos(s)+1);
 // Socket corner lines inside the plate (0 <= Z <= top): radial offset a from the root center and tangential offset y.
 const corners=[];for(let i=0;i<=160;i++){const t=t0+(S-t0)*i/160,R=af(t)/2/Math.cos(Math.PI/8);for(let k=0;k<8;k++){const q=Math.PI/8+k*Math.PI/4,x=R*Math.cos(q),y=R*Math.sin(q),z=z0+x*Math.sin(s)-t*Math.cos(s);if(z>=0&&z<=T.top)corners.push([x*Math.cos(s)+t*Math.sin(s),y]);}}
 const reach=rc=>corners.reduce(([lo,hi],[a,y])=>{const r=Math.hypot(rc+a,y);return[Math.min(lo,r),Math.max(hi,r)];},[Infinity,0]);
 let lo=0,hi=T.rimR;for(let i=0;i<50;i++){const mid=(lo+hi)/2;if(reach(mid)[1]>T.rimR)hi=mid;else lo=mid;}
 const rc=lo,[inner]=reach(rc),half=afRoot/2/Math.cos(Math.PI/8);
 if(inner<T.nutClear||rc*Math.sin(Math.PI/3)<half+1)throw Error(`Ø${+d.toFixed(2)} mm leg sockets do not fit the Ø116 mm base. Use legs up to 25.4 mm.`);
 const legs=[0,1,2].map(k=>{const phi=Math.PI/2+k*2*Math.PI/3,er=[Math.cos(phi),Math.sin(phi),0],et=[-Math.sin(phi),Math.cos(phi),0];
  const u=[Math.sin(s)*er[0],Math.sin(s)*er[1],-Math.cos(s)],n=[Math.cos(s)*er[0],Math.cos(s)*er[1],Math.sin(s)],y=et.map(v=>-v),root=[rc*er[0],rc*er[1],z0];
  const at=(t,a=0,b=0)=>root.map((v,i)=>v+t*u[i]+a*n[i]+b*y[i]);
  return{number:k+1,azimuth:phi*180/Math.PI,root,axis:u,tangent:et,normal:n,cross:y,at,boreEnd:at(c),open:at(S),bolt:at(boltAt)};});
 const boltAF=af(boltAt),grip=boltAF+2*1,boltLength=Math.ceil((grip+nut+2)/5)*5;
 return{d,splay:p.legSplay,bore,engagement:E,tipWall,rootWall,afRoot,afTip,rootRadius:rc,innerRadius:inner,boreFloor:c,length:S,start:t0,
  bolt:{size:bolt,hole,washer,length:boltLength,fromOpenEnd:S-boltAt,acrossFlats:boltAF},legs};
}
// Print-frame base mesh with the sockets: the base blank, sockets unioned and bores cut in the model frame, then the
// azimuth bolt and nut cuts for the selected size (no stand screws).
export function tripodBase(p,t=tripodGeometry(p)){
 const sockets=t.legs.map(L=>{const frame=([x,y,z])=>L.at(z,x,y);return loft([octagon(t.afRoot,t.start),octagon(t.afRoot,0),octagon(t.afTip,t.length)]).transform(frame).trim([0,0,-1],-TRIPOD.top);});
 const w=t.bolt.acrossFlats/2+2,cut=t.legs.flatMap(L=>[cylinder(L.boreEnd,L.at(t.length+1),t.bore/2,64),teardrop(L.at(t.length-t.bolt.fromOpenEnd,0,-w),L.at(t.length-t.bolt.fromOpenEnd,0,w),t.bolt.hole/2,[0,0,-1])]);
 return mountMesh('base-legs',p,{add:sockets.reduce((a,b)=>a.union(b)),cut});
}
// Static checks for the selected dish. `points` are dish-frame-free cradle-frame points (elevation axis at the
// origin, Y = boresight at el 0); `masses` are [cradle-frame point, grams] for everything turning in elevation and
// `fixed` are [world point at az 0, grams] for the yoke side.
export function tripodChecks(t,{points,masses,fixed,axisZ,range,lowest,pose}){
 const T=TRIPOD,s=t.splay*Math.PI/180,cs=Math.cos(s),tn=Math.tan(s),zOpen=T.embed-t.length*cs,outer=t.afRoot/2/Math.cos(Math.PI/8);
 // Leg and socket around one axis in its own vertical plane. A point turning in azimuth comes closest in that plane.
 const axisR=z=>t.rootRadius+(T.embed-z)*tn,limit=z=>(z>zOpen?outer:t.d/2)+T.legMargin;
 const deep=points.filter(([,y,z])=>Math.hypot(y,z)>axisZ-T.embed),hits=[];
 for(let e=range[0];e<=range[1];e++){const a=e*Math.PI/180,ca=Math.cos(a),sa=Math.sin(a);let hit=false;
  for(const[x,y,z]of deep){const Z=axisZ+y*sa+z*ca;if(Z>T.embed)continue;const r=Math.hypot(x,y*ca-z*sa);if(Math.abs(r-axisR(Z))*cs<limit(Z)){hit=true;break;}}
  if(hit)hits.push(e);}
 // The selected pose, exactly: each leg as a line from its root down and out.
 let poseHit=false;{const a=pose.elevation*Math.PI/180,b=-pose.azimuth*Math.PI/180;
  for(const[x,y,z]of deep){const Z=axisZ+y*Math.sin(a)+z*Math.cos(a);if(Z>T.embed)continue;const Y=y*Math.cos(a)-z*Math.sin(a),P=[Math.cos(b)*x-Math.sin(b)*Y,Math.sin(b)*x+Math.cos(b)*Y,Z];
   for(const L of t.legs){const w=P.map((v,k)=>v-L.root[k]),s=w[0]*L.axis[0]+w[1]*L.axis[1]+w[2]*L.axis[2];if(s<0)continue;const r=Math.hypot(...w.map((v,k)=>v-s*L.axis[k]));if(r<(s<t.length?outer:t.d/2)+T.legMargin){poseHit=true;break;}}
   if(poseHit)break;}}
 // Center of mass offset from the azimuth axis, worst case over the elevation range.
 let worst=0;for(let e=range[0];e<=range[1];e++){const a=e*Math.PI/180;let m=0,x=0,y=0;
  for(const[[px,py,pz],g]of masses){m+=g;x+=g*px;y+=g*(py*Math.cos(a)-pz*Math.sin(a));}
  for(const[[px,py],g]of fixed){m+=g;x+=g*px;y+=g*py;}
  worst=Math.max(worst,Math.hypot(x,y)/m);}
 // Tipping: the support triangle's nearest edge sits at half the foot radius. Ground: below the dish's lowest point.
 const footFor=L=>t.rootRadius+(t.boreFloor+L)*Math.sin(s),stable=(2*T.stability*worst-t.rootRadius)/Math.sin(s)-t.boreFloor,ground=(T.ground-lowest+T.embed)/cs-t.boreFloor;
 const minLength=Math.max(100,Math.ceil(Math.max(stable,ground)/10)*10);
 return{comOffset:worst,minLegLength:minLength,limitedBy:stable>=ground?'stability':'ground clearance',footRadius:footFor(minLength),height:(t.boreFloor+minLength)*cs-T.embed,
  clearFrom:hits.length?Math.max(...hits)+1:range[0],blocked:hits.length?[Math.min(...hits),Math.max(...hits)]:null,poseClear:!poseHit};
}
// Volume (mm³) and centroid of a closed mesh.
export function meshMass(mesh){let V=0;const c=[0,0,0];for(const[i,j,k]of mesh.f){const a=mesh.v[i],b=mesh.v[j],d=mesh.v[k],v=(a[0]*(b[1]*d[2]-b[2]*d[1])+a[1]*(b[2]*d[0]-b[0]*d[2])+a[2]*(b[0]*d[1]-b[1]*d[0]))/6;V+=v;for(let q=0;q<3;q++)c[q]+=v*(a[q]+b[q]+d[q])/4;}return{volume:V,centroid:c.map(x=>x/V)};}
