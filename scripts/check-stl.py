"""Reload binary structural STL fixtures written by tests/structure.test.mjs.
Requires trimesh and numpy. This checks export quantization, not printability.
"""
from pathlib import Path
import trimesh
files=list(Path('/tmp/petal5-validation').glob('*.stl'))
assert files, 'Run node tests/structure.test.mjs first'
for file in files:
    mesh=trimesh.load(file,force='mesh')
    assert mesh.is_watertight,(file,'open mesh')
    assert mesh.is_winding_consistent,(file,'inconsistent winding')
    assert len(mesh.split())==1,(file,'multiple components')
    assert mesh.area_faces.min()>1e-10,(file,'degenerate triangle')
print(f'PASS {len(files)} binary STLs: closed, consistently wound, single solid, nondegenerate')
