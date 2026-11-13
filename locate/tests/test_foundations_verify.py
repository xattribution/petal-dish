"""Independent verification of the foundations unit: timebase, geodesy, orbits, geometry.

Nothing here is compared with the implementation's own output. The references are:

- exact rational arithmetic (``fractions``) and 80-bit extended precision;
- scipy root finders, ODE integrators, rotations and interpolators;
- the ``sgp4`` package called directly, one epoch at a time;
- an event simulation in an inertial frame: where the implementation solves the
  light-time equations backwards in the rotating frame by fixed-point iteration, the
  simulation below follows a wavefront forwards from the emitter through the
  transponder to the site, with the ground stations moving on circles, and finds every
  crossing with a bracketing root finder;
- two phase histories built from those events (emitter phase at the emission time plus
  transponder oscillator phase at the satellite time minus receiver oscillator phase
  at the reception time), differentiated with an 8th-order stencil.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np
import pytest
from scipy.integrate import quad, solve_ivp
from scipy.interpolate import CubicHermiteSpline
from scipy.optimize import brentq
from scipy.spatial.transform import Rotation
from sgp4 import exporter
from sgp4.api import WGS72, Satrec, jday
from sgp4.propagation import gstime

from petal_locate.core import geodesy as gd
from petal_locate.core import geometry as gm
from petal_locate.core import orbits as ob
from petal_locate.core import timebase as tb
from petal_locate.core.constants import (
    C,
    GM,
    GMST_REF,
    J2,
    OMEGA_E,
    OMEGA_GMST,
    T_REF_S,
    WGS84_A,
    WGS84_F,
)
from petal_locate.core.types import PathSpec, Uncertainty

LD = np.longdouble
EXTENDED = bool(np.finfo(LD).eps < 1e-18)  # x86 80-bit; elsewhere longdouble is float64
TWO_PI = Fraction("6.28318530717958647692528676655900576839433879875021")
E2 = WGS84_F * (2.0 - WGS84_F)
B_AXIS = WGS84_A * (1.0 - WGS84_F)

EPOCH_ISO = "2026-01-01T00:00:00Z"
EPOCH = 1767225600.0  # 2026-01-01T00:00:00Z, checked against datetime below
T_GEO = EPOCH + 3 * 3600.0 + 0.123  # MATH 12 item 8 epoch, off the whole second
T_LEO = EPOCH + 86400.0 * 20 + 333.0

# 8th-order central first derivative, offsets -4..4 (Fornberg weights)
D8 = np.array([1 / 280, -4 / 105, 1 / 5, -4 / 5, 0.0, 4 / 5, -1 / 5, 4 / 105, -1 / 280])
K8 = np.arange(-4, 5)


def test_reference_epoch_constant_is_what_the_calendar_says() -> None:
    assert EPOCH == (dt.datetime(2026, 1, 1) - dt.datetime(1970, 1, 1)).total_seconds()
    assert T_REF_S == EPOCH


# ============================================================================ timebase


def gmst_exact(t_s: float, dut1_s: float = 0.0) -> float:
    """MATH.md section 1 formula in exact rational arithmetic, reduced with a 50-digit 2 pi."""
    x = Fraction(GMST_REF) + Fraction(OMEGA_GMST) * ((Fraction(t_s) - Fraction(T_REF_S)) + Fraction(dut1_s))
    return float(x - math.floor(x / TWO_PI) * TWO_PI)


def angle_diff(a: Any, b: Any) -> Any:
    return np.mod(np.asarray(a) - np.asarray(b) + np.pi, 2 * np.pi) - np.pi


def test_gmst_against_exact_rational_arithmetic() -> None:
    rng = np.random.default_rng(101)
    t = np.concatenate([rng.uniform(1.70e9, 1.95e9, 400), [T_REF_S, T_REF_S - 2.0**-22, 0.0, 4.0e9]])
    dut1 = rng.uniform(-0.9, 0.9, t.size)
    got = tb.gmst_rad(t, dut1)
    want = np.array([gmst_exact(a, b) for a, b in zip(t, dut1)])
    assert np.all((got >= 0.0) & (got < 2 * np.pi))
    # 1e-11 rad is 0.4 mm at GEO; the error is the float 2 pi used in the reduction
    assert np.max(np.abs(angle_diff(got, want))) < 1e-11
    assert tb.gmst_rad(T_REF_S) == GMST_REF
    # scalar in, scalar out; array dut1 broadcasts
    assert np.ndim(tb.gmst_rad(1.8e9)) == 0
    assert tb.gmst_rad(1.8e9, np.zeros(3)).shape == (3,)


def test_gmst_against_sgp4_gstime_over_the_documented_window() -> None:
    t0 = tb.sec(tb.ns_from_iso("2024-01-01"))
    t1 = tb.sec(tb.ns_from_iso("2031-01-01"))
    t = np.arange(t0, t1, 86400.0 * 2.713)
    ref = np.array([gstime(2440587.5 + x / 86400.0) for x in t])
    assert np.max(np.abs(angle_diff(tb.gmst_rad(t), ref))) < 5e-8
    # dut1 is a pure time shift at the GMST rate (UT1 = UTC + dut1)
    assert angle_diff(tb.gmst_rad(1.8e9, 0.37), tb.gmst_rad(1.8e9)) == pytest.approx(OMEGA_GMST * 0.37, abs=1e-14)


def test_gmst_has_no_sample_to_sample_jitter_when_the_offset_is_split() -> None:
    """Float seconds near 1.8e9 are quantised to 2.4e-7 s; the split offset must not be."""
    offsets = np.arange(8192) / 31250.0 + 1.0e-9
    theta = np.unwrap(tb.gmst_rad(1.9e9 + 0.25, offsets))
    line = theta[0] + OMEGA_GMST * (offsets - offsets[0])
    assert np.max(np.abs(theta - line)) < 2e-15  # 8e-8 m at GEO


def test_sec_is_correctly_rounded_and_refuses_floats() -> None:
    rng = np.random.default_rng(102)
    for t_ns in [0, 1, -1, 1_767_225_600_000_000_000, 1_767_225_600_123_456_789, *rng.integers(-(2**62), 2**62, 200).tolist()]:
        got = tb.sec(int(t_ns))
        exact = Fraction(int(t_ns), 10**9)
        # correctly rounded: no double is closer to the exact quotient
        assert abs(Fraction(got) - exact) <= abs(Fraction(float(np.nextafter(got, np.inf))) - exact)
        assert abs(Fraction(got) - exact) <= abs(Fraction(float(np.nextafter(got, -np.inf))) - exact)
    assert tb.sec(np.int64(5_000_000_000)) == 5.0
    for bad in (1.0, np.float64(2.0), "3", None, True):
        with pytest.raises(ValueError, match="t_ns"):
            tb.sec(bad)  # type: ignore[arg-type]


def test_iso_parsing_against_datetime_and_integer_arithmetic() -> None:
    rng = np.random.default_rng(103)
    unix = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    for _ in range(300):
        micro = int(rng.integers(-2_000_000_000_000_000, 4_000_000_000_000_000))  # 1906 .. 2096
        extra_ns = int(rng.integers(0, 1000))
        offset_min = int(rng.integers(-14 * 60, 14 * 60 + 1))
        zone = dt.timezone(dt.timedelta(minutes=offset_min))
        local = (unix + dt.timedelta(microseconds=micro)).astimezone(zone)
        sign = "-" if offset_min < 0 else "+"
        text = local.strftime("%Y-%m-%dT%H:%M:%S") + f".{local.microsecond:06d}{extra_ns:03d}"
        text += f"{sign}{abs(offset_min) // 60:02d}:{abs(offset_min) % 60:02d}"
        want = micro * 1000 + extra_ns
        assert tb.ns_from_iso(text) == want
        assert tb.ns_from_iso(tb.iso_from_ns(want)) == want
        assert tb.ns_from_datetime(local) == micro * 1000
    assert tb.ns_from_iso("1969-12-31T23:59:59.999999999Z") == -1
    assert tb.iso_from_ns(-1) == "1969-12-31T23:59:59.999999999Z"
    assert tb.iso_from_ns(1_767_225_600_500_000_000) == "2026-01-01T00:00:00.500Z"
    assert tb.ns_from_iso("2026-01-01") == int(EPOCH) * 10**9
    for bad in ("2026-02-29T00:00:00Z", "2026-01-01T24:00:00Z", "2026-01-01T00:00:60Z", "2026-01-01T00:00:00.0000000001Z", "1767225600", "2026-01-01T00:00:00+25:00", ""):
        with pytest.raises(ValueError):
            tb.ns_from_iso(bad)
    with pytest.raises(ValueError):
        tb.iso_from_ns(1.0e18)  # type: ignore[arg-type]


def test_wrapped_phase_against_exact_arithmetic() -> None:
    rng = np.random.default_rng(104)
    dt_s = np.concatenate([rng.uniform(-3e8, 3e8, 300), [0.0, 0.5, -86400.0]])
    for rate in (OMEGA_E, OMEGA_GMST, 1.1e-3):
        got = tb.wrapped_phase(rate, dt_s)
        for g, d in zip(got, dt_s):
            exact = Fraction(rate) * Fraction(float(d))
            turns = (exact - Fraction(float(g))) / TWO_PI
            assert abs(turns - round(turns)) * 2 * math.pi < 2e-11
            assert abs(g) < 2 * np.pi and (g == 0.0 or (g > 0) == (exact > 0))


# ============================================================================ geodesy


def lla_to_ecef_ld(lat_deg: Any, lon_deg: Any, alt_m: Any) -> np.ndarray:
    """Textbook forward conversion in extended precision."""
    lat = np.radians(np.asarray(lat_deg, dtype=LD))
    lon = np.radians(np.asarray(lon_deg, dtype=LD))
    alt = np.asarray(alt_m, dtype=LD)
    n = LD(WGS84_A) / np.sqrt(1 - LD(E2) * np.sin(lat) ** 2)
    return np.stack(
        [(n + alt) * np.cos(lat) * np.cos(lon), (n + alt) * np.cos(lat) * np.sin(lon), (n * (1 - LD(E2)) + alt) * np.sin(lat)],
        axis=-1,
    )


def test_lla_to_ecef_against_extended_precision_and_the_ellipsoid_equation() -> None:
    rng = np.random.default_rng(201)
    lat = np.degrees(np.arcsin(rng.uniform(-1, 1, 5000)))
    lon = rng.uniform(-540, 540, 5000)
    alt = rng.uniform(-10e3, 4.5e7, 5000)
    got = gd.lla_to_ecef(lat, lon, alt)
    err = np.linalg.norm((got - lla_to_ecef_ld(lat, lon, alt)).astype(float), axis=-1)
    assert np.max(err / np.linalg.norm(got, axis=-1)) < 2e-15  # a few ulp of the radius
    surf = gd.lla_to_ecef(lat, lon, 0.0)
    assert np.max(np.abs((surf[:, 0] ** 2 + surf[:, 1] ** 2) / WGS84_A**2 + surf[:, 2] ** 2 / B_AXIS**2 - 1.0)) < 1e-15
    assert gd.lla_to_ecef(0.0, 0.0).tolist() == [WGS84_A, 0.0, 0.0]
    assert gd.lla_to_ecef(90.0, 0.0)[2] == pytest.approx(B_AXIS, abs=1e-9)
    assert gd.lla_to_ecef(np.zeros((2, 3)), 0.0, np.zeros(3)).shape == (2, 3, 3)


def geodetic_by_root_finding(xyz: np.ndarray) -> tuple[float, float]:
    """Latitude from the meridian-plane normal equation with a bracketing root finder."""
    p, z = math.hypot(xyz[0], xyz[1]), xyz[2]

    def f(phi: float) -> float:
        n = WGS84_A / math.sqrt(1 - E2 * math.sin(phi) ** 2)
        return p * math.sin(phi) - z * math.cos(phi) - E2 * n * math.sin(phi) * math.cos(phi)

    phi = brentq(f, -math.pi / 2, math.pi / 2, xtol=1e-16, rtol=1e-15)
    n = WGS84_A / math.sqrt(1 - E2 * math.sin(phi) ** 2)
    alt = p * math.cos(phi) + z * math.sin(phi) - n * (1 - E2 * math.sin(phi) ** 2)
    return math.degrees(phi), alt


def test_ecef_to_lla_against_root_finding_and_extended_round_trip() -> None:
    rng = np.random.default_rng(202)
    lat = np.degrees(np.arcsin(rng.uniform(-1, 1, 4000)))
    lon = rng.uniform(-180, 180, 4000)
    alt = np.concatenate([rng.uniform(-10e3, 30e3, 2000), 10 ** rng.uniform(4.5, 7.7, 2000)])
    xyz = gd.lla_to_ecef(lat, lon, alt)
    lat2, lon2, alt2 = gd.ecef_to_lla(xyz)
    back = lla_to_ecef_ld(lat2, lon2, alt2)
    assert np.max(np.linalg.norm((back - xyz.astype(LD)).astype(float), axis=-1)) < 3e-8
    assert np.all((lon2 >= -180.0) & (lon2 < 180.0))
    for k in range(0, 4000, 97):
        phi, h = geodetic_by_root_finding(xyz[k])
        assert lat2[k] == pytest.approx(phi, abs=2e-13)
        assert alt2[k] == pytest.approx(h, abs=2e-8)
    # axis and equator
    assert gd.ecef_to_lla([0.0, 0.0, B_AXIS + 12.5]) == pytest.approx((90.0, 0.0, 12.5), abs=1e-9)
    assert gd.ecef_to_lla([0.0, 0.0, -B_AXIS]) == pytest.approx((-90.0, 0.0, 0.0), abs=1e-9)
    assert gd.ecef_to_lla([0.0, -(WGS84_A + 7.0), 0.0]) == pytest.approx((0.0, -90.0, 7.0), abs=1e-9)
    assert gd.ecef_to_lla([-WGS84_A, 0.0, 0.0])[1] == -180.0
    assert gd.ecef_to_lla(np.ones((4, 5, 3)) * 5e6)[2].shape == (4, 5)


def test_local_frame_is_the_gradient_of_the_geodetic_map_and_right_handed() -> None:
    rng = np.random.default_rng(203)
    lat, lon = rng.uniform(-89.5, 89.5, 500), rng.uniform(-180, 180, 500)
    basis = gd.enu_basis(lat, lon)
    h = 1e-6
    east = gd.lla_to_ecef(lat, lon + h, 0.0) - gd.lla_to_ecef(lat, lon - h, 0.0)
    north = gd.lla_to_ecef(lat + h, lon, 0.0) - gd.lla_to_ecef(lat - h, lon, 0.0)
    up = gd.lla_to_ecef(lat, lon, 1.0) - gd.lla_to_ecef(lat, lon, -1.0)
    for row, fd in zip(range(3), (east, north, up)):
        unit = fd / np.linalg.norm(fd, axis=-1, keepdims=True)
        assert np.max(np.abs(basis[:, row, :] - unit)) < 5e-8
    assert np.allclose(np.cross(basis[:, 0, :], basis[:, 1, :]), basis[:, 2, :], atol=1e-15)
    # radii of curvature are the lengths of those gradients per radian
    m, n = gd.radii_of_curvature(lat)
    assert np.max(np.abs(np.linalg.norm(north, axis=-1) / np.radians(2 * h) / m - 1.0)) < 1e-7
    assert np.max(np.abs(np.linalg.norm(east, axis=-1) / np.radians(2 * h) / (n * np.cos(np.radians(lat))) - 1.0)) < 1e-7
    assert gd.radii_of_curvature(0.0) == pytest.approx((WGS84_A * (1 - E2), WGS84_A), rel=1e-15)
    assert gd.radii_of_curvature(90.0) == pytest.approx((WGS84_A**2 / B_AXIS, WGS84_A**2 / B_AXIS), rel=1e-14)


def test_look_angles_and_azel_against_an_independent_local_frame() -> None:
    """Targets are placed with scipy rotations of the local frame, not with the module's basis."""
    rng = np.random.default_rng(204)
    for _ in range(200):
        lat, lon = rng.uniform(-89, 89), rng.uniform(-180, 180)
        az, el, dist = rng.uniform(0, 360), rng.uniform(-89, 89), 10 ** rng.uniform(2, 7.6)
        site = gd.lla_to_ecef(lat, lon, 30.0)
        # local frame: start with east=+y, north=+z, up=+x at (0, 0), rotate to the site
        to_site = Rotation.from_euler("yz", [-lat, lon], degrees=True)
        local = np.array([math.sin(math.radians(el)), math.sin(math.radians(az)) * math.cos(math.radians(el)), math.cos(math.radians(az)) * math.cos(math.radians(el))])
        direction = to_site.apply(local)
        got_az, got_el, got_rng = gd.look_angles(site, site + dist * direction)
        assert got_el == pytest.approx(el, abs=1e-7)
        assert (got_az - az + 180.0) % 360.0 - 180.0 == pytest.approx(0.0, abs=1e-7 / math.cos(math.radians(el)))
        assert got_rng == pytest.approx(dist, rel=1e-9)
        assert np.max(np.abs(gd.azel_to_ecef(lat, lon, az, el) - direction)) < 1e-12
    # geostationary satellite from the sub-satellite point: straight up; from the pole: on the horizon side
    sat = np.array([42164172.0, 0.0, 0.0])
    assert gd.look_angles(gd.lla_to_ecef(0.0, 0.0), sat)[1] == pytest.approx(90.0, abs=1e-9)
    az, el, _ = gd.look_angles(gd.lla_to_ecef(45.0, 0.0), sat)
    assert az == pytest.approx(180.0, abs=1e-9) and 0.0 < el < 45.0
    az, _, _ = gd.look_angles(gd.lla_to_ecef(0.0, -30.0), sat)
    assert az == pytest.approx(90.0, abs=1e-9)  # satellite to the east
    assert gd.look_angles(np.stack([gd.lla_to_ecef(0.0, 0.0)] * 4), sat)[0].shape == (4,)


def test_rz_and_angle_between_against_scipy_rotations() -> None:
    rng = np.random.default_rng(205)
    angles = rng.uniform(-7, 7, 50)
    ref = Rotation.from_rotvec(angles[:, None] * np.array([0.0, 0.0, 1.0])).as_matrix()
    assert gd.rz(angles).shape == (50, 3, 3)
    assert np.max(np.abs(gd.rz(angles) - ref)) < 1e-15
    assert gd.rz(0.3).shape == (3, 3)
    assert np.allclose(gd.rz(np.pi / 2) @ [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], atol=1e-15)  # counter-clockwise, active
    u = rng.normal(size=(200, 3))
    axis = np.cross(u, rng.normal(size=(200, 3)))
    axis /= np.linalg.norm(axis, axis=-1, keepdims=True)
    theta = np.concatenate([rng.uniform(0, 180, 190), [0.0, 180.0, 1e-7, 180 - 1e-7, 90.0, 1e-10, 45.0, 60.0, 120.0, 30.0]])
    v = Rotation.from_rotvec(axis * np.radians(theta)[:, None]).apply(u) * rng.uniform(0.1, 10, (200, 1))
    assert np.max(np.abs(gd.angle_between_deg(u, v) - theta)) < 1e-9
    assert gd.wrap_lon_deg(np.array([180.0, -180.0, 540.0, 190.0, -190.0, 0.0])).tolist() == [-180.0, -180.0, -180.0, -170.0, 170.0, 0.0]


def geodesic_ode(lat_deg: float, lon_deg: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """Integrate the geodesic equations of the ellipsoid in geodetic coordinates."""

    def rhs(_s: float, y: np.ndarray) -> list[float]:
        phi, _lam, alpha = y
        w = math.sqrt(1.0 - E2 * math.sin(phi) ** 2)
        m, n = WGS84_A * (1.0 - E2) / w**3, WGS84_A / w
        return [math.cos(alpha) / m, math.sin(alpha) / (n * math.cos(phi)), math.sin(alpha) * math.tan(phi) / n]

    sol = solve_ivp(rhs, [0.0, dist_m], np.radians([lat_deg, lon_deg, bearing_deg]), method="DOP853", rtol=1e-13, atol=1e-15)
    return float(np.degrees(sol.y[0, -1])), float(np.degrees(sol.y[1, -1]))


def test_geodesics_against_the_geodesic_ode_up_to_19900_km() -> None:
    """Any geodesic shorter than pi b^2 / a = 19 903 km is the unique shortest path, so
    the inverse problem must return the length that was integrated, including for end
    points within a degree of the antipode where Vincenty's iteration does not converge."""
    rng = np.random.default_rng(206)
    cases = [(rng.uniform(-80, 80), rng.uniform(-180, 180), rng.uniform(0, 360), s) for s in rng.uniform(1.0, 1.99e7, 24)]
    cases += [(5.0, 10.0, 91.0, 1.99e7), (-0.5, 0.0, 89.0, 1.989e7), (40.0, -70.0, 200.0, 1.9895e7), (0.0, 0.0, 90.0, 1.0e7)]
    worst_direct = worst_inverse = 0.0
    for lat, lon, bearing, dist in cases:
        lat2, lon2 = geodesic_ode(lat, lon, bearing, dist)
        if abs(lat2) > 89.0:
            continue  # the ODE is singular at the poles
        got_lat, got_lon = gd.destination(lat, lon, bearing, dist)
        worst_direct = max(worst_direct, float(np.linalg.norm(gd.lla_to_ecef(got_lat, got_lon) - gd.lla_to_ecef(lat2, lon2))))
        worst_inverse = max(worst_inverse, abs(float(gd.geodesic_distance_m(lat, lon, lat2, lon2)) - dist))
        # symmetric in the end points
        assert gd.geodesic_distance_m(lat2, lon2, lat, lon) == pytest.approx(gd.geodesic_distance_m(lat, lon, lat2, lon2), abs=1e-5)
    assert worst_direct < 5e-4
    assert worst_inverse < 5e-4


def meridian_arc(lat_a: float, lat_b: float) -> float:
    return quad(lambda p: WGS84_A * (1 - E2) / (1 - E2 * math.sin(p) ** 2) ** 1.5, math.radians(lat_a), math.radians(lat_b), epsabs=1e-7)[0]


def test_geodesic_published_values_and_closed_forms() -> None:
    # lines quoted in the GeographicLib documentation (New York - London, Wellington -
    # Salamanca, and Karney's nearly antipodal example) and in the literature on the
    # failure of Vincenty's inverse method near the antipode
    assert gd.geodesic_distance_m(40.6, -73.8, 51.6, -0.5) == pytest.approx(5551759.400, abs=1e-3)
    assert gd.geodesic_distance_m(-41.32, 174.81, 40.96, -5.50) == pytest.approx(19959679.267, abs=1e-3)
    assert gd.geodesic_distance_m(0.0, 0.0, 0.5, 179.5) == pytest.approx(19936288.579, abs=1e-3)
    assert gd.geodesic_distance_m(0.0, 0.0, 0.5, 179.7) == pytest.approx(19944127.421, abs=1e-3)
    # meridians (quadrature of the meridional radius), over the pole, antipodes
    half = 2 * meridian_arc(0.0, 90.0)
    assert gd.geodesic_distance_m(-20.0, 30.0, 55.0, 30.0) == pytest.approx(meridian_arc(-20.0, 55.0), abs=1e-4)
    assert gd.geodesic_distance_m(70.0, 10.0, 80.0, -170.0) == pytest.approx(meridian_arc(70.0, 90.0) + meridian_arc(80.0, 90.0), abs=1e-4)
    assert gd.geodesic_distance_m(20.0, 0.0, -20.0, 180.0) == pytest.approx(half, abs=1e-4)
    assert gd.geodesic_distance_m(0.0, 0.0, 0.0, 180.0) == pytest.approx(half, abs=1e-4)
    # the equator is the geodesic only up to (1 - f) 180 degrees of longitude
    assert gd.geodesic_distance_m(0.0, 0.0, 0.0, 179.0) == pytest.approx(WGS84_A * math.radians(179.0), abs=1e-5)
    beyond = float(gd.geodesic_distance_m(0.0, 0.0, 0.0, 179.9))
    assert math.pi * B_AXIS < beyond < half < WGS84_A * math.radians(179.9)
    assert gd.geodesic_distance_m(33.0, 44.0, 33.0, 44.0) == 0.0
    # broadcasting
    d = gd.geodesic_distance_m(np.zeros((3, 1)), 0.0, np.array([1.0, 2.0]), 5.0)
    assert d.shape == (3, 2)
    assert gd.destination(0.0, 0.0, 90.0, WGS84_A * math.radians(10.0)) == pytest.approx((0.0, 10.0), abs=1e-11)
    lat, lon = gd.destination(0.0, 179.5, 90.0, WGS84_A * math.radians(1.0))
    assert lon == pytest.approx(-179.5, abs=1e-10)  # normalised to [-180, 180)


def test_geodesy_rejects_non_finite_input_instead_of_returning_numbers() -> None:
    """Before the fix a NaN or infinite longitude came back from the bisection fallback
    as a finite distance (6e-6 m for NaN, 1 106 511 m for inf)."""
    for args, name in (((0.0, np.nan, 0.0, 0.0), "lon1"), ((10.0, 0.0, 20.0, np.inf), "lon2"), ((10.0, 0.0, 20.0, np.array([1.0, np.nan])), "lon2"), ((np.nan, 0.0, 0.0, 0.0), "lat1"), ((0.0, 0.0, 91.0, 0.0), "lat2")):
        with pytest.raises(ValueError, match=name):
            gd.geodesic_distance_m(*args)
    for args, name in (((0.0, np.nan, 10.0, 5.0), "lon"), ((0.0, 0.0, np.nan, 5.0), "bearing_deg"), ((0.0, 0.0, 10.0, np.inf), "dist_m"), ((95.0, 0.0, 10.0, 5.0), "lat")):
        with pytest.raises(ValueError, match=name):
            gd.destination(*args)
    for args, name in (((np.nan, 0.0, 0.0), "lat_deg"), ((90.5, 0.0, 0.0), "lat_deg"), ((0.0, np.inf, 0.0), "lon_deg"), ((0.0, 0.0, np.nan), "alt_m")):
        with pytest.raises(ValueError, match=name):
            gd.lla_to_ecef(*args)
    for bad in ([1.0, 2.0], 5.0, np.zeros((3, 2))):
        with pytest.raises(ValueError, match="xyz"):
            gd.ecef_to_lla(bad)


# ============================================================================ orbits


def make_tle(satnum: int, epoch_jd: float, ecc: float, argp: float, inc: float, mean_anomaly: float, rev_per_day: float, raan: float, bstar: float = 0.0) -> tuple[str, str]:
    """Element set lines written by the sgp4 package itself (angles in degrees)."""
    sat = Satrec()
    sat.sgp4init(WGS72, "i", satnum, epoch_jd - 2433281.5, bstar, 0.0, 0.0, ecc, math.radians(argp), math.radians(inc), math.radians(mean_anomaly), rev_per_day * 2 * math.pi / 1440.0, math.radians(raan))
    return exporter.export_tle(sat)


TLE_EPOCH_JD = sum(jday(2026, 3, 1, 12, 0, 0.0))
TLE_EPOCH = (TLE_EPOCH_JD - 2440587.5) * 86400.0
TLE_GEO_LINES = make_tle(40000, TLE_EPOCH_JD, 0.0002, 30.0, 3.0, 100.0, 1.00273790935, 250.0)
TLE_LEO_LINES = make_tle(40001, TLE_EPOCH_JD, 0.001, 30.0, 51.6, 100.0, 15.5, 250.0, 1e-4)


def rot_z(angle: float) -> np.ndarray:
    return Rotation.from_euler("z", angle).as_matrix()


def rot_x(angle: float) -> np.ndarray:
    return Rotation.from_euler("x", angle).as_matrix()


def ecef_derivative(orbit: Any, t: float, h: float) -> np.ndarray:
    """8th-order derivative of the ECEF position using only the split-time position."""
    p = orbit.position_ecef(t, K8 * h)
    return (D8[:, None] * p).sum(axis=0) / h


@pytest.mark.parametrize("lon, inc, phase, drift", [(-177.0, 5.0, 40.0, 0.0), (172.0, 4.0, 200.0, 0.0), (75.0, 5.0, 310.0, 0.03), (10.0, 0.0, 77.0, -0.5), (100.0, 63.0, 10.0, 1.0)])
def test_geo_orbit_is_the_documented_rotation_composition(lon: float, inc: float, phase: float, drift: float) -> None:
    orbit = ob.GeoOrbit(lon, inc, EPOCH_ISO, phase, drift)
    n = OMEGA_E + math.radians(drift) / 86400.0
    a = (GM / n**2) ** (1.0 / 3.0)
    assert orbit.a_m == pytest.approx(a, rel=1e-15)
    for elapsed in (0.0, 1234.5, 3 * 3600.0, 86400.0 * 9.25, -86400.0 * 3.5):
        u = math.radians(phase) + n * elapsed
        r_i = rot_z(math.radians(lon - phase)) @ rot_x(math.radians(inc)) @ np.array([a * math.cos(u), a * math.sin(u), 0.0])
        want = rot_z(-OMEGA_E * elapsed) @ r_i
        r, v = orbit.state_ecef(EPOCH + elapsed)
        assert np.max(np.abs(r - want)) < 2e-6  # the reference loses 1e-13 rad in n * elapsed
        assert np.max(np.abs(v - ecef_derivative(orbit, EPOCH + elapsed, 2.0))) < 2e-8
        assert np.max(np.abs(orbit.position_ecef(EPOCH + elapsed) - r)) == 0.0


def test_math12_1_geostationary_and_inclined_geosynchronous() -> None:
    a = (GM / OMEGA_E**2) ** (1.0 / 3.0)
    assert a == pytest.approx(42164172.37, abs=0.01)
    geo = ob.GeoOrbit(100.0)
    t = EPOCH + np.linspace(0.0, 86400.0 * 400, 2001)
    r, v = geo.state_ecef(t)
    assert np.max(np.abs(r - a * np.array([math.cos(math.radians(100.0)), math.sin(math.radians(100.0)), 0.0]))) < 1e-3
    assert np.max(np.abs(v)) == 0.0
    lat, lon, alt = ob.subsatellite_point(geo, EPOCH + 5.0)
    assert (lat, lon, alt) == pytest.approx((0.0, 100.0, a - WGS84_A), abs=1e-6)

    inc5 = ob.GeoOrbit(100.0, 5.0)
    t = EPOCH + np.linspace(0.0, 86164.1, 40001)
    r, v = inc5.state_ecef(t)
    assert np.max(r[:, 2]) == pytest.approx(a * math.sin(math.radians(5.0)), rel=1e-7)
    assert np.max(np.linalg.norm(v, axis=-1)) == pytest.approx(a * 0.0873 * OMEGA_E, rel=2e-3)  # "about 268 m/s"
    assert np.max(np.abs(np.linalg.norm(r, axis=-1) - a)) < 1e-6
    # figure-8 centred on the slot, northbound through the equator at u = 0 (the epoch for phase 0)
    lat, lon, _ = ob.subsatellite_point(inc5, t)
    assert 0.5 * (lon.max() + lon.min()) == pytest.approx(100.0, abs=1e-6)
    assert lon.max() - lon.min() < 0.25 and lat.max() == pytest.approx(5.0, abs=0.05)
    r0, v0 = inc5.state_ecef(EPOCH)
    assert abs(r0[2]) < 1e-6 and v0[2] > 0.0


def kepler_reference(a: float, e: float, inc: float, raan: float, argp: float, m0: float, elapsed: float, j2: bool, dut1: float) -> np.ndarray:
    """ECEF position from the textbook recipe: Kepler's equation by bracketing, true
    anomaly, perifocal frame, 3-1-3 rotation, secular J2 rates, Earth rotation angle."""
    n0 = math.sqrt(GM / a**3)
    raan_rate = argp_rate = 0.0
    n = n0
    if j2:
        k = 1.5 * J2 * (WGS84_A / (a * (1 - e * e))) ** 2
        n = n0 * (1 + k * math.sqrt(1 - e * e) * (1 - 1.5 * math.sin(inc) ** 2))
        raan_rate = -k * n * math.cos(inc)
        argp_rate = k * n * (2 - 2.5 * math.sin(inc) ** 2)
    mean = m0 + n * elapsed
    ecc_anom = brentq(lambda x: x - e * math.sin(x) - mean, mean - 1.01, mean + 1.01, xtol=1e-15, rtol=1e-15)
    nu = 2 * math.atan2(math.sqrt(1 + e) * math.sin(ecc_anom / 2), math.sqrt(1 - e) * math.cos(ecc_anom / 2))
    radius = a * (1 - e * math.cos(ecc_anom))
    perifocal = np.array([radius * math.cos(nu), radius * math.sin(nu), 0.0])
    inertial = rot_z(raan + raan_rate * elapsed) @ rot_x(inc) @ rot_z(argp + argp_rate * elapsed) @ perifocal
    theta = GMST_REF + OMEGA_GMST * ((EPOCH + elapsed - T_REF_S) + dut1)
    return rot_z(-theta) @ inertial


@pytest.mark.parametrize(
    "a, e, inc, raan, argp, m0, j2",
    [
        (7.0e6, 0.001, 98.0, 30.0, 40.0, 50.0, True),
        (2.656e7, 0.74, 63.4, 200.0, 270.0, 10.0, True),
        (4.2164e7, 0.0003, 2.0, 80.0, 10.0, 300.0, True),
        (7.0e6, 0.1, 45.0, 0.0, 0.0, 0.0, False),
        (8.0e6, 0.95, 10.0, 5.0, 5.0, 179.9, True),
        (7.2e6, 0.0, 0.0, 10.0, 20.0, 30.0, True),
        (7.2e6, 0.3, 180.0, 10.0, 20.0, 30.0, True),
    ],
)
def test_kepler_orbit_against_the_textbook_recipe(a: float, e: float, inc: float, raan: float, argp: float, m0: float, j2: bool) -> None:
    orbit = ob.KeplerOrbit(a, e, inc, raan, argp, m0, EPOCH_ISO, j2, dut1_s=0.3)
    for elapsed in (0.0, 1000.0, 86400.0 * 3.3, -5000.0, 86400.0 * 12.0):
        want = kepler_reference(a, e, *np.radians([inc, raan, argp, m0]), elapsed, j2, 0.3)
        r, v = orbit.state_ecef(EPOCH + elapsed)
        assert np.max(np.abs(r - want)) < 2e-5  # reference rounding in n * elapsed, 1e-12 rad
        speed = float(np.linalg.norm(v))
        assert np.max(np.abs(v - ecef_derivative(orbit, EPOCH + elapsed, 0.125))) < 2e-9 * speed + 3e-8


def test_kepler_two_body_motion_against_numerical_integration_and_invariants() -> None:
    a, e = 7.0e6, 0.1
    orbit = ob.KeplerOrbit(a, e, 45.0, 10.0, 20.0, 30.0, EPOCH_ISO, j2=False)

    def inertial(t: float) -> tuple[np.ndarray, np.ndarray]:
        r, v = orbit.state_ecef(t)
        rot = rot_z(GMST_REF + OMEGA_GMST * (t - T_REF_S))
        return rot @ r, rot @ (v + np.cross([0.0, 0.0, OMEGA_GMST], r))

    r0, v0 = inertial(EPOCH)
    sol = solve_ivp(lambda _t, y: np.concatenate([y[3:], -GM * y[:3] / np.linalg.norm(y[:3]) ** 3]), [0.0, 6000.0], np.concatenate([r0, v0]), method="DOP853", rtol=1e-13, atol=1e-6)
    r1, v1 = inertial(EPOCH + 6000.0)
    assert np.max(np.abs(sol.y[:3, -1] - r1)) < 1e-3
    assert np.max(np.abs(sol.y[3:, -1] - v1)) < 1e-6
    for elapsed in (0.0, 777.0, 5000.0):
        r, v = inertial(EPOCH + elapsed)
        assert float(v @ v) == pytest.approx(GM * (2 / np.linalg.norm(r) - 1 / a), rel=1e-12)  # vis-viva
        assert float(np.linalg.norm(np.cross(r, v))) == pytest.approx(math.sqrt(GM * a * (1 - e * e)), rel=1e-12)


def teme_to_ecef_reference(lines: tuple[str, str], t: float, dut1: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """sgp4 called directly for one epoch, rotated as MATH.md section 2 says.

    Returns (r_ecef, v_ecef, r_ecef rotated with sgp4's own gstime instead)."""
    sat = Satrec.twoline2rv(*lines)
    jd = 2440587.5 + math.floor(t / 86400.0)
    fr = (t - 86400.0 * math.floor(t / 86400.0)) / 86400.0
    err, r_km, v_kms = sat.sgp4(jd, fr)
    assert err == 0
    r_teme, v_teme = np.array(r_km) * 1e3, np.array(v_kms) * 1e3
    theta = gmst_exact(t, dut1)
    r = rot_z(-theta) @ r_teme
    v = rot_z(-theta) @ v_teme - np.cross([0.0, 0.0, OMEGA_GMST], r)
    return r, v, rot_z(-gstime(jd + fr + dut1 / 86400.0)) @ r_teme


@pytest.mark.parametrize("lines", [TLE_GEO_LINES, TLE_LEO_LINES], ids=["geo", "leo"])
def test_tle_orbit_against_sgp4_called_directly(lines: tuple[str, str]) -> None:
    orbit = ob.TleOrbit(*lines, dut1_s=-0.21)
    assert orbit.epoch_s == pytest.approx(TLE_EPOCH, abs=2e-5)
    times = TLE_EPOCH + np.array([0.0, 1234.567, 86400.0 * 2.5, -3600.0, 86400.0 * 9.123, 43200.0 - 2.0**-10, 43200.0])
    r_all, v_all = orbit.state_ecef(times)
    for k, t in enumerate(times):
        r, v, r_gstime = teme_to_ecef_reference(lines, float(t), -0.21)
        assert np.max(np.abs(r_all[k] - r)) < 2e-6
        assert np.max(np.abs(v_all[k] - v)) < 1e-9
        # the linear Earth rotation angle is within 5e-8 rad of gstime: 2 m at GEO
        assert np.linalg.norm(r_all[k] - r_gstime) < 5e-8 * np.linalg.norm(r) + 1e-6
        # scalar call, and a sub-resolution offset passed separately
        assert np.max(np.abs(orbit.state_ecef(float(t))[0] - r_all[k])) == 0.0
    # SGP4's velocity is not exactly the derivative of the ECEF position (MATH.md section 1) ...
    numeric = ecef_derivative(orbit, float(times[1]), 0.25)
    gap = float(np.linalg.norm(numeric - v_all[1]))
    assert 1e-6 < gap < 0.06
    # ... and the position at t + dt is evaluated at the true sum, not at a rounded one
    moved = orbit.position_ecef(float(times[1]), 1.0e-9) - r_all[1]
    assert np.max(np.abs(moved - numeric * 1.0e-9)) < 3e-8


def test_tle_results_do_not_depend_on_the_order_of_calls() -> None:
    """SGP4's deep-space resonance integrator keeps state between calls; the outputs must not."""
    fresh = ob.TleOrbit(*TLE_GEO_LINES)
    used = ob.TleOrbit(*TLE_GEO_LINES)
    t = TLE_EPOCH + 86400.0 * 3 + 777.0
    used.state_ecef(TLE_EPOCH + 86400.0 * 40)
    used.state_ecef(TLE_EPOCH - 86400.0 * 11)
    batch = used.position_ecef(np.array([TLE_EPOCH + 86400.0 * 25, t, TLE_EPOCH - 5.0]))
    assert np.array_equal(fresh.position_ecef(t), used.position_ecef(t))
    assert np.array_equal(fresh.position_ecef(t), batch[1])


def test_math12_1_geostationary_tle_sits_at_its_catalogued_longitude() -> None:
    # mean longitude of the element set: RAAN + argument of perigee + mean anomaly - GMST
    catalogued = (250.0 + 30.0 + 100.0 - math.degrees(gstime(TLE_EPOCH_JD)) + 180.0) % 360.0 - 180.0
    lines = make_tle(40002, TLE_EPOCH_JD, 0.0002, 30.0, 0.02, 100.0, 1.00273790935, 250.0)
    lat, lon, alt = ob.subsatellite_point(ob.TleOrbit(*lines), TLE_EPOCH + np.array([0.0, 3600.0, 43200.0]))
    assert np.max(np.abs(lon - catalogued)) < 0.15  # 2 e sin M = 0.023 deg of equation of centre plus SGP4 periodics
    assert np.max(np.abs(lat)) < 0.03
    assert np.all(np.abs(alt - 35786e3) < 30e3)


def test_tle_validation() -> None:
    l1, l2 = TLE_GEO_LINES
    assert ob.tle_checksum(l1) == int(l1[68]) and ob.tle_checksum(l2) == int(l2[68])
    broken = l1[:68] + str((int(l1[68]) + 1) % 10)
    for args, name in (((broken, l2), "line1"), ((l1, l2[:60]), "line2"), ((l2, l1), "line1"), ((l1, 5), "line2")):
        with pytest.raises(ValueError, match=name):
            ob.TleOrbit(*args)  # type: ignore[arg-type]


def test_table_orbit_against_scipy_hermite_interpolation() -> None:
    base = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 30.0, 40.0, 50.0, EPOCH_ISO)
    nodes = T_LEO + np.concatenate([np.arange(-30, 0) * 10.0, [0.0, 7.0, 13.0, 40.0], np.arange(5, 31) * 10.0])  # uneven spacing
    r, v = base.state_ecef(nodes)
    table = ob.TableOrbit(nodes, r, v)
    spline = CubicHermiteSpline(nodes - T_LEO, r, v, axis=0)
    query = T_LEO + np.array([-300.0, -299.9, -123.456, 0.0, 3.5, 7.0, 12.999, 77.7, 299.99, 300.0])
    got_r, got_v = table.state_ecef(query)
    assert np.max(np.abs(got_r - spline(query - T_LEO))) < 5e-9
    assert np.max(np.abs(got_v - spline(query - T_LEO, 1))) < 1e-10
    assert np.max(np.abs(table.position_ecef(query) - got_r)) == 0.0
    assert np.max(np.abs(got_r - base.position_ecef(query))) < 1e-3  # 10 s Hermite on a LEO: sub-millimetre
    assert table.state_ecef(query.reshape(2, 5))[0].shape == (2, 5, 3)
    for outside in (nodes[0] - 1e-3, nodes[-1] + 1e-3):
        with pytest.raises(ValueError, match="t_s"):
            table.position_ecef(outside)
    with pytest.raises(ValueError, match="t_s"):
        table.position_ecef(nodes[-1] - 0.5, 0.75)  # the offset counts
    with pytest.raises(ValueError, match="t_s"):
        ob.TableOrbit(nodes[::-1], r, v)
    with pytest.raises(ValueError, match="r_m"):
        ob.TableOrbit(nodes, r[:-1], v)
    cfg = [{"t": tb.iso_from_ns(int(t * 1e9)), "r_m": list(rr), "v_mps": list(vv)} for t, rr, vv in zip(nodes[:5], r[:5], v[:5])]
    built = ob.orbit_from_config({"type": "table", "samples": cfg})
    assert np.max(np.abs(built.state_ecef(nodes[2] + 1.0)[0] - table.position_ecef(nodes[2] + 1.0))) < 1e-8


def test_perturbed_orbit_ric_frame_and_velocity() -> None:
    base = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 30.0, 40.0, 50.0, EPOCH_ISO)
    ric_pos, ric_vel, t_ref = np.array([120.0, -800.0, 300.0]), np.array([0.02, -0.3, 0.07]), T_LEO - 100.0
    orbit = ob.PerturbedOrbit(base, ric_pos, ric_vel, t_ref)
    for t in (T_LEO, T_LEO + 1234.5):
        r, v = base.state_ecef(t)
        v_inertial = v + np.cross([0.0, 0.0, OMEGA_E], r)
        radial = r / np.linalg.norm(r)
        cross = np.cross(r, v_inertial) / np.linalg.norm(np.cross(r, v_inertial))
        in_track = np.cross(cross, radial)
        assert in_track @ v_inertial > 0.0  # in-track points along the motion
        want = r + np.column_stack([radial, in_track, cross]) @ (ric_pos + ric_vel * (t - t_ref))
        got_r, got_v = orbit.state_ecef(t)
        assert np.max(np.abs(got_r - want)) < 1e-8
        numeric = ecef_derivative(orbit, t, 0.0625)
        assert np.max(np.abs(got_v - numeric)) < 2e-7
        # d r'/dt is not v + Q ric_vel: the frame rotates with the orbit (0.9 m/s here)
        naive = v + np.column_stack([radial, in_track, cross]) @ ric_vel
        assert np.linalg.norm(got_v - naive) > 0.3
    # geostationary base has zero ECEF velocity; the frame must still be radial / east / north
    geo = ob.PerturbedOrbit(ob.GeoOrbit(100.0), [1000.0, 2000.0, 3000.0], [0.0, 0.0, 0.0], T_GEO)
    lat, lon, alt = ob.subsatellite_point(geo, T_GEO)
    a = (GM / OMEGA_E**2) ** (1.0 / 3.0)
    assert alt - (a - WGS84_A) == pytest.approx(1000.0, abs=0.2)
    assert math.radians(lon - 100.0) * a == pytest.approx(2000.0, abs=0.2)
    assert geo.position_ecef(T_GEO)[2] == pytest.approx(3000.0, abs=1e-6)
    assert geo.state_ecef(T_GEO + np.zeros((2, 4)))[1].shape == (2, 4, 3)
    assert np.array_equal(ob.PerturbedOrbit(base, [0, 0, 0], [0, 0, 0], T_LEO).position_ecef(T_LEO, 0.25), base.position_ecef(T_LEO, 0.25))


def all_orbit_types() -> dict[str, tuple[Any, float]]:
    leo = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 30.0, 40.0, 50.0, EPOCH_ISO)
    nodes = T_LEO + np.arange(-6, 7) * 5.0
    return {
        "geo": (ob.GeoOrbit(172.0, 4.0, EPOCH_ISO, 200.0), T_GEO),
        "geo_drift": (ob.GeoOrbit(-177.0, 5.0, EPOCH_ISO, 40.0, 0.5), EPOCH + 86400.0 * 77.3),
        "kepler_leo": (leo, T_LEO),
        "kepler_heo": (ob.KeplerOrbit(2.656e7, 0.74, 63.4, 200.0, 270.0, 10.0, EPOCH_ISO), EPOCH + 86400.0 * 3 + 1000.0),
        "tle_geo": (ob.TleOrbit(*TLE_GEO_LINES), TLE_EPOCH + 86400.0 * 3 + 777.0),
        "tle_leo": (ob.TleOrbit(*TLE_LEO_LINES), TLE_EPOCH + 5000.0),
        "table": (ob.TableOrbit(nodes, *leo.state_ecef(nodes)), T_LEO + 0.4),
        "perturbed": (ob.PerturbedOrbit(leo, [100.0, -2000.0, 500.0], [0.1, -0.5, 0.2], T_LEO - 50.0), T_LEO),
    }


@pytest.mark.parametrize("name", list(all_orbit_types()))
def test_math12_1_site_range_is_a_smooth_function_of_the_sample_offset(name: str) -> None:
    """Degree-5 fit to the site range over 0.5 s at 31.25 kHz: residual below 1e-6 m."""
    orbit, t0 = all_orbit_types()[name]
    offsets = np.arange(15625) / 31250.0
    site = gd.lla_to_ecef(21.3, -157.8, 10.0)
    rng_m = np.linalg.norm(orbit.position_ecef(t0, offsets) - site, axis=-1)
    x = offsets / offsets[-1] * 2.0 - 1.0
    fit = np.polynomial.chebyshev.Chebyshev.fit(x, rng_m - rng_m[0], 5)
    assert np.max(np.abs(rng_m - rng_m[0] - fit(x))) < 1e-6
    # and the split evaluation is the state at the true sum wherever the sum is exact
    exact_offsets = np.array([0.0, 0.5, 0.25, 2.0**-10])
    assert np.max(np.abs(orbit.position_ecef(t0, exact_offsets) - orbit.position_ecef(t0 + exact_offsets))) < 2e-6
    r, v = orbit.state_ecef(t0, exact_offsets)
    assert np.max(np.abs(r - orbit.position_ecef(t0, exact_offsets))) < 1e-9
    if not name.startswith("tle"):
        assert np.max(np.abs(v[0] - ecef_derivative(orbit, t0, 0.0625))) < 1e-6


def test_seconds_arguments_refuse_nanosecond_and_millisecond_timestamps() -> None:
    """A t_ns handed to a seconds argument used to give a plausible position silently."""
    t_ns = 1_767_236_400_000_000_000
    orbits = all_orbit_types()
    for name in ("geo", "kepler_leo", "tle_geo", "perturbed"):
        orbit = orbits[name][0]
        for bad in (t_ns, float(t_ns), t_ns // 10**6, np.array([orbits[name][1], float(t_ns)])):
            with pytest.raises(ValueError, match="t_s"):
                orbit.state_ecef(bad)
            with pytest.raises(ValueError, match="t_s"):
                orbit.position_ecef(bad)
    with pytest.raises(ValueError, match="t_ref_s"):
        ob.PerturbedOrbit(orbits["geo"][0], [1.0, 0.0, 0.0], [0.0, 0.0, 0.0], t_ns)
    with pytest.raises(ValueError, match="epoch"):
        ob.GeoOrbit(0.0, epoch=t_ns)
    with pytest.raises(ValueError, match="t_s"):
        ob.TableOrbit(np.array([1.0, 2.0]) * 1e18, np.zeros((2, 3)), np.zeros((2, 3)))
    geo, sites = orbits["geo"][0], {"a": gd.lla_to_ecef(21.3, -157.8), "b": gd.lla_to_ecef(35.7, 139.8)}
    x = gd.lla_to_ecef(30.0, 140.0)
    path_a, path_b = PathSpec("g", "a", 3e8, -4.1e7), PathSpec("g", "b", 3e8, -4.1e7)
    calls: list[Callable[[], Any]] = [
        lambda: gm.uplink_delay(x, geo, t_ns),
        lambda: gm.downlink_delay(geo, sites["a"], t_ns),
        lambda: gm.bent_pipe(x, geo, sites["a"], t_ns),
        lambda: gm.bent_pipe_rates(x, geo, sites["a"], t_ns),
        lambda: gm.predict_tdoa(x, path_a, path_b, t_ns, {"g": geo}, sites),
        lambda: gm.predict_fdoa(x, path_a, path_b, float(t_ns), {"g": geo}, sites),
        lambda: gm.self_peak("g", -4.1e7, 3e8, path_a, path_b, t_ns, {"g": geo}, sites),
        lambda: gm.elevation_deg(x, geo, t_ns),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="Unix seconds"):
            call()
    for bad in (t_ns, float(t_ns), np.array([1.8e9, 1.8e12])):
        with pytest.raises(ValueError, match="t_s"):
            tb.gmst_rad(bad)
    # times that are legitimately far from now are still accepted
    assert np.all(np.isfinite(geo.position_ecef(np.array([0.0, -1.0e9, 9.0e9]))))
    assert np.all(np.isfinite(tb.gmst_rad(np.array([0.0, -1.0e9, 9.0e9]))))


def test_orbit_from_config_reads_plain_objects_and_names_missing_fields() -> None:
    geo = ob.orbit_from_config(SimpleNamespace(type="geo", lon_deg=172.0, inclination_deg=4.0, epoch=dt.datetime(2026, 1, 1, 9, tzinfo=dt.timezone(dt.timedelta(hours=9))), phase_deg=200.0, drift_deg_per_day=0.0))
    assert np.array_equal(geo.position_ecef(T_GEO), ob.GeoOrbit(172.0, 4.0, EPOCH_ISO, 200.0).position_ecef(T_GEO))
    default_epoch = ob.orbit_from_config({"type": "geo", "lon_deg": 172.0, "inclination_deg": 4.0, "phase_deg": 200.0})
    assert np.array_equal(default_epoch.position_ecef(T_GEO), geo.position_ecef(T_GEO))
    kep = ob.orbit_from_config({"type": "kepler", "a_m": 7.0e6, "e": 0.001, "inc_deg": 98.0, "raan_deg": 30.0, "argp_deg": 40.0, "mean_anomaly_deg": 50.0, "epoch": EPOCH_ISO}, dut1_s=0.3)
    want = kepler_reference(7.0e6, 0.001, *np.radians([98.0, 30.0, 40.0, 50.0]), 1000.0, True, 0.3)
    assert np.max(np.abs(kep.position_ecef(EPOCH + 1000.0) - want)) < 1e-5
    tle = ob.orbit_from_config(SimpleNamespace(type="tle", line1=TLE_GEO_LINES[0], line2=TLE_GEO_LINES[1]), dut1_s=0.2)
    assert np.max(np.abs(tle.position_ecef(TLE_EPOCH + 60.0) - teme_to_ecef_reference(TLE_GEO_LINES, TLE_EPOCH + 60.0, 0.2)[0])) < 2e-6
    assert ob.orbit_from_config(geo) is geo
    for model, name in (({"type": "sgp8"}, "orbit.type"), ({"type": "kepler", "a_m": 7e6}, "orbit.e"), ({"lon_deg": 1.0}, "orbit.type"), ({"type": "geo"}, "orbit.lon_deg"), ({"type": "table", "samples": []}, "samples")):
        with pytest.raises(ValueError, match=name):
            ob.orbit_from_config(model)


# ============================================================================ light time


def rot_z_ld(vec: np.ndarray, angle: Any) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([vec[0] * c - vec[1] * s, vec[0] * s + vec[1] * c, vec[2]], dtype=LD)


class Inertial:
    """Event simulation in the inertial frame that coincides with ECEF at ``t0``.

    Times are extended-precision offsets from ``t0``. A point fixed to the Earth moves
    on a circle, ``Rz(OMEGA_E dt) x``; the satellite is wherever ``sat(dt)`` says in
    the same frame. Every light-time equation is a 1-D root bracketed in [0, 2] s.
    """

    def __init__(self, sat: Callable[[Any], np.ndarray]) -> None:
        self.sat = sat

    @classmethod
    def of(cls, orbit: Any, t0: float) -> "Inertial":
        return cls(lambda d: rot_z_ld(np.asarray(ob.orbit_position(orbit, t0, float(d)), dtype=LD), LD(OMEGA_E) * d))

    @staticmethod
    def ground(x: Any, d: Any) -> np.ndarray:
        return rot_z_ld(np.asarray(x, dtype=LD), LD(OMEGA_E) * d)

    @staticmethod
    def _root(miss: Callable[[Any], Any]) -> Any:
        """Root of miss(tau) = range(tau) - C tau: bracketed, then polished in extended precision."""
        tau = LD(brentq(lambda v: float(miss(LD(v))), 0.0, 2.0, xtol=1e-17, rtol=1e-15))
        return tau + miss(tau) / LD(C)

    @staticmethod
    def _range(p: np.ndarray, q: np.ndarray) -> Any:
        return np.sqrt(np.sum((p - q) ** 2))

    def up_arriving(self, x: Any, d_sat: Any) -> Any:
        """Flight time of the wavefront that reaches the satellite at ``d_sat``."""
        sat = self.sat(d_sat)
        return self._root(lambda tau: self._range(sat, self.ground(x, d_sat - tau)) - LD(C) * tau)

    def down_arriving(self, g: Any, d_rx: Any) -> Any:
        """Flight time of the wavefront that reaches the site at ``d_rx``."""
        site = self.ground(g, d_rx)
        return self._root(lambda tau: self._range(site, self.sat(d_rx - tau)) - LD(C) * tau)

    def up_leaving(self, x: Any, d_emit: Any) -> Any:
        """Flight time to the satellite of the wavefront emitted at ``d_emit``."""
        source = self.ground(x, d_emit)
        return self._root(lambda tau: self._range(self.sat(d_emit + tau), source) - LD(C) * tau)

    def down_leaving(self, g: Any, d_leave: Any) -> Any:
        """Flight time to the site of the wavefront that leaves the satellite at ``d_leave``."""
        sat = self.sat(d_leave)
        return self._root(lambda tau: self._range(self.ground(g, d_leave + tau), sat) - LD(C) * tau)

    def backward(self, x: Any, g: Any, tau_x: float, d_rx: Any) -> tuple[Any, Any]:
        """(tau_d, tau_u) of the signal received at ``d_rx``, traced back to its emission."""
        tau_d = self.down_arriving(g, LD(d_rx))
        return tau_d, self.up_arriving(x, LD(d_rx) - tau_d - LD(tau_x))

    def forward(self, x: Any, g: Any, tau_x: float, d_emit: Any) -> tuple[Any, Any]:
        """(arrival at the satellite, reception at the site) of the wavefront emitted at ``d_emit``."""
        d_sat = LD(d_emit) + self.up_leaving(x, LD(d_emit))
        return d_sat, d_sat + LD(tau_x) + self.down_leaving(g, d_sat + LD(tau_x))


def ground_points_under(orbit: Any, t: float) -> tuple[np.ndarray, np.ndarray]:
    """An emitter and a site a few degrees either side of the sub-satellite point (geocentric)."""
    r = np.asarray(ob.orbit_position(orbit, t))
    lat = math.degrees(math.asin(r[2] / np.linalg.norm(r)))
    lon = math.degrees(math.atan2(r[1], r[0]))
    lat = max(-80.0, min(80.0, lat))
    return gd.lla_to_ecef(lat + 3.0, lon - 4.0, 100.0), gd.lla_to_ecef(lat - 2.0, lon + 5.0, 30.0)


DELAY_TOL = 3e-16 if EXTENDED else 2e-15  # seconds: 0.1 micrometre of path


@pytest.mark.parametrize("name", list(all_orbit_types()))
def test_light_time_against_the_inertial_event_simulation(name: str) -> None:
    orbit, t0 = all_orbit_types()[name]
    sim = Inertial.of(orbit, t0)
    near_e, near_g = ground_points_under(orbit, t0)
    far_e, far_g = gd.lla_to_ecef(30.37, 140.41, 0.0), gd.lla_to_ecef(-33.9, 18.4, 1200.0)  # any geometry is evaluated
    in_table = 0.0 if name != "table" else None
    for x, g, tau_x, offset in ((near_e, near_g, 0.0, 0.0), (near_e, near_g, 1.7e-3, 0.3), (far_e, far_g, 2.5e-6, -0.25), (far_e, near_g, 0.0, 1.234567e-4)):
        if in_table is None and abs(offset) > 0.2:
            offset = 0.1
        tau_d, tau_u = sim.backward(x, g, tau_x, offset)
        got = gm.bent_pipe(x, orbit, g, t0, tau_x, offset)
        assert abs(got.tau_d - float(tau_d)) < DELAY_TOL
        assert abs(got.tau_u - float(tau_u)) < DELAY_TOL
        assert abs(got.D - float(tau_d + LD(tau_x) + tau_u)) < 2 * DELAY_TOL
        assert got.t_sat_s == pytest.approx(t0 + offset - float(tau_d) - tau_x, abs=3e-7)  # a rounded float
        assert abs(gm.downlink_delay(orbit, g, t0, offset) - float(tau_d)) < DELAY_TOL
        assert abs(gm.uplink_delay(x, orbit, t0, offset) - float(sim.up_arriving(x, LD(offset)))) < DELAY_TOL
        # the same event followed forwards arrives when it should
        d_sat, d_rx = sim.forward(x, g, tau_x, LD(offset) - LD(got.D))
        assert abs(float(d_rx - LD(offset))) < 2 * DELAY_TOL
        assert abs(float(d_sat - (LD(offset) - LD(got.tau_d) - LD(tau_x)))) < 2 * DELAY_TOL


class CircularOrbitStandIn:
    """A circular orbit written for this test only, defined in the inertial frame that
    coincides with ECEF at ``t_ref``. Implements just the documented protocol."""

    def __init__(self, a_m: float, inc_deg: float, node_deg: float, phase_deg: float, t_ref: float) -> None:
        self.a, self.t_ref = a_m, t_ref
        self.n = math.sqrt(GM / a_m**3)
        self.frame = Rotation.from_euler("zx", [node_deg, inc_deg], degrees=True).as_matrix().astype(LD)
        self.phase = math.radians(phase_deg)

    def inertial(self, d: Any) -> np.ndarray:
        u = LD(self.phase) + LD(self.n) * d
        return self.frame @ np.array([LD(self.a) * np.cos(u), LD(self.a) * np.sin(u), LD(0.0)], dtype=LD)

    def state_ecef(self, t_s: Any) -> tuple[np.ndarray, np.ndarray]:
        d = np.asarray(t_s, dtype=np.float64) - self.t_ref
        u = self.phase + self.n * d
        frame = self.frame.astype(np.float64)
        r_i = self.a * (np.cos(u)[..., None] * frame[:, 0] + np.sin(u)[..., None] * frame[:, 1])
        v_i = self.a * self.n * (-np.sin(u)[..., None] * frame[:, 0] + np.cos(u)[..., None] * frame[:, 1])
        c, s = np.cos(OMEGA_E * d), np.sin(OMEGA_E * d)
        r = np.stack([r_i[..., 0] * c + r_i[..., 1] * s, -r_i[..., 0] * s + r_i[..., 1] * c, r_i[..., 2]], axis=-1)
        v = np.stack([v_i[..., 0] * c + v_i[..., 1] * s, -v_i[..., 0] * s + v_i[..., 1] * c, v_i[..., 2]], axis=-1)
        v = v - np.stack([-OMEGA_E * r[..., 1], OMEGA_E * r[..., 0], np.zeros_like(c)], axis=-1)
        return r, v


@pytest.mark.parametrize("a_m, inc, tol", [(4.2164e7, 7.0, 2e-14), (2.0e7, 55.0, 3e-13), (7.0e6, 80.0, 2e-12)])
def test_light_time_with_a_protocol_only_orbit_and_no_shared_orbit_code(a_m: float, inc: float, tol: float) -> None:
    """Here neither side uses petal_locate.orbits: the simulation has the satellite in
    inertial coordinates analytically, the implementation only sees state_ecef(t_s).
    The tolerance is the satellite's motion during the 2.4e-7 s rounding of t - tau,
    which a protocol-only orbit cannot avoid."""
    t0 = 1.8e9
    orbit = CircularOrbitStandIn(a_m, inc, 40.0, 25.0, t0)
    sim = Inertial(orbit.inertial)
    x, g = ground_points_under(orbit, t0)
    tau_d, tau_u = sim.backward(x, g, 1.0e-3, 0.0)
    got = gm.bent_pipe(x, orbit, g, t0, 1.0e-3)
    assert abs(got.tau_d - float(tau_d)) < tol
    assert abs(got.tau_u - float(tau_u)) < tol
    assert abs(gm.uplink_delay(x, orbit, t0) - float(sim.up_arriving(x, LD(0.0)))) < DELAY_TOL  # no retarded satellite time
    # Sagnac matters: the non-rotating answer is wrong by far more than the tolerance
    static = np.linalg.norm(orbit.state_ecef(t0)[0] - x) / C
    assert abs(static - float(sim.up_arriving(x, LD(0.0)))) > 100 * tol


def test_math12_2_and_12_3_closed_forms() -> None:
    a = (GM / OMEGA_E**2) ** (1.0 / 3.0)
    geo = ob.GeoOrbit(0.0)
    under = gd.lla_to_ecef(0.0, 0.0, 0.0)
    tau = gm.uplink_delay(under, geo, T_GEO)
    assert (a - WGS84_A) == pytest.approx(35786035.37, abs=0.01)
    assert tau == pytest.approx(0.119369365075, abs=3e-12)
    # the 0.28 mm it differs by is the emitter's own motion during the flight: second order, positive
    assert 0.0 < tau - (a - WGS84_A) / C < 1.5e-12
    r = geo.position_ecef(T_GEO)
    east60 = gd.lla_to_ecef(0.0, 60.0, 0.0)  # 60 degrees east of the satellite
    up = gm.uplink_delay(east60, geo, T_GEO) - np.linalg.norm(r - east60) / C
    down = gm.downlink_delay(geo, east60, T_GEO) - np.linalg.norm(east60 - r) / C
    cross_z = east60[0] * r[1] - east60[1] * r[0]
    first_order = OMEGA_E * cross_z / C**2
    assert first_order == pytest.approx(-1.88964e-7, abs=5e-13)  # the value MATH.md quotes, to its 6 digits
    assert up == pytest.approx(first_order, abs=1e-12)
    assert down == pytest.approx(-first_order, abs=1e-12)
    # the second-order remainder MATH.md quotes (0.66 ps), same sign on both legs
    assert up - first_order == pytest.approx(0.66e-12, abs=0.02e-12)
    assert down + first_order == pytest.approx(0.66e-12, abs=0.02e-12)
    # MATH.md: up to 0.19 us per leg on GEO geometry
    worst = max(abs(gm.uplink_delay(gd.lla_to_ecef(0.0, lon, 0.0), geo, T_GEO) - np.linalg.norm(r - gd.lla_to_ecef(0.0, lon, 0.0)) / C) for lon in range(-80, 81, 5))
    assert 1.7e-7 < worst < 2.2e-7


RATE_TOL = 1e-15 if EXTENDED else 1e-14


@pytest.mark.parametrize("name", list(all_orbit_types()))
def test_rates_against_eighth_order_differentiation_of_the_event_simulation(name: str) -> None:
    orbit, t0 = all_orbit_types()[name]
    if name == "table":
        orbit = ob.TableOrbit(T_LEO + np.arange(-6, 7) * 5.0, *all_orbit_types()["kepler_leo"][0].state_ecef(T_LEO + np.arange(-6, 7) * 5.0))
    sim = Inertial.of(orbit, t0)
    x, g = ground_points_under(orbit, t0)
    tau_x = 1.7e-3
    geosynchronous = np.linalg.norm(ob.orbit_position(orbit, t0)) > 3e7
    h = 1.0 if geosynchronous else 0.125
    events = [sim.backward(x, g, tau_x, LD(k * h)) for k in K8]
    tau_d = np.array([e[0] for e in events], dtype=LD)
    total = np.array([e[0] + e[1] for e in events], dtype=LD)
    want_d_dot = float(np.dot(D8.astype(LD), total) / LD(h))
    want_tau_d_dot = float(np.dot(D8.astype(LD), tau_d) / LD(h))
    # uplink rate with respect to its own arrival time at the satellite
    d_sat = -events[4][0] - LD(tau_x)
    want_tau_u_dot = float(np.dot(D8.astype(LD), np.array([sim.up_arriving(x, d_sat + LD(k * h)) for k in K8], dtype=LD)) / LD(h))

    d_dot, tau_d_dot, tau_u_dot = gm.bent_pipe_rates(x, orbit, g, t0, tau_x)
    # the table is a piecewise cubic: its own 5th derivative limits a 5-point stencil
    tol = 5e-12 if name == "table" else (RATE_TOL if geosynchronous else 4e-14)
    assert abs(d_dot - want_d_dot) < tol
    assert abs(tau_d_dot - want_tau_d_dot) < tol
    assert abs(tau_u_dot - want_tau_u_dot) < tol
    assert d_dot == pytest.approx(tau_d_dot + tau_u_dot * (1.0 - tau_d_dot), abs=1e-18)
    if name in ("kepler_leo", "tle_leo", "perturbed"):
        # d tau_u / d t_sat is not d tau_u / d t_rx: they differ by the factor (1 - tau_d_dot),
        # which this comparison resolves
        assert abs(tau_u_dot * tau_d_dot) > 100 * tol


def test_rate_stencil_error_on_a_low_overhead_pass_is_quantified() -> None:
    """Worst case for the 5-point, 0.5 s stencil: a 600 km satellite passing overhead."""
    orbit = ob.KeplerOrbit(6.978e6, 0.0005, 97.8, 30.0, 40.0, 50.0, EPOCH_ISO)
    worst = 0.0
    for t0 in T_LEO + np.arange(0.0, 5800.0, 290.0):
        r = orbit.position_ecef(t0)
        lat, lon = math.degrees(math.asin(r[2] / np.linalg.norm(r))), math.degrees(math.atan2(r[1], r[0]))
        x = gd.lla_to_ecef(max(-85.0, min(85.0, lat)) + 0.3, lon + 0.3, 0.0)  # nearly at zenith
        g = gd.lla_to_ecef(max(-85.0, min(85.0, lat)) - 1.0, lon - 2.0, 0.0)
        sim = Inertial.of(orbit, float(t0))
        h = 0.0625
        total = np.array([sum(sim.backward(x, g, 0.0, LD(k * h))) for k in K8], dtype=LD)
        want = float(np.dot(D8.astype(LD), total) / LD(h))
        worst = max(worst, abs(gm.bent_pipe_rates(x, orbit, g, float(t0))[0] - want))
    # measured 6e-15 s/s (2e-6 Hz at 300 MHz); MATH.md quotes 1e-15 for geosynchronous geometry
    assert worst < 5e-14


def test_delay_is_smooth_over_a_capture_only_when_the_sample_offset_is_passed_separately() -> None:
    orbit = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 30.0, 40.0, 50.0, EPOCH_ISO)
    x, g = ground_points_under(orbit, T_LEO)
    offsets = np.arange(4096) / 31250.0
    xs = offsets / offsets[-1] * 2.0 - 1.0

    def roughness(delay: np.ndarray) -> float:
        fit = np.polynomial.chebyshev.Chebyshev.fit(xs, delay - delay[0], 5)
        return float(np.max(np.abs(delay - delay[0] - fit(xs))))

    split = gm.bent_pipe(x, orbit, g, T_LEO, 0.0, offsets).D
    summed = gm.bent_pipe(x, orbit, g, T_LEO + offsets).D
    assert roughness(split) < 1e-16  # 1e-8 cycles at 300 MHz
    assert roughness(summed) > 1e-13  # float64 seconds: 2.4e-7 s steps times 2e-5 s/s of delay rate
    assert split.shape == (4096,)


# ============================================================================ predictors


def measured_by_events(sim_a: Inertial, g_a: np.ndarray, a: PathSpec, sim_b: Inertial, g_b: np.ndarray, b: PathSpec, x: np.ndarray, h: float, *, true_translation: tuple[float, float] | None = None, eps_g: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float, float, float]:
    """What a correlator would measure, from two phase histories.

    One wavefront is followed from the emitter through each satellite to each site. The
    emission whose channel-B copy is received at offset 0 (t_mid) defines the TDOA: the
    reception time of its channel-A copy. Around it, nine emissions ``h`` apart give
    each channel's phase history in cycles,

        phase = f_e t_emit + T t_sat - f_tune (1 + eps_g) t_rx

    (emitter phase when emitted, transponder oscillator phase when the signal passes
    through, receiver oscillator phase when received), and the baseband frequency is
    d phase / d t_rx. Returns (tdoa, fdoa, f_bb_a, f_bb_b)."""
    d_emit0 = LD(brentq(lambda v: float(sim_b.forward(x, g_b, b.tau_x_s, LD(v))[1]), -1.5, 0.0, xtol=1e-17, rtol=1e-15))
    for _ in range(2):  # polish: d t_rx / d t_emit is 1 to within 3e-5
        d_emit0 = d_emit0 - sim_b.forward(x, g_b, b.tau_x_s, d_emit0)[1]
    translations = true_translation or (a.translation_hz, b.translation_hz)
    out = []
    for sim, g, path, translation, eps in ((sim_a, g_a, a, translations[0], eps_g[0]), (sim_b, g_b, b, translations[1], eps_g[1])):
        events = [sim.forward(x, g, path.tau_x_s, d_emit0 + LD(k * h)) for k in K8]
        d_emit = d_emit0 + K8.astype(LD) * LD(h)
        d_sat = np.array([e[0] for e in events], dtype=LD)
        d_rx = np.array([e[1] for e in events], dtype=LD)
        f_tune = LD(path.f_e_hz) + LD(path.translation_hz)  # tuned to the nominal downlink
        # the same phase with the common f * t_rx removed, so nothing large is differenced
        phase = -LD(path.f_e_hz) * (d_rx - d_emit) - LD(translation) * (d_rx - d_sat)
        offset = (LD(path.f_e_hz) + LD(translation)) - f_tune * (1 + LD(eps))
        d_phase = np.dot(D8.astype(LD), phase) / LD(h)
        d_rx_dot = 1 + np.dot(D8.astype(LD), d_rx - d_emit) / LD(h)
        out.append((d_rx[4], offset + d_phase / d_rx_dot))
    return float(out[0][0] - out[1][0]), float(out[0][1] - out[1][1]), float(out[0][1]), float(out[1][1])


SITES = {"hnl": gd.lla_to_ecef(21.3, -157.8, 20.0), "tyo": gd.lla_to_ecef(35.68, 139.77, 50.0)}
EMITTER = gd.lla_to_ecef(30.37, 140.41, 0.0)
PREDICT_ORBITS: dict[str, Any] = {
    "s1": ob.GeoOrbit(-177.0, 5.0, EPOCH_ISO, 40.0),
    "s2": ob.GeoOrbit(172.0, 4.0, EPOCH_ISO, 200.0),
    "s3": ob.GeoOrbit(75.0, 5.0, EPOCH_ISO, 310.0),
    "l1": ob.KeplerOrbit(7.0e6, 0.001, 98.0, 30.0, 40.0, 50.0, EPOCH_ISO),
    "l2": ob.KeplerOrbit(7.1e6, 0.002, 60.0, 100.0, 10.0, 200.0, EPOCH_ISO),
    "t1": ob.TleOrbit(*TLE_GEO_LINES),
    "t2": ob.TleOrbit(*TLE_LEO_LINES),
}
PREDICT_CASES = [
    ("geo-geo", PathSpec("s1", "hnl", 300e6, -41e6, 1e-3), PathSpec("s2", "tyo", 300e6, -33.6e6, 2e-3), T_GEO),
    ("geo-geo-swapped", PathSpec("s2", "tyo", 300e6, -33.6e6, 2e-3), PathSpec("s1", "hnl", 300e6, -41e6, 1e-3), T_GEO),
    ("geo-geo-one-site", PathSpec("s1", "hnl", 300e6, -41e6), PathSpec("s3", "hnl", 300e6, -41e6), T_GEO),
    ("geo-geo-ku", PathSpec("s1", "hnl", 14.1e9, -2.3e9), PathSpec("s3", "tyo", 14.1e9, -1.75e9, 5e-4), T_GEO),
    ("leo-leo", PathSpec("l1", "hnl", 300e6, -41e6, 1e-3), PathSpec("l2", "tyo", 300e6, -33.6e6), T_LEO),
    ("leo-leo-upconverting", PathSpec("l2", "tyo", 300e6, -33.6e6, 1e-3), PathSpec("l1", "hnl", 300e6, 41e6), T_LEO),
    ("leo-geo", PathSpec("l1", "hnl", 300e6, -41e6, 1e-3), PathSpec("s2", "tyo", 300e6, -33.6e6), T_LEO),
    ("tle-geo-leo", PathSpec("t1", "hnl", 300e6, -41e6, 1e-3), PathSpec("t2", "tyo", 300e6, -33.6e6), TLE_EPOCH + 5000.0),
    ("tle-leo-geo", PathSpec("t2", "hnl", 300e6, -41e6, 1e-3), PathSpec("t1", "tyo", 300e6, -33.6e6), TLE_EPOCH + 5000.0),
]


@pytest.mark.parametrize("case", PREDICT_CASES, ids=lambda c: c[0])
def test_predict_tdoa_and_fdoa_against_two_simulated_phase_histories(case: tuple[str, PathSpec, PathSpec, float]) -> None:
    _, a, b, t_mid = case
    sim_a, sim_b = Inertial.of(PREDICT_ORBITS[a.sat_id], t_mid), Inertial.of(PREDICT_ORBITS[b.sat_id], t_mid)
    low = min(np.linalg.norm(ob.orbit_position(PREDICT_ORBITS[p.sat_id], t_mid)) for p in (a, b)) < 3e7
    tdoa, fdoa, _, _ = measured_by_events(sim_a, SITES[a.site_id], a, sim_b, SITES[b.site_id], b, EMITTER, 0.125 if low else 2.0)
    got_tdoa = gm.predict_tdoa(EMITTER, a, b, t_mid, PREDICT_ORBITS, SITES)
    got_fdoa = gm.predict_fdoa(EMITTER, a, b, t_mid, PREDICT_ORBITS, SITES)
    pair = gm.predict_pair(EMITTER, a, b, t_mid, PREDICT_ORBITS, SITES)
    assert (pair[0], pair[1]) == (got_tdoa, got_fdoa)
    # MATH.md section 5 prescribes one fixed-point pass for the epoch shift of path A:
    # what is left is D_dot_A^2 * TDOA (56 ps for the LEO-GEO pair, nothing for GEO)
    d_dot_a = gm.bent_pipe_rates(EMITTER, PREDICT_ORBITS[a.sat_id], SITES[a.site_id], t_mid, a.tau_x_s)[0]
    assert abs(got_tdoa - tdoa) < 1.5 * d_dot_a**2 * abs(tdoa) + 4 * DELAY_TOL
    assert abs(got_tdoa) > 1e-3 and abs(got_fdoa) > 1.0  # the comparison is not vacuous
    # 3e-15 of the carrier: 1e-6 Hz at 300 MHz
    assert abs(got_fdoa - fdoa) < 3e-15 * a.f_e_hz + (1e-7 if low else 0.0)


def test_swapping_the_channels_flips_both_signs_up_to_the_epoch_shift() -> None:
    _, a, b, t_mid = PREDICT_CASES[0]
    ab = gm.predict_pair(EMITTER, a, b, t_mid, PREDICT_ORBITS, SITES)
    ba = gm.predict_pair(EMITTER, b, a, t_mid, PREDICT_ORBITS, SITES)
    assert ab[0] < 0.0 < ba[0] and ab[1] > 0.0 > ba[1]
    # A is evaluated at t_mid + TDOA, so the two differ by rate * TDOA, exactly as two captures would
    d_dot = [gm.bent_pipe_rates(EMITTER, PREDICT_ORBITS[p.sat_id], SITES[p.site_id], t_mid, p.tau_x_s)[0] for p in (a, b)]
    assert ab[0] + ba[0] == pytest.approx(ab[0] * (d_dot[0] - d_dot[1]), rel=1e-3, abs=1e-18)
    assert abs(ab[1] + ba[1]) < 1e-4


def test_baseband_frequency_against_the_phase_history_with_oscillator_errors() -> None:
    """MATH.md section 4 formula, its signs and section 12 item 6 (satellite oscillator)."""
    _, a, b, t_mid = PREDICT_CASES[4]  # low orbit: every term matters
    sims = [Inertial.of(PREDICT_ORBITS[p.sat_id], t_mid) for p in (a, b)]
    eps_sat, eps_g = (3e-7, -2e-7), (4e-9, -1e-9)
    true_t = (a.translation_hz * (1 + eps_sat[0]), b.translation_hz * (1 + eps_sat[1]))
    tdoa, fdoa, f_a, f_b = measured_by_events(sims[0], SITES[a.site_id], a, sims[1], SITES[b.site_id], b, EMITTER, 0.125, true_translation=true_t, eps_g=eps_g)
    # channel B is observed at t_mid, channel A at t_mid + TDOA
    _, tau_d_dot_b, tau_u_dot_b = gm.bent_pipe_rates(EMITTER, PREDICT_ORBITS[b.sat_id], SITES[b.site_id], t_mid, b.tau_x_s)
    _, tau_d_dot_a, tau_u_dot_a = gm.bent_pipe_rates(EMITTER, PREDICT_ORBITS[a.sat_id], SITES[a.site_id], t_mid, a.tau_x_s, tdoa)
    got_a = gm.baseband_frequency(a.f_e_hz, true_t[0], a.f_tune_hz, tau_d_dot_a, tau_u_dot_a, eps_g[0])
    got_b = gm.baseband_frequency(b.f_e_hz, true_t[1], b.f_tune_hz, tau_d_dot_b, tau_u_dot_b, eps_g[1])
    assert got_a == pytest.approx(f_a, abs=2e-6)
    assert got_b == pytest.approx(f_b, abs=2e-6)
    assert abs(f_a) > 100.0 and abs(f_b) > 100.0
    # without the errors the difference is the predictor
    nominal = gm.predict_fdoa(EMITTER, a, b, t_mid, PREDICT_ORBITS, SITES)
    shift = (got_a - got_b) - nominal
    want = eps_sat[0] * a.translation_hz * (1 - tau_d_dot_a) - eps_sat[1] * b.translation_hz * (1 - tau_d_dot_b) - eps_g[0] * a.f_tune_hz + eps_g[1] * b.f_tune_hz
    assert shift == pytest.approx(want, abs=1e-6)
    # item 6: raising eps_sat of satellite i by eps raises FDOA_AB by eps * T_nom,i
    base = gm.baseband_frequency(a.f_e_hz, a.translation_hz, a.f_tune_hz, tau_d_dot_a, tau_u_dot_a)
    raised = gm.baseband_frequency(a.f_e_hz, a.translation_hz * (1 + 1e-6), a.f_tune_hz, tau_d_dot_a, tau_u_dot_a)
    assert raised - base == pytest.approx(1e-6 * a.translation_hz, rel=1e-4)
    assert gm.baseband_frequency(np.full(3, 3e8), -4e7, 2.6e8, np.zeros((2, 1)), 0.0).shape == (2, 3)


def test_self_peak_against_the_event_simulation_of_one_satellite_into_two_channels() -> None:
    a = PathSpec("s1", "hnl", 300.00e6, -41e6, 1e-3)  # the pair being correlated: tunings differ
    b = PathSpec("s2", "tyo", 300.02e6, -33.6e6, 2e-3)
    for sat, translation, t_mid, h, tol in (("s1", -41e6, T_GEO, 2.0, 2e-6), ("s2", -33.6e6, T_GEO, 2.0, 2e-6), ("l1", -41e6, T_LEO, 0.125, 0.05)):
        f_e = 300.01e6
        sim = Inertial.of(PREDICT_ORBITS[sat], t_mid)
        # both copies go through the same transponder: its delay (any value) must cancel
        copy_a = PathSpec(sat, a.site_id, f_e, translation, 0.7e-3)
        copy_b = PathSpec(sat, b.site_id, f_e, translation, 0.7e-3)
        tdoa, fdoa, _, _ = measured_by_events(sim, SITES[a.site_id], copy_a, sim, SITES[b.site_id], copy_b, EMITTER, h)
        fdoa += b.f_tune_hz - a.f_tune_hz  # channels are tuned to a and b, not to this satellite's downlink
        got = gm.self_peak(sat, translation, f_e, a, b, t_mid, PREDICT_ORBITS, SITES)
        assert got[0] == pytest.approx(tdoa, abs=4 * DELAY_TOL)
        # in low orbit the emitter-dependent term f_e tau_u_dot (tau_d_dot_a - tau_d_dot_b) is not
        # representable in this signature: millihertz, far inside a CAF main lobe
        assert got[1] == pytest.approx(fdoa, abs=tol)
    many = gm.self_peak("s1", -41e6, 3e8, a, b, T_GEO + np.arange(4.0), PREDICT_ORBITS, SITES)
    assert many[0].shape == (4,) and many[1].shape == (4,)


def test_shapes_broadcast_as_documented_and_match_scalar_calls() -> None:
    """Emitter axes [..., 3] against time [M], including sizes chosen to collide (M = 3, M = N)."""
    rng = np.random.default_rng(301)
    a, b = PREDICT_CASES[0][1], PREDICT_CASES[0][2]
    orbit, site = PREDICT_ORBITS["s1"], SITES["hnl"]
    for x_shape in [(), (3,), (5,), (3, 3), (2, 5), (1,), (4, 1)]:
        x = gd.lla_to_ecef(rng.uniform(10, 40, x_shape), rng.uniform(130, 170, x_shape), 0.0)
        for m in (None, 1, 3, 5):
            t = T_GEO if m is None else T_GEO + np.arange(m) * 10.0
            t_shape = () if m is None else (m,)
            want = x_shape + t_shape
            pipe = gm.bent_pipe(x, orbit, site, t, 1e-3)
            rates = gm.bent_pipe_rates(x, orbit, site, t, 1e-3)
            tdoa = gm.predict_tdoa(x, a, b, t, PREDICT_ORBITS, SITES)
            fdoa = gm.predict_fdoa(x, a, b, t, PREDICT_ORBITS, SITES)
            for value in (gm.uplink_delay(x, orbit, t), pipe.D, pipe.tau_u, rates[0], rates[2], tdoa, fdoa, gm.elevation_deg(x, orbit, t)):
                assert np.shape(value) == want
            for value in (pipe.tau_d, pipe.t_sat_s, rates[1], gm.downlink_delay(orbit, site, t)):
                assert np.shape(value) == t_shape
            flat_x, flat_t = x.reshape(-1, 3), np.atleast_1d(t)
            loop_f = np.array([[gm.predict_fdoa(p, a, b, float(q), PREDICT_ORBITS, SITES) for q in flat_t] for p in flat_x]).reshape(want)
            loop_t = np.array([[gm.predict_tdoa(p, a, b, float(q), PREDICT_ORBITS, SITES) for q in flat_t] for p in flat_x]).reshape(want)
            loop_d = np.array([[gm.bent_pipe(p, orbit, site, float(q), 1e-3).D for q in flat_t] for p in flat_x]).reshape(want)
            assert np.max(np.abs(loop_f - fdoa), initial=0.0) < 1e-9
            assert np.max(np.abs(loop_t - tdoa), initial=0.0) < 1e-16
            assert np.max(np.abs(loop_d - pipe.D), initial=0.0) < 1e-16
    assert gm.downlink_delay(orbit, np.stack([site] * 4), T_GEO + np.arange(3.0)).shape == (4, 3)
    assert gm.uplink_delay(EMITTER, orbit, T_GEO, np.arange(6) * 1e-5).shape == (6,)
    assert isinstance(gm.predict_tdoa(EMITTER, a, b, T_GEO, PREDICT_ORBITS, SITES), float)
    assert gm.PathSpec is PathSpec


def test_grid_evaluation_is_vectorised() -> None:
    lat, lon = np.meshgrid(np.arange(-70.0, 70.5, 1.0), np.arange(-180.0, 180.0, 1.0), indexing="ij")
    pts = gd.lla_to_ecef(lat, lon, 0.0)
    a, b = PREDICT_CASES[0][1], PREDICT_CASES[0][2]
    best = np.inf
    for _ in range(3):
        start = time.perf_counter()
        tdoa, fdoa = gm.predict_pair(pts, a, b, T_GEO + np.array([0.0, 5.0]), PREDICT_ORBITS, SITES)
        best = min(best, time.perf_counter() - start)
    assert tdoa.shape == fdoa.shape == (141, 360, 2)
    # 50 760 points x 2 epochs: about 0.06 s vectorised, a minute as scalar calls
    assert best < 2.0
    k = (37, 211)
    assert tdoa[k][1] == pytest.approx(gm.predict_tdoa(pts[k], a, b, T_GEO + 5.0, PREDICT_ORBITS, SITES), abs=1e-16)


# ============================================================================ visibility and windows


def elevation_reference(lat: float, lon: float, alt: float, sat: np.ndarray) -> float:
    """Elevation from the ellipsoid normal obtained by differencing the geodetic map in height."""
    p = lla_to_ecef_ld(lat, lon, alt)
    up = lla_to_ecef_ld(lat, lon, alt + 1.0) - p
    d = sat.astype(LD) - p
    return float(np.degrees(np.arcsin(np.dot(d, up) / np.sqrt(np.dot(d, d)))))


def test_elevation_against_an_independent_normal_and_closed_forms() -> None:
    rng = np.random.default_rng(401)
    for name in ("s1", "l1"):
        orbit = PREDICT_ORBITS[name]
        t = T_GEO + np.array([0.0, 900.0])
        sats = orbit.position_ecef(t)
        lat, lon, alt = rng.uniform(-90, 90, 60), rng.uniform(-180, 180, 60), rng.uniform(0, 5000, 60)
        got = gm.elevation_deg(gd.lla_to_ecef(lat, lon, alt), orbit, t)
        assert got.shape == (60, 2)
        want = np.array([[elevation_reference(la, lo, al, s) for s in sats] for la, lo, al in zip(lat, lon, alt)])
        assert np.max(np.abs(got - want)) < 1e-9
    geo = ob.GeoOrbit(100.0)
    a = (GM / OMEGA_E**2) ** (1.0 / 3.0)
    horizon = math.degrees(math.acos(WGS84_A / a))  # 81.3 degrees of longitude along the equator
    assert gm.elevation_deg(gd.lla_to_ecef(0.0, 100.0), geo, T_GEO) == pytest.approx(90.0, abs=1e-9)
    assert gm.elevation_deg(gd.lla_to_ecef(0.0, 100.0 + horizon), geo, T_GEO) == pytest.approx(0.0, abs=1e-9)
    assert gm.elevation_deg(gd.lla_to_ecef(0.0, 100.0 - horizon - 1.0), geo, T_GEO) < 0.0
    assert gm.elevation_deg(gd.lla_to_ecef(0.0, -80.0), geo, T_GEO) == pytest.approx(-90.0, abs=1e-9)  # antipode of the slot


@pytest.mark.parametrize("name, t, mask", [("geo", T_GEO, 0.0), ("geo", T_GEO, 10.0), ("s1", T_GEO, 5.0), ("l1", T_GEO, 0.0), ("l1", T_GEO + 1300.0, 20.0)])
def test_footprint_ring_is_the_elevation_contour(name: str, t: float, mask: float) -> None:
    orbit = ob.GeoOrbit(100.0) if name == "geo" else PREDICT_ORBITS[name]
    ring = np.array(gm.footprint(orbit, t, mask, 181))
    assert ring.shape == (181, 2)
    sat = orbit.position_ecef(t)
    elevations = np.array([elevation_reference(la, lo, 0.0, sat) for la, lo in ring])
    assert np.max(np.abs(elevations - mask)) < 1e-6
    assert ring[0, 0] == ring[-1, 0] and (ring[0, 1] - ring[-1, 1]) % 360.0 == 0.0  # closed
    assert np.max(np.abs(np.diff(ring[:, 1]))) < 180.0  # continuous
    _, sub_lon, _ = ob.subsatellite_point(orbit, t)
    assert ring[0, 1] == pytest.approx(sub_lon, abs=1e-9)  # starts due north of the sub-satellite point
    assert ring[1, 1] < ring[0, 1]  # and runs west first
    # the sub-satellite point is inside: every vertex is on the far side of the contour from it
    inside = gd.lla_to_ecef(*ob.subsatellite_point(orbit, t)[:2], 0.0)
    assert gm.elevation_deg(inside, orbit, t) > mask
    if name == "geo" and mask == 0.0:
        horizon = math.degrees(math.acos(WGS84_A / np.linalg.norm(sat)))
        assert ring[:, 1].min() == pytest.approx(100.0 - horizon, abs=1e-6)
        assert ring[:, 1].max() == pytest.approx(100.0 + horizon, abs=1e-6)
        assert ring[:, 0].max() == pytest.approx(-ring[:, 0].min(), abs=1e-9)


def test_footprint_containing_a_pole_and_validation() -> None:
    polar = ob.KeplerOrbit(7.2e6, 0.0, 90.0, 0.0, 0.0, 90.0, EPOCH_ISO, j2=False)  # over the north pole at the epoch
    ring = np.array(gm.footprint(polar, EPOCH, 0.0, 73))
    sat = polar.position_ecef(EPOCH)
    assert abs(ring[-1, 1] - ring[0, 1]) == pytest.approx(360.0, abs=1e-6)  # wraps all the way round
    assert np.max(np.abs([elevation_reference(la, lo, 0.0, sat) for la, lo in ring])) < 1e-6
    assert np.all(ring[:, 0] > 55.0)
    for kwargs, name in (({"n": 3}, "n"), ({"n": 10.5}, "n"), ({"min_elevation_deg": 90.0}, "min_elevation_deg"), ({"min_elevation_deg": np.nan}, "min_elevation_deg")):
        with pytest.raises(ValueError, match=name):
            gm.footprint(polar, EPOCH, **kwargs)
    with pytest.raises(ValueError, match="t_s"):
        gm.footprint(polar, np.array([EPOCH, EPOCH]))  # type: ignore[arg-type]


class BoxRegion:
    """Local stand-in for the configuration Region (ARCHITECTURE.md section 3)."""

    def __init__(self, lat_min: float = -70.0, lat_max: float = 70.0, lon_min: float = -180.0, lon_max: float = 180.0) -> None:
        self.lat_min, self.lat_max, self.lon_min, self.lon_max = lat_min, lat_max, lon_min, lon_max

    def lon_span_deg(self) -> float:
        full = self.lon_min == -180.0 and self.lon_max == 180.0
        return 360.0 if full else (self.lon_max - self.lon_min) % 360.0

    def contains(self, lat_deg: Any, lon_deg: Any) -> np.ndarray:
        lat, lon = np.asarray(lat_deg, dtype=float), np.asarray(lon_deg, dtype=float)
        return (lat >= self.lat_min) & (lat <= self.lat_max) & (np.mod(lon - self.lon_min, 360.0) <= self.lon_span_deg())

    def grid(self, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
        lat = self.lat_min + step_deg * np.arange(int(math.floor((self.lat_max - self.lat_min) / step_deg + 1e-9)) + 1)
        span = self.lon_span_deg()
        count = int(math.floor(span / step_deg + 1e-9)) + (0 if span >= 360.0 else 1)
        return lat, self.lon_min + step_deg * np.arange(count)


WINDOW_A = PathSpec("s1", "hnl", 300e6, -41e6, 1e-3)
WINDOW_B = PathSpec("s2", "tyo", 300e6, -33.6e6, 2e-3)
WINDOW_C = PathSpec("s3", "tyo", 300e6, -41e6)


@pytest.mark.parametrize(
    "a, b, region, mask, height, step",
    [
        (WINDOW_A, WINDOW_B, BoxRegion(), 0.0, 0.0, 2.0),
        (WINDOW_A, WINDOW_C, BoxRegion(-60, 60, 60, -120), 0.0, 0.0, 2.0),  # wraps the antimeridian
        (WINDOW_A, WINDOW_C, BoxRegion(-60, 60, 60, -120), 5.0, 100.0, 5.0),
        (WINDOW_A, WINDOW_B, BoxRegion(20, 31.3, 130, 141.7), 0.0, 0.0, 2.0),  # edges off the grid
        (WINDOW_A, WINDOW_B, BoxRegion(20, 21, 130, 130.5), 0.0, 0.0, 2.0),  # smaller than one cell
        (WINDOW_B, WINDOW_A, BoxRegion(-70, 70, 100, 250), 40.0, 0.0, 3.0),  # high mask: a small patch
    ],
)
def test_measurement_windows_bound_every_admissible_emitter(a: PathSpec, b: PathSpec, region: BoxRegion, mask: float, height: float, step: float) -> None:
    rng = np.random.default_rng(402)
    lat = np.degrees(np.arcsin(rng.uniform(math.sin(math.radians(region.lat_min)), math.sin(math.radians(region.lat_max)), 6000)))
    lon = region.lon_min + rng.uniform(0.0, region.lon_span_deg(), 6000)
    x = gd.lla_to_ecef(lat, lon, height)
    sat_a, sat_b = (PREDICT_ORBITS[p.sat_id].position_ecef(T_GEO) for p in (a, b))
    visible = np.array([min(elevation_reference(la, lo, height, sat_a), elevation_reference(la, lo, height, sat_b)) >= mask for la, lo in zip(lat[:1500], lon[:1500])])
    x = x[:1500][visible]
    assert x.shape[0] > 20
    tdoa = gm.predict_tdoa(x, a, b, T_GEO, PREDICT_ORBITS, SITES)
    fdoa = gm.predict_fdoa(x, a, b, T_GEO, PREDICT_ORBITS, SITES)
    windows = gm.measurement_windows(a, b, T_GEO, PREDICT_ORBITS, SITES, region, mask, height, step)
    assert windows is not None
    (t_lo, t_hi), (f_lo, f_hi) = windows
    assert t_lo <= tdoa.min() and tdoa.max() <= t_hi
    assert f_lo <= fdoa.min() and fdoa.max() <= f_hi
    # and not uselessly wide: within a cell's worth of the sampled extremes
    assert (t_hi - t_lo) < (tdoa.max() - tdoa.min()) + 3e-3 * step
    assert (f_hi - f_lo) < (fdoa.max() - fdoa.min()) + 1.0 * step


def test_measurement_windows_none_only_for_a_real_absence_and_errors_for_bad_arguments() -> None:
    args = (WINDOW_A, WINDOW_B, T_GEO, PREDICT_ORBITS, SITES)
    # the Atlantic sees neither Pacific satellite
    assert gm.measurement_windows(*args, BoxRegion(-70, 70, -60, 30)) is None
    several = gm.measurement_windows(WINDOW_A, WINDOW_B, T_GEO + np.array([0.0, 3600.0, 7200.0]), PREDICT_ORBITS, SITES, BoxRegion())
    singles = [gm.measurement_windows(WINDOW_A, WINDOW_B, T_GEO + d, PREDICT_ORBITS, SITES, BoxRegion()) for d in (0.0, 3600.0, 7200.0)]
    assert several is not None and all(s is not None for s in singles)
    assert several[0][0] <= min(s[0][0] for s in singles) + 1e-9 and several[0][1] >= max(s[0][1] for s in singles) - 1e-9
    assert several[1][0] <= min(s[1][0] for s in singles) + 1e-6 and several[1][1] >= max(s[1][1] for s in singles) - 1e-6
    # NaN used to compare false everywhere and come back as None, "no admissible point"
    for kwargs, name in (({"min_elevation_deg": np.nan}, "min_elevation_deg"), ({"min_elevation_deg": 95.0}, "min_elevation_deg"), ({"height_m": np.nan}, "height_m"), ({"step_deg": 0.0}, "step_deg"), ({"step_deg": np.nan}, "step_deg")):
        with pytest.raises(ValueError, match=name):
            gm.measurement_windows(*args, BoxRegion(), **kwargs)
    with pytest.raises(ValueError, match="a.sat_id"):
        gm.measurement_windows(PathSpec("zz", "hnl", 3e8, -4e7), WINDOW_B, T_GEO, PREDICT_ORBITS, SITES, BoxRegion())


def test_search_windows_by_hand_and_validation() -> None:
    unc = Uncertainty(site_clock_s={"hnl": 30e-9, "tyo": 40e-9}, site_osc_frac={"hnl": 1e-11, "tyo": 2e-11}, sat_osc_frac={"s1": 1e-6, "s2": 2e-6}, tdoa_bias_s=50e-9)
    caf = SimpleNamespace(delay_margin_s=20e-6, doppler_margin_hz=20.0)
    geo = ((-1e-3, 2e-3), (-5.0, 7.0))
    a, b = WINDOW_A, WINDOW_B
    pad_t = 20e-6 + 3 * math.sqrt(30e-9**2 + 40e-9**2 + 50e-9**2)
    pad_f = 20.0 + 3 * math.sqrt((1e-6 * 41e6) ** 2 + (2e-6 * 33.6e6) ** 2 + (1e-11 * 259e6) ** 2 + (2e-11 * 266.4e6) ** 2)
    delay, doppler = gm.search_windows(geo, a, b, caf, unc)
    assert delay == pytest.approx((-1e-3 - pad_t, 2e-3 + pad_t), rel=1e-12)
    assert doppler == pytest.approx((-5.0 - pad_f, 7.0 + pad_f), rel=1e-12)
    # no uncertainty: fixed margins only; a mapping config works
    delay, doppler = gm.search_windows(geo, a, b, {"delay_margin_s": 20e-6, "doppler_margin_hz": 20.0}, None)
    assert delay == pytest.approx((-1.02e-3, 2.02e-3), rel=1e-12) and doppler == pytest.approx((-25.0, 27.0), rel=1e-12)
    # a valid calibration shifts its window by the correction (z = h + c) and drops the 3 sigma term
    delay, doppler = gm.search_windows(geo, a, b, caf, unc, (1e-4, None))
    assert delay == pytest.approx((-1e-3 + 1e-4 - 20e-6, 2e-3 + 1e-4 + 20e-6), rel=1e-12)
    assert doppler == pytest.approx((-5.0 - pad_f, 7.0 + pad_f), rel=1e-12)
    delay, doppler = gm.search_windows(geo, a, b, caf, unc, {"fdoa": -3.0})
    assert doppler == pytest.approx((-28.0, 24.0), rel=1e-12) and delay == pytest.approx((-1e-3 - pad_t, 2e-3 + pad_t), rel=1e-12)
    # one site, one satellite, two transponders: only what does not cancel in the measurement
    same = gm.search_windows(geo, PathSpec("s1", "hnl", 300e6, -41e6), PathSpec("s1", "hnl", 300e6, -33.6e6), caf, unc)
    assert same[0] == pytest.approx((-1e-3 - 20e-6 - 150e-9, 2e-3 + 20e-6 + 150e-9), rel=1e-12)
    pad = 20.0 + 3 * math.sqrt((1e-6 * 7.4e6) ** 2 + (1e-11 * 7.4e6) ** 2)
    assert same[1] == pytest.approx((-5.0 - pad, 7.0 + pad), rel=1e-12)
    # non-finite input used to come back as NaN or infinite windows
    for call, name in (
        (lambda: gm.search_windows(geo, a, b, caf, Uncertainty(tdoa_bias_s=np.nan)), "tdoa_bias_s"),
        (lambda: gm.search_windows(geo, a, b, caf, Uncertainty(sat_osc_frac={"s1": np.nan})), "sat_osc_frac"),
        (lambda: gm.search_windows(geo, a, b, caf, Uncertainty(site_clock_s={"tyo": np.inf})), "site_clock_s"),
        (lambda: gm.search_windows(geo, a, b, caf, Uncertainty(site_osc_frac={"hnl": np.nan})), "site_osc_frac"),
        (lambda: gm.search_windows(geo, PathSpec("s1", "hnl", 3e8, np.nan), b, caf, unc), "a.translation_hz"),
        (lambda: gm.search_windows(((0.0, np.inf), (0.0, 1.0)), a, b, caf, None), "geo_windows"),
        (lambda: gm.search_windows(((np.nan, 1.0), (0.0, 1.0)), a, b, caf, None), "geo_windows"),
        (lambda: gm.search_windows(((1.0, 0.0), (0.0, 1.0)), a, b, caf, None), "geo_windows"),
        (lambda: gm.search_windows(None, a, b, caf, None), "geo_windows"),  # type: ignore[arg-type]
        (lambda: gm.search_windows(geo, a, b, caf, None, (np.nan, None)), "correction"),
        (lambda: gm.search_windows(geo, a, b, SimpleNamespace(delay_margin_s=-1.0, doppler_margin_hz=1.0), None), "delay_margin_s"),
        (lambda: gm.search_windows(geo, a, b, SimpleNamespace(delay_margin_s=1.0), None), "doppler_margin_hz"),
    ):
        with pytest.raises(ValueError, match=name):
            call()


def test_geometry_rejects_bad_input_naming_the_field() -> None:
    orbit, site = PREDICT_ORBITS["s1"], SITES["hnl"]
    a, b = WINDOW_A, WINDOW_B
    cases: list[tuple[Callable[[], Any], str]] = [
        (lambda: gm.uplink_delay([np.nan, 0.0, 0.0], orbit, T_GEO), "x_e"),
        (lambda: gm.uplink_delay([1.0, 2.0], orbit, T_GEO), "x_e"),
        (lambda: gm.uplink_delay(EMITTER, orbit, np.nan), "t_sat_s"),
        (lambda: gm.uplink_delay(EMITTER, orbit, np.zeros((2, 2))), "t_sat_s"),
        (lambda: gm.downlink_delay(orbit, [0.0, np.inf, 0.0], T_GEO), "x_g"),
        (lambda: gm.bent_pipe(EMITTER, orbit, np.zeros((2, 3)), T_GEO), "x_g"),
        (lambda: gm.bent_pipe(EMITTER, orbit, site, T_GEO, np.nan), "tau_x"),
        (lambda: gm.bent_pipe(EMITTER, orbit, site, T_GEO, 0.0, np.nan), "dt_s"),
        (lambda: gm.bent_pipe(EMITTER, orbit, site, T_GEO + np.arange(3.0), 0.0, np.zeros(2)), "dt_s"),
        (lambda: gm.predict_tdoa(EMITTER, PathSpec("zz", "hnl", 3e8, -4e7), b, T_GEO, PREDICT_ORBITS, SITES), "a.sat_id"),
        (lambda: gm.predict_tdoa(EMITTER, a, PathSpec("s2", "zz", 3e8, -4e7), T_GEO, PREDICT_ORBITS, SITES), "b.site_id"),
        (lambda: gm.predict_tdoa(EMITTER, PathSpec("s1", "hnl", 3e8, -4e7, np.nan), b, T_GEO, PREDICT_ORBITS, SITES), "a.tau_x_s"),
        # these two used to return NaN
        (lambda: gm.predict_fdoa(EMITTER, PathSpec("s1", "hnl", np.nan, -4e7), b, T_GEO, PREDICT_ORBITS, SITES), "a.f_e_hz"),
        (lambda: gm.predict_pair(EMITTER, a, PathSpec("s2", "tyo", 3e8, np.inf), T_GEO, PREDICT_ORBITS, SITES), "b.translation_hz"),
        (lambda: gm.self_peak("s1", np.nan, 3e8, a, b, T_GEO, PREDICT_ORBITS, SITES), "translation_hz"),
        (lambda: gm.self_peak("s1", -4.1e7, np.nan, a, b, T_GEO, PREDICT_ORBITS, SITES), "f_e_hz"),
        (lambda: gm.self_peak("zz", -4.1e7, 3e8, a, b, T_GEO, PREDICT_ORBITS, SITES), "sat_id"),
        (lambda: gm.elevation_deg([0.0, 0.0], orbit, T_GEO), "x"),
    ]
    for call, name in cases:
        with pytest.raises(ValueError, match=name):
            call()
    # TDOA does not need the frequencies and must not start requiring them
    assert np.isfinite(gm.predict_tdoa(EMITTER, PathSpec("s1", "hnl", np.nan, np.nan), b, T_GEO, PREDICT_ORBITS, SITES))
