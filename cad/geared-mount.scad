// PETAL geared alt-az head, revision 3. Millimeters. Printed in ASA-GF; only bolts, nuts and inserts are bought.
// One printed worm per axis turns a printed gear: 1 knob turn = 6°, self-locking, so the dish stays where you leave it.
// Both worms are the same part. The head is a module: its base plate has a 4 x M5 pattern that takes
// the printed stand, a 1-1/4" pipe-mast clamp, or a 3/8-16 tripod puck.
// Print rules: nothing steeper than 45° from vertical and no bridges. Gear teeth use a 45° pressure
// angle so the worm prints upright and the ear's teeth print sideways without overhangs.
// Frame: Z up, +Y = boresight at az 0 / el 0, X = elevation axle. Compass azimuth runs clockwise from +Y.

part = "assembly";   // [assembly,base,head,cradle,worm,stand,pipe-adapter,tripod-puck]
printing = false;
dish_d = 400; dish_fd = 0.42; el_min = -5;
el = 30; az = 0;            // assembly preview pose
engrave = 0.6;
$fn = 72;

// ---------- gear set (shared by both axes) ----------
gm = 2;  gN = 60;  gpa = 48;          // module, teeth per full circle, pressure angle (48° keeps every flank ≤ 42° from vertical, helix included)
g_rp = gN*gm/2;                       // 60
g_ad = 0.5*gm; g_de = 0.7*gm;         // stub teeth: a 45° tooth is pointed above ~0.7 m
g_bl = 0.15;                          // backlash per side (arc at pitch)
w_r1 = 8;                             // worm pitch radius
w_p = PI*gm;                          // axial pitch = lead (single start)
lam = atan(w_p/(2*PI*w_r1));          // lead angle, ~7.1°: self-locks with plastic friction
g_a = g_rp + w_r1;                    // center distance 68
deg_per_turn = 360/gN;                // 6
// worm stations along its axis s (0 = knob face). The collar and knob sit far enough from the
// thread that they clear the gear teeth: a point on the worm axis t mm from the thread center is
// sqrt(g_a² + t²) from the gear axis.
knob_d = 28; knob_t = 8;
bore_d = 19.5;                        // worm chamber / collar journal
s_collar1 = 21;                       // collar journal 8..21
s_thread0 = 30;                       // neck 21..30 at root radius
w_len = 24;
s_thread1 = s_thread0 + w_len;        // 54
s_c = (s_thread0 + s_thread1)/2;      // 42 = thread center
s_floor = s_thread1 + 0.3;            // worm end bears on the bore floor
s_nut0 = 62;                          // nylock pocket 62..70.6, clear of the gear window
lead_el = 1; lead_az = 1;             // lead-tilt directions (set by mesh check)

// ---------- interfaces ----------
m8 = 8.4; m4 = 4.5; m5_ins = 6.4; efoot = 0.6;
L = 100;                              // axle to hub rear face
plate_t = 10; plate_r = 47; bcd_r = 30; port_d = 34;
ear_in = 28; ear_t = 12; ear_out = ear_in + ear_t;
gap = 0.4;
cheek_in = ear_out + gap; cheek_t = 12; cheek_out = cheek_in + cheek_t;
band_top = 24;                        // head: gear teeth Z 0..12, scale band Z 12..24
band_r = g_rp - g_de - 0.6;           // 58.0, inside the gear root
Ha = 90;                              // band top to elevation axle
Zax = band_top + Ha;
base_t = 10;
psi_w = 230;                          // elevation worm position about the axle; its axis sits 50° from vertical so axis-square faces lean 40°
el_lo = -5; el_hi = 100; stop_pad = 3;
iface = 25;                           // 4 x M5 interface at (±25, ±25)

// ---------- dish envelope ----------
focal = dish_d * dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);
rim_u = vertex_u + zAt(dish_d/2);
rim_drop = dish_d/2*cos(el_min) - rim_u*sin(el_min);
axle_h = rim_drop + 15;
stand_top = axle_h - Zax - base_t;    // bench to base-plate bottom

echo(str("PETAL-GEARED lead=", lam, " deg/turn=", deg_per_turn, " axle_h=", axle_h, " stand_top=", stand_top));

// ---------- math helpers ----------
function Tm(v) = [[1,0,0,v[0]],[0,1,0,v[1]],[0,0,1,v[2]],[0,0,0,1]];
function Rx(a) = [[1,0,0,0],[0,cos(a),-sin(a),0],[0,sin(a),cos(a),0],[0,0,0,1]];
function Ry(a) = [[cos(a),0,sin(a),0],[0,1,0,0],[-sin(a),0,cos(a),0],[0,0,0,1]];
function Rz(a) = [[cos(a),-sin(a),0,0],[sin(a),cos(a),0,0],[0,0,1,0],[0,0,0,1]];
// worm frames: local z = worm axis, s=0 at the knob face, thread centered at s=31; local +y points at the gear
cw = cos(psi_w); sw = sin(psi_w);
F_el = Tm([ear_in + ear_t/2, g_a*cw, Zax + g_a*sw]) *
       [[-1,0,0,0],[0,-cw,-sw,0],[0,-sw,cw,0],[0,0,0,1]] * Ry(lead_el*lam) * Tm([0,0,-s_c]);
F_az = Tm([0, -g_a, 6]) * [[0,0,-1,0],[0,1,0,0],[1,0,0,0],[0,0,0,1]] * Ry(lead_az*lam) * Tm([0,0,-s_c]);

// ---------- 2D helpers ----------
module uv(x0, t) multmatrix([[0,0,1,x0],[1,0,0,0],[0,1,0,0]]) linear_extrude(t) children();
module at(r, a) translate([r*cos(a), r*sin(a)]) children();
module atc(r, a) translate([r*sin(a), r*cos(a)]) children();
module lbl(t, sz) text(t, size=sz, font="DejaVu Sans:style=Bold", halign="center", valign="center");
module face(side) if (side > 0) children(); else mirror([1,0]) children();
module ring_label(t, r, a, sz) at(r, a) rotate(a - 90) lbl(t, sz);
module tick(r0, r1, a, w=0.45) rotate(a) translate([r0, -w/2]) square([r1 - r0, w]);
hfn = 40;
module tdrop(d, up) union() { circle(d=d, $fn=hfn); rotate(up) polygon([[d/2*cos(45), -d/2*sin(45)], [d/sqrt(2), 0], [d/2*cos(45), d/2*sin(45)]]); }
// hex nut pocket with flats square to `up` and a 45° roof on top (horizontal-ish axes)
module hexroof(af, up) rotate(up) union() {
  R = af/sqrt(3);
  rotate(30) circle(r=R, $fn=6);
  polygon([[af/2, -R/2], [af/2 + R/2, 0], [af/2, R/2]]);
}
module entry(d) translate([0,0,-1]) cylinder(d1=d + 2*efoot + 2, d2=d, h=efoot + 1, $fn=hfn);
module rim_text(t, a, R, z, sz) rotate([0,0,-a]) translate([0, R - engrave, z]) rotate([90,0,180]) linear_extrude(engrave + 1) lbl(t, sz);
module rim_tick(a, R, z0, h, w=0.45) rotate([0,0,-a]) translate([-w/2, R - engrave, z0]) cube([w, engrave + 1, h]);

// ---------- gears ----------
function inv(a) = tan(a) - a*PI/180;
function g_psi(r) = let(rb = g_rp*cos(gpa), ar = acos(min(1, rb/max(r, rb))))
  ((PI*gm/2 - g_bl)/(2*g_rp) + inv(gpa) - inv(ar)) * 180/PI;
module gear2d(i0=0, i1=gN-1) union() {
  rf = g_rp - g_de; ra = g_rp + g_ad;
  circle(r=rf, $fn=gN*4);
  for (i=[i0:i1]) let(c = i*360/gN, rs = [for (k=[0:8]) rf - 0.4 + (ra - rf + 0.4)*k/8])
    polygon(concat([for (r=rs) [r*cos(c - g_psi(r)), r*sin(c - g_psi(r))]],
                   [for (k=[8:-1:0]) let(r=rs[k]) [r*cos(c + g_psi(r)), r*sin(c + g_psi(r))]]));
}
// worm cross-section: 45° flanks on an axial rack of pitch w_p
function w_rad(a) = let(s = w_p*a/360, d = abs(((s + w_p/2) % w_p + w_p) % w_p - w_p/2))
  min(w_r1 + g_ad, max(w_r1 - g_de, w_r1 + (w_p/4 - g_bl/2 - d)/tan(gpa)));
module worm2d() polygon([for (a=[0:2:358]) [w_rad(a)*cos(a), w_rad(a)*sin(a)]]);

// ---------- worm (both axes) ----------
module worm() difference() {
  union() {
    difference() {
      cylinder(d=knob_d, h=knob_t, $fn=96);
      for (i=[0:15]) rotate(i*22.5) translate([knob_d/2 + 0.6, 0, -1]) cylinder(d=3.2, h=knob_t + 2, $fn=24);
    }
    translate([0,0,knob_t - 0.01]) cylinder(d=bore_d - 0.6, h=s_collar1 - knob_t + 0.02, $fn=96);
    translate([0,0,s_collar1 - 0.01]) cylinder(r=w_r1 - g_de, h=s_thread0 - s_collar1 + 0.01, $fn=180);
    intersection() {   // thread run-in chamfered at 45° so it never starts with a ledge
      translate([0,0,s_thread0]) linear_extrude(w_len, twist=-360*w_len/w_p, slices=round(w_len/w_p*180), convexity=10) worm2d();
      translate([0,0,s_thread0]) cylinder(r1=w_r1 - g_de, r2=w_r1 - g_de + 40, h=40, $fn=180);
    }
  }
  translate([0,0,-1]) cylinder(d=m8, h=s_thread1 + 2, $fn=hfn);
  entry(m8);
  // 12 ticks = 0.5° each; long ticks every 1°
  for (i=[0:11]) rotate([0,0,i*30]) translate([knob_d/2 - engrave, -0.3, i % 2 ? 4.5 : 2]) cube([engrave + 1, 0.6, i % 2 ? 3.5 : 6]);
}

// ---------- cradle ----------
el_t0 = ceil((psi_w - el_hi - stop_pad - 9)/deg_per_turn);
el_t1 = floor((psi_w - el_lo + stop_pad + 9)/deg_per_turn);
module ear_profile() hull() { circle(r=g_rp - g_de, $fn=gN*4); translate([L - 10, -16]) square([10, 32]); }
module stop_lug(body_a) {   // corbel lug on the right ear's outer face; it strikes the cheek post at the travel ends
  rect = [[-4, 34], [4, 34], [4, 42], [-4, 42]];
  hull() {
    uv(ear_out - 0.01, 0.01) rotate(body_a - 90) polygon(rect);
    uv(ear_out + lug_x, 0.01) translate([-lug_x, 0]) rotate(body_a - 90) polygon(rect);
  }
}
module ear(side) {
  x0 = side > 0 ? ear_in : -ear_out;
  difference() {
    uv(x0, ear_t) difference() {
      union() { ear_profile(); if (side > 0) gear2d(el_t0, el_t1); }
      tdrop(m8, 180);
    }
    xf = side > 0 ? ear_out - engrave : -ear_out - 1;
    uv(xf, engrave + 1) face(side) {
      for (t=[-10:1:105]) let(a = side > 0 ? 90 - t : 90 + t)
        tick(51.5, t % 5 == 0 ? 57 : 54, a);
      for (t=[-10:10:100]) let(a = side > 0 ? 90 - t : 90 + t) ring_label(str(t), 46.5, a, 3.4);
    }
  }
}
// lugs: low stop at el_lo - stop_pad, high stop at el_hi + stop_pad (cheek post edges at world 270 ± post_a)
post_a = 33;
module cradle() difference() {
  union() {
    translate([0, L - plate_t, 0]) rotate([-90,0,0]) {
      cylinder(r=plate_r, h=plate_t - efoot, $fn=120);
      translate([0,0,plate_t - efoot]) cylinder(r1=plate_r, r2=plate_r - efoot, h=efoot, $fn=120);
    }
    ear(1); ear(-1);
    stop_lug(337);
    stop_lug(114);
  }
  translate([0, L - plate_t - 1, 0]) rotate([-90,0,0]) cylinder(d=port_d, h=plate_t + 2, $fn=hfn);
  for (a=[45:90:359]) translate([bcd_r*cos(a), L - plate_t - 1, bcd_r*sin(a)]) rotate([-90,0,0]) cylinder(d=m4, h=plate_t + 2, $fn=hfn);
  for (c=[[0,0,port_d], [bcd_r*cos(45), bcd_r*sin(45), m4], [bcd_r*cos(135), bcd_r*sin(135), m4],
          [bcd_r*cos(225), bcd_r*sin(225), m4], [bcd_r*cos(315), bcd_r*sin(315), m4]])
    translate([c[0], L, c[1]]) rotate([90,0,0]) entry(c[2]);
}

// ---------- head ----------
module cheek_profile() hull() { circle(26); translate([-20, -Ha]) square([40, 1]); }
lug_x = 3.6;   // stop-lug protrusion past the ear face
module cheek(side) {
  x0 = side > 0 ? cheek_in : -cheek_out;
  uv(x0, cheek_t) translate([0, Zax]) difference() { cheek_profile(); tdrop(m8, 90); }
  // pointer blade on the outer part of the cheek, clear of the stop lugs' path
  uv(side > 0 ? cheek_in + lug_x : -cheek_out, cheek_t - lug_x) translate([0, Zax])
    polygon([[-3, 20], [3, 20], [3, 47.5], [0, 50.5], [-3, 47.5]]);
}
module worm_bore() {   // elevation housing, worm-frame coordinates (z = s); world-up projects onto local +y
  translate([0,0,-20]) linear_extrude(20 + knob_t) tdrop(knob_d + 3, 90);   // knob clearance, square to the worm
  translate([0,0,-1]) linear_extrude(s_floor + 1) tdrop(bore_d, 90);
  translate([0,0,s_nut0]) rotate(90) cylinder(r=13.4/sqrt(3), h=8.6, $fn=6);  // vertex toward +y
  translate([0,0,s_floor - 0.01]) linear_extrude(s_nut0 + 16 - s_floor) tdrop(m8, 90);
}
module el_housing() difference() {
  hull() {
    translate([21, -33, band_top - 0.01]) cube([26, 13, 0.01]);
    multmatrix(F_el) translate([0,0,knob_t]) cylinder(r=13, h=s_nut0 + 16 - knob_t, $fn=64);
  }
  multmatrix(F_el) worm_bore();
}
module head() difference() {
  union() {
    linear_extrude(12) gear2d();
    translate([0,0,12 - 0.01]) cylinder(r=band_r, h=band_top - 12 + 0.01, $fn=180);
    cheek(1); cheek(-1);
    el_housing();
  }
  // gear-mesh window for the ear sector
  translate([ear_in - 0.6, 0, Zax]) rotate([0,90,0]) cylinder(r=g_rp + g_ad + 1.0, h=ear_t + 0.6 + gap, $fn=180);
  multmatrix(F_el) worm_bore();
  translate([0,0,-1]) cylinder(d=m8, h=band_top + 2, $fn=hfn);
  entry(m8);
  // azimuth scale on the band rim, read at the base pointer (compass 210)
  for (A=[0:359]) let(a = 210 - A) rim_tick(a, band_r, band_top - (A % 5 == 0 ? 6.5 : 4), A % 5 == 0 ? 6.5 : 4);
  for (A=[0:10:350]) rim_text(str(A), 210 - A, band_r, 15, 3.6);
}

// ---------- base (fixed; carries the azimuth worm and the interface) ----------
module az_window() {
  translate([0,0,-0.01]) cylinder(r=g_rp + g_ad + 0.5, h=12.01, $fn=180);
  translate([0,0,12]) cylinder(r1=g_rp + g_ad + 0.5, r2=band_r + 1, h=g_rp + g_ad - band_r - 0.5, $fn=180);
  cylinder(r=band_r + 1, h=60, $fn=180);
}
module base() difference() {
  union() {
    translate([0,0,-base_t]) cylinder(r=56, h=base_t, $fn=160);
    translate([-38, -85, -base_t]) cube([38 + s_c - knob_t, 31, base_t + 23]);
    // azimuth pointer fin at compass 210
    hull() { translate([-38, -56, -base_t]) cube([6, 2, base_t + 23]); translate([0,0,-base_t]) atc(band_r + 1.2, 210) cylinder(r=0.3, h=base_t + 23, $fn=8); }
  }
  az_window();
  multmatrix(F_az) {
    translate([0,0,-20]) linear_extrude(20 + knob_t) tdrop(knob_d + 3, 0);
    translate([0,0,-1]) linear_extrude(s_floor + 1) tdrop(bore_d, 0);
    translate([0,0,s_nut0]) linear_extrude(8.6) hexroof(13.4, 0);
    translate([0,0,s_floor - 0.01]) linear_extrude(s_nut0 + 16 - s_floor) tdrop(m8, 0);
  }
  // pivot bolt: M8 flat-head (90° countersunk) from below; the seat is a 45° cone
  translate([0,0,-base_t - 0.01]) cylinder(d1=16.4, d2=m8, h=(16.4 - m8)/2, $fn=hfn);
  translate([0,0,-base_t - 1]) cylinder(d=m8, h=base_t + 2, $fn=hfn);
  // 4 x M5 heat-set inserts (interface)
  for (x=[-iface, iface], y=[-iface, iface]) translate([x, y, -base_t]) {
    translate([0,0,-1]) cylinder(d=m5_ins, h=8 + 1, $fn=hfn);
    translate([0,0,8]) cylinder(d1=m5_ins, d2=0, h=m5_ins/2, $fn=hfn);   // drill-point top, no ceiling
    entry(m5_ins);
  }
}

// ---------- mounts (bolt to the base's 4 x M5 pattern with flat-head screws from below) ----------
// M5 flat-head from below: 90° countersink seated in a plate of thickness t whose top is z_top
module iface_csk(z_top, t) for (x=[-iface, iface], y=[-iface, iface]) translate([x, y, 0]) {
  translate([0,0,z_top - t]) cylinder(d=5.5, h=t + 1, $fn=hfn);
  translate([0,0,z_top - t]) cylinder(d1=11, d2=5.5, h=2.75, $fn=hfn);
  translate([0,0,z_top - t - 60]) cylinder(d=11, h=60, $fn=hfn);
}
module stand() difference() {
  H = stand_top;
  union() {
    cylinder(r=75, h=8, $fn=160);
    cylinder(r=22, h=H, $fn=96);
    translate([0,0,H - 8 - 23]) cylinder(r1=22, r2=45, h=23, $fn=120);
    translate([0,0,H - 8]) cylinder(r=45, h=8, $fn=120);
    for (a=[0:90:359]) rotate(a) hull() {
      translate([20, -3, 0]) cube([55, 6, 8]);
      translate([20, -3, 0]) cube([2, 6, 8 + 53]);
    }
  }
  translate([0,0,-1]) cylinder(d=30, h=H - 30, $fn=64);
  translate([0,0,H - 31]) cylinder(d1=30, d2=0, h=15, $fn=64);
  iface_csk(H, 8);
  for (a=[45:90:359]) rotate(a) translate([62, 0, 0]) {   // bench screws, countersunk from the top
    translate([0,0,-1]) cylinder(d=5.5, h=10, $fn=hfn);
    translate([0,0,8 - 2.75]) cylinder(d1=5.5, d2=11, h=2.76, $fn=hfn);
  }
}
module pipe_adapter() difference() {   // in use: plate top at Z 0, socket hangs below for 1-1/4" NPS (42.2 OD)
  union() {
    translate([0,0,-8]) cylinder(r=45, h=8, $fn=120);
    translate([0,0,-58]) cylinder(r=27.3, h=50.01, $fn=120);
    translate([-12, 24, -58]) cube([24, 16, 50]);
  }
  translate([0,0,-59]) cylinder(d=42.8, h=51, $fn=180);
  translate([-1.5, 15, -59]) cube([3, 30, 51]);
  // pinch bolts along X; print-up is -Z here, which rotate([0,90,0]) maps to the 2D +x direction
  for (z=[-45, -21]) translate([-20, 33, z]) rotate([0,90,0]) {
    linear_extrude(40) tdrop(5.5, 0);
    translate([0,0,7]) linear_extrude(7) hexroof(8.3, 0);
  }
  iface_csk(0, 8);
}
module tripod_puck() difference() {    // in use: top at Z 0; 3/8-16 nut trapped under the base plate
  translate([0,0,-14]) cylinder(r=45, h=14, $fn=120);
  translate([0,0,-9]) cylinder(d=14.5/cos(30), h=10, $fn=6);
  translate([0,0,-15]) cylinder(d=10, h=7, $fn=hfn);
  iface_csk(0, 14);
}

// ---------- output ----------
if (part == "worm") worm();
if (part == "cradle") { if (printing) translate([0,0,L]) rotate([-90,0,0]) cradle(); else cradle(); }
if (part == "head") head();
if (part == "base") { if (printing) translate([0,0,base_t]) base(); else base(); }
if (part == "stand") stand();
if (part == "pipe-adapter") { if (printing) rotate([180,0,0]) pipe_adapter(); else pipe_adapter(); }
if (part == "tripod-puck") { if (printing) translate([0,0,14]) tripod_puck(); else tripod_puck(); }
if (part == "assembly") {
  color("dimgray") translate([0,0,-base_t - stand_top]) stand();
  color("slategray") base();
  color("gold") multmatrix(F_az) worm();
  rotate([0,0,-az]) {
    color("steelblue") head();
    color("gold") multmatrix(F_el) worm();
    translate([0,0,Zax]) rotate([el,0,0]) {
      color("orange") cradle();
      %translate([0, vertex_u, 0]) rotate([-90,0,0])
        rotate_extrude($fn=96) polygon(concat([[0, zAt(45) - 16], [60, zAt(45) - 16]],
          [for (r=[60:20:dish_d/2]) [r, zAt(r) - 17]], [for (r=[dish_d/2:-20:0]) [r, zAt(r)]]));
    }
  }
}
