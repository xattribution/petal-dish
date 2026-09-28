import assert from 'node:assert/strict';
import {build,defaults,volume,bounds,validate} from '../dist/geometry.js';
import {feedGeometry,cassegrainGeometry,feedManifest,rodCSV,feedHardware} from '../dist/feed.js';
import {kit,manifest,guideSections} from '../dist/exports.js';
function closed(mesh,name){const es=new Map();for(const f of mesh.f){const[a,b,c]=f.map(i=>mesh.v[i]),u=b.map((x,k)=>x-a[k]),v=c.map((x,k)=>x-a[k]);assert(Math.hypot(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])>1e-9,name+' degenerate');for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],k=Math.min(a,b)+':'+Math.max(a,b),s=es.get(k)||[0,0];s[0]++;s[1]+=a<b?1:-1;es.set(k,s);}}for(const[n,w]of es.values())assert(n===2&&w===0,name+' open edge');assert(volume(mesh)>0,name+' volume');}
for(const cfg of [{feedMode:1},{feedMode:1,feedLegs:4},{feedMode:2},{feedMode:2,feedLegs:4},{feedMode:1,fastenerStyle:1},{feedMode:1,fastenerStyle:2,perforate:1},{feedMode:1,diameter:600,bedX:300,bedY:300},{feedMode:1,phaseOffset:15,packPlates:1}]){
 const m=build({...defaults,...cfg}),g=m.feed;assert.equal(m.layout.n%m.p.feedLegs,0);assert.equal(m.parts.find(p=>p.id.endsWith('-feed')).qty,m.p.feedLegs);assert.equal(m.instances.filter(i=>i.part.id.endsWith('-feed')).length,m.p.feedLegs);
 for(const p of m.parts){closed(p.mesh,p.id);closed(p.output,p.id+' output');assert(Math.abs(bounds(p.output).min[2])<1e-6);assert(p.dim[0]<=m.p.bedX-2*m.p.margin+.001&&p.dim[1]<=m.p.bedY-2*m.p.margin+.001&&p.dim[2]<=m.p.bedZ-2+.001);}
 for(const l of g.legs){const d=Math.hypot(...l.upper.map((x,k)=>x-l.lower[k]));assert(Math.abs(d-(l.cutLength+2*(44-22)))<1e-9);assert(Math.abs(l.lower[2]-(g.datum.front+22))<1e-9);assert(Math.abs(l.upper[2]-(g.carrierFace-22))<1e-9);}
 if(m.p.feedMode===1)assert(Math.abs(g.carrierFace+m.p.phaseOffset-m.focal)<1e-9);
 assert.equal(rodCSV(m).trim().split('\n').length,m.p.feedLegs+1);assert(manifest(m).feed_support.enabled);assert(guideSections(m).some(s=>s.title==='Rod support and optical layout'));
 const zip=new TextDecoder().decode(await kit(m,'').arrayBuffer());assert(zip.includes('RODS.csv')&&zip.includes('FEED-SUPPORT.md')&&zip.includes('FEED-HARDWARE.csv'));const lower=feedHardware(m)[0];assert.equal(lower.quantity,2*m.p.feedLegs);assert(Number(lower.spec.split(' × ')[1])>=lower.grip_mm+7);
 console.log('PASS feed topology, quantities, rods and exports',cfg,'cut',g.cutLength);
}
// Independent ray reflection and constant optical path, not a second invocation of the formula.
const p={...defaults,feedMode:2},s=cassegrainGeometry(p),f=p.diameter*p.fd;
let path;
for(let r=1;r<=s.interceptRadius;r+=1){const z=s.surface(r),norm=[-s.a*r/(s.b2*Math.sqrt(1+r*r/s.b2)),1],nl=Math.hypot(...norm),n=norm.map(x=>x/nl),u=[-r,f-z],ul=Math.hypot(...u),d=u.map(x=>x/ul),dot=d[0]*n[0]+d[1]*n[1],v=d.map((x,i)=>x-2*dot*n[i]),target=[-r,s.backFocus-z],tl=Math.hypot(...target);assert(Math.hypot(...v.map((x,i)=>x-target[i]/tl))<1e-10);const diff=Math.hypot(r,z-s.backFocus)-Math.hypot(r,z-f);if(path!==undefined)assert(Math.abs(diff-path)<1e-9);path=diff;}
assert(s.returnRadius<13.5);assert.throws(()=>validate({...defaults,feedMode:1,sectors:8,feedLegs:3}));assert.throws(()=>build({...defaults,feedMode:1,rearStyle:1}));assert.throws(()=>build({...defaults,feedMode:1,diameter:180}));assert.throws(()=>cassegrainGeometry({...p,secondaryPosition:.95}));assert.throws(()=>cassegrainGeometry({...p,secondaryPosition:.6}));
assert.equal(feedManifest(build(defaults)).enabled,false);
console.log('PASS Cassegrain reflected-ray direction, equal optical path, return aperture and invalid configurations');
