"""Light-time geometry of bent-pipe paths (MATH.md sections 3, 5 and 6).

All positions are ECEF (WGS84) metres, times are float Unix seconds (UTC), delays are
seconds, rates are seconds per second and frequencies hertz.

Shapes: an emitter position ``x_e`` may carry leading axes ``[..., 3]``; a time
argument may be a float or ``ndarray[M]``; results broadcast to ``[..., M]`` (``[...]``
for a scalar time). ``orbits`` is ``dict[sat_id, Orbit]`` and ``sites_xyz`` is
``dict[site_id, ndarray[3]]`` (antenna phase centres).

Delays solve the light-time equations with the Earth-rotation (Sagnac) terms. Rates
are derivatives of those delay functions obtained by differencing them on the 5-point
stencil ``t + k h``, ``k = -2..2``, ``h = 0.5 s``; they never use the velocity returned
by ``state_ecef``. The same five samples define a quartic in time, which is how the
predictors evaluate path A at the shifted epoch ``t_mid + TDOA`` without re-evaluating
the orbit for every emitter position.

A float64 Unix time resolves 2.4e-7 s, during which a satellite in low orbit moves
2 mm. Internally the satellite is therefore evaluated at "reception time minus delay"
with the two terms kept apart (``orbits.orbit_position(orbit, t_s, dt_s)``), and the
delay functions accept an optional ``dt_s`` so that callers holding a base time and
small per-sample offsets (``t0`` plus ``n / fs``) can do the same: the delay returned
is the one at ``t + dt_s`` exactly.

Time arguments are Unix seconds; a value beyond ``timebase.MAX_UNIX_S`` (an integer
``t_ns`` passed by mistake) raises ValueError naming the argument, as do non-finite
positions, delays, frequencies and uncertainties.

Nothing here applies a visibility test to a delay: the equations are evaluated for any
geometry. ``elevation_deg``, ``footprint`` and ``measurement_windows`` are where the
horizon enters.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .constants import C, OMEGA_E, WGS84_A, WGS84_F
from .geodesy import ecef_to_lla, lla_to_ecef, wrap_lon_deg
from .orbits import Orbit, orbit_position
from .timebase import MAX_UNIX_S
from .types import PathSpec, Uncertainty

__all__ = [
    "PathSpec",
    "BentPipe",
    "STENCIL_STEP_S",
    "uplink_delay",
    "downlink_delay",
    "bent_pipe",
    "bent_pipe_rates",
    "baseband_frequency",
    "predict_tdoa",
    "predict_fdoa",
    "predict_pair",
    "elevation_deg",
    "footprint",
    "measurement_windows",
    "search_windows",
    "self_peak",
]

STENCIL_STEP_S: float = 0.5
"""Step ``h`` of the 5-point rate stencil, seconds (a power of two: ``t +/- h`` is exact)."""

_OFFSETS = np.array([-2.0, -1.0, 0.0, 1.0, 2.0]) * STENCIL_STEP_S
_PASSES = 3  # downlink (moving transmitter): plain fixed point from the geometric range
_UPLINK_PASSES = 1  # uplink: one pass from the first-order start, see _uplink
_MAX_SHIFT_S = 2.0 * STENCIL_STEP_S
_BLOCK_ELEMENTS = 1 << 16  # emitter-by-time elements per uplink block (0.5 MB of float64)
_B = WGS84_A * (1.0 - WGS84_F)

# measurement_windows: the predictions vary over the ground on the scale of the lowest
# satellite's height, so the grid step is capped at this fraction of that height (as an
# angle at the Earth's centre: 40 deg for GEO, 0.45 deg at 400 km). Measured against
# dense grids: second differences stop bounding the in-cell variation above about 1/4.
_GRID_HEIGHT_FRACTION = 0.125
_MAX_GRID_POINTS = 4_000_000
_CONTOUR_POINTS = 720  # samples of each satellite's elevation-mask contour
_CONTOUR_BISECTIONS = 30  # pi / 2^30 rad: 2 cm on the ground
_ARC_END_SUBDIVISIONS = 33  # applied twice: arc ends located to 1/1024 of the contour spacing


@dataclass
class BentPipe:
    """Delays of one bent-pipe path received at the site at ``t_rx_s``.

    D: total delay ``tau_d + tau_x + tau_u``, s, ``[..., M]``.
    tau_d: downlink delay for reception at ``t_rx_s``, s, ``[M]``.
    tau_u: uplink delay for arrival at the satellite at ``t_sat_s``, s, ``[..., M]``.
    t_sat_s: time the signal left the satellite, ``t_rx_s - tau_d - tau_x``, Unix s, ``[M]``.
    (No ``M`` axis when the time argument is a scalar.)
    """

    D: Any
    tau_d: Any
    tau_u: Any
    t_sat_s: Any


# --------------------------------------------------------------------------- light time


def uplink_delay(x_e: Any, orbit: Orbit, t_sat_s: Any, dt_s: Any = 0.0) -> Any:
    """Uplink light time for a signal arriving at the satellite at ``t_sat_s``.

    x_e: emitter position, ECEF m, ``[..., 3]`` (fixed to the Earth).
    t_sat_s: arrival time at the satellite, Unix s, scalar or ``[M]``.
    dt_s: optional small offset, s (scalar or ``[M]``): the arrival is at
    ``t_sat_s + dt_s``, evaluated without rounding that sum to float64.
    Returns ``tau_u`` in seconds, ``[..., M]``, solving
    ``C tau_u = | r(t_sat) - Rz(-OMEGA_E tau_u) x_e |``.
    """
    x = _points(x_e, "x_e")
    t, extra, scalar = _time_arg(t_sat_s, "t_sat_s", dt_s)
    tau = _uplink(x, orbit_position(orbit, t, extra))
    return _finish(tau, scalar)


def downlink_delay(orbit: Orbit, x_g: Any, t_rx_s: Any, dt_s: Any = 0.0) -> Any:
    """Downlink light time for a signal arriving at a fixed site at ``t_rx_s``.

    x_g: site position, ECEF m, ``[3]`` (leading axes ``[..., 3]`` are allowed).
    t_rx_s: reception time, Unix s, scalar or ``[M]``.
    dt_s: optional small offset, s (scalar or ``[M]``): reception is at
    ``t_rx_s + dt_s``, evaluated without rounding that sum to float64.
    Returns ``tau_d`` in seconds, ``[M]`` (``[..., M]`` with leading axes), solving
    ``C tau_d = | x_g - Rz(-OMEGA_E tau_d) r(t_rx - tau_d) |``.
    """
    g = _points(x_g, "x_g")
    t, extra, scalar = _time_arg(t_rx_s, "t_rx_s", dt_s)
    tau = _downlink(orbit, g[..., None, :], t, extra)
    return _finish(tau, scalar)


def bent_pipe(
    x_e: Any, orbit: Orbit, x_g: Any, t_rx_s: Any, tau_x: float = 0.0, dt_s: Any = 0.0
) -> BentPipe:
    """Delays of the path emitter -> satellite -> site for reception at ``t_rx_s``.

    x_e: emitter, ECEF m, ``[..., 3]``. x_g: site, ECEF m, ``[3]``. t_rx_s: reception
    time at the site, Unix s, scalar or ``[M]``. tau_x: transponder group delay, s.
    dt_s: optional small offset, s (scalar or ``[M]``): reception is at
    ``t_rx_s + dt_s`` exactly, e.g. a capture start time plus ``n / fs``.

    ``t_sat = t_rx - tau_d(t_rx) - tau_x``; the uplink delay is evaluated for arrival
    at ``t_sat``; ``D = tau_d + tau_x + tau_u``.

    Internally the satellite is evaluated at ``t_rx`` minus the delays without
    rounding that difference to float64, so ``D`` is a smooth function of ``t_rx`` to
    about 1e-16 s. The reported ``t_sat_s`` is the rounded float (2.4e-7 s resolution):
    ``uplink_delay(x_e, orbit, t_sat_s)`` can therefore differ from ``tau_u`` by
    ``tau_u_dot`` times that rounding (picoseconds in low orbit).
    """
    x = _points(x_e, "x_e")
    g = _site(x_g, "x_g")
    t, extra, scalar = _time_arg(t_rx_s, "t_rx_s", dt_s)
    tau_d, t_sat, tau_u = _bent_pipe(x, orbit, g, t, extra, _delay(tau_x, "tau_x"))
    total = (tau_d + tau_x) + tau_u
    return BentPipe(
        D=_finish(total, scalar),
        tau_d=_finish(tau_d, scalar),
        tau_u=_finish(tau_u, scalar),
        t_sat_s=_finish(t_sat, scalar),
    )


def bent_pipe_rates(
    x_e: Any, orbit: Orbit, x_g: Any, t_rx_s: Any, tau_x: float = 0.0, dt_s: Any = 0.0
) -> tuple[Any, Any, Any]:
    """Delay rates of a bent-pipe path: ``(D_dot, tau_d_dot, tau_u_dot)``.

    Arguments as for ``bent_pipe``. ``D_dot`` (``[..., M]``) and ``tau_d_dot``
    (``[M]``) are derivatives with respect to the reception time; ``tau_u_dot``
    (``[..., M]``) is ``d tau_u / d t_sat``, with respect to the uplink's own arrival
    time. They satisfy ``D_dot = tau_d_dot + tau_u_dot (1 - tau_d_dot)``.

    ``D_dot`` and ``tau_d_dot`` come from differencing ``bent_pipe`` on the 5-point
    stencil ``f' = [8 (f(t+h) - f(t-h)) - (f(t+2h) - f(t-2h))] / (12 h)``, ``h = 0.5 s``;
    ``tau_u_dot`` follows from the identity above. No state velocity is involved.
    """
    x = _points(x_e, "x_e")
    g = _site(x_g, "x_g")
    t, extra, scalar = _time_arg(t_rx_s, "t_rx_s", dt_s)
    total, tau_d = _path_quartics(x, orbit, g, t, extra, _delay(tau_x, "tau_x"))
    d_dot = total.rate0()
    tau_d_dot = tau_d.rate0()
    tau_u_dot = (d_dot - tau_d_dot) / (1.0 - tau_d_dot)
    return _finish(d_dot, scalar), _finish(tau_d_dot, scalar), _finish(tau_u_dot, scalar)


def baseband_frequency(
    f_e: Any, translation: Any, f_tune: Any, tau_d_dot: Any, tau_u_dot: Any, eps_g: Any = 0.0
) -> Any:
    """Baseband frequency of one path in a channel (MATH.md section 4), hertz.

    f_e: emitter carrier, Hz. translation: transponder translation actually applied,
    Hz (downlink = uplink + translation). f_tune: channel tuning, Hz. tau_d_dot,
    tau_u_dot: delay rates as returned by ``bent_pipe_rates``. eps_g: fractional error
    of the site oscillator.

    ``f_bb = (f_e + T - f_tune (1 + eps_g)) - (f_e + T) tau_d_dot - f_e tau_u_dot (1 - tau_d_dot)``
    """
    f_e = np.asarray(f_e, dtype=np.float64)
    f_down = f_e + np.asarray(translation, dtype=np.float64)
    f_tune = np.asarray(f_tune, dtype=np.float64)
    tau_d_dot = np.asarray(tau_d_dot, dtype=np.float64)
    tau_u_dot = np.asarray(tau_u_dot, dtype=np.float64)
    # (f_down - f_tune) - f_tune eps_g keeps the small oscillator term out of a 1 + eps product
    offset = (f_down - f_tune) - f_tune * np.asarray(eps_g, dtype=np.float64)
    out = offset - f_down * tau_d_dot - f_e * tau_u_dot * (1.0 - tau_d_dot)
    return out[()] if out.ndim == 0 else out


# --------------------------------------------------------------------------- predictors


def predict_tdoa(
    x_e: Any,
    a: PathSpec,
    b: PathSpec,
    t_rx_s: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
) -> Any:
    """Predicted TDOA of path A minus path B, seconds, ``[..., M]`` (MATH.md section 5).

    x_e: emitter, ECEF m, ``[..., 3]``. a, b: the two paths. t_rx_s: ``t_mid``, the
    midpoint of the channel-B samples, Unix s, scalar or ``[M]``.

    ``h_tdoa = D_A(t_mid + h_tdoa) - D_B(t_mid)``, one fixed-point pass from
    ``D_A(t_mid) - D_B(t_mid)``, nominal oscillators and zero clock offsets. Because
    path A is evaluated at the shifted epoch, swapping A and B does not exactly negate
    the result: ``h_AB + h_BA ~ h_AB (D_dot_A - D_dot_B)``.
    """
    tdoa, _ = _predict(x_e, a, b, t_rx_s, orbits, sites_xyz, want_fdoa=False)
    return tdoa


def predict_fdoa(
    x_e: Any,
    a: PathSpec,
    b: PathSpec,
    t_rx_s: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
) -> Any:
    """Predicted FDOA of path A minus path B, hertz, ``[..., M]`` (MATH.md section 5).

    Arguments as for ``predict_tdoa``.
    ``h_fdoa = f_bb,A(t_mid + h_tdoa) - f_bb,B(t_mid)`` with each channel tuned to its
    own nominal downlink ``f_e + T_nom``, so
    ``f_bb = -f_e D_dot - T_nom tau_d_dot`` for each path.
    """
    _, fdoa = _predict(x_e, a, b, t_rx_s, orbits, sites_xyz, want_fdoa=True)
    return fdoa


def predict_pair(
    x_e: Any,
    a: PathSpec,
    b: PathSpec,
    t_rx_s: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
) -> tuple[Any, Any]:
    """``(predict_tdoa, predict_fdoa)`` from one evaluation of the two paths."""
    return _predict(x_e, a, b, t_rx_s, orbits, sites_xyz, want_fdoa=True)


def self_peak(
    sat_id: str,
    translation_hz: float,
    f_e_hz: float,
    a: PathSpec,
    b: PathSpec,
    t_rx_s: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
) -> tuple[Any, Any]:
    """CAF location of one satellite heard by both channels: ``(tdoa_s, fdoa_hz)``.

    sat_id: the satellite whose downlink reaches both channel A (at ``a.site_id``,
    tuned to ``a.f_tune_hz``) and channel B (at ``b.site_id``, tuned to
    ``b.f_tune_hz``). translation_hz: nominal translation of the transponder relaying
    the signal on that satellite, Hz. f_e_hz: uplink carrier, Hz. t_rx_s: ``t_mid``,
    Unix s, scalar or ``[M]``.

    The uplink leg, the transponder delay and the satellite oscillator are common to
    both copies and cancel, so the location follows from the downlink geometry alone:

        tdoa = tau_d,a(t_mid + tdoa) - tau_d,b(t_mid)                    (solved to convergence)
        fdoa = (b.f_tune_hz - a.f_tune_hz) - (f_e_hz + translation_hz) (tau_d,a_dot(t_mid + tdoa) - tau_d,b_dot(t_mid))

    Not included: the uplink Doppler on the downlink scale factor,
    ``+ f_e tau_u_dot (tau_d,a_dot - tau_d,b_dot)``, which needs the emitter position
    (below 1e-6 Hz for geosynchronous satellites, up to a few tenths of a hertz in low
    orbit).
    """
    orbit = _lookup(orbits, sat_id, "sat_id", "orbits")
    g_a = _site(_lookup(sites_xyz, a.site_id, "a.site_id", "sites_xyz"), "sites_xyz[a.site_id]")
    g_b = _site(_lookup(sites_xyz, b.site_id, "b.site_id", "sites_xyz"), "sites_xyz[b.site_id]")
    f_down = _frequency(f_e_hz, "f_e_hz") + _frequency(translation_hz, "translation_hz")
    tune_offset = _frequency(b.f_tune_hz, "b.f_tune_hz") - _frequency(a.f_tune_hz, "a.f_tune_hz")
    t, extra, scalar = _time_arg(t_rx_s, "t_rx_s")
    stencil_t = (t[:, None] + _OFFSETS).reshape(-1)
    stencil_extra = np.repeat(extra, 5)
    q_a = _Quartic(_downlink(orbit, g_a, stencil_t, stencil_extra).reshape(t.size, 5))
    q_b = _Quartic(_downlink(orbit, g_b, stencil_t, stencil_extra).reshape(t.size, 5))
    tdoa = q_a.c0 - q_b.c0
    for _ in range(_PASSES):
        tdoa = q_a.value(tdoa / STENCIL_STEP_S) - q_b.c0
    rate_diff = q_a.rate(tdoa / STENCIL_STEP_S) - q_b.rate0()
    fdoa = tune_offset - f_down * rate_diff
    return _finish(tdoa, scalar), _finish(fdoa, scalar)


# --------------------------------------------------------------------------- visibility


def elevation_deg(x: Any, orbit: Orbit, t_s: Any) -> Any:
    """Elevation of the satellite seen from ground point(s) ``x``, degrees.

    x: ECEF m, ``[..., 3]``. t_s: Unix s, scalar or ``[M]``. Returns ``[..., M]``.
    Measured from the plane normal to the WGS84 local vertical at ``x``; geometric
    (no refraction, no light-time correction).
    """
    pts = _points(x, "x")
    t, _, scalar = _time_arg(t_s, "t_s")
    lat, lon, _ = ecef_to_lla(pts)
    el = _elevation(pts, _up_vectors(np.asarray(lat), np.asarray(lon)), orbit_position(orbit, t))
    return _finish(el, scalar)


def footprint(
    orbit: Orbit, t_s: float, min_elevation_deg: float = 0.0, n: int = 181
) -> list[tuple[float, float]]:
    """Boundary on the WGS84 ellipsoid of the area that sees the satellite.

    orbit, t_s: the satellite and the (scalar) time, Unix s. min_elevation_deg: the
    ring is where the satellite's elevation equals this value, in (-90, 90). n: number
    of vertices including the repeated closing vertex (at least 4).

    Returns ``[(lat_deg, lon_deg), ...]``: a closed ring (last vertex equal to the
    first) starting due north of the sub-satellite point and running west, south, east
    (counter-clockwise seen from above). Longitudes are continuous around the
    sub-satellite longitude: they never jump by more than 180 degrees between
    consecutive vertices and may leave ``[-180, 180)``. A footprint that contains a
    pole therefore ends 360 degrees away from where it starts.
    """
    t = np.asarray(t_s, dtype=np.float64)
    if t.ndim != 0 or not np.isfinite(t):
        raise ValueError("t_s must be a finite scalar")
    if isinstance(n, bool) or int(n) != n or n < 4:
        raise ValueError(f"n must be an integer of at least 4, got {n!r}")
    el_min = float(min_elevation_deg)
    if not -90.0 < el_min < 90.0:
        raise ValueError(f"min_elevation_deg must be within (-90, 90), got {min_elevation_deg!r}")
    r = orbit_position(orbit, float(t))
    lat_s, lon_s, alt_s = (float(v) for v in ecef_to_lla(r))
    if alt_s <= 0.0:
        raise ValueError("orbit: the satellite is not above the ellipsoid at t_s")

    bearing = np.linspace(0.0, 2.0 * np.pi, int(n))
    p, normal = _contour(r, el_min, 0.0, bearing, 60)
    lat = np.degrees(np.arctan2(normal[:, 2], np.hypot(normal[:, 0], normal[:, 1])))
    rel = np.mod(np.degrees(np.arctan2(p[:, 1], p[:, 0])) - lon_s + 180.0, 360.0) - 180.0
    lon = lon_s + np.unwrap(rel, period=360.0)
    lat[-1] = lat[0]
    if abs(lon[-1] - lon[0]) < 180.0:
        lon[-1] = lon[0]
    return [(float(la), float(lo_)) for la, lo_ in zip(lat, lon)]


# --------------------------------------------------------------------------- search windows


def measurement_windows(
    a: PathSpec,
    b: PathSpec,
    t_rx_s: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    region: Any,
    min_elevation_deg: float = 0.0,
    height_m: float = 0.0,
    step_deg: float = 2.0,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Extremes of the predicted TDOA and FDOA over the admissible emitter region.

    a, b: the two paths. t_rx_s: ``t_mid``, Unix s (scalar, or ``[M]`` to cover several
    epochs). region: an object with ``grid(step_deg) -> (lat_deg[n_lat], lon_deg[n_lon])``
    and ``contains(lat_deg, lon_deg) -> bool array`` (the configuration ``Region``).
    min_elevation_deg: both satellites must be at least this high above the emitter's
    horizon. height_m: emitter height above the ellipsoid. step_deg: largest grid step,
    degrees; it is reduced automatically to one eighth of the lowest satellite's height
    (as an angle at the Earth's centre), which leaves it alone above 1000 km for the
    default 2 degrees and makes it 0.45 degrees at 400 km.

    Returns ``((tdoa_min, tdoa_max), (fdoa_min, fdoa_max))`` in seconds and hertz, or
    ``None`` when no emitter position is admissible (inside the region and seeing both
    satellites): neither a grid point nor a point of either satellite's mask contour.
    ``None`` is never the answer to a bad argument: a non-finite ``min_elevation_deg``
    or ``height_m``, an elevation outside [-90, 90], a non-positive ``step_deg`` and a
    satellite that is not above the emitter height raise ValueError.

    The result bounds the predictions over the admissible area, not just at the grid
    points, because an emitter may be anywhere:

    - the extremes are taken over the admissible grid points and their eight grid
      neighbours, whether or not those neighbours see the satellites (the horizon
      crosses cells; sampled extremes alone fall short by up to a cell's variation,
      hundreds of microseconds for widely separated satellites at the default step);
    - when the region exposes ``lat_max`` and ``lon_span_deg()``, its northern and
      eastern edges are added if the step does not land on them;
    - the edge of the admissible area that is not a region edge is followed explicitly:
      each satellite's mask contour (at ``height_m``) is sampled at 720 points per
      epoch, the admissible ones are evaluated, and the ends of every admissible arc
      (where the contour meets the other satellite's contour or leaves the region) are
      located to a thousandth of that spacing. Extremes usually sit there, and for
      low satellites the co-visible area can be a lens with no grid point in it;
    - both ends are padded by the bilinear-interpolation error bound, one eighth of the
      largest second difference along latitude plus that along longitude (curvature
      inside a cell; microseconds at the default step).
    """
    step = float(step_deg)
    if not step > 0.0 or not np.isfinite(step):
        raise ValueError(f"step_deg must be positive, got {step_deg!r}")
    # NaN here would make every comparison below false and return None ("no admissible
    # point") for what is really a bad argument
    el_min = float(min_elevation_deg)
    if not -90.0 <= el_min <= 90.0:
        raise ValueError(f"min_elevation_deg must be within [-90, 90], got {min_elevation_deg!r}")
    height = float(height_m)
    if not np.isfinite(height):
        raise ValueError(f"height_m must be finite, got {height_m!r}")
    t, _, _ = _time_arg(t_rx_s, "t_rx_s")
    orbit_a = _lookup(orbits, a.sat_id, "a.sat_id", "orbits")
    orbit_b = _lookup(orbits, b.sat_id, "b.sat_id", "orbits")
    one_satellite = orbit_b is orbit_a
    sat_a = orbit_position(orbit_a, t)  # [M, 3]
    sat_b = sat_a if one_satellite else orbit_position(orbit_b, t)

    lowest = float(min(np.min(np.linalg.norm(sat_a, axis=-1)), np.min(np.linalg.norm(sat_b, axis=-1))))
    lowest -= WGS84_A + max(height, 0.0)
    if not lowest > 0.0:
        raise ValueError("orbits: a satellite of the pair is not above the emitter height at t_rx_s")
    step = min(step, _GRID_HEIGHT_FRACTION * float(np.degrees(lowest / WGS84_A)))

    lat, lon = (np.asarray(v, dtype=np.float64).reshape(-1) for v in region.grid(step))
    if lat.size * lon.size > _MAX_GRID_POINTS:
        raise ValueError(
            f"step_deg: a {step:.3g} degree grid (needed for a satellite {lowest / 1e3:.0f} km up) has "
            f"{lat.size * lon.size} points over this region; use a smaller region"
        )
    have_grid = lat.size > 0 and lon.size > 0
    lon_ref = float(lon[0]) if lon.size else 0.0

    def inside_region(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
        # Region.grid longitudes are unwrapped; accept a contains() written for either convention
        unwrapped = lon_ref + np.mod(lon_deg - lon_ref, 360.0)
        return np.asarray(region.contains(lat_deg, unwrapped), dtype=bool) | np.asarray(
            region.contains(lat_deg, wrap_lon_deg(lon_deg)), dtype=bool
        )

    # the edge of the admissible area, epoch by epoch: values and nothing else
    edge_tdoa: list[np.ndarray] = []
    edge_fdoa: list[np.ndarray] = []
    for m in range(t.size):
        rings = [_contour_candidates(sat_a[m], None if one_satellite else sat_b[m], el_min, height, inside_region)]
        if not one_satellite:
            rings.append(_contour_candidates(sat_b[m], sat_a[m], el_min, height, inside_region))
        candidates = np.concatenate(rings)
        if candidates.shape[0]:
            tdoa_m, fdoa_m = _predict(candidates, a, b, t[m : m + 1], orbits, sites_xyz, want_fdoa=True)
            edge_tdoa.append(tdoa_m.reshape(-1))
            edge_fdoa.append(fdoa_m.reshape(-1))
    edges = [np.concatenate(v) if v else np.empty(0) for v in (edge_tdoa, edge_fdoa)]

    admissible = np.zeros((0, 0, t.size), dtype=bool)
    if have_grid:
        n_lat, n_lon = lat.size, lon.size  # the uniform part of the grid
        lat_max = getattr(region, "lat_max", None)
        if lat_max is not None and float(lat_max) - lat[-1] > 1e-9 * step:
            lat = np.append(lat, float(lat_max))
        span_fn = getattr(region, "lon_span_deg", None)
        if callable(span_fn):
            span = float(span_fn())
            full_circle = span >= 360.0 - 1e-9
            if not full_circle and (lon[0] + span) - lon[-1] > 1e-9 * step:
                lon = np.append(lon, lon[0] + span)
        else:
            full_circle = bool((lon[-1] - lon[0]) + step >= 360.0 - 1e-9)

        lat2, lon2 = np.meshgrid(lat, lon, indexing="ij")
        up = _up_vectors(lat2, lon2)
        pts = lla_to_ecef(lat2, lon2, height)
        inside = np.asarray(region.contains(lat2, lon2), dtype=bool) | np.asarray(
            region.contains(lat2, wrap_lon_deg(lon2)), dtype=bool
        )
        visible = _elevation(pts, up, sat_a) >= el_min
        if not one_satellite:
            visible &= _elevation(pts, up, sat_b) >= el_min
        admissible = inside[..., None] & visible  # [n_lat, n_lon, M]

    if not np.any(admissible):
        if edges[0].size == 0:
            return None
        # a co-visible area that fits between the grid points: its edge is all there is
        return (
            (float(np.min(edges[0])), float(np.max(edges[0]))),
            (float(np.min(edges[1])), float(np.max(edges[1]))),
        )

    use = _dilate(admissible, wrap_lon=full_circle)
    needed = np.any(use, axis=-1)  # grid points in play at any epoch
    windows = []
    for predicted, edge in zip(_predict(pts[needed], a, b, t, orbits, sites_xyz, want_fdoa=True), edges):
        values = np.full(use.shape, np.nan)
        values[needed] = predicted
        pad = _cell_curvature(values[:n_lat, :n_lon], use[:n_lat, :n_lon], wrap_lon=full_circle)
        picked = values[use]
        low, high = float(np.min(picked)), float(np.max(picked))
        if edge.size:
            low, high = min(low, float(np.min(edge))), max(high, float(np.max(edge)))
        windows.append((low - pad, high + pad))
    return windows[0], windows[1]


def search_windows(
    geo_windows: tuple[tuple[float, float], tuple[float, float]],
    a: PathSpec,
    b: PathSpec,
    caf_cfg: Any,
    uncertainty: Uncertainty | None,
    correction: Sequence[float | None] | Mapping[str, float | None] | None = None,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """CAF delay and Doppler search windows from the geometric extremes (MATH.md section 6).

    geo_windows: ``((tdoa_min, tdoa_max), (fdoa_min, fdoa_max))`` from
    ``measurement_windows``, s and Hz. caf_cfg: object (or mapping) with
    ``delay_margin_s`` and ``doppler_margin_hz``. uncertainty: resolved one-sigma
    systematics (``None`` = all zero). correction: ``None``, or the corrections of
    this pair's calibrations that are still valid, as
    ``(tdoa_correction_s | None, fdoa_correction_hz | None)`` or a mapping with keys
    ``"tdoa"`` and/or ``"fdoa"``.

    Without a correction the window is widened on both sides by the fixed margin plus
    three sigma of the uncalibrated systematics:

        delay:   delay_margin_s   + 3 sqrt(clock_a^2 + clock_b^2 + tdoa_bias^2)
        doppler: doppler_margin_hz + 3 sqrt((sat_osc_i T_i)^2 + (sat_osc_j T_j)^2
                                            + (site_osc_a f_tune,a)^2 + (site_osc_b f_tune,b)^2)

    Terms that are common to both paths cancel as in the measurement itself: one site
    serving both channels contributes no clock term and only
    ``site_osc (f_tune,a - f_tune,b)``; one satellite on both paths contributes
    ``sat_osc (T_i - T_j)``. With a correction for a kind, that window is shifted by
    the correction and only the fixed margin applies.

    Returns ``((delay_min, delay_max), (doppler_min, doppler_max))``.
    """
    if geo_windows is None:
        raise ValueError("geo_windows is None: no admissible emitter position, nothing to search")
    try:
        (t_lo, t_hi), (f_lo, f_hi) = geo_windows
        t_lo, t_hi, f_lo, f_hi = float(t_lo), float(t_hi), float(f_lo), float(f_hi)
    except (TypeError, ValueError):
        raise ValueError("geo_windows must be ((tdoa_min, tdoa_max), (fdoa_min, fdoa_max))") from None
    if not (np.isfinite([t_lo, t_hi, f_lo, f_hi]).all() and t_lo <= t_hi and f_lo <= f_hi):
        raise ValueError("geo_windows must be ordered (min, max) and finite")
    delay_margin = _nonnegative(_cfg(caf_cfg, "delay_margin_s", "caf_cfg"), "caf_cfg.delay_margin_s")
    doppler_margin = _nonnegative(
        _cfg(caf_cfg, "doppler_margin_hz", "caf_cfg"), "caf_cfg.doppler_margin_hz"
    )
    tdoa_corr, fdoa_corr = _corrections(correction)
    unc = uncertainty if uncertainty is not None else Uncertainty()

    if tdoa_corr is None:
        if a.site_id == b.site_id:
            clock_var = 0.0
        else:
            clock_var = (
                _sigma(unc.site_clock_s, a.site_id, "site_clock_s") ** 2
                + _sigma(unc.site_clock_s, b.site_id, "site_clock_s") ** 2
            )
        bias = _nonnegative(unc.tdoa_bias_s, "uncertainty.tdoa_bias_s")
        delay_pad = delay_margin + 3.0 * float(np.sqrt(clock_var + bias**2))
        delay_shift = 0.0
    else:
        delay_pad, delay_shift = delay_margin, tdoa_corr

    if fdoa_corr is None:
        t_a = _frequency(a.translation_hz, "a.translation_hz")
        t_b = _frequency(b.translation_hz, "b.translation_hz")
        tune_a = t_a + _frequency(a.f_e_hz, "a.f_e_hz")
        tune_b = t_b + _frequency(b.f_e_hz, "b.f_e_hz")
        if a.sat_id == b.sat_id:
            sat_var = (_sigma(unc.sat_osc_frac, a.sat_id, "sat_osc_frac") * (t_a - t_b)) ** 2
        else:
            sat_var = (_sigma(unc.sat_osc_frac, a.sat_id, "sat_osc_frac") * t_a) ** 2 + (
                _sigma(unc.sat_osc_frac, b.sat_id, "sat_osc_frac") * t_b
            ) ** 2
        if a.site_id == b.site_id:
            site_var = (_sigma(unc.site_osc_frac, a.site_id, "site_osc_frac") * (tune_a - tune_b)) ** 2
        else:
            site_var = (_sigma(unc.site_osc_frac, a.site_id, "site_osc_frac") * tune_a) ** 2 + (
                _sigma(unc.site_osc_frac, b.site_id, "site_osc_frac") * tune_b
            ) ** 2
        doppler_pad = doppler_margin + 3.0 * float(np.sqrt(sat_var + site_var))
        doppler_shift = 0.0
    else:
        doppler_pad, doppler_shift = doppler_margin, fdoa_corr

    return (
        (t_lo + delay_shift - delay_pad, t_hi + delay_shift + delay_pad),
        (f_lo + doppler_shift - doppler_pad, f_hi + doppler_shift + doppler_pad),
    )


# --------------------------------------------------------------------------- internals


class _Quartic:
    """The quartic through five samples at ``t + k h``, ``k = -2..2``, in Taylor form.

    ``p(u) = c0 + c1 u + c2 u^2 + c3 u^3 + c4 u^4`` with ``u = (time - t) / h``.
    ``c1 / h`` is the 5-point stencil derivative of MATH.md section 3.
    """

    __slots__ = ("c0", "c1", "c2", "c3", "c4")

    def __init__(self, samples: np.ndarray) -> None:
        f_m2, f_m1, f_0, f_p1, f_p2 = (samples[..., k] for k in range(5))
        odd1 = f_p1 - f_m1
        odd2 = f_p2 - f_m2
        even1 = (f_p1 - f_0) - (f_0 - f_m1)
        even2 = (f_p2 - f_0) - (f_0 - f_m2)
        self.c0 = f_0
        self.c1 = (8.0 * odd1 - odd2) / 12.0
        self.c2 = (16.0 * even1 - even2) / 24.0
        self.c3 = (odd2 - 2.0 * odd1) / 12.0
        self.c4 = (even2 - 4.0 * even1) / 24.0

    def value(self, u: Any) -> np.ndarray:
        """The function ``u`` steps of ``h`` after the centre."""
        return self.c0 + u * (self.c1 + u * (self.c2 + u * (self.c3 + u * self.c4)))

    def rate(self, u: Any) -> np.ndarray:
        """Time derivative ``u`` steps of ``h`` after the centre, per second."""
        return (
            self.c1 + u * (2.0 * self.c2 + u * (3.0 * self.c3 + u * (4.0 * self.c4)))
        ) / STENCIL_STEP_S

    def rate0(self) -> np.ndarray:
        """Time derivative at the centre: the 5-point stencil."""
        return self.c1 / STENCIL_STEP_S


def _light_time(
    dx: np.ndarray, dy: np.ndarray, dz: np.ndarray, dot_xy: np.ndarray, cross_z: np.ndarray, tau: Any
) -> np.ndarray:
    """One fixed-point pass of ``C tau = | rx - Rz(-OMEGA_E tau) tx |``.

    dx, dy, dz: ``rx - tx`` (unrotated). dot_xy = rx_x tx_x + rx_y tx_y and
    cross_z = rx_x tx_y - rx_y tx_x. Uses
    ``|rx - Rz(-phi) tx|^2 = |rx - tx|^2 + 4 dot_xy sin^2(phi / 2) - 2 cross_z sin(phi)``,
    which keeps the microsecond-level rotation term free of cancellation.
    """
    phi = OMEGA_E * tau
    half = np.sin(0.5 * phi)
    rho_sq = (dx * dx + dy * dy + dz * dz) + 4.0 * dot_xy * half * half - 2.0 * cross_z * np.sin(phi)
    return np.sqrt(rho_sq) / C


def _uplink(x: np.ndarray, r_sat: np.ndarray) -> np.ndarray:
    """Uplink delay for emitters ``x`` ``[..., 3]`` at satellite positions ``r_sat`` ``[K, 3]``: ``[..., K]``.

    Large grids are worked through a few satellite positions at a time so the
    temporaries stay in cache (twice as fast for a 141 x 360 grid as one pass over all
    five stencil positions); the arithmetic per element is the same either way.
    """
    count = r_sat.shape[0]
    block = max(1, _BLOCK_ELEMENTS // max(x.size // 3, 1))
    if count <= block:
        return _uplink_block(x, r_sat)
    out = np.empty(x.shape[:-1] + (count,))
    for start in range(0, count, block):
        out[..., start : start + block] = _uplink_block(x, r_sat[start : start + block])
    return out


def _uplink_block(x: np.ndarray, r_sat: np.ndarray) -> np.ndarray:
    """Uplink delay for emitters ``x`` ``[..., 3]`` and satellite positions ``r_sat`` ``[M, 3]``: ``[..., M]``.

    Solves ``C tau = | r - Rz(-OMEGA_E tau) x |`` with
    ``|r - Rz(-phi) x|^2 = |r - x|^2 + 4 (r_x x + r_y y) sin^2(phi / 2) - 2 (r_x y - r_y x) sin(phi)``.
    This is the hot loop of every grid evaluation, so it works in place and starts the
    fixed-point iteration from the geometric range plus the first-order rotation term
    ``-OMEGA_E (r_x y - r_y x) / C^2`` instead of the bare range: the start is then
    within a picosecond and one pass (contraction 1.5e-6 for a point rotating with the
    Earth) lands below 1e-18 s, where three passes from the bare range also end.
    """
    ex, ey, ez = x[..., 0:1], x[..., 1:2], x[..., 2:3]
    rx, ry, rz_ = r_sat[..., 0], r_sat[..., 1], r_sat[..., 2]
    d = rx - ex
    rho0_sq = d * d
    d = ry - ey
    rho0_sq += d * d
    d = rz_ - ez
    rho0_sq += d * d
    dot4 = rx * ex
    dot4 += ry * ey
    dot4 *= 4.0
    cross2 = rx * ey
    cross2 -= ry * ex
    cross2 *= 2.0
    tau = np.sqrt(rho0_sq)
    tau -= (0.5 * OMEGA_E / C) * cross2
    tau /= C
    for _ in range(_UPLINK_PASSES):
        tau *= OMEGA_E  # phi
        sin_phi = np.sin(tau)
        sin_phi *= cross2
        tau *= 0.5
        np.sin(tau, out=tau)
        tau *= tau
        tau *= dot4
        tau -= sin_phi
        tau += rho0_sq
        np.sqrt(tau, out=tau)
        tau /= C
    return tau


def _downlink(orbit: Orbit, g: np.ndarray, t: np.ndarray, extra: np.ndarray) -> np.ndarray:
    """Downlink delay to site(s) ``g`` (``[3]`` or ``[..., 1, 3]``) for reception at ``t + extra`` (``[M]`` each)."""
    gx, gy, gz = g[..., 0], g[..., 1], g[..., 2]
    r = orbit_position(orbit, t, extra)
    tau = np.sqrt((gx - r[..., 0]) ** 2 + (gy - r[..., 1]) ** 2 + (gz - r[..., 2]) ** 2) / C
    for _ in range(_PASSES):
        # the satellite at t - tau, with the two terms kept apart: rounding t - tau to the
        # float64 grid (2.4e-7 s) would move a low-orbit satellite by millimetres
        r = orbit_position(orbit, t, extra - tau)
        rx, ry, rz_ = r[..., 0], r[..., 1], r[..., 2]
        tau = _light_time(gx - rx, gy - ry, gz - rz_, gx * rx + gy * ry, gx * ry - gy * rx, tau)
    return tau


def _bent_pipe(
    x: np.ndarray, orbit: Orbit, g: np.ndarray, t: np.ndarray, extra: np.ndarray, tau_x: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(tau_d [M], t_sat [M], tau_u [..., M])`` for reception at ``t + extra`` (``[M]`` each)."""
    tau_d = _downlink(orbit, g, t, extra)
    back = tau_d + tau_x
    tau_u = _uplink(x, orbit_position(orbit, t, extra - back))
    return tau_d, t + (extra - back), tau_u


def _path_quartics(
    x: np.ndarray, orbit: Orbit, g: np.ndarray, t: np.ndarray, extra: np.ndarray, tau_x: float
) -> tuple[_Quartic, _Quartic]:
    """Quartics in reception time of ``D`` (``[..., M]``) and ``tau_d`` (``[M]``) around ``t + extra``."""
    stencil_t = (t[:, None] + _OFFSETS).reshape(-1)  # [M * 5], exact: the offsets are multiples of 0.5 s
    tau_d, _, tau_u = _bent_pipe(x, orbit, g, stencil_t, np.repeat(extra, 5), tau_x)
    tau_d = tau_d.reshape(t.size, 5)
    total = (tau_d + tau_x) + tau_u.reshape(tau_u.shape[:-1] + (t.size, 5))
    return _Quartic(total), _Quartic(tau_d)


def _predict(
    x_e: Any,
    a: PathSpec,
    b: PathSpec,
    t_rx_s: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    want_fdoa: bool,
) -> tuple[Any, Any]:
    """Shared evaluation of the section 5 predictors: ``(tdoa, fdoa or None)``."""
    x = _points(x_e, "x_e")
    t, extra, scalar = _time_arg(t_rx_s, "t_rx_s")
    orbit_a = _lookup(orbits, a.sat_id, "a.sat_id", "orbits")
    orbit_b = _lookup(orbits, b.sat_id, "b.sat_id", "orbits")
    g_a = _site(_lookup(sites_xyz, a.site_id, "a.site_id", "sites_xyz"), "sites_xyz[a.site_id]")
    g_b = _site(_lookup(sites_xyz, b.site_id, "b.site_id", "sites_xyz"), "sites_xyz[b.site_id]")
    tau_x_a = _delay(a.tau_x_s, "a.tau_x_s")
    tau_x_b = _delay(b.tau_x_s, "b.tau_x_s")
    if want_fdoa:
        f_e_a, f_e_b = _frequency(a.f_e_hz, "a.f_e_hz"), _frequency(b.f_e_hz, "b.f_e_hz")
        t_a, t_b = _frequency(a.translation_hz, "a.translation_hz"), _frequency(b.translation_hz, "b.translation_hz")

    d_a, tau_d_a = _path_quartics(x, orbit_a, g_a, t, extra, tau_x_a)
    if want_fdoa:
        d_b, tau_d_b = _path_quartics(x, orbit_b, g_b, t, extra, tau_x_b)
        d_b0 = d_b.c0
    else:
        tau_d, _, tau_u = _bent_pipe(x, orbit_b, g_b, t, extra, tau_x_b)
        d_b0 = (tau_d + tau_x_b) + tau_u

    first = d_a.c0 - d_b0
    if first.size and float(np.max(np.abs(first))) > _MAX_SHIFT_S:
        raise ValueError(
            "x_e: the two path delays differ by more than 1 s; outside the model of MATH.md section 5"
        )
    tdoa = d_a.value(first / STENCIL_STEP_S) - d_b0
    if not want_fdoa:
        return _finish(tdoa, scalar), None
    u = tdoa / STENCIL_STEP_S
    f_a = -f_e_a * d_a.rate(u) - t_a * tau_d_a.rate(u)
    f_b = -f_e_b * d_b.rate0() - t_b * tau_d_b.rate0()
    return _finish(tdoa, scalar), _finish(f_a - f_b, scalar)


def _up_vectors(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
    """Unit WGS84 local-vertical vectors (ECEF) at geodetic positions, ``[..., 3]``."""
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    cl = np.cos(lat)
    return np.stack([cl * np.cos(lon), cl * np.sin(lon), np.sin(lat) + 0.0 * lon], axis=-1)


def _elevation(pts: np.ndarray, up: np.ndarray, r_sat: np.ndarray) -> np.ndarray:
    """Elevation (deg) of ``r_sat`` (``[M, 3]`` or ``[3]``) from ``pts`` with verticals ``up`` (``[..., 3]``)."""
    if r_sat.ndim == 1:
        d = r_sat - pts
        upv = up
    else:
        d = r_sat - pts[..., None, :]
        upv = up[..., None, :]
    vertical = np.sum(d * upv, axis=-1)
    horizontal = np.linalg.norm(d - vertical[..., None] * upv, axis=-1)
    return np.degrees(np.arctan2(vertical, horizontal))


def _contour(
    r_sat: np.ndarray, el_min: float, height: float, bearing: np.ndarray, iterations: int
) -> tuple[np.ndarray, np.ndarray]:
    """Where the satellite at ``r_sat`` (ECEF m, ``[3]``) is at elevation ``el_min`` degrees.

    One point per ``bearing`` (rad, measured at the sub-satellite point from north
    towards west), found by bisection on the geocentric angle from the sub-satellite
    direction. Returns ``(p, normal)``, both ``[n, 3]``: the point of the WGS84
    ellipsoid below each contour point and the unit ellipsoid normal there; the
    contour point itself is ``p + height * normal`` (``height`` in metres).
    """
    lat_s, lon_s, _ = (float(v) for v in ecef_to_lla(r_sat))
    centre = lla_to_ecef(lat_s, lon_s, 0.0)
    s_hat = centre / np.linalg.norm(centre)
    east = np.cross([0.0, 0.0, 1.0], s_hat)
    if np.linalg.norm(east) < 1e-9:  # sub-satellite point on the rotation axis
        east = np.array([0.0, 1.0, 0.0])
    east = east / np.linalg.norm(east)
    north = np.cross(s_hat, east)
    tangent = np.cos(bearing)[:, None] * north - np.sin(bearing)[:, None] * east

    def surface(psi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Ellipsoid point along each bearing at geocentric angle psi, and its outward normal."""
        d = np.cos(psi)[:, None] * s_hat + np.sin(psi)[:, None] * tangent
        scale = 1.0 / np.sqrt((d[:, 0] ** 2 + d[:, 1] ** 2) / WGS84_A**2 + d[:, 2] ** 2 / _B**2)
        p = d * scale[:, None]
        normal = p / np.array([WGS84_A**2, WGS84_A**2, _B**2])
        return p, normal / np.linalg.norm(normal, axis=1, keepdims=True)

    # elevation falls monotonically from 90 deg at psi = 0 to -90 deg at psi = pi
    lo = np.zeros(bearing.shape)
    hi = np.full(bearing.shape, np.pi)
    for _ in range(iterations):
        mid = 0.5 * (lo + hi)
        p, normal = surface(mid)
        above = _elevation(p + height * normal, normal, r_sat) > el_min
        lo = np.where(above, mid, lo)
        hi = np.where(above, hi, mid)
    return surface(0.5 * (lo + hi))


def _contour_candidates(
    r_own: np.ndarray, r_other: np.ndarray | None, el_min: float, height: float, inside_region: Any
) -> np.ndarray:
    """Points of one satellite's mask contour that bound the admissible area: ``[K, 3]`` ECEF m.

    r_own: the satellite whose ``el_min`` contour (at ``height`` above the ellipsoid)
    is followed. r_other: the other satellite of the pair, which must be at least
    ``el_min`` high as well (``None`` when both paths use one satellite).
    inside_region(lat_deg, lon_deg) -> bool array.

    Returns the admissible contour samples and, for every admissible arc, the two points
    that bracket each of its ends (found by subdividing the chord between the last
    admissible sample and the first inadmissible one; the chord leaves the contour by
    tens of metres at most).
    """
    bearing = np.linspace(0.0, 2.0 * np.pi, _CONTOUR_POINTS, endpoint=False)
    p, normal = _contour(r_own, el_min, height, bearing, _CONTOUR_BISECTIONS)
    pts = p + height * normal
    lat = np.degrees(np.arctan2(normal[:, 2], np.hypot(normal[:, 0], normal[:, 1])))
    lon = np.degrees(np.arctan2(p[:, 1], p[:, 0]))

    def admissible(q: np.ndarray, lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
        ok = inside_region(lat_deg, lon_deg)
        if r_other is not None:
            ok = ok & (_elevation(q, _up_vectors(lat_deg, lon_deg), r_other) >= el_min)
        return ok

    ok = admissible(pts, lat, lon)
    found = [pts[ok]]
    ends = np.flatnonzero(ok != np.roll(ok, -1))
    if ends.size:
        p0, p1 = pts[ends], pts[(ends + 1) % _CONTOUR_POINTS]
        rows = np.arange(ends.size)
        frac = np.linspace(0.0, 1.0, _ARC_END_SUBDIVISIONS)[None, :, None]
        for _ in range(2):
            chord = p0[:, None, :] + frac * (p1 - p0)[:, None, :]
            c_lat, c_lon, _ = ecef_to_lla(chord)
            chord = lla_to_ecef(c_lat, c_lon, height)
            state = admissible(chord, np.asarray(c_lat), np.asarray(c_lon))
            k = np.argmax(state != state[:, :1], axis=1)  # first sample on the other side
            k = np.where(k == 0, _ARC_END_SUBDIVISIONS - 1, k)
            p0, p1 = chord[rows, k - 1], chord[rows, k]
        found += [p0, p1]
    return np.concatenate(found)


def _dilate(mask: np.ndarray, wrap_lon: bool) -> np.ndarray:
    """Grow a ``[n_lat, n_lon, ...]`` boolean mask to the 8 grid neighbours of each true cell."""
    out = mask.copy()
    out[1:] |= mask[:-1]
    out[:-1] |= mask[1:]
    rows = out.copy()
    out[:, 1:] |= rows[:, :-1]
    out[:, :-1] |= rows[:, 1:]
    if wrap_lon and mask.shape[1] > 1:
        out[:, 0] |= rows[:, -1]
        out[:, -1] |= rows[:, 0]
    return out


def _cell_curvature(values: np.ndarray, use: np.ndarray, wrap_lon: bool) -> float:
    """Bound on how far a smooth field can stray outside its corner values within a grid cell.

    values, use: ``[n_lat, n_lon, ...]`` samples on a uniform grid and the mask of those
    in play (entries outside the mask may be NaN and are never read). Bilinear
    interpolation stays between the corner values and is in error by at most
    ``(|f_lat_lat| d_lat^2 + |f_lon_lon| d_lon^2) / 8``; the second differences of the
    samples estimate those two products.
    """
    pad = 0.0
    if values.shape[0] >= 3:
        d2 = values[2:] - 2.0 * values[1:-1] + values[:-2]
        ok = use[2:] & use[1:-1] & use[:-2]
        if np.any(ok):
            pad += float(np.max(np.abs(d2[ok])))
    if values.shape[1] >= 3:
        if wrap_lon:
            values = np.concatenate([values[:, -1:], values, values[:, :1]], axis=1)
            use = np.concatenate([use[:, -1:], use, use[:, :1]], axis=1)
        d2 = values[:, 2:] - 2.0 * values[:, 1:-1] + values[:, :-2]
        ok = use[:, 2:] & use[:, 1:-1] & use[:, :-2]
        if np.any(ok):
            pad += float(np.max(np.abs(d2[ok])))
    return pad / 8.0


def _points(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 0 or arr.shape[-1] != 3:
        raise ValueError(f"{name} must have shape [..., 3], got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite")
    return arr


def _site(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.shape != (3,) or not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be 3 finite ECEF coordinates, got shape {arr.shape}")
    return arr


def _time_arg(value: Any, name: str, offset: Any = 0.0) -> tuple[np.ndarray, np.ndarray, bool]:
    """Time argument and its small offset as 1-D float64 arrays ``[M]``, plus whether both were scalars."""
    arr = np.asarray(value, dtype=np.float64)
    extra = np.asarray(offset, dtype=np.float64)
    if arr.ndim > 1:
        raise ValueError(f"{name} must be a scalar or a 1-D array, got shape {arr.shape}")
    if extra.ndim > 1:
        raise ValueError(f"dt_s must be a scalar or a 1-D array, got shape {extra.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite")
    if not np.all(np.isfinite(extra)):
        raise ValueError("dt_s must be finite")
    if arr.size and float(np.max(np.abs(arr))) > MAX_UNIX_S:
        raise ValueError(
            f"{name} must be Unix seconds, got a value of magnitude {float(np.max(np.abs(arr))):.3g} "
            "(integer nanoseconds t_ns go through timebase.sec first)"
        )
    scalar = arr.ndim == 0 and extra.ndim == 0
    try:
        arr, extra = np.broadcast_arrays(arr.reshape(-1), extra.reshape(-1))
    except ValueError:
        raise ValueError(f"dt_s (shape {extra.shape}) does not broadcast against {name} (shape {arr.shape})") from None
    return arr, extra, scalar


def _finish(value: np.ndarray, scalar: bool) -> Any:
    """Drop the time axis for a scalar time argument; 0-d results become numpy scalars."""
    out = value[..., 0] if scalar else value
    return out[()] if out.ndim == 0 else out


def _delay(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of seconds, got {value!r}") from None
    if not np.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def _frequency(value: Any, name: str) -> float:
    """A finite frequency in hertz as a float (ValueError naming the field otherwise)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of hertz, got {value!r}") from None
    if not np.isfinite(out):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def _lookup(table: Mapping[str, Any], key: str, key_name: str, table_name: str) -> Any:
    try:
        return table[key]
    except (KeyError, TypeError):
        raise ValueError(f"{key_name}={key!r} is not in {table_name}") from None


def _cfg(cfg: Any, name: str, label: str) -> Any:
    if isinstance(cfg, Mapping):
        if name in cfg:
            return cfg[name]
    elif hasattr(cfg, name):
        return getattr(cfg, name)
    raise ValueError(f"{label}.{name} is required")


def _nonnegative(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not np.isfinite(out) or out < 0.0:
        raise ValueError(f"{name} must be finite and non-negative, got {value!r}")
    return out


def _sigma(table: Mapping[str, Any], key: str, name: str) -> float:
    """One-sigma entry ``uncertainty.<name>[key]`` (missing = 0); must be a finite number."""
    try:
        out = float(table.get(key, 0.0))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"uncertainty.{name}[{key!r}] must be a number") from None
    if not np.isfinite(out):
        raise ValueError(f"uncertainty.{name}[{key!r}] must be finite")
    return out


def _corrections(correction: Any) -> tuple[float | None, float | None]:
    if correction is None:
        return None, None
    if isinstance(correction, Mapping):
        unknown = set(correction) - {"tdoa", "fdoa"}
        if unknown:
            raise ValueError(f"correction has unknown keys {sorted(map(str, unknown))}; use 'tdoa' and 'fdoa'")
        tdoa_corr, fdoa_corr = correction.get("tdoa"), correction.get("fdoa")
    else:
        try:
            tdoa_corr, fdoa_corr = correction
        except (TypeError, ValueError):
            raise ValueError(
                "correction must be None, (tdoa_correction_s | None, fdoa_correction_hz | None) "
                "or a mapping with keys 'tdoa' / 'fdoa'"
            ) from None
    out: list[float | None] = []
    for value, name in ((tdoa_corr, "correction[0]"), (fdoa_corr, "correction[1]")):
        if value is None:
            out.append(None)
            continue
        number = float(value)
        if not np.isfinite(number):
            raise ValueError(f"{name} must be finite")
        out.append(number)
    return out[0], out[1]
