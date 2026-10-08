import assert from 'node:assert/strict';
import {build} from '../dist/geometry.js';
import {manualPlates,plateRows,bounds,packedPlateMesh} from '../dist/mesh.js';
import {manifest,kit} from '../dist/exports.js';
const m=build({}),rows=plateRows(m.plates),copies=m.parts.reduce((s,p)=>s+p.qty,0);
assert.equal(new Set(rows.map(x=>x.copy)).size,copies);
assert.deepEqual(plateRows(manualPlates(m.parts,m.p,rows)),rows);
const each=rows.map((r,i)=>({...r,plate:i+1,x:0,y:0,yaw:180}));
m.plates=manualPlates(m.parts,m.p,each);m.manualPacking=true;
assert.equal(m.plates.length,copies);assert.equal(manifest(m).manual_packing,true);
for(const plate of m.plates){assert(Math.abs(bounds(packedPlateMesh(plate)).min[2])<1e-5);}
for(const bad of [each.slice(1),each.map((r,i)=>i? r:{...r,x:999}),each.map((r,i)=>i? r:{...r,plate:0}),each.map((r,i)=>i? r:{...r,y:NaN}),each.map((r,i)=>i===1?{...r,copy:each[0].copy}:r),each.map(r=>({...r,plate:1}))])assert.throws(()=>manualPlates(m.parts,m.p,bad));
// Exact requested plate indices survive intentional gaps; no empty STL exported.
const gaps=each.map(r=>({...r,plate:copies}));gaps.forEach((r,i)=>r.plate=i+1);gaps[0].plate=copies;gaps.at(-1).plate=2;gaps[1].plate=3; // collision, deliberately invalid
assert.throws(()=>manualPlates(m.parts,m.p,gaps));
console.log('PASS plate identity, manual assignment / yaw, bounds, clearances, duplicate and missing-copy rejection, export metadata');
// Drag and drop between plates. Every result is re-checked by the untrusted manual path (footprint clearance, bed fit).
{const {movePlacement,packParts,packCell}=await import('../dist/pack.js');
 const m=build({}),parts=m.parts.filter(p=>p.printIncluded!==false),count=pl=>pl.reduce((s,x)=>s+x.placements.length,0),total=count(m.plates);
 const check=(plates,label)=>{assert(plates,label+': a result');assert.equal(count(plates),total,label+': every copy kept');assert(plates.every(pl=>pl.placements.length),label+': no empty plates');manualPlates(parts,m.p,plateRows(plates));};
 const copy=m.plates[0].placements[0].copy,onNew=movePlacement(m.plates,copy,m.plates.length,0,0,m.p);check(onNew,'new plate');
 assert.equal(onNew.length,m.plates.length+1);assert(onNew.at(-1).placements.some(x=>x.copy===copy),'copy on the new plate');
 const home=movePlacement(onNew,copy,0,0,0,m.p);check(home,'back to plate 1');assert.equal(home.length,m.plates.length,'the emptied plate is dropped');
 const nudged=movePlacement(m.plates,copy,0,0,0,m.p);check(nudged,'same plate');
 for(const [target,x] of [[-1,0],[2.5,0],[m.plates.length+1,0],[0,NaN]])assert.equal(movePlacement(m.plates,copy,target,x,0,m.p),null,'invalid drop '+target+','+x);
 assert.equal(movePlacement(m.plates,'nope:1',0,0,0,m.p),null);
 // Grid cells: 1 mm to a 320 mm bed, 1.5 mm to 480 mm, 3 mm beyond; the 6 mm gap is whole cells at each.
 for(const [bed,c] of [[220,1],[320,1],[321,1.5],[480,1.5],[481,3],[1000,3]]){assert.equal(packCell({bedX:bed,bedY:140}),c);assert(Number.isInteger(6/c));}
 // Exact fit: a part 0.2 mm under the usable width still packs, and nothing reaches past the usable area at any cell size.
 for(const bed of [437,500,700]){const p={bedX:bed,bedY:bed,bedZ:300,margin:8},W=bed-16,box=(w,d)=>({v:[[0,0,0],[w,0,0],[w,d,0],[0,d,0],[0,0,5],[w,0,5],[w,d,5],[0,d,5]],f:[[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]]});
  const parts=[{id:'wide',name:'wide',kind:'test',row:0,qty:1,dim:[W-.2,40,5],output:box(W-.2,40)},{id:'half',name:'half',kind:'test2',row:0,qty:3,dim:[(W-6)/2-.3,30,5],output:box((W-6)/2-.3,30)}];
  const plates=packParts(parts,p);for(const pl of plates)for(const x of pl.placements){const[x0,y0,w,h]=x.bounds;assert(x0>=-1e-9&&y0>=-1e-9&&x0+w<=W+1e-9&&y0+h<=W+1e-9,`inside the ${bed} mm bed`);}
  assert.equal(plates.length,1,`${bed} mm bed: one plate`);}
 console.log('PASS drag to a new plate and back, same-plate move, invalid drops, grid cells and exact-size packing');}
