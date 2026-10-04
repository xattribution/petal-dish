# PETAL compact rod support · prime-focus

Accessory revision 7: rods fasten directly into the underside of the selected outer petals. There are no rim shoes, backing plates or paired mounting bolts. Regenerate the mount petals, carrier and rod cuts as a matching kit; earlier rods and petals do not interchange. Seam/hub interface remains revision 12. Changing rod count may change segmentation.

## Rods and attachment

3 × Ø6.35 mm smooth SOLID aluminum rods; nominal cut 218.55 mm each. The petal has an angled through-hole and a small continuous underside saddle, chamfered into the shell. A side M3×12 headless set screw bears on the rod through a short M3 heat-set insert (maximum 4 mm long, compatible with the 4.2 mm pilot). The carrier retains its hex nuts. No printed threads or extra petal mounting bolts.

Rod elevation is 39.42°. The lower face crossing is solved from the dish curvature, not a fixed shoe height. Cut = datum span − lower face crossing − upper entrance + 36 mm. Mark 18 mm from each rod end. At the petal, align the mark with the center of the front opening; the through-hole has no floor and the rear end may protrude. This insertion mark is not a claim of 18 mm continuous plastic bearing. At the carrier, retain 16–19 mm engagement without bottoming the rod. RODS.csv gives the actual angles, positions and cuts. Trial-fit long stock before final trimming.

## Printing and assembly

The integrated saddle is designed for the petal's existing side-print orientation. Its compact circular tapers blend into the underside. The rod bore has 45° roof shoulders and a 0.8 mm bridge aligned to print-up. The side screw and insert pilot are circular, nearly vertical in the print orientation. Keep this exported orientation. Nominal diametral rod clearance is 0.35 mm; print one mount petal and verify fit first. The circular lower arc locates the rod; the short roof relief clears the full specified diameter.

Heat-set the short M3 insert from the side, recessed 0.5 mm; then insert the rod through the petal and fit the side screw. Keep the screw loose while aligning the carrier with a height/centering jig. Fit the fourth rod last without preload. Tighten progressively and mark the rods to reveal slip. Petal inserts and carrier hex nuts are distinct items in FEED-HARDWARE.csv.

The existing carrier prints top-face down and still needs slicer review/localized support for circular bores and hex-nut roofs. The new petal socket's support-free geometry does not certify the carrier or secondary as support-free. PCTG and ASA still require a fit/slip/temperature test of the printed joint; resin data alone does not rate the assembled part. Use locally solid socket walls and a suitable material profile. Avoid hollow tubing with point screws unless separately engineered for crushing and stiffness.

The carrier has 3 M3 adapter holes on 24 mm BCD and a 12 mm cable opening. A feed-specific adapter and its hardware remain separate. Strain-relieve the cable along a rod. Measure height/centering/tilt before and after tightening and warm soaking.

## Frequency and optical meaning

Frequency is unknown; enter it to enable wavelength-based dimensions and accuracy guidance.

Frequency alone does not move the focus of a fixed parabola. Prime focus remains f = D × f/D = 168.000 mm. A phase-center offset can be entered in mm or wavelengths ONLY when supported by the chosen feed design; scaling a generic unknown feed is not valid. The puck lower-face datum is z=168.000 mm; phase-center offset is 0.000 mm along +z.

Place the actual feed PHASE CENTER at the focus, not automatically its mouth or flange. A zero offset remains unverified. The feed points toward the main dish.

Auto rod sizing screens 4, 5, 6, 6.35 and 8 mm SOLID aluminum using E=69 GPa and a deliberately conservative single cantilever with the entire entered 100 g payload applied transversely over the exposed span: δ=FL³/(3EI). Selected-rod result 0.361 mm. Include feed/secondary, puck, adapter and cable loads in the payload input; wind, joint compliance, resonance and creep are excluded. This is not an allowable-load calculation or an RF guarantee. Manual diameter overrides retain an over-budget warning.

- Prototype: no wind, payload or RF performance rating. Small radial screws require physical slip and warm-creep tests.
- Frequency unspecified: wavelength-dependent dimensions and RF tolerances are unavailable.
- Zero phase-center offset is a placeholder; specify the actual feed datum.

Coat the dish-facing secondary surface with continuous bonded aluminum/copper foil or a verified conductive coating; ordinary metallic paint is not sufficient evidence of RF conductivity. Keep datums, bores and the boss back clean. Surface seams/wrinkles count toward the RF error budget. Test one assembled leg for slip and creep, then the complete assembly at several elevations before committing to a full outdoor installation.
