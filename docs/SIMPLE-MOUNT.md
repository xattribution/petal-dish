# Simple alt-az mount

![Simple mount with a 400 mm PETAL at 20° elevation, arc lock fitted](simple-mount.png)

Five printed parts and one clamp per axis. Each clamp is two flat faces squeezed by one M8 bolt: loosen, aim, tighten. The two arms, the upright and the cheek, are separate parts bolted on with M4 heat-set inserts. That lets every part print flat, with no supports.

| | |
|---|---|
| Parts | Base, yoke, upright, cradle, cheek. Without the base: four parts |
| Azimuth | 360°. The yoke turns on the base; one bolt in the center, tightened with a hex key from above |
| Elevation | −10° to 100°, checked with the dish fitted. The cheek clamps against the inside of the upright, with one wing nut. Optional arc lock: a second bolt in an arc slot |
| Scale | Azimuth grooves down the base rim every 5°, wide every 10°, read at the groove on the back of the yoke |
| Joints | Upright → yoke plate: 4 × M4 flat-head from below. Cheek → cradle plate: 3 × M4 flat-head from the hub face. All into heat-set inserts, heads flush |
| Mounting | 4 × M5 flat-head through the base, or through the yoke plate without the base |
| Supports | None. The overhang scan passes for every part; inspect your slicer toolpath |

## Options

Set them in PETAL under **Aiming mount**, or in `cad/simple-mount.scad`:

| App setting | SCAD parameter | Effect |
|---|---|---|
| Azimuth base: *Printed base with azimuth scale* (default) | `stand_holes = false` | Base + yoke turntable with the azimuth clamp and the scale |
| Azimuth base: *No base · turntable bolts to the stand* | `stand_holes = true` | No base part. The yoke plate gets 4 countersunk M5 holes from its top and screws straight onto a flat stand. No azimuth clamp: turn the stand to aim in azimuth |
| Elevation lock: *Add arc-slot lock bolt* | `arc_lock = true` | An M6 bolt runs in an arc slot through the upright, from a captive head in the cheek, for extra grip. It covers the whole −10° to 100° range |

## Generator integration

Enable **Aiming mount** in PETAL 5.3. The app embeds the canonical STLs for the selected options, applies the same assembly transforms in the viewer/PDF/SCAD snapshot, includes mount parts in plate packing, and checks printed-part interference at the selected pose. It reports the clearance below the lowest mount face at that pose and the geometric minimum stand height across the displayed elevation range. Add stand/cable/handling margin. The stand, hardware envelopes and complete collision sweep are not runtime-certified.

For **blind hub inserts**, use M4 × 18 through the 12 mm cradle with a 1 mm head washer, nominal 5 mm entry. Never use the through-bolt length in a blind insert. For **through bolts**, use the generated schedule; M4 × 30 is the default 400 mm setup.

The kinematic frame is the same for every option: elevation axis 94 mm above the base bottom (base 14 + yoke plate 10 + 70), hub face 75 mm ahead of the axis, clamp faces at x = 40. Without the base the yoke bottom, 14 mm up in that frame, is the lowest face, and the app measures stand clearance from it.

## Design choices

- **One adjustment per axis.** Elevation is a single clamp on one side. It's asymmetric on purpose, so there's one wing nut to work instead of two.
- **Split arms for print orientation.** The upright and the cheek print lying on their clamp faces. The clamp faces come out flat, both elevation bolt holes and the arc slot are vertical in the print, and the layers run along each arm, the way the dish bends it. The plates print on their bearing faces.
- **Bolted joints that resist the moment both ways.** The upright stands on the yoke plate on 4 screws: two under the arm itself (x 48) and two in a foot flange on its outer side (x 69), 64 mm apart. The dish's weight tips the upright inward, which loads the flange screws, and anything that tips it outward loads the inner pair. A low shoulder on the plate locates the upright's inner face. The large triangular flare of the one-piece yoke is gone.
- **Countersunk heads, no bridges.** All joint and stand screws are 90° flat-heads. The countersinks open toward the bed, so their 45° cones print without support where a counterbore would need bridging. The heads finish flush or just below the face: under the yoke plate that is the azimuth bearing face, and on the cradle it is the face the dish hub bears on.
- **Bigger faces to make up for one joint.** Both elevation arms are round, r 40 around the axis, about 50 cm² of contact.
- **The cheek end stays inside the plate rim.** Its end face is 14 × 68 mm at the inner edge, with 45° chamfers at the outer corners, so all of it bears on the plate inside the rim chamfer. It clears the four hub bolt heads behind the plate.

![Elevation clamp and arc lock from the outside](simple-mount-clamps.png)

## Parts

STLs are in `cad/STL/`, already in print orientation. Use the variant that matches your options.

| Part | STL | Prints | Size (mm) |
|---|---|---|---|
| Base | `simple-base.stl` | Top (clamp) face down | Ø116 × 14 |
| Yoke | `simple-yoke.stl`; without the base `simple-yoke-stand.stl` | Bearing face down | 138 × 116 × 18 |
| Upright | `simple-upright.stl`; with arc lock `simple-upright-arc.stl` | Lying on its clamp face (x = 40) | 110 × 88 × 36 |
| Cradle | `simple-cradle.stl` | Hub face down | Ø94 × 12 |
| Cheek | `simple-cheek.stl`; with arc lock `simple-cheek-arc.stl` | Lying on its clamp face (x = 40) | 103 × 80 × 14 |

![Print orientation](simple-mount-print.png)

The cradle keeps the hub interface: Ø94 × 12 plate, Ø34 port, 4 × M4 on a 60 mm circle.

**Material:** use a qualified ASA or reinforced ASA profile for the rigid mount parts, with slip and warm-creep tests. This is separate from the flexible clip material; filled filament is not automatically suitable for repeatedly flexed snaps.

**Print settings:**
- 5–6 walls.
- 30–40% infill.
- Elephant-foot compensation on.
- Insert pilots are Ø5.6 × 8 mm blind holes with pointed roofs where they lie horizontal in the print. Check your insert supplier's recommended hole, and test one on a scrap print first.

## Hardware

| Qty | Item | Use |
|---|---|---|
| 1 | M8 × 25 socket head, washer, nut | Azimuth (base only). The nut sits in the pocket under the base |
| 1 | M8 × 40 hex bolt, fender washer (24–30 mm OD), wing nut | Elevation. The head sits in a hex pocket on the inside of the cheek |
| 7 | M4 heat-set insert, 6 mm long or shorter, for a Ø5.6 pilot | 4 in the bottom of the upright, 3 in the cheek's end face |
| 4 | M4 × 16 flat-head (ISO 10642 / DIN 7991) | Upright → yoke plate, up from below |
| 3 | M4 × 18 flat-head (ISO 10642 / DIN 7991) | Cheek → cradle plate, from the hub face |
| 4 | M4 × 30 socket head, 2 washers, nut | Cradle → PETAL hub. Head behind the cradle plate; washer and nut in the seats on the hub front |
| 4 | M5 flat-head screw | Base → your stand, or yoke plate → your stand without the base |
| 1 | M6 × 45 hex bolt (ISO 4017), M6 washer 12 mm OD, M6 nyloc nut | Arc lock only. Head captive in the cheek; washer and nyloc on the upright's outside |

Joint screw lengths are the plate thickness plus the 6 mm insert: the tips stop about 1.5 mm short of the pilot bottoms. Use a nyloc or flange nut on the arc lock, not a wing nut: its wings would foul the M8 wing nut. The Ø12 washer is the largest that clears the M8 wing nut's swept circle (Ø39).

## Assembly

![The bolted joints, exploded](simple-mount-joints.png)

1. **Inserts.**
   - Heat-set 4 inserts into the pilots in the bottom of the upright and 3 into the cheek's end face, each flush with its face.
   - Let them cool. Scrape off any raised rim so the mating faces stay flat.
2. **Upright on the yoke plate.**
   - Stand the upright on the plate against the locating shoulder.
   - Drive the 4 × M4 × 16 up through the plate from underneath. The heads must finish flush with or below the bottom face.
3. **Base on the stand** (with the base).
   - Press the M8 nut into the pocket underneath.
   - Screw the base down with the 4 × M5 flat-heads.
   - Set the yoke on the base. Drop in the M8 × 25 and washer and tighten it into the nut with a 6 mm hex key.
4. **Yoke on the stand** (without the base).
   - Put the yoke plate, upright already fitted, on a flat stand.
   - Drive the 4 × M5 flat-heads down through its countersinks, heads flush. They are reached from above beside the upright, so do this before the cradle goes on.
5. **Cheek on the cradle.** Fit the cheek to the back of the cradle plate with the 3 × M4 × 18 from the hub face, heads flush. Do this before the dish hub covers that face.
6. **Cradle on the dish.**
   - Push the 4 × M4 × 30 with washers through the cradle plate from behind, then through the hub.
   - Put a washer and nut on each in the Ø10 seats on the hub front and tighten.
7. **Elevation bolt.** Seat the M8 × 40 head in its pocket on the inside of the cheek. With the arc lock, also seat the M6 × 45 head in the second pocket.
8. **Cradle on the upright.**
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
- **Azimuth:** only wind loads it, over 98 cm² of contact.

## Validation

### `scripts/check-simple-mount.py <stl dir>`
It reads the print transforms from the SCAD, places all eight STLs with them, and checks the default, arc-lock and no-base variants:

- **Bed fit and printability:** closed, single-body meshes that fit the bed, and an overhang scan: nothing faces down more than 45° from vertical, and no bridges longer than 10 mm.
- **Joints:**
  - The upright sits flush on the yoke plate (30 cm²) and the cheek on the cradle plate (6.6 cm²), each face bearing fully.
  - All 7 flat-heads seat with their heads flush and stop short of the pilot ends; the inserts fit their pilots with at least 2 mm of plastic around them.
  - The screws clear every other bolt, head and nut.
- **Clamp contact:**
  - Azimuth: 98 cm².
  - Elevation: 48–53 cm² across the range, 44–49 cm² with the arc slot.
- **Hardware:** the bolts, heads and wing nut sit in their holes and pockets at every angle.
- **Elevation travel:** −10° to 100°, clear of the yoke, upright and hardware. The first contact is at −22.5°.
- **Arc lock:** the M6 runs clear through the slot from −10° to 100°, and its washer stays inside both clamp faces and clear of the M8 fender washer and wing nut at every angle. The slot ends stop travel at about −12° and 102°.
- **Dish clearance:** the dish clears the yoke, upright and base over the same range. With snap-clip seams, modeled as a ring 26 mm deep behind the dish, it clears from −7.5° to 100°.
- **Azimuth sweep:** everything that turns clears the base all the way around, at −10°, 0°, 45° and 90°.
- **Tool access:** the hex key reaches the azimuth bolt, and a Ø10 driver reaches each stand screw with the upright fitted.
- **Hub interface:** the M4 holes and the Ø34 port are open with straight access from behind. The root nuts and washers behind the hub and the hub bolt heads behind the plate are clear. The joint countersinks sit between the port, the hub bolts and the rim.

### `scripts/check-printability.py cad/STL/simple-*.stl`
All eight parts pass: nothing steeper than 45° and no flat ceilings.

### Regenerating
`python3 scripts/pack-mount.py --export` re-exports every STL variant from the SCAD and rebuilds `dist/mount-meshes.js`, with each part's frame and the inverse of its SCAD print transform. `python3 scripts/render-mount.py` redraws these images.

These are geometry checks. Grip and creep need a physical test: set the dish at 0°, tighten everything, leave it somewhere warm for 48–72 hours, re-tighten once after the first day, then check the angle again.
