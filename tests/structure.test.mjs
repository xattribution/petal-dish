import assert from 'node:assert/strict';
import fs from 'node:fs';
import {build,defaults,volume,bounds,backZ,connectionCoupon,binarySTL,rootBottom,hubFace,hubSeatFloor,ROOT,clipSolid,clipStrain,CLIP,stationFrame,boltY,hardwareSchedule} from '../dist/geometry.js';
import {zAt} from '../dist/mesh.js';
import {solid,solidScope,cylinder} from '../dist/solid.js';
import {kit,manifest} from '../dist/exports.js';
const rotate=a=>q=>[Math.cos(a)*q[0]-Math.sin(a)*q[1],Math.sin(a)*q[0]+Math.cos(a)*q[1],q[2]];
function checkMesh(mesh,name){const edges=new Map();for(const f of mesh.f)for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],key=[Math.min(a,b),Math.max(a,b)].join(':'),e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}assert([...edges.values()].every(([n,w])=>n===2&&w===0),name+' manifold topology');assert(volume(mesh)>0,name+' positive volume');}
fs.mkdirSync('/tmp/petal5-validation',{recursive:true});
const cases=[{}, {rearStyle:1},{diameter:260},{diameter:600},{diameter:800,bedX:300,bedY:300,bedZ:300},{diameter:400,sectors:8,rows:2},{fd:.25},{fd:.8},{thickness:1.6},{thickness:6},{bedZ:120},{rearStyle:1,facetSize:30,resolution:10},{mountThrough:0},{rootThrough:1,hubFlat:0},{seamJoint:1},{seamJoint:1,clipDetent:2},{seamJoint:2,diameter:600},{seamJoint:1,diameter:800,rows:3,sectors:8,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4},{diameter:600,staggerRings:0},{diameter:800,rows:3,sectors:8,bedX:300,bedY:300,bedZ:300}];
for(const[c,cfg]of cases.entries()){
 const m=build({...defaults,...cfg});
 solidScope(()=>{
 for(const p of m.parts){checkMesh(p.mesh,p.id);checkMesh(p.output,p.id+' print');const raw=solid(p.mesh).raw,components=raw.decompose();assert.equal(components.length,1,p.id+' single connected solid');components.forEach(x=>x.delete());assert(p.dim[0]<=m.p.bedX-2*m.p.margin+.001&&p.dim[1]<=m.p.bedY-2*m.p.margin+.001&&p.dim[2]<=m.p.bedZ-2+.001);assert(Math.abs(bounds(p.output).min[2])<1e-5);fs.writeFileSync(`/tmp/petal5-validation/${c}-${p.id}.stl`,Buffer.from(binarySTL(p.output)));}
 const panels=m.parts.filter(x=>x.kind==='panel'),angle=2*Math.PI/m.layout.n;
 // Final petal insertion with fixed neighbors, including all radial segments.
 for(const part of panels){const s=solid(part.mesh);for(const delta of [4,2,1,.8,.6,.4,.2,0]){const moving=s.transform(q=>[q[0]+delta,q[1],q[2]]);for(const sign of [-1,1])assert(moving.intersect(s.transform(rotate(sign*angle))).raw.volume()<.02,`radial insertion collision ${c} ${delta} ${sign}`);}}
 // Test actual phased neighboring rows and the outer segment's radial insertion.
 for(let j=1;j<m.layout.rows;j++){
 const A=panels.find(p=>p.row===j),B=panels.find(p=>p.row===j-1),phase=m.ringPhases[j-1]-m.ringPhases[j];
 for(const delta of [4,2,1,.4,0])for(const k of [-1,0,1])assert(solid(A.mesh).transform(q=>[q[0]+delta,q[1],q[2]]).intersect(solid(B.mesh).transform(rotate(phase+k*angle))).raw.volume()<.02,'phased cross-ring mating / insertion');
 if(m.p.staggerRings)assert(Math.abs(Math.abs(phase)-angle/2)<1e-9,'half-petal stagger');
 }
 const hub=solid(m.parts.find(x=>x.kind==='hub').mesh);for(const p of panels)assert(hub.intersect(solid(p.mesh)).raw.volume()<.02,'hub/petal collision');
 const bottom=rootBottom(m.p),root=solid(panels.find(x=>x.row===0).mesh);
 if(m.p.rootThrough){const seat=zAt(52.5-ROOT.seatR,m.p)-ROOT.seatDepth;assert(root.intersect(cylinder([52.5,0,bottom-1],[52.5,0,zAt(70,m.p)],2.2)).raw.volume()<.001,'root through bore');assert(root.intersect(cylinder([52.5,0,seat+.05],[52.5,0,zAt(70,m.p)],ROOT.seatR-.05)).raw.volume()<.001,'root front seat open');assert(root.intersect(cylinder([52.5,0,seat-1],[52.5,0,seat-.05],ROOT.seatR-.05)).raw.volume()>40,'root seat floor');}
 else{assert(root.intersect(cylinder([52.5,0,bottom-.1],[52.5,0,bottom+6.9],m.p.insertDiameter/2-.02)).raw.volume()<.001,'root blind pilot');assert(root.intersect(cylinder([52.5,0,bottom+7.2],[52.5,0,bottom+8],1)).raw.volume()>1,'root pilot roof');}
 // Seam bolts: open bore through both flanges and a flat, open seat on each side for a washer and loose nut.
 // Snap clips: each installed clip (seats 10 mm apart) clears its petal and every other assembled part; pulled 1 mm it bites the groove.
 if(m.p.seamJoint){const clip=m.parts.find(x=>x.kind==='clip');assert(clip&&clip.qty>=hardwareSchedule(m)[0].quantity&&clipStrain(m.p)>0,'clip part');
  let others=null;for(const ins of m.instances){if(ins.part.kind==='clip')continue;const x=solid(ins.part.mesh).transform(rotate(ins.a));others=others?others.union(x):x;}
  for(const part of panels){const body=solid(part.mesh),a=m.instances.find(i=>i.part===part).a;for(const f of part.spec.flanges){for(const i of f.stations.keys()){const F=stationFrame(f,i),at=dy=>clipSolid(-.01,m.p.clipDetent,(X,Y,U)=>F(X,Y+dy,U));
   assert(body.intersect(at(0)).raw.volume()<.01,'installed clip fit '+c);assert(body.intersect(at(-1)).raw.volume()>.3,'clip detent '+c);assert(others.intersect(at(0).transform(rotate(a))).raw.volume()<.05,'clip clears the assembled dish '+c);}}}}
 if(m.p.seamJoint!==1)for(const part of panels){const body=solid(part.mesh);for(const f of part.spec.flanges){for(const i of f.stations.keys()){const F=stationFrame(f,i),y=boltY(m.p),pt=(t,r)=>F(t,y,0);assert(body.intersect(cylinder(pt(-.5),pt(5.5),1.5,24)).raw.volume()<.001,'seam bore');const wr=m.p.seamJoint===2?3:3.5;assert(body.intersect(cylinder(pt(5.05),pt(5.55),wr,32)).raw.volume()+body.intersect(cylinder(pt(5.55),pt(8.2),3.2,32)).raw.volume()<.5,'seam washer and nut clear '+c);assert(body.intersect(cylinder(pt(4),pt(4.95),wr+.1,32)).raw.volume()>(m.p.seamJoint===2?15:25),'flat seam seat '+c);}}}
 const face=hubFace(m.p),hb=hub.raw.boundingBox();if(m.p.hubFlat){assert(Math.abs(hb.max[2]-face)<1e-6,'flat hub front');assert(hub.intersect(cylinder([0,0,face-.5],[0,0,face+1],44,96).subtract(cylinder([0,0,face-2],[0,0,face+2],37,96))).raw.volume()>.5*Math.PI*(44*44-37*37)*.99,'hub front is flat');}else assert(hb.max[2]>face+.5&&hub.intersect(cylinder([0,0,zAt(20,m.p)+.05],[0,0,face+5],19,96).subtract(cylinder([0,0,0],[0,0,face+6],16,96))).raw.volume()<.5,'curved hub front follows the dish');for(let i=0;i<4;i++){const a=Math.PI/4+i*Math.PI/2,x=30*Math.cos(a),y=30*Math.sin(a);assert(hub.intersect(cylinder([x,y,bottom-7],[x,y,m.p.diameter],2.25)).raw.volume()<(m.p.mountThrough?.001:Infinity),'mount bore through');if(m.p.mountThrough)assert(hub.intersect(cylinder([x,y,hubSeatFloor(m.p)+.05],[x,y,face+5],4.95,48)).raw.volume()<.001,'mount front seat');if(!m.p.mountThrough)assert(hub.intersect(cylinder([x,y,bottom+1.2],[x,y,bottom+2],1)).raw.volume()>1,'blind mount roof');}
 assert(hub.intersect(cylinder([0,0,-50],[0,0,100],14.9)).raw.volume()<.001,'clear center');
 });
 assert.equal(m.plates.flatMap(p=>p.placements).length,m.parts.reduce((s,p)=>s+p.qty,0));for(const plate of m.plates){const boxes=plate.placements.map(p=>{const[x,y,w,h]=p.bounds;assert(x>=-1e-6&&y>=-1e-6&&x+w<=m.p.bedX-2*m.p.margin+1e-5&&y+h<=m.p.bedY-2*m.p.margin+1e-5);return[x,y,w,h];});for(let i=0;i<boxes.length;i++)for(let j=0;j<i;j++){const[a,b,w,h]=boxes[i],[x,y,W,H]=boxes[j];assert(a>=x+W+5.99||x>=a+w+5.99||b>=y+H+5.99||y>=b+h+5.99,'packing clearance');}}
 const coupon=connectionCoupon(m)[0];checkMesh(coupon.mesh,'coupon');assert.equal(coupon.qty,2);assert.equal(manifest(m).interface_revision,12);if(!m.p.seamJoint)assert.equal(hardwareSchedule(m)[0].quantity,m.layout.n*(2*m.layout.rows+(m.p.staggerRings?4:2)*(m.layout.rows-1)));
 console.log('PASS structure, closed solids, bed fit, mating, insertion, flat seats, root/hub fasteners, coupons and packing',cfg,m.layout);
}
const p={...defaults,rearStyle:1};for(let x=45;x<200;x+=3.1)for(let y=-80;y<80;y+=4.3){const ideal=(x*x+y*y)/(4*p.diameter*p.fd)-p.thickness,d=ideal-backZ(x,y,p);assert(d>=-1e-9&&d<=p.facetSize**2/(8*p.diameter*p.fd)+1e-9);}
const m=build(defaults);assert.equal(m.plates.length,3,'two three-petal beds plus hub');fs.writeFileSync('/tmp/petal5-validation/default-kit.zip',Buffer.from(await kit(m).arrayBuffer()));
console.log('PASS tangent facet thickness bound and complete kit export');
