import assert from 'node:assert/strict';
import fs from 'node:fs';
import {unzip} from '../dist/zip.js';
import {build,defaults,volume,bounds,backZ,connectionCoupon,binarySTL,rootBottom,hubFace,hubSeatFloor,rootSeatFloor,rootHole,hubEdge,ROOT,clipSolid,clipStrain,CLIP,stationFrame,boltY,hardwareSchedule,usesClips,usesLevers,seamBolt} from '../dist/geometry.js';
import {apply} from '../dist/scene.js';
import {zAt} from '../dist/mesh.js';
import {solid,solidScope,cylinder} from '../dist/solid.js';
import {kit,manifest} from '../dist/exports.js';
const rotate=a=>q=>[Math.cos(a)*q[0]-Math.sin(a)*q[1],Math.sin(a)*q[0]+Math.cos(a)*q[1],q[2]];
function checkMesh(mesh,name){const edges=new Map();for(const f of mesh.f)for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],key=[Math.min(a,b),Math.max(a,b)].join(':'),e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}assert([...edges.values()].every(([n,w])=>n===2&&w===0),name+' manifold topology');assert(volume(mesh)>0,name+' positive volume');}
fs.mkdirSync('/tmp/petal5-validation',{recursive:true});
// Packing clearance, checked independently of the packer's grid footprints: slice each pair of parts on a plate every
// 1 mm up their shared height, grow each section by just under half the 6 mm gap and require that they never meet.
function plateClearance(m){const half=(m.p.plateGap??6)/2-.05;solidScope(()=>{for(const[pi,plate]of m.plates.entries()){
 const placed=plate.placements.map(x=>{const c=Math.cos(x.yaw*Math.PI/180),s=Math.sin(x.yaw*Math.PI/180),out=x.part.output,body=solid({v:out.v.map(([X,Y,Z])=>[c*X-s*Y+x.x,s*X+c*Y+x.y,Z]),f:out.f});return{copy:x.copy,body,box:body.raw.boundingBox()};});
 for(let i=0;i<placed.length;i++)for(let j=0;j<i;j++){const A=placed[i],B=placed[j];if([0,1].some(k=>A.box.min[k]>B.box.max[k]+2*half+1||B.box.min[k]>A.box.max[k]+2*half+1))continue;
  for(let z=.5;z<Math.min(A.box.max[2],B.box.max[2]);z+=1){const a=A.body.raw.slice(z),b=B.body.raw.slice(z),ga=a.offset(half,'Round',2,32),gb=b.offset(half,'Round',2,32),both=ga.intersect(gb),area=both.area();for(const x of [a,b,ga,gb,both])x.delete();
   assert(area<1e-6,`packing clearance: ${A.copy} and ${B.copy} closer than ${2*half+.1} mm at z = ${z} on plate ${pi+1}`);}}}});}
const cases=[{}, {rimBand:0},{rimBand:2,ribs:1},{rimBand:3,ribs:1,seamJoint:1},{rimBand:2,ribs:1,seamJoint:3,diameter:600,bedX:300,bedY:300,bedZ:300},{rimBand:3,ribs:1,diameter:600,sectors:8,rows:2},{ribs:3,rimLip:3,seamJoint:1}, {rearStyle:1},{diameter:600,bedX:300,bedY:300,bedZ:330,plateGap:16},{diameter:260},{diameter:600},{diameter:800,bedX:300,bedY:300,bedZ:300},{diameter:400,sectors:8,rows:2},{fd:.25},{fd:.8},{thickness:1.6},{thickness:6},{bedZ:120},{rearStyle:1,facetSize:30,resolution:10},{mountThrough:0},{rootThrough:1,hubFlat:0},{rootThrough:1,rootSeat:1,mountSeat:1,rootBolt:5},{rootThrough:1,rootSeat:1,mountSeat:1,hubFlat:0},{hubFloat:.3,rootThrough:1},{seamJoint:1},{seamJoint:1,clipDetent:2,clipAllowableStrain:7},{seamJoint:2,diameter:600},{seamJoint:1,diameter:800,rows:3,sectors:8,bedX:300,bedY:300,bedZ:300,feedMode:1,feedLegs:4},{diameter:600,staggerRings:0},{seamBolt:4},{seamJoint:2,seamBolt:4,diameter:600},{seamJoint:3},{seamJoint:3,seamBolt:4,diameter:600,rows:2,bedX:300,bedY:300,bedZ:300},{diameter:800,rows:3,sectors:8,bedX:300,bedY:300,bedZ:300}];
cases.push({fd:.25,seamBolt:4},{diameter:260,fd:.25,seamBolt:4},{diameter:600,fd:.8,seamBolt:4,bedX:300,bedY:300,bedZ:300},{diameter:1200,fd:.25,bedX:400,bedY:400,bedZ:400},{diameter:1200,fd:.8,bedX:400,bedY:400,bedZ:400});
// A rim band that closes into a tube (triangle, or a U on a deep dish) encloses a void between seam flanges: decompose
// lists it with negative volume, and only solid pieces count.
for(const[c,cfg]of cases.entries()){
 const m=build({...defaults,...cfg});
 solidScope(()=>{
 for(const p of m.parts){checkMesh(p.mesh,p.id);checkMesh(p.output,p.id+' print');const raw=solid(p.mesh).raw,components=raw.decompose();assert.equal(components.filter(x=>x.volume()>0).length,1,p.id+' single connected solid');components.forEach(x=>x.delete());assert(p.dim[0]<=m.p.bedX-2*m.p.margin+.001&&p.dim[1]<=m.p.bedY-2*m.p.margin+.001&&p.dim[2]<=m.p.bedZ-2+.001);assert(Math.abs(bounds(p.output).min[2])<1e-5);fs.writeFileSync(`/tmp/petal5-validation/${c}-${p.id}.stl`,Buffer.from(binarySTL(p.output)));}
 const panels=m.parts.filter(x=>x.kind==='panel'),angle=2*Math.PI/m.layout.n;
 if(m.p.seamJoint===0)for(const part of panels){
  // Bearing lands must join the underside and remain inside the curved lower
  // flange edge. Check the real exported vertices, not the pad construction.
  for(const f of part.spec.flanges){const{o,e,v}=f.frame;for(const q of part.mesh.v){
   const ss=(q[0]-o[0])*e[0]+(q[1]-o[1])*e[1],t=(q[0]-o[0])*v[0]+(q[1]-o[1])*v[1];
   if(t>3.25&&t<5.01&&f.stations.some(s=>Math.abs(ss-s)<seamBolt(m.p).padW+2.1))assert(q[2]>=backZ(q[0],q[1],m.p)-14-.2,'bearing land protrudes past curved flange floor');
  }}
  const chordTolerance=.03+m.p.resolution**2/(2*m.focal);
  // only the rim band's front lip (outer 3 mm of an outer petal, with 0.5 mm for chords) stands above the reflecting face, by rimLip
  const lipR=m.p.rimBand&&part.spec.row===m.layout.rows-1?m.p.diameter/2-3.5:Infinity;
  for(const q of part.mesh.v)assert(q[2]<=(q[0]**2+q[1]**2)/(4*m.focal)+chordTolerance+(Math.hypot(q[0],q[1])>=lipR?m.p.rimLip:0),'bearing land protrudes through reflector face');
  const body=solid(part.mesh),SB=seamBolt(m.p),outer=m.p.seamBolt===4?4.5:3.5,inner=SB.holeR*Math.SQRT2+.1;
  for(const f of part.spec.flanges)for(const i of f.stations.keys()){
   const F=stationFrame(f,i),ring=cylinder(F(4.6,0,0),F(4.9,0,0),outer,48).subtract(cylinder(F(4.5,0,0),F(5,0,0),inner,48));
   assert(body.intersect(ring).raw.volume()>.95*Math.PI*(outer**2-inner**2)*.3,'washer bearing annulus remains supported');
  }
 }
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
 if(m.p.rootThrough){const seat=rootSeatFloor(m.p),seatR=rootHole(m.p).seatR;assert(root.intersect(cylinder([52.5,0,bottom-1],[52.5,0,zAt(70,m.p)],2.2)).raw.volume()<.001,'root through bore');assert(root.intersect(cylinder([52.5,0,seat+.05],[52.5,0,zAt(70,m.p)],seatR-.05)).raw.volume()<.001,'root front seat open');assert(root.intersect(cylinder([52.5,0,seat-1],[52.5,0,seat-.05],seatR-.05)).raw.volume()>40,'root seat floor');if(m.p.rootSeat)assert(Math.abs(seat-zAt(52.5-seatR,m.p))<1e-9,'plain root seat is only a spot face');}
 else{assert(root.intersect(cylinder([52.5,0,bottom-.1],[52.5,0,bottom+6.9],m.p.insertDiameter/2-.02)).raw.volume()<.001,'root blind pilot');assert(root.intersect(cylinder([52.5,0,bottom+7.2],[52.5,0,bottom+8],1)).raw.volume()>1,'root pilot roof');}
 // Seam bolts: open bore through both flanges and a flat, open seat on each side for a washer and loose nut.
 // Snap clips: each installed clip (seats 10 mm apart) clears its petal and every other assembled part; pulled 1 mm it bites the groove.
 if(usesClips(m.p)){const clip=m.parts.find(x=>x.kind==='clip');assert(clip&&clip.qty>=hardwareSchedule(m)[0].quantity&&clipStrain(m.p)>0,'clip part');
  let others=null;for(const ins of m.instances){if(ins.part.kind==='clip')continue;const x=solid(ins.part.mesh).transform(rotate(ins.a));others=others?others.union(x):x;}
  for(const part of panels){const body=solid(part.mesh),a=m.instances.find(i=>i.part===part).a;for(const f of part.spec.flanges){for(const i of f.stations.keys()){const F=stationFrame(f,i),at=dy=>clipSolid(-.01,m.p.clipDetent,(X,Y,U)=>F(X,Y+dy,U));
   assert(body.intersect(at(0)).raw.volume()<.01,'installed clip fit '+c);assert(body.intersect(at(-1)).raw.volume()>.3,'clip detent '+c);assert(others.intersect(at(0).transform(rotate(a))).raw.volume()<.05,'clip clears the assembled dish '+c);}}}}
 // Seam levers: every installed set clears the assembled dish; pushed 0.5 mm toward the wall the lever and keeper bear on the flanges.
 if(usesLevers(m.p)){const sets=m.parts.filter(x=>x.kind==='lever');assert.equal(sets.length,4,'four lever parts');let dish=null;for(const ins of m.instances.filter(i=>i.part.kind==='panel')){const x=solid(ins.part.mesh).transform(rotate(ins.a));dish=dish?dish.union(x):x;}
  for(const part of sets){const placed=m.instances.filter(i=>i.part===part);assert.equal(placed.length,part.installed);assert.equal(part.qty,part.installed+part.spares);assert.equal(part.installed+(m.boltStations||0),hardwareSchedule(m).find(h=>h.item==='seam lever set').quantity-part.spares+(m.boltStations||0));
   for(const ins of placed.slice(0,6)){const at=dx=>solid(part.mesh).transform(q=>rotate(ins.a)(apply(ins.matrix,[q[0]+dx,q[1],q[2]])));assert(at(0).intersect(dish).raw.volume()<.05,part.id+' installed clear '+c);if(part.id==='seam-lever-lever')assert(at(-.5).intersect(dish).raw.volume()>1,'lever bears on its flange '+c);if(part.id==='seam-lever-spring')assert(at(.5).intersect(dish).raw.volume()>1,'spring bears on the far flange '+c);}}
  for(const plate of m.plates)assert(new Set(plate.placements.map(x=>Boolean(x.part.flex))).size<=1,'TPU springs on their own plates');}
 if(m.p.seamJoint!==1)for(const part of panels){const body=solid(part.mesh);for(const f of part.spec.flanges){for(const i of f.stations.keys()){const F=stationFrame(f,i),y=boltY(m.p),pt=(t,r)=>F(t,y,0),SB=seamBolt(m.p),big=m.p.seamBolt===4;assert(body.intersect(cylinder(pt(-.5),pt(5.5),SB.holeR-.2,24)).raw.volume()<.001,'seam bore');const wr=m.p.seamJoint>=2?(big?4:3):(big?4.5:3.5);assert(body.intersect(cylinder(pt(5.05),pt(5.55),wr,32)).raw.volume()+body.intersect(cylinder(pt(5.55),pt(8.2),big?4.05:3.2,32)).raw.volume()<.5,'seam washer and nut clear '+c);assert(body.intersect(cylinder(pt(4),pt(4.95),wr+.1,32)).raw.volume()>(m.p.seamJoint>=2?15:25),'flat seam seat '+c);}}}
 const face=hubFace(m.p,m.layout.n),hb=hub.raw.boundingBox();if(m.p.hubFlat){assert(Math.abs(hb.max[2]-face)<1e-6,'flat hub front');assert(Math.abs(face-zAt(45/Math.cos(Math.PI/m.layout.n),m.p))<1e-9,'flat hub level with the highest petal edge');assert(hub.intersect(cylinder([0,0,face-.5],[0,0,face+1],40,96).subtract(cylinder([0,0,face-2],[0,0,face+2],36,96))).raw.volume()>.5*Math.PI*(40*40-36*36)*.99,'hub front is flat outside the mount seats');}else assert(hb.max[2]>face+.02&&hub.intersect(cylinder([0,0,zAt(20,m.p)+.05],[0,0,face+5],19,96).subtract(cylinder([0,0,0],[0,0,face+6],16,96))).raw.volume()<.5,'curved hub front follows the dish');for(let i=0;i<4;i++){const a=Math.PI/4+i*Math.PI/2,x=30*Math.cos(a),y=30*Math.sin(a);assert(hub.intersect(cylinder([x,y,bottom-7],[x,y,m.p.diameter],2.25)).raw.volume()<(m.p.mountThrough?.001:Infinity),'mount bore through');if(m.p.mountThrough)assert(hub.intersect(cylinder([x,y,hubSeatFloor(m.p,m.layout.n)+.05],[x,y,face+5],4.95,48)).raw.volume()<.001,'mount front seat');if(!m.p.mountThrough)assert(hub.intersect(cylinder([x,y,bottom+1.2],[x,y,bottom+2],1)).raw.volume()>1,'blind mount roof');}
 assert(hub.intersect(cylinder([0,0,-50],[0,0,100],14.9)).raw.volume()<.001,'clear center');
 // Hub edges stop at the hub edge line (petal edge less half the gap and any hub clearance); the hub's root holes grow by the clearance.
 {const e=hubEdge(m.p),n=m.layout.n,top=rootBottom(m.p);for(let i=0;i<n;i++){const a=i*2*Math.PI/n,u=[Math.cos(a),Math.sin(a)],t=[-u[1],u[0]],at=(r,s,z)=>[r*u[0]+s*t[0],r*u[1]+s*t[1],z];
  assert(hub.intersect(cylinder(at(e+.02,-5,top+.05),at(e+.02,5,top+.05),.01,8)).raw.volume()<1e-6&&hub.intersect(cylinder(at(e-.05,-5,top+.5),at(e-.05,5,top+.5),.01,8)).raw.volume()>0,'hub edge at the clearance line');
  const x=52.5*u[0],y=52.5*u[1];assert(hub.intersect(cylinder([x,y,top-7],[x,y,top+1],rootHole(m.p).hubR+(m.p.hubFloat||0)-.05,32)).raw.volume()<.001,'hub root hole with clearance');}}
 });
 assert.equal(m.plates.flatMap(p=>p.placements).length,m.parts.reduce((s,p)=>s+p.qty,0));for(const plate of m.plates){const boxes=plate.placements.map(p=>{const[x,y,w,h]=p.bounds;assert(x>=-1e-6&&y>=-1e-6&&x+w<=m.p.bedX-2*m.p.margin+1e-5&&y+h<=m.p.bedY-2*m.p.margin+1e-5);return[x,y,w,h];});}plateClearance(m);
 const coupon=connectionCoupon(m)[0];checkMesh(coupon.mesh,'coupon');assert.equal(coupon.qty,2);assert.equal(manifest(m).interface_revision,12);if(!m.p.seamJoint)assert.equal(hardwareSchedule(m)[0].quantity,m.parts.filter(part=>part.kind==='panel').reduce((sum,part)=>sum+part.qty*part.spec.flanges.reduce((n,f)=>n+f.stations.length,0),0)/2);
 console.log('PASS structure, closed solids, bed fit, mating, insertion, flat seats, root/hub fasteners, coupons and packing',cfg,m.layout);
}
const p={...defaults,rearStyle:1};for(let x=45;x<200;x+=3.1)for(let y=-80;y<80;y+=4.3){const ideal=(x*x+y*y)/(4*p.diameter*p.fd)-p.thickness,d=ideal-backZ(x,y,p);assert(d>=-1e-9&&d<=p.facetSize**2/(8*p.diameter*p.fd)+1e-9);}
const m=build(defaults);assert.equal(m.plates.length,2,'six nested petals and the hub on two beds');fs.writeFileSync('/tmp/petal5-validation/default-kit.zip',Buffer.from(await kit(m).arrayBuffer()));
console.log('PASS tangent facet thickness bound and complete kit export');
{const m=build({seamJoint:3,seamBolt:4}),man=manifest(m),files=await unzip(kit(m)),text=Object.keys(files).join('\n')+new TextDecoder().decode(files['ASSEMBLY.md']);
 assert.equal(man.seam_bolt,'M4');assert.equal(man.seam_levers.hole_mm,4.5);assert(man.parts.some(x=>x.file.startsWith('seam-lever-spring')&&x.material==='TPU 95A'));
 for(const name of ['lever','bar','keeper','spring'])assert(text.includes(`seam-lever-${name}_qty-${m.parts.find(p=>p.id==='seam-lever-'+name).qty}.stl`),'kit has the lever '+name);
 assert(text.includes('seam lever'),'guide covers seam levers');console.log('PASS seam lever kit, manifest and M4 bolt size');}
