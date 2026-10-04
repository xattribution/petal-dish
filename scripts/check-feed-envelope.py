"""Independent solids/clearance checks on check-feed-envelope.mjs exports.
Requires numpy, trimesh and manifold3d. No load, fatigue or RF qualification.
"""
import json,sys
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
    A,B=np.array(A,dtype=float),np.array(B,dtype=float);axis=B-A;axis/=np.linalg.norm(axis)
    u,w=axis[0],axis[2]
    return lambda q:A+np.array([w*q[0]+u*q[2],q[1],-u*q[0]+w*q[2]])
def rotated(mesh,a):
    m=mesh.copy();m.apply_transform(trimesh.transformations.rotation_matrix(a,[0,0,1]));return m
for item in json.loads((folder/'summary.json').read_text()):
    if item['status']!='built' or (len(sys.argv)>1 and item['id']!=sys.argv[1]):continue
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
    puck=parts['feed-puck']
    if g['secondary']:
        penetration(puck,cylinder([0,0,g['secondary']['backZ']-.1],[0,0,g['carrierFace']+17],2,32),(item['id'],'M4 screw through full stem'))
        # A continuous witness annulus catches nut slots cutting the stem wall.
        lo=g['secondary']['backZ']+.05;hi=g['carrierFace']-.05
        witness=cylinder([0,0,lo],[0,0,hi],8.8,32)
        witness=trimesh.boolean.difference([witness,cylinder([0,0,lo-1],[0,0,hi+1],2.5,32)],engine='manifold')
        missing=trimesh.boolean.difference([witness,puck],engine='manifold')
        assert abs(missing.volume)<.025,(item['id'],'secondary stem wall notches',missing.volume)
    panel=next(m for name,m in parts.items() if name.startswith('petal'))
    assert 'feed-rim-shoe' not in parts
    assert 'feed-backer' not in parts
    if 'secondary-reflector' in parts:penetration(puck,parts['secondary-reflector'],(item['id'],'puck/secondary'))
    # Exact cylinders include all nominal 18 mm of engagement at either end.
    for leg in g['legs']:
        A,B=np.array(leg['lower']),np.array(leg['upper']);axis=(B-A)/np.linalg.norm(B-A)
        rod=cylinder(A+axis*(g['lowerEntrance']-18),B-axis*(g['upperEntrance']-18),g['rodDiameter']/2,48)
        penetration(rod,rotated(panel,leg['angle']),(item['id'],'rod/petal',leg['number']))
        penetration(rod,puck,(item['id'],'rod/puck',leg['number']))
    # The new petal saddle: independently reconstruct its print and screw frames.
    A=np.array([g['datum']['r'],0,g['lowerZ']]);B=np.array([18,0,g['upperZ']]);axis=(B-A)/np.linalg.norm(B-A)
    h=np.pi/data['layout']['n'];up=np.array([np.sin(h),np.cos(h),0]);V=up-axis*np.dot(axis,up);V/=np.linalg.norm(V)
    T=np.cross(axis,V)
    if T[2]<0:T=-T
    radius=(g['rodDiameter']+p['rodClearance'])/2;C=A+axis*1.3
    penetration(panel,cylinder(C+[0,-40,0],C,1.5,32),(item['id'],'side screw access'))
    # Side pilot remains open for the short insert; no nut slot or backing plate.
    penetration(panel,cylinder(C+[0,-40,0],C+[0,-radius-2.51,0],2,48),(item['id'],'insert access'))
    # Full 4 mm insert has at least 0.8 mm of plastic around the pilot.
    lo=C+[0,-radius-6.5,0];hi=C+[0,-radius-2.5,0]
    wall=trimesh.boolean.difference([cylinder(lo,hi,2.9,48),cylinder(lo-[0,.1,0],hi+[0,.1,0],2.11,48)],engine='manifold')
    missing=trimesh.boolean.difference([wall,panel],engine='manifold')
    assert abs(missing.volume)<.025,(item['id'],'insert retaining wall',missing.volume)
    # Inspect the actual assembled mesh against actual side-print up, rather than
    # assuming the rod axis or world Z is the printer's vertical direction.
    centers=panel.triangles_center;r=g['datum']['r']
    local=(abs(centers[:,0]-r)<22)&(centers[:,1]>-25)&(centers[:,1]<18)
    unsupported=local&(panel.face_normals@up<-(np.cos(np.pi/4)+.0001))
    # The only permitted bridge is the measured <=0.8 mm flat bore roof.
    transverse=(panel.triangles-A)@T
    longitudinal=(panel.triangles-A)@axis
    height=(panel.triangles-A)@V
    bridge=(np.ptp(transverse,axis=1)<=.801)&(np.max(abs(transverse),axis=1)<=.401)&(np.max(abs(height-(radius*np.sqrt(2)-.4)),axis=1)<.002)
    area=float(panel.area_faces[unsupported&~bridge].sum())
    assert area<.1,(item['id'],'new socket overhang beyond 45 degrees',area)
    print_summary.append({'case':item['id'],'socket_overhang_mm2':round(area,4),'petal_volume_cm3':round(float(panel.volume/1000),2)})
    # Radial nut pockets: check the real hex and its complete side-loading path.
    low=[g['datum']['r'],0,g['lowerZ']];high=[18,0,g['upperZ']];ro=g['rodDiameter']/2+2.5
    upper_frame=frame(high,low);a=g['legs'][0]['angle']
    rotation=np.array([[np.cos(a),-np.sin(a),0],[np.sin(a),np.cos(a),0],[0,0,1]])
    for body,fn,direction in [(puck,lambda q:rotation@upper_frame(q),-1)]:
        for slide in [0,1,2,4,8,16,32,60]:
            # Match the pocket's hex orientation before the arbitrary socket transform.
            # trimesh's cylinder basis differs from the CAD cylinder basis. Rebuild
            # the local vertices explicitly with x/z aligned hex corners.
            vertices=[]
            for y in [ro+.7,ro+3.1]:
                for a in np.arange(6)*np.pi/3:vertices.append([direction*slide-np.cos(a)*5.5/np.sqrt(3),y,12+np.sin(a)*5.5/np.sqrt(3)])
            nut=trimesh.convex.convex_hull(np.array([fn(v) for v in vertices]))
            penetration(body,nut,(item['id'],'socket nut insertion',slide))
    print('PASS independent topology, reflector/rod fit and hardware access',item['id'],flush=True)
(folder/'print-summary.json').write_text(json.dumps(print_summary,indent=2))
print('PASS geometry review on',len(print_summary),'print meshes; residual overhangs require slicer review')
