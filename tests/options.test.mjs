import assert from 'node:assert/strict';
import {build,defaults,volume,bounds,packedPlateMesh,zAt,JOINT,connectionCoupon} from '../dist/geometry.js';
function closed(mesh){const edges=new Map();for(const f of mesh.f){const[a,b,c]=f.map(i=>mesh.v[i]),u=b.map((x,i)=>x-a[i]),v=c.map((x,i)=>x-a[i]);assert(Math.hypot(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])>1e-9,'Nondegenerate triangles');for(let j=0;j<3;j++){const a=f[j],b=f[(j+1)%3],key=[Math.min(a,b),Math.max(a,b)].join(':');const e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}}for(const [count,w]of edges.values())assert(count===2&&w===0,'Watertight oriented surface');assert(volume(mesh)>0);}
function ray(mesh,x,y){const zs=[];for(const f of mesh.f){const[a,b,c]=f.map(i=>mesh.v[i]),den=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1]);if(Math.abs(den)<1e-9)continue;const u=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/den,v=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/den;if(u>=-1e-7&&v>=-1e-7&&u+v<=1+1e-7)zs.push(u*a[2]+v*b[2]+(1-u-v)*c[2]);}return zs.sort((a,b)=>a-b).filter((z,i,a)=>!i||z-a[i-1]>1e-5);}
for(const cfg of [{perforate:1},{perforate:1,jointStyle:0},{fastenerStyle:1},{fastenerStyle:2},{fastenerStyle:2,perforate:1,supports:1},{fastenerStyle:1,perforate:1,rearStyle:1},{fastenerStyle:2,diameter:600,fd:.25,rows:3},{fastenerStyle:2,diameter:180,thickness:1.6,insertDepth:12,insertDiameter:6.5}]){
 const m=build({...defaults,...cfg,packPlates:1});
 for(const p of m.parts){closed(p.mesh);closed(p.output);
  for(const h of p.spec.perforations||[]){assert.equal(ray(p.mesh,h.r*Math.cos(h.a),h.r*Math.sin(h.a)).length,0,'Perforation passes through');assert(h.r>=84&&h.r<=p.spec.r1-14);}
  if(p.spec.pockets)for(const h of p.spec.pockets){
   const x=h.r*Math.cos(h.a),y=h.r*Math.sin(h.a);
   if(m.p.fastenerStyle===2){
    const zs=ray(p.mesh,x,y);assert.equal(zs.length,2,'Blind pocket has a closed face');assert(zs[1]-zs[0]>=1.99,'At least 2 mm front skin');
    assert(Math.abs(zs[1]-zAt(h.r,m.p))<.06,'Front follows the parabola');
    const entry=p.kind==='panel'?p.spec.backFn(x,y)+JOINT.seatDepth:p.spec.levels[1](x,y);
    assert(zs[0]-entry>=m.p.insertDepth-.03,'Insert cavity clears its configured minimum depth');
   }else{
    assert.equal(ray(p.mesh,x,y).length,0,'Captured nut keeps a through bore');
    const xx=x+3*Math.cos(h.a),yy=y+3*Math.sin(h.a),zs=ray(p.mesh,xx,yy);
    assert(zs.length>=2);assert(Math.abs(zs.at(-1)-p.spec.pocketFloor(xx,yy))<.03,'Nut sits on a flat bearing face');
    assert(zs.at(-1)+3.2<zAt(h.r-h.w/2,m.p),'Nut stays below the front');
   }
  }
 }
 const counts=new Map();
 for(const plate of m.plates){
  for(const a of plate.placements){counts.set(a.part.id,(counts.get(a.part.id)||0)+1);const[x,y,w,h]=a.bounds;assert(x>=-1e-6&&y>=-1e-6&&x+w<=m.p.bedX-2*m.p.margin+1e-6&&y+h<=m.p.bedY-2*m.p.margin+1e-6);}
  for(let i=0;i<plate.placements.length;i++)for(let j=0;j<i;j++){const a=plate.placements[i].bounds,b=plate.placements[j].bounds;assert(a[0]+a[2]+6<=b[0]+1e-6||b[0]+b[2]+6<=a[0]+1e-6||a[1]+a[3]+6<=b[1]+1e-6||b[1]+b[3]+6<=a[1]+1e-6,'Packed copies retain 6 mm spacing');}
  const mesh=packedPlateMesh(plate),b=bounds(mesh);assert(b.min[0]>=-m.p.bedX/2+m.p.margin-1e-5&&b.max[0]<=m.p.bedX/2-m.p.margin+1e-5);assert(b.min[1]>=-m.p.bedY/2+m.p.margin-1e-5&&b.max[1]<=m.p.bedY/2-m.p.margin+1e-5);
 }
 for(const p of m.parts)assert.equal(counts.get(p.id),p.qty,'Packing accounts for every copy');
 for(const p of connectionCoupon(m))closed(p.output);
 console.log('PASS optional geometry, hardware cavities and packing',cfg,'beds',m.plates.length);
}
