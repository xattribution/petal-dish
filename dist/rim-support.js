// Breakaway rim support: print only, never part of the assembled dish.
// In the side print every outer petal's rim is a tall edge that rises from the bed and leans in toward the hub, and its
// front (the front lip, or the reflecting face without one) is the part's front-most edge, at one depth all the way up.
// The support is a solid wall supportWall thick standing on the bed just in front of that edge, its face vertical and
// parallel to it, supportGap away. Its outer edge follows the rim; its inner edge runs from the rim's top almost straight
// down to the bed, leaning supportLean out (12° by default), so the wall is wide at the bed and every layer is one solid island (no travel across
// a window, no strings). Tines, 0.3 mm tall (one or two layers), bridge the gap: each narrows from the wall to tineWidth
// where it meets the rim's front, so it snaps there and leaves only a small nub on the rim's outer edge. They run from
// 1 mm above the bed to the top, evenly, never more than tinePitch apart.
// Everything prints without support: the outer edge steps in as it rises, the inner edge leans at most 30°, and a tine is a
// bridge of about 1 mm.
// Coordinates: built in the side print's own frame (X' along the bed, Y' the dish axis toward the back, Z' up, the
// petal's lower seam on the bed) and returned in the petal's frame, so it follows the petal through sidePrint.
import {solid,prism,hullPoints} from './solid.js';
import {zAt,mergeMeshes} from './mesh.js';
// bow: the narrowest the wall gets at the bed, inside the rim; top: its width
// inside the rim at the top; tineH: a tine's height; root: how far a tine starts inside the wall; bite: how far it
// reaches into the rim; edge: how far it stays inside the rim's outer edge; seam: how far tines stay from the seam faces.
export const SUPPORT={bow:5,top:4,tineH:.3,root:.3,bite:.35,edge:.25,seam:.8};
// The support for one outer petal, from its mesh (in the petal's frame). Returns {solid, tines, height} with the solid in
// the petal's frame, or null when the rim is too short. tines:false leaves the tines out (for fit checks).
export function rimSupport(p,n,mesh,{tines:withTines=true}={}){
 const h=Math.PI/n,c=Math.cos(h),s=Math.sin(h),R=p.diameter/2,S=SUPPORT,T=p.supportWall,toLocal=([X,Y,Z])=>[c*X+s*Z,-s*X+c*Z,-Y];
 // bed level and the rim's front edge, the petal's front-most point
 let Zb=Infinity,Yf=Infinity;for(const i of new Set(mesh.f.flat())){const[x,y,z]=mesh.v[i],Z=s*x+c*y;if(Z<Zb)Zb=Z;if(-z<Yf)Yf=-z;}
 const top=2*h,Zt=R*Math.sin(top)-.2,H=Zt-Zb;if(H<20)return null;
 const XR=Z=>Math.sqrt(R*R-Z*Z),face=Yf-p.supportGap;
 // inner edge: from S.top inside the rim at the top, straight down to the bed leaning supportLean out (at least S.bow wide there)
 const Lt=XR(Zt)-S.top,Lb=Math.min(Lt+H*Math.tan(p.supportLean*Math.PI/180),XR(Zb)-S.bow);
 const outline=[[Lb,Zb],[XR(Zb),Zb]],steps=Math.max(8,Math.ceil(H/2));
 for(let i=1;i<steps;i++){const Z=Zb+H*i/steps;outline.push([XR(Z),Z]);}
 outline.push([XR(Zt),Zt],[Lt,Zt]);
 const wall=prism(outline,0,T);
 // prism z (0..T) is the depth in front of the face
 let support=wall.transform(([x,y,z])=>toLocal([x,face-z,y])),count=0;
 if(withTines){
  // the rim's front at radius r (it falls back from the edge as r gets smaller)
  const zR=zAt(R,p),front=r=>Yf+zR-zAt(r,p),tw=p.tineWidth,W0=Math.max(1.2,tw+.6),T2=S.tineH/2,t2=Math.sin(top),c2=Math.cos(top);
  // a tine at height Zk: its narrow end sits S.edge inside the rim's outer edge, clear of both seam faces
  const at=Zk=>{const Z0=Zk-T2,Z1=Zk+T2,xr=XR(Z1)-S.edge,x0=xr-tw;return{Z0,Z1,xr,x0,ok:Z0>=Zb+S.seam&&Z1<=Zt-.3&&-t2*x0+c2*Z1<=-S.seam};};
  const tine=({Z0,Z1,xr,x0})=>{const near=front(Math.hypot(xr,Z1))-.02,far=front(Math.hypot(x0,Z0))+S.bite,rect=(y,a,b)=>[[a,y,Z0],[b,y,Z0],[a,y,Z1],[b,y,Z1]],wide=Math.min(xr-tw/2+W0/2,XR(Z1));
   const taper=hullPoints([...rect(face-S.root,wide-W0,wide),...rect(near,x0,xr)].map(toLocal)),stub=hullPoints([...rect(near-.05,x0,xr),...rect(far,x0,xr)].map(toLocal));
   return taper.union(stub).mesh();};
  // evenly spaced from just above the bed to as high as a tine stays on the rim, never further apart than tinePitch
  const Zf=Zb+1;let Zl=Zt-.5-T2;while(Zl>Zf&&!at(Zl).ok)Zl-=.05;
  if(Zl>Zf){const k=Math.ceil((Zl-Zf)/p.tinePitch-1e-9)+1,parts=[];for(let i=0;i<k;i++){const t=at(Zf+(Zl-Zf)*i/(k-1));if(t.ok)parts.push(tine(t));}
   count=parts.length;if(count)support=support.union(solid(mergeMeshes(parts)));}}
 return{solid:support,tines:count,height:H};}
