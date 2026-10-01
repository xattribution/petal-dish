// PETAL simple alt-az mount. Millimeters. Three printed parts, two disc-on-disc clamps.
//   base     - flat disc that screws to your stand; M8 nut captured underneath
//   rotator  - flat disc on the base (azimuth) carrying a faceted tower; its flat +X face is the elevation clamp face
//   cradle   - bolts to the PETAL hub; a faceted arm rises from the plate and clamps flat against the tower
// Each joint: two flat faces and one M8 bolt. Azimuth tightens with a hex key down the well in the tower;
// elevation with an M8 wing screw from outside the arm into a nut dropped into a slot in the tower top.
// Shapes are hulls of polygons, so every face is flat and every slope leans the printable way.
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
H = 73;                 // elevation axis above the rotator disc
Z_el = rot_z + rot_t + H;
up_x0 = 10; up_x1 = 30; up_a = 30;    // tower top: octagon (apothem 30) around the elevation axis, x 10..30
foot_a = 42.5;                         // tower foot: octagon (apothem 42.5) on the disc, cut flat at x = up_x1
arm_x0 = 30; arm_x1 = 42; arm_a = 26;  // cradle pivot: octagon (apothem 26), clamp face at x = arm_x0
chamf = 2;                             // 45° chamfer on the top edges of the discs and plate
L = 75;                 // hub rear face ahead of the elevation axis
plate_t = 12; plate_r = 45; plate_rb = 56;   // hub face r 45 (inside the root-screw heads), widening at ~43° to r 56 behind
bcd_r = 30; port_d = 34; m4 = 4.5;
// M8 hardware
m8 = 8.5; m8_af = 13.3; m8_nut = 7;
el_nut_x = 14;          // elevation nut sits at x 14..20.5, dropped in from the tower top
az_well = 18;           // hex-key well over the azimuth bolt head

// ---------- dish ----------
focal = dish_d*dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);

// ---------- helpers ----------
module tdrop(d, up) union() { circle(d=d, $fn=hfn); rotate(up) polygon([[d/2*cos(45), -d/2*sin(45)], [d/sqrt(2), 0], [d/2*cos(45), d/2*sin(45)]]); }
module hexroof(af, up) rotate(up) union() { R = af/sqrt(3); rotate(30) circle(r=R, $fn=6); polygon([[af/2, -R/2], [af/2 + R/2, 0], [af/2, R/2]]); }
module oct(a) rotate(22.5) circle(r=a/cos(22.5), $fn=8);   // flats at 0/90/180/270
// profile in the YZ plane (2D x -> Y, 2D y -> Z), extruded along +X from x0 by t
module yz(x0, t) multmatrix([[0,0,1,x0],[1,0,0,0],[0,1,0,0],[0,0,0,1]]) linear_extrude(t) children();
// disc with a first-layer chamfer on the bed side and a 45° chamfer on the far side
module chamfered_disc(r, t, bottom=true) {
  hull() {
    translate([0,0,bottom ? efoot : chamf]) cylinder(r=r, h=t - efoot - chamf, $fn=180);
    translate([0,0,bottom ? 0 : t - efoot]) cylinder(r=r - efoot, h=efoot, $fn=180);
    translate([0,0,bottom ? t - chamf : 0]) cylinder(r=r - chamf, h=chamf, $fn=180);
  }
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
module rotator() difference() {
  union() {
    translate([0,0,rot_z]) rotate(1) chamfered_disc(disc_R, rot_t);   // facet corners between the ticks
    hull() {
      yz(up_x0, up_x1 - up_x0) translate([0, Z_el]) oct(up_a);
      translate([0, 0, rot_z + rot_t - 0.01]) linear_extrude(0.01) intersection() {
        oct(foot_a);
        translate([-100, -100]) square([100 + up_x1, 200]);
      }
    }
  }
  // azimuth bolt: socket head sits on the disc at the bottom of a well
  translate([0,0,rot_z - 1]) cylinder(d=m8, h=rot_t + 2, $fn=hfn);
  translate([0,0,rot_z + rot_t]) cylinder(d=az_well, h=200, $fn=hfn);
  // elevation nut: drops into a slot from the top flat, bolt hole out through the clamp face
  translate([el_nut_x, -m8_af/2, Z_el - m8_af/sqrt(3) - 0.3]) cube([m8_nut - 0.5, m8_af, 100]);
  translate([el_nut_x - 4, 0, Z_el]) rotate([0,90,0]) linear_extrude(up_x1 - el_nut_x + 5) tdrop(m8, 180);   // runs 4 mm past the nut for bolt overshoot
  // azimuth scale: grooves down the rim, short every 5°, full height every 10°.
  // The groove at the rear of the base is the pointer.
  for (a=[0:5:355]) rotate(270 + a) translate([disc_R - 1, -0.4, a % 10 == 0 ? rot_z - 1 : rot_z + rot_t - 4])
    cube([3, 0.8, rot_t + 2]);
}

// ---------- cradle (elevation axis at the origin, el = 0) ----------
// Plate: a frustum, hub face on the bed. Arm: faceted bar from the pivot to a 45° skirt on the plate.
// Skirt foot stays clear of the M4 heads (r 30 at 45°) and the port, and inside the plate's back face.
skirt_y = L - plate_t - 12;      // where the arm's straight section meets the skirt
foot_pts = [[20, -9], [20, 9], [34, 33], [44, 33], [54, 12], [54, -12], [44, -33], [34, -33]];
module arm_section(y) translate([arm_x0, y, -arm_a]) cube([arm_x1 - arm_x0, 0.01, 2*arm_a]);
module cradle() difference() {
  union() {
    translate([0, L, 0]) rotate([90,0,0]) rotate_extrude($fn=180)
      polygon([[0, 0], [plate_r - efoot, 0], [plate_r, efoot], [plate_rb, plate_t], [0, plate_t]]);
    hull() { yz(arm_x0, arm_x1 - arm_x0) oct(arm_a); arm_section(skirt_y); }
    hull() {
      arm_section(skirt_y);
      translate([0, L - plate_t + 0.01, 0]) rotate([90,0,0]) linear_extrude(0.01) polygon(foot_pts);
    }
  }
  // elevation bolt along X (teardrop points to print-up, -Y)
  translate([arm_x0 - 1, 0, 0]) rotate([0,90,0]) linear_extrude(arm_x1 - arm_x0 + 2) tdrop(m8, 270);
  // hub interface: Ø34 port and 4 x M4 on a 60 mm circle
  translate([0, L - plate_t - 1, 0]) rotate([-90,0,0]) cylinder(d=port_d, h=plate_t + 2, $fn=hfn*2);
  for (a=[45:90:359]) translate([bcd_r*cos(a), L - plate_t - 1, bcd_r*sin(a)]) rotate([-90,0,0]) cylinder(d=m4, h=plate_t + 2, $fn=hfn);
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
    translate([el_nut_x, 0, Z_el]) rotate([0,90,0]) color("silver") rotate(30) cylinder(r=13/sqrt(3), h=6.5, $fn=6);
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
