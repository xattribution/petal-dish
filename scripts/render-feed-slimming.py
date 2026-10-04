"""Compare actual revision-5 and revision-6 petal attachments at equal scale."""
import io,json,subprocess,tarfile,tempfile,importlib.util
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw,ImageFont
root=Path(__file__).resolve().parents[1]
code="import{build,defaults,volume}from'./dist/geometry.js';let m=build({...defaults,feedMode:1});console.log(JSON.stringify(m.parts.filter(p=>p.kind==='panel'||p.kind==='feed').map(p=>({id:p.id,mesh:p.mesh,volume:volume(p.mesh),mount:p.spec.feedMount}))));"
with tempfile.TemporaryDirectory(prefix='petal-direct-before-') as folder:
 baseline=Path(folder)
 archive=subprocess.check_output(['git','archive','ef6dae4','dist','package.json'],cwd=root)
 with tarfile.open(fileobj=io.BytesIO(archive)) as files:files.extractall(baseline,filter='data')
 (baseline/'node_modules').symlink_to((root/'node_modules').resolve(),target_is_directory=True)
 old=json.loads(subprocess.check_output(['node','--input-type=module','-e',code],cwd=baseline))
new=json.loads(subprocess.check_output(['node','--input-type=module','-e',code],cwd=root))
spec=importlib.util.spec_from_file_location('render',root/'scripts/render-feed-review.py');render=importlib.util.module_from_spec(spec);spec.loader.exec_module(render)
oldmount=next(p for p in old if p['mount']);newmount=next(p for p in new if p['mount']);shoe=next(p for p in old if p['id']=='feed-rim-shoe')
base=next(p for p in new if p['id']=='petal-1')['volume'];before=oldmount['volume']-base+shoe['volume'];after=newmount['volume']-base
image=Image.new('RGB',(1200,820),'#f6f6f6');draw=ImageDraw.Draw(image);font=ImageFont.truetype(render.FONT,20)
draw.text((20,14),'Actual CAD | direct rod-to-petal attachment | equal scale',font=font,fill='#22313c')
for col,(mount,foot,label,material) in enumerate([(oldmount,shoe,'Before: separate rim shoe',before),(newmount,None,'After: integrated underside socket',after)]):
 local=render.local_panel(mount['mesh'],180);objects=[(local,[230,176,88])]
 if foot:objects.append((foot['mesh'],[84,155,148]))
 draw.text((col*600+20,52),f'{label} | attachment material {material/1000:.1f} cm³',font=font,fill='#22313c')
 for row,(e,a) in enumerate([(25,-55),(-25,-55)]):image.paste(render.render(objects,e,a,np.array([182,0,49]),85,size=350),(col*600+125,90+row*350))
image.save(root/'docs/feed-slimming.png')
print(json.dumps({'before_attachment_mm3':before,'after_attachment_mm3':after,'reduction_percent':100*(1-after/before)},indent=2))
