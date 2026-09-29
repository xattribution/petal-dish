# PETAL 5.0 validation record

Run on the integral-flange candidate. These checks establish numerical geometry and application behavior, not physical qualification.

- Twelve structural configurations: default; optional facets; 260, 600 and 800 mm diameters; explicit two-segment petals; f/D 0.25 and 0.8; 1.6 and 6 mm shells; a 120 mm printer-height constraint; coarse mesh with 30 mm facets.
- Closed consistently oriented meshes, positive volume, one connected solid per part, actual bed/height fit and print-plane placement.
- Sampled final-petal radial insertion against both neighbors, including cross-segment neighbors; inter-segment fit and hub/petal interference. Numerical intersection tolerance 0.02 mm³ accommodates float-precision contact slivers; this is not a dimensional tolerance or exhaustive motion proof.
- Root blind-pilot void and roof, clear central passage, matching seam strips, packed quantities and 6 mm bounding-box separation.
- Twenty-seven actual binary structural STLs reloaded independently with trimesh: watertight, consistent winding, one component, nondegenerate triangles. Export quantization is included.
- Nine feed configurations plus independent hyperbolic reflected-ray/equal-path calculations, frequency sizing, rod cuts, fixed primary focus, phase offsets and automatic rod screening.
- Sampled feed-part interference on six configurations, including 4 and 8 mm rods and a steeper support angle.
- Seven OpenSCAD mesh-snapshot renders match generated part volume within 0.002 relative error and dimensions within 0.05 mm (actual errors substantially smaller).
- Offline DOM checks pass embedded WASM initialization, generation, facets, feed-frequency changes, invalid-state export disabling, reset and generated instructions. WebGL rendering is not exercised by that test.
- A 1200 mm dish with a 1000 mm print volume generates longer flanges with six screw stations per side and a matching 36-screw seam schedule; this is a generation check, not structural qualification of a dish that size.

Default 400 mm result: 6 petals + 1 hub, 2 unique parts; approximately 580.28 cm³ solid CAD volume. Side-oriented petal bounds are approximately 173.8 × 70.0 × 173.1 mm. Shared-bed packing puts three petals on each of two 220 mm beds plus one hub bed, with 8 mm edge margin and 6 mm between part bounds. Brims, supports and machine toolhead clearance remain slicer responsibilities.

A default-petal triangle-normal inspection after the orientation fixes found remaining steep downward surfaces localized to pocket/roof details (largest individual triangle approximately 6.6 mm²); this is not a layer-by-layer support check.

No physical print, slicer toolpath, FEA, wind/creep test, RF gain measurement or outdoor-life qualification is claimed. See TEST_ARTICLE.md for the next gate.

## 5.1 hosting and illustrated exports

The geometry/interface remains revision 10. The app now identifies version 5.1 and a content-derived build ID; the standalone file is `petal-5.1-offline.html`. BUILD.json lists checksums of the complete hosted asset set. The stable old offline URL is only a redirect, so downloaded standalone files should use the versioned name.

PDF fixtures cover the default dish, a 600 mm four-rod prime-focus dish with facets, and a 24 GHz secondary configuration. Tests check actual part filenames/quantities, frequency-dependent rod cuts, text bounds, text visibility after page breaks, and a readable ASSEMBLY.pdf inside the ZIP. Rendered pages were visually reviewed. Views use the generated triangle meshes and are illustrative, not dimensioned drawings; exploded separation is not an assembly motion path. Offline DOM tests also activate the PDF button and verify the downloaded PDF header and filename.

The installer-generated configuration was exercised with the official checksum-verified Caddy binary on ports 56302 and 56303: every hosted asset matched BUILD.json, cache revalidation headers were present, and missing routes returned 404. Installer argument validation and Bash syntax checks passed. Package-manager installation and boot-time systemd execution were not run on this workspace; the script targets the documented systemd distributions and preserves an existing PETAL installation on startup/health-check failure.
