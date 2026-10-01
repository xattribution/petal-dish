# Simple alt-az mount

![Simple mount with a 400 mm PETAL at 20° elevation](simple-mount.png)

Three printed parts and two disc-on-disc clamps. Each joint is two flat faces with one M8 bolt through the middle. Loosen, aim, tighten.

The rotator and cradle are faceted solids built from flat faces only. The rotator's tower flares to a wide foot on its disc. The cradle's arm flares into a skirt on a hub plate that widens toward the back. No face leans past the print limit, so none need supports.

| | |
|---|---|
| Parts | Base, rotator, cradle |
| Azimuth | 360°. Rotator disc on the base disc; rim ticks every 5°, full height every 10° |
| Elevation | −10° to 100°, checked with the dish fitted. The cradle arm clamps to the rotator's outer face |
| Hold | Friction between flat faces, squeezed by one M8 bolt per joint |
| Mounting | 4 × M5 flat-head through the base |
| Supports | None. No surface steeper than 45° and no flat ceilings |

![Both clamps without the dish](simple-mount-clamps.png)

## Parts

STLs are in `cad/STL/`, already in print orientation.

| Part | STL | Prints | Size (mm) |
|---|---|---|---|
| Base | `simple-base.stl` | Clamp face down | Ø116 × 14 |
| Rotator | `simple-rotator.stl` | Clamp face down, tower standing | Ø116 × 113 |
| Cradle | `simple-cradle.stl` | Hub face down, arm standing | Ø112 × 101 |

![Print orientation](simple-mount-print.png)

The base and rotator print with their clamp faces on the bed, so those faces come out flat and smooth. The cradle's hub face is on the bed as well.

**Material:** print all three in ASA-GF. Glass fill creeps less under clamp pressure and holds up in sun.

**Print settings:**
- 5–6 walls.
- 30–40% infill.
- Elephant-foot compensation on, so the clamp faces stay flat.

## Hardware

| Qty | Item | Use |
|---|---|---|
| 1 | M8 × 25 socket head + washer, M8 nut | Azimuth. The nut sits in the pocket under the base; the head sits at the bottom of the well in the rotator |
| 1 | M8 × 30 wing screw (DIN 316), fender washer (24–30 mm OD), M8 nut | Elevation. The nut drops into the slot in the top of the tower; the wing screw goes in from outside the arm |
| 4 | M4 × 16 socket head + washer | Cradle → PETAL hub inserts |
| 4 | M5 flat-head screw | Base → your stand. They sit just below the clamp face |

## Assembly

1. **Base on the stand.**
   - Press the M8 nut into the pocket underneath.
   - Screw the base down with the 4 × M5 flat-heads.
2. **Rotator.**
   - Set it on the base.
   - Drop the M8 × 25 and its washer down the well in the rotator.
   - Thread it into the nut with a long 6 mm hex key.
3. **Elevation nut.** Drop the M8 nut into the slot in the top of the tower, flats vertical. It lines up with the hole in the tower's outer face.
4. **Cradle on the dish.** 4 × M4 × 16 into the hub inserts. The heads are in the open, beside the arm's skirt.
5. **Cradle on the rotator.** Hold the arm against the tower's outer face, then run the wing screw and fender washer through it into the nut.

## Using it

- **Azimuth:**
  - Loosen the bolt with a long 6 mm hex key straight down the well, then turn the dish.
  - Read the rim ticks at the groove on the rear of the base, then tighten.
  - The key reaches the bolt with the dish anywhere from −10° to 30°. Above that, the cradle covers the well.
- **Elevation:**
  - Hold the dish, loosen the wing screw, set the angle and tighten.
  - Measure the angle with a phone inclinometer on the flat top of the arm.
- **Clearance:** at −10° the rim hangs 126 mm below the base bottom. Mount the base at least that high.

## Loads

- **Elevation:**
  - About 1.2 N·m per kg of dish at the horizon, with the center of mass about 118 mm ahead of the axis.
  - The elevation faces have about 24 cm² in contact. A hand-tight M8 at about 1.5 kN gives roughly 8 N·m of grip, as an estimate.
- **Azimuth:** only wind loads it. Its faces have 99 cm² in contact.
- **More grip:**
  - Use an M8 hex bolt and a wrench instead of the wing screw.
  - The layer-line texture on the elevation faces grips better than the smooth bed faces.

## Validation

### `scripts/check-simple-mount.py <stl dir>`
It places the STLs with the same transforms as the SCAD and checks:

- **Bed fit:** closed, single-body meshes that fit the bed.
- **Clamp contact:** both pairs of faces sit flat, with 99 cm² in contact on azimuth and 24–26 cm² on elevation across the range.
- **Hardware:** both M8 bolts, the elevation nut and the azimuth bolt head sit in their holes at every angle.
- **Elevation travel:** the cradle tilts from −10° to 100° clear of the rotator and the azimuth bolt. It first touches at −20°.
- **Tool access:** a hex key reaches the azimuth bolt straight down the well from −10° to 30° elevation.
- **Dish clearance:** the dish clears the rotator and base over the same range.
- **Azimuth sweep:** all the way around at −10°, 0°, 45° and 90°.
- **Hub interface:** the M4 holes and the Ø34 port are open with straight access from behind, and the root-screw head band is clear.

### `scripts/check-printability.py cad/STL/simple-*.stl`
All three parts pass.

These are geometry checks only. Grip and creep need a physical test.

## Drift test

1. Set the dish at 0° and tighten both bolts.
2. Leave it somewhere warm for 48–72 hours.
3. Re-tighten once after the first day, then check the angle with the phone again.
