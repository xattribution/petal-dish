"""Tests of the Monte Carlo driver (sim/montecarlo.py): simulated IQ -> CAF -> solver.

The statistical tests size their tolerances from the trial count: a containment
fraction of ``N`` trials has a binomial standard error ``sqrt(0.95 * 0.05 / N)`` and a
mean NEES of a 2-D Gaussian error has standard error ``2 / sqrt(N)`` (chi-square with
two degrees of freedom has variance 4); the bounds are three of those. Independent
references are the simulated world's own link budgets and truth measurements,
``downlink_quality_db``, ``geometry.self_peak`` and the configured one-sigma values.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from petal_locate.config.loader import load_config
from petal_locate.config.models import SystemConfig
from petal_locate.core.geometry import self_peak
from petal_locate.core.linkbudget import downlink_quality_db
from petal_locate.core.signals import key128
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.sim import Scenario, SimWorld, load_scenario
from petal_locate.sim import montecarlo as mc
from petal_locate.sim.montecarlo import (
    DEFAULT_T_NS,
    draw_truth_errors,
    ephemeris_allowance,
    format_summary,
    plan_event,
    run_trials,
    trial_seed,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(ROOT / "config" / "pacific-uhf.yaml")


@pytest.fixture(scope="module")
def bare(cfg: SystemConfig) -> SystemConfig:
    """pacific-uhf without carriers: the captures hold the test jammer only."""
    data = cfg.export_dict()
    data["carriers"] = []
    return SystemConfig.model_validate(data)


@pytest.fixture(scope="module")
def clean() -> Scenario:
    """One open-ocean noise jammer, phase centre on the ellipsoid (the solver's height), no
    truth errors."""
    return Scenario.model_validate({
        "name": "mc-clean",
        "seed": 11,
        "description": "Simulated test scenario (not real data): one noise jammer at a fictional open-ocean point.",
        "emitters": [{"id": "j1", "name": "test jammer (simulated)", "lat_deg": 20.0, "lon_deg": 132.0,
                      "alt_m": 0.0, "antenna_height_m": 0.0, "freq_hz": 301.005e6, "eirp_dbw": 20.0,
                      "waveform": {"type": "noise", "bandwidth_hz": 10e3}}],
    })


@pytest.fixture(scope="module")
def demo() -> Scenario:
    return load_scenario(ROOT / "config" / "scenarios" / "pacific-demo.yaml")


def caf_errors(result: dict[str, Any], kind: str) -> np.ndarray:
    """(estimate - truth) / reported sigma of every CAF result of a run."""
    unit = "s" if kind == "tdoa" else "hz"
    out = []
    for trial in result["trials"]:
        for p in trial["pairs"]:
            if p.get(f"{kind}_{unit}") is not None:
                out.append((p[f"{kind}_{unit}"] - p[f"truth_{kind}_{unit}"]) / p[f"sigma_{kind}_{unit}"])
    return np.array(out)


# --------------------------------------------------------------------------- plan


def test_plan_follows_the_tasking_rule(cfg: SystemConfig, demo: Scenario) -> None:
    """Primary = highest received C/N0; per other satellite the channel with the best
    downlink quality (or one at the primary's site within 3 dB); captures at the tasking
    rate and tunings; self-peaks of satellites heard by both channels excluded; the
    reference carrier on the same transponders assigned."""
    world = SimWorld(cfg, demo)
    plan = plan_event(cfg, world, "jammer-philsea", DEFAULT_T_NS)
    t_s = sec(DEFAULT_T_NS)
    cn0 = {}
    for sat in ("muos-pac", "uhf-172e", "muos-io"):
        for _, ch in cfg.channels_watching(sat):
            pb = world.path_budget("jammer-philsea", sat, "shared-1", ch.id, t_s)
            if pb is not None:
                cn0[ch.id] = float(pb.cn0_total_dbhz)
    assert plan.primary_channel == max(cn0, key=cn0.get)
    primary_sat = cfg.channel(plan.primary_channel)[1].pointing.satellite_id
    primary_site = cfg.channel(plan.primary_channel)[0].id
    assert sorted(p.b.sat_id for p in plan.pairs) == sorted({"muos-pac", "uhf-172e", "muos-io"} - {primary_sat})
    for task in plan.pairs:
        q = {ch.id: downlink_quality_db(cfg, ch.id, task.b.sat_id, "shared-1", t_s)
             for _, ch in cfg.channels_watching(task.b.sat_id)}
        q = {k: v for k, v in q.items() if v is not None}
        best = max(q.values())
        local = [k for k, v in q.items() if cfg.channel(k)[0].id == primary_site and v >= best - 3.0]
        assert task.b_channel == (max(local, key=q.get) if local else max(q, key=q.get))
        assert task.a_channel == plan.primary_channel
        assert (task.plan.sample_rate_hz, task.plan.n_samples) == (20e3, 20000)
        assert task.a.f_tune_hz == 301.005e6 + cfg.satellite(task.a.sat_id).transponder("shared-1").translation_hz
        assert task.b.f_tune_hz == 301.005e6 + cfg.satellite(task.b.sat_id).transponder("shared-1").translation_hz
        assert task.reference is not None and task.reference.carrier_id == "ref-guam"
        assert task.reference.plan.f_e_nom_hz == 300.9925e6
        for sat in task.search.heard_a:
            if sat in task.search.heard_b:
                t_x, f_x = self_peak(sat, cfg.satellite(sat).transponder("shared-1").translation_hz, 301.005e6,
                                     task.a, task.b, task.search.t_mid_s, cfg.orbits(), cfg.sites_xyz())
                assert any(abs(e[0] - t_x) < 1e-12 and abs(e[1] - f_x) < 1e-9 for e in task.search.exclude)


def test_draw_truth_errors_follow_the_configured_sigmas(cfg: SystemConfig) -> None:
    unc = cfg.resolved_uncertainty()
    rng = np.random.Generator(np.random.Philox(key=key128("test-draws")))
    draws = [draw_truth_errors(cfg, unc, rng, DEFAULT_T_NS) for _ in range(1500)]
    tol = 4.0 / math.sqrt(2 * 1500)  # four standard errors of a standard deviation
    pos = np.array([d["ephemeris"]["uhf-172e"]["ric_pos_m"] for d in draws])
    vel = np.array([d["ephemeris"]["muos-io"]["ric_vel_mps"] for d in draws])
    assert np.allclose(np.std(pos, axis=0) / unc.sat_pos_ric_m["uhf-172e"], 1.0, atol=tol)
    assert np.allclose(np.std(vel, axis=0) / unc.sat_vel_ric_mps["muos-io"], 1.0, atol=tol)
    clock = np.array([d["site_clock_offset_ns"]["tokyo"] for d in draws])
    assert np.std(clock) / (unc.site_clock_s["tokyo"] * 1e9) == pytest.approx(1.0, abs=tol)
    osc = np.array([d["sat_osc_frac"]["muos-pac"] for d in draws])
    assert np.std(osc) / unc.sat_osc_frac["muos-pac"] == pytest.approx(1.0, abs=tol)
    assert abs(np.mean(osc)) < 4.0 * unc.sat_osc_frac["muos-pac"] / math.sqrt(1500)
    assert all(ns_from_iso(d["ephemeris"][s]["t_ref"]) == DEFAULT_T_NS for d in draws[:3] for s in d["ephemeris"])
    Scenario.model_validate({"name": "x", "errors": draws[0]}).validate_against(cfg)


# --------------------------------------------------------------------------- trials


def test_smoke_run_is_deterministic_per_trial_and_labelled(bare: SystemConfig, clean: Scenario) -> None:
    calls: list[tuple[int, int]] = []
    two = run_trials(bare, clean, "j1", 2, seed=1, capture_s=0.25, progress=lambda d, n: calls.append((d, n)))
    one = run_trials(bare, clean, "j1", 1, seed=1, capture_s=0.25)
    assert calls == [(1, 2), (2, 2)]
    assert two["simulated"] is True and "Simulated" in two["description"]
    assert one["trials"][0] == two["trials"][0]  # trial 0 does not depend on how many follow
    assert [t["scenario_seed"] for t in two["trials"]] == [trial_seed(1, 0), trial_seed(1, 1)]
    a, b = two["trials"]
    assert "miss_m" in a and "miss_m" in b, (a.get("reason"), b.get("reason"))
    assert a["lat_deg"] != b["lat_deg"]  # independent noise in every trial
    s = two["summary"]
    assert s["n_solved"] == 2 and s["failures"] == {}
    assert s["median_miss_m"] == pytest.approx(np.median([a["miss_m"], b["miss_m"]]))
    for trial in two["trials"]:
        assert trial["n_measurements"] == 4
        for p in trial["pairs"]:
            assert p["reason"] is None and p["snr_db"] > 25.0
            assert abs(p["tdoa_s"] - p["truth_tdoa_s"]) < 5.0 * p["sigma_tdoa_s"]
            assert abs(p["fdoa_hz"] - p["truth_fdoa_hz"]) < 5.0 * p["sigma_fdoa_hz"]
    text = format_summary(two)
    assert text.startswith("SIMULATED") and "NEES" in text


def test_drawn_errors_and_pair_bias_reach_the_measurements(bare: SystemConfig, clean: Scenario) -> None:
    """With drawn errors the CAF measures the drawn world's truth plus the pair bias:
    channel B rendered late by the bias raises the TDOA by it. A 3 us bias sigma makes the
    bias, and its sign, visible against the estimator's 0.25 us sigma."""
    data = bare.export_dict()
    data.setdefault("processing", {})["uncertainty"] = {"tdoa_bias_ns": 3000.0}
    config = SystemConfig.model_validate(data)
    out = run_trials(config, clean, "j1", 1, seed=4, draw_errors=True, use_calibration=False, capture_s=0.25)
    trial = out["trials"][0]
    assert set(trial["errors"]["ephemeris"]) == {"muos-pac", "uhf-172e", "muos-io"}
    biases = trial["tdoa_bias_ns"]
    assert len(biases) == 2 and max(abs(b) for b in biases) > 1000
    drawn = SimWorld(config, Scenario.model_validate({**clean.model_dump(mode="json"), "errors": trial["errors"]}))
    undrawn = SimWorld(config, clean)
    plan = plan_event(config, undrawn, "j1", out["t_ns"], capture_s=0.25, use_calibration=False)
    for k, p in enumerate(trial["pairs"]):
        task = plan.pairs[k]
        t_mid = task.plan.t_mid_ns(out["t_ns"])
        t_true, f_true = drawn.truth_measurement("j1", task.a, task.b, t_mid)
        t_free, f_free = undrawn.truth_measurement("j1", task.a, task.b, t_mid)
        assert abs(p["tdoa_s"] - (t_true + biases[k] * 1e-9)) < 5.0 * p["sigma_tdoa_s"]
        assert abs(p["fdoa_hz"] - f_true) < 5.0 * p["sigma_fdoa_hz"]
        if abs(biases[k]) * 1e-9 > 10.0 * p["sigma_tdoa_s"]:
            assert abs(p["tdoa_s"] - t_true) > 5.0 * p["sigma_tdoa_s"]
        # the drawn satellite oscillators (1e-6 of a 41 MHz translation) move the FDOA by hertz
        assert abs(f_true - f_free) > 100.0 * p["sigma_fdoa_hz"]
        assert out["pairs"][k]["reference_note"] == "calibration not requested"


def test_invalid_arguments_raise(bare: SystemConfig, clean: Scenario) -> None:
    with pytest.raises(ValueError, match="n_trials"):
        run_trials(bare, clean, "j1", 0)
    with pytest.raises(ValueError, match="emitter_id"):
        run_trials(bare, clean, "nobody", 1)
    with pytest.raises(ValueError, match="t_ns"):
        run_trials(bare, clean, "j1", 1, t_ns=float(DEFAULT_T_NS))
    with pytest.raises(ValueError, match="pairs\\[0\\]"):
        run_trials(bare, clean, "j1", 1, pairs=[("manila-pac", "tokyo-pac")])
    late = Scenario.model_validate({**clean.model_dump(mode="json"),
                                    "emitters": [{**clean.emitters[0].model_dump(mode="json", exclude_none=True),
                                                  "t_start": "2026-06-01T00:00:00Z"}]})
    with pytest.raises(ValueError, match="t_ns"):
        run_trials(bare, late, "j1", 1, t_ns=DEFAULT_T_NS)


# --------------------------------------------------------------------------- statistics (slow)


@pytest.mark.slow
def test_noise_only_trials_scatter_as_p_noise_predicts(bare: SystemConfig, clean: Scenario) -> None:
    """No truth errors: estimates centred on the truth, mean NEES with P_noise about 2 and
    95 % of them inside the P_noise ellipse; the P_total ellipse (which also covers the
    undrawn systematics) contains at least as many. 80 trials: containment within
    +/- 0.073 of 0.95, mean NEES within +/- 0.67 of 2."""
    n = 80
    out = run_trials(bare, clean, "j1", n, seed=21, draw_errors=False, use_calibration=False, capture_s=0.5)
    s = out["summary"]
    assert s["n_solved"] == n, s["failures"]
    mean, stderr = np.array(s["mean_error_en_m"]), np.array(s["stderr_error_en_m"])
    assert np.all(np.abs(mean) < 3.0 * stderr + 5.0), (mean, stderr)
    assert abs(s["containment_95_noise"] - 0.95) <= 3.0 * math.sqrt(0.95 * 0.05 / n)
    assert abs(s["mean_nees_noise"] - 2.0) <= 3.0 * 2.0 / math.sqrt(n)
    assert s["containment_95_total"] >= s["containment_95_noise"]
    # the estimator's sigmas describe its scatter (2 n results per kind)
    for kind in ("tdoa", "fdoa"):
        z = caf_errors(out, kind)
        assert z.size == 2 * n
        assert abs(np.mean(z)) < 3.0 / math.sqrt(z.size) + 0.1
        assert abs(np.std(z) - 1.0) < 3.0 / math.sqrt(2 * z.size) + 0.05
    # predicted noise CEP50 against the median miss (a median of 80 is good to about 15 %)
    assert s["cep50_empirical_m"] == pytest.approx(s["cep50_noise_predicted_m"], rel=0.35)


@pytest.mark.slow
def test_drawn_errors_scatter_as_p_total_predicts(bare: SystemConfig, clean: Scenario) -> None:
    """MATH.md section 12 item 10 through the full chain: with ephemeris, oscillator, clock
    and pair-bias errors drawn from the configured sigmas in every trial, mean NEES with
    P_total is about 2 and 95 % of the fixes fall inside its ellipse, while NEES with
    P_noise is far larger. 80 trials, tolerances as above."""
    n = 80
    out = run_trials(bare, clean, "j1", n, seed=22, draw_errors=True, use_calibration=False, capture_s=0.5)
    s = out["summary"]
    assert s["n_solved"] == n, s["failures"]
    assert abs(s["mean_nees_total"] - 2.0) <= 3.0 * 2.0 / math.sqrt(n)
    assert abs(s["containment_95_total"] - 0.95) <= 3.0 * math.sqrt(0.95 * 0.05 / n)
    assert s["mean_nees_noise"] > 20.0 * s["mean_nees_total"]
    assert s["containment_95_noise"] < 0.5


@pytest.fixture(scope="module")
def ephemeris_cfg(cfg: SystemConfig) -> SystemConfig:
    """pacific-uhf with ephemeris errors dominating (5 km in-track and cross-track) and the
    configured Guam reference (20 dBW omni: its own noise, about 0.1 us, matters little)."""
    data = cfg.export_dict()
    for sat in data["satellites"]:
        sat["ephemeris_sigma"] = {"pos_m": [500.0, 5000.0, 5000.0], "vel_mps": [0.001, 0.001, 0.001]}
    data.setdefault("processing", {})["uncertainty"] = {"sat_osc_frac": 1e-8, "site_osc_frac": 1e-12, "tdoa_bias_ns": 5.0}
    for site in data["sites"]:
        site["clock"] = {"sigma_ns": 5.0}
    return SystemConfig.model_validate(data)


@pytest.fixture(scope="module")
def tle_cfg(cfg: SystemConfig) -> SystemConfig:
    """pacific-uhf with every satellite at the TLE-grade one-sigma ephemeris."""
    data = cfg.export_dict()
    tle = cfg.processing.uncertainty.ephemeris.tle
    for sat in data["satellites"]:
        sat["ephemeris_sigma"] = {"pos_m": list(tle.pos_m), "vel_mps": list(tle.vel_mps)}
    return SystemConfig.model_validate(data)


def test_ephemeris_allowance_matches_tasking(cfg: SystemConfig, tle_cfg: SystemConfig, demo: Scenario) -> None:
    """Uncalibrated windows (and every reference window) are widened by 6 |sigma_pos| / c per
    satellite in delay and 6 f |sigma_vel| / c in Doppler, as server/pipeline/tasking.py
    does. Without it a reference carrier, searched in a box around its prediction, is lost
    when the satellites are off by TLE-class errors (seed 1: the second pair's reference
    falls 'below threshold' and that pair goes uncalibrated)."""
    c = 299_792_458.0
    pos = math.sqrt(100.0**2 + 1000.0**2 + 1000.0**2)  # the configured operator sigmas, written out
    vel = math.sqrt(0.02**2 + 0.02**2 + 0.05**2)
    t, f = ephemeris_allowance(cfg.resolved_uncertainty(), "muos-pac", "uhf-172e", 301.005e6)
    assert t == pytest.approx(6.0 * 2.0 * pos / c, rel=1e-12) and f == pytest.approx(6.0 * 301.005e6 * 2.0 * vel / c, rel=1e-12)
    assert ephemeris_allowance(None, "muos-pac", "uhf-172e", 301.005e6) == (0.0, 0.0)
    kw: dict[str, Any] = dict(seed=1, draw_errors=True, use_calibration=True, capture_s=0.5)
    with_pad = run_trials(tle_cfg, demo, "jammer-philsea", 1, **kw)
    assert all(p["calibrated"] == ["tdoa", "fdoa"] for p in with_pad["trials"][0]["pairs"])
    original = mc._with_allowance
    try:
        mc._with_allowance = lambda search, uncertainty, correction: search  # type: ignore[assignment]
        without = run_trials(tle_cfg, demo, "jammer-philsea", 1, **kw)
    finally:
        mc._with_allowance = original
    assert [p["calibrated"] for p in without["trials"][0]["pairs"]] == [["tdoa", "fdoa"], []]
    assert without["trials"][0]["pairs"][1]["reference"]["reason"] == "below threshold"


def test_cw_is_band_limited_like_tasking(cfg: SystemConfig) -> None:
    """A CW has no occupied band; tasking still filters to max(detected width, 10 Hz) plus the
    Doppler reach, and so does the driver. Unfiltered, the 8 kHz Sapporo reference, whose
    upper edge (301.1215 MHz) lies inside the CW's 20 kHz capture, correlated too and every
    pair came back 'ambiguous'; filtered, every pair gives FDOA only ('narrowband')."""
    sc = load_scenario(ROOT / "config" / "scenarios" / "uplink-cw.yaml")
    res = run_trials(cfg, sc, "jammer-cw", 1, seed=31, use_calibration=False, t_ns=ns_from_iso("2026-01-01T03:00:00Z"))
    pairs = res["trials"][0]["pairs"]
    assert len(pairs) == 2
    for p in pairs:
        assert p["reason"] == "narrowband" and p["tdoa_s"] is None and p["fdoa_hz"] is not None
        assert abs(p["fdoa_hz"] - p["truth_fdoa_hz"]) < 4.0 * p["sigma_fdoa_hz"]
        assert p["noise_bandwidth_hz"] < 2_000.0  # filtered (unfiltered it is the 20 kHz capture rate)


@pytest.mark.slow
def test_calibration_reduces_the_miss_when_ephemeris_errors_are_drawn(ephemeris_cfg: SystemConfig, demo: Scenario) -> None:
    """The same drawn errors and noise (same seed), with and without the reference
    calibration: calibration cancels the ephemeris error common to target and reference.
    The test asks for less than half the miss and less than 0.3 of the predicted CEP50
    (the reference carrier is the configured omni design)."""
    n = 8
    kw: dict[str, Any] = dict(seed=5, draw_errors=True, capture_s=0.5)
    cal = run_trials(ephemeris_cfg, demo, "jammer-philsea", n, use_calibration=True, **kw)
    raw = run_trials(ephemeris_cfg, demo, "jammer-philsea", n, use_calibration=False, **kw)
    assert cal["summary"]["n_solved"] == n and raw["summary"]["n_solved"] == n
    for a, b in zip(cal["trials"], raw["trials"]):
        assert a["errors"] == b["errors"] and a["tdoa_bias_ns"] == b["tdoa_bias_ns"]
        assert all(p["calibrated"] == ["tdoa", "fdoa"] for p in a["pairs"])
        assert a["dominant_error"] != "ephemeris_position" and b["dominant_error"] == "ephemeris_position"
    assert cal["summary"]["median_miss_m"] < 0.5 * raw["summary"]["median_miss_m"]
    assert cal["summary"]["cep50_predicted_m"] < 0.3 * raw["summary"]["cep50_predicted_m"]


# --------------------------------------------------------------------------- review fixes


def test_draws_include_the_propagation_media(cfg: SystemConfig) -> None:
    """One VTEC and one zenith delay per draw, magnitudes of normals with the configured
    sigmas (their RMS is the sigma), enabled when non-zero; a valid TruthErrors mapping."""
    unc = cfg.resolved_uncertainty()
    rng = np.random.Generator(np.random.Philox(key=key128("test-media-draws")))
    draws = [draw_truth_errors(cfg, unc, rng, DEFAULT_T_NS) for _ in range(1500)]
    vtec = np.array([d["iono"]["vtec_tecu"] for d in draws])
    zenith = np.array([d["tropo"]["zenith_m"] for d in draws])
    tol = 4.0 / math.sqrt(2 * 1500)
    assert np.all(vtec >= 0.0) and np.all(zenith >= 0.0)
    assert math.sqrt(np.mean(vtec**2)) / unc.iono_vtec_tecu == pytest.approx(1.0, abs=2 * tol)
    assert math.sqrt(np.mean(zenith**2)) / unc.tropo_zenith_m == pytest.approx(1.0, abs=2 * tol)
    assert all(d["iono"]["enabled"] and d["tropo"]["enabled"] for d in draws[:5])
    Scenario.model_validate({"name": "x", "errors": draws[0]}).validate_against(cfg)
    off = mc.Uncertainty(**{**unc.__dict__, "iono_vtec_tecu": 0.0, "tropo_zenith_m": 0.0})
    d = draw_truth_errors(cfg, off, rng, DEFAULT_T_NS)
    assert not d["iono"]["enabled"] and not d["tropo"]["enabled"]


def test_scenario_media_kept_and_rows_share_the_primary_capture(bare: SystemConfig, clean: Scenario,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """With drawn errors a scenario that sets its own ionosphere keeps it (a study at that
    level); every row names its two captures, all pairs share the primary's, and carries the
    estimator's noise shares (MATH-2)."""
    seen: list[Any] = []
    real = mc.solve

    def spy(rows: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(list(rows))
        return real(rows, *args, **kwargs)

    monkeypatch.setattr(mc, "solve", spy)
    data = clean.model_dump(mode="json")
    data["errors"]["iono"] = {"enabled": True, "vtec_tecu": 10.0}
    out = run_trials(bare, Scenario.model_validate(data), "j1", 1, seed=5, draw_errors=True, use_calibration=False,
                     capture_s=0.25)
    assert out["trials"][0]["errors"]["iono"] == {"enabled": True, "vtec_tecu": 10.0}
    assert out["trials"][0]["errors"]["tropo"]["enabled"]  # drawn (the scenario has none)
    rows = seen[0]
    assert len(rows) >= 4
    assert len({r.capture_a for r in rows}) == 1 and rows[0].capture_a.startswith(out["primary_channel"] + "|")
    assert len({r.capture_b for r in rows}) == len(out["pairs"])
    assert all(0.0 < r.noise_share_a < 1.0 and 0.0 < r.noise_share_b < 1.0 for r in rows)


def test_explicit_pairs_resolve_multi_satellite_channels(cfg: SystemConfig, demo: Scenario) -> None:
    """A fixed wide beam watching two satellites that relay the carrier: an explicit pair
    takes the satellite the other end does not use, or the one named 'channel@sat'
    (ADAPT-4); a channel paired with itself is refused."""
    data = cfg.export_dict()
    hilo = next(s for s in data["sites"] if s["id"] == "hilo")
    hilo["channels"] = [{
        "id": "hilo-fix", "antenna": {"type": "beam", "gain_dbi": 6.0, "hpbw_deg": 90.0, "front_to_back_db": 15.0},
        "pointing": {"az_deg": 250.0, "el_deg": 60.0, "satellite_ids": ["muos-pac", "uhf-172e"]},
    }]
    fixed = SystemConfig.model_validate(data)
    world = SimWorld(fixed, demo)
    emitter = demo.emitters[0].id
    t = DEFAULT_T_NS + 3 * 3600 * 10**9
    plan = plan_event(fixed, world, emitter, t, pairs=[("hilo-fix", "honolulu-pac")], use_calibration=False)
    assert (plan.pairs[0].a.sat_id, plan.pairs[0].b.sat_id) == ("uhf-172e", "muos-pac")
    assert plan.pairs[0].a_channel == "hilo-fix"
    plan = plan_event(fixed, world, emitter, t, pairs=[("hilo-fix@muos-pac", "honolulu-172e")], use_calibration=False)
    assert (plan.pairs[0].a.sat_id, plan.pairs[0].b.sat_id) == ("muos-pac", "uhf-172e")
    with pytest.raises(ValueError, match="paired with itself"):
        plan_event(fixed, world, emitter, t, pairs=[("hilo-fix@muos-pac", "hilo-fix@uhf-172e")])


@pytest.mark.slow
def test_calibrated_fixes_contain_a_30_tecu_ionosphere(cfg: SystemConfig) -> None:
    """MATH-1 through the full chain: South China Sea, calibrated against Guam, the
    simulator's ionosphere at 30 TECU and drawn errors. Before the ionospheric consider
    term no fix was inside its own 95 % ellipse (mean NEES 21 to 29); with the default
    term the mean NEES is near 2. 16 trials: mean NEES within 3 x 2 / sqrt(16) of 2."""
    scenario = Scenario.model_validate({
        "name": "iono-30", "seed": 1003, "description": "Simulated test scenario (not real data).",
        "emitters": [{"id": "j", "name": "test jammer (simulated)", "kind": "jammer", "lat_deg": 15.0,
                      "lon_deg": 115.0, "freq_hz": 301.005e6, "eirp_dbw": 20.0,
                      "waveform": {"type": "noise", "bandwidth_hz": 10e3}}],
        "errors": {"iono": {"enabled": True, "vtec_tecu": 30.0}},
    })
    n = 16
    out = run_trials(cfg, scenario, "j", n, seed=1003, draw_errors=True, use_calibration=True,
                     t_ns=ns_from_iso("2026-01-01T03:00:00Z"))
    s = out["summary"]
    assert s["n_solved"] == n, s["failures"]
    assert abs(s["mean_nees_total"] - 2.0) <= 3.0 * 2.0 / math.sqrt(n)
    assert s["containment_95_total"] >= 0.8


def test_the_reference_is_the_eligible_one_nearest_the_emitter(cfg: SystemConfig, demo: Scenario) -> None:
    """R7 / K6: a pair is calibrated against the reference tasking would use once it has a
    fix, the eligible carrier nearest the emitter, not the first one in configuration
    order; an explicit carrier is still honoured. A carrier at Sapporo, listed first and
    relayed by shared-1 like Guam's, is 2,700 km from the demo jammer (20 N 132 E); Guam
    is 1,500 km away."""
    data = cfg.export_dict()
    guam = next(c for c in data["carriers"] if c["id"] == "ref-guam")
    north = {**guam, "id": "ref-north", "name": "Reference carrier, Sapporo city center (illustrative, test)",
             "location": dict(next(c for c in data["carriers"] if c["id"] == "ref-sapporo")["location"])}
    data["carriers"] = [north, *data["carriers"]]
    two = SystemConfig.model_validate(data)
    world = SimWorld(two, demo)
    plan = plan_event(two, world, "jammer-philsea", DEFAULT_T_NS)
    assert plan.pairs and all(t.reference is not None and t.reference.carrier_id == "ref-guam" for t in plan.pairs)
    named = plan_event(two, world, "jammer-philsea", DEFAULT_T_NS, reference_carrier_id="ref-north")
    assert all(t.reference is not None and t.reference.carrier_id == "ref-north" for t in named.pairs)
