import {connectionCatalog,CONNECTION_METHODS} from './connections.js';
import {scenePoint,dishPoint} from './scene.js';
import {INTERFACE_REVISION} from './interface.js';
import {jsPDF} from 'jspdf';
import {volume} from './mesh.js';
import {feedHardware} from './feed.js';
import {VERSION,BUILD_ID} from './version.js';

// Vector views use the exported triangles. No WebGL, screenshots or network.
const dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0);
const clean=s=>String(s).replace(/[–—−]/g,'-').replace(/×/g,'x').replace(/Ø/g,'dia. ').replace(/°/g,' deg').replace(/λ/g,'lambda').replace(/δ/g,'delta').replace(/ν/g,'nu').replace(/³/g,'3').replace(/²/g,'2').replace(/[’‘]/g,"'").replace(/[“”]/g,'"').replace(/→/g,' -> ').replace(/[≤≥]/g,c=>c==='≤'?'<=':'>=').replace(/[^\x20-\x7e\n]/g,' ');
const colors={panel:[105,147,174],hub:[194,143,67],feed:[74,144,119],mount:[85,115,150],clip:[219,142,65],lever:[196,86,72]};
// Hidden-surface pass: rasterize every front-facing triangle into a depth buffer at 3 samples per point and keep only
// triangles that are nearest at some sample. Fully hidden structure never reaches the PDF, which keeps large dishes
// small and fast; the painter's order of what remains is unchanged.
function visible(triangles,pt,w,h,x0,y0){
 const S=3,W=Math.ceil(w*S),H=Math.ceil(h*S),depth=new Float32Array(W*H).fill(-Infinity),owner=new Int32Array(W*H).fill(-1);
 triangles.forEach((t,i)=>{const p=t.v.map(v=>{const q=pt(v);return[(q[0]-x0)*S,(q[1]-y0)*S,v[2]];}),[a,b,c]=p,area=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);if(Math.abs(area)<1e-12)return;
  const x1=Math.max(0,Math.floor(Math.min(a[0],b[0],c[0]))),x2=Math.min(W-1,Math.ceil(Math.max(a[0],b[0],c[0]))),y1=Math.max(0,Math.floor(Math.min(a[1],b[1],c[1]))),y2=Math.min(H-1,Math.ceil(Math.max(a[1],b[1],c[1])));
  let hit=false;for(let py=y1;py<=y2;py++)for(let px=x1;px<=x2;px++){const X=px+.5,Y=py+.5,l1=((b[0]-X)*(c[1]-Y)-(b[1]-Y)*(c[0]-X))/area,l2=((c[0]-X)*(a[1]-Y)-(c[1]-Y)*(a[0]-X))/area,l3=1-l1-l2;if(l1<-1e-9||l2<-1e-9||l3<-1e-9)continue;hit=true;const z=l1*a[2]+l2*b[2]+l3*c[2],k=py*W+px;if(z>=depth[k]){depth[k]=z;owner[k]=i;}}
  // A triangle smaller than one sample still marks the sample it sits on.
  if(!hit){const px=Math.min(W-1,Math.max(0,Math.floor((a[0]+b[0]+c[0])/3))),py=Math.min(H-1,Math.max(0,Math.floor((a[1]+b[1]+c[1])/3))),k=py*W+px,z=(a[2]+b[2]+c[2])/3;if(z>=depth[k]){depth[k]=z;owner[k]=i;}}});
 const keep=new Uint8Array(triangles.length);for(const i of owner)if(i>=0)keep[i]=1;return triangles.filter((_,i)=>keep[i]);
}
export function drawModel(doc,m,{x,y,w,h,exploded=false,rear=false,part=null,labels=false}){
 const az=-Math.PI/3,el=(rear?-38:30)*Math.PI/180,eye=[Math.cos(az)*Math.cos(el),Math.sin(az)*Math.cos(el),Math.sin(el)],right=[-Math.sin(az),Math.cos(az),0],up=[-Math.cos(az)*Math.sin(el),-Math.sin(az)*Math.sin(el),Math.cos(el)];
 const project=v=>[dot(v,right),-dot(v,up),dot(v,eye)],triangles=[],points=[],centers=new Map();
 const instances=part?[{part,a:0}]:m.instances;
 for(const inst of instances){const p=inst.part,mesh=part?p.output:p.mesh;
  const vs=mesh.v.map(v=>project(part?v:scenePoint(m,inst,v,exploded?1:0)));for(const q of vs)points.push(q);
  if(!centers.has(p.id)){const used=[...new Set(mesh.f.flat())];centers.set(p.id,used.reduce((a,i)=>a.map((v,k)=>v+vs[i][k]/used.length),[0,0,0]));}
  for(const face of mesh.f){const[a,b,c]=face.map(i=>vs[i]),u=b.map((v,k)=>v-a[k]),v=c.map((v,k)=>v-a[k]),n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]],l=Math.hypot(...n);if(n[2]>=-1e-9||l<1e-9)continue;const light=.62+.38*Math.abs((n[0]*.25+n[1]*-.4+n[2]*-.88)/l),base=colors[p.kind]||colors.panel;triangles.push({v:[a,b,c],z:(a[2]+b[2]+c[2])/3,color:base.map(v=>Math.round(Math.min(255,v*light)))});}
 }
 const lo=[0,1].map(k=>points.reduce((a,v)=>Math.min(a,v[k]),Infinity)),hi=[0,1].map(k=>points.reduce((a,v)=>Math.max(a,v[k]),-Infinity)),scale=Math.min(w/(hi[0]-lo[0]),h/(hi[1]-lo[1]))*.93,pt=v=>[x+w/2+(v[0]-(lo[0]+hi[0])/2)*scale,y+h/2+(v[1]-(lo[1]+hi[1])/2)*scale];
 doc.setLineWidth(.3);triangles.sort((a,b)=>a.z-b.z);for(const t of visible(triangles,pt,w,h,x,y)){doc.setFillColor(...t.color);doc.setDrawColor(...t.color);doc.triangle(...t.v.flatMap(pt),'FD');}
 if(!part&&!exploded&&m.feed){doc.setDrawColor(49,85,69);doc.setLineWidth(1.3);for(const l of m.feed.legs)doc.line(...pt(project(dishPoint(m,l.lower))),...pt(project(dishPoint(m,l.upper))));}
 if(labels){const entries=[...centers.entries()].slice(0,12);entries.forEach(([id,v],i)=>{const target=pt(v),lx=x+w-12,ly=y+14+i*Math.min(25,(h-28)/Math.max(1,entries.length-1));doc.setDrawColor(80,91,100);doc.setLineWidth(.6);doc.line(target[0],target[1],lx-10,ly);doc.setFillColor(30,47,61);doc.circle(lx,ly,9,'F');doc.setTextColor(255);doc.setFontSize(9);doc.text(String(m.parts.findIndex(p=>p.id===id)+1),lx,ly+3,{align:'center'});});}
}
const hull=pts=>{const p=[...pts].sort((a,b)=>a[0]-b[0]||a[1]-b[1]);if(p.length<3)return p;const cross=(o,a,b)=>(a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0]),lower=[],upper=[];for(const q of p){while(lower.length>1&&cross(lower.at(-2),lower.at(-1),q)<=0)lower.pop();lower.push(q);}for(const q of [...p].reverse()){while(upper.length>1&&cross(upper.at(-2),upper.at(-1),q)<=0)upper.pop();upper.push(q);}return lower.slice(0,-1).concat(upper.slice(0,-1));};
// Top-down bed diagrams: every copy's footprint (convex outline of its exported mesh) at its packed position, numbered
// by the part key. Two plates per row.
function platePages(doc,m,page,text,getY,setY,B){
 page('Print plates');text(`${m.manualPacking?'Manual arrangement':'Automatic packing'}. Print the packed plates OR the individual part files, never both. Numbers match the part key; PLATES.csv has every copy's X / Y offset and rotation. Empty plate numbers are reserved and have no STL.`,10);
 const L=44,colW=243,gap=20,bw=m.p.bedX,bh=m.p.bedY,scale=Math.min(colW/bw,170/bh),cellH=bh*scale+34,outline=new Map();
 const shape=(part,yaw)=>{const key=part.id+':'+yaw;if(!outline.has(key)){const c=Math.cos(yaw*Math.PI/180),s=Math.sin(yaw*Math.PI/180),step=Math.max(1,Math.floor(part.output.v.length/1500));const pts=[];for(let i=0;i<part.output.v.length;i+=step){const v=part.output.v[i];pts.push([c*v[0]-s*v[1],s*v[0]+c*v[1]]);}outline.set(key,hull(pts));}return outline.get(key);};
 let y=getY(),col=0;
 m.plates.forEach((plate,i)=>{if(col===0&&y+cellH>B){page('Print plates - continued');y=getY();}const x=L+col*(colW+gap),top=y+16,ox=x+bw*scale/2,oy=top+bh*scale/2;
  doc.setFont('helvetica','bold');doc.setFontSize(10);doc.setTextColor(24,68,93);doc.text(`Plate ${i+1}`,x,y+8);doc.setFont('helvetica','normal');doc.setFontSize(8);doc.setTextColor(90,105,116);doc.text(`${plate.placements.length} ${plate.placements.length===1?'copy':'copies'}`,x+bw*scale,y+8,{align:'right'});
  doc.setDrawColor(150,165,175);doc.setLineWidth(.6);doc.rect(x,top,bw*scale,bh*scale);doc.setDrawColor(205,214,220);doc.setLineWidth(.4);doc.setLineDashPattern([2,2],0);doc.rect(x+m.p.margin*scale,top+m.p.margin*scale,(bw-2*m.p.margin)*scale,(bh-2*m.p.margin)*scale);doc.setLineDashPattern([],0);
  for(const q of plate.placements){const poly=shape(q.part,q.yaw).map(([u,v])=>[ox+(u+q.x)*scale,oy-(v+q.y)*scale]);if(poly.length<3)continue;const color=colors[q.part.kind]||colors.panel;doc.setFillColor(...color.map(c=>Math.round(c+(255-c)*.35)));doc.setDrawColor(...color);doc.setLineWidth(.5);doc.lines(poly.slice(1).map((pt,k)=>[pt[0]-poly[k][0],pt[1]-poly[k][1]]),poly[0][0],poly[0][1],[1,1],'FD',true);
   const cx=poly.reduce((a,p)=>a+p[0],0)/poly.length,cy=poly.reduce((a,p)=>a+p[1],0)/poly.length,label=String(m.parts.indexOf(q.part)+1);doc.setFontSize(7);doc.setTextColor(20,32,40);doc.text(label,cx,cy+2.4,{align:'center'});}
  col=(col+1)%2;if(col===0)y+=cellH;});
 setY(col?y+cellH:y);
}
// Hardware: one row per item; quantity, item and specification on the first line, the note in small type below.
function hardwareTable(doc,items,page,getY,setY,B){
 const L=44,R=595.28-44,cols=[L,L+40,L+170];let y=getY();
 const head=()=>{doc.setFont('helvetica','bold');doc.setFontSize(8.5);doc.setTextColor(90,105,116);doc.text('QTY',cols[0],y);doc.text('ITEM',cols[1],y);doc.text('SPECIFICATION',cols[2],y);y+=6;doc.setDrawColor(205,214,220);doc.setLineWidth(.6);doc.line(L,y,R,y);y+=13;};
 head();
 for(const item of items){doc.setFontSize(10);const spec=doc.splitTextToSize(clean(item.spec),R-cols[2]),name=doc.splitTextToSize(clean(item.item),cols[2]-cols[1]-8);doc.setFontSize(8.5);const note=item.note?doc.splitTextToSize(clean(item.note),R-cols[1]):[];
  const need=Math.max(spec.length,name.length)*13+note.length*11+10;if(y+need>B){page('Hardware / quantities - continued');y=getY();head();}
  doc.setFont('helvetica','bold');doc.setFontSize(10);doc.setTextColor(24,43,57);doc.text(String(item.quantity),cols[0],y);doc.text(name,cols[1],y,{lineHeightFactor:1.3});doc.setFont('helvetica','normal');doc.setTextColor(40,53,62);doc.text(spec,cols[2],y,{lineHeightFactor:1.3});y+=Math.max(spec.length,name.length)*13;
  if(note.length){doc.setFontSize(8.5);doc.setTextColor(90,105,116);doc.text(note,cols[1],y-1,{lineHeightFactor:1.3});y+=note.length*11;}
  doc.setDrawColor(228,233,237);doc.setLineWidth(.4);doc.line(L,y,R,y);y+=13;}
 setY(y+6);
}
export function instructionPDF(m,{sections,reflector,hardware}){
 const doc=new jsPDF({unit:'pt',format:'a4',compress:true,putOnlyUsedFonts:true,floatPrecision:3});const W=595.28,H=841.89,L=44,R=W-44,B=H-49;let y=0;
 doc.setProperties({title:`PETAL ${m.p.diameter} mm - assembly instructions`,subject:`${VERSION} / ${BUILD_ID} / interface ${INTERFACE_REVISION}`,creator:'PETAL local model export',author:'PETAL'});
 let section='';
 const header=title=>{section=title.replace(/ - continued$/,'');doc.setFillColor(24,43,57);doc.rect(0,0,W,66,'F');doc.setTextColor(255);doc.setFont('helvetica','bold');doc.setFontSize(20);doc.text(title,L,34);doc.setFont('helvetica','normal');doc.setFontSize(9);doc.text(`PETAL ${VERSION}  |  ${m.p.diameter} mm  |  ${m.layout.n} petals x ${m.layout.rows} segment(s)  |  build ${BUILD_ID}`,L,52);y=91;};
 const page=title=>{doc.addPage();header(title);};
 const text=(s,size=10.5)=>{doc.setFont('helvetica','normal');doc.setFontSize(size);doc.setTextColor(40,53,62);const lines=doc.splitTextToSize(clean(s),R-L),step=size*1.4;for(const line of lines){if(y+step>B){page(section+' - continued');doc.setFont('helvetica','normal');doc.setFontSize(size);doc.setTextColor(40,53,62);}doc.text(line,L,y);y+=step;}y+=8;};
 const heading=s=>{if(y+55>B)page(section+' - continued');doc.setFont('helvetica','bold');doc.setFontSize(13);doc.setTextColor(24,68,93);doc.text(clean(s),L,y);y+=21;};
 const row=(a,b)=>{doc.setFont('helvetica','bold');doc.setFontSize(10);doc.setTextColor(34,52,64);doc.text(clean(a),L,y);doc.setFont('helvetica','normal');doc.text(clean(b),L+172,y);y+=21;};
 header('Your dish / assembly manual');
 text('Model-specific instructions and views. Print the fit strips before a complete article. Geometry is an unprinted prototype; these views do not qualify strength or RF performance.',10);
 drawModel(doc,m,{x:L,y:145,w:R-L,h:310});
 y=478;heading('Generated configuration');
 row('Dish / focal ratio',`${m.p.diameter} mm / f/D ${m.p.fd}`);row('Focus / dish depth',`${m.focal.toFixed(2)} mm / ${m.depth.toFixed(2)} mm`);row('Printed parts',`${m.parts.filter(p=>p.printIncluded!==false).reduce((s,p)=>s+p.qty,0)} copies; ${m.parts.filter(p=>p.printIncluded===false).length} part types reused`);row('Solid CAD volume',`${(m.parts.reduce((s,p)=>s+volume(p.mesh)*p.qty,0)/1000).toFixed(1)} cm3 (not sliced weight)`);row('Printer volume',`${m.p.bedX} x ${m.p.bedY} x ${m.p.bedZ} mm`);row('Rear / shell',`${m.p.rearStyle?'Faceted, '+m.p.facetSize+' mm pitch':'Curved'} / ${m.p.thickness} mm vertical thickness`);row('Shared beds',m.p.packPlates?`${m.plates.length}; print these OR individual quantities`:'Disabled; use individual part quantities');row('Frequency / support',`${m.p.frequencyGHz?m.p.frequencyGHz+' GHz':'Unspecified'} / ${m.feed?m.feed.mode:'No rod support'}`);
 y+=8;text('Units: mm. Focus is measured from the extrapolated parabola vertex. The image shows the printed parts and, when enabled, rod centerlines; screws and the user-supplied RF feed are not modeled.',9);
 page('Rear assembly / exploded view');drawModel(doc,m,{x:L,y:95,w:225,h:180,rear:true});doc.setFont('helvetica','normal');doc.setFontSize(9);doc.setTextColor(40,53,62);doc.text('Rear assembly',L,290);
 doc.setFont('helvetica','bold');doc.setFontSize(11);doc.text('Part key / total quantities',L+250,112);doc.setFont('helvetica','normal');doc.setFontSize(9);for(const[i,p]of m.parts.slice(0,12).entries())doc.text(clean(`${i+1}. ${p.qty} x ${p.id}`),L+250,132+i*14);
 if(m.parts.length>12)doc.text('Full part key continues on the following pages.',L+250,305,{maxWidth:260});
 doc.text('Exploded separation - not an insertion path',L,323);drawModel(doc,m,{x:L,y:336,w:R-L-26,h:290,exploded:true,rear:true,labels:true});y=665;
 text((m.p.staggerRings&&m.layout.rows>1?'Assemble the inner ring loosely, then add staggered outer segments individually. Align the butt flanges in a profile jig.':'Join radial segments first. Slide complete petals radially inward with screws loose, then fit the hub from behind.')+' The exploded offsets only separate parts for identification. Rods are omitted from the exploded view; use the cut schedule.',9);
 for(let i=0;i<m.parts.length;i++){if(i%3===0)page('Printed parts / exported orientation');const p=m.parts[i],top=94+(i%3)*222;drawModel(doc,m,{x:L,y:top,w:190,h:176,part:p});y=top+18;doc.setFont('helvetica','bold');doc.setFontSize(12);doc.setTextColor(24,68,93);doc.text(`${i+1}. ${clean(p.name)}`,L+204,y,{maxWidth:260});y+=42;doc.setFont('helvetica','normal');doc.setFontSize(10);const facts=[p.printIncluded===false?'Reuse compatible existing parts':`Print ${p.qty} ${p.qty===1?'copy':'copies'}${p.spares?` (${p.spares} spares)`:""}`,p.dim.map(v=>v.toFixed(1)).join(' x ')+' mm',`${(volume(p.mesh)/1000).toFixed(1)} cm3 per copy`,p.kind==='panel'?'Flat radial flange on bed':'Keep the exported orientation',p.printIncluded===false?'STL excluded from kit':`${p.id}_qty-${p.qty}.stl`];for(const line of facts){doc.text(doc.splitTextToSize(clean(line),260),L+204,y);y+=22;}doc.setDrawColor(220,227,232);doc.line(L,top+205,R,top+205);}
 if(m.plates.length)platePages(doc,m,page,text,()=>y,v=>{y=v;},B);
 page('Hardware / quantities');text('Quantities are generated for this dish. Lengths need a real fit check; external mount and feed adapter screws depend on your actual stack.',10);
 hardwareTable(doc,[...hardware,...feedHardware(m)],page,()=>y,v=>{y=v;},B);
 if(m.connectionCounts){page('Connection map / matching variants');text('Keep variant petals at these azimuths. Each seam lists its two matching pieces. CONNECTIONS.csv contains the same joint map.');for(const j of connectionCatalog(m)){const method=j.family==='root'||j.family==='mount'?(j.method?'through bolt':'heat-set insert'):CONNECTION_METHODS[j.method];heading(`${j.label} / M${j.size} / ${method}`);const pieces=j.parts||[{part:j.part||'hub',angle:j.angle||0}];text(pieces.map(p=>`${p.part} @ ${(p.angle*180/Math.PI).toFixed(2)} deg`).join(' + '),9);}}
 if(m.feed){page('Rod cuts / feed placement');heading(`${m.p.feedLegs} solid aluminum rods`);text(`Stock diameter ${m.feed.rodDiameter} mm. Cut ${m.feed.cutLength.toFixed(2)} mm each. Mark 18 mm nominal insertion at both ends; keep 16-19 mm engagement. Trial-fit long stock before final trimming.`);row('Rod elevation',`${m.feed.rodAngle.toFixed(2)} deg above the dish plane`);row('Puck lower face z',`${m.feed.carrierFace.toFixed(2)} mm`);row('Phase-center offset',`${m.feed.offset.toFixed(2)} mm`);for(const leg of m.feed.legs)text(`Rod ${leg.number}: ${leg.cutLength.toFixed(2)} mm; azimuth ${(leg.angle*180/Math.PI).toFixed(2)} deg; lower endpoint [${leg.lower.map(x=>x.toFixed(2)).join(', ')}]; upper endpoint [${leg.upper.map(x=>x.toFixed(2)).join(', ')}].`,10);if(m.feed.warnings.length){heading('Configuration warnings');m.feed.warnings.forEach(x=>text(x,10));}}
 page('Print / fit / assembly');for(const s of sections){if(s.title==='Rod support'){page('Feed-support instructions');for(const p of clean(s.body).split(/\n\s*\n/)){if(/^#+ /.test(p)){heading(p.replace(/^#+ /,''));}else text(p,10);}}else{heading(s.title);text(s.body);}}
 page('Reflective finish / RF setup');for(const p of clean(reflector).split(/\n\s*\n/)){if(/^# /.test(p))continue;if(/^## /.test(p))heading(p.replace(/^## /,''));else text(p,10);}
 page('Build record / inspection');text(`Keep this manual and parameters.json with the matching kit. Interface ${INTERFACE_REVISION}. Do not mix regenerated parts with changed dimensions or clearances.`);
 for(const label of ['Filament / batch / drying','Printer / nozzle / layer height','Clip or flat-seat fit / measured seam step','Root bolt or insert / screw engagement','Full-petal profile before assembly','Profile after tightening / at different elevations','Warm hold / repeat assembly results','Coating adhesion / continuity / final profile','Feed phase-center and RF comparison']){heading(label);doc.setDrawColor(197,209,218);doc.line(L,y+11,R,y+11);y+=37;}
 for(let i=1;i<=doc.getNumberOfPages();i++){doc.setPage(i);doc.setFont('helvetica','normal');doc.setFontSize(8);doc.setTextColor(90,105,116);doc.text(`PETAL ${VERSION} / ${m.p.diameter} mm / interface ${INTERFACE_REVISION} - keep with the matching kit`,L,H-25);doc.text(`${i} / ${doc.getNumberOfPages()}`,R,H-25,{align:'right'});}
 return doc.output('arraybuffer');
}
