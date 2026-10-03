"""Export the seam lever STLs (M3 and M4 holes) from cad/seam-lever.scad and bundle them for the app.
Writes cad/STL/seam-lever-M3|M4/*.stl (print orientation) and dist/lever-meshes.js (print + installed station-frame meshes).
Requires OpenSCAD on PATH. Run after changing cad/seam-lever.scad or the seam station geometry."""
import json, hashlib, pathlib, subprocess, tempfile, re, trimesh

SCAD = pathlib.Path('cad/seam-lever.scad')
HOLES = {3: 3.4, 4: 4.5}   # must match the seam bore the generator cuts (geometry.js seamHoleR)
PARTS = ['lever', 'bar', 'keeper', 'spring']

def export(part, hole_d, out, closed=True):
    subprocess.run(['openscad', '-q', '-D', f'part="{part}"', '-D', f'hole_d={hole_d}', '-D', f'closed={str(closed).lower()}', '--export-format', 'binstl', '-o', str(out), str(SCAD)], check=True)
    m = trimesh.load(out, force='mesh')
    assert m.is_watertight and m.is_winding_consistent and m.volume > 0, f'{part} M{hole_d} is not a closed solid'
    return m

def pack(m): return {'v': m.vertices.round(4).tolist(), 'f': m.faces.tolist()}

bundle = {'source_sha256': hashlib.sha256(SCAD.read_bytes()).hexdigest()}
with tempfile.TemporaryDirectory() as tmp:
    for size, hole_d in HOLES.items():
        folder = pathlib.Path(f'cad/STL/seam-lever-M{size}'); folder.mkdir(parents=True, exist_ok=True)
        info = pathlib.Path(tmp) / 'motion.echo'
        subprocess.run(['openscad', '-o', str(info), '-D', 'part="motion"', '-D', f'hole_d={hole_d}', str(SCAD)], check=True, capture_output=True)
        values = json.loads(re.search(r'ECHO: MOTION = (.*)', info.read_text()).group(1))
        entry = {'hole_d': hole_d, 'motion': dict(zip(['openRadius','closedRadius','peakRadius','stroke','holeY','wall'], values))}
        for part in PARTS:
            printed = export(part, hole_d, folder / f'{part}.stl')
            installed = export('installed_' + part, hole_d, pathlib.Path(tmp) / f'{part}.stl')
            opened = export('installed_' + part, hole_d, pathlib.Path(tmp) / f'open-{part}.stl', closed=False)
            entry[part] = {'print': pack(printed), 'installed': pack(installed), 'open': pack(opened)}
        export('plate', hole_d, folder / 'plate.stl')
        bundle[f'M{size}'] = entry
pathlib.Path('dist/lever-meshes.js').write_text('// Generated from cad/seam-lever.scad by scripts/pack-lever.py; do not hand-edit.\n'
    '// Installed meshes are in the seam station frame: X across the seam, Y toward the shell, Z along the seam.\n'
    'export const leverMeshes=' + json.dumps(bundle, separators=(',', ':')) + ';\n')
print('dist/lever-meshes.js', pathlib.Path('dist/lever-meshes.js').stat().st_size, 'bytes')
