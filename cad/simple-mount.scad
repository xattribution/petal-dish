// PETAL simple alt-az mount. Millimeters. Three printed parts, two pinch clamps, standard hardware.
//   base     - foot that bolts to your stand (3/8-16 tripod nut or 4 x M5), with a split vertical socket
//   rotator  - spigot that drops into the socket (azimuth), carrying a split horizontal sleeve
//   cradle   - bolts to the PETAL hub; its shaft turns in the sleeve (elevation)
// Each clamp is one M6 bolt across a slit. Loosen, aim, tighten: the compression holds the position.
// The bolts run tangent to the bore and sit in a V-groove on the spigot/shaft, so a loosened joint
// still can't lift or slide out.
// Print rules: nothing steeper than 45° from vertical, no bridges, engraving only on top faces.
// Frame: Z up, +Y = boresight at az 0 / el 0, elevation axis along X. Base bottom at Z 0.

part = "assembly";  // [assembly,base,rotator,cradle]
printing = false;
dish_d = 400; dish_fd = 0.42;
el = 30; az = 0; show_dish = true;
engrave = 0.6;
$fn = 96;
hfn = 48;
efoot = 0.6;        // first-layer chamfer
clr = 0.2;          // radial clearance in both joints

// ---------- azimuth joint: base socket <- rotator spigot ----------
sp_r = 25; sock_r = sp_r + clr; sock_R = 35;
foot_R = 55; foot_t = 12;
sp_z0 = 14;                     // spigot bottom
seat_z = 55; seat_top = 60;     // 45° seat cone at the socket mouth
collar_R = 45;
cone_top = seat_z + (collar_R - sp_r);   // rotator's 45° cone rests on the seat and flares to the collar
collar_top = cone_top + 5;

// ---------- elevation joint: rotator sleeve <- cradle shaft ----------
Z_el = 128;                     // elevation axis above the base bottom
sh_r = 20; bore_r = sh_r + clr; sl_R = 28; sl_h = 30;   // sleeve spans X -30..30
lug_R = 33;

// ---------- pinch bolts: M6, tangent to the bore, riding in a V-groove ----------
b_hole = 6.4; b_dip = 1.5;      // bolt dips 1.5 mm into the bore
nut_af = 10.2; nut_d = 5.5;     // M6 nut pocket
gr = 3.5;                       // V-groove depth (90° included)
lug_w = 16;                     // lug faces at ±16 -> M6 x 35
az_bz = 34; az_by = -(sock_r + 3 - b_dip);
el_bz = bore_r + 3 - b_dip;     // above the elevation axis

// ---------- cradle ----------
L = 70;                         // hub rear face ahead of the elevation axis
plate_t = 12; plate_r = 47; bcd_r = 30; port_d = 34; m4 = 4.5;
a0 = sl_h + 0.5;                // cradle starts just past the sleeve's +X end
a1 = 70;                        // top of the cradle as printed
x_bed = -plate_r*cos(45);       // cradle prints with -X down; plate cut flat here
pm_y = 36;                      // webs stay outside r 36 near the axis (sleeve + lugs sweep r <= 33)
webs = [[25.5, 44], [-38, -25.5]];   // clear of the M4 heads (r 30 at 45°) and the Ø34 port

// ---------- dish ----------
focal = dish_d*dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);

// ---------- helpers ----------
// 2D teardrop: circle plus a 45° point toward angle `up`
module tdrop(d, up) union() { circle(d=d, $fn=hfn); rotate(up) polygon([[d/2*cos(45), -d/2*sin(45)], [d/sqrt(2), 0], [d/2*cos(45), d/2*sin(45)]]); }
// 2D hex pocket with a flat plus 45° roof toward `up`
module hexroof(af, up) rotate(up) union() { R = af/sqrt(3); rotate(30) circle(r=R, $fn=6); polygon([[af/2, -R/2], [af/2 + R/2, 0], [af/2, R/2]]); }
module lbl(t, sz, ha="center") text(t, size=sz, font="DejaVu Sans:style=Bold", halign=ha, valign="center");
// V-groove ring cutter around Z at height z0
module vgroove(r, z0) rotate_extrude($fn=180) polygon([[r + 0.01, z0 - gr], [r - gr, z0], [r + 0.01, z0 + gr]]);
UV = [[0,0,1,0],[1,0,0,0],[0,1,0,0],[0,0,0,1]];   // 2D x -> Y, y -> Z, extrude -> X (reads from +X)

// ---------- base ----------
module base() difference() {
  union() {
    translate([0,0,efoot]) cylinder(r=foot_R, h=foot_t - efoot, $fn=180);
    cylinder(r1=foot_R - efoot, r2=foot_R, h=efoot, $fn=180);
    translate([0,0,foot_t - 0.01]) cylinder(r=sock_R, h=seat_top - foot_t + 0.01, $fn=180);
    // pinch lugs at the rear, 45° underside
    hull() {
      translate([-lug_w, -42, az_bz - 7]) cube([2*lug_w, 20, 14]);
      translate([-lug_w, -30, az_bz - 19]) cube([2*lug_w, 8, 26]);
    }
    // azimuth pointer post on the right (+X); its web follows 1 mm under the rotator's cone.
    // Kept to r <= 48 so the hub plate's lower edge clears it at -10° elevation.
    hull() {
      translate([sock_R - 1, -3, foot_t - 0.01]) cube([1, 6, seat_z + (sock_R - sp_r) - 2 - foot_t]);
      translate([collar_R + 0.5, -3, foot_t - 0.01]) cube([2.5, 6, cone_top - 1 - foot_t]);
    }
    translate([collar_R + 0.5, -3, foot_t - 0.01]) cube([2.5, 6, collar_top - 0.5 - foot_t + 0.01]);
  }
  // socket bore and 45° seat
  translate([0,0,foot_t]) cylinder(r=sock_r, h=seat_top, $fn=180);
  translate([0,0,seat_z]) cylinder(r1=sock_r, r2=sock_r + seat_top - seat_z + 0.01, h=seat_top - seat_z + 0.01, $fn=180);
  // slit through the rear wall and lugs
  translate([-1.25, -60, foot_t]) cube([2.5, 60 - sock_r + 1, 60]);
  // pinch bolt along X (head on the -X face, nut in a pocket on the +X face)
  translate([-lug_w - 10, az_by, az_bz]) rotate([0,90,0]) linear_extrude(2*lug_w + 20) tdrop(b_hole, 180);
  translate([lug_w - nut_d, az_by, az_bz]) rotate([0,90,0]) linear_extrude(nut_d + 1) hexroof(nut_af, 180);
  // tripod: 3/8-16 nut pocket in the socket floor, Ø10 through
  translate([0,0,foot_t - 8.7]) rotate(30) cylinder(r=(14.29 + 0.3)/sqrt(3), h=9, $fn=6);
  translate([0,0,-1]) cylinder(d=10, h=foot_t + 2, $fn=hfn);
  translate([0,0,-1]) cylinder(d1=10 + 2*efoot + 2, d2=10, h=efoot + 1, $fn=hfn);
  // 4 x M5 countersunk on a 88 mm circle
  for (a=[45:90:359]) rotate(a) translate([44, 0, 0]) {
    translate([0,0,-1]) cylinder(d=5.5, h=foot_t + 2, $fn=hfn);
    translate([0,0,foot_t - 2.75]) cylinder(d1=5.5, d2=11, h=2.76, $fn=hfn);
    translate([0,0,-1]) cylinder(d1=5.5 + 2*efoot + 2, d2=5.5, h=efoot + 1, $fn=hfn);
  }
}

// ---------- rotator ----------
module sleeve_solid() translate([-sl_h, 0, Z_el]) rotate([0,90,0]) linear_extrude(2*sl_h) tdrop(2*sl_R, 0);   // keel down
module rotator() difference() {
  union() {
    rotate_extrude($fn=180) polygon([[0, sp_z0], [sp_r - efoot, sp_z0], [sp_r, sp_z0 + efoot], [sp_r, seat_z],
                                     [collar_R, cone_top], [collar_R, collar_top], [0, collar_top]]);
    hull() { translate([-22, -10, collar_top - 0.01]) cube([44, 20, 0.01]); sleeve_solid(); }
    intersection() {
      translate([-12, -lug_w, Z_el]) cube([24, 2*lug_w, 40]);
      translate([-12, 0, Z_el]) rotate([0,90,0]) cylinder(r=lug_R, h=24, $fn=120);
    }
  }
  vgroove(sp_r, az_bz);
  // sleeve bore (point up into the slit), slit, bolt along Y (head at the rear), nut pocket at the front
  translate([-sl_h - 1, 0, Z_el]) rotate([0,90,0]) linear_extrude(2*sl_h + 2) tdrop(2*bore_r, 180);
  translate([-sl_h - 1, -1.25, Z_el]) cube([2*sl_h + 2, 2.5, 50]);
  translate([0, -lug_w - 10, Z_el + el_bz]) rotate([-90,0,0]) linear_extrude(2*lug_w + 20) tdrop(b_hole, 270);
  translate([0, lug_w - nut_d, Z_el + el_bz]) rotate([-90,0,0]) linear_extrude(nut_d + 1) hexroof(nut_af, 270);
  // azimuth scale on the collar top: azimuth A sits at angle A (CCW from +X), read at the base post on +X
  translate([0,0,collar_top - engrave]) linear_extrude(engrave + 1) {
    for (A=[0:2:358]) rotate(A) translate([A % 10 == 0 ? 38 : 41, -0.25]) square([collar_R + 1 - (A % 10 == 0 ? 38 : 41), 0.5]);
    for (A=[0:30:330]) rotate(A) translate([31, 0]) rotate(90) lbl(str(A), 4);
  }
}

// ---------- cradle (elevation axis at the origin, el = 0) ----------
// Prints with -X on the bed. Past the sleeve the shaft flares out at 45° (x >= a0 - sh_r + r), and two
// webs, above and below the port, run from that flare to the back of the hub plate.
module flare(rmax) translate([a0, 0, 0]) rotate([0,90,0]) cylinder(r1=sh_r, r2=rmax, h=rmax - sh_r + 0.01, $fn=180);
module band(zr, y0, y1, x0=-200) translate([x0, y0, zr[0]]) cube([a1 - x0, y1 - y0, zr[1] - zr[0]]);
module cradle() difference() {
  union() {
    intersection() {
      translate([0, L - plate_t, 0]) rotate([-90,0,0]) cylinder(r=plate_r, h=plate_t, $fn=180);
      translate([x_bed, 0, -100]) cube([200, 200, 200]);
    }
    translate([x_bed, 0, 0]) rotate([0,90,0]) cylinder(r=sh_r, h=a0 - x_bed + 0.01, $fn=180);
    flare(sh_r + 12);
    for (zr=webs) {
      intersection() { flare(a1 - a0 + sh_r); band(zr, -1, pm_y + 2); }
      hull() {
        intersection() { flare(a1 - a0 + sh_r); band(zr, pm_y, pm_y + 2); }
        translate([x_bed, L - plate_t - 1, zr[0]]) cube([40 - x_bed, 2, zr[1] - zr[0]]);   // stays behind the plate
      }
    }
  }
  rotate([0,90,0]) vgroove(sh_r, 0);
  // hub interface: Ø34 port and 4 x M4 on a 60 mm circle (teardrops point to print-up, +X)
  translate([0, L + 1, 0]) rotate([90,0,0]) linear_extrude(plate_t + 2) tdrop(port_d, 0);
  for (a=[45:90:359]) translate([bcd_r*cos(a), L + 1, bcd_r*sin(a)]) rotate([90,0,0]) linear_extrude(plate_t + 2) tdrop(m4, 0);
  // the upper web's top face (z 44) is flat and parallel to the boresight: lay a phone inclinometer on it
}

// ---------- dish envelope (preview only) ----------
module dish() if (show_dish) color([0.82,0.84,0.88]) translate([0, vertex_u, 0]) rotate([-90,0,0])
  rotate_extrude($fn=96) polygon(concat([[0, zAt(45) - 16], [60, zAt(45) - 16]],
    [for (r=[60:20:dish_d/2]) [r, zAt(r) - 17]], [for (r=[dish_d/2:-20:0]) [r, zAt(r)]]));
module bolt(len) color("silver") { cylinder(d=6, h=len, $fn=24); translate([0,0,-4]) cylinder(d=10, h=4, $fn=6); }

// ---------- output ----------
if (part == "assembly") {
  color("slategray") base();
  color("dimgray") translate([-lug_w - 1.2, az_by, az_bz]) rotate([0,90,0]) bolt(35);
  rotate([0,0,-az]) {
    color("steelblue") rotator();
    color("dimgray") translate([0, -lug_w - 1.2, Z_el + el_bz]) rotate([-90,0,0]) bolt(35);
    translate([0,0,Z_el]) rotate([el,0,0]) { color("orange") cradle(); dish(); }
  }
}
if (part == "base") base();
if (part == "rotator") { if (printing) translate([0,0,-sp_z0]) rotator(); else rotator(); }
if (part == "cradle") { if (printing) translate([0,0,-x_bed]) rotate([0,-90,0]) cradle(); else cradle(); }
