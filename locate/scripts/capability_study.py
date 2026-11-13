#!/usr/bin/env python3
"""Reproduce the PETAL Locate capability study behind docs/RESULTS.md.

EVERY NUMBER THIS SCRIPT PRODUCES IS SIMULATED: it runs the project's own link budgets,
accuracy prediction, correlator, solver and simulator against config/pacific-uhf.yaml,
whose orbital slots are nominal and whose link parameters (G/T, SFD, saturated EIRP,
noise temperatures, antenna gains) are illustrative placeholders, not data for any real
satellite, terminal or site.

Sections (``--sections``, comma separated; default all but ``live``):

    detection   weakest detectable uplink jammer EIRP per site, satellite and antenna,
                from the link budget and confirmed with the server's spectrum tracker
                on simulated PSD frames
    accuracy    predicted 95 % semi-major and CEP50 (core.accuracy.accuracy_map) at
                representative points, for the tasking rule's pairs, uncalibrated and
                calibrated, operator-grade and TLE-grade ephemeris, plus a coarse grid
    limits      mirror ambiguity, band overlap, visibility, CW and swept jammers
    sites       dropping each site in turn, and each site alone
    montecarlo  full-chain Monte Carlo (sim.montecarlo.run_trials, drawn errors)
    scenarios   the shipped pacific-demo, clock-fault, ephemeris-error and two-jammers
                scenarios through the same chain at eight epochs
    live        the running system: ``petal-locate demo`` on 127.0.0.1 with two
                presets, observed through the console API (takes a few minutes)

Each section writes ``<out>/<section>.json`` (default out: docs/results). Absolute times
are integer nanoseconds (UTC) in the JSON files. Run from the repository's ``locate``
directory with the project's virtual environment::

    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/capability_study.py
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 .venv/bin/python scripts/capability_study.py --sections live

Everything is deterministic (fixed epochs and seeds) except the live section, which runs
at the wall-clock time of day and whose noise draws depend on it.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from petal_locate.config.loader import load_config
from petal_locate.config.models import EmitterModel, Region, SystemConfig
from petal_locate.core.accuracy import accuracy_map, relaying_transponder
from petal_locate.core.antennas import electrically_small, peak_gain_dbi
from petal_locate.core.geodesy import geodesic_distance_m, lla_to_ecef
from petal_locate.core.geometry import elevation_deg
from petal_locate.core.linkbudget import path_budget
from petal_locate.core.solver import Measurement, solve
from petal_locate.core.timebase import iso_from_ns, ns_from_iso, sec
from petal_locate.core.types import PathSpec
from petal_locate.server.pipeline.detection import build_trackers, build_window_specs
from petal_locate.sim import Scenario, SimWorld, load_scenario
from petal_locate.sim.montecarlo import _score, plan_event, run_trials

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "pacific-uhf.yaml"
SCENARIOS = ROOT / "config" / "scenarios"
NS = 1_000_000_000
SIDEREAL_DAY_S = 86164.0905

EPOCHS = ["2026-01-01T03:00:00Z", "2026-01-01T09:00:00Z", "2026-01-01T15:00:00Z", "2026-01-01T21:00:00Z"]
T_MAIN = ns_from_iso(EPOCHS[0])
F_SHARED1 = 301.005e6  # a 10 kHz jammer inside shared-1, relayed by all three satellites
JAMMER_EIRP_DBW = 20.0
JAMMER_BW_HZ = 10e3

POINTS: list[tuple[str, str, float, float]] = [
    ("philippine-sea", "Philippine Sea", 20.0, 132.0),
    ("south-china-sea", "South China Sea", 15.0, 115.0),
    ("sea-of-japan", "Sea of Japan", 40.0, 135.0),
    ("near-guam", "near Guam (east of the Marianas)", 15.0, 147.0),
    ("near-hawaii", "near Hawaii (south-west of Kauai)", 20.0, -160.0),
    ("korean-peninsula", "Korean peninsula (inland grid point)", 37.0, 127.5),
    ("east-of-japan", "open Pacific east of Japan", 35.0, 150.0),
]

# What-if reference sites (generic city centres) for "more reference sites": each is
# tried as the only shared-1 reference, with the configured omni design.
WHATIF_REFERENCES: dict[str, tuple[float, float]] = {
    "guam": (13.476, 144.749),
    "sapporo": (43.062, 141.354),
    "koror": (7.340, 134.480),
    "taipei": (25.033, 121.565),
    "honolulu": (21.307, -157.858),
}

ANTENNAS: dict[str, tuple[dict[str, Any], float]] = {  # antenna model, polarisation loss dB (as configured)
    "dish-0.72m": ({"type": "parabolic", "diameter_m": 0.72, "efficiency": 0.55}, 0.0),
    "dish-1.2m": ({"type": "parabolic", "diameter_m": 1.2, "efficiency": 0.55}, 0.0),
    "beam-10dBi": ({"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 20.0}, 3.0),
}

SIMULATED_NOTE = (
    "SIMULATED: every number comes from the PETAL Locate simulator and models with illustrative link parameters "
    "and nominal orbital slots (config/pacific-uhf.yaml), not from real satellites, terminals or sites."
)


# --------------------------------------------------------------------------- helpers


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def base_config() -> SystemConfig:
    return load_config(CONFIG_PATH)


def variant(cfg: SystemConfig, edit: Callable[[dict[str, Any]], None]) -> SystemConfig:
    data = copy.deepcopy(cfg.export_dict())
    edit(data)
    return SystemConfig.model_validate(data)


def tle_config(cfg: SystemConfig) -> SystemConfig:
    """Every satellite given the TLE-grade one-sigma ephemeris of processing.uncertainty."""
    tle = cfg.processing.uncertainty.ephemeris.tle

    def edit(data: dict[str, Any]) -> None:
        for sat in data["satellites"]:
            sat["ephemeris_sigma"] = {"pos_m": list(tle.pos_m), "vel_mps": list(tle.vel_mps)}

    return variant(cfg, edit)


def without_carriers(cfg: SystemConfig) -> SystemConfig:
    return variant(cfg, lambda d: d.__setitem__("carriers", []))


def with_reference_at(cfg: SystemConfig, lat: float, lon: float) -> SystemConfig:
    """ref-guam moved to (lat, lon): the configured omni design at another place."""

    def edit(data: dict[str, Any]) -> None:
        for c in data["carriers"]:
            if c["id"] == "ref-guam":
                c["location"] = dict(c["location"], lat_deg=lat, lon_deg=lon)

    return variant(cfg, edit)


def jammer_scenario(lat: float, lon: float, *, freq_hz: float = F_SHARED1, eirp_dbw: float = JAMMER_EIRP_DBW,
                    waveform: dict[str, Any] | None = None, seed: int = 1, name: str = "study") -> Scenario:
    return Scenario.model_validate({
        "name": name, "seed": seed, "description": "Simulated study probe (not real data).",
        "emitters": [{"id": "j", "name": "Study jammer (simulated)", "kind": "jammer", "lat_deg": lat, "lon_deg": lon,
                      "freq_hz": freq_hz, "eirp_dbw": eirp_dbw,
                      "waveform": waveform or {"type": "noise", "bandwidth_hz": JAMMER_BW_HZ}}],
    })


def jammer_model(bw_hz: float = JAMMER_BW_HZ, waveform: str = "noise", eirp_dbw: float = JAMMER_EIRP_DBW) -> EmitterModel:
    return EmitterModel(eirp_dbw=eirp_dbw, bandwidth_hz=bw_hz, waveform=waveform)


def default_pairs(cfg: SystemConfig, lat: float, lon: float, t_ns: int,
                  freq_hz: float = F_SHARED1) -> tuple[list[tuple[str, str]] | None, str | None, str]:
    """The tasking rule's pairs for a jammer at (lat, lon): (pairs, primary, note)."""
    world = SimWorld(cfg, jammer_scenario(lat, lon, freq_hz=freq_hz), epoch_ns=t_ns)
    try:
        plan = plan_event(cfg, world, "j", t_ns, use_calibration=False)
    except ValueError as exc:
        return None, None, str(exc)
    return [(p.a_channel, p.b_channel) for p in plan.pairs], plan.primary_channel, "; ".join(plan.skipped)


def satellites_seeing(cfg: SystemConfig, lat: float, lon: float, t_ns: int) -> list[str]:
    x = lla_to_ecef(lat, lon, 10.0)
    orbits = cfg.orbits()
    return [s.id for s in cfg.satellites if float(elevation_deg(x, orbits[s.id], sec(t_ns))) > 0.0]


def point_accuracy(cfg: SystemConfig, lat: float, lon: float, t_ns: int, pairs: Sequence[tuple[str, str]],
                   reference: str | None, emitter: EmitterModel | None = None, f_hz: float = F_SHARED1,
                   kinds: tuple[str, ...] = ("tdoa", "fdoa")) -> dict[str, Any]:
    """accuracy_map at one cell (a 1-degree region around the point, 0.5 degree step)."""
    def wrap(x: float) -> float:
        return ((x + 180.0) % 360.0) - 180.0

    region = Region(lat_min=lat - 0.5, lat_max=lat + 0.5, lon_min=wrap(lon - 0.5), lon_max=wrap(lon + 0.5))
    out = accuracy_map(cfg, list(pairs), emitter or jammer_model(), f_hz, t_ns, region, 0.5, kinds=kinds,
                       reference_carrier_id=reference)
    i = int(np.argmin(np.abs(np.asarray(out["lat_deg"]) - lat)))
    j = int(np.argmin(np.abs((np.asarray(out["lon_deg"]) - lon + 180.0) % 360.0 - 180.0)))
    refs = []
    for p in out["pairs"]:
        r = p.get("reference")
        refs.append({"pair": f"{p['a_channel']}/{p['b_channel']}", "calibrated": p.get("calibrated", []),
                     "reference_snr_db": None if not r else r.get("snr_db"),
                     "reference_sigma_tdoa_ns": None if not r or r.get("sigma_tdoa_s") is None else 1e9 * r["sigma_tdoa_s"],
                     "reference_reason": None if not r else r.get("reason"), "reason": p.get("reason")})
    return {
        "semi_major_95_m": out["semi_major_95_m"][i][j],
        "semi_minor_95_m": out["semi_minor_95_m"][i][j],
        "cep50_m": out["cep50_m"][i][j],
        "dominant": out["dominant"][i][j],
        "n_rows": out["n_rows"][i][j],
        "reason": out["reason"][i][j],
        "pairs": refs,
    }


def write_json(out_dir: Path, name: str, data: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=1, sort_keys=False, allow_nan=False) + "\n", encoding="utf-8")
    log(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


def header(section: str, **extra: Any) -> dict[str, Any]:
    sha = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()[:16]
    return {"section": section, "simulated": True, "note": SIMULATED_NOTE, "config": "config/pacific-uhf.yaml",
            "config_sha256_16": sha, "script": "scripts/capability_study.py", **extra}


def clean(value: Any) -> Any:
    """JSON-safe: numpy scalars to Python, non-finite floats to None."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


# --------------------------------------------------------------------------- detection


WINDOW_OFFSETS_S = (0, 600, 1200, 1800)  # four independent 10 s looks at the spectrum


def _tracker_coverage(cfg: SystemConfig, specs: dict[Any, Any], channel_id: str, f_down_lo: float, f_down_hi: float,
                      lat: float, lon: float, freq_hz: float, bw_hz: float, eirp_dbw: float, t_ns: int,
                      n_frames: int = 20) -> list[float]:
    """The server's tracker fed simulated PSD frames (``n_frames`` at the monitor's rate)
    of the channel's window holding the jammer's downlink, from each of the start times
    ``t_ns + WINDOW_OFFSETS_S``: per start time, the largest fraction of the jammer's band
    covered by one confirmed detection after the last frame (0 when none overlaps it)."""
    world = SimWorld(cfg, jammer_scenario(lat, lon, freq_hz=freq_hz, eirp_dbw=eirp_dbw,
                                          waveform={"type": "noise", "bandwidth_hz": bw_hz}), epoch_ns=t_ns)
    mine = [s for s in specs.values() if s.channel_id == channel_id
            and s.center_hz - 0.5 * s.span_hz < f_down_hi and s.center_hz + 0.5 * s.span_hz > f_down_lo]
    out = []
    for offset in WINDOW_OFFSETS_S:
        t0 = t_ns + offset * NS
        best = 0.0
        trackers = build_trackers(cfg.processing.detection, mine)
        for spec in mine:
            tracker = trackers[spec.key]
            found = []
            for k in range(n_frames):
                t = t0 + int(round(k * NS / spec.frame_rate_hz))
                found = tracker.update(world.render_psd(spec.channel_id, t, spec.center_hz, spec.span_hz, spec.nfft,
                                                        spec.navg), t)
            for d in found:
                overlap = min(d.f_hi_hz, f_down_hi) - max(d.f_lo_hz, f_down_lo)
                if overlap > 0.0:
                    best = max(best, max(overlap, 1e-9) / (f_down_hi - f_down_lo))
        out.append(best)
    return out


def _bisect(test: Callable[[float], bool], lo: float, hi: float, step: float = 0.25) -> float | None:
    """Smallest value (to ``step``) in [lo, hi] for which the monotone ``test`` holds, or None."""
    if not test(hi):
        return None
    while hi - lo > step:
        mid = 0.5 * (lo + hi)
        if test(mid):
            hi = mid
        else:
            lo = mid
    return hi


def section_detection(args: argparse.Namespace) -> dict[str, Any]:
    cfg = base_config()
    t_ns = T_MAIN
    t_s = sec(t_ns)
    det = cfg.processing.detection
    lat, lon = 20.0, 132.0  # a jammer in the Philippine Sea, seen by all three satellites
    x_e = lla_to_ecef(lat, lon, 10.0)
    per_bin_db = 10.0 * math.log10(10.0 ** (float(det.threshold_db) / 10.0) - 1.0)
    rows = []
    gains = {}
    for name, (antenna, pol) in ANTENNAS.items():
        def edit(data: dict[str, Any], antenna: dict[str, Any] = antenna, pol: float = pol) -> None:
            data["carriers"] = []  # thresholds of the jammer alone (a reference elsewhere in the band changes nothing)
            for site in data["sites"]:
                for ch in site["channels"]:
                    ch["antenna"] = copy.deepcopy(antenna)
                    ch["polarization_loss_db"] = pol
        vcfg = variant(cfg, edit)
        specs = build_window_specs(vcfg, t_s)
        for site in vcfg.sites:
            for ch in site.channels:
                sat_id = ch.pointing.satellite_id
                for bw in (10e3, 25e3):
                    f_up = F_SHARED1 if bw == 10e3 else 301.000e6
                    tp = relaying_transponder(vcfg.satellite(sat_id), f_up)
                    em = EmitterModel(eirp_dbw=0.0, bandwidth_hz=bw, waveform="noise")
                    pb = path_budget(vcfg, em, f_up, x_e, sat_id, tp.id, ch.id, t_s)
                    f_down = f_up + tp.translation_hz
                    gains[f"{name}@{f_down / 1e6:.3f}MHz"] = (float(peak_gain_dbi(ch.antenna, f_down)),
                                                             bool(electrically_small(ch.antenna, f_down)))
                    if pb is None:
                        rows.append({"site": site.id, "satellite": sat_id, "antenna": name, "bandwidth_hz": bw,
                                     "reason": "satellite not visible"})
                        continue
                    cn0_at_0 = float(pb.cn0_total_dbhz)
                    need_cn0 = per_bin_db + 10.0 * math.log10(bw)
                    nominal = need_cn0 - cn0_at_0  # linear transponder below saturation: C/N0 follows EIRP
                    lo_f, hi_f = f_down - 0.5 * bw, f_down + 0.5 * bw
                    memo: dict[float, list[float]] = {}

                    def cov(e: float, ch_id: str = ch.id, lo_f: float = lo_f, hi_f: float = hi_f,
                            f_up: float = f_up, bw: float = bw, vcfg: SystemConfig = vcfg,
                            specs: dict[Any, Any] = specs, memo: dict[float, list[float]] = memo) -> list[float]:
                        if e not in memo:
                            memo[e] = _tracker_coverage(vcfg, specs, ch_id, lo_f, hi_f, lat, lon, f_up, bw, e, t_ns)
                        return memo[e]

                    # bisection with the tracker between nominal - 8 dB and nominal + 3 dB, 0.25 dB resolution;
                    # each level must hold in at least 3 of the 4 independent 10 s looks
                    full = _bisect(lambda e: sum(c >= 0.8 for c in cov(e)) >= 3, nominal - 8.0, nominal + 3.0)
                    first = _bisect(lambda e: sum(c > 0.0 for c in cov(e)) >= 3, nominal - 8.0, nominal + 3.0)
                    rows.append({"site": site.id, "satellite": sat_id, "channel": ch.id, "antenna": name,
                                 "bandwidth_hz": bw, "cn0_dbhz_at_0dbw": cn0_at_0,
                                 "threshold_cn0_dbhz": need_cn0, "eirp_nominal_dbw": nominal,
                                 "eirp_tracker_full_width_dbw": full, "eirp_tracker_fragments_dbw": first})
        log(f"detection: {name} done")
    # configured channels, for the table of what is actually installed
    installed = {ch.id: ch.antenna_summary if hasattr(ch, "antenna_summary") else str(ch.antenna)
                 for site in cfg.sites for ch in site.channels}
    # slant-range spread of the uplink over the visible footprint (nadir vs 5 degree elevation)
    spread_db = 20.0 * math.log10(41_126e3 / 35_786e3)
    return header(
        "detection", t_ns=t_ns, epoch=iso_from_ns(t_ns), jammer_location=[lat, lon],
        method=(
            "Jammer: noise-like, isotropic, at 20 N 132 E (Philippine Sea), 10 kHz at 301.005 MHz or 25 kHz at "
            "301.000 MHz (shared-1, relayed by all three satellites); reference carriers removed. Nominal: the "
            "EIRP at which the mean per-bin excess of the jammer over the floor (site noise plus relayed "
            "transponder noise) reaches 10 log10(10^(threshold_db/10) - 1) dB, i.e. half of its bins cross the "
            "tracker's threshold (threshold_db 6 dB at navg 8: 4.75 dB per bin, C/N0 44.8 dB-Hz in 10 kHz, "
            "48.7 dB-Hz in 25 kHz), from linkbudget.path_budget. Tracker: the server's SignalTracker "
            "(server.pipeline.detection) fed 20 simulated PSD frames (SimWorld.render_psd, 10 s at 2 Hz) of the "
            "channel's derived monitor from four start times 10 minutes apart, bisected to 0.25 dB: "
            "'full width' is the weakest EIRP at which one confirmed detection covers at least 80 % of the "
            "jammer's band in at least 3 of the 4 looks (a usable detection: the event gets the right uplink "
            "range); 'fragments' the weakest at which any confirmed detection overlaps the band in 3 of 4 "
            "(near it the tracker reports scattered detections a few bins wide). Every channel is given the "
            "antenna under test (dishes: no polarisation loss; beam: 3 dB, linear on circular), pointed at its "
            "satellite as configured."
        ),
        per_bin_threshold_db=per_bin_db, uplink_range_spread_db=spread_db,
        antenna_peak_gain_dbi_at_downlink={k: {"gain_dbi": v[0], "electrically_small": v[1]} for k, v in gains.items()},
        installed_antennas=installed, rows=rows,
    )


# --------------------------------------------------------------------------- accuracy


def _nearest_whatif(lat: float, lon: float) -> tuple[str, float]:
    best = min(WHATIF_REFERENCES.items(), key=lambda kv: float(geodesic_distance_m(lat, lon, *kv[1])))
    return best[0], float(geodesic_distance_m(lat, lon, *best[1]))


def section_accuracy(args: argparse.Namespace) -> dict[str, Any]:
    cfg = base_config()
    tle = tle_config(cfg)
    configs = {"operator": cfg, "tle": tle}
    points = []
    for key, label, lat, lon in POINTS:
        entry: dict[str, Any] = {"key": key, "label": label, "lat_deg": lat, "lon_deg": lon, "epochs": []}
        guam_km = float(geodesic_distance_m(lat, lon, *WHATIF_REFERENCES["guam"])) / 1e3
        near_name, near_m = _nearest_whatif(lat, lon)
        entry["distance_to_guam_reference_km"] = guam_km
        entry["nearest_whatif_reference"] = {"site": near_name, "distance_km": near_m / 1e3}
        whatif = {g: with_reference_at(c, *WHATIF_REFERENCES[near_name]) for g, c in configs.items()}
        for iso in EPOCHS:
            t_ns = ns_from_iso(iso)
            seen = satellites_seeing(cfg, lat, lon, t_ns)
            pairs, primary, note = default_pairs(cfg, lat, lon, t_ns)
            ep: dict[str, Any] = {"epoch": iso, "t_ns": t_ns, "satellites_seeing_emitter": seen,
                                  "primary": primary, "pairs": pairs, "note": note, "results": {}}
            if pairs:
                for grade, c in configs.items():
                    ep["results"][f"{grade}/uncalibrated"] = point_accuracy(c, lat, lon, t_ns, pairs, None)
                    ep["results"][f"{grade}/calibrated-guam"] = point_accuracy(c, lat, lon, t_ns, pairs, "ref-guam")
                    ep["results"][f"{grade}/calibrated-nearest-whatif"] = point_accuracy(
                        whatif[grade], lat, lon, t_ns, pairs, "ref-guam")
            entry["epochs"].append(ep)
        points.append(entry)
        log(f"accuracy: {key} done")

    # coarse grid at the main epoch: where can the network locate a shared-1 jammer at all?
    lats = [-10.0, 0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    lons = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0, 190.0, 200.0]
    grid = []
    for lat in lats:
        row = []
        for lon in lons:
            lon_w = lon if lon <= 180.0 else lon - 360.0
            cell: dict[str, Any] = {"lat_deg": lat, "lon_deg": lon, "satellites": satellites_seeing(cfg, lat, lon_w, T_MAIN)}
            pairs, primary, note = default_pairs(cfg, lat, lon_w, T_MAIN)
            cell["pairs"] = pairs
            cell["note"] = note
            if pairs:
                for grade, c in configs.items():
                    for ref in (None, "ref-guam"):
                        r = point_accuracy(c, lat, lon_w, T_MAIN, pairs, ref)
                        cell[f"{grade}/{'calibrated-guam' if ref else 'uncalibrated'}"] = {
                            k: r[k] for k in ("semi_major_95_m", "cep50_m", "dominant", "reason")}
            row.append(cell)
        grid.append(row)
    log("accuracy: grid done")
    return header(
        "accuracy", epochs=EPOCHS, jammer={"eirp_dbw": JAMMER_EIRP_DBW, "bandwidth_hz": JAMMER_BW_HZ,
                                           "freq_hz": F_SHARED1, "waveform": "noise", "antenna": "isotropic"},
        method=(
            "core.accuracy.accuracy_map at each point (one TDOA and one FDOA per pair, MATH.md section 10) for the "
            "pairs the tasking rule picks for a jammer there (sim.montecarlo.plan_event: primary channel by "
            "received C/N0, one partner per other satellite by downlink quality, same-site preference within "
            "3 dB), one 1 s capture per pair. 'operator': the configured operator-grade one-sigma ephemeris "
            "(RIC 100/1000/1000 m, 0.02/0.02/0.05 m/s); 'tle': every satellite at the TLE-grade sigmas "
            "(2/20/10 km, 0.3/0.3/0.7 m/s). 'calibrated-guam': differenced against the configured Guam "
            "reference (shared-1); 'calibrated-nearest-whatif': the same reference design moved to the nearest "
            "of the what-if sites. Distances in m; semi-major of the 95 % ellipse; CEP50 of P_total."
        ),
        whatif_reference_sites=WHATIF_REFERENCES, points=points, grid_lat_deg=lats, grid_lon_deg=lons, grid=grid,
    )


# --------------------------------------------------------------------------- limits


def _noise_free_solve(cfg_truth: SystemConfig, cfg_solver: SystemConfig, lat: float, lon: float, t_ns: int,
                      sigma: tuple[float, float] = (1.0e-7, 1.0e-3)) -> dict[str, Any]:
    """Truth measurements (no noise, no truth errors) of the tasking rule's pairs, solved."""
    world = SimWorld(cfg_truth, jammer_scenario(lat, lon), epoch_ns=t_ns)
    plan = plan_event(cfg_truth, world, "j", t_ns, use_calibration=False)
    rows = []
    for task in plan.pairs:
        tm = task.plan.t_mid_ns(t_ns)
        z_t, z_f = world.truth_measurement("j", task.a, task.b, tm)
        rows.append(Measurement("tdoa", z_t, sigma[0], tm, task.a, task.b))
        rows.append(Measurement("fdoa", z_f, sigma[1], tm, task.a, task.b))
    sol, reason = solve(rows, cfg_solver.orbits(), cfg_solver.sites_xyz(), cfg_solver.processing.solver,
                        cfg_solver.processing.region, cfg_solver.resolved_uncertainty())
    if sol is None:
        return {"reason": reason, "pairs": [(t.a_channel, t.b_channel) for t in plan.pairs]}
    cands = [sol, *sol.ambiguities]
    return {
        "pairs": [(t.a_channel, t.b_channel) for t in plan.pairs],
        "candidates": [{"lat_deg": c.lat_deg, "lon_deg": c.lon_deg, "chi2": c.chi2,
                        "miss_m": float(geodesic_distance_m(c.lat_deg, c.lon_deg, lat, lon))} for c in cands],
        "ambiguity_resolved_by": sol.ambiguity_resolved_by,
    }


def _mc_summary(res: dict[str, Any]) -> dict[str, Any]:
    s = res["summary"]
    reasons: dict[str, int] = {}
    for t in res["trials"]:
        for p in t["pairs"]:
            reasons[str(p.get("reason"))] = reasons.get(str(p.get("reason")), 0) + 1
    return {k: s[k] for k in ("n_trials", "n_solved", "failures", "median_miss_m", "p95_miss_m", "mean_nees_total",
                              "containment_95_total", "cep50_predicted_m")} | {"pair_reasons": reasons,
                                                                               "pairs": [f"{p['a_channel']}/{p['b_channel']}" for p in res["pairs"]]}


def section_limits(args: argparse.Namespace) -> dict[str, Any]:
    cfg = base_config()
    out: dict[str, Any] = {}
    # 1. mirror ambiguity: the shipped orbits (4-5 deg inclination) against the same orbits flattened to 0 deg
    flat = variant(cfg, lambda d: [s["orbit"].__setitem__("inclination_deg", 0.0) for s in d["satellites"]])
    mirror = []
    for iso in EPOCHS:
        t_ns = ns_from_iso(iso)
        mirror.append({"epoch": iso, "inclined": _noise_free_solve(cfg, cfg, 20.0, 132.0, t_ns),
                       "zero_inclination": _noise_free_solve(flat, flat, 20.0, 132.0, t_ns)})
    out["mirror"] = {"point": [20.0, 132.0], "method": (
        "Noise-free truth TDOA/FDOA (sigmas 0.1 us, 1 mHz) of the tasking rule's pairs, no truth errors, solved "
        "with the configured uncertainty; 'zero_inclination' sets every satellite's inclination to 0 in both "
        "truth and solver."), "epochs": mirror}
    log("limits: mirror done")
    # 2. band overlap: a jammer on a channel only one satellite relays
    solo = {}
    for f_up, label in ((301.500e6, "solo-pac (muos-pac only)"), (301.750e6, "solo-172e (uhf-172e only)"),
                        (302.000e6, "solo-io (muos-io only)")):
        world = SimWorld(cfg, jammer_scenario(20.0, 132.0, freq_hz=f_up), epoch_ns=T_MAIN)
        cn0 = []
        for sat in cfg.satellites:
            tp = relaying_transponder(sat, f_up)
            if tp is None:
                continue
            for site, ch in cfg.channels_watching(sat.id):
                pb = world.path_budget("j", sat.id, tp.id, ch.id, sec(T_MAIN))
                if pb is not None:
                    cn0.append(float(pb.cn0_total_dbhz))
        try:
            plan_event(cfg, world, "j", T_MAIN, use_calibration=False)
            why = None
        except ValueError as exc:
            why = str(exc)
        solo[label] = {"freq_hz": f_up, "channels_receiving": len(cn0),
                       "cn0_dbhz_range": [min(cn0), max(cn0)] if cn0 else None, "locate": why}
    out["band_overlap"] = solo
    # 3. visibility: elevation range of each satellite from each site over a sidereal day
    times = T_MAIN / NS + np.linspace(0.0, SIDEREAL_DAY_S, 145)
    orbits = cfg.orbits()
    vis = {}
    for site in cfg.sites:
        x = cfg.site_xyz(site.id)
        vis[site.id] = {}
        for sat in cfg.satellites:
            el = elevation_deg(x, orbits[sat.id], times)
            vis[site.id][sat.id] = [float(np.min(el)), float(np.max(el))]
    out["visibility_deg"] = vis
    # 4. CW (FDOA only) and the swept jammer, predicted and through the full chain
    cw_model = EmitterModel(eirp_dbw=5.0, bandwidth_hz=0.0, waveform="cw")
    cw = []
    for key, label, lat, lon in POINTS:
        pairs, _, note = default_pairs(cfg, lat, lon, T_MAIN, freq_hz=301.130e6)
        item: dict[str, Any] = {"key": key, "pairs": pairs, "note": note}
        if pairs:
            item["uncalibrated"] = point_accuracy(cfg, lat, lon, T_MAIN, pairs, None, emitter=cw_model, f_hz=301.130e6)
            item["calibrated-sapporo"] = point_accuracy(cfg, lat, lon, T_MAIN, pairs, "ref-sapporo", emitter=cw_model,
                                                        f_hz=301.130e6)
        cw.append(item)
    out["cw_predicted"] = {"emitter": "CW, 5 dBW isotropic, 301.130 MHz (shared-2)", "points": cw}
    n_chain = max(4, args.trials // 4)
    for name, eid in (("uplink-cw", "jammer-cw"), ("uplink-swept", "jammer-swept")):
        sc = load_scenario(SCENARIOS / f"{name}.yaml")
        for cal in (False, True):
            res = run_trials(cfg, sc, eid, n_chain, seed=31, draw_errors=True, use_calibration=cal, t_ns=T_MAIN)
            out[f"{name}/{'calibrated' if cal else 'uncalibrated'}"] = _mc_summary(res)
        log(f"limits: {name} chain done ({n_chain} trials each)")
    return header("limits", **clean(out))


# --------------------------------------------------------------------------- sites


def section_sites(args: argparse.Namespace) -> dict[str, Any]:
    cfg = base_config()
    site_ids = [s.id for s in cfg.sites]

    def subset(keep: Sequence[str]) -> SystemConfig:
        return variant(cfg, lambda d: d.__setitem__("sites", [s for s in d["sites"] if s["id"] in keep]))

    def evaluate(c: SystemConfig) -> dict[str, Any]:
        res = {}
        for key, _, lat, lon in POINTS:
            pairs, primary, note = default_pairs(c, lat, lon, T_MAIN)
            if not pairs:
                res[key] = {"pairs": None, "note": note}
                continue
            unc = point_accuracy(c, lat, lon, T_MAIN, pairs, None)
            cal = point_accuracy(c, lat, lon, T_MAIN, pairs, "ref-guam")
            res[key] = {"pairs": pairs, "uncalibrated_semi_major_95_m": unc["semi_major_95_m"],
                        "calibrated_semi_major_95_m": cal["semi_major_95_m"], "calibrated_cep50_m": cal["cep50_m"],
                        "calibrated_reason": cal["reason"], "uncalibrated_reason": unc["reason"]}
        return res

    out: dict[str, Any] = {"all": evaluate(cfg), "drop_one": {}, "only_one": {}}
    for sid in site_ids:
        out["drop_one"][sid] = evaluate(subset([s for s in site_ids if s != sid]))
        out["only_one"][sid] = evaluate(subset([sid]))
        log(f"sites: {sid} done")
    return header("sites", epoch=EPOCHS[0], t_ns=T_MAIN, method=(
        "The configuration with one site removed (drop_one) or with one site kept (only_one); at each point the "
        "tasking rule's pairs for a shared-1 jammer at 03 UTC and the predicted 95 % semi-major, operator-grade "
        "ephemeris, uncalibrated and calibrated against the Guam reference. A point with no pairs cannot be "
        "located (fewer than two satellites reach a channel)."), **clean(out))


# --------------------------------------------------------------------------- Monte Carlo


MC_POINTS = ["philippine-sea", "south-china-sea", "sea-of-japan", "near-guam", "east-of-japan"]
MC_TLE_POINTS = ["philippine-sea", "near-guam"]


def _mc_job(job: tuple[str, str, bool, int, int]) -> dict[str, Any]:
    key, grade, cal, n, seed = job
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    cfg = base_config()
    if grade == "tle":
        cfg = tle_config(cfg)
    lat, lon = next((p[2], p[3]) for p in POINTS if p[0] == key)
    sc = jammer_scenario(lat, lon, seed=seed)
    start = time.time()
    res = run_trials(cfg, sc, "j", n, seed=seed, draw_errors=True, use_calibration=cal, t_ns=T_MAIN)
    trials = [{k: t.get(k) for k in ("index", "miss_m", "nees_total", "inside_95_total", "cep50_m", "semi_major_m",
                                    "dominant_error", "ambiguity_resolved_by", "n_ambiguities", "reason")}
              for t in res["trials"]]
    return clean({"point": key, "lat_deg": lat, "lon_deg": lon, "ephemeris": grade, "calibrated": cal, "seed": seed,
                  "n_trials": n, "pairs": res["pairs"], "summary": _mc_summary(res), "trials": trials,
                  "elapsed_s": time.time() - start})


def section_montecarlo(args: argparse.Namespace) -> dict[str, Any]:
    jobs = []
    seed = 1000
    for key in MC_POINTS:
        for cal in (False, True):
            jobs.append((key, "operator", cal, args.trials, seed))
            seed += 1
    for key in MC_TLE_POINTS:
        for cal in (False, True):
            jobs.append((key, "tle", cal, args.trials, seed))
            seed += 1
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(_mc_job, jobs):
            s = res["summary"]
            log(f"montecarlo: {res['point']} {res['ephemeris']} cal={res['calibrated']}: solved {s['n_solved']}/{s['n_trials']}, "
                f"median {s['median_miss_m']}, p95 {s['p95_miss_m']}, contain {s['containment_95_total']}")
            results.append(res)
    return header("montecarlo", epoch=EPOCHS[0], t_ns=T_MAIN, method=(
        "sim.montecarlo.run_trials: simulated IQ (SimWorld.render_iq) -> caf_estimate -> calibration -> solve for "
        "a 20 dBW, 10 kHz noise jammer at the point, pairs by the tasking rule, 1 s captures, draw_errors=True "
        "(every trial draws satellite ephemeris, oscillator, site clock and per-pair TDOA bias errors from the "
        "configured one-sigma uncertainty, operator-grade or every satellite at TLE-grade; the solver assumes the "
        "same sigmas), uncalibrated or calibrated against the configured reference in the jammer's channel. "
        "Miss = horizontal distance from the truth; containment = fraction inside the solver's own 95 % ellipse "
        "(NEES <= 5.99); mean NEES 2 is ideal."), runs=results)


# --------------------------------------------------------------------------- shipped scenarios


SCENARIO_EPOCHS = [f"2026-01-01T{h:02d}:00:12Z" for h in range(0, 24, 3)]


def _scenario_job(job: tuple[str, str, float, float, str, bool, int]) -> dict[str, Any]:
    name, eid, lat, lon, iso, cal, n = job
    cfg = base_config()
    sc = load_scenario(SCENARIOS / f"{name}.yaml")
    res = run_trials(cfg, sc, eid, n, seed=7, draw_errors=False, use_calibration=cal, t_ns=ns_from_iso(iso))
    trials = []
    for t in res["trials"]:
        item = {k: t.get(k) for k in ("miss_m", "nees_total", "inside_95_total", "semi_major_m", "semi_minor_m",
                                      "reduced_chi2", "dominant_error", "ambiguity_resolved_by", "n_ambiguities",
                                      "reason")}
        item["reference_snr_db"] = [p.get("reference", {}).get("snr_db") for p in t["pairs"]]
        item["reference_sigma_tdoa_ns"] = [None if p.get("reference", {}).get("sigma_tdoa_s") is None
                                           else 1e9 * p["reference"]["sigma_tdoa_s"] for p in t["pairs"]]
        trials.append(item)
    return clean({"scenario": name, "emitter_id": eid, "epoch": iso, "t_ns": ns_from_iso(iso), "calibrated": cal,
                  "primary": res["primary_channel"], "pairs": [f"{p['a_channel']}/{p['b_channel']}" for p in res["pairs"]],
                  "summary": _mc_summary(res), "trials": trials})


def section_scenarios(args: argparse.Namespace) -> dict[str, Any]:
    n = max(2, args.trials // 10)
    jobs = []
    for name, eid, lat, lon in (("pacific-demo", "jammer-philsea", 20.0, 132.0),
                                ("clock-fault", "jammer-philsea", 20.0, 132.0),
                                ("ephemeris-error", "jammer-philsea", 17.0, 141.0),
                                ("two-jammers", "jammer-a", 20.0, 132.0),
                                ("two-jammers", "jammer-b", 30.0, 135.0)):
        for iso in SCENARIO_EPOCHS:
            for cal in (False, True):
                jobs.append((name, eid, lat, lon, iso, cal, n))
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        runs = list(pool.map(_scenario_job, jobs))
    log(f"scenarios: {len(runs)} runs of {n} trials")
    return header("scenarios", method=(
        "sim.montecarlo.run_trials on the shipped scenario files with their own truth errors (draw_errors=False; "
        "only the noise differs between trials, seed 7), pairs by the tasking rule (primary by link-budget C/N0), "
        "1 s captures, uncalibrated and calibrated against the configured reference in the jammer's channel, at "
        "eight epochs 3 h apart on 2026-01-01. The scenario descriptions quote the server's chain as "
        "re-implemented in tests/test_sim_verify.py (primary by detected SNR, references captured in a later slot); "
        "the two agree within the noise."), trials_per_run=n, runs=runs)


# --------------------------------------------------------------------------- live demo


def section_live(args: argparse.Namespace) -> dict[str, Any]:
    import httpx

    api = "/api/v1"
    out: dict[str, Any] = {}
    work = Path(tempfile.mkdtemp(prefix="petal-study-"))
    log_path = work / "demo.log"
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONUNBUFFERED="1")
    argv = [sys.executable, "-m", "petal_locate.cli", "demo", "--host", "127.0.0.1", "--port", "0",
            "--data-dir", str(work / "data"), "--scenario", str(SCENARIOS / "quiet.yaml"),
            "--log-level", "warning", "--node-log-level", "warning"]
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=logf, text=True, env=env, cwd=str(ROOT),
                                start_new_session=True)
    try:
        base = None
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline and base is None:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                raise RuntimeError("the demo exited before serving:\n" + log_path.read_text()[-3000:])
            m = re.search(r"Console:\s+(http://\S+?)/?\s*$", line)
            base = m.group(1) if m else None
        if base is None:
            raise RuntimeError("the demo printed no console address")
        with httpx.Client(base_url=base, timeout=60.0) as client:
            online_by = time.monotonic() + 120.0
            while True:
                st = client.get(f"{api}/status").json()
                if st["nodes"] and all(n["online"] for n in st["nodes"]):
                    break
                if time.monotonic() > online_by:
                    raise RuntimeError(f"not every node came online: {[(n['site_id'], n['online']) for n in st['nodes']]}")
                time.sleep(0.5)
            time.sleep(15.0)  # let every tracker learn the quiet noise floor first, as in operation
            log("live: demo up, every node online")

            def events(state: str | None = None) -> list[dict[str, Any]]:
                return client.get(f"{api}/events", params={"state": state} if state else {}).json()

            def settle() -> None:
                client.post(f"{api}/sim/presets/quiet/load")
                end = time.monotonic() + 60.0
                while events("active") and time.monotonic() < end:
                    time.sleep(1.0)
                for e in events():
                    if e["state"] != "closed":
                        client.post(f"{api}/events/{e['id']}/close")

            for name, wait_rows in (("pacific-demo", 16), ("ephemeris-error", 16)):
                settle()
                known = {e["id"] for e in events()}
                t_load = time.time_ns()
                client.post(f"{api}/sim/presets/{name}/load")
                history = []
                event = None
                end = time.monotonic() + 240.0
                while time.monotonic() < end:
                    fresh = [e for e in events() if e["id"] not in known and e["state"] != "closed"]
                    located = [e for e in fresh if e["state"] == "located"]
                    if located:
                        event = client.get(f"{api}/events/{located[0]['id']}").json()
                        sol = event.get("solution") or {}
                        truth = event.get("truth") or {}
                        point = {"t_ns": int(time.time_ns()), "n_measurements": sol.get("n_measurements"),
                                 "miss_m": truth.get("miss_m"), "inside_ellipse": truth.get("inside_ellipse"),
                                 "semi_major_m": (sol.get("ellipse") or {}).get("semi_major_m"),
                                 "semi_minor_m": (sol.get("ellipse") or {}).get("semi_minor_m"),
                                 "dominant_error": sol.get("dominant_error")}
                        if not history or history[-1]["n_measurements"] != point["n_measurements"]:
                            history.append(point)
                        if (sol.get("n_measurements") or 0) >= wait_rows:
                            break
                    elif int(time.monotonic()) % 20 == 0:
                        log(f"live: {name}: waiting; events {[(e['id'], e['state'], e['verdict']) for e in fresh]}")
                    time.sleep(2.0)
                rec: dict[str, Any] = {"loaded_t_ns": t_load, "history": history}
                if event is not None:
                    transitions = event["transitions"]
                    t0 = int(transitions[0]["t_ns"])
                    rec["event_id"] = event["id"]
                    rec["verdict"] = event["verdict"]
                    rec["seconds_to_verdict"] = next(((int(t["t_ns"]) - t0) / NS for t in transitions
                                                      if t["verdict"] == "uplink"), None)
                    rec["seconds_to_located"] = next(((int(t["t_ns"]) - t0) / NS for t in transitions
                                                      if t["state"] == "located"), None)
                    used = [m for m in event["measurements"] if m["used"]]
                    rec["rows_used"] = len(used)
                    rec["rows_calibrated"] = sum(bool(m["calibrated"]) for m in used)
                    rec["final"] = history[-1] if history else None
                    # the same used rows without their reference corrections
                    cfg = base_config()
                    f_e = float(round(0.5 * (event["uplink_lo_hz"] + event["uplink_hi_hz"])))
                    rows = []
                    for m in used:
                        if m["correction"] is None:
                            continue
                        ends = []
                        for end_ in (m["pair"]["a"], m["pair"]["b"]):
                            tp = next(t for t in cfg.satellite(end_["sat_id"]).transponders if t.relays(f_e))
                            ends.append(PathSpec(end_["sat_id"], end_["site_id"], f_e, tp.translation_hz))
                        rows.append(Measurement(m["kind"], m["value"] + m["correction"], m["sigma"], int(m["t_ns"]),
                                                ends[0], ends[1]))
                    if len(rows) >= 2:
                        sol, why = solve(rows, cfg.orbits(), cfg.sites_xyz(), cfg.processing.solver,
                                         cfg.processing.region, cfg.resolved_uncertainty())
                        truth = event["truth"]
                        if sol is not None:
                            sc = _score(sol, {"lat_deg": truth["lat_deg"], "lon_deg": truth["lon_deg"]}, 0.0)
                            rec["uncorrected_rows"] = {"miss_m": sc["miss_m"], "nees_total": sc["nees_total"],
                                                       "semi_major_m": sc["semi_major_m"],
                                                       "reduced_chi2": sol.reduced_chi2,
                                                       "n_ambiguities": len(sol.ambiguities)}
                        else:
                            rec["uncorrected_rows"] = {"reason": why}
                out[name] = rec
                log(f"live: {name}: {json.dumps(clean(rec.get('final')))} uncorrected {json.dumps(clean(rec.get('uncorrected_rows')))}")
            settle()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        deadline = time.monotonic() + 15.0
        while True:  # never leave one of OUR nodes behind (``--exit-with-parent <our demo's pid>`` only)
            ours = []
            for child in Path("/proc").iterdir():
                if not child.name.isdigit():
                    continue
                try:
                    argv_c = (child / "cmdline").read_bytes().split(b"\0")
                except OSError:
                    continue
                if b"--exit-with-parent" in argv_c:
                    k = argv_c.index(b"--exit-with-parent")
                    if k + 1 < len(argv_c) and argv_c[k + 1] == str(proc.pid).encode():
                        ours.append(int(child.name))
            if not ours:
                break
            if time.monotonic() > deadline:
                for pid in ours:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)
                break
            time.sleep(0.5)
    return header("live", method=(
        "petal-locate demo (server plus seven node subprocesses with the simulated radio driver, real WebSockets, "
        "127.0.0.1) started on the quiet preset; each preset loaded through POST /api/v1/sim/presets/{name}/load "
        "and the event read through GET /api/v1/events/{id} until its solution used 16 rows (about 4 to 8 "
        "rounds); 'uncorrected_rows' re-solves the very rows the server used without their reference-carrier "
        "corrections. The epoch is the wall-clock time of the run, so the geometry differs from the fixed "
        "epochs of the other sections."), **clean(out))


# --------------------------------------------------------------------------- main


SECTIONS: dict[str, Callable[[argparse.Namespace], dict[str, Any]]] = {
    "detection": section_detection,
    "accuracy": section_accuracy,
    "limits": section_limits,
    "sites": section_sites,
    "montecarlo": section_montecarlo,
    "scenarios": section_scenarios,
    "live": section_live,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "results", help="output directory (default docs/results)")
    parser.add_argument("--sections", default="detection,accuracy,limits,sites,montecarlo,scenarios",
                        help="comma-separated sections: " + ", ".join(SECTIONS))
    parser.add_argument("--trials", type=int, default=40, help="Monte Carlo trials per run (default 40)")
    parser.add_argument("--workers", type=int, default=2, help="Monte Carlo worker processes (default 2)")
    args = parser.parse_args(argv)
    for name in [s.strip() for s in args.sections.split(",") if s.strip()]:
        if name not in SECTIONS:
            parser.error(f"unknown section {name!r}")
        start = time.time()
        log(f"section {name} ...")
        data = SECTIONS[name](args)
        data["elapsed_s"] = time.time() - start
        write_json(args.out, name, clean(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
