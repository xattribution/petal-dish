/* PETAL geometry kernel — millimetres; no external dependencies. */
export const defaults={diameter:400,fd:0.42,thickness:2.4,bedX:220,bedY:220,bedZ:250,margin:8,gap:0.4,resolution:5,sectors:0,rows:0,supports:1,printAngle:-1,ribCount:3,contactGap:0.2,contactWidth:0.6,ribPitch:10,rearStyle:0,facetAngle:12,jointStyle:1,jointClearance:0.2,nutClearance:0.2,adaptiveJoints:1,connectorSpacing:150,staggerRings:1,structuralRibs:1,perforate:0,packPlates:0,fastenerStyle:0,insertDiameter:5.6,insertDepth:9.1};
export const limits={diameter:[180,1200],fd:[0.25,0.8],thickness:[1.6,6],bedX:[140,1000],bedY:[140,1000],bedZ:[60,1000],margin:[2,20],gap:[0.2,1],resolution:[2,10],sectors:[0,32],rows:[0,12],supports:[0,1],printAngle:[-1,70],ribCount:[2,5],contactGap:[0.1,0.4],contactWidth:[0.4,0.8],ribPitch:[6,20],rearStyle:[0,1],facetAngle:[10,15],jointStyle:[0,1],jointClearance:[0.1,0.4],nutClearance:[0.1,0.4],adaptiveJoints:[0,1],connectorSpacing:[60,180],staggerRings:[0,1],structuralRibs:[0,1],perforate:[0,1],packPlates:[0,1],fastenerStyle:[0,2],insertDiameter:[4.8,6.5],insertDepth:[9.1,12]};
const PI=Math.PI,TAU=2*PI;
export function validate(p){if(![0,1].includes(p.structuralRibs)||![0,1].includes(p.perforate)||![0,1].includes(p.packPlates)||![0,1,2].includes(p.fastenerStyle))throw Error('Choose valid perforation, packing and fastener options.');if(p.fastenerStyle&&!p.jointStyle)throw Error('Front nuts and blind inserts require recessed plates.');for(const [k,[a,b]]of Object.entries(limits)){if(!Number.isFinite(p[k])||p[k]<a||p[k]>b)throw Error(`${k} must be between ${a} and ${b}.`);}if(p.sectors!==0&&(p.sectors<6||p.sectors%2))throw Error('Petals must be automatic or an even number from 6 to 32.');if(!Number.isInteger(p.rows))throw Error('Ring count must be a whole number.');if(![0,1].includes(p.supports)||!Number.isInteger(p.ribCount))throw Error('Choose on/off supports and a whole number of ribs.');if(p.printAngle!==-1&&p.printAngle!==0&&p.printAngle<45)throw Error('Choose automatic, low profile, or a 45–70° print angle.');if(p.supports&&p.printAngle===0)throw Error('Rib supports are designed for angled petals. Choose automatic or 45–70°.');if(![0,1].includes(p.rearStyle))throw Error('Choose curved or two-facet rear.');if(p.rearStyle&&p.printAngle===0)throw Error('The faceted rear uses diagonal printing. Choose automatic or 45–70°.');if(![0,1].includes(p.jointStyle))throw Error("Choose legacy or keyed joints.");if(![0,1].includes(p.adaptiveJoints)||![0,1].includes(p.staggerRings))throw Error("Choose on/off adaptive connectors and ring staggering.");return p;}
export const zAt=(r,p)=>r*r/(4*p.diameter*p.fd);
// Preserve the specified wall at seams and fasteners while removing material
// from the broad, lightly loaded field of a curved petal.
export function panelWall(p,spec,x,y,centers=[]){
 const r=Math.hypot(x,y),a=Math.atan2(y,x),edge=Math.min(r-spec.r0,spec.r1-r,r*(a-spec.a0),r*(spec.a1-a));
 const bolt=Math.min(Infinity,...centers.map(([rr,aa])=>Math.hypot(x-rr*Math.cos(aa),y-rr*Math.sin(aa))-10.3));
 const distance=Math.max(0,Math.min(edge,bolt)),t=Math.min(1,distance/12),blend=t*t*(3-2*t);
 return p.structuralRibs?p.thickness:p.thickness-(p.thickness-Math.max(1.6,p.thickness*.75))*blend;
}
const unique=a=>[...new Set(a.map(x=>+x.toFixed(9)))].sort((a,b)=>a-b);
const spaced=(a,b,n)=>Array.from({length:n+1},(_,i)=>a+(b-a)*i/n);
export function hole(r,a,w=4.6){return {r0:r-w/2,r1:r+w/2,a0:a-w/(2*r),a1:a+w/(2*r),r,a,w,shape:"round"};}
const boreSamples=[-1,-.75,-1/Math.sqrt(3),-.25,0,.25,1/Math.sqrt(3),.75,1];
function boreCuts(h,axis){return h.shape?boreSamples.map(t=>axis==='r'?h.r+t*h.w/2:h.a+t*h.w/(2*h.r)):[h[axis+'0'],h[axis+'1']];}
function boreGroups(boxes){const groups=[];for(const h of boxes.filter(h=>h.shape)){let g=groups.find(g=>Math.abs(g[0].r-h.r)<1e-6&&Math.abs(g[0].a-h.a)<1e-6);if(!g){g=[];groups.push(g);}if(!g.some(x=>Math.abs(x.w-h.w)<1e-6))g.push(h);}return groups.map(g=>g.sort((a,b)=>a.w-b.w));}
function warpBores(x,y,groups){const r=Math.hypot(x,y),a=Math.atan2(y,x);for(const g of groups){const h=g[0],da=Math.atan2(Math.sin(a-h.a),Math.cos(a-h.a)),u=r-h.r,v=h.r*da,q=Math.max(Math.abs(u),Math.abs(v)),outer=g.at(-1).w/2,limit=outer*1.15;if(q>=limit||q<1e-10)continue;const angle=Math.atan2(v,u),cs=Math.cos(angle),sn=Math.sin(angle),point=b=>{const radius=b.shape==='hex'?b.af/2/Math.max(...Array.from({length:6},(_,i)=>Math.cos(angle-i*PI/3))):b.w/2;return[radius*cs,radius*sn];};let p0=[0,0],q0=0,p1,q1;for(const b of g){p1=point(b);q1=b.w/2;if(q<=q1)break;p0=p1;q0=q1;}if(q>q1){p1=[Math.cos(h.a)*(x-h.r*Math.cos(h.a))+Math.sin(h.a)*(y-h.r*Math.sin(h.a)),-Math.sin(h.a)*(x-h.r*Math.cos(h.a))+Math.cos(h.a)*(y-h.r*Math.sin(h.a))];q1=limit;}const t=(q-q0)/(q1-q0),U=p0[0]+t*(p1[0]-p0[0]),V=p0[1]+t*(p1[1]-p0[1]);return[h.r*Math.cos(h.a)+U*Math.cos(h.a)-V*Math.sin(h.a),h.r*Math.sin(h.a)+U*Math.sin(h.a)+V*Math.cos(h.a)];}return[x,y];}
export function nutPocket(h,af=7.4){return {...hole(h.r,h.a,af/Math.cos(PI/6)),shape:'hex',af};}
export function facetData(spec,p){const f=p.diameter*p.fd,xc=(spec.r0+spec.r1)/2,base=Math.atan(xc/(2*f)),d=p.facetAngle*PI/360,m1=Math.tan(base-d),m2=Math.tan(base+d);const mins=[m1,m2].map(m=>{const q=m>=0?1:Math.cos(spec.a1),r=Math.max(spec.r0,Math.min(spec.r1,2*f*m*q));return r*r/(4*f)-m*(r*q-xc);});return [Math.min(...mins)-p.thickness,xc,m1,m2];}
export const facetZ=(x,d)=>d[0]+Math.max(d[2]*(x-d[1]),d[3]*(x-d[1]));
// Split every crossing triangle along exact XY crease lines. Shared edge
// intersections reuse indices, preserving closed topology at each crease.
export function splitLines(mesh,lines){let v=mesh.v.map(v=>[...v]),faces=mesh.f;const eps=1e-6;for(const [a,b,c]of lines){const d=v.map(p=>a*p[0]+b*p[1]-c),cuts=new Map(),next=[];const hit=(u,w)=>{const key=Math.min(u,w)+':'+Math.max(u,w);if(!cuts.has(key)){const t=d[u]/(d[u]-d[w]);cuts.set(key,v.length);v.push(v[u].map((x,k)=>x+(v[w][k]-x)*t));}return cuts.get(key);};for(const f of faces){const ds=f.map(i=>d[i]);if(Math.min(...ds)>=-eps||Math.max(...ds)<=eps){next.push(f);continue;}for(const sign of [1,-1]){const poly=[];for(let j=0;j<3;j++){const u=f[j],w=f[(j+1)%3];if(sign*d[u]>=-eps)poly.push(u);if((d[u]>eps&&d[w]<-eps)||(d[u]<-eps&&d[w]>eps))poly.push(hit(u,w));}for(let j=1;j<poly.length-1;j++)next.push([poly[0],poly[j],poly[j+1]]);}}faces=next;}return {v,f:faces};}
export function patch(spec,p){
 if(spec.layers)return layeredPatch(spec,p);
 const {r0,r1,a0,a1,holes=[],thickness=p.thickness,offset=0}=spec;
 const rr=unique([...spaced(r0,r1,Math.max(1,Math.ceil((r1-r0)/p.resolution))),...(spec.extraR||[]),...holes.flatMap(h=>boreCuts(h,'r')).filter(r=>r>r0&&r<r1)]);
 const aa=unique([...spaced(a0,a1,Math.max(2,Math.ceil((a1-a0)*r1/p.resolution))),...holes.flatMap(h=>boreCuts(h,'a')).filter(a=>a>a0&&a<a1)]);
 const nr=rr.length,na=aa.length,full=Math.abs(a1-a0-TAU)<1e-7,cols=full?na-1:na;
 const groups=boreGroups(holes),v=[];for(let l=0;l<2;l++)for(const r of rr)for(let j=0;j<cols;j++){const a=aa[j];v.push([...warpBores(r*Math.cos(a),r*Math.sin(a),groups),l]);}
 const id=(i,j,l)=>l*nr*cols+i*cols+(j%cols);
 const active=Array.from({length:nr-1},(_,i)=>Array.from({length:na-1},(_,j)=>!holes.some(h=>(rr[i]+rr[i+1])/2>h.r0&&(rr[i]+rr[i+1])/2<h.r1&&(aa[j]+aa[j+1])/2>h.a0&&(aa[j]+aa[j+1])/2<h.a1)));
 const is=(i,j)=>{if(full)j=(j+na-1)%(na-1);return i>=0&&i<nr-1&&j>=0&&j<na-1&&active[i][j];};
 const f=[];
 for(let i=0;i<nr-1;i++)for(let j=0;j<na-1;j++)if(active[i][j]){
  const q=[id(i,j,0),id(i+1,j,0),id(i+1,j+1,0),id(i,j+1,0)],b=q.map(x=>x+nr*cols);
  f.push([q[0],q[1],q[2]],[q[0],q[2],q[3]],[b[2],b[1],b[0]],[b[3],b[2],b[0]]);
  [[0,1,i,j-1],[1,2,i+1,j],[2,3,i,j+1],[3,0,i-1,j]].forEach(([s,t,ni,nj])=>{if(!is(ni,nj))f.push([q[s],b[s],b[t]],[q[s],b[t],q[t]]);});
 }
 const split=spec.creaseLines?.length?splitLines({v,f},spec.creaseLines):{v,f};const tops=split.v.map(([x,y])=>spec.topFn?spec.topFn(x,y):zAt(Math.hypot(x,y),p)+offset),floor=spec.flatBottom?Math.min(...tops)-thickness:null;
 return {v:split.v.map(([x,y,w],i)=>{const bottom=spec.backFn?spec.backFn(x,y):spec.flatBottom?floor:tops[i]-thickness;return[x,y,tops[i]*(1-w)+bottom*w];}),f:split.f};
}
export function bounds(mesh){const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];const used=new Set(mesh.f.flat());for(const i of used)for(let k=0;k<3;k++){min[k]=Math.min(min[k],mesh.v[i][k]);max[k]=Math.max(max[k],mesh.v[i][k]);}return{min,max,size:max.map((x,k)=>x-min[k])};}
export function printMesh(mesh,tilt=0){const c=Math.cos(tilt),s=Math.sin(tilt);let m={v:mesh.v.map(([x,y,z])=>[c*x+s*z,y,-s*x+c*z]),f:mesh.f};const b=bounds(m);m.v=m.v.map(v=>v.map((x,k)=>x-(k===2?b.min[k]:(b.min[k]+b.max[k])/2)));return m;}
export function tileSpec(p,n,rows,j){if(p.jointStyle)return keyedPanelSpec(p,n,rows,j);const R=p.diameter/2,w=(R-45)/rows,b0=45+j*w,b1=45+(j+1)*w,half=PI/n,rm=b0+w*.65;const holes=[hole(rm,-half+8/rm),hole(rm,half-8/rm)];if(j===0)holes.push(hole(52.5,0));else for(const a of radialStations(p,n,rows,j-1))holes.push(hole(b0+8,a));if(j<rows-1)for(const a of radialStations(p,n,rows,j))holes.push(hole(b1-8,a));const spec={r0:b0+(j? p.gap/2:0),r1:b1-(j<rows-1?p.gap/2:0),a0:-half+p.gap/(2*b0),a1:half-p.gap/(2*b0),holes};if(p.rearStyle){spec.facet=facetData(spec,p);spec.backFn=x=>facetZ(x,spec.facet);spec.creaseLines=[[1,0,spec.facet[1]]];}if(p.perforate){spec.perforations=perforationHoles(spec,p);spec.holes.push(...spec.perforations);}return spec;}
export const SUPPORT_LIFT=5.6;
export function rotateBed(mesh,degrees){const c=Math.cos(degrees*PI/180),s=Math.sin(degrees*PI/180);return{v:mesh.v.map(([x,y,z])=>[c*x-s*y,s*x+c*y,z]),f:mesh.f};}
export function mergeMeshes(meshes){const v=[],f=[];for(const mesh of meshes){const off=v.length;for(const point of mesh.v)v.push(point);for(const face of mesh.f)f.push(face.map(i=>i+off));}return{v,f};}
function envelopePoints(spec,p,tilt){const rr=spaced(spec.r0,spec.r1,16),aa=spaced(spec.a0,spec.a1,24),c=Math.cos(tilt),s=Math.sin(tilt),vs=[];for(const r of rr)for(const a of aa)for(const l of [0,1]){const x=r*Math.cos(a),y=r*Math.sin(a),z=l?(spec.backFn?spec.backFn(x,y):zAt(r,p)-p.thickness):zAt(r,p);vs.push([c*x+s*(z-(l&&spec.rootLip?2:0)),y,-s*x+c*(z-(l&&spec.rootLip?2:0))]);}return vs;}
function vertexBounds(vs){return [0,1,2].map(k=>Math.max(...vs.map(v=>v[k]))-Math.min(...vs.map(v=>v[k])));}
export function fits(size,p){const x=p.bedX-2*p.margin,y=p.bedY-2*p.margin;return((size[0]<=x&&size[1]<=y)||(size[0]<=y&&size[1]<=x))&&size[2]<=p.bedZ-2;}
export function choosePrint(spec,p){const base=Math.atan((spec.r0+spec.r1)/(4*p.diameter*p.fd)),angles=p.printAngle===-1?((p.supports||p.rearStyle||p.jointStyle)?[45,50,55,60,65]:[0]):[p.printAngle];
 for(const angle of angles){const vs=envelopePoints(spec,p,base-angle*PI/180);let best=null;for(const yaw of angle===0?[0,90]:[0,15,30,45,60,75,90]){const c=Math.cos(yaw*PI/180),s=Math.sin(yaw*PI/180),d=vertexBounds(vs.map(([x,y,z])=>[c*x-s*y,s*x+c*y,z]));if(p.supports){d[0]+=8;d[1]+=8;d[2]+=SUPPORT_LIFT;}if(d[0]>p.bedX-2*p.margin||d[1]>p.bedY-2*p.margin||d[2]>p.bedZ-2)continue;const score=d[0]*d[1]+yaw*.001;if(!best||score<best.score)best={angle,yaw,tilt:base-angle*PI/180,dim:d,score};}if(best)return best;
 }return null;
}
export function plan(p){validate(p);if(!fits([120,120,18],p))throw Error('The 120 mm center clamp needs more usable bed space. Reduce the edge margin or increase the bed size.');let best=null;const ns=p.sectors?[p.sectors]:spaced(6,32,13),rs=p.rows?[p.rows]:spaced(1,12,11);for(const rows of rs){if((p.diameter/2-45)/rows<40)continue;for(const n of ns){const score=n*rows+rows*.15+n*.001;if(best&&score>=best.score)continue;if(p.jointStyle&&!jointsFit(p,n,rows))continue;let largest=0,ok=true;const choices=[];for(let j=0;j<rows;j++){const choice=choosePrint(tileSpec(p,n,rows,j),p);if(!choice){ok=false;break;}choices.push(choice);largest=Math.max(largest,...choice.dim);}if(ok){const effective=score*(1+.025*Math.max(0,...choices.map(c=>c.angle-45)));if(!best||effective<best.score)best={n,rows,score:effective,largest,choices};}}}if(!best)throw Error('No assembly fits these print settings. Try automatic angle / segmentation, more height, or a smaller dish. Connector seats also need space between neighboring joints; try fewer petals with more rings or the legacy interface.');return best;}
export function makeRibs(spec,p,tilt,rawBounds,raw){if(p.jointStyle)return keyedRibs(spec,p,raw,rawBounds);const c=Math.cos(tilt),s=Math.sin(tilt),focal=p.diameter*p.fd,cx=(rawBounds.min[0]+rawBounds.max[0])/2,zmin=rawBounds.min[2],tip=spec.r0*Math.sin(spec.a1),ys=spaced(-tip,tip,p.ribCount-1),foot=Math.min(8,2*tip/(p.ribCount-1)*.75),ribWidth=1.2;
 const domains=y=>{if(Math.abs(y)>=spec.r1)return null;const lo=Math.max(Math.sqrt(Math.max(0,spec.r0**2-y*y)),Math.abs(y)/Math.tan(spec.a1)),hi=Math.sqrt(spec.r1**2-y*y);if(hi<=lo)return null;const tx=x=>c*x+s*(spec.facet?facetZ(x,spec.facet):(x*x+y*y)/(4*focal)-p.thickness)-cx;return [tx(lo),tx(hi)];};
 const underside=(x,y)=>{const dom=domains(y);if(!dom||x<dom[0]-1e-7||x>dom[1]+1e-7)return Infinity;if(spec.facet){const d=spec.facet,hinge=c*d[1]+s*d[0]-cx,m=x<=hinge?d[2]:d[3],xx=(x+cx-s*(d[0]-m*d[1]))/(c+s*m);return -s*xx+c*facetZ(xx,d)-zmin+SUPPORT_LIFT;}const A=s/(4*focal),B=c,C=s*(y*y/(4*focal)-p.thickness)-(x+cx),disc=B*B-4*A*C;if(disc<0)return Infinity;const xx=Math.abs(A)<1e-12?-C/B:-2*C/(B+Math.sqrt(disc));return -s*xx+c*((xx*xx+y*y)/(4*focal)-p.thickness)-zmin+SUPPORT_LIFT;};
 const derivative=spec.facet?Math.min(c+s*spec.facet[2],c+s*spec.facet[3]):c+Math.min(0,s*spec.r1/(2*focal));if(derivative<=.05)throw Error('This angle makes the underside too steep for these ribs. Use a lower angle.');const safety=spec.facet?.025:1/(16*focal*derivative**3)+.025; // max 1 mm chord sag + numerical allowance
 return ys.map(y=>{const probes=[y-ribWidth/2,y,y+ribWidth/2],ds=probes.map(domains).filter(Boolean),a=Math.min(...ds.map(d=>d[0])),b=Math.max(...ds.map(d=>d[1]));const xs=unique([...spaced(a,b,Math.ceil(b-a)),...ds.flat(),...(spec.facet?[c*spec.facet[1]+s*spec.facet[0]-cx].filter(x=>x>a&&x<b):[]),...Array.from({length:Math.ceil((b-a)/p.ribPitch)+1},(_,i)=>[0,1.6,2.6,p.ribPitch-1,p.ribPitch].map(t=>a+i*p.ribPitch+t)).flat().filter(x=>x>a&&x<b)]);
 const hs=xs.map(x=>{const phase=((x-a)%p.ribPitch+p.ribPitch)%p.ribPitch,drop=phase<=1.6?0:phase<2.6?(phase-1.6)*.8:phase<=p.ribPitch-1?.8:(p.ribPitch-phase)*.8;const low=Math.min(...probes.map(yv=>underside(x,yv)));return Number.isFinite(low)?Math.max(3,low-p.contactGap-drop-safety):3;});
 return ribSolid(xs,hs,y,foot,p.contactWidth);
 });
}
export function sideSpec(p,n,rows,j,station=0){if(p.jointStyle)return keyedSideSpec(p,n,rows,j,station);const w=(p.diameter/2-45)/rows,rm=45+j*w+w*.65,half=PI/n,spec={r0:rm-6,r1:rm+6,a0:-14/rm,a1:14/rm,holes:[hole(rm,-8/rm),hole(rm,8/rm)],offset:-p.thickness-.2,thickness:3.2};if(p.rearStyle){const d=facetData(tileSpec(p,n,rows,j),p),c=Math.cos(half),sn=Math.sin(half);spec.topFn=(x,y)=>facetZ(c*x+sn*Math.abs(y),d)-.2;spec.flatBottom=true;spec.creaseLines=[[0,1,0],[c,sn,d[1]],[c,-sn,d[1]]];}return spec;}
export function radialSpec(p,n,rows,j,station=0){if(p.jointStyle)return keyedRadialSpec(p,n,rows,j,station);const w=(p.diameter/2-45)/rows,r=45+(j+1)*w,spec={r0:r-14,r1:r+14,a0:-6/r,a1:6/r,holes:[hole(r-8,0),hole(r+8,0)],offset:-p.thickness-.2,thickness:3.2};if(p.rearStyle){const d1=facetData(tileSpec(p,n,rows,j),p),d2=facetData(tileSpec(p,n,rows,j+1),p),alpha=radialStations(p,n,rows,j)[station],beta=alpha-(p.staggerRings?Math.sign(alpha)*PI/n:0),ca=Math.cos(alpha),sa=Math.sin(alpha),cb=Math.cos(beta),sb=Math.sin(beta);spec.extraR=[r-p.gap/2,r,r+p.gap/2];spec.topFn=(x,y)=>{const t=Math.max(0,Math.min(1,(Math.hypot(x,y)-(r-p.gap/2))/p.gap));return (1-t)*facetZ(ca*x-sa*y,d1)+t*facetZ(cb*x-sb*y,d2)-.2;};spec.flatBottom=true;spec.creaseLines=[[ca,-sa,d1[1]],[cb,-sb,d2[1]]];}return spec;}
export function hubSpec(p,n,rows,rootH,mountH){if(p.jointStyle)return keyedHubSpec(p,n,rows,rootH);const spec={r0:15,r1:60,a0:-PI/n,a1:TAU-PI/n,holes:[...rootH,...mountH],offset:-p.thickness-.2,thickness:6};if(p.rearStyle){const d=facetData(tileSpec(p,n,rows,0),p),cs=Array.from({length:n},(_,i)=>[Math.cos(i*TAU/n),Math.sin(i*TAU/n)]);spec.topFn=(x,y)=>facetZ(Math.max(...cs.map(([c,s])=>c*x+s*y)),d)-.2;spec.flatBottom=true;spec.creaseLines=Array.from({length:n/2},(_,i)=>{const a=(i+.5)*TAU/n;return[-Math.sin(a),Math.cos(a),0];});}return spec;}
export function build(p){const layout=plan(p),{n,rows}=layout,parts=[],instances=[],step=TAU/n,w=(p.diameter/2-45)/rows;
 const add=(id,name,spec,qty,kind,row,tilt=0)=>{const mesh=patch(spec,p);let pm,supportMeshes=[],angle=0,bedRotation=0;
 if(kind==='panel'){const choice=layout.choices[row];tilt=choice.tilt;angle=choice.angle;bedRotation=choice.yaw;const c=Math.cos(tilt),s=Math.sin(tilt),raw={v:mesh.v.map(([x,y,z])=>[c*x+s*z,y,-s*x+c*z]),f:mesh.f},rb=bounds(raw);pm=printMesh(mesh,tilt);if(p.supports){pm.v=pm.v.map(([x,y,z])=>[x,y,z+SUPPORT_LIFT]);supportMeshes=makeRibs(spec,p,tilt,rb,raw);}pm=rotateBed(pm,bedRotation);supportMeshes=supportMeshes.map(m=>rotateBed(m,bedRotation));
 }else{if(spec.floorSlope&&kind==='bridge'){const [mx,my]=spec.floorSlope;tilt=Math.atan(Math.hypot(mx,my));pm=printMesh(rotateBed(mesh,-Math.atan2(my,mx)*180/PI),tilt);}else pm=printMesh(mesh,tilt);const dim=bounds(pm).size;bedRotation=dim[0]>p.bedX-2*p.margin||dim[1]>p.bedY-2*p.margin?90:0;pm=rotateBed(pm,bedRotation);}
 const output=supportMeshes.length?mergeMeshes([pm,...supportMeshes]):pm,dim=bounds(output).size;const sampledThickness=spec.facet?mesh.v.map(([x,y])=>zAt(Math.hypot(x,y),p)-facetZ(x,spec.facet)):[];const part={id,name,spec,qty,kind,row,tilt,angle,bedRotation,mesh,print:pm,output,supportMeshes,dim,wallRange:sampledThickness.length?sampledThickness.reduce(([lo,hi],x)=>[Math.min(lo,x),Math.max(hi,x)],[Infinity,-Infinity]):null};if(dim[0]>p.bedX-2*p.margin+.001||dim[1]>p.bedY-2*p.margin+.001||dim[2]>p.bedZ-2+.001)throw Error(`${name}, including its supports, does not fit the usable print volume.`);parts.push(part);return part;};
 const instance=(part,a=0)=>instances.push({part,a});
 for(let j=0;j<rows;j++){
  const phase=ringPhase(p,n,j),spec=tileSpec(p,n,rows,j),part=add(`panel-${j+1}`,`Panel · ring ${j+1}`,spec,n,'panel',j);for(let i=0;i<n;i++)instance(part,i*step+phase);
  const count=p.jointStyle?jointStations(p,n,rows,j).count:1;for(let k=0;k<count;k++){const rm=45+j*w+w*.65,side=add(`side-bridge-${j+1}${k?'-'+(k+1):''}`,`${p.jointStyle?"Side plate":"Side bridge"} · ring ${j+1}${count>1?' / '+(k+1):''}`,sideSpec(p,n,rows,j,k),n,'bridge',j,(p.rearStyle||p.jointStyle)?0:Math.atan(rm/(2*p.diameter*p.fd)));for(let i=0;i<n;i++)instance(side,(i+.5)*step+phase);}
  if(j<rows-1){const r=45+(j+1)*w,angles=radialStations(p,n,rows,j),distinct=p.rearStyle?angles.length:1;for(let k=0;k<distinct;k++){const rad=add(`ring-bridge-${j+1}${k?'-'+(k+1):''}`,`${p.jointStyle?(p.staggerRings?"Junction plate":"Ring plate"):"Ring bridge"} · ${j+1}–${j+2}${distinct>1?' / '+(k+1):''}`,radialSpec(p,n,rows,j,k),n*(distinct===1?angles.length:1),'bridge',j,(p.rearStyle||p.jointStyle)?0:Math.atan(r/(2*p.diameter*p.fd)));for(let i=0;i<n;i++)for(const a of distinct===1?angles:[angles[k]])instance(rad,i*step+phase+a);}}
 }
 const rootH=Array.from({length:n},(_,i)=>hole(52.5,i*step)),mountH=Array.from({length:4},(_,i)=>hole(20,PI/4+i*PI/2));
 instance(add('hub-rear',p.jointStyle?'Rear hub · 60 mm BCD':'Rear hub · 40 mm BCD',hubSpec(p,n,rows,rootH,mountH),1,'hub',-1));
 instance(add('hub-clamp',p.jointStyle?'Flush retaining ring':'Front clamp ring',p.jointStyle?capturedClampSpec(p,n,rootH):{r0:43,r1:60,a0:-PI/n,a1:TAU-PI/n,holes:rootH,offset:3.4,thickness:3.2},1,'hub',-1));
 for(const part of parts)if((p.rearStyle||p.jointStyle)&&(part.kind==='bridge'||part.id==='hub-rear')){const floor=bounds(part.mesh).min[2],grips=part.spec.holes.filter(h=>part.id!=='hub-rear'||Math.abs((h.r0+h.r1)/2-52.5)<.01).map(h=>{const r=(h.r0+h.r1)/2,a=(h.a0+h.a1)/2,[mx,my,c]=part.spec.floorPlane||[0,0,floor];if(p.jointStyle&&part.spec.nutSeats){const idx=part.spec.holes.indexOf(h),front=part.id==='hub-rear'?zAt(r-4.2,p)-2.4:zAt(r,p);return front-part.spec.nutSeats[idx];}return zAt(r,p)+(part.id==='hub-rear'?3.4:0)-(mx*r*Math.cos(a)+my*r*Math.sin(a)+c);});part.gripRange=[Math.min(...grips),Math.max(...grips)];}
 const bolts=n+parts.filter(p=>p.kind==='bridge').reduce((sum,p)=>sum+p.qty*p.spec.holes.length,0);const result={p:{...p},layout,parts,instances,bolts,ringPhases:Array.from({length:rows},(_,j)=>ringPhase(p,n,j)),depth:p.diameter/(16*p.fd),focal:p.diameter*p.fd};if(p.fastenerStyle)rearHardwareSchedule(result);result.plates=p.packPlates?packParts(parts,p):[];return result;
}
export function binarySTL(mesh){const ab=new ArrayBuffer(84+mesh.f.length*50),d=new DataView(ab);new Uint8Array(ab,0,80).set(new TextEncoder().encode('PETAL / millimetres / procedural mesh'));d.setUint32(80,mesh.f.length,true);let k=84;for(const face of mesh.f){const [a,b,c]=face.map(i=>mesh.v[i]),u=b.map((x,i)=>x-a[i]),v=c.map((x,i)=>x-a[i]);let norm=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]],len=Math.hypot(...norm);norm=norm.map(x=>x/(len||1));for(const x of [...norm,...a,...b,...c]){d.setFloat32(k,x,true);k+=4;}d.setUint16(k,0,true);k+=2;}return ab;}
export function volume(mesh){let sum=0;for(const [i,j,k]of mesh.f){const a=mesh.v[i],b=mesh.v[j],c=mesh.v[k];sum+=a[0]*(b[1]*c[2]-b[2]*c[1])+a[1]*(b[2]*c[0]-b[0]*c[2])+a[2]*(b[0]*c[1]-b[1]*c[0]);}return sum/6;}
// Uncompressed ZIP: small dependency-free archive, standard CRC32.
export function zip(entries){const enc=new TextEncoder(),files=entries.map(e=>({name:enc.encode(e.name),data:typeof e.data==='string'?enc.encode(e.data):new Uint8Array(e.data)}));const crc=data=>{let c=0xffffffff;for(const b of data){c^=b;for(let i=0;i<8;i++)c=(c>>>1)^((c&1)?0xedb88320:0);}return(c^0xffffffff)>>>0;};let offset=0;const locals=[],central=[];for(const f of files){const c=crc(f.data),head=new Uint8Array(30+f.name.length),v=new DataView(head.buffer);v.setUint32(0,0x04034b50,true);v.setUint16(4,20,true);v.setUint16(12,33,true);v.setUint32(14,c,true);v.setUint32(18,f.data.length,true);v.setUint32(22,f.data.length,true);v.setUint16(26,f.name.length,true);head.set(f.name,30);const ch=new Uint8Array(46+f.name.length),d=new DataView(ch.buffer);d.setUint32(0,0x02014b50,true);d.setUint16(4,20,true);d.setUint16(6,20,true);d.setUint16(14,33,true);d.setUint32(16,c,true);d.setUint32(20,f.data.length,true);d.setUint32(24,f.data.length,true);d.setUint16(28,f.name.length,true);d.setUint32(42,offset,true);ch.set(f.name,46);locals.push(head,f.data);central.push(ch);offset+=head.length+f.data.length;}const cs=central.reduce((s,x)=>s+x.length,0),end=new Uint8Array(22),e=new DataView(end.buffer);e.setUint32(0,0x06054b50,true);e.setUint16(8,files.length,true);e.setUint16(10,files.length,true);e.setUint32(12,cs,true);e.setUint32(16,offset,true);return new Blob([...locals,...central,end],{type:'application/zip'});}

// Shallow connector seats, interface revision 4. Plain bolt holes, no raised keys.
const inRect=(r,a,h)=>r>h.r0&&r<h.r1&&a>h.a0&&a<h.a1;
const rectAt=(r,a,radial,tangent)=>({r0:r-radial/2,r1:r+radial/2,a0:a-tangent/(2*r),a1:a+tangent/(2*r)});
export const keyDepth=p=>2.6;
export const ringPhase=(p,n,j)=>p.staggerRings?(j%2)*PI/n:0;
export function jointStations(p,n,rows,j,station=0){const w=(p.diameter/2-45)/rows,b0=45+j*w,b1=b0+w,newLayout=p.jointStyle&&(p.adaptiveJoints||p.staggerRings),lo=newLayout?Math.max(j?b0+16:74,16*n/PI):(j?b0+2:62),hi=b1-(newLayout&&j<rows-1?16:2),count=p.jointStyle&&p.adaptiveJoints?Math.max(1,Math.ceil((hi-lo)/p.connectorSpacing)):1,cell=(hi-lo)/count,rm=lo+(station+.5)*cell,span=Math.min(9,cell/2-6);return {rm,span,b0,b1,w,count};}
export const ringBoltSpan=p=>p.staggerRings?9:p.adaptiveJoints?7:8;
export function radialStations(p,n,rows,j){if(!p.jointStyle)return p.staggerRings?[-PI/(2*n),PI/(2*n)]:[0];if(p.staggerRings)return [PI/n];const r=45+(j+1)*(p.diameter/2-45)/rows,h=PI/n,stagger=Boolean(p.staggerRings),width=stagger?h:2*h,bases=stagger?[-h/2,h/2]:[0],q=p.adaptiveJoints?Math.max(1,Math.ceil(r*width/p.connectorSpacing)):1;return bases.flatMap(base=>Array.from({length:q},(_,i)=>base+((i+.5)/q-.5)*width));}
export function keyCenters(p,n,rows,j){const {b0,b1,count}=jointStations(p,n,rows,j),h=PI/n,centers=[];for(let k=0;k<count;k++){const{rm,span}=jointStations(p,n,rows,j,k);for(const r of [rm])for(const sign of [-1,1])centers.push([r,sign*(h-8/r)]);}if(j===0)centers.push([52.5,0]);else for(const a of p.staggerRings?[0]:radialStations(p,n,rows,j-1))for(const sign of [-1,1])centers.push([b0+8,a+sign*ringBoltSpan(p)/b0]);if(j<rows-1){if(p.staggerRings)for(const sign of [-1,1])centers.push([b1-8,sign*(h-ringBoltSpan(p)/b1)]);else for(const a of radialStations(p,n,rows,j))for(const sign of [-1,1])centers.push([b1-8,a+sign*ringBoltSpan(p)/b1]);}return centers;}
export function jointsFit(p,n,rows){for(let j=0;j<rows;j++){const t=jointStations(p,n,rows,j);if(t.span<0)return false;const spec=tileSpec({...p,jointStyle:0},n,rows,j),pads=keyCenters(p,n,rows,j).map(([r,a])=>rectAt(r,a,12,12));for(const b of pads)if(b.r0<spec.r0+.5||b.r1>spec.r1-.5||b.a0<spec.a0+.001||b.a1>spec.a1-.001)return false;for(let i=0;i<pads.length;i++)for(let k=i+1;k<pads.length;k++){const a=pads[i],b=pads[k];if(Math.min(a.r1,b.r1)>Math.max(a.r0,b.r0)+.001&&Math.min(a.a1,b.a1)>Math.max(a.a0,b.a0)+.001)return false;}}return true;}
export function seatBoxes(p,n,rows,j){const h=PI/n,t=jointStations(p,n,rows,j),c=p.jointClearance,out=[];for(let k=0;k<t.count;k++){const r=jointStations(p,n,rows,j,k).rm;for(const sign of [-1,1])out.push({datumR:r,datumA:sign*h,r0:r-JOINT.plateHalf-c-.3,r1:r+JOINT.plateHalf+c+.3,a0:sign>0?h-(JOINT.plateReach+c)/r:-h-c/r,a1:sign>0?h+c/r:-h+(JOINT.plateReach+c)/r});}for(const [boundary,ring,inner]of [[t.b0,j-1,true],[t.b1,j,false]]){if(inner?j===0:j===rows-1)continue;const half=ringBoltSpan(p)/boundary+JOINT.plateHalf/(boundary-8)+c/boundary;for(const a of p.staggerRings?(inner?[0]:[-h,h]):radialStations(p,n,rows,ring))out.push({datumR:boundary,datumA:a,r0:inner?boundary-c:boundary-JOINT.plateReach-c,r1:inner?boundary+JOINT.plateReach+c:boundary+c,a0:a-half,a1:a+half});}return out;}
export const clampDepth=p=>(p.fastenerStyle===2?p.insertDepth+2.8:p.fastenerStyle===1?6.6:4.4)+zAt(56.7,p)-zAt(48.3,p);
// Revision 7: deep locating seats supported by a blended rear reinforcement.
export const JOINT={seatDepth:2,plateHalf:7.5,plateReach:15.5,bearingWall:2.8,minPanelWall:2.8};
function seatBlend(seats,x,y){
 const r=Math.hypot(x,y),a=Math.atan2(y,x);
 const d=Math.min(Infinity,...seats.map(b=>Math.max(b.r0-r,r-b.r1,r*(b.a0-a),r*(a-b.a1),0)));
 const t=Math.min(1,d/4);return 1-t*t*(3-2*t);
}
function seatGrid(seats){return seats.map(b=>({...b,r0:b.r0-4,r1:b.r1+4,a0:b.a0-4/b.r0,a1:b.a1+4/b.r0}));}
export const HUB={shoulder:3,rootEnd:60.2,taperEnd:66,lipInner:45,lipOuter:47,lipDepth:2,pilotInner:39,pilotOuter:42};
function rootLip(n,clear=0){return [{r0:45-clear,r1:47+clear,a0:-PI,a1:PI}];}
function baseKeyedPanelSpec(p,n,rows,j){const spec=tileSpec({...p,jointStyle:0},n,rows,j),centers=keyCenters(p,n,rows,j),seats=seatBoxes(p,n,rows,j),holes=centers.map(([r,a])=>hole(r,a));const T=clampDepth(p),original=(x,y)=>{const nominal=spec.facet?facetZ(x,spec.facet):zAt(Math.hypot(x,y),p)-p.thickness,bare=spec.facet?nominal:zAt(Math.hypot(x,y),p)-panelWall(p,spec,x,y,centers),blend=seatBlend(seats,x,y),datum=spec.facet?nominal:seatSurface(seats,x,y,p)-p.thickness;return bare*(1-blend)+datum*blend-blend*(JOINT.seatDepth-.35+Math.max(0,fastenerWall(p)-p.thickness))-structuralDepth(spec,p,x,y,seats);},front=(x,y)=>zAt(Math.hypot(x,y),p),rear=(x,y)=>{const r=Math.hypot(x,y);if(j||r>=66)return original(x,y);const z=front(x,y)-T-Math.max(3,p.thickness),t=Math.max(0,(r-60.2)/5.8);return z*(1-t)+original(x,y)*t;};if(j)return {...spec,holes,centers,seats,backFn:rear,levels:[rear,(x,y)=>rear(x,y)+JOINT.seatDepth,front],layers:[{exclude:seats,holes},{holes}],gridBoxes:[...seats,...seatGrid(seats),...holes]};const lip=rootLip(n),notch={r0:44,r1:48,a0:-1.5/46,a1:1.5/46},recess={r0:0,r1:60.2,a0:-PI,a1:PI};return {...spec,holes,centers,seats,rootLip:lip,backFn:rear,extraR:[47,60.2,66].filter(r=>r>spec.r0&&r<spec.r1),levels:[(x,y)=>rear(x,y)-2,rear,(x,y)=>rear(x,y)+JOINT.seatDepth,(x,y)=>Math.hypot(x,y)<=60.200001?front(x,y)-T:(rear(x,y)+JOINT.seatDepth+front(x,y))/2,front],layers:[{regions:lip,exclude:[notch],holes},{exclude:seats,holes},{holes},{exclude:[recess],holes}],gridBoxes:[...seats,...seatGrid(seats),...holes,...lip,notch,recess]};}
function shoulderSpec(spec,p,centers,base){const holes=centers.map(([r,a])=>hole(r,a)),nuts=holes.map(h=>nutPocket(h,7+2*p.nutClearance)),r=(spec.r0+spec.r1)/2,y=r*Math.sin(Math.min(Math.abs(spec.a0),Math.abs(spec.a1))),slope=spec.station===undefined?[0,0]:[(base(spec.r1,0)-base(spec.r0,0))/(spec.r1-spec.r0),(base(r,y)-base(r,-y))/(2*y)];const seats=nuts.map(h=>{const x=h.r*Math.cos(h.a),y=h.r*Math.sin(h.a),rad=h.w/2;return Math.min(...Array.from({length:12},(_,i)=>base(x+rad*Math.cos(i*PI/6),y+rad*Math.sin(i*PI/6))))-JOINT.bearingWall;});const seat=(x,y)=>{let best=Infinity,z=0;for(let i=0;i<nuts.length;i++){const h=nuts[i],d=(x-h.r*Math.cos(h.a))**2+(y-h.r*Math.sin(h.a))**2;if(d<best){best=d;z=seats[i];}}return Math.min(z,base(x,y)-.5);};return {...spec,floorSlope:slope,holes,centers,nuts,nutSeats:seats,baseFn:base,levels:[null,seat,base],floorDepth:3.4,layers:[{holes:nuts},{holes}],gridBoxes:[...holes,...nuts]};}
function baseKeyedSideSpec(p,n,rows,j,station=0){const {rm,span}=jointStations(p,n,rows,j,station),half=PI/n,sp=tileSpec({...p,jointStyle:0},n,rows,j),centers=[];for(const r of [rm])for(const sign of [-1,1])centers.push([r,sign*8/r]);const c=Math.cos(half),s=Math.sin(half),rear=(x,y)=>p.rearStyle?facetZ(c*x+s*Math.abs(y),sp.facet):tangentZ(rm,0,x,y,p)-p.thickness;const lines=p.rearStyle?[[0,1,0],[c,s,sp.facet[1]],[c,-s,sp.facet[1]]]:[];return shoulderSpec({station,r0:rm-JOINT.plateHalf,r1:rm+JOINT.plateHalf,a0:-JOINT.plateReach/rm,a1:JOINT.plateReach/rm,creaseLines:lines},p,centers,(x,y)=>rear(x,y)-Math.max(0,fastenerWall(p)-p.thickness)+.35-(p.rearStyle?.05:0));}
function baseKeyedRadialSpec(p,n,rows,j,station=0){const r=45+(j+1)*(p.diameter/2-45)/rows,a=tileSpec({...p,jointStyle:0},n,rows,j),b=tileSpec({...p,jointStyle:0},n,rows,j+1),alpha=radialStations(p,n,rows,j)[station],beta=alpha-(p.staggerRings?Math.sign(alpha)*PI/n:0),ca=Math.cos(alpha),sa=Math.sin(alpha),cb=Math.cos(beta),sb=Math.sin(beta),centers=[];for(const rr of [r-8,r+8])for(const sign of [-1,1])centers.push([rr,sign*ringBoltSpan(p)/r]);const rear=(x,y)=>{if(!p.rearStyle)return tangentZ(r,0,x,y,p)-p.thickness;const t=Math.max(0,Math.min(1,(Math.hypot(x,y)-r+p.gap/2)/p.gap));return (1-t)*facetZ(p.staggerRings?ca*x+sa*Math.abs(y):ca*x-sa*y,a.facet)+t*facetZ(cb*x-sb*y,b.facet);};return shoulderSpec({station,ringOffset:alpha,outerOffset:beta,r0:r-JOINT.plateReach,r1:r+JOINT.plateReach,a0:-ringBoltSpan(p)/r-JOINT.plateHalf/(r-8),a1:ringBoltSpan(p)/r+JOINT.plateHalf/(r-8),extraR:[r-p.gap/2,r,r+p.gap/2],creaseLines:p.rearStyle?[...(p.staggerRings?[[0,1,0],[ca,sa,a.facet[1]]]:[]),[ca,-sa,a.facet[1]],[cb,-sb,b.facet[1]]]:[]},p,centers,(x,y)=>rear(x,y)-Math.max(0,fastenerWall(p)-p.thickness)+.35-(p.rearStyle?.05:0));}
function baseKeyedHubSpec(p,n,rows,rootH){const T=clampDepth(p),B=T+Math.max(3,p.thickness),top=(x,y)=>zAt(Math.hypot(x,y),p),holes=[...rootH,...Array.from({length:4},(_,i)=>hole(30,PI/4+i*TAU/4))],c=p.jointClearance,all={r0:0,r1:100,a0:-PI/n,a1:TAU-PI/n},central={...all,r1:45-c},pilot={...all,r0:39-c,r1:42+c},grooves=Array.from({length:n},(_,i)=>[-1,1].map(sign=>({r0:45-c,r1:47+c,a0:i*TAU/n+(sign>0?(1.5-c)/46:-PI/n),a1:i*TAU/n+(sign>0?PI/n:-(1.5-c)/46)}))).flat();const bottom=top(15,0)-B-10,nuts=holes.map(h=>nutPocket(h,7+2*p.nutClearance)),seat=bottom+3.4;return {r0:15,r1:60,a0:-PI/n,a1:TAU-PI/n,holes,centers:rootH.map(h=>[h.r,h.a]),nuts,nutSeats:nuts.map(()=>seat),baseFn:(x,y)=>top(x,y)-B-.2,floorPlane:[0,0,bottom],levels:[()=>bottom,()=>seat,(x,y)=>top(x,y)-B-2.2,(x,y)=>top(x,y)-B-.2,(x,y)=>top(x,y)-T-2.2,(x,y)=>top(x,y)-T],layers:[{holes:nuts},{holes},{exclude:grooves,holes},{regions:[central],holes},{regions:[central],exclude:[pilot],holes}],gridBoxes:[...holes,...nuts,...grooves,central,pilot]};}
function baseCapturedClampSpec(p,n,rootH){const T=clampDepth(p),top=(x,y)=>zAt(Math.hypot(x,y),p),pilot={r0:39,r1:42,a0:-PI/n,a1:TAU-PI/n},holes=[...rootH,...Array.from({length:4},(_,i)=>hole(30,PI/4+i*TAU/4))],heads=holes.map(h=>hole(h.r,h.a,8.4)),headSeats=heads.map(h=>zAt(h.r-4.2,p)-2.4),seat=(x,y)=>{let best=Infinity,z=0;for(let i=0;i<heads.length;i++){const h=heads[i],d=(x-h.r*Math.cos(h.a))**2+(y-h.r*Math.sin(h.a))**2;if(d<best){best=d;z=headSeats[i];}}return Math.min(top(x,y)-.2,Math.max(top(x,y)-T+.2,z));};return {r0:15,r1:60,a0:-PI/n,a1:TAU-PI/n,holes,heads,headSeats,levels:[(x,y)=>top(x,y)-T-2,(x,y)=>top(x,y)-T,seat,top],layers:[{regions:[pilot],holes},{holes},{holes:heads}],gridBoxes:[...holes,...heads,pilot]};}
// Boundary mesh of occupied polar cells, with shared vertices across stepped
// socket/pad levels. No overlapping shells or post-export boolean repair.
export function layeredPatch(spec,p){const boxes=spec.gridBoxes||[],rr=unique([...spaced(spec.r0,spec.r1,Math.max(1,Math.ceil((spec.r1-spec.r0)/p.resolution))),...(spec.extraR||[]),...boxes.flatMap(h=>boreCuts(h,'r')).filter(r=>r>spec.r0&&r<spec.r1)]),aa=unique([...spaced(spec.a0,spec.a1,Math.max(2,Math.ceil((spec.a1-spec.a0)*spec.r1/p.resolution))),...boxes.flatMap(h=>boreCuts(h,'a')).filter(a=>a>spec.a0&&a<spec.a1)]),nr=rr.length,na=aa.length,full=Math.abs(spec.a1-spec.a0-TAU)<1e-7,nc=full?na-1:na,nl=spec.layers.length,id=(i,j,l)=>l*nr*nc+i*nc+(j%nc),groups=boreGroups(boxes),v=[];
 for(let l=0;l<=nl;l++)for(const r of rr)for(let j=0;j<nc;j++)v.push([...warpBores(r*Math.cos(aa[j]),r*Math.sin(aa[j]),groups),l]);const active=spec.layers.map(layer=>Array.from({length:nr-1},(_,i)=>Array.from({length:na-1},(_,j)=>{const r=(rr[i]+rr[i+1])/2,a=(aa[j]+aa[j+1])/2;return (!layer.regions||layer.regions.some(h=>inRect(r,a,h)))&&!(layer.exclude||[]).some(h=>inRect(r,a,h))&&!(layer.holes||[]).some(h=>inRect(r,a,h));}))),is=(i,j,l)=>{if(full)j=(j+na-1)%(na-1);return l>=0&&l<nl&&i>=0&&i<nr-1&&j>=0&&j<na-1&&active[l][i][j];},f=[];
 for(let l=0;l<nl;l++)for(let i=0;i<nr-1;i++)for(let j=0;j<na-1;j++)if(is(i,j,l)){const b=[id(i,j,l),id(i+1,j,l),id(i+1,j+1,l),id(i,j+1,l)],q=b.map(x=>x+nr*nc);if(!is(i,j,l+1))f.push([q[0],q[1],q[2]],[q[0],q[2],q[3]]);if(!is(i,j,l-1))f.push([b[2],b[1],b[0]],[b[3],b[2],b[0]]);for(const[s,t,ni,nj]of[[0,1,i,j-1],[1,2,i+1,j],[2,3,i,j+1],[3,0,i-1,j]])if(!is(ni,nj,l))f.push([q[s],b[s],b[t]],[q[s],b[t],q[t]]);}
 const split=spec.creaseLines?.length?splitLines({v,f},spec.creaseLines):{v,f},levels=[...spec.levels];if(levels[0]===null){const [mx,my]=spec.floorSlope||[0,0];let floor=Infinity;for(const[x,y]of split.v)floor=Math.min(floor,levels[1](x,y)-mx*x-my*y);floor-=spec.floorDepth*Math.hypot(1,mx,my);spec.floorPlane=[mx,my,floor];levels[0]=(x,y)=>mx*x+my*y+floor;}
 const mesh={v:split.v.map(([x,y,w])=>{const i=Math.min(nl-1,Math.floor(w)),t=w-i;return[x,y,levels[i](x,y)*(1-t)+levels[i+1](x,y)*t];}),f:split.f};return compactMesh(mesh);}
// Crease intersections can land within numerical noise of a bore-grid vertex.
// Weld those coincident points before repairing flat triangles; otherwise a
// vanishing edge can cause an endless split/repair cycle.
function compactMesh(mesh){
 const used=new Map(),positions=new Map(),v=[];
 const f=mesh.f.map(face=>face.map(i=>{
  if(!used.has(i)){
   const point=mesh.v[i],key=point.map(x=>Math.round(x*1e6)).join(',');
   if(!positions.has(key)){positions.set(key,v.length);v.push(point);}
   used.set(i,positions.get(key));
  }
  return used.get(i);
 })).filter(face=>new Set(face).size===3);
 return repairFlatTriangles({v,f});
}
function repairFlatTriangles(mesh){const {v}=mesh,f=[...mesh.f],edges=new Map(),key=(a,b)=>Math.min(a,b)+':'+Math.max(a,b),add=(face,i)=>{for(let j=0;j<3;j++){const k=key(face[j],face[(j+1)%3]);if(!edges.has(k))edges.set(k,new Set());edges.get(k).add(i);}},remove=(face,i)=>{for(let j=0;j<3;j++)edges.get(key(face[j],face[(j+1)%3]))?.delete(i);},area=face=>{const [a,b,c]=face.map(i=>v[i]),u=b.map((x,k)=>x-a[k]),w=c.map((x,k)=>x-a[k]);return Math.hypot(u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0]);};f.forEach(add);const queue=f.map((_,i)=>i);for(let q=0;q<queue.length;q++){const i=queue[q],face=f[i];if(!face||area(face)>1e-9)continue;if(q>mesh.f.length*4)throw Error("Mesh repair did not converge. Change the mesh spacing and regenerate.");let j=0,dist=-1;for(let k=0;k<3;k++){const d=v[face[k]].reduce((s,x,t)=>s+(x-v[face[(k+1)%3]][t])**2,0);if(d>dist){dist=d;j=k;}}const a=face[j],b=face[(j+1)%3],mid=face[(j+2)%3],adj=[...edges.get(key(a,b))].find(k=>k!==i&&f[k]);if(adj===undefined)throw Error('Cannot repair collapsed mesh edge');const other=f[adj],k=other.findIndex((x,k)=>key(x,other[(k+1)%3])===key(a,b)),u=other[k],w=other[(k+1)%3],tip=other[(k+2)%3];remove(face,i);remove(other,adj);f[i]=null;f[adj]=[u,mid,tip];add(f[adj],adj);const ni=f.length;f.push([mid,w,tip]);add(f[ni],ni);queue.push(adj,ni);}return {v,f:f.filter(Boolean)};}
// Exact triangle slices produce a piecewise-linear support envelope, including
// shallow rear seats. X breakpoints preserve the actual underside profile.
function sliceEnvelope(mesh,y){const seg=[];for(const face of mesh.f){const vs=face.map(i=>mesh.v[i]),pts=[];for(let k=0;k<3;k++){const a=vs[k],b=vs[(k+1)%3];if(Math.abs(a[1]-y)<1e-8)pts.push([a[0],a[2]]);if((a[1]<y&&b[1]>y)||(a[1]>y&&b[1]<y)){const t=(y-a[1])/(b[1]-a[1]);pts.push([a[0]+t*(b[0]-a[0]),a[2]+t*(b[2]-a[2])]);}}if(pts.length<2)continue;pts.sort((a,b)=>a[0]-b[0]);const a=pts[0],b=pts.at(-1);if(b[0]-a[0]>1e-8)seg.push([a[0],b[0],a[1],(b[1]-a[1])/(b[0]-a[0])]);}const buckets=new Map();for(const s of seg)for(let k=Math.floor(s[0]/2);k<=Math.floor(s[1]/2);k++){if(!buckets.has(k))buckets.set(k,[]);buckets.get(k).push(s);}return{xs:seg.flatMap(s=>[s[0],s[1]]),at:x=>{let z=Infinity;for(const s of buckets.get(Math.floor(x/2))||[])if(x>=s[0]-2e-6&&x<=s[1]+2e-6)z=Math.min(z,s[2]+(x-s[0])*s[3]);return z;}};}
function keyedRibs(spec,p,raw,rb){const cx=(rb.min[0]+rb.max[0])/2,tip=spec.r0*Math.sin(spec.a1),mesh={v:raw.v.map(([x,y,z])=>[x-cx,y,z-rb.min[2]+SUPPORT_LIFT]),f:raw.f},foot=Math.min(8,2*tip/(p.ribCount-1)*.75);return spaced(-tip,tip,p.ribCount-1).map(y=>{const slices=[stripEnvelope(mesh,y-.6,y+.6)],ends=slices.flatMap(s=>s.xs),a=Math.min(...ends),b=Math.max(...ends),xs=unique([...ends,...spaced(a,b,Math.ceil(b-a)),...Array.from({length:Math.ceil((b-a)/p.ribPitch)+1},(_,i)=>[0,1.6,2.6,p.ribPitch-1,p.ribPitch].map(t=>a+i*p.ribPitch+t)).flat().filter(x=>x>a&&x<b)].map(x=>Math.round(x*1e6)/1e6)),hs=xs.map(x=>{const phase=((x-a)%p.ribPitch+p.ribPitch)%p.ribPitch,drop=phase<=1.6?0:phase<2.6?(phase-1.6)*.8:phase<=p.ribPitch-1?.8:(p.ribPitch-phase)*.8,z=Math.min(...slices.map(s=>s.at(x)));return Number.isFinite(z)?Math.max(3,z-p.contactGap-drop-.04):3;});return ribSolid(xs,hs,y,foot,p.contactWidth);});}

// A cropped side joint, generated from the same specs and print orientations as
// the full kit. No substitute tolerance geometry or separately tuned dimensions.
export function connectionCoupon(model){
 if(!model.p.jointStyle)return [];
 const panel=model.parts.find(part=>part.kind==='panel'&&part.row===0);
 const plate=model.parts.find(part=>part.id==='side-bridge-1');
 const r=(plate.spec.r0+plate.spec.r1)/2;
 const coupons=[-1,1].map(sign=>{
  const source=panel.spec,r0=Math.max(source.r0,r-12),r1=Math.min(source.r1,r+12);
  const a0=sign<0?source.a0:Math.max(source.a0,source.a1-22/r);
  const a1=sign<0?Math.min(source.a1,source.a0+22/r):source.a1;
  const spec={...source,r0,r1,a0,a1,extraR:(source.extraR||[]).filter(x=>x>r0&&x<r1)};
  const mesh=patch(spec,model.p),output=rotateBed(printMesh(mesh,panel.tilt),panel.bedRotation);
  return {id:sign<0?'coupon-right':'coupon-left',mesh,output,spec,assemblyRotation:sign<0?TAU/model.layout.n:0};
 });
 return [...coupons,{id:'coupon-plate',mesh:plate.mesh,output:plate.print,spec:plate.spec,assemblyRotation:PI/model.layout.n}];
}

// Extra depth includes front-face slope across the pocket and a bearing wall.
function fastenerWall(p){return p.fastenerStyle===1?2.8+4.2+(7+2*p.nutClearance)/Math.cos(PI/6)/(4*p.fd):p.fastenerStyle===2?2.8+p.insertDepth+p.insertDiameter/(4*p.fd):JOINT.minPanelWall;}
export function perforationHoles(spec,p){
 if(!p.perforate)return [];
 const area=(spec.r1**2-spec.r0**2)*(spec.a1-spec.a0)/2,pitch=Math.max(18,Math.sqrt(area/48)),out=[];
 for(let row=0,r=Math.max(84,spec.r0+14);r<=spec.r1-14;r+=pitch,row++){
  const step=pitch/r;
  for(let a=spec.a0+14/r+(row%2)*step/2;a<=spec.a1-14/r;a+=step){
   const x=r*Math.cos(a),y=r*Math.sin(a);
   if(p.structuralRibs&&structuralDepth(spec,p,x,y,spec.seats||[])>.01)continue;
   if((spec.holes||[]).some(h=>Math.hypot(x-h.r*Math.cos(h.a),y-h.r*Math.sin(h.a))<18))continue;
   if((spec.seats||[]).some(b=>r>b.r0-10&&r<b.r1+10&&a>b.a0-10/r&&a<b.a1+10/r))continue;
   out.push(hole(r,a,6));
  }
 }
 return out;
}
export function keyedPanelSpec(p,n,rows,j){
 const spec=baseKeyedPanelSpec(p,n,rows,j);
 if(p.structuralRibs){spec.extraR=unique([...(spec.extraR||[]),...spaced(spec.r0,spec.r1,Math.ceil((spec.r1-spec.r0)/2))]);spec.gridBoxes.push(...structuralGrid(spec));}
 const vents=perforationHoles(spec,p);
 const seam=spec.holes.filter(h=>j||h.r>60.3),root=spec.holes.filter(h=>!j&&h.r<=60.3);
 if(p.fastenerStyle){
  const pockets=seam.map(h=>p.fastenerStyle===1?nutPocket(h,7+2*p.nutClearance):hole(h.r,h.a,p.insertDiameter));
  const old=spec.levels,front=old.at(-1),low=old.at(-2),under=old.at(-3);
  const floor=(x,y)=>{
   const h=pockets.reduce((best,h)=>!best||Math.hypot(x-h.r*Math.cos(h.a),y-h.r*Math.sin(h.a))<Math.hypot(x-best.r*Math.cos(best.a),y-best.r*Math.sin(best.a))?h:best,null);
   const desired=h?zAt(h.r-h.w/2,p)-(p.fastenerStyle===1?4.2:2):front(x,y)-2;
   return Math.max(under(x,y)+.4,Math.min(front(x,y)-.4,desired));
  };
  // This boundary is internal outside the root shoulder. Keep it below the
  // pocket floor even on steep dishes, without moving the root capture face.
  const start=(x,y)=>!j&&Math.hypot(x,y)<=60.200001?low(x,y):Math.min(low(x,y),floor(x,y)-.2);
  spec.levels=[...old.slice(0,-2),start,floor,front];
  const through=p.fastenerStyle===2?[...root,...pockets]:spec.holes;
  spec.layers=spec.layers.map(layer=>({...layer,holes:through}));
  const last=spec.layers.at(-1);
  spec.layers.push({...last,holes:p.fastenerStyle===2?root:[...root,...pockets]});
  spec.gridBoxes.push(...pockets);spec.pockets=pockets;spec.pocketFloor=floor;
 }
 if(vents.length){spec.layers=spec.layers.map(layer=>({...layer,holes:[...(layer.holes||[]),...vents]}));spec.gridBoxes.push(...vents);}
 spec.perforations=vents;
 return spec;
}
function rearScrewPlate(spec,p){
 if(p.fastenerStyle){const heads=spec.holes.map(h=>hole(h.r,h.a,8.4));spec.layers=spec.layers.map((layer,i)=>({...layer,holes:i===0?heads:spec.holes}));spec.gridBoxes=spec.gridBoxes.filter(b=>!spec.nuts.includes(b));spec.gridBoxes.push(...heads);spec.rearHeadSeats=spec.nutSeats;spec.rearHeads=heads;delete spec.nuts;delete spec.nutSeats;}
 return spec;
}
export function keyedSideSpec(p,n,rows,j,station=0){return rearScrewPlate(baseKeyedSideSpec(p,n,rows,j,station),p);}
export function keyedRadialSpec(p,n,rows,j,station=0){return rearScrewPlate(baseKeyedRadialSpec(p,n,rows,j,station),p);}
export function keyedHubSpec(p,n,rows,rootH){return rearScrewPlate(baseKeyedHubSpec(p,n,rows,rootH),p);}
export function capturedClampSpec(p,n,rootH){
 const spec=baseCapturedClampSpec(p,n,rootH);
 if(!p.fastenerStyle)return spec;
 const pockets=spec.holes.map(h=>p.fastenerStyle===1?nutPocket(h,7+2*p.nutClearance):hole(h.r,h.a,p.insertDiameter));
 const front=spec.levels.at(-1),bottom=spec.levels[1];
 const floor=(x,y)=>{const h=pockets.reduce((best,h)=>!best||Math.hypot(x-h.r*Math.cos(h.a),y-h.r*Math.sin(h.a))<Math.hypot(x-best.r*Math.cos(best.a),y-best.r*Math.sin(best.a))?h:best,null);return Math.max(bottom(x,y)+.4,Math.min(front(x,y)-.4,zAt(h.r-h.w/2,p)-(p.fastenerStyle===1?4.2:2)));};
 spec.levels[2]=floor;
 spec.layers=spec.layers.map((layer,i)=>({...layer,holes:p.fastenerStyle===2?(i===2?[]:pockets):(i===2?pockets:spec.holes)}));
 spec.gridBoxes=spec.gridBoxes.filter(b=>!spec.heads.includes(b));spec.gridBoxes.push(...pockets);
 spec.pockets=pockets;spec.pocketFloor=floor;delete spec.heads;delete spec.headSeats;
 return spec;
}

// Conservative rectangular packing: rotated bounds include all built-in
// supports. Six millimetres between bounds leaves room for separate brims.
export function packParts(parts,p){
 const W=p.bedX-2*p.margin,H=p.bedY-2*p.margin,gap=6,plates=[];
 const options=new Map(parts.map(part=>[part,Array.from({length:12},(_,i)=>{const yaw=i*15,b=bounds(rotateBed(part.output,yaw));return{yaw,b,w:b.size[0]+gap,h:b.size[1]+gap};}).filter(o=>o.w<=W+gap+1e-6&&o.h<=H+gap+1e-6)]));
 const items=parts.flatMap(part=>Array.from({length:part.qty},()=>part)).sort((a,b)=>Math.max(...b.dim.slice(0,2))-Math.max(...a.dim.slice(0,2)));
 for(const part of items){
  let best;
  for(let bi=0;bi<=plates.length;bi++){
   const free=bi===plates.length?[{x:0,y:0,w:W+gap,h:H+gap}]:plates[bi].free;
   for(let fi=0;fi<free.length;fi++)for(const o of options.get(part)){
    const r=free[fi];if(o.w>r.w+1e-6||o.h>r.h+1e-6)continue;
    const score=bi*1e9+(r.w*r.h-o.w*o.h)+Math.min(r.w-o.w,r.h-o.h);
    if(!best||score<best.score)best={bi,fi,o,r,score};
   }
   if(best)break;
  }
  if(!best)throw Error('No packed plate fits '+part.name);
  const {bi,fi,o,r}=best;if(bi===plates.length)plates.push({free:[r],placements:[]});
  const plate=plates[bi];plate.free.splice(fi,1);
  if(r.w-o.w>1e-6)plate.free.push({x:r.x+o.w,y:r.y,w:r.w-o.w,h:r.h});
  if(r.h-o.h>1e-6)plate.free.push({x:r.x,y:r.y+o.h,w:o.w,h:r.h-o.h});
  plate.placements.push({part,yaw:o.yaw,x:r.x-W/2-o.b.min[0],y:r.y-H/2-o.b.min[1],bounds:[r.x,r.y,o.w-gap,o.h-gap]});
 }
 return plates.map(({placements})=>({placements}));
}
export function packedPlateMesh(plate){return mergeMeshes(plate.placements.map(({part,yaw,x,y})=>{const mesh=rotateBed(part.output,yaw);return{v:mesh.v.map(v=>[v[0]+x,v[1]+y,v[2]]),f:mesh.f};}));}


function rearHardwareSchedule(model){
 const key=(x,y)=>[x,y].map(v=>v.toFixed(4)).join(','),targets=new Map();
 for(const inst of model.instances.filter(i=>i.part.kind==='panel'))for(const h of inst.part.spec.pockets||[]){const a=h.a+inst.a;targets.set(key(h.r*Math.cos(a),h.r*Math.sin(a)),{spec:inst.part.spec,h});}
 const cap=model.parts.find(p=>p.id==='hub-clamp');
 for(const part of model.parts.filter(p=>p.kind==='bridge'||p.id==='hub-rear')){
  const angle=model.instances.find(i=>i.part===part).a,grips=[],limits=[];
  for(let i=0;i<part.spec.holes.length;i++){
   const h=part.spec.holes[i],target=part.id==='hub-rear'?{spec:cap.spec,h}:targets.get(key(h.r*Math.cos(h.a+angle),h.r*Math.sin(h.a+angle)));
   if(!target)throw Error('Rear screw has no matching front fastening pocket.');
   const {spec,h:dest}=target,x=dest.r*Math.cos(dest.a),y=dest.r*Math.sin(dest.a),seat=part.spec.rearHeadSeats[i];
   const entry=part.id==='hub-rear'?spec.levels[1](x,y):spec.backFn(x,y)+JOINT.seatDepth;
   grips.push(spec.pocketFloor(x,y)-(model.p.fastenerStyle===2?9.1:0)-seat);
   limits.push(model.p.fastenerStyle===2?spec.pocketFloor(x,y)-seat-1:zAt(dest.r,model.p)-seat-.2);
  }
  part.gripRange=[Math.min(...grips),Math.max(...grips)];
  part.maxScrewLengthRange=[Math.min(...limits),Math.max(...limits)];
 }
}


// Permanent back ribs, distinct from sacrificial print supports. A rounded
// cosine shoulder joins a 3 mm half-width crown to the continuous front skin.
// Seat keep-outs preserve the exact clamping datum and tool access.
export function structuralDepth(spec,p,x,y,seats=[]){
 if(!p.structuralRibs||!p.jointStyle)return 0;
 const r=Math.hypot(x,y),a=Math.atan2(y,x);
 const edge=Math.min(r-spec.r0,spec.r1-r,r*Math.sin(a-spec.a0),r*Math.sin(spec.a1-a));
 const d=Math.max(0,Math.min(edge,Math.abs(y)));
 const ridge=d<=3?1:d>=7?0:(1+Math.cos(Math.PI*(d-3)/4))/2;
 const root=Math.max(0,Math.min(1,(r-66)/8));
 let clearance=1;
 for(const b of seats){
  const dr=Math.max(b.r0-r,0,r-b.r1),da=Math.max(b.a0-a,0,a-b.a1)*r;
  const distance=Math.hypot(dr,da);
  clearance=Math.min(clearance,Math.max(0,Math.min(1,(distance-2)/6)));
 }
 return 4*ridge*root*clearance;
}
function structuralGrid(spec){
 const mid=(spec.r0+spec.r1)/2;
 return [0,3,5,7].flatMap(d=>[{r0:spec.r0,r1:spec.r1,a0:spec.a0+d/spec.r1,a1:spec.a1-d/spec.r1},{r0:spec.r0,r1:spec.r1,a0:-d/mid,a1:d/mid}]);
}

export function tangentZ(r,a,x,y,p){const X=r*Math.cos(a),Y=r*Math.sin(a);return zAt(r,p)+(X*(x-X)+Y*(y-Y))/(2*p.diameter*p.fd);}
function seatSurface(seats,x,y,p){
 const r=Math.hypot(x,y),a=Math.atan2(y,x);let nearest=null,best=Infinity;
 for(const b of seats){const d=Math.max(b.r0-r,r-b.r1,r*(b.a0-a),r*(a-b.a1),0);if(d<best){best=d;nearest=b;}}
 return nearest?tangentZ(nearest.datumR,nearest.datumA,x,y,p):zAt(r,p);
}

// Per-position screw selection, with explicit allowances. Mount fasteners
// remain unresolved until the external adapter bearing face is known.
export function hardwareSchedule(model){
 if(!model.p.jointStyle)return [];
 const p=model.p,cap=model.parts.find(x=>x.id==='hub-clamp'),targets=new Map(),key=(x,y)=>[x,y].map(v=>v.toFixed(4)).join(',');
 for(const inst of model.instances.filter(i=>i.part.kind==='panel'))for(const h of inst.part.spec.holes){const a=h.a+inst.a;targets.set(key(h.r*Math.cos(a),h.r*Math.sin(a)),{spec:inst.part.spec,h});}
 const stock=[6,8,10,12,14,16,18,20,22,25,30,35,40,45,50,60,70,80];
 const out=[];
 for(const part of model.parts.filter(x=>x.kind==='bridge'||x.id==='hub-rear')){
  const a=model.instances.find(i=>i.part===part).a;
  for(let i=0;i<part.spec.holes.length;i++){
   const h=part.spec.holes[i],mount=part.id==='hub-rear'&&h.r<40;
   if(mount){out.push({part:part.id,position:i+1,radius_mm:h.r,azimuth_degrees:h.a*180/PI,quantity:part.qty,role:'mount',insert_top_below_mouth_mm:p.fastenerStyle===2?cap.spec.pocketFloor(h.r*Math.cos(h.a),h.r*Math.sin(h.a))-9.1-cap.spec.levels[1](h.r*Math.cos(h.a),h.r*Math.sin(h.a)):null,length_mm:null,note:'Measure adapter stack; four mounting screws are not assigned a stock length.'});continue;}
   const target=part.id==='hub-rear'?{spec:cap.spec,h}:targets.get(key(h.r*Math.cos(h.a+a),h.r*Math.sin(h.a+a))),{spec,h:dest}=target;
   const x=dest.r*Math.cos(dest.a),y=dest.r*Math.sin(dest.a);
   let grip,min,max,washer=0;
   if(p.fastenerStyle){
    const seat=part.spec.rearHeadSeats[i],entry=part.id==='hub-rear'?spec.levels[1](x,y):spec.backFn(x,y)+JOINT.seatDepth;
    grip=(p.fastenerStyle===2?spec.pocketFloor(x,y)-9.1:spec.pocketFloor(x,y))-seat;
    min=grip+(p.fastenerStyle===2?4.4:3.4);
    max=p.fastenerStyle===2?Math.min(spec.pocketFloor(x,y)-seat-1.4,grip+8.1-.4):zAt(dest.r-2.1,p)-seat-.4;
   }else{
    const bearing=part.id==='hub-rear'?cap.spec.headSeats[i]:zAt(dest.r+4.5,p),seat=part.spec.nutSeats[i];
    washer=part.id==='hub-rear'?0:1;
    grip=bearing-seat;min=grip+washer+3.2+1.4+.4;max=Infinity;
   }
   const candidates=stock.filter(l=>l>=min-1e-8&&l<=max+1e-8),length=candidates[0]??null;
   out.push({part:part.id,position:i+1,radius_mm:h.r,azimuth_degrees:h.a*180/PI,quantity:part.qty,role:part.id==='hub-rear'?'root':'seam',insert_top_below_mouth_mm:p.fastenerStyle===2?(spec.pocketFloor(x,y)-9.1)-(part.id==='hub-rear'?spec.levels[1](x,y):spec.backFn(x,y)+JOINT.seatDepth):null,length_mm:length,min_length_mm:min,max_length_mm:Number.isFinite(max)?max:null,grip_mm:grip,washer_mm:washer,engagement_mm:length?length-grip-washer:null,note:length?'Nominal selection; verify against the printed fit coupon.':'No listed stock length fits. Measure and source/cut a suitable length; do not round upward.'});
  }
 }
 return out;
}


// Windowed sacrificial web: 4 mm pillars on 12 mm centers, <=8 mm bridges,
// a continuous flared foot and >=6 mm under the contact ridge. Windows save
// support material without changing the tested contact envelope.
function ribSolid(sourceX,sourceH,y,foot,contact){
 const keep=new Set([0,sourceX.length-1]),stack=[[0,sourceX.length-1]];
 while(stack.length){const [a,b]=stack.pop();let worst=.005,index=-1;for(let i=a+1;i<b;i++){const t=(sourceX[i]-sourceX[a])/(sourceX[b]-sourceX[a]),err=Math.abs(sourceH[i]-(sourceH[a]*(1-t)+sourceH[b]*t));if(err>worst){worst=err;index=i;}}if(index>=0){keep.add(index);stack.push([a,index],[index,b]);}}
 const indices=[...keep].sort((a,b)=>a-b);sourceH=indices.map(i=>sourceH[i]-.005);sourceX=indices.map(i=>sourceX[i]);
 const a=sourceX[0],b=sourceX.at(-1),pitch=12,pillar=4;
 const xs=unique([...sourceX,...Array.from({length:Math.ceil((b-a)/pitch)+1},(_,i)=>[a+i*pitch,a+i*pitch+pillar]).flat().filter(x=>x>a&&x<b)]);
 let cursor=0;
 const hs=xs.map(x=>{while(cursor<sourceX.length-2&&sourceX[cursor+1]<x)cursor++;const t=(x-sourceX[cursor])/(sourceX[cursor+1]-sourceX[cursor]);return sourceH[cursor]*(1-t)+sourceH[cursor+1]*t;});
 const low=new Map();xs.forEach((x,i)=>{for(const k of [Math.floor((x-a)/pitch),Math.floor((x-a-1e-7)/pitch)])low.set(k,Math.min(low.get(k)??Infinity,hs[i]));});
 const tops=xs.map((x,i)=>Math.max(2,Math.min(hs[i]-.9,(low.get(Math.floor((x-a)/pitch))??hs[i])-6)));
 const zs=xs.map((x,i)=>[0,1.2,2,tops[i],hs[i]-.8,hs[i]]),width=[foot,foot,1.2,1.2,1.2,contact],v=[],f=[],N=xs.length,L=6;
 for(let side=0;side<2;side++)for(let i=0;i<N;i++)for(let l=0;l<L;l++)v.push([xs[i],y+(side?1:-1)*width[l]/2,zs[i][l]]);
 const id=(i,l,side)=>side*N*L+i*L+l;
 const active=(i,l)=>{if(i<0||i>=N-1||l<0||l>=L-1)return false;const x=(xs[i]+xs[i+1])/2,phase=(x-a)%pitch;return !(l===2&&phase>=pillar&&Math.min(hs[i],hs[i+1])>=10&&Math.min(tops[i],tops[i+1])>2.1);};
 for(let i=0;i<N-1;i++)for(let l=0;l<L-1;l++)if(active(i,l)){
  const q=[id(i,l,0),id(i+1,l,0),id(i+1,l+1,0),id(i,l+1,0)],r=q.map(j=>j+N*L);
  f.push([q[0],q[1],q[2]],[q[0],q[2],q[3]],[r[2],r[1],r[0]],[r[3],r[2],r[0]]);
  for(const [u,w,ni,nl] of [[0,1,i,l-1],[1,2,i+1,l],[2,3,i,l+1],[3,0,i-1,l]])if(!active(ni,nl))f.push([q[u],r[u],r[w]],[q[u],r[w],q[w]]);
 }
 return compactMesh({v,f});
}


// Minimum underside over the entire support-web width, not just sampled Y
// slices. Clip every actual triangle to the strip; extrema at any X lie on
// one of the clipped polygon edges. This protects narrow seat/rib transitions.
function stripEnvelope(mesh,y0,y1){
 const segments=[];
 for(const face of mesh.f){
  let poly=face.map(i=>mesh.v[i]);
  for(const [bound,sign] of [[y0,1],[y1,-1]]){
   const next=[];for(let i=0;i<poly.length;i++){const a=poly[i],b=poly[(i+1)%poly.length],da=sign*(a[1]-bound),db=sign*(b[1]-bound);if(da>=0)next.push(a);if((da<0&&db>0)||(da>0&&db<0)){const t=da/(da-db);next.push(a.map((x,k)=>x+t*(b[k]-x)));}}poly=next;
  }
  for(let i=0;i<poly.length;i++){let a=poly[i],b=poly[(i+1)%poly.length];if(a[0]>b[0])[a,b]=[b,a];if(b[0]-a[0]>1e-8)segments.push([a[0],b[0],a[2],(b[2]-a[2])/(b[0]-a[0])]);}
 }
 const buckets=new Map();for(const seg of segments)for(let i=Math.floor(seg[0]/2);i<=Math.floor(seg[1]/2);i++){if(!buckets.has(i))buckets.set(i,[]);buckets.get(i).push(seg);}
 return {xs:segments.flatMap(s=>[s[0],s[1]]),at:x=>{let z=Infinity;for(const s of buckets.get(Math.floor(x/2))||[])if(x>=s[0]-2e-6&&x<=s[1]+2e-6)z=Math.min(z,s[2]+(x-s[0])*s[3]);return z;}};
}
