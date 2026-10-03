import {solid,box,cylinder,loft} from './solid.js';
export const feedMesh=s=>s.mesh();
export const feedCylinder=cylinder,feedBox=box,feedSolid=solid;
export const feedTransform=(s,fn)=>s.transform(fn);
export function feedSocket(diameter,clearance){
 const ro=diameter/2+2.5;
 // Taper the nut housing into the barrel over its length. Keep the pocket and
 // screw-face datums unchanged, so the existing M3 hardware still fits.
 const shoulder=loft([[1,2,ro+.4],[7.5,4.6,ro+5.6],[16.5,4.6,ro+5.6],[21,2,ro+.4]].map(([z,w,y])=>[[-w,0,z],[w,0,z],[w,y,z],[-w,y,z]]));
 let body=feedCylinder([0,0,0],[0,0,22],ro).union(shoulder);
 const cuts=[feedCylinder([0,0,2],[0,0,24],(diameter+clearance)/2),feedCylinder([0,0,12],[0,ro+7,12],1.7,20),feedCylinder([0,ro+.6,12],[0,ro+3.2,12],5.8/Math.sqrt(3),6),feedBox([3,ro+1.9,12],[3,1.3,3.45])];
 return{body,cuts,ro};
}
export function feedCurvedFoot(r,inner,focal){
 const rings=[];for(let i=0;i<=12;i++){const x=r-inner+(inner+10)*i/12;const ring=[];for(let j=0;j<=8;j++){const y=-14+28*j/8;ring.push([x,y,(x*x+y*y)/(4*focal)+.08]);}for(let j=8;j>=0;j--){const y=-14+28*j/8;ring.push([x,y,(x*x+y*y)/(4*focal)+3.08]);}rings.push(ring);}return loft(rings);
}
// A continuous curved saddle, rather than a small round pedestal, carries the
// lower barrel into the sole. Bolt recesses are cut after the saddle union.
export function feedRimSaddle(r,lowerZ,angle,ro,focal){
 const inner=18*Math.cos(angle),width=ro,rings=[];
 for(let i=0;i<=12;i++){
  const x=r-inner+(inner+8)*i/12,base=y=>(x*x+y*y)/(4*focal)+.15;
  const roof=Math.max(base(width)+3,lowerZ+(r-x)*Math.tan(angle)+ro*.65);
  // The +14 cheek is the side-print bed face. Its 45-degree shoulder grows
  // inward from it; the upper cheek tapers straight to the sole to save plastic.
  const edge=Math.max(base(14)+3,roof-(14-width));
  rings.push([[-14,base(-14)],[-width,base(-width)],[0,base(0)],[width,base(width)],[14,base(14)],[14,edge],[width,roof],[-width,roof],[-14,base(-14)+3]].map(([y,z])=>[x,y,z]));
 }
 return loft(rings);
}
// Radial webs support each upper socket from the puck. The outer roof falls at
// 45 degrees: when the puck is inverted for printing it forms a rising ramp.
export function feedPuckWeb(face,angle,ro){
 const end=18+18*Math.cos(angle),width=ro-.8;
 return loft([18,26,end].sort((a,b)=>a-b).map(r=>{
  const bottom=face+8-(r-18)*Math.tan(angle)-.5;
  const top=Math.max(bottom+1,face+16-Math.max(0,r-26));
  return [[r,-width,bottom],[r,width,bottom],[r,width,top],[r,-width,top]];
 }));
}
