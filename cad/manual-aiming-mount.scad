// PETAL manual aiming adapter, prototype revision 1. Millimetres.
// Use with a PURCHASED metal geared head (Manfrotto 410 reference).
// This file does not model or print the head's gears, bearings or quick-release latch.
// Dish-local coordinates: XY is hub plane; +Z toward reflector/feed.
// Hub-ring front z=0 contacts the existing rear hub's flat face.
part="assembly"; // [assembly,hub-ring,cradle,bench-plate]
printing=false;
$fn=96;
module ring(){difference(){cylinder(d=94,h=10);cylinder(d=34,h=11);for(a=[45:90:359])rotate([0,0,a])translate([30,0,-1])cylinder(d=4.6,h=12);}}
module hub_ring(){translate([0,0,-10])ring();}
module cradle(){difference(){union(){translate([0,0,-20])ring();for(s=[-1,1])intersection(){hull(){translate([s*34,-28,-15])sphere(r=10,$fn=32);translate([s*36,-86,-48])sphere(r=10,$fn=32);translate([s*36,-91,-74])sphere(r=9,$fn=32);}translate([-80,-130,-100])cube([160,200,90]);}translate([-50,-100,-85])cube([100,16,90]);}
 // Keep the center passage open along the entire axis, including the side supports.
 translate([0,0,-100])cylinder(d=34,h=120);
 // Recut the four hub bores after unioning ribs; continuous M4 hardware clamps both pieces.
 for(a=[45:90:359])rotate([0,0,a])translate([30,0,-45])cylinder(d=4.6,h=50);
 // Metal 3/8-16 nut pocket, accessible from above the horizontal foot.
 translate([0,-101,-40])rotate([-90,0,0])cylinder(d=9.9,h=19);
 translate([0,-92.5,-40])rotate([-90,0,0])cylinder(d=14.8/cos(30),h=9,$fn=6);
 }}
module bench_plate(){difference(){translate([-75,-75,0])cube([150,150,6]);translate([0,0,-1])cylinder(d=9.9,h=8);for(x=[-60,60],y=[-60,60])translate([x,y,-1])cylinder(d=8.6,h=8);}}
if(part=="hub-ring"){if(printing)ring();else hub_ring();}
if(part=="cradle"){if(printing)translate([0,0,100])rotate([90,0,0])cradle();else cradle();}
if(part=="bench-plate")bench_plate();
if(part=="assembly"){
 color("slategray")hub_ring();color("steelblue")cradle();
 // Ghost envelope is NOT a supplied part or accurate manufacturer CAD.
 %color([.7,.7,.7,.2])translate([-42,-195,-82])cube([84,95,84]);
 color("gray")translate([0,-201,-40])rotate([90,0,0])bench_plate();
}
