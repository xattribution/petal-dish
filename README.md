# PETAL — Parametric Expeditionary Tactical Aperture Lab

A browser-based parabolic dish generator with printer-aware segmentation, a 3D assembly view, procedural STL exports, editable OpenSCAD, and a single-file offline edition.

Download [dist/petal-offline.html](dist/petal-offline.html) and open it directly in a browser. The hosted app uses the same geometry and exports.

## Junction plates and captured hub (3.1)

All bolt bores are round, Ø4.6 mm for M4 through-bolts. The default recessed plates and rear hub have hexagonal nut pockets: nominal 7 mm nuts plus twice the fit-clearance setting, 7.4 mm across flats by default. Pockets have at least 3.4 mm depth and a horizontal nut bearing face with at least 2 mm supporting material. Hold nuts in place until their bolts engage; pockets prevent rotation rather than retaining loose nuts upside down.

The center now captures the petal roots mechanically:

- Root lips drop into a matching concentric groove in the rear hub.
- Interrupted sections provide indexing stops against circumferential sliding.
- A front cap overlaps recessed root shoulders and traps them axially.
- The cap's annular pilot enters a second concentric channel in the rear hub.
- Its exposed face follows the same parabola as the dish; reinforcement sits underneath.

Root shoulders are deep enough for recessed screw heads and at least a 3 mm root web. The lip is 2 mm deep. The root rear profile blends back into the normal panel by radius 66 mm. The cap and rear hub retain a Ø30 opening and four M4 mount positions on a 60 mm bolt circle. Matching holes through the cap keep these mounting positions accessible.

The cap uses M4 ISO 7380-1 button-head screws, with heads no larger than Ø8 × 2.2 mm, in Ø8.4 counterbores. Counterbore floors sit at least 2.4 mm below the lowest surrounding front surface. Standard tall socket-cap heads do not fit. Choose screw lengths from exported grip dimensions, nut height, washers where applicable, and mount-adapter thickness. See the exported assembly guide for hardware dimensions and references.

**Interface revision 6 relocates ring holes and seats.** Regenerate panels and ring plates together. Revision-5 hubs and caps remain compatible; earlier hubs, retaining rings and root profiles do not mate. The external 60 mm mount pattern is unchanged. The legacy strap option retains its 40 mm mount pattern and pressure-clamped hub, now with round bores.

## Layout and connectors

Staggering is independent of connector style. Alternate rings rotate by half a petal, `180 / petal_count` degrees. A staggered petal overlaps two neighbors in the adjacent ring. Recessed junction plates sit at inner-ring seams: one bolt in each of two inner petals and two bolts in one outer petal. Each boundary uses one plate per inner seam. Aligned rings and legacy straps retain overlap connectors.

Default side plates use two bolts, one per petal. Between-ring plates use four, two per petal. Shallow 0.35 mm footprint seats replace individual locating keys along the panel seams. Sloped plate backs export rotated flat for printing; local depth accommodates the nut pockets. Adaptive spacing adds connectors along long seams. Spacing defaults to 150 mm and is a geometric placement rule, not a structural calculation.

Set explicit petal and ring counts to fix segmentation. Automatic printer fitting may choose different counts when construction changes. Regenerate matching parts after changing geometry, spacing or staggering.

## Printing and assembly

The front follows `z = r² / (4f)`, where `f = diameter × f/D`. Choose curved backs or two rear faces with a 10–15° slope change. Thickness is vertical; faceted corners and captured roots may be thicker. Seam seats remove 0.35 mm locally, leaving 1.25 mm from the minimum 1.6 mm shell. CAD volume is not a filament estimate.

Automatic petal orientation prefers diagonal printing for the default construction. Optional breakaway ribs follow the actual underside. Preserve all supported shells in their original positions. Inspect root lips, nut-pocket roofs, recesses and the cap pilot in the slicer. The cap has curved faces and may require generated supports; built-in ribs apply to petals only.

Test one panel, connector and hub fit with actual hardware before printing the entire kit. Place rear nuts, seat indexed petal lips, lower the cap into its pilot channel, start root screws loosely, then assemble outer rings and plates. Tighten gradually without forcing misaligned parts. Fit the rear adapter after placing its nuts. The exported guide includes quantities, hardware, grip lengths, part orientations and assembly order.

## Source and checks

- `dist/geometry.js`: meshes, segmentation, print orientation, supports and STL generation
- `dist/kernel.scad`: matching parametric OpenSCAD geometry
- `dist/exports.js`: ZIP, parameters and assembly guide
- `dist/app.js`, `dist/viewer.js`: controls and preview
- `scripts/pack-offline.mjs`: rebuild the offline HTML

Run `node scripts/pack-offline.mjs` after changes. In exported OpenSCAD select the part, one-based ring and connector station, render with F6, then export STL.

`npm test` checks closed topology, winding, printer bounds, mating surfaces, independent staggering, bore mapping, round-shaft clearance, hex-pocket bearing faces, root grooves, indexing stops, pilot clearance, flush cap faces and support separation. Representative OpenSCAD exports are compared with JavaScript dimensions and volume.

Physical printing, assembly, creep, strength and wind loading remain untested. No RF performance estimates or frequency-specific optimization are provided. Reflective finish, feed, feed support, electronics and mount adapters are separate.
