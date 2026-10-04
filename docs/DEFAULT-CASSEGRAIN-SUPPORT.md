# PETAL compact rod support · cassegrain

Accessory revision 5: thin curved mounting feet and tapered socket lobes blended into a small central carrier. The smaller chamfered mounting reinforcement replaces the separate rear backer. Regenerate mount petals, rim fittings and puck together; rod cuts change with the housing entrances. These fittings replace revision 1 hinges and are not interchangeable. Seam/hub interface is revision 12; regenerate the entire matching kit. Changing rod count can change dish segmentation; regenerate the complete matching kit if it does.

## Small parts, long metal rods

3 × Ø6.35 mm smooth SOLID aluminum rods; nominal cut 204.13 mm each. No threaded rod. Tubing needs a separate crush-resistant clamp and stiffness calculation. The selected outer petals have compact paired M3 mounting holes near the rim; fittings bolt directly onto the widened mounting petal. These optional rim mounting holes remain through holes. There is no printed snap latch to carry sustained load. Use the supplied mount petals, not holes drilled blindly into an existing rib.

Socket axes are generated at 36.28° above the dish plane. Socket end-to-end span 212.13 mm; lower/upper bore entrances are 22.00 / 22.00 mm from their blind-end datums, with 18 mm nominal engagement. Cut = span − lower entrance − upper entrance + 36 mm. Deburr and mark 18 mm insertion; keep 16–19 mm engagement (at least 1 mm clearance above the bore floor at maximum insertion). Trial-fit long stock and trim; do not force rods against blind ends. RODS.csv contains actual positions, angle, diameter and cuts.

## Assembly and printing

Print one shoe and the puck first. Nominal diametral rod clearance is 0.35 mm; adjust it after a fit coupon/test socket, not by forcing the rod. Feet export on their flat side cheek; puck exports top-face down. The foot follows the dish curvature and has open washer/nut recesses over the rim bolts. The puck has a 44 mm central plate with tapered socket lobes that follow the rods; their envelopes blend into a single piece. Inspect circular socket bores, nut cavities, bolt recesses and screw channels in the slicer and use localized removable supports for their roofs as required; these parts are not certified support-free. Keep support contact off rod-fit and washer-seat surfaces where possible. Ream gently to a sliding fit. PETG is a practical indoor fit-test material; ASA is preferable for outdoor UV exposure with an enclosed printer. Neither removes polymer creep: qualify at actual temperature/load. Use at least four perimeters and locally solid socket/boss walls, not an infill percentage as a strength guarantee.

Install mount petal → rim fitting; use washers under rear bolt heads. Use M3 hardware in FEED-HARDWARE.csv, separate from dish bolts. The two shoe bolts are alongside the rod socket; tighten before fitting rods. Load carrier nuts inward from the outer socket edges; the entry channels lead away from the secondary stem. Fit real M3 nuts into the side-loading hex pockets; a small retaining dab of adhesive may hold the nut during assembly, but keep the threads and rod bore clean. M3×10 radial screws retain smooth solid rods. Rounded tips reduce gouging; do not substitute hollow tubing without a revised clamp. Mark rods to reveal slip. No printed threads.

Support the puck with an independent height/centering jig, slide all rods freely into place and tighten progressively. For four rods, fit the fourth last at zero preload. Never use screws to pull a distorted petal or puck into position. Measure center/height/tilt before and after tightening, after changing elevation, and after warm soaking. Strain-relieve the feed cable along a rod. The compact puck has 3 M3 adapter bores on 24 mm BCD, halfway between rods, and a central M4 clearance hole. This is a PETAL interface, not a universal RF feed flange. Feed-specific adapter and fasteners are not included.

## Frequency and optical meaning

Frequency is unknown; enter it to enable wavelength-based dimensions and accuracy guidance.

Frequency alone does not move the focus of a fixed parabola. Prime focus remains f = D × f/D = 168.000 mm. A phase-center offset can be entered in mm or wavelengths ONLY when supported by the chosen feed design; scaling a generic unknown feed is not valid. The puck lower-face datum is z=177.760 mm; phase-center offset is 0.000 mm along +z.

The secondary is a convex hyperboloid toward the main dish: vertex z=137.760 mm, diameter 80.336 mm, rear focus z=-20.000 mm. Automatic mode targets the entered secondary wavelength count (minimum 56 mm mechanical diameter), solves its position and then recalculates rod lengths and angles. Above 25% dish diameter it rejects the combination; choose prime focus or a larger dish. These are engineering screening limits, not optimized RF dimensions. The 3 mm reflective shell has a central 10 mm boss with one blind Ø5.6×7 mm insert pocket. Install a short M4 insert at least 0.5 mm recessed, maximum 6 mm long; use M4×55 with a 4 mm metal washer/spacer stack through the 46 mm puck/stem stack. Verify actual engagement and leave the reflective face intact. Secondary prints with its boss down; support the surrounding back shell in the slicer. The rear RF feed, phase center, polarization and illumination must be designed separately.

Auto rod sizing screens 4, 5, 6, 6.35 and 8 mm SOLID aluminum using E=69 GPa and a deliberately conservative single cantilever with the entire entered 100 g payload applied transversely over the exposed span: δ=FL³/(3EI). Selected-rod result 0.282 mm. Include feed/secondary, puck, adapter and cable loads in the payload input; wind, joint compliance, resonance and creep are excluded. This is not an allowable-load calculation or an RF guarantee. Manual diameter overrides retain an over-budget warning.

- Prototype: no wind, payload or RF performance rating. Small radial screws require physical slip and warm-creep tests.
- Frequency unspecified: wavelength-dependent dimensions and RF tolerances are unavailable.
- Hyperbolic ray geometry is not electromagnetic validation. A frequency-specific rear feed remains required.

Coat the dish-facing secondary surface with continuous bonded aluminum/copper foil or a verified conductive coating; ordinary metallic paint is not sufficient evidence of RF conductivity. Keep datums, bores and the boss back clean. Surface seams/wrinkles count toward the RF error budget. Test one assembled leg for slip and creep, then the complete assembly at several elevations before committing to a full outdoor installation.
