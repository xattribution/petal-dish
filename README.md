# PETAL — Parametric Expeditionary Tactical Aperture Lab

A dependency-free parabolic dish generator with a 3D assembly preview, procedural STL export, editable OpenSCAD source, and a single-file offline edition.

Download [dist/petal-offline.html](dist/petal-offline.html) and open it directly in a browser. The app runs locally without a server. The hosted workspace uses the same geometry and export code.

## Recessed plates (2.3)

The default interface uses plain bolt holes and shallow seats for the whole connector end. There are no raised docking pads, per-hole keys, or projecting tongues. Seats are 0.35 mm deep with adjustable edge clearance. A 1.6 mm shell retains 1.25 mm vertical wall at a seat, excluding the bolt opening.

Same-ring connectors use two M4 bolts, one per petal. Between-ring connectors use four bolts, two per petal. The backing plate underside is a plane fitted to the local dish angle, with at least 3.2 mm thickness normal to that plane. STL exports rotate this face onto the print bed. Contact faces follow the actual curved or faceted rear surface. The hub retains its flat mounting face, 60 mm four-M4 bolt circle, and 30 mm opening.

Interface revision 4 requires matching panels, connectors and hub parts. Do not mix with earlier raised-key designs. The 60 mm mount adapter pattern is unchanged. The alternate legacy strap style retains its 40 mm mount pattern and two-bolt straps.

## Layout and connectors

Stagger rings is an independent Layout control available with either connection style. Alternating rings rotate by half a petal: `180 / petal_count` degrees. Turning staggering off aligns them. Joint style does not enable or disable staggering. Automatic printer-fit segmentation may select different petal counts for different constructions; set explicit petals and rings to fix segmentation.

Each staggered panel overlaps two neighbors in the adjacent ring. Connectors lie within those overlaps and join two panels; they do not straddle three-panel junctions. Faceted ring connectors are fitted to both adjoining rear faces. The legacy style also generates matching staggered holes and fitted connectors.

Adaptive connector spacing adds connectors along long seams in the recessed-plate system. Target spacing defaults to 150 mm, adjustable from 60–180 mm. Smaller spacing adds hardware. This is a geometric placement rule, not a structural calculation. Changes to geometry, spacing, or staggering require matching regenerated parts.

## Printing

The front follows `z = r² / (4f)`, where `f = diameter × f/D`. Choose a curved rear or two rear faces with a 10–15° change in slope. Thickness is vertical; faceted corners may be thicker. CAD volume assumes solid material and is not a filament estimate.

Automatic orientation prefers 60° for recessed plates, faceted backs, or built-in supports, then tries other diagonal angles. Legacy curved shells without supports retain low-profile automatic orientation. Bed rotations are checked in 15° steps. This is a fit heuristic, not a print-strength simulation.

Optional breakaway ribs follow the panel underside with adjustable contact gap, ridge width and spacing. Preserve supported STL shells in their exported positions. Inspect slicer layers before disabling generated supports. Check the shallow seats, bolt holes and plate contact faces; test one joint before printing a complete kit.

Exports include STLs, quantities, hardware totals, editable OpenSCAD, parameters, assembly rotations and a print guide. Grip lengths exclude washers, nuts and thread engagement. Sloped connector backs change hardware seating: check washer contact and actual fit before tightening.

## Source

- `dist/geometry.js`: geometry, segmentation, support meshes and STL primitives
- `dist/kernel.scad`: matching OpenSCAD geometry
- `dist/exports.js`: ZIP, parameters and assembly guide
- `dist/app.js`, `dist/viewer.js`: interface and WebGL preview
- `scripts/pack-offline.mjs`: standalone HTML packer

After editing, run `node scripts/pack-offline.mjs`. In exported OpenSCAD select the part, one-based ring and connector station, render with F6, then export STL.

## Verification

Run `npm test` for topology, winding, printer bounds, faceted surfaces, actual mesh mating, shallow-seat depth, connector-to-panel hole mapping, independent staggering, planar print bases and support clearance. Representative OpenSCAD panel and connector renders are compared with JavaScript dimensions and volume.

Physical printing, assembly, strength, creep and wind loads remain untested. Conductive finish, feed, feed support, electronics and mount adapters are separate. No frequency-specific optimization or RF performance estimates are provided.
