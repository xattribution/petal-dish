"""Render generated triangles with orthographic CAD views (no illustrative AI geometry)."""
import json,pathlib,numpy as np,matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from matplotlib.colors import to_rgb
from PIL import Image,ImageChops
out=pathlib.Path('docs/snapshots');out.mkdir(exist_ok=True)
colors={'panel':'#719baa','hub':'#deb36a','clip':'#f1984c','feed':'#609a83','mount':'#526d89','rod':'#a8b3b7'}
cases={'assembly':('400 mm / complete system',24,25),'rear':('400 mm / rear joints and mount',22,-115),'staggered':('600 mm / staggered rings + four rods',25,-115),'exploded':('400 mm / exploded parts',24,-115),'clip':('Snap clip / broad side on the bed',35,-70),'hero':('400 mm / exploded, front',56,-72)}
for name,(title,elev,azim) in cases.items():
 data=json.loads(pathlib.Path('tmp/system-renders/'+name+'.json').read_text());fig=plt.figure(figsize=(12,9),dpi=110,facecolor='#edf1f3');ax=fig.add_axes([0,0,1,1],projection='3d');ax.set_facecolor('#edf1f3');ax.set_proj_type('ortho');allv=[];triangles=[];fc=[]
 for m in data['meshes']:
  v=np.array(m['v']);allv.append(v);t=v[np.array(m['f'])];n=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0]);n/=np.maximum(np.linalg.norm(n,axis=1)[:,None],1e-10);light=.45+.55*np.abs(n@np.array([.3,-.4,.866]));rgb=np.array(to_rgb(colors.get(m['kind'],'#888')));triangles.extend(t);fc.extend(np.clip(rgb*light[:,None],0,1))
 poly=Poly3DCollection(triangles,facecolors=fc,edgecolors='none',linewidths=0,zsort='average');ax.add_collection3d(poly)
 for line in []:
  q=np.array(line);ax.plot(q[:,0],q[:,1],q[:,2],color='#334f48',linewidth=2.5)
 points=np.concatenate(allv);lo=points.min(0);hi=points.max(0);ctr=(lo+hi)/2;span=max(hi-lo)*.36
 for setter,c in zip([ax.set_xlim,ax.set_ylim,ax.set_zlim],ctr):setter(c-span,c+span)
 ax.set_box_aspect((1,1,1));ax.view_init(elev=elev,azim=azim);ax.set_axis_off()
 # No baked-in captions: the Markdown alt text and docs describe each view. Crop to the geometry.
 fig.savefig(out/(name+'.png'),dpi=110,facecolor=fig.get_facecolor(),bbox_inches='tight',pad_inches=.15);plt.close(fig)
 im=Image.open(out/(name+'.png')).convert('RGB');bg=Image.new('RGB',im.size,im.getpixel((0,0)));box=ImageChops.difference(im,bg).getbbox()
 if box:pad=24;im.crop((max(0,box[0]-pad),max(0,box[1]-pad),min(im.width,box[2]+pad),min(im.height,box[3]+pad))).save(out/(name+'.png'))
 print('rendered',name,flush=True)
