"""Bundle the simple mount's blank print meshes for the app (dist/mount-meshes.js).
Usage: python3 scripts/pack-mount.py [--export]
  --export  first re-run OpenSCAD on cad/simple-mount.scad: the complete default-size parts into cad/STL/simple-*.stl
            (print these directly) and the blanks, with every fastener hole left out, into cad/STL/blank/simple-*.stl
The app bundles the blanks and cuts the holes for the selected bolt sizes (dist/mount-fasteners.js); the tests check
that the default sizes reproduce the complete STLs.
Each entry: {v, f, source_sha256, frame, toModel}. toModel (3 x 4) maps print coordinates to the part's
assembly frame and is the inverse of the SCAD's own print transform (read via part = "matrices"):
  frame "world"  - base fixed; yoke and upright turn with azimuth
  frame "cradle" - elevation axis at the origin, el = 0; rotated by elevation, lifted to the axis, turned by azimuth
"""
import sys, json, re, hashlib, pathlib, subprocess, tempfile
import numpy as np, trimesh, manifold3d as m3

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAD = ROOT / 'cad/simple-mount.scad'
STL = ROOT / 'cad/STL'
BLANK = STL / 'blank'
# complete default-size variants: (SCAD part, extra -D settings, frame, print matrix)
VARIANTS = {
    'base':        ('base',    {},                      'world',  'M_base'),
    'yoke':        ('yoke',    {},                      'world',  'M_yoke'),
    'yoke-stand':  ('yoke',    {'stand_holes': 'true'}, 'world',  'M_yoke'),
    'upright':     ('upright', {},                      'world',  'M_upright'),
    'upright-arc': ('upright', {'arc_lock': 'true'},    'world',  'M_upright'),
    'cradle':      ('cradle',  {},                      'cradle', 'M_cradle'),
    'cheek':       ('cheek',   {},                      'cradle', 'M_cheek'),
    'cheek-arc':   ('cheek',   {'arc_lock': 'true'},    'cradle', 'M_cheek'),
}
# blanks: no fastener cuts (stand holes and the base's stand screws are fastener cuts, so yoke-stand and the tripod
# base share the yoke and base blanks)
BLANKS = {k: (part, {**extra, 'fasteners': 'false'}, frame, mat) for k, (part, extra, frame, mat) in VARIANTS.items() if k != 'yoke-stand'}

def scad_info():
    """Print transforms and constants echoed by cad/simple-mount.scad (part = "matrices")."""
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / 'm.echo'
        subprocess.run(['openscad', '-o', str(out), '-D', 'part="matrices"', str(SCAD)], check=True, capture_output=True)
        text = out.read_text()
    info = {}
    for k, v in re.findall(r'ECHO: (\w+) = (.*)', text):
        info[k] = json.loads(v)
    for k in [k for k in info if k.startswith('M_')]:
        info[k] = np.array(info[k], float)
    return info

STL_DTYPE = np.dtype([('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])

def canonical_stl(path):
    """Rewrite a binary STL with a fixed header and a stable triangle order (OpenSCAD's order varies between runs),
    so an unchanged model re-exports byte-identical and keeps its source_sha256."""
    raw = path.read_bytes()
    tri = np.frombuffer(raw, STL_DTYPE, int(np.frombuffer(raw, '<u4', 1, 80)[0]), 84)['v'].copy()
    for t in tri:   # start each triangle at its lexicographically smallest corner, keeping the winding
        k = min(range(3), key=lambda i: tuple(t[i])); t[:] = np.roll(t, -k, axis=0)
    tri = tri[np.lexsort(tri.reshape(-1, 9).T[::-1])]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]); n /= np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]
    rec = np.zeros(len(tri), STL_DTYPE); rec['n'] = n; rec['v'] = tri
    path.write_bytes(b'PETAL simple mount, canonical order'.ljust(80, b' ') + np.uint32(len(tri)).tobytes() + rec.tobytes())

def export():
    BLANK.mkdir(parents=True, exist_ok=True)
    for folder, table in ((STL, VARIANTS), (BLANK, BLANKS)):
        for name, (part, extra, _, _) in table.items():
            out = folder / f'simple-{name}.stl'
            args = ['openscad', '--export-format', 'binstl', '-o', str(out), '-D', f'part="{part}"', '-D', 'printing=true']
            for k, v in extra.items(): args += ['-D', f'{k}={v}']
            print('export', out.relative_to(ROOT), flush=True)
            subprocess.run(args + [str(SCAD)], check=True, capture_output=True)
            canonical_stl(out)

def main():
    if '--export' in sys.argv: export()
    info = scad_info()
    parts = {}
    for name, (part, _, frame, mat) in BLANKS.items():
        p = BLANK / f'simple-{name}.stl'
        m = trimesh.load(p, force='mesh')
        assert m.is_watertight and m.is_winding_consistent and m.body_count == 1, name
        v = m.vertices.round(4)
        # rounding must not break the solid the app builds from these triangles
        mf = m3.Manifold(m3.Mesh(np.asarray(v, np.float32), np.asarray(m.faces, np.uint32)))
        assert mf.status() == m3.Error.NoError and abs(mf.volume() - m.volume) < 1e-3 * m.volume, name
        to_model = np.linalg.inv(info[mat])[:3].round(9) + 0.0
        parts[name] = {'v': v.tolist(), 'f': m.faces.tolist(), 'source_sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                       'frame': frame, 'toModel': to_model.tolist()}
    base_t, yoke_t, z_el, L, plate_t, cr_in, cr_out, up_in, up_out, cap_r = info['FRAME']
    ins_d, ins_deep, ins_len, cs4_d, up_len, ch_len, n_up, n_ch, ch_x = info['JOINT']
    arc_r, arc_phi, arc_w, arc_margin, el_min, el_max, m6_af, m6_head, arc_len = info['ARC']
    frame = {'baseT': base_t, 'yokeT': yoke_t, 'axisZ': z_el, 'hubL': L, 'plateT': plate_t, 'cheek': [cr_in, cr_out], 'upright': [up_in, up_out], 'capR': cap_r,
             'insert': {'pilot': ins_d, 'depth': ins_deep + info['INSET'][0], 'length': ins_len, 'inset': info['INSET'][0], 'recess': 0.5}, 'uprightScrews': {'count': n_up, 'length': up_len, 'at': info['UP_SCREWS']},
             'cheekScrews': {'count': n_ch, 'length': ch_len, 'x': ch_x, 'z': info['CH_SCREWS']}, 'standHoles': info['STAND'],
             'arc': {'radius': arc_r, 'phi': arc_phi, 'slot': arc_w, 'range': [el_min, el_max], 'boltLength': arc_len, 'headAF': m6_af, 'head': m6_head}}
    shoulder, bcd_r, base_stand_r, yoke_z, top, _, hfn = info['CUTS']
    frame['cuts'] = {'shoulder': shoulder, 'bcd': bcd_r, 'baseStandR': base_stand_r, 'yokeZ': yoke_z, 'top': top, 'pilotDepth': ins_deep, 'segments': hfn}
    js = ('// Generated from cad/simple-mount.scad and cad/STL/blank/simple-*.stl by scripts/pack-mount.py; do not hand-edit.\n'
          'export const mountFrame=' + json.dumps(frame, separators=(',', ':')) + ';\n'
          'export const mountMeshes=' + json.dumps(parts, separators=(',', ':')) + ';\n')
    (ROOT / 'dist/mount-meshes.js').write_text(js)
    print(f'dist/mount-meshes.js: {len(js)/1024:.0f} KB, ' + ', '.join(f'{k} {len(v["f"])}' for k, v in parts.items()) + ' faces')

if __name__ == '__main__':
    main()
