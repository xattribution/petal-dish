"""Render the actual generated meshes; no generative imagery or inferred geometry."""
import json,subprocess
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import trimesh
root=Path(__file__).resolve().parents[1]
def model(mode):
 code="import{build,defaults}from'./dist/geometry.js';let m=build({...defaults,feedMode:"+str(mode)+"});console.log(JSON.stringify({parts:m.parts.map(p=>({id:p.id,kind:p.kind,mesh:p.mesh})),instances:m.instances.map(i=>({id:i.part.id,a:i.a})),feed:m.feed}));"
 return json.loads(subprocess.check_output(['node','--input-type=module','-e',code],cwd=root))
def add(ax,v,f,color):
 poly=Poly3DCollection(np.asarray(v)[np.asarray(f)],facecolors=color,linewidth=0,antialiased=False,shade=True,lightsource=matplotlib.colors.LightSource(315,45));poly.set_edgecolor('none');ax.add_collection3d(poly)
fig=plt.figure(figsize=(12,6.5),facecolor='#f4f5f7')
for i,mode in enumerate([1,2]):
 m=model(mode);ax=fig.add_subplot(1,2,i+1,projection='3d');ax.set_facecolor('#f4f5f7');parts={p['id']:p for p in m['parts']}
 for inst in m['instances']:
  p=parts[inst['id']];v=np.asarray(p['mesh']['v']);a=inst['a'];c,s=np.cos(a),np.sin(a);v=v@np.array([[c,s,0],[-s,c,0],[0,0,1]])
  color='#aebccf' if p['kind']=='panel' else '#658fa6' if p['kind']=='feed' else '#b3a184'
  add(ax,v,p['mesh']['f'],color)
 for leg in m['feed']['legs']:
  a,b=np.asarray(leg['lower']),np.asarray(leg['upper']);u=(b-a)/np.linalg.norm(b-a);a=a+u*44;b=b-u*44;ax.plot(*np.array([a,b]).T,color='#34444a',linewidth=4,zorder=100)
 ax.set_xlim(-205,205);ax.set_ylim(-205,205);ax.set_zlim(-20,210);ax.set_box_aspect((410,410,230),zoom=1.28);ax.view_init(elev=23,azim=-60);ax.set_axis_off()
 ax.set_title('Prime-focus carrier' if mode==1 else 'Cassegrain secondary',fontsize=15,pad=-5)
 ax.text2D(.5,-.015,f"3 × Ø6.35 metal rods · cut {m['feed']['cutLength']:.2f} mm",transform=ax.transAxes,ha='center',fontsize=10)
fig.suptitle('PETAL 4.1 — actual default 400 mm support geometry',fontsize=19,y=.97)
fig.text(.5,.045,'RF feed and fasteners not shown. Rods are reference lines; these are untested mechanical prototypes.',ha='center',fontsize=10,color='#4b5563')
fig.subplots_adjust(left=0,right=1,bottom=.12,top=.87,wspace=-.04)
fig.savefig(root/'docs/feed-support.png',dpi=160);plt.close(fig)
# Two real printable parts, shown in dish-local coordinates, not a fabricated geared mechanism.
ring=trimesh.load(root/'cad/STL/hub-ring.stl');ring.vertices[:,2]-=10
cradle=trimesh.load(root/'cad/STL/cradle.stl');v=cradle.vertices.copy();cradle.vertices=np.column_stack([v[:,0],v[:,2]-100,-v[:,1]])
fig=plt.figure(figsize=(7,6),facecolor='#f4f5f7');ax=fig.add_subplot(projection='3d');ax.set_facecolor('#f4f5f7')
# Display x horizontal, -dish-z depth, dish-y up.
for m,col in [(ring,'#b3a184'),(cradle,'#658fa6')]:
 v=m.vertices;add(ax,np.column_stack([v[:,0],-v[:,2],v[:,1]]),m.faces,col)
ax.set_xlim(-65,65);ax.set_ylim(-15,95);ax.set_zlim(-105,50);ax.set_box_aspect((130,110,155));ax.view_init(elev=18,azim=-45);ax.set_axis_off();ax.set_title('Open-center geared-head adapter',fontsize=17,pad=10)
fig.text(.5,.07,'Ø34 clear adapter port · 4 × M4 / 60 mm BCD\nPurchased geared head mounts below the foot; not shown.',ha='center',fontsize=11)
fig.subplots_adjust(left=0,right=1,bottom=.13,top=.91);fig.savefig(root/'docs/manual-aiming-adapter.png',dpi=160);plt.close(fig)
