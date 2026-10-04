// PETAL simple alt-az mount. Millimeters. One clamp per axis; the two arms are bolted on with heat-set inserts.
//   base    - flat disc on your stand; captures the azimuth nut, carries the azimuth scale on its rim (optional)
//   yoke    - turntable plate on the base (azimuth); without the base it screws straight onto the stand
//   upright - elevation arm; M4 flat-heads come up through the yoke plate into its inserts
//   cradle  - hub plate (Ø94 × 12, Ø34 port, 4 × M4 on a 60 mm circle)
//   cheek   - elevation arm on the cradle; M4 flat-heads from the hub face go into its inserts
// Each axis: two flat faces, one M8 bolt. Azimuth: hex key from above. Elevation: one wing nut,
// plus an optional M6 bolt in an arc slot (arc_lock).
// Every part prints without supports in its export orientation (printing = true):
//   base   top face down            yoke   bottom (bearing) face down
//   upright clamp face (x = 40) down  cradle hub face down    cheek clamp face (x = 40) down
// Frame: Z up, +Y = boresight at az 0 / el 0, elevation axis along X. Base bottom at Z 0.

part = "assembly";  // [assembly,base,yoke,upright,cradle,cheek,matrices]
printing = false;
arc_lock = false;     // M6 arc-slot lock bolt: slot in the upright, hole + head pocket in the cheek
stand_holes = false;  // no base: 4 × M5 countersunk holes from the top of the yoke plate; assembly omits the base
dish_d = 400; dish_fd = 0.42;
el = 30; az = 0; show_dish = true;
$fn = 96;
hfn = 32;               // small holes; a multiple of 8 so teardrop roofs meet the circle on a vertex
efoot = 0.6;            // first-layer chamfer
chamf = 1.5;            // top-edge chamfer on plates

// ---------- layout (the kinematic frame is fixed: the app places the dish with these) ----------
disc_R = 58;
base_t = 14; yoke_t = 10;
yoke_z = base_t;                    // azimuth clamp face
top = yoke_z + yoke_t;              // yoke plate top = upright foot
H = 70;                             // elevation axis above the yoke plate
Z_el = yoke_z + yoke_t + H;         // 94
L = 75;                             // hub rear face ahead of the elevation axis
cap_r = 40;                         // upright cap radius around the elevation axis (holds the arc slot)
cr_in = 26; cr_out = 40;            // cheek x 26..40 (clear of the hub M4 heads, inside the plate)
ch_r = 32;                          // cheek clamp disc radius: about 8 N·m of hand-tight friction hold, several times a 400 mm dish's moment
up_in = 40; up_out = 60;            // upright x 40..60; clamp face at x = 40
up_half = 44;                       // upright foot half-width (Y)
foot_out = 76; foot_h = 14;         // outer foot flange x 60..76, 14 tall at its edge, 45° up into the upright
lobe_x = 80; lobe_y = 48;           // yoke plate extends past the disc to carry the upright foot
shoulder = 8; sh_gap = 0.2;         // locating shoulder on the yoke plate along the upright's inner face
plate_t = 12; plate_r = 47; bcd_r = 30; port_d = 34; m4 = 4.5;
ch_z = 21;                          // cheek end face half-height: its corners (x 40, z ±21) sit inside the plate's flat rear face (r 45.5)
m8 = 8.5; m8_af = 13.3; m8_nut = 7; m8_head = 5.5;

// joints: M4 heat-set inserts (≤ 6 mm long) in Ø5.6 × 8 blind pilots, M4 ISO 10642 flat-heads
ins_d = 5.6; ins_deep = 8; ins_len = 6;
cs4_d = 10.0;                       // 90° countersink at the face: M4 head (Ø8.96 max) sits ~0.5 below
up_screws = [[48, -32], [48, 32], [69, -32], [69, 32]];   // (x, y): two under the upright, two in the foot flange
ch_x = 33; ch_screws = [-14, 0, 14];                       // cheek: three along z at x = 33
up_screw_len = yoke_t + ins_len; ch_screw_len = plate_t + ins_len;   // flat-head length includes the head: plate + insert (16, 18)
// inset joints: each arm ends in a tenon that drops into a matching pocket in its plate, for alignment and shear.
// The pilots are deepened by the inset, so the screws above keep their length and full insert engagement.
inset = 2;                          // tenon length = pocket depth
fit = 0.2;                          // pocket clearance per side
lead = 0.5;                         // 45° lead-in on the tenon's end edges
ch_shoulder = 2;                    // the cheek tenon stands 2 mm inside its end face (not on the clamp-face side)
ch_tz = ch_z - ch_shoulder;         // tenon half-height
ch_tenon2d = [[cr_in + ch_shoulder, -ch_tz], [cr_out, -ch_tz], [cr_out, ch_tz], [cr_in + ch_shoulder, ch_tz]];   // (x, z)
// one solid wedge gusset on the cheek's inner face at the plate end, bearing on the cradle plate between the hub M4
// heads (|z| >= 16.7) and outside the Ø34 port; it prints rising from the cheek as an up-facing slope
gus_w = 20; gus_h = 8; gus_len = 24;
up_tenon2d = [[up_in, -up_half], [foot_out, -up_half], [foot_out, up_half], [up_in, up_half]];   // (x, y): the whole upright footprint
// stand holes (no base): M5 flat-head from the top of the yoke plate, away from the upright
stand_pts = [[20, -45], [20, 45], [-44, -20], [-44, 20]];
cs5_d = 12.0; m5 = 5.5;
// arc lock: M6 hex bolt, head captive in the cheek, washer + nyloc on the upright's outside
arc_r = 28; arc_phi = -20;          // hole in the cheek at r 28, 20° below the boresight direction
arc_w = 6.6; arc_margin = 1.5;      // slot width; extra degrees at each end
el_min = -10; el_max = 100;
m6_af = 10.3; m6_head = 4.5;
// M6 hex bolt from the pocket floor: rest of the cheek + upright + washer 1.6 + nyloc 8 + 2 mm, to the next 5 mm
arc_len = ceil((cr_out - cr_in - m6_head + up_out - up_in + 1.6 + 8 + 2)/5)*5;

// ---------- dish ----------
focal = dish_d*dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);

// ---------- print transforms (the app and the checks read these through part = "matrices") ----------
function Tm(v) = [[1,0,0,v[0]],[0,1,0,v[1]],[0,0,1,v[2]],[0,0,0,1]];
function Rxm(a) = [[1,0,0,0],[0,cos(a),-sin(a),0],[0,sin(a),cos(a),0],[0,0,0,1]];
function Rym(a) = [[cos(a),0,sin(a),0],[0,1,0,0],[-sin(a),0,cos(a),0],[0,0,0,1]];
M_base = Tm([0,0,base_t]) * Rxm(180);           // world frame -> print
M_yoke = Tm([0,0,-yoke_z]);                     // world frame -> print
M_upright = Tm([Z_el,0,-up_in]) * Rym(-90);     // world frame -> print (x = 40 face on the bed)
M_cradle = Tm([0,0,L]) * Rxm(-90);              // cradle frame -> print (hub face on the bed)
M_cheek = Tm([0,0,cr_out]) * Rym(90);           // cradle frame -> print (x = 40 face on the bed)

// ---------- helpers ----------
module tdrop(d, up) union() { circle(d=d, $fn=hfn); rotate(up) polygon([[d/2*cos(45), -d/2*sin(45)], [d/sqrt(2), 0], [d/2*cos(45), d/2*sin(45)]]); }
module hex(af) rotate(30) circle(r=af/sqrt(3), $fn=6);
// slab parallel to the YZ plane from x0 to x1 with a 2D profile (2D x -> Y, 2D y -> Z)
module yz(x0, x1) multmatrix([[0,0,1,x0],[1,0,0,0],[0,1,0,0],[0,0,0,1]]) linear_extrude(x1 - x0) children();
// prism along Y from an XZ profile (2D x -> X, 2D y -> Z)
module xz(w) rotate([90,0,0]) linear_extrude(w, center=true) children();
// plate from a 2D outline: first-layer chamfer on the bed side, 45° chamfer on the other
module plate(t, bed_bottom=true) hull() {
  translate([0,0,bed_bottom ? 0 : t - efoot]) linear_extrude(efoot) offset(delta=-efoot) children();
  translate([0,0,bed_bottom ? efoot : chamf]) linear_extrude(t - efoot - chamf) children();
  translate([0,0,bed_bottom ? t - chamf : 0]) linear_extrude(chamf) offset(delta=-chamf) children();
}
// convex YZ slab x0..x1 with a first-layer chamfer on the x = bed face
module yzslab(x0, x1, bed) hull() {
  if (bed == x0) { yz(x0, x0 + efoot) offset(delta=-efoot) children(); yz(x0 + efoot, x1) children(); }
  else { yz(x1 - efoot, x1) offset(delta=-efoot) children(); yz(x0, x1 - efoot) children(); }
}
// 90° countersunk through hole along +Z from a face at z = 0 (head side at z <= 0, cone opens to -Z)
module cs_hole(d, cs, depth) {
  translate([0,0,-1]) cylinder(d=d, h=depth + 1, $fn=hfn);
  translate([0,0,-1]) cylinder(d=cs, h=1.01, $fn=hfn);
  cylinder(d1=cs, d2=d - 0.01, h=(cs - d)/2 + 0.005, $fn=hfn);
}
// tenon: a 2D outline extruded inset deep (+Z from z = 0 is the arm side), with a 45° lead-in on the free end
module tenon(pts) hull() {
  translate([0,0,-inset]) linear_extrude(0.01) offset(delta=-lead) polygon(pts);
  translate([0,0,-inset + lead]) linear_extrude(inset - lead + 0.01) polygon(pts);
}
module yoke2d() hull() { circle(r=disc_R, $fn=144); translate([0, -lobe_y]) square([lobe_x, 2*lobe_y]); }
module up2d() hull() { translate([0, Z_el]) circle(r=cap_r); translate([-up_half, top]) square([2*up_half, 0.01]); }
// clamp disc around the axis tapering straight to a flat end face on the cradle plate
module cheek2d() hull() { circle(r=ch_r); translate([L - plate_t - 0.01, -ch_z]) square([0.01, 2*ch_z]); }
module arc_slot2d() {
  a0 = arc_phi + el_min - arc_margin; a1 = arc_phi + el_max + arc_margin; n = ceil((a1 - a0)/5); s = (a1 - a0)/n;
  for (i=[0:n-1]) hull() for (a=[a0 + i*s, a0 + (i+1)*s]) rotate(a) translate([arc_r, 0]) circle(d=arc_w, $fn=hfn);
}

// ---------- base (modeled upright; prints with its top face on the bed) ----------
module base() difference() {
  plate(base_t, bed_bottom=false) circle(r=disc_R, $fn=144);
  translate([0,0,-1]) rotate(30) cylinder(r=m8_af/sqrt(3), h=m8_nut + 1.5, $fn=6);   // azimuth nut
  translate([0,0,-1]) cylinder(d=m8, h=base_t + 2, $fn=hfn);
  for (a=[45:90:359]) rotate(a) translate([44, 0, base_t]) mirror([0,0,1]) cs_hole(m5, cs5_d, base_t + 1);   // 4 × M5 flat-head
  // azimuth scale: grooves down the rim every 5°, wide every 10°, read at the yoke's rear groove
  for (a=[0:5:355]) rotate(270 + a) translate([disc_R - 1, a % 10 == 0 ? -0.6 : -0.3, -1])
    cube([3, a % 10 == 0 ? 1.2 : 0.6, base_t + 2]);
}

// ---------- yoke: turntable plate ----------
module yoke() difference() {
  union() {
    translate([0,0,yoke_z]) plate(yoke_t) yoke2d();
    // locating shoulder along the upright's inner face, 45° on the inside
    translate([0,0,0]) xz(2*(up_half - 6)) polygon([[up_in - sh_gap - shoulder, top - 0.01], [up_in - sh_gap, top - 0.01], [up_in - sh_gap, top + shoulder]]);
  }
  translate([0,0,yoke_z - 1]) cylinder(d=m8, h=yoke_t + shoulder + 2, $fn=hfn);   // azimuth bolt
  translate([-0.5, -disc_R - 1, yoke_z - 1]) cube([1, 1.6, yoke_t + 2]);          // azimuth pointer groove (rear)
  translate([0,0,top - inset]) linear_extrude(inset + shoulder + 1) offset(delta=fit) polygon(up_tenon2d);   // pocket for the upright's tenon
  for (s=up_screws) translate([s[0], s[1], yoke_z]) cs_hole(m4, cs4_d, yoke_t + 1); // upright joint, heads under the plate
  if (stand_holes) for (s=stand_pts) translate([s[0], s[1], top]) mirror([0,0,1]) cs_hole(m5, cs5_d, yoke_t + 1);
}

// ---------- upright: bolts onto the yoke plate ----------
module upright() difference() {
  union() {
    yzslab(up_in, up_out, up_in) up2d();
    translate([0,0,top]) tenon(up_tenon2d);                                        // drops into the yoke plate's pocket
    intersection() {                                                             // outer foot flange
      yz(up_out - 0.01, foot_out) up2d();
      xz(200) polygon([[up_out - 0.01, top], [foot_out, top], [foot_out, top + foot_h], [up_out - 0.01, top + foot_h + foot_out - up_out + 0.01]]);
    }
  }
  translate([up_in - 1, 0, Z_el]) rotate([0,90,0]) cylinder(d=m8, h=up_out - up_in + 2, $fn=hfn);   // elevation bolt
  // inserts: pilots horizontal in print, pointed roofs toward +X (print up)
  for (s=up_screws) translate([s[0], s[1], top - inset - 0.01]) linear_extrude(ins_deep + inset + 0.01) tdrop(ins_d, 0);
  if (arc_lock) yz(up_in - 1, up_out + 1) translate([0, Z_el]) arc_slot2d();
}

// ---------- cradle: hub plate (elevation axis at the origin, el = 0; prints hub face down) ----------
module cradle() difference() {
  translate([0, L, 0]) rotate([90,0,0]) plate(plate_t) circle(r=plate_r, $fn=144);
  translate([0, L - plate_t - 1, 0]) rotate([-90,0,0]) cylinder(d=port_d, h=plate_t + 2, $fn=64);
  for (a=[45:90:359]) translate([bcd_r*cos(a), L - plate_t - 1, bcd_r*sin(a)]) rotate([-90,0,0]) cylinder(d=m4, h=plate_t + 2, $fn=hfn);
  // cheek joint: flat-heads from the hub face, heads flush (the dish hub bears on this face)
  for (z=ch_screws) translate([ch_x, L, z]) rotate([90,0,0]) cs_hole(m4, cs4_d, plate_t + 1);
  // pocket for the cheek's tenon: the tenon's end and the cheek's end face both bear on the plate
  translate([0, L - plate_t - 1, 0]) rotate([-90,0,0]) mirror([0,1,0]) linear_extrude(inset + 1) offset(delta=fit) polygon(ch_tenon2d);
}

// ---------- cheek: bolts onto the back of the cradle plate ----------
module cheek() difference() {
  union() {
    yzslab(cr_in, cr_out, cr_out) cheek2d();
    // tenon on the end face, flush with the clamp face, dropping into the cradle plate's pocket
    translate([0, L - plate_t, 0]) rotate([90,0,0]) mirror([0,1,0]) tenon(ch_tenon2d);
    // gusset: a triangle in the XY plane from the inner face (x = cr_in) to the plate face (y = L - plate_t)
    translate([0, 0, -gus_w/2]) linear_extrude(gus_w)
      polygon([[cr_in + 0.01, L - plate_t - gus_len], [cr_in + 0.01, L - plate_t], [cr_in - gus_h, L - plate_t]]);
  }
  // elevation bolt: hex head in a pocket on the inner face (opens upward in print)
  translate([cr_in - 1, 0, 0]) rotate([0,90,0]) linear_extrude(m8_head + 1) rotate(90) hex(m8_af);
  translate([cr_in - 1, 0, 0]) rotate([0,90,0]) cylinder(d=m8, h=cr_out - cr_in + 2, $fn=hfn);
  // inserts in the end face: pilots horizontal in print, pointed roofs toward -X (print up)
  for (z=ch_screws) translate([ch_x, L - plate_t + inset + 0.01, z]) rotate([90,0,0]) linear_extrude(ins_deep + inset + 0.01) tdrop(ins_d, 180);
  if (arc_lock) translate([0, arc_r*cos(arc_phi), arc_r*sin(arc_phi)]) {
    translate([cr_in - 1, 0, 0]) rotate([0,90,0]) linear_extrude(m6_head + 1) rotate(90) hex(m6_af);
    translate([cr_in - 1, 0, 0]) rotate([0,90,0]) cylinder(d=arc_w, h=cr_out - cr_in + 2, $fn=hfn);
  }
}

// ---------- preview helpers ----------
module dish() if (show_dish) color([0.82,0.84,0.88]) translate([0, vertex_u, 0]) rotate([-90,0,0])
  rotate_extrude($fn=96) polygon(concat([[0, zAt(45) - 16], [60, zAt(45) - 16]],
    [for (r=[60:20:dish_d/2]) [r, zAt(r) - 17]], [for (r=[dish_d/2:-20:0]) [r, zAt(r)]]));
module wingnut() color("silver") { cylinder(d=24, h=2, $fn=48); translate([0,0,2]) cylinder(d=13, h=7, $fn=24);
  for (s=[-1,1]) translate([s*10, 0, 6]) cube([14, 3, 10], center=true); }
module flathead(len) color("silver") { cylinder(d1=8.96, d2=4, h=2.48, $fn=24); cylinder(d=4, h=len, $fn=16); }

if (part == "assembly") {
  if (!stand_holes) color("slategray") base();
  rotate([0,0,-az]) {
    color("steelblue") yoke();
    color("cornflowerblue") upright();
    for (s=up_screws) translate([s[0], s[1], yoke_z + 0.5]) flathead(up_screw_len);
    color("silver") translate([0,0,top]) { cylinder(d=16, h=1.6); translate([0,0,1.6]) cylinder(d=13, h=8, $fn=24); }
    translate([up_out, 0, Z_el]) rotate([0,90,0]) wingnut();
    translate([0,0,Z_el]) rotate([el,0,0]) {
      color("orange") cradle();
      color("gold") cheek();
      for (z=ch_screws) translate([ch_x, L - 0.5, z]) rotate([90,0,0]) flathead(ch_screw_len);
      color("silver") translate([cr_in + 0.2, 0, 0]) rotate([0,90,0]) rotate(30) cylinder(r=13/sqrt(3), h=5.3, $fn=6);
      if (arc_lock) color("silver") translate([cr_in + 0.5, arc_r*cos(arc_phi), arc_r*sin(arc_phi)]) rotate([0,90,0]) {
        rotate(30) cylinder(r=10/sqrt(3), h=4, $fn=6); cylinder(d=6, h=arc_len, $fn=16);
        translate([0,0,up_out - cr_in - 0.5]) { cylinder(d=12, h=1.6, $fn=24); translate([0,0,1.6]) rotate(30) cylinder(r=10/sqrt(3), h=8, $fn=6); }
      }
      dish();
    }
  }
}
if (part == "base")    { if (printing) multmatrix(M_base) base(); else base(); }
if (part == "yoke")    { if (printing) multmatrix(M_yoke) yoke(); else yoke(); }
if (part == "upright") { if (printing) multmatrix(M_upright) upright(); else upright(); }
if (part == "cradle")  { if (printing) multmatrix(M_cradle) cradle(); else cradle(); }
if (part == "cheek")   { if (printing) multmatrix(M_cheek) cheek(); else cheek(); }
if (part == "matrices") {
  // print transforms (model frame -> print) and the frame constants, read by scripts/pack-mount.py and the checks
  echo(M_base = M_base); echo(M_yoke = M_yoke); echo(M_upright = M_upright); echo(M_cradle = M_cradle); echo(M_cheek = M_cheek);
  echo(FRAME = [base_t, yoke_t, Z_el, L, plate_t, cr_in, cr_out, up_in, up_out, cap_r]);
  echo(JOINT = [ins_d, ins_deep, ins_len, cs4_d, up_screw_len, ch_screw_len, len(up_screws), len(ch_screws), ch_x]);
  echo(UP_SCREWS = up_screws); echo(CH_SCREWS = ch_screws); echo(STAND = stand_pts);
  echo(INSET = [inset, fit, lead, ch_shoulder]); echo(GUSSET = [gus_w, gus_h, gus_len]);
  echo(ARC = [arc_r, arc_phi, arc_w, arc_margin, el_min, el_max, m6_af, m6_head, arc_len]);
}
