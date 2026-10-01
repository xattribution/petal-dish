# Simple alt-az mount

![Simple mount with a 400 mm PETAL at 20° elevation](simple-mount.png)

Three printed parts and one clamp per axis. Each clamp is two flat faces squeezed by one M8 bolt: loosen, aim, tighten.

| | |
|---|---|
| Parts | Base, yoke, cradle |
| Azimuth | 360°. The yoke turns on the base; one bolt in the center, tightened with a hex key from above |
| Elevation | −10° to 100°, checked with the dish fitted. The cradle's cheek clamps against the inside of the yoke's upright, with one wing nut |
| Scale | Azimuth grooves down the base rim every 5°, wide every 10°, read at the groove on the back of the yoke |
| Mounting | 4 × M5 flat-head through the base |
| Supports | None |

## Design choices

- **One adjustment per axis.** Elevation is a single clamp on one side. It's asymmetric on purpose, so there's one wing nut to work instead of two.
- **Bigger faces to make up for one joint.** Both clamp faces are round, r 35 around the elevation axis. They share about 39 cm² of contact.
- **Flared, not thin.** The upright and the cheek are straight only where they clamp. Below that they flare out on every side, all the way to the edges of their plates: the upright across the yoke plate, and the cheek across the hub plate up to the hub screws. No thin wall meets a plate at a right angle. FDM parts are several times weaker across layers than along them, so the flare spreads that load.
- **Clamp faces where they print best.** The azimuth faces print against the bed, so they come out flat and smooth.

![Elevation clamp from the rear](simple-mount-clamps.png)

## Parts

STLs are in `cad/STL/`, already in print orientation.

| Part | STL | Prints | Size (mm) |
|---|---|---|---|
| Base | `simple-base.stl` | Clamp face down | Ø116 × 14 |
| Yoke | `simple-yoke.stl` | Plate down, upright standing | 130 × 116 × 115 |
| Cradle | `simple-cradle.stl` | Hub face down, cheek standing | Ø94 × 110 |

![Print orientation](simple-mount-print.png)

**Material:** print all three in ASA-GF. Glass fill creeps less under clamp pressure and stands up to sun.

**Print settings:**
- 5–6 walls.
- 30–40% infill.
- Elephant-foot compensation on.

## Hardware

| Qty | Item | Use |
|---|---|---|
| 1 | M8 × 25 socket head, washer, nut | Azimuth. The nut sits in the pocket under the base |
| 1 | M8 × 40 hex bolt, fender washer (24–30 mm OD), wing nut | Elevation. The head sits in a hex pocket on the inside of the cradle cheek |
| 4 | M4 × 30 socket head, 2 washers, nut | Cradle → PETAL hub. Head behind the cradle plate; washer and nut in the seats on the hub front |
| 4 | M5 flat-head screw | Base → your stand |

## Assembly

1. **Base on the stand.**
   - Press the M8 nut into the pocket underneath.
   - Screw the base down with the 4 × M5 flat-heads.
2. **Yoke.**
   - Set it on the base.
   - Drop in the M8 × 25 and washer and tighten it into the nut with a 6 mm hex key.
3. **Cradle on the dish.**
   - Push the 4 × M4 × 30 with washers through the cradle plate from behind, then through the hub.
   - Put a washer and nut on each in the Ø10 seats on the hub front and tighten.
4. **Elevation bolt.** Seat the M8 × 40 head in the pocket on the inside of the cradle cheek.
5. **Cradle on the yoke.**
   - Slide the bolt out through the upright.
   - Add the fender washer and wing nut on the outside.

## Using it

- **Azimuth:**
  - Loosen the center bolt from above.
  - Turn the dish, read the rim ticks at the yoke's rear groove, then tighten.
  - The key reaches the bolt with the dish anywhere from −10° to 30°.
- **Elevation:**
  - Hold the dish, loosen the wing nut, set the angle and tighten.
  - Read the angle with a phone inclinometer on the dish rim or the back of the hub plate.
- **Clearance:** at −10° the rim hangs 129 mm below the base. Mount the base at least that high.
- **With snap-clip seams:** the clips hang about 20 mm farther behind the dish than the flanges. Elevation then clears from 0° to 100°.

## Loads

- **Elevation:**
  - About 1.2 N·m per kg of dish at the horizon.
  - As a rough estimate, a hand-tight M8 at about 1.5 kN on the r 35 faces gives about 10 N·m of grip.
  - If it creeps, swap the wing nut for a hex nut and a wrench.
- **Off-center load:** the dish sits about 40 mm off the clamp. The single joint carries that sideways moment through the full face and the bolt.
- **Azimuth:** only wind loads it, over 99 cm² of contact.

## Validation

### `scripts/check-simple-mount.py <stl dir>`
It places the STLs with the same transforms as the SCAD and checks:

- **Bed fit:** closed, single-body meshes that fit the bed.
- **Clamp contact:**
  - Azimuth: 99 cm².
  - Elevation: 38–40 cm² across the range.
- **Hardware:** the bolts, head and wing nut sit in their holes and pockets at every angle.
- **Elevation travel:** −10° to 100°, clear of the yoke and hardware. The first contact is at −22.5°.
- **Dish clearance:** the dish clears the yoke and base over the same range. With snap-clip seams, modeled as a ring 36 mm deep behind the dish, it clears from 0° to 100°.
- **Azimuth sweep:** everything that turns clears the base all the way around, at −10°, 0°, 45° and 90°.
- **Tool access:** the hex key reaches the azimuth bolt.
- **Hub interface:** the M4 holes and the Ø34 port are open with straight access from behind, and the root nuts and washers behind the hub are clear.

### `scripts/check-printability.py cad/STL/simple-*.stl`
All three parts pass: nothing steeper than 45° and no flat ceilings.

These are geometry checks. Grip and creep need a physical test: set the dish at 0°, tighten everything, leave it somewhere warm for 48–72 hours, re-tighten once after the first day, then check the angle again.
