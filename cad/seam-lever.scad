// PETAL seam lever: a quick-release cam clamp for clip/lever seam stations. Millimeters.
// No screws. Three printed parts plus a printed TPU spring (or a small metal spring in the same pocket):
//   lever   - two cheeks with the cam profile, joined by the handle; an open slot takes the bar's cross pin
//   bar     - draw bar: cross pin (lever side), octagon shaft through both flange holes, neck + button (far side)
//   keeper  - slides up onto the neck from behind the dish, holds the spring against the far flange
//   spring  - TPU block in the keeper pocket (or swap in disc/coil springs)
// Install: bar through both holes from the lever side, keeper (with spring) up onto the neck from the rear,
// push the lever's open slot up onto the cross pin with the handle sticking straight out, then flip the handle
// down along the flange; it snaps over-center onto its flat. Release: flip the handle out, drop the lever off
// the pin, slide the keeper down.
//
// Station geometry comes from the PETAL generator (seam joint = "Both", where every station has the bolt
// hole and the clip window): 5 mm flat walls, guide ridges 10.8 mm apart, bore 2.2 mm below station level.
// World frame: X across the seam (seam plane X = 0, lever on +X), Y toward the shell (Y = 0 at the station
// level), Z along the seam.

part = "assembly";  // [assembly,lever,bar,keeper,spring,plate,installed_lever,installed_bar,installed_keeper,installed_spring]
closed = true;      // assembly view: handle down (clamped) or up (open)
$fn = 48;

// ---------- from the PETAL designer (Both mode) ----------
wall      = 5;      // each flange wall at a station (CLIP.wall)
hole_d    = 3.4;    // seam bore: 3.4 = M3 as generated; 4.5 = M4
hole_y    = -2.2;   // boltY() in Both mode
ridge_gap = 10.8;   // clear width between the two guide ridges (2 x (CLIP.width/2 + CLIP.clear))
shell_y   = 6.75;   // nothing may rise above this (CLIP.top)

// ---------- tuning ----------
hole_clr  = 0.25;   // bar corner-to-hole diametral clearance
stroke    = 1.2;    // net draw from handle-up to handle-down
hump      = 0.3;    // over-center: how far the cam lifts before it drops onto its flat
pre       = 0.3;    // spring preload with the handle up (keeps the keeper seated)
spring_t  = 4.5;    // free spring thickness (TPU: ~40% max squeeze at stroke + hump + pre)
pocket    = 7;      // square spring pocket in the keeper
pocket_d  = 2;      // pocket depth
lever_w   = 10;     // lever width, sits between the ridges
handle_l  = 30;     // handle length below the pivot
handle_t  = 3.5;    // handle thickness at the tip

// ---------- derived ----------
p    = floor((hole_d - hole_clr) * cos(22.5) * 10) / 10;   // bar section: octagon across flats
n    = 0.5 * p;                                            // neck width (balances neck vs shoulder area)
gap  = p + 0.6;                                            // central slot between the lever cheeks
rb   = p / 2 / cos(22.5) + 0.2;                            // pin slot half width
h_open = max(5, rb + 3);                                   // cam radius toward the wall, handle up
hf   = h_open + stroke;                                    // pivot-to-flat, handle down
rc   = hf + hump;                                          // peak radius at the flat's corner
yc   = sqrt(rc * rc - hf * hf);                            // corner height above the pivot
th_c = atan2(yc, -hf);                                     // corner angle
floor_t = 2.5;                                             // keeper floor (fork) thickness
button  = 3;
x_pf = -wall - (spring_t - pre);   // pocket floor, handle up
x_k  = x_pf - floor_t;             // keeper back face = button shoulder, handle up
pin_x = wall + h_open;             // pivot, handle up
kw = min(ridge_gap - 1.4, pocket + 2.4);
kb = 6.5; kt = pocket / 2 + 2;

echo(str("bar octagon ", p, " mm, neck ", n, " mm, pin slot ", 2 * rb, " mm, cheeks ", (lever_w - gap) / 2, " mm"));
echo(str("cam radius to the wall: open ", h_open, ", clamped flat ", hf, ", over-center peak ", rc));

echo(MOTION = [h_open, hf, rc, stroke, hole_y, wall]);

// ---------- helpers ----------
module oct(d) rotate(22.5) circle(d = d / cos(22.5), $fn = 8);   // octagon, across flats d
// lever profile in the lever frame: pivot at origin, handle down, x away from the wall, y toward the shell
function spiral() = [for (i = [0:24]) let(t = 90 + (th_c - 90) * i / 24, r = h_open + (rc - h_open) * i / 24) [r * cos(t), r * sin(t)]];
function lever_pts() = concat(
  [[-hf, -handle_l], [-hf + handle_t, -handle_l]],
  [for (t = [-40:10:80]) [h_open * cos(t), h_open * sin(t)]],
  spiral(),
  [[-hf, yc]]);
module lever_profile() intersection() {
  polygon(lever_pts());
  // The open cam must not protrude beyond h_open toward the flange.
  // The radial spiral briefly exceeds this tangent otherwise (about 0.13 mm).
  translate([-60, -60]) square([120, 60 + h_open]);
  // 45° print chamfer up from the flat's corner (the flat is the bed face)
  polygon([[-hf, yc], [40, yc + 40 + hf], [40, -60], [-hf, -60]]);
}

// ---------- parts (each modeled in its own working frame) ----------
module lever() difference() {
  translate([0, 0, -lever_w / 2]) linear_extrude(lever_w) lever_profile();
  // central slot for the bar: everything above the handle root, down through the flat
  translate([-50, -p / 2 - 0.5, -gap / 2]) cube([100, 50, gap]);
  // open pin slot through both cheeks, opening away from the wall
  translate([0, 0, -lever_w]) linear_extrude(2 * lever_w) { circle(r = rb); translate([0, -rb]) square([20, 2 * rb]); }
}

module bar() {   // world frame, handle up
  // cross pin
  translate([pin_x, hole_y, -(lever_w - 0.4) / 2]) linear_extrude(lever_w - 0.4) oct(p);
  // shaft from the pin to the neck
  translate([x_pf + 0.4, hole_y, 0]) rotate([0, 90, 0]) linear_extrude(pin_x - x_pf - 0.4) rotate(90) oct(p);
  // neck
  intersection() {
    translate([x_k - 0.01, hole_y, 0]) rotate([0, 90, 0]) linear_extrude(x_pf + 0.42 - x_k) rotate(90) oct(p);
    translate([x_k - 1, hole_y - p, -n / 2]) cube([floor_t + 2, 2 * p, n]);
  }
  // button
  translate([x_k - button, hole_y, 0]) rotate([0, 90, 0]) linear_extrude(button) rotate(90) oct(p);
}

module keeper() difference() {   // world frame, handle up
  translate([x_k, hole_y - kb, -kw / 2]) cube([x_pf + pocket_d - x_k, kb + kt, kw]);
  translate([x_pf, hole_y - pocket / 2, -pocket / 2]) cube([10, pocket, pocket]);                        // spring pocket
  translate([x_pf - 0.01, hole_y - kb - 1, -(p + 0.4) / 2]) cube([10, kb + 1 + p / 2 + 0.2, p + 0.4]);  // bar clearance
  translate([x_k - 1, hole_y - kb - 1, -(n + 0.3) / 2]) cube([floor_t + 2, kb + 1 + p / 2 + 0.2, n + 0.3]); // neck fork
}

module spring() difference() {   // world frame, free length, seated in the pocket
  translate([x_pf, hole_y - (pocket - 0.25) / 2, -(pocket - 0.25) / 2]) cube([spring_t, pocket - 0.25, pocket - 0.25]);
  translate([x_pf - 1, hole_y - pocket, -(p + 0.3) / 2]) cube([spring_t + 2, pocket + p / 2 + 0.15, p + 0.3]);
}

// ---------- station mock-up for the assembly view ----------
module station() color("lightgray") difference() {
  union() {
    translate([-wall, -9, -20]) cube([2 * wall, shell_y + 9 + 0.25, 40]);
    translate([-30, shell_y + 0.25, -20]) cube([60, 2.4, 40]);   // shell
    for (s = [-1, 1], k = [-1, 1]) translate([s > 0 ? wall : -wall - 1, -9, k > 0 ? ridge_gap / 2 : -ridge_gap / 2 - 1]) cube([1, shell_y + 9, 1]);
  }
  translate([-20, hole_y, 0]) rotate([0, 90, 0]) cylinder(d = hole_d, h = 40);
}

// ---------- print orientations ----------
// lever: flat face (x = -hf) on the bed, cheeks side by side; lever y -> X, z -> Y, x -> Z
module print_lever()  multmatrix([[0, 1, 0, 0], [0, 0, 1, 0], [1, 0, 0, hf], [0, 0, 0, 1]]) lever();
// bar: world Y up, everything flat on the bed; keeper: back face down, pocket up; spring: squeeze axis up
module print_bar()    translate([0, 0, p / 2 - hole_y]) rotate([90, 0, 0]) bar();
module print_keeper() multmatrix([[0, 1, 0, -hole_y], [0, 0, 1, 0], [1, 0, 0, -x_k], [0, 0, 0, 1]]) keeper();
module print_spring() multmatrix([[0, 1, 0, -hole_y], [0, 0, 1, 0], [1, 0, 0, -x_pf], [0, 0, 0, 1]]) spring();

module lever_world(c) translate([wall + (c ? hf : h_open), hole_y, 0]) rotate([0, 0, c ? 0 : 90]) lever();

// installed poses in the station frame (no mock station); the PETAL generator bundles these for its assembly view
module installed_lever()  lever_world(closed);
module installed_bar()    translate([closed ? stroke : 0, 0, 0]) bar();
module installed_keeper() translate([closed ? stroke : 0, 0, 0]) keeper();
module installed_spring() { s = closed ? stroke : 0; translate([s + x_pf, 0, 0]) scale([(spring_t - pre - s) / spring_t, 1, 1]) translate([-x_pf, 0, 0]) spring(); }

if (part == "assembly") {
  station();
  color("dimgray") installed_lever();
  color("firebrick") installed_bar();
  color("goldenrod") installed_keeper();
  color("khaki") installed_spring();
}
if (part == "installed_lever")  installed_lever();
if (part == "installed_bar")    installed_bar();
if (part == "installed_keeper") installed_keeper();
if (part == "installed_spring") installed_spring();
if (part == "lever")  print_lever();
if (part == "bar")    print_bar();
if (part == "keeper") print_keeper();
if (part == "spring") print_spring();
if (part == "plate") {
  print_lever();
  translate([0, 16, 0]) print_bar();
  translate([24, 0, 0]) print_keeper();
  translate([24, -15, 0]) print_spring();
}
