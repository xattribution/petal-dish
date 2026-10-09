"""Checks for cad/simple-mount.scad using the exported print-orientation STLs, for every option variant.
Usage: python3 scripts/check-simple-mount.py <stl dir>      (exit status 1 on any failure; needs OpenSCAD on PATH)
Parts are placed with the SCAD's own print transforms (read through part = "matrices"), inverted; a mismatch
shows up as interference or missing contact.
Variants: default (base, yoke, upright, cradle, cheek), arc lock (upright-arc, cheek-arc), no base (yoke-stand), and the
elevation clamp on both sides (yoke-dual, yoke-dual-stand, cradle-dual and the mirrored upright-left and cheek-left).
The left-only option is the mirror image of the default, so it shares the default's checks.
"""
import sys, math, json, re, pathlib, subprocess, tempfile
import numpy as np, trimesh, manifold3d as m3

D = sys.argv[1]
SCAD = pathlib.Path(__file__).resolve().parents[1] / 'cad/simple-mount.scad'
with tempfile.TemporaryDirectory() as tmp:
    out = pathlib.Path(tmp) / 'm.echo'
    subprocess.run(['openscad', '-o', str(out), '-D', 'part="matrices"', str(SCAD)], check=True, capture_output=True)
    INFO = {k: json.loads(v) for k, v in re.findall(r'ECHO: (\w+) = (.*)', out.read_text())}
base_t, yoke_t, Z_el, L, plate_t, cr_in, cr_out, up_in, up_out, cap_r = INFO['FRAME']
ins_d, ins_deep, ins_len, cb4_d, up_len, ch_len, n_up, n_ch, ch_x = INFO['JOINT']
cb4_h, cb5_d, cb5_h, step = INFO['COUNTERBORE']
arc_r, arc_phi, arc_w, arc_margin, el_min, el_max, m6_af, m6_head, arc_len = INFO['ARC']
inset, fit, lead, ch_shoulder = INFO['INSET']
gus_w, gus_h, gus_len = INFO['GUSSET']
yoke_z = base_t; top = base_t + yoke_t
m8_head = 5.5
dish_d, fd = 400, 0.42
f = dish_d * fd
zAt = lambda r: r * r / (4 * f)
vertex_u = L + 16 - zAt(45)
EL_RANGE = (el_min, el_max)

ok = True
def check(c, msg):
    global ok
    print(("PASS " if c else "FAIL ") + msg); ok &= bool(c)
def T(v): M = np.eye(4); M[:3, 3] = v; return M
def R(axis, deg): return trimesh.transformations.rotation_matrix(math.radians(deg), axis)
Rx = lambda d: R([1, 0, 0], d); Ry = lambda d: R([0, 1, 0], d); Rz = lambda d: R([0, 0, 1], d)
def mani(m): return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))
def place(Mn, M): return Mn.transform(np.asarray(M)[:3, :].tolist())
def vol(a, b): return (a ^ b).volume()
def union(xs):
    out = m3.Manifold()
    for x in xs: out = out + x
    return out
cyl = lambda r, h, n=48: mani(trimesh.creation.cylinder(radius=r, height=h, sections=n))     # centered, along Z
hexp = lambda af, h: mani(trimesh.creation.cylinder(radius=af / math.sqrt(3), height=h, sections=6))
def cone(r0, r1, h, n=48):   # along +Z from z = 0 (radius r0) to z = h (radius r1)
    return m3.Manifold.cylinder(h, r0, r1, n)
def along_x(Mn, x0): return place(Mn, T([x0, 0, 0]) @ Ry(90))   # a +Z solid turned to run along +X from x0

# ---- load, bed fit, overhang scan (print orientation) ----
NAMES = ["base", "yoke", "yoke-stand", "upright", "upright-arc", "cradle", "cheek", "cheek-arc", "yoke-dual", "yoke-dual-stand", "cradle-dual", "upright-left", "cheek-left"]
P = {n: trimesh.load(f"{D}/simple-{n}.stl") for n in NAMES}
COS45 = math.cos(math.radians(44.85))   # flag faces more than 45.15° past vertical
for n, m in P.items():
    e = m.extents
    check(m.is_watertight and m.body_count == 1 and abs(m.bounds[0][2]) < 1e-3 and e[0] <= 204 and e[1] <= 204 and e[2] <= 248,
          f"{n}: closed, on bed, fits 220x220x250 ({e[0]:.0f} x {e[1]:.0f} x {e[2]:.0f} mm, {m.volume/1000:.0f} cm³ solid)")
    nz = m.face_normals[:, 2]; zmin = m.vertices[m.faces][:, :, 2].min(1)
    bad = np.where((nz < -COS45) & (zmin > 0.01))[0]
    over, bridges = 0.0, []
    if len(bad):
        adj = m.face_adjacency[np.isin(m.face_adjacency, bad).all(1)]
        for g in trimesh.graph.connected_components(adj, nodes=bad, min_len=1):
            g = np.asarray(g); area = m.area_faces[g].sum()
            if (nz[g] < -0.999).all():   # flat ceiling: a bridge, judged by its short span
                xy = m.vertices[m.faces[g]].reshape(-1, 3)[:, :2]; span = min(np.ptp(xy, 0))
                bridges.append(span)
                if span > 10: over += area
            else: over += area
    check(over < 1.0, f"{n}: prints without supports - nothing overhangs past 45° ({over:.2f} mm²)" +
          (f"; bridges ≤ {max(bridges):.1f} mm" if bridges else "; no bridges"))

W = lambda n, M: place(mani(P[n]), np.linalg.inv(np.array(INFO[M])))
base = W("base", "M_base")
yoke, yoke_s = W("yoke", "M_yoke"), W("yoke-stand", "M_yoke")
upright, upright_a = W("upright", "M_upright"), W("upright-arc", "M_upright")
cradle0 = W("cradle", "M_cradle")                                         # elevation axis at the origin, el = 0
cheek0, cheek0_a = W("cheek", "M_cheek"), W("cheek-arc", "M_cheek")
def at_el(Mn, th): return place(Mn, T([0, 0, Z_el]) @ Rx(th))
def contact(a, b, d): return vol(place(a, T(np.array(d) * 0.2)), b) / 0.2

# ---- hardware models ----
# azimuth: M8 x 25 socket head from the top into the base nut
az_bolt = place(cyl(4, 25), T([0, 0, top + 1.6 - 12.5]))
az_head = place(cyl(8, 9.6), T([0, 0, top + 4.8]))
# elevation: M8 x 40 hex bolt, head in the cheek's pocket (cradle frame); fender washer Ø30 and a DIN 315 wing nut
# (wings Ø39) outside the upright, modeled as their swept envelope
el_bolt0 = along_x(cone(4, 4, 40), cr_in + m8_head) + along_x(place(hexp(13, 5.3), T([0, 0, 2.65])), cr_in + 0.2)
el_wing = along_x(cone(15, 15, 1.6), up_out) + along_x(cone(19.5, 19.5, 20), up_out + 1.6)
# joint screws: ISO 4762 socket heads (M4: head Ø7 x 4) in counterbores, head 0.5 mm below the face; length under the head
def capscrew(length, r_head=3.5, k=4.0, r=2.0, sink=0.5): return place(cone(r_head, r_head, k), T([0, 0, sink])) + place(cone(r, r, length), T([0, 0, sink + k]))
def insert_at(length): return cone(ins_d / 2 - 0.05, ins_d / 2 - 0.05, length)
up_scr = union([place(capscrew(up_len), T([x, y, yoke_z])) for x, y in INFO['UP_SCREWS']])            # world
up_ins = union([place(insert_at(ins_len), T([x, y, top - inset + 0.5])) for x, y in INFO['UP_SCREWS']])   # seated 0.5 below the tenon face
up_ring = union([place(cone(4.8, 4.8, ins_len) - cone(ins_d / 2, ins_d / 2, ins_len), T([x, y, top - inset])) for x, y in INFO['UP_SCREWS']])
to_minus_y = Rx(90)   # +Z -> -Y
ch_scr = union([place(capscrew(ch_len), T([ch_x, L, z]) @ to_minus_y) for z in INFO['CH_SCREWS']])   # cradle frame
ch_ins = union([place(insert_at(ins_len), T([ch_x, L - plate_t + inset - 0.5, z]) @ to_minus_y) for z in INFO['CH_SCREWS']])
ch_ring = union([place(cone(4.8, 4.8, ins_len) - cone(ins_d / 2, ins_d / 2, ins_len), T([ch_x, L - plate_t + inset, z]) @ to_minus_y) for z in INFO['CH_SCREWS']])
# hub bolts behind the cradle plate: M4 socket head Ø7 x 4 on a Ø9 x 1 washer, shank through the plate (cradle frame)
hub_bolts = union([place(cone(2, 2, plate_t + 8) + place(cone(4.5, 4.5, 1), T([0, 0, -1])) + place(cone(3.5, 3.5, 4), T([0, 0, -5])),
                         T([30 * math.cos(math.radians(a)), L - plate_t, 30 * math.sin(math.radians(a))]) @ Rx(-90)) for a in (45, 135, 225, 315)])
# arc lock (cradle frame at el = 0): M6 hex head in the cheek pocket, bolt, Ø12 washer + nyloc outside the upright
ay, az_ = arc_r * math.cos(math.radians(arc_phi)), arc_r * math.sin(math.radians(arc_phi))
arc_head = place(along_x(place(hexp(10, 4), T([0, 0, 2])), cr_in + 0.3), T([0, ay, az_]))
arc_shaft = place(along_x(cone(3, 3, arc_len), cr_in + 0.3 + 4), T([0, ay, az_]))
arc_out = place(along_x(cone(6, 6, 1.6) + place(hexp(10, 8), T([0, 0, 1.6 + 4])), up_out), T([0, ay, az_]))
arc_hw0 = arc_head + arc_shaft + arc_out
# stand screws (no base): M5 socket heads (Ø8.5 x 5) in the yoke top's counterbores, into the stand below
st_scr = union([place(capscrew(30, 4.25, 5.0, 2.5), T([x, y, top]) @ Rx(180)) for x, y in INFO['STAND']])

# dish envelope: hub, then the seam flanges and bolts (17 mm behind the front)
prof = [(0, zAt(45) - 16), (60, zAt(45) - 16)] + [(r, zAt(r) - 17) for r in range(60, 201, 10)] + [(r, zAt(r)) for r in range(200, -1, -10)]
dish_mesh = trimesh.creation.revolve(np.array(prof), sections=96)
dish0 = mani(dish_mesh.copy().apply_transform(T([0, vertex_u, 0]) @ Rx(-90)))
# with snap clips: a full ring 26 mm deep from r 66 (clips sit only on the seams, so a ring is conservative)
CLIP_RANGE = (-7.5, EL_RANGE[1])
profc = [(0, zAt(45) - 16), (60, zAt(45) - 16), (60, zAt(60) - 17), (66, zAt(66) - 17)] + [(r, zAt(r) - 26) for r in range(66, 201, 10)] + [(200, zAt(200) - 26)] + [(r, zAt(r)) for r in range(200, -1, -10)]
dishc = mani(trimesh.creation.revolve(np.array(profc), sections=96).apply_transform(T([0, vertex_u, 0]) @ Rx(-90)))
ELS = (EL_RANGE[0], 0, 45, EL_RANGE[1])

# ---- joints (every variant uses the same joint geometry; the arc and stand variants are checked too) ----
print("-- joints")
for nm, yk, up, ck in (("default", yoke, upright, cheek0), ("arc lock", yoke, upright_a, cheek0_a), ("no base", yoke_s, upright, cheek0)):
    cu, cc = contact(up, yk, [0, 0, -1]), contact(ck, cradle0, [0, 1, 0])
    ov = vol(up, yk) + vol(ck, cradle0)
    check(cu > 1500 and cc > 600 and ov < 0.5, f"{nm}: upright sits flush on the yoke plate ({cu/100:.1f} cm²), cheek flush on the cradle plate ({cc/100:.1f} cm²), overlap {ov:.2f} mm³")
    v = [vol(up_scr, yk), vol(up_scr, up), vol(ch_scr, cradle0), vol(ch_scr, ck), vol(up_ins, up), vol(ch_ins, ck)]
    check(max(v) < 0.5, f"{nm}: {n_up} x M4 x {up_len} and {n_ch} x M4 x {ch_len} socket heads sit 0.5 mm below the face in their counterbores and stop short of the pilot ends; inserts fit their pilots (max overlap {max(v):.2f} mm³)")
def plane_area(n, M, normal, axis, value):   # area of the part's faces lying in a plane, in the assembly frame
    m = P[n].copy().apply_transform(np.linalg.inv(np.array(INFO[M])))
    sel = (m.face_normals @ np.array(normal) > 0.999) & (abs(m.triangles_center[:, axis] - value) < 1e-3)
    return m.area_faces[sel].sum()
fu, fc = plane_area("upright", "M_upright", [0, 0, -1], 2, top - inset), plane_area("cheek", "M_cheek", [0, 1, 0], 1, L - plate_t)
cu, cc = contact(upright, yoke, [0, 0, -1]), contact(cheek0, cradle0, [0, 1, 0])
check(cu / fu > 0.98 and cc / fc > 0.98, f"mating faces bear fully: upright tenon {min(100, 100*cu/fu):.0f}% on the yoke pocket floor, cheek end face and gusset {min(100, 100*cc/fc):.0f}% on the cradle plate (inside its rim chamfer)")
# inset joints: each tenon sits in its pocket with {fit} mm clearance and locks the arm sideways once it moves more than that
lat = [(d, vol(place(upright, T(np.array(d) * (fit + 0.5))), yoke)) for d in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0])] + \
      [(d, vol(place(cheek0, T(np.array(d) * (fit + 0.5))), cradle0)) for d in ([1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1])]
free = [vol(place(upright, T(np.array(d) * (fit - 0.05))), yoke) for d in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0])] + \
       [vol(place(cheek0, T(np.array(d) * (fit - 0.05))), cradle0) for d in ([1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1])]
check(max(free) < 0.5 and min(v for _, v in lat) > 5, f"inset joints: {inset} mm tenons drop into their pockets with {fit} mm clearance and lock each arm sideways in every direction (min engagement {min(v for _, v in lat):.0f} mm³ at {fit + 0.5} mm)")
gus = vol(place(cheek0, T([0, 0.2, 0])), cradle0) - vol(place(cheek0, T([0, 0.2, 0])), cradle0 - place(m3.Manifold.cube([100, 100, 100]), T([-100 + cr_in, 0, -50])))
check(gus > gus_w * gus_h * 0.2 * 0.95, f"cheek gusset ({gus_w} wide, {gus_h} x {gus_len} mm) bears on the cradle plate ({gus/0.2:.0f} mm² of contact)")
wall_u = vol(up_ring, upright) / up_ring.volume(); wall_c = vol(ch_ring, cheek0) / ch_ring.volume()
check(wall_u > 0.9 and wall_c > 0.9, f"insert bosses: ≥ 2 mm of plastic around each pilot ({100*wall_u:.0f}% / {100*wall_c:.0f}% solid, teardrop roofs excepted)")
hw_world = [("azimuth bolt", az_bolt + az_head), ("wing nut", el_wing), ("stand screws", st_scr)]
v = [vol(up_scr, h) for _, h in hw_world] + [vol(ch_scr, hub_bolts), vol(ch_scr, el_bolt0), vol(ch_scr, arc_hw0), vol(up_scr, base), vol(st_scr, upright)]
check(max(v) < 0.5, f"joint screws clear the azimuth bolt, wing nut, stand screws, hub bolts, elevation bolt and arc bolt (max {max(v):.2f} mm³)")
v = [vol(hub_bolts, cheek0), vol(hub_bolts, cradle0)]
check(max(v) < 0.5, f"hub M4 bolts (head + washer behind the plate) clear the cheek ({max(v):.2f} mm³)")

# ---- clamp faces ----
print("-- clamps (default)")
yk_all = yoke + upright
ov = max(vol(place(yk_all, Rz(a)), base) for a in (0, 37, 90, 200))
ca = contact(yoke, base, [0, 0, -1])
check(ov < 0.5 and ca > 3000, f"azimuth: yoke sits flat on the base and turns without overlap ({ov:.2f} mm³); clamp face contact {ca/100:.0f} cm²")
ce = [contact(at_el(cheek0, th), upright, [1, 0, 0]) for th in ELS]
check(min(ce) > 2500, f"elevation: the cheek sits flat on the upright; clamp face contact {min(ce)/100:.0f}–{max(ce)/100:.0f} cm² over the range")
v = [vol(az_bolt, base), vol(az_bolt + az_head, yk_all), vol(el_wing, upright)] + \
    [vol(at_el(el_bolt0, th), upright) + vol(at_el(el_bolt0, th), at_el(cheek0, th)) for th in ELS]
check(max(v) < 0.5, f"bolts and heads sit in their holes and pockets at every angle (max overlap {max(v):.2f} mm³)")

# ---- elevation travel ----
def travel(fixed, moving, label):
    hits = [th for th in np.arange(-40, 130.1, 2.5) if vol(at_el(moving, th), fixed) > 0.5]
    inside = [h for h in hits if EL_RANGE[0] <= h <= EL_RANGE[1]]
    lo = max([h for h in hits if h < 0], default=None); hi = min([h for h in hits if h > 0], default=None)
    check(not inside, f"{label}: cradle tilts from {EL_RANGE[0]}° to {EL_RANGE[1]}° clear of the yoke and hardware (first contact {lo}° / {hi}°) {inside[:4]}")
fixed = yoke + upright + az_head + el_wing
travel(fixed, cradle0 + cheek0 + ch_scr + hub_bolts, "default")
dh = [th for th in np.arange(EL_RANGE[0], EL_RANGE[1] + 0.1, 2.5) if vol(at_el(dish0, th), yk_all + base) > 0.5]
check(not dh, f"dish envelope clears the yoke, upright and base from {EL_RANGE[0]}° to {EL_RANGE[1]}° {dh[:4]}")
dc = [th for th in np.arange(CLIP_RANGE[0], CLIP_RANGE[1] + 0.1, 2.5) if vol(at_el(dishc, th), yk_all + base) > 0.5]
check(not dc, f"with seam clips: dish clears the yoke, upright and base from {CLIP_RANGE[0]}° to {CLIP_RANGE[1]}° {dc[:4]}")

# ---- azimuth sweep ----
bad = []
for th in sorted({EL_RANGE[0], CLIP_RANGE[0], 0, 45, 90}):
    turning = yk_all + az_head + el_wing + up_scr + at_el(cradle0 + cheek0 + el_bolt0 + ch_scr + hub_bolts, th)
    for A in range(15, 360, 15):
        v = vol(place(turning + at_el(dish0, th), Rz(-A)), base)
        if v > 0.5: bad.append((th, A, round(v, 1)))
        if th >= CLIP_RANGE[0]:
            v = vol(place(turning + at_el(dishc, th), Rz(-A)), base)
            if v > 0.5: bad.append(('clips', th, A, round(v, 1)))
check(not bad, f"azimuth sweep 15–345° at el {EL_RANGE[0]}/0/45/90: everything that turns clears the base, with clips from {CLIP_RANGE[0]}° {bad[:6]}")
low = dish_mesh.copy().apply_transform(T([0, 0, Z_el]) @ Rx(EL_RANGE[0]) @ T([0, vertex_u, 0]) @ Rx(-90)).bounds[0][2]
print(f"     dish rim reaches {-low:.0f} mm below the base bottom ({yoke_z - low:.0f} mm below the yoke without the base) at {EL_RANGE[0]}°")

# ---- tool access: hex key straight down onto the azimuth bolt ----
key = place(cyl(3.5, 300), T([0, 0, top + 10 + 150]))
reach = [th for th in range(-10, 101, 10) if vol(key, yk_all + at_el(cradle0 + cheek0, th) + at_el(dish0, th)) < 0.5]
check(reach and min(reach) <= 0, f"hex key reaches the azimuth bolt straight down at el {min(reach)}° to {max(reach)}°")

# ---- hub interface ----
cr = trimesh.load(f"{D}/simple-cradle.stl").apply_transform(np.linalg.inv(np.array(INFO["M_cradle"])))
ck = trimesh.load(f"{D}/simple-cheek.stl").apply_transform(np.linalg.inv(np.array(INFO["M_cheek"])))
crck = trimesh.util.concatenate([cr, ck])
def open_along(mesh, pts, d): return ~mesh.ray.intersects_any(np.array(pts, float), np.tile(d, (len(pts), 1)))
ph = [[30 * math.cos(math.radians(a)), L - 30, 30 * math.sin(math.radians(a))] for a in (45, 135, 225, 315)] + [[0, L - 30, 0], [14, L - 30, 0]]
check(open_along(crck, ph, [0, 1, 0]).all(), "cradle + cheek: 4 x M4 on 60 BCD and Ø34 port open, with straight access from behind")
ang = np.radians(np.arange(0, 360, 2))
# root bolts: M4 washer (Ø9) + nut on the hub's rear face at r 52.5, up to 4.5 mm proud
pr = np.array([[r * np.cos(a), L - y, r * np.sin(a)] for a in ang for r in (48.2, 52.5, 56.8) for y in (0.5, 2.5, 4.5)])
check(not cr.contains(pr).any() and not ck.contains(pr).any(), "cradle + cheek: root nut and washer band (r 48-57, 4.5 mm behind the hub) clear")
pts = np.array([[ch_x, L + 0.01, z] for z in INFO['CH_SCREWS']])
check(not cr.contains(pts).any() and all(math.hypot(ch_x, z) + cb4_d / 2 < 47 - 0.6 and math.hypot(ch_x, z) - cb4_d / 2 > 17 + 1 and
          min(math.hypot(ch_x - 30 * math.cos(math.radians(a)), z - 30 * math.sin(math.radians(a))) for a in (45, 135, 225, 315)) - cb4_d / 2 - 2.25 > 2
          for z in INFO['CH_SCREWS']),
      "cradle: joint counterbores on the hub face sit between the port, the hub bolts and the rim (≥ 1 mm / 2 mm / 0.6 mm), heads below the face")

# ---- arc lock ----
print("-- arc lock")
EL_STEP = np.arange(EL_RANGE[0], EL_RANGE[1] + 0.01, 2.5)
v = [vol(at_el(arc_shaft, th), upright_a) for th in EL_STEP]
check(max(v) < 0.5, f"M6 bolt (r {arc_r}, {arc_w} mm slot) runs clear through the upright's arc slot from {EL_RANGE[0]}° to {EL_RANGE[1]}° (max {max(v):.2f} mm³)")
v = [vol(arc_head + arc_shaft, cheek0_a), vol(arc_hw0, cradle0)]
check(max(v) < 0.5, f"M6 head sits in its pocket in the cheek, shank through its hole ({max(v):.2f} mm³)")
v = [vol(at_el(arc_out, th), upright_a + el_wing + az_head) + vol(at_el(arc_hw0, th), at_el(el_bolt0, th)) for th in EL_STEP]
check(max(v) < 0.5, f"M6 washer and nyloc slide on the upright's outer face clear of the M8 fender washer and wing nut (Ø39 swept) at every angle (max {max(v):.2f} mm³)")
wash_out = place(along_x(cone(6, 6, 0.3), up_out - 0.4), T([0, ay, az_]))
wash_ck = place(along_x(cone(6, 6, 0.3), cr_out - 0.4), T([0, ay, az_]))
wash_up = place(along_x(cone(6, 6, 0.3), up_in + 0.1), T([0, ay, az_]))
v = [min(vol(at_el(wash_out, th), upright), vol(at_el(wash_up, th), upright)) / wash_out.volume() for th in EL_STEP] + [vol(wash_ck, cheek0) / wash_ck.volume()]
check(min(v) > 0.999, f"arc bolt and its Ø12 washer stay inside both clamp faces and on the upright's outer face at every angle ({100*min(v):.1f}% backed)")
travel(yoke + upright_a + az_head + el_wing, cradle0 + cheek0_a + ch_scr + hub_bolts + arc_hw0, "arc lock")
ce = [contact(at_el(cheek0_a, th), upright_a, [1, 0, 0]) for th in ELS]
check(min(ce) > 2000, f"arc lock: clamp face contact {min(ce)/100:.0f}–{max(ce)/100:.0f} cm² over the range")

# ---- no base: stand holes ----
print("-- no base")
v = [vol(st_scr, yoke_s), vol(st_scr, upright), vol(st_scr, up_scr)]
check(max(v) < 0.5, f"4 x M5 socket heads sit below the yoke plate's top in their counterbores, clear of the upright and joint screws ({max(v):.2f} mm³)")
drv = union([place(cyl(5, 300), T([x, y, top + 150.2])) for x, y in INFO['STAND']])
d0 = vol(drv, upright + yoke_s); d1 = [th for th in (EL_RANGE[0], 0, 30, 60, 90) if vol(drv, upright + yoke_s + at_el(cradle0 + cheek0 + dish0, th)) < 0.5]
check(d0 < 0.5, f"stand screws: a Ø10 driver reaches every head straight down with the upright fitted ({d0:.2f} mm³); with the dish fitted at el {d1}")
travel(yoke_s + upright + el_wing, cradle0 + cheek0 + ch_scr + hub_bolts, "no base")
dh = [th for th in np.arange(CLIP_RANGE[0], EL_RANGE[1] + 0.1, 2.5) if vol(at_el(dishc, th), yoke_s + upright) > 0.5]
check(not dh, f"no base: dish (with clips) clears the yoke and upright {dh[:4]}")
ca = contact(yoke_s, place(cyl(70, 2), T([0, 0, yoke_z - 1])), [0, 0, -1])
check(ca > 8000, f"no base: yoke bottom bears {ca/100:.0f} cm² on a flat stand")

# ---- both sides: a second, mirrored upright and cheek ----
print("-- both sides")
MX = np.diag([-1.0, 1, 1, 1]); mir = lambda Mn: place(Mn, MX)
yoke_d, yoke_ds, cradle_d = W("yoke-dual", "M_yoke"), W("yoke-dual-stand", "M_yoke"), W("cradle-dual", "M_cradle")
upright_l, cheek_l = W("upright-left", "M_upright_left"), W("cheek-left", "M_cheek_left")
diff = lambda a, b: (a - b).volume() + (b - a).volume()
v = [diff(upright_l, mir(upright)), diff(cheek_l, mir(cheek0))]
check(max(v) < 0.5, f"left upright and cheek are mirror images of the right ones (difference {max(v):.2f} mm³)")
for nm, up, ckm, us, cs, dn in (("right", upright, cheek0, up_scr, ch_scr, [1, 0, 0]), ("left", upright_l, cheek_l, mir(up_scr), mir(ch_scr), [-1, 0, 0])):
    cu, cc = contact(up, yoke_d, [0, 0, -1]), contact(ckm, cradle_d, [0, 1, 0])
    ov = vol(up, yoke_d) + vol(ckm, cradle_d) + max(vol(us, yoke_d), vol(us, up), vol(cs, cradle_d), vol(cs, ckm))
    check(cu > 1500 and cc > 600 and ov < 0.5, f"{nm} arm: upright flush on the dual yoke ({cu/100:.1f} cm²), cheek flush on the dual cradle ({cc/100:.1f} cm²), screws seated (overlap {ov:.2f} mm³)")
    ce = [contact(at_el(ckm, th), up, dn) for th in ELS]
    check(min(ce) > 2500, f"{nm} clamp: contact {min(ce)/100:.0f}–{max(ce)/100:.0f} cm² over the range")
wings = el_wing + mir(el_wing); bolts0 = el_bolt0 + mir(el_bolt0)
v = [vol(at_el(bolts0, th), upright + upright_l) + vol(at_el(bolts0, th), at_el(cheek0 + cheek_l, th)) for th in ELS] + [vol(wings, upright + upright_l)]
check(max(v) < 0.5, f"both elevation bolts sit in their holes and pockets, heads inside, wing nuts outside, at every angle (max {max(v):.2f} mm³)")
yk_d = yoke_d + upright + upright_l
travel(yk_d + az_head + wings, cradle_d + cheek0 + cheek_l + ch_scr + mir(ch_scr) + hub_bolts, "both sides")
travel(yoke_d + upright_a + upright_l + az_head + wings, cradle_d + cheek0_a + cheek_l + ch_scr + mir(ch_scr) + hub_bolts + arc_hw0, "both sides, arc lock on the right")
dh = [th for th in np.arange(EL_RANGE[0], EL_RANGE[1] + 0.1, 2.5) if vol(at_el(dish0, th), yk_d + base) > 0.5]
dc = [th for th in np.arange(CLIP_RANGE[0], CLIP_RANGE[1] + 0.1, 2.5) if vol(at_el(dishc, th), yk_d + base) > 0.5]
check(not dh and not dc, f"both sides: the dish clears both uprights, the yoke and the base from {EL_RANGE[0]}° ({CLIP_RANGE[0]}° with clips) to {EL_RANGE[1]}° {dh[:4]} {dc[:4]}")
bad = []
for th in sorted({EL_RANGE[0], CLIP_RANGE[0], 0, 45, 90}):
    turning = yk_d + az_head + wings + up_scr + mir(up_scr) + at_el(cradle_d + cheek0 + cheek_l + bolts0 + ch_scr + mir(ch_scr) + hub_bolts, th)
    for A in range(15, 360, 30):
        if vol(place(turning + at_el(dish0 if th < CLIP_RANGE[0] else dishc, th), Rz(-A)), base) > 0.5: bad.append((th, A))
check(not bad, f"both sides: everything that turns clears the base all the way round {bad[:6]}")
ov = max(vol(place(yk_d, Rz(a)), base) for a in (0, 37, 90, 200)); ca = contact(yoke_d, base, [0, 0, -1])
check(ov < 0.5 and ca > 3000, f"both sides: the dual yoke turns on the base without overlap ({ov:.2f} mm³); azimuth contact {ca/100:.0f} cm²")
reach = [th for th in range(-10, 101, 10) if vol(key, yk_d + at_el(cradle_d + cheek0 + cheek_l, th) + at_el(dish0, th)) < 0.5]
check(reach and min(reach) <= 0, f"both sides: hex key reaches the azimuth bolt straight down at el {min(reach)}° to {max(reach)}°")
crd = trimesh.load(f"{D}/simple-cradle-dual.stl").apply_transform(np.linalg.inv(np.array(INFO["M_cradle"])))
ckl = trimesh.load(f"{D}/simple-cheek-left.stl").apply_transform(np.linalg.inv(np.array(INFO["M_cheek_left"])))
both_c = trimesh.util.concatenate([crd, ck, ckl])
check(open_along(both_c, ph, [0, 1, 0]).all() and not any(m_.contains(pr).any() for m_ in (crd, ck, ckl)),
      "both sides: 4 x M4 on 60 BCD and Ø34 port open from behind; root nut and washer band clear")
st_d = union([place(capscrew(30, 4.25, 5.0, 2.5), T([x, y, top]) @ Rx(180)) for x, y in INFO['STAND_DUAL']])
v = [vol(st_d, yoke_ds), vol(st_d, upright + upright_l), vol(st_d, up_scr + mir(up_scr))]
drv = union([place(cyl(5, 300), T([x, y, top + 150.2])) for x, y in INFO['STAND_DUAL']])
check(max(v) < 0.5 and vol(drv, upright + upright_l + yoke_ds) < 0.5, f"both sides, no base: 4 x M5 socket heads below the top in their counterbores, clear of both uprights, reached straight down ({max(v):.2f} mm³)")

print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
