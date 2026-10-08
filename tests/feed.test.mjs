import assert from 'node:assert/strict';
import {build,defaults,volume,bounds,validate} from '../dist/geometry.js';
import {feedGeometry,cassegrainGeometry,gregorianGeometry,GREGORIAN,feedManifest,rodCSV,feedHardware,feedGuide} from '../dist/feed.js';
import {kit,manifest,guideSections} from '../dist/exports.js';
function closed(mesh,name){const es=new Map();for(const f of mesh.f){const[a,b,c]=f.map(i=>mesh.v[i]),u=b.map((x,k)=>x-a[k]),v=c.map((x,k)=>x-a[k]);assert(Math.hypot(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])>1e-9,name+' degenerate');for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],k=Math.min(a,b)+':'+Math.max(a,b),s=es.get(k)||[0,0];s[0]++;s[1]+=a<b?1:-1;es.set(k,s);}}for(const[n,w]of es.values())assert(n===2&&w===0,name+' open edge');assert(volume(mesh)>0,name+' volume');}
for(const cfg of [{feedMode:1},{feedMode:1,feedLegs:4},{feedMode:2},{feedMode:2,feedLegs:4},{feedMode:1,diameter:600,bedX:300,bedY:300},{feedMode:1,phaseOffset:15,packPlates:1},{feedMode:2,frequencyGHz:15},{feedMode:2,frequencyGHz:20},{feedMode:1,frequencyGHz:20,rodDiameter:0},{feedMode:1,diameter:556},{feedMode:1,feedLegs:4,diameter:478}]){   // the last two once failed with an unresolved seam (Float32 pinch)
 const m=build({...defaults,...cfg}),g=m.feed;assert.equal(g.datum.holes.length,0);assert(!m.parts.some(p=>p.id==='feed-rim-shoe'));assert.equal(m.layout.n%m.p.feedLegs,0);assert.equal(m.parts.find(p=>p.id.endsWith('-mount')).qty,m.p.feedLegs);assert.equal(m.instances.filter(i=>i.part.id.endsWith('-mount')).length,m.p.feedLegs);
 for(const p of m.parts){closed(p.mesh,p.id);closed(p.output,p.id+' output');assert(Math.abs(bounds(p.output).min[2])<1e-6);assert(p.dim[0]<=m.p.bedX-2*m.p.margin+.001&&p.dim[1]<=m.p.bedY-2*m.p.margin+.001&&p.dim[2]<=m.p.bedZ-2+.001);}
 for(const l of g.legs){assert(Math.abs(Math.hypot(...l.upperRodEnd.map((v,k)=>v-l.lowerRodEnd[k]))-l.cutLength)<1e-8);const d=Math.hypot(...l.upper.map((x,k)=>x-l.lower[k]));assert(Math.abs(d-(l.cutLength+g.lowerEntrance+g.upperEntrance-36))<1e-9);assert(Math.abs(l.lower[2]-g.datum.rear)<1e-9);assert(Math.abs(l.upper[2]-(g.carrierFace+8))<1e-9);}
 if(m.p.feedMode===1)assert(Math.abs(g.carrierFace+m.p.phaseOffset-m.focal)<1e-9);
 assert.equal(rodCSV(m).trim().split('\n').length,m.p.feedLegs+1);assert(manifest(m).feed_support.enabled);assert(guideSections(m).some(s=>s.title==='Rod support'));
 const zip=new TextDecoder().decode(await kit(m,'').arrayBuffer());assert(zip.includes('RODS.csv')&&zip.includes('FEED-SUPPORT.md')&&zip.includes('FEED-HARDWARE.csv'));const lower=feedHardware(m)[0];assert.equal(lower.quantity,m.p.feedLegs);assert.equal(lower.spec,'M3 × 12');assert(!feedHardware(m).some(h=>h.item.includes('rim-foot')));
 console.log('PASS feed topology, quantities, rods and exports',cfg,'cut',g.cutLength);
}
// Independent ray reflection and constant optical path, not a second invocation of the formula.
const p={...defaults,feedMode:2},s=cassegrainGeometry(p),f=p.diameter*p.fd;
let path;
for(let r=1;r<=s.interceptRadius;r+=1){const z=s.surface(r),norm=[-s.a*r/(s.b2*Math.sqrt(1+r*r/s.b2)),1],nl=Math.hypot(...norm),n=norm.map(x=>x/nl),u=[-r,f-z],ul=Math.hypot(...u),d=u.map(x=>x/ul),dot=d[0]*n[0]+d[1]*n[1],v=d.map((x,i)=>x-2*dot*n[i]),target=[-r,s.backFocus-z],tl=Math.hypot(...target);assert(Math.hypot(...v.map((x,i)=>x-target[i]/tl))<1e-10);const diff=Math.hypot(r,z-s.backFocus)-Math.hypot(r,z-f);if(path!==undefined)assert(Math.abs(diff-path)<1e-9);path=diff;}
assert(s.returnRadius<13.5);assert.throws(()=>validate({...defaults,feedMode:1,sectors:8,feedLegs:3}));assert(build({...defaults,feedMode:1,rearStyle:1}).feed);assert.throws(()=>build({...defaults,feedMode:1,diameter:180}));assert.throws(()=>cassegrainGeometry({...p,secondaryPosition:.95}));assert.throws(()=>cassegrainGeometry({...p,secondaryPosition:.6}));
const layout={n:6,rows:1};
const a=feedGeometry({...defaults,feedMode:2,frequencyGHz:15},layout),b=feedGeometry({...defaults,feedMode:2,frequencyGHz:20},layout);
assert(Math.abs(2*a.secondary.radius-4*299.792458/15)<1e-7);assert(Math.abs(2*b.secondary.radius-4*299.792458/20)<1e-7);assert.notEqual(a.cutLength,b.cutLength);assert.notEqual(a.rodAngle,b.rodAngle);
assert.throws(()=>feedGeometry({...defaults,feedMode:2,frequencyGHz:2.4},layout),/25%/);
const fixedA=feedGeometry({...defaults,feedMode:1,frequencyGHz:2.4},layout),fixedB=feedGeometry({...defaults,feedMode:1,frequencyGHz:20},layout);assert.equal(fixedA.cutLength,fixedB.cutLength);assert(fixedB.surfaceRmsBudget<fixedA.surfaceRmsBudget);
const scaled=feedGeometry({...defaults,feedMode:1,frequencyGHz:10,phaseUnits:1,phaseOffset:.2},layout);assert(Math.abs(scaled.carrierFace+299.792458/10*.2-168)<1e-9);assert.throws(()=>feedGeometry({...defaults,feedMode:1,phaseUnits:1},layout));
for(const frequencyGHz of [5,20]){const g=feedGeometry({...defaults,feedMode:1,frequencyGHz,rodDiameter:0},layout);assert(g.screenDeflection<=g.targetDeflection);}
assert.throws(()=>feedGeometry({...defaults,feedMode:1,frequencyGHz:50,rodDiameter:0},layout),/deflection budget/);
console.log('PASS frequency sizing, fixed focus, phase scaling and automatic rod screening');
assert.equal(feedManifest(build(defaults)).enabled,false);
console.log('PASS Cassegrain reflected-ray direction, equal optical path, return aperture and invalid configurations');

// Keep the default fittings compact: these budgets guard against enclosing all
// rod angles in large solid blocks again.
const slim=build({...defaults,feedMode:1});assert(volume(slim.parts.find(p=>p.spec.feedMount).mesh)-volume(slim.parts.find(p=>p.kind==='panel'&&!p.spec.feedMount).mesh)<3500);assert(volume(slim.parts.find(p=>p.id==='feed-puck').mesh)<45000);console.log('PASS compact feed fitting material budgets');

for(const rodDiameter of [2,3.175,7.9375,12.7]){validate({...defaults,rodDiameter});assert.equal(feedGeometry({...defaults,feedMode:1,rodDiameter},layout).rodDiameter,rodDiameter);}for(const rodDiameter of [1.99,12.71])assert.throws(()=>validate({...defaults,rodDiameter}));

// Gregorian collector. The bowl is an ellipsoid with foci at the dish focus F1 and the insert F2, re-solved from D and f/D:
// every dish ray through F1 lands on the bowl and folds to F2, the rim ray reaches F2 at the insert half-angle, and the
// insert hides in the bowl's shadow.
for(const cfg of [{},{fd:.3},{fd:.6,collectorAngle:20},{fd:.8,collectorAngle:15},{diameter:800},{frequencyGHz:12},{fd:.25,collectorAngle:45,collectorDiameter:20},{autoSecondary:0,bowlDiameter:110}]){
 const p={...defaults,feedMode:3,...cfg},b=gregorianGeometry(p),f=p.diameter*p.fd,R=p.diameter/2,F1=[0,f],F2=[0,b.insertFocus],unit=v=>{const l=Math.hypot(...v);return v.map(x=>x/l);};
 assert(Math.abs(b.primaryFocus-f)<1e-9&&b.insertFocus<f);
 for(const[r,z]of b.profile)assert(Math.abs(Math.hypot(r,z-f)+Math.hypot(r,z-b.insertFocus)-2*b.a)<1e-6,'ellipse foci');
 for(const k of [-.99,-.7,-.4,.4,.7,.99]){const P=[k*R,(k*R)**2/(4*f)];if(Math.abs(P[0])<b.outerRadius)continue;
  const d=unit([F1[0]-P[0],F1[1]-P[1]]),psi=Math.acos(d[1]);assert(psi<=b.edgeAngle*Math.PI/180+1e-9,'ray lands on the bowl');
  const t=b.b**2/(b.a+b.c*Math.cos(psi)),H=[F1[0]+t*d[0],F1[1]+t*d[1]],n=unit([...unit([H[0]-F1[0],H[1]-F1[1]])].map((x,i)=>x+unit([H[0]-F2[0],H[1]-F2[1]])[i])),dot=d[0]*n[0]+d[1]*n[1],out=[d[0]-2*dot*n[0],d[1]-2*dot*n[1]],to=unit([F2[0]-H[0],F2[1]-H[1]]);
  assert(Math.abs(out[0]*to[1]-out[1]*to[0])<1e-9&&out[0]*to[0]+out[1]*to[1]>0,'reflects to F2');}
 const edge=b.profile.at(-1);assert(Math.abs(Math.atan2(edge[0],edge[1]-b.insertFocus)*180/Math.PI-p.collectorAngle)<1e-6,'insert half-angle');
 assert(Math.abs(b.edgeAngle-b.rimAngle-GREGORIAN.spill)<1e-9&&Math.abs(b.rimAngle-2*Math.atan(R/(2*f))*180/Math.PI)<1e-9,'rim follows the dish');
 assert((f-b.insertFocus-Math.max(0,-b.offset)-2)*Math.tan(b.shadowAngle*Math.PI/180)>=b.insertRadius+1-1e-6,'insert in the shadow');
 assert(Math.abs(b.magnification-(b.a+b.c)/(b.a-b.c))<1e-9&&b.rimInsertAngle<p.collectorAngle,'ellipse magnification');
 assert(2*b.radius<=GREGORIAN.maxFraction*p.diameter+1e-6&&2*b.radius>=b.minDiameter-1e-6);if(cfg.autoSecondary===0)assert.equal(2*b.radius,110);
}
{const a=gregorianGeometry({...defaults,feedMode:3,fd:.3}),b=gregorianGeometry({...defaults,feedMode:3,fd:.6,collectorAngle:25});assert(a.c/a.a!==b.c/b.a&&a.radius<b.radius,'bowl reshapes with the dish');}
{const m=build({...defaults,feedMode:3}),g=m.feed,ids=m.parts.filter(p=>p.kind==='feed').map(p=>p.id);assert.deepEqual(ids,['feed-bowl','feed-mast-foot','feed-insert-cup']);
 for(const p of m.parts.filter(p=>p.kind==='feed')){closed(p.mesh,p.id);closed(p.output,p.id+' output');}
 const hw=feedHardware(m),q=i=>hw.find(h=>h.item===i);assert.equal(q('bowl rod bolt').quantity,3);assert.equal(q('mast set screw').quantity,2);assert(q('insert mast tube').spec.includes('cut 71.8'));
 assert.equal(rodCSV(m).trim().split('\n').length,5);assert(feedGuide(m).includes('Gregorian'));assert.equal(feedManifest(m).collector.insert_half_angle_degrees,25);
 const deep=build({...defaults,feedMode:3,fd:.3});assert(deep.parts.some(p=>p.id==='feed-insert-pedestal')&&!deep.feed.mast.tube);
 assert.throws(()=>build({...defaults,feedMode:3,hubFlat:0}),/Flat/);assert.throws(()=>build({...defaults,feedMode:3,mountThrough:0}),/through bolts/);
 assert.throws(()=>build({...defaults,feedMode:3,collectorDiameter:60,collectorAngle:35}),/30%/);assert.throws(()=>build({...defaults,feedMode:3,autoSecondary:0,bowlDiameter:60}),/cannot hide/);
 assert.throws(()=>build({...defaults,feedMode:3,fd:.8,collectorAngle:40}),/narrower cone/);}
// The printed bowl's reflecting face is the ellipsoid itself: rays cast up from F2 meet the mesh on the ellipse, and the
// rod seats stay within the bowl's height (nothing below the rim) and outside the reflecting face.
for(const cfg of [{},{fd:.3},{fd:.6,collectorAngle:20},{diameter:800,bedX:300,bedY:300,bedZ:300}]){
 const m=build({...defaults,feedMode:3,...cfg}),b=m.feed.bowl,mesh=m.parts.find(p=>p.id==='feed-bowl').mesh,f=b.primaryFocus;
 const ellipseZ=r=>{let lo=0,hi=b.edgeAngle*Math.PI/180;for(let i=0;i<60;i++){const q=(lo+hi)/2,t=b.b**2/(b.a+b.c*Math.cos(q));if(t*Math.sin(q)<r)lo=q;else hi=q;}const t=b.b**2/(b.a+b.c*Math.cos(lo));return f+t*Math.cos(lo);};
 const tris=mesh.f.map(t=>t.map(i=>mesh.v[i]));let worst=0,n=0;
 for(let i=0;i<14;i++)for(let j=0;j<24;j++){const r=(b.radius-.4)*(i+.5)/14,a=j*Math.PI/12,x=r*Math.cos(a),y=r*Math.sin(a);let hit=Infinity;
  for(const[A,B,C]of tris){const d=(B[0]-A[0])*(C[1]-A[1])-(C[0]-A[0])*(B[1]-A[1]);if(Math.abs(d)<1e-12)continue;const u=((x-A[0])*(C[1]-A[1])-(C[0]-A[0])*(y-A[1]))/d,v=((B[0]-A[0])*(y-A[1])-(x-A[0])*(B[1]-A[1]))/d;if(u<-1e-9||v<-1e-9||u+v>1+1e-9)continue;hit=Math.min(hit,A[2]+u*(B[2]-A[2])+v*(C[2]-A[2]));}
  worst=Math.max(worst,Math.abs(hit-ellipseZ(r)));n++;}
 assert(worst<.03,`reflecting face off the ellipse by ${worst} mm`);
 const zs=mesh.v.map(q=>q[2]);assert(Math.min(...zs)>b.edgeZ-.5&&Math.max(...zs)<b.vertex+3+1e-6,'rod seats stay within the bowl height');
 console.log('PASS printed reflecting face matches the ellipse',JSON.stringify(cfg),n,'rays, worst',worst.toFixed(4),'mm');
}
console.log('PASS Gregorian collector focal geometry, shadow, reshaping, parts, hardware and limits');
