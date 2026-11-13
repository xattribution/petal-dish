"""Independent end-to-end verification of the simulator, the sim driver, accuracy
prediction and the Monte Carlo driver (everything here is simulated).

- The signal model of ``SimWorld.render_iq`` against a from-scratch implementation of
  MATH.md sections 2 to 4 in extended precision (own geo orbit, RIC perturbation,
  Sagnac light time, phase, Doppler, link budget with power robbing), not
  ``core.geometry`` / ``core.orbits`` / ``core.linkbudget``.
- ``render_psd`` against ``welch_psd_db(render_iq)``, including the chirp line
  spectrum (Fresnel skirts) checked against an FFT of one sweep period.
- Every shipped scenario through the chain the server runs (``server/pipeline``
  detection specs and trackers on ``render_psd`` frames, the tasking rules for
  primary and partner channels, sample rate, windows with ephemeris pads, self-peak
  exclusions, mirror windows, reference captures in a later slot,
  ``calibration_correction``, ``solve``), with the claims each description makes.
- ``accuracy_map`` against the full-chain Monte Carlo, and the sim driver's world
  switching through an in-process backplane transport.
"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from petal_locate.config.loader import load_config
from petal_locate.config.models import EmitterModel, Region, SystemConfig
from petal_locate.core.accuracy import accuracy_map
from petal_locate.core.antennas import gain_dbi
from petal_locate.core.caf import CafResult, caf_estimate
from petal_locate.core.channel import thermal_noise
from petal_locate.core.constants import C as C_LIGHT
from petal_locate.core.geodesy import geodesic_distance_m, radii_of_curvature
from petal_locate.core.geometry import elevation_deg, measurement_windows, predict_pair, search_windows, self_peak
from petal_locate.core.linkbudget import downlink_quality_db
from petal_locate.core.signals import ChirpWaveform
from petal_locate.core.solver import Measurement, Solution, calibration_correction, solve
from petal_locate.core.spectrum import welch_psd_db
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.core.types import PathSpec
from petal_locate.node.clock import ManualClock
from petal_locate.node.drivers.base import CaptureError, DriverContext
from petal_locate.node.drivers.sim import SimDriver, SimWorldSource
from petal_locate.protocol.messages import ChannelAssignment, MonitorAssignment
from petal_locate.server.pipeline.detection import build_trackers, build_window_specs
from petal_locate.sim import Scenario, SimWorld, load_scenario
from petal_locate.sim.montecarlo import DEFAULT_T_NS, run_trials
from petal_locate.sim.world import _chirp_lines

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "config" / "scenarios"
NS = 1_000_000_000
T_DET = ns_from_iso("2026-01-01T03:00:00Z")  # detection starts; captures 12 s later
CHI2_95 = 5.991
CHI2_999 = 13.816


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(ROOT / "config" / "pacific-uhf.yaml")


@pytest.fixture(scope="module")
def cfg_bare(cfg: SystemConfig) -> SystemConfig:
    data = cfg.export_dict()
    data["carriers"] = []
    return SystemConfig.model_validate(data)


# =========================================================================== first principles

L = np.longdouble
_C = L(299792458.0)
_OMEGA = L("7.2921151467e-5")
_GM = L("3.986004418e14")
_A = L(6378137.0)
_F = L(1) / L("298.257223563")
_K_DBW = -228.6
_EPOCH_NS = ns_from_iso("2026-01-01T00:00:00Z")


def _rz(a: Any) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s, L(0)], [s, c, L(0)], [L(0), L(0), L(1)]], dtype=L)


def _rx(a: Any) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[L(1), L(0), L(0)], [L(0), c, -s], [L(0), s, c]], dtype=L)


def _lla(lat: float, lon: float, h: float) -> np.ndarray:
    la, lo = np.radians(L(lat)), np.radians(L(lon))
    e2 = _F * (2 - _F)
    n = _A / np.sqrt(1 - e2 * np.sin(la) ** 2)
    return np.array([(n + h) * np.cos(la) * np.cos(lo), (n + h) * np.cos(la) * np.sin(lo), (n * (1 - e2) + h) * np.sin(la)],
                    dtype=L)


class _Geo:
    """MATH.md section 2 geo orbit, epoch 2026-01-01; time in seconds since the epoch."""

    def __init__(self, lon: float, inc: float, phase: float) -> None:
        self.lon, self.inc, self.phase = np.radians(L(lon)), np.radians(L(inc)), np.radians(L(phase))
        self.a = (_GM / _OMEGA ** 2) ** (L(1) / 3)

    def pos(self, t: Any) -> np.ndarray:
        u = self.phase + _OMEGA * t
        r_i = _rz(self.lon - self.phase) @ _rx(self.inc) @ np.array([self.a * np.cos(u), self.a * np.sin(u), L(0)], dtype=L)
        return _rz(-_OMEGA * t) @ r_i


class _Perturbed:
    """MATH.md section 2 PerturbedOrbit with an independent RIC frame."""

    def __init__(self, base: _Geo, pos: list[float], vel: list[float], t_ref: float) -> None:
        self.base, self.p, self.v, self.t_ref = base, np.array(pos, dtype=L), np.array(vel, dtype=L), L(t_ref)

    def pos(self, t: Any) -> np.ndarray:
        r = self.base.pos(t)
        h = L(1e-3)
        v = (self.base.pos(t + h) - self.base.pos(t - h)) / (2 * h)
        w = np.array([L(0), L(0), _OMEGA], dtype=L)
        rad = r / np.sqrt(r @ r)
        cross = np.cross(r, v + np.cross(w, r))
        cross = cross / np.sqrt(cross @ cross)
        q = np.stack([rad, np.cross(cross, rad), cross], axis=1)
        return r + q @ (self.p + self.v * (t - self.t_ref))


def _tau_d(orbit: Any, xg: np.ndarray, t: Any) -> Any:
    tau = L(0)
    for _ in range(8):
        tau = np.sqrt(np.sum((xg - _rz(-_OMEGA * tau) @ orbit.pos(t - tau)) ** 2)) / _C
    return tau


def _tau_u(orbit: Any, xe: np.ndarray, t_sat: Any) -> Any:
    r = orbit.pos(t_sat)
    tau = L(0)
    for _ in range(8):
        tau = np.sqrt(np.sum((r - _rz(-_OMEGA * tau) @ xe) ** 2)) / _C
    return tau


def _indep_config() -> SystemConfig:
    tp = dict(bandwidth_hz=25e3, sfd_dbw_m2=-136.0, eirp_sat_dbw=26.0)
    return SystemConfig.model_validate({
        "name": "verify", "description": "simulated verification config",
        "satellites": [
            {"id": "s1", "name": "S1", "orbit": {"type": "geo", "lon_deg": 140.0, "inclination_deg": 3.0, "phase_deg": 30.0},
             "transponders": [
                 {"id": "t1", "uplink_center_hz": 300.0e6, "translation_hz": -40.0e6, "g_over_t_dbk": 60.0,
                  "group_delay_s": 1.5e-6, **tp},
                 {"id": "t2", "uplink_center_hz": 300.2e6, "translation_hz": -40.0e6, "g_over_t_dbk": -30.0, **tp},
                 {"id": "t3", "uplink_center_hz": 300.4e6, "translation_hz": -40.0e6, "g_over_t_dbk": 60.0, **tp}]},
            {"id": "s2", "name": "S2", "orbit": {"type": "geo", "lon_deg": 175.0, "inclination_deg": 2.0, "phase_deg": 100.0},
             "transponders": [{"id": "t1", "uplink_center_hz": 300.0e6, "translation_hz": -33.0e6, "g_over_t_dbk": 60.0, **tp}]},
        ],
        "sites": [
            {"id": "g1", "name": "G1", "lat_deg": 30.0, "lon_deg": 150.0, "alt_m": 25.0, "antenna_height_m": 12.0,
             "channels": [{"id": "c1", "antenna": {"type": "isotropic"}, "pointing": {"satellite_id": "s1"},
                           "system_noise_temp_k": 350.0, "polarization_loss_db": 1.5}]},
            {"id": "g2", "name": "G2", "lat_deg": 10.0, "lon_deg": 160.0, "antenna_height_m": 10.0,
             "channels": [{"id": "c2", "antenna": {"type": "isotropic"}, "pointing": {"satellite_id": "s2"}}]},
        ],
        "processing": {"region": {"lat_min": -60, "lat_max": 60, "lon_min": 100, "lon_max": -150}},
    })


_T_REF_ISO = "2026-03-01T05:00:00Z"
_F_E = 300.0021e6


def _indep_world() -> SimWorld:
    errors = {
        "ephemeris": {"s1": {"ric_pos_m": [120.0, -600.0, 350.0], "ric_vel_mps": [0.01, 0.03, -0.02], "t_ref": _T_REF_ISO}},
        "sat_osc_frac": {"s1": 3.0e-7},
        "site_clock_offset_ns": {"g1": 1000.0},
        "site_osc_frac": {"g1": 2.0e-9},
    }
    emitters = [{"id": "cw", "lat_deg": 20.0, "lon_deg": 145.0, "alt_m": 3.0, "antenna_height_m": 7.0, "freq_hz": _F_E,
                 "eirp_dbw": 40.0, "waveform": {"type": "cw"}},
                {"id": "nz", "lat_deg": 12.0, "lon_deg": 155.0, "antenna_height_m": 5.0, "freq_hz": 300.4e6,
                 "eirp_dbw": 30.0, "waveform": {"type": "noise", "bandwidth_hz": 10e3}}]
    sc = Scenario.model_validate({"name": "verify", "seed": 11, "description": "simulated", "emitters": emitters,
                                  "errors": errors})
    return SimWorld(_indep_config(), sc)


def test_render_iq_matches_an_independent_first_principles_model() -> None:
    """A CW path (perturbed inclined geo orbit with velocity error, transponder group
    delay, site clock and oscillator, satellite oscillator, power robbing) sample by
    sample against MATH.md sections 2-4 written from scratch in extended precision."""
    world = _indep_world()
    t0 = ns_from_iso("2026-03-01T06:00:00Z") + 123_456_789
    fs, n = 31250.0, 31250
    t_nom = -40.0e6
    fc = _F_E + t_nom + 1234.5
    y = world.render_iq("c1", t0, n, fs, fc).astype(np.complex128)
    y -= thermal_noise(n, f"thermal|11|c1|{t0}|{float(fs)}|{float(fc)}")

    s1 = _Perturbed(_Geo(140.0, 3.0, 30.0), [120.0, -600.0, 350.0], [0.01, 0.03, -0.02],
                    (ns_from_iso(_T_REF_ISO) - _EPOCH_NS) * 1e-9)
    xg, xe = _lla(30.0, 150.0, 37.0), _lla(20.0, 145.0, 10.0)
    delta, eps_g, tau_x = L(1000e-9), L(2e-9), L(1.5e-6)
    big_t = L(t_nom) * (1 + L(3e-7))
    f_off = L(_F_E) + big_t - L(fc) * (1 + eps_g)
    base = L(t0 - _EPOCH_NS) / L(1e9)

    def phase(k: float) -> Any:
        tp = base + L(k) / L(fs) + delta
        td = _tau_d(s1, xg, tp)
        tu = _tau_u(s1, xe, tp - td - tau_x)
        return f_off * (L(k) / L(fs) + delta) - (L(_F_E) + big_t) * (td + tau_x) - L(_F_E) * tu

    idx = np.arange(0, n, 211)
    ph = np.array([phase(int(k)) for k in idx], dtype=L)
    expected = np.exp(2j * np.pi * (ph - np.round(ph)).astype(np.float64))
    rot = y[idx] / expected
    resid = (np.angle(rot * np.conj(np.mean(rot))) / (2 * np.pi))
    # MATH.md section 4: a per-path constant phase is immaterial; everything else must match
    assert float(np.max(np.abs(resid))) < 1e-5
    # baseband frequency: derivative of the independent phase vs the rendered phase slope
    f_ind = float(phase(n // 2 + 0.5 * fs) - phase(n // 2 - 0.5 * fs))
    f_meas = float(np.mean(np.angle(y[1:] * np.conj(y[:-1])))) * fs / (2 * np.pi)
    assert abs(f_meas - f_ind) < 1e-5

    # amplitude from a hand link budget: PFD, linear transponder, power robbing, FSPL
    r_sat = s1.pos(base + L(0.5))
    rho_u = float(np.sqrt(np.sum((r_sat - xe) ** 2)))
    rho_d = float(np.sqrt(np.sum((r_sat - xg) ** 2)))
    pfd = 40.0 - 10 * math.log10(4 * math.pi * rho_u ** 2)
    carrier_w = 10 ** ((26.0 + 136.0 + pfd) / 10)
    n0_eirp = 26.0 + 136.0 + 10 * math.log10(4 * math.pi * 300.0e6 ** 2 / float(_C) ** 2) - 60.0 + _K_DBW
    scale = min(1.0, 10 ** 2.6 / (carrier_w + 10 ** (n0_eirp / 10) * 25e3))
    assert scale < 0.1  # the transponder is driven 13 dB into robbing
    c_rx = 10 * math.log10(carrier_w * scale) - 20 * math.log10(4 * math.pi * rho_d * (_F_E + float(big_t)) / float(_C)) - 1.5
    a2 = 10 ** ((c_rx - (_K_DBW + 10 * math.log10(350.0))) / 10) / fs
    assert abs(float(np.mean(np.abs(y) ** 2)) / a2 - 1.0) < 2e-3

    # truth TDOA / FDOA (MATH.md section 5) against the same independent model, pair s1/g1 - s2/g2
    s2, xg2 = _Geo(175.0, 2.0, 100.0), _lla(10.0, 160.0, 10.0)
    t_mid = t0 + round(1e9 * (n - 1) / (2 * fs))
    a = PathSpec("s1", "g1", _F_E, t_nom, 1.5e-6)
    b = PathSpec("s2", "g2", _F_E, -33.0e6, 0.0)
    tdoa_w, fdoa_w = world.truth_measurement("cw", a, b, t_mid)
    tm = L(t_mid - _EPOCH_NS) / L(1e9)

    def d_lab(orbit: Any, x_g: np.ndarray, tx: Any, dl: Any, t_lab: Any) -> Any:
        tp = t_lab + dl
        td = _tau_d(orbit, x_g, tp)
        return td + tx + _tau_u(orbit, xe, tp - td - tx) - dl

    def ph_lab(orbit: Any, x_g: np.ndarray, tx: Any, dl: Any, eg: Any, tt: Any, f_tune: float, t_lab: Any) -> Any:
        tp = t_lab + dl
        td = _tau_d(orbit, x_g, tp)
        tu = _tau_u(orbit, xe, tp - td - tx)
        return (L(_F_E) + tt - L(f_tune) * (1 + eg)) * tp - (L(_F_E) + tt) * (td + tx) - L(_F_E) * tu

    d_b = d_lab(s2, xg2, L(0), L(0), tm)
    tdoa = L(0)
    for _ in range(6):
        tdoa = d_lab(s1, xg, tau_x, delta, tm + tdoa) - d_b
    h = L(0.25)
    fa = (ph_lab(s1, xg, tau_x, delta, eps_g, big_t, a.f_tune_hz, tm + tdoa + h)
          - ph_lab(s1, xg, tau_x, delta, eps_g, big_t, a.f_tune_hz, tm + tdoa - h)) / (2 * h)
    fb = (ph_lab(s2, xg2, L(0), L(0), L(0), L(-33.0e6), b.f_tune_hz, tm + h)
          - ph_lab(s2, xg2, L(0), L(0), L(0), L(-33.0e6), b.f_tune_hz, tm - h)) / (2 * h)
    assert abs(tdoa_w - float(tdoa)) < 1e-15
    assert abs(fdoa_w - float(fa - fb)) < 1e-7


def test_noise_envelope_follows_the_independent_light_time() -> None:
    """A noise emitter's samples equal its waveform at the emission times of the
    independent model (true time = labelled + 1 us clock offset, minus the bent-pipe
    delay), times the independent carrier phase, up to one complex gain."""
    world = _indep_world()
    t0 = ns_from_iso("2026-03-01T06:00:00Z") + 987_654_321
    fs, n = 31250.0, 8192
    fc = 300.4e6 - 40.0e6
    y = world.render_iq("c1", t0, n, fs, fc).astype(np.complex128)
    y -= thermal_noise(n, f"thermal|11|c1|{t0}|{float(fs)}|{float(fc)}")
    s1 = _Perturbed(_Geo(140.0, 3.0, 30.0), [120.0, -600.0, 350.0], [0.01, 0.03, -0.02],
                    (ns_from_iso(_T_REF_ISO) - _EPOCH_NS) * 1e-9)
    xg, xe = _lla(30.0, 150.0, 37.0), _lla(12.0, 155.0, 5.0)
    delta, eps_g = L(1000e-9), L(2e-9)
    big_t = L(-40.0e6) * (1 + L(3e-7))
    f_e = L(300.4e6)
    f_off = f_e + big_t - L(fc) * (1 + eps_g)
    base = L(t0 - _EPOCH_NS) / L(1e9)
    idx = np.arange(0, n, 37)
    emit = np.empty(idx.size)
    ph = np.empty(idx.size, dtype=L)
    for j, k in enumerate(idx):
        t_rel = L(int(k)) / L(fs)
        td = _tau_d(s1, xg, base + t_rel + delta)
        tu = _tau_u(s1, xe, base + t_rel + delta - td)
        emit[j] = float(t_rel + delta - td - tu)
        ph[j] = f_off * (t_rel + delta) - (f_e + big_t) * td - f_e * tu
    x = world.emitter("nz").waveform.evaluate(t0, emit) * np.exp(2j * np.pi * (ph - np.round(ph)).astype(np.float64))
    sel = y[idx]
    gain = np.vdot(x, sel) / np.vdot(x, x)
    assert float(np.linalg.norm(sel - gain * x) / np.linalg.norm(gain * x)) < 1e-3


def test_relayed_transponder_noise_level_matches_a_hand_budget() -> None:
    """The floor inside a transponder's downlink is 1 + N0_relay / N0_site (MATH.md
    section 4) with N0_relay from the transponder alone, computed by hand."""
    world = _indep_world()
    t0 = ns_from_iso("2026-03-01T06:00:00Z")
    fc, span, nfft, navg = 300.2e6 - 40.0e6, 31250.0, 1024, 8
    acc = np.zeros(nfft)
    for r in range(4):
        acc += 10 ** (welch_psd_db(world.render_iq("c1", t0 + r * 1_013_000_000, nfft * navg, span, fc), span, nfft, navg) / 10)
    acc /= 4
    s1 = _Perturbed(_Geo(140.0, 3.0, 30.0), [120.0, -600.0, 350.0], [0.01, 0.03, -0.02],
                    (ns_from_iso(_T_REF_ISO) - _EPOCH_NS) * 1e-9)
    rho_d = float(np.sqrt(np.sum((s1.pos(L(t0 - _EPOCH_NS) / L(1e9)) - _lla(30.0, 150.0, 37.0)) ** 2)))
    n0_eirp = 26.0 + 136.0 + 10 * math.log10(4 * math.pi * 300.2e6 ** 2 / float(_C) ** 2) + 30.0 + _K_DBW
    n0_relay = n0_eirp - 20 * math.log10(4 * math.pi * rho_d * (300.2e6 - 40.0e6 * (1 + 3e-7)) / float(_C)) - 1.5
    ratio = 10 ** ((n0_relay - (_K_DBW + 10 * math.log10(350.0))) / 10)
    assert ratio > 1.0  # the relayed noise is the larger part of the floor here
    f = (np.arange(nfft) - nfft // 2) * span / nfft
    inside, outside = np.abs(f) < 10e3, np.abs(f) > 13.5e3
    assert abs(10 * math.log10(acc[inside].mean() / (1.0 + ratio))) < 0.15
    assert abs(10 * math.log10(acc[outside].mean())) < 0.15
    expected = 10 ** (world.render_psd("c1", t0, fc, span, nfft, navg, expected=True) / 10)
    assert abs(10 * math.log10(expected[inside].mean() / (1.0 + ratio))) < 0.05


# =========================================================================== chirp spectrum


@pytest.mark.parametrize("width, period", [(1e4, 0.04), (1e4, 0.01), (2.5e4, 0.01), (3e3, 0.2)])
def test_chirp_lines_equal_the_fft_of_one_sweep(width: float, period: float) -> None:
    """``_chirp_lines`` (closed-form Fresnel coefficients) against the DFT of one period
    of ``ChirpWaveform.evaluate`` sampled at 16 W; Parseval."""
    n = 1 << int(np.ceil(np.log2(16 * width * period)))
    x = ChirpWaveform(width, period).evaluate(0, np.arange(n) * period / n)
    coeff = np.fft.fft(x) / n
    ref = {int(round(k)): abs(v) ** 2 for k, v in zip(np.fft.fftfreq(n, 1.0 / n), coeff)}
    f_l, p = _chirp_lines(width, period, -4 * width, 4 * width)
    assert abs(float(p.sum()) - 1.0) < 1e-4
    diffs = [abs(v - ref[int(round(f * period))]) for f, v in zip(f_l, p)]
    assert max(diffs) < 1e-5
    assert 0.005 < float(p[np.abs(f_l) > 0.5 * width].sum()) < 0.03  # the Fresnel skirts carry about 1 %


@pytest.mark.parametrize("fc, span", [(260.0e6, 31250.0), (260.0e6, 12000.0), (260.005e6, 12000.0)])
def test_render_psd_matches_welch_of_render_iq_for_a_chirp(cfg_bare: SystemConfig, fc: float, span: float) -> None:
    """Chirp rendered directly at fs (aliased skirts) or decimated: render_psd's line
    spectrum agrees with the Welch PSD of the IQ, also beyond the sweep edges, once the
    sweep alignment is averaged (start times not commensurate with the period)."""
    sc = Scenario.model_validate({"name": "t", "seed": 3, "description": "simulated", "emitters": [
        {"id": "j", "lat_deg": 21.0, "lon_deg": 127.0, "freq_hz": 301.005e6, "eirp_dbw": 20.0,
         "waveform": {"type": "chirp", "bandwidth_hz": 1e4, "sweep_period_s": 0.04}}]})
    world = SimWorld(cfg_bare, sc)
    meas = np.zeros(1024)
    exp = np.zeros(1024)
    for r in range(8):
        t = T_DET + r * 3_013_700_011
        meas += 10 ** (welch_psd_db(world.render_iq("honolulu-pac", t, 8192, span, fc), span, 1024, 8) / 10)
        exp += 10 ** (world.render_psd("honolulu-pac", t, fc, span, 1024, 8, expected=True) / 10)
    occupied = 10 * np.log10(exp / 8) > 3.0
    for sel in (occupied, ~occupied):
        assert abs(10 * math.log10(meas[sel].mean() / exp[sel].mean())) < 0.3
    chunks = 10 * np.log10(meas.reshape(-1, 32).mean(1) / exp.reshape(-1, 32).mean(1))
    assert float(np.max(np.abs(chunks))) < 0.75


# =========================================================================== the server's chain


@dataclass
class _Unit:
    channel_id: str
    site_id: str
    sat_id: str
    tp: Any
    lo: float
    hi: float
    snr_db: float
    cn0_dbhz: float


@dataclass
class _Event:
    lo: float
    hi: float
    units: list[_Unit]
    pairs: list[dict[str, Any]] = field(default_factory=list)
    solution: Solution | None = None
    reason: str | None = None

    @property
    def primary(self) -> _Unit:
        return max(self.units, key=lambda u: u.snr_db)


def _detect(world: SimWorld, cfg: SystemConfig, t0_ns: int, n_frames: int = 20) -> dict[tuple[str, str], list[Any]]:
    """render_psd frames of every monitor window through the server's trackers."""
    specs = build_window_specs(cfg, sec(t0_ns))
    trackers = build_trackers(cfg.processing.detection, specs.values())
    out: dict[tuple[str, str], list[Any]] = {}
    for k in range(n_frames):
        for key, spec in specs.items():
            tracker = trackers[key]
            assert not isinstance(tracker, ValueError), tracker
            t = t0_ns + int(round(k * NS / spec.frame_rate_hz))
            out[key] = tracker.update(world.render_psd(spec.channel_id, t, spec.center_hz, spec.span_hz, spec.nfft, spec.navg), t)
    return out


def _events(cfg: SystemConfig, dets: dict[tuple[str, str], list[Any]]) -> list[_Event]:
    """Unauthorised detections mapped to the uplink; overlapping ranges form one event."""
    units = []
    for (ch, _), found in dets.items():
        site, channel = cfg.channel(ch)
        for d in found:
            if d.authorized_carrier_id is not None and not d.anomaly:
                continue
            for sat_id in channel.watched_satellite_ids:
                mapped = cfg.uplink_for(sat_id, 0.5 * (d.f_lo_hz + d.f_hi_hz))
                if mapped is not None:
                    tp = mapped[0]
                    units.append(_Unit(ch, site.id, sat_id, tp, d.f_lo_hz - tp.translation_hz,
                                       d.f_hi_hz - tp.translation_hz, float(d.snr_db), float(d.cn0_dbhz)))
    units.sort(key=lambda u: u.lo)
    groups: list[list[_Unit]] = []
    for u in units:
        if groups and u.lo < max(x.hi for x in groups[-1]):
            groups[-1].append(u)
        else:
            groups.append([u])
    return [_Event(min(u.lo for u in g), max(u.hi for u in g), g) for g in groups]


def _search(cfg: SystemConfig, geo: Any, a: PathSpec, b: PathSpec, corr: tuple[float | None, float | None]) -> Any:
    """Tasking's windows: ephemeris allowance on uncalibrated kinds, then search_windows."""
    unc = cfg.resolved_uncertainty()
    zero = np.zeros(3)
    pos = sum(float(np.linalg.norm(unc.sat_pos_ric_m.get(s, zero))) for s in {a.sat_id, b.sat_id})
    vel = sum(float(np.linalg.norm(unc.sat_vel_ric_mps.get(s, zero))) for s in {a.sat_id, b.sat_id})
    (t_lo, t_hi), (f_lo, f_hi) = geo
    if corr[0] is None:
        t_lo, t_hi = t_lo - 6.0 * pos / C_LIGHT, t_hi + 6.0 * pos / C_LIGHT
    if corr[1] is None:
        f_lo, f_hi = f_lo - 6.0 * a.f_e_hz * vel / C_LIGHT, f_hi + 6.0 * a.f_e_hz * vel / C_LIGHT
    return search_windows(((t_lo, t_hi), (f_lo, f_hi)), a, b, cfg.processing.caf, unc, corr)


def _in_band(f_c: float, width: float, fc: float, fs: float) -> bool:
    half = 0.5 * max(width, 0.0)
    return min(f_c + half + 1e-9, fc + 0.5 * fs) > max(f_c - half, fc - 0.5 * fs)


def _seen(cfg: SystemConfig, site_id: str, sat_id: str, t_s: float) -> bool:
    return float(elevation_deg(cfg.sites_xyz()[site_id], cfg.orbits()[sat_id], t_s)) > 0.0


def _cross(cfg: SystemConfig, a: PathSpec, b: PathSpec, width: float, fs: float, t_s: float) -> tuple[PathSpec, PathSpec] | None:
    if not (_seen(cfg, a.site_id, b.sat_id, t_s) and _seen(cfg, b.site_id, a.sat_id, t_s)):
        return None
    f = a.f_e_hz
    if not (_in_band(f + b.translation_hz, width, a.f_tune_hz, fs) and _in_band(f + a.translation_hz, width, b.f_tune_hz, fs)):
        return None
    return (PathSpec(b.sat_id, a.site_id, f, b.translation_hz, b.tau_x_s),
            PathSpec(a.sat_id, b.site_id, f, a.translation_hz, a.tau_x_s))


def _samples_for(window: Any, fs: float, noise_bw: float) -> int:
    reach = max(abs(window[0]), abs(window[1]))
    return 2 * (int(math.ceil(reach * fs)) + 24 + 8 * int(math.ceil(fs / max(noise_bw, 1e-9))) + 2) + 2


def _correlate(cfg: SystemConfig, world: SimWorld, ch_a: str, ch_b: str, a: PathSpec, b: PathSpec, t_start: int,
               n: int, fs: float, width: float, windows: Any, mirror: Any) -> tuple[CafResult, int]:
    ya = world.render_iq(ch_a, t_start, n, fs, a.f_tune_hz)
    yb = world.render_iq(ch_b, t_start, n, fs, b.f_tune_hz)
    t_mid = t_start + int(round(1e9 * (n - 1) / (2.0 * fs)))
    t_s = t_mid / 1e9
    exclude = []
    for sat in cfg.satellites:
        if not (_seen(cfg, a.site_id, sat.id, t_s) and _seen(cfg, b.site_id, sat.id, t_s)):
            continue
        tp = next((tp for tp in sat.transponders if tp.relays(a.f_e_hz)), None)
        if tp is None:
            continue
        f_down = a.f_e_hz + tp.translation_hz
        if _in_band(f_down, width, a.f_tune_hz, fs) and _in_band(f_down, width, b.f_tune_hz, fs):
            td, fd = self_peak(sat.id, tp.translation_hz, a.f_e_hz, a, b, t_s, cfg.orbits(), cfg.sites_xyz())
            exclude.append((float(td), float(fd)))
    if a.site_id == b.site_id and a.translation_hz == b.translation_hz:
        exclude.append((0.0, b.f_tune_hz - a.f_tune_hz))
    caf = cfg.processing.caf
    result = caf_estimate(ya, yb, fs, delay_window=windows[0], doppler_window=windows[1], signal_bw_hz=max(width, 10.0),
                          t0_offset_s=0.0, exclude=exclude, mirror_window=mirror, min_snr_db=float(caf.min_snr_db),
                          max_tdoa_sigma_s=float(caf.max_tdoa_sigma_s))
    return result, t_mid


def _usable(r: CafResult) -> bool:
    return bool(r.detected) and r.reason not in ("ambiguous", "delay-doppler coupled", "peak outside window") \
        and not r.mirror_ambiguous


def _partners(cfg: SystemConfig, prim: _Unit, lo: float, hi: float, t_s: float) -> list[tuple[str, str, str, Any]]:
    """Tasking rule: per other satellite, best downlink_quality_db, primary's site within 3 dB."""
    out = []
    seen = {prim.sat_id}
    for sat, tp in cfg.transponders_covering(lo, hi):
        if sat.id in seen:
            continue
        seen.add(sat.id)
        q = [(v, site.id, ch.id) for site, ch in cfg.channels_watching(sat.id)
             if (v := downlink_quality_db(cfg, ch.id, sat.id, tp.id, t_s)) is not None]
        if q:
            top = max(v for v, _, _ in q)
            local = [x for x in q if x[1] == prim.site_id and x[0] >= top - 3.0]
            pick = max(local) if local else max(q)
            out.append((pick[2], pick[1], sat.id, tp))
    return out


def _run_event(cfg: SystemConfig, world: SimWorld, event: _Event, t_start: int, emitter_id: str | None,
               calibrate: bool, primary: str | None = None) -> _Event:
    """One tasking round of server/pipeline/tasking.py for an event, then solve."""
    ev = _Event(event.lo, event.hi, list(event.units))
    capc = cfg.processing.capture
    orbits, sites = cfg.orbits(), cfg.sites_xyz()
    lo, hi = ev.lo, ev.hi
    width = hi - lo
    f_e = float(round(0.5 * (lo + hi)))
    prim = ev.primary if primary is None else next(u for u in ev.units if u.channel_id == primary)
    t_mid_est = (t_start + NS // 2) / 1e9
    a = PathSpec(prim.sat_id, prim.site_id, f_e, float(prim.tp.translation_hz), float(prim.tp.group_delay_s))
    partners = _partners(cfg, prim, lo, hi, t_mid_est)
    max_rate = min(cfg.channel(c)[1].max_sample_rate_hz for c in [prim.channel_id] + [p[0] for p in partners])
    fs_guess = float(min(max(capc.min_sample_rate_hz, capc.oversample * width), max_rate))
    solver_cfg = cfg.processing.solver
    plans = []
    max_dop = 0.0
    for ch_b, site_b, sat_b, tp_b in partners:
        b = PathSpec(sat_b, site_b, f_e, float(tp_b.translation_hz), float(tp_b.group_delay_s))
        geo = measurement_windows(a, b, t_mid_est, orbits, sites, cfg.processing.region,
                                  float(solver_cfg.min_elevation_deg), float(solver_cfg.height_m))
        if geo is None:
            continue
        cross = _cross(cfg, a, b, width, fs_guess, t_mid_est)
        mirror = None
        if cross is not None:
            mgeo = measurement_windows(cross[0], cross[1], t_mid_est, orbits, sites, cfg.processing.region,
                                       float(solver_cfg.min_elevation_deg), float(solver_cfg.height_m))
            mirror = None if mgeo is None else _search(cfg, mgeo, cross[0], cross[1], (None, None))
        windows = _search(cfg, geo, a, b, (None, None))
        plans.append((ch_b, b, tp_b, geo, windows, mirror))
        max_dop = max([max_dop, abs(windows[1][0]), abs(windows[1][1])]
                      + ([abs(mirror[1][0]), abs(mirror[1][1])] if mirror else []))
    fs = float(math.floor(min(math.ceil(max(capc.min_sample_rate_hz, capc.oversample * width, 2.2 * max_dop)), max_rate)))
    noise_bw = min(fs, 1.05 * (max(width, 10.0) + 2.0 * max_dop))
    n = int(min(round(float(capc.duration_s) * fs), capc.max_samples))
    for _, _, _, _, windows, mirror in plans:
        n = max([n, _samples_for(windows[0], fs, noise_bw)] + ([_samples_for(mirror[0], fs, noise_bw)] if mirror else []))
    n = max(min(n, int(capc.max_samples)), 1024)
    cursor = t_start + int(math.ceil(n / fs * NS)) + 50_000_000  # reference slots follow the target capture
    for ch_b, b, tp_b, geo, windows, mirror in plans:
        report: dict[str, Any] = {"a": prim.channel_id, "b": ch_b, "fs": fs, "n": n, "a_path": a, "b_path": b}
        corr: dict[str, tuple[float, Any]] = {}
        refs = sorted((c for c in cfg.reference_carriers()
                       if prim.tp.relays(c.uplink_center_hz) and tp_b.relays(c.uplink_center_hz)
                       and not min(c.uplink_hi_hz, hi) > max(c.uplink_lo_hz, lo)),
                      key=lambda c: abs(float(c.uplink_center_hz) - f_e))
        if calibrate and refs:
            car = refs[0]
            bw = max(float(car.emitter.bandwidth_hz), 10.0)
            fs_r = float(math.floor(min(math.ceil(max(capc.min_sample_rate_hz, capc.oversample * bw)), max_rate)))
            f_r = float(car.uplink_center_hz)
            ra = PathSpec(a.sat_id, a.site_id, f_r, a.translation_hz, a.tau_x_s)
            rb = PathSpec(b.sat_id, b.site_id, f_r, b.translation_hz, b.tau_x_s)
            x_ref = np.asarray(cfg.carrier_xyz(car.id), dtype=np.float64)
            n_r = int(min(round(float(capc.duration_s) * fs_r), capc.max_samples))
            t_r = (cursor + int(0.5 * n_r / fs_r * NS)) / 1e9
            td, fd = predict_pair(x_ref, ra, rb, t_r, orbits, sites)
            rw = _search(cfg, ((float(td), float(td)), (float(fd), float(fd))), ra, rb, (None, None))
            rcross = _cross(cfg, ra, rb, bw, fs_r, t_r)
            rmirror = None
            if rcross is not None:
                mt, mf = predict_pair(x_ref, rcross[0], rcross[1], t_r, orbits, sites)
                rmirror = _search(cfg, ((float(mt), float(mt)), (float(mf), float(mf))), rcross[0], rcross[1], (None, None))
            n_r = max(min(max(n_r, _samples_for(rw[0], fs_r, min(fs_r, 1.05 * (bw + 2 * max(map(abs, rw[1])))))),
                          int(capc.max_samples)), 1024)
            rres, r_mid = _correlate(cfg, world, prim.channel_id, ch_b, ra, rb, cursor, n_r, fs_r, bw, rw, rmirror)
            cursor += int(math.ceil(n_r / fs_r * NS)) + 50_000_000
            report["reference"] = {"carrier": car.id, "result": rres}
            if _usable(rres):
                for kind, value, sigma in (("tdoa", rres.tdoa_s, rres.sigma_tdoa_s), ("fdoa", rres.fdoa_hz, rres.sigma_fdoa_hz)):
                    if value is not None and sigma is not None:
                        corr[kind] = calibration_correction(Measurement(kind, float(value), float(sigma), r_mid, ra, rb),
                                                            x_ref, orbits, sites, f"{car.id}|{prim.channel_id}|{ch_b}|{kind}")
        windows = _search(cfg, geo, a, b, (corr["tdoa"][0] if "tdoa" in corr else None,
                                           corr["fdoa"][0] if "fdoa" in corr else None))
        result, t_mid = _correlate(cfg, world, prim.channel_id, ch_b, a, b, t_start, n, fs, width, windows, mirror)
        report.update(result=result, t_mid=t_mid, calibrated=sorted(corr), windows=windows)
        if emitter_id is not None:
            report["truth"] = world.truth_measurement(emitter_id, a, b, t_mid)
        rows = []
        if _usable(result):
            for kind, value, sigma in (("tdoa", result.tdoa_s, result.sigma_tdoa_s), ("fdoa", result.fdoa_hz, result.sigma_fdoa_hz)):
                if value is not None and sigma is not None:
                    c, cal = corr.get(kind, (0.0, None))
                    rows.append(Measurement(kind, float(value), float(sigma), t_mid, a, b, correction=c, cal=cal))
        report["rows"] = rows
        ev.pairs.append(report)
    rows = [r for p in ev.pairs for r in p["rows"]]
    if len(rows) < 2:
        ev.reason = f"fewer than 2 rows ({len(rows)})"
        return ev
    ev.solution, ev.reason = solve(rows, orbits, sites, solver_cfg, cfg.processing.region, cfg.resolved_uncertainty())
    return ev


def _score(sol: Solution, lat: float, lon: float) -> tuple[float, float]:
    """(miss m, NEES with P_total) of a solution against the truth."""
    m, nrad = radii_of_curvature(lat)
    e = np.array([float(nrad) * math.cos(math.radians(lat)) * math.radians(((sol.lon_deg - lon + 180.0) % 360.0) - 180.0),
                  float(m) * math.radians(sol.lat_deg - lat)])
    return float(geodesic_distance_m(sol.lat_deg, sol.lon_deg, lat, lon)), float(e @ np.linalg.solve(sol.cov_en_m2, e))


def _nearest(sol: Solution, lat: float, lon: float) -> tuple[float, float]:
    """``_score`` of the candidate (solution or ambiguity) nearest the truth."""
    return min((_score(s, lat, lon) for s in [sol, *sol.ambiguities]), key=lambda x: x[0])


def _caf_errors(ev: _Event) -> list[float]:
    out = []
    for p in ev.pairs:
        r, (tt, tf) = p["result"], p["truth"]
        if r.tdoa_s is not None and r.sigma_tdoa_s:
            out.append((r.tdoa_s - tt) / r.sigma_tdoa_s)
        if r.fdoa_hz is not None and r.sigma_fdoa_hz:
            out.append((r.fdoa_hz - tf) / r.sigma_fdoa_hz)
    return out


def _scenario_world(cfg: SystemConfig, name: str, t_ns: int = T_DET) -> SimWorld:
    return SimWorld(cfg, load_scenario(SCENARIOS / f"{name}.yaml"), epoch_ns=t_ns)


def test_pacific_demo_end_to_end_matches_its_description(cfg: SystemConfig) -> None:
    world = _scenario_world(cfg, "pacific-demo")
    dets = _detect(world, cfg, T_DET)
    found = [(key[0], d) for key, ds in dets.items() for d in ds]
    assert len({ch for ch, _ in found}) == 19  # every channel's shared-1 window
    assert all(51.5 <= d.cn0_dbhz <= 56.5 and 12.0 <= d.snr_db <= 16.5 for _, d in found)
    (event,) = _events(cfg, dets)
    # transponders about 6 dB below saturation: hand budget of shared-1 on every satellite
    t_s = sec(T_DET)
    jam, ref = world.emitter("jammer-philsea"), world.emitter("ref-guam")
    for sat in cfg.satellites:
        tp = sat.transponder("shared-1")
        r = np.asarray(world.truth_orbit(sat.id).state_ecef(t_s)[0], dtype=np.float64)
        pfd_j = jam.eirp_dbw - 10 * math.log10(4 * math.pi * float(np.sum((r - jam.xyz) ** 2)))
        bore = cfg.orbits()[ref.pointing.satellite_id].state_ecef(t_s)[0] - ref.xyz
        off = math.degrees(math.acos(float(np.dot(bore, r - ref.xyz) / np.linalg.norm(bore) / np.linalg.norm(r - ref.xyz))))
        pfd_r = ref.eirp_dbw + float(gain_dbi(ref.antenna, off, ref.freq_hz)) - float(gain_dbi(ref.antenna, 0.0, ref.freq_hz)) \
            - 10 * math.log10(4 * math.pi * float(np.sum((r - ref.xyz) ** 2)))
        n0 = tp.eirp_sat_dbw - tp.sfd_dbw_m2 + 10 * math.log10(4 * math.pi * (tp.uplink_center_hz / C_LIGHT) ** 2) \
            - tp.g_over_t_dbk + _K_DBW
        total = sum(10 ** ((tp.eirp_sat_dbw - tp.sfd_dbw_m2 + p) / 10) for p in (pfd_j, pfd_r)) + 10 ** (n0 / 10) * tp.bandwidth_hz
        assert 5.0 < tp.eirp_sat_dbw - 10 * math.log10(total) < 8.0
    t_start = T_DET + 12 * NS
    cal = _run_event(cfg, world, event, t_start, "jammer-philsea", calibrate=True)
    assert len(cal.pairs) == 2 and {p["b"].split("-")[1] for p in cal.pairs} | {cal.pairs[0]["a"].split("-")[1]} == {"pac", "172e", "io"}
    assert all(38.0 <= p["result"].snr_db <= 41.5 for p in cal.pairs)  # "about 40 dB"
    assert all(abs(e) < 3.5 for e in _caf_errors(cal))
    assert all(p["calibrated"] == ["fdoa", "tdoa"] for p in cal.pairs)
    assert cal.solution is not None
    miss, nees = _score(cal.solution, 20.0, 132.0)
    assert 300.0 < miss < 3000.0 and nees < CHI2_999 and cal.solution.dominant_error == "reference"
    assert 2000.0 < cal.solution.ellipse["semi_major_m"] < 4500.0
    unc = _run_event(cfg, world, event, t_start, "jammer-philsea", calibrate=False)
    assert unc.solution is not None
    miss_u, nees_u = _nearest(unc.solution, 20.0, 132.0)
    assert miss_u < 3000.0 and nees_u < CHI2_999 and unc.solution.dominant_error == "ephemeris_position"


def test_clock_fault_is_removed_by_reference_calibration(cfg: SystemConfig) -> None:
    """The likely primaries both pair Naha across sites; uncalibrated the 10 us Naha
    clock error moves the fix by over 10 km and outside its ellipse, calibrated it is gone."""
    world = _scenario_world(cfg, "clock-fault")
    (event,) = _events(cfg, _detect(world, cfg, T_DET))
    t_s = sec(T_DET + 12 * NS)
    for primary in ("honolulu-pac", "naha-172e"):
        unit = next(u for u in event.units if u.channel_id == primary)
        sites = [(unit.site_id, site_b) for _, site_b, _, _ in _partners(cfg, unit, event.lo, event.hi, t_s)]
        assert any("naha" in pair and pair[0] != pair[1] for pair in sites), (primary, sites)
    t_start = T_DET + 12 * NS
    unc = _run_event(cfg, world, event, t_start, "jammer-philsea", calibrate=False, primary="honolulu-pac")
    assert all(abs(e) < 3.5 for e in _caf_errors(unc))  # the CAF measures the biased truth
    assert unc.solution is not None
    miss_u, nees_u = _nearest(unc.solution, 20.0, 132.0)
    assert miss_u > 8000.0 and nees_u > CHI2_95 and unc.solution.reduced_chi2 is not None and unc.solution.reduced_chi2 < 1.0
    cal = _run_event(cfg, world, event, t_start, "jammer-philsea", calibrate=True, primary="honolulu-pac")
    assert cal.solution is not None
    miss_c, nees_c = _score(cal.solution, 20.0, 132.0)
    assert miss_c < 3000.0 and nees_c < CHI2_999


def test_uplink_cw_gives_fdoa_only_and_needs_calibration_for_a_fix(cfg: SystemConfig) -> None:
    world = _scenario_world(cfg, "uplink-cw")
    dets = _detect(world, cfg, T_DET)
    found = [d for ds in dets.values() for d in ds]
    assert len(found) == 19 and all(35.0 <= d.cn0_dbhz <= 42.5 for d in found)
    (event,) = _events(cfg, dets)
    assert event.hi - event.lo < 200.0
    t_start = T_DET + 12 * NS
    unc = _run_event(cfg, world, event, t_start, "jammer-cw", calibrate=False)
    for p in unc.pairs:
        r = p["result"]
        assert r.detected and r.reason == "narrowband" and r.tdoa_s is None and r.fdoa_hz is not None
        assert r.snr_db > 30.0
    assert all(abs(e) < 3.5 for e in _caf_errors(unc))
    assert unc.solution is None and unc.reason == "unobservable"
    cal = _run_event(cfg, world, event, t_start, "jammer-cw", calibrate=True)
    assert all(p["result"].tdoa_s is None for p in cal.pairs)
    assert cal.solution is not None
    miss, nees = _score(cal.solution, 15.0, 130.0)
    assert nees < CHI2_999 and miss < 200e3 and cal.solution.ellipse["semi_major_m"] > 10 * cal.solution.ellipse["semi_minor_m"]


def test_uplink_swept_is_ambiguous_with_or_without_calibration(cfg: SystemConfig) -> None:
    world = _scenario_world(cfg, "uplink-swept")
    dets = _detect(world, cfg, T_DET)
    found = [d for ds in dets.values() for d in ds]
    assert len(found) == 19 and all(51.5 <= d.cn0_dbhz <= 56.5 for d in found)
    (event,) = _events(cfg, dets)
    for calibrate in (False, True):
        ev = _run_event(cfg, world, event, T_DET + 12 * NS, "jammer-swept", calibrate=calibrate)
        assert ev.solution is None
        for p in ev.pairs:
            r = p["result"]
            assert r.detected and r.reason == "ambiguous" and r.tdoa_s is None and len(r.candidates) >= 2


def test_local_interferer_is_heard_at_busan_only(cfg: SystemConfig) -> None:
    world = _scenario_world(cfg, "local-interferer")
    dets = _detect(world, cfg, T_DET)
    found = {key[0]: d for key, ds in dets.items() for d in ds}
    assert set(found) == {"busan-pac", "busan-io"}
    assert 69.0 <= found["busan-pac"].cn0_dbhz <= 71.5 and 58.5 <= found["busan-io"].cn0_dbhz <= 61.0
    for site in cfg.sites:
        for ch in site.channels:
            direct = world.direct_power_ratio_dbhz("local-busan", ch.id, sec(T_DET))
            assert (direct is not None) == (site.id == "busan")
    # its mapped uplink is relayed by no satellite: nothing correlates across satellites
    (event,) = _events(cfg, dets)
    ev = _run_event(cfg, world, event, T_DET + 12 * NS, None, calibrate=False)
    assert ev.solution is None and all(not p["result"].detected for p in ev.pairs)


def test_quiet_scenario_raises_no_detection(cfg: SystemConfig) -> None:
    for t in (T_DET, ns_from_iso("2026-01-01T09:00:00Z")):
        world = _scenario_world(cfg, "quiet", t)
        assert not any(_detect(world, cfg, t, n_frames=24).values())


def test_two_jammers_form_two_located_events(cfg: SystemConfig) -> None:
    world = _scenario_world(cfg, "two-jammers")
    events = _events(cfg, _detect(world, cfg, T_DET))
    assert len(events) == 2 and events[0].hi < events[1].lo
    for ev, (eid, lat, lon) in zip(events, (("jammer-a", 20.0, 132.0), ("jammer-b", 30.0, 135.0))):
        out = _run_event(cfg, world, ev, T_DET + 12 * NS, eid, calibrate=True)
        assert all(abs(e) < 3.5 for e in _caf_errors(out))
        assert out.solution is not None
        miss, nees = _score(out.solution, lat, lon)
        assert miss < 3000.0 and nees < CHI2_999


@pytest.mark.slow
def test_scenario_claims_hold_over_a_day(cfg: SystemConfig) -> None:
    """Four epochs (the primary channel and the geometry change with the time of day)."""
    inside = []
    for hour in (3, 9, 15, 21):
        t = ns_from_iso(f"2026-01-01T{hour:02d}:00:00Z")
        t_start = t + 12 * NS
        for name, eid, lat, lon in (("pacific-demo", "jammer-philsea", 20.0, 132.0),
                                    ("clock-fault", "jammer-philsea", 20.0, 132.0)):
            world = _scenario_world(cfg, name, t)
            (event,) = _events(cfg, _detect(world, cfg, t))
            cal = _run_event(cfg, world, event, t_start, eid, calibrate=True)
            assert cal.solution is not None
            miss, nees = _score(cal.solution, lat, lon)
            assert miss < 3000.0
            inside.append(nees <= CHI2_95)
            unc = _run_event(cfg, world, event, t_start, eid, calibrate=False)
            assert unc.solution is not None
            miss_u, _ = _nearest(unc.solution, lat, lon)
            cross_naha = any(p["a_path"].site_id != p["b_path"].site_id and "naha" in (p["a_path"].site_id, p["b_path"].site_id)
                             for p in unc.pairs)
            if name == "clock-fault" and cross_naha:
                assert miss_u > 8000.0
            elif name == "clock-fault":  # every pair at one site: clocks cancel
                assert all(p["a_path"].site_id == p["b_path"].site_id for p in unc.pairs) and miss_u < 5000.0
            else:
                assert miss_u < 5000.0
        world = _scenario_world(cfg, "uplink-swept", t)
        (event,) = _events(cfg, _detect(world, cfg, t))
        for calibrate in (False, True):
            ev = _run_event(cfg, world, event, t_start, "jammer-swept", calibrate=calibrate)
            assert ev.solution is None and all(p["result"].reason == "ambiguous" for p in ev.pairs)
        world = _scenario_world(cfg, "two-jammers", t)
        events = _events(cfg, _detect(world, cfg, t))
        assert len(events) == 2
        for ev, (eid, lat, lon) in zip(events, (("jammer-a", 20.0, 132.0), ("jammer-b", 30.0, 135.0))):
            out = _run_event(cfg, world, ev, t_start, eid, calibrate=True)
            assert out.solution is not None
            miss, nees = _score(out.solution, lat, lon)
            assert miss < 3000.0
            inside.append(nees <= CHI2_95)
        world = _scenario_world(cfg, "uplink-cw", t)
        (event,) = _events(cfg, _detect(world, cfg, t))
        out = _run_event(cfg, world, event, t_start, "jammer-cw", calibrate=True)
        assert all(p["result"].tdoa_s is None and p["result"].reason == "narrowband" for p in out.pairs)
        if out.solution is not None:
            inside.append(_score(out.solution, 15.0, 130.0)[1] <= CHI2_95)
    assert np.mean(inside) >= 0.8, inside


@pytest.mark.slow
def test_render_psd_matches_welch_on_every_scenario_window(cfg: SystemConfig) -> None:
    for path in sorted(SCENARIOS.glob("*.yaml")):
        world = SimWorld(cfg, load_scenario(path), epoch_ns=T_DET)
        for site in cfg.sites:
            for ch in site.channels:
                for mon in ch.monitors:
                    meas = np.zeros(mon.nfft)
                    exp = np.zeros(mon.nfft)
                    for r in range(2):
                        t = T_DET + r * 7_013_700_011
                        iq = world.render_iq(ch.id, t, mon.nfft * mon.navg, mon.span_hz, mon.center_hz)
                        meas += 10 ** (welch_psd_db(iq, mon.span_hz, mon.nfft, mon.navg) / 10)
                        exp += 10 ** (world.render_psd(ch.id, t, mon.center_hz, mon.span_hz, mon.nfft, mon.navg,
                                                       expected=True) / 10)
                    occupied = 10 * np.log10(exp / 2) > 3.0
                    for sel in (occupied, ~occupied):
                        if sel.sum() >= 16:
                            err = abs(10 * math.log10(meas[sel].mean() / exp[sel].mean()))
                            assert err < 0.5, (path.stem, ch.id, mon.id, err)


@pytest.mark.slow
def test_accuracy_map_agrees_with_the_full_chain_monte_carlo(cfg: SystemConfig) -> None:
    """The planning map at the true cell against run_trials (simulated IQ -> CAF ->
    calibration -> solve) with truth errors drawn from the configured uncertainty."""
    pairs = [("honolulu-pac", "naha-172e"), ("honolulu-pac", "seoul-io")]
    f_e = 301.005e6
    t_mid = DEFAULT_T_NS + round(1e9 * (20000 - 1) / (2 * 20000.0))
    m = accuracy_map(cfg, pairs, EmitterModel(eirp_dbw=20.0, bandwidth_hz=10e3, waveform="noise"), f_e, t_mid,
                     Region(lat_min=19.0, lat_max=21.0, lon_min=131.0, lon_max=133.0), 0.5, reference_carrier_id="ref-guam")
    i, j = list(np.round(m["lat_deg"], 6)).index(20.0), list(np.round(m["lon_deg"], 6)).index(132.0)
    a95, b95, az, cep = m["semi_major_95_m"][i][j], m["semi_minor_95_m"][i][j], m["azimuth_deg"][i][j], m["cep50_m"][i][j]
    k = math.sqrt(-2.0 * math.log(0.05))
    u, v = np.array([math.sin(math.radians(az)), math.cos(math.radians(az))]), np.array([math.cos(math.radians(az)), -math.sin(math.radians(az))])
    p_map = (a95 / k) ** 2 * np.outer(u, u) + (b95 / k) ** 2 * np.outer(v, v)
    sc = Scenario.model_validate({"name": "accmc", "seed": 1, "description": "simulated", "emitters": [
        {"id": "j", "lat_deg": 20.0, "lon_deg": 132.0, "antenna_height_m": 0.0, "freq_hz": f_e, "eirp_dbw": 20.0,
         "waveform": {"type": "noise", "bandwidth_hz": 10e3}}]})
    res = run_trials(cfg, sc, "j", 30, pairs=pairs, seed=11, draw_errors=True, use_calibration=True)
    solved = [t for t in res["trials"] if "miss_m" in t]
    assert len(solved) == 30
    # deterministic: the solver's covariance at its fix equals the map's at the cell
    assert abs(float(np.median([t["cep50_m"] for t in solved])) / cep - 1.0) < 0.02
    assert abs(float(np.median([t["semi_major_m"] for t in solved])) / a95 - 1.0) < 0.02
    # statistical: the actual misses follow the map's covariance (3-sigma bounds for n = 30)
    e = np.array([t["error_en_m"] for t in solved])
    nees = np.einsum("ij,jk,ik->i", e, np.linalg.inv(p_map), e)
    assert 0.9 < float(nees.mean()) < 3.1
    assert float(np.mean(nees <= CHI2_95)) >= 0.83


@pytest.mark.slow
def test_in_band_slices_correlate_on_a_wideband_configuration() -> None:
    """ku-adjacent: a 30 MHz noise jammer captured at 5 MHz is rendered as in-band
    slices in every channel; the slices of a pair correlate at the true TDOA/FDOA."""
    ku = load_config(ROOT / "config" / "ku-adjacent.yaml")
    f_e, fs, n, width = 14.250e9, 5e6, 262144, 30e6
    sc = Scenario.model_validate({"name": "ku", "seed": 9, "description": "simulated", "emitters": [
        {"id": "j", "lat_deg": 25.0, "lon_deg": 125.0, "freq_hz": f_e, "eirp_dbw": 45.0,
         "waveform": {"type": "noise", "bandwidth_hz": width}}],
        "errors": {"sat_osc_frac": {"ku-a": 2e-8, "ku-b": -1e-8}, "site_clock_offset_ns": {"manila": 25.0}}})
    world = SimWorld(ku, sc)
    a = PathSpec("ku-a", "tokyo", f_e, -2.3e9)
    b = PathSpec("ku-b", "manila", f_e, -2.3e9)
    assert {p.treatment for p in world.channel_paths("tokyo-ku-a", T_DET, a.f_tune_hz, fs) if p.emitter_id == "j"
            and p.treatment != "omitted"} == {"slice"}
    t_est = (T_DET + n // 2 * 200) / 1e9
    windows = _search(ku, measurement_windows(a, b, t_est, ku.orbits(), ku.sites_xyz(), ku.processing.region), a, b, (None, None))
    cross = _cross(ku, a, b, width, fs, t_est)
    mirror = None
    if cross is not None:
        mgeo = measurement_windows(cross[0], cross[1], t_est, ku.orbits(), ku.sites_xyz(), ku.processing.region)
        mirror = None if mgeo is None else _search(ku, mgeo, cross[0], cross[1], (None, None))
    result, t_mid = _correlate(ku, world, "tokyo-ku-a", "manila-ku-b", a, b, T_DET, n, fs, width, windows, mirror)
    tt, tf = world.truth_measurement("j", a, b, t_mid)
    assert result.tdoa_s is not None and result.fdoa_hz is not None and result.snr_db > 20.0
    assert abs(result.tdoa_s - tt) < 3.5 * result.sigma_tdoa_s and abs(result.fdoa_hz - tf) < 3.5 * result.sigma_fdoa_hz


# =========================================================================== sim driver


async def test_sim_driver_follows_a_configuration_change_at_its_effective_time(cfg: SystemConfig) -> None:
    """A second backplane version (another scenario, with a velocity ephemeris error and
    no t_ref, and a configuration without tokyo-io) takes over at its effective time:
    earlier intervals still come from version 1, later ones from version 2 built with
    epoch_ns = effective_ns (as the server's truth must be), and tokyo-io fails then."""
    t_now = ns_from_iso("2026-01-01T03:00:00Z")
    e1, e2 = t_now - 100 * NS, t_now + 2 * NS
    sc1 = Scenario.model_validate({"name": "v1", "seed": 4, "description": "simulated", "emitters": [
        {"id": "j", "lat_deg": 20.0, "lon_deg": 132.0, "freq_hz": 301.005e6, "eirp_dbw": 20.0,
         "waveform": {"type": "noise", "bandwidth_hz": 10e3}}]})
    sc2 = Scenario.model_validate({"name": "v2", "seed": 4, "description": "simulated", "emitters": [
        {"id": "j", "lat_deg": 20.0, "lon_deg": 132.0, "freq_hz": 301.005e6, "eirp_dbw": 26.0,
         "waveform": {"type": "noise", "bandwidth_hz": 10e3}}],
        "errors": {"ephemeris": {"muos-pac": {"ric_pos_m": [0.0, 300.0, 0.0], "ric_vel_mps": [0.0, 0.5, 0.0]}}}})
    data = cfg.export_dict()
    for site in data["sites"]:
        site["channels"] = [c for c in site["channels"] if c["id"] != "tokyo-io"]
    cfg2 = SystemConfig.model_validate(data)

    def doc(version: int, effective: int, sc: Scenario, config: SystemConfig) -> bytes:
        return json.dumps({"version": version, "effective_ns": str(effective), "config": config.export_dict(),
                           "scenario": sc.model_dump(mode="json")}).encode()

    served = {"doc": doc(1, e1, sc1, cfg)}
    seen_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, content=served["doc"], headers={"content-type": "application/json"})

    clock = ManualClock(t_now)
    source = SimWorldSource("http://backplane.test/node/v1/sim", "tok", transport=httpx.MockTransport(handler),
                            retry_base_s=0.05, retry_max_s=0.2)
    ctx = DriverContext(site_id="tokyo", server_http_url="http://backplane.test", token="tok",
                        simulation={"enabled": True, "scenario_url": "/node/v1/sim", "version": 1}, clock=clock)
    fc, fs = 260.005e6, 31250.0
    drivers = {}
    for ch in ("tokyo-pac", "tokyo-io"):
        driver = SimDriver(source=source, clock=clock, proxy=None, world_wait_s=2.0, refresh_timeout_s=3.0)
        await driver.open(ChannelAssignment(channel_id=ch, antenna_summary="simulated", monitors=[
            MonitorAssignment(monitor_id="m0", center_hz=fc, span_hz=fs, nfft=256, navg=4, frame_rate_hz=2.0)]), ctx)
        drivers[ch] = driver
    try:
        served["doc"] = doc(2, e2, sc2, cfg2)
        await drivers["tokyo-pac"].on_sim_update(2, e2)
        assert drivers["tokyo-pac"].world_version == 2 and set(seen_auth) == {"Bearer tok"}
        clock.set(e2 + 5 * NS)
        w1, w2 = SimWorld(cfg, sc1, epoch_ns=e1), SimWorld(cfg2, sc2, epoch_ns=e2)
        before, after = e2 - 600_000_000, e2 + 300_000_000
        got = await asyncio.gather(drivers["tokyo-pac"].read(before, 4096, fs, fc),
                                   drivers["tokyo-pac"].read(after, 4096, fs, fc),
                                   drivers["tokyo-io"].read(before, 4096, fs, fc))
        assert np.array_equal(got[0].iq, w1.render_iq("tokyo-pac", before, 4096, fs, fc))
        assert np.array_equal(got[1].iq, w2.render_iq("tokyo-pac", after, 4096, fs, fc))
        assert not np.array_equal(got[1].iq, w1.render_iq("tokyo-pac", after, 4096, fs, fc))
        assert np.array_equal(got[2].iq, w1.render_iq("tokyo-io", before, 4096, fs, fc))
        with pytest.raises(CaptureError) as info:
            await drivers["tokyo-io"].read(after, 4096, fs, fc)
        assert info.value.code == "driver"
        psd = await drivers["tokyo-pac"].psd(after, fc, fs, 256, 4)
        assert np.array_equal(psd, w2.render_psd("tokyo-pac", after, fc, fs, 256, 4))
    finally:
        for driver in drivers.values():
            await driver.close()
        await source.aclose()


def test_run_trials_accepts_a_growing_ephemeris_error_without_t_ref(cfg_bare: SystemConfig) -> None:
    """The Monte Carlo world takes its epoch from t_ns (or DEFAULT_T_NS), so a scenario
    whose ephemeris error grows from an unstated reference time can be run."""
    sc = Scenario.model_validate({"name": "vel", "seed": 2, "description": "simulated", "emitters": [
        {"id": "j", "lat_deg": 20.0, "lon_deg": 132.0, "antenna_height_m": 0.0, "freq_hz": 301.005e6, "eirp_dbw": 20.0,
         "waveform": {"type": "noise", "bandwidth_hz": 10e3}}],
        "errors": {"ephemeris": {"muos-pac": {"ric_pos_m": [0.0, 200.0, 0.0], "ric_vel_mps": [0.0, 0.01, 0.0]}}}})
    with pytest.raises(ValueError, match="t_ref"):
        SimWorld(cfg_bare, sc)
    res = run_trials(cfg_bare, sc, "j", 1, pairs=[("honolulu-pac", "naha-172e"), ("honolulu-pac", "seoul-io")],
                     use_calibration=False, capture_s=0.5)
    assert res["simulated"] is True and res["summary"]["n_solved"] == 1
