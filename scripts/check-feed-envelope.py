"""Independent solids/clearance checks on check-feed-envelope.mjs exports.
Requires numpy, trimesh and manifold3d. No load, fatigue or RF qualification.
"""
import json
from pathlib import Path
import numpy as np
import trimesh

folder=Path(__file__).resolve().parents[1]/'tmp/feed-envelope'
print_summary=[]
def cylinder(a,b,r,sections=32):
    a,b=np.array(a),np.array(b)
    return trimesh.creation.cylinder(radius=r,segment=np.array([a,b]),sections=sections)
def penetration(a,b,label):
    if np.any(a.bounds[0]>b.bounds[1]) or np.any(b.bounds[0]>a.bounds[1]):return
    overlap=trimesh.boolean.intersection([a,b],engine='manifold')
    assert abs(overlap.volume)<.025,(label,'penetration mm3',overlap.volume)
def frame(A,B):
    A,B=np.array(A),np.array(B);axis=B-A;axis/=np.linalg.norm(axis)
    u,w=axis[0],axis[2]
    return lambda q:A+np.array([w*q[0]+u*q[2],q[1],-u*q[0]+w*q[2]])
def rotated(mesh,a):
    m=mesh.copy();m.apply_transform(trimesh.transformations.rotation_matrix(a,[0,0,1]));return m
for item in json.loads((folder/'summary.json').read_text()):
    if item['status']!='built':continue
    data=json.loads((folder/(item['id']+'.json')).read_text());p,g=data['parameters'],data['feed']
    parts={x['id']:trimesh.Trimesh(x['mesh']['v'],x['mesh']['f'],process=False) for x in data['parts']}
    for name,m in parts.items():
        assert m.is_watertight and m.is_winding_consistent,(item['id'],name,'topology')
        assert len(m.split(only_watertight=False))==1,(item['id'],name,'disconnected')
        path=folder/(item['id']+'-'+name+'.stl')
        if name in ['feed-rim-shoe','feed-puck'] and path.exists():
            printed=trimesh.load(path);floor=printed.bounds[0,2]
            bed=(printed.face_normals[:,2]<-.99)&(printed.triangles_center[:,2]<floor+.01)
            overhang=(printed.face_normals[:,2]<-(np.cos(np.pi/4)+.01))&(printed.triangles_center[:,2]>floor+.05)
            bed_area=float(printed.area_faces[bed].sum())
            assert bed_area>40,(item['id'],name,'insufficient flat print contact',bed_area)
            print_summary.append({'file':path.name,'flat_bed_mm2':round(bed_area,1),'overhang_mm2':round(float(printed.area_faces[overhang].sum()),1),'volume_cm3':round(float(printed.volume/1000),2)})
    shoe,puck=parts['feed-rim-shoe'],parts['feed-puck']
    panel=next(m for name,m in parts.items() if name.endswith('-mount'))
    penetration(shoe,panel,(item['id'],'shoe/petal'))
    penetration(parts['feed-backer'],panel,(item['id'],'backer/petal'))
    if 'secondary-reflector' in parts:penetration(puck,parts['secondary-reflector'],(item['id'],'puck/secondary'))
    # Exact cylinders include all nominal 18 mm of engagement at either end.
    for leg in g['legs']:
        A,B=np.array(leg['lower']),np.array(leg['upper']);axis=(B-A)/np.linalg.norm(B-A)
        rod=cylinder(A+axis*4,B-axis*4,g['rodDiameter']/2,48)
        penetration(rod,rotated(shoe,leg['angle']),(item['id'],'rod/shoe',leg['number']))
        penetration(rod,puck,(item['id'],'rod/puck',leg['number']))
    # Front washer/nut access in the saddle recesses, not just a clear bolt bore.
    focal=p['diameter']*p['fd']
    for h in g['datum']['holes']:
        x,y=h['r']*np.cos(h['a']),h['r']*np.sin(h['a']);z=h['r']**2/(4*focal)+3.08
        penetration(shoe,cylinder([x,y,z+.02],[x,y,z+.5],3),(item['id'],'rim washer'))
        penetration(shoe,cylinder([x,y,z+.5],[x,y,z+2.9],5.5/np.sqrt(3),6),(item['id'],'rim nut'))
    # Radial nut pockets: check the real hex and its complete side-loading path.
    low=[g['datum']['r'],0,g['lowerZ']];high=[18,0,g['upperZ']];ro=g['rodDiameter']/2+2.5
    upper_frame=frame(high,low);a=g['legs'][0]['angle']
    rotation=np.array([[np.cos(a),-np.sin(a),0],[np.sin(a),np.cos(a),0],[0,0,1]])
    for body,fn in [(shoe,frame(low,high)),(puck,lambda q:rotation@upper_frame(q))]:
        for slide in [0,1,2,4,8]:
            # Match the pocket's hex orientation before the arbitrary socket transform.
            # trimesh's cylinder basis differs from the CAD cylinder basis. Rebuild
            # the local vertices explicitly with x/z aligned hex corners.
            vertices=[]
            for y in [ro+.7,ro+3.1]:
                for a in np.arange(6)*np.pi/3:vertices.append([slide-np.cos(a)*5.5/np.sqrt(3),y,12+np.sin(a)*5.5/np.sqrt(3)])
            nut=trimesh.convex.convex_hull(np.array([fn(v) for v in vertices]))
            penetration(body,nut,(item['id'],'socket nut insertion',slide))
    print('PASS independent topology, reflector/rod fit and hardware access',item['id'],flush=True)
(folder/'print-summary.json').write_text(json.dumps(print_summary,indent=2))
print('PASS flat bed contact on',len(print_summary),'print meshes; residual overhangs require slicer review')
