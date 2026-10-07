export const CONNECTION_METHODS=['Bolts','Snap clips','Bolts + clips','Quick-release levers'];
export function jointKey(frame,start,end,angle){
 const {o,e}=frame,c=Math.cos(angle),s=Math.sin(angle),points=[start,end].map(t=>{const x=o[0]+t*e[0],y=o[1]+t*e[1];return [c*x-s*y,s*x+c*y].map(v=>Math.round(v*10)).join(',');}).sort();
 return 'seam:'+points.join('/');
}
export function seamChoice(p,frame,start,end,angle=0){
 const family=frame.o[0]===0&&frame.o[1]===0?'radial':'ring',id=jointKey(frame,start,end,angle),config=p.connections;
 return {family,id,...{seamJoint:p.seamJoint,seamBolt:p.seamBolt},...config?.families?.[family],...config?.joints?.[id]};
}
export const rootChoice=(p,index)=>p.connections?.roots?.[index]??p.rootThrough;
export const mountChoice=(p,index)=>p.connections?.mounts?.[index]??p.mountThrough;
export function validateConnections(config){
 if(config===undefined)return;
 if(!config||typeof config!=='object'||Array.isArray(config))throw Error('Invalid connection settings.');
 for(const key of Object.keys(config))if(!['families','joints','roots','mounts'].includes(key))throw Error('Unknown connection group.');
 for(const group of Object.values(config))if(!group||typeof group!=='object'||Array.isArray(group))throw Error('Invalid connection group.');
 for(const key of ['families','joints'])for(const [id,choice]of Object.entries(config[key]||{})){
  if(key==='families'&&!['radial','ring'].includes(id))throw Error('Unknown seam family.');
  if(!choice||typeof choice!=='object'||Object.keys(choice).some(k=>!['seamJoint','seamBolt'].includes(k)))throw Error('Invalid seam choice.');
  if(choice.seamJoint!==undefined&&![0,1,2,3].includes(choice.seamJoint))throw Error('Choose an available seam method.');
  if(choice.seamBolt!==undefined&&![3,4].includes(choice.seamBolt))throw Error('Choose M3 or M4 seam hardware.');
 }
 for(const key of ['roots','mounts'])for(const [id,value]of Object.entries(config[key]||{}))if(!/^\d+$/.test(id)||![0,1].includes(value))throw Error('Choose inserts or through bolts.');
}
export function connectionCatalog(m){
 const seams=new Map(),roots=[];
 for(const ins of m.instances.filter(i=>i.part.kind==='panel')){
  for(const f of ins.part.spec.flanges){const id=jointKey(f.frame,f.start,f.end,ins.a);if(!seams.has(id))seams.set(id,{id,family:f.family||(f.frame.o.every(x=>x===0)?'radial':'ring'),row:ins.part.row,method:f.joint??m.p.seamJoint,size:f.bolt??m.p.seamBolt,parts:[],stations:f.stations.length});seams.get(id).parts.push({part:ins.part.id,angle:ins.a});}
  if(ins.part.row===0){const index=Math.round(ins.a/(2*Math.PI/m.layout.n))%m.layout.n;roots.push({id:'root:'+index,family:'root',index,method:rootChoice(m.p,index),size:m.p.rootBolt,part:ins.part.id,angle:ins.a});}
 }
 const joints=[...seams.values()];joints.sort((a,b)=>a.family.localeCompare(b.family)||a.row-b.row||a.id.localeCompare(b.id));const numbering={radial:0,ring:0};joints.forEach(j=>j.label=j.family==='radial'?`Petal seam ${++numbering.radial} · ring ${j.row+1}`:`Ring ${j.row+1} → ${j.row+2} · seam ${++numbering.ring}`);
 roots.sort((a,b)=>a.index-b.index);roots.forEach(j=>j.label='Petal '+(j.index+1)+' → hub');
 return [...joints,...roots,...Array.from({length:4},(_,index)=>({id:'mount:'+index,family:'mount',index,method:mountChoice(m.p,index),size:m.p.mountBolt,label:'Hub → mount '+(index+1)}))];
}
