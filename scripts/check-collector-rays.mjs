// Independent ray trace of the Gregorian collector: plane wave down -> parabola -> F1 -> the generated bowl profile
// (marched, not the closed form) -> reflect with the exact ellipse normal -> where does it cross the axis?
// Usage: node scripts/check-collector-rays.mjs ['[{"fd":0.3}, ...]']   (feedMode 3 is implied)
import {defaults} from '../dist/params.js';
import {feedGeometry} from '../dist/feed.js';
import assert from 'node:assert/strict';
const cases=JSON.parse(process.argv[2]||'[{},{"fd":0.3},{"fd":0.6,"collectorAngle":20},{"fd":0.8,"collectorAngle":15},{"fd":0.25,"collectorAngle":45,"collectorDiameter":20},{"diameter":800},{"frequencyGHz":24,"secondaryWaves":8}]').map(c=>({feedMode:3,...c}));
for(const c of cases){try{const p={...defaults,...c},g=feedGeometry(p,{n:6,rows:1}),b=g.bowl,f=p.diameter*p.fd,R=p.diameter/2;
 const prof=b.profile; // [r,z] from vertex to edge
 const surf=r=>{for(let i=1;i<prof.length;i++)if(prof[i][0]>=r){const[r0,z0]=prof[i-1],[r1,z1]=prof[i];const t=(r-r0)/(r1-r0);return z0+t*(z1-z0);}return NaN;};
 let worstEll=0,worst=0,hits=0,miss=0,blocked=0;
 for(let i=1;i<=60;i++){const x=R*i/60;
  if(x<=b.outerRadius){blocked++;continue;}
  // ray from dish point to F1 continues beyond F1 on the other side: direction from (x, x²/4f) through (0,f)
  const P=[x,x*x/(4*f)],d=[-x,f-P[1]],L=Math.hypot(...d),u=[d[0]/L,d[1]/L];
  // march to the bowl surface: find t where point is above surf (the bowl is above F1 on the opposite side, r = -(point r))
  let t0=L,t1=null;for(let t=L;t<L+4*f;t+=.05){const q=[P[0]+u[0]*t,P[1]+u[1]*t],zs=surf(Math.abs(q[0]));if(Number.isFinite(zs)&&q[1]>=zs){t1=t;break;}t0=t;}
  if(t1===null){miss++;continue;}for(let k=0;k<60;k++){const t=(t0+t1)/2,q=[P[0]+u[0]*t,P[1]+u[1]*t],zs=surf(Math.abs(q[0]));if(Number.isFinite(zs)&&q[1]>=zs)t1=t;else t0=t;}
  const t=(t0+t1)/2,q=[P[0]+u[0]*t,P[1]+u[1]*t],r=Math.abs(q[0]);if(!(r<=b.radius)){miss++;continue;}hits++;
  // surface normal from the profile slope at r (in the q half-plane)
  // exact ellipse normal at the hit: bisector of the unit vectors toward both foci
  const e1=[0-q[0],f-q[1]],e2=[0-q[0],b.insertFocus-q[1]],l1=Math.hypot(...e1),l2=Math.hypot(...e2);let nrm=[e1[0]/l1+e2[0]/l2,e1[1]/l1+e2[1]/l2];const nl=Math.hypot(...nrm);nrm=nrm.map(v=>v/nl);
  worstEll=Math.max(worstEll,Math.abs(l1+l2-2*b.a));
  const dn=u[0]*nrm[0]+u[1]*nrm[1],v=[u[0]-2*dn*nrm[0],u[1]-2*dn*nrm[1]];
  // where does the reflected ray cross the axis (r=0)?
  const s=-q[0]/v[0],zAxis=q[1]+s*v[1];worst=Math.max(worst,Math.abs(zAxis-b.insertFocus));}
 assert.equal(miss,0,'every unshaded ray lands on the bowl');assert(worst<1e-3&&worstEll<.01,'reflections reach F2');
 const res={case:JSON.stringify(c),bowlD:+(2*b.radius).toFixed(1),hits,miss,blocked,worstFocusErrorMM:+worst.toFixed(4),hitOffEllipseMM:+worstEll.toFixed(4),mastTube:g.mast.tube,F2:+b.insertFocus.toFixed(1),theta:+(Math.atan2(b.radius,b.edgeZ-b.insertFocus)*180/Math.PI).toFixed(2)};console.log(JSON.stringify(res));
}catch(e){console.log(JSON.stringify(c),'ERR',e.message);process.exitCode=1;}}
