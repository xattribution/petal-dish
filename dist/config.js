// Configuration codes. A code holds every setting that differs from a fixed baseline, the joint overrides, the print
// selection and any hand arrangement of the plates, as deflated JSON in URL-safe base64 behind the tag "P1.". Its ID is
// a hash of the same content, so two screens that show the same ID have the same design.
// Format P1 is frozen: no entry of BASE and nothing in DICT may change, or old codes would load as different designs.
// A new setting is added to BASE only with the value that reproduces the designs made before it existed (its feature
// off); a setting missing from BASE is always carried in the code. A new dictionary needs a new tag (P2). Pure (no DOM, no geometry kernel): the page, the engine and Node all use it.
import {deflateSync,inflateSync} from 'fflate';
import {defaults,validate} from './params.js';
import {VERSION} from './version.js';
export const TAG='P1.';
const BASE=Object.freeze({segmentGoal:0,rootBolt:4,mountBolt:4,jointBolt:4,clampBolt:8,standBolt:5,legBolt:0,rootSeat:0,mountSeat:0,headSeat:0,nutSeat:0,standSeat:0,hubFloat:0,feedMode:0,feedLegs:0,rodDiameter:6.35,rodClearance:.35,feedPayload:100,phaseUnits:0,autoSecondary:1,secondaryWaves:4,phaseOffset:0,secondaryPosition:.82,backFocus:-20,frequencyGHz:0,diameter:400,fd:.42,thickness:2.4,bedX:220,bedY:220,bedZ:250,margin:8,gap:.4,resolution:5,sectors:0,rows:0,rearStyle:0,facetSize:20,packPlates:1,staggerRings:1,plateGap:6,seamJoint:0,clipFit:.05,clipDetent:.3,clipMaterial:0,clipAllowableStrain:1.5,mountMode:0,mountBase:1,mountArcLock:0,mountSides:0,azimuth:0,elevation:30,seamBolt:3,hubFlat:1,rootThrough:0,mountThrough:1,insertDiameter:5.6,collectorDiameter:30,collectorAngle:25,bowlDiameter:90,mastDiameter:16,legDiameter:20,legSplay:20,
 // added 2026-10-09 with the values that reproduce earlier designs (no band, no ribs)
 rimBand:0,rimDepth:14,ribs:0,ribHeight:2.5,ribPitch:50});
// Preset deflate dictionary: the text codes are made of, most common last. It roughly halves a typical code.
const DICT=new TextEncoder().encode('"segmentGoal":"rootBolt":"mountBolt":"jointBolt":"clampBolt":"standBolt":"legBolt":"rootSeat":"mountSeat":"headSeat":"nutSeat":"standSeat":"hubFloat":"rodClearance":"feedPayload":"phaseUnits":"autoSecondary":"secondaryWaves":"phaseOffset":"secondaryPosition":"backFocus":"collectorDiameter":"collectorAngle":"bowlDiameter":"mastDiameter":"legDiameter":"legSplay":"insertDiameter":"clipFit":"clipDetent":"clipMaterial":"clipAllowableStrain":"margin":"gap":"resolution":"rearStyle":"facetSize":"packPlates":"staggerRings":"plateGap":"seamBolt":"hubFlat":"rootThrough":"mountThrough":"mountArcLock":"mountSides":"mountBase":"azimuth":"elevation":"frequencyGHz":"rodDiameter":"feedLegs":"thickness":"sectors":"rows":"seamJoint":"c":{"families":{"radial":{"seamBolt":3,"seamJoint":"joints":{"roots":{"mounts":{"ring":{"pl":[["petal-1-mount:1",1,["petal-2:1",1,["hub:1",1,["mount-yoke:1",["feed-carrier:1",["clip:1",["seam-lever-","s":["clip","feed","lever","mount"],"fd":0.42,"feedMode":1,"mountMode":1,"diameter":600,"diameter":1000,"bedX":325,"bedY":320,"bedZ":325},"bedX":300,"bedY":300,"bedZ":330},"p":{"bedX":300,"bedY":300,"bedZ":330,"diameter":{"a":"5.3","p":{"bedX":');
const b64=bytes=>{let s='';for(let i=0;i<bytes.length;i++)s+=String.fromCharCode(bytes[i]);return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');};
const unb64=text=>{const s=atob(text.replace(/-/g,'+').replace(/_/g,'/')),out=new Uint8Array(s.length);for(let i=0;i<s.length;i++)out[i]=s.charCodeAt(i);return out;};
// Canonical JSON: sorted keys, so equal content always gives the same text, code and ID.
const canon=x=>Array.isArray(x)?'['+x.map(canon).join(',')+']':x&&typeof x==='object'?'{'+Object.keys(x).filter(k=>x[k]!==undefined).sort().map(k=>JSON.stringify(k)+':'+canon(x[k])).join(',')+'}':JSON.stringify(x);
const norm=v=>typeof v==='number'?+v.toPrecision(12):v;
const mm=v=>Math.round(v*1000)/1000+0;
const PRINT_KEYS=['feed','mount','clip','lever'];
// Plate rows (as plateRows gives them) of a model's hand arrangement, or null when the packer arranged the plates.
export const handRows=m=>m.manualPacking&&m.p.packPlates?m.plates.flatMap((plate,i)=>plate.placements.map(x=>({copy:x.copy,plate:i+1,x:x.x,y:x.y,yaw:x.yaw}))):null;
// The content of a code: settings that differ from BASE (and any setting BASE predates), joint overrides, accessories
// left off the plates, and the hand arrangement (positions to 0.001 mm, which the packing grid snaps back exactly).
export function configOf(p,rows=null){
 const q={};for(const k of Object.keys(defaults)){const v=norm(p[k]);if(!(k in BASE)||v!==BASE[k])q[k]=v;}
 const cfg={p:q};
 if(p.connections&&Object.keys(p.connections).length)cfg.c=JSON.parse(canon(p.connections));
 const off=Object.entries(p.printSelection||{}).filter(([,v])=>v===false).map(([k])=>k).sort();if(off.length)cfg.s=off;
 if(rows)cfg.pl=rows.map(r=>[r.copy,r.plate,mm(r.x),mm(r.y),mm(r.yaw)]);
 return cfg;}
export const modelConfig=m=>configOf(m.p,handRows(m));
// The app's defaults (the page then needs no code in its address).
export const isBaseline=cfg=>canon({c:cfg.c,p:cfg.p,pl:cfg.pl,s:cfg.s})===canon(configOf({...defaults,printSelection:Object.fromEntries(PRINT_KEYS.map(k=>[k,true]))}));
// ID: 40 bits of a 53-bit hash of the content (not the app version), as two groups of four Crockford base-32 digits.
const ALPHABET='0123456789ABCDEFGHJKMNPQRSTVWXYZ';
function cyrb53(text){let h1=0xdeadbeef,h2=0x41c6ce57;for(let i=0;i<text.length;i++){const ch=text.charCodeAt(i);h1=Math.imul(h1^ch,2654435761);h2=Math.imul(h2^ch,1597334677);}
 h1=Math.imul(h1^(h1>>>16),2246822507);h1^=Math.imul(h2^(h2>>>13),3266489909);h2=Math.imul(h2^(h2>>>16),2246822507);h2^=Math.imul(h1^(h1>>>13),3266489909);return 4294967296*(2097151&h2)+(h1>>>0);}
export function configId(cfg){let n=cyrb53(canon({c:cfg.c,p:cfg.p,pl:cfg.pl,s:cfg.s})),id='';for(let i=0;i<8;i++){id=ALPHABET[n%32]+id;n=Math.floor(n/32);}return id.slice(0,4)+'-'+id.slice(4);}
export const encodeConfig=cfg=>TAG+b64(deflateSync(new TextEncoder().encode(canon({a:VERSION,c:cfg.c,p:cfg.p,pl:cfg.pl,s:cfg.s})),{level:9,dictionary:DICT}));
// What the PDF, manifest and SCAD snapshot record for a model.
export function configInfo(m){const cfg=modelConfig(m);return{id:configId(cfg),code:encodeConfig(cfg),app:VERSION};}
const damaged=()=>Error('This code is incomplete or damaged. Copy all of it and try again.');
function decodeCode(code){
 if(code.length>65536)throw damaged();let cfg;
 try{cfg=JSON.parse(new TextDecoder().decode(inflateSync(unb64(code.slice(TAG.length)),{dictionary:DICT})));}catch{throw damaged();}
 const obj=x=>x&&typeof x==='object'&&!Array.isArray(x);
 if(!obj(cfg)||!obj(cfg.p)||!Object.values(cfg.p).every(Number.isFinite)||cfg.c!==undefined&&!obj(cfg.c)||cfg.s!==undefined&&!(Array.isArray(cfg.s)&&cfg.s.every(x=>typeof x==='string'))
  ||cfg.pl!==undefined&&!(Array.isArray(cfg.pl)&&cfg.pl.every(r=>Array.isArray(r)&&r.length===5&&typeof r[0]==='string'&&r.slice(1).every(Number.isFinite))))throw damaged();
 return{p:cfg.p,...(cfg.c?{c:cfg.c}:{}),...(cfg.s?{s:cfg.s}:{}),...(cfg.pl?{pl:cfg.pl}:{}),app:typeof cfg.a==='string'?cfg.a:null};}
// The configuration in a pasted code, a link that carries one, an exported configuration file, or a kit's
// parameters.json (its code when it has one; otherwise its parameters, which hold no hand arrangement; a setting an
// older kit lacks takes its BASE value, which reproduces that kit).
export function readConfig(input){
 const text=String(input??'').trim();
 if(text.startsWith('{')){let j;try{j=JSON.parse(text);}catch{throw Error('This file is not a PETAL configuration.');}
  const code=typeof j.code==='string'?j.code:typeof j.configuration?.code==='string'?j.configuration.code:null;
  if(code)return readConfig(code);
  if(j.parameters&&typeof j.parameters==='object'){const cfg=configOf({...defaults,...BASE,...j.parameters,printSelection:j.print_selection||j.parameters.printSelection});return{...cfg,app:typeof j.generator==='string'?j.generator.replace(/^PETAL /,'').split(' ')[0]:null,noArrangement:Boolean(j.manual_packing)};}
  throw Error('This file is not a PETAL configuration.');}
 // A code copied out of a PDF or a chat may arrive split over lines: retry with the whitespace taken out.
 const match=text.match(/P1\.[A-Za-z0-9_-]+/);
 if(match){const tokens=text.slice(text.indexOf(match[0])+TAG.length).split(/\s+/);let joined=TAG,first=null;
  for(const token of tokens.slice(0,64)){const part=token.match(/^[A-Za-z0-9_-]*/)[0];joined+=part;try{return decodeCode(joined);}catch(e){first??=e;}if(part!==token)break;}
  throw first;}
 if(/\bP(?:[2-9]|\d\d+)\.[A-Za-z0-9_-]{8,}/.test(text))throw Error('This code comes from a newer version of PETAL.');
 throw Error('Paste a PETAL code, a link with one, or a configuration file.');}
// Settings to build from a configuration (validated), its hand arrangement, and any settings this version lacks.
export function resolveConfig(cfg){
 const params={...defaults};for(const k in BASE)if(k in defaults)params[k]=BASE[k];
 const unknown=[];for(const[k,v]of Object.entries(cfg.p))if(k in defaults)params[k]=v;else unknown.push(k);
 if(cfg.c)params.connections=structuredClone(cfg.c);
 params.printSelection=Object.fromEntries(PRINT_KEYS.map(k=>[k,!(cfg.s||[]).includes(k)]));
 validate(params);
 return{params,rows:cfg.pl?cfg.pl.map(([copy,plate,x,y,yaw])=>({copy,plate,x,y,yaw})):null,unknown};}
