# PETAL 4.0 — modular printed reflector

A browser-based parabolic reflector generator with printer-aware segmentation, a 3D assembly view, STL kits and a standalone offline app.

Open `dist/index.html` through a local web server, or open [the offline edition](dist/petal-offline.html) directly. **The printed plastic needs a conductive front surface and a suitable feed to function as an RF reflector.**

## Start here

Use the default 400 mm curved-shell configuration for the first test article. Permanent rear ribs and sacrificial supports are on; perforations are off. Export the kit and follow its mode-specific `ASSEMBLY.md`, `HARDWARE.csv`, `REFLECTOR.md` and `INSPECTION.csv`. The app's Print notes are generated from the same instructions as the kit.

Print the small FIT_TEST joint first, then one complete petal and the center assembly. A coupon cannot establish full-petal warping or assembled dish stiffness. See [the test-article procedure](docs/TEST_ARTICLE.md).

## Revision 9 structure and joints

- The recommended ribbed shell retains the selected field thickness, rather than thinning it to 75%. Blended rear ribs reinforce its boundaries and radial centerline. These are permanent structure, distinct from the removable print supports.
- Rear locating plates enter 2 mm seats. Curved-shell panels and plates share matching flat tangent datums, eliminating the previous intentional 0.2 mm clamp gap. Faceted backs retain 0.05 mm relief.
- Same-ring seats have 0.30 mm additional radial relief per side, while the seam-normal locating clearance remains adjustable. Bolt clearance still limits actual motion. Nut fit has its own setting and no longer changes the plate or root fit.
- Removable plates permit assembling and aligning a complete ring before tightening. Continuous interlocking tongues were not selected because they can trap the final petal and accumulate shrinkage errors around a closed ring.
- The rear hub is 2 mm thicker than revision 8. This improves the geometric bearing section but does not qualify the mount for wind or creep loads.

**Regenerate the entire kit. Revision-9 parts are not to be mixed with earlier panels, plates, hubs or caps.** Keep one parameter set and material/process for a test assembly.

This revision deliberately trades more printed material for section depth and shape retention. It is not a demonstrated load-capacity improvement. See [engineering decisions and limits](docs/ENGINEERING.md).

## Hardware modes

| Mode | Assembly | Front surface |
|---|---|---|
| Round through holes | Front screws, rear nuts | Exposed seam screw heads/washers; recessed cap heads |
| Front hex pockets | Rear screws, recessed front nuts | Open nut recesses; tips must stay recessed |
| Blind heat-set inserts | Rear screws into M4 inserts installed before assembly | Closed hardware holes |

Recessed screw heads must fit **Ø8 × 2.2 mm**. Nominal M4 nuts are **7 mm across flats × 3.2 mm high**. Pockets prevent rotation; removable tape holds loose nuts during assembly.

The insert reference remains Ruthex RX-M4×8.1, with a modeled 5.6 mm hole and minimum 9.1 mm cavity. The flat front end of the insert is located 1 mm behind the cavity roof; the insert mouth can be recessed below the sloped rear surface. Use the exported depth information and a guided depth stop. Verify front-face distortion and actual screw engagement on a coupon.

`HARDWARE.csv` selects nominal stock screw lengths per hole position and quantity, with explicit engagement and front-clearance allowances. A custom-length flag must be resolved rather than rounded upward. The four mounting screws require the actual external adapter stack and are intentionally not assigned guessed lengths. The adapter, feed support and compression limiters are not supplied.

## Printing

Automatic placement prefers a supported 45° nominal petal tilt and penalizes steeper segmentation candidates. It can choose additional petals to avoid a tall, steep print. It is a geometric heuristic, not a measured print-time or strength optimizer. Manual orientations remain available.

Windowed breakaway ribs retain continuous feet and contact ridges, with 4 mm pillars on 12 mm centers and short openings. Inspect the resulting bridges, root lips, pockets and cap in the slicer. Keep supported STL shells together. Built-in supports do not cover the hub/cap or guarantee every local overhang.

Optional shared-bed packing preserves tilt and includes built-in supports. It uses bounding rectangles and 6 mm separation. Slicer-generated supports, wider brims and sequential-print toolhead clearance require separate checks. Print either packed beds or individual quantities, not both.

ASA is the preferred outdoor starting material with controlled enclosed printing. PCTG is useful for fit/handling prototypes, subject to warm-load validation. Use the filament manufacturer's settings, dry material, and inspect wall/solid-boss toolpaths. [Reflector and material guidance](docs/REFLECTOR.md) includes manufacturer references and conductive finishing options.

## RF finishing

For the first article, use thin aluminum foil tape with specified conductive adhesive, such as 3M 1170; 3M 1181 copper tape is a heavier alternative. Narrow strips accommodate the compound curve better than one large sheet. Keep joints uncoated, assemble first, and bridge seams afterward with removable conductive tape without tensioning the petals. Check adhesion, continuity and profile after finishing.

Specified silver or silver-coated-copper coatings are alternatives, but need substrate compatibility, film-thickness and resistance checks. Metallic craft paint and carbon-filled filament are not equivalent. Coating examples are not validated PETAL RF finishes.

Feed phase center, polarization, illumination and support stiffness must match the selected band and focal ratio. Perforations remain an experimental option; they are not necessary for the first article and do not imply a wind-load or RF improvement.

## Code and validation

- `dist/geometry.js`: the authoritative mesh generator, layout, permanent structure, supports, hardware schedule and STL generation.
- `dist/exports.js`: shared user instructions, reflective finish guide, manifest and ZIP/OpenSCAD snapshot exports.
- `dist/app.js`, `dist/viewer.js`: controls and preview.
- `scripts/pack-offline.mjs`: rebuild the standalone edition.
- `archive/kernel-revision-7.scad`: historical parametric source only; **not revision-9 geometry**.

All current OpenSCAD exports are exact mesh snapshots with assembly/part selection. Adjust dimensions in PETAL and regenerate. This avoids silently exporting a second, stale geometry implementation.

Run `npm test` for topology, winding, bed fit, actual mesh mating, hardware cavities, supports, connection mapping, coupons and instructions. Run `npm run test:scad` with OpenSCAD installed to verify representative exports. Run `npm run build` to refresh the offline edition. After `npm ci`, run `npm run test:ui` for the DOM-level app check (no WebGL rendering). Run `npm run docs` to regenerate the default assembly and reflector guides.

The design has not yet been physically printed, load-tested or RF-tested. Mesh checks and section-property comparisons do not establish allowable wind, torque, temperature, life or antenna gain.
