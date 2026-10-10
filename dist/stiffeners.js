// Petal stiffeners, both printed as part of the petal and both optional.
// Rim band: a wall behind the rim of the outer petals, square to the dish (parallel to its axis), with a 45° fillet to
// the shell. L is the wall alone. Frame (the default) bends its foot 45° in toward the hub and runs on 5 mm; U bends it
// 90° and runs on 2 mm. The bends are round. In the side print every layer is one long curve that ends at the rim; the
// band turns that free end into a hook, so the tall rim edge stops flexing when the nozzle reverses there, and the
// band's end face adds bed contact where a long strip starts to lift. Assembled, the bands of all the outer petals
// form one frame around the dish. A front lip (1 mm by default) carries the wall on above the reflecting face, so the
// edge is braced on both sides; it covers only the outer 3 mm of the aperture, where the feed puts little energy.
// The wall stays square to the dish with any petal count. With 6 petals its inner face reaches 60° from vertical near
// the top of the side print (45° at 8 petals, less above): a thick, short face on the inside, printed without support.
// Underside ribs: a diamond grid of low ribs in two mirrored families, a ring rib half way along each petal, or both.
// Their 45° flanks keep every face printable in any direction, and the ribs of neighboring petals meet at the seams.
// Coordinates: a petal's own frame (it spans -h..h about +x), z along the dish axis. In the side print, up is
// (sin h, cos h, 0) and z is horizontal, so a face is steeper than 45° when its normal's in-plane part points down.
import {loft,solid} from './solid.js';
import {patch} from './mesh.js';
const DEG=Math.PI/180;
export const RIM={wall:3,gusset:4},RIB={top:1.6,angle:35*DEG};
// Feet: the bend toward the hub (degrees), the bend's radius to the middle of the wall, and the straight lip after it.
const FEET={2:{angle:90,bend:4.5,lip:2},3:{angle:45,bend:4,lip:5}};
// The section below the shell, as (r, z) points from the wall's inner face down, round the foot and back up its outer
// face. R is the rim radius and rimZ the rim's rear surface; the wall runs rimDepth back from there before its foot.
function bandFoot(p,R,rimZ){const W=RIM.wall,zS=rimZ-p.rimDepth,foot=FEET[p.rimBand];
 if(!foot)return[[R-W,zS],[R,zS]];
 // centerline: down the wall, then round a bend of radius rc about C toward the hub, then straight on
 const rc=foot.bend,th=foot.angle*DEG,C=[R-W/2-rc,zS],arc=(radius,t)=>[C[0]+radius*Math.cos(t),C[1]-radius*Math.sin(t)],dir=[-Math.sin(th),-Math.cos(th)],
  steps=Math.max(4,Math.ceil(foot.angle/9)),ts=Array.from({length:steps+1},(_,k)=>th*k/steps),I=t=>arc(rc-W/2,t),O=t=>arc(rc+W/2,t),on=(P,L)=>[P[0]+L*dir[0],P[1]+L*dir[1]];
 return[...ts.map(I),on(I(th),foot.lip),on(O(th),foot.lip),...ts.slice().reverse().map(O)];}
// How far in from the rim the band reaches (its foot or its fillet). Seam hardware stops short of it.
export const rimReach=p=>p.rimBand?Math.max(RIM.wall+RIM.gusset,-Math.min(...bandFoot(p,0,0).map(([r])=>r))):0;
// Ear-clipping triangulation of a simple polygon ([x, y] points), for the end caps of a non-convex section.
function triangulate(poly){const cross=(a,b,c)=>(poly[b][0]-poly[a][0])*(poly[c][1]-poly[a][1])-(poly[b][1]-poly[a][1])*(poly[c][0]-poly[a][0]);
 let idx=poly.map((_,k)=>k);if(idx.reduce((sum,k)=>sum+cross(0,k,(k+1)%poly.length),0)<0)idx.reverse();
 const inside=(q,a,b,c)=>{const s1=cross(a,b,q),s2=cross(b,c,q),s3=cross(c,a,q);return s1>=0&&s2>=0&&s3>=0;},tris=[];
 for(let guard=0;idx.length>3&&guard<10000;guard++){let cut=false;for(let k=0;k<idx.length;k++){const a=idx[(k+idx.length-1)%idx.length],b=idx[k],c=idx[(k+1)%idx.length];
   if(cross(a,b,c)<=1e-9||idx.some(j=>j!==a&&j!==b&&j!==c&&inside(j,a,b,c)))continue;tris.push([a,b,c]);idx.splice(k,1);cut=true;break;}if(!cut)break;}
 if(idx.length!==3)throw Error('Rim band section is not a simple polygon.');return[...tris,idx];}
// One (r, z) section swept around the rim, slightly past both seams (the petal's seam trims cut it back). Convex
// sections use the plain loft; a non-convex one (a foot) gets its end caps triangulated properly.
const sweep=(p,h,R,section,concave=false)=>{const count=Math.max(12,Math.ceil((2*h+.06)*R/Math.max(2,p.resolution))),rings=Array.from({length:count+1},(_,i)=>{const phi=-h-.03+(2*h+.06)*i/count,c=Math.cos(phi),s=Math.sin(phi);return section(c,s).map(([r,z])=>[r*c,r*s,z]);});
 if(!concave)return loft(rings);
 const n=rings[0].length,v=rings.flat(),f=[],caps=triangulate(section(1,0)),last=(rings.length-1)*n;
 for(const[a,b,c]of caps){f.push([a,c,b],[last+a,last+b,last+c]);}
 for(let k=0;k<rings.length-1;k++)for(let j=0;j<n;j++){const a=k*n+j,b=k*n+(j+1)%n,c=b+n,d=a+n;f.push([a,b,c],[a,c,d]);}
 let vol=0;for(const[i,j,k]of f){const a=v[i],b=v[j],c=v[k];vol+=a[0]*(b[1]*c[2]-b[2]*c[1])+a[1]*(b[2]*c[0]-b[0]*c[2])+a[2]*(b[0]*c[1]-b[1]*c[0]);}if(vol<0)f.forEach(x=>x.reverse());return solid({v,f});};
// back(x, y): the shell's rear surface; front(x, y): the reflecting face; rimZ: the smooth rear surface at the rim,
// from which the wall's depth counts.
export function rimBandSolid(p,h,back,rimZ,front){
 const R=p.diameter/2,W=RIM.wall,G=RIM.gusset,foot=bandFoot(p,R,rimZ);
 // the wall and its foot as one section: its top runs 0.5 mm into the shell so the two merge, and its outer face is
 // trimmed to the rim with the petal
 let band=sweep(p,h,R,(c,s)=>{const top=r=>back(r*c,r*s)+.5;return[[R+1,top(R+1)],[R-W,top(R-W)],...foot];},Boolean(FEET[p.rimBand]));
 // a 45° fillet where the wall meets the shell
 band=band.union(sweep(p,h,R,(c,s)=>{const top=r=>back(r*c,r*s)+.5,zg=top(R-W)-.5-G,rg=R-W-G;return[[R-W+.3,zg],[R-W+.3,top(R-W+.3)],[rg,top(rg)]];}));
 // Nothing may reach the reflecting face: the band is kept behind the shell's rear surface (plus the 0.5 mm that
 // merges it). On a very deep dish the shell falls away fast near the rim and can meet a foot.
 const behind=solid(patch({r0:R-rimReach(p)-G-10,r1:R+2,a0:-h-.04,a1:h+.04,topFn:(x,y)=>back(x,y)+.5,backFn:()=>Math.min(...foot.map(([,z])=>z))-5},p));
 band=band.intersect(behind);
 // the front lip: the wall carried on through the shell to rimLip above the reflecting face, following it
 if(p.rimLip>0)band=band.union(sweep(p,h,R,(c,s)=>{const b=r=>back(r*c,r*s)+.3,f=r=>front(r*c,r*s)+p.rimLip;return[[R-W,b(R-W)],[R+1,b(R+1)],[R+1,f(R+1)],[R-W,f(R-W)]];}));
 return band;}
// A rib along a path: at(t) gives the point [x, y] and the across direction q at distance t; it runs from a to b and ramps
// down over three times its height at each end, so the ends print like the flanks. The flanks stay at 45° to the dish
// axis whatever the shell's slope across the rib: the base widens on a slope.
function ribRun(back,at,a,b,H,step){const w=RIB.top/2,ramp=3*H,count=Math.max(4,Math.ceil((b-a)/step));
 return loft(Array.from({length:count+1},(_,i)=>{const t=a+(b-a)*i/count,[[x,y],q]=at(t),hh=H*Math.min(1,(t-a)/ramp,(b-t)/ramp),
  g=Math.abs(back(x+q[0],y+q[1])-back(x-q[0],y-q[1]))/2,k2=(hh+.5)/(1-Math.min(.8,g)),side=(u,z)=>[x+u*q[0],y+u*q[1],z];
  return[side(-w-k2,back(x-(w+k2)*q[0],y-(w+k2)*q[1])+.5),side(w+k2,back(x+(w+k2)*q[0],y+(w+k2)*q[1])+.5),side(w,back(x,y)-hh),side(-w,back(x,y)-hh)];}));}
// Stretches of a path where a rib may stand, as [a, b] in path distance, long enough to ramp up and down.
function runs(free,t0,t1,step,H){const out=[];let start=null;for(let t=t0;t<=t1+step;t+=step){const ok=t<=t1&&free(t);if(ok&&start===null)start=t;else if(!ok&&start!==null){out.push([start,t-step]);start=null;}}return out.filter(([a,b])=>b-a>=6*H+4);}
// Underside ribs. region: rMin/rMax (radii a diamond rib may reach; past a seam, a ring flange or the band it is trimmed
// off with the petal and merges there), ringR (the ring rib's radius, half way along the petal) and keepOut(x, y) for
// hardware and sockets.
export function ribSolids(p,h,back,{rMin,rMax,ringR,keepOut}){
 const step=Math.max(1,Math.min(3,p.resolution/2)),ribs=[];
 if(p.ribs&1){const H=p.ribHeight,th=RIB.angle,span=[[0,-rMax],[rMax,-rMax],[rMax,rMax],[0,rMax]];
  const free=(x,y)=>{const r=Math.hypot(x,y);return Math.abs(Math.atan2(y,x))<=h+.15&&r>=rMin&&r<=rMax&&!keepOut(x,y);};
  for(const sign of [1,-1]){const d=[Math.cos(th),sign*Math.sin(th)],q=[-Math.sin(th),sign*Math.cos(th)],dot=(a,b)=>a[0]*b[0]+a[1]*b[1];
   const cs=span.map(P=>dot(P,q)),ts=span.map(P=>dot(P,d)),t0=Math.min(...ts),t1=Math.max(...ts);
   // the same offsets in both families, so the grid is mirror-symmetric and its diamonds close on the centerline
   for(let k=Math.floor(-Math.max(...cs)/p.ribPitch);k<=Math.ceil(-Math.min(...cs)/p.ribPitch);k++){const c=-k*p.ribPitch,at=t=>[[c*q[0]+t*d[0],c*q[1]+t*d[1]],q];
    for(const[a,b]of runs(t=>free(...at(t)[0]),t0,t1,step,H))ribs.push(ribRun(back,at,a,b,H,step));}}}
 // the ring rib: an arc at ringR from seam to seam (past both, trimmed with the petal), broken around hardware
 if(p.ribs&2){const H=p.ringHeight,at=t=>{const phi=t/ringR,c=Math.cos(phi),s=Math.sin(phi);return[[ringR*c,ringR*s],[c,s]];},t0=-(h+.12)*ringR,t1=(h+.12)*ringR;
  for(const[a,b]of runs(t=>!keepOut(...at(t)[0]),t0,t1,step,H))ribs.push(ribRun(back,at,a,b,H,step));}
 return ribs.length?ribs.reduce((a,b)=>a.union(b)):null;}
