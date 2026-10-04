"""Orthographic multi-view review of the actual feed-envelope meshes.

Run check-feed-envelope.mjs first. Uses a per-pixel depth buffer, not triangle
painter ordering, so cavities and intersecting assembly parts occlude correctly.
Pass case IDs to select cases; otherwise renders every supported configuration.
"""
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tmp/feed-envelope'
OUTPUT = ROOT / 'tmp/feed-review'
OUTPUT.mkdir(parents=True, exist_ok=True)
VIEWS = [('Top',90,-90),('Side',0,-90),('End',0,0),
         ('Front oblique',25,-55),('Underside',-25,-55),('Rear oblique',25,125)]
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

def render(objects, elevation, azimuth, center, span, size=360):
    e,a = np.deg2rad([elevation,azimuth])
    camera = np.array([np.cos(e)*np.cos(a),np.cos(e)*np.sin(a),np.sin(e)])
    right = np.array([-np.sin(a),np.cos(a),0])
    up = np.cross(camera,right)
    basis = np.array([right,up,camera]).T
    color = np.full((size,size,3),246.,dtype=float)
    depth = np.full((size,size),-np.inf)
    light = np.array([-.35,.55,.76]); light /= np.linalg.norm(light)
    for mesh, rgb in objects:
        v=(np.asarray(mesh['v'])-center)@basis
        v[:,:2] *= (size-30)/span
        v[:,0] += size/2; v[:,1] = size/2-v[:,1]
        triangles=v[np.asarray(mesh['f'])]
        original=np.asarray(mesh['v'])[np.asarray(mesh['f'])]
        normals=np.cross(original[:,1]-original[:,0],original[:,2]-original[:,0])@basis
        normals /= np.maximum(np.linalg.norm(normals,axis=1)[:,None],1e-12)
        for t,n in zip(triangles,normals):
            x0,y0=np.maximum(0,np.floor(t[:,:2].min(0))).astype(int)
            x1,y1=np.minimum(size-1,np.ceil(t[:,:2].max(0))).astype(int)
            if x1<x0 or y1<y0:continue
            denominator=(t[1,1]-t[2,1])*(t[0,0]-t[2,0])+(t[2,0]-t[1,0])*(t[0,1]-t[2,1])
            if abs(denominator)<1e-8:continue
            yy,xx=np.mgrid[y0:y1+1,x0:x1+1];xx=xx+.5;yy=yy+.5
            b0=((t[1,1]-t[2,1])*(xx-t[2,0])+(t[2,0]-t[1,0])*(yy-t[2,1]))/denominator
            b1=((t[2,1]-t[0,1])*(xx-t[2,0])+(t[0,0]-t[2,0])*(yy-t[2,1]))/denominator
            b2=1-b0-b1;z=b0*t[0,2]+b1*t[1,2]+b2*t[2,2]
            region=depth[y0:y1+1,x0:x1+1]
            visible=(b0>=-1e-7)&(b1>=-1e-7)&(b2>=-1e-7)&(z>region)
            if not visible.any():continue
            shade=.32+.68*max(0,float(n@light))
            color[y0:y1+1,x0:x1+1][visible]=np.array(rgb)*shade
            region[visible]=z[visible]
    return Image.fromarray(np.uint8(np.clip(color,0,255)))

def local_panel(mesh,r):
    v=np.asarray(mesh['v']);f=np.asarray(mesh['f'])
    # Deliberate local crop: show the entire pad and surrounding petal/flange.
    keep=((v[:,0]>r-35)&(v[:,0]<r+14)&(abs(v[:,1])<25))[f].any(axis=1)
    return dict(v=mesh['v'],f=f[keep].tolist())

def sheet(item):
    data=json.loads((SOURCE/(item['id']+'.json')).read_text())
    parts={p['id']:p['mesh'] for p in data['parts']};g=data['feed'];r=g['datum']['r']
    shoe=parts['feed-rim-shoe'];puck=parts['feed-puck']
    panel=local_panel(next(m for k,m in parts.items() if k.endswith('-mount')),r)
    groups=[('Rim fitting',[(shoe,[84,155,148])]),('Carrier',[(puck,[84,155,148])]),
            ('Petal interface',[(panel,[230,176,88]),(shoe,[84,155,148])])]
    sheet=Image.new('RGB',(2160,1260),'#f6f6f6');draw=ImageDraw.Draw(sheet)
    font=ImageFont.truetype(FONT,20);small=ImageFont.truetype(FONT,16)
    draw.text((20,12),f"{item['id']} | rod {g['rodAngle']:.2f} deg | {len(g['legs'])} legs | actual CAD meshes",font=font,fill='#22313c')
    for row,(label,objects) in enumerate(groups):
        focus=np.asarray((shoe if row==2 else objects[0][0])['v'])
        center=(focus.min(0)+focus.max(0))/2;span=np.linalg.norm(np.ptp(focus,axis=0))*1.02
        if row==2:span=max(span,68)
        for col,(view,e,a) in enumerate(VIEWS):
            image=render(objects,e,a,center,span)
            y=70+row*400;sheet.paste(image,(360*col,y))
            draw.text((360*col+12,y-24),f'{label} / {view}',font=small,fill='#22313c')
    path=OUTPUT/(item['id']+'.png');sheet.save(path)
    print(path.name,flush=True)

def turntable(case_ids):
    cases=[]
    for case_id in case_ids:
        data=json.loads((SOURCE/(case_id+'.json')).read_text())
        parts={p['id']:p['mesh'] for p in data['parts']}
        cases.append((case_id,data['feed']['rodAngle'],parts))
    frames=[];font=ImageFont.truetype(FONT,16)
    for azimuth in range(0,360,15):
        image=Image.new('RGB',(320*len(cases),740),'#f6f6f6');draw=ImageDraw.Draw(image)
        draw.text((16,12),'Actual CAD | full orbit, above and below | no smoothing of mesh normals',font=font,fill='#22313c')
        for col,(case_id,angle,parts) in enumerate(cases):
            draw.text((col*320+16,44),f'{case_id} / {angle:.1f} deg',font=font,fill='#22313c')
            for row,part_id in enumerate(['feed-rim-shoe','feed-puck']):
                mesh=parts[part_id];v=np.asarray(mesh['v']);center=(v.min(0)+v.max(0))/2
                frame=render([(mesh,[84,155,148])],25*np.cos(np.deg2rad(azimuth)),azimuth,center,np.linalg.norm(np.ptp(v,axis=0))*1.02,size=320)
                image.paste(frame,(col*320,80+row*330))
        frames.append(image)
    frames[0].save(ROOT/'docs/feed-review-orbit.gif',save_all=True,append_images=frames[1:],duration=180,loop=0)

if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--turntable':turntable(sys.argv[2:])
    else:
        for item in json.loads((SOURCE/'summary.json').read_text()):
            if item['status']=='built' and (len(sys.argv)==1 or item['id'] in sys.argv[1:]):sheet(item)
