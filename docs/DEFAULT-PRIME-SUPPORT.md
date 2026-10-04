# PETAL compact rod support · prime-focus

Accessory revision 4: continuous fixed outer housings, angled internal bores and 45° perimeter chamfers. The widened mounting petal replaces the separate rear backer. Regenerate mount petals, rim fittings and puck together; rod cuts change with the housing entrances. These fittings replace revision 1 hinges and are not interchangeable. Seam/hub interface is revision 12; regenerate the entire matching kit. Changing rod count can change dish segmentation; regenerate the complete matching kit if it does.

## Small parts, long metal rods

3 × Ø6.35 mm smooth SOLID aluminum rods; nominal cut 189.11 mm each. No threaded rod. Tubing needs a separate crush-resistant clamp and stiffness calculation. The selected outer petals have compact paired M3 mounting holes near the rim; fittings bolt directly onto the widened mounting petal. These optional rim mounting holes remain through holes. There is no printed snap latch to carry sustained load. Use the supplied mount petals, not holes drilled blindly into an existing rib.

Socket axes are generated at 34.10° above the dish plane. Socket end-to-end span 206.50 mm; lower/upper bore entrances are 28.91 / 24.48 mm from their blind-end datums, with 18 mm nominal engagement. Cut = span − lower entrance − upper entrance + 36 mm. Deburr and mark 18 mm insertion; keep 16–19 mm engagement (at least 1 mm clearance above the bore floor at maximum insertion). Trial-fit long stock and trim; do not force rods against blind ends. RODS.csv contains actual positions, angle, diameter and cuts.

## Assembly and printing

Print one shoe and the puck first. Nominal diametral rod clearance is 0.35 mm; adjust it after a fit coupon/test socket, not by forcing the rod. Shoes export on their broad flat side cheek; puck exports top-face down. The saddle follows the dish curvature and has open washer/nut recesses over the rim bolts. The puck is one tapered housing; its rod bores and retention pockets follow the calculated rod angle. Inspect circular socket bores, nut cavities, bolt recesses and screw channels in the slicer and use localized removable supports for their roofs as required; these parts are not certified support-free. Keep support contact off rod-fit and washer-seat surfaces where possible. Ream gently to a sliding fit. PETG is a practical indoor fit-test material; ASA is preferable for outdoor UV exposure with an enclosed printer. Neither removes polymer creep: qualify at actual temperature/load. Use at least four perimeters and locally solid socket/boss walls, not an infill percentage as a strength guarantee.

Install mount petal → rim fitting; use washers under rear bolt heads. Use M3 hardware in FEED-HARDWARE.csv, separate from dish bolts. The two shoe bolts are alongside the rod socket; tighten before fitting rods. Fit real M3 nuts into the side-loading hex pockets; a small retaining dab of adhesive may hold the nut during assembly, but keep the threads and rod bore clean. M3×10 radial screws retain smooth solid rods. Rounded tips reduce gouging; do not substitute hollow tubing without a revised clamp. Mark rods to reveal slip. No printed threads.

Support the puck with an independent height/centering jig, slide all rods freely into place and tighten progressively. For four rods, fit the fourth last at zero preload. Never use screws to pull a distorted petal or puck into position. Measure center/height/tilt before and after tightening, after changing elevation, and after warm soaking. Strain-relieve the feed cable along a rod. The compact puck has 3 M3 adapter bores on 24 mm BCD, halfway between rods, and a 12 mm cable opening. This is a PETAL interface, not a universal RF feed flange. Feed-specific adapter and fasteners are not included.

## Frequency and optical meaning

Frequency is unknown; enter it to enable wavelength-based dimensions and accuracy guidance.

Frequency alone does not move the focus of a fixed parabola. Prime focus remains f = D × f/D = 168.000 mm. A phase-center offset can be entered in mm or wavelengths ONLY when supported by the chosen feed design; scaling a generic unknown feed is not valid. The puck lower-face datum is z=168.000 mm; phase-center offset is 0.000 mm along +z.

Place the actual feed PHASE CENTER at the focus, not automatically its mouth or flange. A zero offset remains unverified. The feed points toward the main dish.

Auto rod sizing screens 4, 5, 6, 6.35 and 8 mm SOLID aluminum using E=69 GPa and a deliberately conservative single cantilever with the entire entered 100 g payload applied transversely over the exposed span: δ=FL³/(3EI). Selected-rod result 0.213 mm. Include feed/secondary, puck, adapter and cable loads in the payload input; wind, joint compliance, resonance and creep are excluded. This is not an allowable-load calculation or an RF guarantee. Manual diameter overrides retain an over-budget warning.

- Prototype: no wind, payload or RF performance rating. Small radial screws require physical slip and warm-creep tests.
- Frequency unspecified: wavelength-dependent dimensions and RF tolerances are unavailable.
- Zero phase-center offset is a placeholder; specify the actual feed datum.

Coat the dish-facing secondary surface with continuous bonded aluminum/copper foil or a verified conductive coating; ordinary metallic paint is not sufficient evidence of RF conductivity. Keep datums, bores and the boss back clean. Surface seams/wrinkles count toward the RF error budget. Test one assembled leg for slip and creep, then the complete assembly at several elevations before committing to a full outdoor installation.
