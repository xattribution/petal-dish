// PETAL simple alt-az mount. Millimeters. Three printed parts, two disc-on-disc clamps.
//   base     - flat disc that screws to your stand; M8 nut captured underneath
//   rotator  - flat disc on the base (azimuth) with a 45° wedge rising to the elevation clamp face
//   cradle   - bolts to the PETAL hub; its arm, braced by a wedge, clamps flat against the rotator (elevation)
// Each joint: two flat faces and one M8 bolt through the middle. Azimuth tightens with a hex key
// down the hole in the wedge; elevation with a wing nut on the outside of the arm.
// Loosen, aim, tighten. Friction between the faces holds the position.
// Print rules: nothing steeper than 45° from vertical, no bridges, engraving only on top faces.
// Frame: Z up, +Y = boresight at az 0 / el 0, elevation axis along X. Base bottom at Z 0.

part = "assembly";  // [assembly,base,rotator,cradle]
printing = false;
dish_d = 400; dish_fd = 0.42;
el = 30; az = 0; show_dish = true;
engrave = 0.6;
$fn = 96;
hfn = 48;
efoot = 0.6;            // edge chamfer on faces printed against the bed

// ---------- layout ----------
disc_R = 58;            // base and rotator discs
base_t = 14; rot_t = 10;
rot_z = base_t;         // rotator disc bottom = azimuth clamp face
H = 65;                 // elevation axis above the rotator disc
Z_el = rot_z + rot_t + H;
up_x0 = 12; up_x1 = 26; up_w = 24; up_R = 30;   // upright; elevation clamp face on its outer side (x = up_x1)
wedge_x = -40;          // rotator foot: x -40..26 inside r 46 (keeps the rim scale clear)
foot_r = 46;
arm_x0 = 26; arm_x1 = 38; arm_R = 26;            // cradle arm, clamp face on its inner side (x = arm_x0)
cw_y = 42;              // brace meets the arm at y >= 42 so it clears the rotator down to -17.5°
L = 75;                 // hub rear face ahead of the elevation axis
plate_t = 12; plate_r = 47; bcd_r = 30; port_d = 34; m4 = 4.5;
arm_zb = -20;           // arm foot on the plate: z -20..26 keeps it inside r 47
// M8 hardware
m8 = 8.5; m8_af = 13.3; m8_nut = 7; m8_head = 5.5;
az_well = 18;           // hex-key well over the azimuth bolt head

// ---------- dish ----------
focal = dish_d*dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);

// ---------- helpers ----------
module tdrop(d, up) union() { circle(d=d, $fn=hfn); rotate(up) polygon([[d/2*cos(45), -d/2*sin(45)], [d/sqrt(2), 0], [d/2*cos(45), d/2*sin(45)]]); }
module hexroof(af, up) rotate(up) union() { R = af/sqrt(3); rotate(30) circle(r=R, $fn=6); polygon([[af/2, -R/2], [af/2 + R/2, 0], [af/2, R/2]]); }
module lbl(t, sz) text(t, size=sz, font="DejaVu Sans:style=Bold", halign="center", valign="center");
// profile in the YZ plane (2D x -> Y, 2D y -> Z), extruded along +X from x0 by t
module yz(x0, t) multmatrix([[0,0,1,x0],[1,0,0,0],[0,1,0,0],[0,0,0,1]]) linear_extrude(t) children();
module chamfered_disc(r, t, bottom=true) {
  translate([0,0,bottom ? efoot : 0]) cylinder(r=r, h=t - efoot, $fn=180);
  translate([0,0,bottom ? 0 : t - efoot]) cylinder(r1=bottom ? r - efoot : r, r2=bottom ? r : r - efoot, h=efoot, $fn=180);
}

// ---------- base (modeled upright; prints top face down) ----------
module base() difference() {
  chamfered_disc(disc_R, base_t, bottom=false);   // top face is the clamp face, printed on the bed
  // M8 nut pocket in the underside, hole up through
  translate([0,0,-1]) rotate(30) cylinder(r=m8_af/sqrt(3), h=m8_nut + 1.5, $fn=6);   // nut rides up to z 7.5
  cylinder(d=m8, h=base_t + 1, $fn=hfn);
  // 4 x M5 flat-head, flush with the clamp face (fit them before the rotator goes on)
  for (a=[45:90:359]) rotate(a) translate([44, 0, 0]) {
    translate([0,0,-1]) cylinder(d=5.5, h=base_t + 2, $fn=hfn);
    translate([0,0,base_t - 3.05]) cylinder(d1=5.5, d2=5.5 + 2*4.05, h=4.05, $fn=hfn);   // 90° countersink, heads 0.25 below the face
  }
  // azimuth pointer: groove down the rear of the rim
  translate([-0.5, -disc_R - 1, -1]) cube([1, 1 + engrave, base_t + 2]);
}

// ---------- rotator ----------
module upright2d() hull() {
  translate([0, Z_el]) circle(r=up_R, $fn=180);
  translate([-up_w, rot_z + rot_t - 0.01]) square([2*up_w, 1]);
}
module rotator() difference() {
  union() {
    translate([0,0,rot_z]) rotate(1) chamfered_disc(disc_R, rot_t);   // facet corners between the 2° ticks
    // one solid: the upright's round top, braced down to a wide foot on the disc (sloped faces up)
    hull() {
      yz(up_x0, up_x1 - up_x0) translate([0, Z_el]) circle(r=up_R, $fn=180);
      translate([0, 0, rot_z + rot_t - 0.01]) linear_extrude(1) intersection() {
        circle(r=foot_r, $fn=180);
        translate([wedge_x, -100]) square([up_x1 - wedge_x, 200]);
      }
    }
  }
  // azimuth bolt: socket head sits on the disc at the bottom of a well through the wedge
  translate([0,0,rot_z - 1]) cylinder(d=m8, h=rot_t + 2, $fn=hfn);
  translate([0,0,rot_z + rot_t]) cylinder(d=az_well, h=200, $fn=hfn);
  // elevation bolt: hex head pocket inside the upright, reached by a bore from the wedge face
  translate([up_x0 + 1.5 - 100, 0, Z_el]) rotate([0,90,0]) linear_extrude(100) tdrop(16.5, 180);
  translate([up_x0 + 1.5 - 0.01, 0, Z_el]) rotate([0,90,0]) linear_extrude(m8_head + 0.5) hexroof(m8_af, 180);
  translate([up_x0, 0, Z_el]) rotate([0,90,0]) linear_extrude(up_x1 - up_x0 + 1) tdrop(m8, 180);
  // azimuth scale: ticks down the rim every 2°, labels every 30° on top.
  // Azimuth A sits at angle 270 + A, read at the groove on the rear of the base.
  for (a=[0:2:358]) rotate(270 + a) translate([disc_R - (a % 10 == 0 ? 1.2 : engrave), -(a % 10 == 0 ? 0.5 : 0.3), rot_z - 1])
    cube([2, a % 10 == 0 ? 1 : 0.6, rot_t + 2]);
  translate([0,0,rot_z + rot_t - engrave]) linear_extrude(engrave + 1)
    for (A=[0:30:330]) rotate(270 + A) translate([disc_R - 7, 0]) rotate(90) lbl(str(A), 4);
}

// ---------- cradle (elevation axis at the origin, el = 0) ----------
module cradle() difference() {
  union() {
    translate([0, L - plate_t, 0]) rotate([-90,0,0]) cylinder(r=plate_r, h=plate_t, $fn=180);
    // arm: clamp disc around the axis, running forward to the plate; flat top at z = arm_R
    yz(arm_x0, arm_x1 - arm_x0) hull() {
      circle(r=arm_R, $fn=180);
      translate([L - plate_t - 1, arm_zb]) square([1.01, arm_R - arm_zb]);
    }
    // brace: spreads over the back of the plate (inside r 45) and slopes in to the arm
    hull() {
      translate([0, L - plate_t, 0]) rotate([90,0,0]) linear_extrude(0.01) intersection() {
        circle(r=45, $fn=180);
        translate([-30, -100]) square([arm_x0 + 30, 200]);
      }
      translate([arm_x0 - 0.01, cw_y, arm_zb]) cube([0.01, L - plate_t - cw_y, arm_R - arm_zb]);
    }
  }
  // elevation bolt along X (teardrop points to print-up, -Y)
  translate([arm_x0 - 1, 0, 0]) rotate([0,90,0]) linear_extrude(arm_x1 - arm_x0 + 2) tdrop(m8, 270);
  // hub interface: Ø34 port and 4 x M4 on a 60 mm circle, all open straight through the wedge
  translate([0, 0, 0]) rotate([-90,0,0]) cylinder(d=port_d, h=L + 1, $fn=hfn*2);
  for (a=[45:90:359]) translate([bcd_r*cos(a), 0, bcd_r*sin(a)]) rotate([-90,0,0]) {
    cylinder(d=m4, h=L + 1, $fn=hfn);
    cylinder(d=9, h=L - plate_t, $fn=hfn);      // head and hex-key access
  }
  // hub face is printed on the bed: chamfer its edge
  translate([0, L + 0.01, 0]) rotate([90,0,0]) difference() {
    cylinder(r=plate_r + 1, h=efoot + 0.4, $fn=180);
    translate([0,0,-0.01]) cylinder(r1=plate_r - efoot, r2=plate_r + 0.4, h=efoot + 0.41, $fn=180);
  }
}

// ---------- dish envelope (preview only) ----------
module dish() if (show_dish) color([0.82,0.84,0.88]) translate([0, vertex_u, 0]) rotate([-90,0,0])
  rotate_extrude($fn=96) polygon(concat([[0, zAt(45) - 16], [60, zAt(45) - 16]],
    [for (r=[60:20:dish_d/2]) [r, zAt(r) - 17]], [for (r=[dish_d/2:-20:0]) [r, zAt(r)]]));
module bolt(len) color("silver") { cylinder(d=8, h=len, $fn=24); translate([0,0,-5.3]) cylinder(d=14.4, h=5.3, $fn=6); }
module wingnut() color("silver") { cylinder(d=24, h=2, $fn=48); translate([0,0,2]) cylinder(d=13, h=7, $fn=24);
  for (s=[-1,1]) translate([s*10, 0, 6]) cube([14, 3, 10], center=true); }

// ---------- output ----------
if (part == "assembly") {
  color("slategray") base();
  rotate([0,0,-az]) {
    color("steelblue") rotator();
    translate([0,0,rot_z + rot_t + 1.6]) rotate([180,0,0]) color("silver") { cylinder(d=8, h=25, $fn=24); translate([0,0,-8]) cylinder(d=13, h=8, $fn=24); }
    translate([up_x0 + 1.5 + m8_head, 0, Z_el]) rotate([0,90,0]) bolt(40);
    translate([0,0,Z_el]) rotate([el,0,0]) {
      color("orange") cradle();
      translate([arm_x1, 0, 0]) rotate([0,90,0]) wingnut();
      dish();
    }
  }
}
if (part == "base") { if (printing) translate([0,0,base_t]) rotate([180,0,0]) base(); else base(); }
if (part == "rotator") { if (printing) translate([0,0,-rot_z]) rotator(); else rotator(); }
if (part == "cradle") { if (printing) translate([0,0,L]) rotate([-90,0,0]) cradle(); else cradle(); }
