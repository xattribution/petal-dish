import assert from 'node:assert/strict';
import {build,defaults,jointStations,radialStations} from '../dist/geometry.js';
const key=(r,a)=>[r*Math.cos(a),r*Math.sin(a)].map(x=>x.toFixed(5)).join(',');
for(const cfg of [{diameter:800},{diameter:800,rearStyle:1},{diameter:600,rows:3,connectorSpacing:80},{diameter:800,staggerRings:0},{diameter:800,staggerRings:0,adaptiveJoints:0}]){
 const m=build({...defaults,...cfg}),n=m.layout.n,rows=m.layout.rows,holes=new Map(),graph=new Map();
 for(const [id,inst]of m.instances.entries())if(inst.part.kind==='panel')for(const[r,a]of inst.part.spec.centers){const k=key(r,a+inst.a);assert(!holes.has(k),'Panel holes must be distinct');holes.set(k,{id,row:inst.part.row});}
 for(const inst of m.instances.filter(i=>i.part.kind==='bridge'||i.part.id==='hub-rear')){
  const touched=new Map();for(const[r,a]of inst.part.spec.centers){const match=holes.get(key(r,a+inst.a));assert(match,`${inst.part.id}: bolt has no matching panel bore`);touched.set(match.id,{...match,count:(touched.get(match.id)?.count||0)+1});}
  if(inst.part.kind==='bridge'){assert.equal(touched.size,inst.part.id.startsWith('ring-')&&m.p.staggerRings?3:2,'Correct number of joined panels');assert.equal(inst.part.spec.holes.length,inst.part.id.startsWith('side-')?2:4,'Side joints use two bolts; ring joints use four');}
  if(inst.part.id.startsWith('ring-bridge')){
   const inner=[...touched.values()].filter(x=>x.row===inst.part.row),outer=[...touched.values()].filter(x=>x.row===inst.part.row+1);
   assert.equal(inner.length,m.p.staggerRings?2:1);assert.equal(outer.length,1);
   if(m.p.staggerRings){assert(inner.every(x=>x.count===1));assert.equal(outer[0].count,2);assert(Math.abs(Math.sin(n*(inst.a-m.ringPhases[inst.part.row]-Math.PI/n)/2))<1e-8);}
   for(const a of inner){const k=a.id+':'+outer[0].row;if(!graph.has(k))graph.set(k,new Set());graph.get(k).add(outer[0].id);}
  }
 }
 if(m.p.staggerRings){assert.equal(m.instances.filter(i=>i.part.id.startsWith("ring-bridge")).length,n*(rows-1));for(const neighbors of graph.values())assert.equal(neighbors.size,2,'Staggered panel must connect to both outer neighbors');for(let j=1;j<rows;j++)assert(Math.abs(m.ringPhases[j]-m.ringPhases[j-1])>1e-8);}else assert(m.ringPhases.every(x=>x===0));
 for(let j=0;j<rows;j++){const stations=jointStations(m.p,n,rows,j);assert.equal(m.parts.filter(p=>p.id.startsWith(`side-bridge-${j+1}`)).length,stations.count);}
 assert.equal(m.parts.reduce((s,p)=>s+p.qty,0),m.instances.length);
 console.log('PASS ring phases, bore mapping and connection graph',cfg,'bolts',m.bolts);
}
const cfg={...defaults,diameter:1000,rows:2,sectors:8,bedX:500,bedY:500,bedZ:500},sparse=build({...cfg,connectorSpacing:180}),dense=build({...cfg,connectorSpacing:80});assert(dense.bolts>sparse.bolts);assert(radialStations(dense.p,8,2,0).length>=radialStations(sparse.p,8,2,0).length);console.log('PASS connector density increases hardware count',sparse.bolts,'→',dense.bolts);
