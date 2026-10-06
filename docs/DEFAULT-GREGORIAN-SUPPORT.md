# PETAL Gregorian collector

The dish gathers, the bowl folds the focus back down, and the insert sits under the bowl looking up. Rays from the dish converge on the prime focus F1 (z = 168.0 mm), cross it, strike the concave bowl just beyond it and reflect down to the second focus F2 (z = 96.8 mm). Put the insert's phase center at F2. The insert, its cup and the mast all stand inside the bowl's shadow, so the only aperture blockage is the bowl itself: 4.9%.

## How the bowl is shaped

The bowl is a section of a prolate ellipsoid whose two foci are F1 and F2 (the Gregorian condition). Any ray through one focus reflects through the other, so the fold is exact for every ray from the dish. About F1, the surface is t(ψ) = b² / (a + c·cos ψ) with a = 74.101, b = 64.999 and c = 35.581 mm; ψ is measured from the dish axis.

It is re-solved on every build from the dish diameter and f/D:

- Rim: the bowl edge sits at the dish rim angle 61.5° plus 3° spill margin (64.5°), so every ray from the dish lands on the bowl.
- Focus spacing: F1 to F2 is 71.2 mm, set so the rim ray reaches the insert at ±25°.
- Size: the smallest bowl whose shadow still covers the insert cup, or N wavelengths when a frequency is set, or the entered diameter. This one is Ø85.3 mm (minimum Ø85.3).

A deeper dish or a narrower insert angle spreads F1 and F2 further apart, so a smaller bowl still shades the insert. A larger insert needs a larger bowl.

Magnification M = (1 + e) / (1 − e) = 2.85, with eccentricity e = c / a = 0.480. The insert sees the system as an f/D 1.20 dish: the dish rim arrives at ±23.6° and the bowl edge at ±25°. Choose an insert whose −10 dB beam edge is near ±25°.

## Parts

- Collector bowl, Ø85.3 mm, 3 mm shell. The wall thickens into 3 rod seats between the flat top and the rim, so nothing hangs below the rim. Prints on its flat top with the reflecting face up; no supports.
- Mast foot: bolts to the hub front through the four mount holes. Prints flange down.
- Insert cup: Ø30 mm bore, 12.0 mm deep, on the mast tube. Prints socket down.
- Mast tube: Ø16 mm, cut 72.8 mm. Aluminum or rigid conduit; the insert cable runs inside it.

## Assembly

1. Heat-set the M3 inserts: one in each petal rod socket and one in the rib of the mast foot and insert cup.
2. Bolt the mast foot to the hub front with the four mount through bolts; the nuts sit on top of the 6 mm flange.
3. Feed the insert cable through the tube, seat the tube in the foot and the cup, and snug both set screws.
4. Seat the insert in the cup with its phase center 0.0 mm above the cup rim (z = 96.8 mm).
5. Mark each of the 3 × Ø6.35 mm rods (cut 229.0 mm) 18 mm from the petal end and 11 mm from the bowl end. Fit them into the petals, then push them into the bowl's rod seats up to the marks. Center the bowl over the hub, drill each rod Ø3.2 through the seat's cross hole, then fit the M3 bolts with a washer on each flat face and a nyloc.
6. Line the bowl's concave face with bonded aluminum or copper foil, seams pressed flat.

## Checks

- Heights run along the dish axis from the paraboloid vertex; the hub front is at z = 3.0 mm.
- Bowl vertex z = 206.5 mm, rim z = 188.3 mm.
- Bowl apex to insert: 109.7 mm.
- Shadow cone half-angle from the prime focus: 15.0°. Keep everything under the bowl inside it.

- Prototype: no wind, payload or RF performance rating. Small radial screws require physical slip and warm-creep tests.
- Frequency unspecified: wavelength-dependent dimensions and RF tolerances are unavailable.
- Ellipsoidal ray geometry is not an electromagnetic validation; measure the assembled collector against a reference antenna.
