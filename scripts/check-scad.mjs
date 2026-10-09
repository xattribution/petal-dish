// Optional integration check: requires OpenSCAD on PATH.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {build,defaults,volume,bounds} from '../dist/geometry.js';
import {scadSource} from '../dist/exports.js';
import {mountSolid,mountMesh} from '../dist/mount-fasteners.js';
import {solid,solidScope} from '../dist/solid.js';
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
 for(const [cfg,id,part] of [[{},'petal-1','petal-1'],[{},'hub','hub'],[{rearStyle:1},'petal-1','petal-1'],[{diameter:600},'petal-2','petal-2'],[{feedMode:1},'petal-1-mount','petal-1-mount'],[{feedMode:2},'secondary-reflector','secondary-reflector'],[{feedMode:1},'feed-puck','feed-puck'],[{feedMode:2},'feed-puck','feed-puck'],[{feedMode:3},'feed-bowl','feed-bowl'],[{feedMode:3},'feed-insert-cup','feed-insert-cup'],[{mountMode:1,mountBase:2},'mount-base','mount-base'],[{rootThrough:1,rootBolt:5,mountBolt:3},'hub','hub'],[{rootBolt:3},'petal-1','petal-1'],[{mountMode:1,clampBolt:10,jointBolt:3},'mount-cheek','mount-cheek'],[{mountMode:1,mountBase:0,standBolt:6,jointBolt:5},'mount-yoke','mount-yoke']]){
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
 // cad/simple-mount.scad at non-default sizes against the app's cuts on the bundled blanks
 const sizes={jointBolt:5,clampBolt:10,standBolt:6,mountBolt:3},D={joint_m:5,clamp_m:10,stand_m:6,hub_m:3};
 for(const part of ['base','yoke','upright','cradle','cheek']){const output=path.join(dir,part+'.stl');
  const run=spawnSync('openscad',['--export-format','binstl','-o',output,'-D',`part="${part}"`,'-D','printing=true',...Object.entries(D).flatMap(([k,v])=>['-D',`${k}=${v}`]),'cad/simple-mount.scad'],{encoding:'utf8',timeout:300000});
  assert.equal(run.status,0,run.stderr);
  solidScope(()=>{const {body}=mountSolid(part,{...defaults,...sizes}),ref=solid(readSTL(output)),diff=body.subtract(ref).raw.volume()+ref.subtract(body).raw.volume();assert(diff<.5,`${part}: SCAD and app cuts differ by ${diff} mm³`);console.log('PASS simple-mount.scad sizes match the app cuts',part,D,'differ',diff.toFixed(3),'mm³');});}
 // plain holes: SCAD switches against the app's seat options
 for(const [variant,part,D,cfg] of [['cheek-arc','cheek',{arc_lock:'true',head_pockets:'false'},{headSeat:1}],['base','base',{nut_pocket:'false',clamp_m:10},{nutSeat:1,clampBolt:10}],['yoke-stand','yoke',{stand_holes:'true',stand_counterbore:'false',stand_m:6},{standSeat:1,standBolt:6}]]){const output=path.join(dir,variant+'-plain.stl');
  const run=spawnSync('openscad',['--export-format','binstl','-o',output,'-D',`part="${part}"`,'-D','printing=true',...Object.entries(D).flatMap(([k,v])=>['-D',`${k}=${v}`]),'cad/simple-mount.scad'],{encoding:'utf8',timeout:300000});
  assert.equal(run.status,0,run.stderr);
  solidScope(()=>{const {body}=mountSolid(variant,{...defaults,...cfg}),ref=solid(readSTL(output)),diff=body.subtract(ref).raw.volume()+ref.subtract(body).raw.volume();assert(diff<.5,`${variant}: SCAD and app plain holes differ by ${diff} mm³`);console.log('PASS simple-mount.scad plain holes match the app',variant,D,'differ',diff.toFixed(3),'mm³');});}
 // elevation clamp on both sides (a U), at non-default sizes: the dual yoke (with stand holes) and cradle, and a left arm
 for(const [variant,part,extra] of [['yoke-dual-stand','yoke',{stand_holes:'true'}],['cradle-dual','cradle',{}],['upright-left','upright_left',{}]]){const output=path.join(dir,variant+'.stl');
  const run=spawnSync('openscad',['--export-format','binstl','-o',output,'-D',`part="${part}"`,'-D','printing=true','-D','sides="both"',...Object.entries({...D,...extra}).flatMap(([k,v])=>['-D',`${k}=${v}`]),'cad/simple-mount.scad'],{encoding:'utf8',timeout:300000});
  assert.equal(run.status,0,run.stderr);
  solidScope(()=>{const cfg={...defaults,...sizes},body=variant.endsWith('-left')?solid(mountMesh(variant,cfg)):mountSolid(variant,cfg).body,ref=solid(readSTL(output)),diff=body.subtract(ref).raw.volume()+ref.subtract(body).raw.volume();assert(diff<.5,`${variant}: SCAD and app differ by ${diff} mm³`);console.log('PASS simple-mount.scad both sides match the app',variant,D,'differ',diff.toFixed(3),'mm³');});}
}finally{fs.rmSync(dir,{recursive:true,force:true});}
