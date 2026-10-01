"""Checks for cad/simple-mount.scad (default parameters) using the exported print-orientation STLs.
Usage: python3 scripts/check-simple-mount.py <stl dir>      (exit status 1 on any failure)
Parts are placed with the same transforms the SCAD uses; a mismatch shows up as interference.
"""
import sys, math, numpy as np, trimesh, manifold3d as m3

D = sys.argv[1]
# ---- parameters (keep in sync with the SCAD) ----
base_t, yoke_t, H, L = 14, 10, 70, 75
yoke_z = base_t
Z_el = yoke_z + yoke_t + H
yk_in, yk_out = 18, 30
side_clr = 0.2
cr_in = yk_out + side_clr; cr_out = cr_in + 12
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
MX = np.diag([-1.0, 1, 1, 1])
def mani(m): return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))
def place(Mn, M): return Mn.transform(M[:3, :].tolist())
def vol(a, b): return (a ^ b).volume()

# ---- load / bed fit ----
names = ["base", "yoke", "cradle"]
P = {n: trimesh.load(f"{D}/{n}.stl") for n in names}
for n, m in P.items():
    e = m.extents
    check(m.is_watertight and m.body_count == 1 and abs(m.bounds[0][2]) < 1e-3 and e[0] <= 204 and e[1] <= 204 and e[2] <= 248,
          f"{n}: closed, on bed, fits 220x220x250 ({e[0]:.0f} x {e[1]:.0f} x {e[2]:.0f} mm, {m.volume/1000:.0f} cm³ solid)")
base = place(mani(P["base"]), np.linalg.inv(T([0, 0, base_t]) @ Rx(180)))
yoke = place(mani(P["yoke"]), T([0, 0, yoke_z]))
cradle0 = place(mani(P["cradle"]), np.linalg.inv(T([0, 0, L]) @ Rx(-90)))       # elevation axis at origin
cyl = lambda r, h: mani(trimesh.creation.cylinder(radius=r, height=h, sections=48))
hexp = lambda af, h: mani(trimesh.creation.cylinder(radius=af / math.sqrt(3), height=h, sections=6))
# hardware: azimuth M8 x 25 socket head from the top into the base nut; elevation M8 x 30 hex bolts, one per side
az_bolt = place(cyl(4, 25), T([0, 0, yoke_z + yoke_t + 1.6 - 12.5]))
az_head = place(cyl(8, 9.6), T([0, 0, yoke_z + yoke_t + 4.8]))
el_r = place(cyl(4, 30), T([yk_in + m8_head + 15, 0, Z_el]) @ Ry(90)) + \
       place(hexp(13, 5.3), T([yk_in + 0.2 + 2.65, 0, Z_el]) @ Ry(90) @ Rz(30))
el_hw = el_r + place(el_r, MX)
el_wing = place(cyl(13, 12), T([cr_out + 6, 0, 0]) @ Ry(90))
el_wing = el_wing + place(el_wing, MX)                                             # in the cradle frame
prof = [(0, zAt(45) - 16), (60, zAt(45) - 16)] + [(r, zAt(r) - 17) for r in range(60, 201, 10)] + [(r, zAt(r)) for r in range(200, -1, -10)]
dish_mesh = trimesh.creation.revolve(np.array(prof), sections=96)
dish0 = mani(dish_mesh.copy().apply_transform(T([0, vertex_u, 0]) @ Rx(-90)))
def at_el(Mn, th): return place(Mn, T([0, 0, Z_el]) @ Rx(th))
def contact(a, b, d): return vol(place(a, T(np.array(d) * 0.2)), b) / 0.2

# ---- clamp faces ----
ov = max(vol(place(yoke, Rz(a)), base) for a in (0, 37, 90, 200))
ca = contact(yoke, base, [0, 0, -1])
check(ov < 0.5 and ca > 3000, f"azimuth: yoke sits flat on the base and turns without overlap ({ov:.2f} mm³); clamp face contact {ca/100:.0f} cm²")
cr_r = place(cradle0, T([0, 0, 0]))
ce = []
for th in (EL_RANGE[0], 0, 45, EL_RANGE[1]):
    c = at_el(cradle0, th)
    # shift each cheek in by the side clearance plus 0.2 mm and measure the overlap with the yoke cheek it clamps
    for sgn in (-1, 1):
        half = c ^ m3.Manifold.cube([200, 400, 400]).translate([0 if sgn > 0 else -200, -200, -200 + Z_el])
        ce.append(vol(place(half, T([-sgn * (side_clr + 0.2), 0, 0])), yoke) / 0.2)
ce = [ce[i] + ce[i + 1] for i in range(0, len(ce), 2)]
check(min(ce) > 2 * 1500, f"elevation: both cradle cheeks sit flat on the yoke cheeks; total clamp face contact {min(ce)/100:.0f}–{max(ce)/100:.0f} cm² over the range")
v = [vol(az_bolt, base), vol(az_bolt + az_head, yoke), vol(el_hw, yoke)] + [vol(el_hw, at_el(cradle0, th)) for th in (EL_RANGE[0], 0, 45, EL_RANGE[1])]
check(max(v) < 0.5, f"bolts and heads sit in their holes and pockets at every angle (max overlap {max(v):.2f} mm³)")

# ---- elevation travel ----
fixed = yoke + az_head                     # the wing nuts thread onto the elevation bolts, so those two aren't compared
hits = [th for th in np.arange(-40, 130.1, 2.5) if vol(at_el(cradle0 + el_wing, th), fixed) > 0.5 or vol(at_el(cradle0, th), el_hw) > 0.5]
inside = [h for h in hits if EL_RANGE[0] <= h <= EL_RANGE[1]]
lo = max([h for h in hits if h < 0], default=None); hi = min([h for h in hits if h > 0], default=None)
check(not inside, f"cradle tilts from {EL_RANGE[0]}° to {EL_RANGE[1]}° clear of the yoke and hardware (first contact {lo}° / {hi}°)")
dh = [th for th in np.arange(EL_RANGE[0], EL_RANGE[1] + 0.1, 2.5) if vol(at_el(dish0, th), yoke + base) > 0.5]
check(not dh, f"dish envelope clears the yoke and base from {EL_RANGE[0]}° to {EL_RANGE[1]}° {dh[:4]}")

# ---- azimuth sweep ----
bad = []
for th in (EL_RANGE[0], 0, 45, 90):
    mov = yoke + az_head + el_hw + at_el(cradle0 + el_wing, th) + at_el(dish0, th)
    for A in range(15, 360, 15):
        v = vol(place(mov, Rz(-A)), base)
        if v > 0.5: bad.append((th, A, round(v, 1)))
check(not bad, f"azimuth sweep 15–345° at el {EL_RANGE[0]}/0/45/90: everything that turns clears the base {bad[:6]}")
low = dish_mesh.copy().apply_transform(T([0, 0, Z_el]) @ Rx(EL_RANGE[0]) @ T([0, vertex_u, 0]) @ Rx(-90)).bounds[0][2]
print(f"     dish rim reaches {-low:.0f} mm below the base bottom at {EL_RANGE[0]}°: mount the base at least that high")

# ---- tool access: hex key straight down onto the azimuth bolt, between the yoke cheeks ----
key = place(cyl(3.5, 300), T([0, 0, yoke_z + yoke_t + 10 + 150]))
reach = [th for th in range(-10, 101, 10) if vol(key, yoke + at_el(cradle0, th) + at_el(dish0, th)) < 0.5]
check(reach and min(reach) <= 0, f"hex key reaches the azimuth bolt straight down between the cheeks at el {min(reach)}° to {max(reach)}°")

# ---- hub interface ----
cr = trimesh.load(f"{D}/cradle.stl"); cr.apply_transform(np.linalg.inv(T([0, 0, L]) @ Rx(-90)))
def open_along(mesh, pts, d): return ~mesh.ray.intersects_any(np.array(pts, float), np.tile(d, (len(pts), 1)))
ph = [[30 * math.cos(math.radians(a)), L - 30, 30 * math.sin(math.radians(a))] for a in (45, 135, 225, 315)] + [[0, L - 30, 0], [14, L - 30, 0]]
check(open_along(cr, ph, [0, 1, 0]).all(), "cradle: 4 x M4 on 60 BCD and Ø34 port open, with straight access from behind")
ang = np.radians(np.arange(0, 360, 2))
pr = np.array([[r * np.cos(a), L - 2.5, r * np.sin(a)] for a in ang for r in (48.5, 52.5, 56.5)])
check(not cr.contains(pr).any(), "cradle: root-screw head band (r >= 48) clear")
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
