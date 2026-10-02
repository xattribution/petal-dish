# PETAL 5.3 geometry snapshots

These views render actual generated triangles, not concept illustrations. Orange parts are installed seam clips, blue parts are the mount, gold is the hub, and green parts support stock aluminum rods. Spare clips, metal fasteners, RF feed hardware and the user stand are omitted.

- `assembly.png`: 400 mm dish, clip seams, three-rod prime-focus support, integrated mount at 30° elevation.
- `rear.png`: the same assembly viewed from behind.
- `staggered.png`: 600 mm dish, two staggered rings, hybrid seams, four rods, mount at 20° elevation; rear view emphasizes three-panel joints.
- `exploded.png`: 400 mm dish and mount with separated parts. Separation is illustrative, not an insertion trajectory.
- `clip.png`: clip in its exported broad-side print orientation.

Regenerate with `node scripts/render-system.mjs` and `python3 scripts/render-system.py` (NumPy and Matplotlib required). Numerical geometry is not a load rating or physical print qualification.
