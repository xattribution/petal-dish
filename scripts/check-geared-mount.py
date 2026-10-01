"""Checks for cad/geared-mount.scad (default parameters) using the exported print-orientation STLs.
Usage: python3 scripts/check-geared-mount.py <stl dir>
Mirrors the SCAD placement math; a mismatch shows up as worm/housing or gear interference.
"""
import sys, math, numpy as np, trimesh, manifold3d as m3

D = sys.argv[1]
LEAD_EL = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
LEAD_AZ = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
# ---- parameters (keep in sync with the SCAD) ----
gm, gN = 2, 60
g_rp, w_r1 = 60, 8
w_p = math.pi * gm
lam = math.degrees(math.atan(w_p / (2 * math.pi * w_r1)))
g_a = g_rp + w_r1
L, band_top, Ha, base_t = 100, 24, 90, 10
Zax = band_top + Ha
psi_w = 230
ear_in, ear_t = 28, 12
el_lo, el_hi, el_min = -5, 100, -5
dish_d, fd = 400, 0.42
f = dish_d * fd
zAt = lambda r: r * r / (4 * f)
vertex_u = L + 16 - zAt(45)
rim_u = vertex_u + zAt(dish_d / 2)
axle_h = dish_d / 2 * math.cos(math.radians(el_min)) - rim_u * math.sin(math.radians(el_min)) + 15
stand_top = axle_h - Zax - base_t

ok = True
def check(c, msg):
    global ok
    print(("PASS " if c else "FAIL ") + msg); ok &= bool(c)
def T(v): M = np.eye(4); M[:3, 3] = v; return M
def R(axis, deg): return trimesh.transformations.rotation_matrix(math.radians(deg), axis)
Rx = lambda d: R([1, 0, 0], d); Ry = lambda d: R([0, 1, 0], d); Rz = lambda d: R([0, 0, 1], d)
def X(m, M): m = m.copy(); m.apply_transform(M); return m
def mani(m): return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))
def mt(M, A): return A.transform(np.asarray(M[:3, :], np.float32).T.tolist() if False else M[:3, :])
def place(Mn, M): return Mn.transform(M[:3, :].tolist())
def vol(a, b): return (a ^ b).volume()

cw, sw = math.cos(math.radians(psi_w)), math.sin(math.radians(psi_w))
B_el = np.array([[-1, 0, 0, 0], [0, -cw, -sw, 0], [0, -sw, cw, 0], [0, 0, 0, 1.0]])
F_el = T([ear_in + ear_t / 2, g_a * cw, Zax + g_a * sw]) @ B_el @ Ry(LEAD_EL * lam) @ T([0, 0, -42])
B_az = np.array([[0, 0, -1, 0], [0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 1.0]])
F_az = T([0, -g_a, 6]) @ B_az @ Ry(LEAD_AZ * lam) @ T([0, 0, -42])

# ---- load, bed fit ----
names = ["worm", "cradle", "head", "base", "stand", "pipe-adapter", "tripod-puck"]
P = {n: trimesh.load(f"{D}/{n}.stl") for n in names}
for n, m in P.items():
    e = m.extents
    check(m.is_watertight and m.body_count == 1 and abs(m.bounds[0][2]) < 1e-3 and e[0] <= 204 and e[1] <= 204 and e[2] <= 248,
          f"{n}: closed, on bed, fits 220x220x250 ({e[0]:.0f} x {e[1]:.0f} x {e[2]:.0f} mm, {m.volume/1000:.0f} cm3 solid)")
cradle = X(P["cradle"], np.linalg.inv(T([0, 0, L]) @ Rx(-90)))
head = P["head"]
base = X(P["base"], T([0, 0, -base_t]))
worm = P["worm"]
stand = X(P["stand"], T([0, 0, -base_t - stand_top]))
Wm, Cm, Hm, Bm, Sm = mani(worm), mani(cradle), mani(head), mani(base), mani(stand)
print(f"     lead angle {lam:.2f}°, {360/gN:.0f}° per knob turn; self-locks while lead < friction angle "
      f"(μ 0.25 at 48° pressure angle → {math.degrees(math.atan(0.25/math.cos(math.radians(48)))):.1f}°)")

# ---- worm inside its housing (any rotation) ----
wf = [vol(place(Wm, F_el @ Rz(p)), Hm) for p in (0, 73, 151, 229, 307)]
check(max(wf) < 0.5, f"elevation worm turns freely in the head housing (max overlap {max(wf):.2f} mm³)")
wa = [vol(place(Wm, F_az @ Rz(p)), Bm) for p in (0, 73, 151, 229, 307)]
check(max(wa) < 0.5, f"azimuth worm turns freely in the base housing (max overlap {max(wa):.2f} mm³)")

# ---- gear mesh: a clear phase exists, and ±60° off it the teeth collide (real engagement) ----
def mesh_scan(gear, Fw, label):
    best = min(((vol(place(Wm, Fw @ Rz(p)), gear), p) for p in range(0, 360, 6)))
    p0 = best[1]
    best = min(((vol(place(Wm, Fw @ Rz(p)), gear), p) for p in np.arange(p0 - 5, p0 + 5.1, 0.5)))
    v0, p0 = best
    lo = vol(place(Wm, Fw @ Rz(p0 - 60)), gear); hi = vol(place(Wm, Fw @ Rz(p0 + 60)), gear)
    return v0, p0, lo, hi
res = []
for th in (-8, -5, 0, 30, 60, 90, 100, 103):
    gear = place(Cm, T([0, 0, Zax]) @ Rx(th))
    res.append((th,) + mesh_scan(gear, F_el, "el"))
for th, v0, p0, lo, hi in res:
    print(f"     el {th:5.0f}°: clear phase {p0:6.1f}° (overlap {v0:.2f} mm³); ±60° off → {lo:.1f} / {hi:.1f} mm³")
check(all(r[1] < 0.5 and r[3] > 1 and r[4] > 1 for r in res), "elevation worm meshes the ear sector from -8° to 103° with real tooth engagement")
ph = [r[2] for r in res]
res_az = []
for A in (0, 90, 200, 333):
    gear = place(Hm, Rz(-A))
    res_az.append((A,) + mesh_scan(gear, F_az, "az"))
for A, v0, p0, lo, hi in res_az:
    print(f"     az {A:5.0f}°: clear phase {p0:6.1f}° (overlap {v0:.2f} mm³); ±60° off → {lo:.1f} / {hi:.1f} mm³")
check(all(r[1] < 0.5 and r[3] > 1 and r[4] > 1 for r in res_az), "azimuth worm meshes the head ring with real tooth engagement")

# ---- elevation travel: free between the stops, stops engage just outside ----
def hit(th): return vol(place(Cm, T([0, 0, Zax]) @ Rx(th)), Hm)
free = [th for th in np.arange(el_lo - 2.5, el_hi + 2.6, 2.5) if hit(th) > 0.5]
check(not free, f"cradle clears the head from {el_lo-2.5}° to {el_hi+2.5}° (hits at {free})")
def edge(a, b):  # a free, b hitting
    for _ in range(12):
        m = (a + b) / 2
        a, b = (m, b) if hit(m) < 0.5 else (a, m)
    return b
lo_stop, hi_stop = edge(el_lo - 2.5, el_lo - 12), edge(el_hi + 2.5, el_hi + 12)
check(el_lo - 6 < lo_stop < el_lo - 1 and el_hi + 1 < hi_stop < el_hi + 6,
      f"hard stops at {lo_stop:.1f}° and {hi_stop:.1f}°")
st = [(th,) + mesh_scan(place(Cm, T([0, 0, Zax]) @ Rx(th)), F_el, "el") for th in (lo_stop, hi_stop)]
check(all(r[1] < 0.5 and r[3] > 1 and r[4] > 1 for r in st), "worm still fully meshed at both stops: " +
      ", ".join(f"{r[0]:.1f}° overlap {r[1]:.2f} / engaged {min(r[3], r[4]):.0f} mm³" for r in st))

# ---- dish envelope ----
prof = [(0, zAt(45) - 16), (60, zAt(45) - 16)] + [(r, zAt(r) - 17) for r in range(60, 201, 10)] + [(r, zAt(r)) for r in range(200, -1, -10)]
dish = X(trimesh.creation.revolve(np.array(prof), sections=96), T([0, vertex_u, 0]) @ Rx(-90))
Dm = mani(dish)
bench = -base_t - stand_top
lows = {th: X(dish, T([0, 0, Zax]) @ Rx(th)).bounds[0][2] - bench for th in (el_min, lo_stop)}
check(lows[el_min] >= 14.9 and lows[lo_stop] > 0, f"rim clears the bench: {lows[el_min]:.1f} mm at {el_min}°, {lows[lo_stop]:.1f} mm at the stop; axle {axle_h:.0f} mm above bench")
dh = [th for th in np.arange(lo_stop, hi_stop, 5) if vol(place(Dm, T([0, 0, Zax]) @ Rx(th)), Hm + place(Wm, F_el)) > 0.5]
check(not dh, f"dish envelope clears head and elevation worm over the full travel ({dh})")

# ---- azimuth sweep: everything that turns clears everything fixed ----
fixed = Bm + Sm
head_up = Hm.trim_by_plane([0, 0, 1], 12.5)
bad = []
for th in (el_min, 0, 30, 90):
    mov_c = place(Cm, T([0, 0, Zax]) @ Rx(th)) + place(Dm, T([0, 0, Zax]) @ Rx(th))
    for A in range(0, 360, 15):
        Ra_ = Rz(-A)
        mov = place(mov_c, Ra_) + place(Wm, Ra_ @ F_el)
        v = vol(fixed, mov + place(Hm, Ra_)) + vol(place(Wm, F_az), mov + place(head_up, Ra_)) + vol(place(Dm, Ra_ @ T([0, 0, Zax]) @ Rx(th)), fixed)
        if v > 0.5: bad.append((th, A, round(v)))
check(not bad, f"azimuth sweep 0–345° at el {el_min}/0/30/90: no contact with base, azimuth worm or stand ({bad[:6]})")

# ---- hub interface ----
def open_along(mesh, pts, d): return ~mesh.ray.intersects_any(np.array(pts, float), np.tile(d, (len(pts), 1)))
ph_ = [[30 * math.cos(math.radians(a)), L - 20, 30 * math.sin(math.radians(a))] for a in (45, 135, 225, 315)] + [[0, L - 20, 0], [14, L - 20, 0]]
check(open_along(cradle, ph_, [0, 1, 0]).all(), "cradle: 4 x M4 on 60 BCD and Ø34 port open")
ang = np.radians(np.arange(0, 360, 2))
pr = np.array([[r * np.cos(a), L - 2.5, r * np.sin(a)] for a in ang for r in (48.5, 52.5, 56.5)])
check(not cradle.contains(pr).any(), "cradle: root-screw head band (r >= 48) clear")
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
