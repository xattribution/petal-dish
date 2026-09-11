# PETAL — Parametric Expeditionary Tactical Aperture Lab

A dependency-free parabolic dish generator with a 3D assembly preview, procedural STL export, editable OpenSCAD source, and a single-file offline edition.

## Run it

Download [dist/petal-offline.html](dist/petal-offline.html) and open the downloaded file in a browser. No installation, server, CDN, or connection is required. For development, serve `dist/` with any static HTTP server. All geometry and downloads are generated locally in the browser.

Set dish diameter, focal ratio, rear construction, joint system, and printer volume. Inspect the assembly, exploded view, rear view, or oriented print parts. Export individual STLs or a ZIP containing all unique parts, quantities, OpenSCAD source, parameters, and assembly instructions.

## Staggered rings and adaptive fasteners (2.1)

Alternate rings can rotate by **half a petal**, or `180 / petal_count` degrees. An eight-petal layout uses 22.5°, a ten-petal layout 18°. A fixed 90° rotation would leave seams aligned for some petal counts. The new layout interrupts continuous radial seams while keeping the same parabolic surface.

Each petal overlaps two neighbors in the next ring. Four-bolt saddles connect both overlaps. Faceted-back saddles follow the actual orientations of both adjoining rear faces; they are not merely rotated copies of aligned fittings. Identical curved-back ring saddles share one STL with the required quantity.

**Adaptive connectors** add saddles along long side seams and long ring overlaps. The default target spacing is **150 mm**, adjustable from 60–180 mm. Smaller spacing increases hardware count. Placement reserves room for the center hub and neighboring fittings, and rejects overlapping sockets. This is a construction rule based on geometry, not a structural or wind-load calculation.

The app reports ring offsets, saddle count and assembly bolts. Exported manifests include every part's assembly rotations and its OpenSCAD part/ring/station selection. Side saddle stations are numbered from center toward rim; ring saddle stations run counterclockwise within an inner petal. In OpenSCAD, select the one-based **station** to render each required fitting.

Turn off both staggering and adaptive connectors to reproduce the preceding keyed layout. Legacy two-bolt straps remain aligned and use their original placement. Changing these settings requires regenerating matching panels and fittings. Staggering is intended to improve load distribution, but no physical strength improvement has been measured.

## Revision 2: keyed joints and indexed hub

The default connection now separates location from clamping:

- **Four-bolt saddles:** four spaced M4 bolts replace the narrow two-bolt seam straps. Angular and radial seams have matching saddle shapes.
- **Locating tongues:** four 8 mm shoulder tongues enter rear sockets, resisting lateral slip and twisting through bearing contact. Bolts supply the clamping force.
- **Reinforced sockets:** 12 mm docking pads add 3 mm behind the original shell. Sockets stop 0.4 mm short of the original rear surface, preserving nominal shell thickness outside the through-bolt windows.
- **Adjustable fit:** clearance is 0.2 mm per side by default, adjustable from 0.1 to 0.4 mm. Tongues are 2.6 mm tall. The assembly includes 0.2 mm axial clearance.
- **Rear installation:** all joint pieces install from behind. The final petal can be lowered into place without sliding along a closed ring of continuous tongues. This is discrete tongue-and-socket registration through the saddle, not a continuous tongue-and-groove edge.
- **Indexed center:** one locating shoulder per petal root, a flat rear mounting face, and a slimmer 116 mm OD front clamp with a 94 mm opening.
- **Wider mount pattern:** custom four-M4 pattern on a **60 mm bolt circle**, clocked at 45°, with a 30 mm center opening and 120 mm hub OD.

**Revision 2 requires a matching full kit.** It does not mate with legacy panels, straps, or 40 mm BCD adapters. Select **Legacy two-bolt straps** for old designs. Changing dish geometry, petal count, rear style, or joint generation requires regenerating the matching fittings.

The redesign adds bolts and local material. The load path is intended to reduce dependence on friction at narrow straps; no numerical strength improvement or load rating is claimed. Print one joint to establish clearance and check physical fit before committing to a full dish. Do not coat mating sockets before testing them.

## Dish and printing geometry

The front follows `z = r² / (4f)`, where `f = diameter × f/D`. Panels are annular sectors with a 45 mm inner radius. Automatic segmentation searches even counts from 6–32 and up to 12 radial rings, minimizing panel count while reserving room for fittings and checking print volume. Keyed layouts reject socket crowding, so not every manual petal count is available for every dish.

Choose a curved rear or two planar rear faces with a 10–15° change of slope. Thickness is measured vertically. Faceted corners may be considerably thicker; local docking pads are additional. Solid CAD volume is not a filament estimate—slicer infill determines material use.

Automatic orientation prefers 60° for keyed joints, faceted backs, or built-in supports; it tries 55°, 50°, 45°, and 65° if needed. Bed rotations are searched in 15° steps. Legacy curved shells without supports retain low-profile auto orientation. Explicit angles remain available. This is a geometric fit heuristic, not a simulation of layer strength or printing reliability.

Optional breakaway ribs have flared feet, 1.2 mm webs, serrated contact ridges, and adjustable clearance. For keyed panels, ribs follow slices of the actual mesh, including docking pads. Supported panels are lifted 5.6 mm above the bed; support feet start at Z=0. Keep all STL shells together in their original positions. Inspect the slicer layer preview before disabling generated supports; individual docking pads and socket ceilings may still need support. Test the contact settings with your material and layer height.

Saddles and the revised rear hub have flat printing bases. The front clamp and legacy fittings may need slicer support. The hardware list reports grip stacks before washers, nuts, and thread engagement. All bolt windows are 4.6 mm polar rectangles, not round drilled bores or threads.

## Source

| File | Purpose |
| --- | --- |
| `dist/geometry.js` | Planner, layered watertight mesh construction, mating features, supports, STL and ZIP writers |
| `dist/kernel.scad` | Standalone procedural OpenSCAD counterpart; no external libraries |
| `dist/exports.js` | Parameters, interface specification, hardware list and assembly guide |
| `dist/viewer.js` | WebGL assembly and print-layout renderer |
| `dist/app.js` | Controls, generation and downloads |
| `scripts/pack-offline.mjs` | Rebuilds the single-file offline edition |

After editing sources, run `node scripts/pack-offline.mjs`. Open exported `petal.scad`, choose a part, one-based ring and saddle station, render with F6, then export STL. Set sectors/rows to zero to repeat automatic segmentation.

## Verification and limits

Run `npm test` for mesh, facet, mating, connection-graph and support-clearance checks. Tests cover small and large dishes, rectangular beds, both rear styles, legacy compatibility, and fit extremes. They check closed topology, winding, nondegenerate faces, printer bounds, actual-mesh joint separation, socket depth, support clearance, and STL counts. Representative browser exports and actual OpenSCAD renders were compared for bounding boxes and volume, including a supported panel and multi-ring fittings.

No physical printing, assembly testing, load testing, wind rating, or reflector-performance testing has been performed. Browser interaction and optional WebMCP registration have not been runtime-tested. The polymer shell has seams, openings and protruding fasteners; reflective finish, feed, feed support, electronics, and mount adapter are separate. There are no frequency-specific optimizations or RF performance estimates. Larger dishes may require additional bracing.

## Design references

- [Prusa: cut-tool connectors and tolerances](https://help.prusa3d.com/article/cut-tool_1779)
- [Formlabs: printed snap-fit design considerations](https://formlabs.com/global/blog/designing-3d-printed-snap-fit-enclosures/)
- [Prusa: support-material settings](https://help.prusa3d.com/article/support-material_1698)
- [OpenSCAD syntax reference](https://openscad.org/cheatsheet/)
