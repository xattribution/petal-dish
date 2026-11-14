"""geometry: light-time delays, rates, predictors, visibility and search windows.

Includes MATH.md section 12 items 2, 3 and 4. References are closed forms, root finding
in an inertial frame, or finite differences of scalar calls: never the function's own
vectorised output.
"""
from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from scipy.optimize import brentq
from sgp4.propagation import gstime

from petal_locate.core import geometry as gm
from petal_locate.core import orbits as ob
from petal_locate.core import types as core_types
from petal_locate.core.constants import C, GM, JD_UNIX_EPOCH, OMEGA_E, WGS84_A
from petal_locate.core.geodesy import ecef_to_lla, lla_to_ecef, look_angles, rz
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.core.types import PathSpec, Uncertainty

A_GEO = (GM / OMEGA_E**2) ** (1.0 / 3.0)
EPOCH = sec(ns_from_iso("2026-01-01T00:00:00Z"))
T_MID = EPOCH + 3 * 3600.0  # MATH.md section 12 item 8 epoch
H = gm.STENCIL_STEP_S

# Pacific UHF-like constellation (MATH.md section 12 item 8) and two sites.
ORBITS: dict[str, Any] = {
    "s1": ob.GeoOrbit(-177.0, 5.0, phase_deg=40.0),
    "s2": ob.GeoOrbit(172.0, 4.0, phase_deg=200.0),
    "s3": ob.GeoOrbit(75.0, 5.0, phase_deg=310.0),
}
SITES: dict[str, np.ndarray] = {
    "hnl": lla_to_ecef(21.31, -157.86, 15.0),
    "tyo": lla_to_ecef(35.68, 139.77, 50.0),
    "mnl": lla_to_ecef(14.60, 120.98, 20.0),
}
EMITTER = lla_to_ecef(30.37, 140.41, 0.0)
PATH_A = PathSpec("s1", "hnl", 300e6, -41.0e6)
PATH_B = PathSpec("s2", "tyo", 300e6, -33.6e6)
PATH_C = PathSpec("s3", "mnl", 300e6, -41.0e6, tau_x_s=2.5e-6)


def make_geo_tle(lon_deg: float, inc_deg: float) -> tuple[str, str]:
    """A syntactically valid geostationary element set with correct checksums (epoch 2026-11-10)."""
    epoch = dt.datetime(2026, 11, 10)
    jd = JD_UNIX_EPOCH + (epoch - dt.datetime(1970, 1, 1)).total_seconds() / 86400.0
    raan, argp = 80.0, 30.0
    mean_anomaly = (lon_deg + np.degrees(gstime(jd)) - raan - argp) % 360.0
    day = (epoch - dt.datetime(epoch.year, 1, 1)).total_seconds() / 86400.0 + 1.0
    l1 = f"1 99002U 26001A   {epoch.year % 100:02d}{day:012.8f}  .00000000  00000-0  00000-0 0  999"
    l2 = f"2 99002 {inc_deg:8.4f} {raan:8.4f} 0001000 {argp:8.4f} {mean_anomaly:8.4f} {1.00273791:11.8f}{1:5d}"
    checksum = lambda line: sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line) % 10
    return l1 + str(checksum(l1)), l2 + str(checksum(l2))


T_NOV = sec(ns_from_iso("2026-11-13T07:00:00Z"))
LEO = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 10.0, epoch="2026-11-13T00:00:00Z")
LEO2 = ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 12.5, epoch="2026-11-13T00:00:00Z")
TLE_GEO = ob.TleOrbit(*make_geo_tle(172.0, 4.0))
KEPLER_GEO = ob.KeplerOrbit(4.2164172e7, 0.0003, 4.0, 80.0, 30.0, 200.0, epoch="2026-11-01T00:00:00Z")


def leo_ground_points(orbit: Any, t: float) -> tuple[np.ndarray, np.ndarray]:
    """An emitter and a site a few degrees from the sub-satellite point (satellite well above both)."""
    lat, lon, _ = ob.subsatellite_point(orbit, t)
    return lla_to_ecef(lat + 3.0, lon - 4.0, 100.0), lla_to_ecef(lat - 2.0, lon + 5.0, 30.0)


def central(f: Any, t: float, step: float) -> Any:
    """Fourth-order central difference of a scalar-time function (step exactly representable)."""
    return (8.0 * (f(t + step) - f(t - step)) - (f(t + 2 * step) - f(t - 2 * step))) / (12.0 * step)


class Region:
    """Stand-in for the configuration Region (ARCHITECTURE.md section 3)."""

    def __init__(self, lat_min: float = -70.0, lat_max: float = 70.0, lon_min: float = -180.0, lon_max: float = 180.0) -> None:
        self.lat_min, self.lat_max, self.lon_min, self.lon_max = lat_min, lat_max, lon_min, lon_max

    def lon_span_deg(self) -> float:
        if self.lon_min == -180.0 and self.lon_max == 180.0:
            return 360.0
        return (self.lon_max - self.lon_min) % 360.0

    def contains(self, lat_deg: Any, lon_deg: Any) -> np.ndarray:
        lat, lon = np.asarray(lat_deg, dtype=float), np.asarray(lon_deg, dtype=float)
        in_lat = (lat >= self.lat_min) & (lat <= self.lat_max)
        return in_lat & (np.mod(lon - self.lon_min, 360.0) <= self.lon_span_deg())

    def grid(self, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
        lat = self.lat_min + step_deg * np.arange(int(np.floor((self.lat_max - self.lat_min) / step_deg + 1e-9)) + 1)
        span = self.lon_span_deg()
        n_lon = int(np.floor(span / step_deg + 1e-9)) + (0 if span >= 360.0 else 1)
        return lat, self.lon_min + step_deg * np.arange(n_lon)


class BareRegion:
    """Only the two documented methods, with a contains() that expects [-180, 180) longitudes."""

    def __init__(self, inner: Region) -> None:
        self._inner = inner

    def contains(self, lat_deg: Any, lon_deg: Any) -> np.ndarray:
        lon = np.asarray(lon_deg, dtype=float)
        return self._inner.contains(lat_deg, lon) & (lon >= -180.0) & (lon < 180.0)

    def grid(self, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
        return self._inner.grid(step_deg)


# ----------------------------------------------------------------------------- MATH 12.2, 12.3


def test_math12_2_uplink_delay_directly_under_a_geostationary_satellite() -> None:
    assert A_GEO - WGS84_A == pytest.approx(35786035.37, abs=0.01)
    for lon in (0.0, -177.0, 75.0):
        tau_u = gm.uplink_delay(lla_to_ecef(0.0, lon, 0.0), ob.GeoOrbit(lon), T_MID)
        assert tau_u == pytest.approx((A_GEO - WGS84_A) / C, abs=3e-12)  # within 1 mm of range
        assert tau_u == pytest.approx(0.119369365075, abs=3e-12)
        # what is left is the emitter's 55 m of eastward travel during the flight, seen at right
        # angles: a r phi^2 / (2 rho) with phi = OMEGA_E tau, 0.28 mm
        phi = OMEGA_E * tau_u
        second_order = A_GEO * WGS84_A * phi**2 / (2.0 * (A_GEO - WGS84_A)) / C
        assert tau_u - (A_GEO - WGS84_A) / C == pytest.approx(second_order, abs=2e-14)


def test_math12_3_sagnac_uplink_and_downlink() -> None:
    orbit = ob.GeoOrbit(0.0)
    r = orbit.position_ecef(T_MID)
    x = lla_to_ecef(0.0, 60.0, 0.0)  # on the equator, 60 degrees east of the satellite
    first_order = OMEGA_E * np.cross(x, r)[2] / C**2
    assert first_order == pytest.approx(-1.88964e-7, abs=1e-12)

    up = gm.uplink_delay(x, orbit, T_MID) - np.linalg.norm(r - x) / C
    assert up == pytest.approx(first_order, abs=1e-12)  # within 1 ps
    down = gm.downlink_delay(orbit, x, T_MID) - np.linalg.norm(x - r) / C
    assert down == pytest.approx(-first_order, abs=1e-12)
    assert down == pytest.approx(1.88964e-7, abs=2e-12)  # the document's six-digit figure
    # the true second-order residual is 0.66 ps: the delays are solved, not approximated
    assert abs(up - first_order) == pytest.approx(0.66e-12, abs=0.05e-12)
    assert abs(down + first_order) == pytest.approx(0.66e-12, abs=0.05e-12)
    # mirror geometry (emitter west of the satellite): signs flip
    x_w = lla_to_ecef(0.0, -60.0, 0.0)
    assert gm.uplink_delay(x_w, orbit, T_MID) - np.linalg.norm(r - x_w) / C == pytest.approx(-first_order, abs=1e-12)
    assert gm.downlink_delay(orbit, x_w, T_MID) - np.linalg.norm(r - x_w) / C == pytest.approx(first_order, abs=1e-12)


# ----------------------------------------------------------------------------- brute-force light time


def inertial(pos_ecef: np.ndarray, since_frame_s: float) -> np.ndarray:
    """An ECEF position, ``since_frame_s`` after the instant at which the inertial frame used
    here coincided with ECEF, expressed in that inertial frame (the Earth has turned since)."""
    return rz(OMEGA_E * since_frame_s) @ pos_ecef


def brute_uplink(x_e: np.ndarray, orbit: Any, t_arrive: float, back_s: float = 0.0) -> float:
    """Emission-to-arrival time by root finding on inertial-frame positions.

    The arrival is at ``t_arrive - back_s``; intervals are kept as small numbers because a
    float64 near 1.8e9 s only resolves 2.4e-7 s.
    """
    lead = 137.0  # the frame coincides with ECEF 137 s before the arrival: not the implementation's frame
    sat = inertial(ob.orbit_position(orbit, t_arrive, -back_s), lead)
    f = lambda tau: np.linalg.norm(sat - inertial(x_e, lead - tau)) - C * tau
    return brentq(f, 0.0, 1.0, xtol=1e-18, rtol=1e-15)


def brute_downlink(orbit: Any, x_g: np.ndarray, t_rx: float) -> float:
    lag = -41.0  # the frame coincides with ECEF 41 s after the reception
    site = inertial(x_g, lag)
    f = lambda tau: np.linalg.norm(site - inertial(ob.orbit_position(orbit, t_rx, -tau), lag - tau)) - C * tau
    return brentq(f, 0.0, 1.0, xtol=1e-18, rtol=1e-15)


def light_time_cases() -> list[tuple[str, Any, np.ndarray, np.ndarray, float]]:
    leo_e, leo_g = leo_ground_points(LEO, T_NOV)
    return [
        ("geo_inclined", ORBITS["s1"], EMITTER, SITES["hnl"], T_MID),
        ("geo_low_elevation", ORBITS["s3"], EMITTER, SITES["tyo"], T_MID + 7777.0),
        ("tle_geo", TLE_GEO, EMITTER, SITES["tyo"], T_NOV),
        ("kepler_geo", KEPLER_GEO, EMITTER, SITES["mnl"], T_NOV),
        ("kepler_leo", LEO, leo_e, leo_g, T_NOV),
        ("aircraft_under_leo", LEO, lla_to_ecef(*ecef_to_lla(leo_e)[:2], 12000.0), leo_g, T_NOV + 20.0),
    ]


@pytest.mark.parametrize("case", light_time_cases(), ids=lambda c: c[0])
def test_light_time_against_inertial_frame_root_finding(case: tuple[str, Any, np.ndarray, np.ndarray, float]) -> None:
    _, orbit, x_e, x_g, t = case
    tau_u = gm.uplink_delay(x_e, orbit, t)
    tau_d = gm.downlink_delay(orbit, x_g, t)
    assert tau_u == pytest.approx(brute_uplink(x_e, orbit, t), abs=2e-16)
    assert tau_d == pytest.approx(brute_downlink(orbit, x_g, t), abs=2e-16)
    # neglecting Earth rotation and satellite motion during the flight is a visible error
    naive_u = np.linalg.norm(ob.orbit_position(orbit, t) - x_e) / C
    naive_d = np.linalg.norm(ob.orbit_position(orbit, t) - x_g) / C
    assert abs(tau_u - naive_u) > 1e-10 and abs(tau_d - naive_d) > 1e-10


@pytest.mark.parametrize("case", light_time_cases(), ids=lambda c: c[0])
def test_bent_pipe_against_event_by_event_inertial_calculation(case: tuple[str, Any, np.ndarray, np.ndarray, float]) -> None:
    """Follow the signal backwards: site reception, satellite transmission, transponder, emission."""
    _, orbit, x_e, x_g, t_rx = case
    tau_x = 3.0e-6
    tau_d = brute_downlink(orbit, x_g, t_rx)
    tau_u = brute_uplink(x_e, orbit, t_rx, back_s=tau_d + tau_x)  # arrival at t_rx - tau_d - tau_x
    bp = gm.bent_pipe(x_e, orbit, x_g, t_rx, tau_x)
    assert bp.tau_d == pytest.approx(tau_d, abs=2e-16)
    assert bp.tau_u == pytest.approx(tau_u, abs=2e-16)
    assert bp.D == pytest.approx(tau_d + tau_x + tau_u, abs=4e-16)
    assert bp.t_sat_s == pytest.approx(t_rx - tau_d - tau_x, abs=3e-7)  # float seconds near 1.8e9 resolve 2.4e-7 s
    # the public pieces compose the same way
    assert bp.tau_d == gm.downlink_delay(orbit, x_g, t_rx)
    assert bp.D == (bp.tau_d + tau_x) + bp.tau_u
    assert bp.t_sat_s == t_rx - (bp.tau_d + tau_x)
    # uplink_delay at the reported (rounded) t_sat_s agrees to the uplink rate times that rounding
    tau_u_dot = gm.bent_pipe_rates(x_e, orbit, x_g, t_rx, tau_x)[2]
    assert bp.tau_u == pytest.approx(gm.uplink_delay(x_e, orbit, bp.t_sat_s), abs=abs(tau_u_dot) * 2.4e-7 + 1e-16)


def test_uplink_one_pass_solution_equals_long_plain_iteration() -> None:
    """Six plain fixed-point passes from the bare range, written with explicit rotations."""
    rng = np.random.default_rng(3)
    lat, lon = rng.uniform(-60, 60, 40), rng.uniform(100, 220, 40)
    x = lla_to_ecef(lat, lon, rng.uniform(0, 3000, 40))
    for orbit, t in ((ORBITS["s1"], T_MID), (ORBITS["s2"], T_MID + 5000.0)):
        r = orbit.position_ecef(t)
        got = gm.uplink_delay(x, orbit, t)
        for k in range(40):
            tau = np.linalg.norm(r - x[k]) / C
            for _ in range(6):
                tau = np.linalg.norm(r - rz(-OMEGA_E * tau) @ x[k]) / C
            assert got[k] == pytest.approx(tau, abs=6e-17)


# ----------------------------------------------------------------------------- shapes


def test_shapes_follow_the_architecture_contract() -> None:
    x = lla_to_ecef(np.linspace(-20, 40, 6).reshape(2, 3), 150.0, 0.0)  # [2, 3, 3]
    t = T_MID + np.arange(4) * 60.0
    orbit, site = ORBITS["s1"], SITES["hnl"]
    bp = gm.bent_pipe(x, orbit, site, t)
    assert bp.D.shape == bp.tau_u.shape == (2, 3, 4)
    assert bp.tau_d.shape == bp.t_sat_s.shape == (4,)
    bp0 = gm.bent_pipe(x, orbit, site, float(t[2]))
    assert bp0.D.shape == bp0.tau_u.shape == (2, 3)
    assert np.ndim(bp0.tau_d) == 0 and np.ndim(bp0.t_sat_s) == 0
    assert np.array_equal(bp0.D, bp.D[..., 2]) and bp0.tau_d == bp.tau_d[2]
    single = gm.bent_pipe(x[1, 2], orbit, site, float(t[2]))
    assert np.ndim(single.D) == 0 and single.D == bp.D[1, 2, 2]

    d_dot, tau_d_dot, tau_u_dot = gm.bent_pipe_rates(x, orbit, site, t)
    assert d_dot.shape == tau_u_dot.shape == (2, 3, 4) and tau_d_dot.shape == (4,)
    d0, td0, tu0 = gm.bent_pipe_rates(x[1, 2], orbit, site, float(t[2]))
    assert (np.ndim(d0), np.ndim(td0), np.ndim(tu0)) == (0, 0, 0)
    assert d0 == d_dot[1, 2, 2] and td0 == tau_d_dot[2] and tu0 == tau_u_dot[1, 2, 2]

    assert gm.uplink_delay(x, orbit, t).shape == (2, 3, 4)
    assert gm.uplink_delay(x, orbit, float(t[0])).shape == (2, 3)
    assert gm.downlink_delay(orbit, site, t).shape == (4,)
    assert np.ndim(gm.downlink_delay(orbit, site, float(t[0]))) == 0
    assert gm.downlink_delay(orbit, np.stack([site, SITES["tyo"]]), t).shape == (2, 4)

    for fn in (gm.predict_tdoa, gm.predict_fdoa):
        full = fn(x, PATH_A, PATH_B, t, ORBITS, SITES)
        assert full.shape == (2, 3, 4)
        assert fn(x, PATH_A, PATH_B, float(t[1]), ORBITS, SITES).shape == (2, 3)
        one = fn(x[0, 1], PATH_A, PATH_B, float(t[1]), ORBITS, SITES)
        assert np.ndim(one) == 0 and one == full[0, 1, 1]
    assert gm.elevation_deg(x, orbit, t).shape == (2, 3, 4)
    assert gm.elevation_deg(x, orbit, float(t[0])).shape == (2, 3)
    tdoa, fdoa = gm.self_peak("s1", -41e6, 300e6, PATH_A, PATH_B, t, ORBITS, SITES)
    assert tdoa.shape == fdoa.shape == (4,)


def test_path_spec_is_re_exported() -> None:
    assert gm.PathSpec is core_types.PathSpec


# ----------------------------------------------------------------------------- MATH 12.4: rates


def rate_cases() -> list[tuple[str, Any, np.ndarray, np.ndarray, float]]:
    leo_e, leo_g = leo_ground_points(LEO, T_NOV)
    return [
        ("geo", ORBITS["s1"], EMITTER, SITES["hnl"], T_MID),
        ("tle", TLE_GEO, EMITTER, SITES["tyo"], T_NOV),
        ("kepler", KEPLER_GEO, EMITTER, SITES["mnl"], T_NOV),
        ("kepler_leo", LEO, leo_e, leo_g, T_NOV),
        ("perturbed", ob.PerturbedOrbit(TLE_GEO, [2000.0, 20000.0, 10000.0], [0.3, 0.3, 0.7], T_NOV), EMITTER, SITES["tyo"], T_NOV),
    ]


@pytest.mark.parametrize("case", rate_cases(), ids=lambda c: c[0])
def test_math12_4_rates_equal_a_central_difference_of_bent_pipe(case: tuple[str, Any, np.ndarray, np.ndarray, float]) -> None:
    _, orbit, x_e, x_g, t = case
    tau_x = 1.5e-6
    d_dot, tau_d_dot, tau_u_dot = gm.bent_pipe_rates(x_e, orbit, x_g, t, tau_x)
    # plain two-point central difference, step 2**-7 s (truncation and rounding both below 1e-13)
    step = 2.0**-7
    plus, minus = gm.bent_pipe(x_e, orbit, x_g, t + step, tau_x), gm.bent_pipe(x_e, orbit, x_g, t - step, tau_x)
    assert d_dot == pytest.approx((plus.D - minus.D) / (2 * step), abs=1e-13)
    assert tau_d_dot == pytest.approx((plus.tau_d - minus.tau_d) / (2 * step), abs=1e-13)
    # tau_u_dot is with respect to the uplink's own arrival time: difference the uplink delay
    # about t_sat = t - back (brute force, arrival time kept as two terms)
    back = float(gm.bent_pipe(x_e, orbit, x_g, t, tau_x).tau_d) + tau_x
    up = lambda shift: brute_uplink(x_e, orbit, t + shift, back_s=back)
    assert tau_u_dot == pytest.approx((up(step) - up(-step)) / (2 * step), abs=1e-13)
    # ... and the literal 5-point stencil of MATH.md section 3 on that function, h = 0.5 s
    assert tau_u_dot == pytest.approx(central(up, 0.0, H), abs=2e-15)
    assert tau_d_dot == pytest.approx(central(lambda tt: gm.downlink_delay(orbit, x_g, tt), t, H), abs=1e-16)
    # the identity that ties them together
    assert d_dot == pytest.approx(tau_d_dot + tau_u_dot * (1.0 - tau_d_dot), abs=1e-18)
    assert abs(d_dot) > 1e-10  # the geometry is actually moving


def test_rates_are_accurate_to_1e_15_on_geosynchronous_geometry() -> None:
    """Against a Chebyshev fit of D over +/- 4 s differentiated at the centre (independent of the stencil)."""
    nodes = np.round(np.cos(np.pi * (np.arange(24) + 0.5) / 24) * 4.0 * 1024.0) / 1024.0  # exact offsets
    for orbit, site, t in ((ORBITS["s1"], SITES["hnl"], T_MID), (ORBITS["s3"], SITES["mnl"], T_MID + 30000.0)):
        d = gm.bent_pipe(EMITTER, orbit, site, t + nodes).D
        coeff = np.polynomial.chebyshev.chebfit(nodes / 4.0, d - d.mean(), 8)
        reference = np.polynomial.chebyshev.chebval(0.0, np.polynomial.chebyshev.chebder(coeff)) / 4.0
        assert gm.bent_pipe_rates(EMITTER, orbit, site, t)[0] == pytest.approx(reference, abs=1e-15)


def test_rates_do_not_use_state_velocity() -> None:
    class WrongVelocity:
        """A protocol-only orbit whose velocity is nonsense."""

        def state_ecef(self, t_s: Any) -> tuple[np.ndarray, np.ndarray]:
            r, v = ORBITS["s1"].state_ecef(t_s)
            return r, v + 1.0e4

    good = gm.bent_pipe_rates(EMITTER, ORBITS["s1"], SITES["hnl"], T_MID)
    bad = gm.bent_pipe_rates(EMITTER, WrongVelocity(), SITES["hnl"], T_MID)
    # 1e4 m/s of velocity error would be 3e-5 s/s; what is left is the float64 rounding of
    # t - tau for an orbit that only implements the protocol (no split-time evaluation)
    assert good == pytest.approx(bad, abs=5e-15)
    paths = (PATH_A, PATH_B, T_MID)
    wrong = gm.predict_fdoa(EMITTER, *paths, {**ORBITS, "s1": WrongVelocity()}, SITES)
    assert wrong == pytest.approx(gm.predict_fdoa(EMITTER, *paths, ORBITS, SITES), abs=2e-6)


def test_transponder_delay_moves_the_uplink_epoch() -> None:
    orbit, site = LEO, leo_ground_points(LEO, T_NOV)[1]
    x_e = leo_ground_points(LEO, T_NOV)[0]
    tau_x = 4.0e-3
    with_delay, without = gm.bent_pipe(x_e, orbit, site, T_NOV, tau_x), gm.bent_pipe(x_e, orbit, site, T_NOV)
    assert with_delay.tau_d == without.tau_d
    assert with_delay.t_sat_s == pytest.approx(without.t_sat_s - tau_x, abs=3e-7)
    tau_u_dot = gm.bent_pipe_rates(x_e, orbit, site, T_NOV)[2]
    assert abs(tau_u_dot) > 1e-7
    # the uplink is sampled tau_x earlier: D changes by tau_x (1 - tau_u_dot) + O(tau_x^2 tau_u_ddot)
    assert with_delay.D - without.D == pytest.approx(tau_x * (1.0 - tau_u_dot), abs=5e-12)
    assert with_delay.tau_u == pytest.approx(brute_uplink(x_e, orbit, T_NOV, back_s=float(without.tau_d) + tau_x), abs=2e-16)


# ----------------------------------------------------------------------------- split time


def test_delays_are_exact_in_a_sub_resolution_time_offset() -> None:
    """dt_s: delays at t + dt_s without rounding the sum to float64 (2.4e-7 s resolution)."""
    x_e, site = leo_ground_points(LEO, T_NOV)
    rates = gm.bent_pipe_rates(x_e, LEO, site, T_NOV)
    base = gm.bent_pipe(x_e, LEO, site, T_NOV)
    assert T_NOV + 1e-8 == T_NOV  # 10 ns cannot be added to a Unix time in float64
    for dt_s in (1e-8, -3e-8, 1e-7):
        shifted = gm.bent_pipe(x_e, LEO, site, T_NOV, dt_s=dt_s)
        assert shifted.D - base.D == pytest.approx(rates[0] * dt_s, abs=2e-17)
        assert shifted.tau_d - base.tau_d == pytest.approx(rates[1] * dt_s, abs=2e-17)
        assert gm.downlink_delay(LEO, site, T_NOV, dt_s) == shifted.tau_d
        assert abs(shifted.D - base.D) > 3e-14  # a real, resolved change
        up = lambda tt: gm.uplink_delay(x_e, LEO, tt)
        assert gm.uplink_delay(x_e, LEO, T_NOV, dt_s) - up(T_NOV) == pytest.approx(central(up, T_NOV, 2.0**-5) * dt_s, abs=2e-17)
    # a representable sum gives the same answer either way
    both = gm.bent_pipe(x_e, LEO, site, T_NOV, 1e-6, dt_s=0.25)
    direct = gm.bent_pipe(x_e, LEO, site, T_NOV + 0.25, 1e-6)
    assert both.D == pytest.approx(direct.D, abs=1e-16) and both.t_sat_s == pytest.approx(direct.t_sat_s, abs=3e-7)
    r_split = gm.bent_pipe_rates(x_e, LEO, site, T_NOV, 0.0, dt_s=0.25)
    r_direct = gm.bent_pipe_rates(x_e, LEO, site, T_NOV + 0.25)
    assert r_split == pytest.approx(r_direct, abs=1e-15)
    # arrays: a capture's sample times as start + n / fs
    rel = np.arange(6) / 31250.0
    series = gm.bent_pipe(x_e, LEO, site, T_NOV, dt_s=rel)
    assert series.D.shape == series.tau_d.shape == (6,)
    assert series.D[3] == gm.bent_pipe(x_e, LEO, site, T_NOV, dt_s=float(rel[3])).D
    with pytest.raises(ValueError, match="dt_s"):
        gm.bent_pipe(x_e, LEO, site, T_NOV + np.arange(3.0), dt_s=np.zeros(4))
    with pytest.raises(ValueError, match="dt_s"):
        gm.bent_pipe(x_e, LEO, site, T_NOV, dt_s=float("nan"))


def test_delay_is_smooth_over_a_capture_in_low_orbit() -> None:
    """D at t0 + n / fs over 65 ms at 31.25 kHz: a degree-6 fit leaves 1e-16 s with dt_s, and
    picoseconds of quantisation scatter when the sample times are rounded to float64 first
    (1e-3 cycle of a 300 MHz carrier)."""
    x_e, site = leo_ground_points(LEO, T_NOV)
    rel = np.arange(2048) / 31250.0
    x = 2.0 * rel / rel[-1] - 1.0

    def scatter(d: np.ndarray) -> float:
        y = d - d[0]
        fit = np.polynomial.chebyshev.chebfit(x, y, 6)
        return float(np.max(np.abs(y - np.polynomial.chebyshev.chebval(x, fit))))

    assert scatter(gm.bent_pipe(x_e, LEO, site, T_NOV, dt_s=rel).D) < 5e-16
    assert scatter(gm.bent_pipe(x_e, LEO, site, T_NOV + rel).D) > 5e-13


# ----------------------------------------------------------------------------- baseband frequency


def test_baseband_frequency_formula_and_signs() -> None:
    f_e, trans, f_tune = 300.0e6, -41.0e6, 259.0e6
    assert gm.baseband_frequency(f_e, trans, f_tune, 0.0, 0.0) == 0.0
    assert gm.baseband_frequency(f_e, trans, f_tune + 1000.0, 0.0, 0.0) == -1000.0
    # approaching on the uplink (delay shrinking) raises the frequency by f_e |tau_u_dot|
    assert gm.baseband_frequency(f_e, trans, f_tune, 0.0, -1e-8) == pytest.approx(3.0, abs=1e-12)
    assert gm.baseband_frequency(f_e, trans, f_tune, -1e-8, 0.0) == pytest.approx(2.59, abs=1e-12)
    td, tu, eps = 3.1e-6, -2.2e-6, 4e-9
    expected = (f_e + trans - f_tune * (1 + eps)) - (f_e + trans) * td - f_e * tu * (1 - td)
    assert gm.baseband_frequency(f_e, trans, f_tune, td, tu, eps) == pytest.approx(expected, abs=1e-7)
    # MATH.md section 12 item 6: a satellite oscillator error eps raises the path by eps T (1 - tau_d_dot)
    eps_sat = 1e-6
    shifted = gm.baseband_frequency(f_e, trans * (1 + eps_sat), f_tune, td, tu)
    assert shifted - gm.baseband_frequency(f_e, trans, f_tune, td, tu) == pytest.approx(eps_sat * trans * (1 - td), abs=1e-9)
    # a site oscillator error lowers it by eps_g f_tune
    assert gm.baseband_frequency(f_e, trans, f_tune, td, tu, 1e-9) - gm.baseband_frequency(f_e, trans, f_tune, td, tu) == pytest.approx(-0.259, abs=1e-9)
    out = gm.baseband_frequency(f_e, trans, f_tune, np.zeros(3), np.zeros((2, 3)))
    assert out.shape == (2, 3)


def path_phase_cycles(x_e: np.ndarray, orbit: Any, x_g: np.ndarray, spec: PathSpec, t: float) -> float:
    """Carrier phase (cycles) of MATH.md section 4 for a channel tuned to f_e + T, as a function of
    reception time. The first term, (f_e + T - f_tune) t, is zero for that tuning."""
    bp = gm.bent_pipe(x_e, orbit, x_g, t, spec.tau_x_s)
    return -(spec.f_e_hz + spec.translation_hz) * (bp.tau_d + spec.tau_x_s) - spec.f_e_hz * bp.tau_u


def at_shifted_time(f: Any, t: float, shift: float) -> float:
    """f(t + shift) for a smooth scalar-time function, although t + shift is not a float64.

    Evaluates at the nearest representable time and adds the first-order term for the
    remainder (at most 1.2e-7 s), with the slope from a finite difference of f itself.
    """
    nearest = t + shift
    remainder = (t - nearest) + shift  # exact
    slope = (f(nearest + 2.0**-6) - f(nearest - 2.0**-6)) / 2.0**-5
    return float(f(nearest) + slope * remainder)


def brute_tdoa(x_e: np.ndarray, a: PathSpec, b: PathSpec, t: float, orbits: dict[str, Any], sites: dict[str, np.ndarray]) -> float:
    """TDOA = D_A(t + TDOA) - D_B(t), iterated to convergence with scalar calls."""
    d_a = lambda tt: gm.bent_pipe(x_e, orbits[a.sat_id], sites[a.site_id], tt, a.tau_x_s).D
    d_b = gm.bent_pipe(x_e, orbits[b.sat_id], sites[b.site_id], t, b.tau_x_s).D
    tdoa = 0.0
    for _ in range(8):
        tdoa = at_shifted_time(d_a, t, tdoa) - d_b
    return float(tdoa)


def brute_fdoa(x_e: np.ndarray, a: PathSpec, b: PathSpec, t: float, orbits: dict[str, Any], sites: dict[str, np.ndarray]) -> float:
    """FDOA = d(phase_A)/dt at t + TDOA minus d(phase_B)/dt at t, by differencing the phase."""
    tdoa = brute_tdoa(x_e, a, b, t, orbits, sites)
    step = 2.0**-5
    phase_a = lambda tt: path_phase_cycles(x_e, orbits[a.sat_id], sites[a.site_id], a, tt)
    f_a = at_shifted_time(lambda tt: central(phase_a, tt, step), t, tdoa)
    f_b = central(lambda tt: path_phase_cycles(x_e, orbits[b.sat_id], sites[b.site_id], b, tt), t, step)
    return float(f_a - f_b)


def leo_scenario() -> tuple[dict[str, Any], dict[str, np.ndarray], np.ndarray, PathSpec, PathSpec]:
    x_e, site = leo_ground_points(LEO, T_NOV)
    orbits = {"leo1": LEO, "leo2": LEO2}
    sites = {"g": site, "g2": lla_to_ecef(*np.add(ecef_to_lla(site)[:2], (1.5, -2.0)), 10.0)}
    return orbits, sites, x_e, PathSpec("leo1", "g", 300e6, -41e6, 1e-6), PathSpec("leo2", "g2", 300e6, -41e6)


def test_predictors_against_scalar_brute_force_geo() -> None:
    rng = np.random.default_rng(12)
    lat, lon = rng.uniform(-45, 55, 12), rng.uniform(125, 200, 12)
    x = lla_to_ecef(lat, lon, 0.0)
    for a, b, t in ((PATH_A, PATH_B, T_MID), (PATH_B, PATH_C, T_MID + 12345.0), (PATH_C, PATH_A, T_MID - 4000.0)):
        tdoa = gm.predict_tdoa(x, a, b, t, ORBITS, SITES)
        fdoa = gm.predict_fdoa(x, a, b, t, ORBITS, SITES)
        both = gm.predict_pair(x, a, b, t, ORBITS, SITES)
        assert np.array_equal(both[0], tdoa) and np.array_equal(both[1], fdoa)
        for k in range(12):
            # a few units in the last place of the two 0.25 s delays
            assert tdoa[k] == pytest.approx(brute_tdoa(x[k], a, b, t, ORBITS, SITES), abs=1e-15)
            # 300 MHz times the 5e-15 s/s rounding of a differenced 0.25 s delay
            assert fdoa[k] == pytest.approx(brute_fdoa(x[k], a, b, t, ORBITS, SITES), abs=2e-6)
        assert np.max(np.abs(tdoa)) > 1e-3 and np.max(np.abs(fdoa)) > 1.0


def test_predict_fdoa_is_the_time_derivative_of_the_phase_in_low_orbit() -> None:
    """Fast geometry: Doppler of kilohertz, and the epoch shift t_mid + TDOA matters."""
    orbits, sites, x_e, a, b = leo_scenario()
    for t in (T_NOV, T_NOV + 30.0, T_NOV - 45.0):
        tdoa = gm.predict_tdoa(x_e, a, b, t, orbits, sites)
        fdoa = gm.predict_fdoa(x_e, a, b, t, orbits, sites)
        d_dot_a = gm.bent_pipe_rates(x_e, orbits["leo1"], sites["g"], t, a.tau_x_s)[0]
        # one fixed-point pass leaves D_dot_A^2 |TDOA| (picoseconds here), as documented
        assert abs(tdoa - brute_tdoa(x_e, a, b, t, orbits, sites)) < 1.5 * d_dot_a**2 * abs(tdoa) + 1e-16
        assert fdoa == pytest.approx(brute_fdoa(x_e, a, b, t, orbits, sites), abs=2e-5)
        assert abs(fdoa) > 100.0
        # evaluating path A at t_mid instead of t_mid + TDOA would be wrong by far more than that
        rates_a = gm.bent_pipe_rates(x_e, orbits["leo1"], sites["g"], t, a.tau_x_s)
        rates_b = gm.bent_pipe_rates(x_e, orbits["leo2"], sites["g2"], t, b.tau_x_s)
        unshifted = gm.baseband_frequency(a.f_e_hz, a.translation_hz, a.f_tune_hz, rates_a[1], rates_a[2]) - gm.baseband_frequency(
            b.f_e_hz, b.translation_hz, b.f_tune_hz, rates_b[1], rates_b[2]
        )
        assert abs(unshifted - fdoa) > 1e-3


def test_predict_fdoa_at_zero_shift_is_baseband_frequency_of_the_rates() -> None:
    """Same satellite, same site, same delay: TDOA is zero and FDOA reduces to the per-path formula."""
    x = lla_to_ecef(np.array([10.0, 30.37]), np.array([150.0, 140.41]), 0.0)
    a = PathSpec("s1", "hnl", 300.0e6, -41.0e6)
    b = PathSpec("s1", "hnl", 300.0e6 + 2.0e3, -41.0e6)  # same path, different assumed carrier
    assert np.all(gm.predict_tdoa(x, a, b, T_MID, ORBITS, SITES) == 0.0)
    _, td, tu = gm.bent_pipe_rates(x, ORBITS["s1"], SITES["hnl"], T_MID)
    expected = gm.baseband_frequency(a.f_e_hz, a.translation_hz, a.f_tune_hz, td, tu) - gm.baseband_frequency(
        b.f_e_hz, b.translation_hz, b.f_tune_hz, td, tu
    )
    assert np.allclose(gm.predict_fdoa(x, a, b, T_MID, ORBITS, SITES), expected, rtol=0, atol=1e-9)


def test_swapping_a_and_b_flips_the_signs_up_to_the_epoch_term() -> None:
    """h_AB + h_BA is not zero because path A is evaluated at t_mid + TDOA:

        tdoa_AB + tdoa_BA = TDOA (D_dot_A - D_dot_B)            (about -TDOA FDOA / f_e)
        fdoa_AB + fdoa_BA = TDOA (f_bb,A_dot - f_bb,B_dot)

    to first order. For the Pacific UHF geometry (TDOA of milliseconds, FDOA of tens of
    hertz) that is a few 1e-10 s and a few 1e-5 Hz.
    """
    rng = np.random.default_rng(21)
    x = lla_to_ecef(rng.uniform(-40, 50, 30), rng.uniform(130, 195, 30), 0.0)
    worst_t, worst_f = 0.0, 0.0
    for a, b in ((PATH_A, PATH_B), (PATH_A, PATH_C), (PATH_B, PATH_C)):
        t_ab, f_ab = gm.predict_pair(x, a, b, T_MID, ORBITS, SITES)
        t_ba, f_ba = gm.predict_pair(x, b, a, T_MID, ORBITS, SITES)
        rates = {}
        f_dot = {}
        d_ddot = {}
        for spec in (a, b):
            orbit, site = ORBITS[spec.sat_id], SITES[spec.site_id]
            rates[spec] = gm.bent_pipe_rates(x, orbit, site, T_MID, spec.tau_x_s)
            d = lambda tt, spec=spec, orbit=orbit, site=site: gm.bent_pipe(x, orbit, site, tt, spec.tau_x_s).D
            d_ddot[spec] = (d(T_MID + 16.0) - 2.0 * d(T_MID) + d(T_MID - 16.0)) / 256.0

            def f_bb(tt: float, spec: PathSpec = spec, orbit: Any = orbit, site: np.ndarray = site) -> np.ndarray:
                _, td, tu = gm.bent_pipe_rates(x, orbit, site, tt, spec.tau_x_s)
                return gm.baseband_frequency(spec.f_e_hz, spec.translation_hz, spec.f_tune_hz, td, tu)

            f_dot[spec] = (f_bb(T_MID + 8.0) - f_bb(T_MID - 8.0)) / 16.0
        epoch_t = t_ab * (rates[a][0] - rates[b][0])
        epoch_f = t_ab * (f_dot[a] - f_dot[b])
        # first order to within the next term, TDOA^2 D_ddot (femtoseconds) ...
        assert np.max(np.abs((t_ab + t_ba) - epoch_t)) < 1e-14
        # ... and with that term, 0.5 TDOA^2 (D_ddot_A + D_ddot_B), to rounding
        second = 0.5 * t_ab**2 * (d_ddot[a] + d_ddot[b]) + rates[b][0] * (t_ab + t_ba)
        assert np.max(np.abs((t_ab + t_ba) - epoch_t - second)) < 1e-15
        assert np.max(np.abs((f_ab + f_ba) - epoch_f)) < 2e-7
        worst_t = max(worst_t, float(np.max(np.abs(t_ab + t_ba))))
        worst_f = max(worst_f, float(np.max(np.abs(f_ab + f_ba))))
        # antisymmetric to that level, and not more
        assert np.max(np.abs(t_ab + t_ba)) < 5e-9 and np.max(np.abs(f_ab + f_ba)) < 5e-4
    assert 1e-10 < worst_t < 5e-9  # tenths of a nanosecond to nanoseconds
    assert 1e-5 < worst_f < 5e-4


# ----------------------------------------------------------------------------- self peak


def brute_self_tdoa(orbit: Any, g_a: np.ndarray, g_b: np.ndarray, t: float) -> float:
    tau_a = lambda tt: gm.downlink_delay(orbit, g_a, tt)
    tau_b = gm.downlink_delay(orbit, g_b, t)
    tdoa = 0.0
    for _ in range(8):
        tdoa = at_shifted_time(tau_a, t, tdoa) - tau_b
    return float(tdoa)


def test_self_peak_equals_predict_on_the_degenerate_pair_geo() -> None:
    """Both paths through the same satellite: the uplink cancels, whatever the emitter position."""
    a = PathSpec("s2", "hnl", 300e6, -33.6e6)
    b = PathSpec("s2", "tyo", 300e6, -33.6e6)
    tdoa, fdoa = gm.self_peak("s2", -33.6e6, 300e6, a, b, T_MID, ORBITS, SITES)
    assert tdoa == pytest.approx(brute_self_tdoa(ORBITS["s2"], SITES["hnl"], SITES["tyo"], T_MID), abs=1e-16)
    rng = np.random.default_rng(4)
    x = lla_to_ecef(rng.uniform(-50, 50, 25), rng.uniform(110, 230, 25), 0.0)
    p_tdoa, p_fdoa = gm.predict_pair(x, a, b, T_MID, ORBITS, SITES)
    assert np.max(np.abs(p_tdoa - tdoa)) < 2e-16
    assert np.max(np.abs(p_fdoa - fdoa)) < 1e-6  # the neglected uplink-Doppler term is ~1e-8 Hz here
    assert abs(tdoa) > 1e-4 and abs(fdoa) > 1e-3  # not a trivial zero
    # reversed pair: signs flip (to the epoch term)
    tdoa_r, fdoa_r = gm.self_peak("s2", -33.6e6, 300e6, b, a, T_MID, ORBITS, SITES)
    assert tdoa_r == pytest.approx(-tdoa, abs=1e-10) and fdoa_r == pytest.approx(-fdoa, abs=1e-5)


def test_self_peak_in_low_orbit_differs_from_predict_by_the_documented_term() -> None:
    orbits, sites, x_e, a, _ = leo_scenario()
    b = PathSpec("leo1", "g2", 300e6, -41e6, a.tau_x_s)
    tdoa, fdoa = gm.self_peak("leo1", -41e6, 300e6, a, b, T_NOV, orbits, sites)
    assert tdoa == pytest.approx(brute_self_tdoa(LEO, sites["g"], sites["g2"], T_NOV), abs=1e-16)
    p_tdoa, p_fdoa = gm.predict_pair(x_e, a, b, T_NOV, orbits, sites)
    _, td_a, tu = gm.bent_pipe_rates(x_e, LEO, sites["g"], T_NOV + tdoa, a.tau_x_s)
    _, td_b, _ = gm.bent_pipe_rates(x_e, LEO, sites["g2"], T_NOV, b.tau_x_s)
    assert abs(p_tdoa - tdoa) < 1.5 * (td_a + tu) ** 2 * abs(tdoa) + 1e-16
    # predict includes the uplink Doppler on the downlink scale factor; self_peak cannot (no emitter)
    missing = 300e6 * tu * (td_a - td_b)
    assert abs(missing) > 1e-3
    assert p_fdoa - fdoa == pytest.approx(missing, abs=1e-5)


def test_self_peak_includes_the_tuning_offsets() -> None:
    """Satellite s1 (translation -41 MHz) heard in channel A (tuned for s1) and in channel B
    (tuned for s2's -33.6 MHz plan, 2 kHz higher carrier)."""
    a = PathSpec("s1", "hnl", 300.0e6, -41.0e6)
    b = PathSpec("s2", "tyo", 300.002e6, -33.6e6)
    f_e, trans = 300.0005e6, -41.0e6
    tdoa, fdoa = gm.self_peak("s1", trans, f_e, a, b, T_MID, ORBITS, SITES)
    # independent: per-channel baseband frequencies of the two copies of the s1 downlink
    _, td_a, tu_a = gm.bent_pipe_rates(EMITTER, ORBITS["s1"], SITES["hnl"], T_MID + tdoa)
    _, td_b, tu_b = gm.bent_pipe_rates(EMITTER, ORBITS["s1"], SITES["tyo"], T_MID)
    expected = gm.baseband_frequency(f_e, trans, a.f_tune_hz, td_a, tu_a) - gm.baseband_frequency(f_e, trans, b.f_tune_hz, td_b, tu_b)
    assert fdoa == pytest.approx(expected, abs=1e-6)
    assert fdoa == pytest.approx(b.f_tune_hz - a.f_tune_hz, abs=5.0)  # dominated by the 7.402 MHz retune
    assert b.f_tune_hz - a.f_tune_hz == pytest.approx(7.402e6)
    # one site, two channels: no geometry left at all
    b_same_site = PathSpec("s2", "hnl", 300.002e6, -33.6e6)
    assert gm.self_peak("s1", trans, f_e, a, b_same_site, T_MID, ORBITS, SITES) == (0.0, b_same_site.f_tune_hz - a.f_tune_hz)
    assert gm.self_peak("s1", trans, f_e, a, a, T_MID, ORBITS, SITES) == (0.0, 0.0)


# ----------------------------------------------------------------------------- elevation, footprint


def test_elevation_closed_forms_and_look_angle_agreement() -> None:
    orbit = ob.GeoOrbit(0.0)
    assert gm.elevation_deg(lla_to_ecef(0.0, 0.0, 0.0), orbit, T_MID) == pytest.approx(90.0, abs=1e-9)
    gamma = np.radians(60.0)
    expected = np.degrees(np.arctan((np.cos(gamma) - WGS84_A / A_GEO) / np.sin(gamma)))
    assert gm.elevation_deg(lla_to_ecef(0.0, 60.0, 0.0), orbit, T_MID) == pytest.approx(expected, abs=1e-10)
    # on the equator the horizon is at acos(R / r) of longitude
    edge = np.degrees(np.arccos(WGS84_A / A_GEO))
    assert gm.elevation_deg(lla_to_ecef(0.0, edge, 0.0), orbit, T_MID) == pytest.approx(0.0, abs=1e-9)
    assert gm.elevation_deg(lla_to_ecef(0.0, edge + 1.0, 0.0), orbit, T_MID) < 0.0
    # anywhere: equals geodesy.look_angles (WGS84 vertical), including above the ellipsoid
    rng = np.random.default_rng(2)
    pts = lla_to_ecef(rng.uniform(-80, 80, 50), rng.uniform(-180, 180, 50), rng.uniform(0, 9000, 50))
    t = T_MID + np.array([0.0, 20000.0, 60000.0])
    for name in ("s1", "s3"):
        el = gm.elevation_deg(pts, ORBITS[name], t)
        ref = look_angles(pts[:, None, :], ORBITS[name].position_ecef(t)[None, :, :])[1]
        assert np.max(np.abs(el - ref)) < 1e-9
    # geodetic, not geocentric, vertical: straight up along the ellipsoid normal at 45 N reads 90
    class Fixed:
        def state_ecef(self, t_s: Any) -> tuple[np.ndarray, np.ndarray]:
            r = lla_to_ecef(45.0, 10.0, 2.0e7)
            shape = np.shape(t_s)
            return np.broadcast_to(r, shape + (3,)).copy(), np.zeros(shape + (3,))

    assert gm.elevation_deg(lla_to_ecef(45.0, 10.0, 0.0), Fixed(), T_MID) == pytest.approx(90.0, abs=1e-6)


def test_footprint_ring_properties() -> None:
    orbit = ob.GeoOrbit(0.0)
    ring = gm.footprint(orbit, T_MID)
    assert len(ring) == 181 and ring[0] == ring[-1]
    assert all(isinstance(v, float) for vertex in ring for v in vertex)
    lat, lon = np.array(ring).T
    # every vertex is on the ellipsoid where the satellite sits exactly on the horizon
    pts = lla_to_ecef(lat, lon, 0.0)
    assert np.max(np.abs(look_angles(pts, orbit.position_ecef(T_MID))[1])) < 1e-9
    # closed form on the equator: +/- acos(R / r) = 81.30 deg; the northern limit is slightly less
    edge = np.degrees(np.arccos(WGS84_A / A_GEO))
    assert np.max(lon) == pytest.approx(edge, abs=1e-9) and np.min(lon) == pytest.approx(-edge, abs=1e-9)
    assert edge == pytest.approx(81.30, abs=0.01)
    assert lat[0] == pytest.approx(np.max(lat), abs=1e-12) and 81.2 < lat[0] < 81.5  # starts due north
    assert lon[0] == pytest.approx(0.0, abs=1e-9)
    assert lon[45] == pytest.approx(-edge, abs=1e-9) and lat[45] == pytest.approx(0.0, abs=1e-9)  # then west
    assert lat[90] == pytest.approx(-lat[0], abs=1e-9)
    # a higher mask gives a smaller ring, strictly inside
    ring10 = np.array(gm.footprint(orbit, T_MID, min_elevation_deg=10.0, n=73))
    assert ring10.shape == (73, 2)
    assert np.max(np.abs(look_angles(lla_to_ecef(ring10[:, 0], ring10[:, 1], 0.0), orbit.position_ecef(T_MID))[1] - 10.0)) < 1e-9
    assert np.max(ring10[:, 1]) == pytest.approx(71.4, abs=0.1)  # textbook 10 deg coverage half-width of GEO
    assert np.max(ring10[:, 0]) < np.max(lat)


@pytest.mark.parametrize("slot", [172.0, -177.0, 75.0])
def test_footprint_longitudes_are_continuous_around_the_subsatellite_longitude(slot: float) -> None:
    orbit = ORBITS["s1"] if slot == -177.0 else ob.GeoOrbit(slot, 4.0, phase_deg=200.0)
    ring = np.array(gm.footprint(orbit, T_MID, min_elevation_deg=5.0))
    _, sub_lon, _ = ob.subsatellite_point(orbit, T_MID)
    assert np.max(np.abs(np.diff(ring[:, 1]))) < 30.0  # no antimeridian jump
    assert np.all(np.abs(ring[:, 1] - sub_lon) < 90.0)
    assert np.array_equal(ring[0], ring[-1])
    if slot == 172.0:
        assert np.max(ring[:, 1]) > 180.0
    if slot == -177.0:
        assert np.min(ring[:, 1]) < -180.0
    el = look_angles(lla_to_ecef(ring[:, 0], ring[:, 1], 0.0), orbit.position_ecef(T_MID))[1]
    assert np.max(np.abs(el - 5.0)) < 1e-9


def test_footprint_low_orbit_over_the_pole_and_validation() -> None:
    # find a time when the polar orbiter is near its northernmost point: the footprint holds the pole
    t = T_NOV + np.arange(0.0, 6000.0, 10.0)
    lat_s = ob.subsatellite_point(LEO, t)[0]
    t_north = float(t[np.argmax(lat_s)])
    assert np.max(lat_s) > 80.0
    ring = np.array(gm.footprint(LEO, t_north, n=361))
    assert np.max(np.abs(np.diff(ring[:, 1]))) < 180.0
    assert abs(ring[-1, 1] - ring[0, 1]) == pytest.approx(360.0, abs=1e-6)  # winds once around the pole
    assert ring[-1, 0] == ring[0, 0]
    el = look_angles(lla_to_ecef(ring[:, 0], ring[:, 1], 0.0), LEO.position_ecef(t_north))[1]
    assert np.max(np.abs(el)) < 1e-8
    for kwargs, name in ((dict(n=3), "n"), (dict(min_elevation_deg=90.0), "min_elevation_deg"), (dict(n=10.5), "n")):
        with pytest.raises(ValueError, match=name):
            gm.footprint(LEO, t_north, **kwargs)
    with pytest.raises(ValueError, match="t_s"):
        gm.footprint(LEO, np.array([t_north, t_north]))  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- measurement windows


def admissible_samples(region: Region, a: PathSpec, b: PathSpec, t: float, min_el: float, n: int, seed: int) -> np.ndarray:
    """Random emitter positions inside the region that see both satellites."""
    rng = np.random.default_rng(seed)
    lat = rng.uniform(region.lat_min, region.lat_max, n)
    lon = region.lon_min + rng.uniform(0.0, region.lon_span_deg(), n)
    pts = lla_to_ecef(lat, lon, 0.0)
    ok = (look_angles(pts, ORBITS[a.sat_id].position_ecef(t))[1] >= min_el) & (look_angles(pts, ORBITS[b.sat_id].position_ecef(t))[1] >= min_el)
    return pts[ok]


@pytest.mark.parametrize(
    "region, min_el, step",
    [
        (Region(-60.0, 60.0, 60.0, -120.0), 0.0, 2.0),  # pacific-uhf.yaml region, wraps the antimeridian
        (Region(), 5.0, 2.0),  # default region, full circle
        (Region(-35.5, 47.3, 101.7, 163.2), 0.0, 3.0),  # edges that the grid step does not land on
        (Region(10.0, 50.0, 120.0, 160.0), 10.0, 5.0),
    ],
)
def test_measurement_windows_contain_every_admissible_emitter(region: Region, min_el: float, step: float) -> None:
    windows = gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, region, min_elevation_deg=min_el, step_deg=step)
    assert windows is not None
    (t_lo, t_hi), (f_lo, f_hi) = windows
    pts = admissible_samples(region, PATH_A, PATH_B, T_MID, min_el, 6000, seed=5)
    assert len(pts) > 300
    tdoa, fdoa = gm.predict_pair(pts, PATH_A, PATH_B, T_MID, ORBITS, SITES)
    assert t_lo <= np.min(tdoa) and np.max(tdoa) <= t_hi
    assert f_lo <= np.min(fdoa) and np.max(fdoa) <= f_hi
    # ... without being loose: at most about one grid cell of variation beyond the sampled extremes
    cell = np.radians(step) * 6.4e6 * np.sqrt(2.0)
    assert t_hi - np.max(tdoa) < 2.0 * cell / C and np.min(tdoa) - t_lo < 2.0 * cell / C
    assert (t_hi - t_lo) < 1.35 * np.ptp(tdoa) + 4.0 * cell / C
    assert (f_hi - f_lo) < 1.5 * np.ptp(fdoa) + 0.5


@pytest.mark.parametrize(
    "region, a, b, step",
    [
        (Region(-60.0, 60.0, 60.0, -120.0), PATH_A, PATH_C, 2.0),  # 108 deg apart: horizons cut the grid cells
        (Region(-60.0, 60.0, 60.0, -120.0), PATH_B, PATH_C, 2.0),
        (Region(-35.5, 47.3, 101.7, 163.2), PATH_A, PATH_B, 3.0),  # region edges between grid lines
    ],
)
def test_measurement_windows_plain_grid_extremes_would_miss_emitters(region: Region, a: PathSpec, b: PathSpec, step: float) -> None:
    """Why neighbours, region edges and curvature are included: the extremes sampled at admissible
    grid points fall short of real emitters by far more than the 20 microsecond CAF margin."""
    lat, lon = region.grid(step)
    grid = lla_to_ecef(*np.meshgrid(lat, lon, indexing="ij"), 0.0).reshape(-1, 3)
    ok = (look_angles(grid, ORBITS[a.sat_id].position_ecef(T_MID))[1] >= 0.0) & (look_angles(grid, ORBITS[b.sat_id].position_ecef(T_MID))[1] >= 0.0)
    plain = gm.predict_tdoa(grid[ok], a, b, T_MID, ORBITS, SITES)
    pts = admissible_samples(region, a, b, T_MID, 0.0, 60000, seed=6)
    tdoa = gm.predict_tdoa(pts, a, b, T_MID, ORBITS, SITES)
    shortfall = max(np.max(tdoa) - np.max(plain), np.min(plain) - np.min(tdoa))
    assert shortfall > 90e-6
    (t_lo, t_hi), _ = gm.measurement_windows(a, b, T_MID, ORBITS, SITES, region, step_deg=step)
    assert t_lo < np.min(tdoa) and np.max(tdoa) < t_hi
    assert t_lo < np.min(plain) and np.max(plain) < t_hi


def test_measurement_windows_cover_curvature_inside_cells() -> None:
    """A coarse grid whose points all sit away from the extremum: the bilinear error bound
    (second differences / 8) has to make up the difference."""
    region = Region(-60.0, 60.0, 60.0, -120.0)
    pts = admissible_samples(region, PATH_A, PATH_B, T_MID, 0.0, 200000, seed=8)
    tdoa, fdoa = gm.predict_pair(pts, PATH_A, PATH_B, T_MID, ORBITS, SITES)
    for step in (2.0, 5.0, 10.0, 15.0):
        (t_lo, t_hi), (f_lo, f_hi) = gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, region, step_deg=step)
        assert t_lo < np.min(tdoa) and np.max(tdoa) < t_hi
        assert f_lo < np.min(fdoa) and np.max(fdoa) < f_hi


def test_measurement_windows_none_when_nothing_is_admissible() -> None:
    # s3 (75 E) is below the horizon everywhere in a box around Hawaii
    hawaii = Region(10.0, 30.0, -170.0, -150.0)
    assert gm.measurement_windows(PATH_A, PATH_C, T_MID, ORBITS, SITES, hawaii) is None
    assert gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, hawaii) is not None
    # an elevation mask no point can meet for both satellites at once
    assert gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, Region(), min_elevation_deg=85.0) is None


def test_measurement_windows_options() -> None:
    region = Region(-60.0, 60.0, 60.0, -120.0)
    base = gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, region)
    masked = gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, region, min_elevation_deg=20.0)
    assert base is not None and masked is not None
    assert base[0][0] < masked[0][0] and masked[0][1] < base[0][1]  # a mask narrows the delay window
    # several epochs: the union
    later = gm.measurement_windows(PATH_A, PATH_B, T_MID + 3600.0, ORBITS, SITES, region)
    both = gm.measurement_windows(PATH_A, PATH_B, np.array([T_MID, T_MID + 3600.0]), ORBITS, SITES, region)
    assert later is not None and both is not None
    for k, slack in ((0, 1e-7), (1, 1e-3)):  # the curvature pad is shared by the epochs
        assert both[k][0] <= min(base[k][0], later[k][0]) and both[k][1] >= max(base[k][1], later[k][1])
        assert both[k][0] == pytest.approx(min(base[k][0], later[k][0]), abs=slack)
        assert both[k][1] == pytest.approx(max(base[k][1], later[k][1]), abs=slack)
    # emitter height changes the geometry slightly
    high = gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, region, height_m=10000.0)
    assert high is not None and high != base
    assert high[0][0] == pytest.approx(base[0][0], abs=5e-5)
    # a region object with only grid() and contains() (and a contains() written for wrapped longitudes)
    bare = gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, BareRegion(region))
    assert bare == base
    # returned as plain floats, ordered
    assert all(isinstance(v, float) for pair in base for v in pair)
    assert base[0][0] < base[0][1] and base[1][0] < base[1][1]
    with pytest.raises(ValueError, match="step_deg"):
        gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, region, step_deg=0.0)
    with pytest.raises(ValueError, match="a.sat_id"):
        gm.measurement_windows(PathSpec("nope", "hnl", 3e8, 0.0), PATH_B, T_MID, ORBITS, SITES, region)


# ----------------------------------------------------------------------------- search windows


CAF = SimpleNamespace(min_snr_db=13.0, max_tdoa_sigma_s=5e-4, doppler_margin_hz=20.0, delay_margin_s=20e-6)
UNC = Uncertainty(
    site_clock_s={"hnl": 30e-9, "tyo": 40e-9},
    site_osc_frac={"hnl": 1e-11, "tyo": 2e-11},
    sat_osc_frac={"s1": 1e-6, "s2": 2e-6},
    tdoa_bias_s=50e-9,
)
GEO_WIN = ((-0.012, 0.004), (-35.0, 22.0))


def test_search_windows_widening_by_hand() -> None:
    delay, doppler = gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC)
    # delay: 20 us + 3 sqrt(30^2 + 40^2 + 50^2) ns = 20 us + 3 * 70.7107 ns
    pad_t = 20e-6 + 3 * 70.71067811865476e-9
    assert delay == pytest.approx((-0.012 - pad_t, 0.004 + pad_t), abs=1e-15)
    # doppler: sat 1e-6 * 41 MHz = 41 Hz, 2e-6 * 33.6 MHz = 67.2 Hz; sites 1e-11 * 259 MHz = 2.59 mHz,
    # 2e-11 * 266.4 MHz = 5.328 mHz -> 3 * sqrt(41^2 + 67.2^2 + 0.00259^2 + 0.005328^2) = 3 * 78.7199
    pad_f = 20.0 + 3 * np.sqrt(41.0**2 + 67.2**2 + 0.00259**2 + 0.005328**2)
    assert pad_f == pytest.approx(20.0 + 236.16, abs=0.01)
    assert doppler == pytest.approx((-35.0 - pad_f, 22.0 + pad_f), abs=1e-9)
    assert all(isinstance(v, float) for v in (*delay, *doppler))
    # no uncertainty at all: just the fixed margins; a mapping works as the CAF config
    delay0, doppler0 = gm.search_windows(GEO_WIN, PATH_A, PATH_B, {"delay_margin_s": 20e-6, "doppler_margin_hz": 20.0}, None)
    assert delay0 == pytest.approx((-0.01202, 0.00402), abs=1e-15) and doppler0 == pytest.approx((-55.0, 42.0))
    # sites and satellites missing from the uncertainty count as zero
    delay1, _ = gm.search_windows(GEO_WIN, PATH_A, PATH_C, CAF, UNC)
    assert delay1[1] - 0.004 == pytest.approx(20e-6 + 3 * np.hypot(30e-9, 50e-9), abs=1e-15)


def test_search_windows_with_a_calibration_shift_and_fixed_margins() -> None:
    delay, doppler = gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction=(1.5e-7, -48.0))
    assert delay == pytest.approx((-0.012 + 1.5e-7 - 20e-6, 0.004 + 1.5e-7 + 20e-6), abs=1e-15)
    assert doppler == pytest.approx((-35.0 - 48.0 - 20.0, 22.0 - 48.0 + 20.0))
    # a calibration of one kind only
    wide = gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC)
    delay, doppler = gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction=(None, -48.0))
    assert delay == wide[0] and doppler == pytest.approx((-103.0, -6.0))
    delay, doppler = gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction=(1.5e-7, None))
    assert doppler == wide[1] and delay[1] - delay[0] == pytest.approx(0.016 + 40e-6, abs=1e-15)
    # the same, keyed by kind
    by_kind = gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction={"tdoa": 1.5e-7})
    assert by_kind == (delay, doppler)
    both = gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction={"tdoa": 1.5e-7, "fdoa": -48.0})
    assert both == gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction=(1.5e-7, -48.0))
    with pytest.raises(ValueError, match="correction"):
        gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction={"delay": 1e-7})


def test_search_windows_common_mode_terms_cancel() -> None:
    """One site on both channels has one clock and one oscillator; one satellite has one oscillator."""
    same_site = PathSpec("s2", "hnl", 300e6, -33.6e6)
    delay, doppler = gm.search_windows(GEO_WIN, PATH_A, same_site, CAF, UNC)
    assert delay[1] - 0.004 == pytest.approx(20e-6 + 3 * 50e-9, abs=1e-15)  # bias only, no clock
    site_term = 1e-11 * (PATH_A.f_tune_hz - same_site.f_tune_hz)
    assert doppler[1] - 22.0 == pytest.approx(20.0 + 3 * np.sqrt(41.0**2 + 67.2**2 + site_term**2), abs=1e-9)
    same_sat = PathSpec("s1", "tyo", 300e6, -41.0e6)
    _, doppler = gm.search_windows(GEO_WIN, PATH_A, same_sat, CAF, UNC)
    assert doppler[1] - 22.0 == pytest.approx(20.0 + 3 * np.hypot(1e-11 * 259e6, 2e-11 * 259e6), abs=1e-9)


def test_search_windows_validation() -> None:
    with pytest.raises(ValueError, match="geo_windows"):
        gm.search_windows(None, PATH_A, PATH_B, CAF, UNC)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="geo_windows"):
        gm.search_windows(((1.0, 0.0), (0.0, 1.0)), PATH_A, PATH_B, CAF, UNC)
    with pytest.raises(ValueError, match="caf_cfg.delay_margin_s"):
        gm.search_windows(GEO_WIN, PATH_A, PATH_B, SimpleNamespace(doppler_margin_hz=20.0), UNC)
    with pytest.raises(ValueError, match="caf_cfg.doppler_margin_hz"):
        gm.search_windows(GEO_WIN, PATH_A, PATH_B, SimpleNamespace(delay_margin_s=1e-6, doppler_margin_hz=-1.0), UNC)
    with pytest.raises(ValueError, match="correction"):
        gm.search_windows(GEO_WIN, PATH_A, PATH_B, CAF, UNC, correction=1e-7)  # type: ignore[arg-type]


def test_windows_end_to_end_contain_a_measurement_with_systematic_errors() -> None:
    """measurement_windows + search_windows hold the apparent TDOA/FDOA of an emitter anywhere in
    the region when clocks and oscillators are off by two sigma (MATH.md section 5 clock terms)."""
    region = Region(-60.0, 60.0, 60.0, -120.0)
    geo = gm.measurement_windows(PATH_A, PATH_B, T_MID, ORBITS, SITES, region)
    assert geo is not None
    delay, doppler = gm.search_windows(geo, PATH_A, PATH_B, CAF, UNC)
    pts = admissible_samples(region, PATH_A, PATH_B, T_MID, 0.0, 3000, seed=9)
    tdoa, fdoa = gm.predict_pair(pts, PATH_A, PATH_B, T_MID, ORBITS, SITES)
    # TDOA_meas = (D_A - delta_a) - (D_B - delta_b); satellite oscillators shift f_bb by eps T
    tdoa_meas = tdoa - 2 * 30e-9 + 2 * 40e-9 + 2 * 50e-9
    fdoa_meas = fdoa + 2 * 1e-6 * PATH_A.translation_hz - (-2) * 2e-6 * PATH_B.translation_hz
    assert np.all((tdoa_meas > delay[0]) & (tdoa_meas < delay[1]))
    assert np.all((fdoa_meas > doppler[0]) & (fdoa_meas < doppler[1]))


# ----------------------------------------------------------------------------- validation


def test_invalid_inputs_name_the_field() -> None:
    with pytest.raises(ValueError, match="x_e"):
        gm.predict_tdoa([1.0, 2.0], PATH_A, PATH_B, T_MID, ORBITS, SITES)
    with pytest.raises(ValueError, match="x_e"):
        gm.uplink_delay([np.nan, 0.0, 0.0], ORBITS["s1"], T_MID)
    with pytest.raises(ValueError, match="x_g"):
        gm.bent_pipe(EMITTER, ORBITS["s1"], np.zeros((2, 3)), T_MID)
    with pytest.raises(ValueError, match="t_rx_s"):
        gm.bent_pipe(EMITTER, ORBITS["s1"], SITES["hnl"], np.zeros((2, 2)))
    with pytest.raises(ValueError, match="t_rx_s"):
        gm.predict_fdoa(EMITTER, PATH_A, PATH_B, float("inf"), ORBITS, SITES)
    with pytest.raises(ValueError, match="tau_x"):
        gm.bent_pipe(EMITTER, ORBITS["s1"], SITES["hnl"], T_MID, tau_x=float("nan"))
    with pytest.raises(ValueError, match="a.sat_id='zz'"):
        gm.predict_tdoa(EMITTER, PathSpec("zz", "hnl", 3e8, 0.0), PATH_B, T_MID, ORBITS, SITES)
    with pytest.raises(ValueError, match="b.site_id='nowhere'"):
        gm.predict_fdoa(EMITTER, PATH_A, PathSpec("s2", "nowhere", 3e8, 0.0), T_MID, ORBITS, SITES)
    with pytest.raises(ValueError, match="sat_id='zz'"):
        gm.self_peak("zz", 0.0, 3e8, PATH_A, PATH_B, T_MID, ORBITS, SITES)
    with pytest.raises(ValueError, match="sites_xyz"):
        gm.predict_tdoa(EMITTER, PATH_A, PATH_B, T_MID, ORBITS, {**SITES, "hnl": np.zeros(2)})
    # a relay at lunar distance: the two delays differ by 2 s, beyond what the +/- 1 s stencil covers
    far = ob.KeplerOrbit(3.84e8, 0.0, 5.0, 0.0, 0.0, 0.0, epoch=T_MID, j2=False)
    assert gm.bent_pipe(EMITTER, far, SITES["hnl"], T_MID).D == pytest.approx(2.5, abs=0.1)
    with pytest.raises(ValueError, match="more than 1 s"):
        gm.predict_tdoa(EMITTER, PathSpec("far", "hnl", 3e8, 0.0), PATH_B, T_MID, {**ORBITS, "far": far}, SITES)


# ----------------------------------------------------------------------------- performance


def _best_of(fn: Any, repeats: int) -> float:
    best = np.inf
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def test_grid_and_many_epoch_evaluation_are_vectorised() -> None:
    """141 x 360 emitter positions at one epoch, and 400 epochs at one position.

    Measured on the development machine: 15 ms (TDOA) and 24 ms (FDOA) for the grid,
    1.4 ms and 2.1 ms for 400 epochs. The limits are loose because the test machine is
    shared; a Python loop over points would take seconds.
    """
    lat, lon = np.linspace(-70.0, 70.0, 141), np.linspace(-180.0, 179.0, 360)
    grid = lla_to_ecef(*np.meshgrid(lat, lon, indexing="ij"), 0.0)
    assert grid.shape == (141, 360, 3)
    epochs = T_MID + 5.0 * np.arange(400)
    for fn in (gm.predict_tdoa, gm.predict_fdoa):
        assert fn(grid, PATH_A, PATH_B, T_MID, ORBITS, SITES).shape == (141, 360)
        assert _best_of(lambda: fn(grid, PATH_A, PATH_B, T_MID, ORBITS, SITES), 5) < 0.25
        assert fn(EMITTER, PATH_A, PATH_B, epochs, ORBITS, SITES).shape == (400,)
        assert _best_of(lambda: fn(EMITTER, PATH_A, PATH_B, epochs, ORBITS, SITES), 5) < 0.03
    # the grid result is the scalar result at any one cell
    full = gm.predict_pair(grid, PATH_A, PATH_B, T_MID, ORBITS, SITES)
    for i, j in ((0, 0), (70, 180), (140, 359), (33, 271)):
        one = gm.predict_pair(grid[i, j], PATH_A, PATH_B, T_MID, ORBITS, SITES)
        assert full[0][i, j] == one[0] and full[1][i, j] == one[1]
