"""Cross-sections of the Gregorian collector for three dish shapes (docs/gregorian-collector.png).
Geometry comes from dist/feed.js; rays are traced exactly: down to the parabola, through F1, onto the ellipse, to F2.
Usage: python3 scripts/render-collector.py
"""
import json, subprocess, pathlib, numpy as np, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
CASES = [{'fd': .3}, {'fd': .42}, {'fd': .6, 'collectorAngle': 20}]
JS = """import('./dist/geometry.js').then(async g=>{const {feedGeometry}=await import('./dist/feed.js');
const out=JSON.parse(process.argv[1]).map(c=>{const p={...g.defaults,feedMode:3,...c},f=feedGeometry(p,{n:6,rows:1});
return {p:{D:p.diameter,fd:p.fd,theta:p.collectorAngle},bowl:f.bowl,mast:f.mast,legs:f.legs.map(l=>[l.lower,l.upper]),rodD:f.rodDiameter};});
console.log(JSON.stringify(out));});"""
data = json.loads(subprocess.run(['node', '-e', JS, json.dumps(CASES)], cwd=ROOT, check=True, capture_output=True, text=True).stdout)

INK, DISH, BOWL, RAY_IN, RAY_OUT, SHADE = '#2b2f36', '#3f6fb5', '#2b2f36', '#d9822b', '#2f9e6e', '#e9edf2'
fig, axs = plt.subplots(1, len(data), figsize=(5.0 * len(data), 5.4), dpi=110)
for ax, d in zip(axs, data):
    D, fd = d['p']['D'], d['p']['fd']; R = D / 2; b = d['bowl']; f = b['primaryFocus']; F2 = b['insertFocus']
    prof = np.array(b['profile']); full = np.vstack([prof[::-1] * [-1, 1], prof])
    # bowl shadow cone below F1: dish rays inside the bowl radius never arrive
    ps = np.radians(b['shadowAngle']); z = np.linspace(0, f, 20)
    ax.fill_betweenx(z, -(f - z) * np.tan(ps), (f - z) * np.tan(ps), color=SHADE, lw=0, zorder=0)
    for k in np.linspace(-.97, .97, 12):
        x = k * R
        if abs(x) < b['outerRadius']: continue
        P = np.array([x, x * x / (4 * f)]); u = np.array([-x, f - P[1]]); u /= np.linalg.norm(u)
        psi = np.arccos(u[1]); t = b['b'] ** 2 / (b['a'] + b['c'] * np.cos(psi)); H = np.array([0, f]) + t * u
        ax.plot([x, x], [f * 1.45 + 20, P[1]], color=RAY_IN, lw=.7, alpha=.75)
        ax.plot([P[0], H[0]], [P[1], H[1]], color=RAY_IN, lw=.7, alpha=.75)
        ax.plot([H[0], 0], [H[1], F2], color=RAY_OUT, lw=.8)
    r = np.linspace(-R, R, 300); ax.plot(r, r * r / (4 * f), color=DISH, lw=2.4)
    ax.plot(full[:, 0], full[:, 1], color=BOWL, lw=3)
    m = d['mast']; ax.plot([0, 0], [m['footTop'], m['tubeTop']], color='#8a929c', lw=5, solid_capstyle='butt')
    cr = b['insertRadius']; ax.add_patch(plt.Rectangle((-cr, m['tubeTop']), 2 * cr, b['cupRim'] - m['tubeTop'], fc='#c9a46a', ec='none'))
    lo, hi = d['legs'][0]   # one rod, drawn in its own radial plane on the right
    ax.plot([np.hypot(lo[0], lo[1]), np.hypot(hi[0], hi[1])], [lo[2], hi[2]], color='#6b7380', lw=1.8)
    for y, name in ((f, 'F1'), (F2, 'F2')):
        ax.plot(0, y, 'o', color=INK, ms=4, zorder=5); ax.annotate(name, (0, y), (-b['radius'] - 10, y), fontsize=9, color=INK, va='center', ha='right')
    ax.set_aspect('equal'); ax.set_xlim(-R * 1.04, R * 1.04); ax.set_ylim(-8, f * 1.45 + 20); ax.axis('off')
    ax.set_title(f"f/D {fd}  ·  bowl Ø{2 * b['radius']:.0f} mm  ·  insert ±{d['p']['theta']}°  ·  {100 * b['blockage']:.1f}% blockage", fontsize=9.5, color=INK)
fig.suptitle('Gregorian collector, 400 mm dish: the bowl re-solves for each dish shape', fontsize=11, color=INK, y=.98)
fig.tight_layout(rect=(0, 0, 1, .95)); out = ROOT / 'docs/gregorian-collector.png'; fig.savefig(out, facecolor='white'); print('wrote', out)
