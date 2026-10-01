# PETAL 5.2 — integral-flange reflector prototype

A browser/offline generator for a segmented parabolic dish, with an assembly viewer, side-oriented STLs, shared-bed packing and frequency-aware metal-rod feed supports.

Open [the standalone app](dist/petal-5.2-offline.html) directly, or serve `dist/` locally. The app includes its solid-modeling kernel and works without a network connection.

**Start with the two seam test strips in the exported ZIP, then one full petal.** This version is an unprinted engineering prototype. A closed STL is not a print-process, load or RF qualification.

## Self-host / illustrated PDF

Use [the one-shot Linux installer](docs/SELF-HOSTING.md) to serve on port **56302** or a port you choose. It installs the web server and requirements, verifies the build, and creates a boot-persistent service. While this PR is unmerged, select `--ref codex/integral-flange-petals`; `main` still contains the older generator.

Every ZIP now includes a customized **ASSEMBLY.pdf** with generated assembly/exploded views, print-orientation part catalog, counts, hardware, instructions, finishing guidance and optional rod cuts. The **Assembly manual PDF** button downloads it separately. All of this works offline. The header shows **5.2 / build ID / flange joints** so an old downloaded file is easy to identify.

## Simpler assembly

- Integral underside flanges replace separate seam brackets. Sideways M3 bolts and captive nuts stay behind the RF surface.
- Short tapered keys locate neighboring petals while allowing radial insertion, including the final petal. Screws retain seated datums; they must not pull warp out of the shell.
- One rear hub, a clear Ø30 mm opening, and four M4 mounts on Ø60 mm BCD: blind heat-set inserts or full through holes with front washer seats. Root screws also enter blind inserts, keeping the reflecting face closed.
- Petals print on a flat radial flange. Curved undersides are the default; optional small tangent-plane facets add little material and preserve the front parabola.
- Larger dishes default to staggered rings with half-petal offsets and three-panel junctions. Shared polygon boundaries use integral cross-flanges; turn staggering off for aligned strips. The planner rejects layouts without space for joints or the printer.
- Default 400 mm dish: six petals and one hub, two unique printed parts. Optional feed fittings add parts. This is a geometric simplification, not a demonstrated strength rating.

**Interface revision 11 requires a complete regenerated kit.** Previous petals, seam saddles, front caps and hubs are incompatible. Short M4 inserts (maximum 6 mm long) replace the former long inserts. Check the separate aiming adapter's screw lengths against the new blind hub before use.

## Arrange plates

Under Side printing, open **Arrange print plates**. Assign each physical copy to a numbered plate, set X/Y from bed center and rotation on the bed, then Apply arrangement. The exported side-print orientation is preserved. Bounds and 6 mm clearance are checked before accepting changes. Reset automatic packing restores the planner. Shape/printer changes reset overrides; unapplied edits block exports. Plate STLs, PLATES.csv, parameters.json and the PDF carry the accepted arrangement. Overrides apply to this session/export; reloading starts a new arrangement. Empty plate numbers are retained but have no STL.

## Print and assemble

Read the kit's `ASSEMBLY.md`, `HARDWARE.csv` and `REFLECTOR.md`; the app's Print notes use the same source. Keep exported orientations. Add a brim and inspect every layer around flange roots, pointed bore roofs and keys. No disposable supports are generated; local slicer supports may still be needed. Shared beds leave 6 mm between part bounds, permitting at most 3 mm individual brims. Print either packed beds or individual quantities, not both.

PETG is a practical indoor fit-test material. ASA is an outdoor starting point with a controlled enclosure and thermal/creep testing. Four perimeters and locally solid pads are starting settings, not a strength guarantee. See [the test procedure](docs/TEST_ARTICLE.md) and [engineering limits](docs/ENGINEERING.md).

Use M3 × 12 seam screws, 0.5 mm head washers and ordinary M3 nuts. Root screws are M4 × 12 with 1 mm washers. Insert dimensions must match the actual supplier and filament. The generated schedule specifies quantities; external mount screw length depends on the adapter stack.

## RF finish and feed supports

Printed plastic needs a conductive surface and an appropriate RF feed. Thin aluminum foil tape with specified conductive adhesive is the baseline finishing approach; copper foil or a specified conductive coating are alternatives. Mask joints, align first and bridge seams afterward without tensioning the dish. Ordinary metallic paint is not evidence of adequate conductivity. See [REFLECTOR.md](docs/REFLECTOR.md) for preparation and source references.

Optional three/four-rod supports retain compact bolted rim shoes and a round puck. Smooth solid aluminum rod defaults to 6.35 mm (1/4 inch); generated socket angles and `RODS.csv` give the actual cuts. Frequency informs accuracy budgets, rod screening and experimental Cassegrain secondary sizing. A fixed parabola's focal point does not move with frequency. Prime-focus offsets require the actual feed phase center. See [FEED-OPTICS.md](docs/FEED-OPTICS.md).

For an all-printed option, the [geared alt-az head](docs/GEARED-MOUNT.md) aims with a self-locking printed worm on each axis, 6° per knob turn (`cad/geared-mount.scad`). It mounts on a printed stand, a 1-1/4" pipe mast or a 3/8-16 tripod. The separate [manual aiming adapter](docs/MANUAL-AIMING.md) remains in `cad/`. Its interface and actual hardware stack require a physical fit check with this hub.

## Develop and validate

```sh
npm ci
npm test
npm run test:ui
npm run test:scad   # requires OpenSCAD
npm run docs
npm run build
```

`dist/geometry.js` builds structural solids, `mesh.js` supplies mesh/packing/export utilities, and `solid.js` owns Manifold WASM lifetimes. `feed.js` retains the optics and feed fittings. `exports.js` is the source for generated instructions. `app.bundle.js` and `petal-5.2-offline.html` are reproducible build outputs.

Legacy straps, recessed seam plates, through-face seam fastener modes, perforations, two-facet bulk backs and sacrificial support modes have been removed. Git history preserves earlier implementations; they are not shipped in the current generator.
