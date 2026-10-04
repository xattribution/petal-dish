export const rad=d=>d*Math.PI/180;
export const rz=(q,a)=>[Math.cos(a)*q[0]-Math.sin(a)*q[1],Math.sin(a)*q[0]+Math.cos(a)*q[1],q[2]];
export const rx=(q,a)=>[q[0],Math.cos(a)*q[1]-Math.sin(a)*q[2],Math.sin(a)*q[1]+Math.cos(a)*q[2]];
export const affine=(fn)=>{const o=fn([0,0,0]),b=[[1,0,0],[0,1,0],[0,0,1]].map(q=>fn(q).map((x,k)=>x-o[k]));return [0,1,2].map(k=>[b[0][k],b[1][k],b[2][k],o[k]]);};
export const apply=(mat,q)=>mat?mat.map(r=>r[0]*q[0]+r[1]*q[1]+r[2]*q[2]+r[3]):q;
export const localPoint=(i,q)=>rz(apply(i.matrix,q),i.a||0);
// Exploded mount: [offset in the azimuth-turned frame, optional offset in the cradle frame (turned with elevation)], mm.
// Separates each joint: base | yoke | upright (Z), upright | cheek clamp (-X), cheek | cradle plate (along the boresight).
const MOUNT_EXPLODE={'mount-base':[[0,0,-60]],'mount-yoke':[[0,0,-30]],'mount-upright':[[0,0,0]],'mount-cradle':[[-30,0,25]],'mount-cheek':[[-30,0,25],[0,-30,0]]};
export function dishPoint(m,q){if(!m.p.mountMode)return q;const offset=75+16-45**2/(4*m.focal);let v=rx(q,-Math.PI/2);v[1]+=offset;v=rx(v,rad(m.p.elevation));v[2]+=94;return rz(v,rad(-m.p.azimuth));}
// Exploded views move every instance rigidly: this is its world-space offset at full separation.
// Petals and their clips/levers move out along their petal's azimuth (including the petal at azimuth 0).
export function explodeOffset(m,i){const p=i.part;
 if(p.kind==='mount'){const[w,c=[0,0,0]]=MOUNT_EXPLODE[p.id]||[[0,0,0]];return rz(rx(c,rad(m.p.elevation)).map((x,k)=>x+w[k]),rad(-m.p.azimuth));}
 const a=i.a??0,s=m.p.diameter*.16,delta=p.kind==='hub'?[0,0,-s]:p.kind==='feed'?[0,0,s]:[Math.cos(a)*s,Math.sin(a)*s,s*.3*(Math.max(0,p.row)+1)],o=dishPoint(m,[0,0,0]);
 return dishPoint(m,delta).map((x,k)=>x-o[k]);}
export function scenePoint(m,i,q,explode=0){const v=localPoint(i,q),w=i.part.kind==='mount'?v:dishPoint(m,v);if(!explode)return w;const o=explodeOffset(m,i);return w.map((x,k)=>x+o[k]*explode);}
export function sceneBounds(m,explode=0){const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];for(const i of m.instances){const o=explode?explodeOffset(m,i):[0,0,0];for(const q of i.part.mesh.v){const v=scenePoint(m,i,q);for(let k=0;k<3;k++){min[k]=Math.min(min[k],v[k]+o[k]*explode);max[k]=Math.max(max[k],v[k]+o[k]*explode);}}}return{min,max,size:max.map((x,k)=>x-min[k])};}
