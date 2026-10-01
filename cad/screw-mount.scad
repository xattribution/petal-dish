// PETAL pin-and-screw alt-az mount. Millimeters. Printed in ASA-GF (structure) and PCTG (wear parts).
// Design rule: nothing that holds the dish relies on friction or bolt preload.
//   Coarse: a tapered printed pin drops into a hole every 10° (elevation: in an index wheel on the
//           trunnion; azimuth: in the base). The taper wedges out all play.
//   Fine:   the pin rides in a nut block driven by a captive printed screw, ±9 mm ≈ ±7-9°.
//   Hold:   dish -> pin -> nut block -> screw thread (self-locking, 4° lead) -> housing walls.
// Both axes use the same screw, nut block, nut and pin. Only the 4 M4 screws into the PETAL
// hub's existing inserts are metal.
// Print rules: nothing steeper than 45° from vertical, no bridges, engraving only on top faces.
// Frame: Z up, +Y = boresight at az 0 / el 0, X = elevation axle. Yoke frame: flange bottom at Z 0.

part = "assembly";  // [assembly,base,yoke,cradle,trunnion-pin,index-wheel,trunnion-nut,keeper,washer,screw,nut-block,nut,index-pin,lock-pin,riser,arca-plate,pipe-adapter,coupon]
printing = false;
dish_d = 400; dish_fd = 0.42; el_min = -5;
el = 30; az = 0;
engrave = 0.6;
$fn = 72;
hfn = 48;

// ---------- layout ----------
L = 90;                 // hub rear face ahead of the elevation axle
Ha = 100;               // elevation axle above the yoke flange bottom
plate_t = 10; plate_r = 47; bcd_r = 30; port_d = 34; m4 = 4.5;
ck_in = 28; ck_t = 12; ck_out = ck_in + ck_t;          // cradle cheeks
arm_in = ck_out + 0.5; arm_t = 12; arm_out = arm_in + arm_t;
wheel_t = 10; wheel_in = arm_out + 0.1; wheel_out = wheel_in + wheel_t; wheel_R = 64;
R_el = 56;              // elevation index holes in the wheel
R_az = 70;              // azimuth index holes in the base
fl_R = 72; fl_t = 12;   // yoke flange core (lobes added at the rear and right for the housings)
base_R = 78;            // base stays inside the dish-back sweep at low elevation
boss_r = 35; boss_top = 30;
sp_d = 50; sp_top = 40; thread_z0 = 30;                  // base spindle, keeper thread Z 30..40
base_bot = -34; base_top = -2;                          // washer Z -2..0
efoot = 0.6;

// ---------- shared drive parts ----------
sc_d = 12; sc_p = 2.5;          // drive screw Ø12, lead 2.5 (4.1° lead angle)
knob_d = 24; knob_t = 8; col_d = 13; col_t = 8; thr_l = 40; nose_d = 7; nose_t = 8;
blk_T = 14; blk_L = 38; blk_H = 22;   // nut block: along pin, along radius, along screw
blk_pin = 7;                          // pin hole center from the block's inner end
blk_nut = 25;                         // screw axis from the block's inner end (offset 18)
nut_af = 18; nut_t = 10;
travel = 9;                           // ± block travel
cav = blk_H + 2*travel;               // 40 between housing walls
wall_t = 8;
pin_d = 9; pin_tip = 7.9; pin_taper = 10; pin_shaft = 38; pin_head_d = 16; pin_head_t = 5;
// screw axis offset from the pin, radially outward
off = blk_nut - blk_pin;

// ---------- dish / heights ----------
focal = dish_d * dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);
rim_u = vertex_u + zAt(dish_d/2);
axle_h = dish_d/2*cos(el_min) - rim_u*sin(el_min) + 15;
riser_h = axle_h - (Ha - base_bot);
echo(str("PETAL-SCREW axle_h=", axle_h, " riser_h=", riser_h,
  " el_deg_per_turn=", sc_p/R_el*180/PI, " az_deg_per_turn=", sc_p/R_az*180/PI,
  " lead_angle=", atan(sc_p/(PI*(sc_d - 1)))));

// ---------- helpers ----------
module uv(x0, t) multmatrix([[0,0,1,x0],[1,0,0,0],[0,1,0,0]]) linear_extrude(t) children();
module at(r, a) translate([r*cos(a), r*sin(a)]) children();
module atc(r, a) translate([r*sin(a), r*cos(a)]) children();
module lbl(t, sz) text(t, size=sz, font="DejaVu Sans:style=Bold", halign="center", valign="center");
module tdrop(d, up) union() { circle(d=d, $fn=hfn); rotate(up) polygon([[d/2*cos(45), -d/2*sin(45)], [d/sqrt(2), 0], [d/2*cos(45), d/2*sin(45)]]); }
module hexroof(af, up) rotate(up) union() { R = af/sqrt(3); rotate(30) circle(r=R, $fn=6); polygon([[af/2, -R/2], [af/2 + R/2, 0], [af/2, R/2]]); }
module entry(d) translate([0,0,-1]) cylinder(d1=d + 2*efoot + 2, d2=d, h=efoot + 1, $fn=hfn);
// threads: 48° flanks (42° from vertical when printed upright), crest/root flats
function thr_r(a, rmaj, p, dep) = let(s = p*a/360, d = abs(((s + p/2) % p + p) % p - p/2), r1 = rmaj - dep/2)
  min(rmaj, max(rmaj - dep, r1 + (p/4 - d)/tan(48)));
module thread2d(rmaj, p, dep) polygon([for (a=[0:4:356]) [thr_r(a, rmaj, p, dep)*cos(a), thr_r(a, rmaj, p, dep)*sin(a)]]);
module thread(dmaj, p, len, dep) linear_extrude(len, twist=-360*len/p, slices=round(len/p*90), convexity=10) thread2d(dmaj/2, p, dep);
// male thread with 45° run-in/run-out so no ledge starts in mid-air
module ext_thread(dmaj, p, len, dep) intersection() {
  thread(dmaj, p, len, dep);
  union() {
    cylinder(r1=dmaj/2 - dep, r2=dmaj/2 - dep + len, h=len, $fn=hfn*2);
  }
}
// cutter for a printed nut (thread axis vertical when printed)
module int_thread_cut(dmaj, p, len, dep, clr=0.3) translate([0,0,-0.5]) thread(dmaj + 2*clr, p, len + 1, dep);

// ---------- drive screw (z up from the knob face) ----------
module screw() difference() {
  union() {
    difference() {
      cylinder(d=knob_d, h=knob_t, $fn=96);
      for (i=[0:13]) rotate(i*360/14) translate([knob_d/2 + 0.8, 0, -1]) cylinder(d=3.4, h=knob_t + 2, $fn=24);
    }
    translate([0,0,knob_t - 0.01]) cylinder(d=col_d, h=col_t + 0.02, $fn=96);
    translate([0,0,knob_t + col_t]) ext_thread(sc_d, sc_p, thr_l, 1.0);
    translate([0,0,knob_t + col_t + thr_l - 0.01]) cylinder(d=nose_d, h=nose_t + 0.01, $fn=hfn);
  }
  // 8 knob ticks, each 1/8 turn
  for (i=[0:7]) rotate(i*45) translate([knob_d/2 - engrave, -0.4, i == 0 ? 0 : 3]) cube([engrave + 1, 0.8, knob_t + 1]);
}
module nut() difference() {
  rotate(30) cylinder(r=nut_af/sqrt(3), h=nut_t, $fn=6);
  int_thread_cut(sc_d, sc_p, nut_t, 1.0);
  translate([0,0,-0.01]) cylinder(d1=sc_d + 1.2, d2=sc_d - 2, h=(sc_d + 1.2 - sc_d + 2)/2, $fn=hfn);
  translate([0,0,nut_t + 0.01]) mirror([0,0,1]) cylinder(d1=sc_d + 1.2, d2=sc_d - 2, h=1.6, $fn=hfn);
}
// nut block, print frame: Z = pin axis (thickness blk_T), X = along screw, Y = radial (inner end at Y 0)
module nut_block() difference() {
  translate([-blk_H/2, 0, 0]) cube([blk_H, blk_L, blk_T]);
  translate([0, blk_pin, -1]) cylinder(d=pin_d + 0.15, h=blk_T + 2, $fn=hfn);
  // screw clearance, axis along X at Y = blk_nut, mid-thickness; roof toward print-up (+Z)
  translate([-blk_H/2 - 1, blk_nut, blk_T/2]) rotate([0,90,0]) linear_extrude(blk_H + 2) tdrop(sc_d + 0.8, 180);
  // nut slot: drops in from print-top
  hull() {
    translate([-nut_t/2 - 0.15, blk_nut, blk_T/2]) rotate([0,90,0]) linear_extrude(nut_t + 0.3) circle(r=(nut_af + 0.3)/sqrt(3), $fn=6);
    translate([-nut_t/2 - 0.15, blk_nut, blk_T + 10]) rotate([0,90,0]) linear_extrude(nut_t + 0.3) circle(r=(nut_af + 0.3)/sqrt(3), $fn=6);
  }
}
module index_pin(shaft=pin_shaft) union() {    // head down
  cylinder(d=pin_head_d, h=pin_head_t, $fn=hfn);
  translate([0,0,pin_head_t - 0.01]) cylinder(d=pin_d, h=shaft + 0.01, $fn=hfn);
  translate([0,0,pin_head_t + shaft - 0.01]) cylinder(d1=pin_d, d2=pin_tip, h=pin_taper + 0.01, $fn=hfn);
}
// rail lock: same pin, long enough to cross the rail from the base rim
lock_shaft = 92;
// tapered, radially oblong seat for the pin tip; length along the pin = pin_taper, wide end at z 0
// (radial = local +y). Tangential fit is the taper wedge; radial slack absorbs the straight-line travel.
// the taper runs on 3 mm past the pin tip so the pin wedges on its flank instead of bottoming
seat_run = 3; seat_end = pin_tip + 0.1 - (pin_d - pin_tip)/pin_taper*seat_run;
module pin_seat(up=undef) {
  slack = 0.7;
  hull() for (dy=[-slack, slack]) translate([0, dy, 0]) {
    translate([0,0,-1]) cylinder(d=pin_d + 0.1, h=1.01, $fn=hfn);      // starts above the face: never skins over
    cylinder(d1=pin_d + 0.1, d2=seat_end, h=pin_taper + seat_run + 0.01, $fn=hfn);
  }
  translate([0,0,pin_taper + seat_run]) hull() for (dy=[-slack, slack]) translate([0, dy, 0]) cylinder(d1=seat_end, d2=0, h=seat_end/2, $fn=hfn);
}

// ---------- housing (same for both axes) ----------
// local frame: screw axis = X (knob end at -X), radial outward = +Y from the pin row, pin axis = Z.
// Block sits on a floor at z 0; screw axis at z = blk_T/2. Walls stand at x = ±cav/2.
// local frame: origin at the pin axis on the floor plane; screw along X (knob at -X); radial outward +Y;
// pin axis Z (block occupies z 0..blk_T). `up` = 2D roof angle so the roof points to print-up.
wall_h = blk_T/2 + (col_d + 0.5)/2*sqrt(2) + 3;
module housing_walls(y1=blk_L + 4) {
  for (s=[-1, 1]) translate([s > 0 ? cav/2 : -cav/2 - wall_t, -4 - blk_pin, 0]) cube([wall_t, y1 + 4, wall_h]);
}
module housing_holes(up) {
  translate([-cav/2 - wall_t - 1, blk_nut - blk_pin, blk_T/2]) rotate([0,90,0]) linear_extrude(wall_t + 2) tdrop(col_d + 0.5, up);
  translate([cav/2 - 1, blk_nut - blk_pin, blk_T/2]) rotate([0,90,0]) linear_extrude(wall_t + 2) tdrop(nose_d + 0.5, up);
  translate([-cav/2 - wall_t - 30, blk_nut - blk_pin, blk_T/2]) rotate([0,90,0]) linear_extrude(30) tdrop(knob_d + 2, up);
}
module housing_cavity() translate([-cav/2, -4 - blk_pin, 0]) cube([cav, blk_L + 8, 80]);

// ---------- base ----------
rail_w0 = 22; rail_w1 = 30; rail_h1 = 4; rail_ridge = rail_h1 + rail_w1/2;
module rail2d(c=0) polygon([[-rail_w0/2 - c, -0.01], [rail_w0/2 + c, -0.01], [rail_w1/2 + c, rail_h1], [0, rail_ridge + c], [-rail_w1/2 - c, rail_h1]]);
lock_y = -30; lock_z = 7;   // lock pin: along X, Y -30, 7 mm above the rail base
module base() difference() {
  union() {
    translate([0,0,base_bot + efoot]) cylinder(r=base_R, h=base_top - base_bot - efoot, $fn=180);
    translate([0,0,base_bot]) cylinder(r1=base_R - efoot, r2=base_R, h=efoot, $fn=180);
    translate([0,0,base_top - 0.01]) cylinder(d=sp_d, h=thread_z0 - base_top + 0.01, $fn=180);
    translate([0,0,thread_z0]) ext_thread(sp_d, 3, sp_top - thread_z0, 1.2);
  }
  // rail channel along Y, open both ends
  translate([0, base_R + 1, base_bot]) rotate([90,0,0]) linear_extrude(2*base_R + 2) rail2d(0.2);
  // azimuth index seats
  for (j=[0:35]) atc(R_az, j*10) translate([0,0,base_top]) rotate([0,0,-j*10]) rotate([180,0,0]) pin_seat();
  // lock pin across the rail
  translate([base_R + 1, lock_y, base_bot + lock_z]) rotate([0,-90,0]) linear_extrude(base_R*2 + 2) tdrop(pin_d + 0.4, 0);
  // azimuth pointer: full-height groove at the front of the rim
  translate([-0.5, base_R - engrave, base_bot - 1]) cube([1, engrave + 1, base_top - base_bot + 2]);
}
module washer() difference() {
  cylinder(r=R_az - pin_d/2 - 3, h=2, $fn=180);
  translate([0,0,-1]) cylinder(d=sp_d + 0.8, h=4, $fn=180);
}
module keeper() difference() {
  cylinder(d=70, h=sp_top - boss_top, $fn=120);
  int_thread_cut(sp_d, 3, sp_top - boss_top, 1.2);
  for (i=[0:11]) rotate(i*30) translate([35.6, 0, -1]) cylinder(d=4, h=20, $fn=24);
  translate([0,0,-0.01]) cylinder(d1=sp_d + 2.2, d2=sp_d + 0.6 - 2*1.2, h=(2.2 - 0.6 + 2.4)/2, $fn=hfn*2);
}

// ---------- yoke ----------
az_pad = 8;
// azimuth housing at the rear: pin vertical at Y = -R_az, screw along X (knob to -X)
module az_frame() translate([0, -R_az, fl_t + az_pad]) mirror([0,1,0]) children();
// elevation housing below the axle on the right: pin along X at Z = Ha - R_el, screw along Y (knob to the rear),
// radial outward = -Z. (A reflection; the housing is symmetric apart from which wall holds the collar.)
module el_frame() translate([wheel_out + 0.9, 0, Ha - R_el]) multmatrix([[0,0,1,0],[1,0,0,0],[0,-1,0,0],[0,0,0,1]]) children();
module arm_profile() hull() { translate([0, Ha]) circle(26); translate([-45, fl_t - 0.01]) square([90, 1]); }
module flange2d() hull() {
  circle(r=fl_R, $fn=180);
  translate([-cav/2 - wall_t - 2, -(R_az + blk_L - blk_pin + 6)]) square([cav + 2*wall_t + 4, 40]);   // rear lobe
  translate([wheel_out, -(cav/2 + wall_t + 2)]) square([wall_h + 2, cav + 2*wall_t + 4]);               // right lobe
}
module yoke() difference() {
  union() {
    translate([0,0,efoot]) linear_extrude(fl_t - efoot) flange2d();
    linear_extrude(efoot) offset(delta=-efoot) flange2d();
    hull() { linear_extrude(0.01) offset(delta=-efoot) flange2d(); translate([0,0,efoot]) linear_extrude(0.01) flange2d(); }
    translate([0,0,fl_t - 0.01]) cylinder(r=boss_r, h=boss_top - fl_t + 0.01, $fn=120);
    for (s=[-1, 1]) {
      uv(s > 0 ? arm_in : -arm_out, arm_t) arm_profile();
      intersection() {
        hull() {
          translate([s > 0 ? arm_in - 6 : -arm_out - 6, -45, fl_t - 0.01]) cube([arm_t + 12, 90, 0.01]);
          translate([s > 0 ? arm_in : -arm_out, -45, fl_t + 6]) cube([arm_t, 90, 0.01]);
        }
        linear_extrude(50) flange2d();
      }
    }
    az_frame() { translate([-cav/2 - wall_t, -4 - blk_pin, -az_pad]) cube([cav + 2*wall_t, blk_L + 8, az_pad]); housing_walls(); }
    // elevation walls reach down into the flange
    el_frame() housing_walls(blk_L + 4 + 6);
    // elevation scale pointer: blade from the rear wall up to r = 49 at world el_lbl
    hull() {
      translate([wheel_out + 0.4, -cav/2 - wall_t, Ha - R_el - 2]) cube([3, wall_t, 2]);
      translate([wheel_out + 0.4, 49.5*cos(el_lbl) - 1, Ha + 49.5*sin(el_lbl)]) cube([3, 2, 0.5]);
    }
  }
  translate([0,0,-1]) cylinder(d=sp_d + 0.6, h=boss_top + 2, $fn=180);
  translate([0,0,-0.01]) cylinder(d1=sp_d + 0.6 + 2*efoot, d2=sp_d + 0.6, h=efoot, $fn=180);
  for (s=[-1, 1]) uv(s > 0 ? arm_in - 1 : -arm_out - 1, arm_t + 2) translate([0, Ha]) tdrop(30.5, 90);
  az_frame() { housing_holes(180); housing_cavity();
    hull() for (x=[-travel, travel]) translate([x, 0, -az_pad - fl_t - 1]) cylinder(d=pin_d + 2.4, h=fl_t + az_pad + 2, $fn=hfn); }
  el_frame() { housing_holes(270); housing_cavity(); }
  // wheel slab stays clear
  translate([arm_out, 0, Ha]) rotate([0,90,0]) cylinder(r=wheel_R + 1, h=wheel_out + 0.9 - arm_out, $fn=180);
  // azimuth scale on the flange top, read at the base's front groove; labels only on clear flange
  translate([0,0,fl_t - engrave]) linear_extrude(engrave + 1) {
    for (A=[0:5:355]) if (cos(A) > -0.3 && (abs((fl_R - 2)*sin(A)) < arm_in - 7 || cos(A) > 0.75)) rotate(A) translate([-0.4, fl_R - (A % 10 == 0 ? 6 : 3.5)]) square([0.8, 8]);
    for (A=[0:30:330]) if (abs(62*sin(A)) < arm_in - 8 && cos(A) > -0.6) atc(fl_R - 10, -A) rotate(A) lbl(str(A), 4);
  }
}

// ---------- cradle ----------
module cheek_profile() hull() { circle(26); translate([L - 10, -16]) square([10, 32]); }
module cradle() difference() {
  union() {
    translate([0, L - plate_t, 0]) rotate([-90,0,0]) {
      cylinder(r=plate_r, h=plate_t - efoot, $fn=120);
      translate([0,0,plate_t - efoot]) cylinder(r1=plate_r, r2=plate_r - efoot, h=efoot, $fn=120);
    }
    for (s=[-1, 1]) uv(s > 0 ? ck_in : -ck_out, ck_t) cheek_profile();
  }
  for (s=[-1, 1]) uv(s > 0 ? ck_in - 1 : -ck_out - 1, ck_t + 2) hexroof(22.3, 180);
  translate([0, L - plate_t - 1, 0]) rotate([-90,0,0]) cylinder(d=port_d, h=plate_t + 2, $fn=hfn*2);
  for (a=[45:90:359]) translate([bcd_r*cos(a), L - plate_t - 1, bcd_r*sin(a)]) rotate([-90,0,0]) cylinder(d=m4, h=plate_t + 2, $fn=hfn);
  for (c=[[0,0,port_d], [bcd_r*cos(45), bcd_r*sin(45), m4], [bcd_r*cos(135), bcd_r*sin(135), m4],
          [bcd_r*cos(225), bcd_r*sin(225), m4], [bcd_r*cos(315), bcd_r*sin(315), m4]])
    translate([c[0], L, c[1]]) rotate([90,0,0]) entry(c[2]);
}

// ---------- trunnion pin, wheel, nut (pin frame: z from the head outward-to-inward) ----------
tp_head = 4; tp_whex = wheel_t; tp_jour = wheel_in - ck_out; tp_chex = ck_t; tp_thr = 12;
module trunnion_pin() union() {
  cylinder(d=44, h=tp_head, $fn=120);
  translate([0,0,tp_head - 0.01]) rotate(30) cylinder(r=32/sqrt(3), h=tp_whex + 0.01, $fn=6);
  translate([0,0,tp_head + tp_whex - 0.01]) cylinder(d=30, h=tp_jour + 0.01, $fn=120);
  translate([0,0,tp_head + tp_whex + tp_jour - 0.01]) rotate(30) cylinder(r=22/sqrt(3), h=tp_chex + 0.01, $fn=6);
  translate([0,0,tp_head + tp_whex + tp_jour + tp_chex - 0.05]) ext_thread(18, 3, tp_thr + 0.05, 1.2);
}
module trunnion_nut() difference() {
  union() { cylinder(d=26, h=10, $fn=96); for (a=[0, 180]) rotate(a) hull() { translate([10,-4,0]) cube([1, 8, 10]); translate([21,-3,0]) cube([1, 6, 5]); } }
  int_thread_cut(18, 3, 10, 1.2);
  translate([0,0,-0.01]) cylinder(d1=19.8, d2=15.4, h=2.2, $fn=hfn);
}
el_lbl = 225;   // pointer world angle for the elevation scale
module index_wheel() difference() {   // printed outer face up; z 0 = inner face
  union() {
    translate([0,0,efoot]) cylinder(r=wheel_R, h=wheel_t - efoot, $fn=180);
    cylinder(r1=wheel_R - efoot, r2=wheel_R, h=efoot, $fn=180);
  }
  translate([0,0,-1]) rotate(30) cylinder(r=32.3/sqrt(3), h=wheel_t + 2, $fn=6);
  // index seats: wide end on the outer face (pin enters from outside)
  for (k=[16:28]) at(R_el, k*10) rotate(k*10 - 90) translate([0,0,wheel_t]) mirror([0,0,1]) pin_seat();
  // scale on the outer face: label t sits at body angle (el_lbl - t)
  translate([0,0,wheel_t - engrave]) linear_extrude(engrave + 1) {
    for (t=[-10:1:105]) rotate(el_lbl - t) translate([42.5, -0.3]) square([t % 5 == 0 ? 6 : 3.5, 0.6]);
    for (t=[-10:10:100]) rotate(el_lbl - t + 180) translate([-41, 0]) text(str(t), size=3.2, font="DejaVu Sans:style=Bold", halign="left", valign="center");   // radial (reads toward the hub at the pointer), so 3-digit labels fit 10° apart
  }
}

// ---------- mounts (male rail on top) ----------
module male_rail(len) translate([0, len/2, 0]) rotate([90,0,0]) linear_extrude(len) rail2d(0);
module rail_lock_hole(z0) translate([60, lock_y, z0 + lock_z]) rotate([0,-90,0]) linear_extrude(120) tdrop(pin_d + 0.4, 0);
module riser() difference() {
  H = riser_h;
  union() {
    hull() { cylinder(r=75, h=8, $fn=160); translate([-22, -62, H - 1]) cube([44, 124, 1]); }
    translate([0,0,H - 0.01]) male_rail(124);
  }
  rail_lock_hole(H);
  for (a=[45:90:359]) rotate(a) translate([64, 0, 0]) {
    translate([0,0,-1]) cylinder(d=5.5, h=10, $fn=hfn);
    translate([0,0,8 - 2.75]) cylinder(d1=5.5, d2=11, h=2.76, $fn=hfn);
    translate([0,0,8]) cylinder(d=11, h=H, $fn=hfn);
  }
}
module arca_plate() difference() {   // Arca-Swiss compatible dovetail (38 mm, 45°) underneath
  union() {
    translate([0, 45, 0]) rotate([90,0,0]) linear_extrude(90) polygon([[-19,0],[19,0],[16,3],[16,9],[-16,9],[-16,3]]);
    translate([0,0,9 - 0.01]) male_rail(90);
  }
  rail_lock_hole(9);
}
module pipe_adapter() difference() {   // 1-1/4" NPS mast (42.2 OD)
  union() {
    cylinder(r=29, h=55, $fn=160);
    hull() { translate([0,0,54.99]) cylinder(r=29, h=0.01, $fn=160); translate([-22, -62, 95]) cube([44, 124, 1]); }
    translate([0,0,95.99]) male_rail(124);
    translate([22, -17, 0]) cube([26, 34, 55]);
  }
  translate([0,0,-1]) cylinder(d=42.8, h=56, $fn=180);
  translate([0,0,55]) cylinder(d1=42.8, d2=0, h=21.4, $fn=180);
  translate([20, 0, 0]) rotate([90,0,90]) linear_extrude(30) polygon([[-1.5,-1],[1.5,-1],[1.5,55],[0,56.5],[-1.5,55]]);   // 45° roof
  // pinch: drive screw through the -Y lug into a nut in the +Y lug
  translate([38, -18, 27.5]) rotate([-90,0,0]) linear_extrude(16) tdrop(col_d + 0.5, 270);
  translate([38, 0, 27.5]) rotate([-90,0,0]) linear_extrude(18) tdrop(sc_d + 0.8, 270);
  translate([38, 5, 27.5]) rotate([-90,0,0]) linear_extrude(nut_t + 0.3) hexroof(nut_af + 0.3, 270);
  translate([38, -48, 27.5]) rotate([-90,0,0]) linear_extrude(30) tdrop(knob_d + 2, 270);
  rail_lock_hole(96);
}
// fit coupon: one housing section, a pin seat, a nut pocket -- print before the full set
module coupon() difference() {   // one housing on a floor with a pin seat: checks screw, nut, block and pin fit
  union() { translate([-cav/2 - wall_t, -4 - blk_pin, -12]) cube([cav + 2*wall_t, blk_L + 8, 12]); housing_walls(); }
  housing_holes(180); housing_cavity();
  rotate([180,0,0]) pin_seat();
}

// ---------- output ----------
if (part == "base") { if (printing) translate([0,0,-base_bot]) base(); else base(); }
if (part == "yoke") yoke();
if (part == "cradle") { if (printing) translate([0,0,L]) rotate([-90,0,0]) cradle(); else cradle(); }
if (part == "trunnion-pin") trunnion_pin();
if (part == "index-wheel") index_wheel();
if (part == "trunnion-nut") trunnion_nut();
if (part == "keeper") keeper();
if (part == "washer") washer();
if (part == "screw") screw();
if (part == "nut-block") nut_block();
if (part == "nut") nut();
if (part == "index-pin") index_pin();
if (part == "lock-pin") index_pin(lock_shaft);
if (part == "riser") riser();
if (part == "arca-plate") arca_plate();
if (part == "pipe-adapter") pipe_adapter();
if (part == "coupon") { if (printing) translate([0,0,12]) coupon(); else coupon(); }
