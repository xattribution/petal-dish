// Fastener cuts for the simple mount at the selected bolt sizes. The bundled meshes are the SCAD blanks
// (fasteners = false); this file cuts the same holes cad/simple-mount.scad cuts with fasteners = true, with the
// same transforms and 32-gon circles, so the default sizes reproduce cad/STL/simple-*.stl.
// Sizes: joint = socket heads into the arm inserts, clamp = azimuth and elevation bolts, stand = stand screws,
// hub = the four hub-to-cradle bolts.
import {mountMeshes,mountFrame as F} from './mount-meshes.js';
import {hullPoints,solid} from './solid.js';
import {apply} from './scene.js';
import {FASTENER,insertPilot} from './params.js';
const {PI,cos,sin,sqrt}=Math,deg=PI/180;
// Hole sizes from the fastener table, as cad/simple-mount.scad's clr, cbd, cbh, insd, hexaf, nuth and headk.
export const mountHoles=(m,p)=>{const f=FASTENER[m];return{clear:f.clear,cb:f.capD+1,cbh:f.capK+.5,pilot:p?insertPilot(p,m):f.pilot,af:f.nutAF+.3,nut:f.nutH+.2,head:f.headK+.2};};
export const mountSizes=p=>({joint:p.jointBolt,clamp:p.clampBolt,stand:p.standBolt,hub:p.mountBolt});
// 3 x 4 affine matrices, composed like OpenSCAD's transform chain.
const mul=(A,B)=>A.map(r=>[0,1,2,3].map(j=>r[0]*B[0][j]+r[1]*B[1][j]+r[2]*B[2][j]+(j===3?r[3]:0)));
const T=(x,y,z)=>[[1,0,0,x],[0,1,0,y],[0,0,1,z]];
const Rx=a=>{const c=cos(a*deg),s=sin(a*deg);return[[1,0,0,0],[0,c,-s,0],[0,s,c,0]];};
const Ry=a=>{const c=cos(a*deg),s=sin(a*deg);return[[c,0,s,0],[0,1,0,0],[-s,0,c,0]];};
const Rz=a=>{const c=cos(a*deg),s=sin(a*deg);return[[c,-s,0,0],[s,c,0,0],[0,0,1,0]];};
const Mz=[[1,0,0,0],[0,1,0,0],[0,0,-1,0]];
const chain=(...ms)=>ms.reduce(mul);
// Convex primitives as point sets in their local frame. Circles start at angle 0, like OpenSCAD.
const N=()=>F.cuts.segments,ring=(r,z,n=N(),phase=0)=>Array.from({length:n},(_,i)=>{const a=phase*deg+2*PI*i/n;return[r*cos(a),r*sin(a),z];});
const cyl=(d,z0,z1,n)=>[...ring(d/2,z0,n),...ring(d/2,z1,n)];
const hexPrism=(af,phase,z0,z1)=>[...ring(af/sqrt(3),z0,6,phase),...ring(af/sqrt(3),z1,6,phase)];
// tdrop(d, up): the circle plus a 45° roof whose apex points along `up` degrees
const tdrop=(d,up,z0,z1)=>{const pts=[...Array.from({length:N()},(_,i)=>2*PI*i/N()).map(a=>[d/2*cos(a),d/2*sin(a)]),[d/sqrt(2),0]].map(([x,y])=>[x*cos(up*deg)-y*sin(up*deg),x*sin(up*deg)+y*cos(up*deg)]);return[0,1].flatMap(k=>pts.map(([x,y])=>[x,y,k?z1:z0]));};
// cb_hole(d, cb, cbh, depth, bridge): counterbored hole along +z, head side at z <= 0. With bridge, a slot then a square
// the width of the hole, one step each above the counterbore, so its ceiling prints as short bridges.
const box=(x0,x1,y0,y1,z0,z1)=>[[x0,y0,z0],[x1,y0,z0],[x1,y1,z0],[x0,y1,z0],[x0,y0,z1],[x1,y0,z1],[x1,y1,z1],[x0,y1,z1]];
const cbHole=(d,cb,cbh,depth,bridge)=>{const s=F.cuts.step,c=Math.sqrt(cb*cb-d*d);return[cyl(d,-1,depth),cyl(cb,-1,cbh),...(bridge?[box(-c/2,c/2,-d/2,d/2,cbh-.01,cbh+s),box(-d/2,d/2,-d/2,d/2,cbh+s-.01,cbh+2*s)]:[])];};
const place=(M,...shapes)=>shapes.map(pts=>pts.map(q=>apply(M,q)));
// Cuts for one part in its model frame (world or cradle frame), as lists of points whose hulls are subtracted.
export function mountCuts(part,s,p,{stand=false,baseScrews=true,arc=false}={}){
 const C=F.cuts,j=mountHoles(s.joint,p),c=mountHoles(s.clamp),st=mountHoles(s.stand),h=mountHoles(s.hub),[upIn,upOut]=F.upright,[crIn,crOut]=F.cheek,ins=F.insert.inset,deep=C.pilotDepth,out=[];
 // Seats: p.nutSeat, p.headSeat and p.standSeat set to 1 leave plain holes instead of the nut pocket, the cheek's head
 // pockets and the counterbored stand holes (cad/simple-mount.scad: nut_pocket, head_pockets, stand_counterbore = false).
 if(part==='base'){if(!p?.nutSeat)out.push(...place(chain(T(0,0,-1),Rz(30)),hexPrism(c.af,0,0,c.nut+1.5)));out.push(cyl(c.clear,-1,F.baseT+1));
  if(baseScrews)for(const a of [45,135,225,315])out.push(...place(chain(Rz(a),T(C.baseStandR,0,F.baseT),Mz),...cbHole(st.clear,st.cb,st.cbh,F.baseT+1,true)));}
 if(part==='yoke'){out.push(...place(T(0,0,C.yokeZ-1),cyl(c.clear,0,F.yokeT+C.shoulder+2)));
  for(const [x,y] of F.uprightScrews.at)out.push(...place(T(x,y,C.yokeZ),...cbHole(j.clear,j.cb,j.cbh,F.yokeT+1,true)));
  if(stand)for(const [x,y] of F.standHoles)out.push(...(p?.standSeat?place(T(x,y,C.yokeZ-1),cyl(st.clear,0,F.yokeT+2)):place(chain(T(x,y,C.top),Mz),...cbHole(st.clear,st.cb,st.cbh,F.yokeT+1,false))));}
 if(part==='upright'){out.push(...place(chain(T(upIn-1,0,F.axisZ),Ry(90)),cyl(c.clear,0,upOut-upIn+2)));
  for(const [x,y] of F.uprightScrews.at)out.push(...place(T(x,y,C.top-ins-.01),tdrop(j.pilot,0,0,deep+ins+.01)));}
 if(part==='cradle'){for(const a of [45,135,225,315])out.push(...place(chain(T(C.bcd*cos(a*deg),F.hubL-F.plateT-1,C.bcd*sin(a*deg)),Rx(-90)),cyl(h.clear,0,F.plateT+2)));
  for(const z of F.cheekScrews.z)out.push(...place(chain(T(F.cheekScrews.x,F.hubL,z),Rx(90)),...cbHole(j.clear,j.cb,j.cbh,F.plateT+1,true)));}
 if(part==='cheek'){const at=chain(T(crIn-1,0,0),Ry(90)),A=F.arc,arcAt=chain(T(crIn-1,A.radius*cos(A.phi*deg),A.radius*sin(A.phi*deg)),Ry(90));
  out.push(...place(at,...(p?.headSeat?[]:[hexPrism(c.af,90+30,0,c.head+1)]),cyl(c.clear,0,crOut-crIn+2)));
  if(arc&&!p?.headSeat)out.push(...place(arcAt,hexPrism(A.headAF,90+30,0,A.head+1)));
  for(const z of F.cheekScrews.z)out.push(...place(chain(T(F.cheekScrews.x,F.hubL-F.plateT+ins+.01,z),Rx(90)),tdrop(j.pilot,180,0,deep+ins+.01)));}
 return out;
}
// The bundled blank for each logical variant.
export const blankOf=variant=>({'base-legs':'base','yoke-stand':'yoke'})[variant]??variant;
const invert=M=>{const R=[0,1,2].map(i=>M[i].slice(0,3));return[0,1,2].map(j=>[R[0][j],R[1][j],R[2][j],-(R[0][j]*M[0][3]+R[1][j]*M[1][3]+R[2][j]*M[2][3])]);};
// Print-frame solid of a variant with its holes cut. `add` and `cut` are extra model-frame solids: `add` is unioned
// before the fastener cuts, `cut` subtracted after them.
export function mountSolid(variant,p,{add=null,cut=[]}={}){
 const src=mountMeshes[blankOf(variant)],toPrint=invert(src.toModel),part=blankOf(variant).replace('-arc',''),print=s=>s.transform(q=>apply(toPrint,q));
 let body=solid(src);
 if(add)body=body.union(print(add));
 for(const pts of mountCuts(part,mountSizes(p),p,{stand:variant==='yoke-stand',baseScrews:variant==='base',arc:variant.endsWith('-arc')}))body=body.subtract(hullPoints(pts.map(q=>apply(toPrint,q))));
 for(const c of cut)body=body.subtract(print(c));
 return{body,src};
}
// Print-frame mesh in the shape the app's parts use: {v, f, frame, toModel, source_sha256 (of the blank)}.
export function mountMesh(variant,p,options){const {body,src}=mountSolid(variant,p,options),mesh=body.mesh();return{v:mesh.v,f:mesh.f,frame:src.frame,toModel:src.toModel,source_sha256:src.source_sha256};}
