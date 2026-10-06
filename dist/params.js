// Parameters, limits and validation. Pure: no geometry kernel, so the page can use it without loading WASM.
import {validateConnections} from './connections.js';
import {INTERFACE_REVISION} from './interface.js';
export const defaults={segmentGoal:0,rootBolt:4,mountBolt:4,jointBolt:4,clampBolt:8,standBolt:5,legBolt:0,feedMode:0,feedLegs:3,rodDiameter:6.35,rodClearance:.35,feedPayload:100,phaseUnits:0,autoSecondary:1,secondaryWaves:4,phaseOffset:0,secondaryPosition:.82,backFocus:-20,frequencyGHz:0,diameter:400,fd:.42,thickness:2.4,bedX:220,bedY:220,bedZ:250,margin:8,gap:.4,resolution:5,sectors:0,rows:0,rearStyle:0,facetSize:20,packPlates:1,staggerRings:1,seamJoint:0,clipFit:.05,clipDetent:.3,clipMaterial:0,clipAllowableStrain:1.5,mountMode:0,mountBase:1,mountArcLock:0,azimuth:0,elevation:30,seamBolt:3,hubFlat:1,rootThrough:0,mountThrough:1,insertDiameter:5.6,collectorDiameter:30,collectorAngle:25,bowlDiameter:90,mastDiameter:16,legDiameter:20,legSplay:20};
export const limits={segmentGoal:[0,1],rootBolt:[3,5],mountBolt:[3,5],jointBolt:[3,5],clampBolt:[6,10],standBolt:[4,6],legBolt:[0,6],feedMode:[0,3],feedLegs:[3,4],rodDiameter:[0,12.7],rodClearance:[.15,.7],feedPayload:[1,2000],phaseUnits:[0,1],autoSecondary:[0,1],secondaryWaves:[2,10],phaseOffset:[-150,150],secondaryPosition:[.6,.95],backFocus:[-200,-5],frequencyGHz:[0,100],diameter:[260,1200],fd:[.25,.8],thickness:[1.6,6],bedX:[140,1000],bedY:[140,1000],bedZ:[60,1000],margin:[2,20],gap:[.2,1],resolution:[2,10],sectors:[0,24],rows:[0,8],rearStyle:[0,1],facetSize:[10,30],packPlates:[0,1],staggerRings:[0,1],seamJoint:[0,3],clipFit:[0,.1],clipDetent:[.3,2],clipMaterial:[0,2],clipAllowableStrain:[.2,10],mountMode:[0,1],mountBase:[0,2],mountArcLock:[0,1],azimuth:[-180,180],elevation:[-10,100],seamBolt:[3,4],hubFlat:[0,1],rootThrough:[0,1],mountThrough:[0,1],insertDiameter:[5.2,6],collectorDiameter:[8,120],collectorAngle:[10,60],bowlDiameter:[56,400],mastDiameter:[6,25.4],legDiameter:[8,25.4],legSplay:[10,30]};
// Feed layouts: 0 none, 1 prime focus, 2 Cassegrain (convex secondary), 3 Gregorian collector (concave bowl + insert mast).
// Mount bases: 0 none (yoke screws to a stand), 1 printed azimuth base, 2 base with three tapered leg sockets.
// Snap clip: a solid trapezoid block with a channel that snaps straight up over both flange walls from behind.
// In clip modes the flange wall is a uniform 5 mm, so each petal presents one flat face. Each clip jaw carries a
// cylindrical bump straight across its full width; it clicks into a matching cylindrical groove in each wall, cut
// slightly wider than the clip. Two low vertical ridges on each wall either side of the clip stop it sliding along
// the seam or twisting. Detent depth sizes both the bump and the groove.
// Clip spots are tilted to follow the shell along the seam, so the clip's top sits just under the shell.
// Profile: X across the seam, Y toward the shell, Y = 0 at the station level (flange bottom near -7, shell at 7).
export const CLIP={width:10,detentR:2.6,detentY:3,tip:2.5,root:4,top:6.75,floor:-9.5,bottom:-14.5,fillet:1.5,clear:.4,ridge:{top:1,h:1,maxRamp:4},wall:5};
export const clipOuter=(a0,Y)=>a0+CLIP.root+(CLIP.tip-CLIP.root)*(Y-CLIP.floor)/(CLIP.top-CLIP.floor);
// Bolt height in the station frame: with both bolts and clips, the bolt sits low and the detent high so the seat stays flat.
export const boltY=p=>p.seamJoint>=2?-2.2:0;
// Seam fastening modes: 0 bolts, 1 snap clips, 2 both (hole and clip window), 3 seam levers (same stations as 2).
export const usesClips=p=>p.seamJoint===1||p.seamJoint===2;
export const usesLevers=p=>p.seamJoint===3;
export const seamHoles=p=>p.seamJoint!==1;
// Seam bolt size: M3 (Ø3.4 bore, Ø10 seats) or M4 (Ø4.5 bore, Ø11 seats, larger pads).
export const SEAM_BOLT={3:{holeR:1.7,seatR:5,padW:6,padH:5,screw:'M3 × 16 socket head',nut:'M3 / 5.5 mm AF',washer:'M3 / 0.5 mm',smallWasher:'M3 / 6 mm OD (DIN 433)',driver:5.5},4:{holeR:2.25,seatR:5.5,padW:6.5,padH:5.5,screw:'M4 × 20 socket head',nut:'M4 / 7 mm AF',washer:'M4 / 0.8 mm, 9 mm OD (DIN 125)',smallWasher:'M4 / 8 mm OD (DIN 433)',driver:7}};
export const seamBolt=p=>SEAM_BOLT[p.seamBolt]||SEAM_BOLT[3];
// Peak jaw strain while the bump rides over the wall: tapered cantilever from the channel floor, loaded at the detent.
export function clipStrain(p){const C=CLIP,a0=5-p.clipFit,yl=C.detentY,N=400;let d=0,em=0;const h=Y=>clipOuter(a0,Y)-a0;
 for(let i=0;i<N;i++){const Y=C.floor+(yl-C.floor)*(i+.5)/N,x=yl-Y,H=h(Y);d+=x*x/(H**3/12)*(yl-C.floor)/N;}
 const P=(p.clipDetent+p.clipFit)/d;for(let i=0;i<=N;i++){const Y=C.floor+(yl-C.floor)*i/N;em=Math.max(em,6*P*(yl-Y)/h(Y)**2);}return em;}
// Metric fasteners for the hub and mount: medium clearance holes, washer OD (ISO 7089), 90° countersink about 1 mm over
// the ISO 10642 head, heat-set insert pilot and longest short insert, hex nut and head (ISO 4032 / 4017) across flats
// and heights. Insert pilots scale with the "Insert pilot" setting, which is the M4 value.
export const FASTENER={
 3:{clear:3.4,washer:7,cs:7.7,pilot:4.2,insert:4,nutAF:5.5,nutH:2.4,headK:2},
 4:{clear:4.5,washer:9,cs:10,pilot:5.6,insert:6,nutAF:7,nutH:3.2,headK:2.8},
 5:{clear:5.5,washer:10,cs:12,pilot:6.4,insert:7,nutAF:8,nutH:4.7,headK:3.5},
 6:{clear:6.6,washer:12,cs:14.4,pilot:8,insert:8,nutAF:10,nutH:5.2,headK:4},
 8:{clear:8.5,washer:16,nutAF:13,nutH:6.8,headK:5.3},
 10:{clear:10.5,washer:20,nutAF:16,nutH:8.4,headK:6.4}};
export const insertPilot=(p,m)=>+(FASTENER[m].pilot+(p.insertDiameter-FASTENER[4].pilot)).toFixed(2);
// Shortest stock length (ISO 4762 / 10642 / 4017 series) at least `need` long.
export const stockLength=need=>[6,8,10,12,14,16,18,20,22,25,30,35,40,45,50,55,60,65,70,75,80,90,100].find(l=>l>=need-1e-9)??Math.ceil(need/10)*10;
export const stockDown=max=>[6,8,10,12,14,16,18,20,22,25,30,35,40,45,50].filter(l=>l<=max+1e-9).pop();
export const JOINT={revision:INTERFACE_REVISION,wall:3,depth:14,boss:5,insertDepth:7,insertMaxLength:6};
export function validate(p){validateConnections(p.connections);for(const[k,[a,b]]of Object.entries(limits))if(!Number.isFinite(p[k])||p[k]<a||p[k]>b)throw Error(`${k} must be between ${a} and ${b}.`);if(p.seamBolt!==3&&p.seamBolt!==4)throw Error('seamBolt must be 3 (M3) or 4 (M4).');if(![6,8,10].includes(p.clampBolt))throw Error('clampBolt must be 6, 8 or 10 (M6, M8 or M10).');if(![0,3,4,5,6].includes(p.legBolt))throw Error('legBolt must be 0 (automatic) or 3 to 6.');for(const k of ['feedMode','feedLegs','phaseUnits','autoSecondary','sectors','rows','rearStyle','packPlates','staggerRings','seamJoint','clipMaterial','mountMode','mountBase','mountArcLock','seamBolt','hubFlat','rootThrough','mountThrough','segmentGoal','rootBolt','mountBolt','jointBolt','clampBolt','standBolt','legBolt'])if(!Number.isInteger(p[k]))throw Error(k+' must be a whole-number option.');if(p.sectors&&(p.sectors<6||p.sectors%2))throw Error('Choose automatic or an even petal count from 6 to 24.');if(p.rodDiameter!==0&&p.rodDiameter<2)throw Error('Rod diameter must be 2–12.7 mm, or 0 for automatic stock sizing.');if(p.feedMode&&p.sectors&&p.sectors%p.feedLegs)throw Error('Petal count must be divisible by the rod count.');if(p.mountMode&&(p.seamJoint||[...Object.values(p.connections?.families||{}),...Object.values(p.connections?.joints||{})].some(c=>c.seamJoint))&&p.elevation< -7.5)throw Error('Clip mount preview requires elevation at least -7.5°.');if((usesClips(p)||[...Object.values(p.connections?.families||{}),...Object.values(p.connections?.joints||{})].some(c=>usesClips(c)))&&clipStrain(p)*100>p.clipAllowableStrain)throw Error(`Clip strain ${(100*clipStrain(p)).toFixed(2)}% exceeds your ${p.clipAllowableStrain}% budget. Reduce detent/squeeze or supply a qualified material budget.`);return p;}
