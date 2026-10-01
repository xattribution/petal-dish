"""Overhang scan for print-orientation STLs.
Flags every downward-facing surface steeper than 45° from vertical (excluding the bed face),
splits out flat ceilings (bridges), and groups flagged faces into connected regions so each
region's location and size can be reported.
Usage: python3 scripts/check-printability.py part1.stl [part2.stl ...]
"""
import sys, numpy as np, trimesh

LIMIT = np.cos(np.radians(45)) + 0.01   # n_z below -LIMIT means steeper than ~44.4°
NOISE = 0.05                             # mm² per connected region: tessellation slivers below slicer resolution
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
            regions.append((a[comp].sum(), span, pts.mean(0)))
        regions.sort(key=lambda r: -r[0])
    name = path.split('/')[-1]
    real = [r for r in regions if r[0] >= NOISE]
    noise = len(regions) - len(real)
    status = "PASS" if not real else "FAIL"
    ok &= not real
    print(f"{status} {name}: {sum(r[0] for r in real):.1f} mm² steeper than 45° ({a[flat].sum():.1f} mm² flat ceilings) in {len(real)} regions"
          + (f"; ignored {noise} sliver regions < {NOISE} mm² ({sum(r[0] for r in regions if r[0] < NOISE):.2f} mm² total)" if noise else ""))
    for area, span, ctr in real[:6]:
        print(f"     {area:7.1f} mm²  span {span[0]:5.1f} x {span[1]:5.1f} x {span[2]:4.1f}  at ({ctr[0]:.0f}, {ctr[1]:.0f}, z={ctr[2]:.0f})")
print("ALL PASS" if ok else "OVERHANGS FOUND")
