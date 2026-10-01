"""Checks for cad/simple-mount.scad (default parameters) using the exported print-orientation STLs.
Usage: python3 scripts/check-simple-mount.py <stl dir>      (exit status 1 on any failure)
Parts are placed with the same transforms the SCAD uses; a mismatch shows up as interference.
"""
import sys, math, numpy as np, trimesh, manifold3d as m3

D = sys.argv[1]
# ---- parameters (keep in sync with the SCAD) ----
base_t, rot_t, H, L = 14, 10, 65, 75
rot_z = base_t
Z_el = rot_z + rot_t + H
arm_x0, arm_x1, up_x0, up_x1 = 26, 38, 12, 26
m8_head = 5.5
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
base = place(mani(P["base"]), np.linalg.inv(T([0, 0, base_t]) @ Rx(180)))
rot = place(mani(P["rotator"]), T([0, 0, rot_z]))
cradle0 = place(mani(P["cradle"]), np.linalg.inv(T([0, 0, L]) @ Rx(-90)))       # elevation axis at origin
cyl = lambda r, h: mani(trimesh.creation.cylinder(radius=r, height=h, sections=48))
az_bolt = place(cyl(4, 25), T([0, 0, rot_z + rot_t + 1.6 - 12.5]))                      # M8 x 25 down into the base nut
az_head = place(cyl(8, 9.6), T([0, 0, rot_z + rot_t + 4.8]))                              # washer + socket head in the well
el_bolt0 = place(cyl(4, 40), T([up_x0 + 1.5 + m8_head + 20, 0, Z_el]) @ Ry(90))           # M8 x 40 from the head pocket
el_wing = place(cyl(14, 12), T([arm_x1 + 6, 0, Z_el]) @ Ry(90))                           # washer + wing nut, generous
prof = [(0, zAt(45) - 16), (60, zAt(45) - 16)] + [(r, zAt(r) - 17) for r in range(60, 201, 10)] + [(r, zAt(r)) for r in range(200, -1, -10)]
dish_mesh = trimesh.creation.revolve(np.array(prof), sections=96)
dish0 = mani(dish_mesh.copy().apply_transform(T([0, vertex_u, 0]) @ Rx(-90)))
def at_el(Mn, th): return place(Mn, T([0, 0, Z_el]) @ Rx(th))

# ---- clamp faces: in contact, and how much area ----
def contact(a, b, d):
    return vol(place(a, T(np.array(d) * 0.2)), b) / 0.2
ov = max(vol(place(rot, Rz(a)), base) for a in (0, 37, 90, 200))
ca = contact(rot, base, [0, 0, -1])
check(ov < 0.5 and ca > 3000, f"azimuth: rotator sits flat on the base and turns without overlap ({ov:.2f} mm³); clamp face contact {ca/100:.0f} cm²")
ce = [contact(at_el(cradle0, th), rot, [-1, 0, 0]) for th in (EL_RANGE[0], 0, 45, EL_RANGE[1])]
check(min(ce) > 1500, f"elevation: cradle arm sits flat on the upright; clamp face contact {min(ce)/100:.0f}–{max(ce)/100:.0f} cm² over the range")
v = [vol(az_bolt, base), vol(az_bolt, rot), vol(az_head, rot), vol(el_bolt0, rot)] + [vol(el_bolt0, at_el(cradle0, th)) for th in (EL_RANGE[0], 0, 45, EL_RANGE[1])]
check(max(v) < 0.5, f"M8 bolts pass through both joints at every angle (max overlap {max(v):.2f} mm³)")

# ---- elevation travel ----
fixed = rot + az_head
hits = [th for th in np.arange(-40, 130.1, 2.5) if vol(at_el(cradle0, th), fixed) > 0.5]
inside = [h for h in hits if EL_RANGE[0] <= h <= EL_RANGE[1]]
lo = max([h for h in hits if h < 0], default=None); hi = min([h for h in hits if h > 0], default=None)
check(not inside, f"cradle tilts from {EL_RANGE[0]}° to {EL_RANGE[1]}° clear of the rotator and the azimuth bolt (first contact {lo}° / {hi}°)")
dh = [th for th in np.arange(EL_RANGE[0], EL_RANGE[1] + 0.1, 2.5) if vol(at_el(dish0, th), rot + base) > 0.5]
check(not dh, f"dish envelope clears the rotator and base from {EL_RANGE[0]}° to {EL_RANGE[1]}° {dh[:4]}")

# ---- azimuth sweep ----
bad = []
for th in (EL_RANGE[0], 0, 45, 90):
    mov = rot + az_head + at_el(cradle0, th) + at_el(dish0, th) + at_el(place(el_wing, T([0, 0, -Z_el])), th)
    for A in range(15, 360, 15):
        v = vol(place(mov, Rz(-A)), base)
        if v > 0.5: bad.append((th, A, round(v, 1)))
check(not bad, f"azimuth sweep 15–345° at el {EL_RANGE[0]}/0/45/90: rotator, cradle and dish clear the base {bad[:6]}")
low = dish_mesh.copy().apply_transform(T([0, 0, Z_el]) @ Rx(EL_RANGE[0]) @ T([0, vertex_u, 0]) @ Rx(-90)).bounds[0][2]
print(f"     dish rim reaches {-low:.0f} mm below the base bottom at {EL_RANGE[0]}°: mount the base at least that high")

# ---- hub interface ----
cr = trimesh.load(f"{D}/cradle.stl"); cr.apply_transform(np.linalg.inv(T([0, 0, L]) @ Rx(-90)))
def open_along(mesh, pts, d): return ~mesh.ray.intersects_any(np.array(pts, float), np.tile(d, (len(pts), 1)))
ph = [[30 * math.cos(math.radians(a)), L - 30, 30 * math.sin(math.radians(a))] for a in (45, 135, 225, 315)] + [[0, L - 30, 0], [14, L - 30, 0]]
check(open_along(cr, ph, [0, 1, 0]).all(), "cradle: 4 x M4 on 60 BCD and Ø34 port open, with straight access from behind")
ang = np.radians(np.arange(0, 360, 2))
pr = np.array([[r * np.cos(a), L - 2.5, r * np.sin(a)] for a in ang for r in (48.5, 52.5, 56.5)])
check(not cr.contains(pr).any(), "cradle: root-screw head band (r >= 48) clear")
# ---- tool access: hex key straight down the well to the azimuth bolt, at the elevations where it's reachable ----
key = place(cyl(3.5, 200), T([0, 0, rot_z + rot_t + 10 + 100]))
reach = [th for th in range(-10, 101, 10) if vol(key, rot + at_el(cradle0, th) + at_el(dish0, th)) < 0.5]
check(reach and min(reach) <= 0, f"hex key reaches the azimuth bolt straight down the well at el {min(reach)}° to {max(reach)}°")
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
