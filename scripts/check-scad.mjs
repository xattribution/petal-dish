// Optional integration check: requires OpenSCAD on PATH.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {build,defaults,volume,bounds} from '../dist/geometry.js';
import {scadSource} from '../dist/exports.js';
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'petal-scad-'));
const kernel='';
function readSTL(file){
 const b=fs.readFileSync(file),count=b.readUInt32LE(80),v=[],f=[],ids=new Map();
 assert.equal(b.length,84+50*count,'Binary STL byte count');
 for(let i=0;i<count;i++){
  const face=[];
  for(let j=0;j<3;j++){
   const p=[0,1,2].map(k=>b.readFloatLE(84+50*i+12+12*j+4*k)),key=p.map(x=>x.toFixed(5)).join(',');
   if(!ids.has(key)){ids.set(key,v.length);v.push(p);}face.push(ids.get(key));
  }
  if(new Set(face).size===3)f.push(face);
 }
 return {v,f};
}
try{
 for(const [cfg,id,part] of [[{},'panel-1','panel-1'],[{},'side-bridge-1','side-bridge-1'],[{diameter:600,rows:3,rearStyle:1,connectorSpacing:80},'side-bridge-1','side-bridge-1'],[{diameter:800},'ring-bridge-1','ring-bridge-1'],[{perforate:1},'panel-1','panel-1'],[{fastenerStyle:2},'panel-1','panel-1'],[{fastenerStyle:1},'hub-clamp','hub-clamp']]){
  const m=build({...defaults,...cfg}),expected=m.parts.find(p=>p.id===id).output;
  const input=path.join(dir,'check.scad'),output=path.join(dir,'check.stl');
  fs.writeFileSync(input,scadSource(m,kernel).replace('part = "assembly"',`part = "${part}"`));
  const run=spawnSync('openscad',['--export-format','binstl','-o',output,input],{encoding:'utf8',timeout:300000});
  assert.equal(run.status,0,run.error?.message||run.stderr);
  assert(!/ERROR:|WARNING:/.test(run.stderr),run.stderr);
  const actual=readSTL(output),dv=Math.abs(volume(actual)/volume(expected)-1),a=bounds(actual).size,b=bounds(expected).size;
  assert(dv<.002,`Volume mismatch ${dv}`);
  assert(a.every((x,i)=>Math.abs(x-b[i])<.05),`Bounds mismatch ${a} vs ${b}`);
  console.log('PASS OpenSCAD render / JS volume and bounds',cfg,id,'relative volume error',dv);
 }
}finally{fs.rmSync(dir,{recursive:true,force:true});}
