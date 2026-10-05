# PETAL 5.3 images

Every image is generated from the real app or its real triangles, not concept art.

- `hero.png`: the README header, a 400 mm dish with snap clips, exploded and seen from the front.
- `app.png`: the app in a browser, 400 mm dish with snap clips, a three-rod prime-focus support and the aiming mount, Connections tab open. `node scripts/render-app.mjs` (needs Playwright).
- `assembly.png`: the same dish from the front at 30° elevation.
- `rear.png`: the same assembly from behind.
- `staggered.png`: 600 mm dish, two staggered rings, bolt-or-clip seams, four rods, mount at 20° elevation; the rear view shows the three-panel ring joints.
- `exploded.png`: the 400 mm dish and mount with separated parts. Separation is illustrative, not an assembly path.
- `clip.png`: one clip in its exported broad-side print orientation.
- `collector.png`: 400 mm dish with the Gregorian collector (bowl, mast and insert cup) on the tripod base, with legs at the computed minimum length, 35° elevation.

Orange parts are installed seam clips, blue is the mount, gold is the hub and green parts hold the aluminum rods. Spare clips, metal fasteners, the RF feed and your stand are omitted.

Regenerate the renders with `node scripts/render-system.mjs && python3 scripts/render-system.py` (NumPy and Matplotlib; name cases to render only those). Numerical geometry is not a load rating or a print qualification.
