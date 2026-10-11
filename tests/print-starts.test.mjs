// No floating starts in the side print: every island of every layer (0.3 mm first layer, 0.24 mm after) touches the
// layer below. The clip and lever guide ridges on the upper flange, whose wall faces down, used to start in mid-air.
import assert from 'node:assert/strict';
import {build,defaults} from '../dist/geometry.js';
import {solid,solidScope} from '../dist/solid.js';
const floating=mesh=>solidScope(()=>{const man=solid(mesh).raw,top=man.boundingBox().max[2],out=[];let prev=null;
 for(let z=.15;z<top;z=z<.3?.42:z+.24){const cs=man.slice(z);if(prev)for(const isl of cs.decompose()){const a=isl.area();if(a>1e-3&&isl.intersect(prev).area()<.02*a+1e-3)out.push({z:+z.toFixed(2),a:+a.toFixed(3)});}prev=cs;}return out;});
for(const c of [{diameter:600,sectors:8,seamJoint:1,bedX:325,bedY:320,bedZ:320},{diameter:600,sectors:12,seamJoint:2,bedX:300,bedY:300,bedZ:330},{seamJoint:3}]){
 const m=build({...defaults,...c});for(const p of m.parts.filter(p=>p.kind==='panel')){const f=floating(p.output);assert.equal(f.length,0,`${JSON.stringify(c)} ${p.id}: floating starts ${JSON.stringify(f.slice(0,5))}`);}
 console.log('PASS no floating starts',JSON.stringify(c),`${m.layout.n} petals`);}
