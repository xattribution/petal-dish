import assert from 'node:assert/strict';
import {build,defaults,jointStations,radialStations} from '../dist/geometry.js';
const key=(r,a)=>[r*Math.cos(a),r*Math.sin(a)].map(x=>x.toFixed(5)).join(',');
for(const cfg of [{diameter:800},{diameter:800,rearStyle:1},{diameter:600,rows:3,connectorSpacing:80},{diameter:800,staggerRings:0},{diameter:800,staggerRings:0,adaptiveJoints:0}]){
 const m=build({...defaults,...cfg}),n=m.layout.n,rows=m.layout.rows,holes=new Map(),graph=new Map();
 for(const [id,inst]of m.instances.entries())if(inst.part.kind==='panel')for(const[r,a]of inst.part.spec.centers){const k=key(r,a+inst.a);assert(!holes.has(k),'Panel holes must be distinct');holes.set(k,{id,row:inst.part.row});}
 for(const inst of m.instances.filter(i=>i.part.kind==='bridge'||i.part.id==='hub-rear')){
  const touched=new Map();for(const[r,a]of inst.part.spec.centers){const match=holes.get(key(r,a+inst.a));assert(match,`${inst.part.id}: locating tongue has no matching panel socket`);touched.set(match.id,match);}
  if(inst.part.kind==='bridge')assert.equal(touched.size,2,'Each saddle joins exactly two panels');
  if(inst.part.id.startsWith('ring-bridge')){const pair=[...touched.values()].sort((a,b)=>a.row-b.row);assert.equal(pair[1].row-pair[0].row,1);const k=pair[0].id+':'+pair[1].row;if(!graph.has(k))graph.set(k,new Set());graph.get(k).add(pair[1].id);}
 }
 if(m.p.staggerRings){for(const neighbors of graph.values())assert.equal(neighbors.size,2,'Staggered panel must connect to both outer neighbors');for(let j=1;j<rows;j++)assert(Math.abs(m.ringPhases[j]-m.ringPhases[j-1])>1e-8);}else assert(m.ringPhases.every(x=>x===0));
 for(let j=0;j<rows;j++){const stations=jointStations(m.p,n,rows,j);assert.equal(m.parts.filter(p=>p.id.startsWith(`side-bridge-${j+1}`)).length,stations.count);}
 assert.equal(m.parts.reduce((s,p)=>s+p.qty,0),m.instances.length);
 console.log('PASS ring phases, socket mapping and connection graph',cfg,'bolts',m.bolts);
}
const cfg={...defaults,diameter:1000,rows:2,sectors:8,bedX:500,bedY:500,bedZ:500},sparse=build({...cfg,connectorSpacing:180}),dense=build({...cfg,connectorSpacing:80});assert(dense.bolts>sparse.bolts);assert(radialStations(dense.p,8,2,0).length>=radialStations(sparse.p,8,2,0).length);console.log('PASS connector density increases hardware count',sparse.bolts,'→',dense.bolts);
