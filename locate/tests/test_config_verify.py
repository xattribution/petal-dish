"""Independent verification of the "config" unit.

Covers ``config/models.py``, ``config/loader.py``, ``core/antennas.py``,
``core/linkbudget.py``, ``core/iono.py`` and the three shipped YAML files.

Every expected value below is produced in this file from first principles: closed
forms written out here, linear watts instead of decibels, brute force, scipy
quadrature / root finding / interpolation, or a plain scalar re-statement of MATH.md
and ARCHITECTURE.md. Nothing is compared with a second call of the routine under test,
and none of the unit's helpers (nor the geodesy / orbit modules it leans on) is used to
produce an expectation. The physical constants are written out again on purpose.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import scipy.constants as sc
import yaml
from scipy import integrate, interpolate, optimize

from petal_locate.config import (
    ConfigError,
    EmitterModel,
    Region,
    SystemConfig,
    config_json_schema,
    dump_config,
    fit_monitor_frame,
    load_config,
    loads_config,
)
from petal_locate.core import antennas as ant
from petal_locate.core import iono
from petal_locate.core import linkbudget as lb

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
SHIPPED = ("pacific-uhf", "ku-adjacent", "leo-example")

# MATH.md section 1, written out again (not imported from the code under test).
C = 299_792_458.0
OMEGA_E = 7.2921151467e-5
GM = 3.986004418e14
J2 = 1.08262668e-3
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
K_LIN = 10.0 ** (-22.86)  # Boltzmann, W/K/Hz, from K_DBW = -228.6
R_E = 6371e3
T_REF = 1767225600.0  # 2026-01-01T00:00:00Z
GMST_REF = 1.756863409365046
OMEGA_GMST = 7.2921158554e-5
T_EVAL = T_REF + 3 * 3600.0  # epoch + 3 h (the epoch of MATH.md section 12 item 8)
SIDEREAL_DAY_S = 86164.0905

# The classic published ISS element set; both checksums are 7.
ISS_L1 = "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
ISS_L2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"


# --------------------------------------------------------------------------- independent helpers


def db(x: Any) -> Any:
    return 10.0 * np.log10(x)


def lin(x_db: Any) -> Any:
    return 10.0 ** (np.asarray(x_db, dtype=float) / 10.0)


def ecef(lat_deg: float, lon_deg: float, h_m: float) -> np.ndarray:
    """WGS84 geodetic to ECEF, the textbook closed form."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
    return np.array(
        [
            (n + h_m) * math.cos(lat) * math.cos(lon),
            (n + h_m) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - WGS84_E2) + h_m) * math.sin(lat),
        ]
    )


def enu(lat_deg: float, lon_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    return east, north, up


def elevation_deg(lat_deg: float, lon_deg: float, h_m: float, target: np.ndarray) -> float:
    """Elevation of ``target`` above the plane normal to the ellipsoid normal."""
    d = target - ecef(lat_deg, lon_deg, h_m)
    return math.degrees(math.asin(float(d @ enu(lat_deg, lon_deg)[2]) / float(np.linalg.norm(d))))


def angle_deg(u: np.ndarray, v: np.ndarray) -> float:
    """Angle between two vectors by Kahan's chord formula (accurate at 0 and at 180 degrees)."""
    a, b = u / np.linalg.norm(u), v / np.linalg.norm(v)
    return math.degrees(2.0 * math.atan2(float(np.linalg.norm(a - b)), float(np.linalg.norm(a + b))))


def rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def geo_position(orbit: dict[str, Any], t_s: float) -> np.ndarray:
    """MATH.md section 2 ``geo`` orbit, composed literally from the rotation matrices."""
    assert orbit["type"] == "geo" and orbit.get("epoch", "2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    drift = math.radians(orbit.get("drift_deg_per_day", 0.0)) / 86400.0
    n = OMEGA_E + drift
    a = (GM / n**2) ** (1.0 / 3.0)
    lon, phase = math.radians(orbit["lon_deg"]), math.radians(orbit.get("phase_deg", 0.0))
    inc = math.radians(orbit.get("inclination_deg", 0.0))
    dt = t_s - T_REF
    u = phase + n * dt
    r_i = rot_z(lon - phase) @ rot_x(inc) @ np.array([a * math.cos(u), a * math.sin(u), 0.0])
    return rot_z(-OMEGA_E * dt) @ r_i


def kepler_position(orbit: dict[str, Any], t_s: float) -> np.ndarray:
    """MATH.md section 2 ``kepler`` orbit: two-body, secular J2, rotated with the linear GMST."""
    assert orbit["type"] == "kepler" and orbit["epoch"] == "2026-01-01T00:00:00Z"
    a, e, inc = orbit["a_m"], orbit["e"], math.radians(orbit["inc_deg"])
    dt = t_s - T_REF
    n0 = math.sqrt(GM / a**3)
    k = 1.5 * J2 * (WGS84_A / (a * (1.0 - e * e))) ** 2 * n0
    raan = math.radians(orbit["raan_deg"]) - k * math.cos(inc) * dt
    argp = math.radians(orbit["argp_deg"]) + k * (2.0 - 2.5 * math.sin(inc) ** 2) * dt
    mean = math.radians(orbit["mean_anomaly_deg"]) + (
        n0 + k * math.sqrt(1.0 - e * e) * (1.0 - 1.5 * math.sin(inc) ** 2)
    ) * dt
    ecc = mean
    for _ in range(30):
        ecc -= (ecc - e * math.sin(ecc) - mean) / (1.0 - e * math.cos(ecc))
    perifocal = np.array([a * (math.cos(ecc) - e), a * math.sqrt(1.0 - e * e) * math.sin(ecc), 0.0])
    inertial = rot_z(raan) @ rot_x(inc) @ rot_z(argp) @ perifocal
    return rot_z(-(GMST_REF + OMEGA_GMST * dt)) @ inertial


def dish_gain_dbi(d_over_lambda: float, efficiency: float, theta_deg: float) -> float:
    """Scalar re-statement of the parabolic rule of ARCHITECTURE.md section 5. For dishes of 3
    wavelengths and more the document defines the side-lobe floor from 1 degree outwards only, so
    inside 1 degree this is the bare main lobe (the tests compare it at 0 and from 1 degree on)."""
    g0 = 10.0 * math.log10(efficiency * (math.pi * d_over_lambda) ** 2)
    if d_over_lambda < 3.0:
        hpbw = min(70.0 / d_over_lambda, 120.0)
        gain = max(g0 - 12.0 * (theta_deg / hpbw) ** 2, g0 - 15.0)
    else:
        hpbw = 70.0 / d_over_lambda
        gain = g0 - 12.0 * (theta_deg / hpbw) ** 2
        if 1.0 <= theta_deg <= 48.0:
            gain = max(gain, min(g0, 32.0 - 25.0 * math.log10(theta_deg)))
    return max(gain, -10.0)


def beam_gain_dbi(g0: float, hpbw: float, front_to_back: float, theta_deg: float) -> float:
    return max(g0 - 12.0 * (theta_deg / hpbw) ** 2, g0 - front_to_back)


def antenna_gain_dbi(antenna: dict[str, Any], theta_deg: float, freq_hz: float) -> float:
    """Gain of a raw (YAML) antenna entry, from the reference rules above."""
    kind = antenna["type"]
    if kind == "isotropic":
        return float(antenna.get("gain_dbi", 0.0))
    if kind == "beam":
        return beam_gain_dbi(antenna["gain_dbi"], antenna["hpbw_deg"], antenna.get("front_to_back_db", 20.0), theta_deg)
    assert kind == "parabolic"
    return dish_gain_dbi(antenna["diameter_m"] * freq_hz / C, antenna.get("efficiency", 0.55), theta_deg)


def raw(name: str) -> dict[str, Any]:
    """A shipped configuration as plain data, read without the code under test."""
    return yaml.safe_load((CONFIG_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def by_id(items: list[dict[str, Any]], ident: str) -> dict[str, Any]:
    (found,) = [item for item in items if item["id"] == ident]
    return found


def raw_channel(doc: dict[str, Any], channel_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for site in doc["sites"]:
        for channel in site["channels"]:
            if channel["id"] == channel_id:
                return site, channel
    raise AssertionError(channel_id)


def site_position(site: dict[str, Any]) -> np.ndarray:
    return ecef(site["lat_deg"], site["lon_deg"], site.get("alt_m", 0.0) + site.get("antenna_height_m", 10.0))


@pytest.fixture(scope="module")
def pacific() -> SystemConfig:
    return load_config(CONFIG_DIR / "pacific-uhf.yaml")


@pytest.fixture(scope="module")
def ku() -> SystemConfig:
    return load_config(CONFIG_DIR / "ku-adjacent.yaml")


@pytest.fixture(scope="module")
def leo() -> SystemConfig:
    return load_config(CONFIG_DIR / "leo-example.yaml")


@pytest.fixture(scope="module")
def pacific_raw() -> dict[str, Any]:
    return raw("pacific-uhf")


def minimal() -> dict[str, Any]:
    """A small valid configuration as plain data (one geo satellite, one site)."""
    return {
        "name": "mini",
        "satellites": [
            {
                "id": "s1",
                "name": "S1",
                "orbit": {"type": "geo", "lon_deg": 100.0},
                "transponders": [
                    {
                        "id": "t1",
                        "uplink_center_hz": 300.0e6,
                        "bandwidth_hz": 40.0e3,
                        "translation_hz": -40.0e6,
                        "g_over_t_dbk": -14.0,
                        "sfd_dbw_m2": -136.0,
                        "eirp_sat_dbw": 26.0,
                    }
                ],
            }
        ],
        "sites": [
            {
                "id": "g1",
                "name": "G1",
                "lat_deg": 10.0,
                "lon_deg": 110.0,
                "channels": [
                    {"id": "c1", "antenna": {"type": "isotropic"}, "pointing": {"satellite_id": "s1"}},
                ],
            }
        ],
    }


def expect_error(doc: dict[str, Any], *fragments: str) -> ConfigError:
    """The document must be rejected with a ValueError that mentions every fragment."""
    with pytest.raises(ValueError) as info:
        loads_config(json.dumps(doc), fmt="json")
    assert isinstance(info.value, ConfigError)
    for fragment in fragments:
        assert fragment in str(info.value), (fragment, str(info.value))
    return info.value


# =========================================================================== antennas


def test_parabolic_boresight_gain_and_beamwidth_closed_forms() -> None:
    # 2.4 m, efficiency 0.6, 11.95 GHz: lambda = 25.087 mm, D / lambda = 95.67
    model = {"type": "parabolic", "diameter_m": 2.4, "efficiency": 0.6}
    f = 11.95e9
    lam = C / f
    g0 = 10.0 * math.log10(0.6 * (math.pi * 2.4 / lam) ** 2)
    assert g0 == pytest.approx(47.34, abs=0.005)  # pocket-calculator value
    assert float(ant.peak_gain_dbi(model, f)) == pytest.approx(g0, abs=1e-10)
    assert float(ant.gain_dbi(model, 0.0, f)) == pytest.approx(g0, abs=1e-10)
    assert float(ant.hpbw_deg(model, f)) == pytest.approx(70.0 * lam / 2.4, rel=1e-12)
    # the half-power direction found by root finding on the pattern is HPBW / 2 off axis
    half = optimize.brentq(lambda th: float(ant.gain_dbi(model, th, f)) - (g0 - 3.0), 0.0, 0.9)
    assert 2.0 * half == pytest.approx(70.0 * lam / 2.4, rel=1e-9)
    assert not ant.electrically_small(model, f)


@pytest.mark.parametrize(
    "theta_deg,expected_dbi",
    [(2.0, 24.474), (5.0, 14.526), (10.0, 7.0), (20.0, -0.526), (40.0, -8.051), (47.9, -10.0), (48.0, -10.0), (90.0, -10.0), (180.0, -10.0)],
)
def test_parabolic_side_lobe_envelope_table(theta_deg: float, expected_dbi: float) -> None:
    # 32 - 25 log10(theta) by hand: 2 -> 24.474, 5 -> 14.526, 10 -> 7, 20 -> -0.526, 40 -> -8.051;
    # 47.9 -> -10.008 which the -10 dBi floor lifts to -10.
    model = SimpleNamespace(type="parabolic", diameter_m=2.4, efficiency=0.6)
    assert float(ant.gain_dbi(model, theta_deg, 11.95e9)) == pytest.approx(expected_dbi, abs=1e-3)


def test_parabolic_pattern_against_scalar_restatement_of_the_rule() -> None:
    rng = np.random.default_rng(20261113)
    ratios = np.concatenate([10.0 ** rng.uniform(-1.3, 2.6, 60), [0.5, 7.0 / 12.0, 1.0, 2.999999, 3.0, 3.000001, 155.0]])
    thetas = np.concatenate([np.linspace(1.0, 180.0, 359), [47.999, 48.0, 48.001]])
    for ratio in ratios:
        efficiency = float(rng.uniform(0.3, 0.8))
        model = {"type": "parabolic", "diameter_m": float(ratio), "efficiency": efficiency}
        got = ant.gain_dbi(model, thetas, C)  # lambda = 1 m, so diameter_m is D / lambda
        want = np.array([dish_gain_dbi(float(ratio), efficiency, float(th)) for th in thetas])
        np.testing.assert_allclose(got, want, rtol=0, atol=1e-9, err_msg=f"D/lambda={ratio}")


def test_parabolic_pattern_is_bounded_and_never_rises_off_axis() -> None:
    rng = np.random.default_rng(7)
    theta = np.linspace(0.0, 180.0, 7201)
    for ratio in 10.0 ** rng.uniform(-1.5, 2.7, 40):
        model = {"type": "parabolic", "diameter_m": float(ratio), "efficiency": 0.55}
        gain = ant.gain_dbi(model, theta, C)
        peak = float(ant.peak_gain_dbi(model, C))
        assert gain.shape == theta.shape
        assert gain[0] == pytest.approx(peak, abs=1e-12)
        assert np.all(gain <= peak + 1e-12) and np.all(gain >= -10.0)
        assert np.all(np.diff(gain) <= 1e-12), ratio
        # inside 1 degree the pattern lies between the bare main lobe and the boresight gain
        inner = theta < 1.0
        main = peak - 12.0 * (theta[inner] / float(ant.hpbw_deg(model, C))) ** 2
        assert np.all(gain[inner] >= main - 1e-12)


def test_electrically_small_dish_numbers_for_the_uhf_downlink() -> None:
    # 0.72 m at 260 MHz: lambda = 1.15305 m, D / lambda = 0.62443
    f = 260.0e6
    ratio = 0.72 * f / C
    assert ratio == pytest.approx(0.62443, abs=1e-5)
    g0 = 10.0 * math.log10(0.55 * (math.pi * ratio) ** 2)
    assert g0 == pytest.approx(3.256, abs=1e-3)
    model = {"type": "parabolic", "diameter_m": 0.72}  # efficiency defaults to 0.55
    assert ant.electrically_small(model, f) is True
    assert float(ant.peak_gain_dbi(model, f)) == pytest.approx(g0, abs=1e-10)
    assert float(ant.hpbw_deg(model, f)) == pytest.approx(70.0 / ratio, rel=1e-12)  # 112.1 deg, under the 120 deg cap
    assert float(ant.gain_dbi(model, 56.05, f)) == pytest.approx(g0 - 3.0, abs=2e-3)
    # floor G0 - 15 = -11.74 dBi would be below the absolute floor: -10 dBi wins behind the dish
    assert float(ant.gain_dbi(model, 180.0, f)) == pytest.approx(-10.0, abs=1e-12)
    # 1.2 m at 260 MHz: G0 = 7.69 dBi, floor G0 - 15 = -7.31 dBi (above -10)
    big = {"type": "parabolic", "diameter_m": 1.2, "efficiency": 0.55}
    g0_big = 10.0 * math.log10(0.55 * (math.pi * 1.2 * f / C) ** 2)
    assert g0_big == pytest.approx(7.693, abs=1e-3)
    assert float(ant.gain_dbi(big, 150.0, f)) == pytest.approx(g0_big - 15.0, abs=1e-10)
    # the beamwidth cap: D / lambda = 0.3 gives 70 / 0.3 = 233 deg, limited to 120
    assert float(ant.hpbw_deg({"type": "parabolic", "diameter_m": 0.3}, C)) == pytest.approx(120.0)
    assert float(ant.gain_dbi({"type": "parabolic", "diameter_m": 0.3, "efficiency": 0.55}, 60.0, C)) == pytest.approx(
        10.0 * math.log10(0.55 * (math.pi * 0.3) ** 2) - 3.0, abs=1e-10
    )


def test_three_wavelength_boundary_is_strict() -> None:
    at = {"type": "parabolic", "diameter_m": 3.0}
    below = {"type": "parabolic", "diameter_m": 3.0 - 1e-9}
    assert ant.electrically_small(at, C) is False  # D = 3 lambda exactly is not "under 3 wavelengths"
    assert ant.electrically_small(below, C) is True
    flags = ant.electrically_small(at, np.array([0.5 * C, C, 2.0 * C]))
    assert flags.dtype == bool and flags.tolist() == [True, False, False]
    for other in ({"type": "isotropic"}, {"type": "beam", "gain_dbi": 3.0, "hpbw_deg": 80.0}, {"type": "table", "theta_deg": [0, 90], "gain_dbi": [3, 0]}):
        assert ant.electrically_small(other, 1e6) is False


def test_shipped_uhf_antennas_do_not_radiate_more_than_isotropic_power() -> None:
    # (1 / 4 pi) * integral of G over the sphere is the radiated fraction; it cannot exceed 1.
    cases = [
        ({"type": "parabolic", "diameter_m": 0.72, "efficiency": 0.55}, 260.0e6),
        ({"type": "parabolic", "diameter_m": 1.2, "efficiency": 0.55}, 268.15e6),
        ({"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 20.0}, 260.0e6),
        ({"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 15.0}, 301.0e6),
    ]
    for model, f in cases:
        fraction, _ = integrate.quad(
            lambda th: 0.5 * 10.0 ** (float(ant.gain_dbi(model, math.degrees(th), f)) / 10.0) * math.sin(th),
            0.0,
            math.pi,
            limit=200,
        )
        assert 0.5 < fraction <= 1.0, (model, fraction)


def test_beam_pattern_half_power_and_back_lobe() -> None:
    model = {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 15.0}
    assert float(ant.peak_gain_dbi(model, 3e8)) == 10.0 and float(ant.hpbw_deg(model, 3e8)) == 52.0
    assert float(ant.gain_dbi(model, 26.0, 3e8)) == pytest.approx(7.0, abs=1e-12)
    assert float(ant.gain_dbi(model, -26.0, 3e8)) == pytest.approx(7.0, abs=1e-12)  # symmetric
    # main lobe reaches the back-lobe level where 12 (theta / 52)^2 = 15, i.e. theta = 58.14 deg
    knee = 52.0 * math.sqrt(15.0 / 12.0)
    assert float(ant.gain_dbi(model, knee - 0.5, 3e8)) > -5.0 > float(ant.gain_dbi(model, knee + 0.5, 3e8)) - 1e-12
    np.testing.assert_allclose(ant.gain_dbi(model, np.array([60.0, 120.0, 180.0]), 3e8), -5.0, atol=1e-12)
    default = {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0}
    assert float(ant.gain_dbi(default, 180.0, 3e8)) == pytest.approx(-10.0)  # front_to_back_db defaults to 20
    thetas = np.linspace(0.0, 180.0, 181)
    want = [beam_gain_dbi(10.0, 52.0, 15.0, float(th)) for th in thetas]
    np.testing.assert_allclose(ant.gain_dbi(model, thetas, 3e8), want, atol=1e-12)


def test_table_pattern_against_scipy_interpolation() -> None:
    theta = [2.0, 10.0, 30.0, 90.0, 150.0]
    gain = [12.0, 9.5, 1.0, -6.0, -3.0]
    model = {"type": "table", "theta_deg": theta, "gain_dbi": gain}
    reference = interpolate.interp1d(theta, gain, kind="linear", bounds_error=False, fill_value=(gain[0], gain[-1]))
    query = np.linspace(0.0, 180.0, 721)
    np.testing.assert_allclose(ant.gain_dbi(model, query, 1e9), reference(query), atol=1e-12)
    assert float(ant.peak_gain_dbi(model, 1e9)) == 12.0
    # -3 dB relative to boresight (12 dBi) is 9 dBi, crossed between 10 and 30 degrees
    crossing = optimize.brentq(lambda th: float(reference(th)) - 9.0, 2.0, 150.0)
    assert float(ant.hpbw_deg(model, 1e9)) == pytest.approx(2.0 * crossing, rel=1e-12)
    flat = {"type": "table", "theta_deg": [0.0, 180.0], "gain_dbi": [2.0, 0.5]}
    assert float(ant.hpbw_deg(flat, 1e9)) == 360.0  # never 3 dB down
    assert float(ant.hpbw_deg({"type": "isotropic", "gain_dbi": 2.0}, 1e9)) == 360.0
    assert float(ant.gain_dbi({"type": "isotropic", "gain_dbi": 2.0}, 77.0, 1e9)) == 2.0


def test_antenna_broadcasting_matches_scalar_calls() -> None:
    model = {"type": "parabolic", "diameter_m": 1.8, "efficiency": 0.6}
    theta = np.array([[0.0], [0.7], [3.0], [55.0]])
    freq = np.array([250e6, 1.5e9, 12e9])
    grid = ant.gain_dbi(model, theta, freq)
    assert grid.shape == (4, 3)
    for i in range(4):
        for j in range(3):
            assert grid[i, j] == pytest.approx(float(ant.gain_dbi(model, float(theta[i, 0]), float(freq[j]))), abs=1e-12)
    assert ant.peak_gain_dbi(model, freq).shape == (3,) and ant.hpbw_deg(model, freq).shape == (3,)
    assert np.ndim(ant.gain_dbi(model, 1.0, 12e9)) == 0


@pytest.mark.parametrize(
    "model,theta,freq,fragment",
    [
        ({"type": "parabolic", "diameter_m": 1.0}, 181.0, 1e9, "off_axis_deg"),
        ({"type": "parabolic", "diameter_m": 1.0}, float("nan"), 1e9, "off_axis_deg"),
        ({"type": "parabolic", "diameter_m": 1.0}, 1.0, 0.0, "freq_hz"),
        ({"type": "parabolic", "diameter_m": 1.0}, 1.0, -1e9, "freq_hz"),
        ({"type": "parabolic"}, 1.0, 1e9, "diameter_m"),
        ({"type": "parabolic", "diameter_m": -1.0}, 1.0, 1e9, "diameter_m"),
        ({"type": "parabolic", "diameter_m": 1.0, "efficiency": 1.5}, 1.0, 1e9, "efficiency"),
        ({"type": "beam", "gain_dbi": 10.0}, 1.0, 1e9, "hpbw_deg"),
        ({"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 0.0}, 1.0, 1e9, "hpbw_deg"),
        ({"type": "table", "theta_deg": [0.0, 10.0], "gain_dbi": [1.0]}, 1.0, 1e9, "gain_dbi"),
        ({"type": "table", "theta_deg": [10.0, 5.0], "gain_dbi": [1.0, 0.0]}, 1.0, 1e9, "theta_deg"),
        ({"type": "horn"}, 1.0, 1e9, "type"),
        ({}, 1.0, 1e9, "type"),
    ],
)
def test_antenna_invalid_input_raises_naming_the_field(model: dict[str, Any], theta: float, freq: float, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        ant.gain_dbi(model, theta, freq)


# =========================================================================== ionosphere


def test_iono_check_value_and_physical_constant() -> None:
    # MATH.md: 30 TECU, 305 MHz, zenith = 434 ns (433.5 before rounding)
    delay = float(iono.group_delay_s(30.0, 90.0, 305.0e6))
    assert delay == pytest.approx(433.5e-9, abs=0.1e-9)
    # first principles: group delay = e^2 / (8 pi^2 eps0 m_e) * STEC / (c f^2); the constant is 40.308
    k_phys = sc.e**2 / (8.0 * math.pi**2 * sc.epsilon_0 * sc.m_e)
    assert k_phys == pytest.approx(40.3, abs=0.01)
    assert delay == pytest.approx(k_phys * 30.0e16 / (sc.c * 305.0e6**2), rel=3e-4)


@pytest.mark.parametrize("el_deg", [0.0, 0.5, 3.0, 11.5, 30.0, 57.0, 85.0, 90.0])
def test_iono_slant_factor_is_the_path_length_through_a_thin_shell(el_deg: float) -> None:
    # A straight ray leaving the sphere of radius R_E at elevation el is at radius r after
    # s(r) = -R sin(el) + sqrt(r^2 - R^2 cos^2(el)). Path inside a 20 m shell at 350 km / 20 m.
    el = math.radians(el_deg)

    def along(radius: float) -> float:
        return -R_E * math.sin(el) + math.sqrt(radius**2 - (R_E * math.cos(el)) ** 2)

    shell = R_E + 350e3
    factor = (along(shell + 10.0) - along(shell - 10.0)) / 20.0
    assert float(iono.slant_factor(el_deg)) == pytest.approx(factor, rel=1e-7)
    assert 1.0 <= float(iono.slant_factor(el_deg)) <= 3.15


def test_iono_delay_scaling_legs_and_broadcast() -> None:
    assert float(iono.slant_factor(90.0)) == pytest.approx(1.0, abs=1e-15)
    assert float(iono.slant_factor(0.0)) == pytest.approx(6721.0 / math.sqrt(6721.0**2 - 6371.0**2), rel=1e-12)
    base = float(iono.group_delay_s(10.0, 40.0, 300e6))
    assert float(iono.group_delay_s(20.0, 40.0, 300e6)) == pytest.approx(2.0 * base, rel=1e-12)
    assert float(iono.group_delay_s(10.0, 40.0, 600e6)) == pytest.approx(base / 4.0, rel=1e-12)
    assert float(iono.group_delay_s(0.0, 40.0, 300e6)) == 0.0
    # the lower (downlink) frequency of a UHF path is delayed more: (301 / 260)^2
    up = float(iono.group_delay_s(30.0, 25.0, 301e6))
    down = float(iono.group_delay_s(30.0, 25.0, 260e6))
    assert down / up == pytest.approx((301.0 / 260.0) ** 2, rel=1e-12)
    grid = iono.group_delay_s(np.array([5.0, 30.0])[:, None, None], np.array([10.0, 45.0, 90.0])[None, :, None], np.array([260e6, 301e6, 12e9, 14e9]))
    assert grid.shape == (2, 3, 4)
    assert grid[1, 2, 1] == pytest.approx(40.3 * 30e16 / (C * 301e6**2), rel=1e-12)
    assert np.all(np.diff(grid, axis=1) < 0) and np.all(np.diff(grid, axis=2) < 0) and np.all(np.diff(grid, axis=0) > 0)


@pytest.mark.parametrize(
    "args,fragment",
    [
        ((30.0, -0.1, 3e8), "el_deg"),
        ((30.0, 90.1, 3e8), "el_deg"),
        ((30.0, float("nan"), 3e8), "el_deg"),
        ((-1.0, 45.0, 3e8), "vtec_tecu"),
        ((30.0, 45.0, 0.0), "freq_hz"),
        ((30.0, 45.0, float("inf")), "freq_hz"),
    ],
)
def test_iono_invalid_input_raises_naming_the_field(args: tuple[float, float, float], fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        iono.group_delay_s(*args)


# =========================================================================== link budget legs


def test_fspl_is_the_friis_ratio() -> None:
    for dist, freq in [(1.0e3, 2.4e9), (3.9e7, 260e6), (3.5786e7, 12e9), (25.0, 301e6)]:
        ratio = (C / freq / (4.0 * math.pi * dist)) ** 2  # received / transmitted between isotropic antennas
        assert float(lb.fspl_db(dist, freq)) == pytest.approx(-10.0 * math.log10(ratio), abs=1e-10)
    # textbook: 32.45 + 20 log10(d_km) + 20 log10(f_MHz); 35786 km at 12 GHz is 205.1 dB
    assert float(lb.fspl_db(3.5786e7, 12e9)) == pytest.approx(32.45 + 20 * math.log10(35786.0) + 20 * math.log10(12000.0), abs=0.01)
    assert float(lb.fspl_db(3.5786e7, 12e9)) == pytest.approx(205.1, abs=0.05)
    grid = lb.fspl_db(np.array([1e3, 1e4])[:, None], np.array([1e8, 1e9, 1e10]))
    assert grid.shape == (2, 3)
    np.testing.assert_allclose(np.diff(grid, axis=0), 20.0, atol=1e-10)
    np.testing.assert_allclose(np.diff(grid, axis=1), 20.0, atol=1e-10)
    for bad in [(0.0, 1e9), (-1.0, 1e9), (1e3, 0.0), (float("nan"), 1e9)]:
        with pytest.raises(ValueError):
            lb.fspl_db(*bad)


def test_uplink_leg_from_first_principles_in_watts() -> None:
    # Emitter on the equator at 100 E, satellite on the equator at 130 E, geostationary radius.
    x_e = ecef(0.0, 100.0, 0.0)
    sat = 42164172.37 * np.array([math.cos(math.radians(130.0)), math.sin(math.radians(130.0)), 0.0])
    f_e = 301.0e6
    eirp_w, g_rx, t_sat = 20.0, 2.5, 2.5 / 10.0 ** (-1.4)  # G/T = -14 dB/K whatever the split
    transponder = SimpleNamespace(g_over_t_dbk=-14.0)
    rho = float(np.linalg.norm(sat - x_e))
    pfd = eirp_w / (4.0 * math.pi * rho**2)  # W/m^2
    carrier_w = pfd * g_rx * (C / f_e) ** 2 / (4.0 * math.pi)  # flux times effective aperture
    cn0 = carrier_w / (K_LIN * t_sat)
    up = lb.uplink_budget(db(eirp_w), None, None, x_e, sat, f_e, transponder)
    assert float(up.eirp_toward_sat_dbw) == pytest.approx(db(eirp_w), abs=1e-12)
    assert float(up.pfd_dbw_m2) == pytest.approx(db(pfd), abs=1e-9)
    assert float(up.cn0_up_dbhz) == pytest.approx(db(cn0), abs=1e-9)
    # spherical-trig elevation for an equatorial pair 30 degrees apart: tan(el) = (cos g - R/a) / sin g
    gamma = math.radians(30.0)
    el = math.degrees(math.atan((math.cos(gamma) - WGS84_A / 42164172.37) / math.sin(gamma)))
    assert float(up.elevation_deg) == pytest.approx(el, abs=1e-9)


def test_uplink_pattern_is_relative_to_boresight_and_batches() -> None:
    antenna = {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 15.0}
    sat = np.array([42164172.37, 0.0, 0.0])
    points = np.array([ecef(0.0, 0.0, 0.0), ecef(20.0, 30.0, 0.0), ecef(-40.0, -50.0, 100.0), ecef(0.0, 120.0, 0.0)])
    boresight = np.array([0.0, 0.0, 1.0])  # every emitter points along +z
    up = lb.uplink_budget(13.0, antenna, boresight, points, sat, 301e6, {"g_over_t_dbk": -14.0})
    assert up.eirp_toward_sat_dbw.shape == (4,)
    for k, point in enumerate(points):
        off = angle_deg(boresight, sat - point)
        rel = beam_gain_dbi(10.0, 52.0, 15.0, off) - 10.0
        rho = float(np.linalg.norm(sat - point))
        assert up.eirp_toward_sat_dbw[k] == pytest.approx(13.0 + rel, abs=1e-9)
        assert up.pfd_dbw_m2[k] == pytest.approx(13.0 + rel - db(4.0 * math.pi * rho**2), abs=1e-9)
    assert up.elevation_deg[0] == pytest.approx(90.0, abs=1e-6)
    assert up.elevation_deg[3] < 0.0  # 120 degrees of longitude away: below the horizon, and reported so
    lat, lon, h = -40.0, -50.0, 100.0
    assert up.elevation_deg[2] == pytest.approx(elevation_deg(lat, lon, h, sat), abs=1e-8)


def test_transponder_noise_density_from_the_receiver_chain() -> None:
    # A flux PFD is received as PFD * G_r lambda^2 / 4 pi and leaves as EIRP_sat * PFD / SFD, so the
    # transponder multiplies input power by EIRP_sat * 4 pi / (SFD G_r lambda^2). Its input noise
    # density is k T. The product does not depend on how G/T is split into G_r and T.
    tp = SimpleNamespace(eirp_sat_dbw=26.0, sfd_dbw_m2=-136.0, g_over_t_dbk=-14.0, uplink_center_hz=301.0e6, bandwidth_hz=25e3)
    lam = C / 301.0e6
    for g_rx in (1.0, 37.0):
        t_sat = g_rx / 10.0 ** (-1.4)
        gain = 10.0**2.6 * 4.0 * math.pi / (10.0**-13.6 * g_rx * lam**2)
        assert lb.transponder_noise_eirp_dbw_hz(tp) == pytest.approx(db(K_LIN * t_sat * gain), abs=1e-9)
    assert lb.transponder_noise_eirp_dbw_hz(tp) == pytest.approx(-41.57, abs=0.01)  # by hand: 26 + 136 + 11.03 + 14 - 228.6
    quiet = lb.transponder_outputs(np.empty(0), tp)
    assert quiet.eirp_down_dbw.shape == (0,)
    assert quiet.relayed_n0_eirp_dbw_hz == pytest.approx(-41.57, abs=0.01)
    # noise power over 25 kHz is 2.41 dBW, 23.59 dB under the 26 dBW saturated EIRP
    assert quiet.backoff_db == pytest.approx(26.0 - (-41.57 + 43.98), abs=0.01)


def test_transponder_power_robbing_brute_force_in_watts() -> None:
    rng = np.random.default_rng(42)
    tp = {"eirp_sat_dbw": 26.0, "sfd_dbw_m2": -136.0, "g_over_t_dbk": -14.0, "uplink_center_hz": 301.0e6, "bandwidth_hz": 25e3}
    sat_w, sfd_w = 10.0**2.6, 10.0**-13.6
    n0_w = sat_w / sfd_w * 4.0 * math.pi * (301.0e6 / C) ** 2 * K_LIN / 10.0**-1.4
    saturated_cases = 0
    for _ in range(200):
        k = int(rng.integers(1, 6))
        pfd_db = rng.uniform(-175.0, -128.0, k)
        carriers = [sat_w * 10.0 ** (p / 10.0) / sfd_w for p in pfd_db]
        total = sum(carriers) + n0_w * 25e3
        factor = sat_w / total if total > sat_w else 1.0
        saturated_cases += total > sat_w
        out = lb.transponder_outputs(pfd_db, tp)
        np.testing.assert_allclose(lin(out.eirp_down_dbw), [c * factor for c in carriers], rtol=1e-10)
        assert isinstance(out.relayed_n0_eirp_dbw_hz, float) and isinstance(out.backoff_db, float)
        assert 10.0 ** (out.relayed_n0_eirp_dbw_hz / 10.0) == pytest.approx(n0_w * factor, rel=1e-10)
        assert out.backoff_db == pytest.approx(db(sat_w / total), abs=1e-9)
        radiated = float(np.sum(lin(out.eirp_down_dbw))) + 10.0 ** (out.relayed_n0_eirp_dbw_hz / 10.0) * 25e3
        assert radiated <= sat_w * (1.0 + 1e-10)
        if total > sat_w:
            assert radiated == pytest.approx(sat_w, rel=1e-10)
    assert 20 < saturated_cases < 180  # both regimes were exercised


def test_transponder_single_carrier_at_sfd_and_absent_carriers() -> None:
    tp = {"eirp_sat_dbw": 50.0, "sfd_dbw_m2": -85.0, "g_over_t_dbk": 3.0, "uplink_center_hz": 14.25e9, "bandwidth_hz": 36e6}
    # 14.25 GHz: 10 log10(4 pi f^2 / c^2) = 44.53 dB; EIRP_n0 = 50 + 85 + 44.53 - 3 - 228.6 = -52.07 dBW/Hz
    n0 = lb.transponder_noise_eirp_dbw_hz(tp)
    assert n0 == pytest.approx(-52.07, abs=0.01)
    below = lb.transponder_outputs([-95.0], tp)
    assert below.eirp_down_dbw[0] == pytest.approx(40.0, abs=1e-9)  # 10 dB under SFD -> 10 dB under saturation
    assert below.relayed_n0_eirp_dbw_hz == pytest.approx(n0, abs=1e-12)
    absent = lb.transponder_outputs([-np.inf, -95.0], tp)
    assert absent.eirp_down_dbw[0] == -np.inf and absent.eirp_down_dbw[1] == pytest.approx(40.0, abs=1e-9)
    batch = lb.transponder_outputs(np.array([[-95.0, -np.inf], [-85.0, -85.0]]), tp)
    assert batch.eirp_down_dbw.shape == (2, 2) and np.shape(batch.backoff_db) == (2,)
    noise_w = 10.0 ** (n0 / 10.0) * 36e6
    total = 2.0 * 1e5 + noise_w
    np.testing.assert_allclose(lin(batch.eirp_down_dbw[1]), 1e5 * 1e5 / total, rtol=1e-10)
    assert batch.relayed_n0_eirp_dbw_hz[0] == pytest.approx(n0, abs=1e-12)
    assert batch.relayed_n0_eirp_dbw_hz[1] == pytest.approx(n0 + db(1e5 / total), abs=1e-9)
    for bad in ([np.nan], [np.inf]):
        with pytest.raises(ValueError, match="pfd_dbw_m2"):
            lb.transponder_outputs(bad, tp)


def test_downlink_leg_from_first_principles_in_watts() -> None:
    site_lat, site_lon, site_h = 35.0, 139.0, 60.0
    site = ecef(site_lat, site_lon, site_h)
    sat = 42164172.37 * np.array([math.cos(math.radians(160.0)), math.sin(math.radians(160.0)), 0.02])
    channel = SimpleNamespace(
        antenna={"type": "beam", "gain_dbi": 12.0, "hpbw_deg": 40.0, "front_to_back_db": 25.0},
        system_noise_temp_k=290.0,
        polarization_loss_db=1.5,
    )
    east, north, up = enu(site_lat, site_lon)
    boresight = math.cos(math.radians(30.0)) * (-north) + math.sin(math.radians(30.0)) * up  # due south, 30 deg up
    f_d = 260.0e6
    rho = float(np.linalg.norm(sat - site))
    off = angle_deg(boresight, sat - site)
    gain = 10.0 ** (beam_gain_dbi(12.0, 40.0, 25.0, off) / 10.0)
    spreading = (C / f_d / (4.0 * math.pi * rho)) ** 2
    l_pol = 10.0**0.15
    eirp_w = np.array([4.0, 0.25, 0.0])
    n0_eirp_w = 7e-5
    with np.errstate(divide="ignore"):
        down = lb.downlink_budget(db(eirp_w), db(n0_eirp_w), f_d, sat, site, channel, boresight)
    np.testing.assert_allclose(lin(down.c_rx_dbw), eirp_w * gain * spreading / l_pol, rtol=1e-10)
    assert 10.0 ** (float(down.relayed_n0_dbw_hz) / 10.0) == pytest.approx(n0_eirp_w * gain * spreading / l_pol, rel=1e-10)
    assert 10.0 ** (down.site_n0_dbw_hz / 10.0) == pytest.approx(K_LIN * 290.0, rel=1e-12)
    assert float(down.off_axis_deg) == pytest.approx(off, abs=1e-8)
    assert float(down.elevation_deg) == pytest.approx(elevation_deg(site_lat, site_lon, site_h, sat), abs=1e-8)
    total = K_LIN * 290.0 + n0_eirp_w * gain * spreading / l_pol
    assert float(down.n0_total_dbw_hz) == pytest.approx(db(total), abs=1e-9)
    assert float(down.relay_rise_db) == pytest.approx(db(total / (K_LIN * 290.0)), abs=1e-9)


def test_direct_path_hand_calculation_and_radio_horizon() -> None:
    lat, lon = 26.212, 127.679
    site = ecef(lat, lon, 10.0)
    east, north, up = enu(lat, lon)
    channel = {"antenna": {"type": "parabolic", "diameter_m": 0.72, "efficiency": 0.55}, "polarization_loss_db": 3.0}
    # the dish looks north-north-east (azimuth 30 deg), 20 deg up: 36 deg off the emitters, 144 deg off their mirror
    channel_boresight = math.cos(math.radians(20.0)) * (0.5 * east + math.sqrt(0.75) * north) + math.sin(math.radians(20.0)) * up
    f = 260.0e6
    emitter_antenna = {"type": "beam", "gain_dbi": 6.0, "hpbw_deg": 90.0, "front_to_back_db": 12.0}
    # emitters due north of the site, displaced along the local tangent plane
    distances = np.array([200.0, 8.0e3, 18.0e3, 26.0e3, 26.2e3, 60.0e3])
    points = site + distances[:, None] * north
    horizon = 4.12e3 * (math.sqrt(10.0) + math.sqrt(10.0))  # 26.06 km for two 10 m masts
    assert horizon == pytest.approx(26057.0, abs=1.0)
    emitter_boresight = -north  # aimed straight at the site
    out = lb.direct_budget(5.0, emitter_antenna, emitter_boresight, points, 10.0, site, 10.0, channel, channel_boresight, f)
    assert out.in_horizon.tolist() == [True, True, True, True, False, False]
    g0 = 10.0 * math.log10(0.55 * (math.pi * 0.72 * f / C) ** 2)
    hpbw = 70.0 * C / f / 0.72
    for k, dist in enumerate(distances):
        if dist >= horizon:
            assert out.c_rx_dbw[k] == -np.inf
            continue
        off_site = angle_deg(channel_boresight, points[k] - site)
        assert off_site == pytest.approx(math.degrees(math.acos(math.cos(math.radians(20.0)) * math.sqrt(0.75))), abs=1e-6)  # 35.5 deg
        site_gain = max(g0 - 12.0 * (off_site / hpbw) ** 2, g0 - 15.0, -10.0)
        friis = (C / f / (4.0 * math.pi * dist)) ** 2
        want = 5.0 + 10.0 * math.log10(friis) + site_gain - 3.0  # emitter on its own boresight
        assert out.c_rx_dbw[k] == pytest.approx(want, abs=1e-8)
    # an omni emitter with lower masts: horizon 4.12 km (sqrt(2) + sqrt(10)) = 18.86 km
    low = lb.direct_budget(5.0, None, None, points, 2.0, site, 10.0, channel, None, f)
    assert low.in_horizon.tolist() == [True, True, True, False, False, False]
    assert low.c_rx_dbw[1] == pytest.approx(5.0 + 10.0 * math.log10((C / f / (4.0 * math.pi * 8.0e3)) ** 2) + g0 - 3.0, abs=1e-8)
    single = lb.direct_budget(5.0, None, None, points[1], 2.0, site, 10.0, channel, None, f)
    assert isinstance(single.in_horizon, bool) and single.in_horizon and single.c_rx_dbw == pytest.approx(low.c_rx_dbw[1])
    # the emitter's own pattern: turned 60 degrees away from the site it is 12 (60 / 90)^2 = 5.33 dB down,
    # turned right round (boresight pointing away) it is on its 12 dB back lobe
    aside = math.cos(math.radians(60.0)) * (-north) + math.sin(math.radians(60.0)) * east
    turned = lb.direct_budget(5.0, emitter_antenna, aside, points[1], 10.0, site, 10.0, channel, channel_boresight, f)
    assert turned.c_rx_dbw == pytest.approx(float(out.c_rx_dbw[1]) - 12.0 * (60.0 / 90.0) ** 2, abs=1e-8)
    away = lb.direct_budget(5.0, emitter_antenna, north, points[1], 10.0, site, 10.0, channel, channel_boresight, f)
    assert away.c_rx_dbw == pytest.approx(float(out.c_rx_dbw[1]) - 12.0, abs=1e-8)
    # the same emitter on the far side of the site (due south) is 144 deg off the dish: on its G0 - 15 dB floor
    behind = lb.direct_budget(5.0, None, None, site - 8.0e3 * north, 10.0, site, 10.0, channel, channel_boresight, f)
    assert behind.c_rx_dbw == pytest.approx(5.0 + 10.0 * math.log10((C / f / (4.0 * math.pi * 8.0e3)) ** 2) + max(g0 - 15.0, -10.0) - 3.0, abs=1e-8)


# =========================================================================== whole paths, by hand


def hand_path_budget(
    doc: dict[str, Any],
    emitter: dict[str, Any],
    x_e: np.ndarray,
    emitter_lla: tuple[float, float, float],
    f_e: float,
    sat_id: str,
    transponder_id: str,
    channel_id: str,
    t_s: float,
    other_pfd_w_m2: tuple[float, ...] = (),
) -> dict[str, float] | None:
    """Uplink -> transponder -> downlink in watts, straight from MATH.md section 4."""
    satellite = by_id(doc["satellites"], sat_id)
    tp = by_id(satellite["transponders"], transponder_id)
    if abs(f_e - tp["uplink_center_hz"]) > 0.5 * tp["bandwidth_hz"]:
        return None
    sat = geo_position(satellite["orbit"], t_s)
    site, channel = raw_channel(doc, channel_id)
    x_g = site_position(site)
    el_site = elevation_deg(site["lat_deg"], site["lon_deg"], site.get("alt_m", 0.0) + site.get("antenna_height_m", 10.0), sat)
    el_emitter = elevation_deg(*emitter_lla, sat)
    if el_site <= 0.0 or el_emitter <= 0.0:
        return None

    # uplink
    rho_u = float(np.linalg.norm(sat - x_e))
    pointing = emitter.get("pointing", "omni")
    if pointing == "omni":
        off_e, rel_db = 0.0, 0.0
    else:
        if "satellite_id" in pointing:
            aim = geo_position(by_id(doc["satellites"], pointing["satellite_id"])["orbit"], t_s) - x_e
        else:  # fixed azimuth (clockwise from north) and elevation at the emitter
            east, north, up = enu(emitter_lla[0], emitter_lla[1])
            az, el = math.radians(pointing["az_deg"]), math.radians(pointing["el_deg"])
            aim = math.cos(el) * (math.sin(az) * east + math.cos(az) * north) + math.sin(el) * up
        off_e = angle_deg(aim, sat - x_e)
        rel_db = antenna_gain_dbi(emitter["antenna"], off_e, f_e) - antenna_gain_dbi(emitter["antenna"], 0.0, f_e)
    eirp_w = 10.0 ** ((emitter["eirp_dbw"] + rel_db) / 10.0)
    pfd = eirp_w / (4.0 * math.pi * rho_u**2)
    g_over_t = 10.0 ** (tp["g_over_t_dbk"] / 10.0)
    cn0_up = eirp_w * g_over_t * (C / f_e / (4.0 * math.pi * rho_u)) ** 2 / K_LIN

    # transponder
    sat_w, sfd = 10.0 ** (tp["eirp_sat_dbw"] / 10.0), 10.0 ** (tp["sfd_dbw_m2"] / 10.0)
    n0_eirp = sat_w / sfd * 4.0 * math.pi * (tp["uplink_center_hz"] / C) ** 2 * K_LIN / g_over_t
    carriers = [sat_w * p / sfd for p in (pfd, *other_pfd_w_m2)]
    total = sum(carriers) + n0_eirp * tp["bandwidth_hz"]
    factor = min(1.0, sat_w / total)
    eirp_c = carriers[0] * factor
    n0_eirp *= factor

    # downlink
    f_d = f_e + tp["translation_hz"]
    rho_d = float(np.linalg.norm(sat - x_g))
    tracked = geo_position(by_id(doc["satellites"], channel["pointing"]["satellite_id"])["orbit"], t_s)
    off_g = angle_deg(tracked - x_g, sat - x_g)
    g_site = 10.0 ** (antenna_gain_dbi(channel["antenna"], off_g, f_d) / 10.0)
    l_pol = 10.0 ** (channel.get("polarization_loss_db", 0.0) / 10.0)
    spreading = (C / f_d / (4.0 * math.pi * rho_d)) ** 2
    c_rx = eirp_c * g_site * spreading / l_pol
    n0_relay = n0_eirp * g_site * spreading / l_pol
    n0_site = K_LIN * channel.get("system_noise_temp_k", 400.0)
    return {
        "eirp_toward_sat_dbw": db(eirp_w),
        "pfd_dbw_m2": db(pfd),
        "cn0_up_dbhz": db(cn0_up),
        "el_emitter_deg": el_emitter,
        "off_emitter_deg": off_e,
        "transponder_eirp_dbw": db(eirp_c),
        "backoff_db": db(sat_w / total),
        "c_rx_dbw": db(c_rx),
        "relayed_n0_dbw_hz": db(n0_relay),
        "site_n0_dbw_hz": db(n0_site),
        "el_site_deg": el_site,
        "off_site_deg": off_g,
        "cn0_total_dbhz": db(c_rx / (n0_site + n0_relay)),
        "cn0_cascade_dbhz": db(1.0 / (1.0 / cn0_up + n0_site / c_rx)),
    }


def assert_budget_matches(got: lb.PathBudget, want: dict[str, float]) -> None:
    assert float(got.uplink.eirp_toward_sat_dbw) == pytest.approx(want["eirp_toward_sat_dbw"], abs=1e-8)
    assert float(got.uplink.pfd_dbw_m2) == pytest.approx(want["pfd_dbw_m2"], abs=1e-8)
    assert float(got.uplink.cn0_up_dbhz) == pytest.approx(want["cn0_up_dbhz"], abs=1e-8)
    assert float(got.uplink.elevation_deg) == pytest.approx(want["el_emitter_deg"], abs=1e-7)
    assert float(got.transponder_eirp_dbw) == pytest.approx(want["transponder_eirp_dbw"], abs=1e-8)
    assert float(got.downlink.c_rx_dbw) == pytest.approx(want["c_rx_dbw"], abs=1e-8)
    assert float(got.downlink.relayed_n0_dbw_hz) == pytest.approx(want["relayed_n0_dbw_hz"], abs=1e-8)
    assert float(got.downlink.site_n0_dbw_hz) == pytest.approx(want["site_n0_dbw_hz"], abs=1e-9)
    assert float(got.downlink.elevation_deg) == pytest.approx(want["el_site_deg"], abs=1e-7)
    assert float(got.downlink.off_axis_deg) == pytest.approx(want["off_site_deg"], abs=1e-6)
    assert float(got.cn0_total_dbhz) == pytest.approx(want["cn0_total_dbhz"], abs=1e-8)


# A test emitter at the ref-guam location: 13 dBW from a 10 dBi beam (15 dB front-to-back)
# aimed at muos-io, on ref-guam's frequency. It is the reference design shipped before the
# references became omnidirectional, kept here because its beam exercises the emitter's
# off-axis gain toward the other satellites, which an omni reference no longer does.
BEAM_EMITTER: dict[str, Any] = {
    "eirp_dbw": 13.0,
    "antenna": {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 15.0},
    "pointing": {"satellite_id": "muos-io"},
    "bandwidth_hz": 5.0e3,
    "waveform": "noise",
}


def beam_emitter() -> EmitterModel:
    return EmitterModel.model_validate(BEAM_EMITTER)


def guam(doc: dict[str, Any]) -> tuple[dict[str, Any], np.ndarray, tuple[float, float, float]]:
    """ref-guam's carrier (location, frequency) with the test beam emitter in place of its own."""
    carrier = dict(by_id(doc["carriers"], "ref-guam"), emitter=copy.deepcopy(BEAM_EMITTER))
    loc = carrier["location"]
    lla = (loc["lat_deg"], loc["lon_deg"], loc.get("alt_m", 0.0) + loc.get("antenna_height_m", 10.0))
    return carrier, ecef(*lla), lla


def test_pacific_full_budget_by_hand_on_axis(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    """The test beam emitter at ref-guam (13 dBW, 10 dBi beam aimed at muos-io) -> muos-io shared-1 -> tokyo-io."""
    carrier, x_e, lla = guam(pacific_raw)
    f_e = carrier["uplink_center_hz"]
    want = hand_path_budget(pacific_raw, carrier["emitter"], x_e, lla, f_e, "muos-io", "shared-1", "tokyo-io", T_EVAL)
    assert want is not None
    # Coarse pocket-calculator values first (slant range about 3.9e7 m, lambda = 0.996 m up, 1.153 m down):
    #   PFD = 13 - 10 log10(4 pi rho^2) = 13 - 162.8 ~ -150 dBW/m^2; EIRP_c = 26 - (-136 + 150) ~ 12 dBW;
    #   C/N0_up = 13 - 174 - 14 + 228.6 ~ 53.5 dBHz; FSPL_down ~ 172.6 dB;
    #   C_rx = 12 - 172.6 + 10 - 3 ~ -154 dBW; N0_site = -228.6 + 26.0 = -202.6 dBW/Hz;
    #   N0_relay = -41.6 - 172.6 + 7 ~ -207 dBW/Hz; C/N0 ~ -154 + 201.3 ~ 47.5 dBHz.
    assert want["pfd_dbw_m2"] == pytest.approx(-150.0, abs=0.5)
    assert want["cn0_up_dbhz"] == pytest.approx(53.5, abs=0.5)
    assert want["transponder_eirp_dbw"] == pytest.approx(12.0, abs=0.5)
    assert want["c_rx_dbw"] == pytest.approx(-154.0, abs=0.5)
    assert want["site_n0_dbw_hz"] == pytest.approx(-202.58, abs=0.01)
    assert want["cn0_total_dbhz"] == pytest.approx(47.5, abs=0.5)
    assert want["off_emitter_deg"] == 0.0 and want["off_site_deg"] < 1e-6
    assert want["backoff_db"] > 10.0  # no power robbing
    # The total is the cascade of uplink and downlink C/N0 up to (f_e / f_uc)^2: EIRP_n0 is defined at the
    # transponder's uplink centre and this carrier sits 7.5 kHz below it (5e-5 dB).
    assert want["cn0_cascade_dbhz"] == pytest.approx(want["cn0_total_dbhz"], abs=1e-3)

    got = lb.path_budget(
        pacific, beam_emitter(), f_e, pacific.carrier_xyz("ref-guam"), "muos-io", "shared-1", "tokyo-io", T_EVAL
    )
    assert got is not None
    assert_budget_matches(got, want)
    assert float(got.snr_db(5.0e3)) == pytest.approx(want["cn0_total_dbhz"] - 10.0 * math.log10(5.0e3), abs=1e-8)


@pytest.mark.parametrize(
    "sat_id,channel_id",
    [
        ("uhf-172e", "naha-172e"),  # emitter beam far off axis; 1.2 m dish (electrically small) on its own satellite
        ("muos-pac", "tokyo-io"),  # co-channel satellite heard through the back of a beam aimed at muos-io
        ("muos-pac", "honolulu-pac"),  # across the antimeridian
        ("uhf-172e", "busan-pac"),  # 0.72 m dish aimed at another satellite
    ],
)
def test_pacific_full_budget_by_hand_off_axis(pacific: SystemConfig, pacific_raw: dict[str, Any], sat_id: str, channel_id: str) -> None:
    carrier, x_e, lla = guam(pacific_raw)
    f_e = carrier["uplink_center_hz"]
    want = hand_path_budget(pacific_raw, carrier["emitter"], x_e, lla, f_e, sat_id, "shared-1", channel_id, T_EVAL)
    assert want is not None
    assert want["off_emitter_deg"] > 30.0  # the uplink really is off the emitter's boresight
    got = lb.path_budget(
        pacific, beam_emitter(), f_e, pacific.carrier_xyz("ref-guam"), sat_id, "shared-1", channel_id, T_EVAL
    )
    assert got is not None
    assert_budget_matches(got, want)


def test_pacific_budget_with_power_robbing_by_hand(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    carrier, x_e, lla = guam(pacific_raw)
    f_e = carrier["uplink_center_hz"]
    others_db = (-134.0, -139.0)  # one above the -136 dBW/m^2 SFD: the transponder saturates
    want = hand_path_budget(
        pacific_raw, carrier["emitter"], x_e, lla, f_e, "muos-io", "shared-1", "naha-io", T_EVAL,
        other_pfd_w_m2=tuple(10.0 ** (p / 10.0) for p in others_db),
    )
    quiet = hand_path_budget(pacific_raw, carrier["emitter"], x_e, lla, f_e, "muos-io", "shared-1", "naha-io", T_EVAL)
    assert want is not None and quiet is not None
    assert want["backoff_db"] < -2.0
    assert want["transponder_eirp_dbw"] == pytest.approx(quiet["transponder_eirp_dbw"] + want["backoff_db"], abs=1e-9)
    emitter = beam_emitter()
    got = lb.path_budget(pacific, emitter, f_e, pacific.carrier_xyz("ref-guam"), "muos-io", "shared-1", "naha-io", T_EVAL, other_pfd_dbw_m2=others_db)
    assert got is not None
    assert_budget_matches(got, want)
    # the uplink C/N0 is untouched by robbing: carrier and transponder noise shrink together
    assert float(got.uplink.cn0_up_dbhz) == pytest.approx(quiet["cn0_up_dbhz"], abs=1e-8)
    assert float(got.cn0_total_dbhz) < quiet["cn0_total_dbhz"]


def test_pacific_paths_that_do_not_exist(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    carrier, x_e, lla = guam(pacific_raw)
    emitter = beam_emitter()
    x = pacific.carrier_xyz("ref-guam")
    f_e = carrier["uplink_center_hz"]
    # centre frequency outside the 25 kHz uplink passband of shared-1 (301.000 MHz +/- 12.5 kHz)
    assert lb.path_budget(pacific, emitter, 301.0130e6, x, "muos-io", "shared-1", "tokyo-io", T_EVAL) is None
    assert lb.path_budget(pacific, emitter, 301.0120e6, x, "muos-io", "shared-1", "tokyo-io", T_EVAL) is not None
    # muos-io (75 E) is about 40 degrees below Honolulu's horizon: no downlink into any Honolulu channel
    assert hand_path_budget(pacific_raw, carrier["emitter"], x_e, lla, f_e, "muos-io", "shared-1", "honolulu-pac", T_EVAL) is None
    assert lb.path_budget(pacific, emitter, f_e, x, "muos-io", "shared-1", "honolulu-pac", T_EVAL) is None
    assert lb.downlink_quality_db(pacific, "honolulu-pac", "muos-io", "shared-1", T_EVAL) is None
    assert lb.relay_rise_db(pacific, "honolulu-pac", "muos-io", "shared-1", T_EVAL) is None
    # ... and no uplink from Honolulu to it either
    honolulu = pacific.site_xyz("honolulu")
    assert lb.path_budget(pacific, emitter, f_e, honolulu, "muos-io", "shared-1", "tokyo-io", T_EVAL) is None
    with pytest.raises(ValueError, match="transponder_id"):
        lb.path_budget(pacific, emitter, f_e, x, "muos-io", "solo-pac", "tokyo-io", T_EVAL)
    with pytest.raises(ValueError, match="sat_id"):
        lb.path_budget(pacific, emitter, f_e, x, "nope", "shared-1", "tokyo-io", T_EVAL)
    with pytest.raises(ValueError, match="channel_id"):
        lb.path_budget(pacific, emitter, f_e, x, "muos-io", "shared-1", "nope", T_EVAL)
    with pytest.raises(ValueError, match="t_s"):
        lb.path_budget(pacific, emitter, f_e, x, "muos-io", "shared-1", "tokyo-io", np.array([T_EVAL, T_EVAL + 1.0]))


def test_pacific_budget_over_the_wrapped_region_grid(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    """A batch of emitter positions over the antimeridian-wrapping region against per-cell hand budgets."""
    lat, lon = pacific.processing.region.grid(7.5)
    assert lon[0] == 60.0 and lon[-1] == 240.0 and lat[0] == -60.0 and lat[-1] == 60.0
    cells = [(float(la), float(lo)) for la in lat for lo in lon]
    points = np.array([ecef(la, lo, 0.0) for la, lo in cells]).reshape(len(lat), len(lon), 3)
    emitter_raw = {"eirp_dbw": 20.0, "pointing": "omni"}
    emitter = SimpleNamespace(eirp_dbw=20.0, antenna=None, pointing="omni")
    f_e = 301.125e6
    got = lb.path_budget(pacific, emitter, f_e, points, "muos-pac", "shared-2", "hilo-pac", T_EVAL)
    assert got is not None and got.cn0_total_dbhz.shape == (len(lat), len(lon))
    visible = 0
    for k, (la, lo) in enumerate(cells):
        i, j = divmod(k, len(lon))
        want = hand_path_budget(pacific_raw, emitter_raw, points[i, j], (la, lo, 0.0), f_e, "muos-pac", "shared-2", "hilo-pac", T_EVAL)
        if want is None:  # muos-pac below this cell's horizon
            assert got.cn0_total_dbhz[i, j] == -np.inf and got.downlink.c_rx_dbw[i, j] == -np.inf
            assert got.uplink.pfd_dbw_m2[i, j] == -np.inf and got.uplink.elevation_deg[i, j] <= 0.0
            continue
        visible += 1
        assert got.uplink.elevation_deg[i, j] == pytest.approx(want["el_emitter_deg"], abs=1e-7)
        assert got.uplink.pfd_dbw_m2[i, j] == pytest.approx(want["pfd_dbw_m2"], abs=1e-8)
        assert got.downlink.c_rx_dbw[i, j] == pytest.approx(want["c_rx_dbw"], abs=1e-8)
        assert got.cn0_total_dbhz[i, j] == pytest.approx(want["cn0_total_dbhz"], abs=1e-8)
    # muos-pac at 177 W sees roughly 81 degrees of arc: most of the region east of 100 E, none of 60..95 E
    assert 0.5 * len(cells) < visible < 0.8 * len(cells)
    assert np.all(got.cn0_total_dbhz[:, lon <= 95.0] == -np.inf)
    assert np.all(np.isfinite(got.cn0_total_dbhz[:, (lon >= 150.0) & (lon <= 215.0)][np.abs(lat) <= 45.0]))


def test_pacific_downlink_quality_and_relay_rise_by_hand(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    """MATH.md section 7: q = G_site - FSPL - L_pol - 10 log10(N0_site + N0_relay), idle transponder."""
    values: dict[str, float] = {}
    for channel_id, sat_id, tp_id in [("seoul-pac", "muos-pac", "solo-pac"), ("naha-pac", "muos-pac", "solo-pac"), ("manila-172e", "uhf-172e", "shared-1"), ("tokyo-pac", "muos-io", "shared-2")]:
        satellite = by_id(pacific_raw["satellites"], sat_id)
        tp = by_id(satellite["transponders"], tp_id)
        site, channel = raw_channel(pacific_raw, channel_id)
        sat = geo_position(satellite["orbit"], T_EVAL)
        x_g = site_position(site)
        f_d = tp["uplink_center_hz"] + tp["translation_hz"]
        tracked = geo_position(by_id(pacific_raw["satellites"], channel["pointing"]["satellite_id"])["orbit"], T_EVAL)
        off = angle_deg(tracked - x_g, sat - x_g)
        path = 10.0 ** (antenna_gain_dbi(channel["antenna"], off, f_d) / 10.0) * (C / f_d / (4.0 * math.pi * float(np.linalg.norm(sat - x_g)))) ** 2
        path /= 10.0 ** (channel.get("polarization_loss_db", 0.0) / 10.0)
        n0_eirp = 10.0**2.6 / 10.0**-13.6 * 4.0 * math.pi * (tp["uplink_center_hz"] / C) ** 2 * K_LIN / 10.0**-1.4
        n0_site = K_LIN * 400.0
        q = db(path / (n0_site + n0_eirp * path))
        rise = db(1.0 + n0_eirp * path / n0_site)
        values[channel_id] = q
        assert lb.downlink_quality_db(pacific, channel_id, sat_id, tp_id, T_EVAL) == pytest.approx(q, abs=1e-8)
        assert lb.relay_rise_db(pacific, channel_id, sat_id, tp_id, T_EVAL) == pytest.approx(rise, abs=1e-8)
        assert rise > 0.0
    # capability rule: the same carrier through the same transponder differs between two channels by q_B - q_A
    emitter = SimpleNamespace(eirp_dbw=10.0, antenna=None, pointing="omni")
    x = ecef(20.0, 150.0, 0.0)
    a = lb.path_budget(pacific, emitter, 301.5e6, x, "muos-pac", "solo-pac", "seoul-pac", T_EVAL)
    b = lb.path_budget(pacific, emitter, 301.5e6, x, "muos-pac", "solo-pac", "naha-pac", T_EVAL)
    assert a is not None and b is not None
    assert float(b.cn0_total_dbhz) - float(a.cn0_total_dbhz) == pytest.approx(values["naha-pac"] - values["seoul-pac"], abs=1e-8)
    # through the back of the Tokyo dish (aimed at muos-pac) muos-io is weaker than on a dish aimed at it
    assert values["tokyo-pac"] < lb.downlink_quality_db(pacific, "naha-io", "muos-io", "shared-2", T_EVAL)


def test_ku_adjacent_satellite_budget_by_hand(ku: SystemConfig) -> None:
    """The Ku reference carrier into the neighbouring satellite, 2.5 degrees off its dish axis."""
    doc = raw("ku-adjacent")
    carrier = by_id(doc["carriers"], "ref-seoul")
    loc = carrier["location"]
    lla = (loc["lat_deg"], loc["lon_deg"], loc.get("alt_m", 0.0) + loc.get("antenna_height_m", 10.0))
    x_e = ecef(*lla)
    f_e = carrier["uplink_center_hz"]
    main = hand_path_budget(doc, carrier["emitter"], x_e, lla, f_e, "ku-b", "k1", "manila-ku-b", T_EVAL)
    side = hand_path_budget(doc, carrier["emitter"], x_e, lla, f_e, "ku-a", "k1", "tokyo-ku-a", T_EVAL)
    assert main is not None and side is not None
    # Seen from Seoul the neighbours are 2.7 to 2.9 degrees apart: on the 32 - 25 log10(theta) envelope,
    # 21.1 dBi against 42.8 dBi on axis, so about 21.7 dB down.
    assert 2.5 < side["off_emitter_deg"] < 3.0
    assert side["eirp_toward_sat_dbw"] == pytest.approx(50.0 - 42.84 + 32.0 - 25.0 * math.log10(side["off_emitter_deg"]), abs=0.01)
    assert main["eirp_toward_sat_dbw"] == pytest.approx(50.0, abs=1e-9)
    emitter = ku.carrier("ref-seoul").emitter
    x = ku.carrier_xyz("ref-seoul")
    assert_budget_matches(lb.path_budget(ku, emitter, f_e, x, "ku-b", "k1", "manila-ku-b", T_EVAL), main)
    assert_budget_matches(lb.path_budget(ku, emitter, f_e, x, "ku-a", "k1", "tokyo-ku-a", T_EVAL), side)
    # a 1.2 m dish aimed at ku-b hears ku-a through its side lobes as well
    cross = hand_path_budget(doc, carrier["emitter"], x_e, lla, f_e, "ku-a", "k1", "tokyo-ku-b", T_EVAL)
    assert cross is not None and 2.5 < cross["off_site_deg"] < 3.2
    assert_budget_matches(lb.path_budget(ku, emitter, f_e, x, "ku-a", "k1", "tokyo-ku-b", T_EVAL), cross)


def test_leo_budget_by_hand_through_the_fixed_zenith_channel(leo: SystemConfig) -> None:
    """Kepler orbit, fixed (non-tracking) boresight, isotropic 2 dBi antenna, 600 K, another band."""
    doc = raw("leo-example")
    t = T_REF + 120.0
    site = doc["sites"][0]
    x_g = site_position(site)
    lat_e, lon_e = 24.0, 126.0
    x_e = ecef(lat_e, lon_e, 0.0)
    f_e = 435.3e6  # centre of u2
    for sat_id in ("leo-a", "leo-b"):
        satellite = by_id(doc["satellites"], sat_id)
        tp = by_id(satellite["transponders"], "u2")
        sat = kepler_position(satellite["orbit"], t)
        el_e = elevation_deg(lat_e, lon_e, 0.0, sat)
        el_g = elevation_deg(site["lat_deg"], site["lon_deg"], 10.0, sat)
        assert el_e > 10.0 and el_g > 10.0
        rho_u, rho_d = float(np.linalg.norm(sat - x_e)), float(np.linalg.norm(sat - x_g))
        assert 800e3 < rho_u < 3000e3
        eirp_w = 10.0**0.7  # 7 dBW, omni
        pfd = eirp_w / (4.0 * math.pi * rho_u**2)
        sat_w, sfd, g_over_t = 10.0**0.3, 10.0**-10.5, 10.0**-2.5
        cn0_up = eirp_w * g_over_t * (C / f_e / (4.0 * math.pi * rho_u)) ** 2 / K_LIN
        n0_eirp = sat_w / sfd * 4.0 * math.pi * (tp["uplink_center_hz"] / C) ** 2 * K_LIN / g_over_t
        eirp_c = sat_w * pfd / sfd
        assert eirp_c + n0_eirp * 100e3 < sat_w  # linear
        f_d = f_e - 289.2e6
        path = 10.0**0.2 * (C / f_d / (4.0 * math.pi * rho_d)) ** 2  # 2 dBi whatever the direction
        n0_site = K_LIN * 600.0
        got = lb.path_budget(leo, SimpleNamespace(eirp_dbw=7.0, antenna=None, pointing="omni"), f_e, x_e, sat_id, "u2", "naha-leo", t)
        assert got is not None
        # the two Kepler implementations agree to about 0.1 m here, i.e. 1e-6 dB on a 1000 km path
        assert float(got.uplink.pfd_dbw_m2) == pytest.approx(db(pfd), abs=1e-4)
        assert float(got.uplink.cn0_up_dbhz) == pytest.approx(db(cn0_up), abs=1e-4)
        assert float(got.uplink.elevation_deg) == pytest.approx(el_e, abs=1e-4)
        assert float(got.transponder_eirp_dbw) == pytest.approx(db(eirp_c), abs=1e-4)
        assert float(got.downlink.c_rx_dbw) == pytest.approx(db(eirp_c * path), abs=1e-4)
        assert float(got.downlink.relayed_n0_dbw_hz) == pytest.approx(db(n0_eirp * path), abs=1e-4)
        assert float(got.downlink.site_n0_dbw_hz) == pytest.approx(-228.6 + 10.0 * math.log10(600.0), abs=1e-9)
        assert float(got.downlink.elevation_deg) == pytest.approx(el_g, abs=1e-4)
        # a zenith boresight: the off-axis angle is the zenith distance of the satellite
        assert float(got.downlink.off_axis_deg) == pytest.approx(90.0 - el_g, abs=1e-4)
        assert float(got.cn0_total_dbhz) == pytest.approx(db(eirp_c * path / (n0_site + n0_eirp * path)), abs=1e-4)
    # eight minutes later leo-b is still up but 30 minutes later both have set: no path
    assert lb.path_budget(leo, SimpleNamespace(eirp_dbw=7.0, antenna=None, pointing="omni"), f_e, x_e, "leo-a", "u2", "naha-leo", T_REF + 1800.0) is None
    assert lb.downlink_quality_db(leo, "naha-leo", "leo-b", "u1", T_REF + 1800.0) is None


def test_pacific_budget_for_an_emitter_with_a_fixed_azimuth_and_elevation(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    """An EmitterModel aimed by {az_deg, el_deg}: a 1.8 m dish (5.4 wavelengths) looking east-south-east."""
    from petal_locate.config import EmitterModel

    lla = (35.0, 135.0, 25.0)
    x_e = ecef(*lla)
    f_e = 301.75e6  # solo-172e
    # where is uhf-172e from here? (east-south-east, about 35 degrees up)
    east, north, up = enu(lla[0], lla[1])
    los = geo_position(by_id(pacific_raw["satellites"], "uhf-172e")["orbit"], T_EVAL) - x_e
    az_true = math.degrees(math.atan2(float(los @ east), float(los @ north))) % 360.0
    el_true = elevation_deg(*lla, x_e + los)
    assert 100.0 < az_true < 140.0 and 25.0 < el_true < 45.0
    for d_az, d_el in [(0.0, 0.0), (6.0, -3.0), (-20.0, 10.0), (180.0, 0.0)]:
        raw_emitter = {
            "eirp_dbw": 17.0,
            "antenna": {"type": "parabolic", "diameter_m": 5.4 * C / f_e, "efficiency": 0.5},
            "pointing": {"az_deg": az_true + d_az, "el_deg": el_true + d_el},
            "bandwidth_hz": 20e3,
        }
        want = hand_path_budget(pacific_raw, raw_emitter, x_e, lla, f_e, "uhf-172e", "solo-172e", "seoul-172e", T_EVAL)
        assert want is not None
        got = lb.path_budget(pacific, EmitterModel.model_validate(raw_emitter), f_e, x_e, "uhf-172e", "solo-172e", "seoul-172e", T_EVAL)
        assert got is not None
        assert_budget_matches(got, want)
        if d_az == 0.0:
            assert want["off_emitter_deg"] < 1e-6 and want["eirp_toward_sat_dbw"] == pytest.approx(17.0, abs=1e-9)
        if d_az == 6.0:  # 5.6 deg off a 12.96 deg beam: main lobe, 12 (5.6 / 12.96)^2 = 2.2 dB down
            assert want["eirp_toward_sat_dbw"] == pytest.approx(17.0 - 12.0 * (want["off_emitter_deg"] * 5.4 / 70.0) ** 2, abs=1e-9)
            assert 1.5 < 17.0 - want["eirp_toward_sat_dbw"] < 3.0
        if d_az == 180.0:  # behind the dish: -10 dBi against G0 = 21.6 dBi
            assert want["eirp_toward_sat_dbw"] == pytest.approx(17.0 - 10.0 - 10.0 * math.log10(0.5 * (5.4 * math.pi) ** 2), abs=1e-9)


def test_dut1_reaches_the_inertial_orbits_and_only_them() -> None:
    """UT1 = UTC + dut1_s: a later Earth angle turns a kepler orbit westward in ECEF; geo orbits are ECEF-defined."""
    doc = raw("leo-example")
    doc["satellites"].append({"id": "geo-x", "name": "X (illustrative)", "orbit": {"type": "geo", "lon_deg": 120.0, "inclination_deg": 2.0}, "transponders": []})
    doc["sites"][0]["channels"].append({"id": "naha-track", "antenna": {"type": "isotropic"}, "pointing": {"satellite_id": "leo-a"}})
    reference = loads_config(json.dumps(doc), fmt="json")
    doc["dut1_s"] = 0.4
    shifted = loads_config(json.dumps(doc), fmt="json")
    t = T_REF + 200.0
    turn = rot_z(-OMEGA_GMST * 0.4)
    for sat_id in ("leo-a", "leo-b"):
        r0, _ = reference.orbits()[sat_id].state_ecef(t)
        r1, _ = shifted.orbits()[sat_id].state_ecef(t)
        assert float(np.linalg.norm(r1 - turn @ r0)) < 1e-5
        assert float(np.linalg.norm(r1 - r0)) == pytest.approx(OMEGA_GMST * 0.4 * math.hypot(r0[0], r0[1]), rel=1e-6)  # ~ 150 m
        assert float(np.linalg.norm(r1 - r0)) > 100.0
    g0, _ = reference.orbits()["geo-x"].state_ecef(t)
    g1, _ = shifted.orbits()["geo-x"].state_ecef(t)
    assert float(np.linalg.norm(g1 - g0)) == 0.0
    # the boresight of a tracking channel follows the same (shifted) orbit
    los = turn @ reference.orbits()["leo-a"].state_ecef(t)[0] - site_position(doc["sites"][0])
    np.testing.assert_allclose(shifted.boresight_ecef("naha-track", t), los / np.linalg.norm(los), atol=1e-9)
    for bad in (1.5, -1.5):
        doc["dut1_s"] = bad
        expect_error(doc, "dut1_s")


def test_emitter_pointing_forms(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    carrier, x_e, lla = guam(pacific_raw)
    orbits = pacific.orbits()
    assert lb.emitter_boresight(SimpleNamespace(pointing="omni"), x_e, orbits, T_EVAL) is None
    tracked = lb.emitter_boresight(SimpleNamespace(pointing={"satellite_id": "uhf-172e"}), x_e, orbits, T_EVAL)
    want = geo_position(by_id(pacific_raw["satellites"], "uhf-172e")["orbit"], T_EVAL) - x_e
    np.testing.assert_allclose(tracked, want / np.linalg.norm(want), atol=1e-12)
    east, north, up = enu(lla[0], lla[1])
    fixed = lb.emitter_boresight(SimpleNamespace(pointing=SimpleNamespace(az_deg=135.0, el_deg=30.0)), x_e, orbits, T_EVAL)
    c = math.cos(math.radians(30.0))
    want_fixed = c * math.sin(math.radians(135.0)) * east + c * math.cos(math.radians(135.0)) * north + math.sin(math.radians(30.0)) * up
    np.testing.assert_allclose(fixed, want_fixed, atol=1e-9)
    with pytest.raises(ValueError, match="pointing"):
        lb.emitter_boresight(SimpleNamespace(pointing="sideways"), x_e, orbits, T_EVAL)
    with pytest.raises(ValueError, match="satellite_id"):
        lb.emitter_boresight(SimpleNamespace(pointing={"satellite_id": "nope"}), x_e, orbits, T_EVAL)


# =========================================================================== shipped files


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_config_loads_dumps_and_reloads(name: str, tmp_path: Path) -> None:
    path = CONFIG_DIR / f"{name}.yaml"
    cfg = load_config(path)
    doc = raw(name)
    assert cfg.name == name == doc["name"] and cfg.version == 1
    assert [s.id for s in cfg.satellites] == [s["id"] for s in doc["satellites"]]
    assert [s.id for s in cfg.sites] == [s["id"] for s in doc["sites"]]
    assert [c.id for c in cfg.carriers] == [c["id"] for c in doc.get("carriers", [])]
    assert "illustrative" in cfg.description.lower()
    assert all("illustrative" in s.notes.lower() for s in cfg.satellites)
    # YAML -> model -> YAML -> model, and the same document as JSON, give equal models
    text = dump_config(cfg)
    again = loads_config(text)
    assert again == cfg
    as_json = tmp_path / f"{name}.json"
    as_json.write_text(json.dumps(doc), encoding="utf-8")
    assert load_config(as_json) == cfg
    dumped = yaml.safe_load(text)
    for site in dumped["sites"]:
        for channel in site["channels"]:
            assert channel["monitors"] == []  # derived windows are not written back as if configured
    # numbers survive the round trip exactly
    for sat_doc, sat in zip(doc["satellites"], again.satellites):
        for tp_doc, tp in zip(sat_doc["transponders"], sat.transponders):
            for key, value in tp_doc.items():
                assert getattr(tp, key) == value
    schema = config_json_schema()
    assert {"satellites", "sites", "carriers", "processing", "dut1_s", "name", "version"} <= set(schema["properties"])
    assert set(schema["required"]) == {"name", "satellites", "sites"}
    assert schema.get("additionalProperties") is False


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_site_positions_are_phase_centres(name: str) -> None:
    cfg = load_config(CONFIG_DIR / f"{name}.yaml")
    doc = raw(name)
    positions = cfg.sites_xyz()
    assert list(positions) == [s["id"] for s in doc["sites"]]
    for site in doc["sites"]:
        want = ecef(site["lat_deg"], site["lon_deg"], site.get("alt_m", 0.0) + site.get("antenna_height_m", 10.0))
        np.testing.assert_allclose(cfg.site_xyz(site["id"]), want, atol=1e-6)
        np.testing.assert_allclose(positions[site["id"]], want, atol=1e-6)
        # ... which is antenna_height_m above the ground point, along the ellipsoid normal
        ground = ecef(site["lat_deg"], site["lon_deg"], site.get("alt_m", 0.0))
        lift = cfg.site_xyz(site["id"]) - ground
        assert float(np.linalg.norm(lift)) == pytest.approx(site.get("antenna_height_m", 10.0), abs=1e-6)
        assert float(lift @ enu(site["lat_deg"], site["lon_deg"])[2]) == pytest.approx(site.get("antenna_height_m", 10.0), abs=1e-6)
    for carrier in doc.get("carriers", []):
        loc = carrier["location"]
        want = ecef(loc["lat_deg"], loc["lon_deg"], loc.get("alt_m", 0.0) + loc.get("antenna_height_m", 10.0))
        np.testing.assert_allclose(cfg.carrier_xyz(carrier["id"]), want, atol=1e-6)


def test_pacific_file_matches_the_architecture_brief(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    names = {s.id: s.name for s in pacific.satellites}
    assert names == {
        "muos-pac": "MUOS Pacific (nominal 177 W)",
        "uhf-172e": "UFO-class legacy UHF (nominal 172 E)",
        "muos-io": "MUOS Indo-Asia (nominal 75 E)",
    }
    slots = {s["id"]: s["orbit"]["lon_deg"] for s in pacific_raw["satellites"]}
    assert slots == {"muos-pac": -177.0, "uhf-172e": 172.0, "muos-io": 75.0}
    for sat in pacific.satellites:
        assert sat.orbit.type == "geo" and 1.0 <= sat.orbit.inclination_deg <= 8.0
        translations = {tp.translation_hz for tp in sat.transponders}
        assert translations == ({-33.6e6} if sat.id == "uhf-172e" else {-41.0e6})
        for tp in sat.transponders:
            assert tp.bandwidth_hz == 25e3 and 299e6 < tp.uplink_center_hz < 303e6
            assert (tp.g_over_t_dbk, tp.sfd_dbw_m2, tp.eirp_sat_dbw) == (-14.0, -136.0, 26.0)
    assert [s.id for s in pacific.sites] == ["honolulu", "hilo", "tokyo", "naha", "seoul", "busan", "manila"]
    kinds = set()
    for site in pacific.sites:
        for channel in site.channels:
            antenna = channel.antenna
            kinds.add((antenna.type, getattr(antenna, "diameter_m", None), getattr(antenna, "gain_dbi", None)))
    assert kinds == {("parabolic", 0.72, None), ("parabolic", 1.2, None), ("beam", None, 10.0)}
    references = pacific.reference_carriers()
    assert [c.id for c in references] == ["ref-guam", "ref-sapporo"]
    for carrier in references:
        assert carrier.location is not None and carrier.emitter is not None
        # a reference is only useful when every satellite relays it: its transponder id exists on all three
        relaying = pacific.transponders_covering(carrier.uplink_lo_hz, carrier.uplink_hi_hz)
        assert {sat.id for sat, _ in relaying} == {"muos-pac", "uhf-172e", "muos-io"}
    region = pacific.processing.region
    assert (region.lat_min, region.lat_max, region.lon_min, region.lon_max) == (-60.0, 60.0, 60.0, -120.0)


def test_pacific_channels_match_visibility_over_a_sidereal_day(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    """One channel per satellite that is visible from the site (elevation from independent orbits)."""
    times = T_REF + np.linspace(0.0, SIDEREAL_DAY_S, 97)
    for site in pacific_raw["sites"]:
        h = site.get("alt_m", 0.0) + site.get("antenna_height_m", 10.0)
        watched = sorted(channel["pointing"]["satellite_id"] for channel in site["channels"])
        visible = []
        for satellite in pacific_raw["satellites"]:
            els = [elevation_deg(site["lat_deg"], site["lon_deg"], h, geo_position(satellite["orbit"], float(t))) for t in times]
            assert min(els) > 5.0 or max(els) < -30.0, (site["id"], satellite["id"])  # never marginal
            if min(els) > 5.0:
                visible.append(satellite["id"])
        assert watched == sorted(visible), site["id"]
        assert [c.id for _, c in pacific.channels_watching("muos-io") if c.id.startswith(site["id"])] == (
            [f"{site['id']}-io"] if "muos-io" in visible else []
        )
    hawaii = [s["id"] for s in pacific_raw["sites"] if s["lon_deg"] < 0.0]
    assert hawaii == ["honolulu", "hilo"]
    assert all(len(pacific.site(s).channels) == 2 for s in hawaii)
    # the reference emitters see all three satellites all day
    for carrier in pacific_raw["carriers"]:
        loc = carrier["location"]
        for satellite in pacific_raw["satellites"]:
            els = [elevation_deg(loc["lat_deg"], loc["lon_deg"], 10.0, geo_position(satellite["orbit"], float(t))) for t in times]
            assert min(els) > 3.0, (carrier["id"], satellite["id"], min(els))


def test_ku_file_matches_the_architecture_brief(ku: SystemConfig) -> None:
    doc = raw("ku-adjacent")
    slots = sorted(s["orbit"]["lon_deg"] for s in doc["satellites"])
    assert len(slots) == 3 and all(2.0 <= b - a <= 3.0 for a, b in zip(slots, slots[1:]))
    for sat in ku.satellites:
        for tp in sat.transponders:
            assert tp.bandwidth_hz == 36e6 and 13.75e9 <= tp.uplink_lo_hz and tp.uplink_hi_hz <= 14.5e9
            assert 11.7e9 <= tp.downlink_lo_hz and tp.downlink_hi_hz <= 12.2e9
            assert (tp.g_over_t_dbk, tp.sfd_dbw_m2, tp.eirp_sat_dbw) == (3.0, -85.0, 50.0)
    assert len(ku.sites) == 2 and len(ku.reference_carriers()) == 1
    diameters = {channel.antenna.diameter_m for site in ku.sites for channel in site.channels}
    assert diameters == {1.2, 2.4}
    assert ku.warnings() == []  # nothing electrically small at Ku band
    # each channel's radio samples 45 MS/s: one derived window covers the whole 36 MHz transponder,
    # inside one tuning (review findings ADAPT-9 and M8: the earlier 5 MHz radio could not serve
    # the two transponders' windows 45 MHz apart, and saw 14 % of each)
    for site in ku.sites:
        for channel in site.channels:
            (sat_id,) = channel.watched_satellite_ids
            assert channel.monitors_derived
            centres = sorted(m.center_hz for m in channel.monitors)
            assert centres == sorted(tp.uplink_center_hz + tp.translation_hz for tp in ku.satellite(sat_id).transponders)
            assert all(m.span_hz == 45.0e6 == channel.max_sample_rate_hz for m in channel.monitors)
    assert ku.warnings(1794571200.0) == []  # no coverage or tuning note either


def test_leo_file_claims_hold_with_an_independent_propagator(leo: SystemConfig) -> None:
    doc = raw("leo-example")
    assert [s["orbit"]["type"] for s in doc["satellites"]] == ["kepler", "kepler"]
    assert len(doc["sites"]) == 1
    plans = [[(tp["id"], tp["uplink_center_hz"], tp["bandwidth_hz"], tp["translation_hz"]) for tp in s["transponders"]] for s in doc["satellites"]]
    assert plans[0] == plans[1]
    site = doc["sites"][0]
    for satellite in doc["satellites"]:
        assert 160e3 < satellite["orbit"]["a_m"] - WGS84_A < 2000e3  # low Earth orbit
        els = [elevation_deg(site["lat_deg"], site["lon_deg"], 10.0, kepler_position(satellite["orbit"], T_REF + t)) for t in range(0, 481, 20)]
        assert min(els) > 0.0, (satellite["id"], min(els))  # "above the horizon ... for 8 minutes"
    # the numbers quoted in the file header: leo-a 27 -> 85 degrees, culminating near 180 s
    a = doc["satellites"][0]["orbit"]
    assert elevation_deg(site["lat_deg"], site["lon_deg"], 10.0, kepler_position(a, T_REF)) == pytest.approx(27.5, abs=0.5)
    assert elevation_deg(site["lat_deg"], site["lon_deg"], 10.0, kepler_position(a, T_REF + 180.0)) == pytest.approx(85.4, abs=0.5)
    # the fixed zenith channel: boresight is the local vertical at any time, as [M, 3] for [M] times
    _, _, up = enu(site["lat_deg"], site["lon_deg"])
    np.testing.assert_allclose(leo.boresight_ecef("naha-leo", T_REF + 5.0), up, atol=1e-12)
    many = leo.boresight_ecef("naha-leo", T_REF + np.arange(4.0))
    assert many.shape == (4, 3)
    np.testing.assert_allclose(many, np.tile(up, (4, 1)), atol=1e-12)
    (pair,) = leo.channels_watching("leo-b")
    assert (pair[0].id, pair[1].id) == ("naha", "naha-leo") and pair[1].watched_satellite_ids == ("leo-a", "leo-b")
    # the numeric orbits handed to the geometry code are these orbits (not swapped, right epoch, right frame)
    orbits = leo.orbits()
    assert list(orbits) == ["leo-a", "leo-b"]
    for satellite in doc["satellites"]:
        r, _ = orbits[satellite["id"]].state_ecef(T_REF + 240.0)
        assert float(np.linalg.norm(r - kepler_position(satellite["orbit"], T_REF + 240.0))) < 5.0


def test_horizon_warnings_agree_with_independent_elevations(leo: SystemConfig, pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    doc = raw("leo-example")
    site = doc["sites"][0]
    for t in (T_REF + 100.0, T_REF + 520.0, T_REF + 1800.0):
        below = [
            satellite["id"]
            for satellite in doc["satellites"]
            if elevation_deg(site["lat_deg"], site["lon_deg"], 10.0, kepler_position(satellite["orbit"], t)) <= 0.0
        ]
        notes = [note for note in leo.warnings(t) if note["loc"] == "sites.0.channels.0.pointing"]
        assert len(notes) == len(below)
        for sat_id, note in zip(below, notes):
            assert f"'{sat_id}'" in note["msg"] and "below the horizon" in note["msg"]
    assert [len([n for n in leo.warnings(T_REF + dt) if n["loc"].endswith("pointing")]) for dt in (100.0, 520.0, 1800.0)] == [0, 1, 2]
    assert leo.warnings() == []  # no time, no horizon statement; the 2 dBi antenna is not a dish
    # the Pacific file: every dish is under 3 wavelengths (3.4 m) at 260..268 MHz, and nothing is below the horizon
    dishes = [
        f"sites.{s}.channels.{c}.antenna"
        for s, site_doc in enumerate(pacific_raw["sites"])
        for c, channel in enumerate(site_doc["channels"])
        if channel["antenna"]["type"] == "parabolic" and channel["antenna"]["diameter_m"] < 3.0 * C / 268.2e6
    ]
    assert len(dishes) == 13
    assert [note["loc"] for note in pacific.warnings(T_EVAL)] == dishes


def test_documented_signatures_are_what_other_modules_call() -> None:
    """ARCHITECTURE.md section 5 (and the section 3 helper list), argument for argument."""
    import inspect

    def params(fn: Any) -> list[str]:
        return [name for name in inspect.signature(fn).parameters if name != "self"]

    assert params(ant.gain_dbi) == ["antenna_model", "off_axis_deg", "freq_hz"]
    assert params(ant.peak_gain_dbi) == ["antenna_model", "freq_hz"]
    assert params(ant.hpbw_deg) == ["antenna_model", "freq_hz"]
    assert params(ant.electrically_small) == ["antenna_model", "freq_hz"]
    assert params(iono.slant_factor) == ["el_deg"]
    assert params(iono.group_delay_s) == ["vtec_tecu", "el_deg", "freq_hz"]
    assert params(lb.fspl_db) == ["dist_m", "freq_hz"]
    assert params(lb.uplink_budget) == ["eirp_dbw", "antenna", "boresight", "x_e", "sat_r", "f_e_hz", "transponder"]
    assert params(lb.transponder_outputs) == ["pfd_dbw_m2", "transponder"]
    assert params(lb.downlink_budget) == ["eirp_down_dbw", "relayed_n0_eirp_dbw_hz", "f_d_hz", "sat_r", "site_xyz", "channel", "boresight"]
    assert params(lb.direct_budget) == ["eirp_dbw", "antenna", "boresight", "x_e", "h_e_m", "site_xyz", "h_g_m", "channel", "channel_boresight", "f_hz"]
    assert params(lb.path_budget) == ["config", "emitter", "f_e_hz", "x_e", "sat_id", "transponder_id", "channel_id", "t_s", "other_pfd_dbw_m2"]
    assert inspect.signature(lb.path_budget).parameters["other_pfd_dbw_m2"].default == ()
    assert params(lb.downlink_quality_db) == ["config", "channel_id", "sat_id", "transponder_id", "t_s"]
    fields = lambda cls: [f.name for f in __import__("dataclasses").fields(cls)]  # noqa: E731
    assert fields(lb.UplinkBudget) == ["eirp_toward_sat_dbw", "pfd_dbw_m2", "cn0_up_dbhz", "elevation_deg"]
    assert fields(lb.TransponderOutputs) == ["eirp_down_dbw", "relayed_n0_eirp_dbw_hz", "backoff_db"]
    assert fields(lb.DownlinkBudget) == ["c_rx_dbw", "relayed_n0_dbw_hz", "site_n0_dbw_hz", "elevation_deg", "off_axis_deg"]
    assert fields(lb.DirectBudget) == ["c_rx_dbw", "in_horizon"]
    assert fields(lb.PathBudget) == ["uplink", "transponder_eirp_dbw", "downlink", "cn0_total_dbhz"]
    assert params(lb.PathBudget.snr_db) == ["bandwidth_hz"]
    assert params(SystemConfig.satellite) == ["sat_id"] and params(SystemConfig.site) == ["site_id"]
    assert params(SystemConfig.channel) == ["channel_id"] and params(SystemConfig.site_xyz) == ["site_id"]
    assert params(SystemConfig.sites_xyz) == [] and params(SystemConfig.orbits) == [] and params(SystemConfig.resolved_uncertainty) == []
    assert params(SystemConfig.boresight_ecef) == ["channel_id", "t_s"]
    assert params(SystemConfig.channels_watching) == ["sat_id"]
    assert params(SystemConfig.transponders_covering) == ["f_lo_up", "f_hi_up"]
    assert params(SystemConfig.uplink_for) == ["sat_id", "f_down_hz"]
    assert params(SystemConfig.downlink_hz) == ["sat_id", "transponder_id", "f_up"]
    assert params(Region.contains) == ["lat_deg", "lon_deg"] and params(Region.grid) == ["step_deg"] and params(Region.lon_span_deg) == []
    assert params(load_config) == ["path"] and params(dump_config) == ["cfg"] and params(config_json_schema) == []
    # core/ reads configuration by attribute and never imports the config package
    for module in (ant, iono, lb):
        source = inspect.getsource(module)
        assert "petal_locate.config" not in source.replace("``petal_locate.config``", "") and "from ..config" not in source and "from petal_locate.config" not in source


# =========================================================================== wrapped Pacific region


def in_box(region: tuple[float, float, float, float], lat: float, lon: float) -> bool:
    """Independent membership test on the circle: walk eastward from lon_min."""
    lat_min, lat_max, lon_min, lon_max = region
    if not lat_min <= lat <= lat_max:
        return False
    a, b, x = lon_min % 360.0, lon_max % 360.0, lon % 360.0
    return a <= x <= b if a <= b else (x >= a or x <= b)


def test_pacific_region_wraps_the_antimeridian(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    region = pacific.processing.region
    assert region.lon_span_deg() == 180.0 and isinstance(region.lon_span_deg(), float)
    box = (-60.0, 60.0, 60.0, -120.0)
    # hand-picked points: (lat, lon, inside)
    cases = [
        (0.0, 60.0, True), (0.0, 59.99, False), (0.0, 179.99, True), (0.0, 180.0, True), (0.0, -180.0, True),
        (0.0, -179.99, True), (0.0, -120.0, True), (0.0, -119.99, False), (0.0, 0.0, False), (0.0, -90.0, False),
        (0.0, 240.0, True), (0.0, 240.01, False), (0.0, 420.0, True), (0.0, -300.0, True), (0.0, 419.9, False),
        (60.0, 150.0, True), (60.01, 150.0, False), (-60.0, -150.0, True), (-60.01, -150.0, False),
    ]
    for lat, lon, inside in cases:
        assert bool(region.contains(lat, lon)) is inside, (lat, lon)
        assert in_box(box, lat, lon) is inside, (lat, lon)
    rng = np.random.default_rng(99)
    lats = rng.uniform(-90.0, 90.0, 4000)
    lons = rng.uniform(-540.0, 540.0, 4000)
    got = region.contains(lats, lons)
    assert got.shape == (4000,) and got.dtype == bool
    want = np.array([in_box(box, float(la), float(lo)) for la, lo in zip(lats, lons)])
    np.testing.assert_array_equal(got, want)
    assert 0.2 < want.mean() < 0.5  # a third of the sphere's lat/lon rectangle
    # broadcasting: a latitude column against a longitude row
    table = region.contains(np.array([[-75.0], [0.0], [45.0]]), np.array([30.0, 100.0, -170.0, -100.0]))
    assert table.tolist() == [[False] * 4, [False, True, True, False], [False, True, True, False]]
    # every site and every reference emitter of the file is inside, on both sides of the antimeridian
    for site in pacific_raw["sites"]:
        assert bool(region.contains(site["lat_deg"], site["lon_deg"])), site["id"]
    for carrier in pacific_raw["carriers"]:
        assert bool(region.contains(carrier["location"]["lat_deg"], carrier["location"]["lon_deg"]))
    assert {s["lon_deg"] > 0 for s in pacific_raw["sites"]} == {True, False}


@pytest.mark.parametrize("step", [0.25, 1.0, 2.0, 7.0, 0.3])
def test_pacific_region_grid_is_unwrapped_and_increasing(pacific: SystemConfig, step: float) -> None:
    region = pacific.processing.region
    lat, lon = region.grid(step)
    # brute-force expectation: lon_min + k * step for every k that stays within the 180 degree span
    want_lon = [60.0 + k * step for k in range(100000) if k * step <= 180.0 + 1e-9]
    want_lat = [-60.0 + k * step for k in range(100000) if k * step <= 120.0 + 1e-9]
    np.testing.assert_allclose(lon, want_lon, rtol=0, atol=1e-9)
    np.testing.assert_allclose(lat, want_lat, rtol=0, atol=1e-9)
    assert lon[0] == 60.0 and lat[0] == -60.0
    assert np.all(np.diff(lon) > 0) and np.all(np.diff(lat) > 0)
    assert lon.max() > 180.0 and lon.max() <= 240.0 and lon.max() < 60.0 + 360.0  # unwrapped past the antimeridian
    assert lat.max() <= 60.0
    assert np.all(region.contains(lat[:, None], lon[None, :]))
    wrapped = (lon + 180.0) % 360.0 - 180.0
    assert np.all(region.contains(0.0, wrapped))  # the same meridians in [-180, 180) are inside too
    assert np.sum(np.diff(wrapped) < 0) == 1  # exactly one jump: this is why the grid is unwrapped


def test_region_rules_full_circle_and_invalid_boxes() -> None:
    full = Region()
    assert (full.lat_min, full.lat_max, full.lon_min, full.lon_max) == (-70.0, 70.0, -180.0, 180.0)
    assert full.lon_span_deg() == 360.0
    lat, lon = full.grid(5.0)
    assert lon[0] == -180.0 and lon[-1] == 175.0 and len(lon) == 72  # no meridian twice
    assert lat[0] == -70.0 and lat[-1] == 70.0 and len(lat) == 29
    assert np.all(full.contains(0.0, np.array([-180.0, 0.0, 179.999, 180.0, 359.0, -500.0])))
    assert not bool(full.contains(70.5, 0.0))
    east = Region(lat_min=0.0, lat_max=10.0, lon_min=170.0, lon_max=-170.0)
    assert east.lon_span_deg() == pytest.approx(20.0)
    np.testing.assert_allclose(east.grid(5.0)[1], [170.0, 175.0, 180.0, 185.0, 190.0])
    plain = Region(lat_min=0.0, lat_max=60.0, lon_min=90.0, lon_max=170.0)
    assert plain.lon_span_deg() == 80.0 and plain.grid(40.0)[1].tolist() == [90.0, 130.0, 170.0]
    assert bool(plain.contains(30.0, 170.0)) and not bool(plain.contains(30.0, 171.0)) and not bool(plain.contains(30.0, -170.0))
    half = Region(lon_min=-180.0, lon_max=0.0)  # touches -180 but is not the full circle
    assert half.lon_span_deg() == 180.0 and not bool(half.contains(0.0, 90.0)) and bool(half.contains(0.0, -90.0))
    for bad in [
        {"lon_min": 10.0, "lon_max": 10.0},
        {"lon_min": 180.0, "lon_max": -180.0},
        {"lon_min": -180.0, "lon_max": -180.0},
        {"lat_min": 20.0, "lat_max": 20.0},
        {"lat_min": 30.0, "lat_max": 20.0},
        {"lat_min": -91.0},
        {"lon_max": 181.0},
    ]:
        with pytest.raises(ValueError):
            Region(**bad)
    for bad_step in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="step_deg"):
            full.grid(bad_step)


# =========================================================================== helper methods


def test_pacific_boresight_tracks_the_satellite(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    for site in pacific_raw["sites"]:
        x_g = site_position(site)
        for channel in site["channels"]:
            orbit = by_id(pacific_raw["satellites"], channel["pointing"]["satellite_id"])["orbit"]
            for t in (T_REF, T_EVAL, T_REF + 61234.5):
                los = geo_position(orbit, t) - x_g
                got = pacific.boresight_ecef(channel["id"], t)
                assert got.shape == (3,)
                np.testing.assert_allclose(got, los / np.linalg.norm(los), atol=1e-12)
    # an inclined satellite moves: the boresight is recomputed, and vectorises over time
    times = T_REF + np.array([0.0, 21600.0, 43200.0])
    many = pacific.boresight_ecef("tokyo-io", times)
    assert many.shape == (3, 3)
    np.testing.assert_allclose(np.linalg.norm(many, axis=-1), 1.0, atol=1e-14)
    assert angle_deg(many[0], many[1]) > 1.0
    with pytest.raises(ValueError, match="channel_id"):
        pacific.boresight_ecef("nope", T_REF)


def test_pacific_frequency_plan_helpers(pacific: SystemConfig, pacific_raw: dict[str, Any]) -> None:
    plan = [
        (sat["id"], tp["id"], tp["uplink_center_hz"], tp["bandwidth_hz"], tp["translation_hz"])
        for sat in pacific_raw["satellites"]
        for tp in sat["transponders"]
    ]
    assert len(plan) == 9
    for sat_id, tp_id, f_up, bw, translation in plan:
        f_down = f_up + translation
        assert pacific.downlink_hz(sat_id, tp_id, f_up) == f_down
        for offset in (-0.5 * bw, -0.2 * bw, 0.0, 0.3 * bw, 0.5 * bw):
            found = pacific.uplink_for(sat_id, f_down + offset)
            assert found is not None and found[0].id == tp_id and found[1] == pytest.approx(f_up + offset, abs=1e-6)
        assert pacific.uplink_for(sat_id, f_down + 0.5 * bw + 1.0) is None
        assert pacific.uplink_for(sat_id, f_down - 0.5 * bw - 1.0) is None
    np.testing.assert_array_equal(pacific.downlink_hz("uhf-172e", "shared-2", np.array([301.12e6, 301.13e6])), [267.52e6, 267.53e6])
    # muos-pac and muos-io are co-channel on the downlink; uhf-172e is 7.4 MHz higher
    assert pacific.downlink_hz("muos-pac", "shared-1", 301.0e6) == pacific.downlink_hz("muos-io", "shared-1", 301.0e6) == 260.0e6
    assert pacific.downlink_hz("uhf-172e", "shared-1", 301.0e6) == 267.4e6
    # a 260 MHz downlink is not a uhf-172e frequency
    assert pacific.uplink_for("uhf-172e", 260.0e6) is None

    def overlapping(lo: float, hi: float) -> list[tuple[str, str]]:
        out = []
        for sat_id, tp_id, f_up, bw, _ in plan:
            p_lo, p_hi = f_up - 0.5 * bw, f_up + 0.5 * bw
            if (lo == hi and p_lo <= lo <= p_hi) or (lo < hi and lo < p_hi and hi > p_lo):
                out.append((sat_id, tp_id))
        return out

    rng = np.random.default_rng(5)
    ranges = [(301.0e6, 301.0e6), (300.9e6, 302.1e6), (301.4e6, 301.49e6), (301.49e6, 301.76e6), (305e6, 306e6)]
    for _ in range(300):
        lo = float(rng.uniform(300.9e6, 302.1e6))
        ranges.append((lo, lo + float(rng.choice([0.0, 2e3, 30e3, 200e3]))))
    non_empty = 0
    for lo, hi in ranges:
        got = [(sat.id, tp.id) for sat, tp in pacific.transponders_covering(lo, hi)]
        assert got == overlapping(lo, hi), (lo, hi)
        non_empty += bool(got)
    assert non_empty > 30
    assert [(s.id, t.id) for s, t in pacific.transponders_covering(301.5e6, 301.5e6)] == [("muos-pac", "solo-pac")]
    with pytest.raises(ValueError, match="f_hi_up"):
        pacific.transponders_covering(301.1e6, 301.0e6)
    with pytest.raises(ValueError, match="transponder_id"):
        pacific.downlink_hz("muos-pac", "solo-io", 301e6)
    for call in (lambda: pacific.satellite("x"), lambda: pacific.site("x"), lambda: pacific.channel("x"), lambda: pacific.channels_watching("x"), lambda: pacific.site_xyz("x"), lambda: pacific.uplink_for("x", 260e6)):
        with pytest.raises(ValueError, match="'x'"):
            call()
    site, channel = pacific.channel("naha-172e")
    assert site.id == "naha" and channel.id == "naha-172e" and pacific.satellite("muos-io").name.startswith("MUOS Indo-Asia")
    assert [c.id for _, c in pacific.channels_watching("uhf-172e")] == [f"{s['id']}-172e" for s in pacific_raw["sites"]]


def brute_force_frame(span_hz: float, frame_rate_hz: float = 2.0) -> tuple[int, int] | None:
    """The derivation rule, by trying every candidate in order of preference: 8 averages with
    the finest bins that fit (nfft 1024 down to 64), and only then fewer averages at nfft = 64.
    (ARCHITECTURE.md section 3 gives up averages first; the detection threshold is calibrated
    for navg >= 8, so the configuration keeps them as long as it can.)"""
    period = 1.0 / frame_rate_hz
    for nfft in (1024, 512, 256, 128, 64):
        if nfft * 8 / span_hz <= period:
            return nfft, 8
    for navg in range(7, 0, -1):
        if 64 * navg / span_hz <= period:
            return 64, navg
    return None


def test_monitor_derivation_rule_brute_force() -> None:
    rng = np.random.default_rng(11)
    spans = np.concatenate([10.0 ** rng.uniform(1.5, 7.0, 300), [128.0, 127.9, 1024.0, 1023.0, 2048.0, 2047.0, 16384.0, 16383.0, 4096.0, 31250.0, 5e6]])
    seen = set()
    for span in spans:
        want = brute_force_frame(float(span))
        if want is None:
            with pytest.raises(ValueError, match="span_hz"):
                fit_monitor_frame(float(span))
        else:
            assert fit_monitor_frame(float(span)) == want, span
        seen.add(want)
    assert {None, (1024, 8), (512, 8), (64, 8), (64, 7), (64, 1)} <= seen
    # every span of 1024 Hz and more keeps 8 averages at 2 frames per second
    assert all(want[1] == 8 for span, want in zip(spans, map(brute_force_frame, map(float, spans))) if span >= 1024.0)
    assert fit_monitor_frame(6250.0) == (256, 8)  # 1.25 * 5 kHz: 2048 samples in 0.328 s
    for rate in (0.5, 1.0, 4.0, 10.0, 40.0):
        for span in (300.0, 900.0, 5000.0, 2.0e5):
            want = brute_force_frame(span, rate)
            if want is None:
                with pytest.raises(ValueError, match="too narrow"):
                    fit_monitor_frame(span, frame_rate_hz=rate)
            else:
                assert fit_monitor_frame(span, frame_rate_hz=rate) == want, (span, rate)


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_monitors_follow_the_derivation_rule(name: str) -> None:
    cfg = load_config(CONFIG_DIR / f"{name}.yaml")
    doc = raw(name)
    for site_doc in doc["sites"]:
        for channel_doc in site_doc["channels"]:
            assert "monitors" not in channel_doc
            _, channel = cfg.channel(channel_doc["id"])
            assert channel.monitors_derived
            pointing = channel_doc["pointing"]
            watched = [pointing["satellite_id"]] if "satellite_id" in pointing else pointing["satellite_ids"]
            max_rate = channel_doc.get("max_sample_rate_hz", 2e6)
            want: dict[tuple[float, float], tuple[int, int]] = {}
            for sat_id in watched:
                for tp in by_id(doc["satellites"], sat_id)["transponders"]:
                    span = min(1.25 * tp["bandwidth_hz"], max_rate)
                    want.setdefault((tp["uplink_center_hz"] + tp["translation_hz"], span), brute_force_frame(span))
            got = {(m.center_hz, m.span_hz): (m.nfft, m.navg) for m in channel.monitors}
            assert got == want
            ids = [m.id for m in channel.monitors]
            assert len(set(ids)) == len(ids)
            for monitor in channel.monitors:
                assert monitor.frame_rate_hz == 2.0
                assert monitor.span_hz <= max_rate
                assert monitor.nfft * monitor.navg / monitor.span_hz <= 1.0 / monitor.frame_rate_hz


def test_resolved_uncertainty_sources(pacific: SystemConfig) -> None:
    unc = pacific.resolved_uncertainty()
    assert set(unc.sat_pos_ric_m) == set(unc.sat_vel_ric_mps) == set(unc.sat_osc_frac) == {"muos-pac", "uhf-172e", "muos-io"}
    for sat_id in unc.sat_pos_ric_m:  # geo orbits use the operator sigmas
        assert unc.sat_pos_ric_m[sat_id].tolist() == [100.0, 1000.0, 1000.0]
        assert unc.sat_vel_ric_mps[sat_id].tolist() == [0.02, 0.02, 0.05]
        assert unc.sat_osc_frac[sat_id] == 1e-6
    assert set(unc.site_clock_s) == set(unc.site_osc_frac) == {s.id for s in pacific.sites}
    assert all(v == pytest.approx(30e-9, rel=1e-12) for v in unc.site_clock_s.values())
    assert all(v == 1e-11 for v in unc.site_osc_frac.values())
    assert unc.tdoa_bias_s == pytest.approx(50e-9, rel=1e-12)

    doc = minimal()
    doc["satellites"].append(
        {"id": "s2", "name": "S2", "orbit": {"type": "tle", "line1": ISS_L1, "line2": ISS_L2}, "transponders": []}
    )
    doc["satellites"].append(
        {
            "id": "s3",
            "name": "S3",
            "orbit": {"type": "kepler", "a_m": 7.0e6, "e": 0.0, "inc_deg": 50.0, "raan_deg": 0.0, "argp_deg": 0.0, "mean_anomaly_deg": 0.0, "epoch": "2026-01-01T00:00:00Z"},
            "transponders": [],
            "ephemeris_sigma": {"pos_m": [1.0, 2.0, 3.0], "vel_mps": [0.1, 0.2, 0.3]},
        }
    )
    doc["sites"][0]["clock"] = {"sigma_ns": 250.0}
    doc["processing"] = {"uncertainty": {"sat_osc_frac": 2e-7, "site_osc_frac": 3e-12, "tdoa_bias_ns": 8.0, "ephemeris": {"tle": {"pos_m": [5.0, 6.0, 7.0], "vel_mps": [0.5, 0.6, 0.7]}}}}
    unc = loads_config(json.dumps(doc), fmt="json").resolved_uncertainty()
    assert unc.sat_pos_ric_m["s1"].tolist() == [100.0, 1000.0, 1000.0]  # geo -> operator defaults
    assert unc.sat_pos_ric_m["s2"].tolist() == [5.0, 6.0, 7.0] and unc.sat_vel_ric_mps["s2"].tolist() == [0.5, 0.6, 0.7]  # tle
    assert unc.sat_pos_ric_m["s3"].tolist() == [1.0, 2.0, 3.0] and unc.sat_vel_ric_mps["s3"].tolist() == [0.1, 0.2, 0.3]  # override
    assert unc.site_clock_s == {"g1": pytest.approx(250e-9, rel=1e-12)}
    assert unc.sat_osc_frac == {"s1": 2e-7, "s2": 2e-7, "s3": 2e-7} and unc.site_osc_frac == {"g1": 3e-12}
    assert unc.tdoa_bias_s == pytest.approx(8e-9, rel=1e-12)
    # the default tle sigmas, when nothing is configured
    del doc["processing"]
    unc = loads_config(json.dumps(doc), fmt="json").resolved_uncertainty()
    assert unc.sat_pos_ric_m["s2"].tolist() == [2000.0, 20000.0, 10000.0] and unc.sat_vel_ric_mps["s2"].tolist() == [0.3, 0.3, 0.7]


# =========================================================================== validation


def tle_checksum(line: str) -> int:
    return sum(int(ch) if ch.isdigit() else (1 if ch == "-" else 0) for ch in line[:68]) % 10


def test_tle_validation_against_the_published_checksum_rule() -> None:
    assert tle_checksum(ISS_L1) == 7 == int(ISS_L1[68]) and tle_checksum(ISS_L2) == 7 == int(ISS_L2[68])
    doc = minimal()
    doc["satellites"][0]["orbit"] = {"type": "tle", "line1": ISS_L1 + "  ", "line2": ISS_L2}  # trailing blanks are harmless
    cfg = loads_config(json.dumps(doc), fmt="json")
    assert cfg.satellites[0].orbit.line1 == ISS_L1
    # the element set survives a YAML round trip character for character (runs of blanks included)
    assert loads_config(dump_config(cfg)).satellites[0].orbit.line1 == ISS_L1
    # every single-digit corruption of the payload must be caught
    for position in (20, 30, 40, 55):
        for line_key, line in (("line1", ISS_L1), ("line2", ISS_L2)):
            if not line[position].isdigit():
                continue
            corrupted = line[:position] + str((int(line[position]) + 1) % 10) + line[position + 1 :]
            bad = copy.deepcopy(doc)
            bad["satellites"][0]["orbit"][line_key] = corrupted
            expect_error(bad, f"satellites.0.orbit.{line_key}", "checksum")
    swapped = copy.deepcopy(doc)
    swapped["satellites"][0]["orbit"] = {"type": "tle", "line1": ISS_L2, "line2": ISS_L1}
    expect_error(swapped, "satellites.0.orbit.line1")
    short = copy.deepcopy(doc)
    short["satellites"][0]["orbit"]["line2"] = ISS_L2[:-1]
    expect_error(short, "satellites.0.orbit.line2", "69")


def test_table_orbit_validation_and_time_precision() -> None:
    orbit = {"type": "geo", "lon_deg": 100.0, "inclination_deg": 3.0, "phase_deg": 15.0}
    step = 60.0

    def sample(k: int, stamp: str) -> dict[str, Any]:
        t = T_REF + k * step
        velocity = (geo_position(orbit, t + 0.5) - geo_position(orbit, t - 0.5)) / 1.0
        return {"t": stamp, "r_m": geo_position(orbit, t).tolist(), "v_mps": velocity.tolist()}

    stamps = ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", "2026-01-01T00:02:00Z", "2026-01-01T00:03:00Z"]
    doc = minimal()
    doc["satellites"][0]["orbit"] = {"type": "table", "samples": [sample(k, s) for k, s in enumerate(stamps)]}
    cfg = loads_config(json.dumps(doc), fmt="json")
    assert cfg.resolved_uncertainty().sat_pos_ric_m["s1"].tolist() == [100.0, 1000.0, 1000.0]  # table -> operator
    r, _ = cfg.orbits()["s1"].state_ecef(T_REF + 90.0)  # ECEF samples are used as they are
    assert float(np.linalg.norm(r - geo_position(orbit, T_REF + 90.0))) < 1e-3
    # local-time stamps are converted, sub-second stamps keep every digit
    shifted = copy.deepcopy(doc)
    shifted["satellites"][0]["orbit"]["samples"][0]["t"] = "2026-01-01T09:00:00+09:00"
    shifted["satellites"][0]["orbit"]["samples"][1]["t"] = "2026-01-01T00:01:00.000000001Z"
    loaded = loads_config(json.dumps(shifted), fmt="json")
    assert [s.t for s in loaded.satellites[0].orbit.samples][:2] == ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00.000000001Z"]
    assert yaml.safe_load(dump_config(loaded))["satellites"][0]["orbit"]["samples"][1]["t"] == "2026-01-01T00:01:00.000000001Z"
    for bad_stamps in (
        ["2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z", "2026-01-01T00:01:00Z", "2026-01-01T00:03:00Z"],  # out of order
        ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", "2026-01-01T00:01:00Z", "2026-01-01T00:03:00Z"],  # repeated
    ):
        bad = copy.deepcopy(doc)
        for entry, stamp in zip(bad["satellites"][0]["orbit"]["samples"], bad_stamps):
            entry["t"] = stamp
        expect_error(bad, "satellites.0.orbit", "strictly increasing")
    one = copy.deepcopy(doc)
    one["satellites"][0]["orbit"]["samples"] = one["satellites"][0]["orbit"]["samples"][:1]
    expect_error(one, "satellites.0.orbit.samples")
    not_a_time = copy.deepcopy(doc)
    not_a_time["satellites"][0]["orbit"]["samples"][2]["t"] = "noon"
    expect_error(not_a_time, "satellites.0.orbit.samples.2.t")


def mutate(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    doc = minimal()
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    if value is ...:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


@pytest.mark.parametrize(
    "path,value,fragments",
    [
        (("satellites", 0, "transponders", 0, "bandwidth_hz"), 0.0, ("satellites.0.transponders.0.bandwidth_hz",)),
        (("satellites", 0, "transponders", 0, "bandwidth_hz"), -25e3, ("satellites.0.transponders.0.bandwidth_hz",)),
        (("satellites", 0, "transponders", 0, "uplink_center_hz"), 0.0, ("satellites.0.transponders.0.uplink_center_hz",)),
        (("satellites", 0, "transponders", 0, "translation_hz"), -300.0e6, ("satellites.0.transponders.0", "translation_hz")),
        (("satellites", 0, "transponders", 0, "group_delay_s"), -1e-6, ("satellites.0.transponders.0.group_delay_s",)),
        (("satellites", 0, "transponders", 0, "gain_db"), 3.0, ("satellites.0.transponders.0.gain_db",)),
        (("satellites", 0, "orbit", "lon_deg"), 181.0, ("satellites.0.orbit.lon_deg",)),
        (("satellites", 0, "orbit", "type"), "molniya", ("satellites.0.orbit",)),
        (("satellites", 0, "orbit", "epoch"), "yesterday", ("satellites.0.orbit.epoch",)),
        (("sites", 0, "lat_deg"), 90.5, ("sites.0.lat_deg",)),
        (("sites", 0, "lat_deg"), -90.5, ("sites.0.lat_deg",)),
        (("sites", 0, "lon_deg"), -180.5, ("sites.0.lon_deg",)),
        (("sites", 0, "lon_deg"), float("nan"), ("sites.0.lon_deg",)),
        (("sites", 0, "antenna_height_m"), -1.0, ("sites.0.antenna_height_m",)),
        (("sites", 0, "clock"), {"sigma_ns": -1.0}, ("sites.0.clock.sigma_ns",)),
        (("sites", 0, "channels", 0, "pointing"), {"satellite_id": "s9"}, ("sites.0.channels.0.pointing.satellite_id", "s9")),
        (("sites", 0, "channels", 0, "pointing"), {"az_deg": 10.0, "el_deg": 20.0, "satellite_ids": []}, ("sites.0.channels.0.pointing.satellite_ids",)),
        (("sites", 0, "channels", 0, "pointing"), {"az_deg": 10.0, "el_deg": 20.0, "satellite_ids": ["s1", "s9"]}, ("sites.0.channels.0.pointing.satellite_ids.1", "s9")),
        (("sites", 0, "channels", 0, "pointing"), {"az_deg": 10.0, "el_deg": 20.0}, ("sites.0.channels.0.pointing", "satellite_ids")),
        (("sites", 0, "channels", 0, "antenna"), {"type": "parabolic"}, ("sites.0.channels.0.antenna.diameter_m",)),
        (("sites", 0, "channels", 0, "antenna"), {"type": "parabolic", "diameter_m": 0.0}, ("sites.0.channels.0.antenna.diameter_m",)),
        (("sites", 0, "channels", 0, "antenna"), {"type": "table", "theta_deg": [0.0, 10.0], "gain_dbi": [3.0]}, ("sites.0.channels.0.antenna", "gain_dbi")),
        (("sites", 0, "channels", 0, "system_noise_temp_k"), 0.0, ("sites.0.channels.0.system_noise_temp_k",)),
        (("sites", 0, "channels", 0, "monitors"), [{"id": "m", "center_hz": 260e6, "span_hz": 2.5e6}], ("sites.0.channels.0", "'m'", "max_sample_rate_hz")),
        (("sites", 0, "channels", 0, "monitors"), [{"id": "m", "center_hz": 260e6, "span_hz": 16000.0}], ("sites.0.channels.0.monitors.0", "'m'", "frame")),
        (("sites", 0, "channels", 0, "monitors"), [{"id": "m", "center_hz": 260e6, "span_hz": 50e3}, {"id": "m", "center_hz": 261e6, "span_hz": 50e3}], ("sites.0.channels.0", "'m'")),
        (("processing",), {"region": {"lon_min": 20.0, "lon_max": 20.0}}, ("processing.region",)),
        (("processing",), {"region": {"lat_min": 5.0, "lat_max": -5.0}}, ("processing.region",)),
        (("processing",), {"solver": {"grid_step_deg": 0.0}}, ("processing.solver.grid_step_deg",)),
        (("processing",), {"detection": {"confirm_m": 6, "confirm_n": 5}}, ("processing.detection", "confirm_m")),
        (("processing",), {"cafe": {}}, ("processing.cafe",)),
        (("name",), ..., ("name",)),
        (("satellites",), ..., ("satellites",)),
        (("colour",), "blue", ("colour",)),
    ],
)
def test_invalid_configurations_are_rejected_naming_the_field(path: tuple[Any, ...], value: Any, fragments: tuple[str, ...]) -> None:
    doc = mutate(path, value)
    if isinstance(value, float) and math.isnan(value):
        with pytest.raises(ValueError) as info:  # NaN is not JSON; go through the model directly
            SystemConfig.model_validate(doc)
        assert "lon_deg" in str(info.value)
        return
    expect_error(doc, *fragments)


def test_monitor_frame_boundary_is_inclusive() -> None:
    # 8192 samples at 16384 Hz last exactly the 0.5 s frame period: allowed; one hertz less is not.
    ok = mutate(("sites", 0, "channels", 0, "monitors"), [{"id": "m", "center_hz": 260e6, "span_hz": 16384.0}])
    channel = loads_config(json.dumps(ok), fmt="json").channel("c1")[1]
    assert not channel.monitors_derived and [(m.id, m.nfft, m.navg) for m in channel.monitors] == [("m", 1024, 8)]
    expect_error(mutate(("sites", 0, "channels", 0, "monitors"), [{"id": "m", "center_hz": 260e6, "span_hz": 16383.0}]), "'m'")
    at_rate = mutate(("sites", 0, "channels", 0, "monitors"), [{"id": "m", "center_hz": 260e6, "span_hz": 2.0e6}])
    assert loads_config(json.dumps(at_rate), fmt="json").channel("c1")[1].monitors[0].span_hz == 2.0e6  # span == max rate is allowed
    explicit = loads_config(json.dumps(ok), fmt="json")
    assert yaml.safe_load(dump_config(explicit))["sites"][0]["channels"][0]["monitors"][0]["id"] == "m"  # explicit monitors are written
    assert loads_config(dump_config(explicit)) == explicit


def test_configured_monitors_that_miss_a_watched_transponder_are_reported() -> None:
    """Derived monitors follow the frequency plan; configured ones do not. A plan edited after
    the monitors were written down (the API serialises derived monitors like configured ones)
    must not go unnoticed."""
    doc = minimal()
    cfg = loads_config(json.dumps(doc), fmt="json")
    assert cfg.warnings() == []
    # what model_dump (and so any plain JSON serialisation) hands out: the derived window, spelled out
    served = cfg.model_dump(mode="json", exclude_none=True)
    assert served["sites"][0]["channels"][0]["monitors"][0]["center_hz"] == 260.0e6
    unchanged = SystemConfig.model_validate(served)
    assert unchanged.warnings() == []  # the window still sits on the transponder
    # the operator moves the transponder by 1 MHz and sends the document back
    served["satellites"][0]["transponders"][0]["uplink_center_hz"] = 301.0e6
    moved = SystemConfig.model_validate(served)
    (window,) = moved.channel("c1")[1].monitors
    assert window.center_hz == 260.0e6 and not moved.channel("c1")[1].monitors_derived  # stale, as configured
    (note,) = moved.warnings()
    assert note["loc"] == "sites.0.channels.0.monitors"
    assert "'t1'" in note["msg"] and "'s1'" in note["msg"] and "260980000" in note["msg"] and "261020000" in note["msg"]
    # the export that drops derived monitors does not have the problem: they are derived again
    exported = cfg.export_dict()
    exported["satellites"][0]["transponders"][0]["uplink_center_hz"] = 301.0e6
    again = SystemConfig.model_validate(exported)
    assert again.channel("c1")[1].monitors[0].center_hz == 261.0e6 and again.warnings() == []
    # a window that overlaps the passband only partly is reported with the covered share and the
    # blind band (review finding ADAPT-5: 86 % of every Ku transponder was blind without a word)
    partial = minimal()
    partial["sites"][0]["channels"][0]["monitors"] = [{"id": "m", "center_hz": 260.03e6, "span_hz": 40e3}]
    (note,) = loads_config(json.dumps(partial), fmt="json").warnings()
    assert note["loc"] == "sites.0.channels.0.monitors"
    assert "cover 25 %" in note["msg"] and "259.980 to 260.010 MHz" in note["msg"]
    edge = minimal()
    edge["sites"][0]["channels"][0]["monitors"] = [{"id": "m", "center_hz": 260.04e6, "span_hz": 40e3}]  # touches at 260.02 MHz
    assert [n["loc"] for n in loads_config(json.dumps(edge), fmt="json").warnings()] == ["sites.0.channels.0.monitors"]


def test_ids_and_cross_references() -> None:
    doc = minimal()
    doc["satellites"].append(copy.deepcopy(doc["satellites"][0]))
    expect_error(doc, "satellites.1.id", "'s1'")
    doc = minimal()
    doc["sites"].append(copy.deepcopy(doc["sites"][0]))
    doc["sites"][1]["id"] = "g2"  # the site id differs, its channel id does not: channel ids are global
    expect_error(doc, "sites.1.channels.0.id", "'c1'")
    doc = minimal()
    doc["satellites"][0]["transponders"].append(copy.deepcopy(doc["satellites"][0]["transponders"][0]))
    expect_error(doc, "satellites.0.transponders.1.id", "'t1'")
    # the same transponder id on two satellites is fine (ids are scoped to the satellite)
    doc = minimal()
    second = copy.deepcopy(doc["satellites"][0])
    second["id"] = "s2"
    doc["satellites"].append(second)
    cfg = loads_config(json.dumps(doc), fmt="json")
    assert [(s.id, t.id) for s, t in cfg.transponders_covering(300.0e6, 300.0e6)] == [("s1", "t1"), ("s2", "t1")]

    carrier = {"id": "k1", "name": "K1", "satellite_id": "s1", "transponder_id": "t1", "uplink_center_hz": 300.0e6, "bandwidth_hz": 5e3}
    doc = minimal()
    doc["carriers"] = [carrier, dict(carrier)]
    expect_error(doc, "carriers.1.id", "'k1'")
    for key, value, fragments in [
        ("satellite_id", "s9", ("carriers.0.satellite_id", "s9")),
        ("transponder_id", "t9", ("carriers.0.transponder_id", "t9")),
        ("uplink_center_hz", 300.0205e6, ("carriers.0.uplink_center_hz",)),  # centre outside the 40 kHz passband
        ("bandwidth_hz", 0.0, ("carriers.0.bandwidth_hz",)),
        ("use_as_reference", True, ("carriers.0", "use_as_reference")),  # needs location and emitter
    ]:
        doc = minimal()
        doc["carriers"] = [dict(carrier, **{key: value})]
        expect_error(doc, *fragments)
    doc = minimal()
    doc["carriers"] = [dict(carrier, use_as_reference=True, location={"lat_deg": 1.0, "lon_deg": 2.0})]
    expect_error(doc, "carriers.0", "emitter")
    doc["carriers"][0]["emitter"] = {"eirp_dbw": 10.0, "bandwidth_hz": 4e3, "pointing": {"satellite_id": "s9"}}
    expect_error(doc, "carriers.0.emitter.pointing.satellite_id", "s9")
    doc["carriers"][0]["emitter"]["pointing"] = {"az_deg": 100.0, "el_deg": 45.0}
    cfg = loads_config(json.dumps(doc), fmt="json")
    reference = cfg.reference_carriers()[0]
    assert reference.emitter.waveform == "noise" and reference.emitter.antenna.type == "isotropic"
    np.testing.assert_allclose(cfg.carrier_xyz("k1"), ecef(1.0, 2.0, 10.0), atol=1e-6)  # antenna_height_m defaults to 10 m
    assert loads_config(dump_config(cfg)) == cfg
    assert yaml.safe_load(dump_config(cfg))["carriers"][0]["emitter"]["pointing"] == {"az_deg": 100.0, "el_deg": 45.0}


def test_defaults_of_the_architecture_document() -> None:
    cfg = loads_config(json.dumps(minimal()), fmt="json")
    assert (cfg.version, cfg.description, cfg.dut1_s, cfg.carriers) == (1, "", 0.0, [])
    site = cfg.site("g1")
    assert (site.alt_m, site.antenna_height_m, site.clock.sigma_ns) == (0.0, 10.0, 30.0)
    channel = site.channels[0]
    assert (channel.name, channel.system_noise_temp_k, channel.polarization_loss_db, channel.max_sample_rate_hz) == ("", 400.0, 0.0, 2e6)
    assert channel.antenna.gain_dbi == 0.0
    orbit = cfg.satellite("s1").orbit
    assert (orbit.inclination_deg, orbit.epoch, orbit.phase_deg, orbit.drift_deg_per_day) == (0.0, "2026-01-01T00:00:00Z", 0.0, 0.0)
    assert cfg.satellite("s1").transponders[0].group_delay_s == 0.0 and cfg.satellite("s1").notes == ""
    p = cfg.processing
    assert p.detection.model_dump() == {
        "threshold_db": 6.0, "confirm_m": 3, "confirm_n": 5, "release_frames": 6, "merge_bins": 2, "baseline_tau_s": 600.0,
        "power_anomaly_db": 6.0, "min_site_separation_km": 200.0, "verdict_settle_s": 5.0, "sensitivity_margin_db": 6.0,
    }
    assert p.capture.model_dump() == {
        "duration_s": 1.0, "lead_s": 0.6, "interval_s": 5.0, "oversample": 1.25, "min_sample_rate_hz": 20e3, "max_samples": 262144, "timeout_s": 15.0,
    }
    assert p.caf.model_dump() == {"min_snr_db": 13.0, "max_tdoa_sigma_s": 5e-4, "doppler_margin_hz": 20.0, "delay_margin_s": 20e-6}
    assert p.solver.model_dump() == {
        "height_m": 0.0, "min_elevation_deg": 0.0, "confidence": 0.95, "grid_step_deg": 1.0, "n_starts": 16, "chi2_ambiguity": 9.0, "max_measurements": 400, "max_reduced_chi2": 5.0,
    }
    assert p.uncertainty.model_dump() == {
        "ephemeris": {
            "operator": {"pos_m": [100.0, 1000.0, 1000.0], "vel_mps": [0.02, 0.02, 0.05]},
            "tle": {"pos_m": [2000.0, 20000.0, 10000.0], "vel_mps": [0.3, 0.3, 0.7]},
        },
        "sat_osc_frac": 1e-6, "site_osc_frac": 1e-11, "tdoa_bias_ns": 50.0,
        "iono_vtec_tecu": 30.0, "iono_correlation": 0.5, "tropo_zenith_m": 2.4, "tropo_correlation": 0.9,
    }
    assert p.calibration.model_dump() == {
        "enabled": True, "interval_s": 30.0, "max_age_s": 300.0, "ephemeris_allowance_max_scale": 20.0
    }
    assert p.region.model_dump() == {"lat_min": -70.0, "lat_max": 70.0, "lon_min": -180.0, "lon_max": 180.0}
    (monitor,) = channel.monitors  # 1.25 * 40 kHz = 50 kHz centred on the 260 MHz downlink
    assert (monitor.center_hz, monitor.span_hz, monitor.nfft, monitor.navg, monitor.frame_rate_hz) == (260.0e6, 50e3, 1024, 8, 2.0)


def test_yaml_specific_hazards() -> None:
    text = (CONFIG_DIR / "pacific-uhf.yaml").read_text(encoding="utf-8")
    reference = loads_config(text)
    # a key written twice must not silently drop the first value
    with pytest.raises(ValueError, match="duplicate"):
        loads_config(text + "\nname: again\n")
    # unquoted YAML timestamps (parsed by YAML itself, with and without an offset) mean the same instant
    for stamp in ("2026-01-01T00:00:00Z", "2026-01-01 00:00:00", "2026-01-01T09:00:00+09:00", "2026-01-01"):
        variant = loads_config(text.replace('epoch: "2026-01-01T00:00:00Z"', f"epoch: {stamp}"))
        assert variant == reference, stamp
    moved = loads_config(text.replace('epoch: "2026-01-01T00:00:00Z"', "epoch: 2026-01-01T06:00:00+09:00"))
    assert moved.satellite("muos-pac").orbit.epoch == "2025-12-31T21:00:00Z"
    # "301.000e6" (no sign in the exponent) is a string in YAML 1.1: it must still become the number
    assert loads_config(text.replace("301.000e+6", "301.000e6")) == reference
    for broken in ("", "- 1\n- 2\n", "name: [unclosed\n", "just a string"):
        with pytest.raises(ConfigError):
            loads_config(broken)
    with pytest.raises(ConfigError, match="JSON"):
        loads_config("{not json", fmt="json")
    with pytest.raises(ConfigError, match="duplicate"):
        loads_config('{"name": "a", "name": "b", "satellites": [], "sites": []}', fmt="json")


def test_load_config_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing.yaml"):
        load_config(tmp_path / "missing.yaml")
    other = tmp_path / "config.toml"
    other.write_text("name = 'x'", encoding="utf-8")
    with pytest.raises(ConfigError, match="extension"):
        load_config(other)
    bad = tmp_path / "bad.yml"
    doc = minimal()
    doc["sites"][0]["lat_deg"] = 123.0
    doc["sites"][0]["channels"][0]["pointing"] = {"satellite_id": "zz"}
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(bad)
    assert "bad.yml" in str(info.value) and "sites.0.lat_deg" in str(info.value)
    assert {"loc": "sites.0.lat_deg"}.items() <= info.value.errors[0].items()
    with pytest.raises(ValueError, match="SystemConfig"):
        dump_config({"name": "x"})  # type: ignore[arg-type]


# =========================================================================== worked budget, literal numbers


def test_pacific_worked_budget_in_decibels() -> None:
    """The test beam emitter at ref-guam (BEAM_EMITTER) -> muos-io shared-1 -> tokyo-io at 2026-01-01T03:00:00Z,
    worked out once in decibels.

    The numbers below came from a separate scratch derivation (Friis in dB, the geo orbit
    written with explicit trigonometry, no project code):

        slant ranges       rho_u = 40 474 373.40 m (el 11.046 deg), rho_d = 40 424 450.62 m (el 11.496 deg)
        FSPL               up 174.1625 dB at 300.9925 MHz, down 172.8799 dB at 259.9925 MHz
        PFD                13 - 10 log10(4 pi rho_u^2) = -150.1357 dBW/m^2
        C/N0 up            13 - 174.1625 - 14 + 228.6 = 53.4375 dBHz
        EIRP_c             26 - (-136 + 150.1357) = 11.8643 dBW          (back-off 13.6696 dB: linear)
        EIRP_n0            26 + 136 + 11.0272 + 14 - 228.6 = -41.5730 dBW/Hz
        C_rx               11.8643 - 172.8799 + 10 - 3 = -154.0156 dBW   (beam on axis, 3 dB polarisation)
        N0_relay           -41.5730 - 172.8799 + 10 - 3 = -207.4529 dBW/Hz
        N0_site            -228.6 + 10 log10(400) = -202.5794 dBW/Hz
        C/N0               -154.0156 - 10 log10(10^-20.25794 + 10^-20.74529) = 47.3398 dBHz
    """
    cfg = load_config(CONFIG_DIR / "pacific-uhf.yaml")
    got = lb.path_budget(cfg, beam_emitter(), 300.9925e6, cfg.carrier_xyz("ref-guam"), "muos-io", "shared-1", "tokyo-io", T_EVAL)
    assert got is not None
    assert float(got.uplink.elevation_deg) == pytest.approx(11.046084, abs=1e-5)
    assert float(got.uplink.eirp_toward_sat_dbw) == pytest.approx(13.0, abs=1e-9)
    assert float(got.uplink.pfd_dbw_m2) == pytest.approx(-150.135701, abs=1e-5)
    assert float(got.uplink.cn0_up_dbhz) == pytest.approx(53.437501, abs=1e-5)
    assert float(got.transponder_eirp_dbw) == pytest.approx(11.864299, abs=1e-5)
    assert lb.transponder_noise_eirp_dbw_hz(cfg.satellite("muos-io").transponder("shared-1")) == pytest.approx(-41.572986, abs=1e-5)
    assert lb.transponder_outputs([float(got.uplink.pfd_dbw_m2)], cfg.satellite("muos-io").transponder("shared-1")).backoff_db == pytest.approx(13.669598, abs=1e-5)
    assert float(got.downlink.elevation_deg) == pytest.approx(11.496384, abs=1e-5)
    assert float(got.downlink.off_axis_deg) == pytest.approx(0.0, abs=1e-6)
    assert float(got.downlink.c_rx_dbw) == pytest.approx(-154.015583, abs=1e-5)
    assert float(got.downlink.relayed_n0_dbw_hz) == pytest.approx(-207.452868, abs=1e-5)
    assert float(got.downlink.site_n0_dbw_hz) == pytest.approx(-202.579400, abs=1e-5)
    assert float(got.cn0_total_dbhz) == pytest.approx(47.339768, abs=1e-5)


# =========================================================================== monitors the spectrum tracker can use


def detection_base(transponder_widths_hz: list[float]) -> dict[str, Any]:
    """``minimal()`` with one 300 MHz-band transponder per requested width, 1 MHz apart."""
    doc = minimal()
    template = doc["satellites"][0]["transponders"][0]
    doc["satellites"][0]["transponders"] = [
        dict(template, id=f"t{k}", uplink_center_hz=300.0e6 + 1.0e6 * k, bandwidth_hz=width) for k, width in enumerate(transponder_widths_hz)
    ]
    return doc


def test_the_six_db_threshold_needs_eight_averages() -> None:
    """Why derived monitors keep navg = 8: a Welch bin of white noise averaged over navg
    independent segments is Gamma(navg, 1/navg). The chance that it lies more than 6 dB above
    its median (detection.threshold_db over a median baseline, MATH.md section 7):"""
    from scipy import stats

    def exceedance(navg: int) -> float:
        bin_power = stats.gamma(navg, scale=1.0 / navg)
        return float(bin_power.sf(10.0**0.6 * bin_power.median()))

    # navg = 1 in closed form: an exponential bin, median ln 2, exceeds 10^0.6 ln 2 with probability 2^(-10^0.6)
    assert exceedance(1) == pytest.approx(2.0 ** (-(10.0**0.6)), rel=1e-9)
    assert exceedance(1) == pytest.approx(0.0633, abs=1e-4)  # 6 % of the bins of every frame
    assert exceedance(2) > 5e-3 and exceedance(4) > 1e-4  # 20 and 0.6 noise bins per second in a 1024-bin window at 2 Hz
    assert exceedance(8) < 1e-6  # 3.5e-7: a few single-bin crossings an hour, which M-of-N confirmation removes
    # every monitor the shipped configurations derive has the 8 averages
    for name in SHIPPED:
        cfg = load_config(CONFIG_DIR / f"{name}.yaml")
        monitors = [m for site in cfg.sites for channel in site.channels for m in channel.monitors]
        assert monitors and all(m.navg == 8 and m.nfft == 1024 for m in monitors), name
        assert not [n for n in cfg.warnings() if "navg" in n["msg"]]


def test_narrow_transponders_keep_eight_averages_and_the_rest_is_flagged() -> None:
    # 1.25 x width = span: 31.25 kHz, 6.25 kHz, 1.25 kHz, 1024 Hz, 625 Hz, 160 Hz
    widths = [25e3, 5e3, 1e3, 819.2, 500.0, 128.0]
    cfg = loads_config(json.dumps(detection_base(widths)), fmt="json")
    channel = cfg.channel("c1")[1]
    assert channel.monitors_derived
    got = {m.id: (m.span_hz, m.nfft, m.navg) for m in channel.monitors}
    want = {f"s1:t{k}": (1.25 * w, *brute_force_frame(1.25 * w)) for k, w in enumerate(widths)}
    assert got == want
    assert [got[f"s1:t{k}"][1:] for k in range(6)] == [(1024, 8), (256, 8), (64, 8), (64, 8), (64, 4), (64, 1)]
    for m in channel.monitors:  # each frame fits its period, by the literal inequality
        assert m.nfft * m.navg / m.span_hz <= 1.0 / m.frame_rate_hz
    flagged = [n for n in cfg.warnings() if n["loc"] == "sites.0.channels.0.monitors" and "navg" in n["msg"]]
    assert len(flagged) == 2
    assert "'s1:t4'" in flagged[0]["msg"] and "navg = 4" in flagged[0]["msg"]
    assert "'s1:t5'" in flagged[1]["msg"] and "navg = 1" in flagged[1]["msg"]
    # a configured window with too few averages is flagged the same way (it stays valid)
    doc = minimal()
    doc["sites"][0]["channels"][0]["monitors"] = [{"id": "m", "center_hz": 260.0e6, "span_hz": 50e3, "navg": 4}]
    (note,) = loads_config(json.dumps(doc), fmt="json").warnings()
    assert note["loc"] == "sites.0.channels.0.monitors" and "'m'" in note["msg"] and "navg = 4" in note["msg"]


def test_baseline_must_span_ten_frames_of_every_monitor() -> None:
    from petal_locate.core.detect import SignalTracker

    def tracker_accepts(cfg: SystemConfig) -> bool:
        for site in cfg.sites:
            for channel in site.channels:
                for m in channel.monitors:
                    try:
                        SignalTracker(m.center_hz, m.span_hz, m.nfft, m.frame_rate_hz, cfg.processing.detection, [], [])
                    except ValueError:
                        return False
        return True

    # a configured monitor at one frame a minute against the default 600 s baseline: 10 frames, accepted
    doc = minimal()
    doc["sites"][0]["channels"][0]["monitors"] = [{"id": "slow", "center_hz": 260.0e6, "span_hz": 50e3, "frame_rate_hz": 1.0 / 60.0}]
    assert tracker_accepts(loads_config(json.dumps(doc), fmt="json"))
    # 1 / 61 Hz: 600 s span 9.84 frames; the tracker would raise, so the configuration does, at the field
    doc["sites"][0]["channels"][0]["monitors"][0]["frame_rate_hz"] = 1.0 / 61.0
    with pytest.raises(ValueError):
        SignalTracker(260.0e6, 50e3, 1024, 1.0 / 61.0, SimpleNamespace(**{**minimal_detection(), "baseline_tau_s": 600.0}), [], [])
    error = expect_error(doc, "sites.0.channels.0.monitors.0.frame_rate_hz", "'slow'", "baseline_tau_s")
    assert [e["loc"] for e in error.errors] == ["sites.0.channels.0.monitors.0.frame_rate_hz"]
    # ... and a longer baseline cures it
    doc["processing"] = {"detection": {"baseline_tau_s": 610.0}}
    assert tracker_accepts(loads_config(json.dumps(doc), fmt="json"))
    # derived monitors run at 2 frames per second: a baseline under 5 s is reported once, at the setting
    doc = minimal()
    doc["sites"][0]["channels"].append({"id": "c2", "antenna": {"type": "isotropic"}, "pointing": {"satellite_id": "s1"}})
    doc["processing"] = {"detection": {"baseline_tau_s": 4.9}}
    error = expect_error(doc, "processing.detection.baseline_tau_s", "frame periods")
    assert [e["loc"] for e in error.errors] == ["processing.detection.baseline_tau_s"]
    doc["processing"] = {"detection": {"baseline_tau_s": 5.0}}  # exactly 10 frames
    assert tracker_accepts(loads_config(json.dumps(doc), fmt="json"))
    for name in SHIPPED:
        assert tracker_accepts(load_config(CONFIG_DIR / f"{name}.yaml")), name


def minimal_detection() -> dict[str, Any]:
    return SystemConfig.model_validate(minimal()).processing.detection.model_dump()


def test_monitor_nfft_floor_is_the_trackers() -> None:
    from petal_locate.core.detect import SignalTracker

    cfg_detection = SystemConfig.model_validate(minimal()).processing.detection
    with pytest.raises(ValueError, match="nfft"):
        SignalTracker(260.0e6, 50e3, 15, 2.0, cfg_detection, [], [])
    doc = minimal()
    doc["sites"][0]["channels"][0]["monitors"] = [{"id": "m", "center_hz": 260.0e6, "span_hz": 50e3, "nfft": 15}]
    expect_error(doc, "sites.0.channels.0.monitors.0.nfft")
    doc["sites"][0]["channels"][0]["monitors"][0]["nfft"] = 16
    (m,) = loads_config(json.dumps(doc), fmt="json").channel("c1")[1].monitors
    assert m.nfft == 16
    SignalTracker(m.center_hz, m.span_hz, m.nfft, m.frame_rate_hz, cfg_detection, [], [])


# =========================================================================== electrically small dishes


def test_every_small_dish_is_flagged_at_its_operating_frequency() -> None:
    """D < 3 wavelengths, wavelength = C / f, at the lowest frequency the antenna works at."""
    doc = minimal()  # s1: one transponder, 300 MHz up, 260 MHz down
    template = doc["satellites"][0]["transponders"][0]
    doc["satellites"].append(
        {"id": "s2", "name": "S2", "orbit": {"type": "geo", "lon_deg": 105.0}, "transponders": [dict(template, translation_hz=-60.0e6)]}  # 240 MHz down
    )
    doc["satellites"].append(
        {"id": "s3", "name": "S3", "orbit": {"type": "geo", "lon_deg": 95.0}, "transponders": [dict(template, uplink_center_hz=14.25e9, bandwidth_hz=36e6, translation_hz=-2.3e9)]}
    )
    dish = lambda d: {"type": "parabolic", "diameter_m": d}  # noqa: E731
    doc["sites"][0]["channels"] = [
        {"id": "a", "antenna": dish(1.2), "pointing": {"satellite_id": "s1"}},  # 1.04 wavelengths at 260 MHz
        {"id": "b", "antenna": dish(3.5), "pointing": {"az_deg": 180.0, "el_deg": 60.0, "satellite_ids": ["s1", "s2"]}},  # 3.04 at 260, 2.80 at 240 MHz
        {"id": "c", "antenna": dish(3.5), "pointing": {"satellite_id": "s1"}},  # 3.04 wavelengths at 260 MHz: focuses
        {"id": "d", "antenna": dish(0.6), "pointing": {"satellite_id": "s3"}},  # 23.9 wavelengths at 11.95 GHz
        {"id": "e", "antenna": dish(3.6), "pointing": {"satellite_id": "s1"}, "monitors": [{"id": "low", "center_hz": 249.0e6, "span_hz": 50e3}]},  # 2.99 at 249 MHz
        {"id": "f", "antenna": {"type": "beam", "gain_dbi": 3.0, "hpbw_deg": 120.0}, "pointing": {"satellite_id": "s1"}},
    ]
    carrier = {"id": "k", "name": "K", "satellite_id": "s1", "transponder_id": "t1", "uplink_center_hz": 300.0e6, "bandwidth_hz": 5e3}
    doc["carriers"] = [
        dict(carrier, id="k-small", emitter={"eirp_dbw": 10.0, "bandwidth_hz": 5e3, "antenna": dish(2.0), "pointing": {"satellite_id": "s1"}}),  # 2.00 wavelengths
        dict(carrier, id="k-big", emitter={"eirp_dbw": 10.0, "bandwidth_hz": 5e3, "antenna": dish(3.1), "pointing": {"satellite_id": "s1"}}),  # 3.10 wavelengths
        dict(carrier, id="k-plain"),
    ]
    cfg = loads_config(json.dumps(doc), fmt="json")

    # the independent expectation, straight from the document
    def lowest_frequency(channel: dict[str, Any]) -> float:
        pointing = channel["pointing"]
        watched = [pointing["satellite_id"]] if "satellite_id" in pointing else pointing["satellite_ids"]
        freqs = [tp["uplink_center_hz"] + tp["translation_hz"] for s in watched for tp in by_id(doc["satellites"], s)["transponders"]]
        return min(freqs + [m["center_hz"] for m in channel.get("monitors", [])])

    want = [
        f"carriers.{k}.emitter.antenna"
        for k, c in enumerate(doc["carriers"])
        if "emitter" in c and c["emitter"]["antenna"]["type"] == "parabolic" and c["emitter"]["antenna"]["diameter_m"] * c["uplink_center_hz"] / C < 3.0
    ] + [
        f"sites.0.channels.{k}.antenna"
        for k, ch in enumerate(doc["sites"][0]["channels"])
        if ch["antenna"]["type"] == "parabolic" and ch["antenna"]["diameter_m"] * lowest_frequency(ch) / C < 3.0
    ]
    assert want == ["carriers.0.emitter.antenna", "sites.0.channels.0.antenna", "sites.0.channels.1.antenna", "sites.0.channels.4.antenna"]
    notes = [n for n in cfg.warnings() if n["loc"].endswith(".antenna")]
    assert [n["loc"] for n in notes] == want  # one note per antenna, a channel watching two satellites included
    assert "240.000 MHz" in notes[2]["msg"] and "249.000 MHz" in notes[3]["msg"] and "300.000 MHz" in notes[0]["msg"]
    # the Ku file: nothing small, the reference emitter's 1.2 m dish included (57 wavelengths at 14.236 GHz)
    assert not [n for n in load_config(CONFIG_DIR / "ku-adjacent.yaml").warnings() if n["loc"].endswith(".antenna")]


# =========================================================================== ephemeris tables


def table_doc(n_samples: int, step_s: float = 60.0) -> tuple[dict[str, Any], dict[str, Any]]:
    """``minimal()`` with its geo satellite replaced by an ECEF table sampled from the same orbit."""
    from petal_locate.core.timebase import iso_from_ns

    doc = minimal()
    orbit = {"type": "geo", "lon_deg": 100.0, "inclination_deg": 3.0, "phase_deg": 15.0}
    samples = []
    for k in range(n_samples):
        t = T_REF + k * step_s
        velocity = (geo_position(orbit, t + 0.5) - geo_position(orbit, t - 0.5)) / 1.0
        samples.append({"t": iso_from_ns(int(round(t)) * 10**9), "r_m": geo_position(orbit, t).tolist(), "v_mps": velocity.tolist()})
    doc["satellites"][0]["orbit"] = {"type": "table", "samples": samples}
    return doc, orbit


def test_table_orbits_are_built_once_and_follow_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    import petal_locate.config.models as models

    doc, orbit = table_doc(1440)
    cfg = loads_config(json.dumps(doc), fmt="json")
    builds = []
    real = models.orbit_from_config
    monkeypatch.setattr(models, "orbit_from_config", lambda model, dut1_s=0.0: builds.append(model.type) or real(model, dut1_s))
    emitter = SimpleNamespace(eirp_dbw=10.0, antenna=None, pointing="omni")
    t = T_REF + 3 * 3600.0
    first = cfg.orbits()["s1"]
    for _ in range(5):  # what a link budget asks for, again and again
        assert lb.path_budget(cfg, emitter, 300.0e6, ecef(5.0, 105.0, 0.0), "s1", "t1", "c1", t) is not None
        assert lb.downlink_quality_db(cfg, "c1", "s1", "t1", t) is not None
    assert cfg.orbits()["s1"] is first and cfg.orbit("s1") is first
    assert builds == []  # the table was unpacked once, at validation
    # the orbit is the table: at a sample time the Hermite interpolant is the sample itself
    np.testing.assert_allclose(first.state_ecef(T_REF + 600.0)[0], geo_position(orbit, T_REF + 600.0), atol=1e-6)
    # an edit made in place after validation is not missed
    cfg.satellites[0].orbit.samples[10].r_m[2] += 5000.0
    moved = cfg.orbits()["s1"]
    assert builds == ["table"] and moved is not first
    np.testing.assert_allclose(moved.state_ecef(T_REF + 600.0)[0] - geo_position(orbit, T_REF + 600.0), [0.0, 0.0, 5000.0], atol=1e-6)
    # model equality ignores the kept orbit; a copy with new samples gets its own
    a = loads_config(json.dumps(doc), fmt="json")
    b = loads_config(json.dumps(doc), fmt="json")
    a.orbits()
    assert a == b and a.model_copy(deep=True) == b
    shorter = b.satellites[0].orbit.model_copy(update={"samples": b.satellites[0].orbit.samples[:100]})
    with pytest.raises(ValueError, match="outside the ephemeris table span"):
        shorter.numeric_orbit().state_ecef(T_REF + 200 * 60.0)
    # the budget through the table equals the budget through the analytic orbit it samples (Hermite error ~ mm)
    analytic = loads_config(json.dumps(minimal() | {"satellites": [dict(minimal()["satellites"][0], orbit=orbit)]}), fmt="json")
    via_table = lb.path_budget(b, emitter, 300.0e6, ecef(5.0, 105.0, 0.0), "s1", "t1", "c1", t + 17.3)
    via_orbit = lb.path_budget(analytic, emitter, 300.0e6, ecef(5.0, 105.0, 0.0), "s1", "t1", "c1", t + 17.3)
    assert float(via_table.cn0_total_dbhz) == pytest.approx(float(via_orbit.cn0_total_dbhz), abs=1e-6)


def test_warnings_survive_a_time_outside_an_ephemeris_table() -> None:
    doc, _ = table_doc(5)  # four minutes of ephemeris
    cfg = loads_config(json.dumps(doc), fmt="json")
    assert cfg.warnings(T_REF + 120.0) == []
    (note,) = cfg.warnings(T_REF + 3600.0)
    assert note["loc"] == "satellites.0.orbit" and "'s1'" in note["msg"] and "table span" in note["msg"]
    for bad in (float("nan"), (T_REF + 120.0) * 1e9):  # not a time; nanoseconds by mistake
        with pytest.raises(ValueError, match="t_s"):
            cfg.warnings(bad)
