# Design review, November 2026

Five independent reviews looked at PETAL: code correctness and speed, UI and UX in a real browser, printability of every exported part, and two separate studies of tool-less petal joints. This page records what they found, what changed, and what is still open.

Nothing here is a load, weather or RF rating. All numbers are calculations or measurements of generated geometry, not of printed parts.

## Why the petals need screws

The clips do hold petals that already meet. What they cannot do is pull apart edges together, and the reviews found two separate reasons the edges are apart.

### 1. The petal's angle, from uneven shrinkage

A petal prints standing on one seam flange, so one seam face lies on the bed and the other leans up at 360°/n.

- If the printer shrinks height (Z) differently from width (XY), every petal comes out with a slightly wrong included angle. Most printers do, and fibre-filled ASA does a lot.
- The hub prints flat, so its angles stay exact.
- The angle errors add up around the ring into a wedge-shaped gap: zero at the hub and largest at the rim.
- With 0.3% more shrink in Z than in XY:

| Dish | Petals | Gap per seam at the rim | Around the ring |
|---|---|---|---|
| 400 mm | 6 | 0.26 mm | 1.6 mm |
| 600 mm | 6 | 0.39 mm | 2.3 mm |
| 1000 mm | 10 | 0.71 mm | 7.1 mm |
| 1200 mm | 12 | 0.78 mm | 9.4 mm |

This looks exactly like warped petals that have to be pulled together. Closing it takes hundreds of newtons of hoop strain: within an M3 screw's preload, and far beyond a clip.

**Changed:** a new **Plates → Petal Z scale** setting scales only the side-printed parts along print Z.
- Every kit's FIT_TEST folder now has `sector-scale-gauge.stl`, the outer 14 mm of an outer petal printed exactly like the petal.
- `SECTOR-GAUGE.md` turns two caliper readings (length along the bed, and height) into the setting. See [PRINTING-LARGE-PETALS.md](PRINTING-LARGE-PETALS.md).

### 2. The clip cannot draw a gap closed

Both joint studies reached the same numbers independently.

- **What a warped edge needs.** Flattening a 1–2 mm bow over 150 mm takes 50–240 N per edge. The edge stiffness is EI ≈ 2,200–4,400 mm⁴ × 1.6–2.0 GPa.
- **What the clip gives.** It is a fixed channel with zero draw. Its clamp is about 5–6 N per jaw, from a 0.05 mm squeeze. Print error of ±0.2 mm swings that from 0 to about 32 N, and the detent pops at about 33–50 N.
- **Why no snap can do better.** At the 1–2% strain printed PETG, ASA or PCTG can take, a station-sized spring stores 20–40 N·mm. Holding 100–200 N across a 0.4 mm tolerance band needs about 80 N·mm. No clip tuning fixes that.
- **Why bolts work.** A thread pulls the walls together by any distance and stops rigidly. The bent petal itself is the spring.

## Tool-less joints: the recommendation

Both studies recommend the same architecture.

1. **A V-ridge and groove register on every seam.** The ridge is on the upper (gusset) flange face and the groove in the bed-side face, with lead-ins so the last petal still slides in radially. It lines up the front-surface step to about ±0.1 mm and turns clamp force into alignment.
2. **A keyed slot station** through both flanges. It takes:
   - **A printed T-key and a 1:12 self-locking wedge**, pushed home by thumb. This gives 70–165 N of clamp by thumb and 215–310 N by palm, with 2 mm of draw. It is the default, tool-less fastener.
   - **The same slot for the bad seams:**
     - an M4 screw with a printed Ø12 thumb nut, giving 250–500 N by fingers with unlimited draw;
     - or a 3.6 mm UV-stabilised cable tie, giving 100–200 N.
3. **Verdict.** A printed elastic snap can't replace screws for warp correction. Tool-less positive draw (wedge keys, thumb nuts, ties) can, for typical warp, provided the register supplies the alignment. Fix the angle and the bed lift at the printer first.

**Added:** **Joints → Wedge keys · tool-less** (seam mode 4), with a fit-test set. See [WEDGE-KEY.md](WEDGE-KEY.md). How it differs from the studies:

- **The register is in straight segments between stations, not continuous.** The last petal slides in radially. A ridge that follows the curved shell would drift off its groove during that slide by more than the flanks allow at 8 or more petals.
- **The wedge is 29 mm, not 32 mm,** so that neighbouring sets fit the 34 mm station pitch.
- **Some stations get thumb screws.** Where neither orientation of a key set clears the neighbouring flanges, the station takes the M4 thumb-screw option, and the kit includes the drift keeper and thumb nut for it. That is 8 of 56 stations on a 600 mm two-ring dish, all next to ring junctions. Single-ring dishes had none in the tests.
- **The kit is fixed at 0.20 mm key clearance and a 1:12 taper.** If the fit test favours another, print that FIT_TEST part in quantity.
- **Builds are slower:** about 10% on the default dish and 40% on a 1200 mm dish, from the placement checks.

All of this is untested in print. The clamp figures are hand calculations.

## Fixed in this review

- **Seam station pairing.** The two halves of a station could round to different 0.1 mm keys. Single-joint overrides then failed on some dishes, such as 600 mm with levers.
- **Joint IDs on odd diameters.** On 6-petal dishes the two petals of a seam rounded to different IDs. The editor listed phantom one-sided seams, and kit export failed whenever any override was set.
- **Faceted rear with staggered rings and clips or levers** did not build. The two halves of a ring seam now compute the same station levels.
- **Manifest and guide with overrides.** The manifest crashed when overrides removed every clip. The guide dropped the fastening steps for levers or clips brought in by an override.
- **Mixed hardware list.** Plain and small washers are now counted separately. Lever sizes whose stations all fell back to bolts no longer ship two spare levers.
- **Tripod leg sockets.** A tapered root collar now carries leg bending into the plate. It adds no overhang and keeps clear of the azimuth-nut pocket.
- **Speed:**
  - **Gregorian rod datum:** the cache no longer depends on the aiming pose, and the radius search is bounded by the steepness limit. Results are identical over a 120-configuration sweep; the sweep dropped from 201 s to 12 s, and one large dish from 23 s to 0.3 s.
  - **Pose-only rebuilds:** they reuse the feed and mount meshes. A Gregorian dish with a tripod went from about 2.9 s to 0.4 s per elevation step.
  - **The planner:** it reuses the winning petals and skips layouts whose seam is longer than the bed diagonal. 1000 mm builds went from 5.6 s to 3.3 s, with identical output.
  - **Rods over 1 m:** they are now rejected before building anything, with a hint to lower the focal ratio.
  - **Engine failure:** after a fatal error, calls fail at once instead of leaving the page on "Updating…".

## Open recommendations

These were measured, but not changed yet. Each needs a design decision or a test print first.

| Item | Why | Where |
|---|---|---|
| Same flange section on both seam edges | The upper flange has a 45° gusset, about 125 mm² of section; the bed flange is a 3 mm slab plus chamfer, about 50 mm². The two edges of one petal bow differently. Gusset the bed flange too (+6% material), or core the upper one | `flange()` in `dist/geometry.js` |
| More bolt stations, one near the rim | Bolt mode places 2 per seam up to 240 mm: 68 mm apart on 400 mm, 118 mm on 600 mm, 34–59 mm short of the rim. The rim is where the wedge gap and bed lift are largest. One per ~60 mm plus one at the rim band | `countOf` in `flange()` |
| Upper-flange bolt seats at 10+ petals | The seat faces down at 54–67° from vertical, a real overhang under the nut and washer. Put the head on the bed-side flange and an insert in the upper gusset, or roof the seat at 45° | `flange()` seats |
| Reflector edge ledge on the bed seam | The shell is set back 0.2 mm, so layer 2 is about 2.2 mm wider than layer 1 along the whole seam, and a brim welds to the reflector edge. A small front chamfer, or a rear part flush with the flange face | `clipped()` in `dist/geometry.js` |
| Rim band at 6 petals | Its inner face reaches 58–60° from vertical near the top of the print. The repo's printability check fails the default 600 mm petal on it. Cap it at 45°, or prefer 8 petals at 600 mm and up | `dist/stiffeners.js` |
| Cassegrain secondary | It prints with no flat bed contact. A support-free plano-convex back puts the rim 0–15 mm above the boss, so the stem would have to be re-solved. Today: support it in the slicer | `dist/feed.js` |
| Kit export speed | The PDF views (about 2 s) and re-welding the plate STLs (about 0.8 s) dominate | `dist/manual.js`, `binarySTL` |
| Try printing outer petals rim-down | Both seams would rise as identical walls and the rim band would become a long foot. Radial bending then crosses layers, and the root needs a 45° underside. Print one petal each way before deciding | – |
