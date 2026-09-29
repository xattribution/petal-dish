# PETAL 5.0 — print and assembly

## New interface · print a test first

PETAL 5.0 uses integral flanges and one rear hub, interface revision 10. Do not mix older petals, saddles, caps or hubs with this kit. The first article is a geometric prototype, not a validated antenna or outdoor load-bearing structure. Start with two copies of FIT_TEST/seam-strip-print-two.stl in their exported orientations. These are sections of the real petal, including both flange edges, one screw position and the locating key. They intentionally retain the petal width so the upper flange prints in the same orientation as the full part. They do not test full-length warping or the hub.

## Materials and side printing

Use dry PETG for an indoor fit article; use ASA for outdoor trials only with a controlled enclosure and a verified material profile. PLA can establish fit but is a poor choice for a warm, sunlit dish. Start with a 0.4 mm nozzle, 0.2 mm layers and at least four perimeters; preview thin walls and use locally solid flange pads and insert bosses. Exported petals stand on their flat radial flange; the curved underside is sideways. A full-depth upper-flange gusset, ramped inner end and bed-connected root boss reduce broad unsupported projections. Curved is the default. Optional small tangent-plane underside facets add material without changing the RF face. Add a 3–6 mm brim for full petals and check adhesion. Packed copies have 6 mm between bounding boxes: limit individual brims to 3 mm or disable packing. Avoid sequential-by-object printing unless the slicer confirms toolhead clearance. The upper flange, bores and key recesses still require layer-preview inspection; pointed roofs reduce unsupported spans but are not a universal support-free guarantee. Use localized supports if your slicer/material requires them. Hub prints flat rear down.

## Fit and inserts

Remove first-layer flare only from mating edges. Keys have 0.20 mm clearance per side; nut pockets have 5.80 mm across flats before their printable roof extension. Nuts should slide in and remain rotationally retained. Test the real M3 hardware before printing the whole dish. Root and hub pockets are Ø5.60 × 7 mm for short M4 inserts, maximum 6 mm long, seated 0.5 mm below the entry. Insert pilot requirements depend on the exact insert and filament: verify the supplier specification and use an insert scrap before installation. Heat-set while supporting the boss, never by loading the RF face. Let it cool fully. Do not substitute the previous 8.1 mm inserts. The face above every root and mount insert stays closed.

## Assembly without pulling in warp

If petals are segmented, join each radial strip first using its integral cross-flanges. Then bring complete petals together by sliding radially inward from outside the rim; the short tapered keys enter as the edges approach. Keep all screws loose. The final petal also approaches radially; do not try to snap it into a closed ring from the front. Keys locate and flange pads establish contact; bolts retain. Place the shape in a measured support jig, check seam steps, then lightly snug opposite seams in several passes. Install the rear hub after the petal ring closes freely. Never use bolts to bend a warped petal until its seam disappears. Reprint or correct the process if keys bind, pads do not seat or the final petal needs force. Full-length flanges increase stiffness but thermal shrinkage and accumulated print error still require measurement.

## Hardware and access

Use M3 × 12 socket-head seam screws, one 0.5 mm washer under each head and ordinary 5.5 mm AF / 2.4 mm tall M3 nuts in the male flange pockets. Insert from the female flange side, access from behind the dish. Verify tool access before fitting rods. M4 × 12 root screws use one 1 mm washer through the 6 mm hub into the petal inserts, nominal 5 mm entry. The external mount retains a clear 30 mm center hole and four blind M4 inserts on a 60 mm bolt circle; select mount screw length from the actual adapter stack, targeting 4–5 mm entry. The old adapter may need shorter screws with this hub. No qualified torque is specified: snug by hand and stop if plastic compresses, whitening appears, or a seam moves. Recheck after a warm hold; printed plastic creeps.

## RF surface and accuracy

Apply continuous conductive foil or a specified conductive coating; see REFLECTOR.md for preparation and source references. Keep keys, flange pads, insert pilots and bores clean. Align before bridging seams with removable foil strips. The parabola focus is 168.00 mm from the extrapolated vertex, not from the hub back. Frequency does not move that geometric focus. Feed phase-center position, illumination, surface error and pointing determine RF performance. Mesh spacing is a geometric approximation too: reduce it for short wavelengths and measure the final coated dish. No calculated focal point or closed STL establishes measured gain.

## Inspection before a full article

Record seam steps and dish profile before tightening, after tightening, at several elevations, and after thermal settling. A key that fits a coupon does not prove a full petal will stay true. Check insert pull-out, screw bearing, flange-root cracking, nut retention and tool access on the test parts. Test repeated assembly and mark screws/rods to reveal movement. Wind, water, UV, creep and transport loads are not qualified. Keep a foil-covered dish away from direct sunlight during handling because it can concentrate heat.
