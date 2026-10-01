# Simple alt-az mount

![Simple mount with a 400 mm PETAL at 25° elevation](simple-mount.png)

Three printed parts and two clamp bolts. Loosen a bolt, aim, tighten it again. The clamp's squeeze holds the position.

| | |
|---|---|
| Parts | Base, rotator, cradle |
| Azimuth | 360°, 1° scale on the rotator collar, read at the post on the base |
| Elevation | −10° to 100° checked with the dish fitted. Read it with a phone inclinometer on the cradle's top web |
| Hold | One M6 split clamp per axis |
| Mounting | 3/8-16 tripod screw, or 4 × M5 countersunk on an 88 mm circle |
| Supports | None. No surface steeper than 45° and no flat ceilings, engraving included |

## How it works

![Both clamps, seen from the rear](simple-mount-clamps.png)

- **Base:** a foot with a vertical socket. The socket is slit at the rear, and an M6 bolt across the slit squeezes it shut.
- **Rotator:**
  - Its Ø50 spigot drops into the socket.
  - A 45° cone under its collar rests on a matching seat at the socket mouth, which centers it and carries the weight.
  - On top is a horizontal sleeve, slit along the top and squeezed by the second M6 bolt.
- **Cradle:**
  - Bolts to the PETAL hub with the existing 4 × M4 inserts.
  - Its Ø40 shaft turns in the sleeve.
  - Two webs, above and below the Ø34 port, connect the shaft to the hub plate. Every M4 head can be reached straight from behind.

Each bolt runs tangent to its bore and sits in a V-groove on the spigot or shaft. That has two effects:

- **Loosened:** the joint still can't lift out or slide off, so nothing drops while you aim.
- **Tightened:** the same bolt clamps the joint.

## Parts

STLs are in `cad/STL/`, already in print orientation. Load and slice without rotating.

![Print orientation](simple-mount-print.png)

| Part | STL | Prints | Size (mm) |
|---|---|---|---|
| Base | `simple-base.stl` | foot down | 110 × 110 × 80 |
| Rotator | `simple-rotator.stl` | spigot down | 90 × 90 × 147 |
| Cradle | `simple-cradle.stl` | shaft end down; the hub plate stands upright | 94 × 102 × 103 |

**Material:** print all three in ASA-GF. The clamps hold by squeezing, so creep is what lets a clamp slip over time. Glass fill creeps less than PCTG and stands up to sun.

**Print settings:**
- Brim on the rotator; it's tall.
- 6 walls around the clamps.
- 30–40% gyroid infill.
- Elephant-foot compensation on.

## Hardware

| Qty | Item | Use |
|---|---|---|
| 2 | M6 × 35 bolt, washer, hex nut | One clamp per axis. The nut drops into a pocket. A thumb screw or star knob lets you skip the hex key |
| 4 | M4 × 16 socket head + washer | Cradle → PETAL hub inserts |
| 1 | 3/8-16 hex nut | Tripod mounting. It sits in the pocket in the socket floor |
| or 4 | M5 countersunk screw | Bolting the base to a plate, pole bracket or bench |

## Assembly

1. **Base on its mount.**
   - For a tripod, drop the 3/8-16 nut into the pocket at the bottom of the socket, then thread the tripod screw up into it.
   - Otherwise, use 4 × M5 through the foot.
2. **Rotator.**
   - Drop the spigot into the socket until the cone seats.
   - Put a nut in the base's pocket on the right lug, then push the M6 bolt through from the left. It passes through the spigot's groove.
3. **Cradle on the dish.** Hub plate against the PETAL hub, 4 × M4 × 16 from behind into the mount inserts.
4. **Cradle into the rotator.**
   - Slide the shaft into the sleeve from the right until the groove lines up with the bolt hole.
   - Put a nut in the front pocket and push the bolt in from the rear.
5. **Snug both bolts:** firm by hand with a hex key. Don't crank them; the lugs are plastic.

## Using it

- **Azimuth:**
  - Loosen the rear bolt on the base and turn the dish.
  - Read the collar scale at the post on the right side of the base, then tighten.
- **Elevation:**
  - Loosen the bolt on top of the sleeve and tilt the dish.
  - Lay a phone inclinometer on the cradle's top web, then tighten. The web's flat top is parallel to the boresight, so the phone reads elevation directly.
- **Clearance:** at −10° the rim hangs 94 mm below the base's bottom (72 mm at 0°). Mount the base at least that high above whatever is under the dish.

## Loads

Each kilogram of dish and feed puts about 1.1 N·m on the elevation clamp at the horizon, with the center of mass about 113 mm ahead of the axis.

A split clamp's grip scales with bolt tension. As a rough estimate, a snug M6 at around 800 N on the Ø40 shaft gives 10–15 N·m. Treat that as a starting point for testing, not a rating.

## Validation

### `scripts/check-simple-mount.py <stl dir>`
It places the exported STLs with the same transforms as the SCAD and checks:

- **Bed fit:** closed single-body meshes that fit the bed.
- **Azimuth joint:** the rotator turns in the socket at 5 azimuths without contact.
- **Elevation joint:** the cradle turns in the sleeve from −10° to 100° clear of the rotator and its bolt. The cradle's own stop is near −22°.
- **Bolts:**
  - Each one fits its hole and sits in its groove without contact.
  - Moved 2 mm along the axis, it hits the groove wall, which shows it holds the joint in place.
- **Dish envelope:** clears the rotator and base from −10° to 100°.
- **Azimuth sweep:** at −10°, 0°, 45° and 90° elevation, the rotator, cradle and dish clear the base and its bolt all the way around.
- **Hub interface:** the 4 × M4 holes and the Ø34 port are open with straight access from behind, and the root-screw head band is clear.

### `scripts/check-printability.py cad/STL/simple-*.stl`
It scans all three parts with engraving on for overhangs and flat ceilings. All three pass.

### Limits
These are geometry checks. They don't model:

- clamp grip
- creep
- wind

The dish envelope covers the shell and hub only, so check feed rods and coax by hand at the ends of travel.

## Before trusting it outdoors

1. **Fit.**
   - Print the base and rotator first.
   - The spigot should turn freely when the bolt is loose and lock when it's snug.
   - If it still slips when snug, or binds when loose, change `clr` in `cad/simple-mount.scad` (radial clearance, default 0.2 mm) and re-export.
2. **Drift test.**
   - Set the dish at 0° elevation, where the load is highest.
   - Put a phone inclinometer on the top web, snug the clamps, and leave it somewhere warm for 48–72 hours.
   - Re-snug once after the first day, since printed parts settle, then check again.
