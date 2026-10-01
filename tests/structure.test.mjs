import assert from 'node:assert/strict';
import fs from 'node:fs';
import {build,defaults,volume,bounds,backZ,connectionCoupon,binarySTL,rootBottom,hubFace,ROOT,hardwareSchedule} from '../dist/geometry.js';
import {zAt} from '../dist/mesh.js';
import {solid,solidScope,cylinder} from '../dist/solid.js';
import {kit,manifest} from '../dist/exports.js';
const rotate=a=>q=>[Math.cos(a)*q[0]-Math.sin(a)*q[1],Math.sin(a)*q[0]+Math.cos(a)*q[1],q[2]];
function checkMesh(mesh,name){const edges=new Map();for(const f of mesh.f)for(let i=0;i<3;i++){const a=f[i],b=f[(i+1)%3],key=[Math.min(a,b),Math.max(a,b)].join(':'),e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}assert([...edges.values()].every(([n,w])=>n===2&&w===0),name+' manifold topology');assert(volume(mesh)>0,name+' positive volume');}
fs.mkdirSync('/tmp/petal5-validation',{recursive:true});
const cases=[{}, {rearStyle:1},{diameter:260},{diameter:600},{diameter:800,bedX:300,bedY:300,bedZ:300},{diameter:400,sectors:8,rows:2},{fd:.25},{fd:.8},{thickness:1.6,jointClearance:.1},{thickness:6},{bedZ:120},{rearStyle:1,facetSize:30,resolution:10},{mountThrough:0},{diameter:600,staggerRings:0},{diameter:800,rows:3,sectors:8,bedX:300,bedY:300,bedZ:300}];
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
 if(m.p.mountThrough){const seat=zAt(52.5-ROOT.seatR,m.p)-ROOT.seatDepth;assert(root.intersect(cylinder([52.5,0,bottom-1],[52.5,0,zAt(70,m.p)],2.2)).raw.volume()<.001,'root through bore');assert(root.intersect(cylinder([52.5,0,seat+.05],[52.5,0,zAt(70,m.p)],ROOT.seatR-.05)).raw.volume()<.001,'root front seat open');assert(root.intersect(cylinder([52.5,0,seat-1],[52.5,0,seat-.05],ROOT.seatR-.05)).raw.volume()>40,'root seat floor');}
 else{assert(root.intersect(cylinder([52.5,0,bottom-.1],[52.5,0,bottom+6.9],m.p.insertDiameter/2-.02)).raw.volume()<.001,'root blind pilot');assert(root.intersect(cylinder([52.5,0,bottom+7.2],[52.5,0,bottom+8],1)).raw.volume()>1,'root pilot roof');}
 // Seam bolts: open bore through both flanges and a flat, open seat on each side for a washer and loose nut.
 for(const part of panels){const body=solid(part.mesh);for(const f of part.spec.flanges){const{o,e,v}=f.frame,pt=(s,t,z)=>[o[0]+s*e[0]+t*v[0],o[1]+s*e[1]+t*v[1],z];for(const s of f.stations){const q=pt(s,0,0),z=backZ(q[0],q[1],m.p)-7;assert(body.intersect(cylinder(pt(s,-.5,z),pt(s,5.5,z),1.5,24)).raw.volume()<.001,'seam bore');assert(body.intersect(cylinder(pt(s,5.05,z),pt(s,5.55,z),3.5,32)).raw.volume()+body.intersect(cylinder(pt(s,5.55,z),pt(s,8.2,z),3.2,32)).raw.volume()<.5,'seam washer and nut clear '+c);assert(body.intersect(cylinder(pt(s,4,z),pt(s,4.95,z),4.9,32)).raw.volume()>50,'flat seam seat '+c);}}}
 const face=hubFace(m.p),hb=hub.raw.boundingBox();assert(Math.abs(hb.max[2]-face)<1e-6,'flat hub front');assert(hub.intersect(cylinder([0,0,face-.5],[0,0,face+1],44,96).subtract(cylinder([0,0,face-2],[0,0,face+2],37,96))).raw.volume()>.5*Math.PI*(44*44-37*37)*.99,'hub front is flat');for(let i=0;i<4;i++){const a=Math.PI/4+i*Math.PI/2,x=30*Math.cos(a),y=30*Math.sin(a);assert(hub.intersect(cylinder([x,y,bottom-7],[x,y,m.p.diameter],2.25)).raw.volume()<(m.p.mountThrough?.001:Infinity),'mount bore through');if(m.p.mountThrough)assert(hub.intersect(cylinder([x,y,face-4.95],[x,y,face+1],4.95,48)).raw.volume()<.001,'mount front seat');if(!m.p.mountThrough)assert(hub.intersect(cylinder([x,y,bottom+1.2],[x,y,bottom+2],1)).raw.volume()>1,'blind mount roof');}
 assert(hub.intersect(cylinder([0,0,-50],[0,0,100],14.9)).raw.volume()<.001,'clear center');
 });
 assert.equal(m.plates.flatMap(p=>p.placements).length,m.parts.reduce((s,p)=>s+p.qty,0));for(const plate of m.plates){const boxes=plate.placements.map(p=>{const[x,y,w,h]=p.bounds;assert(x>=-1e-6&&y>=-1e-6&&x+w<=m.p.bedX-2*m.p.margin+1e-5&&y+h<=m.p.bedY-2*m.p.margin+1e-5);return[x,y,w,h];});for(let i=0;i<boxes.length;i++)for(let j=0;j<i;j++){const[a,b,w,h]=boxes[i],[x,y,W,H]=boxes[j];assert(a>=x+W+5.99||x>=a+w+5.99||b>=y+H+5.99||y>=b+h+5.99,'packing clearance');}}
 const coupon=connectionCoupon(m)[0];checkMesh(coupon.mesh,'coupon');assert.equal(coupon.qty,2);assert.equal(manifest(m).interface_revision,12);assert.equal(hardwareSchedule(m)[0].quantity,m.layout.n*(2*m.layout.rows+(m.p.staggerRings?4:2)*(m.layout.rows-1)));
 console.log('PASS structure, closed solids, bed fit, mating, insertion, flat seats, root/hub fasteners, coupons and packing',cfg,m.layout);
}
const p={...defaults,rearStyle:1};for(let x=45;x<200;x+=3.1)for(let y=-80;y<80;y+=4.3){const ideal=(x*x+y*y)/(4*p.diameter*p.fd)-p.thickness,d=ideal-backZ(x,y,p);assert(d>=-1e-9&&d<=p.facetSize**2/(8*p.diameter*p.fd)+1e-9);}
const m=build(defaults);assert.equal(m.plates.length,3,'two three-petal beds plus hub');fs.writeFileSync('/tmp/petal5-validation/default-kit.zip',Buffer.from(await kit(m).arrayBuffer()));
console.log('PASS tangent facet thickness bound and complete kit export');
