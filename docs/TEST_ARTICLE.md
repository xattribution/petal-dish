# PETAL revision-9 test article

Use the default 400 mm curved-shell kit, structural ribs enabled, perforations disabled, and one material/slicer profile. Keep the exported parameter file with the printed parts. The goal is to establish fit and shape repeatability before reducing weight or scaling up.

## Stage 1 — small joint

Print all three FIT_TEST pieces with the exported orientations, adding supports as needed. Confirm the actual nut or insert dimensions against the selected hardware mode. Test the locating plate without screws first: hand seating, no rocking, no interference. Check screw engagement and tip clearance. Record the clearance setting and measured seam step. If a fit correction is needed, regenerate the complete kit; do not independently rescale one mating part.

For inserts, use a guided heated tool with a depth stop. Measure the front before/after installation. Compare the recess depth to the per-position hardware schedule. Reject a bulged face, cracked boss or loose insert. The holes and instructions assume an 8.1 mm insert, not an arbitrary M4 insert.

## Stage 2 — full-size process check

Print one complete petal plus the rear hub and cap. Inspect the supported surfaces and root lip before removing supports, then after removal. Check layer bonding, edge curl, elephant foot, bore ovality and complete seat engagement. A successful small coupon does not compensate for a warped full panel.

Dry-fit the root, pilot and cap. The screws must start without pulling the components into alignment. Do not flatten a petal against a table to make it fit. If the part has curled, correct the print process or orientation and reprint it.

## Stage 3 — complete first ring

Print the remaining matching petals and plates. Support the assembly face-up on a padded cradle, fit every component loosely and close the ring before tightening. Compare opposite seams. Snug opposing joints progressively and verify that tightening does not create a new seam step or rim distortion.

Measure several radial profiles at multiple azimuths using the exported INSPECTION.csv. A light straightedge supported at the rim can provide a depth reference, but must not bend the rim. The central 30 mm opening has no surface; the vertex is extrapolated. Avoid measurement points inside bores, recesses or seams. Record fixture repeatability and instrument resolution.

As a starting design budget, choose a structural surface-error limit no larger than wavelength/50 RMS for the intended highest operating frequency. It is a proposed acceptance target, not a capability claimed for the print. Also track maximum seam steps, best-fit focal length and rim runout; RMS alone can conceal a local defect.

## Stage 4 — repeatability and warm-load checks

Disassemble/reassemble at least five times, using the same sequence. Record seam steps, profile change, nut-pocket wear and screw loosening. Heat-set inserts must not rotate or migrate. A test article should not need progressively more screw force to align.

Mount the dish on a broadly supporting test fixture. Compare its shape face-up, near horizontal and at the intended operating elevation. Apply only controlled, measured loads; record displacement and recovery. Do not infer a wind rating from an arbitrary hanging weight or household fan.

Hold the assembly at the intended warm operating condition under its actual support/fastener loads, checking initially and after sustained exposure. Start conservatively; keep within the material and equipment limits. Measure permanent change after cooling. There is no qualified temperature or torque specification yet.

## Stage 5 — conductive finish and RF comparison

Only finish a mechanically acceptable assembly. Test the tape/paint on a scrap, coat individual front surfaces, reassemble and bridge seams without tension. Repeat the profile and continuity measurements. Fix the feed phase-center position, polarization and cable strain relief before comparing signal level or beam pattern to a reference antenna.

Record: filament brand/lot, drying, printer/enclosure, nozzle/layers/walls, support settings, orientation, print scale, clearances, hardware sizes, insert process, finish, feed, frequency, fixture, loads, temperature and all before/after measurements. These observations are what should drive the next revision.
