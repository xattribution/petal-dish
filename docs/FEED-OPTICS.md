# Feed support and secondary optics — PETAL 4.1

![Actual support geometry](feed-support.png)

## Two different optical systems

A prime-focus dish holds an actual antenna (horn, patch, helix, etc.) with its **phase center** at the parabola's focus. It faces the main reflector. A small object in front of the dish is often this feed, not a second mirror. Three/four metal struts and an adjustable carrier are common mechanical arrangements; there is no single universal feed flange or universal phase-center offset.

A Cassegrain dish instead uses a **convex hyperbolic secondary** ahead of the primary. One hyperbola focus coincides with the primary parabola focus; the other is the phase center of a separate rear feed. The secondary redirects energy toward the rear feed through the center region. A flat disc, a small parabola, or an arbitrary reflector placed exactly at the primary focus is not an equivalent system. The secondary's position, curvature, diameter and rear-feed illumination must agree.

Manufacturer/reference examples:
- RF HAMDESIGN three-leg feed mounting brackets: https://www.rfhamdesign.com/products/parabolicdishkit/accessories/index.php
- RF HAMDESIGN four-leg feed example: https://rfhamdesign.com/products/dish-feeds/septum-dish-feed/index.php
- Cassegrain geometry and electromagnetic analysis example: https://www.mathworks.com/help/antenna/ug/design-and-analyze-cassegrain-antenna.html
- NASA dual-reflector description: https://www.mdscc.nasa.gov/index.php/en/antennas/

## Coordinates and calculations

All z coordinates refer to the **extrapolated primary vertex**, not its rear hub or rim. Positive z points away from the dish toward the feed/sky.

Primary surface: `z(r) = r²/(4f)`, with `f = D × (f/D)` and depth `D²/(16f)`.

Prime-focus mode: `carrier lower face = f − phase offset`. A positive phase offset means the phase center lies farther from the primary than the carrier's lower face. Zero is an explicit placeholder, not a claim that every feed focuses at its flange. The input must come from the selected feed's data or measurement.

Rod geometry uses the actual lower/upper pivot coordinates. If their radii are R and r and their heights are z1 and z2, pivot span is `sqrt((R−r)² + (z2−z1)²)`. The standard printed fittings put each rod mouth 44 mm from its pivot. With 22 mm nominal insertion at each end:

`cut length = pivot span − 2×44 + 2×22 = pivot span − 44 mm`.

The nominal 6.7 mm socket is for **smooth 6.35 mm metal rod**. It is not a nut for 1/4-20 threaded rod, and 6 mm stock is not the specified fit. Clamp fit, cut tolerance, actual insertion and material creep still need physical checks. RODS.csv is generated from the same coordinates used to place the fittings, avoiding a separate approximate length calculator.

Cassegrain mode chooses primary focus f, rear focus g<0, and secondary vertex v between their midpoint and f. Define:

- `center = (f+g)/2`
- `c = (f−g)/2`
- `a = v−center`
- `b² = c²−a²`
- secondary front: `z(r) = center + a sqrt(1+r²/b²)`.

The rim ray from `(D/2, depth)` toward `(0,f)` is intersected with this surface by bounded bisection. The secondary extends 4% farther in radius than that intersection as a geometric allowance. This is not optimized illumination, diffraction control or a gain margin. Its flat rear mounting plane is 10 mm beyond its highest front point; four blind cavities are placed on a 56 mm bolt circle. No fastener breaks the reflecting face.

The return-ray cone is checked against a conservative plane at the highest hub surface, allowing 1.5 mm radial clearance in the Ø30 port. This check says nothing about whether the physical feed horn, connector, cable bend or housing fits. In particular, the default rear phase center at z=−20 mm is close to the hub; an actual feed may require a different layout and a different rear adapter.

## Mechanical scope

Only the selected outer petals get new pads and paired bores; the rest retain their original geometry. Changing segmentation requires a regenerated complete dish/hub, not just new mount petals. The planner uses 6, 12, ... petals for a three-leg arrangement and 8, 12, ... for four legs, while retaining the existing even-petal rule. It rejects pads crowded by ring boundaries or side joints. Optional supports require reinforced curved petals and revision-9 joints. Pad and bracket mass are intentional prototype costs, not a claim of optimized weight. At default 400 mm settings, solid printed CAD volume is 604.57 cm³ with the feed-support option off, 1,005.31 cm³ with the three-leg prime-focus support, and 1,079.82 cm³ with the example Cassegrain secondary. These figures exclude sacrificial print supports, metal rods, fasteners, the RF feed and the separate aiming mount.

The upper carrier has a 40 mm opening and four M4 adapter holes on a 56 mm circle. This is **PETAL's adapter interface**, not an industry-standard RF feed mount. Three broad carrier arms are the baseline; four are optional but add redundant constraint and blockage. The fourth rod is fitted last without preload. Single-axis pivots help alignment and must be locked after it; free hinges are not the finished structure.

The fixed carrier currently accepts 70–86 mm secondaries. Bigger/smaller optical layouts are rejected rather than silently colliding with the fittings or losing mounting edge distance. Rod cuts above 450 mm and spans below 100 mm are likewise rejected for this prototype hardware. These are design-envelope limits, not strength ratings. Larger dishes need a different carrier/rod design, rather than merely scaling this one.

## RF limitations and the next decision

Specify the intended band, polarization, feed type and feed phase center before treating either support as a working antenna design. A geometric focus alone does not establish gain, sidelobes, spillover, polarization purity or bandwidth. Conductive rods, carrier arms, feed body and secondary all obstruct/scatter the aperture. Their projected area is not directly equal to a gain penalty.

Small secondaries can be electrically tiny at common low-GHz bands. The UI warns when secondary diameter is below five wavelengths; that is a prompt for electromagnetic analysis, not a pass/fail qualification threshold. An 80 mm secondary is only about 0.64 wavelength at 2.4 GHz. Such a layout should not be presented as an efficient Cassegrain antenna based on ray tracing. Prime focus is the preferred first RF test at those scales. Larger/high-frequency systems may justify a secondary, with suitable analysis and finishing tolerances.

Coat the secondary's curved face toward the primary using the same verified conductive finish as the main dish. Mask the flat back, insert holes and datums. Fit first, coat later. Validate phase center and focus empirically with stable RF geometry; a DC continuity check alone is not an RF efficiency test.

## Verification record

- Automated tests cover all new meshes' closed topology/winding, quantities, bed bounds, rod lengths and datum relationships, primary phase offset, exported files and representative invalid layouts.
- Independent reflected-ray and constant-path checks verify the hyperboloid construction and the selected rear focus.
- Representative sampled mesh interference checks include clevis/rod-end, saddle/petal, backer/petal, carrier and secondary at default and steeper feed angles. They caught and led to removal of an initial rod-neck/clevis collision. Sampling is not an exhaustive swept-volume proof.
- OpenSCAD snapshot renders are compared with JavaScript meshes for a mounting petal, secondary and rod end in addition to the previous seven cases.
- Offline UI logic tests cover both feed modes and three/four legs; no WebGL visual-browser claim is made.

Next physical sequence: one rod clamp/pin/clevis, then a reinforced mount petal with its saddle/backer, then the complete carrier jig. Measure clamp slip, locked-pivot drift, shape changes when tightening, repeated assembly, temperature drift and feed displacement while changing elevation. Do not use the extra hardware to force the dish or carrier into alignment.
