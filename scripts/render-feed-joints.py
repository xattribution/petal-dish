"""Render actual feed meshes, including their exported print orientations."""
import json,subprocess
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
root=Path(__file__).resolve().parents[1]
code="import{build,defaults}from'./dist/geometry.js';let m=build({...defaults,feedMode:1});console.log(JSON.stringify(m.parts.filter(p=>p.id==='feed-puck'||p.spec.feedMount).map(p=>({id:p.id,mesh:p.mesh,print:p.output}))));"
parts=json.loads(subprocess.check_output(['node','--input-type=module','-e',code],cwd=root))
fig=plt.figure(figsize=(12,8),facecolor='#f4f5f7')
for i,p in enumerate(parts):
 for j,mode in enumerate(['mesh','print']):
  mesh=p[mode];v=np.array(mesh['v']);v-=v.min(0);v-=v.max(0)/2
  ax=fig.add_subplot(2,2,i*2+j+1,projection='3d');ax.set_facecolor('#f4f5f7')
  poly=Poly3DCollection(v[np.array(mesh['f'])],facecolors='#549b94',linewidth=0,antialiased=False,shade=True,lightsource=matplotlib.colors.LightSource(315,40));poly.set_edgecolor('none');ax.add_collection3d(poly)
  span=v.max(0)-v.min(0);half=max(span)*.6
  ax.set_xlim(-half,half);ax.set_ylim(-half,half);ax.set_zlim(-half,half);ax.set_box_aspect((1,1,1),zoom=1.25);ax.view_init(elev=25,azim=-55 if mode=='mesh' else -120);ax.set_axis_off()
  name='Integrated mount petal' if p['id'].startswith('petal') else 'Lobed carrier'
  ax.set_title(name+' — '+('installed orientation' if mode=='mesh' else 'exported print orientation'),fontsize=12,pad=-8)
fig.suptitle('PETAL feed attachment revision 6 · actual 400 mm / f/D 0.42 meshes',fontsize=17,y=.97)
fig.text(.5,.025,'Direct rod-to-petal attachment. New petal socket uses print-aligned 45° roofs; carrier still needs slicer review.',ha='center',fontsize=10,color='#475569')
fig.subplots_adjust(left=.01,right=.99,bottom=.07,top=.9,wspace=.02,hspace=.04)
fig.savefig(root/'docs/feed-joints.png',dpi=160);plt.close(fig)
