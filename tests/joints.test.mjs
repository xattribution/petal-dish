import assert from 'node:assert/strict';
import {build,defaults,keyCenters,jointsFit} from '../dist/geometry.js';
// Intersect actual triangle meshes along Z; both sides of every key/pad
// are sampled instead of assuming the analytic mating profiles are enough.
function intervals(mesh){const buckets=new Map();for(const f of mesh.f){const [a,b,c]=f.map(i=>mesh.v[i]),d=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1]);if(Math.abs(d)<1e-10)continue;const t={a,b,c,d};for(let x=Math.floor(Math.min(a[0],b[0],c[0])/4);x<=Math.floor(Math.max(a[0],b[0],c[0])/4);x++)for(let y=Math.floor(Math.min(a[1],b[1],c[1])/4);y<=Math.floor(Math.max(a[1],b[1],c[1])/4);y++){const key=x+':'+y;if(!buckets.has(key))buckets.set(key,[]);buckets.get(key).push(t);}}
 return(x,y)=>{const zs=[];for(const{a,b,c,d}of buckets.get(Math.floor(x/4)+':'+Math.floor(y/4))||[]){const u=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/d,v=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/d;if(u>=-1e-8&&v>=-1e-8&&u+v<=1+1e-8)zs.push(u*a[2]+v*b[2]+(1-u-v)*c[2]);}zs.sort((a,b)=>a-b);return zs.filter((z,i)=>!i||z-zs[i-1]>1e-6);};}
for(const cfg of [{},{rearStyle:1},{diameter:800,rearStyle:1},{diameter:600,rows:3,rearStyle:1,connectorSpacing:80},{diameter:800,staggerRings:0},{diameter:800,staggerRings:0,adaptiveJoints:0},{diameter:180,thickness:1.6,jointClearance:.1},{diameter:600,fd:.25,jointClearance:.4}]){
 const m=build({...defaults,...cfg}),n=m.layout.n,k=m.layout.rows,panels=m.parts.filter(p=>p.kind==='panel'),rays=panels.map(p=>intervals(p.mesh));let samples=0,minGap=Infinity;
 for(const part of m.parts.filter(p=>p.kind==='bridge'||p.id==='hub-rear')){const lookup=intervals(part.mesh),angle=m.instances.find(i=>i.part.id===part.id).a,c=Math.cos(angle),s=Math.sin(angle);const points=part.mesh.f.map(f=>[0,1].map(i=>f.reduce((sum,v)=>sum+part.mesh.v[v][i],0)/3));
 for(const[x,y]of points){const a=lookup(x,y);if(a.length<2)continue;const X=c*x-s*y,Y=s*x+c*y,theta=Math.atan2(Y,X);
 for(let row=0;row<rays.length;row++){const phase=m.ringPhases[row],i=Math.round((theta-phase)/(2*Math.PI/n)),rot=i*2*Math.PI/n+phase,u=Math.cos(rot)*X+Math.sin(rot)*Y,v=-Math.sin(rot)*X+Math.cos(rot)*Y,b=rays[row](u,v);if(b.length<2)continue;const gap=b[0]-a.at(-1);assert(gap>=-.002,`${part.id} collision ${gap} at ${x},${y}`);minGap=Math.min(minGap,gap);samples++;}}
 }
 assert(samples>100);assert.equal(m.bolts,n+m.parts.filter(p=>p.kind==='bridge').reduce((s,p)=>s+p.qty*(p.id.startsWith('side-')?2:4),0));assert(jointsFit(m.p,n,k));
 for(const p of panels){assert.equal(p.spec.centers.length,keyCenters(m.p,n,k,p.row).length);for(const[r,a]of p.spec.centers){const x=(r+3)*Math.cos(a),y=(r+3)*Math.sin(a),zs=rays[p.row](x,y);assert(zs.length>=2);const rear=p.spec.backFn(x,y);assert(zs[0]>=rear-.401,'Socket must stop below the original shell');}}
 console.log('PASS keyed mating meshes',cfg,'samples',samples,'minimum gap',minGap);
}
