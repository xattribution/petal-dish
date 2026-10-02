"""Bundle canonical print meshes; regenerate after re-exporting cad/simple-mount.scad."""
import json,hashlib,pathlib,trimesh
parts={}
for name in ['base','yoke','cradle']:
 p=pathlib.Path('cad/STL/simple-'+name+'.stl');m=trimesh.load(p,force='mesh')
 assert m.is_watertight and m.is_winding_consistent
 parts[name]={'v':m.vertices.round(6).tolist(),'f':m.faces.tolist(),'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
pathlib.Path('dist/mount-meshes.js').write_text('// Generated from cad/STL/simple-*.stl by scripts/pack-mount.py; do not hand-edit.\nexport const mountMeshes='+json.dumps(parts,separators=(',',':'))+';\n')
