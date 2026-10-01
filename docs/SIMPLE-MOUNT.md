# Simple alt-az mount

![Simple mount with a 400 mm PETAL at 20° elevation](simple-mount.png)

Three printed parts. Every joint is two flat faces squeezed together by an M8 bolt: loosen, aim, tighten.

| | |
|---|---|
| Parts | Base, yoke, cradle |
| Azimuth | 360°. The yoke's disc turns on the base disc |
| Elevation | −10° to 100°, checked with the dish fitted. The cradle's two cheeks clamp to the outside of the yoke's two cheeks |
| Scale | Azimuth grooves down the yoke rim, short every 5° and full height every 10°, read at the groove on the base |
| Mounting | 4 × M5 flat-head through the base |
| Supports | None |

## Why it looks like this

- **Symmetric fork.** It's the layout commercial dish brackets use, with the dish held between two clamped side plates. Both sides carry load and nothing is cantilevered.
- **Two friction faces on elevation.** Each side has its own bolt, so the grip doubles compared with a single joint.
- **Flared cheeks.** Each cheek stays straight where it clamps, then flares out on every side into its disc or plate. There are no thin walls meeting at a right angle. FDM parts are several times weaker across layers than along them, so the flares spread that load.
- **Clamp faces on the bed.** The azimuth faces print against the bed, so they come out flat and smooth.

![Both clamps without the dish](simple-mount-clamps.png)

## Parts

STLs are in `cad/STL/`, already in print orientation.

| Part | STL | Prints | Size (mm) |
|---|---|---|---|
| Base | `simple-base.stl` | Clamp face down | Ø116 × 14 |
| Yoke | `simple-yoke.stl` | Disc down, cheeks standing | Ø116 × 107 |
| Cradle | `simple-cradle.stl` | Hub face down, cheeks standing | Ø94 × 102 |

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
| 2 | M8 × 30 hex bolt, fender washer (24–30 mm OD), wing nut | Elevation, one per side. The heads sit in hex pockets on the inside of the yoke cheeks |
| 4 | M4 × 16 socket head + washer | Cradle → PETAL hub inserts |
| 4 | M5 flat-head screw | Base → your stand |

## Assembly

1. **Base on the stand.**
   - Press the M8 nut into the pocket underneath.
   - Screw the base down with the 4 × M5 flat-heads.
2. **Yoke.**
   - Set it on the base.
   - Drop the M8 × 25 and washer between the cheeks and tighten it into the nut with a 6 mm hex key.
3. **Cradle on the dish.** 4 × M4 × 16 into the hub inserts.
4. **Cradle on the yoke.** Lower the cradle cheeks over the yoke cheeks. They slide on with 0.2 mm to spare on each side.
5. **Elevation bolts.**
   - From between the yoke cheeks, push an M8 × 30 out through each side so its head drops into the hex pocket. The 36 mm gap just takes the bolt and head; tilt it in.
   - Add a fender washer and wing nut on the outside.

## Using it

- **Azimuth:**
  - Loosen the center bolt with a hex key from above, between the cheeks.
  - Turn the dish, read the rim ticks at the base groove, then tighten.
  - The key reaches the bolt with the dish anywhere from −10° to 30°.
- **Elevation:**
  - Hold the dish, loosen both wing nuts, set the angle, then tighten both.
  - Read the angle with a phone inclinometer on the dish rim or the back of the hub plate.
- **Clearance:** at −10° the rim hangs 129 mm below the base. Mount the base at least that high.

## Loads

- **Elevation:**
  - About 1.2 N·m per kg of dish at the horizon.
  - The two elevation faces share about 46 cm² of contact.
  - As a rough estimate, two hand-tight M8s at about 1.5 kN each give about 15 N·m of grip.
- **Azimuth:** only wind loads it, over 99 cm² of contact.
- **More grip:** use hex nuts and a wrench in place of the wing nuts.

## Validation

### `scripts/check-simple-mount.py <stl dir>`
It places the STLs with the same transforms as the SCAD and checks:

- **Bed fit:** closed, single-body meshes that fit the bed.
- **Clamp contact:**
  - Azimuth: 99 cm².
  - Elevation: 45–48 cm² across the range.
- **Hardware:** the bolts, heads and nut sit in their holes and pockets at every angle.
- **Elevation travel:** −10° to 100°, clear of the yoke and hardware. The first contact is at −20°.
- **Dish clearance:** the dish clears the yoke and base over the same range.
- **Azimuth sweep:** everything that turns clears the base all the way around, at −10°, 0°, 45° and 90°.
- **Tool access:** the hex key reaches the azimuth bolt.
- **Hub interface:** the M4 holes and the Ø34 port are open with straight access from behind, and the root-screw head band is clear.

### `scripts/check-printability.py cad/STL/simple-*.stl`
All three parts pass: nothing steeper than 45° and no flat ceilings.

These are geometry checks. Grip and creep need a physical test: set the dish at 0°, tighten everything, leave it somewhere warm for 48–72 hours, re-tighten once after the first day, then check the angle again.
