import fs from 'node:fs';
import {solidScope,cylinder} from '../dist/solid.js';
import {build} from '../dist/geometry.js';
import {scenePoint,dishPoint} from '../dist/scene.js';
fs.mkdirSync('tmp/system-renders',{recursive:true});
const cases=[['assembly',{mountMode:1,feedMode:1,seamJoint:1,elevation:30},0],['rear',{mountMode:1,feedMode:1,seamJoint:1,elevation:30},0],['staggered',{diameter:600,rows:2,bedX:300,bedY:300,bedZ:300,mountMode:1,feedMode:1,feedLegs:4,seamJoint:2,elevation:20},0],['exploded',{mountMode:1,seamJoint:1,elevation:30},1]];
for(const[name,cfg,ex]of cases){const m=build(cfg),meshes=m.instances.map(i=>({id:i.part.id,kind:i.part.kind,v:i.part.mesh.v.map(q=>scenePoint(m,i,q,ex)),f:i.part.mesh.f}));if(m.feed&&!ex)solidScope(()=>{for(const l of m.feed.legs){const rod=cylinder(l.lower,l.upper,m.feed.rodDiameter/2,16).mesh();meshes.push({id:'metal-rod',kind:'rod',v:rod.v.map(q=>dishPoint(m,q)),f:rod.f});}});fs.writeFileSync('tmp/system-renders/'+name+'.json',JSON.stringify({p:m.p,mount:m.mount,meshes,rods:m.feed&&!ex?m.feed.legs.map(l=>[dishPoint(m,l.lower),dishPoint(m,l.upper)]):[]}));console.log('snapshot geometry',name);}
const m=build({seamJoint:1}),p=m.parts.find(p=>p.kind==='clip');fs.writeFileSync('tmp/system-renders/clip.json',JSON.stringify({p:m.p,meshes:[{id:p.id,kind:'clip',...p.output}],rods:[]}));
