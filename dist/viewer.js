import {scenePoint,dishPoint,explodeOffset,affine} from './scene.js';
// Orthographic preview. Geometry buffers are built once per model, view mode and mesh toggle; selection, explosion
// and camera changes only redraw (explosion is a per-instance rigid offset passed as a uniform).
const vs=`attribute vec3 position;attribute vec3 normal;uniform vec3 center,offset;uniform float yaw,pitch,scale,aspect,depthRange;uniform vec2 pan;varying vec3 n;void main(){vec3 p=position+offset-center;float c=cos(yaw),s=sin(yaw);p=vec3(c*p.x-s*p.y,s*p.x+c*p.y,p.z);c=cos(pitch);s=sin(pitch);p=vec3(p.x,c*p.y+s*p.z,-s*p.y+c*p.z);gl_Position=vec4((p.x+pan.x)/(scale*aspect),(p.y+pan.y)/scale,-p.z/depthRange,1.);n=normal;}`;
const fs=`precision mediump float;varying vec3 n;uniform vec3 color;uniform float unlit;void main(){float light=.38+.62*abs(dot(n/max(length(n),.00001),normalize(vec3(-.35,-.45,1.))));gl_FragColor=vec4(color*mix(light,1.,unlit),1.);}`;
const SELECTED=[.60,.76,1],ZERO=[0,0,0];
const kindColor=(part,support)=>support?[.90,.43,.30]:part.kind==='panel'?(part.row%2?[.48,.63,.85]:[.72,.73,.76]):part.kind==='hub'?[.88,.65,.34]:part.kind==='mount'?[.35,.48,.64]:part.kind==='clip'?[.95,.58,.25]:part.kind==='lever'?(part.flex?[.86,.80,.45]:[.80,.36,.30]):part.kind==='feed'?[.42,.70,.62]:[.46,.48,.54];
// Flat-shaded triangle soup (and optional edge lines) for a mesh under an affine map [[a b c d] x3].
function soup(mesh,M,wire){
 const v=mesh.v,n=v.length,W=new Float64Array(n*3);
 for(let i=0;i<n;i++){const q=v[i];for(let k=0;k<3;k++)W[i*3+k]=M[k][0]*q[0]+M[k][1]*q[1]+M[k][2]*q[2]+M[k][3];}
 const f=mesh.f,positions=new Float32Array(f.length*9),normals=new Float32Array(f.length*9),edges=wire?new Float32Array(f.length*18):null;
 const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
 for(let t=0;t<f.length;t++){const a=f[t][0]*3,b=f[t][1]*3,c=f[t][2]*3;
  const ux=W[b]-W[a],uy=W[b+1]-W[a+1],uz=W[b+2]-W[a+2],vx=W[c]-W[a],vy=W[c+1]-W[a+1],vz=W[c+2]-W[a+2];
  let nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;const l=Math.hypot(nx,ny,nz)||1;nx/=l;ny/=l;nz/=l;
  for(let j=0;j<3;j++){const s=f[t][j]*3,o=t*9+j*3;for(let k=0;k<3;k++){const x=W[s+k];positions[o+k]=x;if(x<min[k])min[k]=x;if(x>max[k])max[k]=x;}normals[o]=nx;normals[o+1]=ny;normals[o+2]=nz;}
  if(edges){const o=t*18,pairs=[a,b,b,c,c,a];for(let j=0;j<6;j++)for(let k=0;k<3;k++)edges[o+j*3+k]=W[pairs[j]+k];}
 }
 return{positions,normals,edges,min,max};
}
export class Viewer{
 constructor(canvas){this.canvas=canvas;this.yaw=-.35;this.pitch=.92;this.zoom=1;this.pan=[0,0];this.navigation='orbit';this.mode='assembled';this.explode=.65;this.selected='';this.focus=false;this.wire=false;this.jointSelection=null;this.frame=0;const gl=canvas.getContext('webgl',{antialias:true,alpha:true});if(!gl)throw Error('3D preview needs WebGL. Geometry and downloads still work.');this.gl=gl;const shader=(type,src)=>{const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;};this.program=gl.createProgram();gl.attachShader(this.program,shader(gl.VERTEX_SHADER,vs));gl.attachShader(this.program,shader(gl.FRAGMENT_SHADER,fs));gl.linkProgram(this.program);if(!gl.getProgramParameter(this.program,gl.LINK_STATUS))throw Error('Preview shader could not link');this.loc={};for(const x of ['yaw','pitch','scale','aspect','center','offset','color','unlit','pan','depthRange'])this.loc[x]=gl.getUniformLocation(this.program,x);this.pos=gl.getAttribLocation(this.program,'position');this.norm=gl.getAttribLocation(this.program,'normal');this.objects=[];this.overlays=[];
 const pointers=new Map();let gesture=null;
 const snapshot=()=>{const points=[...pointers.values()];if(points.length>1){const [a,b]=points;return{x:(a.x+b.x)/2,y:(a.y+b.y)/2,d:Math.hypot(a.x-b.x,a.y-b.y),pan:true};}return points[0]||null;};
 const movePan=(dx,dy)=>{const r=canvas.getBoundingClientRect(),scale=this.extent*.66/this.zoom*Math.max(1,r.height/r.width);this.pan[0]+=dx*2*scale/r.height;this.pan[1]-=dy*2*scale/r.height;};
 canvas.addEventListener('contextmenu',e=>e.preventDefault());
 canvas.addEventListener('pointerdown',e=>{canvas.focus();pointers.set(e.pointerId,{x:e.clientX,y:e.clientY,pan:e.button!==0||e.shiftKey||this.navigation==='pan'});canvas.setPointerCapture(e.pointerId);gesture=snapshot();});
 canvas.addEventListener('pointermove',e=>{if(!pointers.has(e.pointerId))return;const old=pointers.get(e.pointerId);pointers.set(e.pointerId,{...old,x:e.clientX,y:e.clientY});const next=snapshot();if(gesture){const dx=next.x-gesture.x,dy=next.y-gesture.y;if(next.pan)movePan(dx,dy);else{this.yaw+=dx*.008;this.pitch=Math.max(-Math.PI,Math.min(Math.PI,this.pitch+dy*.008));}if(next.d&&gesture.d)this.zoom=Math.max(.05,Math.min(100,this.zoom*next.d/gesture.d));}gesture=next;this.requestDraw();});
 for(const event of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(event,e=>{pointers.delete(e.pointerId);gesture=snapshot();});
 canvas.addEventListener('wheel',e=>{e.preventDefault();const rect=canvas.getBoundingClientRect(),before=this.extent*.66/this.zoom*Math.max(1,rect.height/rect.width);this.zoom=Math.max(.05,Math.min(100,this.zoom*Math.exp(-e.deltaY*.001)));const after=this.extent*.66/this.zoom*Math.max(1,rect.height/rect.width);this.pan[0]+=(e.clientX-rect.left-rect.width/2)*2*(after-before)/rect.height;this.pan[1]-=(e.clientY-rect.top-rect.height/2)*2*(after-before)/rect.height;this.requestDraw();},{passive:false});
 canvas.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','-','=','Home'].includes(e.key))return;e.preventDefault();if(e.shiftKey){const step=30;movePan(e.key==='ArrowLeft'?-step:e.key==='ArrowRight'?step:0,e.key==='ArrowUp'?-step:e.key==='ArrowDown'?step:0);}else{if(e.key==='ArrowLeft')this.yaw-=.1;if(e.key==='ArrowRight')this.yaw+=.1;if(e.key==='ArrowUp')this.pitch+=.1;if(e.key==='ArrowDown')this.pitch-=.1;}if(e.key==='+'||e.key==='=')this.zoom=Math.min(100,this.zoom*1.1);if(e.key==='-')this.zoom=Math.max(.05,this.zoom/1.1);if(e.key==='Home')this.reset();this.requestDraw();});this.resize=new ResizeObserver(()=>this.requestDraw());this.resize.observe(canvas);}
 reset(){this.yaw=-.35;this.pitch=.92;this.zoom=1;this.pan=[0,0];this.requestDraw();}
 // Coalesce bursts of pointer, wheel and slider events into one draw per frame.
 requestDraw(){if(this.frame)return;const raf=globalThis.requestAnimationFrame;if(!raf){this.draw();return;}this.frame=raf(()=>{this.frame=0;this.draw();});}
 setModel(m){this.model=m;this.rebuild();}
 buffer(data){const gl=this.gl,b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);return b;}
 add(list,positions,normals,props){if(!positions.length)return;list.push({p:this.buffer(positions),n:this.buffer(normals||new Float32Array(positions.length)),count:positions.length/3,...props});}
 free(list){for(const o of list){this.gl.deleteBuffer(o.p);this.gl.deleteBuffer(o.n);}list.length=0;}
 rebuild(){if(!this.model)return;const m=this.model;this.free(this.objects);
  const layout=this.mode==='layout',packing=layout&&m.p.packPlates,printParts=m.parts.filter(p=>p.printIncluded!==false);
  const items=packing?m.plates.flatMap((plate,i)=>plate.placements.map((placement,j)=>({part:placement.part,yaw:placement.yaw,i,placement,first:j===0}))):layout?printParts.flatMap((part,i)=>[{part,yaw:0,i,first:true},...part.supportMeshes.map(mesh=>({part,yaw:0,i,supportMesh:mesh}))]):m.instances.map(inst=>({part:inst.part,inst}));
  const bedCount=packing?m.plates.length:printParts.length,cols=Math.max(1,Math.ceil(Math.sqrt(bedCount))),cell=Math.max(m.p.bedX,m.p.bedY)+24;
  for(const item of items){const {part}=item;let M,mesh,dir=ZERO,bed=null;
   if(layout){const c=Math.cos(item.yaw*Math.PI/180),s=Math.sin(item.yaw*Math.PI/180),tr=[(item.i%cols-(cols-1)/2)*cell,(Math.floor(item.i/cols)-(Math.ceil(bedCount/cols)-1)/2)*cell,0];bed=[...tr];if(packing){tr[0]+=item.placement.x;tr[1]+=item.placement.y;}M=[[c,-s,0,tr[0]],[s,c,0,tr[1]],[0,0,1,0]];mesh=item.supportMesh||(packing?part.output:part.print);}
   else{M=affine(q=>scenePoint(m,item.inst,q));mesh=part.mesh;dir=explodeOffset(m,item.inst);}
   const g=soup(mesh,M,this.wire),props={partId:part.id,angle:item.inst?.a??0,support:Boolean(item.supportMesh),base:kindColor(part,item.supportMesh),dir,min:g.min,max:g.max};
   this.add(this.objects,g.positions,g.normals,props);if(g.edges)this.add(this.objects,g.edges,null,{...props,lines:true,base:[.10,.10,.12],edge:true});
   if(bed&&item.first){const x=m.p.bedX/2,y=m.p.bedY/2,c=[[-x,-y],[x,-y],[x,y],[-x,y]],ev=[];for(let k=0;k<4;k++)for(const v of [c[k],c[(k+1)%4]])ev.push(v[0]+bed[0],v[1]+bed[1],-1);this.add(this.objects,new Float32Array(ev),null,{lines:true,base:[.3,.3,.34],dir:ZERO,fixed:true});}
  }
  this.layoutExtent=layout?cell*Math.max(cols,Math.ceil(bedCount/cols)):0;
  this.rebuildOverlays();}
 // Grid, focus marker and rod centerlines.
 rebuildOverlays(){if(!this.model)return;const m=this.model;this.free(this.overlays);this.reach=0;
  if(this.mode!=='layout'){
   if(m.feed&&this.mode==='assembled'){const rods=[];for(const l of m.feed.legs)rods.push(...dishPoint(m,l.lower),...dishPoint(m,l.upper));this.add(this.overlays,new Float32Array(rods),null,{lines:true,base:[.4,.85,.72]});}
   const r=m.p.diameter*.65,step=m.p.diameter/10,grid=[];for(let v=-r;v<=r+1e-9;v+=step)grid.push(-r,v,-18,r,v,-18,v,-r,-18,v,r,-18);this.add(this.overlays,new Float32Array(grid),null,{lines:true,base:[.13,.13,.15],grid:true});
   if(this.focus){const f=m.focal,h=m.p.diameter*.015,lines=[-h,0,f,h,0,f,0,-h,f,0,h,f,0,0,f-h,0,0,f+h];for(let z=0;z<f;z+=f/24)lines.push(0,0,z,0,0,z+f/48);const pts=[];for(let i=0;i<lines.length;i+=3)pts.push(...dishPoint(m,lines.slice(i,i+3)));this.add(this.overlays,new Float32Array(pts),null,{lines:true,base:[1,.7,.3]});this.reach=Math.max(this.reach,f*1.65);}
   if(m.feed&&this.mode==='assembled')this.reach=Math.max(this.reach,m.feed.carrierFace*1.7);
  }
  this.requestDraw();}
 // Framing from cached bounds at the current separation.
 frameScene(){if(this.mode==='layout'){this.center=[0,0,0];this.extent=this.layoutExtent||1;return;}const e=this.mode==='exploded'?this.explode:0,min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];for(const o of this.objects){if(!o.min)continue;for(let k=0;k<3;k++){min[k]=Math.min(min[k],o.min[k]+o.dir[k]*e);max[k]=Math.max(max[k],o.max[k]+o.dir[k]*e);}}if(!Number.isFinite(min[0])){this.center=[0,0,0];this.extent=this.model?.p?.diameter||400;return;}this.center=min.map((v,k)=>(v+max[k])/2);const size=Math.max(...max.map((v,k)=>v-min[k]));this.extent=this.mode==='exploded'?Math.max(this.model.p.diameter*1.3,size)*1.15:Math.max(this.model.p.diameter,size,this.reach||0);}
 color(o){let c=o.base;if(o.edge||o.fixed||o.partId===undefined)return c;if(this.selected&&o.partId!==this.selected)c=c.map(x=>x*.55);if(this.selected===o.partId&&!o.support)c=SELECTED;if(this.jointSelection){const active=this.jointSelection.some(j=>j.part===o.partId&&Math.abs(j.angle-o.angle)<.001);c=active?SELECTED:o.base.map(v=>v*.4);}return c;}
 draw(){const gl=this.gl;if(!this.model)return;if(this.model.p)this.frameScene();const rect=this.canvas.getBoundingClientRect(),dpr=Math.min(2,window.devicePixelRatio||1),w=Math.max(1,Math.round(rect.width*dpr)),h=Math.max(1,Math.round(rect.height*dpr));if(this.canvas.width!==w||this.canvas.height!==h){this.canvas.width=w;this.canvas.height=h;}gl.viewport(0,0,w,h);gl.clearColor(.031,.031,.035,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);gl.disable(gl.CULL_FACE);gl.useProgram(this.program);gl.uniform1f(this.loc.yaw,this.yaw);gl.uniform1f(this.loc.pitch,this.pitch);gl.uniform1f(this.loc.scale,this.extent*.66/this.zoom*Math.max(1,h/w));gl.uniform1f(this.loc.aspect,w/h);gl.uniform2fv(this.loc.pan,this.pan);gl.uniform1f(this.loc.depthRange,this.extent*12);gl.uniform3fv(this.loc.center,this.center||ZERO);gl.enableVertexAttribArray(this.pos);gl.enableVertexAttribArray(this.norm);
  const e=this.mode==='exploded'?this.explode:0,below=Math.cos(this.pitch)<.05;
  for(const o of [...this.objects,...this.overlays]){if(o.grid&&below)continue;gl.bindBuffer(gl.ARRAY_BUFFER,o.p);gl.vertexAttribPointer(this.pos,3,gl.FLOAT,false,0,0);gl.bindBuffer(gl.ARRAY_BUFFER,o.n);gl.vertexAttribPointer(this.norm,3,gl.FLOAT,false,0,0);gl.uniform3fv(this.loc.offset,o.dir?o.dir.map(x=>x*e):ZERO);gl.uniform3fv(this.loc.color,this.color(o));gl.uniform1f(this.loc.unlit,o.lines?1:0);gl.drawArrays(o.lines?gl.LINES:gl.TRIANGLES,0,o.count);}}
}
