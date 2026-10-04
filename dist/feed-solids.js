import {box,cylinder,loft} from './solid.js';
export const feedMesh=s=>s.mesh();
export const feedCylinder=cylinder,feedBox=box;
export const feedTransform=(s,fn)=>s.transform(fn);
// Fixed outer housings. Only rod bores and their retention pockets follow the rod axis.
export function feedRimBody(r,focal){
 const x0=r-30,x1=r+10,rings=[];
 for(let i=0;i<=20;i++){const x=x0+(x1-x0)*i/20,edge=Math.min(x-x0,x1-x),bevel=Math.max(0,3-edge),w=edge<8?8+Math.sqrt(Math.max(0,64-(8-edge)**2)):16,z=y=>(x*x+y*y)/(4*focal)+.08,h=38-16*(x-x0)/(x1-x0);
 rings.push([[-w+3,z(-w+3)+bevel],[-w+bevel,z(-w+bevel)+3],[-w+bevel,z(-w+bevel)+h-3],[-w+3,z(-w+3)+h],[w-3,z(w-3)+h],[w-bevel,z(w-bevel)+h-3],[w-bevel,z(w-bevel)+3],[w-3,z(w-3)+bevel]].map(([y,z])=>[x,y,z]));}return loft(rings);
}
export function feedPuckBody(face){return loft([[-18,26],[-4,40],[13,40],[16,37]].map(([z,r])=>Array.from({length:64},(_,i)=>{const a=i*Math.PI/32;return[r*Math.cos(a),r*Math.sin(a),face+z];})));}
export function feedContinuousSocket(diameter,clearance){const ro=diameter/2+2.5;return{ro,cuts:[
 feedCylinder([0,0,2],[0,0,120],(diameter+clearance)/2,48),
 feedCylinder([0,0,12],[0,120,12],1.7,24),
 feedCylinder([0,ro+5.6,12],[0,120,12],3.3,32),
 feedCylinder([0,ro+.6,12],[0,ro+3.2,12],5.8/Math.sqrt(3),6),
 feedBox([60,ro+1.9,12],[60,1.3,3.45])
 ]};}
export function feedMountPad(r,diameter,focal,thickness){
 const x0=r-16,x1=Math.sqrt((diameter/2)**2-19**2),rear=r*r/(4*focal)-thickness-5,slope=r/(2*focal),rings=[];
 for(let i=0;i<=20;i++){const x=x0+(x1-x0)*i/20,endBevel=Math.max(0,3-Math.min(x-x0,x1-x)),ys=Array.from({length:39},(_,j)=>j-19),bottom=y=>rear+slope*(x-r)+Math.max(endBevel,Math.max(0,Math.abs(y)-16)),top=y=>(x*x+y*y)/(4*focal);
 rings.push([...ys.map(y=>[x,y,bottom(y)]),...ys.toReversed().map(y=>[x,y,top(y)])]);}return loft(rings);
}

export function feedRimEnvelope(x,r,focal){const x0=r-30,step=2,i=Math.max(0,Math.min(19,Math.floor((x-x0)/step))),t=(x-(x0+i*step))/step;
 const sample=X=>{const edge=Math.min(X-x0,r+10-X),bevel=Math.max(0,3-edge),w=edge<8?8+Math.sqrt(Math.max(0,64-(8-edge)**2)):16,base=(X*X+(w-3)**2)/(4*focal)+.08;return[base+bevel,base+38-16*(X-x0)/40];};
 const a=sample(x0+i*step),b=sample(x0+(i+1)*step);return a.map((v,k)=>v+(b[k]-v)*t);
}
