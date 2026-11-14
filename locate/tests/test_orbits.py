"""orbits: every orbit type against closed forms, SGP4 itself, or numerical integration.

Includes MATH.md section 12 item 1.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, Literal

import numpy as np
import pytest
from pydantic import BaseModel
from scipy.integrate import solve_ivp
from sgp4.api import Satrec
from sgp4.propagation import gstime

from petal_locate.core import orbits as ob
from petal_locate.core.constants import GM, J2, JD_UNIX_EPOCH, OMEGA_E, OMEGA_GMST, T_REF_S, WGS84_A
from petal_locate.core.geodesy import ecef_to_lla, enu_basis, lla_to_ecef, rz
from petal_locate.core.timebase import gmst_rad, ns_from_iso, sec

T0 = sec(ns_from_iso("2026-11-13T07:00:00Z"))
A_GEO = (GM / OMEGA_E**2) ** (1.0 / 3.0)
SIDEREAL_DAY = 2.0 * np.pi / OMEGA_E
TLE_EPOCH = dt.datetime(2026, 11, 10, 0, 0, 0)

# The classic ISS element set from the TLE format description (checksums 7 and 7).
ISS_L1 = "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
ISS_L2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"


# ----------------------------------------------------------------------------- helpers


def stencil_derivative(f: Any, t: np.ndarray, h: float) -> np.ndarray:
    """Fourth-order central difference with an exactly representable step."""
    return (8.0 * (f(t + h) - f(t - h)) - (f(t + 2 * h) - f(t - 2 * h))) / (12.0 * h)


def make_tle(
    inc_deg: float,
    raan_deg: float,
    ecc: float,
    argp_deg: float,
    mean_anomaly_deg: float,
    rev_per_day: float,
    epoch: dt.datetime,
    satnum: int = 99001,
    bstar: str = " 00000-0",
) -> tuple[str, str]:
    """A syntactically valid element set, column by column, with correct checksums."""
    day_of_year = (epoch - dt.datetime(epoch.year, 1, 1)).total_seconds() / 86400.0 + 1.0
    l1 = (
        f"1 {satnum:05d}U 26001A   {epoch.year % 100:02d}{day_of_year:012.8f} "
        f" .00000000  00000-0 {bstar} 0  999"
    )
    l2 = (
        f"2 {satnum:05d} {inc_deg:8.4f} {raan_deg % 360.0:8.4f} {int(round(ecc * 1e7)):07d} "
        f"{argp_deg % 360.0:8.4f} {mean_anomaly_deg % 360.0:8.4f} {rev_per_day:11.8f}{1:5d}"
    )
    assert len(l1) == 68 and len(l2) == 68
    checksum = lambda line: sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line) % 10
    return l1 + str(checksum(l1)), l2 + str(checksum(l2))


def geo_tle(lon_deg: float, inc_deg: float = 0.05, epoch: dt.datetime = TLE_EPOCH) -> tuple[str, str]:
    """Geostationary element set whose mean longitude at its epoch is ``lon_deg``.

    For a near-circular, near-equatorial orbit the sub-satellite longitude is
    RAAN + argp + M - GMST, so M is chosen from sgp4's own gstime at the epoch.
    """
    jd = JD_UNIX_EPOCH + (epoch - dt.datetime(1970, 1, 1)).total_seconds() / 86400.0
    raan, argp = 80.0, 30.0
    mean_anomaly = lon_deg + np.degrees(gstime(jd)) - raan - argp
    return make_tle(inc_deg, raan, 0.0001, argp, mean_anomaly, 1.00273791, epoch)


def to_inertial(r: np.ndarray, v: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """ECEF state -> the inertial frame of the kepler/tle types (rotation by +gmst)."""
    rot = rz(gmst_rad(t))
    spin = np.cross([0.0, 0.0, OMEGA_GMST], r)
    return np.einsum("...ij,...j->...i", rot, r), np.einsum("...ij,...j->...i", rot, v + spin)


def range_fit_residual(orbit: Any, site: np.ndarray, t0: float) -> float:
    """Largest residual (m) of a degree-5 fit to site range over 0.5 s at 31.25 kHz."""
    t = t0 + np.arange(15625) / 31250.0
    rng_m = np.linalg.norm(ob.orbit_position(orbit, t) - site, axis=-1)
    x = t - t[0]  # exact: the fit is against the float times actually used
    x = 2.0 * x / x[-1] - 1.0
    y = rng_m - rng_m[0]
    coeff = np.polynomial.chebyshev.chebfit(x, y, 5)
    return float(np.max(np.abs(y - np.polynomial.chebyshev.chebval(x, coeff))))


# ----------------------------------------------------------------------------- MATH 12.1: geo


def test_math12_1_geostationary_is_fixed_in_ecef() -> None:
    orbit = ob.GeoOrbit(lon_deg=-177.0)
    t = T0 + np.linspace(0.0, 5 * 86400.0, 4001)
    r, v = orbit.state_ecef(t)
    assert np.max(np.ptp(r, axis=0)) < 1e-3  # constant to 1 mm (it is constant to the bit)
    assert np.max(np.abs(v)) < 1e-9  # zero ECEF velocity
    lat, lon, alt = ecef_to_lla(r[0])
    assert (lat, lon) == pytest.approx((0.0, -177.0), abs=1e-12)
    assert np.linalg.norm(r[0]) == pytest.approx(42164172.37, abs=0.01)  # MATH.md section 2
    assert orbit.a_m == pytest.approx(A_GEO, rel=1e-15)
    assert alt == pytest.approx(A_GEO - WGS84_A, abs=1e-6)


def test_math12_1_inclined_geosynchronous_amplitude_and_speed() -> None:
    orbit = ob.GeoOrbit(lon_deg=172.0, inclination_deg=5.0, phase_deg=40.0)
    t = T0 + np.linspace(0.0, SIDEREAL_DAY, 40001)
    r, v = orbit.state_ecef(t)
    assert np.max(np.abs(r[:, 2])) == pytest.approx(A_GEO * np.sin(np.radians(5.0)), abs=0.5)
    assert np.min(r[:, 2]) == pytest.approx(-A_GEO * np.sin(np.radians(5.0)), abs=0.5)
    speed = np.linalg.norm(v, axis=1)
    assert np.max(speed) == pytest.approx(A_GEO * 0.0873 * OMEGA_E, rel=2e-3)  # "about 268 m/s"
    assert np.max(speed) == pytest.approx(268.0, abs=0.5)
    # exact: |v_ecef| = a n sqrt((1 - cos i)^2 + sin^2 i cos^2 u), peak 2 a n sin(i / 2)
    assert np.max(speed) == pytest.approx(2.0 * A_GEO * OMEGA_E * np.sin(np.radians(2.5)), rel=1e-7)
    assert np.allclose(np.linalg.norm(r, axis=1), A_GEO, rtol=0, atol=1e-6)


def test_geo_matches_the_literal_rotation_composition() -> None:
    """r_ecef = Rz(-OMEGA_E dt) Rz(lon - phase) Rx(inc) [a cos u, a sin u, 0], built from matrices."""
    lon, inc, phase, drift = np.radians(75.0), np.radians(5.0), np.radians(310.0), np.radians(0.25) / 86400.0
    orbit = ob.GeoOrbit(75.0, 5.0, epoch="2026-11-01T00:00:00Z", phase_deg=310.0, drift_deg_per_day=0.25)
    epoch = sec(ns_from_iso("2026-11-01T00:00:00Z"))
    n = OMEGA_E + drift
    a = (GM / n**2) ** (1.0 / 3.0)
    rx = np.array([[1, 0, 0], [0, np.cos(inc), -np.sin(inc)], [0, np.sin(inc), np.cos(inc)]])
    for dt_s, tol in ((0.0, 1e-7), (4321.0, 1e-6), (86400.0, 1e-6), (40 * 86400.0 + 123.0, 1e-3)):
        u = phase + n * dt_s
        r_i = rz(lon - phase) @ rx @ (a * np.array([np.cos(u), np.sin(u), 0.0]))
        expected = rz(-OMEGA_E * dt_s) @ r_i
        assert np.max(np.abs(orbit.position_ecef(epoch + dt_s) - expected)) < tol
    assert orbit.a_m == pytest.approx(a, rel=1e-15)


def test_geo_ground_track_is_a_figure_eight_centred_on_the_slot() -> None:
    orbit = ob.GeoOrbit(lon_deg=75.0, inclination_deg=5.0, epoch=T0, phase_deg=0.0)
    t = T0 + np.linspace(0.0, SIDEREAL_DAY, 20001)
    r, v = orbit.state_ecef(t)
    lon = np.degrees(np.arctan2(r[:, 1], r[:, 0]))
    geocentric_lat = np.degrees(np.arcsin(r[:, 2] / np.linalg.norm(r, axis=1)))
    assert np.max(geocentric_lat) == pytest.approx(5.0, abs=1e-6)
    assert np.min(geocentric_lat) == pytest.approx(-5.0, abs=1e-6)
    # longitude libration of an inclined circular orbit: atan(cos i tan u) - u, peak (1 - cos i)/(1 + cos i) rad... small
    half_width = np.degrees(np.arctan((1 - np.cos(np.radians(5.0))) / (2 * np.sqrt(np.cos(np.radians(5.0))))))
    assert np.max(lon) - 75.0 == pytest.approx(half_width, abs=1e-6)
    assert 75.0 - np.min(lon) == pytest.approx(half_width, abs=1e-6)
    # u = 0 at the epoch: on the equator, at the slot longitude, heading north
    assert r[0, 2] == pytest.approx(0.0, abs=1e-6)
    assert lon[0] == pytest.approx(75.0, abs=1e-12)
    assert v[0, 2] == pytest.approx(A_GEO * OMEGA_E * np.sin(np.radians(5.0)), rel=1e-12)
    # phase_deg moves the satellite along the same track: 90 deg is the northern turning point
    north = ob.GeoOrbit(lon_deg=75.0, inclination_deg=5.0, epoch=T0, phase_deg=90.0)
    r_n, v_n = north.state_ecef(T0)
    assert r_n[2] == pytest.approx(A_GEO * np.sin(np.radians(5.0)), abs=1e-6)
    assert v_n[2] == pytest.approx(0.0, abs=1e-9)
    assert np.degrees(np.arctan2(r_n[1], r_n[0])) == pytest.approx(75.0, abs=1e-9)


def test_geo_drift_moves_the_slot_and_changes_the_radius() -> None:
    orbit = ob.GeoOrbit(lon_deg=10.0, epoch=T0, drift_deg_per_day=0.5)
    for days in (0.0, 1.0, 10.0):
        _, lon, _ = ob.subsatellite_point(orbit, T0 + days * 86400.0)
        assert lon == pytest.approx(10.0 + 0.5 * days, abs=1e-9)
    n = OMEGA_E + np.radians(0.5) / 86400.0
    assert orbit.a_m == pytest.approx((GM / n**2) ** (1.0 / 3.0), rel=1e-15)
    assert orbit.a_m < A_GEO  # an eastward drifter is below the geostationary radius
    r, v = orbit.state_ecef(T0 + 5000.0)
    assert np.linalg.norm(v) == pytest.approx(orbit.a_m * np.radians(0.5) / 86400.0, rel=1e-9)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(lon_deg=172.0, inclination_deg=5.0, phase_deg=200.0),
        dict(lon_deg=-177.0, inclination_deg=4.0, phase_deg=40.0, drift_deg_per_day=-0.3, epoch="2025-03-01T00:00:00Z"),
        dict(lon_deg=75.0, inclination_deg=63.0, phase_deg=310.0),
    ],
)
def test_geo_velocity_is_the_derivative_of_position(kwargs: dict[str, Any]) -> None:
    orbit = ob.GeoOrbit(**kwargs)
    t = T0 + np.linspace(0.0, SIDEREAL_DAY, 301)
    _, v = orbit.state_ecef(t)
    assert np.max(np.abs(stencil_derivative(orbit.position_ecef, t, 1.0) - v)) < 2e-7


def test_geo_validation() -> None:
    with pytest.raises(ValueError, match="inclination_deg"):
        ob.GeoOrbit(0.0, inclination_deg=-1.0)
    with pytest.raises(ValueError, match="lon_deg"):
        ob.GeoOrbit(float("nan"))
    with pytest.raises(ValueError, match="epoch"):
        ob.GeoOrbit(0.0, epoch="yesterday")
    with pytest.raises(ValueError, match="epoch"):
        ob.GeoOrbit(0.0, epoch=1_767_225_600_000_000_000)  # nanoseconds are not seconds
    with pytest.raises(ValueError, match="t_s"):
        ob.GeoOrbit(0.0).state_ecef(float("nan"))
    assert ob.GeoOrbit(0.0).epoch_s == T_REF_S  # default epoch 2026-01-01T00:00:00Z
    assert ob.GeoOrbit(0.0, epoch=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)).epoch_s == T_REF_S


# ----------------------------------------------------------------------------- TLE


def test_tle_checksum_on_published_element_sets() -> None:
    assert ob.tle_checksum(ISS_L1) == 7 and ob.tle_checksum(ISS_L2) == 7
    iss = ob.TleOrbit(ISS_L1, ISS_L2)
    # epoch 2008 day 264.51782528
    expected = sec(ns_from_iso("2008-01-01T00:00:00Z")) + 263.51782528 * 86400.0
    assert iss.epoch_s == pytest.approx(expected, abs=1e-3)
    r, v = iss.state_ecef(iss.epoch_s)
    assert 330e3 < np.linalg.norm(r) - 6371e3 < 370e3  # ISS altitude in 2008
    assert 7.3e3 < np.linalg.norm(v + np.cross([0, 0, OMEGA_GMST], r)) < 7.8e3


def test_math12_1_geostationary_tle_returns_its_catalogued_longitude() -> None:
    """Sub-satellite longitude against sgp4's own TEME output rotated by sgp4's own gstime."""
    for slot in (-177.0, 172.0, 75.0):
        l1, l2 = geo_tle(slot)
        orbit = ob.TleOrbit(l1, l2)
        sat = Satrec.twoline2rv(l1, l2)
        for offset_s in (0.0, 5400.0):
            t = orbit.epoch_s + offset_s
            jd, fr = JD_UNIX_EPOCH + np.floor(t / 86400.0), (t % 86400.0) / 86400.0
            err, r_teme, _ = sat.sgp4(jd, fr)
            assert err == 0
            independent = np.degrees(np.arctan2(r_teme[1], r_teme[0]) - gstime(jd + fr))
            lat, lon, alt = ob.subsatellite_point(orbit, t)
            assert abs((lon - independent + 180.0) % 360.0 - 180.0) < 1e-5
            assert abs((lon - slot + 180.0) % 360.0 - 180.0) < 0.05  # the catalogued slot
            assert abs(lat) < 0.06 and alt == pytest.approx(35786e3, abs=30e3)


def test_tle_teme_to_ecef_rotation_and_velocity_formula() -> None:
    l1, l2 = geo_tle(172.0, inc_deg=4.0)
    orbit = ob.TleOrbit(l1, l2)
    sat = Satrec.twoline2rv(l1, l2)
    for t in (T0, T0 + 4000.25, T0 + 86400.0 * 2.5):
        jd, fr = JD_UNIX_EPOCH + np.floor(t / 86400.0), (t % 86400.0) / 86400.0
        _, r_teme, v_teme = sat.sgp4(jd, fr)
        r_teme, v_teme = 1e3 * np.array(r_teme), 1e3 * np.array(v_teme)
        r, v = orbit.state_ecef(t)
        # with the module's rotation angle: exact formulae of MATH.md section 2
        rot = rz(-gmst_rad(t))
        assert np.max(np.abs(r - rot @ r_teme)) < 1e-6
        assert np.max(np.abs(v - (rot @ v_teme - np.cross([0, 0, OMEGA_GMST], r)))) < 1e-9
        # with sgp4's own sidereal time: the linear angle is within 5e-8 rad (2 m at GEO)
        assert np.max(np.abs(r - rz(-gstime(jd + fr)) @ r_teme)) < 2.5
        assert np.array_equal(orbit.position_ecef(t), r)


def test_tle_velocity_is_close_to_but_not_exactly_the_derivative() -> None:
    l1, l2 = geo_tle(172.0, inc_deg=4.0)
    orbit = ob.TleOrbit(l1, l2)
    t = T0 + np.linspace(0.0, 86400.0, 97)
    _, v = orbit.state_ecef(t)
    diff = np.max(np.abs(stencil_derivative(orbit.position_ecef, t, 0.5) - v))
    assert diff < 0.1  # MATH.md section 1: up to 0.06 m/s
    assert diff > 1e-4  # which is why delay rates never use it


def test_tle_dut1_rotates_the_earth() -> None:
    l1, l2 = geo_tle(75.0)
    base, shifted = ob.TleOrbit(l1, l2), ob.TleOrbit(l1, l2, dut1_s=0.4)
    r0, r1 = base.position_ecef(T0), shifted.position_ecef(T0)
    assert np.allclose(r1, rz(-OMEGA_GMST * 0.4) @ r0, rtol=0, atol=1e-6)
    assert np.linalg.norm(r1 - r0) == pytest.approx(OMEGA_GMST * 0.4 * np.hypot(r0[0], r0[1]), rel=1e-6)


def test_tle_vectorised_over_any_shape() -> None:
    l1, l2 = geo_tle(-177.0)
    orbit = ob.TleOrbit(l1, l2)
    t = T0 + np.arange(6.0).reshape(2, 3) * 1000.0
    r, v = orbit.state_ecef(t)
    assert r.shape == v.shape == (2, 3, 3)
    r1, v1 = orbit.state_ecef(float(t[1, 2]))
    assert r1.shape == (3,)
    assert np.array_equal(r1, r[1, 2]) and np.array_equal(v1, v[1, 2])
    assert orbit.state_ecef(np.array([]))[0].shape == (0, 3)


def test_tle_validation() -> None:
    l1, l2 = geo_tle(75.0)
    bad1 = l1[:-1] + str((int(l1[-1]) + 1) % 10)
    with pytest.raises(ValueError, match="line1 checksum"):
        ob.TleOrbit(bad1, l2)
    with pytest.raises(ValueError, match="line2 checksum"):
        ob.TleOrbit(l1, l2[:-1] + str((int(l2[-1]) + 3) % 10))
    with pytest.raises(ValueError, match="line1 must be 69"):
        ob.TleOrbit(l1[:60], l2)
    with pytest.raises(ValueError, match="line2 must start"):
        ob.TleOrbit(l1, l1)
    other1, other2 = make_tle(0.05, 80.0, 0.0001, 30.0, 10.0, 1.00273791, TLE_EPOCH, satnum=12345)
    with pytest.raises(ValueError, match="catalog number"):
        ob.TleOrbit(l1, other2)
    assert ob.TleOrbit(l1 + "\n", l2 + "  \r\n").line1 == l1  # trailing blanks and newlines are tolerated


def test_tle_propagation_failure_is_an_error_not_garbage() -> None:
    l1, l2 = make_tle(51.64, 100.0, 0.0005, 30.0, 200.0, 15.5, TLE_EPOCH, bstar=" 10000-3")
    low = ob.TleOrbit(l1, l2)
    assert np.all(np.isfinite(low.position_ecef(T0)))
    with pytest.raises(ValueError, match="t_s.*sgp4 error code"):
        low.state_ecef(T0 + 4000 * 86400.0)  # long decayed


# ----------------------------------------------------------------------------- Kepler


def two_body(t: float, y: np.ndarray, j2: bool = False) -> np.ndarray:
    r = y[:3]
    rn = np.linalg.norm(r)
    acc = -GM * r / rn**3
    if j2:
        k = 1.5 * J2 * GM * WGS84_A**2 / rn**5
        zz = 5.0 * r[2] ** 2 / rn**2
        acc = acc - k * np.array([r[0] * (1 - zz), r[1] * (1 - zz), r[2] * (3 - zz)])
    return np.concatenate([y[3:], acc])


@pytest.mark.parametrize(
    "a_m, e, inc, raan, argp, m0",
    [
        (7.0e6, 0.001, 98.0, 40.0, 30.0, 10.0),
        (2.656e7, 0.74, 63.4, 250.0, 270.0, 200.0),  # Molniya-like
        (4.2164e7, 0.3, 20.0, 10.0, 120.0, 359.0),
    ],
)
def test_kepler_two_body_closed_forms(a_m: float, e: float, inc: float, raan: float, argp: float, m0: float) -> None:
    orbit = ob.KeplerOrbit(a_m, e, inc, raan, argp, m0, epoch=T0, j2=False)
    period = 2.0 * np.pi * np.sqrt(a_m**3 / GM)
    assert orbit.mean_motion_rad_s == pytest.approx(2.0 * np.pi / period, rel=1e-15)
    t = T0 + np.linspace(0.0, period, 2001)
    r_i, v_i = to_inertial(*orbit.state_ecef(t), t)
    rn = np.linalg.norm(r_i, axis=1)
    # vis-viva
    assert np.allclose(np.sum(v_i * v_i, axis=1), GM * (2.0 / rn - 1.0 / a_m), rtol=1e-12)
    # angular momentum: constant, magnitude sqrt(GM p), direction set by inclination and node
    h = np.cross(r_i, v_i)
    i_r, o_r = np.radians(inc), np.radians(raan)
    h_expected = np.sqrt(GM * a_m * (1 - e * e)) * np.array([np.sin(i_r) * np.sin(o_r), -np.sin(i_r) * np.cos(o_r), np.cos(i_r)])
    assert np.allclose(h, h_expected, rtol=0, atol=1e-11 * np.linalg.norm(h_expected))
    # radius range and the conic equation r = p / (1 + e cos(nu)) about the perigee direction
    assert np.min(rn) >= a_m * (1 - e) * (1 - 1e-12) and np.max(rn) <= a_m * (1 + e) * (1 + 1e-12)
    w_r = np.radians(argp)
    plane = rz(o_r) @ np.array([[1, 0, 0], [0, np.cos(i_r), -np.sin(i_r)], [0, np.sin(i_r), np.cos(i_r)]]) @ rz(w_r)
    perigee_dir, ahead_dir = plane @ [1.0, 0.0, 0.0], plane @ [0.0, 1.0, 0.0]
    cos_nu = r_i @ perigee_dir / rn
    assert np.allclose(rn, a_m * (1 - e * e) / (1 + e * cos_nu), rtol=1e-12)
    # Periodic in the inertial frame. A float time near 1.8e9 s is quantised to 2.4e-7 s
    # (millimetres along the track), so compare at the time actually representable.
    late = (t[-1] - T0) - period
    assert np.allclose(r_i[-1], r_i[0] + v_i[0] * late, rtol=0, atol=2e-6)
    # perigee (M = 0): on the perigee direction at a (1 - e), speed sqrt(GM (1 + e) / (a (1 - e)))
    t_perigee = T0 + (2 * np.pi - np.radians(m0)) / orbit.mean_motion_rad_s
    late = (t_perigee - T0) - (2 * np.pi - np.radians(m0)) / orbit.mean_motion_rad_s
    r_p, v_p = to_inertial(*orbit.state_ecef(t_perigee), t_perigee)
    v_perigee = np.sqrt(GM * (1 + e) / (a_m * (1 - e))) * ahead_dir
    g_perigee = -GM / (a_m * (1 - e)) ** 2 * perigee_dir
    assert np.allclose(r_p, a_m * (1 - e) * perigee_dir + v_perigee * late, rtol=0, atol=2e-6)
    assert np.allclose(v_p, v_perigee + g_perigee * late, rtol=0, atol=1e-8)
    # apogee half a period later: opposite side at a (1 + e), speed sqrt(GM (1 - e) / (a (1 + e)))
    t_apogee = t_perigee + period / 2
    late = (t_apogee - T0) - ((2 * np.pi - np.radians(m0)) / orbit.mean_motion_rad_s + period / 2)
    r_a, v_a = to_inertial(*orbit.state_ecef(t_apogee), t_apogee)
    v_apogee = -np.sqrt(GM * (1 - e) / (a_m * (1 + e))) * ahead_dir
    g_apogee = GM / (a_m * (1 + e)) ** 2 * perigee_dir
    assert np.allclose(r_a, -a_m * (1 + e) * perigee_dir + v_apogee * late, rtol=0, atol=2e-6)
    assert np.allclose(v_a, v_apogee + g_apogee * late, rtol=0, atol=1e-8)


@pytest.mark.parametrize("a_m, e, inc", [(7.0e6, 0.02, 98.0), (2.656e7, 0.74, 63.4)])
def test_kepler_two_body_against_numerical_integration(a_m: float, e: float, inc: float) -> None:
    orbit = ob.KeplerOrbit(a_m, e, inc, 40.0, 30.0, 10.0, epoch=T0, j2=False)
    period = 2.0 * np.pi * np.sqrt(a_m**3 / GM)
    r0, v0 = to_inertial(*orbit.state_ecef(T0), T0)
    times = np.linspace(0.0, 1.3 * period, 14)
    sol = solve_ivp(two_body, [0.0, times[-1]], np.concatenate([r0, v0]), t_eval=times, method="DOP853", rtol=1e-13, atol=1e-6)
    r_i, v_i = to_inertial(*orbit.state_ecef(T0 + times), T0 + times)
    assert np.max(np.abs(sol.y[:3].T - r_i)) < 5e-3
    assert np.max(np.abs(sol.y[3:].T - v_i)) < 5e-6


def test_kepler_j2_secular_rates_textbook_values() -> None:
    day = 86400.0
    # ISS-like orbit: the node regresses about 5 deg/day and the perigee advances about 3.7 deg/day
    iss = ob.KeplerOrbit(6.78e6, 0.0005, 51.64, 0.0, 0.0, 0.0, epoch=T0)
    assert np.degrees(iss.raan_rate_rad_s) * day == pytest.approx(-5.0, abs=0.1)
    assert np.degrees(iss.argp_rate_rad_s) * day == pytest.approx(3.73, abs=0.1)
    # sun-synchronous: 700 km circular at 98.19 deg precesses 360 deg per tropical year
    sso = ob.KeplerOrbit(WGS84_A + 700e3, 0.0, 98.19, 0.0, 0.0, 0.0, epoch=T0)
    assert np.degrees(sso.raan_rate_rad_s) * day == pytest.approx(360.0 / 365.2422, rel=5e-3)
    # critical inclination: no apsidal rotation
    crit = ob.KeplerOrbit(2.656e7, 0.74, np.degrees(np.arcsin(np.sqrt(0.8))), 0.0, 270.0, 0.0, epoch=T0)
    assert abs(crit.argp_rate_rad_s) < 1e-20
    # equatorial geosynchronous: node -0.0134 deg/day (1.5 J2 (Re/a)^2 n), a known small number
    geo = ob.KeplerOrbit(A_GEO, 0.0, 0.0, 0.0, 0.0, 0.0, epoch=T0)
    assert np.degrees(geo.raan_rate_rad_s) * day == pytest.approx(-0.0134, abs=2e-4)
    # without J2 nothing precesses and the mean motion is Keplerian
    plain = ob.KeplerOrbit(6.78e6, 0.0005, 51.64, 0.0, 0.0, 0.0, epoch=T0, j2=False)
    assert plain.raan_rate_rad_s == 0.0 and plain.argp_rate_rad_s == 0.0
    assert plain.mean_motion_rad_s == pytest.approx(np.sqrt(GM / 6.78e6**3), rel=1e-15)
    assert iss.mean_motion_rad_s != plain.mean_motion_rad_s


def test_kepler_j2_node_drift_against_numerical_integration() -> None:
    """Integrate the J2 equations of motion for a day and fit the drift of the osculating node."""
    orbit = ob.KeplerOrbit(6.9e6, 0.001, 51.64, 100.0, 40.0, 0.0, epoch=T0)
    r0, v0 = to_inertial(*orbit.state_ecef(T0), T0)
    times = np.linspace(0.0, 86400.0, 2881)
    sol = solve_ivp(two_body, [0.0, 86400.0], np.concatenate([r0, v0]), t_eval=times, args=(True,), method="DOP853", rtol=1e-10, atol=1e-4)
    h = np.cross(sol.y[:3].T, sol.y[3:].T)
    node = np.unwrap(np.arctan2(h[:, 0], -h[:, 1]))
    slope = np.polyfit(times, node, 1)[0]
    assert slope == pytest.approx(orbit.raan_rate_rad_s, rel=0.01)
    # and the module's own orbit plane (normal from two positions a quarter orbit apart, so
    # no velocity is involved) turns at exactly its stated rate
    quarter = 0.5 * np.pi / orbit.mean_motion_rad_s
    ends = np.array([T0, T0 + 86400.0])
    r_a, _ = to_inertial(*orbit.state_ecef(ends), ends)
    r_b, _ = to_inertial(*orbit.state_ecef(ends + quarter), ends + quarter)
    # undo the node motion during the quarter orbit before taking the plane normal
    turn = orbit.raan_rate_rad_s * quarter
    normal = np.cross(r_a, np.einsum("ij,kj->ki", rz(-turn), r_b))
    own = np.arctan2(normal[:, 0], -normal[:, 1])
    assert (own[1] - own[0]) / 86400.0 == pytest.approx(orbit.raan_rate_rad_s, rel=1e-9)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(a_m=7.0e6, e=0.001, inc_deg=98.0, raan_deg=40.0, argp_deg=30.0, mean_anomaly_deg=10.0, epoch="2026-06-01T00:00:00Z"),
        dict(a_m=2.656e7, e=0.74, inc_deg=63.4, raan_deg=250.0, argp_deg=270.0, mean_anomaly_deg=5.0, epoch="2026-11-01T00:00:00Z"),
        dict(a_m=4.2164e7, e=0.0003, inc_deg=4.0, raan_deg=80.0, argp_deg=30.0, mean_anomaly_deg=200.0, epoch="2026-01-01T00:00:00Z", j2=False),
    ],
)
def test_kepler_velocity_is_the_derivative_of_position(kwargs: dict[str, Any]) -> None:
    orbit = ob.KeplerOrbit(**kwargs)
    t = T0 + np.linspace(0.0, 2.0 * np.pi / orbit.mean_motion_rad_s, 257)
    _, v = orbit.state_ecef(t)
    assert np.max(np.abs(stencil_derivative(orbit.position_ecef, t, 0.125) - v)) < 5e-7


def test_kepler_high_eccentricity_solves_keplers_equation() -> None:
    for e in (0.0, 0.3, 0.74, 0.95, 0.999):
        orbit = ob.KeplerOrbit(3.0e7, e, 10.0, 0.0, 0.0, 0.0, epoch=T0, j2=False)
        period = 2.0 * np.pi / orbit.mean_motion_rad_s
        t = T0 + np.linspace(0.0, period, 4001)[:-1]
        r_i, v_i = to_inertial(*orbit.state_ecef(t), t)
        rn = np.linalg.norm(r_i, axis=1)
        # r = a (1 - e cos E) and r . v = sqrt(GM a) e sin E give E without the solver; then
        # Kepler's equation must reproduce the mean anomaly n (t - T0)
        cos_e = (1.0 - rn / 3.0e7) / e if e > 0 else np.cos(orbit.mean_motion_rad_s * (t - T0))
        sin_e = np.sum(r_i * v_i, axis=1) / (np.sqrt(GM * 3.0e7) * e) if e > 0 else np.sin(orbit.mean_motion_rad_s * (t - T0))
        big_e = np.arctan2(sin_e, cos_e)
        mean = orbit.mean_motion_rad_s * (t - T0)
        resid = np.mod(big_e - e * np.sin(big_e) - mean + np.pi, 2 * np.pi) - np.pi
        assert np.max(np.abs(resid)) < 1e-9


def test_kepler_validation_and_dut1() -> None:
    args = dict(a_m=7e6, e=0.1, inc_deg=10.0, raan_deg=0.0, argp_deg=0.0, mean_anomaly_deg=0.0, epoch=T0)
    for field, value in (("e", 1.0), ("e", -0.1), ("a_m", 0.0), ("inc_deg", 181.0), ("epoch", None), ("raan_deg", "x")):
        with pytest.raises(ValueError, match=field):
            ob.KeplerOrbit(**{**args, field: value})
    base, shifted = ob.KeplerOrbit(**args), ob.KeplerOrbit(**args, dut1_s=-0.3)
    assert np.allclose(shifted.position_ecef(T0), rz(OMEGA_GMST * 0.3) @ base.position_ecef(T0), rtol=0, atol=1e-6)


# ----------------------------------------------------------------------------- table


def test_table_reproduces_cubics_exactly() -> None:
    """Cubic Hermite interpolation is exact for cubic polynomials, on any knots."""
    rng = np.random.default_rng(1)
    coeff = rng.normal(size=(4, 3)) * np.array([[1e7], [1e3], [1e-1], [1e-4]])
    poly = lambda x: coeff[0] + coeff[1] * x[..., None] + coeff[2] * x[..., None] ** 2 + coeff[3] * x[..., None] ** 3
    dpoly = lambda x: coeff[1] + 2 * coeff[2] * x[..., None] + 3 * coeff[3] * x[..., None] ** 2
    knots = np.array([0.0, 7.0, 10.0, 31.0, 60.0, 61.5, 120.0])
    table = ob.TableOrbit(T0 + knots, poly(knots), dpoly(knots))
    x = np.round(rng.uniform(0.0, 120.0, 400) * 1024.0) / 1024.0  # exactly representable times
    r, v = table.state_ecef(T0 + x)
    assert np.max(np.abs(r - poly(x))) < 1e-7
    assert np.max(np.abs(v - dpoly(x))) < 1e-9
    assert np.array_equal(table.position_ecef(T0 + x), r)
    # the knots themselves, including both ends of the span
    r_k, v_k = table.state_ecef(T0 + knots)
    assert np.max(np.abs(r_k - poly(knots))) < 1e-8 and np.max(np.abs(v_k - dpoly(knots))) < 1e-12


def test_table_error_is_within_the_hermite_bound() -> None:
    """|error| <= h^4 / 384 max|r''''| (and sqrt(3)/216 h^3 max|r''''| for the derivative)."""
    truth = ob.GeoOrbit(lon_deg=172.0, inclination_deg=5.0, phase_deg=200.0)
    h = 60.0
    knots = T0 + np.arange(-30, 31) * h
    table = ob.TableOrbit(knots, *truth.state_ecef(knots))
    t = T0 + np.linspace(-1790.0, 1790.0, 5001)
    r, v = table.state_ecef(t)
    r_true, v_true = truth.state_ecef(t)
    # fourth derivative of the ECEF motion: z at rate n, the (1 - cos i) terms at rate 2 n
    inc = np.radians(5.0)
    d4 = truth.a_m * OMEGA_E**4 * (np.sin(inc) + 16.0 * (1 - np.cos(inc)))
    pos_err = np.max(np.linalg.norm(r - r_true, axis=1))
    vel_err = np.max(np.linalg.norm(v - v_true, axis=1))
    assert 1e-8 < pos_err < h**4 / 384.0 * d4
    assert vel_err < np.sqrt(3.0) / 216.0 * h**3 * d4
    # the interpolant's velocity is its own derivative
    inner = t[5:-5]
    assert np.max(np.abs(stencil_derivative(table.position_ecef, inner, 0.125) - table.state_ecef(inner)[1])) < 1e-6


def test_table_raises_outside_its_span() -> None:
    knots = T0 + np.array([0.0, 60.0, 120.0])
    table = ob.TableOrbit(knots, *ob.GeoOrbit(10.0, 3.0).state_ecef(knots))
    table.state_ecef(T0)
    table.state_ecef(T0 + 120.0)
    for bad in (T0 - 2.0**-20, T0 + 120.0 + 2.0**-20, np.array([T0 + 1.0, T0 + 500.0])):
        with pytest.raises(ValueError, match="t_s outside the ephemeris table span"):
            table.state_ecef(bad)
        with pytest.raises(ValueError, match="t_s outside the ephemeris table span"):
            table.position_ecef(bad)


def test_table_validation() -> None:
    r = np.zeros((3, 3))
    with pytest.raises(ValueError, match="strictly increasing"):
        ob.TableOrbit([0.0, 2.0, 1.0], r, r)
    with pytest.raises(ValueError, match="strictly increasing"):
        ob.TableOrbit([0.0, 1.0, 1.0], r, r)
    with pytest.raises(ValueError, match="r_m"):
        ob.TableOrbit([0.0, 1.0, 2.0], np.zeros((2, 3)), r)
    with pytest.raises(ValueError, match="v_mps"):
        ob.TableOrbit([0.0, 1.0, 2.0], r, np.zeros((3, 2)))
    with pytest.raises(ValueError, match="t_s"):
        ob.TableOrbit([0.0], np.zeros((1, 3)), np.zeros((1, 3)))


# ----------------------------------------------------------------------------- perturbation


def test_perturbed_geostationary_offsets_are_radial_east_and_north() -> None:
    """For a geostationary base the RIC axes are hand-computable: up, east and +z."""
    lon = 140.0
    base = ob.GeoOrbit(lon_deg=lon, epoch=T0)
    east, _north, up = enu_basis(0.0, lon)
    pert = ob.PerturbedOrbit(base, [100.0, 2000.0, -500.0], [0.0, 0.0, 0.0], T0)
    r_base = base.position_ecef(T0)
    expected = r_base + 100.0 * up + 2000.0 * east - 500.0 * np.array([0.0, 0.0, 1.0])
    assert np.max(np.abs(pert.position_ecef(T0) - expected)) < 1e-7
    radial_only = ob.PerturbedOrbit(base, [250.0, 0.0, 0.0], [0.0, 0.0, 0.0], T0)
    assert np.linalg.norm(radial_only.position_ecef(T0 + 777.0)) == pytest.approx(A_GEO + 250.0, abs=1e-6)
    # a rate: the offset grows linearly from t_ref_s, here in-track (east) at 0.5 m/s
    drifting = ob.PerturbedOrbit(base, [0.0, 10.0, 0.0], [0.0, 0.5, 0.0], T0 - 100.0)
    for t in (T0 - 100.0, T0, T0 + 1000.0):
        assert np.max(np.abs(drifting.position_ecef(t) - (r_base + (10.0 + 0.5 * (t - (T0 - 100.0))) * east))) < 1e-7
    # constant offset in a frame fixed to the Earth: velocity stays zero; drifting: 0.5 m/s east
    assert np.max(np.abs(pert.state_ecef(T0)[1])) < 1e-6
    assert np.max(np.abs(drifting.state_ecef(T0)[1] - 0.5 * east)) < 1e-6


def test_perturbed_ric_frame_definition_on_an_inclined_orbit() -> None:
    base = ob.KeplerOrbit(7.0e6, 0.01, 98.0, 40.0, 30.0, 10.0, epoch=T0 - 5000.0)
    ric = np.array([120.0, -900.0, 400.0])
    pert = ob.PerturbedOrbit(base, ric, [0.0, 0.0, 0.0], T0)
    t = T0 + np.array([0.0, 500.0, 2500.0])
    r, v = base.state_ecef(t)
    d = pert.position_ecef(t) - r
    v_inertial = v + np.cross([0.0, 0.0, OMEGA_E], r)
    radial = r / np.linalg.norm(r, axis=1, keepdims=True)
    normal = np.cross(r, v_inertial)
    normal /= np.linalg.norm(normal, axis=1, keepdims=True)
    # components: radial along r, cross-track along the orbit normal, in-track completes the triad
    assert np.allclose(np.sum(d * radial, axis=1), ric[0], atol=1e-7)
    assert np.allclose(np.sum(d * normal, axis=1), ric[2], atol=1e-7)
    assert np.allclose(np.sum(d * np.cross(normal, radial), axis=1), ric[1], atol=1e-7)
    assert np.allclose(np.linalg.norm(d, axis=1), np.linalg.norm(ric), atol=1e-7)
    # in-track is within a degree of the direction of inertial motion (e = 0.01)
    along = v_inertial / np.linalg.norm(v_inertial, axis=1, keepdims=True)
    assert np.all(np.sum(np.cross(normal, radial) * along, axis=1) > np.cos(np.radians(1.0)))


def _bases() -> dict[str, Any]:
    l1, l2 = geo_tle(172.0, inc_deg=4.0)
    return {
        "geo": ob.GeoOrbit(172.0, 5.0, phase_deg=200.0),
        "kepler_leo": ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 10.0, epoch="2026-06-01T00:00:00Z"),
        "tle": ob.TleOrbit(l1, l2),
    }


@pytest.mark.parametrize("name", ["geo", "kepler_leo", "tle"])
def test_perturbed_velocity_is_the_derivative_of_its_position(name: str) -> None:
    base = _bases()[name]
    pert = ob.PerturbedOrbit(base, [2000.0, 20000.0, -10000.0], [0.3, -0.3, 0.7], T0 - 600.0)
    t = T0 + np.linspace(0.0, 5400.0, 61)
    r, v = pert.state_ecef(t)
    assert np.array_equal(r, pert.position_ecef(t))
    # independent derivative: Chebyshev fit of the position over +/- 4 s, differentiated
    # (nodes rounded to 2**-10 s so that t + node is exact in float64)
    nodes = np.round(np.cos(np.pi * (np.arange(16) + 0.5) / 16) * 4.0 * 1024.0) / 1024.0
    samples = pert.position_ecef(t[:, None] + nodes)  # [61, 16, 3]
    for axis in range(3):
        coeff = np.polynomial.chebyshev.chebfit(nodes / 4.0, samples[:, :, axis].T, 11)
        deriv = np.polynomial.chebyshev.chebval(0.0, np.polynomial.chebyshev.chebder(coeff)) / 4.0
        assert np.max(np.abs(deriv - v[:, axis])) < 2e-6
    # ric_vel_mps is not simply added to the base velocity: the frame itself rotates
    _, v_base = base.state_ecef(t)
    r_b = base.position_ecef(t)
    v_in = v_base + np.cross([0.0, 0.0, OMEGA_E], r_b)
    radial = r_b / np.linalg.norm(r_b, axis=1, keepdims=True)
    normal = np.cross(r_b, v_in)
    normal /= np.linalg.norm(normal, axis=1, keepdims=True)
    naive = v_base + 0.3 * radial - 0.3 * np.cross(normal, radial) + 0.7 * normal
    # (tens of mm/s for a geosynchronous base seen from the Earth, metres per second in low orbit)
    assert np.min(np.linalg.norm(v - naive, axis=1)) > (5.0 if name == "kepler_leo" else 0.02)


def test_perturbed_zero_offset_is_the_base_orbit() -> None:
    base = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 10.0, epoch=T0)
    same = ob.PerturbedOrbit(base, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], T0)
    t = T0 + np.linspace(0.0, 3000.0, 7)
    r, v = base.state_ecef(t)
    r2, v2 = same.state_ecef(t)
    assert np.array_equal(r2, r)
    assert np.max(np.abs(v2 - v)) < 1e-6
    r_s, v_s = same.state_ecef(T0 + 10.0)
    assert r_s.shape == v_s.shape == (3,)


def test_perturbed_validation() -> None:
    base = ob.GeoOrbit(0.0)
    with pytest.raises(ValueError, match="ric_pos_m"):
        ob.PerturbedOrbit(base, [1.0, 2.0], [0.0, 0.0, 0.0], T0)
    with pytest.raises(ValueError, match="ric_vel_mps"):
        ob.PerturbedOrbit(base, [1.0, 2.0, 3.0], [0.0, np.nan, 0.0], T0)
    with pytest.raises(ValueError, match="base"):
        ob.PerturbedOrbit(object(), [1.0, 2.0, 3.0], [0.0, 0.0, 0.0], T0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="t_ref_s"):
        ob.PerturbedOrbit(base, [1.0, 2.0, 3.0], [0.0, 0.0, 0.0], float("inf"))


# ----------------------------------------------------------------------------- MATH 12.1: smoothness


def test_math12_1_site_range_is_smooth_for_every_orbit_type() -> None:
    """Residual of a degree-5 fit to site range over 0.5 s at 31.25 kHz below 1e-6 m."""
    site = lla_to_ecef(35.68, 139.77, 50.0)
    l1, l2 = geo_tle(172.0, inc_deg=4.0)
    low1, low2 = make_tle(51.64, 100.0, 0.0005, 30.0, 200.0, 15.5, TLE_EPOCH, bstar=" 10000-3")
    geo = ob.GeoOrbit(172.0, 5.0, phase_deg=200.0)
    knots = T0 + np.arange(-60, 61) * 10.0
    orbits = {
        "tle": ob.TleOrbit(l1, l2),
        "tle_leo": ob.TleOrbit(low1, low2),
        "geo": geo,
        "geo_drifting_old_epoch": ob.GeoOrbit(172.0, 5.0, epoch="2024-01-01T00:00:00Z", phase_deg=200.0, drift_deg_per_day=0.3),
        "kepler_geo": ob.KeplerOrbit(4.2164172e7, 0.0003, 4.0, 80.0, 30.0, 200.0, epoch="2026-01-01T00:00:00Z"),
        "kepler_leo": ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 10.0, epoch="2026-06-01T00:00:00Z"),
        "table": ob.TableOrbit(knots, *geo.state_ecef(knots)),
    }
    orbits["perturbed_tle"] = ob.PerturbedOrbit(orbits["tle"], [2000.0, 20000.0, 10000.0], [0.3, 0.3, 0.7], T0)
    orbits["perturbed_kepler"] = ob.PerturbedOrbit(orbits["kepler_leo"], [100.0, 1000.0, 1000.0], [0.02, 0.02, 0.05], T0)
    for name, orbit in orbits.items():
        # SGP4 keeps time as float64 minutes since the element epoch: 1.4e-11 s of granularity
        # one day after it, which a low orbiter turns into 1e-7 m. It doubles with every doubling
        # of the element age (1e-6 m is passed after about 11 days at 7.5 km/s of range rate;
        # geosynchronous sets stay below 4e-7 m at 90 days), so the fast set is checked fresh.
        start = orbit.epoch_s + 86400.0 if name == "tle_leo" else T0
        for t0 in (start, start + 123.456):
            assert range_fit_residual(orbit, site, t0) < 1e-6, name
    assert range_fit_residual(orbits["tle"], site, orbits["tle"].epoch_s + 90 * 86400.0) < 1e-6


# ----------------------------------------------------------------------------- split time


def _all_orbit_types() -> dict[str, Any]:
    l1, l2 = geo_tle(172.0, inc_deg=4.0)
    low1, low2 = make_tle(51.64, 100.0, 0.0005, 30.0, 200.0, 15.5, TLE_EPOCH, bstar=" 10000-3")
    leo = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 10.0, epoch="2026-06-01T00:00:00Z")
    knots = T0 + np.arange(-6, 7) * 10.0
    return {
        "tle": ob.TleOrbit(l1, l2),
        "tle_leo": ob.TleOrbit(low1, low2),
        "geo": ob.GeoOrbit(172.0, 5.0, phase_deg=200.0),
        "geo_drift": ob.GeoOrbit(172.0, 5.0, epoch="2025-01-01T00:00:00Z", phase_deg=200.0, drift_deg_per_day=0.3),
        "kepler_leo": leo,
        "table": ob.TableOrbit(knots, *leo.state_ecef(knots)),
        "perturbed": ob.PerturbedOrbit(leo, [100.0, 1000.0, -500.0], [0.02, 0.02, 0.05], T0 - 50.0),
    }


@pytest.mark.parametrize("name", list(_all_orbit_types()))
def test_split_time_evaluates_at_the_exact_sum(name: str) -> None:
    """state(t, dt) is the state at t + dt: equal to state(t + dt) when that sum is a float64,
    and resolving offsets far below the 2.4e-7 s spacing of float64 times when it is not."""
    orbit = _all_orbit_types()[name]
    assert orbit.supports_split_time
    t = T0 + np.array([0.0, 7.0, 31.5])
    r0, v0 = orbit.state_ecef(t)
    # representable sum: the two routes agree to rounding (one ulp of a 4e7 m coordinate is 7e-9 m)
    for dt_s in (0.5, -0.25, 2.0):
        r_split, v_split = orbit.state_ecef(t, dt_s)
        r_sum, v_sum = orbit.state_ecef(t + dt_s)
        assert np.max(np.abs(r_split - r_sum)) < 3e-8
        assert np.max(np.abs(v_split - v_sum)) < 1e-7
        assert np.max(np.abs(orbit.position_ecef(t, dt_s) - r_split)) == 0.0
        assert np.max(np.abs(ob.orbit_position(orbit, t, dt_s) - r_split)) == 0.0
    # 10 ns: float64(t + 1e-8) == t, yet the satellite must move by v * 1e-8
    assert np.all(t + 1e-8 == t)
    tiny = 1e-8
    moved = orbit.position_ecef(t, tiny) - orbit.position_ecef(t, -tiny)
    expected = 2.0 * tiny * stencil_derivative(orbit.position_ecef, t, 0.125)
    # limited by coordinate rounding (7e-9 m per ulp at GEO), and for SGP4 by its own time
    # variable, float64 minutes since the element epoch (3e-11 s three days after it)
    assert np.max(np.abs(moved - expected)) < (5e-7 if name.startswith("tle") else 1e-7)
    speed = np.linalg.norm(expected, axis=1) / (2.0 * tiny)
    if name in ("kepler_leo", "table", "perturbed"):  # the fast ones without SGP4's time granularity
        assert np.all(np.linalg.norm(moved, axis=1) > 1e-4)  # 0.15 mm for 7.5 km/s: well resolved
        assert np.max(np.abs(np.linalg.norm(moved, axis=1) / (2.0 * tiny) - speed)) < 1e-3 * np.max(speed)
    # per-element offsets broadcast
    offsets = np.array([1e-9, -3e-8, 5e-7])
    r_vec = orbit.position_ecef(t, offsets)
    for k in range(3):
        assert np.max(np.abs(r_vec[k] - orbit.position_ecef(float(t[k]), float(offsets[k])))) < 1e-9
    assert orbit.position_ecef(t[:, None], np.zeros(4)).shape == (3, 4, 3)
    with pytest.raises(ValueError, match="dt_s"):
        orbit.position_ecef(t, np.nan)


def test_split_time_makes_low_orbit_positions_smooth_in_the_offset() -> None:
    """Sampling a low orbit at t0 + n / fs: rounding the sum jitters the position by up to
    v * 1.2e-7 s = 0.9 mm; passing the offset separately leaves nanometres."""
    orbit = _all_orbit_types()["kepler_leo"]
    n = np.arange(2048)
    rel = n / 31250.0
    x = 2.0 * rel / rel[-1] - 1.0

    def scatter(pos: np.ndarray) -> float:
        worst = 0.0
        for axis in range(3):
            y = pos[:, axis] - pos[0, axis]
            fit = np.polynomial.chebyshev.chebfit(x, y, 6)
            worst = max(worst, float(np.max(np.abs(y - np.polynomial.chebyshev.chebval(x, fit)))))
        return worst

    assert scatter(orbit.position_ecef(T0, rel)) < 5e-8
    assert scatter(orbit.position_ecef(T0 + rel)) > 1e-4


def test_orbit_position_and_state_for_protocol_only_orbits_use_the_rounded_sum() -> None:
    inner = ob.GeoOrbit(100.0, 5.0)

    class Minimal:
        def state_ecef(self, t_s: Any) -> tuple[np.ndarray, np.ndarray]:
            return inner.state_ecef(t_s)

    t = T0 + np.array([0.0, 10.0])
    assert np.array_equal(ob.orbit_position(Minimal(), t, 0.5), inner.position_ecef(t + 0.5))
    r, v = ob.orbit_state(Minimal(), t, 0.5)
    r_ref, v_ref = inner.state_ecef(t + 0.5)
    assert np.array_equal(r, r_ref) and np.array_equal(v, v_ref)
    r, v = ob.orbit_state(inner, t, 0.5)
    assert np.max(np.abs(r - r_ref)) < 3e-8 and np.max(np.abs(v - v_ref)) < 1e-9


# ----------------------------------------------------------------------------- construction


class GeoModel(BaseModel):
    type: Literal["geo"] = "geo"
    lon_deg: float
    inclination_deg: float = 0.0
    epoch: dt.datetime = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    phase_deg: float = 0.0
    drift_deg_per_day: float = 0.0


class KeplerModel(BaseModel):
    type: Literal["kepler"] = "kepler"
    a_m: float
    e: float
    inc_deg: float
    raan_deg: float
    argp_deg: float
    mean_anomaly_deg: float
    epoch: str
    j2: bool = True


def test_orbit_from_config_accepts_models_namespaces_and_dicts() -> None:
    t = T0 + np.array([0.0, 1000.0])
    direct = ob.GeoOrbit(-177.0, 5.0, phase_deg=40.0)
    for model in (
        GeoModel(lon_deg=-177.0, inclination_deg=5.0, phase_deg=40.0),
        SimpleNamespace(type="geo", lon_deg=-177.0, inclination_deg=5.0, phase_deg=40.0),
        {"type": "geo", "lon_deg": -177.0, "inclination_deg": 5.0, "phase_deg": 40.0, "epoch": "2026-01-01T00:00:00Z"},
        SimpleNamespace(type="geo", lon_deg=-177.0, inclination_deg=5.0, phase_deg=40.0, epoch="2026-01-01T09:00:00+09:00", drift_deg_per_day=0.0),
    ):
        orbit = ob.orbit_from_config(model)
        assert isinstance(orbit, ob.GeoOrbit) and isinstance(orbit, ob.Orbit)
        assert np.array_equal(orbit.position_ecef(t), direct.position_ecef(t))
    assert ob.orbit_from_config(direct) is direct  # already an orbit
    wrapped = SimpleNamespace(root=GeoModel(lon_deg=-177.0, inclination_deg=5.0, phase_deg=40.0))  # RootModel-style
    assert np.array_equal(ob.orbit_from_config(wrapped).position_ecef(t), direct.position_ecef(t))

    kep_args = dict(a_m=7.0e6, e=0.01, inc_deg=98.0, raan_deg=40.0, argp_deg=30.0, mean_anomaly_deg=10.0)
    kep_direct = ob.KeplerOrbit(**kep_args, epoch="2026-06-01T00:00:00Z", j2=False, dut1_s=0.2)
    for model in (
        KeplerModel(**kep_args, epoch="2026-06-01T00:00:00Z", j2=False),
        SimpleNamespace(type="kepler", **kep_args, epoch=dt.datetime(2026, 6, 1), j2=False),
        {"type": "kepler", **kep_args, "epoch": "2026-06-01T00:00:00Z", "j2": False},
    ):
        orbit = ob.orbit_from_config(model, dut1_s=0.2)
        assert isinstance(orbit, ob.KeplerOrbit)
        assert np.array_equal(orbit.position_ecef(t), kep_direct.position_ecef(t))
    assert ob.orbit_from_config(SimpleNamespace(type="kepler", **kep_args, epoch="2026-06-01T00:00:00Z")).j2 is True

    l1, l2 = geo_tle(75.0)
    tle = ob.orbit_from_config({"type": "tle", "line1": l1, "line2": l2}, dut1_s=-0.1)
    assert isinstance(tle, ob.TleOrbit) and tle.dut1_s == -0.1
    assert np.array_equal(tle.position_ecef(t), ob.TleOrbit(l1, l2, dut1_s=-0.1).position_ecef(t))


def test_orbit_from_config_table() -> None:
    truth = ob.GeoOrbit(10.0, 3.0)
    stamps = ["2026-11-13T07:00:00Z", "2026-11-13T07:01:00Z", "2026-11-13T07:02:00Z"]
    knots = np.array([sec(ns_from_iso(s)) for s in stamps])
    r, v = truth.state_ecef(knots)
    samples_dict = [{"t": s, "r_m": list(r[k]), "v_mps": list(v[k])} for k, s in enumerate(stamps)]
    samples_obj = [SimpleNamespace(t=dt.datetime(2026, 11, 13, 7, k, 0, tzinfo=dt.timezone.utc), r_m=tuple(r[k]), v_mps=tuple(v[k])) for k in range(3)]
    for model in ({"type": "table", "samples": samples_dict}, SimpleNamespace(type="table", samples=samples_obj)):
        table = ob.orbit_from_config(model)
        assert isinstance(table, ob.TableOrbit)
        assert np.array_equal(table.t_s, knots)
        assert np.max(np.abs(table.position_ecef(knots[0] + 30.0) - truth.position_ecef(knots[0] + 30.0))) < 1e-4
    assert np.array_equal(ob.TableOrbit(samples_dict).r_m, r)  # the constructor takes the same list
    assert np.array_equal(ob.TableOrbit.from_samples(samples_obj).v_mps, v)
    with pytest.raises(ValueError, match="samples"):
        ob.orbit_from_config({"type": "table", "samples": samples_dict[:1]})
    with pytest.raises(ValueError, match="r_m and v_mps"):
        ob.TableOrbit(knots, r)
    with pytest.raises(ValueError, match=r"samples\[\]\.r_m"):
        ob.orbit_from_config({"type": "table", "samples": [{"t": stamps[0], "v_mps": [0, 0, 0]}, samples_dict[1]]})


def test_orbit_from_config_errors_name_the_field() -> None:
    with pytest.raises(ValueError, match="orbit.type"):
        ob.orbit_from_config(SimpleNamespace(type="sgp8"))
    with pytest.raises(ValueError, match="orbit.type"):
        ob.orbit_from_config({"lon_deg": 1.0})
    with pytest.raises(ValueError, match="orbit.lon_deg"):
        ob.orbit_from_config(SimpleNamespace(type="geo"))
    with pytest.raises(ValueError, match="orbit.epoch"):
        ob.orbit_from_config({"type": "kepler", "a_m": 7e6, "e": 0.0, "inc_deg": 0.0, "raan_deg": 0.0, "argp_deg": 0.0, "mean_anomaly_deg": 0.0})
    with pytest.raises(ValueError, match="orbit.line2"):
        ob.orbit_from_config({"type": "tle", "line1": ISS_L1})
    with pytest.raises(ValueError, match="epoch"):
        ob.orbit_from_config({"type": "geo", "lon_deg": 0.0, "epoch": 12.5 + 3j})


# ----------------------------------------------------------------------------- misc


def test_subsatellite_point_and_orbit_position() -> None:
    lat, lon, alt = ob.subsatellite_point(ob.GeoOrbit(-177.0), T0)
    assert (lat, lon) == pytest.approx((0.0, -177.0), abs=1e-12) and alt == pytest.approx(A_GEO - WGS84_A, abs=1e-6)
    inclined = ob.GeoOrbit(100.0, 5.0)
    t = T0 + np.linspace(0.0, SIDEREAL_DAY, 501)
    lat, lon, alt = ob.subsatellite_point(inclined, t)
    assert lat.shape == lon.shape == alt.shape == (501,)
    # geodetic latitude of the sub-point exceeds the geocentric 5 deg slightly
    assert 5.0 < np.max(lat) < 5.01
    assert np.all((lon >= -180.0) & (lon < 180.0))

    class Minimal:
        """Only the protocol method: orbit_position must fall back to state_ecef."""

        def state_ecef(self, t_s: Any) -> tuple[np.ndarray, np.ndarray]:
            return inclined.state_ecef(t_s)

    assert isinstance(Minimal(), ob.Orbit)
    assert np.array_equal(ob.orbit_position(Minimal(), t), inclined.position_ecef(t))
    assert np.array_equal(ob.subsatellite_point(Minimal(), T0)[1], ob.subsatellite_point(inclined, T0)[1])
    # a perturbed orbit can wrap a protocol-only orbit
    wrapped = ob.PerturbedOrbit(Minimal(), [10.0, 0.0, 0.0], [0.0, 0.0, 0.0], T0)
    assert np.linalg.norm(wrapped.position_ecef(T0)) == pytest.approx(A_GEO + 10.0, abs=1e-6)
