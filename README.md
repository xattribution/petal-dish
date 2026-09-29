# PETAL 5.0 — integral-flange reflector prototype

A browser/offline generator for a segmented parabolic dish, with an assembly viewer, side-oriented STLs, shared-bed packing and frequency-aware metal-rod feed supports.

Open [the standalone app](dist/petal-offline.html) directly, or serve `dist/` locally. The app includes its solid-modeling kernel and works without a network connection.

**Start with the two seam test strips in the exported ZIP, then one full petal.** This version is an unprinted engineering prototype. A closed STL is not a print-process, load or RF qualification.

## Simpler assembly

- Integral underside flanges replace separate seam brackets. Sideways M3 bolts and captive nuts stay behind the RF surface.
- Short tapered keys locate neighboring petals while allowing radial insertion, including the final petal. Screws retain seated datums; they must not pull warp out of the shell.
- One rear hub, a clear Ø30 mm opening, and four blind M4 mount inserts on Ø60 mm BCD. Root screws also enter blind inserts, keeping the reflecting face closed.
- Petals print on a flat radial flange. Curved undersides are the default; optional small tangent-plane facets add little material and preserve the front parabola.
- Larger dishes split into aligned chordal segments with integral cross-flanges. The planner rejects layouts without space for joints or the printer.
- Default 400 mm dish: six petals and one hub, two unique printed parts. Optional feed fittings add parts. This is a geometric simplification, not a demonstrated strength rating.

**Interface revision 10 requires a complete regenerated kit.** Previous petals, seam saddles, front caps and hubs are incompatible. Short M4 inserts (maximum 6 mm long) replace the former long inserts. Check the separate aiming adapter's screw lengths against the new blind hub before use.

## Print and assemble

Read the kit's `ASSEMBLY.md`, `HARDWARE.csv` and `REFLECTOR.md`; the app's Print notes use the same source. Keep exported orientations. Add a brim and inspect every layer around flange roots, pointed bore roofs and keys. No disposable supports are generated; local slicer supports may still be needed. Shared beds leave 6 mm between part bounds, permitting at most 3 mm individual brims. Print either packed beds or individual quantities, not both.

PETG is a practical indoor fit-test material. ASA is an outdoor starting point with a controlled enclosure and thermal/creep testing. Four perimeters and locally solid pads are starting settings, not a strength guarantee. See [the test procedure](docs/TEST_ARTICLE.md) and [engineering limits](docs/ENGINEERING.md).

Use M3 × 12 seam screws, 0.5 mm head washers and ordinary M3 nuts. Root screws are M4 × 12 with 1 mm washers. Insert dimensions must match the actual supplier and filament. The generated schedule specifies quantities; external mount screw length depends on the adapter stack.

## RF finish and feed supports

Printed plastic needs a conductive surface and an appropriate RF feed. Thin aluminum foil tape with specified conductive adhesive is the baseline finishing approach; copper foil or a specified conductive coating are alternatives. Mask joints, align first and bridge seams afterward without tensioning the dish. Ordinary metallic paint is not evidence of adequate conductivity. See [REFLECTOR.md](docs/REFLECTOR.md) for preparation and source references.

Optional three/four-rod supports retain compact bolted rim shoes and a round puck. Smooth solid aluminum rod defaults to 6.35 mm (1/4 inch); generated socket angles and `RODS.csv` give the actual cuts. Frequency informs accuracy budgets, rod screening and experimental Cassegrain secondary sizing. A fixed parabola's focal point does not move with frequency. Prime-focus offsets require the actual feed phase center. See [FEED-OPTICS.md](docs/FEED-OPTICS.md).

The separate [manual aiming adapter](docs/MANUAL-AIMING.md) remains in `cad/`. Its interface and actual hardware stack require a physical fit check with this hub.

## Develop and validate

```sh
npm ci
npm test
npm run test:ui
npm run test:scad   # requires OpenSCAD
npm run docs
npm run build
```

`dist/geometry.js` builds structural solids, `mesh.js` supplies mesh/packing/export utilities, and `solid.js` owns Manifold WASM lifetimes. `feed.js` retains the optics and feed fittings. `exports.js` is the source for generated instructions. `app.bundle.js` and `petal-offline.html` are reproducible build outputs.

Legacy straps, recessed seam plates, through-face seam fastener modes, perforations, two-facet bulk backs and sacrificial support modes have been removed. Git history preserves earlier implementations; they are not shipped in the current generator.
