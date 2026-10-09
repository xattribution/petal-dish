import {setupWorkspace,workspaceSettings,overridesUseClips} from './workspace-ui.js';
import {VERSION,BUILD_ID} from './version.js';
import {defaults,limits,validate,clipStrain,usesClips,usesLevers,seamBolt,FASTENER} from './params.js';
import {binarySTL,volume,plateRows,manualPlates} from './mesh.js';
import {Viewer} from './viewer.js';
import {startEngine} from './engine-client.js';
import {modelConfig,configId,encodeConfig,isBaseline,readConfig,resolveConfig} from './config.js';
const $=id=>document.getElementById(id);
let model,viewer,selected='',timer,busy=false,valid=false,plateDirty=false,workspaceUI;
let building=null,rebuild=false,firstModel=true,buildSeq=0,queued=false;
// The configuration on screen ({cfg, id, code}) and a loaded hand arrangement waiting for its build.
let shown=null,pendingRows=null;
$('app-version').textContent=VERSION;$('app-version').title='Build '+BUILD_ID;$('build-info').textContent=`${VERSION} · ${BUILD_ID}`;document.title='PETAL '+VERSION;
for(let n=6;n<=16;n+=2)$('sectors').add(new Option(`${n} petals`,String(n)));for(let n=1;n<=8;n++)$('rows').add(new Option(`${n} ring${n===1?'':'s'}`,String(n)));
try{viewer=new Viewer($('canvas'));}catch(e){$('view-error').hidden=false;$('view-error').textContent=e.message;}
// Short status messages over the preview (a live region, shown on every screen size).
let flashTimer=0;const flash=text=>{const el=$('view-flash');el.textContent=text;clearTimeout(flashTimer);flashTimer=setTimeout(()=>{el.textContent='';},3200);};
const printed=m=>m.parts.filter(p=>p.printIncluded!==false);
const packingResult=()=>{const n=model.plates.length;$('packing-result').textContent=model.p.packPlates?`${n} shared ${n===1?'bed':'beds'}${model.manualPacking?' · arranged by hand':''}`:'';};
// Plate view: a short press on a part selects it; dragging it onto another plate (or the empty slot after the last one)
// moves that copy there, wherever it fits nearest the drop point. Other plates keep their arrangement.
const dragBlock=()=>plateDirty?'Apply or reset the typed plate edits first.':busy||building||queued?'Wait for the update to finish.':!valid?'Fix the settings first.':'';
if(viewer){viewer.onPickPart=id=>{if(id!==selected)select(id);};
 viewer.canDrag=()=>!dragBlock();viewer.onDragRefused=()=>flash(dragBlock());
 viewer.onDropPart=async({copy,plate,x,y})=>{const block=dragBlock();if(block||!model?.p.packPlates){viewer.settle();if(block)flash(block);return;}
  const[id,n]=copy.split(':'),name=`${model.parts.find(q=>q.id===id)?.name??id} · copy ${n}`,key=model.key;busy=true;buttons();flash('Placing '+name+'…');
  let rows;try{rows=await engine.call('move',{key,plates:model.manualPacking?plateRows(model.plates):null,copy,target:plate,x,y});}catch(e){viewer.settle();flash(e.message);return;}finally{busy=false;buttons();}
  if(model.key!==key)return;if(!rows){viewer.settle();flash(`${name} does not fit on plate ${plate+1}`);return;}
  let next;try{next=manualPlates(printed(model),model.p,rows,true);}catch(e){viewer.settle();flash(e.message);return;}
  model.plates=next;model.manualPacking=true;renderPlates();packingResult();viewer.setModel(model);flash(`${name} → plate ${next.findIndex(pl=>pl.placements.some(q=>q.copy===copy))+1}`);};}
const showFatal=e=>{valid=false;$('error').hidden=false;$('error').textContent=e.message;$('fit-badge').textContent='Unavailable';$('fit-badge').className='badge bad';buttons();};
const engine=startEngine(globalThis.PETAL_ENGINE_SOURCE,showFatal);

// Stock diameters can be entered in mm or inches; the model always takes mm.
const UNIT_FIELDS={rodDiameter:'rodUnits',mastDiameter:'mastUnits',legDiameter:'legUnits'};
const unitScale=k=>$(UNIT_FIELDS[k]).value==='in'?25.4:1,previousScale=Object.fromEntries(Object.keys(UNIT_FIELDS).map(k=>[k,1]));
const fieldValue=k=>k==='rodDiameter'&&$('rodSizing').value==='auto'?0:$(k).type==='checkbox'?Number($(k).checked):$(k).value.trim()===''?NaN:UNIT_FIELDS[k]?Number($(k).value)*unitScale(k):Number($(k).value);
// A field that is disabled or hidden does not apply to the current design, so an out-of-range value left in it falls
// back to the last valid value instead of blocking the build.
let lastGood={...defaults};
const inRange=(k,v)=>Number.isFinite(v)&&v>=limits[k][0]&&v<=limits[k][1];
function read(){return {...workspaceSettings(),...Object.fromEntries(Object.keys(defaults).map(k=>{const v=fieldValue(k),el=$(k);return[k,(el.disabled||el.closest('[hidden]'))&&!inRange(k,v)?lastGood[k]:v];}))};}
// Validation messages name settings by their label, not their internal key.
const labelOf=k=>{const l=$(k)?.labels?.[0];const text=l?[...l.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join(' ').replace(/\s+/g,' ').trim():'';return text||k;};
const readable=message=>message.replace(/^(\w+) must/,(m,k)=>k in defaults?`${labelOf(k)} must`:m);
function setFields(p){for(const k in defaults){if($(k).type==='checkbox')$(k).checked=Boolean(p[k]);else if(k==='rodDiameter'){$('rodSizing').value=p[k]===0?'auto':'manual';$(k).value=(p[k]||6.35)/unitScale(k);}else if(UNIT_FIELDS[k])$(k).value=Number((p[k]/unitScale(k)).toPrecision(12));else $(k).value=p[k];if($(k+'-range'))$(k+'-range').value=p[k];}syncBed();optionUI();}
function syncBed(){const v=[$('bedX').value,$('bedY').value,$('bedZ').value].join(',');$('bedPreset').value=[...$('bedPreset').options].some(o=>o.value===v)?v:'custom';}
// The printer volume of the last valid build is remembered on this device (best effort: storage may be unavailable).
const BED_KEY='petal.printer';
function saveBed(p){try{localStorage.setItem(BED_KEY,JSON.stringify([p.bedX,p.bedY,p.bedZ]));}catch{}}
function savedBed(){try{const v=JSON.parse(localStorage.getItem(BED_KEY)||'null');return Array.isArray(v)&&v.length===3&&v.every((x,i)=>Number.isFinite(x)&&x>=limits[['bedX','bedY','bedZ'][i]][0]&&x<=limits[['bedX','bedY','bedZ'][i]][1])?{bedX:v[0],bedY:v[1],bedZ:v[2]}:null;}catch{return null;}}
// Show only the controls that apply. Fields stay disabled as well as hidden so read-only state is explicit.
function optionUI(){
 const seam=Number($('seamJoint').value),clips=usesClips({seamJoint:seam})||overridesUseClips(),feed=Number($('feedMode').value),auto=$('rodSizing').value==='auto';
 $('rodDiameter').disabled=$('rodUnits').disabled=auto;$('rod-manual').hidden=auto;$('payload-field').hidden=!auto;$('feedPayload').disabled=!auto;
 for(const k in UNIT_FIELDS){$(k).min=(k==='rodDiameter'?2:limits[k][0])/unitScale(k);$(k).max=limits[k][1]/unitScale(k);}
 $('mount-settings').hidden=!Number($('mountMode').value);$('leg-settings').hidden=Number($('mountBase').value)!==2;{const base=Number($('mountBase').value);$('stand-fields').hidden=base===2;$('stand-seat-field').hidden=base!==0;$('nut-seat-field').hidden=base===0;}$('elevation').min=seam?'-7.5':'-10';
 for(const id of ['clipMaterial','clipAllowableStrain','clipFit','clipDetent'])$(id).disabled=!clips;$('clip-tuning').hidden=!clips;
 $('seamBolt').disabled=seam===1;$('seam-bolt-field').hidden=seam===1;
 $('feed-settings').hidden=!feed;$('prime-settings').hidden=feed!==1&&feed!==3;$('secondary-settings').hidden=feed!==2;$('collector-settings').hidden=$('mast-field').hidden=feed!==3;$('sizing-settings').hidden=feed!==2&&feed!==3;
 const autoSize=Boolean(Number($('autoSecondary').value)),freq=Number($('frequencyGHz').value)>0,bowl=feed===3;
 $('secondaryPosition').disabled=autoSize&&freq;$('secondaryWaves').disabled=!autoSize||bowl&&!freq;$('waves-field').hidden=bowl&&(!autoSize||!freq);$('bowlDiameter').disabled=!bowl||autoSize;$('bowl-field').hidden=!bowl||autoSize;
 $('sizing-label').textContent=bowl?'Bowl sizing':'Secondary sizing';$('waves-label').textContent=bowl?'Bowl diameter':'Secondary diameter';
 const sizing=$('autoSecondary').options;sizing[0].text=bowl?'Smallest that hides the insert':'From frequency when specified';sizing[1].text=bowl?'Enter diameter':'Manual geometric experiment';
 const tip=bowl?'Auto uses the smallest bowl whose shadow covers the insert, or the wavelength count when a frequency is set, whichever is larger.':'Geometric prototype. Secondary size, diffraction and rear-feed clearance need RF validation.';$('sizing-tip').dataset.tip=tip;$('sizing-tip').setAttribute('aria-label',tip);
 $('facet-settings').hidden=!Number($('rearStyle').value);$('segmentGoal').disabled=Number($('sectors').value)>0&&Number($('rows').value)>0;
 const inserts=!(Number($('mountThrough').value)===1&&Number($('rootThrough').value)===1)||Number($('mountMode').value)===1;$('insertDiameter').disabled=!inserts;$('insert-field').hidden=!inserts;{const rt=Number($('rootThrough').value)===1,mt=Number($('mountThrough').value)===1;$('root-seat-field').hidden=!rt;$('mount-seat-field').hidden=!mt;$('seat-fields').hidden=!rt&&!mt;$('seat-fields').classList.toggle('single',rt!==mt);}
 $('plateGap').closest('label').hidden=!$('packPlates').checked;
 workspaceUI?.seamChanged();
}
function buttons(){$('config-toggle').classList.toggle('stale',!valid);$('fit-plates').disabled=busy;$('apply-plates').disabled=!valid||busy;$('auto-plates').disabled=!valid||busy;for(const id of ['export-all','export-scad','export-pdf'])$(id).disabled=!valid||busy||plateDirty;$('export-part').disabled=!valid||busy||plateDirty||!selected;}
// Cross-section: dish, prime focus and, with a secondary or collector bowl, its profile, F2 and four traced rays.
function profile(m){
 const D=m.p.diameter,R=D/2,f=m.focal,b=m.feed?.bowl,s=m.feed?.secondary,sub=b?.profile??s?.profile,F2=b?b.insertFocus:s?.backFocus;
 const zMax=Math.max(f,...(sub??[]).map(q=>q[1])),zMin=Math.min(0,F2??0),scale=Math.min(218/D,92/(zMax-zMin)),cx=130,X=r=>(cx+r*scale).toFixed(2),Y=z=>(108-(z-zMin)*scale).toFixed(2);
 const line=pts=>pts.map(([r,z],i)=>`${i?'L':'M'}${X(r)},${Y(z)}`).join(' '),dish=Array.from({length:61},(_,i)=>{const x=-R+D*i/60;return[x,x*x/(4*f)];});
 let svg=`<line class="axis" x1="${cx}" y1="${(Y(zMax)-8).toFixed(2)}" x2="${cx}" y2="${(+Y(zMin)+4).toFixed(2)}"/><path class="dish" d="${line(dish)}"/>`;
 // Each ray: straight down to the dish, toward F1, then (with a reflector) to its first crossing of the mirrored profile and on to F2.
 const both=sub?[...[...sub].reverse().map(([r,z])=>[-r,z]),...sub]:null,hit=(P,d)=>{let best=null;for(let i=1;i<both.length;i++){const[a,c]=[both[i-1],both[i]],e=[c[0]-a[0],c[1]-a[1]],den=d[0]*e[1]-d[1]*e[0];if(Math.abs(den)<1e-12)continue;const w=[a[0]-P[0],a[1]-P[1]],t=(w[0]*e[1]-w[1]*e[0])/den,u=(w[0]*d[1]-w[1]*d[0])/den;if(t>1e-6&&u>=0&&u<=1&&(!best||t<best))best=t;}return best===null?null:[P[0]+best*d[0],P[1]+best*d[1]];};
 for(const k of [-.92,-.6,.6,.92]){const P=[k*R,(k*R)**2/(4*f)],H=both?hit(P,[-P[0],f-P[1]]):null;svg+=`<path class="ray" d="${line([[P[0],zMax+8/scale],P,...(H?[H,[0,F2]]:[[0,f]])])}"/>`;}
 if(sub)svg+=`<path class="sub" d="${line(both)}"/><circle class="focus" cx="${cx}" cy="${Y(F2)}" r="3"/><text x="${cx+9}" y="${+Y(F2)+4}">F2 ${F2.toFixed(0)} mm</text>`;
 svg+=`<circle class="focus" cx="${cx}" cy="${Y(f)}" r="3"/><text x="${(cx+9+(b?b.radius*scale:0)).toFixed(2)}" y="${+Y(f)+4}">${sub?'F1':'focus'} ${f.toFixed(0)} mm</text>`;
 $('profile').innerHTML=svg;}
const tripodResult=t=>`${t.poseClear?'':'The dish meets a leg at this pose · '}Legs ${t.minLegLength} mm or longer · feet on a Ø${Math.round(2*t.footRadius)} mm circle · M${t.bolt.size} × ${t.bolt.length} cross bolts · ${t.blocked?`dish clears the legs above ${t.clearFrom}°`:'dish clears the legs at every pose'}`;
const collectorResult=m=>{const g=m.feed,b=g.bowl,t=g.mast;return `Bowl Ø${(2*b.radius).toFixed(1)} mm${b.waves?` (${b.waves.toFixed(1)} λ)`:''} · insert ±${b.insertAngle}° at ${b.apexToInsert.toFixed(1)} mm below the bowl · ${(100*b.blockage).toFixed(1)}% blockage · ${g.rodCount} rods Ø${+g.rodDiameter.toFixed(4)} × ${g.cutLength.toFixed(1)} mm · ${t.tube?`mast Ø${+t.diameter.toFixed(3)} × ${t.cutLength.toFixed(1)} mm`:'printed pedestal'}`;};
function select(id){selected=id===selected?'':id;showSelection();}
function showSelection(){for(const b of $('parts').children)b.setAttribute('aria-pressed',b.dataset.id===selected?'true':'false');const p=model?.parts.find(x=>x.id===selected);const facts=p?[p.dim.map(x=>x.toFixed(1)).join(' × ')+' mm · '+(volume(p.mesh)/1000).toFixed(1)+' cm³']:[];if(p?.kind==='panel'){if(p.wallRange)facts.push('Wall '+p.wallRange.map(x=>x.toFixed(1)).join('–')+' mm');facts.push('Side-printed · bed rotation '+p.bedRotation+'°');}else if(p?.gripRange)facts.push('Grip '+p.gripRange.map(x=>x.toFixed(1)).join('–')+' mm');if(p&&p.printIncluded===false)facts.push('Left off the plates and STL export');$('selected-info').textContent=p?facts.join('\n'):'Select a part';if(viewer){viewer.selected=selected;viewer.jointSelection=null;viewer.draw();}buttons();}
function setBusyBadge(on){if(on){$('fit-badge').textContent='Updating…';$('fit-badge').className='badge busy';}}
// One build at a time: changes made while a build runs are folded into a single follow-up build.
function generate(){optionUI();clearTimeout(timer);queued=false;$('export-status').textContent='';
 if(building){rebuild=true;return building;}
 let p;try{p=validate(read());}catch(e){showError(e);return Promise.resolve(null);}
 const key=++buildSeq;setBusyBadge(true);valid=false;buttons();
 // A hand arrangement goes along; the engine keeps it if every copy still fits where it was put.
 const plates=pendingRows&&p.packPlates?pendingRows:model?.manualPacking&&model.p.packPlates&&p.packPlates?plateRows(model.plates):null;pendingRows=null;
 building=engine.call('build',{key,params:p,plates}).then(m=>({m}),error=>({error})).then(({m,error})=>{building=null;
  if(rebuild){rebuild=false;return generate();}
  if(error){showError(error);return null;}
  m.key=key;apply(m);return m;});
 return building;
}
function showError(e){valid=false;$('error').hidden=false;$('error').textContent=readable(e.message)+(model?' The preview shows the last valid model.':'');$('fit-badge').textContent='Needs adjustment';$('fit-badge').className='badge bad';buttons();}
function apply(next){
 model=next;const p=model.p;lastGood=Object.fromEntries(Object.keys(defaults).map(k=>[k,p[k]]));saveBed(p);if(model.arrangementLost)flash('The hand arrangement no longer fits, so the plates were packed again.');
 const fitted=model.parts.find(q=>q.kind==='clip'||q.id.startsWith('seam-lever-lever'));
 $('clip-result').textContent=model.connectionCounts?`${model.connectionCounts.clip} clip stations · ${model.connectionCounts.lever[3]+model.connectionCounts.lever[4]} lever stations`:usesClips(p)?`Jaw strain ${(100*clipStrain(p)).toFixed(2)}% of ${p.clipAllowableStrain}% · ${fitted.installed} clips + ${fitted.spares} spares`:usesLevers(p)?`${fitted.installed} lever sets + ${fitted.spares} spares for Ø${fitted.spec.hole_d} holes`+(model.boltStations?` · ${model.boltStations} junction stations take M${p.seamBolt} bolts`:''):'';
 $('mount-result').textContent=model.mount?.tripod?tripodResult(model.mount.tripod):model.mount?`Clearance below the ${model.mount.base?'base':'yoke plate'}: ${model.mount.standClearance.toFixed(1)} mm now · ${model.mount.sweepStandClearance.toFixed(1)} mm over ${model.mount.elevationRange[0]}…${model.mount.elevationRange[1]}°`:'';
 $('rf-result').textContent=p.frequencyGHz?`λ ${(299.792458/p.frequencyGHz).toFixed(2)} mm · surface target ≤ ${(299.792458/p.frequencyGHz/50).toFixed(2)} mm RMS`:'';
 $('hub-tip').dataset.tip=`Flat prints cleanly. It sits level with the petals at the hub's corners, chamfers down to them along each edge, and stands up to ${(((45/Math.cos(Math.PI/model.layout.n))**2-225)/(4*p.diameter*p.fd)).toFixed(2)} mm above the parabola near the center opening, inside the feed's shadow. Curved follows the parabola.`;$('hub-tip').setAttribute('aria-label',$('hub-tip').dataset.tip);
 $('feed-result').textContent=model.feed?.bowl?collectorResult(model):model.feed?`${model.feed.rodCount} rods Ø${+model.feed.rodDiameter.toFixed(4)} × ${model.feed.cutLength.toFixed(1)} mm · ${model.feed.rodAngle.toFixed(1)}° · carrier z ${model.feed.carrierFace.toFixed(1)} mm`+(model.feed.secondary?` · secondary Ø${(2*model.feed.secondary.radius).toFixed(1)} mm`:'')+(model.feed.lambda?` · RMS target ${model.feed.surfaceRmsBudget.toFixed(3)} mm`:''):'';
 valid=!queued;plateDirty=false;workspaceUI?.update();renderPlates();
 if(viewer?.mode==='layout')$('view-caption').textContent=p.packPlates?'Packed print beds · all quantities':'Individual print beds';
 packingResult();
 const screw=seamBolt(p).screw.replace(' socket head','');
 $('hardware-type').textContent=model.connectionCounts?'Mixed connections · see the joint map and hardware schedule':[`${screw} seams`,'Snap-clip seams',`${screw} or clip seams`,`Seam levers${model.boltStations?' + '+model.boltStations+' × '+screw:''}`][p.seamJoint]+(p.rootThrough?` · ${model.rootScrew} roots · nuts and washers`:` · ${model.rootScrew} roots · short M${p.rootBolt} inserts`);
 $('mount-style').textContent=p.mountThrough?`Ø${+(FASTENER[p.mountBolt].clear+.1).toFixed(1)} through holes · M${p.mountBolt} nuts`:`Blind M${p.mountBolt} mount inserts`;$('mount-pattern').textContent=`4 × M${p.mountBolt} / 60 mm BCD`;
 $('error').hidden=true;if(!queued){$('fit-badge').textContent='Fits bed';$('fit-badge').className='badge';}
 $('model-title').textContent=`${p.diameter} / ${model.layout.n}P${model.layout.rows>1?' × '+model.layout.rows+'R':''}`;
 $('depth-stat').innerHTML=`${model.depth.toFixed(1)} <small>mm</small>`;$('focal-stat').innerHTML=`${model.focal.toFixed(1)} <small>mm</small>`;
 $('count-stat').innerHTML=`${model.parts.reduce((s,q)=>s+q.qty,0)} <small>/ ${model.parts.length} unique</small>`;
 $('volume-stat').innerHTML=`${(model.parts.reduce((s,q)=>s+volume(q.mesh)*q.qty,0)/1000).toFixed(0)} <small>cm³</small>`;
 $('bolts').textContent=model.bolts;
 const focused=document.activeElement?.closest?.('#parts .part-row')?.dataset.id;
 $('parts').replaceChildren(...model.parts.map(q=>{const b=document.createElement('button');b.className='part-row '+q.kind;b.dataset.id=q.id;if(q.printIncluded===false)b.dataset.excluded='';b.setAttribute('aria-pressed','false');b.innerHTML=`<span class="part-swatch" aria-hidden="true"></span><span class="part-name">${q.name}<small>${q.dim.map(x=>x.toFixed(1)).join(' × ')} mm</small></span><span class="qty">×${q.qty}</span>`;b.onclick=()=>select(q.id);return b;}));
 // the selection and keyboard focus survive a rebuild when the part still exists
 if(!model.parts.some(q=>q.id===selected))selected='';if(focused)[...$('parts').children].find(b=>b.dataset.id===focused)?.focus();
 if(viewer){viewer.jointSelection=null;viewer.setModel(model);}
 profile(model);showSelection();showConfig();
 if(firstModel){firstModel=false;document.body.dataset.ready='true';}
}
function schedule(){valid=false;queued=true;buttons();clearTimeout(timer);timer=setTimeout(generate,260);}
for(const [k,units] of Object.entries(UNIT_FIELDS))$(units).addEventListener('input',()=>{const scale=unitScale(k);$(k).value=Number((Number($(k).value)*previousScale[k]/scale).toPrecision(12));previousScale[k]=scale;optionUI();schedule();});$('rodSizing').addEventListener('input',()=>{optionUI();schedule();});
$('parameters').addEventListener('submit',e=>e.preventDefault());
const geometryKeys=['diameter','fd','sectors','rows','segmentGoal','staggerRings','bedX','bedY','bedZ','feedMode','feedLegs'];
for(const k in defaults){$(k).addEventListener('input',()=>{if($(k+'-range'))$(k+'-range').value=$(k).value;if(k.startsWith('bed'))syncBed();if(geometryKeys.includes(k))workspaceUI.geometryChanged();optionUI();schedule();});if($(k+'-range'))$(k+'-range').addEventListener('input',()=>{$(k).value=$(k+'-range').value;if(geometryKeys.includes(k))workspaceUI.geometryChanged();schedule();});}
$('bedPreset').addEventListener('change',()=>{if($('bedPreset').value==='custom')return;const [x,y,z]=$('bedPreset').value.split(',');$('bedX').value=x;$('bedY').value=y;$('bedZ').value=z;workspaceUI.geometryChanged();generate();});
$('reset').onclick=()=>{workspaceUI.reset();setFields({...defaults,bedX:lastGood.bedX,bedY:lastGood.bedY,bedZ:lastGood.bedZ});generate();};$('home').onclick=()=>viewer?.reset();
for(const b of document.querySelectorAll('[data-view]'))b.onclick=()=>{for(const x of document.querySelectorAll('[data-view]')){x.classList.toggle('active',x===b);x.setAttribute('aria-pressed',x===b?'true':'false');}$('explosion-control').hidden=b.dataset.view!=='exploded';$('view-caption').textContent=b.dataset.view==='layout'?(model?.p.packPlates?'Packed print beds · all quantities':'Individual print beds'):'';if(viewer){viewer.mode=b.dataset.view;viewer.reset();viewer.rebuild();}};
$('rear-view').onclick=()=>{document.querySelector('[data-view=assembled]').click();if(viewer){viewer.pitch=2.3;viewer.draw();}};
$('pan-view').onclick=()=>{if(viewer){viewer.navigation=viewer.navigation==='pan'?'orbit':'pan';$('pan-view').setAttribute('aria-pressed',String(viewer.navigation==='pan'));}};
$('focus').onchange=()=>{if(viewer){viewer.focus=$('focus').checked;viewer.rebuildOverlays();}};$('wire').onchange=()=>{if(viewer){viewer.wire=$('wire').checked;viewer.rebuild();}};$('explosion').oninput=()=>{if(viewer){viewer.explode=Number($('explosion').value);viewer.requestDraw();}};
function download(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
async function exporting(fn){if(!valid||busy||plateDirty)return;busy=true;buttons();$('export-status').textContent='Preparing your files…';try{await new Promise(r=>setTimeout(r,30));await fn();$('export-status').textContent='Download prepared.';}catch(e){$('export-status').textContent=e.message;}finally{busy=false;buttons();}}
// Exports run in the engine against the model on screen, with any manual plate arrangement made here.
const fromEngine=op=>engine.call(op,{key:model.key,plates:model.manualPacking?plateRows(model.plates):null});
$('export-part').onclick=()=>exporting(()=>{const p=model.parts.find(p=>p.id===selected);if(!p)throw Error('Select a part first.');download(new Blob([binarySTL(p.output)],{type:'model/stl'}),`${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl`);});
$('export-all').onclick=()=>exporting(async()=>{const name=`PETAL-${model.p.diameter}mm-print-kit.zip`;download(await fromEngine('kit'),name);});
$('export-scad').onclick=()=>exporting(async()=>download(new Blob([await fromEngine('scad')],{type:'text/plain'}),'petal-snapshot.scad'));
$('export-pdf').onclick=()=>exporting(async()=>{const name=`PETAL-${model.p.diameter}mm-assembly.pdf`;download(new Blob([await fromEngine('pdf')],{type:'application/pdf'}),name);});

// Narrow screens: the two side panels become drawers.
const narrow=matchMedia('(max-width:1100px)');
let panel='';
function setPanel(next,restore=true){
 const previous=panel;panel=narrow.matches?next:'';
 if(panel)document.body.dataset.panel=panel;else delete document.body.dataset.panel;
 $('panel-shade').hidden=!panel;
 document.querySelector('.stage-column').inert=Boolean(panel);
 for(const name of ['design','parts']){
  const el=$(name+'-panel'),active=name===panel;
  $(name+'-toggle').setAttribute('aria-expanded',String(active));
  el.inert=narrow.matches&&!active;
  if(active){el.setAttribute('role','dialog');el.setAttribute('aria-modal','true');}
  else{el.removeAttribute('role');el.removeAttribute('aria-modal');}
 }
 if(panel)$(panel+'-panel').querySelector('[data-close-panel]').focus();
 else if(previous&&restore)$(previous+'-toggle').focus();
}
for(const name of ['design','parts'])$(name+'-toggle').onclick=()=>setPanel(panel===name?'':name);
document.querySelectorAll('[data-close-panel]').forEach(b=>b.onclick=()=>setPanel(''));
$('panel-shade').onclick=()=>setPanel('');
narrow.addEventListener('change',()=>setPanel('',false));setPanel('',false);
document.addEventListener('keydown',e=>{
 if(!panel||document.querySelector('dialog[open]'))return;
 if(e.key==='Escape'){e.preventDefault();setPanel('');}
 if(e.key==='Tab'){
  const items=[...$(panel+'-panel').querySelectorAll('button,input,select,summary,a[href],[tabindex="0"]')].filter(el=>!el.disabled&&el.getClientRects().length);
  const first=items[0],last=items.at(-1);
  if(e.shiftKey&&(document.activeElement===first||!$(panel+'-panel').contains(document.activeElement))){e.preventDefault();last?.focus();}
  else if(!e.shiftKey&&(document.activeElement===last||!$(panel+'-panel').contains(document.activeElement))){e.preventDefault();first?.focus();}
 }
});
$('guide-content').innerHTML='<ol><li><strong>Print the fit strips first.</strong> They are real sections of a petal. Try your bolts, clips or levers on them before the full kit.</li><li><strong>Keep the exported orientation.</strong> Petals stand on their flat radial flange. Check bores and overhangs in your slicer.</li><li><strong>Follow the kit manual.</strong> It has the hardware list, print notes and assembly steps for these exact settings.</li></ol><button type="button" class="button secondary" id="guide-pdf">Download the assembly PDF</button>';
$('guide-pdf').onclick=()=>{$('guide').close();$('export-pdf').click();};
$('guide-toggle').onclick=()=>{setPanel('',false);$('guide').showModal();$('guide-toggle').setAttribute('aria-expanded','true');};
$('guide-close').onclick=()=>$('guide').close();
$('guide').addEventListener('close',()=>{$('guide-toggle').setAttribute('aria-expanded','false');$('guide-toggle').focus();});

// Configuration codes: the ID in the header, the share / save / load dialog, and the address bar, which always holds
// the code of the design on screen (nothing for the defaults), so a reload or a copied address keeps it.
function showConfig(){const cfg=modelConfig(model),id=configId(cfg),code=encodeConfig(cfg);shown={cfg,id,code};
 const t=$('config-toggle');t.disabled=false;t.textContent=id;t.setAttribute('aria-label','Configuration '+id);if($('config-dialog').open)renderConfig();
 // A page opened from a file cannot use replaceState in some browsers; a fragment-only location.replace still works there.
 {const want=isBaseline(cfg)?'':'#'+code;if(location.hash!==want)try{history.replaceState(history.state,'',want||location.pathname+location.search);}catch{try{location.replace(want||'#');}catch{}}}}
// Read and check a configuration without touching the form, so a bad code leaves the design as it was.
function prepareConfig(input){const cfg=readConfig(input);let r;try{r=resolveConfig(cfg);}catch(e){throw Error(readable(e.message));}
 for(const[k,v]of Object.entries(r.params)){const el=$(k);if(el?.tagName==='SELECT'&&![...el.options].some(o=>o.value===String(v)))throw Error(`${labelOf(k)} has no option ${v} in this version.`);}
 return{cfg,...r};}
async function applyConfig({cfg,params,rows,unknown}){
 workspaceUI.restore(params);setFields(params);pendingRows=rows;
 const m=await generate();if(!m)throw Error($('error').textContent||'This configuration does not build.');
 const notes=[`Loaded ${shown.id}.`],want=configId(cfg);
 if(unknown.length)notes.push(`Skipped ${unknown.join(', ')}, which this version does not have.`);
 if(cfg.noArrangement)notes.push('The file has no hand arrangement, so the plates were packed again.');else if(rows&&!m.manualPacking)notes.push('The hand arrangement no longer fits, so the plates were packed again.');
 if(want!==shown.id)notes.push(`The code's ID was ${want}.`);
 if(cfg.app&&cfg.app!==VERSION)notes.push(`Made with PETAL ${cfg.app}; parts may differ from that version's.`);
 return notes.join(' ');}
const SAVED_KEY='petal.configurations',configStatus=text=>{$('config-status').textContent=text;};
function savedConfigs(){try{const v=JSON.parse(localStorage.getItem(SAVED_KEY)||'[]');return Array.isArray(v)?v.filter(x=>x&&typeof x.name==='string'&&typeof x.code==='string'&&typeof x.id==='string').sort((a,b)=>String(b.at).localeCompare(String(a.at))):[];}catch{return[];}}
function storeConfigs(list){try{localStorage.setItem(SAVED_KEY,JSON.stringify(list));return true;}catch{return false;}}
const savedDate=at=>{const d=new Date(at);return Number.isNaN(+d)?'':d.toLocaleDateString(undefined,d.getFullYear()===new Date().getFullYear()?{month:'short',day:'numeric'}:{year:'numeric',month:'short',day:'numeric'});};
function renderConfig(){if(!shown)return;$('config-id').textContent=shown.id;$('config-app').textContent='PETAL '+VERSION;$('config-code').value=shown.code;$('copy-link').hidden=location.protocol==='file:';
 $('config-list').replaceChildren(...savedConfigs().map(entry=>{const li=document.createElement('li'),load=document.createElement('button'),del=document.createElement('button'),name=document.createElement('span'),meta=document.createElement('small');
  if(entry.id===shown.id)li.setAttribute('aria-current','true');name.textContent=entry.name;meta.textContent=[entry.id,savedDate(entry.at)].filter(Boolean).join(' · ');
  load.type=del.type='button';load.className='config-load';load.append(name,meta);load.onclick=()=>loadFromDialog(entry.code);
  del.className='config-delete';del.textContent='×';del.setAttribute('aria-label','Delete '+entry.name);del.onclick=()=>{storeConfigs(savedConfigs().filter(x=>!(x.name===entry.name&&x.at===entry.at)));renderConfig();configStatus(`Deleted “${entry.name}”.`);};
  li.append(load,del);return li;}));}
async function copyText(text,what){try{await navigator.clipboard.writeText(text);configStatus(what+' copied.');}catch{const el=$('config-code');el.focus();el.select();configStatus('This browser blocked copying. The code is selected; copy it with the keyboard.');}}
async function loadFromDialog(input){let prep;try{prep=prepareConfig(input);}catch(e){configStatus(e.message);return;}
 if(busy){configStatus('Wait for the update to finish.');return;}
 configStatus('Loading…');try{const note=await applyConfig(prep);$('config-input').value='';$('config-dialog').close();flash(note);}catch(e){configStatus(e.message);}}
$('config-toggle').onclick=()=>{setPanel('',false);configStatus('');renderConfig();$('config-dialog').showModal();$('config-toggle').setAttribute('aria-expanded','true');};
$('config-close').onclick=()=>$('config-dialog').close();
$('config-dialog').addEventListener('close',()=>{$('config-toggle').setAttribute('aria-expanded','false');$('config-toggle').focus();});
$('copy-code').onclick=()=>{if(shown)copyText(shown.code,'Code');};
$('copy-link').onclick=()=>{if(shown)copyText(location.href.split('#')[0]+'#'+shown.code,'Link');};
$('load-config').onclick=()=>loadFromDialog($('config-input').value);
$('save-config').onclick=()=>{if(!shown)return;const name=$('config-name').value.trim()||$('model-title').textContent,list=savedConfigs().filter(x=>x.name!==name);
 list.unshift({name,id:shown.id,code:shown.code,app:VERSION,at:new Date().toISOString()});if(!storeConfigs(list.slice(0,100))){configStatus('This browser is not keeping saved designs. Use Save file instead.');return;}
 $('config-name').value='';renderConfig();configStatus(`Saved “${name}”.`);};
$('config-name').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('save-config').click();}});
// The file carries the code (which loads it) and, for reading, the changed settings and any hand arrangement.
$('export-config').onclick=()=>{if(!shown)return;const name=$('config-name').value.trim(),c=shown.cfg,file=(name||shown.id).replace(/[^\w.-]+/g,'-').replace(/^-+|-+$/g,'')||shown.id;
 download(new Blob([JSON.stringify({petal_configuration:1,name:name||shown.id,id:shown.id,app:VERSION,build:BUILD_ID,saved:new Date().toISOString(),code:shown.code,changed_settings:c.p,...(c.c?{connections:c.c}:{}),...(c.s?{left_off_plates:c.s}:{}),...(c.pl?{plates:c.pl.map(([copy,plate,x,y,yaw])=>({copy,plate,x,y,yaw}))}:{})},null,2)+'\n'],{type:'application/json'}),`PETAL-${file}.json`);configStatus('File saved.');};
$('import-config').onclick=()=>$('config-file').click();
$('config-file').onchange=()=>{const input=$('config-file'),file=input.files?.[0];if(!file)return;const reader=new FileReader();reader.onload=()=>{input.value='';loadFromDialog(String(reader.result));};reader.onerror=()=>configStatus('That file could not be read.');reader.readAsText(file);};
// A different code pasted into the address bar of this tab loads without a reload.
addEventListener('hashchange',()=>{const code=decodeURIComponent(location.hash.slice(1));if(!/P1\./.test(code)||code===shown?.code)return;let prep;try{prep=prepareConfig(code);}catch(e){flash(e.message);return;}applyConfig(prep).then(flash,e=>flash(e.message));});
workspaceUI=setupWorkspace({changed:generate,getModel:()=>model,showJoint:j=>{if(viewer){viewer.jointSelection=j.parts||[{part:j.part||'hub',angle:j.angle||0}];viewer.draw();}}});
// A link that carries a configuration code opens that design; otherwise the defaults on this device's printer.
{let start=null;try{start=/P1\./.test(location.hash)?prepareConfig(decodeURIComponent(location.hash.slice(1))):null;}catch(e){flash(e.message);}
 if(start)applyConfig(start).then(note=>flash(note),e=>flash(e.message));else{setFields({...defaults,...savedBed()});generate();}}
// Optional browser agent interface; it shares the visible form and generation path.
if(document.modelContext?.registerTool){const abort=new AbortController();window.addEventListener('pagehide',()=>abort.abort(),{once:true});const register=t=>{try{Promise.resolve(document.modelContext.registerTool(t,{signal:abort.signal})).catch(()=>{});}catch{}};
register({name:'inspect_dish_geometry',description:'Read the current valid dish dimensions, print parts and quantities. Does not download files.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:false},async execute(){return valid?await fromEngine('manifest'):{error:'The form has invalid settings.'};}});
register({name:'configure_dish_geometry',description:'Update geometric and printer parameters in the visible form and regenerate the model. Does not download files.',inputSchema:{type:'object',properties:Object.fromEntries(Object.entries(limits).map(([k,[min,max]])=>[k,{type:'number',minimum:min,maximum:max}])),additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:false},async execute(input){if(!input||typeof input!=='object'||Array.isArray(input))throw Error('Expected an object.');for(const k in input)if(!(k in defaults))throw Error(`Unknown setting: ${k}`);for(const [k,v] of Object.entries(input)){const el=$(k);if(el.tagName==='SELECT'&&![...el.options].some(o=>o.value===String(v)))throw Error(`Choose an available option for ${k}.`);}const p=validate({...read(),...input});setFields(p);if(building)await building;const m=await generate();if(!m)throw Error($('error').textContent||'Model generation failed.');return{diameter:m.p.diameter,petals:m.layout.n,rings:m.layout.rows,unique_parts:m.parts.length};}});}

function renderPlates(){
 $('plate-editor').hidden=!model.p.packPlates;
 $('plate-status').textContent='';
 $('plate-rows').replaceChildren(...plateRows(model.plates).map(r=>{const el=document.createElement('fieldset');el.className='plate-copy';el.dataset.copy=r.copy;const title=document.createElement('legend');title.textContent=`${model.parts.find(q=>q.id===r.part)?.name??r.part} · copy ${r.copy.split(':')[1]}`;el.append(title);for(const [key,title]of [['plate','Plate'],['x','X mm'],['y','Y mm'],['yaw','Rotation °']]){const label=document.createElement('label'),input=document.createElement('input');label.textContent=title;input.type='number';input.dataset.key=key;input.value=r[key];input.step=key==='plate'?'1':'any';if(key==='plate')input.min=1;label.append(input);el.append(label);}return el;}));
}
// Typed arrangements are checked in the engine, where the parts' footprints are cached, so the page stays responsive.
$('apply-plates').onclick=async()=>{if(!valid||busy)return;const rows=[...$('plate-rows').children].map(el=>({copy:el.dataset.copy,...Object.fromEntries([...el.querySelectorAll('input')].map(i=>[i.dataset.key,i.value.trim()===''?NaN:Number(i.value)]))})),key=model.key;
 busy=true;buttons();$('plate-status').textContent='Checking the arrangement…';let checked;
 try{checked=await engine.call('plates',{key,plates:rows});}catch(e){$('plate-status').textContent=e.message+' The previous arrangement is kept.';return;}finally{busy=false;buttons();}
 if(model.key!==key)return;try{model.plates=manualPlates(printed(model),model.p,checked,true);}catch(e){$('plate-status').textContent=e.message;return;}
 model.manualPacking=true;plateDirty=false;buttons();renderPlates();$('plate-status').textContent='Arrangement applied to the preview and exports.';packingResult();viewer?.setModel(model);document.querySelector('[data-view=layout]').click();};
$('auto-plates').onclick=async()=>{if(!valid||busy)return;const key=model.key;busy=true;buttons();let rows;try{rows=await engine.call('pack',{key});}catch(e){$('plate-status').textContent=e.message;return;}finally{busy=false;buttons();}if(model.key!==key)return;
 try{model.plates=manualPlates(printed(model),model.p,rows,true);}catch(e){$('plate-status').textContent=e.message;return;}model.manualPacking=false;plateDirty=false;buttons();renderPlates();packingResult();viewer?.setModel(model);};
// Largest dish on N plates: the engine searches the diameter (any petal and ring count) and reports each step.
$('fit-plates').onclick=async()=>{if(busy)return;let p;try{p=validate(read());}catch(e){$('fit-status').textContent=readable(e.message);return;}
 const N=Number($('plateBudget').value);if(!Number.isInteger(N)||N<1||N>40){$('fit-status').textContent='Plates must be a whole number from 1 to 40.';return;}
 busy=true;buttons();$('fit-status').textContent='Searching…';let r;
 try{r=await engine.call('fit',{key:model?.key,params:p,plates:N},text=>{$('fit-status').textContent=text;});}catch(e){$('fit-status').textContent=e.message;return;}finally{busy=false;buttons();}
 $('diameter').value=r.diameter;if($('diameter-range'))$('diameter-range').value=r.diameter;$('sectors').value=String(r.sectors);$('rows').value=String(r.rows);workspaceUI.geometryChanged();
 $('fit-status').textContent=`${r.diameter} mm · ${r.sectors} petals × ${r.rows} ${r.rows===1?'ring':'rings'} · ${r.plates} ${r.plates===1?'plate':'plates'}`;await generate();};
$('plate-rows').addEventListener('input',()=>{plateDirty=true;buttons();$('plate-status').textContent='Unapplied edits. Apply or reset before exporting.';});
