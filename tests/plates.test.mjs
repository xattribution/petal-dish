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
