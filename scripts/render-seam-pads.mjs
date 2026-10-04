// Produce actual-mesh closeups against a selected pre-cleanup commit.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {build} from '../dist/geometry.js';
import {solid,solidScope} from '../dist/solid.js';
const folder='tmp/seam-pads';fs.mkdirSync(folder,{recursive:true});
const baseline=folder+'/baseline';fs.cpSync('dist',baseline,{recursive:true});
fs.writeFileSync(baseline+'/geometry.js',execFileSync('git',['show',(process.argv[2]||'065c1275dbb004c288c78517e7c93478d21b539c')+':dist/geometry.js']));
const before=await import(pathToFileURL(path.resolve(baseline+'/geometry.js')));
const cases=[{fd:.42,seamBolt:3},{fd:.25,seamBolt:4}];
const scenes=[];
for(const cfg of cases)for(const [label,generate]of [['Before',before.build],['After',build]]){
 const m=generate({...cfg,bedX:300,bedY:300}),p=m.parts.find(p=>p.kind==='panel'),f=p.spec.flanges[1],{o,e,v}=f.frame;
 const lo=f.stations[0]-12,hi=f.stations.at(-1)+12,offset=f.levels[0];
 const mesh=solidScope(()=>solid(p.mesh).trim([e[0],e[1],0],lo+o[0]*e[0]+o[1]*e[1]).trim([-e[0],-e[1],0],-hi-o[0]*e[0]-o[1]*e[1]).trim([v[0],v[1],0],-.1+o[0]*v[0]+o[1]*v[1]).trim([-v[0],-v[1],0],-18-o[0]*v[0]-o[1]*v[1]).mesh());
 mesh.v=mesh.v.map(([x,y,z])=>[(x-o[0])*e[0]+(y-o[1])*e[1]-lo,(x-o[0])*v[0]+(y-o[1])*v[1],z-offset]);
 scenes.push({label,cfg,mesh});
}
fs.writeFileSync(folder+'/closeups.json',JSON.stringify(scenes));
