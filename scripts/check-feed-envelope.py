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
    radius=(g['rodDiameter']+p['rodClearance'])/2;screwL=max(1,g['lowerEntrance']-10);nutT=-(radius+2.8);C=A+axis*screwL
    for slide in [0,1,2,4,8,16,24]:
        nut=trimesh.creation.box([2.4,5.5,5.5]);matrix=np.eye(4);matrix[:3,:3]=np.array([T,V,axis]).T;matrix[:3,3]=C+T*nutT+V*slide;nut.apply_transform(matrix)
        penetration(panel,nut,(item['id'],'square nut insertion',slide))
    penetration(panel,cylinder(C-T*40,C,1.5,32),(item['id'],'underside screw access'))
    # Witnesses on both bearing walls exclude the screw channel itself.
    for dt in [-2.5,2.5]:
        for dl in [-2.4,2.4]:
            witness=trimesh.creation.box([.2,.2,.2]);witness.apply_translation(C+T*(nutT+dt)+axis*dl)
            missing=trimesh.boolean.difference([witness,panel],engine='manifold')
            assert missing.volume<.001,(item['id'],'nut bearing wall missing',dt,dl,missing.volume)
    # Inspect the actual assembled mesh against actual side-print up, rather than
    # assuming the rod axis or world Z is the printer's vertical direction.
    centers=panel.triangles_center;r=g['datum']['r'];x0=r-max(18,g['lowerEntrance']*abs(axis[0])+radius+3)
    depth=6+radius+7.6
    local=(centers[:,0]>x0-.5)&(centers[:,0]<p['diameter']/2+.1)&(abs(centers@up-r*up[0])<depth+.5)
    unsupported=local&(panel.face_normals@up<-(np.cos(np.pi/4)+.0001))
    area=float(panel.area_faces[unsupported].sum())
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
