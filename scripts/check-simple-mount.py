"""Checks for cad/simple-mount.scad (default parameters) using the exported print-orientation STLs.
Usage: python3 scripts/check-simple-mount.py <stl dir>      (exit status 1 on any failure)
Parts are placed with the same transforms the SCAD uses; a mismatch shows up as interference.
"""
import sys, math, numpy as np, trimesh, manifold3d as m3

D = sys.argv[1]
# ---- parameters (keep in sync with the SCAD) ----
sp_z0, Z_el, L = 14, 128, 70
sp_r, sh_r, clr, b_dip = 25, 20, 0.2, 1.5
sock_r, bore_r = sp_r + clr, sh_r + clr
lug_w = 16
az_bz, az_by = 34, -(sp_r + clr + 3 - b_dip)
el_bz = bore_r + 3 - b_dip
plate_r = 47
x_bed = -plate_r * math.cos(math.radians(45))
dish_d, fd = 400, 0.42
f = dish_d * fd
zAt = lambda r: r * r / (4 * f)
vertex_u = L + 16 - zAt(45)
EL_RANGE = (-10, 100)

ok = True
def check(c, msg):
    global ok
    print(("PASS " if c else "FAIL ") + msg); ok &= bool(c)
def T(v): M = np.eye(4); M[:3, 3] = v; return M
def R(axis, deg): return trimesh.transformations.rotation_matrix(math.radians(deg), axis)
Rx = lambda d: R([1, 0, 0], d); Ry = lambda d: R([0, 1, 0], d); Rz = lambda d: R([0, 0, 1], d)
def mani(m): return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))
def place(Mn, M): return Mn.transform(M[:3, :].tolist())
def vol(a, b): return (a ^ b).volume()

# ---- load / bed fit ----
names = ["base", "rotator", "cradle"]
P = {n: trimesh.load(f"{D}/{n}.stl") for n in names}
for n, m in P.items():
    e = m.extents
    check(m.is_watertight and m.body_count == 1 and abs(m.bounds[0][2]) < 1e-3 and e[0] <= 204 and e[1] <= 204 and e[2] <= 248,
          f"{n}: closed, on bed, fits 220x220x250 ({e[0]:.0f} x {e[1]:.0f} x {e[2]:.0f} mm, {m.volume/1000:.0f} cm³ solid)")
base = mani(P["base"])
rot = place(mani(P["rotator"]), T([0, 0, sp_z0]))
cradle0 = place(mani(P["cradle"]), np.linalg.inv(T([0, 0, -x_bed]) @ Ry(-90)))     # el axis at origin
cyl = lambda d, h: mani(trimesh.creation.cylinder(radius=d / 2, height=h, sections=48))
az_bolt = place(cyl(6, 2 * lug_w), T([0, az_by, az_bz]) @ Ry(90))
el_bolt0 = place(cyl(6, 2 * lug_w), T([0, 0, Z_el + el_bz]) @ Rx(90))
prof = [(0, zAt(45) - 16), (60, zAt(45) - 16)] + [(r, zAt(r) - 17) for r in range(60, 201, 10)] + [(r, zAt(r)) for r in range(200, -1, -10)]
dish0 = mani(trimesh.creation.revolve(np.array(prof), sections=96).apply_transform(T([0, vertex_u, 0]) @ Rx(-90)))
def el_set(th, dish=True):
    M = T([0, 0, Z_el]) @ Rx(th)
    s = place(cradle0, M)
    return s + place(dish0, M) if dish else s

# ---- azimuth joint ----
ov = max(vol(place(rot, Rz(a)), base) for a in (0, 17, 90, 180, 271))
check(ov < 0.5, f"rotator turns in the base socket (max overlap {ov:.2f} mm³ at 5 azimuths)")
v0 = vol(az_bolt, base); v1 = vol(az_bolt, rot)
cap = min(vol(place(az_bolt, T([0, 0, dz])), rot) for dz in (-2, 2))
check(v0 < 0.5 and v1 < 0.5 and cap > 1, f"azimuth bolt fits its hole ({v0:.2f} mm³), rides in the spigot groove ({v1:.2f} mm³) and captures it (±2 mm → {cap:.0f} mm³)")

# ---- elevation joint ----
rot_fixed = rot + el_bolt0
hits = [th for th in np.arange(-25, 115.1, 2.5) if vol(el_set(th, False), rot_fixed) > 0.5]
free = [th for th in np.arange(-25, 115.1, 2.5) if th not in hits]
check(not [h for h in hits if EL_RANGE[0] <= h <= EL_RANGE[1]],
      f"cradle turns in the sleeve from {EL_RANGE[0]}° to {EL_RANGE[1]}° without touching the rotator or bolt (cradle stops at {[h for h in hits][:2]}…)")
v0 = vol(el_bolt0, rot); v1 = max(vol(el_bolt0, el_set(th, False)) for th in (-10, 0, 45, 100))
cap = min(vol(place(el_bolt0, T([dx, 0, 0])), el_set(0, False)) for dx in (-2, 2))
check(v0 < 0.5 and v1 < 0.5 and cap > 1, f"elevation bolt fits its hole ({v0:.2f} mm³), rides in the shaft groove ({v1:.2f} mm³) and captures it (±2 mm → {cap:.0f} mm³)")
dh = [th for th in np.arange(EL_RANGE[0], EL_RANGE[1] + 0.1, 2.5) if vol(place(dish0, T([0, 0, Z_el]) @ Rx(th)), rot + base) > 0.5]
check(not dh, f"dish envelope clears the rotator and base from {EL_RANGE[0]}° to {EL_RANGE[1]}° {dh[:4]}")

# ---- azimuth sweep ----
fixed = base + az_bolt
bad = []
for th in (EL_RANGE[0], 0, 45, 90):
    mov = rot + el_bolt0 + el_set(th)
    for A in range(0, 360, 15):
        v = vol(place(mov, Rz(-A)), fixed) if A else 0
        if A and v > 0.5: bad.append((th, A, round(v, 1)))
check(not bad, f"azimuth sweep 15–345° at el {EL_RANGE[0]}/0/45/90: rotator, cradle and dish clear the base and its bolt {bad[:6]}")
low = [trimesh.creation.revolve(np.array(prof), sections=96).apply_transform(T([0, 0, Z_el]) @ Rx(th) @ T([0, vertex_u, 0]) @ Rx(-90)).bounds[0][2]
       for th in (EL_RANGE[0], 0)]
print(f"     dish rim reaches {-low[0]:.0f} mm below the base bottom at {EL_RANGE[0]}° ({-low[1]:.0f} mm at 0°): mount the base at least that high")

# ---- hub interface ----
cr = trimesh.load(f"{D}/cradle.stl"); cr.apply_transform(np.linalg.inv(T([0, 0, -x_bed]) @ Ry(-90)))
def open_along(mesh, pts, d): return ~mesh.ray.intersects_any(np.array(pts, float), np.tile(d, (len(pts), 1)))
ph = [[30 * math.cos(math.radians(a)), L - 30, 30 * math.sin(math.radians(a))] for a in (45, 135, 225, 315)] + [[0, L - 30, 0], [14, L - 30, 0]]
check(open_along(cr, ph, [0, 1, 0]).all(), "cradle: 4 x M4 on 60 BCD and Ø34 port open, with straight access from behind")
ang = np.radians(np.arange(0, 360, 2))
pr = np.array([[r * np.cos(a), L - 2.5, r * np.sin(a)] for a in ang for r in (48.5, 52.5, 56.5)])
check(not cr.contains(pr).any(), "cradle: root-screw head band (r >= 48) clear")
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
