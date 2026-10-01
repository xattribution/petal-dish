// PETAL simple alt-az mount. Millimeters. Three printed parts, every joint flat-on-flat and clamped by a bolt.
//   base    - flat disc on your stand; captures the azimuth nut underneath
//   yoke    - flat disc turning on the base (azimuth), with two cheeks that flare into it
//   cradle  - the hub plate with two cheeks that sit outside the yoke cheeks (elevation)
// Symmetric fork: the dish sits between two clamped pairs of faces, so nothing is cantilevered.
// Every junction flares at a shallow angle on all sides; every face prints without supports.
// Frame: Z up, +Y = boresight at az 0 / el 0, elevation axis along X. Base bottom at Z 0.

part = "assembly";  // [assembly,base,yoke,cradle]
printing = false;
dish_d = 400; dish_fd = 0.42;
el = 30; az = 0; show_dish = true;
$fn = 96;
hfn = 48;
efoot = 0.6;            // first-layer chamfer
chamf = 1.5;            // top-edge chamfer on the discs and plate

// ---------- layout ----------
disc_R = 58;
base_t = 14; yoke_t = 10;
yoke_z = base_t;                    // azimuth clamp face
H = 70;                             // elevation axis above the yoke disc
Z_el = yoke_z + yoke_t + H;
cap_r = 27;                         // round clamp cap around the elevation axis (yoke and cradle alike)
yk_in = 18; yk_out = 30;            // yoke cheeks |x| 18..30 near the axis
side_clr = 0.2;                     // cradle slides over the yoke; the bolts pull this out when tightened
cr_in = yk_out + side_clr; cr_out = cr_in + 12;   // cradle cheeks outside the yoke cheeks
yk_mid = Z_el - 30;                 // yoke cheeks are straight above this, flared below it
yk_foot = [10, 38, 42];             // flared foot on the disc: x 10..38, y ±42
L = 75;                             // hub rear face ahead of the elevation axis
plate_t = 12; plate_r = 47; bcd_r = 30; port_d = 34; m4 = 4.5;
cr_mid = 45;                        // cradle cheeks are straight behind y 45, flared in front of it
cr_mid_z = 21;
// cradle cheek foot on the plate: inside r 45 (the back chamfer), clear of the M4 heads (r 30 at 45°) and the port
cr_foot = [[26, -25], [37, -25], [43, -12], [43, 12], [37, 25], [26, 25]];
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
// disc: first-layer chamfer on the bed side, 45° chamfer on the other
module disc(r, t, bed_bottom=true) rotate_extrude($fn=180) polygon(bed_bottom
  ? [[0, 0], [r - efoot, 0], [r, efoot], [r, t - chamf], [r - chamf, t], [0, t]]
  : [[0, 0], [r - chamf, 0], [r, chamf], [r, t - efoot], [r - efoot, t], [0, t]]);
module mirror_x() { children(); mirror([1,0,0]) children(); }

// ---------- base (modeled upright; prints with its top face on the bed) ----------
module base() difference() {
  disc(disc_R, base_t, bed_bottom=false);
  translate([0,0,-1]) rotate(30) cylinder(r=m8_af/sqrt(3), h=m8_nut + 1.5, $fn=6);   // azimuth nut
  translate([0,0,-1]) cylinder(d=m8, h=base_t + 2, $fn=hfn);
  for (a=[45:90:359]) rotate(a) translate([44, 0, 0]) {                         // 4 x M5 flat-head
    translate([0,0,-1]) cylinder(d=5.5, h=base_t + 2, $fn=hfn);
    translate([0,0,base_t - 3.05]) cylinder(d1=5.5, d2=5.5 + 2*4.05, h=4.05, $fn=hfn);
  }
  translate([-0.5, -disc_R - 1, -1]) cube([1, 1.6, base_t + 2]);                  // azimuth pointer groove
}

// ---------- yoke ----------
module yoke_cheek() {
  hull() {
    yz(yk_in, yk_out) translate([0, Z_el]) circle(r=cap_r, $fn=180);
    translate([yk_in, -cap_r, yk_mid]) cube([yk_out - yk_in, 2*cap_r, 0.01]);
  }
  hull() {
    translate([yk_in, -cap_r, yk_mid]) cube([yk_out - yk_in, 2*cap_r, 0.01]);
    translate([yk_foot[0], -yk_foot[2], yoke_z + yoke_t - 0.01]) cube([yk_foot[1] - yk_foot[0], 2*yk_foot[2], 0.01]);
  }
}
module yoke() difference() {
  union() {
    translate([0,0,yoke_z]) rotate(1) disc(disc_R, yoke_t);   // facet corners between the ticks
    mirror_x() yoke_cheek();
  }
  translate([0,0,yoke_z - 1]) cylinder(d=m8, h=yoke_t + 2, $fn=hfn);              // azimuth bolt
  mirror_x() {
    // elevation bolt: hex head in a pocket on the cheek's inner face, out through the clamp face
    translate([yk_in - 1, 0, Z_el]) rotate([0,90,0]) linear_extrude(m8_head + 1.01) hexroof(m8_af, 180);
    translate([yk_in, 0, Z_el]) rotate([0,90,0]) linear_extrude(yk_out - yk_in + 1) tdrop(m8, 180);
  }
  // azimuth scale: grooves down the rim, short every 5°, full height every 10°, read at the base groove
  for (a=[0:5:355]) rotate(270 + a) translate([disc_R - 1, -0.4, a % 10 == 0 ? yoke_z - 1 : yoke_z + yoke_t - 4])
    cube([3, 0.8, yoke_t + 2]);
}

// ---------- cradle (elevation axis at the origin, el = 0; prints hub face down) ----------
module cradle_cheek() {
  hull() {
    yz(cr_in, cr_out) circle(r=cap_r, $fn=180);
    translate([cr_in, cr_mid, -cr_mid_z]) cube([cr_out - cr_in, 0.01, 2*cr_mid_z]);
  }
  hull() {
    translate([cr_in, cr_mid, -cr_mid_z]) cube([cr_out - cr_in, 0.01, 2*cr_mid_z]);
    translate([0, L - plate_t + 0.01, 0]) rotate([90,0,0]) linear_extrude(0.01) polygon(cr_foot);
  }
}
module cradle() difference() {
  union() {
    translate([0, L, 0]) rotate([90,0,0]) disc(plate_r, plate_t);
    mirror_x() cradle_cheek();
  }
  mirror_x() translate([cr_in - 1, 0, 0]) rotate([0,90,0]) linear_extrude(cr_out - cr_in + 2) tdrop(m8, 270);
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
    translate([0,0,Z_el]) rotate([el,0,0]) {
      color("orange") cradle();
      mirror_x() translate([cr_out, 0, 0]) rotate([0,90,0]) wingnut();
      dish();
    }
  }
}
if (part == "base") { if (printing) translate([0,0,base_t]) rotate([180,0,0]) base(); else base(); }
if (part == "yoke") { if (printing) translate([0,0,-yoke_z]) yoke(); else yoke(); }
if (part == "cradle") { if (printing) translate([0,0,L]) rotate([-90,0,0]) cradle(); else cradle(); }
