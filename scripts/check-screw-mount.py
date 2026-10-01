"""Checks for cad/screw-mount.scad (default parameters) using the exported print-orientation STLs.
Usage: python3 scripts/check-screw-mount.py <stl dir>      (exit status 1 on any failure)
Parts are placed with the same transforms the SCAD uses; a mismatch shows up as interference.
"""
import sys, math, numpy as np, trimesh, manifold3d as m3

D = sys.argv[1]
# ---- parameters (keep in sync with the SCAD) ----
L, Ha = 90, 100
ck_in, ck_t = 28, 12; ck_out = ck_in + ck_t
arm_in = ck_out + 0.5; arm_out = arm_in + 12
wheel_in = arm_out + 0.1; wheel_t = 10; wheel_out = wheel_in + wheel_t
R_el, R_az = 56, 70
fl_t, az_pad = 12, 8
base_bot, base_top = -34, -2
sc_p = 2.5
knob_t, col_t, thr_l = 8, 8, 40
blk_T, blk_L, blk_H, blk_pin, blk_nut = 14, 38, 22, 7, 25
travel, cav, wall_t = 9, 40, 8
pin_head_t, pin_shaft, pin_d = 5, 38, 9
tp_head = 4
slack = 0.7
dish_d, fd, el_min = 400, 0.42, -5
f = dish_d * fd
zAt = lambda r: r * r / (4 * f)
vertex_u = L + 16 - zAt(45)
rim_u = vertex_u + zAt(dish_d / 2)
axle_h = dish_d / 2 * math.cos(math.radians(el_min)) - rim_u * math.sin(math.radians(el_min)) + 15
riser_h = axle_h - (Ha - base_bot)
el_holes = [k * 10 for k in range(17, 28)]
az_holes = [j * 10 for j in range(36)]

ok = True
def check(c, msg):
    global ok
    print(("PASS " if c else "FAIL ") + msg); ok &= bool(c)
def T(v): M = np.eye(4); M[:3, 3] = v; return M
def R(axis, deg): return trimesh.transformations.rotation_matrix(math.radians(deg), axis)
Rx = lambda d: R([1, 0, 0], d); Ry = lambda d: R([0, 1, 0], d); Rz = lambda d: R([0, 0, 1], d)
MIRY = np.diag([1.0, -1, 1, 1])
UV = np.array([[0, 0, 1, 0], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1.0]])          # local x->Y, y->Z, z->X
EL = np.array([[0, 0, 1, 0], [1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 0, 1.0]])         # local x->Y, y->-Z, z->X
AZF = T([0, -R_az, fl_t + az_pad]) @ MIRY
ELF = T([wheel_out + 0.9, 0, Ha - R_el]) @ EL
def mani(m): return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))
def place(Mn, M): return Mn.transform(M[:3, :].tolist())
def vol(a, b): return (a ^ b).volume()
def U(*ms):
    out = ms[0]
    for m in ms[1:]: out = out + m
    return out

# ---- load / bed fit ----
names = ["base", "yoke", "cradle", "trunnion-pin", "index-wheel", "trunnion-nut", "keeper", "washer", "screw",
         "nut-block", "nut", "index-pin", "lock-pin", "riser", "arca-plate", "pipe-adapter", "coupon"]
P = {n: trimesh.load(f"{D}/{n}.stl") for n in names}
for n, m in P.items():
    e = m.extents
    check(m.is_watertight and m.body_count == 1 and abs(m.bounds[0][2]) < 1e-3 and e[0] <= 204 and e[1] <= 204 and e[2] <= 248,
          f"{n}: closed, on bed, fits 220x220x250 ({e[0]:.0f} x {e[1]:.0f} x {e[2]:.0f} mm)")
Mn = {n: mani(m) for n, m in P.items()}
base = place(Mn["base"], T([0, 0, base_bot]))
washer = place(Mn["washer"], T([0, 0, -2]))
keeper = place(Mn["keeper"], T([0, 0, 30]))
yoke = Mn["yoke"]
cradle_c = place(Mn["cradle"], np.linalg.inv(T([0, 0, L]) @ Rx(-90)))           # axle at origin
pinR = T([wheel_out + tp_head, 0, 0]) @ Ry(-90) @ Rz(30)
pinL = T([-(wheel_out + tp_head), 0, 0]) @ Ry(90) @ Rz(30)
wheel = T([wheel_in, 0, 0]) @ UV
nutR = T([ck_in, 0, 0]) @ Ry(-90)
nutL = T([-ck_in, 0, 0]) @ Ry(90)
def cradle_set(th, dish=False):
    M = T([0, 0, Ha]) @ Rx(th)
    parts = [place(cradle_c, M), place(Mn["trunnion-pin"], M @ pinR), place(Mn["trunnion-pin"], M @ pinL),
             place(Mn["index-wheel"], M @ wheel), place(Mn["trunnion-nut"], M @ nutR), place(Mn["trunnion-nut"], M @ nutL)]
    if dish: parts.append(place(Dm, M))
    return parts
def blk(s): return T([s, -blk_pin, 0])
def scr(rot): return T([-cav / 2 - wall_t - knob_t, blk_nut - blk_pin, blk_T / 2]) @ Ry(90) @ Rz(rot)
nut_in_blk = T([-5, blk_nut, blk_T / 2]) @ Ry(90) @ Rz(-30)

prof = [(0, zAt(45) - 16), (60, zAt(45) - 16)] + [(r, zAt(r) - 17) for r in range(60, 201, 10)] + [(r, zAt(r)) for r in range(200, -1, -10)]
Dm = mani(trimesh.creation.revolve(np.array(prof), sections=96).apply_transform(T([0, vertex_u, 0]) @ Rx(-90)))

# ---- coverage: every angle reachable with a hole inside screw travel and inside the seat's radial slack ----
def wrap(x): return (x + 180) % 360 - 180
def reach_el(th):
    # hole k sits at world angle h+th about the axle; the pin is fixed at world 270, offset s along Y
    c = [(h, R_el * math.sin(math.radians(wrap(h + th - 270))), R_el * (1 - math.cos(math.radians(wrap(h + th - 270))))) for h in el_holes]
    c = [x for x in c if abs(x[1]) <= travel and x[2] <= slack]
    return min(c, key=lambda x: abs(x[1])) if c else None
def reach_az(A):
    # in the yoke frame base hole j sits at compass c_j - A; the pin is at the rear (compass 180), offset s along X
    c = [(h, -R_az * math.sin(math.radians(wrap(h - A - 180))), R_az * (1 - math.cos(math.radians(wrap(h - A - 180))))) for h in az_holes]
    c = [x for x in c if abs(x[1]) <= travel and x[2] <= slack]
    return min(c, key=lambda x: abs(x[1])) if c else None
miss_el = [t / 4 for t in range(-20, 401) if reach_el(t / 4) is None]
check(not miss_el, f"elevation: every angle from -5° to 100° (0.25° steps) has a hole within ±{travel} mm travel and {slack} mm radial slack {miss_el[:5]}")
okay = [t / 10 for t in range(-300, 1300) if reach_el(t / 10)]
lo, hi = min(okay), max(okay)
miss_az = [a / 4 for a in range(0, 1440) if reach_az(a / 4) is None]
check(not miss_az, f"azimuth: every angle over 360° (0.25° steps) is reachable {miss_az[:5]}")
print(f"     elevation pinnable from {lo:.1f}° to {hi:.1f}°; {sc_p/R_el*180/math.pi:.2f}°/turn el, {sc_p/R_az*180/math.pi:.2f}°/turn az at center "
      f"(±{math.degrees(math.asin(travel/R_el)):.1f}° el / ±{math.degrees(math.asin(travel/R_az)):.1f}° az of travel per hole); lead angle "
      f"{math.degrees(math.atan(sc_p/(math.pi*11))):.1f}°")

# ---- free running: screws, blocks at both travel ends, nuts in blocks ----
for name, F in (("azimuth", AZF), ("elevation", ELF)):
    ov = max(vol(place(Mn["screw"], F @ scr(r)), yoke) for r in (0, 90, 180, 270))
    check(ov < 0.5, f"{name} screw turns freely in its housing (max overlap {ov:.2f} mm³)")
    ov = max(vol(place(Mn["nut-block"], F @ blk(s)), yoke) for s in (-travel, 0, travel))
    check(ov < 0.5, f"{name} nut block travels ±{travel} mm without touching the housing (max {ov:.2f} mm³)")
ov = vol(place(Mn["nut"], nut_in_blk), Mn["nut-block"])
check(ov < 0.5, f"nut fits its slot in the nut block ({ov:.2f} mm³)")
def phase_scan(a, b_fn, step=10):
    best = min((vol(a, b_fn(p)), p) for p in range(0, 360, step))
    best = min((vol(a, b_fn(p)), p) for p in np.arange(best[1] - step, best[1] + step + 0.1, 1))
    return best
v, p = phase_scan(place(Mn["nut"], T([0, 0, 20])), lambda r: place(Mn["screw"], Rz(r)))
check(v < 0.5, f"printed nut threads onto the drive screw (clear at {p:.0f}°, {v:.2f} mm³)")
v, p = phase_scan(keeper, lambda r: place(base, Rz(r)))
check(v < 0.5, f"keeper ring threads onto the base spindle (clear at {p:.0f}°, {v:.2f} mm³)")
v, p = phase_scan(place(Mn["trunnion-nut"], nutR), lambda r: place(Mn["trunnion-pin"], pinR @ Rz(r)))
check(v < 0.5, f"trunnion nut threads onto the trunnion pin (clear at {p:.0f}°, {v:.2f} mm³)")

# ---- trunnion stack and az bearing at rest ----
for th in (0, 45, 100):
    ov = vol(U(*cradle_set(th)), yoke + keeper)
    check(ov < 0.5, f"elevation stack (cradle, pins, wheel, nuts) clears the yoke at {th}° ({ov:.2f} mm³)")
ov = vol(yoke, base + washer)
check(ov < 0.5, f"yoke turns on the spindle and washer without overlap ({ov:.2f} mm³)")

# ---- pin seating: the seat is 0.1 mm over the pin's diameter, so the ~1:9 taper takes up the gap after ~0.9 mm of push.
#      Placed at the nominal depth the pin must be clear; pushing further it must bind (the taper is what holds), and it must
#      bind before the tip runs out of seat. Contact depth is found by bisection. ----
def el_pin(s, extra=0): return T([wheel_out + pin_head_t + pin_shaft - extra, s, Ha - R_el]) @ Ry(-90)
def az_pin(s, extra=0): return T([s, -R_az, base_top + pin_head_t + pin_shaft - extra]) @ Rx(180)
def contact(vol_at):
    a, b = 0.0, 3.0
    if vol_at(b) < 0.02: return None
    for _ in range(10):
        m = (a + b) / 2
        a, b = (m, b) if vol_at(m) < 0.02 else (a, m)
    return b
for target in (37, -4, 81.5):
    h, s, dev = reach_el(target)
    Mset = U(*cradle_set(target))
    fix = yoke + place(Mn["nut-block"], ELF @ blk(s))
    seated = vol(place(Mn["index-pin"], el_pin(s)), Mset + fix)
    c = contact(lambda e: vol(place(Mn["index-pin"], el_pin(s, e)), Mset))
    check(seated < 0.5 and c is not None and 0.3 < c < 2.0, f"elevation {target}°: hole {h}° at screw offset {s:+.2f} mm; pin clear at nominal depth ({seated:.2f} mm³), taper binds after {c if c is None else round(c, 2)} mm more push")
for A in (0, 123, 247.5):
    h, s, dev = reach_az(A)
    Y = Rz(-A)
    seated = vol(place(place(Mn["index-pin"], az_pin(s)), Y), base + place(yoke + place(Mn["nut-block"], AZF @ blk(s)), Y))
    c = contact(lambda e: vol(place(place(Mn["index-pin"], az_pin(s, e)), Y), base))
    check(seated < 0.5 and c is not None and 0.3 < c < 2.0, f"azimuth {A}°: hole {h}° at screw offset {s:+.2f} mm; pin clear at nominal depth ({seated:.2f} mm³), taper binds after {c if c is None else round(c, 2)} mm more push")

# ---- sweeps ----
fixed_el = yoke + keeper + base + place(Mn["nut-block"], ELF @ blk(0)) + place(Mn["screw"], ELF @ scr(0)) + \
           place(Mn["nut-block"], AZF @ blk(0)) + place(Mn["screw"], AZF @ scr(0))
bad = [th for th in np.arange(lo, hi + 0.01, 2) if vol(U(*cradle_set(th, True)), fixed_el) > 0.5]
check(not bad, f"elevation sweep {lo:.0f}…{hi:.0f}°: cradle, trunnion parts and dish clear the yoke, base, housings, blocks and screws {bad[:6]}")
riser = place(Mn["riser"], T([0, 0, base_bot - riser_h]))
fixed_az = base + washer + keeper + riser
bad = []
turning = yoke + place(Mn["nut-block"], ELF @ blk(0)) + place(Mn["screw"], ELF @ scr(0)) + \
          place(Mn["nut-block"], AZF @ blk(0)) + place(Mn["screw"], AZF @ scr(0))
for th in (lo, el_min, 0, 30, 90):
    mov = turning + U(*cradle_set(th, True))
    for A in range(0, 360, 15):
        v = vol(place(mov, Rz(-A)), base + riser)
        if v > 0.5: bad.append((th, A, round(v, 1)))
check(not bad, f"azimuth sweep 0–345° at el {lo:.0f}/{el_min}/0/30/90: nothing that turns touches the base or riser {bad[:6]}")
# rail lock: the long pin goes in from the base rim, through the base and across the mount's rail
lock = place(Mn["lock-pin"], T([math.sqrt(78**2 - (30 - 8)**2) + pin_head_t, -30, base_bot + 7]) @ Ry(-90))
lx = np.array(lock.bounding_box())
ov = vol(lock, base + riser)
check(ov < 0.5 and lx[0] < -16, f"rail lock pin slides through base and riser rail ({ov:.2f} mm³) and reaches x {lx[0]:.0f} (rail is |x| <= 15)")
bench = base_bot - riser_h
def low(th):
    m = trimesh.creation.revolve(np.array(prof), sections=96); m.apply_transform(T([0, 0, Ha]) @ Rx(th) @ T([0, vertex_u, 0]) @ Rx(-90))
    return m.bounds[0][2] - bench
check(low(el_min) >= 14.9 and low(lo) > 0, f"rim clears the bench by {low(el_min):.1f} mm at {el_min}° and {low(lo):.1f} mm at the lowest pinnable angle; riser {riser_h:.0f} mm, axle {axle_h:.0f} mm above bench")

# ---- hub interface ----
cr = trimesh.load(f"{D}/cradle.stl"); cr.apply_transform(np.linalg.inv(T([0, 0, L]) @ Rx(-90)))
def open_along(mesh, pts, d): return ~mesh.ray.intersects_any(np.array(pts, float), np.tile(d, (len(pts), 1)))
ph = [[30 * math.cos(math.radians(a)), L - 20, 30 * math.sin(math.radians(a))] for a in (45, 135, 225, 315)] + [[0, L - 20, 0], [14, L - 20, 0]]
check(open_along(cr, ph, [0, 1, 0]).all(), "cradle: 4 x M4 on 60 BCD and Ø34 port open")
ang = np.radians(np.arange(0, 360, 2))
pr = np.array([[r * np.cos(a), L - 2.5, r * np.sin(a)] for a in ang for r in (48.5, 52.5, 56.5)])
check(not cr.contains(pr).any(), "cradle: root-screw head band (r >= 48) clear")
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
