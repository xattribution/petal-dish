"""Compare actual revision-4 and revision-5 meshes at the same scale."""
import io,json,subprocess,tarfile,tempfile
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
root=Path(__file__).resolve().parents[1]
code="import{build,defaults,volume}from'./dist/geometry.js';let m=build({...defaults,feedMode:1});console.log(JSON.stringify(m.parts.filter(p=>p.kind==='feed').map(p=>({id:p.id,mesh:p.mesh,volume:volume(p.mesh)}))));"
# Rebuild the last bulky fitting revision so this comparison is reproducible.
with tempfile.TemporaryDirectory(prefix='petal-feed-before-') as folder:
 baseline=Path(folder)
 archive=subprocess.check_output(['git','archive','b74ae24199f5ee0ef5211d3071f0de2f8ef7cc1c','dist','package.json'],cwd=root)
 with tarfile.open(fileobj=io.BytesIO(archive)) as files:files.extractall(baseline,filter='data')
 (baseline/'node_modules').symlink_to((root/'node_modules').resolve(),target_is_directory=True)
 old=json.loads(subprocess.check_output(['node','--input-type=module','-e',code],cwd=baseline))
new=json.loads(subprocess.check_output(['node','--input-type=module','-e',code],cwd=root))
fig=plt.figure(figsize=(11,8),facecolor='#f4f5f7')
for i,name in enumerate(['feed-rim-shoe','feed-puck']):
 pair=[next(p for p in group if p['id']==name) for group in [old,new]]
 half=max(np.ptp(np.array(p['mesh']['v']),axis=0).max() for p in pair)*.58
 for j,p in enumerate(pair):
  v=np.array(p['mesh']['v']);v-=(v.min(0)+v.max(0))/2
  ax=fig.add_subplot(2,2,i*2+j+1,projection='3d');ax.set_facecolor('#f4f5f7')
  poly=Poly3DCollection(v[np.array(p['mesh']['f'])],facecolors='#88909a' if j==0 else '#549b94',linewidth=0,antialiased=False,shade=True,lightsource=matplotlib.colors.LightSource(315,40));poly.set_edgecolor('none');ax.add_collection3d(poly)
  ax.set_xlim(-half,half);ax.set_ylim(-half,half);ax.set_zlim(-half,half);ax.set_box_aspect((1,1,1),zoom=1.25);ax.view_init(elev=25,azim=-55);ax.set_axis_off()
  label='Rim fitting' if i==0 else 'Carrier'
  change='' if j==0 else f" · {100*(1-p['volume']/pair[0]['volume']):.0f}% less material"
  ax.set_title(f"{label} · {'before' if j==0 else 'after'}\n{p['volume']/1000:.1f} cm³{change}",fontsize=13,pad=-10)
fig.suptitle('PETAL · smaller, blended feed fittings',fontsize=19,y=.97)
fig.text(.5,.025,'Actual default 400 mm / f/D 0.42 CAD meshes. Each before/after pair uses the same scale.',ha='center',fontsize=10,color='#475569')
fig.subplots_adjust(left=.01,right=.99,bottom=.07,top=.87,wspace=.02,hspace=.12)
fig.savefig(root/'docs/feed-slimming.png',dpi=150);plt.close(fig)
