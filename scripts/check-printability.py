"""Overhang scan for print-orientation STLs.
Flags every downward-facing surface steeper than 45° from vertical (excluding the bed face),
splits out flat ceilings (bridges), and groups flagged faces into connected regions so each
region's location and size can be reported.
Usage: python3 scripts/check-printability.py part1.stl [part2.stl ...]
"""
import sys, numpy as np, trimesh

LIMIT = np.cos(np.radians(45)) + 0.01   # n_z below -LIMIT means steeper than ~44.4°
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
    status = "PASS" if not bad.any() else "FAIL"
    ok &= not bad.any()
    print(f"{status} {name}: {a[bad].sum():.0f} mm² steeper than 45° ({a[flat].sum():.0f} mm² flat ceilings), {len(regions)} regions")
    for area, span, ctr in regions[:6]:
        print(f"     {area:7.1f} mm²  span {span[0]:5.1f} x {span[1]:5.1f} x {span[2]:4.1f}  at ({ctr[0]:.0f}, {ctr[1]:.0f}, z={ctr[2]:.0f})")
print("ALL PASS" if ok else "OVERHANGS FOUND")
