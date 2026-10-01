// PETAL simple alt-az mount. Millimeters. Three printed parts, one clamp per axis.
//   base    - flat disc on your stand; captures the azimuth nut, carries the azimuth scale on its rim
//   yoke    - turntable on the base (azimuth) with one solid upright on the +X side
//   cradle  - hub plate with one cheek that clamps flat against the inside of the upright (elevation)
// Each axis: two flat faces, one M8 bolt. Azimuth: hex key from above. Elevation: one wing nut.
// The upright and the cheek are straight where they clamp and flare into their plates; no supports needed.
// Frame: Z up, +Y = boresight at az 0 / el 0, elevation axis along X. Base bottom at Z 0.

part = "assembly";  // [assembly,base,yoke,cradle]
printing = false;
dish_d = 400; dish_fd = 0.42;
el = 30; az = 0; show_dish = true;
$fn = 96;
hfn = 48;
efoot = 0.6;            // first-layer chamfer
chamf = 1.5;            // top-edge chamfer on plates

// ---------- layout ----------
disc_R = 58;
base_t = 14; yoke_t = 10;
yoke_z = base_t;                    // azimuth clamp face
H = 70;                             // elevation axis above the yoke plate
Z_el = yoke_z + yoke_t + H;
cap_r = 35;                         // clamp faces: round, r 35 around the elevation axis
cr_in = 26; cr_out = 40;            // cradle cheek x 26..40 (clear of the M4 heads, inside the plate)
up_in = 40; up_out = 60;            // yoke upright x 40..60; clamp face at x = 40
up_straight = Z_el - cap_r - 3;     // upright is straight above this (the cheek sweeps r <= 35), flared below
up_foot = [32, 66, 44];             // upright foot on the yoke plate: x 32..66, y ±44 (flares 8 mm on every side)
lobe_x = 72; lobe_y = 48;           // yoke plate extends past the disc to carry the upright
L = 75;                             // hub rear face ahead of the elevation axis
plate_t = 12; plate_r = 47; bcd_r = 30; port_d = 34; m4 = 4.5;
cr_mid = 45; cr_mid_z = 24;         // cradle cheek is straight behind y 45, flared in front of it
// cheek foot on the plate: inside r 44.5 (back chamfer), clear of the M4 heads (r 30 at 45°) and the port
cr_foot = [[20, -6], [20, 6], [28, 26], [36, 26], [43, 10], [43, -10], [36, -26], [28, -26]];
m8 = 8.5; m8_af = 13.3; m8_nut = 7; m8_head = 5.5;

// ---------- dish ----------
focal = dish_d*dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);

// ---------- helpers ----------
module tdrop(d, up) union() { circle(d=d, $fn=hfn); rotate(up) polygon([[d/2*cos(45), -d/2*sin(45)], [d/sqrt(2), 0], [d/2*cos(45), d/2*sin(45)]]); }
module hexroof(af, up) rotate(up) union() { R = af/sqrt(3); rotate(30) circle(r=R, $fn=6); polygon([[af/2, -R/2], [af/2 + R/2, 0], [af/2, R/2]]); }
// slab parallel to the YZ plane from x0 to x1 with a 2D profile (2D x -> Y, 2D y -> Z)
module yz(x0, x1) multmatrix([[0,0,1,x0],[1,0,0,0],[0,1,0,0],[0,0,0,1]]) linear_extrude(x1 - x0) children();
// plate from a 2D outline: first-layer chamfer on the bed side, 45° chamfer on the other
module plate(t, bed_bottom=true) hull() {
  translate([0,0,bed_bottom ? 0 : t - efoot]) linear_extrude(efoot) offset(delta=-efoot) children();
  translate([0,0,bed_bottom ? efoot : chamf]) linear_extrude(t - efoot - chamf) children();
  translate([0,0,bed_bottom ? t - chamf : 0]) linear_extrude(chamf) offset(delta=-chamf) children();
}
module yoke2d() hull() { circle(r=disc_R, $fn=180); translate([0, -lobe_y]) square([lobe_x, 2*lobe_y]); }

// ---------- base (modeled upright; prints with its top face on the bed) ----------
module base() difference() {
  plate(base_t, bed_bottom=false) circle(r=disc_R, $fn=180);
  translate([0,0,-1]) rotate(30) cylinder(r=m8_af/sqrt(3), h=m8_nut + 1.5, $fn=6);   // azimuth nut
  translate([0,0,-1]) cylinder(d=m8, h=base_t + 2, $fn=hfn);
  for (a=[45:90:359]) rotate(a) translate([44, 0, 0]) {                         // 4 x M5 flat-head
    translate([0,0,-1]) cylinder(d=5.5, h=base_t + 2, $fn=hfn);
    translate([0,0,base_t - 3.05]) cylinder(d1=5.5, d2=5.5 + 2*4.05, h=4.05, $fn=hfn);
  }
  // azimuth scale: grooves down the rim every 5°, wide every 10°, read at the yoke's rear groove
  for (a=[0:5:355]) rotate(270 + a) translate([disc_R - 1, a % 10 == 0 ? -0.6 : -0.3, -1])
    cube([3, a % 10 == 0 ? 1.2 : 0.6, base_t + 2]);
}

// ---------- yoke ----------
module upright() {
  hull() {
    yz(up_in, up_out) translate([0, Z_el]) circle(r=cap_r, $fn=180);
    translate([up_in, -cap_r, up_straight]) cube([up_out - up_in, 2*cap_r, 0.01]);
  }
  hull() {
    translate([up_in, -cap_r, up_straight]) cube([up_out - up_in, 2*cap_r, 0.01]);
    translate([up_foot[0], -up_foot[2], yoke_z + yoke_t - 0.01]) cube([up_foot[1] - up_foot[0], 2*up_foot[2], 0.01]);
  }
}
module yoke() difference() {
  union() {
    translate([0,0,yoke_z]) plate(yoke_t) yoke2d();
    upright();
  }
  translate([0,0,yoke_z - 1]) cylinder(d=m8, h=yoke_t + 2, $fn=hfn);              // azimuth bolt
  translate([up_in - 1, 0, Z_el]) rotate([0,90,0]) linear_extrude(up_out - up_in + 2) tdrop(m8, 180);   // elevation bolt
  translate([-0.5, -disc_R - 1, yoke_z - 1]) cube([1, 1.6, yoke_t + 2]);          // azimuth pointer groove (rear)
}

// ---------- cradle (elevation axis at the origin, el = 0; prints hub face down) ----------
module cradle() difference() {
  union() {
    translate([0, L, 0]) rotate([90,0,0]) plate(plate_t) circle(r=plate_r, $fn=180);
    hull() {
      yz(cr_in, cr_out) circle(r=cap_r, $fn=180);
      translate([cr_in, cr_mid, -cr_mid_z]) cube([cr_out - cr_in, 0.01, 2*cr_mid_z]);
    }
    hull() {
      translate([cr_in, cr_mid, -cr_mid_z]) cube([cr_out - cr_in, 0.01, 2*cr_mid_z]);
      translate([0, L - plate_t + 0.01, 0]) rotate([90,0,0]) linear_extrude(0.01) polygon(cr_foot);
    }
  }
  // elevation bolt: hex head in a pocket on the cheek's inner face (teardrops point to print-up, -Y)
  translate([cr_in - 1, 0, 0]) rotate([0,90,0]) linear_extrude(m8_head + 1.01) hexroof(m8_af, 270);
  translate([cr_in, 0, 0]) rotate([0,90,0]) linear_extrude(cr_out - cr_in + 1) tdrop(m8, 270);
  // hub interface: Ø34 port and 4 x M4 on a 60 mm circle
  translate([0, L - plate_t - 1, 0]) rotate([-90,0,0]) cylinder(d=port_d, h=plate_t + 2, $fn=hfn*2);
  for (a=[45:90:359]) translate([bcd_r*cos(a), L - plate_t - 1, bcd_r*sin(a)]) rotate([-90,0,0]) cylinder(d=m4, h=plate_t + 2, $fn=hfn);
}

// ---------- preview helpers ----------
module dish() if (show_dish) color([0.82,0.84,0.88]) translate([0, vertex_u, 0]) rotate([-90,0,0])
  rotate_extrude($fn=96) polygon(concat([[0, zAt(45) - 16], [60, zAt(45) - 16]],
    [for (r=[60:20:dish_d/2]) [r, zAt(r) - 17]], [for (r=[dish_d/2:-20:0]) [r, zAt(r)]]));
module wingnut() color("silver") { cylinder(d=24, h=2, $fn=48); translate([0,0,2]) cylinder(d=13, h=7, $fn=24);
  for (s=[-1,1]) translate([s*10, 0, 6]) cube([14, 3, 10], center=true); }

if (part == "assembly") {
  color("slategray") base();
  rotate([0,0,-az]) {
    color("steelblue") yoke();
    color("silver") translate([0,0,yoke_z + yoke_t]) { cylinder(d=16, h=1.6); translate([0,0,1.6]) cylinder(d=13, h=8, $fn=24); }
    translate([up_out, 0, Z_el]) rotate([0,90,0]) wingnut();
    translate([0,0,Z_el]) rotate([el,0,0]) {
      color("orange") cradle();
      color("silver") translate([cr_in + 0.2, 0, 0]) rotate([0,90,0]) rotate(30) cylinder(r=13/sqrt(3), h=5.3, $fn=6);
      dish();
    }
  }
}
if (part == "base") { if (printing) translate([0,0,base_t]) rotate([180,0,0]) base(); else base(); }
if (part == "yoke") { if (printing) translate([0,0,-yoke_z]) yoke(); else yoke(); }
if (part == "cradle") { if (printing) translate([0,0,L]) rotate([-90,0,0]) cradle(); else cradle(); }
