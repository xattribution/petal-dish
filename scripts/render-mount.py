"""Render the simple mount's documentation images with OpenSCAD (uses xvfb-run when present, for headless use).
Usage: python3 scripts/render-mount.py [outdir]     (default docs/)
Writes simple-mount.png (assembly with the dish), simple-mount-clamps.png (elevation clamp and arc lock),
simple-mount-both.png (the elevation clamp on both sides),
simple-mount-print.png (all parts in print orientation), simple-mount-joints.png (the bolted joints, exploded) and
simple-mount-tripod.png (the tripod base from the app's generator with its default 20 mm legs, from below).
"""
import sys, pathlib, shutil, subprocess, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAD = ROOT / 'cad/simple-mount.scad'
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'docs'

def render(name, camera, defs=(), size=(1400, 1100), src=SCAD):
    """camera: eye x,y,z, center x,y,z"""
    args = (['xvfb-run', '-a'] if shutil.which('xvfb-run') else []) + ['openscad', '-o', str(OUT / name), f'--imgsize={size[0]},{size[1]}',
            '--colorscheme=Tomorrow', '--projection=p', f'--camera={camera}']
    for d in defs: args += ['-D', d]
    subprocess.run(args + [str(src)], check=True, capture_output=True)
    print('rendered', OUT / name, flush=True)

PRINT_LAYOUT = f'''include <{SCAD}>
translate([-300, 0, 0]) color("slategray") multmatrix(M_base) base();
translate([-150, 0, 0]) color("steelblue") multmatrix(M_yoke) yoke();
translate([-10, -20, 0]) color("cornflowerblue") multmatrix(M_upright) upright();
translate([135, 0, 0]) color("orange") multmatrix(M_cradle) cradle();
translate([230, 10, 0]) color("gold") multmatrix(M_cheek) cheek();
'''

EXPLODED = f'''include <{SCAD}>
color("slategray") translate([0,0,-45]) base();
color("steelblue") yoke();
for (s=up_screws) translate([s[0], s[1], yoke_z - 35]) capscrew(up_screw_len);
color("cornflowerblue") translate([0,0,40]) upright();
translate([-45, 0, Z_el + 40]) rotate([el,0,0]) {{
  color("orange") translate([0, 45, 0]) cradle();
  for (z=ch_screws) translate([ch_x, L + 80, z]) rotate([90,0,0]) capscrew(ch_screw_len);
  color("gold") cheek();
}}
'''

if __name__ == '__main__':
    OUT.mkdir(exist_ok=True)
    render('simple-mount.png', '520,-330,330,10,60,60', ['el=20', 'arc_lock=true'])
    render('simple-mount-clamps.png', '330,-220,210,30,10,75', ['el=20', 'show_dish=false', 'arc_lock=true'])
    render('simple-mount-both.png', '300,-330,250,0,20,75', ['el=20', 'show_dish=false', 'sides="both"'])
    with tempfile.TemporaryDirectory() as d:
        # Tripod base: the app adds the leg sockets in JavaScript, so export its model-frame mesh and three leg stubs.
        js = f"""import('./dist/geometry.js').then(async g=>{{const fs=await import('node:fs'),{{binarySTL}}=await import('./dist/mesh.js'),{{apply}}=await import('./dist/scene.js'),{{solidScope,cylinder}}=await import('./dist/solid.js');
const m=g.build({{mountMode:1,mountBase:2}}),b=m.parts.find(p=>p.id==='mount-base'),t=m.mount.tripod;
fs.writeFileSync('{d}/base.stl',Buffer.from(binarySTL({{v:b.mesh.v.map(q=>apply(b.mesh.toModel,q)),f:b.mesh.f}})));
solidScope(()=>{{let s=null;for(const l of t.legs){{const c=cylinder(l.boreEnd,l.boreEnd.map((v,k)=>v+l.axis[k]*(t.engagement+90)),t.d/2,48);s=s?s.union(c):c;}}fs.writeFileSync('{d}/legs.stl',Buffer.from(binarySTL(s.mesh())));}});}});"""
        subprocess.run(['node', '-e', js], cwd=ROOT, check=True)
        t = pathlib.Path(d) / 'tripod.scad'
        t.write_text(f'color("slategray") import("{d}/base.stl");\ncolor("silver") import("{d}/legs.stl");\n')
        render('simple-mount-tripod.png', '400,-440,-150,0,0,-55', [], (1300, 1000), t)
        p = pathlib.Path(d) / 'layout.scad'
        p.write_text(PRINT_LAYOUT); render('simple-mount-print.png', '-45,-640,560,-45,10,0', ['part="none"', 'arc_lock=true'], (1600, 800), p)
        p.write_text(EXPLODED); render('simple-mount-joints.png', '-430,-420,300,20,40,60', ['part="none"', 'el=20', 'arc_lock=true'], (1400, 1100), p)
