"""geodesy: WGS84 conversions, local frames, look angles and geodesics.

Every check is against something that does not share code with the implementation:
closed forms, published test lines, numerical quadrature / ODE integration, or a
different algorithm.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.integrate import quad, solve_ivp
from scipy.optimize import brentq

from petal_locate.core import geodesy as g
from petal_locate.core.constants import WGS84_A, WGS84_E2, WGS84_F

B = WGS84_A * (1.0 - WGS84_F)
A_GEO = 42164172.36566204


def dms(d: float, m: float, s: float) -> float:
    sign = -1.0 if d < 0 else 1.0
    return sign * (abs(d) + m / 60.0 + s / 3600.0)


# ----------------------------------------------------------------------------- lla <-> ecef


def test_lla_to_ecef_cardinal_points() -> None:
    assert np.allclose(g.lla_to_ecef(0.0, 0.0, 0.0), [WGS84_A, 0.0, 0.0], atol=1e-9)
    assert np.allclose(g.lla_to_ecef(0.0, 90.0, 100.0), [0.0, WGS84_A + 100.0, 0.0], atol=1e-9)
    assert np.allclose(g.lla_to_ecef(90.0, 0.0, 0.0), [0.0, 0.0, B], atol=1e-9)
    assert np.allclose(g.lla_to_ecef(-90.0, 45.0, 1000.0), [0.0, 0.0, -B - 1000.0], atol=1e-9)
    assert np.allclose(g.lla_to_ecef(0.0, 180.0, 0.0), [-WGS84_A, 0.0, 0.0], atol=1e-9)


def test_surface_points_satisfy_the_ellipsoid_equation() -> None:
    rng = np.random.default_rng(1)
    p = g.lla_to_ecef(rng.uniform(-90, 90, 500), rng.uniform(-180, 180, 500), 0.0)
    q = (p[:, 0] ** 2 + p[:, 1] ** 2) / WGS84_A**2 + p[:, 2] ** 2 / B**2
    assert np.max(np.abs(q - 1.0)) < 1e-14


@pytest.mark.parametrize("alt_lo, alt_hi", [(-10e3, 0.0), (0.0, 20e3), (2e5, 2e6), (2e7, 4.5e7)])
def test_geodetic_round_trip(alt_lo: float, alt_hi: float) -> None:
    rng = np.random.default_rng(2)
    lat = np.concatenate([rng.uniform(-90, 90, 5000), [90.0, -90.0, 0.0, 89.9999999, -89.9999999, 1e-9]])
    lon = np.concatenate([rng.uniform(-180, 180, 5000), [0.0, 10.0, -180.0, 179.9999999, 45.0, -1e-9]])
    alt = rng.uniform(alt_lo, alt_hi, lat.size)
    xyz = g.lla_to_ecef(lat, lon, alt)
    lat2, lon2, alt2 = g.ecef_to_lla(xyz)
    assert np.max(np.abs(g.lla_to_ecef(lat2, lon2, alt2) - xyz)) < 1e-7
    assert np.max(np.abs(alt2 - alt)) < 1e-7
    assert np.max(np.abs(lat2 - lat)) < 1e-12
    off_axis = np.abs(lat) < 89.9
    dlon = np.mod(lon2 - lon + 180.0, 360.0) - 180.0
    assert np.max(np.abs(dlon[off_axis])) < 1e-12
    assert np.all((lon2 >= -180.0) & (lon2 < 180.0))


def test_ecef_to_lla_against_a_root_finder() -> None:
    """Independent algorithm: solve the latitude equation p tan(phi) - z = e^2 N sin(phi) by Brent."""
    rng = np.random.default_rng(3)
    for _ in range(40):
        lat = rng.uniform(-89.0, 89.0)
        alt = rng.choice([rng.uniform(-5e3, 9e3), rng.uniform(3e5, 4.3e7)])
        x, y, z = g.lla_to_ecef(lat, rng.uniform(-180, 180), alt)
        p = np.hypot(x, y)

        def f(phi: float) -> float:
            n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * np.sin(phi) ** 2)
            return p * np.sin(phi) - z * np.cos(phi) - WGS84_E2 * n * np.sin(phi) * np.cos(phi)

        phi = brentq(f, -np.pi / 2, np.pi / 2, xtol=1e-15, rtol=1e-15)
        n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * np.sin(phi) ** 2)
        h = p / np.cos(phi) - n
        lat2, _, alt2 = g.ecef_to_lla([x, y, z])
        assert abs(lat2 - np.degrees(phi)) < 1e-11
        assert abs(alt2 - h) < 1e-6


def test_ecef_to_lla_shapes_and_validation() -> None:
    lat, lon, alt = g.ecef_to_lla([WGS84_A + 5.0, 0.0, 0.0])
    assert (np.ndim(lat), np.ndim(lon), np.ndim(alt)) == (0, 0, 0)
    assert (lat, lon) == (0.0, 0.0) and alt == pytest.approx(5.0, abs=1e-9)
    lat, lon, alt = g.ecef_to_lla(np.zeros((4, 5, 3)) + [0.0, 0.0, B])
    assert lat.shape == lon.shape == alt.shape == (4, 5)
    assert np.all(lat == 90.0)
    assert g.lla_to_ecef(np.zeros((2, 1)), np.zeros(3), 0.0).shape == (2, 3, 3)
    with pytest.raises(ValueError, match="xyz"):
        g.ecef_to_lla([1.0, 2.0])
    with pytest.raises(ValueError, match="lat_deg"):
        g.lla_to_ecef(91.0, 0.0)


def test_wrap_lon() -> None:
    assert np.array_equal(g.wrap_lon_deg([180.0, -180.0, 190.0, -190.0, 540.0, 0.0]), [-180.0, -180.0, -170.0, 170.0, -180.0, 0.0])
    assert g.wrap_lon_deg(359.0) == -1.0


# ----------------------------------------------------------------------------- local frames


def test_enu_basis_is_the_gradient_of_lla_to_ecef() -> None:
    """East, north and up are the directions the position moves when lon, lat, alt increase."""
    rng = np.random.default_rng(4)
    for _ in range(20):
        lat, lon = rng.uniform(-85, 85), rng.uniform(-180, 180)
        basis = g.enu_basis(lat, lon)
        d = 1e-6
        east = g.lla_to_ecef(lat, lon + d, 0.0) - g.lla_to_ecef(lat, lon - d, 0.0)
        north = g.lla_to_ecef(lat + d, lon, 0.0) - g.lla_to_ecef(lat - d, lon, 0.0)
        up = g.lla_to_ecef(lat, lon, 1.0) - g.lla_to_ecef(lat, lon, -1.0)
        for row, ref in zip(basis, (east, north, up)):
            assert np.allclose(row, ref / np.linalg.norm(ref), atol=2e-8)
        assert np.allclose(basis @ basis.T, np.eye(3), atol=1e-15)
        assert np.linalg.det(basis) == pytest.approx(1.0, abs=1e-14)
        assert np.allclose(np.cross(basis[0], basis[1]), basis[2], atol=1e-15)


def test_enu_basis_cardinal_and_shapes() -> None:
    assert np.allclose(g.enu_basis(0.0, 0.0), [[0, 1, 0], [0, 0, 1], [1, 0, 0]], atol=1e-16)
    assert np.allclose(g.enu_basis(90.0, 0.0), [[0, 1, 0], [-1, 0, 0], [0, 0, 1]], atol=1e-16)
    assert g.enu_basis(np.zeros(4), np.zeros(4)).shape == (4, 3, 3)
    assert g.enu_basis(np.zeros((2, 1)), np.zeros(5)).shape == (2, 5, 3, 3)


def test_look_angles_hand_computed_cases() -> None:
    site = g.lla_to_ecef(0.0, 0.0, 0.0)
    # 1. satellite at the zenith of an equatorial site
    az, el, rng_m = g.look_angles(site, [A_GEO, 0.0, 0.0])
    assert el == pytest.approx(90.0, abs=1e-9)
    assert rng_m == pytest.approx(A_GEO - WGS84_A, abs=1e-6)
    # 2. geostationary satellite 60 degrees east along the equator: plane trigonometry in the
    #    equatorial plane, where the vertical is radial. el = atan((cos g - R/r) / sin g).
    gamma = np.radians(60.0)
    sat = A_GEO * np.array([np.cos(gamma), np.sin(gamma), 0.0])
    az, el, rng_m = g.look_angles(site, sat)
    assert az == pytest.approx(90.0, abs=1e-10)
    assert el == pytest.approx(np.degrees(np.arctan((np.cos(gamma) - WGS84_A / A_GEO) / np.sin(gamma))), abs=1e-10)
    # by hand: (0.5 - 0.151269) / 0.866025 = 0.40268; atan(0.4) = 21.8014 deg, plus
    # 0.00268 / (1 + 0.16) rad = 0.1324 deg
    assert el == pytest.approx(21.934, abs=1e-3)
    assert rng_m == pytest.approx(np.sqrt(WGS84_A**2 + A_GEO**2 - WGS84_A * A_GEO), abs=1e-6)
    # ... and 60 degrees west: due west, same elevation
    az_w, el_w, _ = g.look_angles(site, sat * [1.0, -1.0, 1.0])
    assert az_w == pytest.approx(270.0, abs=1e-10) and el_w == pytest.approx(el, abs=1e-12)
    # 3. from the north pole the equatorial GEO arc is below the horizon by atan(b / r), due south
    pole = g.lla_to_ecef(90.0, 0.0, 0.0)
    az, el, rng_m = g.look_angles(pole, [A_GEO, 0.0, 0.0])
    assert az == pytest.approx(180.0, abs=1e-10)
    assert el == pytest.approx(-np.degrees(np.arctan(B / A_GEO)), abs=1e-10)
    assert rng_m == pytest.approx(np.hypot(A_GEO, B), abs=1e-6)
    # 4. a point 1 km north and 1 km up of a mid-latitude site: az 0, el 45, range sqrt(2) km
    lat, lon = 35.0, 139.0
    s = g.lla_to_ecef(lat, lon, 20.0)
    e_hat, n_hat, u_hat = g.enu_basis(lat, lon)
    az, el, rng_m = g.look_angles(s, s + 1000.0 * n_hat + 1000.0 * u_hat)
    assert (az + 180.0) % 360.0 - 180.0 == pytest.approx(0.0, abs=1e-9)  # due north, either side of 0/360
    assert (el, rng_m) == pytest.approx((45.0, 1000.0 * np.sqrt(2.0)), abs=1e-9)
    az, el, _ = g.look_angles(s, s - 500.0 * e_hat - 500.0 * n_hat)
    assert (az, el) == pytest.approx((225.0, 0.0), abs=1e-9)


def test_look_angles_use_the_geodetic_vertical() -> None:
    """At 45 N the geodetic and geocentric verticals differ by 0.19 deg; a target straight up
    along the ellipsoid normal must read 90 deg, and one along the geocentric radius must not."""
    site = g.lla_to_ecef(45.0, 10.0, 0.0)
    up = g.enu_basis(45.0, 10.0)[2]
    assert g.look_angles(site, site + 1e6 * up)[1] == pytest.approx(90.0, abs=1e-6)
    radial = site / np.linalg.norm(site)
    el = g.look_angles(site, site + 1e6 * radial)[1]
    assert 90.0 - el == pytest.approx(g.angle_between_deg(up, radial), abs=1e-9)
    assert 0.18 < 90.0 - el < 0.20


def test_look_angles_broadcast() -> None:
    sites = g.lla_to_ecef(np.array([0.0, 10.0, 20.0]), 0.0, 0.0)
    az, el, rng_m = g.look_angles(sites[:, None, :], np.array([[A_GEO, 0.0, 0.0], [0.0, A_GEO, 0.0]]))
    assert az.shape == el.shape == rng_m.shape == (3, 2)
    assert np.all((az >= 0.0) & (az < 360.0))


def test_azel_to_ecef_inverts_look_angles() -> None:
    rng = np.random.default_rng(5)
    for _ in range(50):
        lat, lon = rng.uniform(-89, 89), rng.uniform(-180, 180)
        az, el = rng.uniform(0, 360), rng.uniform(-89, 89)
        u = g.azel_to_ecef(lat, lon, az, el)
        assert np.linalg.norm(u) == pytest.approx(1.0, abs=1e-15)
        site = g.lla_to_ecef(lat, lon, 0.0)
        az2, el2, rng_m = g.look_angles(site, site + 1e5 * u)
        assert (az2, el2, rng_m) == pytest.approx((az, el, 1e5), abs=1e-8)
    assert np.allclose(g.azel_to_ecef(0.0, 0.0, 90.0, 0.0), [0, 1, 0], atol=1e-15)
    assert np.allclose(g.azel_to_ecef(0.0, 0.0, 0.0, 90.0), [1, 0, 0], atol=1e-15)
    assert g.azel_to_ecef(np.zeros(3), 0.0, 10.0, 20.0).shape == (3, 3)


def test_angle_between() -> None:
    assert g.angle_between_deg([1, 0, 0], [0, 2, 0]) == pytest.approx(90.0, abs=1e-13)
    assert g.angle_between_deg([1, 0, 0], [5, 0, 0]) == 0.0
    assert g.angle_between_deg([1, 0, 0], [-3, 0, 0]) == pytest.approx(180.0, abs=1e-13)
    assert g.angle_between_deg([1, 1, 0], [1, 0, 0]) == pytest.approx(45.0, abs=1e-13)
    # tiny angles keep full relative precision (acos of the dot product would return 0)
    tiny = 1e-9
    assert g.angle_between_deg([1, 0, 0], [1, np.tan(np.radians(tiny)), 0]) == pytest.approx(tiny, rel=1e-9)
    out = g.angle_between_deg(np.eye(3)[:, None, :], np.eye(3)[None, :, :])
    assert out.shape == (3, 3) and np.allclose(out, 90.0 * (1 - np.eye(3)))
    with pytest.raises(ValueError, match="u"):
        g.angle_between_deg([1, 0], [1, 0, 0])


def test_rz_is_an_active_counter_clockwise_rotation() -> None:
    assert np.allclose(g.rz(np.pi / 2) @ [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], atol=1e-16)
    assert np.allclose(g.rz(np.pi / 2) @ [0.0, 1.0, 5.0], [-1.0, 0.0, 5.0], atol=1e-16)
    a, b = 0.3, -1.1
    assert np.allclose(g.rz(a) @ g.rz(b), g.rz(a + b), atol=1e-15)
    assert np.allclose(g.rz(a).T @ g.rz(a), np.eye(3), atol=1e-15)
    assert np.linalg.det(g.rz(a)) == pytest.approx(1.0, abs=1e-15)
    stack = g.rz(np.array([0.0, a, b]))
    assert stack.shape == (3, 3, 3) and np.allclose(stack[1], g.rz(a))


# ----------------------------------------------------------------------------- curvature


def test_radii_of_curvature_closed_forms() -> None:
    m, n = g.radii_of_curvature(0.0)
    assert m == pytest.approx(WGS84_A * (1.0 - WGS84_E2), abs=1e-8)  # b^2 / a
    assert n == pytest.approx(WGS84_A, abs=1e-9)
    m, n = g.radii_of_curvature(90.0)
    assert m == pytest.approx(WGS84_A**2 / B, abs=1e-6) and n == pytest.approx(WGS84_A**2 / B, abs=1e-6)
    m, n = g.radii_of_curvature(np.array([-30.0, 30.0]))
    assert m[0] == m[1] and n[0] == n[1]


def test_radii_of_curvature_match_surface_displacements() -> None:
    """(M + h) dlat is the northward and (N + h) cos(lat) dlon the eastward arc length."""
    for lat, h in ((0.0, 0.0), (30.37, 0.0), (-62.0, 1500.0), (80.0, 9000.0)):
        m, n = g.radii_of_curvature(lat)
        d = 1e-5
        north = np.linalg.norm(g.lla_to_ecef(lat + d, 20.0, h) - g.lla_to_ecef(lat - d, 20.0, h))
        east = np.linalg.norm(g.lla_to_ecef(lat, 20.0 + d, h) - g.lla_to_ecef(lat, 20.0 - d, h))
        assert north == pytest.approx((m + h) * np.radians(2 * d), rel=1e-9)
        assert east == pytest.approx((n + h) * np.cos(np.radians(lat)) * np.radians(2 * d), rel=1e-9)


# ----------------------------------------------------------------------------- geodesics


def meridian_arc(lat1_deg: float, lat2_deg: float) -> float:
    """Meridian arc length by quadrature of the meridional radius (independent of Vincenty)."""
    f = lambda phi: WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * np.sin(phi) ** 2) ** 1.5
    return quad(f, np.radians(lat1_deg), np.radians(lat2_deg), epsabs=1e-6, epsrel=1e-14)[0]


def test_geodesic_published_lines() -> None:
    # Vincenty (1975) / Geoscience Australia: Flinders Peak to Buninyong, 54 972.271 m
    d = g.geodesic_distance_m(dms(-37, 57, 3.72030), dms(144, 25, 29.52440), dms(-37, 39, 10.15610), dms(143, 55, 35.38390))
    assert d == pytest.approx(54972.271, abs=1e-3)
    # Karney (2013), "Algorithms for geodesics", table 4 / 5: nearly antipodal line
    assert g.geodesic_distance_m(-30.0, 0.0, 29.9, 179.8) == pytest.approx(19989832.827610, abs=1e-3)
    # same paper, section 5: a 5 m line
    assert g.geodesic_distance_m(-30.12345, 0.0, -30.12344, 0.00005) == pytest.approx(4.944208, abs=1e-6)


def test_geodesic_closed_forms() -> None:
    assert g.geodesic_distance_m(12.0, 34.0, 12.0, 34.0) == 0.0
    # along the equator the geodesic is the equator itself (up to 179.39 deg of longitude)
    assert g.geodesic_distance_m(0.0, -20.0, 0.0, 70.0) == pytest.approx(WGS84_A * np.pi / 2, abs=1e-6)
    assert g.geodesic_distance_m(0.0, 170.0, 0.0, -100.0) == pytest.approx(WGS84_A * np.pi / 2, abs=1e-6)
    # along a meridian: quadrature of the meridional radius
    assert g.geodesic_distance_m(-33.0, 151.0, 47.5, 151.0) == pytest.approx(meridian_arc(-33.0, 47.5), abs=1e-4)
    assert g.geodesic_distance_m(10.0, 0.0, 85.0, 0.0) == pytest.approx(meridian_arc(10.0, 85.0), abs=1e-4)
    # over the pole: 60 N on opposite meridians
    assert g.geodesic_distance_m(60.0, 20.0, 60.0, -160.0) == pytest.approx(2 * meridian_arc(60.0, 90.0), abs=1e-4)
    quarter = meridian_arc(0.0, 90.0)
    assert quarter == pytest.approx(10001965.7293, abs=1e-3)  # WGS84 quarter meridian
    # pole to pole, and antipodal points on the equator (shortest way is over a pole)
    assert g.geodesic_distance_m(90.0, 0.0, -90.0, 77.0) == pytest.approx(2 * quarter, abs=1e-4)
    assert g.geodesic_distance_m(0.0, 0.0, 0.0, 180.0) == pytest.approx(2 * quarter, abs=1e-4)
    assert g.geodesic_distance_m(0.0, 10.0, 0.0, -170.0) == pytest.approx(2 * quarter, abs=1e-4)


def test_geodesic_nearly_antipodal_pairs_are_solved_and_bounded() -> None:
    """Vincenty's iteration fails here; the result must still be finite, symmetric and
    between the longest equatorial arc that is a geodesic and the half meridian."""
    quarter = meridian_arc(0.0, 90.0)
    rng = np.random.default_rng(6)
    lat1 = rng.uniform(-60, 60, 300)
    lon1 = rng.uniform(-180, 180, 300)
    lat2 = -lat1 + rng.uniform(-0.3, 0.3, 300)
    lon2 = lon1 + 180.0 + rng.uniform(-0.3, 0.3, 300)
    d = g.geodesic_distance_m(lat1, lon1, lat2, lon2)
    assert np.all(np.isfinite(d))
    assert np.all(d <= 2 * quarter + 1e-3)
    assert np.all(d > 2 * quarter - 80e3)
    assert np.allclose(d, g.geodesic_distance_m(lat2, lon2, lat1, lon1), rtol=0, atol=1e-5)
    # triangle inequality through the pole, which is itself a geodesic route
    via_pole = g.geodesic_distance_m(lat1, lon1, 90.0, 0.0) + g.geodesic_distance_m(90.0, 0.0, lat2, lon2)
    assert np.all(d <= via_pole + 1e-3)


def test_geodesic_two_algorithms_agree() -> None:
    """Vincenty's lambda iteration against bisection on the departure azimuth."""
    rng = np.random.default_rng(8)
    n = 1500
    lat1, lat2 = rng.uniform(-89, 89, n), rng.uniform(-89, 89, n)
    lon1, lon2 = rng.uniform(-180, 180, n), rng.uniform(-180, 180, n)
    u1 = np.arctan((1 - WGS84_F) * np.tan(np.radians(lat1)))
    u2 = np.arctan((1 - WGS84_F) * np.tan(np.radians(lat2)))
    lam = np.abs(np.mod(np.radians(lon2 - lon1) + np.pi, 2 * np.pi) - np.pi)
    vincenty, converged = g._vincenty_inverse(u1, u2, lam)
    bisect = g._inverse_by_bisection(u1, u2, lam)
    assert converged.mean() > 0.98
    assert np.max(np.abs(vincenty - bisect)[converged]) < 1e-5
    public = g.geodesic_distance_m(lat1, lon1, lat2, lon2)
    assert np.max(np.abs(public - bisect)) < 1e-5


def geodesic_ode(lat_deg: float, lon_deg: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """Integrate the geodesic equations on the ellipsoid (independent of Vincenty's series)."""

    def rhs(_s: float, y: np.ndarray) -> list[float]:
        phi, _lam, alpha = y
        w = np.sqrt(1.0 - WGS84_E2 * np.sin(phi) ** 2)
        m = WGS84_A * (1.0 - WGS84_E2) / w**3
        n = WGS84_A / w
        return [np.cos(alpha) / m, np.sin(alpha) / (n * np.cos(phi)), np.sin(alpha) * np.tan(phi) / n]

    sol = solve_ivp(rhs, [0.0, dist_m], np.radians([lat_deg, lon_deg, bearing_deg]), method="DOP853", rtol=1e-13, atol=1e-15)
    return float(np.degrees(sol.y[0, -1])), float(np.degrees(sol.y[1, -1]))


def test_destination_against_geodesic_ode() -> None:
    cases = [
        (30.37, 140.41, 47.0, 1.0e6),
        (-12.5, -70.0, 200.0, 3.5e6),
        (60.0, 10.0, 275.0, 8.0e6),
        (5.0, 179.5, 90.0, 5.0e5),
        (-45.0, 20.0, 10.0, 1.2e7),
    ]
    for lat, lon, bearing, dist in cases:
        lat2, lon2 = g.destination(lat, lon, bearing, dist)
        ref_lat, ref_lon = geodesic_ode(lat, lon, bearing, dist)
        m, n = g.radii_of_curvature(ref_lat)
        north_err = np.radians(lat2 - ref_lat) * m
        east_err = np.radians(np.mod(lon2 - ref_lon + 180.0, 360.0) - 180.0) * n * np.cos(np.radians(ref_lat))
        assert np.hypot(north_err, east_err) < 2e-3
        assert -180.0 <= lon2 < 180.0
        assert g.geodesic_distance_m(lat, lon, lat2, lon2) == pytest.approx(dist, abs=1e-3)


def test_destination_published_line_and_simple_cases() -> None:
    # Vincenty direct, Flinders Peak: azimuth 306 52 05.37, 54 972.271 m -> Buninyong
    lat2, lon2 = g.destination(dms(-37, 57, 3.72030), dms(144, 25, 29.52440), dms(306, 52, 5.37), 54972.271)
    assert lat2 == pytest.approx(dms(-37, 39, 10.15610), abs=5e-8)
    assert lon2 == pytest.approx(dms(143, 55, 35.38390), abs=5e-8)
    # due east along the equator and due north along a meridian
    lat2, lon2 = g.destination(0.0, 10.0, 90.0, WGS84_A * np.radians(25.0))
    assert (lat2, lon2) == pytest.approx((0.0, 35.0), abs=1e-10)
    lat2, lon2 = g.destination(-33.0, 151.0, 0.0, meridian_arc(-33.0, 47.5))
    assert (lat2, lon2) == pytest.approx((47.5, 151.0), abs=1e-9)
    # zero distance, negative distance, antimeridian wrap
    assert g.destination(12.0, 34.0, 77.0, 0.0) == pytest.approx((12.0, 34.0), abs=1e-12)
    back = g.destination(*g.destination(12.0, 34.0, 77.0, 2.5e5), 77.0, 0.0)
    assert back == pytest.approx(g.destination(12.0, 34.0, 77.0, 2.5e5), abs=1e-12)
    lat_b, lon_b = g.destination(0.0, 0.0, 90.0, -1.0e5)
    assert (lat_b, lon_b) == pytest.approx((0.0, -np.degrees(1.0e5 / WGS84_A)), abs=1e-10)
    lat2, lon2 = g.destination(0.0, 179.9, 90.0, 5.0e4)
    assert lon2 < -179.0


def test_destination_round_trip_random() -> None:
    rng = np.random.default_rng(9)
    n = 500
    lat, lon = rng.uniform(-80, 80, n), rng.uniform(-180, 180, n)
    bearing, dist = rng.uniform(0, 360, n), rng.uniform(1.0, 1.8e7, n)
    lat2, lon2 = g.destination(lat, lon, bearing, dist)
    assert lat2.shape == (n,)
    assert np.max(np.abs(g.geodesic_distance_m(lat, lon, lat2, lon2) - dist)) < 1e-3


def test_geodesic_broadcast_and_validation() -> None:
    d = g.geodesic_distance_m(np.zeros((3, 1)), 0.0, 0.0, np.array([1.0, 2.0]))
    assert d.shape == (3, 2)
    assert np.allclose(d[:, 1], WGS84_A * np.radians(2.0), atol=1e-6)
    assert np.ndim(g.geodesic_distance_m(0.0, 0.0, 1.0, 1.0)) == 0
    with pytest.raises(ValueError, match="lat1"):
        g.geodesic_distance_m(95.0, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="lat"):
        g.destination(-91.0, 0.0, 0.0, 1.0)
