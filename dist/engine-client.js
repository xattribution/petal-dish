// Page side of the geometry engine (engine.js). The bundled engine source arrives as a string
// (globalThis.PETAL_ENGINE_SOURCE) and runs in a Web Worker from a Blob URL, which also works from file:// for the
// offline app. Without workers (or if the worker cannot start) the same source runs on the page instead.
export function startEngine(source,onFatal){
 const pending=new Map();let seq=0,inPage=false,ready,started=false;
 let markReady,failReady;const readyPromise=new Promise((resolve,reject)=>{markReady=resolve;failReady=reject;});
 const fail=message=>{const error=Error(message);failReady(error);for(const p of pending.values())p.reject(error);pending.clear();onFatal?.(error);};
 const receive=msg=>{
  if(msg?.op==='ready'){ready=true;markReady();return;}
  if(msg?.op==='fatal'){fail('Could not initialize geometry: '+msg.error);return;}
  const p=pending.get(msg?.id);if(!p)return;pending.delete(msg.id);
  // On the page, give the caller its own copy so page-side edits never reach the engine's cached model.
  if(!msg.ok){p.reject(Error(msg.error));return;}
  try{p.resolve(inPage&&p.op==='build'?structuredClone(msg.result):msg.result);}catch(e){p.reject(e);}
 };
 let send;
 const runInPage=()=>{
  if(started&&inPage)return;inPage=true;started=true;
  const port={onmessage:null,postMessage:msg=>receive(msg)};
  globalThis.__PETAL_ENGINE_PORT__=port;
  // Requests are handled on a later task, like a worker; replies are delivered immediately.
  send=msg=>setTimeout(()=>port.onmessage?.({data:msg}),0);
  try{(0,eval)(source);}catch(e){fail('Could not initialize geometry: '+e.message);}
 };
 try{
  if(typeof Worker!=='function'||typeof Blob!=='function')throw Error('no workers');
  const url=URL.createObjectURL(new Blob([source],{type:'text/javascript'})),worker=new Worker(url);started=true;
  worker.onmessage=e=>receive(e.data);
  worker.onerror=e=>{e.preventDefault?.();if(!ready){worker.terminate();runInPage();}else fail('Geometry engine stopped: '+(e.message||'unknown error'));};
  send=msg=>worker.postMessage(msg);
 }catch{runInPage();}
 return{
  get inPage(){return inPage;},
  call(op,payload={}){return readyPromise.then(()=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject,op});send({id,op,...payload});}));}
 };
}
