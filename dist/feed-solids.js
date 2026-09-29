import {CSG} from './csg.js';
// BSP output has T junctions. Split every shared edge before triangulation so
// browser STL exports are conforming meshes, not merely visually closed solids.
export function feedMesh(solid){
 const v=[],bins=new Map(),eps=1e-5,index=p=>{const q=[p.x,p.y,p.z].map(x=>Math.round(x*1e6)/1e6),cell=q.map(x=>Math.floor(x/eps));for(let x=-1;x<=1;x++)for(let y=-1;y<=1;y++)for(let z=-1;z<=1;z++){const bucket=bins.get([cell[0]+x,cell[1]+y,cell[2]+z].join(','));for(const i of bucket||[])if(Math.hypot(...q.map((x,k)=>x-v[i][k]))<eps)return i;}const k=cell.join(',');if(!bins.has(k))bins.set(k,[]);bins.get(k).push(v.length);v.push(q);return v.length-1;};
 const polygons=solid.toPolygons().map(p=>[...new Set(p.vertices.map(v=>index(v.pos)))]),nv=v.length,f=[];
 for(const poly of polygons){const loop=[];for(let j=0;j<poly.length;j++){const ai=poly[j],bi=poly[(j+1)%poly.length],a=v[ai],b=v[bi],d=b.map((x,k)=>x-a[k]),len=d.reduce((s,x)=>s+x*x,0);if(len<1e-12)continue;const edge=[[0,ai]];for(let i=0;i<nv;i++){if(i===ai||i===bi)continue;const w=v[i];if(w[0]<Math.min(a[0],b[0])-eps||w[0]>Math.max(a[0],b[0])+eps||w[1]<Math.min(a[1],b[1])-eps||w[1]>Math.max(a[1],b[1])+eps||w[2]<Math.min(a[2],b[2])-eps||w[2]>Math.max(a[2],b[2])+eps)continue;const x=w[0]-a[0],y=w[1]-a[1],z=w[2]-a[2],t=(x*d[0]+y*d[1]+z*d[2])/len;if(t>1e-7&&t<1-1e-7&&(x-t*d[0])**2+(y-t*d[1])**2+(z-t*d[2])**2<eps*eps)edge.push([t,i]);}edge.sort((a,b)=>a[0]-b[0]);loop.push(...edge.map(x=>x[1]));}
 const uniqueLoop=[...new Set(loop)];loop.length=0;loop.push(...uniqueLoop);if(loop.length<3)continue;if(loop.length===3){f.push(loop);continue;}const c=loop.reduce((s,i)=>s.map((x,k)=>x+v[i][k]/loop.length),[0,0,0]),ci=v.length;v.push(c);for(let i=0;i<loop.length;i++){const a=loop[i],b=loop[(i+1)%loop.length];if(a!==b)f.push([ci,a,b]);}}
 // Collapse sub-0.1-micron Boolean edges after conformity, preserving shared
 // indices across adjacent polygons. Such slivers cannot survive STL float32.
 const parent=v.map((_,i)=>i),root=i=>{while(parent[i]!==i){parent[i]=parent[parent[i]];i=parent[i];}return i;};
 for(const face of f)for(let j=0;j<3;j++){const a=face[j],b=face[(j+1)%3];if(Math.hypot(...v[a].map((x,k)=>x-v[b][k]))<1e-4)parent[root(b)]=root(a);}
 const faces=f.map(face=>face.map(root)).filter(face=>new Set(face).size===3),edges=new Map();
 for(const face of faces){const[a,b,c]=face.map(i=>v[i]),u=b.map((x,k)=>x-a[k]),w=c.map((x,k)=>x-a[k]);if(Math.hypot(u[1]*w[2]-u[2]*w[1],u[2]*w[0]-u[0]*w[2],u[0]*w[1]-u[1]*w[0])<=1e-9)throw Error('Fitting mesh contains a numerical sliver. Adjust socket clearance slightly and regenerate.');for(let j=0;j<3;j++){const a=face[j],b=face[(j+1)%3],k=Math.min(a,b)+':'+Math.max(a,b),e=edges.get(k)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(k,e);}}
 if([...edges.values()].some(([n,w])=>n!==2||w!==0))throw Error('Fitting mesh could not be closed at these parameters. Adjust socket clearance slightly and regenerate.');
 return{v,f:faces};
}
export const feedCylinder=(a,b,r,n=20)=>CSG.cylinder({start:a,end:b,radius:r,slices:n});
export const feedBox=(center,radius)=>CSG.cube({center,radius});
export function feedSolid(mesh){return CSG.fromPolygons(mesh.f.map(f=>new CSG.Polygon(f.map(i=>new CSG.Vertex(new CSG.Vector(...mesh.v[i]),new CSG.Vector(0,0,0))))));}
export function feedTransform(solid,fn){return CSG.fromPolygons(solid.toPolygons().map(p=>new CSG.Polygon(p.vertices.map(v=>new CSG.Vertex(new CSG.Vector(...fn([v.pos.x,v.pos.y,v.pos.z])),new CSG.Vector(0,0,0))))));}
// Compact blind socket with a side-loaded real M3 nut and radial clamping screw.
// Coordinates: rod axis +Z, screw axis Y. No printed threads or long hinges.
export function feedSocket(diameter,clearance){
 const ro=diameter/2+2.5;
 let body=feedCylinder([0,0,0],[0,0,22],ro).union(feedBox([0,ro+1.8,12],[4.6,3.8,4.5]));
 const cuts=[feedCylinder([0,0,2],[0,0,24],(diameter+clearance)/2),feedCylinder([0,0,12],[0,ro+7,12],1.7,20),feedCylinder([0,ro+.6,12],[0,ro+3.2,12],5.8/Math.sqrt(3),6),feedBox([3,ro+1.9,12],[3,1.3,3.45])];
 return{body,cuts,ro};
}
export function feedCurvedFoot(r,half,front,focal){
 const polys=[],vertex=p=>new CSG.Vertex(new CSG.Vector(...p),new CSG.Vector(0,0,0)),face=p=>polys.push(new CSG.Polygon(p.map(vertex))),n=4,point=(i,j)=>{const x=r-half+2*half*i/n,y=-14+28*j/n;return[x,y,(x*x+y*y)/(4*focal)+.08];},up=p=>[p[0],p[1],p[2]+3];
 for(let i=0;i<n;i++)for(let j=0;j<n;j++)face([point(i,j+1),point(i+1,j+1),point(i+1,j),point(i,j)]);
 for(let i=0;i<n;i++)for(let j=0;j<n;j++)face([up(point(i,j)),up(point(i+1,j)),up(point(i+1,j+1)),up(point(i,j+1))]);
 for(let i=0;i<n;i++){let a=point(i,0),b=point(i+1,0);face([a,b,up(b),up(a)]);a=point(n,i);b=point(n,i+1);face([a,b,up(b),up(a)]);a=point(n-i,n);b=point(n-i-1,n);face([a,b,up(b),up(a)]);a=point(0,n-i);b=point(0,n-i-1);face([a,b,up(b),up(a)]);}
 return CSG.fromPolygons(polys);
}
