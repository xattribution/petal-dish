# PETAL 4.1 — print and assembly

## Before printing

PETAL 4.1 · 400 mm · 6 petals × 1 rings. Revision 9: regenerate the entire kit. Earlier interface revisions do not match. Start with the three FIT_TEST pieces, then one full petal and the hub. Perforations are experimental and unnecessary for the baseline test.

## Material and slicing

ASA is the preferred outdoor starting material if your enclosed printer produces dimensionally stable parts. PCTG is useful for fit and handling prototypes; verify warm-load creep before outdoor service. ABS needs UV protection. Use dry filament and the manufacturer’s profile. A 0.4 mm nozzle, 0.2 mm layers and 4–5 walls are starting points, not a qualified process. Make hardware bosses, plates and hub bearing regions locally solid with modifiers. Keep the continuous skin fully filled; inspect the actual toolpaths.

## Permanent rear structure

The recommended construction keeps the selected field thickness rather than thinning it to 75%. Up to 4 mm-deep rear ribs reinforce petal boundaries and the radial centerline, with blended shoulders and clearance around joint seats. They increase CAD volume; they are intended to improve shape retention, not to establish a load rating. Do not remove these ribs as if they were supports.

## Orientation and supports

Ring 1: 45° nominal tilt, 45° bed rotation, 134.5 mm tall. Automatic placement tries 45° first and penalizes steeper layouts; it is a fit/height heuristic, not a slicer optimization. Keep each supported STL as one multipart object with all shells in place. Breakaway ribs are sacrificial; the permanent rear ribs belong to the petal. Windowed support webs use 4 mm pillars on 12 mm centers and bridges up to 8 mm; inspect their bridge toolpaths as well as root lips, hole roofs and the cap pilot. Built-in supports cover petals only. Use layer-by-layer printing, not sequential-by-object printing. Print one full petal before filling beds.

## Fit without forcing

Plate seats are 2 mm deep with 0.20 mm clearance per side. Same-ring seats have an additional 0.30 mm radial relief per side; screw clearance still limits total motion. Nut clearance is independent: pockets are 7.40 mm across flats. Matching flat tangent seats locate the curved-shell panels without closing an intentional axial gap. Remove burrs and elephant foot only. A plate must seat by hand with the seam open. Do not clamp a warped petal flat or use screws to pull holes into alignment. Check scale and shrinkage on a full petal if coupons fit but the ring will not close.

## Hardware for this mode

Bolts enter from the FRONT into rear nuts. Temporarily tape rear nuts in place, keeping tape clear of bearing faces. Cap/root screws require button heads no larger than Ø8 × 2.2 mm and no washers in the recess. Seam screw selections allow a 1 mm washer; check actual washer thickness and contact on the curved face. Stop if a washer rocks, tilts the screw or dimples the panel; the front-hex mode provides flat nut-bearing seats.

## Nominal screw purchase list

12 × M4 × 16 mm; 6 × M4 × 22 mm. Four mount screws are additional and require the adapter dimensions. Length is measured under the head. Use HARDWARE.csv for position, allowance, washer and insert depth details. Verify against the printed coupon before buying the complete set.

## Assembly without accumulated distortion

Support the dish face-up on a padded cradle without flattening its parabola. Seat indexed roots in the rear hub, then place the front cap. Start all root screws loosely. Add an entire ring with the listed stagger offset, fitting rear plates loosely; do not finish one seam before the ring closes. Inspect opposite seams and the rim before tightening. Snug opposing joints in small alternating passes, then recheck the profile. Leave the designed gaps open. If a plate rocks or a seam requires force, stop and identify the mismatched or warped part. Removable rear plates avoid trapping the last petal in a closed ring of tongues.

## Mount and clamp load

Use the 60 mm mounting bolt circle and a rigid external adapter that supports the hub broadly while leaving root screws accessible. A separate prototype geared-head adapter is provided in the repository (cad/manual-aiming-mount.scad), outside this dish ZIP. At the four mounting positions, a rear backing plate needs external washers/nuts behind the adapter; do not trap nuts in the hub pockets and assume they clamp that adapter. Do not use generic steel-joint M4 torque tables. Hand-snug for the test article and stop at visible deformation. Recheck after a warm hold and assembly cycles. Inserts do not prevent creep of the surrounding plastic. Large/outdoor builds need a verified backing structure or properly designed compression limiters; this hub has no wind rating.

## Make it an RF reflector

Bare printing plastic is not the intended RF reflector. For a first article, use thin aluminum foil tape with a specified conductive adhesive, such as 3M 1170, in narrow strips on the FRONT face. Copper tape such as 3M 1181 is an alternative but adds mass. Test adhesion on a scrap of the actual filament. Fit and measure the dish before coating; keep metal and adhesive off joint datums, nut seats, roots and bores. Coat petals individually, then bridge assembled seams with removable conductive tape. Do not tension the tape to pull seams together. The exported REFLECTOR.md covers conductive paints, finishing, continuity checks and the feed.

## Acceptance checks

The part must fit before tightening and retain its profile after tightening. Use INSPECTION.csv for nominal front heights; check several azimuths, seam steps and rim runout, then repeat after disassembly and a warm hold. Record material, drying, orientation, slicer profile and hardware. Stop for cracks, whitening, pocket deformation, insert movement or permanent profile change. Final surface tolerance depends on operating frequency. Shape, printability, stiffness, creep and RF gain still require the test article.

## Screw schedule

HARDWARE.csv gives one row per unique hole position and the quantity across repeated parts. Radius and azimuth refer to the part in assembly orientation before its listed ring/sector rotation. Lengths are measured under the head. Seams and roots have nominal stock selections with a 0.2 mm nut-engagement / 0.4 mm insert-engagement allowance; verify the real print and hardware before buying the complete set. Blind inserts assume 8.1 mm length, at least 4 mm thread engagement and a 1 mm roof reserve plus allowance. No listed length means source/cut a suitable screw, not round upward. Mount lengths remain unresolved until the adapter is specified.

## Files and offsets

- panel-1: 6 copies; 158.4 × 158.4 × 134.5 mm
- side-bridge-1: 6 copies; 21.3 × 32.6 × 10.5 mm
- hub-rear: 1 copies; 120.0 × 120.0 × 15.7 mm
- hub-clamp: 1 copies; 120.0 × 120.0 × 10.8 mm

Ring offsets: 1: 0.00°.

petal-snapshot.scad is an exact assembly/part mesh snapshot. Change parameters in PETAL and regenerate; this file is not an editable parametric kernel.
