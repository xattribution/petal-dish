// Petal stiffeners, both printed as part of the petal and both optional.
// Rim band: a wall behind the rim of the outer petals, parallel to the dish axis (an L in section), with a lip toward the
// hub on a round bend at its foot for a U, or a 45° brace from its foot back up to the shell for a closed triangle. In the side print every layer is one long curve that ends at the rim; the band turns that
// free end into a hook, so the tall rim edge stops flexing when the nozzle reverses there, and the band's end face adds
// bed contact where a long strip starts to lift. Assembled, the bands of all the outer petals form one ring.
// Diamond ribs: low ribs in two mirrored families across the underside. Their 45° flanks keep every face printable in
// any direction, and the mirrored layout makes the ribs of neighboring petals meet at the seams.
// Coordinates: a petal's own frame (it spans -h..h about +x), z along the dish axis. In the side print, up is
// (sin h, cos h, 0) and z is horizontal, so a face is steeper than 45° when its normal's in-plane part points down.
import {loft,solid} from './solid.js';
import {patch} from './mesh.js';
const DEG=Math.PI/180;
export const RIM={wall:3,gusset:4,bend:4.5,lip:2},RIB={top:1.6,angle:35*DEG};
// Lean of the band (inward going back): none unless the petal is so wide (6 petals) that a band parallel to the axis
// would face down more than 45° near the top of its side print; then just enough to keep it at 45°.
export const rimLean=h=>{const s=Math.sin(2*h);return s>Math.SQRT1_2+1e-9?Math.acos(Math.SQRT1_2/s)+2*DEG:0;};
// How far in from the rim the band reaches at its deepest. Seam hardware stops short of it.
export const rimReach=(p,h)=>{if(!p.rimBand)return 0;const b=rimLean(h);return Math.max(RIM.wall+RIM.gusset,RIM.wall/Math.cos(b)+p.rimDepth*Math.tan(b)+(p.rimBand===2?RIM.bend+RIM.wall/2+RIM.lip:p.rimBand===3?p.rimDepth:0));};
// Ear-clipping triangulation of a simple polygon ([x, y] points), for the end caps of a non-convex section.
function triangulate(poly){const cross=(a,b,c)=>(poly[b][0]-poly[a][0])*(poly[c][1]-poly[a][1])-(poly[b][1]-poly[a][1])*(poly[c][0]-poly[a][0]);
 let idx=poly.map((_,k)=>k);if(idx.reduce((sum,k)=>sum+cross(0,k,(k+1)%poly.length),0)<0)idx.reverse();
 const inside=(q,a,b,c)=>{const s1=cross(a,b,q),s2=cross(b,c,q),s3=cross(c,a,q);return s1>=0&&s2>=0&&s3>=0;},tris=[];
 for(let guard=0;idx.length>3&&guard<10000;guard++){let cut=false;for(let k=0;k<idx.length;k++){const a=idx[(k+idx.length-1)%idx.length],b=idx[k],c=idx[(k+1)%idx.length];
   if(cross(a,b,c)<=1e-9||idx.some(j=>j!==a&&j!==b&&j!==c&&inside(j,a,b,c)))continue;tris.push([a,b,c]);idx.splice(k,1);cut=true;break;}if(!cut)break;}
 if(idx.length!==3)throw Error('Rim band section is not a simple polygon.');return[...tris,idx];}
// One (r, z) section swept around the rim, slightly past both seams (the petal's seam trims cut it back). Convex
// sections use the plain loft; a non-convex one (the U) gets its end caps triangulated properly.
const sweep=(p,h,R,section,concave=false)=>{const count=Math.max(12,Math.ceil((2*h+.06)*R/Math.max(2,p.resolution))),rings=Array.from({length:count+1},(_,i)=>{const phi=-h-.03+(2*h+.06)*i/count,c=Math.cos(phi),s=Math.sin(phi);return section(c,s).map(([r,z])=>[r*c,r*s,z]);});
 if(!concave)return loft(rings);
 const n=rings[0].length,v=rings.flat(),f=[],caps=triangulate(section(1,0)),last=(rings.length-1)*n;
 for(const[a,b,c]of caps){f.push([a,c,b],[last+a,last+b,last+c]);}
 for(let k=0;k<rings.length-1;k++)for(let j=0;j<n;j++){const a=k*n+j,b=k*n+(j+1)%n,c=b+n,d=a+n;f.push([a,b,c],[a,c,d]);}
 let vol=0;for(const[i,j,k]of f){const a=v[i],b=v[j],c=v[k];vol+=a[0]*(b[1]*c[2]-b[2]*c[1])+a[1]*(b[2]*c[0]-b[0]*c[2])+a[2]*(b[0]*c[1]-b[1]*c[0]);}if(vol<0)f.forEach(x=>x.reverse());return solid({v,f});};
// back(x, y): the shell's rear surface; rimZ: the smooth rear surface at the rim, which sets the band's flat foot.
export function rimBandSolid(p,h,back,rimZ){
 const R=p.diameter/2,b=rimLean(h),tb=Math.tan(b),T=RIM.wall/Math.cos(b),W=RIM.wall,G=RIM.gusset,z0=rimZ-p.rimDepth,
  out=z=>R-(rimZ-z)*tb,inn=z=>out(z)-T;
 // The U's foot: the wall bends toward the hub on a round 90° bend (4.5 mm to its middle) and runs on 2 mm. The bend
 // and lip lie across the print layers, so they print as curves in each layer. Built from convex pieces.
 const U=p.rimBand===2,w=[-Math.sin(b),-Math.cos(b)],i=[-Math.cos(b),Math.sin(b)],F=[(out(z0)+inn(z0))/2,z0],rc=RIM.bend,
  C=[F[0]+rc*i[0]-(rc+W/2)*w[0],F[1]+rc*i[1]-(rc+W/2)*w[1]],arc=(radius,t)=>[C[0]-radius*Math.cos(t)*i[0]+radius*Math.sin(t)*w[0],C[1]-radius*Math.cos(t)*i[1]+radius*Math.sin(t)*w[1]],
  O=t=>arc(rc+W/2,t),I=t=>arc(rc-W/2,t);
 // the wall: its top runs 0.5 mm into the shell so the two merge; its outer face is trimmed to the rim with the petal.
 // A U's wall, bend and lip are one section: down the inner face, round the inner arc, across the lip end and back
 // round the outer arc.
 const steps=10,bend=k=>Math.PI/2*k/steps,ends=()=>{const o=O(Math.PI/2),n=I(Math.PI/2);return[[n[0]+RIM.lip*i[0],n[1]+RIM.lip*i[1]],[o[0]+RIM.lip*i[0],o[1]+RIM.lip*i[1]]];};
 let band=sweep(p,h,R,(c,s)=>{const top=r=>back(r*c,r*s)+.5;return[[R+1,top(R+1)],[R-T,top(R-T)],...(U?[...Array.from({length:steps+1},(_,k)=>I(bend(k))),...ends(),...Array.from({length:steps+1},(_,k)=>O(bend(steps-k)))]:[[inn(z0),z0],[out(z0),z0]])];},U);
 // a 45° fillet where the wall meets the shell
 band=band.union(sweep(p,h,R,(c,s)=>{const top=r=>back(r*c,r*s)+.5,zg=top(R-T)-.5-G,rg=inn(zg)-G;return[[inn(zg)+.3,zg],[R-T+.3,top(R-T+.3)],[rg,top(rg)]];}));
 // the U's lip toward the hub, as thick as the wall; its inner edge leans like the wall
 // the triangle's brace: a wall from the foot at 45° back up to the shell, closing a triangular tube with the wall
 // and the shell; it runs on past the shell and is cut back to it below
 if(p.rimBand===3){const f=[inn(z0)+1,z0],L=2*p.rimDepth+20,d=[-Math.SQRT1_2,Math.SQRT1_2],nn=[Math.SQRT1_2,Math.SQRT1_2],at=(P,k,j=0)=>[P[0]+d[0]*k+nn[0]*j,P[1]+d[1]*k+nn[1]*j];
  band=band.union(sweep(p,h,R,()=>[f,at(f,L),at(f,L,W),at(f,0,W)]));}
 // Nothing may reach the reflecting face: the band is kept behind the shell's rear surface (plus the 0.5 mm that merges
 // it). On a deep dish the shell falls away fast near the rim, so a U's lip can meet it and close into a tube.
 const behind=solid(patch({r0:R-rimReach(p,h)-G-10,r1:R+2,a0:-h-.04,a1:h+.04,topFn:(x,y)=>back(x,y)+.5,backFn:()=>z0-5},p));
 return band.intersect(behind);}
// Diamond-grid ribs. region: rMin/rMax (radii a rib may reach; past a seam, a ring flange or the band it is trimmed off
// with the petal and merges there) and keepOut(x, y) for hardware and sockets. A rib that stops in the open ramps down
// over three times its height, so its end prints like its flanks.
export function ribSolids(p,h,back,{rMin,rMax,keepOut}){
 const H=p.ribHeight,w=RIB.top/2,th=RIB.angle,ramp=3*H,step=Math.max(1,Math.min(3,p.resolution/2)),ribs=[];
 const free=(x,y)=>{const r=Math.hypot(x,y);return Math.abs(Math.atan2(y,x))<=h+.15&&r>=rMin&&r<=rMax&&!keepOut(x,y);};
 const span=[[0,-rMax],[rMax,-rMax],[rMax,rMax],[0,rMax]];
 for(const sign of [1,-1]){const d=[Math.cos(th),sign*Math.sin(th)],q=[-Math.sin(th),sign*Math.cos(th)],dot=(a,b)=>a[0]*b[0]+a[1]*b[1];
  const cs=span.map(P=>dot(P,q)),ts=span.map(P=>dot(P,d)),t0=Math.min(...ts),t1=Math.max(...ts);
  // the same offsets in both families, so the grid is mirror-symmetric and its diamonds close on the centerline
  for(let k=Math.floor(-Math.max(...cs)/p.ribPitch);k<=Math.ceil(-Math.min(...cs)/p.ribPitch);k++){const c=-k*p.ribPitch,at=t=>[c*q[0]+t*d[0],c*q[1]+t*d[1]];
   let start=null;const runs=[];for(let t=t0;t<=t1+step;t+=step){const ok=t<=t1&&free(...at(t));if(ok&&start===null)start=t;else if(!ok&&start!==null){runs.push([start,t-step]);start=null;}}
   for(const[a,b]of runs){if(b-a<2*ramp+4)continue;const count=Math.max(4,Math.ceil((b-a)/step));
    ribs.push(loft(Array.from({length:count+1},(_,i)=>{const t=a+(b-a)*i/count,[x,y]=at(t),hh=H*Math.min(1,(t-a)/ramp,(b-t)/ramp);
     // flanks at 45° to the dish axis whatever the shell's slope across the rib: the base widens on a slope
     const g=Math.abs(back(x+q[0],y+q[1])-back(x-q[0],y-q[1]))/2,k2=(hh+.5)/(1-Math.min(.8,g)),side=(u,z)=>[x+u*q[0],y+u*q[1],z];
     return[side(-w-k2,back(x-(w+k2)*q[0],y-(w+k2)*q[1])+.5),side(w+k2,back(x+(w+k2)*q[0],y+(w+k2)*q[1])+.5),side(w,back(x,y)-hh),side(-w,back(x,y)-hh)];})));}}}
 return ribs.length?ribs.reduce((a,b)=>a.union(b)):null;}
