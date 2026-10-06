# Integral-flange design decisions

PETAL 5.3 is a test-article candidate, not a qualified structure.

## Load path and fit

The shell has continuous edge walls extending 14 mm below its underside. Bolt-only walls are 3 mm with 5 mm fastening stations; clip-compatible walls are uniformly 5 mm. The upper flange has a full-depth 45° gusset and a ramped inner end. Root bosses extend to the bed-side edge. Cross-flanges join the side flanges at their ends. M3 screws clamp sideways between flat seats with loose nuts; there are no captured seam nuts or alignment keys. Spaced stations resist rotation; longer seams add stations. Clips provide removable retention, but do not provide the same preload as bolts.

Mating faces are flat, with no keys; the bolts or clips align the petals. Radial insertion between fixed neighbors is sampled by computer collision checks; accumulated shrinkage, layer ridges and thermal distortion remain physical risks. Do not force a ring closed with screws.

Side flanges stop outside the Ø120 hub envelope. Petal root bosses bear on the hub's flat rear support ring. By default each root takes a short blind M4 insert and an M4 × 12 from the hub rear, so the reflecting face stays closed. A through bolt is an option: its head sits in a recessed flat seat in the front, and the nut sits on the hub's rear. Roots and the four hub-to-mount bolts take M3, M4 or M5; holes, seats, insert pilots and screw lengths follow the size. The one-piece hub includes the center surface and eliminates the front cap. The flat front sits level with the petals at the hub's corners, where the inner petal edge is highest, and chamfers down to meet each petal along its edge, so there is no step at the joint. It stands above the parabola by up to zAt(45 / cos(π/n)) − zAt(15), about 3.7 mm on the default dish. The curved front follows the parabola. The flat front's local surface departure is reported against the chosen frequency in the UI and in the exported instructions. Feed shadowing is not guaranteed to hide this error. Root fasteners are not an independently qualified overturning-moment connection. Mount and feed loads still need creep and temperature tests.

## Print orientation

Petals stand on the bed-side radial flange. The primary curved underside becomes side-facing instead of plate-facing. The chosen orientation provides a long contact strip but makes a tall thin wall: brim adhesion, vibration and layer-direction bending remain concerns. Upper-edge details, root-boss transitions and bore roofs need slicer inspection. A 45° gusset in assembly coordinates is not by itself proof of a support-free print in every orientation.

Optional underside facets use a square lattice of tangent planes, never intruding into the nominal shell. Maximum added vertical material is pitch²/(8f), about 0.30 mm for 20 mm pitch and f=168 mm. Crease lines are inserted into the mesh. The reflective front is still a tessellated parabola; coarse triangle spacing can consume a high-frequency surface-error budget.

Minimum supported dish diameter is 260 mm with the present hub and flange hardware. The planner may reject other dimension/bed combinations. This is preferable to exporting overlapping or inaccessible hardware. Segmentation uses straight chord boundaries, maintaining flat printable cross-flanges. **Largest petals** takes the fewest sectors and rings that fit the bed. **Fewest print plates** ranks larger counts by a packed-plate estimate, then packs the real petals and switches only when they need strictly fewer plates. Both are fit heuristics, not a globally optimal packing or stiffness calculation.

## Numerical checks and remaining gates

Regression tests exercise oriented closed meshes, connected solids, actual print bounds, packing separation, hub/root clearances, flat seam seats with washer and nut clearance, root and mount through bores and seats, insert-pilot roofs, sampled radial seam insertion, inter-segment mating, feed geometry and independent secondary reflection/path calculations. Binary STL reload and OpenSCAD comparison independently check exports. Offline DOM tests exercise the embedded kernel, options, errors and instructions; they do not qualify WebGL rendering.

Manifold solid operations replace the earlier BSP Boolean code. WASM solids are disposed after each build. Mesh output welds coincident float32 vertices and repairs collinear triangulation before STL export. These numerical checks cannot establish layer adhesion or dimensional accuracy.

Before a full dish: two seam strips, one full petal, then a root/hub fit. Measure profile and seam steps before and after tightening, multiple assembly cycles, several elevations and warm settling. Check flange-root cracking, screw bearing on the flat seats, seat flatness on the upper flange and feed-rod slip. RF gain, wind, service temperature and outdoor life remain unmeasured. Do not interpret a part-count reduction as proof of greater strength.

## Integrated aiming mount and clips

The canonical three mount STLs are bundled from `cad/STL` using `scripts/pack-mount.py`; regenerate that asset after changing mount CAD. Shared scene transforms drive the viewer, PDF and SCAD assembly. The generator checks mount/dish interference at the selected pose and reports below-base clearance, including the elevation-range envelope. This does not certify the continuous collision path, hardware, cables or a user stand.

Clip jaws print in the layer plane on their broad side. The 0.3 mm default detent reduces estimated flex strain; generation rejects settings above the user-entered strain budget. Material selection is an explicit record, not an automatically qualified allowable strain. Test unfilled materials separately from reinforced rigid mount parts. Installed clips and spare print quantities are distinct.
