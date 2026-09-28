# PETAL — Parametric Expeditionary Tactical Aperture Lab

A browser-based parabolic dish generator with printer-aware segmentation, a 3D assembly view, procedural STL exports, editable OpenSCAD, and a single-file offline edition.

Download [dist/petal-offline.html](dist/petal-offline.html) and open it directly in a browser. The hosted app uses the same geometry and exports.

## Reinforced connections (3.2)

All bolt bores are round, Ø4.6 mm for M4 through-bolts. The default recessed plates and rear hub have hexagonal nut pockets: nominal 7 mm nuts plus twice the fit-clearance setting, 7.4 mm across flats by default. Pockets have at least 3.4 mm depth and a horizontal nut bearing face with at least 2.8 mm supporting material in seam plates (2 mm in the hub). Hold nuts in place until their bolts engage; pockets prevent rotation rather than retaining loose nuts upside down.

The center now captures the petal roots mechanically:

- Root lips drop into a matching concentric groove in the rear hub.
- Interrupted sections provide indexing stops against circumferential sliding.
- A front cap overlaps recessed root shoulders and traps them axially.
- The cap's annular pilot enters a second concentric channel in the rear hub.
- Its exposed face follows the same parabola as the dish; reinforcement sits underneath.

Root shoulders are deep enough for recessed screw heads and at least a 3 mm root web. The lip is 2 mm deep. The root rear profile blends back into the normal panel by radius 66 mm. The cap and rear hub retain a Ø30 opening and four M4 mount positions on a 60 mm bolt circle. Matching holes through the cap keep these mounting positions accessible.

The cap uses M4 ISO 7380-1 button-head screws, with heads no larger than Ø8 × 2.2 mm, in Ø8.4 counterbores. Counterbore floors sit at least 2.4 mm below the lowest surrounding front surface. Standard tall socket-cap heads do not fit. Choose screw lengths from exported grip dimensions, nut height, washers where applicable, and mount-adapter thickness. See the exported assembly guide for hardware dimensions and references.

**Interface revision 7 widens all seam plates and adds reinforced locating seats.** Regenerate panels and all seam plates together. Revision-5 and revision-6 hubs and caps remain compatible; earlier hubs, retaining rings and root profiles do not mate. The external 60 mm mount pattern is unchanged. The legacy strap option retains its 40 mm mount pattern and pressure-clamped hub, now with round bores.

## Layout and connectors

Staggering is independent of connector style. Alternate rings rotate by half a petal, `180 / petal_count` degrees. A staggered petal overlaps two neighbors in the adjacent ring. Recessed junction plates sit at inner-ring seams: one bolt in each of two inner petals and two bolts in one outer petal. Each boundary uses one plate per inner seam. Aligned rings and legacy straps retain overlap connectors.

Default side plates use two bolts, one per petal. Between-ring plates use four, two per petal. Plates enter 1 mm footprint seats, with 0.8 mm nominal engagement before tightening. Rear reinforcement blends out over 4 mm; at least 2.45 mm nominal panel wall remains below a seat. Plate ends are 3 mm wider to protect the nut pockets. Sloped plate backs export rotated flat for printing; local depth accommodates the nut pockets. Adaptive spacing adds connectors along long seams. Spacing defaults to 150 mm and is a geometric placement rule, not a structural calculation.

Set explicit petal and ring counts to fix segmentation. Automatic printer fitting may choose different counts when construction changes. Regenerate matching parts after changing geometry, spacing or staggering.

## Printing and assembly

The front follows `z = r² / (4f)`, where `f = diameter × f/D`. Choose curved backs or two rear faces with a 10–15° slope change. Thickness is vertical; faceted corners and captured roots may be thicker. Reinforced seam seats retain at least 2.45 mm nominal panel wall independently of the 1.6 mm minimum field shell. CAD volume is not a filament estimate.

Curved keyed petals now use a graded wall: the chosen thickness remains at the seams and within 10.3 mm of bolt centers; the broad field eases to 75% of that thickness, with a 1.6 mm floor. The transition spans 12 mm, so the default 2.4 mm shell reaches 1.8 mm away from its joints. Revision-7 seats have additional rear reinforcement. The front parabola and root capture remain the reference surfaces; both curved and faceted backs carry the local seat reinforcement. This reduces CAD material volume without claiming a verified strength rating; slice a sample and test stiffness and wind loading before relying on a full dish.

Automatic petal orientation prefers diagonal printing for the default construction. Optional breakaway ribs follow the actual underside. Preserve all supported shells in their original positions. Inspect root lips, nut-pocket roofs, recesses and the cap pilot in the slicer. The cap has curved faces and may require generated supports; built-in ribs apply to petals only.

The ZIP now includes three small FIT_TEST STLs cropped from the actual side joint. Print them in the intended PCTG or ASA/ABS with the final slicer settings, then dry-fit the full hub and a ring junction before printing the entire kit. Preload nuts into the rear pockets and temporarily retain them with removable tape while starting the screws. Place rear nuts, seat indexed petal lips, lower the cap into its pilot channel, start root screws loosely, then assemble outer rings and plates. Tighten gradually without forcing misaligned parts. Fit the rear adapter after placing its nuts. The exported guide includes quantities, hardware, grip lengths, part orientations and assembly order.

## Source and checks

- `dist/geometry.js`: meshes, segmentation, print orientation, supports and STL generation
- `dist/kernel.scad`: matching parametric OpenSCAD geometry
- `dist/exports.js`: ZIP, parameters and assembly guide
- `dist/app.js`, `dist/viewer.js`: controls and preview
- `scripts/pack-offline.mjs`: rebuild the offline HTML

Run `node scripts/pack-offline.mjs` after changes. In exported OpenSCAD select the part, one-based ring and connector station, render with F6, then export STL.

`npm test` checks closed topology, winding, printer bounds, mating surfaces, independent staggering, bore mapping, round-shaft clearance, hex-pocket bearing faces, root grooves, indexing stops, pilot clearance, flush cap faces and support separation. Run `npm run test:scad` with OpenSCAD installed to render representative exports and compare their dimensions and volume with JavaScript.

Physical printing, assembly, creep, strength and wind loading remain untested. No RF performance estimates or frequency-specific optimization are provided. Reflective finish, feed, feed support, electronics and mount adapters are separate.

## Engineering scope

Revision 7 reinforces joint geometry and makes alignment less dependent on screw force. It does not establish a strength rating or justify reducing the number of fasteners. Keep the existing fastener count until representative joints and the assembled dish have been physically tested. Use washers on petal fasteners, start every screw by hand, and tighten progressively. Visible panel dimpling or pocket deformation is a failed assembly check, not a signal to tighten further.


## Optional construction and shared beds (3.3)

All new options are off by default:

- **Perforate petal fields:** 6 mm round voids, procedurally spaced with at least 14 mm center clearance from petal edges and clear zones around roots, seats and fastening pockets. Density is capped by increasing pitch on larger petals. Inspect the roofs and supports in your slicer. Perforations remove reflector surface; no RF performance claim is made.
- **Pack copies onto shared beds:** rotates finished parts about the bed normal in 15° steps, preserves print tilt, includes breakaway supports, and leaves 6 mm between bounding boxes. The layout view shows every copy on its assigned bed. The ZIP includes `PACKED/plate-N.stl` and placement metadata. Print either packed beds or the individual quantities, not both. Allow for slicer-generated supports and brims; the packer only knows the exported geometry. This is conservative packing rather than optimal polygon nesting.
- **Fastening:** original front screws with round bores and rear nuts; rear screws with recessed front hex nuts; or rear screws into blind heat-set inserts. The two new styles have round rear button-head recesses. Selecting a new style requires a complete regenerated kit, including the hub and cap (revision 8). Recessed nuts still leave a visible pocket; only the insert option closes the hardware holes on the front.

The default insert reference is [ruthex RX-M4x8.1](https://www.ruthex.de/en/collections/gewindeeinsatze/products/ruthex-gewindeeinsatz-m4-50-stuck-rx-m4x8-1-messing-gewindebuchsen): 5.6 mm installation hole, at least 9.1 mm cavity, and at least 2 mm front skin. Hole diameter and minimum cavity depth can be adjusted. Heat-set holes are not standardized across suppliers. Print the actual joint coupon with your PCTG or ASA/ABS and verify insertion depth and screw length first. Blind insert bosses add material behind the face; this style is heavier than through-bolting.

Perforated or alternative-fastener configurations export `petal-snapshot.scad`, an exact mesh snapshot with an assembly/part selector. Change dimensions in PETAL and regenerate this file. The original configuration continues to export editable parametric OpenSCAD. Packed beds are exported as STL.
