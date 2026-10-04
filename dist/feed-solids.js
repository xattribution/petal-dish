import {box,cylinder,loft} from './solid.js';
export const feedMesh=s=>s.mesh();
export const feedCylinder=cylinder,feedBox=box;
export const feedTransform=(s,fn)=>s.transform(fn);
// Thin bearing feet and tapered socket lobes, blended by convex envelopes.
export function feedRimBody(r,focal){const rings=[],x0=r-18,x1=r+10;
 for(let i=0;i<=20;i++){const x=x0+(x1-x0)*i/20,edge=Math.min(x-x0,x1-x),w=edge<4?10.5+Math.sqrt(Math.max(0,16-(4-edge)**2)):14.5,ys=Array.from({length:31},(_,j)=>-w+2*w*j/30),floor=y=>(x*x+y*y)/(4*focal)+.08,bevel=y=>Math.max(0,.8-Math.min(edge,w-Math.abs(y)));
 rings.push([...ys.map(y=>[x,y,floor(y)+bevel(y)]),...ys.toReversed().map(y=>[x,y,floor(y)+3])]);}return loft(rings);}
export function feedPuckBody(face,secondary=false){const h=16;return loft([[0,21],[1,22],[h-1,22],[h,21]].map(([z,r])=>Array.from({length:64},(_,i)=>{const a=i*Math.PI/32;return[r*Math.cos(a),r*Math.sin(a),face+z];})));}
export function feedSocketBody(diameter){const ro=diameter/2+2.5;
 return loft([[0,ro-.7,.8],[2,ro,1.8],[7,ro,4.7],[12,ro,5.6],[17,ro,4.7],[20.5,ro,1.8],[22,ro-.7,.8]].map(([z,r,bulge])=>Array.from({length:48},(_,i)=>{const a=i*Math.PI/24,s=Math.sin(a),positive=Math.max(0,s);return[(r+3.5*positive)*Math.cos(a),r*s+bulge*positive*positive,z];})));}
export function feedContinuousSocket(diameter,clearance){const ro=diameter/2+2.5;return{ro,cuts:[
 feedCylinder([0,0,2],[0,0,120],(diameter+clearance)/2,48),
 feedCylinder([0,0,12],[0,120,12],1.7,24),
 feedCylinder([0,ro+5.6,12],[0,120,12],3.3,32),
 feedCylinder([0,ro+.6,12],[0,ro+3.2,12],5.8/Math.sqrt(3),6),
 feedBox([60,ro+1.9,12],[60,1.3,3.45])
 ]};}
export function feedMountPad(r,diameter,focal,thickness){
 const x0=r-14,x1=Math.sqrt((diameter/2)**2-17**2),rear=r*r/(4*focal)-thickness-5,slope=r/(2*focal),rings=[];
 for(let i=0;i<=20;i++){const x=x0+(x1-x0)*i/20,endBevel=Math.max(0,3-Math.min(x-x0,x1-x)),ys=Array.from({length:35},(_,j)=>j-17),bottom=y=>rear+slope*(x-r)+Math.max(endBevel,Math.max(0,Math.abs(y)-14)),top=y=>(x*x+y*y)/(4*focal);
 rings.push([...ys.map(y=>[x,y,bottom(y)]),...ys.toReversed().map(y=>[x,y,top(y)])]);}return loft(rings);
}
