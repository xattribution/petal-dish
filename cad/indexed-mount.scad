// PETAL indexed alt-az mount, revision 1. Millimeters. Fully printed (ASA-GF), steel pins/bolts.
// Three parts: column (bench stand), head (azimuth turntable + elevation cheeks),
// cradle (bolts to the hub's four M4 mount inserts on the 60 mm BCD).
// Both axes lock with one Ø5 steel pin through a vernier pair of hole rings:
// the coarse ring has holes every P degrees, the fine ring every P-step degrees,
// so exactly one hole pair lines up at every multiple of `step`.
// Frame: Z up, +Y = dish boresight at az 0 / el 0, X = elevation axle. Axle at cradle origin.

part = "assembly";   // [assembly,cradle,head,column]
printing = false;    // true = export orientation (flat on bed)
step = 1;            // [1,2,3] smallest angle increment in degrees
dish_d = 400;        // PETAL diameter; sets column height for rim clearance
dish_fd = 0.42;      // PETAL f/D
el_min = -5;         // lowest elevation the rim must clear the bench at
el = 30;             // assembly preview elevation
az = 0;              // assembly preview azimuth (clockwise from above)
pin_d = 5;           // steel index pin
pin_fit = 0.3;       // printed hole oversize; drill/ream to a slip fit
engrave = 0.6;
$fn = 72;

// ---------- vernier ----------
P  = step == 3 ? 15 : 10;   // coarse pitch
V  = P - step;              // fine pitch
NV = P / step;              // fine holes; hole k locks angles ≡ k*step (mod P)
W0 = step == 1 ? 180 : step == 2 ? 150 : 165;  // elevation fine ring start (≡0 mod P), behind/above axle
B0 = step == 1 ? 140 : step == 2 ? 160 : 150;  // azimuth fine ring start (≡0 mod P), rear sector
function el_fine(k) = W0 - V*k;   // world angle in (Y,Z) from +Y toward +Z, on cheeks
function az_fine(k) = B0 + V*k;   // compass angle on head disk, clockwise from +Y

// ---------- interfaces ----------
hole = pin_d + pin_fit;
m8 = 8.4; m4 = 4.5;
L = 100;                        // axle to hub rear face (dish back clears the column flange to el -5)
plate_t = 10; plate_r = 47;     // hub contact ring stays inside root-screw heads (r >= 48)
bcd_r = 30; port_d = 34;
ear_in = 28; ear_t = 12; ear_out = ear_in + ear_t;
gap = 0.4;
cheek_in = ear_out + gap; cheek_t = 12; cheek_out = cheek_in + cheek_t;
Ri = 50; ear_R = 68; cheek_R = 58;   // elevation pin ring / outer radii
Ha = 78;                             // head disk top to axle
head_t = 12; head_R = 78; Ra = 70;   // azimuth disk and pin ring
flange_t = 14; flange_R = 92; tube_R = 30; foot_R = 98; foot_t = 10;

// ---------- dish envelope (PETAL generator: hub back = zAt(45) - 16 from vertex) ----------
focal = dish_d * dish_fd;
function zAt(r) = r*r/(4*focal);
vertex_u = L + 16 - zAt(45);
rim_u = vertex_u + zAt(dish_d/2);
rim_drop = dish_d/2*cos(el_min) - rim_u*sin(el_min);
axle_h = rim_drop + 15;
Hc = max(110, axle_h - Ha - head_t);  // column height (bench to azimuth face)

echo(str("PETAL-MOUNT step=",step," P=",P," V=",V," NV=",NV," column=",Hc," axle_height=",Hc+head_t+Ha));
for (k=[0:NV-1]) echo(str("EL_FINE k=",k," angle=",el_fine(k)," label=",k*step));
for (k=[0:NV-1]) echo(str("AZ_FINE k=",k," angle=",az_fine(k)," label=",k*step));

// ---------- helpers ----------
// 2D (u=Y, v=Z) profile extruded along X from x0 for t.
module uv(x0, t) multmatrix([[0,0,1,x0],[1,0,0,0],[0,1,0,0]]) linear_extrude(t) children();
module at(r, a) translate([r*cos(a), r*sin(a)]) children();      // math angle in uv plane
module atc(r, a) translate([r*sin(a), r*cos(a)]) children();     // compass angle in XY
module lbl(t, sz) text(t, size=sz, font="DejaVu Sans:style=Bold", halign="center", valign="center");
// Text readable from outside a +X face is drawn as-is in uv; a -X face mirrors u.
module face(side) if (side > 0) children(); else mirror([1,0]) children();
module ring_label(t, r, a, sz) at(r, a) rotate(a - 90) lbl(t, sz);
module tick(r0, r1, a, w=0.8) rotate(a) translate([r0, -w/2]) square([r1 - r0, w]);

// ---------- cradle ----------
module ear_profile() hull() { circle(ear_R); translate([L - 10, -16]) square([10, 32]); }
module ear(side) {
  x0 = side > 0 ? ear_in : -ear_out;
  difference() {
    uv(x0, ear_t) difference() {
      ear_profile();
      circle(d=m8, $fn=40);
      for (j=[0:360/P-1]) at(Ri, P*j) circle(d=hole, $fn=32);
    }
    // coarse elevation scale on the outer face, read at the cheek's top notch (world 90°)
    xf = side > 0 ? ear_out - engrave : -ear_out - 1;
    uv(xf, engrave + 1) face(side) {
      for (t=[floor(-10/P)*P : P : 100]) {
        a = side > 0 ? 90 - t : 180 - (90 - t);
        tick(cheek_R + 0.6, cheek_R + 3.2, a);
        ring_label(str(t), cheek_R + 6.4, a, 3.4);
      }
      for (t=[floor(-10/P)*P + P/2 : P : 100]) {
        a = side > 0 ? 90 - t : 180 - (90 - t);
        tick(cheek_R + 0.6, cheek_R + 1.9, a, 0.6);
      }
    }
  }
}
module cradle() difference() {
  union() {
    translate([0, L - plate_t, 0]) rotate([-90,0,0]) cylinder(r=plate_r, h=plate_t, $fn=120);
    ear(1); ear(-1);
  }
  translate([0, L - plate_t - 1, 0]) rotate([-90,0,0]) cylinder(d=port_d, h=plate_t + 2, $fn=96);
  for (a=[45:90:359]) translate([bcd_r*cos(a), L - plate_t - 1, bcd_r*sin(a)]) rotate([-90,0,0]) cylinder(d=m4, h=plate_t + 2, $fn=28);
}

// ---------- head ----------
module cheek_profile() hull() { circle(cheek_R); translate([-38, -Ha - 1]) square([76, 1]); }
module cheek(side) {
  x0 = side > 0 ? cheek_in : -cheek_out;
  difference() {
    uv(x0, cheek_t) translate([0, Ha]) difference() {
      cheek_profile();
      circle(d=m8, $fn=40);
      for (k=[0:NV-1]) at(Ri, el_fine(k)) circle(d=hole, $fn=32);
      at(cheek_R, 90) polygon([[-2.6, 0.5], [2.6, 0.5], [0, -3.2]]);   // pointer notch
    }
    xf = side > 0 ? cheek_out - engrave : -cheek_out - 1;
    uv(xf, engrave + 1) translate([0, Ha]) face(side) {
      for (k=[0:NV-1]) {
        a = side > 0 ? el_fine(k) : 180 - el_fine(k);
        ring_label(str(k*step), Ri - 9, a, 3.6);
      }
      tick(cheek_R - 7, cheek_R - 3, 90, 1);
    }
  }
}
module cheek_fillet(side) {
  x_in = side > 0 ? cheek_in : -cheek_out;
  hull() {
    translate([x_in - (side > 0 ? 3 : 7), -38, 0]) cube([cheek_t + 10, 76, 0.01]);
    translate([x_in, -38, 5]) cube([cheek_t, 76, 0.01]);
  }
}
module head() difference() {
  union() {
    translate([0,0,-head_t]) cylinder(r=head_R, h=head_t, $fn=160);
    cheek(1); cheek(-1);
    intersection() {
      union() { cheek_fillet(1); cheek_fillet(-1); }
      translate([0,0,-1]) cylinder(r=head_R, h=8, $fn=160);
    }
  }
  translate([0,0,-head_t-1]) cylinder(d=m8, h=head_t + 2, $fn=40);
  for (k=[0:NV-1]) atc(Ra, az_fine(k)) translate([0,0,-head_t-1]) cylinder(d=hole, h=head_t + 2, $fn=32);
  translate([0,0,-engrave]) linear_extrude(engrave + 1) {
    for (k=[0:NV-1]) atc(Ra - 9, az_fine(k)) rotate(-az_fine(k)) lbl(str(k*step), 3.8);
    tick(head_R - 6, head_R + 1, 90, 1.2);                              // azimuth pointer (front)
  }
  translate([0, head_R, -head_t - 1]) rotate(45) translate([-2.2,-2.2,0]) cube([4.4, 4.4, head_t + 2]);
}

// ---------- column ----------
module column() difference() {
  union() {
    cylinder(r=foot_R, h=foot_t, $fn=160);
    translate([0,0,foot_t]) cylinder(r1=70, r2=tube_R, h=40, $fn=120);
    cylinder(r=tube_R, h=Hc, $fn=96);
    translate([0,0,Hc - flange_t - (flange_R - tube_R)]) cylinder(r1=tube_R, r2=flange_R, h=flange_R - tube_R, $fn=160);
    translate([0,0,Hc - flange_t]) cylinder(r=flange_R, h=flange_t, $fn=160);
  }
  // M8 hex-head bolt drops up the hex bore; its head stops against an 8 mm roof.
  translate([0,0,-1]) cylinder(r=13.4/2/cos(30), h=Hc - 8 + 1, $fn=6);
  translate([0,0,Hc - 9]) cylinder(d=m8, h=10, $fn=40);
  for (j=[0:360/P-1]) atc(Ra, P*j) translate([0,0,Hc - 70]) cylinder(d=hole, h=71, $fn=32);
  for (a=[45:90:359]) atc(86, a) translate([0,0,-1]) cylinder(d=5.5, h=foot_t + 2, $fn=28);
  translate([0,0,Hc - engrave]) linear_extrude(engrave + 1) {
    for (j=[0:360/P-1]) {
      a = P*j;
      rotate(-a) translate([-0.4, head_R + 1]) square([0.8, 3]);
      atc(head_R + 8.5, a) rotate(-a) lbl(str(a), 3.6);
    }
  }
}

// ---------- output ----------
if (part == "cradle") { if (printing) translate([0,0,L]) rotate([-90,0,0]) cradle(); else cradle(); }
if (part == "head")   { if (printing) translate([0,0,head_t]) head(); else head(); }
if (part == "column") column();
if (part == "assembly") {
  color("dimgray") column();
  translate([0,0,Hc + head_t]) rotate([0,0,-az]) {
    color("steelblue") head();
    translate([0,0,Ha]) rotate([el,0,0]) {
      color("orange") cradle();
      // PETAL envelope (ghost): hub + shell, for clearance only
      %translate([0, vertex_u, 0]) rotate([-90,0,0])
        rotate_extrude($fn=96) polygon(concat([[0, zAt(45) - 16], [60, zAt(45) - 16]],
          [for (r=[60:20:dish_d/2]) [r, zAt(r) - 17]],
          [for (r=[dish_d/2:-20:0]) [r, zAt(r)]]));
    }
  }
}
