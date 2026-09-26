import assert from 'node:assert/strict';
import {defaults,panelWall,build,volume} from '../dist/geometry.js';

const p={...defaults},spec={r0:70,r1:200,a0:-Math.PI/6,a1:Math.PI/6};
const centers=[[125,0]];
assert.equal(panelWall(p,spec,70,0,centers),p.thickness);
assert.equal(panelWall(p,spec,125,0,centers),p.thickness);
assert(Math.abs(panelWall(p,spec,160,0,centers)-1.8)<1e-9);
assert.equal(panelWall({...p,thickness:1.6},spec,160,0,centers),1.6);
const model=build(p),panel=model.parts.find(part=>part.kind==='panel');
assert(panel && volume(panel.mesh)>0);
assert(panel.spec.centers.length>0);
console.log('PASS graded panel wall and keyed panel mesh');
