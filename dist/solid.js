import {compactMesh} from './mesh.js';
import Module from 'manifold-3d';
const binary=globalThis.PETAL_WASM_BASE64;
const kernel=await Module(binary?{wasmBinary:Uint8Array.from(atob(binary),c=>c.charCodeAt(0))}:{});
kernel.setup();
const {Manifold,Mesh}=kernel;
let owned=[];
export function solidScope(fn){const previous=owned;owned=[];try{return fn();}finally{for(const x of owned.reverse())x.delete();owned=previous;}}
class Solid{
 constructor(raw){this.raw=raw;owned.push(raw);}
 hull(){return new Solid(this.raw.hull());}
 union(b){return new Solid(this.raw.add(b.raw));}
 subtract(b){return new Solid(this.raw.subtract(b.raw));}
 intersect(b){return new Solid(this.raw.intersect(b.raw));}
 trim(n,d){return new Solid(this.raw.trimByPlane(n,d));}
 transform(fn){const o=fn([0,0,0]),basis=[[1,0,0],[0,1,0],[0,0,1]].map(v=>fn(v).map((x,k)=>x-o[k]));return new Solid(this.raw.transform([...basis[0],0,...basis[1],0,...basis[2],0,...o,1]));}
 mesh(){const clean=this.raw.simplify(.0001);owned.push(clean);const m=clean.getMesh();return compactMesh({v:Array.from({length:m.vertProperties.length/m.numProp},(_,i)=>Array.from(m.vertProperties.slice(i*m.numProp,i*m.numProp+3))),f:Array.from({length:m.triVerts.length/3},(_,i)=>Array.from(m.triVerts.slice(i*3,i*3+3)))});}
}
export const solid=mesh=>new Solid(new Manifold(new Mesh({numProp:3,vertProperties:Float32Array.from(mesh.v.flat()),triVerts:Uint32Array.from(mesh.f.flat())})));
export const box=(center,radius)=>new Solid(Manifold.cube(radius.map(x=>2*x),true)).transform(v=>v.map((x,k)=>x+center[k]));
export function cylinder(a,b,r,n=32){const axis=b.map((x,k)=>x-a[k]),l=Math.hypot(...axis),w=axis.map(x=>x/l),seed=Math.abs(w[2])<.9?[0,0,1]:[1,0,0],u=[seed[1]*w[2]-seed[2]*w[1],seed[2]*w[0]-seed[0]*w[2],seed[0]*w[1]-seed[1]*w[0]],ul=Math.hypot(...u);u.forEach((x,k)=>u[k]=x/ul);const v=[w[1]*u[2]-w[2]*u[1],w[2]*u[0]-w[0]*u[2],w[0]*u[1]-w[1]*u[0]];return new Solid(Manifold.cylinder(l,r,r,n)).transform(q=>a.map((x,k)=>x+u[k]*q[0]+v[k]*q[1]+w[k]*q[2]));}
// Loft matching convex polygons. Determine orientation from signed volume.
export function loft(rings){const v=rings.flat(),n=rings[0].length,f=[];for(let j=1;j<n-1;j++){f.push([0,j+1,j]);const k=(rings.length-1)*n;f.push([k,k+j,k+j+1]);}for(let k=0;k<rings.length-1;k++)for(let j=0;j<n;j++){const a=k*n+j,b=k*n+(j+1)%n,c=b+n,d=a+n;f.push([a,b,c],[a,c,d]);}let vol=0;for(const[i,j,k]of f){const a=v[i],b=v[j],c=v[k];vol+=a[0]*(b[1]*c[2]-b[2]*c[1])+a[1]*(b[2]*c[0]-b[0]*c[2])+a[2]*(b[0]*c[1]-b[1]*c[0]);}if(vol<0)f.forEach(x=>x.reverse());return solid({v,f});}
export const sphere=(c,r,n=32)=>new Solid(Manifold.sphere(r,n)).transform(v=>v.map((x,k)=>x+c[k]));
