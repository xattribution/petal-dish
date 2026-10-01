# Integral-flange design decisions

PETAL 5.0 is a test-article candidate, not a qualified structure.

## Load path and fit

The shell has continuous 3 mm edge walls extending 14 mm below its underside. The upper/male flange has a full-depth 45° gusset and a ramped inner end; the bed-side flange has a smaller root chamfer. Root bosses extend to the bed-side edge so they do not start as isolated overhangs. Cross-flanges join the side flanges at their ends. Screw stations thicken to 5 mm. M3 screws clamp paired flanges sideways; the male flange contains a 2.6 mm-deep nut recess with a pointed printable roof. Its remaining nominal bearing thickness is 2.4 mm. At least two spaced screws retain each seam segment; longer seams add pairs to keep station spacing at or below 120 mm. A shallow 0.8 mm key locates the interface. Clearance is independent of the nut-pocket setting.

The male mating face is relieved except at contact pads. Keys taper along the seam according to sector angle, permitting radial insertion between fixed neighbors. Computer collision checks sample this motion; accumulated shrinkage, layer ridges and thermal distortion remain physical risks. Do not force a ring closed with screws.

Side flanges stop outside the Ø120 hub envelope. Petal root bosses bear on the hub's flat rear support ring. By default each root takes a short blind M4 insert and an M4 × 12 from the hub rear, so the reflecting face stays closed. An M4 through bolt is an option: its head sits in a recessed flat seat in the front, and the nut sits on the hub's rear. The one-piece hub includes the center surface and eliminates the front cap. By default its front is flat at the height of the petal roots instead of following the parabola (a curved front is an option): up to about 3 mm high at the center hole and under 1 mm of step at the polygon corners, all inside the feed's shadow. This avoids printing a shallow dome in stair-stepped layers. Root fasteners are not an independently qualified overturning-moment connection. Mount and feed loads still need creep and temperature tests.

## Print orientation

Petals stand on the female radial flange. The primary curved underside becomes side-facing instead of plate-facing. The chosen orientation provides a long contact strip but makes a tall thin wall: brim adhesion, vibration and layer-direction bending remain concerns. Upper-edge details, root-boss transitions and socket roofs need slicer inspection. A 45° gusset in assembly coordinates is not by itself proof of a support-free print in every orientation.

Optional underside facets use a square lattice of tangent planes, never intruding into the nominal shell. Maximum added vertical material is pitch²/(8f), about 0.30 mm for 20 mm pitch and f=168 mm. Crease lines are inserted into the mesh. The reflective front is still a tessellated parabola; coarse triangle spacing can consume a high-frequency surface-error budget.

Minimum supported dish diameter is 260 mm with the present hub and flange hardware. The planner may reject other dimension/bed combinations. This is preferable to exporting overlapping or inaccessible hardware. Segmentation uses straight chord boundaries, maintaining flat printable cross-flanges. It is a fit heuristic, not a globally optimal packing or stiffness calculation.

## Numerical checks and remaining gates

Regression tests exercise oriented closed meshes, connected solids, actual print bounds, packing separation, hub/root clearances, flat seam seats with washer and nut clearance, root and mount through bores and seats, insert-pilot roofs, sampled radial seam insertion, inter-segment mating, feed geometry and independent secondary reflection/path calculations. Binary STL reload and OpenSCAD comparison independently check exports. Offline DOM tests exercise the embedded kernel, options, errors and instructions; they do not qualify WebGL rendering.

Manifold solid operations replace the earlier BSP Boolean code. WASM solids are disposed after each build. Mesh output welds coincident float32 vertices and repairs collinear triangulation before STL export. These numerical checks cannot establish layer adhesion or dimensional accuracy.

Before a full dish: two seam strips, one full petal, then a root/hub fit. Measure profile and seam steps before and after tightening, multiple assembly cycles, several elevations and warm settling. Check flange-root cracking, screw bearing on the flat seats, seat flatness on the upper flange and feed-rod slip. RF gain, wind, service temperature and outdoor life remain unmeasured. Do not interpret a part-count reduction as proof of greater strength.
