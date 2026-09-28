"""Sample assembled mesh interference (requires numpy, trimesh, rtree, scipy).
Not an exhaustive collision proof or structural simulation.
"""
import json, subprocess
from pathlib import Path
import numpy as np
import trimesh
root=Path(__file__).resolve().parents[1]
pairs=[('feed-lower-clevis','feed-lower-rod-end'),('feed-upper-clevis','feed-upper-rod-end'),('secondary-reflector','feed-upper-clevis'),('secondary-reflector','feed-upper-rod-end'),('feed-saddle','feed-lower-clevis'),('feed-carrier','feed-upper-clevis'),('feed-carrier','feed-upper-rod-end'),('panel-1-feed','feed-backer'),('panel-1-feed','feed-saddle')]
for cfg in [dict(feedMode=1),dict(feedMode=2),dict(feedMode=1,fd=.65)]:
    code="import{build,defaults}from'./dist/geometry.js';let m=build({...defaults,..."+json.dumps(cfg)+"});console.log(JSON.stringify(m.parts.map(p=>({id:p.id,mesh:p.mesh}))));"
    data=json.loads(subprocess.check_output(['node','--input-type=module','-e',code],cwd=root))
    parts={p['id']:trimesh.Trimesh(p['mesh']['v'],p['mesh']['f'],process=False) for p in data}
    for a,b in pairs:
        if a not in parts:continue
        A,B=parts[a],parts[b];worst=0;n=0
        for x,y in [(A,B),(B,A)]:
            points=np.vstack([x.vertices,x.triangles_center]);box=y.bounds
            points=points[np.all((points>=box[0]-.001)&(points<=box[1]+.001),axis=1)]
            if len(points)>1200:points=points[np.linspace(0,len(points)-1,1200).astype(int)]
            for chunk in np.array_split(points,max(1,int(np.ceil(len(points)/32)))):
                if not len(chunk):continue
                inside=y.contains(chunk)
                if any(inside):
                    _,dist,_=y.nearest.on_surface(chunk[inside]);n+=sum(dist>.035);worst=max(worst,float(max(dist)))
        assert n==0,(cfg,a,b,n,worst)
        print('PASS sampled fit',cfg,a,b,'max penetration',round(worst,6),flush=True)
