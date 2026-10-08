// Geometry engine. Runs in a Web Worker so model generation and kit export never freeze the page; app.js falls back
// to running this same code on the page when workers are unavailable (it then supplies __PETAL_ENGINE_PORT__).
// Messages in:  {id, op:'build', key, params, plates} | {id, op:'kit'|'pdf'|'scad'|'manifest'|'plates', key, plates}
//               | {id, op:'pack', key} | {id, op:'move', key, plates, copy, target, x, y}
// plates: plate rows (plateRows), or null for the model's own packing. Rows sent with a build are a hand arrangement to
// keep if it still fits the new parts; 'plates' checks typed rows and returns them on the packing grid; 'move' trusts
// its rows, which are always ones the engine produced.
// Messages out: {op:'ready'} once the WASM kernel is loaded, then {id, ok:true, result} or {id, ok:false, error}.
import {build} from './geometry.js';
import {manualPlates,packAll,plateRows,movePlacement} from './mesh.js';
import {kit,assemblyPDF,scadSource,manifest} from './exports.js';
const port=globalThis.__PETAL_ENGINE_PORT__||self;
let current=null;
// Functions cannot cross to the page; the only one in a model is the Cassegrain secondary's surface profile.
const forPage=m=>m.feed?.secondary?{...m,feed:{...m.feed,secondary:Object.fromEntries(Object.entries(m.feed.secondary).filter(([,v])=>typeof v!=='function'))}}:m;
// Exports use the model the page is showing: the latest build, with the page's manual plate arrangement if any.
const printed=m=>m.parts.filter(p=>p.printIncluded!==false);
function exported(key,plates,trusted=false){
 if(!current||current.key!==key)throw Error('The design changed. Try again.');
 const m=current.model;
 return plates?{...m,plates:manualPlates(printed(m),m.p,plates,trusted),manualPacking:true}:m;
}
port.onmessage=({data:msg})=>{
 const {id,op}=msg;
 try{
  if(op==='build'){current=null;const model=build(msg.params);
   if(msg.plates&&model.p.packPlates){try{model.plates=manualPlates(printed(model),model.p,msg.plates);model.manualPacking=true;}catch{model.arrangementLost=true;}}
   current={key:msg.key,model};port.postMessage({id,ok:true,result:forPage(model)});return;}
  if(op==='plates'){port.postMessage({id,ok:true,result:plateRows(exported(msg.key,msg.plates).plates)});return;}
  // Plate arrangement runs here too, where the parts' footprints from packing are already cached.
  if(op==='pack'){const m=exported(msg.key,null);port.postMessage({id,ok:true,result:plateRows(packAll(m.parts,m.p))});return;}
  if(op==='move'){const m=exported(msg.key,msg.plates,true),next=movePlacement(m.plates,msg.copy,msg.target,msg.x,msg.y,m.p);port.postMessage({id,ok:true,result:next?plateRows(next):null});return;}
  const m=exported(msg.key,msg.plates);
  if(op==='kit')port.postMessage({id,ok:true,result:kit(m)});
  else if(op==='pdf'){const data=assemblyPDF(m);port.postMessage({id,ok:true,result:data},[data]);}
  else if(op==='scad')port.postMessage({id,ok:true,result:scadSource(m)});
  else if(op==='manifest')port.postMessage({id,ok:true,result:manifest(m)});
  else throw Error('Unknown engine request '+op);
 }catch(e){port.postMessage({id,ok:false,error:e?.message||String(e)});}
};
port.postMessage({op:'ready'});
