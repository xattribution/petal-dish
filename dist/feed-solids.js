import {solid,box,cylinder,loft} from './solid.js';
export const feedMesh=s=>s.mesh();
export const feedCylinder=cylinder,feedBox=box,feedSolid=solid;
export const feedTransform=(s,fn)=>s.transform(fn);
export function feedSocket(diameter,clearance){
 const ro=diameter/2+2.5;
 let body=feedCylinder([0,0,0],[0,0,22],ro).union(feedBox([0,ro+1.8,12],[4.6,3.8,4.5]));
 const cuts=[feedCylinder([0,0,2],[0,0,24],(diameter+clearance)/2),feedCylinder([0,0,12],[0,ro+7,12],1.7,20),feedCylinder([0,ro+.6,12],[0,ro+3.2,12],5.8/Math.sqrt(3),6),feedBox([3,ro+1.9,12],[3,1.3,3.45])];
 return{body,cuts,ro};
}
export function feedCurvedFoot(r,half,front,focal){
 const rings=[];for(let i=0;i<=8;i++){const x=r-half+2*half*i/8;const ring=[];for(let j=0;j<=8;j++){const y=-14+28*j/8;ring.push([x,y,(x*x+y*y)/(4*focal)+.08]);}for(let j=8;j>=0;j--){const y=-14+28*j/8;ring.push([x,y,(x*x+y*y)/(4*focal)+3.08]);}rings.push(ring);}return loft(rings);
}
