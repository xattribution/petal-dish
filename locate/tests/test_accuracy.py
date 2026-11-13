"""Tests of the accuracy prediction (core/accuracy.py, MATH.md section 10).

Independent references: a cell recomputed by hand from ``linkbudget.path_budget``, the
windows of ``geometry``, ``B`` and ``T`` read back from an actual ``caf_estimate`` run
with those windows, ``crlb.stein_sigmas`` and ``solver.position_covariance``; the
solver's own CEP50; elevations from ``geometry.elevation_deg``; and a Monte Carlo of
``solver.solve`` with systematic errors drawn through ``PerturbedOrbit`` and the
nonlinear predictors.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from petal_locate.config.loader import load_config
from petal_locate.config.models import EmitterModel, Region, SystemConfig
from petal_locate.core.accuracy import (
    accuracy_map,
    capture_plan,
    cep50_batch,
    correlation_bandwidth_hz,
    integration_time_s,
    model_rms_bandwidth_hz,
)
from petal_locate.core.caf import caf_estimate
from petal_locate.core.crlb import post_correlation_snr, stein_sigmas
from petal_locate.core.geodesy import lla_to_ecef, radii_of_curvature
from petal_locate.core.geometry import (
    elevation_deg,
    measurement_windows,
    predict_pair,
    search_windows,
)
from petal_locate.core.linkbudget import emitter_boresight, path_budget, uplink_budget
from petal_locate.core.orbits import PerturbedOrbit, orbit_position
from petal_locate.core.signals import waveform_from_spec
from petal_locate.core.solver import CalRef, Measurement, cep50, position_covariance, solve
from petal_locate.core.timebase import sec
from petal_locate.core.types import PathSpec

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_794_000_000 * 10**9  # Nov 2026, as in test_world.py
F_J = 301.005e6  # inside shared-1 of all three satellites
PAIRS = [("manila-pac", "manila-172e"), ("manila-pac", "manila-io")]
SCALE_95 = math.sqrt(-2.0 * math.log(0.05))


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(ROOT / "config" / "pacific-uhf.yaml")


@pytest.fixture(scope="module")
def jammer() -> EmitterModel:
    return EmitterModel(eirp_dbw=20.0, bandwidth_hz=10e3, waveform="noise")


def cell(out: dict[str, Any], key: str, lat: float, lon: float) -> Any:
    return out[key][out["lat_deg"].index(lat)][out["lon_deg"].index(lon)]


def grid(out: dict[str, Any], key: str) -> np.ndarray:
    return np.array([[np.nan if v is None else v for v in row] for row in out[key]], dtype=np.float64)


# --------------------------------------------------------------------------- the hand calculation


def caf_band_and_time(fs: float, n: int, signal_bw: float, tdoa: float, windows: Any) -> tuple[float, float]:
    """B and T as caf_estimate itself reports them, for a peak at ``tdoa`` (synthetic data)."""
    delay_w, doppler_w, mirror_w = windows
    lag = int(round(tdoa * fs))
    rng = np.random.default_rng(12345)
    pad = abs(lag)
    sig = (rng.standard_normal(n + 2 * pad) + 1j * rng.standard_normal(n + 2 * pad)) / math.sqrt(2.0)
    nu = 0.5 * (doppler_w[0] + doppler_w[1])
    t = np.arange(n) / fs
    yb = sig[pad: pad + n] + (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2.0)
    ya = sig[pad - lag: pad - lag + n] * np.exp(2j * np.pi * nu * t)
    ya = ya + (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / math.sqrt(2.0)
    r = caf_estimate(ya, yb, fs, delay_window=delay_w, doppler_window=doppler_w, signal_bw_hz=signal_bw,
                     mirror_window=mirror_w)
    assert r.detected and r.tdoa_s is not None, r.reason
    assert r.tdoa_s == pytest.approx(lag / fs, abs=0.5 / fs)
    return r.noise_bandwidth_hz, r.integration_s


def pfd_of(cfg: SystemConfig, carrier_id: str, sat_id: str, t_s: float) -> float:
    carrier = cfg.carrier(carrier_id)
    x_c = cfg.carrier_xyz(carrier_id)
    orbits = cfg.orbits()
    up = uplink_budget(carrier.emitter.eirp_dbw, carrier.emitter.antenna,
                       emitter_boresight(carrier.emitter, x_c, orbits, t_s), x_c,
                       orbit_position(orbits[sat_id], t_s), carrier.uplink_center_hz,
                       cfg.satellite(sat_id).transponder("shared-1"))
    return float(up.pfd_dbw_m2)


def hand_rows(cfg: SystemConfig, em: EmitterModel, lat: float, lon: float, ref_id: str | None = None,
              pairs: list[tuple[str, str]] = PAIRS) -> list[Measurement]:
    """The rows the map should use at one cell, built from the documents with other modules.

    Valid for pacific-uhf with the jammer in shared-1, the Manila channels and (optionally)
    ref-guam: the only configured carrier in shared-1. Every listed pair must pass the
    detection threshold at the cell (asserted).
    """
    t_s = sec(T0)
    orbits, sites = cfg.orbits(), cfg.sites_xyz()
    unc = cfg.resolved_uncertainty()
    caf_cfg = cfg.processing.caf
    region = cfg.processing.region
    x = lla_to_ecef(lat, lon, cfg.processing.solver.height_m)
    fs, n = 20e3, 20000  # tasking rule: max(20 kHz, 1.25 x 10 kHz), 1 s; both carriers
    rms = waveform_from_spec({"type": "noise", "bandwidth_hz": 10e3}, "x").rms_bandwidth_hz
    assert rms == pytest.approx(0.9934 * 10e3 / math.sqrt(12.0), rel=1e-3)
    rows: list[Measurement] = []
    for k, (ch_a, ch_b) in enumerate(pairs):
        sat_a = cfg.channel(ch_a)[1].pointing.satellite_id
        sat_b = cfg.channel(ch_b)[1].pointing.satellite_id
        site_a, site_b = cfg.channel(ch_a)[0].id, cfg.channel(ch_b)[0].id
        tr_a = cfg.satellite(sat_a).transponder("shared-1").translation_hz
        tr_b = cfg.satellite(sat_b).transponder("shared-1").translation_hz
        a, b = PathSpec(sat_a, site_a, F_J, tr_a), PathSpec(sat_b, site_b, F_J, tr_b)
        co_channel = tr_a == tr_b and all(
            float(elevation_deg(sites[s], orbits[q], t_s)) > 0 for s in (site_a, site_b) for q in (sat_a, sat_b))
        cal = None
        if ref_id is not None:
            carrier = cfg.carrier(ref_id)
            x_ref = cfg.carrier_xyz(ref_id)
            f_ref = carrier.uplink_center_hz
            ra, rb = PathSpec(sat_a, site_a, f_ref, tr_a), PathSpec(sat_b, site_b, f_ref, tr_b)
            pt, pf = predict_pair(x_ref, ra, rb, t_s, orbits, sites)
            ref_win = search_windows(((float(pt), float(pt)), (float(pf), float(pf))), ra, rb, caf_cfg, unc)
            ref_mirror = None
            if co_channel:
                ma, mb = PathSpec(sat_b, site_a, f_ref, tr_b), PathSpec(sat_a, site_b, f_ref, tr_a)
                mt, mf = predict_pair(x_ref, ma, mb, t_s, orbits, sites)
                ref_mirror = search_windows(((float(mt), float(mt)), (float(mf), float(mf))), ma, mb, caf_cfg, unc)
            w_ref = float(carrier.emitter.bandwidth_hz)  # 8 kHz; the reference capture rule still gives 20 kHz, 1 s
            b_ref, t_ref = caf_band_and_time(fs, n, w_ref, float(pt), (*ref_win, ref_mirror))
            cn0 = [float(path_budget(cfg, carrier.emitter, f_ref, x_ref, s, "shared-1", ch, t_s).cn0_total_dbhz)
                   for s, ch in ((sat_a, ch_a), (sat_b, ch_b))]
            g1, g2 = (10 ** ((c - 10 * math.log10(b_ref)) / 10) for c in cn0)
            assert 10 * math.log10(post_correlation_snr(b_ref, t_ref, g1, g2)) >= caf_cfg.min_snr_db
            rms_ref = waveform_from_spec({"type": "noise", "bandwidth_hz": w_ref}, "x").rms_bandwidth_hz
            s_t, s_f = stein_sigmas(b_ref, rms_ref, t_ref, g1, g2)
            cal = {kind: CalRef(f"{ref_id}|{k}|{kind}", x_ref, ra, rb, T0, sig)
                   for kind, sig in (("tdoa", s_t), ("fdoa", s_f))}
        geo = measurement_windows(a, b, t_s, orbits, sites, region)
        correction = (0.0, 0.0) if cal is not None else None
        win = search_windows(geo, a, b, caf_cfg, unc, correction)
        mirror = None
        if co_channel:
            ma, mb = PathSpec(sat_b, site_a, F_J, tr_b), PathSpec(sat_a, site_b, F_J, tr_a)
            mirror = search_windows(measurement_windows(ma, mb, t_s, orbits, sites, region), ma, mb, caf_cfg, unc)
        tdoa, _ = predict_pair(x, a, b, t_s, orbits, sites)
        bw, t_int = caf_band_and_time(fs, n, 10e3, float(tdoa), (*win, mirror))
        others = ["ref-guam"]  # configured carriers sharing shared-1 rob power (none below saturation)
        cn0 = [float(path_budget(cfg, em, F_J, x, s, "shared-1", ch, t_s,
                                 [pfd_of(cfg, c, s, t_s) for c in others]).cn0_total_dbhz)
               for s, ch in ((sat_a, ch_a), (sat_b, ch_b))]
        g1, g2 = (10 ** ((c - 10 * math.log10(bw)) / 10) for c in cn0)
        assert 10 * math.log10(post_correlation_snr(bw, t_int, g1, g2)) >= caf_cfg.min_snr_db
        s_t, s_f = stein_sigmas(bw, rms, t_int, g1, g2)
        assert s_t <= caf_cfg.max_tdoa_sigma_s
        for kind, sig in (("tdoa", s_t), ("fdoa", s_f)):
            rows.append(Measurement(kind, 0.0, float(sig), T0, a, b, cal=None if cal is None else cal[kind]))
    return rows


@pytest.mark.parametrize("ref_id", [None, "ref-guam"])
def test_one_cell_recomputed_by_hand(cfg: SystemConfig, jammer: EmitterModel, ref_id: str | None) -> None:
    """Budgets -> B, T (as caf_estimate reports them) -> Stein sigmas -> position_covariance
    give the map's 95 % semi-major axis, CEP50 and dominant source at a cell."""
    region = Region(lat_min=18.0, lat_max=22.0, lon_min=130.0, lon_max=134.0)
    out = accuracy_map(cfg, PAIRS, jammer, F_J, T0, region, 2.0, reference_carrier_id=ref_id)
    rows = hand_rows(cfg, jammer, 20.0, 132.0, ref_id)
    ref = position_covariance(rows, 20.0, 132.0, cfg.orbits(), cfg.sites_xyz(), cfg.processing.solver.height_m,
                              cfg.resolved_uncertainty(), 0.95)
    assert ref is not None
    assert cell(out, "semi_major_95_m", 20.0, 132.0) == pytest.approx(ref["ellipse"]["semi_major_m"], rel=2e-3)
    assert cell(out, "semi_minor_95_m", 20.0, 132.0) == pytest.approx(ref["ellipse"]["semi_minor_m"], rel=2e-3)
    assert cell(out, "azimuth_deg", 20.0, 132.0) == pytest.approx(ref["ellipse"]["azimuth_deg"], abs=0.05)
    assert cell(out, "cep50_m", 20.0, 132.0) == pytest.approx(ref["cep50_m"], rel=2e-3)
    assert cell(out, "dominant", 20.0, 132.0) == ref["dominant_error"]
    assert cell(out, "n_rows", 20.0, 132.0) == 4
    assert cell(out, "reason", 20.0, 132.0) is None
    assert out["pairs"][0]["calibrated"] == ([] if ref_id is None else ["tdoa", "fdoa"])


# --------------------------------------------------------------------------- building blocks


def test_capture_plan_follows_the_tasking_rule(cfg: SystemConfig) -> None:
    chans = ["manila-pac", "manila-172e"]
    p = capture_plan(cfg, chans, F_J - 5e3, F_J + 5e3)
    assert (p.sample_rate_hz, p.n_samples, p.f_e_nom_hz, p.signal_bw_hz) == (20e3, 20000, F_J, 10e3)
    assert p.t_mid_ns(T0) == T0 + round(1e9 * 19999 / 40e3)
    p = capture_plan(cfg, chans, 301.0e6, 301.2e6)  # 200 kHz: 1.25 x 200 kHz, 1 s
    assert (p.sample_rate_hz, p.n_samples) == (250e3, 250000)
    p = capture_plan(cfg, chans, 300.0e6, 301.0e6)  # 1 MHz: capped by max_samples
    assert (p.sample_rate_hz, p.n_samples) == (1.25e6, 262144)
    p = capture_plan(cfg, chans, 300.0e6, 302.0e6)  # 2 MHz: the channels' 2 Msps limit
    assert p.sample_rate_hz == 2e6
    p = capture_plan(cfg, chans, F_J, F_J)  # a CW: minimum rate, no band filter
    assert (p.sample_rate_hz, p.signal_bw_hz) == (20e3, None)
    p = capture_plan(cfg, chans, 300.0e6, 301.0e6, duration_s=4.0)  # what-if: no max_samples cap
    assert p.n_samples == 5_000_000
    with pytest.raises(ValueError, match="sample_rate_hz"):
        capture_plan(cfg, chans, F_J, F_J, sample_rate_hz=3e6)
    with pytest.raises(ValueError, match="f_hi_hz"):
        capture_plan(cfg, chans, F_J, F_J - 1.0)


@pytest.mark.parametrize("signal_bw, lag", [(10e3, 237), (10e3, -1400), (None, 37)])
def test_band_and_integration_time_are_the_estimators(signal_bw: float | None, lag: int) -> None:
    """correlation_bandwidth_hz and integration_time_s reproduce CafResult.noise_bandwidth_hz
    and integration_s of a real caf_estimate run."""
    fs, n = 20e3, 20000
    windows = ((lag / fs - 2e-3, lag / fs + 2e-3), (-60.0, 45.0), None)
    bw, t_int = caf_band_and_time(fs, n, signal_bw, lag / fs, windows)
    b_pred, taps = correlation_bandwidth_hz(fs, signal_bw, 60.0, n)
    assert b_pred == pytest.approx(bw, rel=1e-12)
    assert integration_time_s(n, taps, fs, lag / fs) == pytest.approx(t_int, rel=1e-12)
    if signal_bw is None:
        assert (b_pred, taps) == (fs, 1)
    else:
        assert 1.0 < b_pred / (signal_bw + 120.0) < 1.08  # pass band plus a 5 % transition


def test_rms_bandwidth_of_the_emitter_model() -> None:
    noise = EmitterModel(eirp_dbw=0.0, bandwidth_hz=25e3, waveform="noise")
    assert model_rms_bandwidth_hz(noise) == pytest.approx(0.9934 * 25e3 / math.sqrt(12.0), rel=1e-3)
    assert model_rms_bandwidth_hz(EmitterModel(eirp_dbw=0.0, bandwidth_hz=0.0, waveform="cw")) == 0.0


def test_cep50_batch_equals_the_solvers() -> None:
    rng = np.random.default_rng(3)
    covs = []
    for _ in range(200):
        s1, ratio, th = 10 ** rng.uniform(0, 6), 10 ** rng.uniform(-4, 0), rng.uniform(0, np.pi)
        rot = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
        covs.append(rot @ np.diag([s1**2, (s1 * ratio) ** 2]) @ rot.T)
    covs.append(np.zeros((2, 2)))
    got = cep50_batch(np.array(covs))
    want = np.array([cep50(c) for c in covs])
    assert np.allclose(got, want, rtol=1e-11, atol=0.0)


# --------------------------------------------------------------------------- the map


def test_cells_where_a_satellite_is_below_the_horizon_are_null(cfg: SystemConfig, jammer: EmitterModel) -> None:
    out = accuracy_map(cfg, PAIRS, jammer, F_J, T0, cfg.processing.region, 4.0)
    lat = np.array(out["lat_deg"])
    lon = np.array(out["lon_deg"])
    lat2, lon2 = np.meshgrid(lat, lon, indexing="ij")
    x = lla_to_ecef(lat2, lon2, 0.0)
    orbits = cfg.orbits()
    el = {s: np.asarray(elevation_deg(x, orbits[s], sec(T0))) for s in ("muos-pac", "uhf-172e", "muos-io")}
    semi = grid(out, "semi_major_95_m")
    reason = np.array(out["reason"], dtype=object)
    no_pac = el["muos-pac"] < 0.0  # the satellite of channel A of both pairs
    no_partner = (el["uhf-172e"] < 0.0) & (el["muos-io"] < 0.0)
    gone = no_pac | no_partner
    assert np.any(no_pac) and np.any(no_partner & ~no_pac) and np.any(~gone)
    assert np.all(np.isnan(semi[gone]))
    assert np.all(reason[gone] == "not admissible")
    # where one partner is missing only one pair (2 rows) can contribute
    one = ~no_pac & ((el["uhf-172e"] < 0.0) ^ (el["muos-io"] < 0.0))
    rows = np.array([[np.nan if v is None else v for v in r] for r in out["n_rows"]])
    assert np.all((rows[one] <= 2) | np.isnan(rows[one]))
    # everything visible at the jammer's area: a finite ellipse from 4 rows
    both = (el["muos-pac"] > 5) & (el["uhf-172e"] > 5) & (el["muos-io"] > 5)
    assert np.all(np.isfinite(semi[both])) and np.all(rows[both] == 4)
    assert set(reason[~np.isnan(semi)].tolist()) == {None}


def ephemeris_dominated(cfg: SystemConfig, reference_eirp_dbw: float) -> SystemConfig:
    """pacific-uhf with large ephemeris sigmas, tiny clock, oscillator and bias sigmas, and
    the Guam reference radiating ``reference_eirp_dbw``."""
    data = cfg.export_dict()
    for sat in data["satellites"]:
        sat["ephemeris_sigma"] = {"pos_m": [500.0, 5000.0, 5000.0], "vel_mps": [0.001, 0.001, 0.001]}
    for site in data["sites"]:
        site["clock"] = {"sigma_ns": 1.0}
    data.setdefault("processing", {})["uncertainty"] = {"sat_osc_frac": 1e-10, "site_osc_frac": 1e-12, "tdoa_bias_ns": 1.0}
    guam = next(c for c in data["carriers"] if c["id"] == "ref-guam")
    guam["emitter"]["eirp_dbw"] = reference_eirp_dbw
    return SystemConfig.model_validate(data)


@pytest.fixture(scope="module")
def ephemeris_cfg(cfg: SystemConfig) -> SystemConfig:
    return ephemeris_dominated(cfg, 20.0)  # the configured reference


@pytest.mark.parametrize("reference_eirp_dbw, ratio, dominant", [
    (13.0, 0.3, {"reference"}),  # a weaker omni: its own noise is what is left
    (20.0, 0.2, {"reference", "noise", "ephemeris_position"}),  # as configured: a mix of what is left
    (30.0, 0.2, {"noise"}),  # 10 dB stronger: the target's own noise is what is left
])
def test_reference_shrinks_the_ellipse_when_ephemeris_dominates(cfg: SystemConfig, jammer: EmitterModel,
                                                               reference_eirp_dbw: float, ratio: float,
                                                               dominant: set[str]) -> None:
    """Near the Guam reference the common ephemeris error cancels (MATH.md section 9): the
    ellipse shrinks and the dominant source moves from the ephemeris to what is left: the
    reference measurement's own noise (a 13 dBW omni), the target's noise (30 dBW), or a
    mix of those and the uncancelled ephemeris residual far from Guam (the configured
    20 dBW). Measured ratios of calibrated to plain semi-major: at most 0.23, 0.15, 0.15."""
    config = ephemeris_dominated(cfg, reference_eirp_dbw)
    region = Region(lat_min=8.0, lat_max=20.0, lon_min=138.0, lon_max=150.0)
    plain = accuracy_map(config, PAIRS, jammer, F_J, T0, region, 3.0)
    cal = accuracy_map(config, PAIRS, jammer, F_J, T0, region, 3.0, reference_carrier_id="ref-guam")
    s_plain, s_cal = grid(plain, "semi_major_95_m"), grid(cal, "semi_major_95_m")
    assert np.all(np.isfinite(s_plain)) and np.all(np.isfinite(s_cal))
    assert set(np.array(plain["dominant"], dtype=object).ravel().tolist()) == {"ephemeris_position"}
    assert set(np.array(cal["dominant"], dtype=object).ravel().tolist()) <= dominant
    assert np.all(s_cal < ratio * s_plain)
    assert [p["calibrated"] for p in cal["pairs"]] == [["tdoa", "fdoa"], ["tdoa", "fdoa"]]


def test_antimeridian_wrapped_region_grid(ephemeris_cfg: SystemConfig, jammer: EmitterModel) -> None:
    """A region crossing 180 E gives unwrapped increasing longitudes, and a cell there holds
    the same prediction as the same point in a region that does not wrap."""
    pairs = [("tokyo-pac", "tokyo-172e"), ("naha-pac", "naha-172e")]
    wrapped = Region(lat_min=10.0, lat_max=30.0, lon_min=170.0, lon_max=-170.0)
    out = accuracy_map(ephemeris_cfg, pairs, jammer, F_J, T0, wrapped, 5.0, reference_carrier_id="ref-guam")
    assert out["lon_deg"] == [170.0, 175.0, 180.0, 185.0, 190.0]
    assert out["lat_deg"] == [10.0, 15.0, 20.0, 25.0, 30.0]
    assert len(out["semi_major_95_m"]) == 5 and all(len(r) == 5 for r in out["semi_major_95_m"])
    plain = Region(lat_min=10.0, lat_max=30.0, lon_min=-180.0, lon_max=-170.0)
    ref = accuracy_map(ephemeris_cfg, pairs, jammer, F_J, T0, plain, 5.0, reference_carrier_id="ref-guam")
    assert ref["lon_deg"] == [-180.0, -175.0, -170.0]
    for lat in out["lat_deg"]:
        for lon_w, lon_p in ((180.0, -180.0), (185.0, -175.0), (190.0, -170.0)):
            a, b = cell(out, "semi_major_95_m", lat, lon_w), cell(ref, "semi_major_95_m", lat, lon_p)
            assert a is not None and b is not None
            # 185 and -175 round differently; finite differences of the FDOA predictor (1e-8 Hz
            # rounding noise over 200 m) leave about 1e-5 of relative noise in J
            assert a == pytest.approx(b, rel=1e-4)
            assert cell(out, "dominant", lat, lon_w) == cell(ref, "dominant", lat, lon_p)
    # the cell at 185 E is the point 175 W: the hand calculation there agrees
    rows = hand_rows(ephemeris_cfg, jammer, 20.0, -175.0, "ref-guam", pairs)
    hand = position_covariance(rows, 20.0, -175.0, ephemeris_cfg.orbits(), ephemeris_cfg.sites_xyz(), 0.0,
                               ephemeris_cfg.resolved_uncertainty(), 0.95)
    assert hand is not None
    assert cell(out, "semi_major_95_m", 20.0, 185.0) == pytest.approx(hand["ellipse"]["semi_major_m"], rel=2e-3)


def test_default_pacific_map_is_fast(cfg: SystemConfig, jammer: EmitterModel) -> None:
    """Default region at 2 degrees (61 x 91 cells), two pairs, both kinds: well under 3 s."""
    region = cfg.processing.region
    accuracy_map(cfg, PAIRS, jammer, F_J, T0, region, 2.0)  # warm imports and caches
    start = time.perf_counter()
    out = accuracy_map(cfg, PAIRS, jammer, F_J, T0, region, 2.0)
    elapsed = time.perf_counter() - start
    assert (len(out["lat_deg"]), len(out["lon_deg"])) == (61, 91)
    assert elapsed < 3.0, elapsed


def test_invalid_arguments_raise(cfg: SystemConfig, jammer: EmitterModel) -> None:
    region = Region(lat_min=18.0, lat_max=22.0, lon_min=130.0, lon_max=134.0)
    with pytest.raises(ValueError, match="kinds"):
        accuracy_map(cfg, PAIRS, jammer, F_J, T0, region, 2.0, kinds=("tdoa", "range"))
    with pytest.raises(ValueError, match="pairs"):
        accuracy_map(cfg, [], jammer, F_J, T0, region, 2.0)
    with pytest.raises(ValueError, match="pairs\\[0\\]"):
        accuracy_map(cfg, [("manila-pac", "honolulu-pac")], jammer, F_J, T0, region, 2.0)
    with pytest.raises(ValueError, match="channel_id"):
        accuracy_map(cfg, [("manila-pac", "nowhere")], jammer, F_J, T0, region, 2.0)
    with pytest.raises(ValueError, match="t_ns"):
        accuracy_map(cfg, PAIRS, jammer, F_J, float(T0), region, 2.0)
    with pytest.raises(ValueError, match="step_deg"):
        accuracy_map(cfg, PAIRS, jammer, F_J, T0, region, 0.0)
    data = cfg.export_dict()
    data["carriers"][0]["use_as_reference"] = False
    data["carriers"][0].pop("location")
    bad = SystemConfig.model_validate(data)
    with pytest.raises(ValueError, match="reference_carrier_id"):
        accuracy_map(bad, PAIRS, jammer, F_J, T0, region, 2.0, reference_carrier_id=data["carriers"][0]["id"])


def test_carrier_not_relayed_contributes_nothing(cfg: SystemConfig, jammer: EmitterModel) -> None:
    """solo-pac is relayed by muos-pac only: no pair, every cell null with a reason."""
    region = Region(lat_min=18.0, lat_max=22.0, lon_min=130.0, lon_max=134.0)
    out = accuracy_map(cfg, PAIRS, jammer, 301.5e6, T0, region, 2.0)
    assert all(v is None for row in out["semi_major_95_m"] for v in row)
    assert all(p["reason"] is not None for p in out["pairs"])


# --------------------------------------------------------------------------- Monte Carlo of the solver


def simulate_rows(cfg: SystemConfig, rows: list[Measurement], x: np.ndarray, rng: np.random.Generator) -> list[Measurement]:
    """Rows with values drawn as the world would make them: noise at each row's sigma and
    systematic errors drawn from the configured uncertainty through the nonlinear
    predictors on perturbed orbits (reference rows the same, at the reference position)."""
    unc = cfg.resolved_uncertainty()
    orbits, sites = cfg.orbits(), cfg.sites_xyz()
    t_s = sec(T0)
    truth = dict(orbits)
    for sat in orbits:
        truth[sat] = PerturbedOrbit(orbits[sat], rng.standard_normal(3) * unc.sat_pos_ric_m[sat],
                                    rng.standard_normal(3) * unc.sat_vel_ric_mps[sat], t_s)
    clock = {s: rng.standard_normal() * unc.site_clock_s[s] for s in sites}
    site_osc = {s: rng.standard_normal() * unc.site_osc_frac[s] for s in sites}
    sat_osc = {s: rng.standard_normal() * unc.sat_osc_frac[s] for s in orbits}
    bias: dict[tuple[str, ...], float] = {}

    def value(kind: str, a: PathSpec, b: PathSpec, point: np.ndarray, sigma: float) -> float:
        t, f = predict_pair(point, a, b, t_s, truth, sites)
        key = tuple(sorted([(a.sat_id, a.site_id), (b.sat_id, b.site_id)]))
        if key not in bias:
            bias[key] = rng.standard_normal() * unc.tdoa_bias_s
        if kind == "tdoa":
            return float(t) - clock[a.site_id] + clock[b.site_id] + bias[key] + rng.standard_normal() * sigma
        return (float(f) - a.f_tune_hz * site_osc[a.site_id] + b.f_tune_hz * site_osc[b.site_id]
                + a.translation_hz * sat_osc[a.sat_id] - b.translation_hz * sat_osc[b.sat_id] + rng.standard_normal() * sigma)

    out = []
    for row in rows:
        z = value(row.kind, row.a, row.b, x, row.sigma)
        correction, cal = 0.0, None
        if row.cal is not None:
            z_ref = value(row.kind, row.cal.a, row.cal.b, row.cal.x_ref, row.cal.sigma)
            t_n, f_n = predict_pair(row.cal.x_ref, row.cal.a, row.cal.b, t_s, orbits, sites)
            correction = z_ref - float(t_n if row.kind == "tdoa" else f_n)
            cal = row.cal
        out.append(Measurement(row.kind, z, row.sigma, row.t_ns, row.a, row.b, correction=correction, cal=cal))
    return out


@pytest.mark.slow
@pytest.mark.parametrize("lat, lon, ref_id", [(20.0, 132.0, None), (4.0, 150.0, None), (14.0, 144.0, "ref-guam")])
def test_prediction_agrees_with_a_monte_carlo_of_the_solver(cfg: SystemConfig, jammer: EmitterModel, lat: float,
                                                           lon: float, ref_id: str | None) -> None:
    """The predicted 95 % semi-major axis matches the scatter of solver.solve over 120
    trials with noise and systematic errors drawn as above, within 20 % (the sampling
    error of a 95 % semi-major estimated from 120 trials is about 7 %)."""
    region = Region(lat_min=lat - 2.0, lat_max=lat + 2.0, lon_min=lon - 2.0, lon_max=lon + 2.0)
    out = accuracy_map(cfg, PAIRS, jammer, F_J, T0, region, 2.0, reference_carrier_id=ref_id)
    predicted = cell(out, "semi_major_95_m", lat, lon)
    assert predicted is not None
    rows = hand_rows(cfg, jammer, lat, lon, ref_id)
    height = cfg.processing.solver.height_m
    x = lla_to_ecef(lat, lon, height)
    meridional, prime = radii_of_curvature(lat)
    rng = np.random.default_rng(2024 + int(lat))
    errors = []
    for k in range(120):
        sol, reason = solve(simulate_rows(cfg, rows, x, rng), cfg.orbits(), cfg.sites_xyz(), cfg.processing.solver,
                            cfg.processing.region, cfg.resolved_uncertainty(), starts=[(lat, lon)])
        assert sol is not None, reason
        errors.append([(prime + height) * math.cos(math.radians(lat)) * math.radians(sol.lon_deg - lon),
                       (meridional + height) * math.radians(sol.lat_deg - lat)])
    cov = np.cov(np.array(errors).T)
    empirical = SCALE_95 * math.sqrt(float(np.linalg.eigvalsh(cov)[-1]))
    assert empirical == pytest.approx(predicted, rel=0.2)
