# Simple alt-az mount

![Simple mount with a 400 mm PETAL at 20° elevation, arc lock fitted](simple-mount.png)

Five printed parts and one clamp per axis. Each clamp is two flat faces squeezed by one bolt (M8 by default): loosen, aim, tighten. The two arms, the upright and the cheek, are separate parts bolted on with heat-set inserts (M4 by default). Every bolt size can be changed; see [Bolt sizes](#bolt-sizes). That lets every part print flat, with no supports.

| | |
|---|---|
| Parts | Base, yoke, upright, cradle, cheek. Without the base: four parts |
| Azimuth | 360°. The yoke turns on the base; one bolt in the center, tightened with a hex key from above |
| Elevation | −10° to 100°, checked with the dish fitted. The cheek clamps against the inside of the upright, with one wing nut. Optional arc lock: a second bolt in an arc slot |
| Scale | Azimuth grooves down the base rim every 5°, wide every 10°, read at the groove on the back of the yoke |
| Joints | Upright → yoke plate: 4 socket heads from below. Cheek → cradle plate: 3 socket heads from the hub face. All into heat-set inserts, heads in counterbores 0.5 mm below the face |
| Mounting | 4 socket heads through the base, through the yoke plate without the base, or three legs in the tripod base |
| Supports | None. The overhang scan passes for every part; inspect your slicer toolpath |

## Options

Set them in PETAL under **Aiming mount**, or in `cad/simple-mount.scad`:

| App setting | SCAD parameter | Effect |
|---|---|---|
| Azimuth base: *Printed base with azimuth scale* (default) | `stand_holes = false` | Base + yoke turntable with the azimuth clamp and the scale |
| Azimuth base: *No base · turntable bolts to the stand* | `stand_holes = true` | No base part. The yoke plate gets 4 counterbored stand screw holes (M5 by default) from its top and screws straight onto a flat stand. No azimuth clamp: turn the stand to aim in azimuth |
| Azimuth base: *Tripod leg sockets* | `base_screws = false`, plus sockets added by the app | The base without its stand screw holes, with three splayed, tapered leg sockets on its underside. See [Tripod base](#tripod-base) |
| Elevation lock: *Add arc-slot bolt* | `arc_lock = true` | An M6 bolt runs in an arc slot through the upright, from a captive head in the cheek, for extra grip. It covers the whole −10° to 100° range |
| Elevation clamp: *Right side* (default), *Left side*, *Both sides* | `sides = "right"`, `"left"`, `"both"` | Seen from behind the dish. Left is the mirror image of right. Both sides is a U: a second upright and cheek, mirrored, on the left, so the dish is clamped at two faces and nothing twists it sideways. The bolt heads stay captive inside the cheeks and the wing nuts outside. With both sides the arc lock goes on the right. See [Keeping it from slipping](#keeping-it-from-slipping) |

### Bolt sizes

| App setting | SCAD parameter | Sizes (default) | What changes |
|---|---|---|---|
| Clamp bolts | `clamp_m` | M6, **M8**, M10 | Azimuth hole and captive nut pocket in the base; elevation holes and the head pocket in the cheek. Azimuth M × 25 socket head; elevation hex bolt 40 mm (M6, M8) or 45 mm (M10) |
| Joint screws | `joint_m` | M3, **M4**, M5 | Counterbores in the yoke and cradle plates (Ø6.5 / 8 / 9.5, 3.5 / 4.5 / 5.5 deep); insert pilots in the upright and cheek (Ø4.2 / 5.6 / 6.4). Socket heads 10 mm (upright) and 12 mm (cheek) at every size |
| Stand screws | `stand_m` | M4, **M5**, M6 | Counterbored holes in the base or, without the base, the yoke plate |
| Hub to mount (Connections tab) | `hub_m` | M3, **M4**, M5 | The four cradle holes, matching the hub's mount bolts |
| Leg cross bolt | (app only) | **Auto**, M3–M6 | Auto picks M3, M4 or M5 by leg diameter. The hole may take up to half the leg diameter |

### Seats

| App setting | SCAD parameter | Options (default) | What changes |
|---|---|---|---|
| Clamp heads | `head_pockets` | **Hex pocket**, plain hole | Pocket: the elevation (and arc lock) bolt head is held in the cheek, so one hand turns the wing nut. Plain: a through hole; the head and a washer sit on the cheek's inner face. The bolts grow to M8 × 50 and M6 × 50 |
| Azimuth nut | `nut_pocket` | **Pocket**, plain hole | Pocket: the nut is captured under the base. Plain: a through hole; the nut and a washer sit under the base. The screw grows to M8 × 40. A stand under the printed base needs a clearance hole; the tripod base has room between its sockets |
| Stand seats (no base) | `stand_counterbore` | **Counterbored**, plain hole | Plain: socket heads and washers on top of the yoke plate |

The joint screws stay counterbored, and so do the printed base's stand screws: their heads sit in the faces the yoke and the dish hub bear on.

The arc lock stays M6. The insert pilots follow the **Insert pilot** setting, which is the M4 value; M3 and M5 move by the same amount.

The STLs in `cad/STL/` are the default sizes. For other sizes export the parts from the app, or set the parameters above in the SCAD.

## Generator integration

Enable **Aiming mount** in PETAL 5.3. The app embeds blank parts for the selected options (`cad/STL/blank/`, exported with `fasteners = false`) and cuts every bolt hole, counterbore, insert pilot and nut or head pocket for the selected sizes, with the SCAD's own transforms and 32-sided holes ([`dist/mount-fasteners.js`](../dist/mount-fasteners.js)). At the default sizes the result matches `cad/STL/simple-*.stl` to float rounding. It applies the same assembly transforms in the viewer/PDF/SCAD snapshot, includes mount parts in plate packing, and checks printed-part interference at the selected pose. It reports the clearance below the lowest mount face at that pose and the geometric minimum stand height across the displayed elevation range. Add stand/cable/handling margin. The stand, hardware envelopes and complete collision sweep are not runtime-certified.

For **blind hub inserts**, use M4 × 18 through the 12 mm cradle with a 1 mm head washer, nominal 5 mm entry (M3 × 16 and M5 × 18 for the other sizes). Never use the through-bolt length in a blind insert. For **through bolts**, use the generated schedule; M4 × 35 is the default 400 mm setup.

The kinematic frame is the same for every option: elevation axis 94 mm above the base bottom (base 14 + yoke plate 10 + 70), hub face 75 mm ahead of the axis, clamp faces at x = 40. Without the base the yoke bottom, 14 mm up in that frame, is the lowest face, and the app measures stand clearance from it.

## Design choices

- **One adjustment per axis.** Elevation is a single clamp on one side. It's asymmetric on purpose, so there's one wing nut to work instead of two.
- **Split arms for print orientation.** The upright and the cheek print lying on their clamp faces. The clamp faces come out flat, both elevation bolt holes and the arc slot are vertical in the print, and the layers run along each arm, the way the dish bends it. The plates print on their bearing faces.
- **Bolted joints that resist the moment both ways.** The upright stands on the yoke plate on 4 screws: two under the arm itself (x 48) and two in a foot flange on its outer side (x 69), 64 mm apart. The dish's weight tips the upright inward, which loads the flange screws, and anything that tips it outward loads the inner pair. A low shoulder on the plate locates the upright's inner face. The large triangular flare of the one-piece yoke is gone.
- **Inset joints.** Each arm ends in a 2 mm tenon that drops into a matching pocket in its plate, with 0.2 mm clearance per side and a 0.5 mm lead-in. The tenon locates the arm and carries sideways shear, so the screws only clamp.
  - The upright's tenon is its whole footprint, foot flange included, and it bears on the pocket floor.
  - The cheek's tenon stands 2 mm inside its end face on the inner, top and bottom sides, and is flush with the clamp face so it prints without overhang. The tenon end and the end face both bear on the plate.
  - The insert pilots are 2 mm deeper, so the screws keep their length and full engagement.
- **Cheek gusset.** One solid wedge (20 mm wide, 8 × 24 mm) on the cheek's inner face at the plate end stiffens the joint and bears on the plate. It sits between the hub's bolt heads and outside the Ø34 port, and prints as an up-facing slope.
- **Socket heads in counterbores.** All joint and stand screws are ISO 4762 socket heads, the common M-series screw with a flat underside. Each sits in a counterbore 1 mm wider than the head and 0.5 mm deeper, so the head finishes below the face: under the yoke plate that is the azimuth bearing face, and on the cradle it is the face the dish hub bears on. A flat face mates cleanly with a flat head, washer or the next part, where a cone would leave a sharp lip. Where a counterbore opens toward the bed its ceiling is stepped: two 0.4 mm layers, first a slot the width of the hole, then a square, so each layer bridges a short span instead of a ring hanging in the air.
- **Clamp faces sized to the load.** The upright's cap is r 40 around the axis and holds the arc slot. The cheek's clamp disc is r 32, about 37 cm² of contact. Hand-tight on the default M8, that holds roughly 8 N·m, several times the moment of a 400 mm dish with a feed in wind.
- **A compact cheek.** The cheek tapers straight from its clamp disc to a flat 14 × 42 mm end face. The whole face sits inside the plate's rim chamfer, so it needs no corner cuts, and it clears the four hub bolt heads behind the plate. It is 70 cm³, about 30% less than the earlier 80 mm wide cheek with ribs.

![Elevation clamp and arc lock from the outside](simple-mount-clamps.png)

## Keeping it from slipping

The elevation clamp holds the dish by friction alone, so how much it holds depends on how hard the wing nut is turned and how grippy the two faces are. The chart compares the dish's weight torque with an estimate of what each setup holds, counting only half of each estimate to allow for creep and a light hand.

![Dish weight torque against clamp hold, by dish size](mount-holding.png)

| Setup | Estimated hold | Usable (half) | Holds, with a 100 g feed |
|---|---|---|---|
| One clamp, hand-tight M8 wing nut | 5.1 N·m | 2.5 N·m | up to about 600 mm |
| Both sides | 10.2 N·m | 5.1 N·m | up to about 800 mm |
| One clamp with a 1 mm rubber or cork pad between the cheek and the upright | 12.2 N·m | 6.1 N·m | up to about 900 mm |
| One clamp plus the arc lock, its M6 nyloc snug with a wrench | 16.8 N·m | 8.4 N·m | up to about 1100 mm |
| Both sides plus the arc lock | 21.8 N·m | 10.9 N·m | up to 1200 mm, with little margin |

How the estimates are made:

- **Preload.** A hand-tight M8 wing nut takes about 1.5 N·m, which with a nut factor of 0.2 clamps with about 940 N.
- **Friction.** PETG or ASA on itself has a friction coefficient of about 0.25; a rubber or cork pad about 0.6. The clamp disc's mean friction radius is 21.7 mm.
- **Arc lock.** The arc lock bolt is tightened with a wrench (about 2 N·m on M6, roughly 1,700 N) and sits 28 mm from the axis, so it adds more than the wing nut does. Snug it; do not crush the plastic under its 12 mm washer.
- **Dish torque.** The app's own masses: the printed parts solid at 1.24 g/cm³, the rods, and the feed payload at its focus, at the elevation where the torque is largest.

These are estimates for comparing options. Test the real thing (see [Validation](#validation)).

![The elevation clamp on both sides: a second, mirrored upright and cheek](simple-mount-both.png)

**Which side.** The bolt head is held in the cheek, so when the dish slips the bolt turns with it. On the right side, a slip that lets the dish nod down turns the bolt the way that unscrews it from the wing nut, so a slip loosens the clamp further. On the left side the same slip screws the bolt further into the nut and tightens it. With right-hand threads the left side is the better choice. With both sides the left clamp tightens as the right one loosens.

**Pads.** A thin rubber, EPDM or cork disc between the cheek and the upright more than doubles the grip and keeps some preload as the plastic creeps. It works with one clamp. With both sides there is no room for it: the cradle fits between the uprights with no designed gap.

**Larger dishes.** Past about 1000 mm, friction is not a dependable way to hold elevation. The reliable answer is a screw drive: a threaded rod between a pivot on the yoke and a pivot under the cradle, turned by a knob. The thread locks itself, so it cannot slip. In a first layout, with the cradle pivot 70 mm from the axis and the yoke pivot 90 mm from it, an M8 rod moves the dish about 1.3° per turn at mid-range, needs about 100 mm of travel (about 80 turns) for the full −10° to 100°, and holds a 1200 mm dish with about 200 N of thrust. It adds two printed pivots, a knob, the rod and two nuts. It is not built yet.

## Tripod base

![Tripod base from below with 20 mm legs](simple-mount-tripod.png)

Pick **Tripod leg sockets** as the azimuth base, enter your leg diameter (8–25.4 mm, in mm or inches) and set the splay with its slider (10–30° from vertical, default 20°). Legs are whatever round stock you have: dowel, aluminum tube or conduit.

| | Rule | 20 mm legs, 20° splay |
|---|---|---|
| Bore | leg Ø + 0.4 mm, blind, floor just below the base | Ø20.4 |
| Engagement | max(35 mm, 2.5 × leg Ø) | 50 mm |
| Wall | max(5 mm, 0.25 × Ø) at the base, tapering to max(3 mm, 0.15 × Ø) at the mouth | 5 → 3 mm |
| Shape | Octagon, flats facing the cross bolt | 30.4 → 26.4 mm across flats |
| Cross bolt | Auto: M3 below Ø14, M4 below Ø20, M5 above; or pick M3–M6. Mid-engagement, nyloc and two washers | M5 × 40 |
| Placement | As far out as the Ø116 disc allows, clear of the azimuth nut | root at r 37 mm |

Leg 1 points along azimuth zero. Each socket's flats face the tangential direction, so the cross bolt clamps two flat faces. Push the leg home, drill it through the socket hole, and bolt it.

**Printing.** The base still prints top face down, so the sockets stand up from it, leaning out by the splay. The outer faces overhang by at most the splay plus about 2° of taper. The bore floor faces up and the cross-bolt holes are teardrops. No supports.

**Leg length.** The app computes the shortest legs that do two jobs, for your actual dish, feed and mount:

- **Stability.** It sums every printed part, the aluminum rods and mast, and the feed payload at its focus, and finds the worst center-of-mass offset from the azimuth axis over the elevation range. A tripod tips first over the edge between two feet, which sits at half the foot radius. The legs must put that edge 1.5 times farther out than the offset.
- **Ground clearance.** The dish's lowest point over the elevation range must stay 20 mm off the ground.

The default 400 mm dish needs 370 mm legs at 20° splay, on a 334 mm foot circle. With the Gregorian collector the center of mass moves forward and the answer becomes 540 mm. More splay shortens the legs.

**Leg clearance.** The dish turns in azimuth over fixed legs. For every elevation the app checks whether any part of the dish comes within 3 mm of a leg or socket at any azimuth, and reports the lowest elevation that is clear all the way round. It also checks the selected pose exactly. The default dish touches a leg only below −8°, and only when it faces that leg.

These are static checks with no wind rating. Stake or weight the feet outdoors.

## Parts

STLs are in `cad/STL/`, already in print orientation, at the default bolt sizes. Use the variant that matches your options.

| Part | STL | Prints | Size (mm) |
|---|---|---|---|
| Base | `simple-base.stl`; tripod: export from the app | Top (clamp) face down | Ø116 × 14; tripod about 127 × 128 × 70 |
| Yoke | `simple-yoke.stl`; without the base `simple-yoke-stand.stl` | Bearing face down | 138 × 116 × 18 |
| Upright | `simple-upright.stl`; with arc lock `simple-upright-arc.stl` | Lying on its clamp face (x = 40) | 112 × 88 × 36 |
| Cradle | `simple-cradle.stl` | Hub face down | Ø94 × 12 |
| Cheek | `simple-cheek.stl`; with arc lock `simple-cheek-arc.stl` | Lying on its clamp face (x = 40) | 97 × 64 × 22 |
| Both sides | `simple-yoke-dual.stl` (without the base `simple-yoke-dual-stand.stl`), `simple-cradle-dual.stl`, plus `simple-upright-left.stl` and `simple-cheek-left.stl` with the right-hand upright and cheek | As above; the left arms are mirror images | Yoke 160 × 116 × 18 |

For the left side alone, mirror the yoke, upright, cradle and cheek in your slicer, or export them from the app.

![Print orientation](simple-mount-print.png)

The cradle keeps the hub interface: Ø94 × 12 plate, Ø34 port, 4 holes on a 60 mm circle for the hub's mount bolts (M4 by default).

**Material:** use a qualified ASA or reinforced ASA profile for the rigid mount parts, with slip and warm-creep tests. This is separate from the flexible clip material; filled filament is not automatically suitable for repeatedly flexed snaps.

**Print settings:**
- 5–6 walls.
- 30–40% infill.
- Elephant-foot compensation on.
- Insert pilots are 10 mm deep blind holes (Ø5.6 for M4, Ø4.2 for M3, Ø6.4 for M5) with pointed roofs where they lie horizontal in the print. Check your insert supplier's recommended hole, and test one on a scrap print first.

## Hardware

Default sizes. The generated hardware list and PDF use the sizes you pick.

| Qty | Item | Use |
|---|---|---|
| 1 | M8 × 25 socket head, washer, nut | Azimuth (base only). The nut sits in the pocket under the base |
| 1 | M8 × 40 hex bolt, fender washer (24–30 mm OD), wing nut | Elevation. The head sits in a hex pocket on the inside of the cheek |
| 7 | M4 heat-set insert, 6 mm long or shorter, for a Ø5.6 pilot | 4 in the bottom of the upright, 3 in the cheek's end face |
| 4 | M4 × 10 socket head (ISO 4762) | Upright → yoke plate, up from below |
| 3 | M4 × 12 socket head (ISO 4762) | Cheek → cradle plate, from the hub face |
| 4 | M4 × 35 socket head, 2 washers, nut | Cradle → PETAL hub. Head behind the cradle plate; washer and nut in the seats on the hub front |
| 4 | M5 socket head (ISO 4762), length from your stand | Base → your stand, or yoke plate → your stand without the base |
| 1 | M6 × 45 hex bolt (ISO 4017), M6 washer 12 mm OD, M6 nyloc nut | Arc lock only. Head captive in the cheek; washer and nyloc on the upright's outside |

Each joint screw is the shortest stock length that reaches through the plate and the whole insert; the tips stop at least 1.5 mm short of the pilot bottoms. Use a nyloc or flange nut on the arc lock, not a wing nut: its wings would foul the M8 wing nut. The Ø12 washer is the largest that clears the M8 wing nut's swept circle (Ø39).

## Assembly

![The bolted joints, exploded](simple-mount-joints.png)

Default sizes; the generated PDF uses yours.

1. **Inserts.**
   - Heat-set 4 inserts into the pilots in the bottom of the upright and 3 into the cheek's end face, each 0.5 mm below the tenon entry face (the tip of the 2 mm tenon), not below its surrounding shoulder.
   - Let them cool. Scrape off any raised rim so the mating faces stay flat.
2. **Upright on the yoke plate.**
   - Drop the upright's tenon into the pocket in the plate, against the locating shoulder.
   - Drive the 4 × M4 × 10 up through the plate from underneath. The heads must finish below the bottom face.
3. **Base on the stand** (with the base).
   - Press the M8 nut into the pocket underneath.
   - Screw the base down with the 4 × M5 socket heads.
   - Set the yoke on the base. Drop in the M8 × 25 and washer and tighten it into the nut with a 6 mm hex key.
4. **Yoke on the stand** (without the base).
   - Put the yoke plate, upright already fitted, on a flat stand.
   - Drive the 4 × M5 socket heads down through its counterbores, heads below the top face. They are reached from above beside the upright, so do this before the cradle goes on.
5. **Cheek on the cradle.** Drop the cheek's tenon into the pocket in the back of the cradle plate, then fit it with the 3 × M4 × 12 from the hub face, heads below the face. Do this before the dish hub covers that face.
6. **Cradle on the dish.**
   - Push the 4 × M4 × 35 with washers through the cradle plate from behind, then through the hub.
   - Put a washer and nut on each in the Ø10 seats on the hub front and tighten.
7. **Elevation bolt.** Seat the M8 × 40 head in its pocket on the inside of the cheek. With the arc lock, also seat the M6 × 45 head in the second pocket.
8. **Both sides.** Do steps 1, 2, 5 and 7 for each arm. The cradle fits between the uprights with no designed gap; each upright's tenon has 0.2 mm of play. Screw the left upright down, offer the cradle with both cheeks fitted, then screw the right upright down while pressing it against its cheek. Build the whole U on the bench before setting the yoke on the base: the upright screws are reached from under the yoke plate. Tighten both wing nuts evenly.
9. **Cradle on the upright.**
   - Slide the M8 out through the upright, and the M6 into the arc slot.
   - Add the fender washer and wing nut on the outside, and the M6 washer and nyloc.

## Using it

- **Azimuth (with the base):**
  - Loosen the center bolt from above.
  - Turn the dish, read the rim ticks at the yoke's rear groove, then tighten.
  - The key reaches the bolt with the dish anywhere from −10° to 30°.
- **Azimuth (without the base):** there is no azimuth clamp. Turn or re-fix the stand. The central Ø8.5 hole stays open.
- **Elevation:**
  - Hold the dish, loosen the wing nut (and the arc lock nyloc), set the angle and tighten the wing nut, then snug the nyloc with a 10 mm wrench.
  - The arc slot's ends stop the cradle just beyond −10° and 100°.
  - Read the angle with a phone inclinometer on the dish rim or the back of the hub plate.
- **Clearance:** at −10° the rim hangs 129 mm below the base bottom, or 143 mm below the yoke plate without the base. Mount the lowest face at least that high.
- **With snap-clip seams:** the clips hang about 6–9 mm farther behind the dish than the flanges. Elevation then clears from −7.5° to 100°.

## Loads

- **Elevation:**
  - About 1.2 N·m per kg of dish at the horizon.
  - Grip depends on actual preload, friction, contact and temperature; no allowable holding torque is established. The arc lock adds a second clamp point 28 mm from the axis; it is not rated either.
  - If it creeps, stop and review preload, material, bearing stress and clamp design. Do not solve slip by indefinitely increasing torque.
- **Off-center load:** the dish sits about 40 mm off the clamp. The single joint carries that sideways moment through the full face and the bolt.
- **Joints:** the insert joints are not load-rated. Check them for creep and loosening along with the clamps.
- **Azimuth:** only wind loads it, over 100 cm² of contact.

## Validation

### `scripts/check-simple-mount.py <stl dir>`
It reads the print transforms from the SCAD, places all eight STLs with them, and checks the default, arc-lock and no-base variants:

- **Bed fit and printability:** closed, single-body meshes that fit the bed, and an overhang scan (below).
- **Joints:**
  - The upright sits flush on the yoke plate (29.6 cm²) and the cheek on the cradle plate (6.2 cm², gusset included), each face bearing fully.
  - Each tenon drops into its pocket with 0.2 mm clearance and locks its arm sideways in every direction.
  - All 7 socket heads sit 0.5 mm below the face in their counterbores and stop short of the pilot ends; the inserts fit their pilots with at least 2 mm of plastic around them.
  - The screws clear every other bolt, head and nut.
- **Clamp contact:**
  - Azimuth: 100 cm².
  - Elevation: 36–38 cm² across the range, 32–34 cm² with the arc slot.
- **Hardware:** the bolts, heads and wing nut sit in their holes and pockets at every angle.
- **Elevation travel:** −10° to 100°, clear of the yoke, upright and hardware. The first contact is at −22.5°.
- **Arc lock:** the M6 runs clear through the slot from −10° to 100°, and its washer stays inside both clamp faces and clear of the M8 fender washer and wing nut at every angle. The slot ends stop travel at about −12° and 102°.
- **Both sides:** the left upright and cheek are exact mirror images of the right ones. Each arm sits flush on the dual yoke and cradle with its screws seated, each clamp keeps 36–38 cm² of contact over the range, and both bolts sit in their holes and pockets with the heads inside and the wing nuts outside. The cradle tilts from −10° to 100° (with the arc lock too), the dish clears both uprights, everything that turns clears the base, the hex key still reaches the azimuth bolt from −10° to 30°, the hub bolts and port stay open, and without the base the four stand screws clear both uprights and are reached straight down.
- **Dish clearance:** the dish clears the yoke, upright and base over the same range. With snap-clip seams, modeled as a ring 26 mm deep behind the dish, it clears from −7.5° to 100°.
- **Azimuth sweep:** everything that turns clears the base all the way around, at −10°, 0°, 45° and 90°.
- **Tool access:** the hex key reaches the azimuth bolt, and a Ø10 driver reaches each stand screw with the upright fitted.
- **Hub interface:** the M4 holes and the Ø34 port are open with straight access from behind. The root nuts and washers behind the hub and the hub bolt heads behind the plate are clear. The joint counterbores sit between the port, the hub bolts and the rim.

### `scripts/check-printability.py cad/STL/simple-*.stl`
All eight parts pass. A part fails on any downward face more than 60° from vertical, unless it is a flat ceiling (within 20° of flat) no more than 10 mm across, which prints as a bridge, or a step within 0.3 mm of the bed. Faces between 45° and 60° are listed but pass: at 0.2 mm layers each layer steps out at most 0.35 mm, under one extrusion width. The counterbore ceilings are the bridges here, 4.5 to 6.5 mm across. The same holds for parts exported from the app at the smallest (M3 / M6 / M4) and largest (M5 / M10 / M6) sizes, and for the tripod base with M10 clamps and M6 leg bolts.

### Bolt sizes
- `tests/integration.test.mjs`: at the default sizes, the app's cuts on the blanks reproduce all eight complete STLs (difference under 0.5 mm³, the same number of holes), the hole diameters follow each size setting, and each plain-hole seat option removes its pocket or counterbore.
- `scripts/check-scad.mjs`: `cad/simple-mount.scad` rendered with `joint_m = 5`, `clamp_m = 10`, `stand_m = 6` and `hub_m = 3` matches the app's parts to float rounding, including the dual yoke and cradle and a left upright with `sides = "both"`.

### Regenerating
`python3 scripts/pack-mount.py --export` re-exports every complete STL variant and every blank (`cad/STL/blank/`) from the SCAD and rebuilds `dist/mount-meshes.js` from the blanks, with each part's frame, the inverse of its SCAD print transform and the constants the cuts need. `python3 scripts/render-mount.py` redraws these images.

These are geometry checks. Grip and creep need a physical test: set the dish at 0°, tighten everything, leave it somewhere warm for 48–72 hours, re-tighten once after the first day, then check the angle again.
