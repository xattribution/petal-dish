"""core/antennas.py against ARCHITECTURE.md section 5: hand numbers, -3 dB widths found
numerically, envelope and floor values, and brute-force broadcasting."""
from __future__ import annotations

import math
from types import SimpleNamespace as NS
from typing import Any

import numpy as np
import pytest
from scipy.optimize import brentq

from petal_locate.core import antennas as ant
from petal_locate.core.constants import C


def dish(diameter_m: float, efficiency: float = 0.55) -> NS:
    return NS(type="parabolic", diameter_m=diameter_m, efficiency=efficiency)


BEAM = NS(type="beam", gain_dbi=10.0, hpbw_deg=52.0, front_to_back_db=20.0)
ISO = NS(type="isotropic", gain_dbi=2.0)
TABLE = NS(type="table", theta_deg=[0.0, 10.0, 20.0, 90.0, 180.0], gain_dbi=[10.0, 8.0, 4.0, -5.0, -12.0])
UHF_DOWN = 260e6
KU_DOWN = 11.95e9


def half_power_width(model: Any, freq_hz: float) -> float:
    """Full -3 dB width located on the pattern itself (independent of ``hpbw_deg``)."""
    peak = float(ant.gain_dbi(model, 0.0, freq_hz))
    half = brentq(lambda th: float(ant.gain_dbi(model, th, freq_hz)) - (peak - 3.0), 0.0, 180.0, xtol=1e-12)
    return 2.0 * half


# --------------------------------------------------------------------------- parabolic


def test_parabolic_peak_gain_matches_aperture_formula() -> None:
    # G = efficiency * 4 pi A / lambda^2 with A = pi D^2 / 4 (the aperture form)
    for diameter, freq, eta in [(1.2, KU_DOWN, 0.55), (2.4, 14.25e9, 0.6), (0.72, 1.5e9, 0.5)]:
        lam = C / freq
        aperture = math.pi * diameter**2 / 4.0
        expected = 10.0 * math.log10(eta * 4.0 * math.pi * aperture / lam**2)
        assert ant.peak_gain_dbi(dish(diameter, eta), freq) == pytest.approx(expected, abs=1e-10)
        assert ant.gain_dbi(dish(diameter, eta), 0.0, freq) == pytest.approx(expected, abs=1e-10)
    # hand number: 1.2 m, 11.95 GHz, 0.55: lambda = 25.087 mm, pi D / lambda = 150.27,
    # 0.55 * 150.27^2 = 12420 -> 40.94 dBi
    assert ant.peak_gain_dbi(dish(1.2), KU_DOWN) == pytest.approx(40.94, abs=0.01)


def test_parabolic_hpbw_is_70_lambda_over_d() -> None:
    # 2.4 m at 11.95 GHz: 70 * 0.025087 / 2.4 = 0.7317 degrees
    assert ant.hpbw_deg(dish(2.4), KU_DOWN) == pytest.approx(0.7317, abs=1e-4)
    assert ant.hpbw_deg(dish(1.2), KU_DOWN) == pytest.approx(70.0 * (C / KU_DOWN) / 1.2, rel=1e-13)


@pytest.mark.parametrize("wavelengths", [3.0, 4.5, 10.0, 24.0, 35.0, 60.0, 155.0, 300.0])
def test_parabolic_half_power_points_are_hpbw_apart(wavelengths: float) -> None:
    freq = 3e9
    model = dish(wavelengths * C / freq)
    assert not ant.electrically_small(model, freq)
    assert half_power_width(model, freq) == pytest.approx(float(ant.hpbw_deg(model, freq)), rel=1e-9)


def test_parabolic_side_lobe_envelope_values() -> None:
    model = dish(2.4)
    # far outside the 0.73 degree main lobe the pattern is the envelope 32 - 25 log10(theta)
    for theta in (1.0, 2.5, 5.0, 20.0, 47.0):
        assert ant.gain_dbi(model, theta, KU_DOWN) == pytest.approx(32.0 - 25.0 * math.log10(theta), abs=1e-12)
    assert ant.gain_dbi(model, 5.0, KU_DOWN) == pytest.approx(14.53, abs=0.01)  # 32 - 17.47
    # -10 dBi beyond 48 degrees (the envelope arrives there within 0.03 dB and is never lower)
    for theta in (47.9, 48.0, 48.1, 60.0, 90.0, 180.0):
        assert ant.gain_dbi(model, theta, KU_DOWN) == pytest.approx(-10.0, abs=1e-12)
    assert 32.0 - 25.0 * math.log10(48.0) == pytest.approx(-10.0, abs=0.035)
    # inside 1 degree the floor is held at its 1 degree value: no hole next to the main lobe
    assert ant.gain_dbi(model, 0.999, KU_DOWN) == pytest.approx(32.0, abs=1e-9)


def test_envelope_never_exceeds_boresight_gain() -> None:
    freq = 3e9
    model = dish(3.0 * C / freq)  # G0 = 16.9 dBi, below the 32 dBi envelope at 1 degree
    g0 = float(ant.peak_gain_dbi(model, freq))
    assert g0 == pytest.approx(10.0 * math.log10(0.55 * (3.0 * math.pi) ** 2), abs=1e-12)
    theta = np.linspace(0.0, 180.0, 3601)
    assert np.max(ant.gain_dbi(model, theta, freq)) <= g0 + 1e-12


def test_electrically_small_072_m_dish_at_260_mhz() -> None:
    model = dish(0.72)
    lam = C / UHF_DOWN
    assert 0.72 / lam == pytest.approx(0.6244, abs=1e-4)  # well under 3 wavelengths
    assert ant.electrically_small(model, UHF_DOWN) is True
    # G0 = 0.55 * (pi * 0.6244)^2 = 2.117 -> 3.26 dBi
    assert ant.peak_gain_dbi(model, UHF_DOWN) == pytest.approx(3.26, abs=0.01)
    # HPBW = min(70 / 0.6244, 120) = 112.1 degrees
    assert ant.hpbw_deg(model, UHF_DOWN) == pytest.approx(112.1, abs=0.05)
    assert half_power_width(model, UHF_DOWN) == pytest.approx(112.10, abs=0.01)
    # floor G0 - 15 = -11.74 dBi, but never below -10 dBi
    assert ant.gain_dbi(model, 180.0, UHF_DOWN) == pytest.approx(-10.0, abs=1e-12)
    # 30 degrees off: 3.256 - 12 (30 / 112.1)^2 = 2.397 dBi (a broad beam, not a focusing dish)
    assert ant.gain_dbi(model, 30.0, UHF_DOWN) == pytest.approx(2.397, abs=2e-3)


def test_electrically_small_floor_and_width_limit() -> None:
    # 1.2 m at 260 MHz: G0 = 7.69 dBi, floor G0 - 15 = -7.31 dBi (above -10)
    model = dish(1.2)
    assert ant.electrically_small(model, UHF_DOWN)
    g0 = 10.0 * math.log10(0.55 * (math.pi * 1.2 * UHF_DOWN / C) ** 2)
    assert g0 == pytest.approx(7.69, abs=0.01)
    assert ant.gain_dbi(model, 180.0, UHF_DOWN) == pytest.approx(g0 - 15.0, abs=1e-12)
    assert ant.gain_dbi(model, 120.0, UHF_DOWN) == pytest.approx(g0 - 15.0, abs=1e-12)
    # 0.5 m at 260 MHz: 70 lambda / D = 161 degrees, limited to 120
    assert ant.hpbw_deg(dish(0.5), UHF_DOWN) == 120.0
    assert half_power_width(dish(0.5), UHF_DOWN) == pytest.approx(120.0, abs=1e-6)


def test_electrically_small_boundary_is_three_wavelengths() -> None:
    freq = 1e9
    lam = C / freq
    assert ant.electrically_small(dish(2.999 * lam), freq) is True
    assert ant.electrically_small(dish(3.001 * lam), freq) is False
    assert ant.electrically_small(dish(1.2), KU_DOWN) is False
    flags = ant.electrically_small(dish(1.2), np.array([260e6, 700e6, 800e6, 12e9]))
    assert flags.tolist() == [True, True, False, False]  # 3 wavelengths at 749.5 MHz
    for other in (BEAM, ISO, TABLE):
        assert ant.electrically_small(other, 260e6) is False


@pytest.mark.parametrize(
    ("diameter_m", "freq_hz"),
    [(0.72, 260e6), (1.2, 260e6), (0.5, 150e6), (0.05, 100e6), (2.4, 260e6), (0.72, 1.3e9), (1.2, 12e9), (9.0, 14e9)],
)
def test_parabolic_never_below_floor_never_above_peak_and_monotone(diameter_m: float, freq_hz: float) -> None:
    model = dish(diameter_m)
    theta = np.linspace(0.0, 180.0, 18001)
    gain = ant.gain_dbi(model, theta, freq_hz)
    peak = float(ant.peak_gain_dbi(model, freq_hz))
    assert np.all(gain >= ant.MIN_GAIN_DBI)
    assert np.all(gain <= peak + 1e-12)
    assert gain[0] == pytest.approx(peak, abs=1e-12)
    assert np.all(np.diff(gain) <= 1e-12)


def test_tiny_reflector_is_clamped_at_the_floor() -> None:
    # 5 cm at 100 MHz: the formula gives -28 dBi; the pattern floor (-10 dBi) applies everywhere
    model = dish(0.05)
    assert 10.0 * math.log10(0.55 * (math.pi * 0.05 * 100e6 / C) ** 2) == pytest.approx(-28.2, abs=0.05)
    assert ant.peak_gain_dbi(model, 100e6) == -10.0
    assert np.all(ant.gain_dbi(model, np.array([0.0, 45.0, 180.0]), 100e6) == -10.0)


def test_shipped_uhf_antennas_do_not_radiate_more_than_they_are_fed() -> None:
    """Energy check: the integral of the gain over the sphere is at most 4 pi."""
    theta = np.linspace(0.0, 180.0, 90001)
    rad = np.radians(theta)
    for model in (dish(0.72), dish(1.2), BEAM):
        gain = 10.0 ** (ant.gain_dbi(model, theta, UHF_DOWN) / 10.0)
        fraction = np.trapezoid(gain * np.sin(rad), rad) / 2.0
        assert 0.3 < fraction <= 1.0


# --------------------------------------------------------------------------- other types


def test_beam_pattern() -> None:
    assert ant.peak_gain_dbi(BEAM, 3e8) == 10.0
    assert ant.hpbw_deg(BEAM, 3e8) == 52.0
    assert ant.gain_dbi(BEAM, 0.0, 3e8) == 10.0
    assert ant.gain_dbi(BEAM, 26.0, 3e8) == pytest.approx(7.0, abs=1e-12)  # half power at HPBW / 2
    assert half_power_width(BEAM, 3e8) == pytest.approx(52.0, abs=1e-9)
    assert ant.gain_dbi(BEAM, 13.0, 3e8) == pytest.approx(10.0 - 12.0 / 16.0, abs=1e-12)
    # floor at gain - front_to_back, reached where 12 (theta / 52)^2 = 20
    edge = 52.0 * math.sqrt(20.0 / 12.0)
    assert ant.gain_dbi(BEAM, edge + 0.01, 3e8) == pytest.approx(-10.0, abs=1e-12)
    assert ant.gain_dbi(BEAM, 180.0, 3e8) == pytest.approx(-10.0, abs=1e-12)
    low = NS(type="beam", gain_dbi=3.0, hpbw_deg=90.0, front_to_back_db=25.0)
    assert ant.gain_dbi(low, 180.0, 3e8) == pytest.approx(-22.0, abs=1e-12)  # the configured back lobe
    theta = np.linspace(0.0, 180.0, 1801)
    gain = ant.gain_dbi(BEAM, theta, 3e8)
    assert np.all(gain >= 10.0 - 20.0 - 1e-12) and np.all(np.diff(gain) <= 1e-12)
    # frequency does not matter for a beam antenna
    assert np.array_equal(gain, ant.gain_dbi(BEAM, theta, 12e9))


def test_beam_front_to_back_defaults_to_20_db() -> None:
    model = {"type": "beam", "gain_dbi": 12.0, "hpbw_deg": 40.0}
    assert ant.gain_dbi(model, 180.0, 3e8) == pytest.approx(-8.0, abs=1e-12)


def test_isotropic() -> None:
    theta = np.array([0.0, 37.0, 180.0])
    assert np.array_equal(ant.gain_dbi(ISO, theta, 3e8), np.full(3, 2.0))
    assert ant.peak_gain_dbi(ISO, 3e8) == 2.0
    assert ant.hpbw_deg(ISO, 3e8) == 360.0
    assert ant.gain_dbi({"type": "isotropic"}, 10.0, 1e9) == 0.0  # gain_dbi defaults to 0


def test_table_pattern() -> None:
    # nodes, linear interpolation in dB, ends held
    assert ant.gain_dbi(TABLE, 10.0, 3e8) == 8.0
    assert ant.gain_dbi(TABLE, 15.0, 3e8) == pytest.approx(6.0, abs=1e-12)
    assert ant.gain_dbi(TABLE, 55.0, 3e8) == pytest.approx(4.0 - 9.0 * 35.0 / 70.0, abs=1e-12)
    assert ant.gain_dbi(TABLE, 180.0, 3e8) == -12.0
    partial = NS(type="table", theta_deg=[5.0, 30.0], gain_dbi=[6.0, -4.0])
    assert ant.gain_dbi(partial, 0.0, 3e8) == 6.0  # held in to boresight
    assert ant.gain_dbi(partial, 120.0, 3e8) == -4.0  # held out to the back
    assert ant.peak_gain_dbi(TABLE, 3e8) == 10.0
    # 3 dB below boresight (7 dBi) lies between the 8 dBi and 4 dBi nodes: 10 + 10 / 4 = 12.5 degrees
    assert ant.hpbw_deg(TABLE, 3e8) == pytest.approx(25.0, abs=1e-12)
    assert half_power_width(TABLE, 3e8) == pytest.approx(25.0, abs=1e-9)
    flat = NS(type="table", theta_deg=[0.0, 180.0], gain_dbi=[1.0, -1.0])
    assert ant.hpbw_deg(flat, 3e8) == 360.0  # never 3 dB down
    cone = NS(type="table", theta_deg=[0.0, 30.0, 90.0], gain_dbi=[0.0, 5.0, -20.0])
    assert ant.peak_gain_dbi(cone, 3e8) == 5.0 and ant.gain_dbi(cone, 0.0, 3e8) == 0.0


# --------------------------------------------------------------------------- interface


def test_symmetry_and_broadcasting_match_a_scalar_loop() -> None:
    theta = np.array([-170.0, -30.0, -1.0, 0.0, 0.4, 3.0, 47.0, 49.0, 180.0])
    freq = np.array([260e6, 1.5e9, 11.95e9])[:, None]
    for model in (dish(1.2), dish(0.72, 0.6), BEAM, ISO, TABLE):
        out = ant.gain_dbi(model, theta[None, :], freq)
        assert out.shape == (3, 9)
        for i in range(3):
            for j in range(9):
                scalar = ant.gain_dbi(model, abs(float(theta[j])), float(freq[i, 0]))
                assert out[i, j] == pytest.approx(float(scalar), abs=1e-12)
        assert np.ndim(ant.gain_dbi(model, 12.0, 260e6)) == 0
        peaks = ant.peak_gain_dbi(model, freq[:, 0])
        widths = ant.hpbw_deg(model, freq[:, 0])
        assert peaks.shape == (3,) and widths.shape == (3,)
        assert peaks[2] == pytest.approx(float(ant.peak_gain_dbi(model, 11.95e9)), abs=1e-12)


def test_mapping_namespace_and_config_models_agree() -> None:
    from petal_locate.config import BeamAntenna, IsotropicAntenna, ParabolicAntenna, TableAntenna

    theta = np.linspace(0.0, 180.0, 37)
    cases = [
        (ParabolicAntenna(type="parabolic", diameter_m=0.72), dish(0.72), {"type": "parabolic", "diameter_m": 0.72}),
        (
            BeamAntenna(type="beam", gain_dbi=10.0, hpbw_deg=52.0),
            BEAM,
            {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 20.0},
        ),
        (IsotropicAntenna(gain_dbi=2.0), ISO, {"type": "isotropic", "gain_dbi": 2.0}),
        (
            TableAntenna(type="table", theta_deg=TABLE.theta_deg, gain_dbi=TABLE.gain_dbi),
            TABLE,
            {"type": "table", "theta_deg": TABLE.theta_deg, "gain_dbi": TABLE.gain_dbi},
        ),
    ]
    for model, namespace, mapping in cases:
        reference = ant.gain_dbi(namespace, theta, UHF_DOWN)
        assert np.array_equal(ant.gain_dbi(model, theta, UHF_DOWN), reference)
        assert np.array_equal(ant.gain_dbi(mapping, theta, UHF_DOWN), reference)
        assert ant.hpbw_deg(model, UHF_DOWN) == ant.hpbw_deg(namespace, UHF_DOWN)


@pytest.mark.parametrize(
    ("model", "theta", "freq", "field"),
    [
        (NS(type="horn"), 0.0, 1e9, "antenna.type"),
        (NS(), 0.0, 1e9, "antenna.type"),
        (None, 0.0, 1e9, "antenna"),
        (NS(type="parabolic"), 0.0, 1e9, "antenna.diameter_m"),
        (NS(type="parabolic", diameter_m=-1.0), 0.0, 1e9, "antenna.diameter_m"),
        (NS(type="parabolic", diameter_m=1.0, efficiency=1.5), 0.0, 1e9, "antenna.efficiency"),
        (NS(type="beam", gain_dbi=10.0), 0.0, 1e9, "antenna.hpbw_deg"),
        (NS(type="beam", gain_dbi="x", hpbw_deg=30.0), 0.0, 1e9, "antenna.gain_dbi"),
        (NS(type="beam", gain_dbi=10.0, hpbw_deg=0.0), 0.0, 1e9, "antenna.hpbw_deg"),
        (NS(type="beam", gain_dbi=10.0, hpbw_deg=30.0, front_to_back_db=-1.0), 0.0, 1e9, "antenna.front_to_back_db"),
        (NS(type="table", theta_deg=[0.0, 10.0], gain_dbi=[1.0]), 0.0, 1e9, "antenna.gain_dbi"),
        (NS(type="table", theta_deg=[10.0, 5.0], gain_dbi=[1.0, 0.0]), 0.0, 1e9, "antenna.theta_deg"),
        (NS(type="table", theta_deg=[0.0, 200.0], gain_dbi=[1.0, 0.0]), 0.0, 1e9, "antenna.theta_deg"),
        (ISO, 181.0, 1e9, "off_axis_deg"),
        (ISO, float("nan"), 1e9, "off_axis_deg"),
        (ISO, 10.0, 0.0, "freq_hz"),
        (ISO, 10.0, float("inf"), "freq_hz"),
    ],
)
def test_invalid_input_names_the_field(model: Any, theta: float, freq: float, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        ant.gain_dbi(model, theta, freq)
