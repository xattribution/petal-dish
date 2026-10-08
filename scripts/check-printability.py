"""Overhang scan for print-orientation STLs.
Flags every downward-facing surface steeper than 45° from vertical (excluding the bed face) and
groups flagged faces into connected regions so each region's location and size can be reported.
Reported but not counted, because they print without support:
  bridge  - a ceiling within 20° of flat whose shorter side spans at most 10 mm (the stepped ceiling over a counterbore,
            a pocket roof)
  ledge   - a downward face within 0.3 mm of the bed (a first-layer step: it can only droop onto the bed)
  minor   - steeper than 45° but within 60° of vertical: at a 0.2 mm layer each layer steps out at most 0.35 mm, under
            one extrusion width, which slicers print without support by default (small ridges and grooves on seams)
Usage: python3 scripts/check-printability.py part1.stl [part2.stl ...]
"""
import sys, numpy as np, trimesh

LIMIT = np.cos(np.radians(45)) + 0.01   # n_z below -LIMIT means steeper than ~44.4°
NOISE = 0.05                             # mm² per connected region: tessellation slivers below slicer resolution
BRIDGE = 10.0                            # mm: longest short span accepted for a flat ceiling
MINOR_NZ = -np.cos(np.radians(30)) - 1e-3   # 60° from vertical
FLAT_NZ = -np.cos(np.radians(20))          # within 20° of a flat ceiling
ok = True
for path in sys.argv[1:]:
    m = trimesh.load(path)
    n, c, a = m.face_normals, m.triangles_center, m.area_faces
    zmin = m.bounds[0][2]
    bad = (n[:, 2] < -LIMIT) & (c[:, 2] > zmin + 0.05)
    flat = bad & (n[:, 2] < -0.99)
    regions = []
    if bad.any():
        idx = np.nonzero(bad)[0]
        adj = m.face_adjacency
        keep = bad[adj[:, 0]] & bad[adj[:, 1]]
        g = trimesh.graph.connected_components(adj[keep], nodes=idx)
        for comp in g:
            comp = np.asarray(comp)
            pts = m.triangles[comp].reshape(-1, 3)
            span = pts.max(0) - pts.min(0)
            nz = n[comp, 2][a[comp] >= 0.005] if (a[comp] >= 0.005).any() else n[comp, 2]   # slivers' normals are rounding noise
            if bool((nz <= FLAT_NZ).all()) and min(span[0], span[1]) <= BRIDGE: kind = 'bridge'
            elif pts[:, 2].max() <= zmin + 0.3: kind = 'ledge'
            elif nz.min() >= MINOR_NZ: kind = 'minor'
            else: kind = ''
            regions.append((a[comp].sum(), span, pts.mean(0), kind))
        regions.sort(key=lambda r: -r[0])
    name = path.split('/')[-1]
    kinds = {k: [r for r in regions if r[3] == k and r[0] >= NOISE] for k in ('bridge', 'ledge', 'minor')}
    real = [r for r in regions if r[0] >= NOISE and not r[3]]
    noise = len([r for r in regions if r[0] < NOISE])
    status = "PASS" if not real else "FAIL"
    ok &= not real
    print(f"{status} {name}: {sum(r[0] for r in real):.1f} mm² steeper than 45° in {len(real)} regions"
          + (f"; {len(kinds['bridge'])} bridges up to {max(min(r[1][0], r[1][1]) for r in kinds['bridge']):.1f} mm ({sum(r[0] for r in kinds['bridge']):.1f} mm²)" if kinds['bridge'] else "")
          + "".join(f"; {len(kinds[k])} {k} ({sum(r[0] for r in kinds[k]):.1f} mm²)" for k in ('ledge', 'minor') if kinds[k])
          + (f"; ignored {noise} sliver regions < {NOISE} mm² ({sum(r[0] for r in regions if r[0] < NOISE):.2f} mm² total)" if noise else ""))
    for area, span, ctr, _ in real[:6]:
        print(f"     {area:7.1f} mm²  span {span[0]:5.1f} x {span[1]:5.1f} x {span[2]:4.1f}  at ({ctr[0]:.0f}, {ctr[1]:.0f}, z={ctr[2]:.0f})")
print("ALL PASS" if ok else "OVERHANGS FOUND")
sys.exit(0 if ok else 1)
