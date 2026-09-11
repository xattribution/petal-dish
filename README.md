# PETAL — Parametric Dish Studio

A dependency-free, browser-side geometry tool for segmented hobbyist parabolic support shells. Parameters drive a 3D assembly preview, individual binary STL downloads, a complete ZIP print kit, and standalone editable OpenSCAD source, including optional designed-in breakaway ribs.

## Use

Open the hosted page or download `petal-offline.html` and open it directly in a browser. No server, installation, CDN or network connection is required by the offline edition. The web page does not store or upload design inputs.

Choose diameter, focal ratio, shell thickness and printer volume. Enable breakaway ribs to add disposable supports to each panel STL. Automatic segmentation searches even petal counts from 6 to 32 and one to twelve radial rings, minimizing panel count subject to part fit and joint space. Advanced controls override counts and adjust seam gap, edge margin and mesh spacing.

The interactive preview supports drag / keyboard orbit, wheel / keyboard zoom, assembly, exploded assembly, unique parts on separate print beds, part highlighting, optional mesh lines and a focal-point marker. ZIP exports include one STL per unique part with quantity in its filename, a parameter manifest, assembly instructions and a standalone `.scad` model.

## Geometry and construction

- Surface: `z = r² / (4f)`, `f = diameter × f/D`; thickness is measured vertically.
- Panels are annular sectors with curved front/back surfaces and M4 clearance windows.
- Curved removable backing straps bridge angular and radial seams.
- Rear hub + front clamp retain inner panel roots.
- Custom fixed rear mounting interface: four M4 clearance windows on a 40 mm bolt circle, clocked at 45°, 30 mm central bore, 120 mm hub OD. This is not an existing commercial mounting standard. Curvature and root-hole count follow the design and must be regenerated with it.
- Windows are 4.6 mm polar rectangles, not round drilled holes or threads. Use bolts, nuts and washers.
- Unsupported auto mode preserves low-profile printing. With ribs enabled, auto mode prefers a 60° radial-chord angle, trying 55°, 50°, 45° and 65° if fit requires it; 15° bed-rotation steps minimize the fitting bounding footprint. Manual 45–70° settings are available. This is a geometric heuristic, not a strength simulation.
- Supported petals sit 5.6 mm above the bed. Two to five separate ribs have 1.2 mm webs, adaptive flared feet, narrow serrated contact ridges and configurable vertical gap. Rib geometry follows the analytic rear envelope with an extra curvature allowance. Ribs are included as closed disconnected shells in each panel STL; retain their relative positions in the slicer.
- Part bounds include enabled supports. Hardware parts keep low-profile orientation and may require slicer support.

The shell has seams, openings and surface fasteners. Conductive finish, feed, feed supports, receiver and mount adapter are separate. There are no frequency optimizations or RF performance estimates.

## Source

- `dist/geometry.js`: parameter validation, bed-aware planner, watertight mesh kernel, binary STL, ZIP and volume calculations.
- `dist/kernel.scad`: standalone parametric OpenSCAD counterpart with optional automatic planner and part selection.
- `dist/exports.js`: SCAD parameter header, manifest, bill of materials and ZIP assembly guide.
- `dist/viewer.js`: dependency-free WebGL renderer and orbit controls.
- `dist/app.js`: visible controls and optional feature-detected WebMCP interface.
- `scripts/pack-offline.mjs`: builds the single-file offline edition from the exact same source.

After edits: `node scripts/pack-offline.mjs`. No package installation or application compilation is required.

## Verification

`node tests/geometry.test.mjs` checks multiple dish/printer configurations, including 1.2 m multi-ring geometry and rectangular beds: every used mesh edge has two opposite-winding faces, triangles are nondegenerate, volume is positive and rotation-invariant, exported bounds fit the printer, Z starts at zero, binary STL counts match, and invalid configurations are rejected.

Representative panel, rear hub, front clamp, side bridge and radial bridge exports were rendered with the installed OpenSCAD CLI. Browser-kernel and OpenSCAD panel/hub exports were compared for watertight topology, volume and bounds. ZIP CRCs, manifest counts, static references and standalone-script syntax were checked.

No browser interaction QA was run. A permitted supported WebMCP browser context was unavailable, so its optional registration and actions have not been runtime-validated. No physical printing, assembly-fit, loading or reflector-performance testing has been performed. Start with one panel and joint; larger shells may need additional bracing.

OpenSCAD syntax reference: https://openscad.org/cheatsheet/

## Breakaway-rib verification

Support-on and support-off configurations were checked for manifold topology, positive volumes, print-volume bounds, zero-height feet, and invalid-input rejection. `node tests/support-clearance.test.mjs` samples ridge vertices and interpolated segments against the actual transformed panel mesh, checking for nominal clearance violations. A representative supported panel was rendered by OpenSCAD as a simple solid collection and compared with browser STL volume and bounds. Tests also cover 2 and 5 ribs, a 70-degree angle, smaller contact clearance, and a multi-ring design. No actual print or slicer toolpath was run.

Support contact references: https://help.prusa3d.com/article/support-material_1698

## Two-facet rear (1.2)

Optional two-plane rear with a 10–15° change of slope (default 12°).
The front remains parabolic. Thickness is a minimum vertical wall, not a
constant normal thickness; corner thickness and solid CAD volume can increase
substantially. Slicer infill determines actual material usage. Petals remain
at a diagonal print angle, with optional breakaway ribs following the rear.
Matching seam bridges and rear hub are regenerated with flat bases; reprint the
complete kit when changing geometry. The mounting bolt pattern stays fixed.
Selected parts and the manifest report sampled wall ranges and hardware grip
stacks, excluding washers, nuts and thread engagement. Rear view reveals the
facets. No skeletonization or physical stiffness simulation is included.

Checks: `node tests/geometry.test.mjs`, `node tests/support-clearance.test.mjs`,
`node tests/facets.test.mjs`. Physical printing remains unvalidated.
