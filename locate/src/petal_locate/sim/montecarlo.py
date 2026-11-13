"""Monte Carlo driver: simulated IQ -> cross-ambiguity estimation -> position solver, no network.

``run_trials`` runs the measurement chain of one event the way the server's tasking
runs it (ARCHITECTURE.md section 7), against a ``SimWorld`` instead of radios:

- **Pairs.** The primary channel is the one that receives the emitter with the highest
  C/N0 (the link budget of the simulated world stands in for the detector's
  ``snr_db``); for every other satellite with a transponder overlapping the uplink
  range, the channel watching it with the highest ``downlink_quality_db``, preferring a
  channel at the primary's site when within 3 dB. Or the caller lists the pairs.
- **Captures.** ``core.accuracy.capture_plan``: identical sample rate and first-sample
  time in both requests, each channel tuned to ``f_e_nom + translation_hz``.
- **Windows.** ``core.accuracy.pair_search``: ``measurement_windows`` +
  ``search_windows``, self-peaks of satellites heard by both channels excluded, a
  mirror window for co-channel satellites.
- **Calibration.** For each pair, the first reference carrier of the configuration
  relayed by the pair's two transponders is captured on its own frequency through the
  same channels at the same time, estimated with windows around its predicted values,
  and turned into corrections with ``solver.calibration_correction``; the target
  windows are then shifted by them and keep only the fixed margins.
- **Solve.** ``solver.solve`` with every accepted TDOA/FDOA row (CAF results that are
  undetected, ambiguous, coupled or mirror-ambiguous are recorded with their reason
  and not used, as in tasking; a mirror-ambiguous reference gives no calibration), the
  configured solver settings, region and ``resolved_uncertainty()``, global search.

Every trial is independent and deterministic in ``(seed, trial index)``: the trial's
scenario seed (which keys every waveform, the relayed transponder noise and the site
thermal noise) and its drawn truth errors come from hashes of the two. With
``draw_errors`` each trial replaces the scenario's truth errors by a fresh draw from
the configured one-sigma uncertainties: satellite ephemeris (RIC position and
velocity, ``t_ref`` at the measurement epoch, which is how the solver defines its
velocity parameter), satellite and site oscillators, site clocks, and the per-pair
TDOA bias, which the simulator has no model for and which is therefore applied by
rendering channel B of a pair that much later than its label (so its reference
capture carries the same bias, as a hardware delay would).

Everything returned is simulated and labelled so.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from petal_locate.config.models import SystemConfig
from petal_locate.core.accuracy import (
    CapturePlan,
    PairSearch,
    capture_plan,
    channel_satellite,
    pair_search,
    relaying_transponder,
)
from petal_locate.core.caf import CafResult, caf_estimate
from petal_locate.core.constants import T_REF_S
from petal_locate.core.geodesy import geodesic_distance_m, radii_of_curvature, wrap_lon_deg
from petal_locate.core.linkbudget import downlink_quality_db
from petal_locate.core.signals import key128
from petal_locate.core.solver import Measurement, Solution, calibration_correction, cep50, solve
from petal_locate.core.timebase import iso_from_ns, sec
from petal_locate.core.types import CalRef, PathSpec, Uncertainty
from petal_locate.sim.scenario import Scenario
from petal_locate.sim.world import SimWorld

__all__ = [
    "DEFAULT_T_NS",
    "REASON_MIRROR_AMBIGUOUS",
    "EventPlan",
    "PairTask",
    "ReferenceTask",
    "plan_event",
    "trial_seed",
    "draw_truth_errors",
    "run_trials",
    "format_summary",
]

DEFAULT_T_NS: int = int(T_REF_S) * 1_000_000_000
"""Default capture start, 2026-01-01T00:00:00Z (integer UTC ns), unless the emitter starts later."""

REASON_MIRROR_AMBIGUOUS = "mirror ambiguous"
"""Reason recorded for a CAF result flagged ``mirror_ambiguous`` (never solved, as in tasking)."""

_SAME_SITE_PREFERENCE_DB = 3.0  # a channel at the primary's site is preferred within this of the best
_CHI2_2_95 = -2.0 * math.log(0.05)  # 5.991: the 95 % ellipse of a 2-D Gaussian


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class ReferenceTask:
    """Calibration of one pair against a reference carrier.

    carrier_id: the configuration carrier. x_ref: its surveyed phase centre, ECEF m.
    a, b: its paths through the pair's satellites, sites and transponders, on its own
    nominal frequency. plan, search: its capture plan and search geometry (windows
    around the prediction at ``x_ref``).
    """

    carrier_id: str
    x_ref: np.ndarray
    a: PathSpec
    b: PathSpec
    plan: CapturePlan
    search: PairSearch


@dataclass(frozen=True)
class PairTask:
    """One channel pair of the event: paths, capture plan, search geometry, calibration.

    a_channel, b_channel: receive channels (A is the primary). a, b: their paths at the
    event's nominal uplink frequency. a_transponder, b_transponder: transponder ids.
    plan, search: capture plan and search geometry (``core.accuracy``). reference:
    the calibration task, or None (``reference_note`` says why).
    """

    a_channel: str
    b_channel: str
    a: PathSpec
    b: PathSpec
    a_transponder: str
    b_transponder: str
    plan: CapturePlan
    search: PairSearch
    reference: ReferenceTask | None
    reference_note: str | None

    def summary(self) -> dict[str, Any]:
        """JSON-ready description."""
        return {
            "a_channel": self.a_channel,
            "b_channel": self.b_channel,
            "a_sat_id": self.a.sat_id,
            "b_sat_id": self.b.sat_id,
            "a_site_id": self.a.site_id,
            "b_site_id": self.b.site_id,
            "a_transponder_id": self.a_transponder,
            "b_transponder_id": self.b_transponder,
            "f_e_nom_hz": self.plan.f_e_nom_hz,
            "a_center_hz": self.a.f_tune_hz,
            "b_center_hz": self.b.f_tune_hz,
            "sample_rate_hz": self.plan.sample_rate_hz,
            "n_samples": self.plan.n_samples,
            "co_channel": self.search.co_channel,
            "exclude": [list(p) for p in self.search.exclude],
            "reference_carrier_id": None if self.reference is None else self.reference.carrier_id,
            "reference_note": self.reference_note,
        }


@dataclass(frozen=True)
class EventPlan:
    """The tasking of one event: its uplink band, epoch and pairs.

    f_lo_hz, f_hi_hz: uplink range, Hz. t_start_ns: first-sample time of every capture
    (labelled, integer UTC ns). primary_channel: channel A of every pair. pairs: the
    pair tasks. skipped: satellites that could not be paired, with the reason.
    """

    emitter_id: str
    f_lo_hz: float
    f_hi_hz: float
    t_start_ns: int
    primary_channel: str
    pairs: tuple[PairTask, ...]
    skipped: tuple[str, ...] = field(default_factory=tuple)


def plan_event(
    config: SystemConfig,
    world: SimWorld,
    emitter_id: str,
    t_start_ns: int,
    *,
    pairs: Sequence[tuple[str, str]] | None = None,
    use_calibration: bool = True,
    capture_s: float | None = None,
    fs: float | None = None,
    reference_carrier_id: str | None = None,
) -> EventPlan:
    """Pairs, captures, windows and calibrations for one emitter (ARCHITECTURE.md section 7).

    world: the simulated world (its link budgets choose the primary channel, standing in
    for the detection with the highest ``snr_db``; nothing else is taken from truth).
    t_start_ns: first-sample time of the captures, integer UTC ns. pairs: explicit
    ``[(channel_a, channel_b), ...]`` instead of the automatic choice. capture_s:
    capture length, s (replaces ``capture.duration_s``, for targets and references).
    fs: target sample rate, Hz (replaces the rule; references keep theirs).
    reference_carrier_id: calibrate against this carrier only.

    The uplink range is the emitter's occupied band, ``freq_hz +/- bandwidth_hz / 2``
    (the detection's mapped range in the server). Raises ValueError naming the argument
    when the emitter is relayed into no channel or an explicit pair is invalid.
    """
    emitter = world.emitter(emitter_id)
    t_s = sec(t_start_ns)
    f_lo = emitter.freq_hz - 0.5 * emitter.bandwidth_hz
    f_hi = emitter.freq_hz + 0.5 * emitter.bandwidth_hz
    f_nom = float(round(0.5 * (f_lo + f_hi)))
    skipped: list[str] = []

    if pairs is None:
        chosen = _automatic_pairs(config, world, emitter_id, f_lo, f_hi, f_nom, t_s, skipped)
    else:
        chosen = []
        for k, item in enumerate(pairs):
            try:
                ch_a, ch_b = item
            except (TypeError, ValueError):
                raise ValueError(f"pairs[{k}] must be (channel_a, channel_b)") from None
            found_a = channel_satellite(config, ch_a, f_nom, f"pairs[{k}][0]")
            found_b = channel_satellite(config, ch_b, f_nom, f"pairs[{k}][1]")
            if found_a is None or found_b is None:
                raise ValueError(f"pairs[{k}]: channel {ch_a if found_a is None else ch_b!r} watches no satellite relaying {f_nom:.0f} Hz")
            if found_a[0] == found_b[0]:
                raise ValueError(f"pairs[{k}]: both channels use satellite {found_a[0]!r}")
            chosen.append((ch_a, found_a[0], found_a[1], ch_b, found_b[0], found_b[1]))
    if not chosen:
        raise ValueError(f"emitter_id {emitter_id!r}: no channel pair can correlate it at t_ns ({'; '.join(skipped) or 'no pairs'})")

    tasks: list[PairTask] = []
    for ch_a, sat_a, tp_a, ch_b, sat_b, tp_b in chosen:
        plan = capture_plan(config, [ch_a, ch_b], f_lo, f_hi, duration_s=capture_s, sample_rate_hz=fs)
        site_a, site_b = config.channel(ch_a)[0].id, config.channel(ch_b)[0].id
        a = PathSpec(sat_a, site_a, plan.f_e_nom_hz, float(tp_a.translation_hz), float(tp_a.group_delay_s))
        b = PathSpec(sat_b, site_b, plan.f_e_nom_hz, float(tp_b.translation_hz), float(tp_b.group_delay_s))
        t_mid_s = sec(plan.t_mid_ns(t_start_ns))
        search = pair_search(config, a, b, t_mid_s, plan.sample_rate_hz, plan.occupied_bw_hz)
        if search is None:
            skipped.append(f"{ch_a}/{ch_b}: no admissible emitter position")
            continue
        reference, note = None, None
        if use_calibration:
            reference, note = _reference_task(config, ch_a, ch_b, a, b, tp_a, tp_b, t_start_ns, capture_s, reference_carrier_id)
        else:
            note = "calibration not requested"
        tasks.append(PairTask(ch_a, ch_b, a, b, tp_a.id, tp_b.id, plan, search, reference, note))
    if not tasks:
        raise ValueError(f"emitter_id {emitter_id!r}: no pair has an admissible emitter position ({'; '.join(skipped)})")
    return EventPlan(emitter_id, f_lo, f_hi, int(t_start_ns), tasks[0].a_channel, tuple(tasks), tuple(skipped))


def _automatic_pairs(
    config: SystemConfig, world: SimWorld, emitter_id: str, f_lo: float, f_hi: float, f_nom: float, t_s: float,
    skipped: list[str],
) -> list[tuple[str, str, Any, str, str, Any]]:
    """The tasking rule: primary by received C/N0, one partner channel per other satellite."""
    best: tuple[float, str, str, Any] | None = None  # (cn0, channel, sat, transponder)
    for satellite in config.satellites:
        tp = relaying_transponder(satellite, f_nom)
        if tp is None:
            continue
        for _, channel in config.channels_watching(satellite.id):
            pb = world.path_budget(emitter_id, satellite.id, tp.id, channel.id, t_s)
            if pb is None:
                continue
            cn0 = float(pb.cn0_total_dbhz)
            if best is None or cn0 > best[0]:
                best = (cn0, channel.id, satellite.id, tp)
    if best is None:
        skipped.append("the emitter reaches no channel through any satellite")
        return []
    _, primary, primary_sat, primary_tp = best
    primary_site = config.channel(primary)[0].id
    out = []
    seen: set[str] = {primary_sat}
    for satellite, tp in config.transponders_covering(f_lo, f_hi):
        if satellite.id in seen:
            continue
        seen.add(satellite.id)
        relaying = relaying_transponder(satellite, f_nom)
        tp = relaying if relaying is not None else tp
        quality = []
        for site, channel in config.channels_watching(satellite.id):
            q = downlink_quality_db(config, channel.id, satellite.id, tp.id, t_s)
            if q is not None:
                quality.append((q, site.id, channel.id))
        if not quality:
            skipped.append(f"{satellite.id}: no channel watching it sees it")
            continue
        top = max(q for q, _, _ in quality)
        local = [item for item in quality if item[1] == primary_site and item[0] >= top - _SAME_SITE_PREFERENCE_DB]
        pick = max(local) if local else max(quality)
        out.append((primary, primary_sat, primary_tp, pick[2], satellite.id, tp))
    if not out:
        skipped.append(f"no satellite other than {primary_sat} relays {f_lo:.0f}..{f_hi:.0f} Hz into a channel")
    return out


def _reference_task(
    config: SystemConfig, ch_a: str, ch_b: str, a: PathSpec, b: PathSpec, tp_a: Any, tp_b: Any, t_start_ns: int,
    capture_s: float | None, only: str | None,
) -> tuple[ReferenceTask | None, str | None]:
    """The first reference carrier relayed by both of the pair's transponders, or a reason."""
    if not config.processing.calibration.enabled:
        return None, "calibration disabled in the configuration"
    if only is not None:
        carrier = config.carrier(only)
        if carrier.location is None or carrier.emitter is None:
            raise ValueError(f"reference_carrier_id: carrier {only!r} needs a location and an emitter model")
        candidates = [carrier]
    else:
        candidates = list(config.reference_carriers())
    for carrier in candidates:
        f_ref = float(carrier.uplink_center_hz)
        ra_tp = relaying_transponder(config.satellite(a.sat_id), f_ref)
        rb_tp = relaying_transponder(config.satellite(b.sat_id), f_ref)
        if ra_tp is None or rb_tp is None or ra_tp.id != tp_a.id or rb_tp.id != tp_b.id:
            continue
        width = float(carrier.emitter.bandwidth_hz)
        plan = capture_plan(config, [ch_a, ch_b], f_ref - 0.5 * width, f_ref + 0.5 * width, duration_s=capture_s)
        ra = PathSpec(a.sat_id, a.site_id, plan.f_e_nom_hz, a.translation_hz, a.tau_x_s)
        rb = PathSpec(b.sat_id, b.site_id, plan.f_e_nom_hz, b.translation_hz, b.tau_x_s)
        x_ref = np.asarray(config.carrier_xyz(carrier.id), dtype=np.float64)
        search = pair_search(config, ra, rb, sec(plan.t_mid_ns(t_start_ns)), plan.sample_rate_hz, plan.occupied_bw_hz,
                             point_xyz=x_ref)
        if search is None:
            continue
        return ReferenceTask(carrier.id, x_ref, ra, rb, plan, search), None
    return None, "no reference carrier is relayed by both of the pair's transponders"


# --------------------------------------------------------------------------- truth draws


def trial_seed(seed: int, trial: int) -> int:
    """Scenario seed of trial ``trial`` of a run seeded ``seed``: a 31-bit hash of both."""
    digest = hashlib.blake2b(f"montecarlo|{int(seed)}|{int(trial)}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "little") % (2**31)


def draw_truth_errors(config: SystemConfig, uncertainty: Uncertainty, rng: np.random.Generator, t_ref_ns: int) -> dict[str, Any]:
    """Truth errors drawn from the one-sigma uncertainties, as a ``TruthErrors`` mapping.

    Per satellite (configuration order): RIC position (m) and velocity (m/s) offsets
    with ``t_ref`` = ``t_ref_ns`` (the measurement epoch: the solver's velocity
    parameter does not move the satellite at a row's own epoch), and a translation
    oscillator fraction; per site: a clock offset (ns) and an oscillator fraction. Every
    entity consumes the same number of normal draws whatever its sigma (a zero sigma
    gives 0), so the draws of one entity do not depend on the others' sigmas.
    """
    t_ref = iso_from_ns(int(t_ref_ns))
    ephemeris: dict[str, Any] = {}
    sat_osc: dict[str, float] = {}
    for satellite in config.satellites:
        pos = np.asarray(uncertainty.sat_pos_ric_m.get(satellite.id, np.zeros(3)), dtype=np.float64)
        vel = np.asarray(uncertainty.sat_vel_ric_mps.get(satellite.id, np.zeros(3)), dtype=np.float64)
        ephemeris[satellite.id] = {
            "ric_pos_m": [float(v) for v in rng.standard_normal(3) * pos],
            "ric_vel_mps": [float(v) for v in rng.standard_normal(3) * vel],
            "t_ref": t_ref,
        }
        sat_osc[satellite.id] = float(rng.standard_normal() * float(uncertainty.sat_osc_frac.get(satellite.id, 0.0)))
    clock: dict[str, float] = {}
    site_osc: dict[str, float] = {}
    for site in config.sites:
        clock[site.id] = float(rng.standard_normal() * float(uncertainty.site_clock_s.get(site.id, 0.0)) * 1e9)
        site_osc[site.id] = float(rng.standard_normal() * float(uncertainty.site_osc_frac.get(site.id, 0.0)))
    return {"ephemeris": ephemeris, "sat_osc_frac": sat_osc, "site_clock_offset_ns": clock, "site_osc_frac": site_osc}


# --------------------------------------------------------------------------- trials


def run_trials(
    config: SystemConfig,
    scenario: Scenario,
    emitter_id: str,
    n_trials: int,
    *,
    pairs: Sequence[tuple[str, str]] | None = None,
    seed: int = 0,
    draw_errors: bool = False,
    use_calibration: bool = True,
    capture_s: float | None = None,
    fs: float | None = None,
    t_ns: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    reference_carrier_id: str | None = None,
) -> dict[str, Any]:
    """Monte Carlo of the full chain ``render_iq -> caf_estimate -> solve`` for one emitter.

    config, scenario: the simulated system (the scenario is validated against the
    configuration). emitter_id: the emitter to locate (a scenario emitter or a carrier's
    emitter). n_trials: number of trials (>= 1). pairs: explicit channel pairs
    ``[(channel_a, channel_b), ...]``; None applies the tasking rule (module docstring).
    seed: run seed; trial ``i`` is a pure function of ``(seed, i)``. draw_errors: draw
    fresh truth errors from the configured uncertainty in every trial (replacing the
    scenario's ephemeris, oscillator and clock errors; its ionosphere setting is kept)
    and a per-pair TDOA bias. use_calibration: calibrate against reference carriers.
    capture_s: capture length, s (default: the tasking rule). fs: target sample rate,
    Hz (default: the tasking rule). t_ns: capture start, integer UTC ns (default
    ``DEFAULT_T_NS``, or the emitter's ``t_start`` when later); every trial uses the
    same epoch, so the geometry and the predicted covariance are the same in all of
    them. progress: called as ``progress(done, n_trials)`` after each trial.
    reference_carrier_id: calibrate against this carrier only.

    Returns a JSON-ready dict (``simulated: True``): the plan (``pairs``, ``skipped``),
    ``truth``, per-trial results (``trials``) and ``summary``. A trial holds its
    ``scenario_seed``, drawn ``errors`` and ``tdoa_bias_ns`` (with ``draw_errors``), per
    pair the CAF results (``snr_db``, ``tdoa_s``, ``fdoa_hz``, sigmas, ``reason``,
    ``mirror_ambiguous``, truth values, calibration), and the solution: ``lat_deg``,
    ``lon_deg``, ``miss_m`` (horizontal geodesic from the truth), ``error_en_m``,
    ``nees_total`` / ``nees_noise`` (``e^T P^-1 e`` with ``cov_en_m2`` /
    ``cov_noise_en_m2``), ``inside_95_total`` / ``inside_95_noise`` (NEES <= 5.991),
    ``cep50_m`` (from ``P_total``), ``cep50_noise_m``, ``semi_major_m`` (at the
    solver's confidence), ``chi2``, ``reduced_chi2``, ``dominant_error``,
    ``ambiguity_resolved_by``, ``n_ambiguities``; or ``reason`` when no solution was
    made. ``summary`` gives ``n_solved``, ``failures`` (reason counts),
    ``median_miss_m``, ``p95_miss_m``, ``mean_error_en_m`` with its standard error,
    ``mean_nees_total``, ``mean_nees_noise``, ``containment_95_total``,
    ``containment_95_noise``, ``cep50_predicted_m`` (median of the trials' ``cep50_m``),
    ``cep50_noise_predicted_m``, ``cep50_empirical_m`` (median miss) and per-pair
    rejection counts.

    The truth position is the emitter's latitude and longitude; the solver works at
    ``solver.height_m``, so an emitter whose phase centre is elsewhere (``alt_m +
    antenna_height_m``) carries a small geometric bias that is part of the result.
    """
    if not isinstance(config, SystemConfig):
        raise ValueError(f"config must be a SystemConfig, got {type(config).__name__}")
    if not isinstance(scenario, Scenario):
        raise ValueError(f"scenario must be a Scenario, got {type(scenario).__name__}")
    if isinstance(n_trials, bool) or not isinstance(n_trials, (int, np.integer)) or n_trials < 1:
        raise ValueError(f"n_trials must be a positive integer, got {n_trials!r}")
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError(f"seed must be a non-negative integer, got {seed!r}")
    if t_ns is not None and (isinstance(t_ns, bool) or not isinstance(t_ns, (int, np.integer))):
        raise ValueError(f"t_ns must be integer nanoseconds (int), got {type(t_ns).__name__}")
    # epoch of ephemeris truth errors that grow (ric_vel_mps) without a t_ref of their own
    base = SimWorld(config, scenario, epoch_ns=DEFAULT_T_NS if t_ns is None else int(t_ns))
    emitter = base.emitter(emitter_id)
    if t_ns is None:
        t_start = DEFAULT_T_NS if emitter.t_start_ns is None else max(DEFAULT_T_NS, emitter.t_start_ns)
    else:
        t_start = int(t_ns)
    if not emitter.active_at(t_start):
        raise ValueError(f"t_ns: emitter {emitter_id!r} is not radiating at {iso_from_ns(t_start)}")

    plan = plan_event(config, base, emitter_id, t_start, pairs=pairs, use_calibration=use_calibration,
                      capture_s=capture_s, fs=fs, reference_carrier_id=reference_carrier_id)
    uncertainty = config.resolved_uncertainty()
    orbits = config.orbits()
    sites = config.sites_xyz()
    height = float(config.processing.solver.height_m)
    truth = {
        "lat_deg": emitter.lat_deg,
        "lon_deg": emitter.lon_deg,
        "alt_m": emitter.alt_m,
        "antenna_height_m": emitter.antenna_height_m,
        "freq_hz": emitter.freq_hz,
        "bandwidth_hz": emitter.bandwidth_hz,
    }
    trials = []
    for i in range(int(n_trials)):
        trials.append(_run_trial(config, scenario, base, plan, i, int(seed), draw_errors, uncertainty, orbits, sites,
                                 height, truth))
        if progress is not None:
            progress(i + 1, int(n_trials))
    return {
        "simulated": True,
        "description": "Simulated Monte Carlo (not real data): simulated IQ through the CAF estimator and the position solver.",
        "config": config.name,
        "scenario": scenario.name,
        "emitter_id": emitter_id,
        "truth": truth,
        "t_ns": t_start,
        "seed": int(seed),
        "n_trials": int(n_trials),
        "draw_errors": bool(draw_errors),
        "use_calibration": bool(use_calibration),
        "solver_height_m": height,
        "f_lo_hz": plan.f_lo_hz,
        "f_hi_hz": plan.f_hi_hz,
        "primary_channel": plan.primary_channel,
        "pairs": [task.summary() for task in plan.pairs],
        "skipped": list(plan.skipped),
        "trials": trials,
        "summary": _summarise(trials, plan),
    }


def _run_trial(
    config: SystemConfig,
    scenario: Scenario,
    base: SimWorld,
    plan: EventPlan,
    index: int,
    seed: int,
    draw_errors: bool,
    uncertainty: Uncertainty,
    orbits: dict[str, Any],
    sites: dict[str, np.ndarray],
    height: float,
    truth: dict[str, Any],
) -> dict[str, Any]:
    """One trial: rebuild the world, capture, calibrate, estimate, solve and score."""
    s_i = trial_seed(seed, index)
    data = scenario.model_dump(mode="json")
    data["seed"] = s_i
    biases_ns = [0] * len(plan.pairs)
    errors_out: dict[str, Any] | None = None
    if draw_errors:
        rng = np.random.Generator(np.random.Philox(key=key128(f"montecarlo-errors|{seed}|{index}")))
        t_mid = plan.pairs[0].plan.t_mid_ns(plan.t_start_ns)
        drawn = draw_truth_errors(config, uncertainty, rng, t_mid)
        biases_ns = [int(round(float(rng.standard_normal()) * float(uncertainty.tdoa_bias_s) * 1e9)) for _ in plan.pairs]
        errors_out = drawn
        data["errors"] = {**drawn, "iono": scenario.errors.iono.model_dump(mode="json")}
    trial_scenario = Scenario.model_validate(data)
    world = base.rebuild(scenario=trial_scenario)
    caf_cfg = config.processing.caf
    captures: dict[tuple[str, int, int, float, float], np.ndarray] = {}

    def capture(channel: str, render_ns: int, cp: CapturePlan, fc: float) -> np.ndarray:
        key = (channel, render_ns, cp.n_samples, cp.sample_rate_hz, fc)
        if key not in captures:
            captures[key] = world.render_iq(channel, render_ns, cp.n_samples, cp.sample_rate_hz, fc)
        return captures[key]

    def estimate(task_a: str, task_b: str, a: PathSpec, b: PathSpec, cp: CapturePlan, search: PairSearch,
                 bias_ns: int, correction: Any) -> tuple[CafResult | None, str | None]:
        ya = capture(task_a, plan.t_start_ns, cp, a.f_tune_hz)
        yb = capture(task_b, plan.t_start_ns + bias_ns, cp, b.f_tune_hz)
        delay_w, doppler_w, mirror_w = search.windows(caf_cfg, uncertainty, correction)
        try:
            result = caf_estimate(
                ya, yb, cp.sample_rate_hz, delay_window=delay_w, doppler_window=doppler_w,
                signal_bw_hz=cp.signal_bw_hz, t0_offset_s=0.0, exclude=search.exclude, mirror_window=mirror_w,
                min_snr_db=float(caf_cfg.min_snr_db), max_tdoa_sigma_s=float(caf_cfg.max_tdoa_sigma_s),
            )
        except ValueError as exc:
            return None, f"caf: {exc}"
        return result, result.reason

    rows: list[Measurement] = []
    pair_reports = []
    for k, task in enumerate(plan.pairs):
        t_mid = task.plan.t_mid_ns(plan.t_start_ns)
        report: dict[str, Any] = {"a_channel": task.a_channel, "b_channel": task.b_channel, "calibrated": []}
        corrections: dict[str, tuple[float, CalRef]] = {}
        if task.reference is not None:
            ref = task.reference
            ref_mid = ref.plan.t_mid_ns(plan.t_start_ns)
            r_ref, why = estimate(task.a_channel, task.b_channel, ref.a, ref.b, ref.plan, ref.search, biases_ns[k], None)
            if r_ref is not None and r_ref.mirror_ambiguous:
                why = why or REASON_MIRROR_AMBIGUOUS
            ref_report: dict[str, Any] = {"carrier_id": ref.carrier_id, "reason": why}
            if r_ref is not None and not r_ref.mirror_ambiguous:  # as tasking: never calibrate from one
                ref_report.update(snr_db=r_ref.snr_db, tdoa_s=r_ref.tdoa_s, fdoa_hz=r_ref.fdoa_hz,
                                  sigma_tdoa_s=r_ref.sigma_tdoa_s, sigma_fdoa_hz=r_ref.sigma_fdoa_hz)
                for kind, value, sigma in (("tdoa", r_ref.tdoa_s, r_ref.sigma_tdoa_s), ("fdoa", r_ref.fdoa_hz, r_ref.sigma_fdoa_hz)):
                    if value is None or sigma is None:
                        continue
                    row = Measurement(kind, float(value), float(sigma), ref_mid, ref.a, ref.b)
                    cal_id = f"{ref.carrier_id}|{task.a_channel}|{task.b_channel}|{kind}|{index}"
                    corrections[kind] = calibration_correction(row, ref.x_ref, orbits, sites, cal_id)
                    ref_report[f"correction_{kind}"] = corrections[kind][0]
            report["reference"] = ref_report
        correction = None
        if corrections:
            correction = (corrections["tdoa"][0] if "tdoa" in corrections else None,
                          corrections["fdoa"][0] if "fdoa" in corrections else None)
        result, why = estimate(task.a_channel, task.b_channel, task.a, task.b, task.plan, task.search, biases_ns[k], correction)
        if result is not None and result.mirror_ambiguous:
            why = why or REASON_MIRROR_AMBIGUOUS
        report["reason"] = why
        try:
            true_tdoa, true_fdoa = world.truth_measurement(plan.emitter_id, task.a, task.b, t_mid)
            report["truth_tdoa_s"] = true_tdoa + biases_ns[k] * 1e-9
            report["truth_fdoa_hz"] = true_fdoa
        except ValueError as exc:  # a listed satellite does not relay the emitter's centre
            report["truth_tdoa_s"] = report["truth_fdoa_hz"] = None
            report["truth_note"] = str(exc)
        if result is not None:
            report.update(snr_db=result.snr_db, detected=result.detected, mirror_ambiguous=result.mirror_ambiguous,
                          tdoa_s=result.tdoa_s, fdoa_hz=result.fdoa_hz, sigma_tdoa_s=result.sigma_tdoa_s,
                          sigma_fdoa_hz=result.sigma_fdoa_hz, noise_bandwidth_hz=result.noise_bandwidth_hz,
                          integration_s=result.integration_s)
            for kind, value, sigma in (("tdoa", result.tdoa_s, result.sigma_tdoa_s), ("fdoa", result.fdoa_hz, result.sigma_fdoa_hz)):
                if value is None or sigma is None or result.mirror_ambiguous:  # as tasking: never solved
                    continue
                c, cal = corrections.get(kind, (0.0, None))
                rows.append(Measurement(kind, float(value), float(sigma), t_mid, task.a, task.b, correction=c, cal=cal,
                                        tag=f"{task.a_channel}|{task.b_channel}"))
                if cal is not None:
                    report["calibrated"].append(kind)
        pair_reports.append(report)

    out: dict[str, Any] = {"index": index, "scenario_seed": s_i, "pairs": pair_reports, "n_measurements": len(rows)}
    if draw_errors:
        out["errors"] = errors_out
        out["tdoa_bias_ns"] = biases_ns
    if len(rows) < 2:
        out["reason"] = f"fewer than 2 measurements ({len(rows)})"
        return out
    solution, reason = solve(rows, orbits, sites, config.processing.solver, config.processing.region, uncertainty)
    if solution is None:
        out["reason"] = reason
        return out
    out.update(_score(solution, truth, height))
    return out


def _score(solution: Solution, truth: dict[str, Any], height: float) -> dict[str, Any]:
    """Miss distance, east/north error, NEES and containment of one solution against the truth."""
    lat_t, lon_t = float(truth["lat_deg"]), float(truth["lon_deg"])
    meridional, prime = radii_of_curvature(lat_t)
    d_lat = math.radians(solution.lat_deg - lat_t)
    d_lon = math.radians(float(wrap_lon_deg(solution.lon_deg - lon_t)))
    err = np.array([(float(prime) + height) * math.cos(math.radians(lat_t)) * d_lon, (float(meridional) + height) * d_lat])

    def nees(cov: np.ndarray) -> float | None:
        try:
            value = float(err @ np.linalg.solve(np.asarray(cov, dtype=np.float64), err))
        except np.linalg.LinAlgError:
            return None
        return value if math.isfinite(value) else None

    n_total, n_noise = nees(solution.cov_en_m2), nees(solution.cov_noise_en_m2)
    return {
        "lat_deg": solution.lat_deg,
        "lon_deg": solution.lon_deg,
        "miss_m": float(geodesic_distance_m(solution.lat_deg, solution.lon_deg, lat_t, lon_t)),
        "error_en_m": [float(err[0]), float(err[1])],
        "nees_total": n_total,
        "nees_noise": n_noise,
        "inside_95_total": None if n_total is None else bool(n_total <= _CHI2_2_95),
        "inside_95_noise": None if n_noise is None else bool(n_noise <= _CHI2_2_95),
        "cep50_m": float(solution.cep50_m),
        "cep50_noise_m": float(cep50(solution.cov_noise_en_m2)),
        "semi_major_m": float(solution.ellipse["semi_major_m"]),
        "semi_minor_m": float(solution.ellipse["semi_minor_m"]),
        "confidence": float(solution.ellipse["confidence"]),
        "chi2": float(solution.chi2),
        "reduced_chi2": solution.reduced_chi2,
        "dominant_error": solution.dominant_error,
        "ambiguity_resolved_by": solution.ambiguity_resolved_by,
        "n_ambiguities": len(solution.ambiguities),
    }


def _summarise(trials: Sequence[dict[str, Any]], plan: EventPlan) -> dict[str, Any]:
    """Statistics over the solved trials (None where nothing was solved)."""
    solved = [t for t in trials if "miss_m" in t]
    failures: dict[str, int] = {}
    for t in trials:
        if "miss_m" not in t:
            failures[str(t.get("reason"))] = failures.get(str(t.get("reason")), 0) + 1
    rejections: list[dict[str, Any]] = []
    for k, task in enumerate(plan.pairs):
        counts: dict[str, int] = {}
        for t in trials:
            why = t["pairs"][k].get("reason")
            if why is not None:
                counts[why] = counts.get(why, 0) + 1
        rejections.append({"a_channel": task.a_channel, "b_channel": task.b_channel, "reasons": counts})
    out: dict[str, Any] = {
        "n_trials": len(trials),
        "n_solved": len(solved),
        "failures": failures,
        "pair_rejections": rejections,
        "median_miss_m": None,
        "p95_miss_m": None,
        "mean_error_en_m": None,
        "stderr_error_en_m": None,
        "mean_nees_total": None,
        "mean_nees_noise": None,
        "containment_95_total": None,
        "containment_95_noise": None,
        "cep50_predicted_m": None,
        "cep50_noise_predicted_m": None,
        "cep50_empirical_m": None,
    }
    if not solved:
        return out
    miss = np.array([t["miss_m"] for t in solved])
    err = np.array([t["error_en_m"] for t in solved])
    out["median_miss_m"] = float(np.median(miss))
    out["p95_miss_m"] = float(np.percentile(miss, 95.0))
    out["mean_error_en_m"] = [float(v) for v in np.mean(err, axis=0)]
    out["stderr_error_en_m"] = [float(v) for v in (np.std(err, axis=0, ddof=1) / math.sqrt(len(solved)))] if len(solved) > 1 else None
    for name in ("total", "noise"):
        values = [t[f"nees_{name}"] for t in solved if t[f"nees_{name}"] is not None]
        inside = [t[f"inside_95_{name}"] for t in solved if t[f"inside_95_{name}"] is not None]
        out[f"mean_nees_{name}"] = float(np.mean(values)) if values else None
        out[f"containment_95_{name}"] = float(np.mean(inside)) if inside else None
    out["cep50_predicted_m"] = float(np.median([t["cep50_m"] for t in solved]))
    out["cep50_noise_predicted_m"] = float(np.median([t["cep50_noise_m"] for t in solved]))
    out["cep50_empirical_m"] = float(np.median(miss))
    return out


def format_summary(result: dict[str, Any]) -> str:
    """A few plain-text lines summarising ``run_trials`` output (for the CLI). Simulated."""
    s = result["summary"]

    def num(value: Any, fmt: str = "{:.1f}") -> str:
        return "n/a" if value is None else fmt.format(value)

    lines = [
        f"SIMULATED Monte Carlo: {result['scenario']} / {result['emitter_id']} on {result['config']}, "
        f"{s['n_solved']} of {s['n_trials']} trials solved "
        f"(errors drawn: {result['draw_errors']}, calibration: {result['use_calibration']})",
        "pairs: " + ", ".join(f"{p['a_channel']}/{p['b_channel']}" for p in result["pairs"]),
        f"miss: median {num(s['median_miss_m'])} m, 95th percentile {num(s['p95_miss_m'])} m",
        f"CEP50: predicted {num(s['cep50_predicted_m'])} m (noise only {num(s['cep50_noise_predicted_m'])} m), "
        f"empirical {num(s['cep50_empirical_m'])} m",
        f"NEES: mean {num(s['mean_nees_total'], '{:.2f}')} with P_total, {num(s['mean_nees_noise'], '{:.2f}')} with P_noise "
        f"(2 expected); inside 95 % ellipse: {num(s['containment_95_total'], '{:.3f}')} (P_total), "
        f"{num(s['containment_95_noise'], '{:.3f}')} (P_noise)",
    ]
    if s["failures"]:
        lines.append("failures: " + ", ".join(f"{k} x{v}" for k, v in s["failures"].items()))
    return "\n".join(lines)
