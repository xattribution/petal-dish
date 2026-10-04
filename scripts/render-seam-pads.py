"""Orthographic closeups of actual seam pad triangles; run the mjs exporter first."""
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
data=json.loads(Path('tmp/seam-pads/closeups.json').read_text())
fig=plt.figure(figsize=(13,7.5),facecolor='#eef1f3')
for i,item in enumerate(data):
 ax=fig.add_subplot(2,2,i+1,projection='3d');ax.set_facecolor('#eef1f3');ax.set_proj_type('ortho')
 v=np.array(item['mesh']['v']);faces=np.array(item['mesh']['f']);tri=v[faces]
 poly=Poly3DCollection(tri,facecolors='#6f9ca8',linewidth=0,shade=True,lightsource=matplotlib.colors.LightSource(300,40));poly.set_edgecolor('none');ax.add_collection3d(poly)
 lo=v.min(0);hi=v.max(0);ctr=(lo+hi)/2;span=hi-lo
 for setter,c,s in zip([ax.set_xlim,ax.set_ylim,ax.set_zlim],ctr,span):setter(c-s*.55,c+s*.55)
 ax.set_box_aspect(np.maximum(span,1),zoom=1.1);ax.view_init(elev=-12,azim=70);ax.set_axis_off()
 ax.set_title(item['label']+' · M'+str(item['cfg']['seamBolt'])+' · f/D '+str(item['cfg']['fd']),fontsize=13,pad=-12)
fig.suptitle('Seam bearing lands follow the flange curve and join the petal underside',fontsize=17,y=.96)
fig.text(.5,.03,'Actual 400 mm geometry · bolt centers and flat washer seats retained · deep and normal dishes',ha='center',fontsize=11,color='#475569')
fig.subplots_adjust(left=.01,right=.99,bottom=.08,top=.88,wspace=.01,hspace=.06)
fig.savefig('docs/seam-pad-cleanup.png',dpi=160);plt.close(fig)
