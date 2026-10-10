// Configuration codes: exact round trips (settings, joint overrides, print selection, hand-placed plates), stable IDs,
// every accepted input form, clear refusals, and a frozen fixture so P1 codes made today load the same design later.
import assert from 'node:assert/strict';
import {build,defaults} from '../dist/geometry.js';
import {movePlacement,manualPlates,plateRows} from '../dist/mesh.js';
import {manifest,scadSource} from '../dist/exports.js';
import {configOf,modelConfig,configId,encodeConfig,readConfig,resolveConfig,isBaseline,configInfo,handRows} from '../dist/config.js';
import {VERSION} from '../dist/version.js';
const ID=/^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/,ALL={feed:true,mount:true,clip:true,lever:true};
const roundTrip=cfg=>readConfig(encodeConfig(cfg));

// Defaults are the baseline: an empty code whose ID is fixed.
const base=configOf({...defaults,printSelection:ALL});assert(isBaseline(base));assert.match(configId(base),ID);
// Settings, overrides and print selection survive exactly; key order and float noise do not change the code or ID.
const cases=[{diameter:600,bedX:300,bedY:300,bedZ:330},{diameter:1000,fd:.38,bedX:325,bedY:320,bedZ:325,feedMode:1,feedLegs:4,mountMode:1,mountSides:2,mountArcLock:1,thickness:2.8,plateGap:18},
 {diameter:600,bedX:300,bedY:300,bedZ:330,feedMode:3,frequencyGHz:10.5,mountMode:1,mountBase:2,legDiameter:19.05,seamJoint:3,connections:{families:{ring:{seamJoint:0,seamBolt:4}}},printSelection:{...ALL,mount:false}}];
const ids=new Set([configId(base)]);
for(const c of cases){const p={...defaults,printSelection:ALL,...c},cfg=configOf(p),code=encodeConfig(cfg),back=roundTrip(cfg);
 assert(code.startsWith('P1.')&&code.length<140,code);assert.equal(back.app,VERSION);assert.equal(configId(back),configId(cfg));assert.equal(encodeConfig(back),code);
 const {params,rows,unknown}=resolveConfig(back);assert.equal(rows,null);assert.deepEqual(unknown,[]);for(const k in defaults)assert.equal(params[k],p[k],k);assert.deepEqual(params.printSelection,p.printSelection);assert.deepEqual(params.connections,c.connections);
 const shuffled=Object.fromEntries(Object.entries(p).reverse().map(([k,v])=>[k,typeof v==='number'?v*(1+1e-14):v]));assert.equal(encodeConfig(configOf(shuffled)),code);
 ids.add(configId(cfg));}
assert.equal(ids.size,cases.length+1);
// Distinct settings give distinct IDs (a sweep of single changes).
const sweep=new Set();for(const d of [260,300,400,500,600,800,1000,1200])for(const fd of [.3,.42,.6])for(const t of [2,2.4,2.8])sweep.add(configId(configOf({...defaults,diameter:d,fd,thickness:t})));assert.equal(sweep.size,72);

// A hand arrangement round-trips through a real build: same rows, same code, same ID.
{const m=build({...defaults,diameter:600,bedX:300,bedY:300,bedZ:330,mountMode:1}),next=movePlacement(m.plates,'hub:1',1,0,0,m.p);assert(next,'the hub moves to plate 1');m.plates=next;m.manualPacking=true;
 const cfg=modelConfig(m),code=encodeConfig(cfg);assert.equal(cfg.pl.length,plateRows(m.plates).length);
 const {params,rows}=resolveConfig(readConfig(code)),again=build(params);again.plates=manualPlates(again.parts.filter(p=>p.printIncluded!==false),again.p,rows);again.manualPacking=true;
 assert.equal(encodeConfig(modelConfig(again)),code);assert.deepEqual(plateRows(again.plates).map(r=>[r.copy,r.plate]),plateRows(m.plates).map(r=>[r.copy,r.plate]));
 for(const[a,b]of plateRows(again.plates).map((r,i)=>[r,plateRows(m.plates)[i]]))assert(Math.abs(a.x-b.x)<1e-6&&Math.abs(a.y-b.y)<1e-6&&a.yaw===b.yaw);
 // the exports carry the same code
 const info=configInfo(m);assert.equal(info.code,code);assert.deepEqual(manifest(m).configuration,info);assert(scadSource(m).includes(code));
 // a packer-arranged model has no rows
 assert.equal(handRows(build({...defaults})),null);}

// Accepted inputs: bare code, link, code split over lines with chatter around it, exported file, kit parameters.json.
{const cfg=configOf({...defaults,diameter:700,bedX:300,bedY:300,bedZ:330,mountMode:1,printSelection:ALL}),code=encodeConfig(cfg),id=configId(cfg);
 for(const text of [code,'https://petal.example/#'+code,`Try this: ${code.slice(0,9)}\n  ${code.slice(9)}  thanks`,JSON.stringify({petal_configuration:1,code}),JSON.stringify({configuration:{id,code},parameters:{diameter:1}})])assert.equal(configId(readConfig(text)),id,text);
 // an older kit's parameters.json has no code: its parameters rebuild the settings, without a hand arrangement
 const legacy=readConfig(JSON.stringify({generator:'PETAL 5.2 prototype',parameters:Object.fromEntries(Object.entries({...defaults,diameter:700,bedX:300,bedY:300,bedZ:330,mountMode:1}).filter(([k])=>!['rimBand','rimDepth','ribs','ribHeight','ribPitch','rimLip','ringHeight','rimSupport','supportGap','supportWall','tinePitch','tineWidth'].includes(k))),print_selection:ALL,manual_packing:true}));
 assert.equal(legacy.app,'5.2');assert.equal(legacy.noArrangement,true);assert.equal(resolveConfig(legacy).params.rimBand,0,'an older kit had no rim band');assert.equal(resolveConfig(legacy).params.rimSupport,0,'or rim support');}
// Settings this version lacks are reported and skipped; settings out of range are refused.
{const cfg=configOf({...defaults,diameter:500,printSelection:ALL});cfg.p.futureSetting=3;const r=resolveConfig(roundTrip(cfg));assert.deepEqual(r.unknown,['futureSetting']);assert.equal(r.params.diameter,500);
 const bad=configOf({...defaults,printSelection:ALL});bad.p.diameter=5000;assert.throws(()=>resolveConfig(roundTrip(bad)),/diameter must be between/);
 const legs=configOf({...defaults,printSelection:ALL});legs.p.feedMode=1;legs.p.sectors=10;assert.throws(()=>resolveConfig(roundTrip(legs)),/6, 8, 12 or 16 petals/);}
for(const[text,message]of [['hello',/Paste a PETAL code/],['P1.AAAA',/damaged/],['P1.'+Buffer.from('not deflate').toString('base64url'),/damaged/],['P2.abcdefghijkl',/newer version/],['{"name":"x"}',/not a PETAL configuration/],['{oops',/not a PETAL configuration/]])assert.throws(()=>readConfig(text),message,text);

// Frozen format: this code was made with P1 on 2026-10-09. It must keep loading as exactly this design.
{const fixture='P1.wyGM022YnsEMBnjKxAgX5JQKDEFEJjC00EFOb0Z6FrW1AA',cfg=readConfig(fixture),{params}=resolveConfig(cfg);
 assert.equal(configId(cfg),'QABF-VBBF');
 for(const[k,v]of Object.entries({diameter:1000,bedX:325,bedY:320,bedZ:325,feedMode:1,mountMode:1,mountSides:2,mountArcLock:1,thickness:2.8,plateGap:18,fd:.42,feedLegs:0,sectors:0,rows:0,seamJoint:0,packPlates:1,rimBand:0,ribs:0,rimLip:0,rimSupport:0}))assert.equal(params[k],v,k);}
// The app's defaults need no code in the address bar, and they include the rim band, so their code carries it.
assert(isBaseline(configOf({...defaults,printSelection:ALL})));{const back=resolveConfig(readConfig(encodeConfig(configOf({...defaults,printSelection:ALL})))).params;assert.equal(back.rimBand,defaults.rimBand);assert.equal(back.rimSupport,defaults.rimSupport);}
// rim support settings round-trip
{const cfg=configOf({...defaults,rimSupport:1,supportWall:4.5,supportGap:.25,tinePitch:6,tineWidth:.8,printSelection:ALL}),{params}=resolveConfig(roundTrip(cfg));for(const[k,v]of Object.entries({rimSupport:1,supportWall:4.5,supportGap:.25,tinePitch:6,tineWidth:.8}))assert.equal(params[k],v,k);}
console.log('PASS configuration codes: exact round trips with joint overrides and hand-placed plates, stable IDs, links, files, kit manifests, refusals and the frozen P1 fixture');
