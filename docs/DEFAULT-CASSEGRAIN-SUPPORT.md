# PETAL rod support · cassegrain

Experimental accessory revision 1. Regenerate the mount petals and all feed-support parts together. The seam/hub interface remains revision 9. If the chosen rod count changes the petal/ring segmentation, regenerate the entire dish including its hub and plates. Four rods cannot be added at equal petal-center spacing to the default six-petal dish. Rods and fasteners are purchased, not printed.

## Layout and rod cuts

3 equally spaced supports. The generator selects a petal count divisible by 3; only the named mount petals receive reinforced pads and two extra through holes. Install them at the azimuths in RODS.csv. These local accessory holes remain through holes even when the dish seam hardware uses blind inserts. Mount feet cover them.

Use smooth 6.35 mm (1/4 inch) metal rod, preferably aluminum for the initial lightweight indoor article. This fitting is NOT for 1/4-20 threaded rod. Deburr the ends. Cut 3 rods nominally 71.72 mm (2.8234 inch). Cut slightly long for trial assembly and trim to the measured fit; do not force an oversize rod between locked pivots.

Pivot-center span = 115.72 mm. Each printed rod end places its mouth 44 mm from the pin center. Cut = span − 2×44 + 2×22. Mark 22 mm nominal insertion at each end; retain 18–26 mm engagement and at least 2 mm bottom clearance. The ±4 mm per-end range is assembly adjustment, not a substitute for recalculating a different feed. The lower/upper pivots are at z=79.29 / 135.76 mm, measured from the extrapolated dish vertex.

## Hardware and assembly

Per leg: two smooth-rod split clamps; two M5×30 pivot screws, four M5 washers and two M5 prevailing-torque nuts; two M4×16 clamp screws and two M4 nuts (7 mm AF); two M4×20 upper-foot screws, washers and nuts. Lower-foot screws: 6 × M4 × 55, nominal grip 45.70 mm plus two 1 mm washers, nut and protruding threads. The flat upper/lower datums make the total grip equal at the two positions even though the saddle and petal thicknesses vary. Verify actual printed stacks before buying the set. FEED-HARDWARE.csv lists support hardware separately from the dish hardware. No printed threads. Start with fit checks of the 6.7 mm socket, 5.4 mm pivot bore and nut pocket.

Rear backer → reinforced petal → curved saddle → clevis. Keep the saddle face clean of foil/glue. Use the paired screws, not a single bolt through an unsupported shell. Place the carrier with the 40 mm center opening above the dish; its clevises face downward. The four M4 holes on a 56 mm bolt circle are an adapter interface, not a universal feed standard. Design a short feed-specific adapter once its dimensions and phase center are known.

Keep pivot pins and rod clamps loose during alignment. Set carrier center, height and tilt with an independent jig, then tighten progressively. Lock the pivot faces after alignment: these are positioning hinges, not free-running ball joints. With four legs, fit the fourth last at zero preload. Never use the fourth rod to bend the petals into place. Recheck shape after every tightening step and after changing elevation. Strain-relieve coax along one rod with an axis service loop.

## Optical meaning

The secondary is a convex hyperboloid facing the primary dish. Primary focus z=168.00 mm; rear feed phase center z=-20.00 mm; secondary vertex z=137.76 mm. Its diameter is 80.34 mm, including a 4% radial geometric margin beyond the intercepted primary-rim ray. This is NOT an optimized edge taper. The secondary rear plane contacts the carrier lower face at z=157.76 mm. Coat the curved face toward the main dish; leave the flat mounting back uncoated. Four blind Ø5.6×7 mm cavities accept a separately verified short M4 heat-set insert no longer than 6 mm. Recess the insert 0.5 mm below the flat back. Use M4×12 screws through the 6 mm carrier without washers, with actual thread engagement checked; tips must never reach the curved face. The feed behind the main dish must point toward the secondary. Its physical aperture, beam taper, phase center, polarization, and clearance through the 30 mm hub are not determined by a ray model. No rear-feed adapter is supplied.

- Support rods and the carrier scatter/block RF; no gain or wind rating is implied.
- The rod-end pivots are locked after alignment. Do not leave a freely hinged structure.
- Cassegrain is an experimental geometric-optics layout, not an RF-qualified antenna. A band-specific rear feed and electromagnetic validation are still required.
- Operating frequency is unspecified: secondary electrical size cannot be assessed.

The long rods are not printable parts and are shown only as reference lines in the viewer. Print clevises base-down and carriers flat; rod clamps are exported on their side so the eye-to-socket load path lies mainly in the layer plane. Inspect/support the horizontal rod bore and split-clamp roofs in the slicer. Print the saddle on its flat face; the secondary is back-down, with slicer support under the central blind-hole roofs if needed. Verify the reflective surface after support removal.

Test one leg before a full set: fit, clamp slip, pin locking, creep and repeated assembly. Then verify carrier movement under light lateral/axial load at several dish elevations. No supported payload, wind speed, or continuous-service temperature is assigned.
