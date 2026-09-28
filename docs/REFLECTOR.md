# Reflector surface and RF setup

PETAL prints a support structure. The frequency, feed, conductive finish and mount determine whether the assembled system works as an antenna. There is no measured gain or RF rating for this design.

## Recommended first article: a continuous foil face

Use a thin aluminum foil tape whose adhesive is explicitly conductive, for example 3M 1170. 3M 1181 copper foil tape is another conductive-adhesive option, but copper adds more mass. These are examples of material construction, not qualified PETAL finishes or outdoor lifetime guarantees. Ordinary HVAC tape may have insulating adhesive; overlapping it does not guarantee electrical contact between strips.

Sources: https://www.3m.com/3M/en_US/p/d/b5005158003/ and https://www.3m.com/3M/en_US/p/d/b5005158005/

1. Print an adhesion/coating scrap from the actual filament. Check the tape and any primer after a warm hold and gentle peel. Do not assume adhesion to an injection-molded plastic transfers to a printed surface.
2. Dry-fit, mark and measure the uncoated assembly. Complete heat-setting before applying the conductive finish. Lightly remove burrs; do not sand the parabola into a different shape.
3. Apply narrow strips to each front petal, burnishing gently from the center outward. Start around 10–20 mm strip width and reduce it where compound curvature causes wrinkles. Avoid stretching foil tight across gaps or unsupported holes. Keep overlaps small and consistent; record their height if operating at short wavelengths.
4. Mask root grooves, plate seats, bearing faces and bores. Cover the front cap with the same finish, preserving access to hardware. Do not bridge a pocket with foil that a screw will tear during service.
5. Assemble and align first. Bridge seams with removable strips afterward, without pulling petals together. Remove these strips before disassembly. Do not use a continuous skin as an alignment clamp.
6. With power disconnected, check foil-to-foil continuity across strips, cap and seams at many points. Compare against the meter’s shorted-lead baseline; intermittent or unexpectedly high resistance needs investigation. DC continuity is a useful workmanship check, not proof of RF efficiency.
7. Recheck surface shape, adhesion and continuity after thermal cycling and repeated assembly. Corrosion, water ingress and differential expansion require an outdoor finishing plan. Do not assume a generic clearcoat is RF-neutral or compatible with the tape. Keep a specular foil-covered dish pointed away from the Sun during handling; concentrated sunlight near the focus can heat the feed or fixture.

## Conductive paints

A specified silver or silver-coated-copper conductive coating is an alternative when foil wrinkles are unacceptable. Examples are MG Chemicals 842AR and 843AR. They are designed as conductive shielding coatings; that does not establish antenna-reflector performance on PETAL. Follow the current technical data and safety sheets, verify dry-film thickness and sheet resistance, and test solvent/primer compatibility on the actual ASA, ABS or PCTG print. Solvent attack, shrinkage and poor adhesion can ruin surface accuracy. Metallic-looking craft paint, ordinary graphite paint and carbon-filled filament are not automatically suitable low-loss reflectors.

Sources: https://mgchemicals.com/products/conductive-paint/conductive-acrylic-paints/silver-conductive-paint/ and https://mgchemicals.com/products/conductive-paint/conductive-acrylic-paint/copper-conductive-paint/

## Perforations and mesh

Keep perforations off for the baseline article. Conductive mesh must be chosen for the intended wavelength; openings, wire size, conductivity and surface accuracy all matter. Do not equate percent open area with gain loss. A continuous foil covering over printed holes needs support against sagging and will largely block airflow. Reference: https://www.gmrt.ncra.tifr.res.in/doc/WEBLF/LFRA/node173.html

## Feed and alignment

This configuration has focal length 168.00 mm from the parabola vertex, dish depth 59.52 mm, and rim half-angle 61.53 degrees as seen from the focus. The vertex is the extrapolated center of the parabola, not the rear hub surface or rim plane. Place the feed phase center at the focus and match its illumination to that rim angle. Provide rigid, adjustable axial/lateral positioning and polarization adjustment. Route and strain-relieve the cable so it cannot move the feed. Feed and support parts are not included.

Choose an operating band before setting surface tolerances. As an engineering starting budget, allocate no more than wavelength/50 RMS to structural/assembly error, then account separately for finish, feed and pointing. This is a proposed test criterion, not a verified capability. Use the shortest wavelength in a band. Measure at several azimuths and elevations; systematic seam steps and defocus need attention beyond a single RMS number.

Check feed return loss if equipment is available, but also compare received level and beam pattern against a known reference with fixed polarization, cable, receiver gain and geometry. Good impedance matching alone does not prove reflector gain. Use a suitable far-field range or a properly characterized near-field method, rather than a nearby transmitter of unknown geometry. RF background: https://www.cv.nrao.edu/~sransom/web/Ch3.html

## Material references

ASA: outdoor UV/temperature suitability, with significant warping risk unless the printer environment is controlled: https://help.prusa3d.com/article/asa_1809
PCTG: manufacturer-specific material data; heat-distortion temperature is not a continuous precision-service limit: https://fiberlogy.com/en/fiberlogy-filaments/pctg/
Inserts and plastic creep: https://www.spirol.com/resources/white-papers/how-to-properly-mate-compression-limiters-and-threaded-inserts-in-plastic-assemblies/
