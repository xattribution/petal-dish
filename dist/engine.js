// Geometry engine. Runs in a Web Worker so model generation and kit export never freeze the page; app.js falls back
// to running this same code on the page when workers are unavailable (it then supplies __PETAL_ENGINE_PORT__).
// Messages in:  {id, op:'build', key, params} | {id, op:'kit'|'pdf'|'scad'|'manifest', key, plates}
// Messages out: {op:'ready'} once the WASM kernel is loaded, then {id, ok:true, result} or {id, ok:false, error}.
import {build} from './geometry.js';
import {manualPlates} from './mesh.js';
import {kit,assemblyPDF,scadSource,manifest} from './exports.js';
const port=globalThis.__PETAL_ENGINE_PORT__||self;
let current=null;
// Functions cannot cross to the page; the only one in a model is the Cassegrain secondary's surface profile.
const forPage=m=>m.feed?.secondary?{...m,feed:{...m.feed,secondary:Object.fromEntries(Object.entries(m.feed.secondary).filter(([,v])=>typeof v!=='function'))}}:m;
// Exports use the model the page is showing: the latest build, with the page's manual plate arrangement if any.
function exported(key,plates){
 if(!current||current.key!==key)throw Error('The design changed while exporting. Try again.');
 const m=current.model;
 return plates?{...m,plates:manualPlates(m.parts.filter(p=>p.printIncluded!==false),m.p,plates),manualPacking:true}:m;
}
port.onmessage=({data:msg})=>{
 const {id,op}=msg;
 try{
  if(op==='build'){current=null;const model=build(msg.params);current={key:msg.key,model};port.postMessage({id,ok:true,result:forPage(model)});return;}
  const m=exported(msg.key,msg.plates);
  if(op==='kit')port.postMessage({id,ok:true,result:kit(m)});
  else if(op==='pdf'){const data=assemblyPDF(m);port.postMessage({id,ok:true,result:data},[data]);}
  else if(op==='scad')port.postMessage({id,ok:true,result:scadSource(m)});
  else if(op==='manifest')port.postMessage({id,ok:true,result:manifest(m)});
  else throw Error('Unknown engine request '+op);
 }catch(e){port.postMessage({id,ok:false,error:e?.message||String(e)});}
};
port.postMessage({op:'ready'});
