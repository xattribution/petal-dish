# Direct rod attachment review · revision 7

The broad revision-6 pyramid is replaced by a small rounded rod collar and side-insert ear. Their tapers follow the dish underside; only the internal rod bore follows the calculated rod angle. There are no separate rim shoes or backers.

![Default actual CAD, six angles and installed hardware](feed-review/400-0.42-1.png)

The default mounting petal adds approximately **1.52 cm³**, versus 7.45 cm³ for revision 6: about **80% less added material**. Maximum local underside depth is approximately 7.55 mm for the default 6.35 mm rod. Both dimensions grow with the supplied rod diameter.

Rod diameter is user-entered in millimeters or inches, from 2 to 12.7 mm. Internally all geometry uses millimeters. Both end bores use the supplied diameter plus diametral clearance; automatic sizing retains the 4/5/6/6.35/8 mm stock list.

The nominal round bearing is fully enclosed by a bore with short 45° roof shoulders and a **0.8 mm bridge**, rather than a pointed teardrop apex. The side pilot is circular and aligned nearly vertically in the exported petal orientation. Petal retention uses a short M3 heat-set insert (maximum 4 mm long, compatible with the 4.2 mm pilot) and an M3 × 12 headless screw. Carrier hex nuts remain unchanged.

## Validation

The size/focal-ratio sweep covers dishes from 260 to 1200 mm and f/D from 0.25 to 0.8, plus custom 3.175, 7.9375 and 12.7 mm rods, phase-offset limits, four legs, thin/thick shells and faceted backs. **52 combinations build; 12 fail the existing optical or rod-path limits explicitly.**

Independent mesh checks verify connected, watertight solids, complete nominal rod clearance, screw and insert access, and the full 4 mm insert's retaining wall. Print-angle checks use the actual side-print up vector. The only bridge exemption is the measured 0.8 mm roof; other local faces must meet the 45° criterion. These checks do not qualify physical strength, creep, or printed fit.

[Small deep dish](feed-review/260-0.25-1.png) · [Large deep dish](feed-review/1200-0.25-1.png) · [Secondary support](feed-review/800-0.6-2.png)

The general test suite, offline UI conversion tests and OpenSCAD volume/bounds comparisons cover the updated geometry and exports. The unchanged carrier and secondary still need localized slicer support review. Regenerate all feed parts and rod cuts together; previous feed revisions do not interchange.

Reproduce the sweep with `node scripts/check-feed-envelope.mjs` and `python3 scripts/check-feed-envelope.py`. Render actual mesh views with `python3 scripts/render-feed-review.py CASE_ID`. Python dependencies: NumPy, Pillow, trimesh and manifold3d.
