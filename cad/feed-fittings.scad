// PETAL support fittings, mm. Smooth 1/4 inch (6.35 mm) metal rod.
// Positive Z points from a rod-end pivot into its rod socket.
part="clevis"; // [clevis,rod-end,carrier-3,carrier-4]
$fn=48;
module bore_y(d,h){rotate([90,0,0]) cylinder(d=d,h=h,center=true);}
module clevis(){difference(){union(){translate([-20,-15,0])cube([40,30,6]);for(y=[-7.6,7.6])hull(){translate([-8,y-2.5,5])cube([16,5,1]);translate([0,y,22])bore_y(16,5);}}for(x=[-14,14])translate([x,0,-1])cylinder(d=4.6,h=9);translate([0,0,22])bore_y(5.4,40);}}
module rod_end(){difference(){union(){hull(){bore_y(18,9.6);translate([-6,-4.8,9])cube([12,9.6,8]);}translate([0,0,16])cylinder(d=16,h=28);translate([6,-7,27])cube([13,14,10]);}bore_y(5.4,25);translate([0,0,16])cylinder(d=6.7,h=30);translate([0,-.6,22])cube([22,1.2,24]);translate([12,0,32])bore_y(4.5,22);translate([12,7.01,32])rotate([90,0,0])cylinder(d=7.5/cos(30),h=3.3,$fn=6);}}
module carrier(n){difference(){union(){cylinder(d=80,h=6);for(a=[0:360/n:359])rotate([0,0,a])translate([27,-15,0])cube([60,30,6]);}translate([0,0,-1])cylinder(d=40,h=8);for(a=[0:360/n:359])rotate([0,0,a])for(r=[51,79])translate([r,0,-1])cylinder(d=4.6,h=8);for(a=[45:90:359])rotate([0,0,a])translate([28,0,-1])cylinder(d=4.6,h=8);}}
if(part=="clevis")clevis();
if(part=="rod-end")rod_end();
if(part=="carrier-3")carrier(3);
if(part=="carrier-4")carrier(4);
