# Simple alt-az mount

![Simple mount with a 400 mm PETAL at 20° elevation](simple-mount.png)

Three printed parts and two disc-on-disc clamps. Each joint is two flat faces with one M8 bolt through the middle. Loosen the wing nut, aim, and tighten it again.

| | |
|---|---|
| Parts | Base, rotator, cradle |
| Azimuth | 360°. Rotator disc on the base disc; 2° ticks and labels every 30° |
| Elevation | −10° to 100°, checked with the dish fitted. Cradle arm against the rotator's upright |
| Hold | Friction between flat faces, squeezed by an M8 bolt and wing nut |
| Mounting | 4 × M5 flat-head through the base |
| Supports | None. No surface steeper than 45° and no flat ceilings |

![Both clamps without the dish](simple-mount-clamps.png)

## Parts

STLs are in `cad/STL/`, already in print orientation.

| Part | STL | Prints | Size (mm) |
|---|---|---|---|
| Base | `simple-base.stl` | Clamp face down | Ø116 × 12 |
| Rotator | `simple-rotator.stl` | Clamp face down, upright standing | Ø116 × 105 |
| Cradle | `simple-cradle.stl` | Hub face down, arm standing | 94 × 94 × 101 |

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
| 1 | M8 × 35 hex bolt | Azimuth. Its head sits in the hex pocket under the base |
| 1 | M8 × 40 hex bolt | Elevation. Its head sits in the hex pocket on the arm's inner face |
| 2 | M8 wing nut + M8 fender washer (24–30 mm OD) | One per joint |
| 4 | M4 × 16 socket head + washer | Cradle → PETAL hub inserts |
| 4 | M5 flat-head screw | Base → your stand. They sit just below the clamp face |

## Assembly

1. **Base on the stand.**
   - Drop the M8 × 35 into the hex pocket from underneath.
   - Screw the base down with the 4 × M5 flat-heads.
2. **Rotator.** Lower it over the bolt. Add the fender washer and wing nut on top.
3. **Cradle on the dish.** 4 × M4 × 16 from behind into the hub inserts.
4. **Cradle on the rotator.**
   - Put the M8 × 40 head into the pocket on the inside of the cradle arm.
   - Pass the bolt through the upright. Add the washer and wing nut on the outside.

## Using it

- **Azimuth:**
  - Loosen the center wing nut and turn the dish.
  - Read the rim ticks at the groove on the rear of the base, then tighten.
- **Elevation:**
  - Hold the dish, loosen the side wing nut, set the angle and tighten.
  - Measure the angle with a phone inclinometer on the arm's flat top edge.
- **Clearance:** at −10° the rim hangs 136 mm below the base bottom. Mount the base at least that high.

## Loads

- **Elevation:**
  - About 1.2 N·m per kg of dish at the horizon, with the center of mass about 118 mm ahead of the axis.
  - The elevation faces have about 23 cm² in contact. A hand-tight M8 at about 1.5 kN gives roughly 8 N·m of grip, as an estimate.
- **Azimuth:** only wind loads it. Its faces have 99 cm² in contact.
- **More grip:**
  - Use a hex nut and a wrench instead of the wing nut.
  - The layer-line texture on the upright and arm faces grips better than the smooth bed faces.

## Validation

### `scripts/check-simple-mount.py <stl dir>`
It places the STLs with the same transforms as the SCAD and checks:

- **Bed fit:** closed, single-body meshes that fit the bed.
- **Clamp contact:** both pairs of faces sit flat, with 99 cm² in contact on azimuth and 23–24 cm² on elevation across the range.
- **Bolts:** both M8 bolts pass through cleanly at every angle.
- **Elevation travel:** the cradle tilts from −10° to 100° clear of the rotator and the azimuth wing nut. It first touches at −17.5°.
- **Dish clearance:** the dish clears the rotator and base over the same range.
- **Azimuth sweep:** all the way around at −10°, 0°, 45° and 90°.
- **Hub interface:** the M4 holes and the Ø34 port are open with straight access from behind, and the root-screw head band is clear.

### `scripts/check-printability.py cad/STL/simple-*.stl`
All three parts pass.

These are geometry checks only. Grip and creep need a physical test.

## Drift test

1. Set the dish at 0° and tighten both wing nuts.
2. Leave it somewhere warm for 48–72 hours.
3. Re-tighten once after the first day, then check the angle with the phone again.
