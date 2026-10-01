"""Independent checks for cad/indexed-mount.scad (default parameters).
Reloads the exported print-orientation STLs, maps them back to the assembly frame, and checks:
  - meshes closed, print bounds, volumes
  - vernier: exactly one pin pair locks every step angle, none in between; labels match
  - the holes the math expects are physically open in the meshes (ray casts)
  - cradle + PETAL envelope never intersect head/column over the elevation range; rim stays above bench
  - azimuth pin heads have clear access above the head disk
"""
import sys, math, numpy as np, trimesh, manifold3d as m3

D = sys.argv[1]
step, P = 1, 10
V, NV = P - step, P // step
W0, B0 = 180, 140
L, Ha, head_t, Ri, Ra = 100, 78, 12, 50, 70
dish_d, fd, el_min = 400, 0.42, -5
f = dish_d * fd
zAt = lambda r: r * r / (4 * f)
vertex_u = L + 16 - zAt(45)
rim_u = vertex_u + zAt(dish_d / 2)
axle_h = dish_d / 2 * math.cos(math.radians(el_min)) - rim_u * math.sin(math.radians(el_min)) + 15
Hc = axle_h - Ha - head_t
ok = True
def check(cond, msg):
    global ok
    print(("PASS " if cond else "FAIL ") + msg); ok &= bool(cond)

def T(m, M): m = m.copy(); m.apply_transform(M); return m
def rx(deg): return trimesh.transformations.rotation_matrix(math.radians(deg), [1, 0, 0])
def rz(deg): return trimesh.transformations.rotation_matrix(math.radians(deg), [0, 0, 1])
def tr(x, y, z): return trimesh.transformations.translation_matrix([x, y, z])

# ---- load + bounds ----
parts = {p: trimesh.load(f"{D}/{p}.stl") for p in ["cradle", "head", "column"]}
for p, m in parts.items():
    ext = m.bounds[1] - m.bounds[0]
    check(m.is_watertight and m.body_count == 1, f"{p}: closed single body ({len(m.faces)} tris)")
    check(ext[0] <= 204 and ext[1] <= 204 and ext[2] <= 248 and abs(m.bounds[0][2]) < 1e-3,
          f"{p}: on bed, fits 220x220x250 with 8 mm margin ({ext[0]:.0f} x {ext[1]:.0f} x {ext[2]:.0f} mm)")
    print(f"     {p}: {m.volume/1000:.0f} cm3 solid model")
# back to assembly frames (cradle: axle origin; head: disk top Z=0; column: bench Z=0)
cradle = T(parts["cradle"], np.linalg.inv(tr(0, 0, L) @ rx(-90)))
head = T(parts["head"], tr(0, 0, -head_t))
column = T(parts["column"], np.linalg.inv(tr(0, 0, Hc) @ rx(180)))   # printed flange-down

# ---- vernier math ----
def wrap(a): return (a + 180) % 360 - 180
el_fine = [W0 - V * k for k in range(NV)]
az_fine = [B0 + V * k for k in range(NV)]
bad = 0
for th10 in range(-100, 1001, 5):
    th = th10 / 10
    hits = [(k, j) for k in range(NV) for j in range(360 // P) if abs(wrap(el_fine[k] - (P * j + th))) < 1e-6]
    if th10 % (10 * step) == 0:
        bad += not (len(hits) == 1 and hits[0][0] * step == (th % P))
    else:
        bad += len(hits) != 0
check(bad == 0, f"elevation: one locking pair at every {step} deg from -10 to 100, labels = angle mod {P}, none between")
bad = 0
for A10 in range(0, 3600, 5):
    A = A10 / 10
    hits = [(k, j) for k in range(NV) for j in range(360 // P) if abs(wrap(az_fine[k] + A - P * j)) < 1e-6]
    if A10 % (10 * step) == 0:
        bad += not (len(hits) == 1 and hits[0][0] * step == (A % P))
    else:
        bad += len(hits) != 0
check(bad == 0, f"azimuth: one locking pair at every {step} deg over 360, labels = angle mod {P}, none between")

# ---- holes physically open (ray through material along hole axis must miss) ----
def open_along(mesh, origins, d):
    o = np.array(origins, float); dirs = np.tile(d, (len(o), 1))
    return ~mesh.ray.intersects_any(o, dirs)
pc = [[60, Ri * math.cos(math.radians(a)), Ha + Ri * math.sin(math.radians(a))] for a in el_fine]
check(open_along(head, pc, [-1, 0, 0]).all(), "head: all 10 elevation fine holes open through both cheeks")
pe = [[60, Ri * math.cos(math.radians(P * j)), Ri * math.sin(math.radians(P * j))] for j in range(360 // P)]
check(open_along(cradle, pe, [-1, 0, 0]).all(), "cradle: all 36 elevation coarse holes open through both ears")
pa = [[Ra * math.sin(math.radians(a)), Ra * math.cos(math.radians(a)), 5] for a in az_fine]
check(open_along(head, pa, [0, 0, -1]).all(), "head: all 10 azimuth fine holes open through disk")
pcol = [[Ra * math.sin(math.radians(P * j)), Ra * math.cos(math.radians(P * j)), Hc + 5] for j in range(360 // P)]
locs, ri, _ = column.ray.intersects_location(np.array(pcol, float), np.tile([0, 0, -1.0], (len(pcol), 1)))
depth = [min([(Hc + 5) - l[2] for l, i in zip(locs, ri) if i == n] or [1e9]) for n in range(len(pcol))]
check(min(depth) > 5 + 60, f"column: all 36 azimuth coarse holes open >= 60 mm deep, through the cone (min {min(depth)-5:.0f} mm)")
# hub interface: 4 x M4 on 60 BCD and the 34 mm port open along the boresight
ph = [[30 * math.cos(math.radians(a)), L - 20, 30 * math.sin(math.radians(a))] for a in (45, 135, 225, 315)] + [[0, L - 20, 0], [14, L - 20, 0]]
check(open_along(cradle, ph, [0, 1, 0]).all(), "cradle: 4 x M4 at 45/135/225/315 on 60 BCD and Ø34 port open")
# root-screw heads (r 48..57, any angle, 5 mm behind hub face) never touched
ang = np.radians(np.arange(0, 360, 2))
pr = np.array([[r * np.cos(a), L - 2.5, r * np.sin(a)] for a in ang for r in (48.5, 52.5, 56.5)])
check(not cradle.contains(pr).any(), "cradle: nothing within the root-screw head band (r >= 48, 5 mm behind hub)")
# az pin access: Ø12 x 40 column above each fine hole free of the head
def mani(m): return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))
Mh = mani(head)
acc = min(min(1e9, (Mh ^ m3.Manifold.cylinder(40, 6, 6, 32).translate([x, y, 0.2])).volume()) for x, y, _ in pa)
check(acc < 1e-6, "head: Ø12 x 40 mm finger/pin-head clearance above every azimuth fine hole")


# ---- assembled pin test: a Ø5.0 steel pin enters only the labeled hole pair ----
def pin_free(M, x0, y, z, axis):
    pin = m3.Manifold.cylinder(30 if axis == 'x' else 34, 2.5, 2.5, 48)
    pin = pin.rotate([0, 90, 0]).translate([x0, y, z]) if axis == 'x' else pin.translate([y, z, x0])
    return (M ^ pin).volume() < 1e-6
for th in (37, -4, 81):
    Mb = mani(head) + mani(T(cradle, tr(0, 0, Ha) @ rx(th)))
    seen = [k for k, a in enumerate(el_fine) if pin_free(Mb, 26.5, Ri*math.cos(math.radians(a)), Ha + Ri*math.sin(math.radians(a)), 'x')]
    check(seen == [(th % P) // step], f"assembled el {th}: Ø5.0 pin fits only cheek hole labeled {th % P} (fits: {seen})")
for A in (210, 7, 359):
    Mb = mani(T(head, rz(-A))) + mani(T(column, tr(0, 0, -Hc - head_t)))
    seen = [k for k, a in enumerate(az_fine) if pin_free(Mb, -head_t - 14 - 18, Ra*math.sin(math.radians(a + A)), Ra*math.cos(math.radians(a + A)), 'z')]
    check(seen == [(A % P) // step], f"assembled az {A}: Ø5.0 pin fits only head hole labeled {A % P} (fits: {seen})")

# ---- sweep ----
prof = [(0, zAt(45) - 16), (60, zAt(45) - 16)] + [(r, zAt(r) - 17) for r in range(60, 201, 10)] + [(r, zAt(r)) for r in range(200, -1, -10)]
dish = trimesh.creation.revolve(np.array(prof), sections=96)
dish = T(dish, tr(0, vertex_u, 0) @ rx(-90))
Mstat = mani(T(head, tr(0, 0, Hc + head_t))) + mani(column)
worst_el, min_rim = None, 1e9
from scipy.spatial import cKDTree
stat = trimesh.util.concatenate([T(head, tr(0, 0, Hc + head_t)), column])
sp, _ = trimesh.sample.sample_surface_even(stat, 400000, radius=0.35, seed=2)
tree = cKDTree(sp)
cpts, _ = trimesh.sample.sample_surface_even(cradle, 120000, radius=0.35, seed=1)
clear = []
for e in range(-5, 101, 5):
    M = tr(0, 0, Hc + head_t + Ha) @ rx(e)
    c = T(cradle, M); d = T(dish, M)
    v = (Mstat ^ (mani(c) + mani(d))).volume()
    if v > 1e-3: worst_el = (worst_el or []) + [(e, round(v))]
    min_rim = min(min_rim, d.bounds[0][2])
    clear.append(tree.query(trimesh.transform_points(cpts, M))[0].min())
check(worst_el is None, f"no intersection: cradle + dish envelope vs head + column, el -5..100 deg" + ("" if worst_el is None else f" (hit at {worst_el})"))
print(f"     smallest moving-to-fixed gap over sweep: ~{min(clear):.1f} mm by surface sampling (cheek/ear running gap is 0.40 by design)")
check(min_rim >= 14.9, f"dish rim stays >= 15 mm above bench down to el {el_min} (min {min_rim:.1f} mm); axle height {axle_h:.0f} mm")
print("ALL PASS" if ok else "SOME CHECKS FAILED")
