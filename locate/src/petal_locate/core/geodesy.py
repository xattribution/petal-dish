"""WGS84 geodesy: coordinate conversions, local frames, look angles and geodesics.

Conventions (MATH.md section 1): angles in degrees at the interface, metres out,
positions in ECEF (WGS84). Latitude is geodetic. Point longitudes returned by this
module are normalised to ``[-180, 180)``. Every function broadcasts over leading axes.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .constants import WGS84_A, WGS84_E2, WGS84_F

__all__ = [
    "lla_to_ecef",
    "ecef_to_lla",
    "enu_basis",
    "look_angles",
    "azel_to_ecef",
    "angle_between_deg",
    "rz",
    "geodesic_distance_m",
    "destination",
    "radii_of_curvature",
    "wrap_lon_deg",
]

_B: float = WGS84_A * (1.0 - WGS84_F)
"""WGS84 semi-minor axis, m."""

_EP2: float = WGS84_E2 / (1.0 - WGS84_E2)
"""Second eccentricity squared."""

_BOWRING_ITERATIONS = 4
_VINCENTY_ITERATIONS = 40
_VINCENTY_TOL = 1e-13
_BISECTIONS = 64


def wrap_lon_deg(lon_deg: Any) -> Any:
    """Normalise longitude(s) in degrees to ``[-180, 180)``."""
    lon = np.asarray(lon_deg, dtype=np.float64)
    out = np.mod(lon + 180.0, 360.0) - 180.0
    out = np.where(out >= 180.0, -180.0, out)
    return _unwrap0(out)


def lla_to_ecef(lat_deg: Any, lon_deg: Any, alt_m: Any = 0.0) -> np.ndarray:
    """Geodetic latitude/longitude (degrees) and ellipsoidal height (m) to ECEF metres.

    Inputs broadcast; returns ``ndarray[..., 3]``. Raises ValueError naming the argument
    for a latitude outside [-90, 90] or any non-finite input.
    """
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    alt = np.asarray(alt_m, dtype=np.float64)
    # written as "not all(<=)" so that NaN is rejected too instead of flowing into the result
    if not np.all(np.abs(lat) <= np.pi / 2 + 1e-12):
        raise ValueError("lat_deg must be finite and within [-90, 90]")
    if not np.all(np.isfinite(lon)):
        raise ValueError("lon_deg must be finite")
    if not np.all(np.isfinite(alt)):
        raise ValueError("alt_m must be finite")
    sin_lat, cos_lat = np.sin(lat), np.cos(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    p = (n + alt) * cos_lat
    x = p * np.cos(lon)
    y = p * np.sin(lon)
    z = (n * (1.0 - WGS84_E2) + alt) * sin_lat
    return np.stack(np.broadcast_arrays(x, y, z), axis=-1)


def ecef_to_lla(xyz: Any) -> tuple[Any, Any, Any]:
    """ECEF metres ``[..., 3]`` to ``(lat_deg, lon_deg, alt_m)``.

    Geodetic latitude in degrees, longitude in ``[-180, 180)`` degrees, height above
    the WGS84 ellipsoid in metres. Bowring's iteration on the reduced latitude (four
    fixed passes, no trigonometry inside the loop): round-trip error below 1e-8 m from
    10 km below the surface to beyond geosynchronous height, poles included. Not
    meaningful within about 50 km of the Earth's centre.
    """
    r = _vec3(xyz, "xyz")
    x, y, z = r[..., 0], r[..., 1], r[..., 2]
    p = np.hypot(x, y)
    # reduced latitude beta as a normalised (sin, cos) pair
    sb, cb = _normalise2(WGS84_A * z, _B * p)
    num, den = z, p
    for _ in range(_BOWRING_ITERATIONS):
        num = z + _EP2 * _B * sb * sb * sb
        den = p - WGS84_E2 * WGS84_A * cb * cb * cb
        sb, cb = _normalise2((1.0 - WGS84_F) * num, den)
    lat = np.arctan2(num, den)
    sin_lat, cos_lat = np.sin(lat), np.cos(lat)
    alt = p * cos_lat + z * sin_lat - WGS84_A * np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    lon = wrap_lon_deg(np.degrees(np.arctan2(y, x)))
    return _unwrap0(np.degrees(lat)), lon, _unwrap0(alt)


def enu_basis(lat_deg: Any, lon_deg: Any) -> np.ndarray:
    """Local east/north/up unit vectors at a geodetic position, in ECEF.

    Returns ``ndarray[..., 3, 3]`` whose rows are east, north and up (the WGS84
    ellipsoid normal). ``enu = basis @ (target_xyz - site_xyz)``.
    """
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    lat, lon = np.broadcast_arrays(lat, lon)
    sl, cl = np.sin(lat), np.cos(lat)
    so, co = np.sin(lon), np.cos(lon)
    zero = np.zeros_like(sl)
    east = np.stack([-so, co, zero], axis=-1)
    north = np.stack([-sl * co, -sl * so, cl], axis=-1)
    up = np.stack([cl * co, cl * so, sl], axis=-1)
    return np.stack([east, north, up], axis=-2)


def look_angles(site_xyz: Any, target_xyz: Any) -> tuple[Any, Any, Any]:
    """Azimuth, elevation and range of a target seen from a site.

    site_xyz, target_xyz: ECEF metres ``[..., 3]`` (broadcast against each other).
    Returns ``(az_deg, el_deg, range_m)``: azimuth clockwise from true north in
    ``[0, 360)``, elevation above the local horizontal plane of the WGS84 vertical at
    the site (no refraction), straight-line range in metres.
    """
    site = _vec3(site_xyz, "site_xyz")
    target = _vec3(target_xyz, "target_xyz")
    lat, lon, _ = ecef_to_lla(site)
    basis = enu_basis(lat, lon)
    d = target - site
    e = np.sum(basis[..., 0, :] * d, axis=-1)
    n = np.sum(basis[..., 1, :] * d, axis=-1)
    u = np.sum(basis[..., 2, :] * d, axis=-1)
    rng = np.sqrt(e * e + n * n + u * u)
    el = np.degrees(np.arctan2(u, np.hypot(e, n)))
    az = np.mod(np.degrees(np.arctan2(e, n)), 360.0)
    az = np.where(az >= 360.0, 0.0, az)
    return _unwrap0(az), _unwrap0(el), _unwrap0(rng)


def azel_to_ecef(lat_deg: Any, lon_deg: Any, az_deg: Any, el_deg: Any) -> np.ndarray:
    """Unit ECEF vector of a pointing direction given in local azimuth/elevation.

    lat_deg, lon_deg: geodetic position of the observer, degrees. az_deg: clockwise
    from true north. el_deg: above the local horizontal. Returns ``ndarray[..., 3]``.
    """
    az = np.radians(np.asarray(az_deg, dtype=np.float64))
    el = np.radians(np.asarray(el_deg, dtype=np.float64))
    basis = enu_basis(lat_deg, lon_deg)
    ce = np.cos(el)
    e, n, u = np.sin(az) * ce, np.cos(az) * ce, np.sin(el)
    out = (
        e[..., None] * basis[..., 0, :]
        + n[..., None] * basis[..., 1, :]
        + u[..., None] * basis[..., 2, :]
    )
    return out / np.linalg.norm(out, axis=-1, keepdims=True)


def angle_between_deg(u: Any, v: Any) -> Any:
    """Angle between vectors ``u`` and ``v`` (``[..., 3]``, any length), degrees in [0, 180].

    Uses ``atan2(|u x v|, u . v)``, accurate for nearly parallel vectors.
    """
    a = _vec3(u, "u")
    b = _vec3(v, "v")
    cross = np.cross(a, b)
    ang = np.arctan2(np.linalg.norm(cross, axis=-1), np.sum(a * b, axis=-1))
    return _unwrap0(np.degrees(ang))


def rz(angle_rad: Any) -> np.ndarray:
    """Active rotation by ``angle_rad`` counter-clockwise about +z: ``ndarray[..., 3, 3]``.

    ``rz(a) @ v`` rotates ``v`` (MATH.md section 2).
    """
    a = np.asarray(angle_rad, dtype=np.float64)
    c, s = np.cos(a), np.sin(a)
    zero, one = np.zeros_like(c), np.ones_like(c)
    return np.stack(
        [
            np.stack([c, -s, zero], axis=-1),
            np.stack([s, c, zero], axis=-1),
            np.stack([zero, zero, one], axis=-1),
        ],
        axis=-2,
    )


def radii_of_curvature(lat_deg: Any) -> tuple[Any, Any]:
    """WGS84 radii of curvature at geodetic latitude ``lat_deg``: ``(M, N)`` in metres.

    M is the meridional radius, N the prime-vertical radius. A displacement of
    ``dlat``/``dlon`` radians at height ``h`` moves ``(M + h) dlat`` north and
    ``(N + h) cos(lat) dlon`` east.
    """
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    w2 = 1.0 - WGS84_E2 * np.sin(lat) ** 2
    n = WGS84_A / np.sqrt(w2)
    m = WGS84_A * (1.0 - WGS84_E2) / (w2 * np.sqrt(w2))
    return _unwrap0(m), _unwrap0(n)


def geodesic_distance_m(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> Any:
    """Length of the shortest geodesic on the WGS84 ellipsoid between two points, metres.

    Arguments are geodetic degrees and broadcast. Vincenty's inverse iteration is used
    where it converges (accurate to about 0.1 mm); the nearly antipodal pairs on which
    it does not are solved by bisection on the departure azimuth with the same series,
    so every pair of valid points gets an answer. Latitudes outside [-90, 90] and
    non-finite latitudes or longitudes raise ValueError naming the argument.
    """
    p1, l1, p2, l2 = np.broadcast_arrays(
        _lat_rad(lat1, "lat1"),
        np.radians(_finite_array(lon1, "lon1")),
        _lat_rad(lat2, "lat2"),
        np.radians(_finite_array(lon2, "lon2")),
    )
    shape = p1.shape
    p1, l1, p2, l2 = (np.ravel(v) for v in (p1, l1, p2, l2))
    # longitude difference in [0, pi]: the distance is symmetric in its sign
    lam = np.abs(np.mod(l2 - l1 + np.pi, 2.0 * np.pi) - np.pi)
    u1 = np.arctan((1.0 - WGS84_F) * np.tan(p1))
    u2 = np.arctan((1.0 - WGS84_F) * np.tan(p2))
    dist, converged = _vincenty_inverse(u1, u2, lam)
    if not np.all(converged):
        bad = ~converged
        dist[bad] = _inverse_by_bisection(u1[bad], u2[bad], lam[bad])
    out = dist.reshape(shape)
    return _unwrap0(out)


def destination(lat: Any, lon: Any, bearing_deg: Any, dist_m: Any) -> tuple[Any, Any]:
    """End point of a geodesic on the WGS84 ellipsoid (Vincenty's direct formula).

    lat, lon: start, geodetic degrees. bearing_deg: initial azimuth clockwise from true
    north. dist_m: geodesic length in metres (negative goes backwards).
    Returns ``(lat_deg, lon_deg)`` with longitude in ``[-180, 180)``. Non-finite
    arguments raise ValueError naming the argument.
    """
    phi1, lon1, alpha1, s = np.broadcast_arrays(
        _lat_rad(lat, "lat"),
        np.radians(_finite_array(lon, "lon")),
        np.radians(_finite_array(bearing_deg, "bearing_deg")),
        _finite_array(dist_m, "dist_m"),
    )
    sa1, ca1 = np.sin(alpha1), np.cos(alpha1)
    u1 = np.arctan((1.0 - WGS84_F) * np.tan(phi1))
    su1, cu1 = np.sin(u1), np.cos(u1)
    sigma1 = np.arctan2(su1, cu1 * ca1)
    sin_alpha = cu1 * sa1
    cos2_alpha = 1.0 - sin_alpha * sin_alpha
    big_a, big_b = _vincenty_ab(cos2_alpha)
    sigma = s / (_B * big_a)
    for _ in range(8):
        two_sigma_m = 2.0 * sigma1 + sigma
        sigma = s / (_B * big_a) + _delta_sigma(big_b, np.sin(sigma), np.cos(sigma), np.cos(two_sigma_m))
    ss, cs = np.sin(sigma), np.cos(sigma)
    c2sm = np.cos(2.0 * sigma1 + sigma)
    lat2 = np.arctan2(
        su1 * cs + cu1 * ss * ca1,
        (1.0 - WGS84_F) * np.hypot(sin_alpha, su1 * ss - cu1 * cs * ca1),
    )
    omega = np.arctan2(ss * sa1, cu1 * cs - su1 * ss * ca1)
    lon2 = lon1 + omega - _longitude_correction(sin_alpha, cos2_alpha, sigma, ss, cs, c2sm)
    return _unwrap0(np.degrees(lat2)), wrap_lon_deg(np.degrees(lon2))


# --------------------------------------------------------------------------- internals


def _vincenty_ab(cos2_alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vincenty's series coefficients A and B for a geodesic with the given cos^2(alpha0)."""
    usq = cos2_alpha * _EP2
    big_a = 1.0 + usq / 16384.0 * (4096.0 + usq * (-768.0 + usq * (320.0 - 175.0 * usq)))
    big_b = usq / 1024.0 * (256.0 + usq * (-128.0 + usq * (74.0 - 47.0 * usq)))
    return big_a, big_b


def _delta_sigma(
    big_b: np.ndarray, sin_sigma: np.ndarray, cos_sigma: np.ndarray, cos_2sm: np.ndarray
) -> np.ndarray:
    """Vincenty's arc-length correction term (radians)."""
    return (
        big_b
        * sin_sigma
        * (
            cos_2sm
            + big_b
            / 4.0
            * (
                cos_sigma * (-1.0 + 2.0 * cos_2sm * cos_2sm)
                - big_b
                / 6.0
                * cos_2sm
                * (-3.0 + 4.0 * sin_sigma * sin_sigma)
                * (-3.0 + 4.0 * cos_2sm * cos_2sm)
            )
        )
    )


def _longitude_correction(
    sin_alpha: np.ndarray,
    cos2_alpha: np.ndarray,
    sigma: np.ndarray,
    sin_sigma: np.ndarray,
    cos_sigma: np.ndarray,
    cos_2sm: np.ndarray,
) -> np.ndarray:
    """Difference between the auxiliary-sphere and ellipsoid longitude differences, rad."""
    c = WGS84_F / 16.0 * cos2_alpha * (4.0 + WGS84_F * (4.0 - 3.0 * cos2_alpha))
    return (
        (1.0 - c)
        * WGS84_F
        * sin_alpha
        * (sigma + c * sin_sigma * (cos_2sm + c * cos_sigma * (-1.0 + 2.0 * cos_2sm * cos_2sm)))
    )


def _vincenty_inverse(
    u1: np.ndarray, u2: np.ndarray, lam_target: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Vincenty's inverse iteration for reduced latitudes u1, u2 and 0 <= longitude diff <= pi.

    Returns ``(distance_m, converged)``; entries that did not converge hold garbage.
    """
    su1, cu1 = np.sin(u1), np.cos(u1)
    su2, cu2 = np.sin(u2), np.cos(u2)
    lam = lam_target.copy()
    converged = np.zeros(lam.shape, dtype=bool)
    sigma = np.zeros_like(lam)
    ss = np.zeros_like(lam)
    cs = np.ones_like(lam)
    c2sm = np.zeros_like(lam)
    cos2_alpha = np.ones_like(lam)
    with np.errstate(divide="ignore", invalid="ignore"):
        for _ in range(_VINCENTY_ITERATIONS):
            sl, cl = np.sin(lam), np.cos(lam)
            ss = np.hypot(cu2 * sl, cu1 * su2 - su1 * cu2 * cl)
            cs = su1 * su2 + cu1 * cu2 * cl
            sigma = np.arctan2(ss, cs)
            sin_alpha = np.where(ss > 0.0, cu1 * cu2 * sl / ss, 0.0)
            cos2_alpha = 1.0 - sin_alpha * sin_alpha
            c2sm = np.where(cos2_alpha > 1e-300, cs - 2.0 * su1 * su2 / cos2_alpha, 0.0)
            lam_new = lam_target + _longitude_correction(sin_alpha, cos2_alpha, sigma, ss, cs, c2sm)
            ok = np.isfinite(lam_new) & (np.abs(lam_new) <= np.pi)
            converged = ok & (np.abs(lam_new - lam) < _VINCENTY_TOL)
            lam = np.where(ok, lam_new, lam)
            if np.all(converged | ~ok):
                break
    big_a, big_b = _vincenty_ab(cos2_alpha)
    dist = _B * big_a * (sigma - _delta_sigma(big_b, ss, cs, c2sm))
    return dist, converged


def _lambda_and_distance(
    alpha1: np.ndarray, u1: np.ndarray, u2: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Longitude difference and length of the geodesic leaving u1 at azimuth alpha1.

    Canonical configuration (u1 <= 0, |u2| <= |u1|, alpha1 in [0, pi]): the geodesic is
    followed to its first northbound crossing of reduced latitude u2. Returns
    ``(lambda12_rad, distance_m)``.
    """
    su1, cu1 = np.sin(u1), np.cos(u1)
    su2, cu2 = np.sin(u2), np.cos(u2)
    sa1, ca1 = np.sin(alpha1), np.cos(alpha1)
    sin_alpha = sa1 * cu1
    cos_alpha = np.hypot(ca1, sa1 * su1)  # cos(alpha0) >= |sin(u1)|
    cos2_alpha = cos_alpha * cos_alpha
    sigma1 = np.arctan2(su1, ca1 * cu1)
    omega1 = np.arctan2(sin_alpha * su1, ca1 * cu1)
    # northbound crossing of u2: cos(alpha2) = +sqrt(cos^2(alpha1) cos^2(u1) + cos^2(u2) - cos^2(u1)) / cos(u2)
    ca2_cu2 = np.sqrt(np.maximum((ca1 * cu1) ** 2 + (cu2 - cu1) * (cu2 + cu1), 0.0))
    sigma2 = np.arctan2(su2, ca2_cu2)
    omega2 = np.arctan2(sin_alpha * su2, ca2_cu2)
    sigma = sigma2 - sigma1
    ss, cs = np.sin(sigma), np.cos(sigma)
    c2sm = np.cos(sigma1 + sigma2)
    lam = (omega2 - omega1) - _longitude_correction(sin_alpha, cos2_alpha, sigma, ss, cs, c2sm)
    big_a, big_b = _vincenty_ab(cos2_alpha)
    dist = _B * big_a * (sigma - _delta_sigma(big_b, ss, cs, c2sm))
    return lam, dist


def _inverse_by_bisection(u1: np.ndarray, u2: np.ndarray, lam: np.ndarray) -> np.ndarray:
    """Geodesic length by bisection on the departure azimuth (robust near the antipode).

    u1, u2: reduced latitudes, rad; lam: longitude difference in [0, pi], rad. The
    points are first put in the canonical order (u1 <= 0, |u2| <= |u1|), in which the
    longitude reached is an increasing function of the azimuth at point 1.
    """
    swap = np.abs(u2) > np.abs(u1)
    a = np.where(swap, u2, u1)
    b = np.where(swap, u1, u2)
    flip = a > 0.0
    a = np.where(flip, -a, a)
    b = np.where(flip, -b, b)
    # a point exactly on the equator is nudged south so the azimuth parametrisation stays regular
    a = np.minimum(a, -1e-12)
    lo = np.zeros_like(lam)
    hi = np.full_like(lam, np.pi)
    for _ in range(_BISECTIONS):
        mid = 0.5 * (lo + hi)
        lam_mid, _ = _lambda_and_distance(mid, a, b)
        below = lam_mid < lam
        lo = np.where(below, mid, lo)
        hi = np.where(below, hi, mid)
    _, dist = _lambda_and_distance(0.5 * (lo + hi), a, b)
    return dist


def _lat_rad(lat_deg: Any, name: str) -> np.ndarray:
    lat = np.asarray(lat_deg, dtype=np.float64)
    if np.any(np.abs(lat) > 90.0 + 1e-9) or not np.all(np.isfinite(lat)):
        raise ValueError(f"{name} must be finite and within [-90, 90] degrees")
    return np.radians(np.clip(lat, -90.0, 90.0))


def _finite_array(value: Any, name: str) -> np.ndarray:
    """``value`` as float64, rejecting NaN and infinities (ValueError naming the argument).

    A non-finite longitude must not reach the geodesic iterations: they treat it as "not
    converged" and the bisection fallback then returns a finite, meaningless distance.
    """
    arr = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite")
    return arr


def _normalise2(s: np.ndarray, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Scale the pair (s, c) to unit length; (0, 0) maps to (0, 1)."""
    h = np.hypot(s, c)
    safe = np.where(h > 0.0, h, 1.0)
    return s / safe, np.where(h > 0.0, c / safe, 1.0)


def _vec3(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 0 or arr.shape[-1] != 3:
        raise ValueError(f"{name} must have shape [..., 3], got {arr.shape}")
    return arr


def _unwrap0(value: Any) -> Any:
    """Return a numpy scalar for 0-d results and the array otherwise."""
    arr = np.asarray(value)
    return arr[()] if arr.ndim == 0 else arr
