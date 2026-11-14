"""core/iono.py against MATH.md section 3: check value, closed forms and ray geometry."""
from __future__ import annotations

import math

import numpy as np
import pytest

from petal_locate.core import iono
from petal_locate.core.constants import C, IONO_SHELL_HEIGHT_M, R_E


def pierce_slant(el_deg: float) -> float:
    """Obliquity from explicit ray geometry, independent of the closed form under test.

    A ray leaves the surface point (0, 0, R_E) at elevation ``el_deg`` and is followed
    to the sphere of radius R_E + h; the factor is 1 / cos(zenith angle of the ray at
    that pierce point).
    """
    el = math.radians(el_deg)
    p = np.array([0.0, 0.0, R_E])
    d = np.array([math.cos(el), 0.0, math.sin(el)])
    shell = R_E + IONO_SHELL_HEIGHT_M
    # |p + s d|^2 = shell^2  ->  s^2 + 2 (p.d) s + (R_E^2 - shell^2) = 0, positive root
    b = float(p @ d)
    s = -b + math.sqrt(b * b - (R_E * R_E - shell * shell))
    q = p + s * d
    assert abs(np.linalg.norm(q) - shell) < 1e-6
    cos_zenith = float(q @ d) / shell
    return 1.0 / cos_zenith


def test_check_value_30_tecu_305_mhz_zenith_is_434_ns() -> None:
    delay = iono.group_delay_s(30.0, 90.0, 305e6)
    assert delay == pytest.approx(434e-9, abs=1e-9)
    # written out: 40.3 * 30e16 / (305e6^2 * c)
    assert delay == pytest.approx(40.3 * 3.0e17 / (9.3025e16 * 299792458.0), rel=1e-14)
    assert isinstance(delay, float)


def test_slant_factor_limits() -> None:
    assert iono.slant_factor(90.0) == pytest.approx(1.0, abs=1e-15)
    shell = R_E + IONO_SHELL_HEIGHT_M
    horizon = shell / math.sqrt(shell * shell - R_E * R_E)
    assert iono.slant_factor(0.0) == pytest.approx(horizon, rel=1e-14)
    assert horizon == pytest.approx(3.14, abs=0.01)


@pytest.mark.parametrize("el_deg", [0.0, 1.0, 5.0, 10.0, 23.5, 45.0, 60.0, 80.0, 89.0, 90.0])
def test_slant_factor_matches_ray_shell_geometry(el_deg: float) -> None:
    assert iono.slant_factor(el_deg) == pytest.approx(pierce_slant(el_deg), rel=1e-11)


def test_slant_factor_decreases_with_elevation_and_vectorises() -> None:
    el = np.linspace(0.0, 90.0, 181)
    slant = iono.slant_factor(el)
    assert slant.shape == el.shape
    assert np.all(np.diff(slant) < 0.0)
    assert np.all(slant >= 1.0)
    grid = iono.slant_factor(el.reshape(1, -1).repeat(3, axis=0))
    assert grid.shape == (3, 181) and np.array_equal(grid[1], slant)


def test_group_delay_scaling_and_broadcast() -> None:
    base = iono.group_delay_s(30.0, 40.0, 305e6)
    assert iono.group_delay_s(60.0, 40.0, 305e6) == pytest.approx(2.0 * base, rel=1e-14)
    assert iono.group_delay_s(30.0, 40.0, 610e6) == pytest.approx(base / 4.0, rel=1e-14)
    assert iono.group_delay_s(0.0, 40.0, 305e6) == 0.0
    # an explicit product with the independently derived obliquity
    assert base == pytest.approx(40.3 * 30e16 * pierce_slant(40.0) / (305e6**2 * C), rel=1e-11)

    vtec = np.array([5.0, 30.0, 80.0])[:, None, None]
    el = np.array([10.0, 45.0, 90.0, 20.0])[None, :, None]
    freq = np.array([260e6, 301e6])[None, None, :]
    out = iono.group_delay_s(vtec, el, freq)
    assert out.shape == (3, 4, 2)
    for i, j, k in np.ndindex(out.shape):
        expected = iono.group_delay_s(float(vtec[i, 0, 0]), float(el[0, j, 0]), float(freq[0, 0, k]))
        assert out[i, j, k] == pytest.approx(expected, rel=1e-14)


def test_uplink_and_downlink_legs_of_a_uhf_path() -> None:
    """Uplink at f_e seen from the emitter, downlink at f_e + T seen from the site."""
    up = iono.group_delay_s(30.0, 35.0, 301.0e6)
    down = iono.group_delay_s(30.0, 35.0, 260.0e6)
    assert down / up == pytest.approx((301.0 / 260.0) ** 2, rel=1e-13)
    assert 0.5e-6 < up < 1.0e-6  # hundreds of nanoseconds at UHF: kilometres of TDOA error


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"vtec_tecu": 30.0, "el_deg": -0.1, "freq_hz": 3e8}, "el_deg"),
        ({"vtec_tecu": 30.0, "el_deg": 90.5, "freq_hz": 3e8}, "el_deg"),
        ({"vtec_tecu": 30.0, "el_deg": float("nan"), "freq_hz": 3e8}, "el_deg"),
        ({"vtec_tecu": -1.0, "el_deg": 45.0, "freq_hz": 3e8}, "vtec_tecu"),
        ({"vtec_tecu": float("inf"), "el_deg": 45.0, "freq_hz": 3e8}, "vtec_tecu"),
        ({"vtec_tecu": 30.0, "el_deg": 45.0, "freq_hz": 0.0}, "freq_hz"),
        ({"vtec_tecu": 30.0, "el_deg": 45.0, "freq_hz": [3e8, -1.0]}, "freq_hz"),
    ],
)
def test_invalid_input_names_the_field(kwargs: dict, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        iono.group_delay_s(**kwargs)


def test_slant_factor_rejects_below_horizon() -> None:
    with pytest.raises(ValueError, match="el_deg"):
        iono.slant_factor(np.array([10.0, -5.0]))
