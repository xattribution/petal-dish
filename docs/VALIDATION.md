# PETAL 5.0 validation record

Run on the integral-flange candidate. These checks establish numerical geometry and application behavior, not physical qualification.

- Twelve structural configurations: default; optional facets; 260, 600 and 800 mm diameters; explicit two-segment petals; f/D 0.25 and 0.8; 1.6 and 6 mm shells; a 120 mm printer-height constraint; coarse mesh with 30 mm facets.
- Closed consistently oriented meshes, positive volume, one connected solid per part, actual bed/height fit and print-plane placement.
- Sampled final-petal radial insertion against both neighbors, including cross-segment neighbors; inter-segment fit and hub/petal interference. Numerical intersection tolerance 0.02 mm³ accommodates float-precision contact slivers; this is not a dimensional tolerance or exhaustive motion proof.
- Root blind-pilot void and roof, clear central passage, matching seam strips, packed quantities and 6 mm bounding-box separation.
- Twenty-seven actual binary structural STLs reloaded independently with trimesh: watertight, consistent winding, one component, nondegenerate triangles. Export quantization is included.
- Nine feed configurations plus independent hyperbolic reflected-ray/equal-path calculations, frequency sizing, rod cuts, fixed primary focus, phase offsets and automatic rod screening.
- Sampled feed-part interference on six configurations, including 4 and 8 mm rods and a steeper support angle.
- Seven OpenSCAD mesh-snapshot renders match generated part volume within 0.002 relative error and dimensions within 0.05 mm (actual errors substantially smaller).
- Offline DOM checks pass embedded WASM initialization, generation, facets, feed-frequency changes, invalid-state export disabling, reset and generated instructions. WebGL rendering is not exercised by that test.
- A 1200 mm dish with a 1000 mm print volume generates longer flanges with six screw stations per side and a matching 36-screw seam schedule; this is a generation check, not structural qualification of a dish that size.

Default 400 mm result: 6 petals + 1 hub, 2 unique parts; approximately 580.28 cm³ solid CAD volume. Side-oriented petal bounds are approximately 173.8 × 70.0 × 173.1 mm. Shared-bed packing puts three petals on each of two 220 mm beds plus one hub bed, with 8 mm edge margin and 6 mm between part bounds. Brims, supports and machine toolhead clearance remain slicer responsibilities.

A default-petal triangle-normal inspection after the orientation fixes found remaining steep downward surfaces localized to pocket/roof details (largest individual triangle approximately 6.6 mm²); this is not a layer-by-layer support check.

No physical print, slicer toolpath, FEA, wind/creep test, RF gain measurement or outdoor-life qualification is claimed. See TEST_ARTICLE.md for the next gate.

## 5.1 hosting and illustrated exports

The geometry/interface remains revision 10. The app now identifies version 5.1 and a content-derived build ID; the standalone file is `petal-5.1-offline.html`. BUILD.json lists checksums of the complete hosted asset set. The stable old offline URL is only a redirect, so downloaded standalone files should use the versioned name.

PDF fixtures cover the default dish, a 600 mm four-rod prime-focus dish with facets, and a 24 GHz secondary configuration. Tests check actual part filenames/quantities, frequency-dependent rod cuts, text bounds, text visibility after page breaks, and a readable ASSEMBLY.pdf inside the ZIP. Rendered pages were visually reviewed. Views use the generated triangle meshes and are illustrative, not dimensioned drawings; exploded separation is not an assembly motion path. Offline DOM tests also activate the PDF button and verify the downloaded PDF header and filename.

The installer-generated configuration was exercised with the official checksum-verified Caddy binary on ports 56302 and 56303: every hosted asset matched BUILD.json, cache revalidation headers were present, and missing routes returned 404. Installer argument validation and Bash syntax checks passed. Package-manager installation and boot-time systemd execution were not run on this workspace; the script targets the documented systemd distributions and preserves an existing PETAL installation on startup/health-check failure.

## 5.2 staggered rings, mount bores and plate overrides

Interface revision 11 restores half-petal staggering by default for multi-ring dishes. Both sides share the same 2n-sided polygon at each ring boundary; each outer panel meets two inner neighbors. Cross-ring flanges overlap at their common vertex and use no projecting keys. Aligned segmentation remains selectable. Flat-side print orientation and the central Ø30 mm passage remain unchanged.

The central mount offers blind inserts or four Ø4.6 mm full-depth bores with Ø10 mm flat front washer seats. Root inserts remain blind. Hardware quantities and generated instructions reflect the selection.

## Revision 12: flat seats, fastener options, flat hub

Revision 12 makes these changes.

- **Separate root and mount fastener options.**
  - Roots default to blind heat-set inserts, so the reflecting face stays closed. The through-bolt option gives each root a Ø4.5 mm teardrop bore and a Ø9.6 mm recessed flat seat in the front.
  - The hub mount defaults to through bolts and nuts; inserts are an option.
- **Flat seam seats.** The captive hex pockets are gone.
  - Each seam bolt clamps between two Ø10 mm flat seats square to the bolt.
  - The seats are cut through the gussets only, never the shell, and never through a neighbor's bolt pad.
- **Flat hub front by default.** It sits at the petal-root height; a curved front that follows the dish is an option.
- **Ring seams** use the same flange and flat-seat system as the seams between petals.
- **Flat mating faces.** The alignment keys are gone; bolts or clips align the petals.
- **Snap-clip seams (option).** Seams can use printed clips instead of bolts, or offer both at every station.

  ![Section through a clip station, the station itself, and the clip as printed](seam-clip.png)

  - With clips, each flange wall is a uniform 5 mm (no pad blocks), so each petal presents one flat face.
  - The clip is a solid trapezoid block with a channel, 10 mm wide along the seam, printed standing on its wide base. It snaps straight up over both walls; the jaws taper from 4 mm at the channel floor to 2.5 mm at the top.
  - Detent: each clip jaw carries a cylindrical bump (R 2.6 mm) straight across its full width, so the clip is a plain extruded profile. It clicks into a cylindrical groove across each wall, 0.1 mm deeper and 0.4 mm wider each side than the clip. Detent depth is a setting (0.3–2 mm, default 1 mm). The wall's bottom edge gets a lead-in chamfer.
  - Where the shell slopes across a seam (ring seams), the walls are made deeper so the full clip and, with both fasteners, the bolt seat stay on the wall.
  - Clip spots tilt to follow the shell along the seam, so the jaw tops sit 0.25 mm under the shell. Across ring seams the shell also slopes across the clip, so one side keeps a small gap there.
  - Two low vertical ridges (1 mm tall) on each wall either side of the clip stop it sliding along the seam or twisting.
  - At each clip station the 45° gusset is cut clean through over the clip and its ridges; the cut end is tilted to 45° or better in print. On radial seams, clips start past the gusset's inner ramp so the wall is full depth at every clip.
  - Where the shell slopes across a clip (toward the rim, and across ring seams), the station sits lower so the jaws clear the shell everywhere.
  - Clip stations are about 40 mm apart (the default dish gets three per seam). Short ring segments get one centered station so the clip stays clear of the corners.
  - Tapered-cantilever estimate of peak jaw strain while a jaw rides over the bump: about 1.0% at 0.3 mm, 1.6% at 0.5 mm and 3.1% at the 1 mm default. Physical testing sets the final detent depth.
  - The fit test includes clips at three squeeze settings.

The tests check, in every structural configuration:
- each seam bore is clear;
- a washer and nut envelope fits on each side;
- the seat has backing;
- in insert mode, the root pilot is clear with a closed roof; in through mode, the bore and seat are clear and the seat floor is solid;
- the hub front is flat, or follows the dish when curved;
- the mount seats are open;
- with clips, each installed clip clears its petal and every other assembled part, including feed parts, and pulling it 1 mm makes the bumps bite the grooves.

The mount check models clips as a conservative ring 26 mm deep behind the dish. With clips, the simple mount clears from −7.5° to 100° instead of −10°.

The upper (male) flange seat faces downward in print. Up to 8 petals it stays within 45°. With more petals it becomes a short overhang about 10 mm across.

Manual plate arrangements preserve copy identity, plate number, X/Y and in-plane yaw. Validation rejects missing/duplicate copies, nonfinite coordinates, out-of-volume placement and less than 6 mm between bounding boxes. Pending edits block exports. Accepted placements reach the STL plates, PLATES.csv, manifest and PDF; model/printer changes reset them. Overrides are session-local.

Validation covers 15 structural configurations, including aligned and staggered two-ring dishes, a three-ring dish and through-bolt hubs. Tests check closed/wound topology, one connected solid per part, print bounds, sampled radial insertion against actual phased neighbors, pilots and roofs, coupons and packing. Separate ring tests check paired cross-ring bore centers and axes, half-petal seam offsets, and three/four-rod registration. Plate tests exercise copy coverage, reassignment, yaw, collision/volume rejection and manifest metadata. Offline UI tests exercise the new controls, apply/reject/reset behavior and PDF download. PDF fixtures include a staggered, faceted 600 mm four-rod dish with the through-hole hub.

These are geometry/software checks. Print a three-panel staggered junction and verify alignment, tool access and side-print overhangs before committing to all rings. No measured strength improvement, slicer toolpath, wind/creep rating or RF performance is established.

Independent trimesh reload of 36 structural binary STLs confirmed watertight, consistently wound, connected positive-volume solids. Mesh cleanup now cancels coincident opposite triangle pairs, collapses sub-resolution connected edges, and refuses unresolved topology rather than exporting a broken seam.


## 5.3 system integration (2026-10-01)

- Full structural, feed, plate and ring regression suite passed, plus five integrated mount configurations: through and blind hub hardware, minimum clip elevation, 600 mm staggered rings with four rods, and a 24 GHz secondary configuration at 90° elevation.
- Mount/dish intersection checks run during generation. Shared affine transforms drive browser, SCAD and PDF assemblies. Stand clearance is calculated at the selected pose and over the permitted elevation interval; it does not replace a continuous collision test or include the stand, cables and metal hardware.
- Canonical mount STL checks passed for manifoldness, interfaces, sampled angular motion and tool access. Exported mount parts and the side-oriented clip had no >45° downward regions or flat ceilings in the triangle-normal printability scan. Slicer toolpaths remain untested.
- Clip tests cover the strain rejection threshold, broad-side print orientation, unique coupon names at tolerance limits, and installed/spare counts. The default detent is 0.3 mm; the user-entered 1.5% strain budget is not a certified material allowable.
- Offline DOM checks exercise mount controls, clip validation, manual plates and exports. Three generated PDF fixtures and ZIP contents pass text/page bounds checks; assembly and part pages were rendered for visual inspection.
- Reproducible CAD snapshots are in `docs/snapshots`: run `node scripts/render-system.mjs` then `python3 scripts/render-system.py`. These are exact generated triangles with stock rod cylinders; fasteners and stand are omitted.
- No physical prints, WebGL rendering test, RF measurement, load rating or warm-creep qualification is claimed. Follow `TEST_ARTICLE.md` before a full assembly.

## Seam levers and M4 seam bolts

- **Seam bolt size.** M3 keeps the revision 12 geometry (Ø3.4 bores, Ø10 seats). M4 cuts Ø4.5 bores with Ø11 seats and, with bolts only, 13 × 11 mm pads. Structural tests check open bores, a clear washer and nut envelope, and flat seat area for M4 with bolts only and with Both.
- **Seam levers.** These use the Both station geometry. The bundled lever, draw bar, keeper and spring meshes come straight from `cad/seam-lever.scad` (`scripts/pack-lever.py`), for M3 and M4 holes. Tests on the 400 mm default and on a 600 mm two-ring staggered M4 dish check three things:
  - every installed set clears the assembled dish;
  - the lever bears on its flange when pushed 0.5 mm toward the wall, and the spring bears on the far flange;
  - TPU springs never share a plate with rigid parts.
- **Ring junctions.** At junctions the set flips to the open side. On the 600 mm two-ring dish, 6 of 48 stations have no room on either side and are scheduled for bolts.
- **Not tested.** Clamp force, cam wear and spring creep are not modeled. Try one set on the seam strips first.


## Unification and lever motion (2026-10-03)

- The open cam tangent no longer penetrates the flange; the M3 and M4 SCAD, print STLs and embedded meshes are regenerated together.
- `tests/lever-motion.test.mjs` checks every installed open lever/bar/keeper/spring against panels and hub on 400 mm and 600 mm dishes, plus the lever at five intermediate cam angles with its pivot following the flange tangent. Cam radii come from SCAD metadata. This is sampled geometry validation, not force, fatigue, physical retention or full insertion-path qualification.
- Mount pilots are 10 mm from the tenon entry face, including the 2 mm inset. Instructions now match the check model: inserts seated 0.5 mm below that entry face. Through-bolt dish instructions distinguish the seven inserts still required by the split mount.
- Assembly/rear/staggered/exploded snapshots were regenerated for the current five-part mount.

## Feed attachment revision 3 — size and focal-ratio envelope

The rim socket pedestal is replaced by a continuous curved saddle with a flat
side-print cheek, 45-degree shoulder and front washer/nut recesses. Socket nut
housings taper into the barrels; radial webs carry the upper sockets into the
puck. Rod axes, 18 mm engagement, pocket/screw datums and rim bolt positions
retain revision 2 dimensions. The accessory manifest and instructions use
revision 3; dish joint interface remains 12.

Run `npm run test:feed-envelope` with numpy, trimesh and manifold3d available to
Python. Its 50-case grid combines dish diameters 260/400/600/800/1200 mm,
f/D 0.25/0.30/0.42/0.60/0.80, and prime-focus/Cassegrain supports. Three additional
cases exercise 4/8 mm rods, clearance extremes, four legs and phase offset.
36 cases build; 17 are explicitly rejected by the existing rod span/rise or
secondary diameter limits. Built cases span focal lengths 65–480 mm and rod
angles 2.29–62.24 degrees. Both staggered ring phases and three/four rod layouts
occur in the grid. These are sampled configurations, not a proof for every
continuous parameter combination.

Independent manifold intersections pass for rim shoe/petal, rear backer/petal,
puck/secondary, and every installed rod against its two fittings. Every exported
assembled solid is watertight, consistently wound and a single connected body.
Real M3 washers/nuts clear the rim recesses; real socket nuts clear their pockets
and sampled side-loading paths. 66 shoe/puck print meshes have at least 116.9 /
1971.2 mm² respectively of flat bed contact. The default shoe increases bed
contact from 60.0 to 204.4 mm². Its material volume rises from 3.76 to 8.10 cm³;
the default prime-focus puck rises from 34.42 to 36.16 cm³. This trades material
for broader roots and bed contact, rather than claiming a mass optimization.

The existing >45-degree overhang scanner still flags circular bores, hardware
pockets and some outer socket faces. At the default prime-focus geometry,
flagged area falls from 371.0 to 288.4 mm² for the shoe and from 809.7 to
648.0 mm² for the puck. Slicer supports/bridging and bore cleanup remain required
as appropriate; neither fitting is certified support-free. Shallow-angle cases
have more puck overhang. No printed load, screw-torque, slip, creep, wind or RF
performance qualification is implied by these checks.

The full regression suite, embedded offline UI, OpenSCAD mesh parity and
illustrated PDF/ZIP validation also pass. The one-off render scripts for this
revision were retired on 2026-10-04 and remain in git history.

## Curved seam bearing lands — bolt-only mode

The fixed-height bolt-pad blocks are replaced by full-depth bearing lands clipped
to a swept curved flange envelope. The top overlaps the petal underside; the
bottom stops inside the existing curved flange edge. A 2 mm chamfer joins one
end to the 3 mm wall, while the other retains the existing print-direction ramp.
The small 0.1 mm bottom / 0.4 mm top envelope offsets avoid coincident boolean
faces. Hole centers, flat 5 mm bearing faces, bolt/nut/washer specifications and
interface revision 12 are retained. Clip and lever flange profiles are unchanged.

The structure regression now checks that bearing lands do not protrude through
the reflector face or below the curved flange floor, and that at least 95% of
the outer washer bearing annulus remains supported. Reflector-face checks allow
for the existing tessellation chord error. The 29 structure configurations include
260/400/600/800/1200 mm dishes, f/D 0.25 and 0.8 extremes, M3/M4 bolts, multiple
rings, staggered/aligned joints, faceted rear surfaces, insertion/mating,
connectivity, hardware access and bed packing.

Before/after print scans at default, shallow and deep M4 geometry show no
significant change in overall >45-degree flagged area: approximately 314 mm²
for default/shallow, and 307 → 314 mm² for the deep M4 case. Most flags are
existing near-bed flat bridge surfaces; this is not a support-free claim.
The complete regression suite, offline UI, OpenSCAD parity and illustrated
PDF/ZIP checks pass. The before/after render scripts were retired on 2026-10-04
and remain in git history.

## Connections workspace and feed revision 4

Build `c2ebade2f49b` adds family/individual connection overrides, matched petal print variants, a joint map in CSV/PDF, accessory print inclusion, compact category controls, and viewport pan/pinch with a wider zoom range.

- The full JS suite passes, including mixed M3/M4 seam methods with staggered and aligned rings, individually selected root/mount inserts or through bolts, hardware totals, matching fit-strip pairs, and ZIP/plate exclusions.
- The feed envelope sweep covers 53 size/focal-ratio/optical-layout combinations: 36 build and 17 are explicitly rejected by stock-span or secondary geometry limits. Independent mesh checks cover connected closed solids, fitting/petal and secondary interference, rods, washer/nut seating, and captured-nut insertion paths. The 66 main fitting print meshes have flat bed contact. Residual bore/pocket overhangs still require slicer review.
- Fixed continuous feed housings replace rotated external sockets. Rod cuts use the actual housing entrances. The widened petal bearing patch replaces the rear backer and has a 45° underside perimeter chamfer. Rim fittings use M3 bolts; seam clips/levers retain their matched 5 mm flange grip faces.
- A previously failing 800 mm / f/D 0.25 Boolean mesh case now passes. Quantization repair remains bounded to 0.0001 mm; only closed numerical islands with at most 12 triangles and less than 0.00001 mm³ volume are removed.
- The embedded offline UI and export controls pass DOM tests. Navigation tests cover orbit, middle/Shift/keyboard pan, pinch, expanded zoom, fixed depth range and reset using mocked WebGL calls. A real browser/GPU viewport run was unavailable; the browser download failed.
- OpenSCAD volume/bounds comparisons pass. Default, custom, secondary and mixed-connection PDFs pass independent content and page-bound checks; representative pages were rendered for inspection.

These checks validate software geometry and export consistency. They do not replace a slicer inspection or physical fit/load test.

## Slim feed fittings — revision 5

Build `12176e22f157` replaces the bulky revision-4 housings with a thin curved
foot and tapered socket lobes blended into a 44 mm central carrier. The mounting
reinforcement footprint is narrower; its underside retains the 45° perimeter
chamfer. The default 400 mm / f/D 0.42 rim fitting drops from 31.90 to 11.47 cm³
(64%); the prime-focus carrier drops from 125.56 to 39.31 cm³ (69%). These are
solid CAD volumes, not slicer estimates. Default material-budget regression
checks prevent the fittings growing back into broad blocks.

The 53-case feed sweep builds 36 supported configurations and explicitly rejects
17 outside the existing stock-span/secondary limits. Independent intersections
and topology checks pass for reflector contact, rods, secondary, washer/nut
seating and side-loading nut paths. All 66 main fitting print meshes retain flat
bed contact. Rod bores retain 18 mm nominal engagement in 22 mm tapered sockets;
regenerate the fittings and rod cuts together.

Feed and integration tests, OpenSCAD mesh parity, the embedded offline UI, and
custom/secondary PDF content and page bounds pass. The UI check is DOM-only;
WebGL is not exercised. Residual bore/pocket overhangs need slicer inspection, and
physical fit/load qualification remains separate. The before/after render script
was retired on 2026-10-04 and remains in git history.

## Multi-view feed review — ecdc20b6aff8

The review found and corrected carrier nut-entry cuts crossing secondary stems
and an M4 screw bore stopping short inside the longer stem. Carrier nuts load
from the outer edges; the clearance bore follows the actual stem back datum.
Independent tests now guard both defects. The expanded sweep builds 40 of 57
cases, with 17 explicit limit rejections, and checks 80 fitting print meshes.
Six views per part/interface and an additional representative full orbit are
recorded in [the geometry review](FEED-GEOMETRY-REVIEW.md).

## Performance, export and editor review — 2026-10-04

Geometry and every export now run in a Web Worker started from a Blob URL, so the
hosted page and the single offline file both stay responsive; without workers the
same engine runs on the page. On a 600 mm dish switched to seam levers and the
mount, the page previously froze for 10 s in one stall and reached the final model
in 23 s; it now never misses a frame and finishes in 5.5 s (headless Chromium,
software WebGL).

Seam-lever placement tests each lever set only against the petals its bounds
reach, one at a time, in its own memory scope. A 700 mm lever dish builds in 4 s
instead of 10 s, 900 mm in 6 s instead of 25 s, and 1200 mm builds instead of
exhausting WASM memory. Mixed-connection kits build each distinct petal once
(5.4 s to 2.2 s). Every model is byte-identical to the previous engine for the
same settings, checked across 14 configurations.

The exploded view moves each part rigidly; the petal at azimuth 0 was previously
stretched about 30 % tangentially in the preview, the PDF and `exploded.png`.
Kits are deflated (about half the size), and the manual culls hidden triangles
(about half the size), draws each print plate as a numbered footprint diagram and
lists hardware as a table. The hub front is flat by default again, as revision 12
specifies.

## 5.3 Gregorian collector and tripod base (2026-10-05)

**Collector optics.** An independent tracer, `node scripts/check-collector-rays.mjs`, sends 60 vertical rays per case onto the parabola, through F1, onto the generated bowl profile, and reflects them with the exact ellipse normal. Seven cases: 400 mm at f/D 0.25, 0.3, 0.42, 0.6 and 0.8; 800 mm; and 24 GHz with an 8 λ bowl.

| Case | Bowl Ø | Rays on bowl | Missed | Shaded | Hit off ellipse | Miss at F2 | θ at F2 |
|---|---|---|---|---|---|---|---|
| 400, f/D 0.42 | 85.3 | 47 | 0 | 13 | 0.0012 mm | 0 | 25° |
| 400, f/D 0.3 (pedestal) | 65.3 | 50 | 0 | 10 | 0.0013 mm | 0 | 25° |
| 400, f/D 0.6, θ 20° | 96.9 | 46 | 0 | 14 | 0.0009 mm | 0 | 20° |
| 400, f/D 0.8, θ 15° | 97.6 | 45 | 0 | 15 | 0.0006 mm | 0 | 15° |
| 400, f/D 0.25, θ 45°, 20 mm insert | 70.9 | 49 | 0 | 11 | 0.0019 mm | 0 | 45° |
| 800, f/D 0.42 | 121.3 | 51 | 0 | 9 | 0.0016 mm | 0 | 25° |
| 400, 24 GHz, 8 λ | 99.9 | 45 | 0 | 15 | 0.0014 mm | 0 | 25° |

"Shaded" rays fall inside the bowl's own radius and never reach the dish. The unit tests add the focal-sum identity on every profile point, the ellipse magnification `(1 + e)/(1 − e)`, the shadow condition, and the reshaping between f/D 0.3 and 0.6.

**Collector parts.** The rod seats are built into the bowl wall between its flat top and the rim. Across 14 dish sizes and focal ratios (260–1200 mm, f/D 0.25–0.6) every bowl, mast foot, insert cup and pedestal is a closed mesh and passes the 45° overhang scan in its export orientation (38 parts). A ray cast up from F2 against the printed bowl mesh lands on the ellipse within 0.01 mm at 336 points per bowl for four dish shapes, and no part of the bowl or its seats sits below the rim. Default part sizes: bowl 91 × 87 × 21 mm (34 cm³), foot 76 × 76 × 32 mm (34 cm³), cup 35 mm (12 cm³).

**Tripod base.** The app starts from the base blank, unions three octagonal sockets, then cuts the azimuth bolt and nut pocket for the clamp size, the bores and the teardrop cross holes. The result passes the overhang scan for 8 mm legs at 10°, 20 mm at 20° and 25.4 mm at 30°. Integration tests cover socket fit for every leg size, the automatic M3/M4/M5 bolt selection, the M3–M6 override and its minimum leg size, leg length falling with splay and rising with a heavier dish, and the exact pose check (default dish at −10° touches leg 1 facing it, clears at 60° azimuth).

No RF measurement, wind test or physical print of the collector or tripod is claimed.

## Raised flat hub, sizing goal and bolt sizes (2026-10-06)

**Flat hub.** The flat front now sits level with the petals at the hub's corners, where the inner petal edge is highest (zAt(45 / cos(π/n))), and chamfers down along each edge to meet the petal. The step between hub and petal along every edge is within ±0.1 mm on the default dish and ±0.25 mm at 260 mm, f/D 0.25 (the chamfer is sampled every 0.5 mm radially). The default hub center now stands 4.02 mm above the vertex instead of 3.01 mm, up to 3.68 mm above the parabola at the center opening. Structure tests check the face height and that the face is flat outside the mount seats. Two lengths follow: the default hub-to-adapter screw is M4 × 35 (was 30) and the Gregorian mast is cut 71.8 mm (was 72.8).

**Automatic sizing.** Packed beds holding petals or the hub, for the two goals:

| Dish / bed | Largest petals | Fewest print plates |
|---|---|---|
| 400 / 220 (default) | 6 × 1 ring · 3 | 6 × 1 · 3 |
| 500 / 256 | 6 × 1 · 3 | 6 × 1 · 3 |
| 600 / 220 | 8 × 2 · 5 | 8 × 2 · 5 |
| 600 / 300 | 6 × 1 · 3 | 6 × 1 · 3 |
| 800 / 256 | 10 × 2 · 7 | 10 × 2 · 7 |
| 900 / 300 | 10 × 2 · 4 | 10 × 2 · 4 |
| 1200 / 350 | 12 × 2 · 8 | 12 × 3 · 5 |

Fewest plates ranks candidates on a packed estimate, then packs the real petals and only switches when they need strictly fewer beds. Without that check the estimate picked 8 petals over 6 for 600 / 300, with no saving.

**Bolt sizes.** Petal roots and hub-to-mount bolts take M3, M4 or M5; the mount takes M3–M5 joint screws, M6/M8/M10 clamp bolts and M4–M6 stand screws; tripod leg bolts are automatic or M3–M6.

- The app bundles mount blanks with no fastener holes and cuts them itself. At the default sizes the cut parts match all eight complete STLs within 0.16 mm³ (float32 rounding), with the same number of holes.
- `cad/simple-mount.scad` with `joint_m = 5`, `clamp_m = 10`, `stand_m = 6`, `hub_m = 3` matches the app's parts within 0.12 mm³.
- Mount parts at the smallest and largest sizes, without the base, and the tripod base with M10 clamps and M6 leg bolts pass the 45° overhang scan.
- OpenSCAD snapshot round trips pass for an M5 through-root / M3 mount hub, an M3-root petal, an M10 / M3 cheek and an M6 / M5 yoke.
- Integration tests check hole diameters for every size, the hardware lengths and the leg bolt's minimum leg size.

**Seats.** Through-bolt seats on the dish front can be recessed (default) or plain holes. A plain seat keeps only a flat spot face at the lowest point under the washer: none on the flat hub, up to about 1.5 mm at the outer edge of a root seat. On the mount, the cheek's head pockets, the base's azimuth nut pocket and the no-base stand countersinks can be plain holes. `cad/simple-mount.scad` with `head_pockets`, `nut_pocket` and `stand_countersink` set to false matches the app's parts within 0.12 mm³. The plain parts pass the overhang scan; the plain-seat petals flag exactly the same areas as the default petals. Integration tests check that each pocket or countersink is present or solid as selected, and that the bolt lengths grow (elevation M8 × 50, azimuth M8 × 40, arc lock M6 × 50).

**Hub clearance.** An optional 0–0.6 mm moves the hub's edges in and enlarges the hub's root holes by the same amount; the mount holes do not move. The hub chamfer now reaches the petal's edge height exactly at the hub edge. Measured step from hub to petal across the gap: 0.10 mm on the default dish (0.11 mm with 0.3 mm clearance) and 0.25 mm at 260 mm, f/D 0.25 (0.31 mm with 0.6 mm clearance), most of it the parabola rising across the gap. Structure tests check the hub edge line and the hub's root hole size.

## Counterbores, nested plates, plate moves and a review pass (2026-10-08)

**Socket heads in counterbores.** The mount's joint and stand screws are ISO 4762 socket heads, which have a flat underside, in counterbores 1 mm wider than the head and 0.5 mm deeper. The heads sit below the faces that the next part or the dish hub bears on, so a flat face mates with a flat face. Where a counterbore opens toward the bed, its ceiling steps up in two 0.4 mm layers, a slot as wide as the hole and then a square, so each layer bridges a short span. Joint screws are M4 × 10 into the upright and M4 × 12 into the cheek, and the same lengths hold at M3 and M5.

- `scripts/check-simple-mount.py` passes: heads 0.5 mm below the face, tips short of the pilot ends, joints flush (29.6 cm² and 6.2 cm²), azimuth contact 100 cm², travel −10° to 100°, hex key access −10° to 30°.
- `cad/simple-mount.scad` matches the app's cut parts in 23 comparisons (sizes, plain seats, `stand_counterbore = false`), within 0.12 mm³.

**Overhang criteria** (`scripts/check-printability.py`). A part fails on any downward face more than 60° from vertical, except a flat ceiling (within 20° of flat) no more than 10 mm across, which prints as a bridge, and a step within 0.3 mm of the bed. Faces between 45° and 60° are listed as minor: at 0.2 mm layers each layer steps out at most 0.35 mm. Normals of faces under 0.005 mm² are ignored. Every part below passes:

- the eight mount STLs, and app-exported mount parts at the smallest and largest sizes, with plain seats, without the base, and the tripod base with M10 clamps and Ø25.4 mm legs on M6 bolts (largest bridge 6.3 mm);
- petals and hub with bolts, M4 bolts, clips, both and seam levers, two-ring 600 mm dishes with clips and with both, through-bolt roots and plain seats (largest bridge 8.4 mm, a clip window roof on a ring petal; largest minor area 121 mm², on the same petal);
- the clip and the four seam-lever parts.

The clip stations were reshaped to get there: the guide ridges slope toward the clip, each window is wider by the ridge, bolt-hole roofs point along the true print-up direction at each station, the blind root pilot has a teardrop roof, and the flange end is trimmed square.

**Nested plates.** Packing now uses each part's outline in every 4 mm height band instead of its bounding box. Plates holding petals or the hub, largest-petals sizing, bed height the larger of 250 mm and the bed width:

| Dish / bed | Bounding boxes | Nested |
|---|---|---|
| 400 / 220 (default) | 3 | 2 |
| 500 / 256 | 3 | 2 |
| 600 / 220 | 5 | 4 |
| 600 / 300 | 3 | 2 |
| 800 / 256 | 7 | 4 |
| 900 / 300 | 4 | 4 |
| 1200 / 350 | 8 (5 with fewest plates) | 4 |

With nesting, the fewest-plates goal picks the same layout as largest petals in all seven cases. Clearance is checked independently of the packer's grid: `tests/structure.test.mjs` slices every pair of parts on each plate every 1 mm up their shared height, grows each section by 2.95 mm and requires that they never meet, across all 32 structural configurations.

**Moving parts between plates.** `tests/plates.test.mjs` moves a copy onto a new plate and back (the emptied plate is dropped), moves one within its plate, rejects invalid targets, and re-checks every result through the untrusted manual path. On 437, 500 and 700 mm beds (1.5 and 3 mm grids) a part 0.2 mm narrower than the usable area still packs and nothing reaches past it. In headless Chromium, dragging a petal from plate 1 onto the empty slot made a third plate; changing the seam bolt size then kept that arrangement, and a click on empty space cleared the selection, with no console errors.

**Printer presets.** Prusa CORE One L 300 × 300 × 330 mm and CORE One L+ INDX 298 × 275 × 330 mm (Prusa product pages); Bambu Lab H2D 325 × 320 × 325 mm and H2C 325 × 320 × 320 mm, their single-nozzle volumes (Bambu Lab H2 series page).

**Review fixes.**

- **Meshing.** Some rod-mount petal sizes failed with "unresolved seam": the kernel works in double precision and two surfaces micrometers apart shared a point once written as Float32. Meshing now welds only exact duplicates first and moves pinched vertices 0.2 µm apart. 556 mm prime focus, 478 and 520 mm with four rods, and 513 mm Cassegrain at 74 GHz with four rods now build; the first two are in the feed tests.
- **Hub mount bolts with the collector.** The grip ran to the recessed seat floor, 5 mm short, though the mast foot sits on the hub front. It now runs to the top of the foot's flange: M4 × 30 plus the adapter instead of 25.
- **Leg cross bolts** count ISO 7089 washer thickness for their size instead of 1 mm.
- **Speed.** A change to the aiming mount or its pose reuses the dish's segmentation, petal and hub meshes (an elevation change on the default dish with the mount: 1.5 s to 0.4–0.6 s). The hub's holes and the collector's seat bores are cut in one subtraction each (default build 1.25 s to 0.96 s). Fewest-plates sizing ranks layouts on a 2 mm grid (1000 mm dish on a 300 mm bed: 10.4 s to 6.9 s of planning). Each petal and the hub free their intermediate solids before the next starts.
- **Interface.** Typed plate positions are checked in the engine. A hand arrangement survives a rebuild when every copy still fits, and the page says so when it does not. A drop that cannot be placed says why. Status messages appear over the preview on every screen size. Field tips describe their control to screen readers instead of joining its name. Hidden or disabled fields no longer block a build, errors name the field by its label, an empty inch field is no longer read as zero, and the selection and keyboard focus survive a rebuild.

## Large dishes with rods, largest dish on N plates, clamp sides (2026-10-08)

**Rod supports on large dishes.** A 1200 mm dish with a rod support used to stop with "Rod geometry is outside the compact fitting envelope", because a rod from 20 mm inside the rim to the focus is longer than 600 mm. The rods now start further in when that helps: the outermost radius whose rod is 580 mm or shorter and no steeper than 62°, on whichever ring holds that radius with room for the socket. Otherwise they stay at the rim, and no rod may be over 1 m. Automatic stock sizing now goes up to 10, 12 and 12.7 mm. All of these build on the CORE One L, CORE One L + INDX and H2D volumes:

| 1200 mm dish | 3 rods | 4 rods |
|---|---|---|
| Prime focus | rods from r 370 mm on ring 2 of 3, 580 mm cut | same datum, ring 2 of 2 |
| Cassegrain | rods from the rim, 679 mm cut (moving in would save little) | builds |
| Gregorian collector | rods from the rim, 659 mm cut | builds |

On a 220 mm bed, three rods still have no fitting petal count at 1200 mm (12 petals are too large and 18 is past the 16-petal limit), and the Cassegrain secondary is larger than the bed; both are reported. The feed tests add the 1200 mm cases, check each rod, its angle and the socket ring, refuse a 1200 mm dish at f/D 0.8 (rods over 1 m), and the deflection screen still rejects a 2 kg payload at 50 GHz. The feed envelope sweep (64 cases: 58 build, 6 are refused with a reason), with its independent mesh, fit and overhang checks, passes.

**Largest dish on N plates.** The search runs on packed estimates and then builds the answer for real:

| Printer | Plates | Result |
|---|---|---|
| 220 × 220 × 250 | 2 | 415 mm, 6 petals × 1 ring |
| CORE One L | 2 | 670 mm, 8 × 2 |
| CORE One L | 4 | 990 mm, 10 × 2 (17 s in headless Chromium) |
| H2D | 6 | 1200 mm, 16 × 2 |

With the mount fitted on a 220 mm bed, two or three plates are refused with the reason: the mount alone takes three.

**Elevation clamp sides.** Left mirrors every part with a side; both sides adds a mirrored upright and cheek and a dual yoke and cradle (`cad/STL/simple-*-dual*.stl`, `simple-*-left.stl`). `scripts/check-simple-mount.py` checks the mirror images, both joints, both clamps (36–38 cm² each over the range), both bolts, travel from −10° to 100° with and without the arc lock, dish and azimuth clearance, hex-key reach, the hub interface and the no-base stand screws: all pass. The app's cuts reproduce the new complete STLs, and the SCAD matches the app at non-default sizes. The new parts pass the overhang scan. Holding estimates and the reasoning for the left side are in SIMPLE-MOUNT.md.

**Gap between parts.** The packing gap is now a setting (3–30 mm, default 6) so wide brims stay separate. The independent slice check in the structure tests uses the setting, and a 600 mm dish at a 16 mm gap is among its cases.

**Printing large petals.** Research and settings for long ASA-GF, ASA-CF and PCTG prints are in PRINTING-LARGE-PETALS.md. No geometry change was made for warping: thickness mostly changes stiffness in use, and a cross rib would not shorten the long layer paths that drive warping.

## Configuration codes and automatic rod count (2026-10-09)

**Configuration codes.** `dist/config.js` turns a design into a code (`P1.` and deflated JSON in URL-safe base64) and an 8-character ID. The code holds every setting that differs from a frozen P1 baseline, joint overrides, accessories left off the plates and any hand arrangement of the plates. `tests/config.test.mjs` checks:

- Exact round trips for plain settings, joint overrides, print selection and a hand-moved hub, through a real build: the rebuilt model gives the same code, rows and positions within 1e-6 mm.
- Key order and float noise do not change the code or the ID, and 72 different settings give 72 different IDs.
- Links, codes split over lines with text around them, saved files and kit `parameters.json` (with a code, or an older kit's parameters only) all load. Unknown settings are reported and skipped; out-of-range settings, damaged codes and codes from a newer format are refused with a reason.
- A frozen fixture code made today still loads as the same design and ID.

Typical lengths: the defaults are 11 characters, a 600 mm CORE One L dish 18, a 1000 mm dish with feed and U mount 49, and a 600 mm kit with a hand-moved hub 218. The UI test covers the header ID, copy, save, load from a code, the saved list, a file and a link, plus refusals. A Chromium run checked the clipboard, a link opened in a new tab (same ID), a reload keeping the design, and the header at 360, 390 and 1440 px wide with no horizontal scroll. The PDF check finds the ID on every page and the full code on the build record page.

**Automatic rod count.** The default rod count is now automatic: four rods with 8, 12 or 16 petals and three with 6. Ten and fourteen petals cannot space rods evenly, so automatic segmentation skips them while a feed is on, and choosing them by hand is refused with a reason. A fixed count of 3 or 4 is still available. With automatic rods, a 1200 mm prime-focus or collector dish now builds on a 220 mm bed (16 petals × 3 rings, 4 rods, 580 mm and 659 mm cuts); the Cassegrain secondary is still larger than that bed.

## Rim band and underside ribs (2026-10-09)

A first ASA petal flexed at its free rim edge each time the nozzle reached the end of a layer and turned around. Each outer petal now has a **rim band** by default: a 3 mm wall behind the rim, 14 mm deep, so every print layer ends in a hook. U (a round bend toward the hub with a 2 mm lip) and triangle (a 45° brace back to the shell) are options, as are **diamond-grid ribs** on the underside. Geometry: `dist/stiffeners.js`; the print-layer sections and a rear view are in PRINTING-LARGE-PETALS.md.

`tests/stiffeners.test.mjs` checks nine designs: L, U and triangle on 400 and 600 mm dishes with 6 and 8 petals, deep (f/D 0.25) and shallow (0.8) dishes, 6 and 30 mm depths, the faceted rear, two staggered rings with levers, and a prime-focus feed with clips and ribs. For every petal:

- The solid and its print mesh are closed manifolds.
- No face is steeper than 45° in the side print beyond those the plain petal already has. With 6 petals the band leans inward (37°) to keep that; from 8 petals up it is parallel to the dish axis.
- Nothing reaches in front of the reflecting face (the band is cut back to the shell's rear surface).
- The last seam station keeps clear of the band by its hardware's size, and a seam keeps its station count or loses one only where it would crowd.
- Assembled, the outer petals' bands close into one ring: a circle through the middle of the wall, half way down, is at least 97% inside material.

The structure tests run with the L band (now the default) in every case and add U, triangle and rib cases with bolts, clips, levers and staggered rings, so clip fit, lever clearance, bolt seats, mating, radial insertion and packing are all checked with the stiffeners on. A band that closes into a tube (the triangle, or a U on a deep dish) encloses a sealed void between the seam flanges; the connectivity check now counts only solid pieces. The default 400 mm dish still packs on two 220 mm beds, and the feed envelope sweep runs with the band on.

**Full-width root boss (2026-10-09).** A printed hub-end petal showed the root boss stopping about a centimeter short of the upper flange, leaving a thin free edge between them. The boss now spans the whole hub end, seam to seam, and the upper radial flange of a hub-end petal runs at full depth from its start, since the boss now carries it (it used to thin out over its first 30 mm so its end would print). Across 6, 8, 10, 12 and 16 petals, with bolts, clips, levers and through-bolt roots, the steep-face area of the print mesh is unchanged except 16 petals, where the full-depth flange start adds 30 mm² of the 0.5 mm step its gusset already has along the rest of the flange. The structure tests' hub fit, root seat, insertion and packing checks pass with the wider boss.

**Square rim band with a 45° foot (2026-10-09).** The band no longer leans inward on 6-petal dishes; it stays square to the dish (parallel to its axis) with any petal count. The triangle option is replaced by the frame, now the default: the wall, then a round 45° bend toward the hub and a 5 mm lip. The wall depth setting is the straight wall; a U or frame foot adds about 6 mm. The stiffener tests now check that away from the band no face is steeper than on the plain petal, and that no band face is steeper than the square wall must be: twice the petal's half angle near the top of the side print, 60° with 6 petals and 45° from 8 up. All nine designs pass, and the bands still close into one ring.

**Front lip and center ring rib (2026-10-09).** The rim band's wall now continues above the reflecting face as a front lip, 1 mm by default (0–3 mm), following the face. Underside ribs gain a center ring: one ring rib half way along each petal, 3 mm tall by default (2–6 mm), alone or with the diamond grid. The stiffener tests now also check that the only material in front of the reflecting face is the lip, on the rim's outer 3 mm, at the height set, and they cover the ring alone, the ring with the grid, and a 3 mm lip. The structure tests add a clip case with both ribs and a 3 mm lip. The default 400 mm dish still packs on two 220 mm beds with the 1 mm lip; with a 3 mm lip it needs three.
