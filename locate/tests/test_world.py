"""Tests of the simulated world (sim/world.py): MATH.md section 4 signal model, section 12
items 5 and 6, PSD synthesis, paths, power robbing, determinism and truth measurements.

Every numeric check compares against an independent calculation: the geometry
predictors on the true orbits, formulas of the link budget written out here, the CAF
estimator on rendered IQ, ``welch_psd_db`` of rendered IQ, or exact per-sample phases.
All randomness is keyed by the scenario seed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from petal_locate.config.loader import load_config
from petal_locate.config.models import SystemConfig
from petal_locate.core.antennas import gain_dbi
from petal_locate.core.caf import caf_estimate
from petal_locate.core.channel import thermal_noise
from petal_locate.core.constants import C, K_DBW
from petal_locate.core.crlb import post_correlation_snr, stein_sigmas
from petal_locate.core.geodesy import angle_between_deg, destination, look_angles
from petal_locate.core.geometry import (
    bent_pipe,
    measurement_windows,
    predict_fdoa,
    predict_tdoa,
    search_windows,
)
from petal_locate.core.iono import group_delay_s
from petal_locate.core.orbits import PerturbedOrbit, orbit_position
from petal_locate.core.spectrum import welch_psd_db
from petal_locate.core.timebase import iso_from_ns, sec
from petal_locate.core.types import PathSpec
from petal_locate.sim import Scenario, ScenarioError, SimWorld, load_scenario

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "pacific-uhf.yaml"
SCENARIOS = ROOT / "config" / "scenarios"
T0 = 1_794_000_000 * 10**9 + 123_456_789  # labelled first-sample time used throughout (Nov 2026)
F_J = 301.005e6  # jammer uplink frequency, inside shared-1 of all three satellites
FS = 31250.0
N = 31250


# --------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def cfg_bare(cfg: SystemConfig) -> SystemConfig:
    """pacific-uhf without its carriers (so a capture holds the test emitter only)."""
    data = cfg.export_dict()
    data["carriers"] = []
    return SystemConfig.model_validate(data)


def jammer(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": "j1",
        "lat_deg": 20.0,
        "lon_deg": 132.0,
        "freq_hz": F_J,
        "eirp_dbw": 20.0,
        "waveform": {"type": "noise", "bandwidth_hz": 10e3},
    }
    spec.update(overrides)
    return spec


def scenario(emitters: list[dict[str, Any]], errors: dict[str, Any] | None = None, seed: int = 1) -> Scenario:
    return Scenario.model_validate({"name": "test", "seed": seed, "emitters": emitters, "errors": errors or {}})


def translation(cfg: SystemConfig, sat_id: str, tp_id: str = "shared-1") -> float:
    return cfg.satellite(sat_id).transponder(tp_id).translation_hz


def pair(cfg: SystemConfig, f_e: float = F_J) -> tuple[PathSpec, PathSpec]:
    """Honolulu on muos-pac (A) and Tokyo on uhf-172e (B): neither channel hears a second
    copy (Honolulu cannot see muos-io; Tokyo's 172 E window holds no MUOS downlink)."""
    return (
        PathSpec("muos-pac", "honolulu", f_e, translation(cfg, "muos-pac")),
        PathSpec("uhf-172e", "tokyo", f_e, translation(cfg, "uhf-172e")),
    )


def t_mid_of(t_b_ns: int, n: int, fs: float) -> int:
    return t_b_ns + round(1e9 * (n - 1) / (2.0 * fs))


def windows(cfg: SystemConfig, a: PathSpec, b: PathSpec, t_mid_ns: int) -> tuple[tuple[float, float], tuple[float, float]]:
    geo = measurement_windows(a, b, sec(t_mid_ns), cfg.orbits(), cfg.sites_xyz(), cfg.processing.region)
    return search_windows(geo, a, b, cfg.processing.caf, cfg.resolved_uncertainty())


def predicted(cfg: SystemConfig, x_e: np.ndarray, a: PathSpec, b: PathSpec, t_mid_ns: int,
              orbits: dict[str, Any] | None = None) -> tuple[float, float]:
    orbits = cfg.orbits() if orbits is None else orbits
    t = sec(t_mid_ns)
    return (float(predict_tdoa(x_e, a, b, t, orbits, cfg.sites_xyz())),
            float(predict_fdoa(x_e, a, b, t, orbits, cfg.sites_xyz())))


def stein_bound(world: SimWorld, a_ch: str, b_ch: str, a: PathSpec, b: PathSpec, t_mid_ns: int, result: Any,
                emitter_id: str = "j1") -> tuple[float, float]:
    """Stein sigmas from the link budget (independent of the estimator's own sigma)."""
    t_s = sec(t_mid_ns)
    pa = world.path_budget(emitter_id, a.sat_id, "shared-1", a_ch, t_s)
    pb = world.path_budget(emitter_id, b.sat_id, "shared-1", b_ch, t_s)
    assert pa is not None and pb is not None
    bw, t_int = result.noise_bandwidth_hz, result.integration_s
    g1 = 10.0 ** ((float(pa.cn0_total_dbhz) - 10.0 * math.log10(bw)) / 10.0)
    g2 = 10.0 ** ((float(pb.cn0_total_dbhz) - 10.0 * math.log10(bw)) / 10.0)
    rms = world.emitter(emitter_id).waveform.rms_bandwidth_hz
    return stein_sigmas(bw, rms, t_int, g1, g2)


def lin(db: Any) -> np.ndarray:
    return 10.0 ** (np.asarray(db, dtype=np.float64) / 10.0)


# --------------------------------------------------------------------------- MATH 12.5


def test_math_12_5_caf_round_trip(cfg_bare: SystemConfig) -> None:
    """An IQ capture of a noise emitter through two satellites, estimated by the CAF,
    returns the section 5 predictions within 3 sigma of the Stein bound; swapping A and B
    flips both signs; A starting 0.38 ms late or B 0.52 ms late leaves tdoa_s unchanged."""
    world = SimWorld(cfg_bare, scenario([jammer()]))
    a, b = pair(cfg_bare)
    a_ch, b_ch = "honolulu-pac", "tokyo-172e"
    x_e = world.emitter("j1").xyz
    t_mid = t_mid_of(T0, N, FS)
    pred_t, pred_f = predicted(cfg_bare, x_e, a, b, t_mid)
    dw, fw = windows(cfg_bare, a, b, t_mid)

    ya = world.render_iq(a_ch, T0, N, FS, a.f_tune_hz)
    yb = world.render_iq(b_ch, T0, N, FS, b.f_tune_hz)
    r = caf_estimate(ya, yb, FS, delay_window=dw, doppler_window=fw, signal_bw_hz=10e3)
    assert r.tdoa_s is not None and r.fdoa_hz is not None, r.reason
    sig_t, sig_f = stein_bound(world, a_ch, b_ch, a, b, t_mid, r)
    assert abs(r.tdoa_s - pred_t) < 3.0 * sig_t
    assert abs(r.fdoa_hz - pred_f) < 3.0 * sig_f
    # the amplitudes are those of the link budget: post-correlation SNR B T C
    pa = world.path_budget("j1", "muos-pac", "shared-1", a_ch, sec(t_mid))
    pb = world.path_budget("j1", "uhf-172e", "shared-1", b_ch, sec(t_mid))
    bw = r.noise_bandwidth_hz
    g1 = lin(float(pa.cn0_total_dbhz) - 10 * math.log10(bw))
    g2 = lin(float(pb.cn0_total_dbhz) - 10 * math.log10(bw))
    assert r.snr_db == pytest.approx(10 * math.log10(post_correlation_snr(bw, r.integration_s, g1, g2)), abs=1.5)

    swapped = caf_estimate(yb, ya, FS, delay_window=(-dw[1], -dw[0]), doppler_window=(-fw[1], -fw[0]),
                           signal_bw_hz=10e3)
    assert abs(swapped.tdoa_s + r.tdoa_s) < 3.0 * sig_t
    assert abs(swapped.fdoa_hz + r.fdoa_hz) < 3.0 * sig_f

    late_a = 380_000
    ya_late = world.render_iq(a_ch, T0 + late_a, N, FS, a.f_tune_hz)
    r_a = caf_estimate(ya_late, yb, FS, delay_window=dw, doppler_window=fw, signal_bw_hz=10e3,
                       t0_offset_s=late_a * 1e-9)
    assert abs(r_a.tdoa_s - r.tdoa_s) < 3.0 * math.sqrt(2.0) * sig_t
    assert abs(r_a.tdoa_s - pred_t) < 3.0 * sig_t

    late_b = 520_000
    yb_late = world.render_iq(b_ch, T0 + late_b, N, FS, b.f_tune_hz)
    r_b = caf_estimate(ya, yb_late, FS, delay_window=dw, doppler_window=fw, signal_bw_hz=10e3,
                       t0_offset_s=-late_b * 1e-9)
    pred_t_b, pred_f_b = predicted(cfg_bare, x_e, a, b, t_mid_of(T0 + late_b, N, FS))
    assert abs(r_b.tdoa_s - r.tdoa_s) < 3.0 * math.sqrt(2.0) * sig_t
    assert abs(r_b.tdoa_s - pred_t_b) < 3.0 * sig_t
    assert abs(r_b.fdoa_hz - pred_f_b) < 3.0 * sig_f


def test_caf_measures_truth_with_all_errors(cfg_bare: SystemConfig) -> None:
    """With a 2 us clock offset, a satellite oscillator error and an ephemeris offset the
    CAF of rendered IQ lands on truth_measurement (MATH 12 item 5 'agree with what the CAF
    measures'), far from the error-free prediction (MATH 12 item 6 through the estimator)."""
    eps = 1e-6
    errors = {
        "site_clock_offset_ns": {"honolulu": 2000.0},
        "sat_osc_frac": {"muos-pac": eps},
        "ephemeris": {"uhf-172e": {"ric_pos_m": [300.0, 1500.0, -1000.0]}},
    }
    world = SimWorld(cfg_bare, scenario([jammer()], errors))
    a, b = pair(cfg_bare)
    t_mid = t_mid_of(T0, N, FS)
    tr_t, tr_f = world.truth_measurement("j1", a, b, t_mid)
    p0_t, p0_f = predicted(cfg_bare, world.emitter("j1").xyz, a, b, t_mid)
    dw, fw = windows(cfg_bare, a, b, t_mid)
    ya = world.render_iq("honolulu-pac", T0, N, FS, a.f_tune_hz)
    yb = world.render_iq("tokyo-172e", T0, N, FS, b.f_tune_hz)
    r = caf_estimate(ya, yb, FS, delay_window=dw, doppler_window=fw, signal_bw_hz=10e3)
    sig_t, sig_f = stein_bound(world, "honolulu-pac", "tokyo-172e", a, b, t_mid, r)
    assert abs(r.tdoa_s - tr_t) < 3.0 * sig_t
    assert abs(r.fdoa_hz - tr_f) < 3.0 * sig_f
    # the clock alone moves the TDOA by -2 us and the oscillator the FDOA by eps T_nom
    assert abs((r.tdoa_s - p0_t) - (tr_t - p0_t)) < 3.0 * sig_t
    assert abs(tr_t - p0_t) > 20.0 * sig_t
    assert abs((r.fdoa_hz - p0_f) - (tr_f - p0_f)) < 3.0 * sig_f
    assert abs(tr_f - p0_f) > 20.0 * sig_f


# --------------------------------------------------------------------------- MATH 12.6 and truth


def test_math_12_6_clock_offset_lowers_tdoa(cfg_bare: SystemConfig) -> None:
    """A positive clock offset delta_a lowers TDOA_AB by delta_a (and delta_b raises it)."""
    a, b = pair(cfg_bare)
    base = SimWorld(cfg_bare, scenario([jammer()]))
    t0, f0 = base.truth_measurement("j1", a, b, T0)
    for site, sign in (("honolulu", -1.0), ("tokyo", 1.0)):
        for delta_ns in (100.0, 2000.0):
            w = SimWorld(cfg_bare, scenario([jammer()], {"site_clock_offset_ns": {site: delta_ns}}))
            t1, f1 = w.truth_measurement("j1", a, b, T0)
            assert t1 - t0 == pytest.approx(sign * delta_ns * 1e-9, abs=1e-12)
            assert abs(f1 - f0) < 1e-6  # the Doppler moves by its rate times delta: negligible


def test_math_12_6_satellite_oscillator_raises_fdoa(cfg_bare: SystemConfig) -> None:
    """Raising the satellite oscillator error of satellite i by eps raises FDOA_AB by
    eps * T_nom,i (and lowers it by eps * T_nom,j for the satellite of path B)."""
    a, b = pair(cfg_bare)
    base = SimWorld(cfg_bare, scenario([jammer()], {"sat_osc_frac": {"muos-pac": 2e-7, "uhf-172e": -1e-7}}))
    t0, f0 = base.truth_measurement("j1", a, b, T0)
    eps = 5e-7
    w_i = SimWorld(cfg_bare, scenario([jammer()], {"sat_osc_frac": {"muos-pac": 2e-7 + eps, "uhf-172e": -1e-7}}))
    t_i, f_i = w_i.truth_measurement("j1", a, b, T0)
    # the scale term (f_e + T) tau_d_dot changes by eps T tau_d_dot: tau_d_dot < 1e-5
    assert f_i - f0 == pytest.approx(eps * a.translation_hz, abs=abs(eps * a.translation_hz) * 1e-5 + 1e-7)
    assert t_i == pytest.approx(t0, abs=1e-15)
    w_j = SimWorld(cfg_bare, scenario([jammer()], {"sat_osc_frac": {"muos-pac": 2e-7, "uhf-172e": -1e-7 + eps}}))
    _, f_j = w_j.truth_measurement("j1", a, b, T0)
    assert f_j - f0 == pytest.approx(-eps * b.translation_hz, abs=abs(eps * b.translation_hz) * 1e-5 + 1e-7)


def test_site_oscillator_moves_fdoa_by_tuning_error(cfg_bare: SystemConfig) -> None:
    """A site oscillator error eps_g retunes the channel to f_tune (1 + eps_g): the FDOA
    falls by eps_g f_tune,A (MATH section 4)."""
    a, b = pair(cfg_bare)
    t0, f0 = SimWorld(cfg_bare, scenario([jammer()])).truth_measurement("j1", a, b, T0)
    eps_g = 3e-9
    t1, f1 = SimWorld(cfg_bare, scenario([jammer()], {"site_osc_frac": {"honolulu": eps_g}})).truth_measurement(
        "j1", a, b, T0)
    assert f1 - f0 == pytest.approx(-eps_g * a.f_tune_hz, rel=1e-9)
    assert t1 == pytest.approx(t0, abs=1e-15)


@pytest.mark.parametrize(
    "sats_sites",
    [
        (("muos-pac", "honolulu"), ("uhf-172e", "tokyo")),
        (("muos-pac", "seoul"), ("muos-io", "naha")),  # co-channel satellites
        (("uhf-172e", "manila"), ("muos-io", "manila")),  # one site
        (("muos-io", "busan"), ("muos-pac", "hilo")),
    ],
)
def test_truth_equals_predictors_with_zero_errors(cfg_bare: SystemConfig, sats_sites: Any) -> None:
    """With no truth errors, truth_measurement equals the geometry predictors (MATH section 5)."""
    (sa, ga), (sb, gb) = sats_sites
    world = SimWorld(cfg_bare, scenario([jammer()]))
    a = PathSpec(sa, ga, F_J, translation(cfg_bare, sa))
    b = PathSpec(sb, gb, F_J, translation(cfg_bare, sb))
    x_e = world.emitter("j1").xyz
    for k in range(4):
        t = T0 + k * 3 * 3600 * 10**9 + 7
        tr_t, tr_f = world.truth_measurement("j1", a, b, t)
        p_t, p_f = predicted(cfg_bare, x_e, a, b, t)
        assert tr_t == pytest.approx(p_t, abs=1e-12)
        assert tr_f == pytest.approx(p_f, abs=1e-5)


def test_truth_with_ephemeris_offset_equals_predictors_on_perturbed_orbits(cfg_bare: SystemConfig) -> None:
    """truth_measurement on a satellite displaced by an ephemeris error equals the
    predictors evaluated on PerturbedOrbit, and moves by the projection of the
    displacement on the up- and downlink lines of sight."""
    ric_pos = [500.0, -2000.0, 1500.0]
    ric_vel = [0.01, -0.02, 0.03]
    t_ref = T0 - 3600 * 10**9
    errors = {"ephemeris": {"muos-pac": {"ric_pos_m": ric_pos, "ric_vel_mps": ric_vel, "t_ref": iso_from_ns(t_ref)}}}
    world = SimWorld(cfg_bare, scenario([jammer()], errors))
    a, b = pair(cfg_bare)
    x_e = world.emitter("j1").xyz
    orbits = cfg_bare.orbits()
    truth_orbits = dict(orbits)
    truth_orbits["muos-pac"] = PerturbedOrbit(orbits["muos-pac"], ric_pos, ric_vel, sec(t_ref))
    tr_t, tr_f = world.truth_measurement("j1", a, b, T0)
    p_t, p_f = predicted(cfg_bare, x_e, a, b, T0, truth_orbits)
    assert tr_t == pytest.approx(p_t, abs=1e-12)
    assert tr_f == pytest.approx(p_f, abs=1e-5)
    # first-order: the change of D_A is the displacement projected on both lines of sight
    t_s = sec(T0)
    r0 = orbit_position(orbits["muos-pac"], t_s)
    r1 = orbit_position(truth_orbits["muos-pac"], t_s)
    x_g = cfg_bare.site_xyz("honolulu")
    u_up = (r0 - x_e) / np.linalg.norm(r0 - x_e)
    u_down = (r0 - x_g) / np.linalg.norm(r0 - x_g)
    expected_shift = float(np.dot(r1 - r0, u_up + u_down)) / C
    zero_t, _ = predicted(cfg_bare, x_e, a, b, T0)
    assert abs(expected_shift) > 1e-6
    assert tr_t - zero_t == pytest.approx(expected_shift, rel=2e-3)
    assert world.truth_orbit("muos-pac") is not orbits["muos-pac"]


def test_velocity_ephemeris_error_needs_a_reference_time(cfg_bare: SystemConfig) -> None:
    errors = {"ephemeris": {"muos-pac": {"ric_pos_m": [0.0, 0.0, 0.0], "ric_vel_mps": [0.0, 0.01, 0.0]}}}
    with pytest.raises(ScenarioError, match=r"errors\.ephemeris\.muos-pac\.t_ref"):
        SimWorld(cfg_bare, scenario([jammer()], errors))
    world = SimWorld(cfg_bare, scenario([jammer()], errors), epoch_ns=T0)
    offset = orbit_position(world.truth_orbit("muos-pac"), sec(T0) + 100.0) - orbit_position(
        cfg_bare.orbits()["muos-pac"], sec(T0) + 100.0)
    assert float(np.linalg.norm(offset)) == pytest.approx(1.0, rel=1e-6)  # 0.01 m/s for 100 s


def test_ionosphere_adds_the_differential_group_delay(cfg_bare: SystemConfig) -> None:
    """With the ionosphere on, the TDOA grows by the difference of the two paths' thin-shell
    group delays (uplink at f_e from the emitter, downlink at f_e + T from the site)."""
    a, b = pair(cfg_bare)
    vtec = 40.0
    off = SimWorld(cfg_bare, scenario([jammer()]))
    on = SimWorld(cfg_bare, scenario([jammer()], {"iono": {"enabled": True, "vtec_tecu": vtec}}))
    t_off, _ = off.truth_measurement("j1", a, b, T0)
    t_on, _ = on.truth_measurement("j1", a, b, T0)
    x_e = off.emitter("j1").xyz
    orbits = cfg_bare.orbits()

    def path_iono(p: PathSpec) -> float:
        r = orbit_position(orbits[p.sat_id], sec(T0))
        _, el_up, _ = look_angles(x_e, r)
        _, el_down, _ = look_angles(cfg_bare.site_xyz(p.site_id), r)
        return float(group_delay_s(vtec, el_up, p.f_e_hz) + group_delay_s(vtec, el_down, p.f_tune_hz))

    expected = path_iono(a) - path_iono(b)
    assert abs(expected) > 1e-8
    assert t_on - t_off == pytest.approx(expected, abs=2e-11)


# --------------------------------------------------------------------------- signal model, sample by sample


def test_rendered_phase_and_amplitude_follow_math_section_4(cfg_bare: SystemConfig) -> None:
    """A CW path, minus the known thermal noise, has exactly the MATH section 4 phase
    (clock offset, site and satellite oscillators and ephemeris error included) at every
    sample, and the link-budget amplitude written out here."""
    data = cfg_bare.export_dict()
    for tp in data["satellites"][0]["transponders"]:  # muos-pac: make its own noise negligible
        tp["g_over_t_dbk"] = 60.0  # relayed noise 74 dB below normal, 95 dB below the CW
    quiet_cfg = SystemConfig.model_validate(data)
    delta, eps_g, eps_sat = 37e-9, 3e-9, 2e-7
    ric = [200.0, -900.0, 600.0]
    errors = {
        "site_clock_offset_ns": {"honolulu": delta * 1e9},
        "site_osc_frac": {"honolulu": eps_g},
        "sat_osc_frac": {"muos-pac": eps_sat},
        "ephemeris": {"muos-pac": {"ric_pos_m": ric}},
    }
    eirp = 25.0
    world = SimWorld(quiet_cfg, scenario([jammer(waveform={"type": "cw"}, eirp_dbw=eirp)], errors))
    t_nom = translation(quiet_cfg, "muos-pac")
    fc = F_J + t_nom + 1234.5
    y = world.render_iq("honolulu-pac", T0, N, FS, fc).astype(np.complex128)
    y -= thermal_noise(N, f"thermal|1|honolulu-pac|{T0}|{FS}|{fc}")

    # exact phase from the light-time functions at every sample
    orbit = PerturbedOrbit(quiet_cfg.orbits()["muos-pac"], ric, [0.0, 0.0, 0.0], 0.0)
    x_e = world.emitter("j1").xyz
    x_g = quiet_cfg.site_xyz("honolulu")
    n = np.arange(N) / FS
    bp = bent_pipe(x_e, orbit, x_g, float(T0 // 10**9), 0.0, (T0 % 10**9) * 1e-9 + n + delta)
    t_true = t_nom * (1.0 + eps_sat)
    f_off = (F_J + t_nom - fc) + t_nom * eps_sat - fc * eps_g
    q = Fraction(f_off) * T0 / 10**9
    phase0 = float(q - math.floor(q))
    phi = phase0 + f_off * (n + delta) - (F_J + t_true) * bp.tau_d - F_J * bp.tau_u
    err_cycles = np.angle(y * np.exp(-2j * np.pi * (phi - np.rint(phi)))) / (2.0 * np.pi)
    assert float(np.max(np.abs(err_cycles))) < 1e-4

    # amplitude: EIRP_c = EIRP_sat - SFD + PFD; C_rx = EIRP_c - FSPL + G(off-axis) - L_pol
    tp = quiet_cfg.satellite("muos-pac").transponder("shared-1")
    t_mid = sec(T0) + 0.5
    r = orbit_position(orbit, t_mid)
    rho_u = float(np.linalg.norm(r - x_e))
    pfd = eirp - 10.0 * math.log10(4.0 * math.pi * rho_u**2)
    eirp_c = tp.eirp_sat_dbw - tp.sfd_dbw_m2 + pfd
    assert eirp_c < tp.eirp_sat_dbw - 1.0  # linear: no power robbing
    f_d = F_J + t_true
    rho_d = float(np.linalg.norm(r - x_g))
    fspl = 20.0 * math.log10(4.0 * math.pi * rho_d * f_d / C)
    believed = orbit_position(quiet_cfg.orbits()["muos-pac"], t_mid)
    off_axis = float(angle_between_deg(believed - x_g, r - x_g))
    _, channel = quiet_cfg.channel("honolulu-pac")
    c_rx = eirp_c - fspl + float(gain_dbi(channel.antenna, off_axis, f_d)) - channel.polarization_loss_db
    n0 = K_DBW + 10.0 * math.log10(channel.system_noise_temp_k)
    amplitude = math.sqrt(10.0 ** ((c_rx - n0) / 10.0) / FS)
    assert float(np.mean(np.abs(y))) == pytest.approx(amplitude, rel=1e-3)


def test_emitter_on_off_times_gate_the_samples(cfg_bare: SystemConfig) -> None:
    """t_start is honoured at the emitter: the signal appears at labelled time
    t_start + D - delta in the capture (D from bent_pipe), not before."""
    t_on = T0 + 200_000_000
    world = SimWorld(cfg_bare, scenario([jammer(t_start=iso_from_ns(t_on))]))
    assert [e.id for e in world.emitters_at(t_on - 1)] == []
    assert [e.id for e in world.emitters_at(t_on)] == ["j1"]
    y = world.render_iq("honolulu-pac", T0, N, FS, F_J + translation(cfg_bare, "muos-pac"))
    orbit = cfg_bare.orbits()["muos-pac"]
    d = float(bent_pipe(world.emitter("j1").xyz, orbit, cfg_bare.site_xyz("honolulu"), sec(t_on) + 0.25).D)
    k_on = int((t_on - T0) * 1e-9 * FS + d * FS)
    before = float(np.mean(np.abs(y[: k_on - 200]) ** 2))
    after = float(np.mean(np.abs(y[k_on + 200:]) ** 2))
    assert before < 2.0  # thermal plus relayed transponder noise
    assert after > 10.0 * before
    with pytest.raises(ValueError, match="t_stop"):
        scenario([jammer(t_start=iso_from_ns(t_on), t_stop=iso_from_ns(t_on - 1))])


# --------------------------------------------------------------------------- PSD


def _psd_case(cfg: SystemConfig, emitters: list[dict[str, Any]], channel: str, fc: float, span: float,
              nfft: int, guard_hz: float = 0.0) -> tuple[float, float, float, float]:
    world = SimWorld(cfg, scenario(emitters))
    navg = 8
    expected = world.render_psd(channel, T0, fc, span, nfft, navg, expected=True)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / span))
    occupied = expected > 3.0
    empty = expected < 0.25  # within 0.25 dB of the thermal reference (chirp skirts are modelled)
    if guard_hz > 0.0:  # optionally stay away from every relayed jammer edge
        for path in world.channel_paths(channel, T0, fc, span):
            if path.emitter_id == "j1" and path.treatment != "omitted":
                empty &= np.abs(freqs - path.f_bb_hz) > 0.5 * path.bandwidth_hz + guard_hz
    assert occupied.sum() > 20 and empty.sum() > 20
    out = []
    for k in range(2):
        t = T0 + k * 700_000_000
        iq = welch_psd_db(world.render_iq(channel, t, nfft * navg, span, fc), span, nfft, navg)
        ps = world.render_psd(channel, t, fc, span, nfft, navg)
        out.append([10 * math.log10(lin(iq[occupied]).mean()), 10 * math.log10(lin(ps[occupied]).mean()),
                    10 * math.log10(lin(iq[empty]).mean()), 10 * math.log10(lin(ps[empty]).mean())])
    res = np.mean(out, axis=0)
    return float(res[0]), float(res[1]), float(res[2]), float(res[3])


MON_172E = 301.0e6 - 33.6e6  # the shared-1 monitor centre of a channel on uhf-172e


@pytest.mark.parametrize(
    "name, emitters, channel, fc, span, nfft, guard",
    [
        ("noise", [jammer()], "seoul-172e", MON_172E, 31250.0, 1024, 0.0),
        ("cw", [jammer(waveform={"type": "cw"}, eirp_dbw=0.0)], "seoul-172e", MON_172E, 31250.0, 1024, 0.0),
        ("psk", [jammer(waveform={"type": "psk", "symbol_rate_hz": 6000})], "seoul-172e", MON_172E, 31250.0, 1024, 0.0),
        ("chirp", [jammer(waveform={"type": "chirp", "bandwidth_hz": 15e3, "sweep_period_s": 0.01})],
         "seoul-172e", MON_172E, 31250.0, 1024, 0.0),
        ("gated", [jammer(waveform={"type": "noise", "bandwidth_hz": 10e3, "duty": 0.3, "period_s": 0.05})],
         "seoul-172e", MON_172E, 31250.0, 1024, 0.0),
        ("slice", [jammer(freq_hz=301.0e6, eirp_dbw=25.0, waveform={"type": "noise", "bandwidth_hz": 200e3})],
         "seoul-172e", MON_172E, 31250.0, 1024, 0.0),
        # ref-guam (8 kHz at 300.9925 MHz) would fill the left of this 20 kHz window, leaving too few
        # empty bins to compare; it is switched off here (a scenario entry naming its carrier)
        ("decimated", [jammer(freq_hz=301.011e6), {"id": "guam-off", "kind": "carrier", "carrier_id": "ref-guam",
                                                   "enabled": False}],
         "seoul-172e", MON_172E, 20000.0, 512, 0.0),
        ("co-channel", [jammer()], "tokyo-pac", 301.0e6 - 41.0e6, 31250.0, 1024, 0.0),
        ("direct", [jammer(kind="local", freq_hz=260.005e6, eirp_dbw=-40.0, lat_deg=35.198, lon_deg=129.113)],
         "busan-pac", 260.0e6, 31250.0, 1024, 0.0),
    ],
)
def test_render_psd_matches_welch_of_render_iq(cfg: SystemConfig, name: str, emitters: Any, channel: str,
                                               fc: float, span: float, nfft: int, guard: float) -> None:
    """render_psd and welch_psd_db(render_iq) agree within 1 dB, mean over occupied bins and
    over empty bins (bins chosen from the noiseless expectation, not from either estimate)."""
    iq_occ, ps_occ, iq_emp, ps_emp = _psd_case(cfg, emitters, channel, fc, span, nfft, guard)
    assert abs(iq_occ - ps_occ) < 1.0, (name, iq_occ, ps_occ)
    assert abs(iq_emp - ps_emp) < 1.0, (name, iq_emp, ps_emp)
    assert abs(ps_emp) < 1.0  # empty bins read the 0 dB thermal reference


def test_relayed_noise_floor_equals_the_relay_rise(cfg_bare: SystemConfig) -> None:
    """Inside a transponder passband the floor is 1 + N0_relay / N0_site (MATH section 7
    relay rise, from core.linkbudget on the configured geometry); outside it is 0 dB."""
    from petal_locate.core.linkbudget import relay_rise_db

    world = SimWorld(cfg_bare, scenario([]))
    span, nfft = 62500.0, 1024
    fc = 301.0e6 - 33.6e6
    expected = world.render_psd("seoul-172e", T0, fc, span, nfft, 8, expected=True)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / span))
    inside = np.abs(freqs) < 11e3
    outside = np.abs(freqs) > 14e3
    rise = relay_rise_db(cfg_bare, "seoul-172e", "uhf-172e", "shared-1", sec(T0))
    assert rise is not None and rise > 0.5
    assert float(np.max(np.abs(expected[inside] - rise))) < 0.02
    assert float(np.max(np.abs(expected[outside]))) < 1e-6
    iq = welch_psd_db(world.render_iq("seoul-172e", T0, nfft * 8, span, fc), span, nfft, 8)
    assert 10 * math.log10(lin(iq[inside]).mean()) == pytest.approx(rise, abs=0.3)
    assert 10 * math.log10(lin(iq[outside]).mean()) == pytest.approx(0.0, abs=0.3)


def test_render_psd_scatter_is_keyed_and_gamma_distributed(cfg: SystemConfig) -> None:
    world = SimWorld(cfg, scenario([]))
    fc = 301.0e6 - 33.6e6
    a = world.render_psd("seoul-172e", T0, fc, 31250.0, 1024, 8)
    assert np.array_equal(a, world.render_psd("seoul-172e", T0, fc, 31250.0, 1024, 8))
    b = world.render_psd("seoul-172e", T0 + 1, fc, 31250.0, 1024, 8)
    assert not np.array_equal(a, b)
    ratio = lin(a) / lin(world.render_psd("seoul-172e", T0, fc, 31250.0, 1024, 8, expected=True))
    assert float(np.mean(ratio)) == pytest.approx(1.0, abs=0.03)
    assert float(np.var(ratio)) == pytest.approx(1.0 / 8.0, rel=0.2)  # Gamma(8, 1/8)
    assert a.dtype == np.float32 and a.shape == (1024,)


def test_render_speed(cfg: SystemConfig) -> None:
    """render_psd costs well under 10 ms per 1024-bin window after warm-up; render_iq of
    31250 samples at 31.25 kHz on a channel with three paths well under a second."""
    world = SimWorld(cfg, load_scenario(SCENARIOS / "pacific-demo.yaml"))
    fc = 301.0e6 - 33.6e6
    world.render_psd("seoul-172e", T0, fc, 31250.0, 1024, 8)
    costs = []
    for k in range(20):
        start = time.perf_counter()
        world.render_psd("seoul-172e", T0 + k * 500_000_000, fc, 31250.0, 1024, 8)
        costs.append(time.perf_counter() - start)
    assert statistics.median(costs) < 0.010
    fc_iq = F_J - 33.6e6
    paths = [p for p in world.channel_paths("seoul-172e", T0, fc_iq, FS) if p.treatment != "omitted"]
    assert len(paths) == 3
    start = time.perf_counter()
    world.render_iq("seoul-172e", T0, N, FS, fc_iq)
    assert time.perf_counter() - start < 1.5


# --------------------------------------------------------------------------- which paths a channel hears


def test_local_interferer_heard_only_inside_its_radio_horizon(cfg: SystemConfig) -> None:
    world = SimWorld(cfg, load_scenario(SCENARIOS / "local-interferer.yaml"))
    local = world.emitter("local-busan")
    horizon = 4120.0 * (math.sqrt(10.0) + math.sqrt(10.0))  # 26.06 km for two 10 m antennas
    for site in cfg.sites:
        dist = float(np.linalg.norm(cfg.site_xyz(site.id) - local.xyz))
        for ch in site.channels:
            direct = [p for p in world.channel_paths(ch.id, T0, 260.0e6, 31250.0) if p.kind == "direct"]
            assert bool(direct) == (dist < horizon), ch.id
    # the level at Busan is the direct-path budget written out here
    _, channel = cfg.channel("busan-pac")
    x_g = cfg.site_xyz("busan")
    d = float(np.linalg.norm(x_g - local.xyz))
    boresight = cfg.boresight_ecef("busan-pac", sec(T0))
    off_axis = float(angle_between_deg(boresight, local.xyz - x_g))
    c_rx = (-40.0 - 20 * math.log10(4 * math.pi * d * 260.005e6 / C)
            + float(gain_dbi(channel.antenna, off_axis, 260.005e6)) - channel.polarization_loss_db)
    cn0 = c_rx - (K_DBW + 10 * math.log10(channel.system_noise_temp_k))
    assert world.direct_power_ratio_dbhz("local-busan", "busan-pac", sec(T0)) == pytest.approx(cn0, abs=1e-9)
    quiet = SimWorld(cfg, load_scenario(SCENARIOS / "quiet.yaml"))
    with_local = world.render_psd("busan-pac", T0, 260.0e6, 31250.0, 1024, 8, expected=True)
    without = quiet.render_psd("busan-pac", T0, 260.0e6, 31250.0, 1024, 8, expected=True)
    freqs = np.fft.fftshift(np.fft.fftfreq(1024, 1.0 / 31250.0))
    band = np.abs(freqs - 5000.0) < 4000.0
    excess = 10 * math.log10(float(np.mean(lin(with_local[band]) - lin(without[band]))))
    assert excess == pytest.approx(cn0 - 10 * math.log10(10e3), abs=0.1)
    # another site watching the same satellite and frequency sees nothing of it (the two scenarios'
    # truth errors move the reference carrier's level in this window by about 2e-4 dB, nothing more)
    assert np.allclose(world.render_psd("seoul-pac", T0, 260.0e6, 31250.0, 1024, 8, expected=True),
                       quiet.render_psd("seoul-pac", T0, 260.0e6, 31250.0, 1024, 8, expected=True), atol=1e-3)
    # a copy just inside / just outside the horizon distance
    for frac, heard in ((0.95, True), (1.05, False)):
        lat, lon = destination(35.180, 129.075, 60.0, frac * horizon)
        w = SimWorld(cfg, scenario([jammer(kind="local", freq_hz=260.005e6, eirp_dbw=-40.0,
                                           lat_deg=float(lat), lon_deg=float(lon), alt_m=0.0)]))
        kinds = [p.kind for p in w.channel_paths("busan-pac", T0, 260.0e6, 31250.0)]
        assert ("direct" in kinds) == heard


def test_satellite_below_the_site_horizon_contributes_nothing(cfg_bare: SystemConfig) -> None:
    """Honolulu cannot see muos-io: a channel there that lists muos-io, with an isotropic
    antenna, receives none of it (no jammer copy, no transponder noise), while Naha does."""
    data = cfg_bare.export_dict()
    hon = next(s for s in data["sites"] if s["id"] == "honolulu")
    hon["channels"].append({"id": "honolulu-io-test", "antenna": {"type": "isotropic"},
                            "pointing": {"az_deg": 270.0, "el_deg": 5.0, "satellite_ids": ["muos-io"]}})
    test_cfg = SystemConfig.model_validate(data)
    x_g = test_cfg.site_xyz("honolulu")
    for h in range(0, 24, 3):
        _, el, _ = look_angles(x_g, orbit_position(test_cfg.orbits()["muos-io"], sec(T0) + h * 3600.0))
        assert float(el) < 0.0
    solo_up = 302.000e6  # only muos-io relays solo-io
    world = SimWorld(test_cfg, scenario([jammer(freq_hz=solo_up)]))
    fc = solo_up + translation(test_cfg, "muos-io", "solo-io")
    paths = world.channel_paths("honolulu-io-test", T0, fc, FS)
    assert all(p.sat_id != "muos-io" for p in paths)
    assert np.all(world.render_psd("honolulu-io-test", T0, fc, FS, 1024, 8, expected=True) == 0.0)
    y = world.render_iq("honolulu-io-test", T0, N, FS, fc)
    assert float(np.mean(np.abs(y) ** 2)) == pytest.approx(1.0, abs=0.03)
    naha = world.channel_paths("naha-io", T0, fc, FS)
    assert any(p.kind == "relay" and p.sat_id == "muos-io" for p in naha)


def test_cochannel_satellite_heard_through_the_pattern(cfg_bare: SystemConfig) -> None:
    """Tokyo's channel on muos-pac also hears the co-channel muos-io copy, weaker by the
    pattern gain toward muos-io (MATH section 4: every visible satellite is heard)."""
    world = SimWorld(cfg_bare, scenario([jammer()]))
    fc = F_J + translation(cfg_bare, "muos-pac")
    paths = {p.sat_id: p for p in world.channel_paths("tokyo-pac", T0, fc, FS)
             if p.kind == "relay" and p.treatment != "omitted"}
    assert set(paths) == {"muos-pac", "muos-io"}
    io_path = world.path_budget("j1", "muos-io", "shared-1", "tokyo-pac", sec(T0))
    own_io = world.path_budget("j1", "muos-io", "shared-1", "tokyo-io", sec(T0))
    assert io_path is not None and own_io is not None
    # same transponder output, different receive antennas: the difference is gain and L_pol
    _, ch_pac = cfg_bare.channel("tokyo-pac")
    _, ch_io = cfg_bare.channel("tokyo-io")
    r = orbit_position(world.truth_orbit("muos-io"), sec(T0))
    x_g = cfg_bare.site_xyz("tokyo")
    f_d = F_J + translation(cfg_bare, "muos-io")
    g_pac = float(gain_dbi(ch_pac.antenna, angle_between_deg(cfg_bare.boresight_ecef("tokyo-pac", sec(T0)), r - x_g), f_d))
    g_io = float(gain_dbi(ch_io.antenna, angle_between_deg(cfg_bare.boresight_ecef("tokyo-io", sec(T0)), r - x_g), f_d))
    diff = (g_pac - ch_pac.polarization_loss_db) - (g_io - ch_io.polarization_loss_db)
    assert float(io_path.downlink.c_rx_dbw - own_io.downlink.c_rx_dbw) == pytest.approx(diff, abs=1e-6)


# --------------------------------------------------------------------------- power robbing


def test_power_robbing_reduces_the_relayed_reference_carrier(cfg: SystemConfig) -> None:
    """A jammer strong enough to saturate shared-1 of muos-pac scales the Guam reference
    carrier's downlink EIRP (and the relayed noise) by EIRP_sat / P_tot, computed here in
    watts from the formulas of MATH section 4. Honolulu sees no co-channel satellite, so
    its shared-1 window holds muos-pac alone."""
    quiet = SimWorld(cfg, scenario([]))
    strong = SimWorld(cfg, scenario([jammer(eirp_dbw=35.0)]))
    t_s = sec(T0)
    before = quiet.path_budget("ref-guam", "muos-pac", "shared-1", "honolulu-pac", t_s)
    after = strong.path_budget("ref-guam", "muos-pac", "shared-1", "honolulu-pac", t_s)
    assert before is not None and after is not None
    tp = cfg.satellite("muos-pac").transponder("shared-1")
    r = orbit_position(strong.truth_orbit("muos-pac"), t_s)
    ref = strong.emitter("ref-guam")
    # the reference is aimed at muos-io: muos-pac sees it through the pattern
    aim = orbit_position(strong.truth_orbit("muos-io"), t_s) - ref.xyz
    off = float(angle_between_deg(aim, r - ref.xyz))
    toward = ref.eirp_dbw + float(gain_dbi(ref.antenna, off, ref.freq_hz) - gain_dbi(ref.antenna, 0.0, ref.freq_hz))

    def pfd(eirp_toward: float, x: np.ndarray) -> float:
        return eirp_toward - 10 * math.log10(4 * math.pi * float(np.sum((r - x) ** 2)))

    pfds = [pfd(toward, ref.xyz), pfd(35.0, strong.emitter("j1").xyz)]
    n0 = (tp.eirp_sat_dbw - tp.sfd_dbw_m2 + 10 * math.log10(4 * math.pi * tp.uplink_center_hz**2 / C**2)
          - tp.g_over_t_dbk + K_DBW)
    carriers_w = [10 ** ((tp.eirp_sat_dbw - tp.sfd_dbw_m2 + p) / 10) for p in pfds]
    total_w = sum(carriers_w) + 10 ** (n0 / 10) * tp.bandwidth_hz
    assert total_w > 10 ** (tp.eirp_sat_dbw / 10)  # saturated
    factor = 10 ** (tp.eirp_sat_dbw / 10) / total_w
    assert float(before.transponder_eirp_dbw) == pytest.approx(10 * math.log10(carriers_w[0]), abs=1e-6)
    assert float(after.transponder_eirp_dbw) == pytest.approx(10 * math.log10(carriers_w[0] * factor), abs=1e-6)
    assert float(after.downlink.c_rx_dbw - before.downlink.c_rx_dbw) == pytest.approx(10 * math.log10(factor), abs=1e-6)
    # in the spectrum, carrier and relayed noise above the thermal floor scale by the same factor
    fc = ref.freq_hz + tp.translation_hz
    freqs = np.fft.fftshift(np.fft.fftfreq(1024, 1.0 / 31250.0))
    band = np.abs(freqs) < 4000.0  # the 5 kHz reference and the noise floor; the jammer is at +12.5 kHz
    above_q = lin(quiet.render_psd("honolulu-pac", T0, fc, 31250.0, 1024, 8, expected=True))[band] - 1.0
    above_s = lin(strong.render_psd("honolulu-pac", T0, fc, 31250.0, 1024, 8, expected=True))[band] - 1.0
    assert float(np.mean(above_s) / np.mean(above_q)) == pytest.approx(factor, rel=1e-3)


# --------------------------------------------------------------------------- determinism and overlap


def test_repeat_calls_and_separate_worlds_agree_sample_for_sample(cfg: SystemConfig) -> None:
    sc = load_scenario(SCENARIOS / "pacific-demo.yaml")
    fc = F_J - 41.0e6
    w1, w2 = SimWorld(cfg, sc), SimWorld(cfg, sc)
    y1 = w1.render_iq("tokyo-pac", T0, 8192, FS, fc)
    assert np.array_equal(y1, w1.render_iq("tokyo-pac", T0, 8192, FS, fc))
    assert np.array_equal(y1, w2.render_iq("tokyo-pac", T0, 8192, FS, fc))
    assert np.array_equal(y1, w1.rebuild().render_iq("tokyo-pac", T0, 8192, FS, fc))
    other = SimWorld(cfg, sc.model_copy(update={"seed": 99}))
    assert not np.allclose(y1, other.render_iq("tokyo-pac", T0, 8192, FS, fc))


def test_overlapping_captures_agree_on_their_overlap(cfg: SystemConfig) -> None:
    """Every deterministic component (emitters through two co-channel satellites, a
    reference carrier, decimated relayed noise) is a function of absolute time: two
    captures that overlap agree there once their per-capture thermal noise is removed."""
    world = SimWorld(cfg, load_scenario(SCENARIOS / "pacific-demo.yaml"))
    fc = F_J - 41.0e6
    treatments = {p.treatment for p in world.channel_paths("tokyo-pac", T0, fc, FS)}
    assert "decimated" in treatments
    n1, n2, shift = 20000, 15000, 7000  # 1 / FS = 32 us exactly, so the samples coincide
    t2 = T0 + shift * 32_000
    y1 = world.render_iq("tokyo-pac", T0, n1, FS, fc) - thermal_noise(n1, f"thermal|1|tokyo-pac|{T0}|{FS}|{fc}")
    y2 = world.render_iq("tokyo-pac", t2, n2, FS, fc) - thermal_noise(n2, f"thermal|1|tokyo-pac|{t2}|{FS}|{fc}")
    a, b = y1[shift + 100: n1 - 100], y2[100: n1 - shift - 100]  # away from the decimation edges
    rms = float(np.sqrt(np.mean(np.abs(a) ** 2)))
    assert float(np.max(np.abs(a - b))) < 1e-3 * rms


def test_waveforms_are_identical_in_another_process(cfg: SystemConfig) -> None:
    """Keys are process independent (blake2b, never hash()): a fresh interpreter with a
    different hash seed renders the same samples."""
    sc_path = SCENARIOS / "pacific-demo.yaml"
    code = (
        "import hashlib, numpy as np\n"
        "from petal_locate.config.loader import load_config\n"
        "from petal_locate.sim import SimWorld, load_scenario\n"
        f"w = SimWorld(load_config({str(CONFIG_PATH)!r}), load_scenario({str(sc_path)!r}))\n"
        f"y = w.render_iq('tokyo-pac', {T0}, 4096, {FS}, {F_J - 41.0e6})\n"
        "print(hashlib.sha256(y.tobytes()).hexdigest())\n"
    )
    env = dict(os.environ, PYTHONHASHSEED="12345", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120, check=True)
    here = SimWorld(cfg, load_scenario(sc_path)).render_iq("tokyo-pac", T0, 4096, FS, F_J - 41.0e6)
    assert out.stdout.strip() == hashlib.sha256(here.tobytes()).hexdigest()


def test_rebuild_reuses_unchanged_waveforms(cfg: SystemConfig) -> None:
    sc = load_scenario(SCENARIOS / "two-jammers.yaml")
    w1 = SimWorld(cfg, sc)
    changed = sc.model_copy(deep=True)
    changed.emitters[0].eirp_dbw = 10.0
    w2 = w1.rebuild(scenario=changed)
    assert w2.emitter("jammer-a").waveform is w1.emitter("jammer-a").waveform
    assert w2.emitter("jammer-a").eirp_dbw == 10.0
    reseeded = changed.model_copy(update={"seed": 6})
    assert w1.rebuild(scenario=reseeded).emitter("jammer-a").waveform is not w1.emitter("jammer-a").waveform


# --------------------------------------------------------------------------- emitters and records


def test_carrier_defaults_and_auto_created_carrier_emitters(cfg: SystemConfig) -> None:
    world = SimWorld(cfg, scenario([]))
    assert [e.id for e in world.emitters] == ["ref-guam", "ref-sapporo"]
    for e in world.emitters:
        carrier = cfg.carrier(e.id)
        assert e.kind == "carrier" and e.from_config and e.carrier_id == carrier.id
        assert (e.lat_deg, e.lon_deg) == (carrier.location.lat_deg, carrier.location.lon_deg)
        assert np.allclose(e.xyz, cfg.carrier_xyz(carrier.id))
        assert e.freq_hz == carrier.uplink_center_hz
        assert e.eirp_dbw == carrier.emitter.eirp_dbw
        assert e.antenna == carrier.emitter.antenna and e.pointing == carrier.emitter.pointing
        assert e.waveform_spec.type == carrier.emitter.waveform
        assert e.bandwidth_hz == carrier.emitter.bandwidth_hz
    # a scenario emitter naming a carrier takes its defaults, overrides win, no duplicate
    sc = scenario([{"id": "guam-missurveyed", "kind": "carrier", "carrier_id": "ref-guam", "lat_deg": 13.5,
                    "eirp_dbw": 10.0}])
    world = SimWorld(cfg, sc)
    assert [e.id for e in world.emitters] == ["guam-missurveyed", "ref-sapporo"]
    e = world.emitter("guam-missurveyed")
    carrier = cfg.carrier("ref-guam")
    assert e.lat_deg == 13.5 and e.lon_deg == carrier.location.lon_deg
    assert e.eirp_dbw == 10.0 and e.freq_hz == carrier.uplink_center_hz and not e.from_config
    assert e.key_label == "emitter|1|guam-missurveyed|0"
    # an id that collides with an auto-created carrier emitter is refused, naming the field
    with pytest.raises(ScenarioError, match=r"emitters\[0\] \(ref-sapporo\)\.id"):
        SimWorld(cfg, scenario([jammer(id="ref-sapporo")]))


def test_emitters_at_honours_enabled_and_times(cfg_bare: SystemConfig) -> None:
    t_on, t_off = T0, T0 + 10 * 10**9
    sc = scenario([
        jammer(id="a"),
        jammer(id="b", enabled=False),
        jammer(id="c", t_start=iso_from_ns(t_on), t_stop=iso_from_ns(t_off)),
    ])
    world = SimWorld(cfg_bare, sc)
    assert [e.id for e in world.emitters_at(t_on - 1)] == ["a"]
    assert [e.id for e in world.emitters_at(t_on)] == ["a", "c"]
    assert [e.id for e in world.emitters_at(t_off)] == ["a"]
    assert world.path_budget("b", "muos-pac", "shared-1", "tokyo-pac", sec(T0)) is None
    assert all(p.emitter_id != "b" for p in world.channel_paths("tokyo-pac", T0, F_J - 41e6, FS))
    summary = world.truth_summary(T0)
    json.dumps(summary)
    assert summary["simulated"] is True
    assert {e["id"]: e["active"] for e in summary["emitters"]} == {"a": True, "b": False, "c": True}
    record = world.emitter("c").as_dict()
    assert record["t_start"] == iso_from_ns(t_on) and record["bandwidth_hz"] == 10e3 and record["simulated"]


def test_path_budget_none_cases(cfg_bare: SystemConfig) -> None:
    world = SimWorld(cfg_bare, scenario([jammer()]))
    t_s = sec(T0)
    assert world.path_budget("j1", "muos-pac", "shared-2", "tokyo-pac", t_s) is None  # not relayed
    assert world.path_budget("j1", "muos-io", "shared-1", "honolulu-pac", t_s) is None  # below the horizon
    far = SimWorld(cfg_bare, scenario([jammer(lat_deg=20.0, lon_deg=-60.0)]))  # cannot see muos-io
    assert far.path_budget("j1", "muos-io", "shared-1", "naha-io", t_s) is None
    pb = world.path_budget("j1", "muos-pac", "shared-1", "tokyo-pac", t_s)
    assert pb is not None and float(pb.cn0_total_dbhz) > 50.0


def test_input_errors_name_the_argument(cfg: SystemConfig) -> None:
    world = SimWorld(cfg, scenario([]))
    with pytest.raises(ValueError, match="channel_id"):
        world.render_iq("nowhere", T0, 10, FS, 260e6)
    with pytest.raises(ValueError, match="t_start_ns"):
        world.render_iq("tokyo-pac", float(T0), 10, FS, 260e6)
    with pytest.raises(ValueError, match="n must"):
        world.render_iq("tokyo-pac", T0, -1, FS, 260e6)
    with pytest.raises(ValueError, match="fs"):
        world.render_iq("tokyo-pac", T0, 10, 0.0, 260e6)
    with pytest.raises(ValueError, match="nfft"):
        world.render_psd("tokyo-pac", T0, 260e6, FS, 1, 8)
    with pytest.raises(ValueError, match="emitter_id"):
        world.truth_measurement("nobody", *pair(cfg), T0)
    solo = SimWorld(cfg, scenario([jammer(freq_hz=302.0e6)]))  # relayed by muos-io only
    with pytest.raises(ValueError, match=r"a\.sat_id"):
        solo.truth_measurement("j1", PathSpec("muos-pac", "tokyo", 302.0e6, -41e6),
                               PathSpec("muos-io", "naha", 302.0e6, -41e6), T0)
    assert world.render_iq("tokyo-pac", T0, 0, FS, 260e6).shape == (0,)


# --------------------------------------------------------------------------- the shipped scenarios


def test_pacific_demo_claims_hold(cfg: SystemConfig) -> None:
    """The pacific-demo description: every channel detects the jammer comfortably, every
    satellite pair correlates far above the 13 dB threshold, the transponders stay linear."""
    world = SimWorld(cfg, load_scenario(SCENARIOS / "pacific-demo.yaml"))
    t_s = sec(T0)
    cn0 = {}
    for site in cfg.sites:
        for ch in site.channels:
            pb = world.path_budget("jammer-philsea", ch.pointing.satellite_id, "shared-1", ch.id, t_s)
            assert pb is not None, ch.id
            cn0[ch.id] = float(pb.cn0_total_dbhz)
            assert 51.0 < cn0[ch.id] < 57.0
            assert pb.snr_db(10e3) > cfg.processing.detection.threshold_db + 5.0
            assert float(pb.transponder_eirp_dbw) < cfg.satellite(ch.pointing.satellite_id).transponder("shared-1").eirp_sat_dbw - 5.0
    bw = 1.05 * (10e3 + 2 * 230.0)
    worst = min(cn0.values())
    g = lin(worst - 10 * math.log10(bw))
    assert 10 * math.log10(post_correlation_snr(bw, 1.0, g, g)) > cfg.processing.caf.min_snr_db + 20.0
