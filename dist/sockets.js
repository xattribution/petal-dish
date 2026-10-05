// Shared tube sockets: tapered octagonal sleeves for tripod legs, the insert mast foot and the insert cup.
// Octagon flats face ±x and ±y in the socket frame, so a cross bolt along y bears on two flat faces.
import {solid,loft,cylinder} from './solid.js';
const PI=Math.PI;
export const octagon=(af,z)=>Array.from({length:8},(_,i)=>{const a=PI/8+i*PI/4,r=af/2/Math.cos(PI/8);return[r*Math.cos(a),r*Math.sin(a),z];});
// Frustum along +z from z0 (across flats af0) to z1 (af1).
export const octFrustum=(af0,af1,z0,z1)=>loft([octagon(af0,z0),octagon(af1,z1)]);
// Teardrop prism along an axis: circle of radius r plus a 45° roof pointing toward `up` (projected off the axis),
// so a near-horizontal hole prints without support. Points p0 -> p1.
export function teardrop(p0,p1,r,up,n=32){
 const ax=p1.map((v,k)=>v-p0[k]),L=Math.hypot(...ax),w=ax.map(v=>v/L),d=up.reduce((s,v,k)=>s+v*w[k],0);
 let u=up.map((v,k)=>v-d*w[k]);const ul=Math.hypot(...u);u=u.map(v=>v/ul);const t=[w[1]*u[2]-w[2]*u[1],w[2]*u[0]-w[0]*u[2],w[0]*u[1]-w[1]*u[0]];
 // circle from 135° round to 405° (t = x, u = y), then the roof apex on +u
 const poly=[...Array.from({length:n+1},(_,i)=>{const a=(135+270*i/n)*PI/180;return[r*Math.cos(a),r*Math.sin(a)];}),[0,r*Math.SQRT2]];
 const pts=z=>poly.map(([x,y])=>p0.map((v,k)=>v+x*t[k]+y*u[k]+z*w[k]));
 return loft([pts(0),pts(L)]);
}
// Radial M3 set screw into a short heat-set insert (≤ 4 mm, Ø4.2 pilot) in a vertical sleeve: bore along +z,
// screw along +y at height z, sleeve face at y = outer. Both holes are teardrops with the roof on +z.
export function insertScrewCuts(boreR,z,outer){
 const seat=boreR+1.5;return[teardrop([0,outer+1,z],[0,seat,z],2.1,[0,0,1]),teardrop([0,seat+.01,z],[0,boreR-1,z],1.7,[0,0,1])];
}
export const tube=(r,z0,z1,n=64)=>cylinder([0,0,z0],[0,0,z1],r,n);
export {solid};
