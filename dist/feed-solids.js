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
// Two small circular tapers join the rod bearing and side insert to the shell.
// Only the bore follows the rod angle; the exterior follows the dish underside.
export function feedIntegralSocket(p,g,n,backFn){
 const h=Math.PI/n,up=[Math.sin(h),Math.cos(h),0],A=[g.datum.r,0,g.lowerZ],B=[18,0,g.upperZ],axis=B.map((v,k)=>(v-A[k])/g.pivotDistance),dot=axis.reduce((s,v,k)=>s+v*up[k],0),v0=up.map((v,k)=>v-dot*axis[k]),vl=Math.hypot(...v0),V=v0.map(v=>v/vl);
 let T=[axis[1]*V[2]-axis[2]*V[1],axis[2]*V[0]-axis[0]*V[2],axis[0]*V[1]-axis[1]*V[0]];if(T[2]<0)T=T.map(v=>-v);
 const at=(t,w,l)=>A.map((a,k)=>a+T[k]*t+V[k]*w+axis[k]*l),r=(g.rodDiameter+p.rodClearance)/2,depth=3+r+1.2;
 const collar=(cx,cy,inner,d)=>{const outer=inner+(d+.3)/.95;return loft([[inner,-d],[outer,.3],[outer,.5],[outer*.75,.5],[outer*.5,.5],[outer*.25,.5],[.1,.5]].map(([radius,z])=>Array.from({length:63},(_,i)=>{const a=(i+.17)*2*Math.PI/63,x=cx+radius*Math.cos(a),y=cy+radius*Math.sin(a);return[x,y,backFn(x,y)+z];})));};
 const earY=-(r+3.6),earDepth=3+2.1*Math.hypot(1,g.datum.slope)+1.4;
 const body=collar(A[0],0,r+.7,depth).union(collar(A[0],earY,2.7,earDepth)).intersect(cylinder([0,0,-p.diameter],[0,0,p.diameter],p.diameter/2-.1,128));
 // Circular bearing with a short 0.8 mm bridge at the print-up roof.
 // The 45-degree shoulders enclose the complete nominal clearance circle.
 const roof=r*Math.SQRT2-.4,poly=[...Array.from({length:37},(_,i)=>{const a=(135+270*i/36)*Math.PI/180;return[r*Math.cos(a),r*Math.sin(a)];}),[.4,roof],[-.4,roof]];
 const bore=loft([-40,g.lowerEntrance+60].map(l=>poly.map(([t,w])=>at(t,w,l))));
 const pilotEnd=-r-2.5,shoulderEnd=-r-.5,C=at(0,0,1.3);
 const screw=loft([[-100,2.1],[pilotEnd,2.1],[shoulderEnd,1.7],[0,1.7]].map(([y,radius])=>Array.from({length:48},(_,i)=>{const a=i*Math.PI/24;return[C[0]+radius*Math.cos(a),y,C[2]+radius*Math.sin(a)];})));
 return{body,cuts:[bore,screw],frame:{A,axis,T,V,screwL:1.3,insertAxis:[0,1,0],pilotEnd},depth};
}
