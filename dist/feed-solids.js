import {box,cylinder,loft} from './solid.js';
export const feedMesh=s=>s.mesh();
export const feedCylinder=cylinder,feedBox=box;
export const feedTransform=(s,fn)=>s.transform(fn);
export function feedPuckBody(face,secondary=false){const h=16;return loft([[0,21],[1,22],[h-1,22],[h,21]].map(([z,r])=>Array.from({length:64},(_,i)=>{const a=i*Math.PI/32;return[r*Math.cos(a),r*Math.sin(a),face+z];})));}
export function feedSocketBody(diameter){const ro=diameter/2+2.5;
 return loft([[0,ro-.7,.8],[2,ro,1.8],[7,ro,4.7],[12,ro,5.6],[17,ro,4.7],[20.5,ro,1.8],[22,ro-.7,.8]].map(([z,r,bulge])=>Array.from({length:48},(_,i)=>{const a=i*Math.PI/24,s=Math.sin(a),positive=Math.max(0,s);return[(r+3.5*positive)*Math.cos(a),r*s+bulge*positive*positive,z];})));}
export function feedContinuousSocket(diameter,clearance,loadingDirection=1){const ro=diameter/2+2.5;return{ro,cuts:[
 feedCylinder([0,0,2],[0,0,120],(diameter+clearance)/2,48),
 feedCylinder([0,0,12],[0,120,12],1.7,24),
 feedCylinder([0,ro+5.6,12],[0,120,12],3.3,32),
 feedCylinder([0,ro+.6,12],[0,ro+3.2,12],5.8/Math.sqrt(3),6),
 feedBox([60*loadingDirection,ro+1.9,12],[60,1.3,3.45])
 ]};}
// A local underside saddle grows from the shell at 45 degrees in the actual
// side-print up direction. It has no separate foot, backing plate or bolt pair.
export function feedIntegralSocket(p,g,n,backFn){
 const h=Math.PI/n,up=[Math.sin(h),Math.cos(h),0],A=[g.datum.r,0,g.lowerZ],B=[18,0,g.upperZ],axis=B.map((v,k)=>(v-A[k])/g.pivotDistance),dot=axis.reduce((s,v,k)=>s+v*up[k],0),v0=up.map((v,k)=>v-dot*axis[k]),vl=Math.hypot(...v0),V=v0.map(v=>v/vl);
 let T=[axis[1]*V[2]-axis[2]*V[1],axis[2]*V[0]-axis[0]*V[2],axis[0]*V[1]-axis[1]*V[0]];if(T[2]<0)T=T.map(v=>-v);
 const at=(t,w,l)=>A.map((a,k)=>a+T[k]*t+V[k]*w+axis[k]*l),r=(g.rodDiameter+p.rodClearance)/2,depth=6+r+7.6;
 const x0=A[0]-Math.max(18,g.lowerEntrance*Math.abs(axis[0])+r+3),x1=Math.min(p.diameter/2-.4,A[0]+20),rings=[];
 for(let i=0;i<=24;i++){const x=x0+(x1-x0)*i/24,e=Math.max(.01,Math.min(depth-.05,x-x0,x1-x)),knots=[-depth,-depth+e,depth-e,depth],ws=[];for(let k=0;k<3;k++)for(let j=0;j<12;j++)ws.push(knots[k]+(knots[k+1]-knots[k])*j/12);ws.push(depth);const ys=ws.map(w=>(w-up[0]*(x-A[0]))/up[1]),floor=y=>backFn(x,y)+.3-Math.max(0,Math.min(depth-Math.abs(up[0]*(x-A[0])+up[1]*y),e));rings.push([...ys.map(y=>[x,y,floor(y)]),...ys.toReversed().map(y=>[x,y,backFn(x,y)+.8])]);}
 const body=loft(rings).intersect(cylinder([0,0,-p.diameter],[0,0,p.diameter],p.diameter/2-.1,128));
 const drop=(radius,roof=45)=>{const tangent=90-roof;return[...Array.from({length:37},(_,i)=>{const a=(180-tangent+(180+2*tangent)*i/36)*Math.PI/180;return[radius*Math.cos(a),radius*Math.sin(a)];}),[0,radius/Math.sin(tangent*Math.PI/180)]];};
 const alongRod=poly=>loft([-40,g.lowerEntrance+60].map(l=>poly.map(([t,w])=>at(t,w,l))));
 const screwL=Math.max(1,g.lowerEntrance-10),nutT=-(r+2.8);
 const H=[T[1]*up[2]-T[2]*up[1],T[2]*up[0]-T[0]*up[2],T[0]*up[1]-T[1]*up[0]],C=at(0,0,screwL);
 const screw=loft([-60,0].map(t=>drop(1.7,50).map(([l,w])=>C.map((c,k)=>c+T[k]*t+H[k]*l+up[k]*w))));
 // Square nut entry is open along print-up: no hidden flat pocket ceiling.
 const nut=loft([0,120].map(w=>[[-1.35,-2.9],[1.35,-2.9],[1.35,2.9],[-1.35,2.9]].map(([t,l])=>at(nutT+t,w-2.9,screwL+l))));
 const nutCap=loft(rings.map(ring=>ring.map(([x,y],j)=>[x,y,j<ring.length/2?-p.diameter:backFn(x,y)-.2])));
 return{body,cuts:[alongRod(drop(r)),screw,nut.intersect(nutCap)],frame:{A,axis,T,V,screwL,nutT},depth};
}
