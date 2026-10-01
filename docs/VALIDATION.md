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
- **Snap-clip seams (option).** Seams can use printed clips instead of bolts, or offer both at every station.

  ![Section through a clip station, the station itself, and the clip as printed](seam-clip.png)

  - The clip is a solid trapezoid block with a channel, 10 mm wide along the seam, printed standing on its wide base so every face is 45° or steeper. It snaps straight up over both flange pads: the jaws taper from 4 mm at the channel floor to 2.5 mm at the top, and a 0.3 mm, 45° lip on each jaw hooks the pad's chamfered top edge. The pad's bottom edge is chamfered as a lead-in.
  - Each station gets a slot through the gusset for the jaw and lip. The slot's down-facing side wall and the pad ends are tilted to 45° or better in print.
  - Where the shell slopes across a clip (toward the rim, and across ring seams), the station sits lower so the jaws and lips clear the shell everywhere.
  - Clip stations are about twice as dense as bolt stations and stay clear of the keys. Short ring segments get one centered station so the clip stays clear of the corners.
  - Peak jaw strain while the lip rides over a pad is about 0.9% at the default 0.05 mm squeeze (tapered-cantilever estimate). A solid jaw only flexes a little, so the lip is kept to 0.3 mm to stay within what ASA-GF tolerates. Rough forces in ASA-GF: about 170 N to snap on and about 300 N to pull straight off.
  - The fit test includes clips at three squeeze settings.

The tests check, in every structural configuration:
- each seam bore is clear;
- a washer and nut envelope fits on each side;
- the seat has backing;
- in insert mode, the root pilot is clear with a closed roof; in through mode, the bore and seat are clear and the seat floor is solid;
- the hub front is flat, or follows the dish when curved;
- the mount seats are open;
- with clips, each installed clip clears its petal and every other assembled part, including feed parts, and pulling it 1 mm makes the lips bite the pads.

The mount check models clips as a conservative ring 26 mm deep behind the dish. With clips, the simple mount clears from −7.5° to 100° instead of −10°.

The upper (male) flange seat faces downward in print. Up to 8 petals it stays within 45°. With more petals it becomes a short overhang about 10 mm across.

Manual plate arrangements preserve copy identity, plate number, X/Y and in-plane yaw. Validation rejects missing/duplicate copies, nonfinite coordinates, out-of-volume placement and less than 6 mm between bounding boxes. Pending edits block exports. Accepted placements reach the STL plates, PLATES.csv, manifest and PDF; model/printer changes reset them. Overrides are session-local.

Validation covers 15 structural configurations, including aligned and staggered two-ring dishes, a three-ring dish and through-bolt hubs. Tests check closed/wound topology, one connected solid per part, print bounds, sampled radial insertion against actual phased neighbors, pilots and roofs, coupons and packing. Separate ring tests check paired cross-ring bore centers and axes, half-petal seam offsets, and three/four-rod registration. Plate tests exercise copy coverage, reassignment, yaw, collision/volume rejection and manifest metadata. Offline UI tests exercise the new controls, apply/reject/reset behavior and PDF download. PDF fixtures include a staggered, faceted 600 mm four-rod dish with the through-hole hub.

These are geometry/software checks. Print a three-panel staggered junction and verify alignment, tool access and side-print overhangs before committing to all rings. No measured strength improvement, slicer toolpath, wind/creep rating or RF performance is established.

Independent trimesh reload of 36 structural binary STLs confirmed watertight, consistently wound, connected positive-volume solids. Mesh cleanup now cancels coincident opposite triangle pairs, collapses sub-resolution connected edges, and refuses unresolved topology rather than exporting a broken seam.
