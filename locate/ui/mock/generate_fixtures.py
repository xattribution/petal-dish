#!/usr/bin/env python3
"""Generate the console's mock fixtures from the real PETAL Locate core.

    cd locate/ui && ../.venv/bin/python mock/generate_fixtures.py [--skip-accuracy] [--only events,spectrum,...]

Every number the mock server sends comes from here, and everything here comes from the
numeric core on ``config/pacific-uhf.yaml``:

- events: IQ for each receive channel is synthesised with ``core.signals`` and
  ``core.channel`` along the bent-pipe delays of ``core.geometry`` (truth orbits with
  the scenario's ephemeris errors, site clock offsets), powers from ``core.linkbudget``;
  each satellite pair is correlated with ``core.caf.caf_estimate`` inside windows from
  ``measurement_windows`` / ``search_windows``; a reference carrier calibrates one pair
  (``calibration_correction``); ``core.solver.solve`` gives the fix, its ellipse and
  ambiguities, re-solved as epochs accumulate for the ellipse history; lines of
  position come from ``core.lop.lop_geojson``.
- spectrum: per monitor window the expected Welch PSD (thermal + relayed transponder
  noise + every carrier and emitter path), the same model ``SimWorld.render_psd`` uses,
  split into a baseline and per-emitter contributions so the mock can switch emitters.
- geometry: sub-satellite points, footprints and look angles from ``core.geometry`` /
  ``core.geodesy`` / ``core.antennas``.
- accuracy: the server's own accuracy maps, from ``core.accuracy.accuracy_map`` through
  ``server.api.rest.compute_accuracy`` (the banded call ``POST /analysis/accuracy`` makes),
  for the console's default request and the other combinations the mock answers.
- advisories, fit assessment and site clock estimates: ``server.pipeline.advisory.advise``,
  ``events`` (fit consistency) and ``tasking.site_clock_offsets``, so their wording and
  rules are the server's.

Every fixture is validated against the pydantic models of ``server/api/schemas.py``
before it is written. Times are fixed at T0 (below); the mock shifts every ``*_ns``
string by ``now - T0`` when it serves them.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "fixtures"

from petal_locate.config.loader import config_json_schema, load_config  # noqa: E402
from petal_locate.config.models import EmitterModel, SystemConfig, field_errors  # noqa: E402
from petal_locate.core import antennas  # noqa: E402
from petal_locate.core.caf import caf_estimate  # noqa: E402
from petal_locate.core.channel import render_path, thermal_noise  # noqa: E402
from petal_locate.core.constants import C  # noqa: E402
from petal_locate.core.geodesy import geodesic_distance_m, lla_to_ecef, look_angles, angle_between_deg  # noqa: E402
from petal_locate.core.geometry import (  # noqa: E402
    bent_pipe,
    footprint,
    measurement_windows,
    search_windows,
    self_peak,
)
from petal_locate.core.linkbudget import direct_budget, emitter_boresight, path_budget, relay_rise_db  # noqa: E402
from petal_locate.core.lop import lop_geojson  # noqa: E402
from petal_locate.core.orbits import PerturbedOrbit, orbit_position, subsatellite_point  # noqa: E402
from petal_locate.core.signals import waveform_from_spec  # noqa: E402
from petal_locate.core.solver import calibration_correction, solve  # noqa: E402
from petal_locate.core.timebase import iso_from_ns, ns_from_iso  # noqa: E402
from petal_locate.core.types import Measurement, PathSpec  # noqa: E402
from petal_locate.core.accuracy import accuracy_map  # noqa: E402
from petal_locate.server.api import schemas as S  # noqa: E402
from petal_locate.server.api.rest import compute_accuracy  # noqa: E402
from petal_locate.server.pipeline import events as server_events  # noqa: E402
from petal_locate.server.pipeline.advisory import advise  # noqa: E402
from petal_locate.server.pipeline.tasking import UNLOCATABLE_ONE_SATELLITE, ClockEdge, _chi2_text, site_clock_offsets  # noqa: E402
from petal_locate.sim.scenario import Scenario  # noqa: E402

T0_ISO = "2026-11-13T18:00:00Z"
T0_NS = ns_from_iso(T0_ISO)
SEED = 1
CONFIG_VERSION = 7

cfg: SystemConfig = load_config(ROOT / "config" / "pacific-uhf.yaml")
ORBITS = cfg.orbits()
SITES_XYZ = cfg.sites_xyz()
UNC = cfg.resolved_uncertainty()
PROC = cfg.processing


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ns_str(t_ns: int) -> str:
    return str(int(t_ns))


def write(name: str, data: Any, indent: int | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, indent=indent, separators=None if indent else (",", ":")) + "\n")
    log(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1024:.0f} KiB)")


def check(model: Any, data: Any) -> Any:
    """Validate against a pydantic model (or TypeAdapter type) and return JSON-mode data."""
    adapter = TypeAdapter(model)
    value = adapter.validate_python(data)
    return adapter.dump_python(value, mode="json")


def b64f32(values: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(values, dtype="<f4").tobytes()).decode("ascii")


# --------------------------------------------------------------------------- scenario


SCENARIO: dict[str, Any] = {
    "name": "philippine-sea-exercise",
    "seed": SEED,
    "description": (
        "SIMULATION. Fictional exercise: a wideband noise jammer over open ocean in the "
        "Philippine Sea on shared-1, a QPSK intruder south of Honshu on "
        "shared-2 and a local interferer next to the Busan antenna. Truth errors: small "
        "ephemeris offsets on two satellites and a drifting clock at Manila."
    ),
    "emitters": [
        {
            "id": "jammer-psea",
            "name": "Noise jammer, Philippine Sea (fictional)",
            "kind": "jammer",
            "lat_deg": 20.31,
            "lon_deg": 132.42,
            "alt_m": 0.0,
            "antenna_height_m": 10.0,
            "freq_hz": 301.005e6,
            "eirp_dbw": 25.0,
            "antenna": {"type": "isotropic", "gain_dbi": 0.0},
            "pointing": "omni",
            "waveform": {"type": "noise", "bandwidth_hz": 8000.0, "seed": 1},
            "enabled": True,
        },
        {
            "id": "psk-honshu",
            "name": "QPSK intruder south of Honshu (fictional)",
            "kind": "jammer",
            "lat_deg": 29.8,
            "lon_deg": 134.9,
            "alt_m": 0.0,
            "antenna_height_m": 10.0,
            "freq_hz": 301.1305e6,
            "eirp_dbw": 22.0,
            "antenna": {"type": "isotropic", "gain_dbi": 0.0},
            "pointing": "omni",
            "waveform": {"type": "psk", "symbol_rate_hz": 3000.0, "order": 4, "rolloff": 0.35, "seed": 2},
            "enabled": True,
        },
        {
            "id": "local-busan",
            "name": "Local interferer next to the Busan antenna (fictional)",
            "kind": "local",
            "lat_deg": 35.162,
            "lon_deg": 129.091,
            "alt_m": 0.0,
            "antenna_height_m": 12.0,
            "freq_hz": 260.5025e6,
            "eirp_dbw": -20.0,
            "antenna": {"type": "isotropic", "gain_dbi": 0.0},
            "pointing": "omni",
            "waveform": {"type": "psk", "symbol_rate_hz": 2400.0, "order": 4, "rolloff": 0.35, "seed": 3},
            "enabled": True,
        },
        {
            "id": "test-chirp",
            "name": "Swept test emitter, switched off (fictional)",
            "kind": "jammer",
            "lat_deg": 3.5,
            "lon_deg": 141.0,
            "alt_m": 0.0,
            "antenna_height_m": 10.0,
            "freq_hz": 301.75e6,
            "eirp_dbw": 20.0,
            "antenna": {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 52.0, "front_to_back_db": 20.0},
            "pointing": {"satellite_id": "uhf-172e"},
            "waveform": {"type": "chirp", "bandwidth_hz": 15000.0, "sweep_period_s": 0.01, "seed": 4},
            "enabled": False,
        },
    ],
    "errors": {
        "ephemeris": {
            "muos-pac": {"ric_pos_m": [0.0, 350.0, -150.0], "ric_vel_mps": [0.0, 0.0, 0.0]},
            "uhf-172e": {"ric_pos_m": [0.0, -200.0, 100.0], "ric_vel_mps": [0.0, 0.0, 0.0]},
        },
        "sat_osc_frac": {"muos-io": 2.0e-7},
        "site_clock_offset_ns": {"manila": 1800.0, "naha": 12.0, "tokyo": -8.0},
        "site_osc_frac": {},
        "iono": {"enabled": False, "vtec_tecu": 30.0},
    },
}
SCN = Scenario.model_validate(SCENARIO)
SCN.validate_against(cfg)

TRUTH_ORBITS = {
    sat_id: (
        PerturbedOrbit(orb, np.array(SCN.errors.ephemeris[sat_id].ric_pos_m), np.array(SCN.errors.ephemeris[sat_id].ric_vel_mps), T0_NS * 1e-9)
        if sat_id in SCN.errors.ephemeris
        else orb
    )
    for sat_id, orb in ORBITS.items()
}


@dataclass
class Emitter:
    id: str
    kind: str
    lat: float
    lon: float
    height: float  # antenna height above ground, m
    freq: float
    model: EmitterModel  # power, antenna, pointing for the link budget
    spec: Any  # waveform spec
    xyz: np.ndarray


def _emitter_from_spec(spec: Any) -> Emitter:
    wf = spec.waveform
    if wf.type == "cw":
        bw = 0.0
    elif wf.type == "psk":
        bw = wf.bandwidth_hz or wf.symbol_rate_hz * (1.0 + wf.rolloff)
    else:
        bw = wf.bandwidth_hz
    model = EmitterModel(eirp_dbw=spec.eirp_dbw, antenna=spec.antenna, pointing=spec.pointing, bandwidth_hz=bw, waveform=wf.type)
    return Emitter(
        id=spec.id, kind=spec.kind, lat=spec.lat_deg, lon=spec.lon_deg, height=spec.antenna_height_m or 10.0,
        freq=spec.freq_hz, model=model, spec=wf, xyz=lla_to_ecef(spec.lat_deg, spec.lon_deg, (spec.alt_m or 0.0) + (spec.antenna_height_m or 10.0)),
    )


EMITTERS = {s.id: _emitter_from_spec(s) for s in SCN.emitters}
# Reference carriers radiate in the simulated world too (SimWorld creates them).
for carrier in cfg.carriers:
    if carrier.location is not None and carrier.emitter is not None:
        loc = carrier.location
        EMITTERS[carrier.id] = Emitter(
            id=carrier.id, kind="carrier", lat=loc.lat_deg, lon=loc.lon_deg, height=loc.antenna_height_m,
            freq=carrier.uplink_center_hz, model=carrier.emitter,
            spec=type("W", (), {"type": carrier.emitter.waveform, "bandwidth_hz": carrier.emitter.bandwidth_hz})(),
            xyz=loc.xyz(),
        )


def bandwidth_of(em: Emitter) -> float:
    return float(em.model.bandwidth_hz)


# --------------------------------------------------------------------------- IQ synthesis


def _site_clock_s(site_id: str) -> float:
    return SCN.errors.site_clock_offset_ns.get(site_id, 0.0) * 1e-9


def _poly(t_grid: np.ndarray, values: np.ndarray, deg: int = 4) -> np.polynomial.Polynomial:
    return np.polynomial.Polynomial.fit(t_grid, values, deg)


def synth_channel(channel_id: str, t_start_ns: int, n: int, fs: float, fc: float, emitter_ids: list[str]) -> np.ndarray:
    """Complex baseband of one receive channel: thermal noise, relayed transponder noise
    and every bent-pipe (and direct) path of the listed emitters inside [fc +- fs/2]."""
    site, channel = cfg.channel(channel_id)
    xg = SITES_XYZ[site.id]
    dg = _site_clock_s(site.id)
    t_s = t_start_ns * 1e-9
    y = thermal_noise(n, f"thermal|{SEED}|{channel_id}|{t_start_ns}|{fs}|{fc}").astype(np.complex128)
    t_grid = np.linspace(0.0, n / fs, 9)
    for sat in cfg.satellites:
        orbit = TRUTH_ORBITS[sat.id]
        osc = SCN.errors.sat_osc_frac.get(sat.id, 0.0)
        for tp in sat.transponders:
            lo, hi = tp.downlink_lo_hz, tp.downlink_hi_hz
            overlap = max(0.0, min(hi, fc + fs / 2) - max(lo, fc - fs / 2))
            if overlap > 0:
                rise = relay_rise_db(cfg, channel_id, sat.id, tp.id, t_s)
                if rise is not None:
                    ratio = 10 ** (rise / 10) - 1.0
                    # relayed noise: white over the overlap; independent per channel (simplification)
                    w = thermal_noise(n, f"relay|{SEED}|{sat.id}|{tp.id}|{channel_id}|{t_start_ns}")
                    y += math.sqrt(ratio * overlap / fs) * w
            for eid in emitter_ids:
                em = EMITTERS[eid]
                if em.kind == "local" or not tp.relays(em.freq):
                    continue
                t_nom = tp.translation_hz
                t_true = t_nom * (1.0 + osc)
                off = em.freq + t_nom - fc
                if abs(off) + bandwidth_of(em) / 2 > fs / 2:
                    continue
                pb = path_budget(cfg, em.model, em.freq, em.xyz, sat.id, tp.id, channel_id, t_s)
                if pb is None:
                    continue
                amp = math.sqrt(10 ** ((float(pb.downlink.c_rx_dbw) - float(pb.downlink.site_n0_dbw_hz)) / 10) / fs)
                bp = bent_pipe(em.xyz, orbit, xg, t_s + dg, tp.group_delay_s, dt_s=t_grid)
                pd = _poly(t_grid, np.asarray(bp.D))
                ptd = _poly(t_grid, np.asarray(bp.tau_d))
                ptu = _poly(t_grid, np.asarray(bp.tau_u))
                f_e, f_tune = em.freq, fc
                wave = waveform_from_spec(em.spec, f"emitter|{SEED}|{em.id}|{getattr(em.spec, 'seed', 0)}")

                def delay_fn(tr: np.ndarray, pd=pd, dg=dg) -> np.ndarray:
                    return pd(tr) - dg

                def phase_fn(tr: np.ndarray, ptd=ptd, ptu=ptu, f_e=f_e, t_true=t_true, f_tune=f_tune, tau_x=tp.group_delay_s, dg=dg) -> np.ndarray:
                    return (f_e + t_true - f_tune) * (tr + dg) - (f_e + t_true) * (ptd(tr) + tau_x) - f_e * ptu(tr)

                y += render_path(wave, t_start_ns, n, fs, delay_fn, phase_fn, amp)
    for eid in emitter_ids:
        em = EMITTERS[eid]
        if em.kind != "local" or abs(em.freq - fc) + bandwidth_of(em) / 2 > fs / 2:
            continue
        db = direct_budget(em.model.eirp_dbw, em.model.antenna, None, em.xyz, em.height, xg, site.antenna_height_m, channel, cfg.boresight_ecef(channel_id, t_s), em.freq)
        if not db.in_horizon:
            continue
        amp = math.sqrt(10 ** ((db.c_rx_dbw - (-228.6 + 10 * math.log10(channel.system_noise_temp_k))) / 10) / fs)
        dist = float(np.linalg.norm(xg - em.xyz))
        wave = waveform_from_spec(em.spec, f"emitter|{SEED}|{em.id}|{getattr(em.spec, 'seed', 0)}")
        y += render_path(wave, t_start_ns, n, fs, lambda tr: dist / C - dg, lambda tr: (em.freq - fc) * (tr + dg) - em.freq * dist / C, amp)
    return y


# --------------------------------------------------------------------------- CAF + measurements


def channel_sat(channel_id: str) -> str:
    _, ch = cfg.channel(channel_id)
    return ch.watched_satellite_ids[0]


def transponder_for(sat_id: str, f_e: float) -> Any:
    for tp in cfg.satellite(sat_id).transponders:
        if tp.relays(f_e):
            return tp
    raise ValueError(f"no transponder of {sat_id} relays {f_e}")


@dataclass
class PairResult:
    a_ch: str
    b_ch: str
    a: PathSpec
    b: PathSpec
    t_mid_ns: int
    res: Any
    caf_snr: float


def correlate(a_ch: str, b_ch: str, f_e_nom: float, bw: float, t_start_ns: int, emitter_ids: list[str], correction: tuple[float | None, float | None] | None = None) -> PairResult:
    fs = max(PROC.capture.min_sample_rate_hz, PROC.capture.oversample * bw)
    n = min(int(round(PROC.capture.duration_s * fs)), PROC.capture.max_samples)
    site_a, _ = cfg.channel(a_ch)
    site_b, _ = cfg.channel(b_ch)
    sat_a, sat_b = channel_sat(a_ch), channel_sat(b_ch)
    tp_a, tp_b = transponder_for(sat_a, f_e_nom), transponder_for(sat_b, f_e_nom)
    a = PathSpec(sat_a, site_a.id, f_e_nom, tp_a.translation_hz, tp_a.group_delay_s)
    b = PathSpec(sat_b, site_b.id, f_e_nom, tp_b.translation_hz, tp_b.group_delay_s)
    fc_a, fc_b = a.f_tune_hz, b.f_tune_hz
    ya = synth_channel(a_ch, t_start_ns, n, fs, fc_a, emitter_ids)
    yb = synth_channel(b_ch, t_start_ns, n, fs, fc_b, emitter_ids)
    t_mid_ns = t_start_ns + round(1e9 * (n - 1) / (2 * fs))
    t_mid = t_mid_ns * 1e-9
    geo = measurement_windows(a, b, t_mid, ORBITS, SITES_XYZ, PROC.region, PROC.solver.min_elevation_deg, PROC.solver.height_m, 2.0)
    dwin, nwin = search_windows(geo, a, b, PROC.caf, UNC, correction)
    exclude = []
    mirror = None
    for sat in cfg.satellites:
        tps = [tp for tp in sat.transponders if tp.relays(f_e_nom)]
        if not tps:
            continue
        f_d = f_e_nom + tps[0].translation_hz
        if abs(f_d - fc_a) < fs / 2 and abs(f_d - fc_b) < fs / 2:
            exclude.append(tuple(float(v) for v in self_peak(sat.id, tps[0].translation_hz, f_e_nom, a, b, t_mid, ORBITS, SITES_XYZ)))
    if sat_a != sat_b and abs(tp_a.translation_hz - tp_b.translation_hz) < 1.0:
        swapped = measurement_windows(
            PathSpec(sat_b, site_a.id, f_e_nom, tp_b.translation_hz), PathSpec(sat_a, site_b.id, f_e_nom, tp_a.translation_hz),
            t_mid, ORBITS, SITES_XYZ, PROC.region, PROC.solver.min_elevation_deg, PROC.solver.height_m, 2.0)
        if swapped is not None:
            mirror = search_windows(swapped, a, b, PROC.caf, UNC, None)
    res = caf_estimate(
        ya, yb, fs, delay_window=dwin, doppler_window=nwin, signal_bw_hz=bw if bw > 0 else None, t0_offset_s=0.0,
        exclude=exclude, mirror_window=mirror, min_snr_db=PROC.caf.min_snr_db, max_tdoa_sigma_s=PROC.caf.max_tdoa_sigma_s,
    )
    return PairResult(a_ch, b_ch, a, b, t_mid_ns, res, float(res.snr_db))


def chref(path: PathSpec, ch: str) -> dict[str, str]:
    return {"sat_id": path.sat_id, "site_id": path.site_id, "channel_id": ch}


def caf_display(pr: PairResult) -> dict[str, Any]:
    r = pr.res
    surf = np.asarray(r.surface, dtype=np.float64)
    return {
        "t_ns": ns_str(pr.t_mid_ns),
        "pair": {"a": chref(pr.a, pr.a_ch), "b": chref(pr.b, pr.b_ch)},
        "surface": [[round(float(v), 2) for v in row] for row in surf],
        "delay_axis_s": [float(v) for v in np.asarray(r.delay_axis_s)],
        "doppler_axis_hz": [float(v) for v in np.asarray(r.doppler_axis_hz)],
        "delay_cut": {"x": [float(v) for v in np.asarray(r.delay_cut[0])], "db": [round(float(v), 2) for v in np.asarray(r.delay_cut[1])]},
        "doppler_cut": {"x": [float(v) for v in np.asarray(r.doppler_cut[0])], "db": [round(float(v), 2) for v in np.asarray(r.doppler_cut[1])]},
        "peak_tdoa_s": None if r.tdoa_s is None else float(r.tdoa_s),
        "peak_fdoa_hz": None if r.fdoa_hz is None else float(r.fdoa_hz),
        "snr_db": float(r.snr_db),
        "detected": bool(r.detected),
        "reason": r.reason,
        # as server/pipeline/events.caf_display builds them (a CW ridge has no delay)
        "candidates": [{"tdoa_s": None if (r.reason == "narrowband" or not np.isfinite(c[0])) else float(c[0]),
                        "fdoa_hz": float(c[1]), "snr_db": float(c[2])}
                       for c in (r.candidates or [])[:8] if np.isfinite(c[1]) and np.isfinite(c[2])],
    }


@dataclass
class Row:
    """A measurement row as stored by the pipeline: the solver input plus display data."""

    m: Measurement | None  # None when there is no value
    kind: str
    pr: PairResult
    value: float | None
    sigma: float | None
    used: bool
    reason: str | None


def rows_from_pair(pr: PairResult, *, clock_unlocked_sites: set[str], cal: dict[str, tuple[float, Any]] | None = None) -> list[Row]:
    r = pr.res
    out: list[Row] = []
    for kind in ("tdoa", "fdoa"):
        value = r.tdoa_s if kind == "tdoa" else r.fdoa_hz
        sigma = r.sigma_tdoa_s if kind == "tdoa" else r.sigma_fdoa_hz
        reason = None
        if not r.detected:
            reason = r.reason or "not_detected"
        elif r.mirror_ambiguous:
            reason = "ambiguous"
        elif value is None or sigma is None:
            reason = r.reason or ("narrowband" if kind == "tdoa" else "not_detected")
        elif kind == "tdoa" and pr.a.site_id != pr.b.site_id and ({pr.a.site_id, pr.b.site_id} & clock_unlocked_sites):
            reason = "clock_unlocked"
        m = None
        if value is not None and sigma is not None:
            corr, ref = (cal or {}).get(kind, (0.0, None))
            m = Measurement(kind, float(value), float(sigma), pr.t_mid_ns, pr.a, pr.b, correction=corr, cal=ref, tag=f"{pr.a_ch}|{pr.b_ch}")
        out.append(Row(m, kind, pr, None if value is None else float(value), None if sigma is None else float(sigma), reason is None, reason))
    return out


def measurement_rows(rows: list[Row], solution: Any) -> list[dict[str, Any]]:
    used = [row for row in rows if row.used and row.m is not None]
    residual_of = {}
    if solution is not None and len(solution.residuals) == len(used):
        for row, res in zip(used, solution.residuals):
            residual_of[id(row)] = float(res)
    out = []
    for row in rows:
        calibrated = row.m is not None and row.m.cal is not None
        value = row.value
        if calibrated and value is not None:
            value = value - row.m.correction
        out.append({
            "t_ns": ns_str(row.pr.t_mid_ns),
            "kind": row.kind,
            "pair": {"a": chref(row.pr.a, row.pr.a_ch), "b": chref(row.pr.b, row.pr.b_ch)},
            "value": value,
            "sigma": row.sigma,
            "residual": residual_of.get(id(row)),
            "snr_db": row.pr.caf_snr,
            "calibrated": bool(calibrated),
            "used": row.used,
            "reason": row.reason,
            # as server/pipeline/tasking.py fills them: the correction already subtracted and its carrier
            "correction": float(row.m.correction) if calibrated else None,
            "reference_carrier_id": row.m.cal.cal_id.split("|")[0] if calibrated else None,
        })
    return out


def lop_collection(event_id: str, rows: list[Row], newest: dict[tuple[str, str, str], Row]) -> dict[str, Any]:
    """GET /events/{id}/lop: lines of position of the newest used row of each (pair, kind),
    from core.lop.lop_geojson, with the console's properties (LopProperties)."""
    features = []
    for r in newest.values():
        seq = rows.index(r) + 1
        fc = lop_geojson(r.m, ORBITS, SITES_XYZ, PROC.region, PROC.solver.height_m, PROC.solver.min_elevation_deg, 0.25, 2.0)
        for f in fc["features"]:
            p = f["properties"]
            props = {"role": p["role"], "kind": p["kind"], "event_id": event_id, "seq": seq, "tag": p.get("tag", ""),
                     "a_sat_id": p["a_sat_id"], "a_site_id": p["a_site_id"], "a_channel_id": r.pr.a_ch,
                     "b_sat_id": p["b_sat_id"], "b_site_id": p["b_site_id"], "b_channel_id": r.pr.b_ch,
                     "t_ns": str(p["t_ns"]), "value": p["value"], "correction": p["correction"], "calibrated": p["calibrated"],
                     "sigma": p["sigma"], "sigma_total": p["sigma_total"], "k_sigma": p["k_sigma"], "height_m": p["height_m"]}
            features.append({"type": "Feature", "geometry": f["geometry"], "properties": props})
    return check(S.LopCollection, {"type": "FeatureCollection", "features": features})


def solve_rows(rows: list[Row], starts: Any = None) -> tuple[Any, str | None]:
    meas = [row.m for row in rows if row.used and row.m is not None]
    if len(meas) < 2:
        return None, "fewer than two measurements"
    return solve(meas, ORBITS, SITES_XYZ, PROC.solver, PROC.region, UNC, starts)


def inside_ellipse(sol: Any, lat: float, lon: float) -> bool:
    from petal_locate.core.geodesy import radii_of_curvature
    m_rad, n_rad = radii_of_curvature(sol.lat_deg)
    d_lon = ((lon - sol.lon_deg + 180.0) % 360.0) - 180.0
    e = math.radians(d_lon) * (float(n_rad) + sol.height_m) * math.cos(math.radians(sol.lat_deg))
    n = math.radians(lat - sol.lat_deg) * (float(m_rad) + sol.height_m)
    v = np.array([e, n])
    d2 = float(v @ np.linalg.solve(np.asarray(sol.cov_en_m2), v))
    return d2 <= -2.0 * math.log(1.0 - sol.ellipse["confidence"])


# --------------------------------------------------------------------------- spectrum model


def monitors_of(channel: Any) -> list[Any]:
    return list(channel.monitors)


def emitter_downlinks(em: Emitter, channel_id: str, t_s: float) -> list[tuple[float, float, float]]:
    """(f_center_hz, C_rx / N0_site linear [1/Hz... per Hz], bandwidth) of every path of the
    emitter into the channel: (center, C/N0_site in Hz, bandwidth)."""
    site, channel = cfg.channel(channel_id)
    n0_site = 10 ** ((-228.6 + 10 * math.log10(channel.system_noise_temp_k)) / 10)
    out = []
    if em.kind == "local":
        db = direct_budget(em.model.eirp_dbw, em.model.antenna, None, em.xyz, em.height, SITES_XYZ[site.id], site.antenna_height_m, channel, cfg.boresight_ecef(channel_id, t_s), em.freq)
        if db.in_horizon:
            out.append((em.freq, 10 ** (db.c_rx_dbw / 10) / n0_site, bandwidth_of(em)))
        return out
    for sat in cfg.satellites:
        for tp in sat.transponders:
            if not tp.relays(em.freq):
                continue
            pb = path_budget(cfg, em.model, em.freq, em.xyz, sat.id, tp.id, channel_id, t_s)
            if pb is None:
                continue
            out.append((em.freq + tp.translation_hz, 10 ** (float(pb.downlink.c_rx_dbw) / 10) / n0_site, bandwidth_of(em)))
    return out


def spectral_shape(freqs: np.ndarray, bin_hz: float, f0: float, bw: float, waveform: str, rolloff: float = 0.35) -> np.ndarray:
    """Fraction of the power in each bin (sums to 1 when the signal is inside the window)."""
    if bw <= 0 or waveform == "cw":
        k = (f0 - freqs[0]) / bin_hz
        shape = np.zeros_like(freqs)
        k0 = int(round(k))
        for dk, w in ((-1, 1 / 6), (0, 4 / 6), (1, 1 / 6)):
            if 0 <= k0 + dk < freqs.size:
                shape[k0 + dk] = w
        return shape
    lo_edges = freqs - bin_hz / 2
    hi_edges = freqs + bin_hz / 2
    if waveform == "psk":
        rs = bw / (1 + rolloff)
        fine = np.linspace(-bw / 2, bw / 2, 2001)
        f1 = (1 - rolloff) * rs / 2
        p = np.where(np.abs(fine) <= f1, 1.0, 0.5 * (1 + np.cos(np.pi / (rolloff * rs) * (np.abs(fine) - f1))))
        p = np.where(np.abs(fine) <= bw / 2, p, 0.0)
        cdf = np.concatenate([[0.0], np.cumsum((p[1:] + p[:-1]) / 2 * np.diff(fine))])
        cdf /= cdf[-1]
        g = lambda x: np.interp(x - f0, fine, cdf, left=0.0, right=1.0)
        return g(hi_edges) - g(lo_edges)
    a, b = f0 - bw / 2, f0 + bw / 2
    return np.clip(np.minimum(hi_edges, b) - np.maximum(lo_edges, a), 0.0, None) / bw


# --------------------------------------------------------------------------- builders


LOST_NS = T0_NS - 63_000_000_000  # EV-0042 lost here ...
RESUMED_NS = T0_NS - 22_000_000_000  # ... and detected again 41 s later (over REACQUIRE_KEEP_S)


def build_events() -> dict[str, Any]:
    """EV-0042: located, then lost for 41 s; on its return the earlier position and rows
    are set aside (``EventBook.reopen`` beyond ``REACQUIRE_KEEP_S``) and it is located
    again from the new rows only, so the dossier shows the break, the start of the
    current transmission and the set-aside ellipses."""
    log("events: synthesising IQ and correlating (this takes a minute)")
    pre = [T0_NS - 75_000_000_000 + k * 5_000_000_000 for k in range(3)]  # before the break
    post = [T0_NS - (3 - k) * 5_000_000_000 for k in range(4)]  # after it, last at T0
    epochs = pre + post
    unlocked = {"manila"}
    j1 = "jammer-psea"
    # all emitters radiating in the band are in the synthesis, including the reference carriers
    band_emitters = [j1, "ref-guam", "ref-sapporo", "psk-honshu"]

    # Reference calibration of pair P1 on ref-guam (shared-1 of every satellite)
    ref = EMITTERS["ref-guam"]
    cal_pair = ("naha-pac", "naha-172e")
    cal_pr = correlate(cal_pair[0], cal_pair[1], ref.freq, bandwidth_of(ref), post[-1] - 2_000_000_000, band_emitters)
    cal: dict[str, tuple[float, Any]] = {}
    for kind in ("tdoa", "fdoa"):
        z = cal_pr.res.tdoa_s if kind == "tdoa" else cal_pr.res.fdoa_hz
        sg = cal_pr.res.sigma_tdoa_s if kind == "tdoa" else cal_pr.res.sigma_fdoa_hz
        if z is not None and sg is not None:
            c, calref = calibration_correction(Measurement(kind, z, sg, cal_pr.t_mid_ns, cal_pr.a, cal_pr.b), ref.xyz, ORBITS, SITES_XYZ, f"ref-guam|{kind}|{cal_pr.t_mid_ns}")
            cal[kind] = (c, calref)
    log(f"  calibration ref-guam on {cal_pair}: {[(k, v[0]) for k, v in cal.items()]}")

    pairs = [("naha-pac", "naha-172e"), ("naha-io", "naha-172e"), ("tokyo-pac", "tokyo-io"), ("manila-io", "busan-172e")]
    f_e_nom = 301.005e6
    all_rows: list[list[Row]] = []
    latest_pr: PairResult | None = None
    for k, t in enumerate(epochs):
        rows_k: list[Row] = []
        for (a_ch, b_ch) in pairs:
            corr = None
            if (a_ch, b_ch) == cal_pair and cal:
                corr = (cal.get("tdoa", (None,))[0], cal.get("fdoa", (None,))[0])
            pr = correlate(a_ch, b_ch, f_e_nom, 8000.0, t, band_emitters, corr)
            rows_k += rows_from_pair(pr, clock_unlocked_sites=unlocked, cal=cal if (a_ch, b_ch) == cal_pair else None)
            if (a_ch, b_ch) == cal_pair:
                latest_pr = pr
        all_rows.append(rows_k)
        log(f"  epoch {k}: " + ", ".join(f"{r.kind}:{'ok' if r.used else r.reason}" for r in rows_k))
    history = []
    sol = None
    # solves before the break (later set aside), then from the new rows only
    for part, first_k, last_k in (("pre", 0, len(pre) - 1), ("post", len(pre), len(epochs) - 1)):
        for k in range(first_k + 1, last_k + 1):
            subset = [r for rk in all_rows[first_k: k + 1] for r in rk]
            s, why = solve_rows(subset)
            if s is not None:
                history.append(history_row(all_rows[k][0].pr.t_mid_ns, s, set_aside=part == "pre"))
                if part == "post":
                    sol = s
            log(f"  {part} solve with epochs {first_k}..{k}: {why or (round(s.lat_deg, 3), round(s.lon_deg, 3), round(s.ellipse['semi_major_m']), s.ambiguity_resolved_by, len(s.ambiguities))}")
    assert sol is not None, "jammer-psea did not solve"
    # the rows measured before the break: kept in the table, no longer used
    lost_at = server_events._clock_text(LOST_NS)
    for rk in all_rows[: len(pre)]:
        for r in rk:
            if r.used:
                r.used = False
                r.reason = f"{server_events.SET_ASIDE_REASON}: measured before the signal was lost at {lost_at}"
    rows = [r for rk in all_rows for r in rk]
    truth = EMITTERS[j1]
    miss = float(geodesic_distance_m(sol.lat_deg, sol.lon_deg, truth.lat, truth.lon))
    log(f"  fix {sol.lat_deg:.4f} {sol.lon_deg:.4f} miss {miss:.0f} m, ellipse {sol.ellipse}, dominant {sol.dominant_error}")

    # Lines of position of the newest used row of each (pair, kind)
    newest: dict[tuple[str, str, str], Row] = {}
    for r in rows:
        if r.used and r.m is not None:
            newest[(r.pr.a_ch, r.pr.b_ch, r.kind)] = r
    lop1 = lop_collection("EV-0042", rows, newest)
    features = lop1["features"]
    log(f"  {len(features)} LOP features")

    return {"rows": rows, "solution": sol, "history": history, "caf": caf_display(latest_pr), "lop": lop1, "miss": miss, "epochs": epochs, "pre": pre, "post": post}


def build_event_mixed() -> dict[str, Any]:
    """EV-0040: two transmitters on one frequency, detected alternately (fictional): the
    rows of both go into one solve, which the server flags inconsistent (UX-1) and never
    calls located. The newest rows are mixed too, so the split test (K2) cannot separate
    them; the position is shown enlarged and is not a fix."""
    log("event mixed: two jammers on shared-2 detected alternately")
    extra = {}
    for eid, lat, lon in (("mixed-a", 14.0, 148.0), ("mixed-b", 16.2, 146.3)):
        spec = EmitterModel(eirp_dbw=25.0, antenna={"type": "isotropic", "gain_dbi": 0.0}, pointing="omni", bandwidth_hz=2000.0, waveform="noise")
        wf = type("W", (), {"type": "noise", "bandwidth_hz": 2000.0, "seed": 7 if eid == "mixed-a" else 8})()
        extra[eid] = Emitter(id=eid, kind="jammer", lat=lat, lon=lon, height=10.0, freq=301.1235e6, model=spec, spec=wf, xyz=lla_to_ecef(lat, lon, 10.0))
    EMITTERS.update(extra)
    try:
        epochs = [T0_NS - (50 - 5 * k) * 1_000_000_000 for k in range(6)]
        who = ["mixed-a", "mixed-b"] * 3
        pairs = [("naha-pac", "naha-172e"), ("naha-io", "naha-172e")]
        all_rows = []
        for t, eid in zip(epochs, who):
            rk = []
            for a_ch, b_ch in pairs:
                pr = correlate(a_ch, b_ch, 301.1235e6, 2000.0, t, [eid, "psk-honshu", "ref-sapporo"])
                rk += rows_from_pair(pr, clock_unlocked_sites={"manila"})
            all_rows.append(rk)
            log(f"  {eid}: " + ", ".join(f"{r.kind}:{'ok' if r.used else r.reason}" for r in rk))
        rows = [r for rk in all_rows for r in rk]
        history, sol = [], None
        for k in range(1, len(epochs)):
            subset = [r for rk in all_rows[: k + 1] for r in rk]
            s, why = solve_rows(subset)
            if s is not None:
                history.append(history_row(all_rows[k][0].pr.t_mid_ns, s))
                sol = s
            log(f"  solve 0..{k}: {why or (round(s.lat_deg, 3), round(s.lon_deg, 3), round(s.reduced_chi2 or 0, 1))}")
        assert sol is not None, "the mixed event did not solve"
        newest: dict[tuple[str, str, str], Row] = {}
        for r in rows:
            if r.used and r.m is not None:
                newest[(r.pr.a_ch, r.pr.b_ch, r.kind)] = r
        return {"rows": rows, "solution": sol, "history": history, "caf": caf_display(all_rows[-1][-1].pr), "lop": lop_collection("EV-0040", rows, newest),
                "epochs": epochs, "truth": extra}
    finally:
        for eid in extra:
            EMITTERS.pop(eid, None)


def build_event2() -> dict[str, Any]:
    log("event 2: QPSK intruder on shared-2, two satellite pairs")
    epochs = [T0_NS - 40_000_000_000]  # first capture only: the fix is still ambiguous
    band = ["psk-honshu", "ref-sapporo", "jammer-psea", "ref-guam"]
    pairs = [("naha-pac", "naha-172e"), ("naha-io", "naha-172e")]
    em = EMITTERS["psk-honshu"]
    f_e_nom = round(em.freq)
    all_rows = []
    for t in epochs:
        rk = []
        for a_ch, b_ch in pairs:
            pr = correlate(a_ch, b_ch, f_e_nom, bandwidth_of(em), t, band)
            rk += rows_from_pair(pr, clock_unlocked_sites={"manila"})
        all_rows.append(rk)
        log("  " + ", ".join(f"{r.kind}:{'ok' if r.used else r.reason}" for r in rk))
    rows = [r for rk in all_rows for r in rk]
    history, sol, why = [], None, None
    for k in range(len(epochs)):
        subset = [r for rk in all_rows[: k + 1] for r in rk]
        s, why = solve_rows(subset)
        log(f"  solve 0..{k}: {why or (round(s.lat_deg, 3), round(s.lon_deg, 3), round(s.ellipse['semi_major_m']), s.ambiguity_resolved_by, [(round(a.lat_deg, 2), round(a.lon_deg, 2)) for a in s.ambiguities])}")
        if s is not None:
            history.append(history_row(all_rows[k][0].pr.t_mid_ns, s))
            sol = s
    newest: dict[tuple[str, str, str], Row] = {}
    for r in rows:
        if r.used and r.m is not None:
            newest[(r.pr.a_ch, r.pr.b_ch, r.kind)] = r
    last_pr = all_rows[-1][-1].pr
    return {"rows": rows, "solution": sol, "why": why, "history": history, "caf": caf_display(last_pr), "lop": lop_collection("EV-0043", rows, newest), "epochs": epochs}


def evidence_for(sat_ids: list[str], f_down_by_sat: dict[str, tuple[float, float]], seen_snr: dict[str, tuple[float, float]], offline: set[str], t_s: float, primary: str | None) -> list[dict[str, Any]]:
    """Evidence rows for every channel watching the satellites, MATH.md section 7."""
    from petal_locate.core.linkbudget import downlink_quality_db
    det = PROC.detection
    rows = []
    q_primary = None
    if primary:
        site_p, _ = cfg.channel(primary)
        sat_p = channel_sat(primary)
        tp_p = cfg.uplink_for(sat_p, sum(f_down_by_sat[sat_p]) / 2)[0]
        q_primary = downlink_quality_db(cfg, primary, sat_p, tp_p.id, t_s)
    cn0_primary = seen_snr.get(primary, (0.0, 0.0))[1] if primary else 0.0
    for site in cfg.sites:
        for ch in site.channels:
            sat = ch.watched_satellite_ids[0]
            if sat not in sat_ids:
                continue
            lo, hi = f_down_by_sat[sat]
            tp = cfg.uplink_for(sat, (lo + hi) / 2)[0]
            q = downlink_quality_db(cfg, ch.id, sat, tp.id, t_s)
            margin = None
            if q is not None and q_primary is not None:
                bin_hz = ch.monitors[0].bin_hz
                margin = cn0_primary + q - q_primary - 10 * math.log10(max(hi - lo, bin_hz)) - (det.threshold_db + det.sensitivity_margin_db)
            if site.id in offline:
                state = "stale"
            elif ch.id in seen_snr:
                state = "seen"
            elif margin is not None and margin >= 0:
                state = "not_seen_capable"
            else:
                state = "not_capable"
            snr, cn0 = seen_snr.get(ch.id, (None, None))
            mon = next((m for m in ch.monitors if abs(m.center_hz - (lo + hi) / 2) <= m.span_hz / 2), None)
            rows.append({
                "channel_id": ch.id, "site_id": site.id, "sat_id": sat, "state": state,
                "snr_db": None if state != "seen" else round(snr, 1), "cn0_dbhz": None if state != "seen" else round(cn0, 1),
                "predicted_margin_db": None if margin is None else round(margin, 1),
                "last_seen_ns": None, "monitor_id": None if mon is None else mon.id, "downlink_lo_hz": lo, "downlink_hi_hz": hi,
            })
    return rows


def detection_stats(em: Emitter, channel_id: str, t_s: float) -> tuple[float, float] | None:
    """(snr_db, cn0_dbhz) of the emitter's detection in the channel (MATH.md section 7)."""
    _, channel = cfg.channel(channel_id)
    best = None
    for f0, cn, bw in emitter_downlinks(em, channel_id, t_s):
        for mon in channel.monitors:
            if abs(f0 - mon.center_hz) > mon.span_hz / 2:
                continue
            bin_hz = mon.bin_hz
            # baseline inside the transponder passband = 1 + relay ratio
            sat = channel_sat(channel_id)
            mapped = cfg.uplink_for(sat, f0)
            base = 1.0
            if mapped is not None:
                rise = relay_rise_db(cfg, channel_id, sat, mapped[0].id, t_s)
                base = 10 ** ((rise or 0) / 10)
            # C / (N0_site bin_hz) is the carrier in units of one bin of thermal noise; spread
            # over the occupied bins (a CW keeps about 2/3 of it in its peak bin)
            width = max(bw, 1.5 * bin_hz)
            p_bin = cn / width
            snr = 10 * math.log10((p_bin + base) / base)
            cn0 = 10 * math.log10(cn / base) if cn > 0 else -99.0
            if snr >= PROC.detection.threshold_db and (best is None or snr > best[0]):
                best = (snr, cn0)
    return best


def _spectrum_simworld(t_s: float) -> list[dict[str, Any]] | None:
    """Expected PSDs from ``SimWorld.render_psd(expected=True)``: a world with no scenario
    emitters gives the baseline (thermal, relayed transponder noise and the reference
    carriers SimWorld creates), and one world per scenario emitter (enabled, alone)
    gives that emitter's contribution as the difference."""
    try:
        from petal_locate.sim.world import SimWorld
    except Exception as exc:  # pragma: no cover - world.py not available
        log(f"  SimWorld unavailable ({exc}); using the link-budget model")
        return None
    t_ns = int(round(t_s * 1e9))
    base_scn = Scenario.model_validate({**SCENARIO, "emitters": []})
    try:
        base_world = SimWorld(cfg, base_scn, epoch_ns=T0_NS)
        worlds = {}
        for spec in SCENARIO["emitters"]:
            one = Scenario.model_validate({**SCENARIO, "emitters": [{**spec, "enabled": True}]})
            worlds[spec["id"]] = SimWorld(cfg, one, epoch_ns=T0_NS)
    except Exception as exc:  # pragma: no cover
        log(f"  SimWorld failed to build ({exc}); using the link-budget model")
        return None
    windows = []
    for site in cfg.sites:
        for ch in site.channels:
            for mon in ch.monitors:
                args = (ch.id, t_ns, mon.center_hz, mon.span_hz, mon.nfft, mon.navg)
                base = 10 ** (base_world.render_psd(*args, expected=True).astype(np.float64) / 10)
                parts = []
                for eid, world in worlds.items():
                    lin = 10 ** (world.render_psd(*args, expected=True).astype(np.float64) / 10) - base
                    if lin.max() > 1e-3:
                        nz = np.nonzero(lin > 1e-4)[0]
                        start, stop = int(nz[0]), int(nz[-1]) + 1
                        parts.append({"emitter_id": eid, "authorized": False, "start": start, "lin": b64f32(np.clip(lin[start:stop], 0.0, None))})
                sat_id, tp_id = mon.id.split(":") if ":" in mon.id else (ch.watched_satellite_ids[0], "")
                windows.append({
                    "channel_id": ch.id, "monitor_id": mon.id, "site_id": site.id, "sat_id": sat_id, "transponder_id": tp_id,
                    "center_hz": mon.center_hz, "span_hz": mon.span_hz, "nfft": mon.nfft, "navg": mon.navg, "frame_rate_hz": mon.frame_rate_hz,
                    "base_lin": b64f32(base), "parts": parts,
                })
    return windows


def build_spectrum(t_s: float) -> dict[str, Any]:
    log("spectrum: expected PSD per monitor window")
    windows = _spectrum_simworld(t_s)
    source = "SimWorld.render_psd(expected=True)"
    if windows is None:
        windows = _spectrum_linkbudget(t_s)
        source = "core.linkbudget path budgets"
    log(f"  {len(windows)} windows from {source}")
    return {"t0_ns": ns_str(T0_NS), "threshold_db": PROC.detection.threshold_db, "source": source, "windows": windows}


def _spectrum_linkbudget(t_s: float) -> list[dict[str, Any]]:
    windows = []
    contributors = [e for e in EMITTERS.values()]
    for site in cfg.sites:
        for ch in site.channels:
            n0 = 1.0
            for mon in ch.monitors:
                nfft = mon.nfft
                freqs = mon.center_hz + (np.arange(nfft) - nfft / 2) * mon.span_hz / nfft
                bin_hz = mon.bin_hz
                base = np.full(nfft, n0)
                for sat in cfg.satellites:
                    for tp in sat.transponders:
                        if tp.downlink_hi_hz < freqs[0] or tp.downlink_lo_hz > freqs[-1]:
                            continue
                        rise = relay_rise_db(cfg, ch.id, sat.id, tp.id, t_s)
                        if rise is None:
                            continue
                        inside = (freqs >= tp.downlink_lo_hz) & (freqs <= tp.downlink_hi_hz)
                        base[inside] += 10 ** (rise / 10) - 1.0
                parts = []
                for em in contributors:
                    lin = np.zeros(nfft)
                    for f0, cn, bw in emitter_downlinks(em, ch.id, t_s):
                        if f0 + bw / 2 < freqs[0] - bin_hz or f0 - bw / 2 > freqs[-1] + bin_hz:
                            continue
                        rolloff = getattr(em.spec, "rolloff", 0.35) if em.model.waveform == "psk" else 0.35
                        shape = spectral_shape(freqs, bin_hz, f0, bw, em.model.waveform, rolloff)
                        lin += cn / mon.span_hz * nfft * shape
                    if em.kind == "carrier":
                        base += lin
                    elif lin.max() > 1e-4:
                        nz = np.nonzero(lin > 1e-6)[0]
                        start, stop = int(nz[0]), int(nz[-1]) + 1
                        parts.append({"emitter_id": em.id, "authorized": False, "start": start, "lin": b64f32(lin[start:stop])})
                sat_id, tp_id = mon.id.split(":") if ":" in mon.id else (ch.watched_satellite_ids[0], "")
                windows.append({
                    "channel_id": ch.id, "monitor_id": mon.id, "site_id": site.id, "sat_id": sat_id, "transponder_id": tp_id,
                    "center_hz": mon.center_hz, "span_hz": mon.span_hz, "nfft": nfft, "navg": mon.navg, "frame_rate_hz": mon.frame_rate_hz,
                    "base_lin": b64f32(base), "parts": parts,
                })
    return windows


def build_geometry(t_s: float) -> dict[str, Any]:
    log("geometry: sub-points, footprints, look angles")
    sub_series = []
    for k in range(96):
        tk = t_s + k * 900.0
        pts = []
        for sat in cfg.satellites:
            lat, lon, alt = subsatellite_point(ORBITS[sat.id], tk)
            pts.append({"sat_id": sat.id, "lat_deg": float(lat), "lon_deg": float(lon), "alt_m": float(alt)})
        sub_series.append({"offset_s": k * 900, "subpoints": check(list[S.Subpoint], pts)})
    footprints = {}
    for el in (0.0, 5.0, 10.0, 20.0):
        fps = []
        for sat in cfg.satellites:
            ring = footprint(ORBITS[sat.id], t_s, el, 181)
            fps.append({"sat_id": sat.id, "min_elevation_deg": el, "ring": [[round(lo, 4), round(la, 4)] for la, lo in ring]})
        footprints[f"{el:g}"] = check(list[S.Footprint], fps)
    looks = []
    for site in cfg.sites:
        xg = SITES_XYZ[site.id]
        for ch in site.channels:
            bore = cfg.boresight_ecef(ch.id, t_s)
            for sat in cfg.satellites:
                r = orbit_position(ORBITS[sat.id], t_s)
                az, el, rng = look_angles(xg, r)
                los = (r - xg) / np.linalg.norm(r - xg)
                off = float(angle_between_deg(bore, los))
                freq = sat.transponders[0].downlink_center_hz
                looks.append({
                    "site_id": site.id, "channel_id": ch.id, "sat_id": sat.id,
                    "az_deg": float(az) % 360.0, "el_deg": float(el), "range_km": float(rng) / 1000.0, "off_axis_deg": off,
                    "gain_dbi": float(antennas.gain_dbi(ch.antenna, off, freq)), "peak_gain_dbi": float(antennas.peak_gain_dbi(ch.antenna, freq)),
                    "freq_hz": freq, "visible": bool(el > 0), "watched": sat.id in ch.watched_satellite_ids,
                    "electrically_small": bool(ch.antenna.type == "parabolic" and antennas.electrically_small(ch.antenna, freq)),
                })
    return {"subpoints": sub_series, "footprints": footprints, "look_angles": check(list[S.LookAngle], looks)}


def link_budget_rows(em: Emitter, t_s: float) -> list[dict[str, Any]]:
    rows = []
    for sat in cfg.satellites:
        for tp in sat.transponders:
            if not tp.relays(em.freq):
                continue
            for site in cfg.sites:
                for ch in site.channels:
                    pb = path_budget(cfg, em.model, em.freq, em.xyz, sat.id, tp.id, ch.id, t_s)
                    if pb is None:
                        continue
                    bw = max(bandwidth_of(em), 1.0)
                    snr = float(pb.snr_db(bw))
                    rows.append({
                        "sat_id": sat.id, "transponder_id": tp.id, "site_id": site.id, "channel_id": ch.id,
                        "f_down_hz": em.freq + tp.translation_hz, "uplink_elevation_deg": float(pb.uplink.elevation_deg),
                        "eirp_toward_sat_dbw": float(pb.uplink.eirp_toward_sat_dbw), "pfd_dbw_m2": float(pb.uplink.pfd_dbw_m2),
                        "cn0_up_dbhz": float(pb.uplink.cn0_up_dbhz), "transponder_eirp_dbw": float(pb.transponder_eirp_dbw),
                        "downlink_elevation_deg": float(pb.downlink.elevation_deg), "off_axis_deg": float(pb.downlink.off_axis_deg),
                        "c_rx_dbw": float(pb.downlink.c_rx_dbw), "cn0_total_dbhz": float(pb.cn0_total_dbhz), "snr_db": snr,
                        "detectable": snr >= PROC.detection.threshold_db,
                    })
    return rows


# --------------------------------------------------------------------------- accuracy


def default_accuracy_request(config: SystemConfig) -> dict[str, Any]:
    """The console's starting accuracy request for a configuration, the rule of
    ``ui/src/domain/plan.ts`` (``accuracyDefaults``): the uplink band most satellites relay
    in common, the site whose channels watch the most of them (up to two pairs sharing its
    last channel; else a pair across sites), the band center and 0.32 of its width
    (1 kHz to 5 MHz). The mock must hold a grid for exactly this request, so the
    console's first "Compute accuracy map" answers."""
    ends = []
    for site in config.sites:
        for ch in site.channels:
            sats = list(ch.watched_satellite_ids)
            for sat in sats:
                ends.append({"value": f"{ch.id}@{sat}" if len(sats) > 1 else ch.id, "channel": ch.id, "sat": sat, "site": site.id})
    best = None
    for sat in config.satellites:
        for tp in sat.transponders:
            lo, hi = tp.uplink_center_hz - tp.bandwidth_hz / 2, tp.uplink_center_hz + tp.bandwidth_hz / 2
            group = [sat.id]
            for other in config.satellites:
                if other.id == sat.id:
                    continue
                overlap = None
                for t in other.transponders:
                    lo2, hi2 = max(lo, t.uplink_center_hz - t.bandwidth_hz / 2), min(hi, t.uplink_center_hz + t.bandwidth_hz / 2)
                    if hi2 > lo2 and (overlap is None or hi2 - lo2 > overlap[1] - overlap[0]):
                        overlap = (lo2, hi2)
                if overlap:
                    group.append(other.id)
                    lo, hi = overlap
            if len(group) < 2:
                continue
            candidates = []
            for site in config.sites:
                at_site = [e for e in ends if e["site"] == site.id and e["sat"] in group]
                chosen: dict[str, dict[str, str]] = {}
                for e in at_site:
                    prev = chosen.get(e["sat"])
                    if prev is None or ("@" in prev["value"] and "@" not in e["value"]):
                        chosen[e["sat"]] = e
                here = [e for e in at_site if chosen.get(e["sat"]) is e]
                if not here:
                    continue
                last = here[-1]
                pairs = [(e["value"], last["value"]) for e in here[:-1] if e["channel"] != last["channel"]][:2]
                if pairs:
                    candidates.append((pairs, len(here)))
            if not candidates:
                a = next((e for e in ends if e["sat"] in group), None)
                b = next((e for e in ends if a and e["sat"] in group and e["sat"] != a["sat"] and e["channel"] != a["channel"]), None)
                if a and b:
                    candidates.append(([(a["value"], b["value"])], 2))
            for pairs, n_sats in candidates:
                if best is None or len(pairs) > len(best[0]) or (len(pairs) == len(best[0]) and n_sats > best[1]):
                    best = (pairs, n_sats, lo, hi)
    if best is None:
        raise ValueError("no pair of channels on satellites relaying a common frequency")
    pairs, _, lo, hi = best
    return {"pairs": [list(p) for p in pairs], "f_e_hz": float(round((lo + hi) / 2)),
            "bandwidth_hz": float(min(5e6, max(1e3, round((hi - lo) * 0.32))))}


ACC_STEP = 3.0
ACC_EIRP_DBW = 25.0


def accuracy_grid(pairs: list[list[str]], kinds: tuple[str, ...], reference: str | None, f_e_hz: float, bandwidth_hz: float, t_ns: int) -> dict[str, Any]:
    """``POST /analysis/accuracy`` as the server answers it (``compute_accuracy`` over
    ``core.accuracy.accuracy_map``, banded the same way)."""
    request = S.AccuracyRequest.model_validate({
        "pairs": pairs, "emitter": {"eirp_dbw": ACC_EIRP_DBW, "bandwidth_hz": bandwidth_hz, "waveform": "noise"},
        "f_e_hz": f_e_hz, "step_deg": ACC_STEP, "kinds": list(kinds), "reference_carrier_id": reference,
    })
    return compute_accuracy(accuracy_map, cfg, request, t_ns).model_dump(mode="json")


def build_accuracy() -> None:
    default = default_accuracy_request(cfg)
    log(f"accuracy: the console's default request is {default}")
    variants = []
    pair_sets = {"default": default["pairs"], "naha2": [["naha-pac", "naha-172e"], ["naha-io", "naha-172e"]], "naha1": [["naha-pac", "naha-172e"]]}
    for pname, pairs in pair_sets.items():
        for kinds in (("tdoa", "fdoa"), ("tdoa",)):
            for ref in (None, "ref-guam"):
                if pname == "naha1" and kinds == ("tdoa",):
                    continue  # one TDOA row: a line, never a position
                t0 = time.time()
                grid = check(S.AccuracyGrid, accuracy_grid(pairs, kinds, ref, default["f_e_hz"], default["bandwidth_hz"], T0_NS))
                n_ok = sum(v is not None for row in grid["semi_major_95_m"] for v in row)
                log(f"accuracy {pname} {kinds} ref={ref}: {n_ok} cells in {time.time() - t0:.0f} s")
                variants.append({"pairs": pairs, "kinds": list(kinds), "reference_carrier_id": ref,
                                 "emitter": {"eirp_dbw": ACC_EIRP_DBW, "bandwidth_hz": default["bandwidth_hz"], "waveform": "noise"},
                                 "f_e_hz": default["f_e_hz"], "step_deg": ACC_STEP, "grid": grid})
    # R7: each cell calibrated against the reference nearest it (the console's "nearest to each cell")
    t0 = time.time()
    grid = check(S.AccuracyGrid, accuracy_grid(default["pairs"], ("tdoa", "fdoa"), "nearest", default["f_e_hz"], default["bandwidth_hz"], T0_NS))
    log(f"accuracy default ('tdoa', 'fdoa') ref=nearest: {sum(v is not None for row in grid['semi_major_95_m'] for v in row)} cells in {time.time() - t0:.0f} s")
    variants.append({"pairs": default["pairs"], "kinds": ["tdoa", "fdoa"], "reference_carrier_id": "nearest",
                     "emitter": {"eirp_dbw": ACC_EIRP_DBW, "bandwidth_hz": default["bandwidth_hz"], "waveform": "noise"},
                     "f_e_hz": default["f_e_hz"], "step_deg": ACC_STEP, "grid": grid})
    write("accuracy.json", {"variants": variants})


# --------------------------------------------------------------------------- main assembly


FIX_LIMIT_M = server_events.fix_semi_major_limit_m(PROC.region)


def solution_out(sol: Any) -> dict[str, Any]:
    """SolutionOut as the server publishes a solve: ``Solution.as_dict()`` with the fit
    assessment of ``processing.solver.max_reduced_chi2`` applied (fit_consistent,
    fit_p_value, ellipse_scale; an inconsistent fit enlarged) and the fix gate (is_fix,
    not_fix_reason; ``events.fix_semi_major_limit_m`` of the region), the same helper
    the server's EventBook uses."""
    return server_events._assessed(sol.as_dict(), PROC.solver.max_reduced_chi2, FIX_LIMIT_M)


def history_row(t_ns: int, sol: Any, set_aside: bool = False) -> dict[str, Any]:
    """SolutionHistoryRow of a converged solve, assessed as the server assesses it."""
    out = solution_out(sol)
    return {"t_ns": ns_str(t_ns), "semi_major_m": out["ellipse"]["semi_major_m"], "semi_minor_m": out["ellipse"]["semi_minor_m"],
            "n_measurements": out["n_measurements"], "fit_consistent": out["fit_consistent"], "is_fix": out["is_fix"], "set_aside": set_aside}


def solution_summary(sol: dict[str, Any] | None) -> dict[str, Any] | None:
    """SolutionSummary of a published SolutionOut dict (as EventBook.summary)."""
    if sol is None:
        return None
    return {"lat_deg": sol["lat_deg"], "lon_deg": sol["lon_deg"], "semi_major_m": sol["ellipse"]["semi_major_m"], "semi_minor_m": sol["ellipse"]["semi_minor_m"],
            "azimuth_deg": sol["ellipse"]["azimuth_deg"], "cep50_m": sol["cep50_m"], "ambiguous": len(sol["ambiguities"]) > 0,
            "ambiguities": [{"lat_deg": a["lat_deg"], "lon_deg": a["lon_deg"], "semi_major_m": a["ellipse"]["semi_major_m"],
                             "semi_minor_m": a["ellipse"]["semi_minor_m"], "azimuth_deg": a["ellipse"]["azimuth_deg"]} for a in sol["ambiguities"]],
            "fit_consistent": sol.get("fit_consistent", True), "is_fix": sol.get("is_fix", True)}


def record_of(ev: dict[str, Any]) -> Any:
    """A server EventRecord holding what the advisory reads, from an Event document."""
    state = ev["state"]
    tasking_doc = ev.get("tasking")
    return server_events.EventRecord(
        id=ev["id"], seq=int(ev["id"].split("-")[1]), created_ns=int(ev["first_seen_ns"]), space="up",
        lo_hz=ev["uplink_lo_hz"], hi_hz=ev["uplink_hi_hz"], first_seen_ns=int(ev["first_seen_ns"]), last_seen_ns=int(ev["last_seen_ns"]),
        settle_from_ns=int(ev["first_seen_ns"]), progress=state if state not in ("lost", "closed") else "located",
        lost=state == "lost", closed=state == "closed", verdict=ev["verdict"],
        verdict_basis="two_sites" if ev["verdict"] != "undetermined" else "single_site", verdict_reason=ev["verdict_reason"],
        evidence=[S.EvidenceRow.model_validate(r) for r in ev["evidence"]],
        transponders=[S.EventTransponder.model_validate(t) for t in ev["transponders"]],
        satellite_ids=list(ev["satellite_ids"]),
        classification=None if ev["classification"] is None else S.ClassificationOut.model_validate(ev["classification"]),
        solution_out=None if ev["solution"] is None else S.SolutionOut.model_validate(ev["solution"]),
        tasking=None if tasking_doc is None else S.TaskingStatus.model_validate(tasking_doc),
        onset_ns=None if ev.get("onset_ns") is None else int(ev["onset_ns"]),
        resumed_ns=None if ev.get("resumed_ns") is None else int(ev["resumed_ns"]),
        break_s=ev.get("break_s"), recheck=ev.get("recheck"),
        not_visible_satellite_ids=list(ev.get("not_visible_satellite_ids") or []),
        width_lower_bound=bool(ev.get("width_lower_bound", False)),
    )


def downlink_ranges(f_lo_up: float, f_hi_up: float) -> list[dict[str, Any]]:
    out = []
    for sat, tp in cfg.transponders_covering(f_lo_up, f_hi_up):
        out.append({"sat_id": sat.id, "transponder_id": tp.id, "downlink_lo_hz": f_lo_up + tp.translation_hz, "downlink_hi_hz": f_hi_up + tp.translation_hz})
    return out


def build_all(skip_accuracy: bool, only: set[str] | None) -> None:
    t_s = T0_NS * 1e-9
    want = lambda name: only is None or name in only

    meta = check(S.Meta, {"version": "0.1.0 (mock)", "marking": {"level": "UNCLASSIFIED", "line": "UNCLASSIFIED"}, "simulation": True,
                          "server_t_ns": ns_str(T0_NS), "config_name": cfg.name, "api_token_required": False})
    write("meta.json", meta, 2)

    if want("config"):
        derived = [ch.id for site in cfg.sites for ch in site.channels if ch.monitors_derived]
        env = check(S.ConfigEnvelope, {"version": CONFIG_VERSION, "config": {**cfg.model_dump(mode="json", exclude_none=True), "version": CONFIG_VERSION},
                                       "warnings": cfg.warnings(t_s), "derived_monitors": derived})
        write("config.json", env, 2)
        write("config_schema.json", config_json_schema())
        # realistic field errors straight from the models, for the mock's validator messages
        def mutate(name: str) -> dict[str, Any]:
            bad = cfg.export_dict()
            if name == "field":
                bad["sites"][2]["channels"][0]["antenna"]["diameter_m"] = -0.72
                bad["sites"][0]["lat_deg"] = 95.0
                bad["satellites"][0]["transponders"][1]["id"] = "shared 2"
            elif name == "xref":
                bad["sites"][1]["channels"][0]["pointing"] = {"satellite_id": "muos-atl"}
            elif name == "carrier":
                bad["carriers"][0]["uplink_center_hz"] = 301.2e6
            elif name == "time_zone":
                bad["sites"][0]["time_zone"] = "Pacific/Atlantis"
                bad["sites"][1]["time_zone"] = "Asia Tokyo"
            return bad

        samples = {}
        for name in ("field", "xref", "carrier", "time_zone"):
            try:
                SystemConfig.model_validate(mutate(name))
                raise AssertionError(f"expected validation errors for {name}")
            except ValueError as exc:
                samples[name] = check(S.ValidateResult, {"ok": False, "errors": field_errors(exc), "warnings": []})
        write("validate_sample.json", samples, 2)

    if want("geometry"):
        geo = build_geometry(t_s)
        write("geometry.json", geo)

    if want("spectrum"):
        write("spectrum.json", build_spectrum(t_s))

    if want("events"):
        e1 = build_events()
        e2 = build_event2()
        e0 = build_event_mixed()
        sol1, sol2, sol0 = e1["solution"], e2["solution"], e0["solution"]
        j1 = EMITTERS["jammer-psea"]
        j2 = EMITTERS["psk-honshu"]
        lo1, hi1 = j1.freq - 4000.0, j1.freq + 4000.0
        lo2, hi2 = j2.freq - bandwidth_of(j2) / 2, j2.freq + bandwidth_of(j2) / 2
        first1 = e1["pre"][0] - 18_000_000_000
        first2 = e2["epochs"][0] - 15_000_000_000
        # evidence from the expected detections
        def seen_map(em: Emitter, sats: list[str]) -> dict[str, tuple[float, float]]:
            out = {}
            for site in cfg.sites:
                if site.id == "hilo":
                    continue  # offline
                for ch in site.channels:
                    if ch.watched_satellite_ids[0] in sats:
                        st = detection_stats(em, ch.id, t_s)
                        if st is not None:
                            out[ch.id] = st
            return out

        sats1 = [s.id for s, _ in cfg.transponders_covering(lo1, hi1)]
        seen1 = seen_map(j1, sats1)
        prim1 = max(seen1, key=lambda c: seen1[c][0])
        ev1_evidence = evidence_for(sats1, {s: (lo1 + transponder_for(s, j1.freq).translation_hz, hi1 + transponder_for(s, j1.freq).translation_hz) for s in sats1}, seen1, {"hilo"}, t_s, prim1)
        for r in ev1_evidence:
            if r["state"] == "seen":
                r["last_seen_ns"] = ns_str(T0_NS - 500_000_000)
            elif r["state"] == "stale":
                r["last_seen_ns"] = ns_str(T0_NS - 1_420_000_000_000)
        sats2 = [s.id for s, _ in cfg.transponders_covering(lo2, hi2)]
        seen2 = seen_map(j2, sats2)
        prim2 = max(seen2, key=lambda c: seen2[c][0]) if seen2 else None
        ev2_evidence = evidence_for(sats2, {s: (lo2 + transponder_for(s, j2.freq).translation_hz, hi2 + transponder_for(s, j2.freq).translation_hz) for s in sats2}, seen2, {"hilo"}, t_s, prim2)
        for r in ev2_evidence:
            if r["state"] == "seen":
                r["last_seen_ns"] = ns_str(T0_NS - 1_000_000_000)

        reasons = {("detected", "undetermined"): "first confirmed detection", ("characterizing", None): "first capture requested",
                   ("detected", "local"): "judged local: nothing to characterize or locate by satellite",
                   ("locating", None): "first correlation accepted", ("located", None): "converged solution", ("lost", None): "all detections released",
                   ("closed", None): "closed by operator"}

        def transitions(first: int, steps: list[tuple[int, str, str]]) -> list[dict[str, Any]]:
            out, prev = [], None
            for dt, st, vd in steps:
                why = reasons.get((st, vd)) or reasons.get((st, None))
                if prev is not None and prev[0] == st and prev[1] != vd:
                    why = f"verdict settled after {PROC.detection.verdict_settle_s:g} s"
                out.append({"t_ns": ns_str(first + dt), "state": st, "verdict": vd, "reason": why})
                prev = (st, vd)
            return out

        sec = 1_000_000_000
        ev1 = {
            "id": "EV-0042", "state": "located", "verdict": "uplink",
            "first_seen_ns": ns_str(first1), "last_seen_ns": ns_str(T0_NS - 500_000_000),
            "uplink_lo_hz": lo1, "uplink_hi_hz": hi1, "satellite_ids": sats1, "transponders": downlink_ranges(lo1, hi1),
            "classification": {"label": "noise", "confidence": 0.91, "features": {"occupied_bw_hz": 7920.0, "spectral_flatness": 0.94, "envelope_kurtosis": 2.02, "sweep_linearity": 0.03, "symbol_line_db": 0.4, "duty_cycle": 1.0}},
            "advisory": {
                "headline": "Uplink interference on shared-1: change channel, not terminal",
                "detail": ("The signal comes down from all three satellites and is seen at sites more than 7,000 km apart, so it enters on the satellite uplink. "
                           "Every user of shared-1 on these satellites is affected; moving or shielding a ground terminal will not help."),
                "actions": ["Move traffic on shared-1 to shared-2, which is clear on all three satellites",
                            "Pass the estimated position and ellipse to the spectrum manager",
                            "Keep captures running; the ellipse shrinks with every 5 s capture"],
            },
            "evidence": ev1_evidence,
            "transitions": transitions(first1, [(0, "detected", "undetermined"), (6 * sec, "detected", "uplink"), (7 * sec, "characterizing", "uplink"), (18 * sec, "locating", "uplink"), (23 * sec, "located", "uplink")])
            + [{"t_ns": ns_str(LOST_NS), "state": "lost", "verdict": "uplink", "reason": "all detections released"},
               {"t_ns": ns_str(RESUMED_NS), "state": "characterizing", "verdict": "undetermined",
                "reason": f"detected again after a break of {server_events._duration_text((RESUMED_NS - LOST_NS) / 1e9)}; the earlier position and measurements are set aside until it is located again"},
               {"t_ns": ns_str(RESUMED_NS + 6 * sec), "state": "characterizing", "verdict": "uplink", "reason": f"verdict settled after {PROC.detection.verdict_settle_s:g} s"},
               {"t_ns": ns_str(e1["post"][1]), "state": "locating", "verdict": "uplink", "reason": "first correlation accepted"},
               {"t_ns": e1["history"][-2]["t_ns"] if len(e1["history"]) > 1 else e1["history"][-1]["t_ns"], "state": "located", "verdict": "uplink", "reason": "converged solution"}],
            "onset_ns": ns_str(RESUMED_NS - 500_000_000), "resumed_ns": ns_str(RESUMED_NS), "break_s": (RESUMED_NS - LOST_NS) / 1e9,
            "solution": solution_out(sol1), "solution_t_ns": e1["history"][-1]["t_ns"], "solution_history": e1["history"],
            "measurements": measurement_rows(e1["rows"], sol1), "caf": e1["caf"],
            "truth": {"emitter_id": "jammer-psea", "lat_deg": j1.lat, "lon_deg": j1.lon, "miss_m": e1["miss"], "inside_ellipse": inside_ellipse(sol1, j1.lat, j1.lon)},
        }
        ev2 = {
            "id": "EV-0043", "state": "located" if sol2 is not None else "locating", "verdict": "uplink",
            "first_seen_ns": ns_str(first2), "last_seen_ns": ns_str(T0_NS - 1_000_000_000),
            "uplink_lo_hz": lo2, "uplink_hi_hz": hi2, "satellite_ids": sats2, "transponders": downlink_ranges(lo2, hi2),
            "classification": {"label": "modulated", "confidence": 0.88, "features": {"occupied_bw_hz": 3930.0, "spectral_flatness": 0.58, "envelope_kurtosis": 1.64, "sweep_linearity": 0.01, "symbol_line_db": 19.5, "duty_cycle": 1.0}},
            "advisory": {
                "headline": "Uplink interference on shared-2: change channel, not terminal",
                "detail": ("A QPSK signal is relayed by all three satellites and seen from Hawaii to Manila, so it enters on the uplink. "
                           "Two captures so far: the lines of position cross twice, and both crossings fit the measurements equally well."),
                "actions": ["Move traffic on shared-2 to another clear channel",
                            "Treat the position as one of two until more captures or a third pair separate them",
                            "Do not act on the position alone while the ambiguity is unresolved"],
            },
            "evidence": ev2_evidence,
            "transitions": transitions(first2, [(0, "detected", "undetermined"), (5 * sec, "detected", "uplink"), (6 * sec, "characterizing", "uplink"), (15 * sec, "locating", "uplink")] + ([(26 * sec, "located", "uplink")] if sol2 is not None else [])),
            "solution": None if sol2 is None else solution_out(sol2), "solution_t_ns": e2["history"][-1]["t_ns"] if e2["history"] else None,
            "solution_history": e2["history"], "measurements": measurement_rows(e2["rows"], sol2), "caf": e2["caf"],
            "truth": {"emitter_id": "psk-honshu", "lat_deg": j2.lat, "lon_deg": j2.lon,
                      "miss_m": None if sol2 is None else float(geodesic_distance_m(sol2.lat_deg, sol2.lon_deg, j2.lat, j2.lon)),
                      "inside_ellipse": None if sol2 is None else inside_ellipse(sol2, j2.lat, j2.lon)},
        }
        # two transmitters on shared-2 detected alternately: an inconsistent fit (UX-1)
        mixed = e0["truth"]["mixed-a"]
        lo0, hi0 = mixed.freq - 1000.0, mixed.freq + 1000.0
        sats0 = [s.id for s, _ in cfg.transponders_covering(lo0, hi0)]
        seen0 = seen_map(mixed, sats0)
        prim0 = max(seen0, key=lambda c: seen0[c][0]) if seen0 else None
        ev0_evidence = evidence_for(sats0, {s: (lo0 + transponder_for(s, mixed.freq).translation_hz, hi0 + transponder_for(s, mixed.freq).translation_hz) for s in sats0}, seen0, {"hilo"}, t_s, prim0)
        for r in ev0_evidence:
            if r["state"] == "seen":
                r["last_seen_ns"] = ns_str(T0_NS - 700_000_000)
        first0 = e0["epochs"][0] - 14 * sec
        meas0 = measurement_rows(e0["rows"], sol0)
        ev0 = {
            "id": "EV-0040", "state": "locating", "verdict": "uplink",
            "first_seen_ns": ns_str(first0), "last_seen_ns": ns_str(T0_NS - 700_000_000),
            "uplink_lo_hz": lo0, "uplink_hi_hz": hi0, "satellite_ids": sats0, "transponders": downlink_ranges(lo0, hi0),
            "classification": {"label": "noise", "confidence": 0.86, "features": {"occupied_bw_hz": 1980.0, "spectral_flatness": 0.93, "envelope_kurtosis": 2.0, "sweep_linearity": 0.02, "symbol_line_db": 0.3, "duty_cycle": 1.0}},
            "advisory": {"headline": "-", "detail": "-", "actions": []},
            "evidence": ev0_evidence,
            "transitions": transitions(first0, [(0, "detected", "undetermined"), (6 * sec, "detected", "uplink"), (7 * sec, "characterizing", "uplink"), (14 * sec, "locating", "uplink")]),
            "solution": solution_out(sol0), "solution_t_ns": e0["history"][-1]["t_ns"], "solution_history": e0["history"],
            "measurements": meas0, "caf": e0["caf"], "truth": None,
        }
        # local event at Busan on solo-pac (direct path only)
        l1 = EMITTERS["local-busan"]
        st = detection_stats(l1, "busan-pac", t_s)
        f_up = l1.freq - transponder_for("muos-pac", 301.5e6).translation_hz
        bw3 = bandwidth_of(l1)
        lo3, hi3 = f_up - bw3 / 2, f_up + bw3 / 2
        ev3_evidence = evidence_for(["muos-pac"], {"muos-pac": (l1.freq - bw3 / 2, l1.freq + bw3 / 2)}, {"busan-pac": st} if st else {}, {"hilo"}, t_s, "busan-pac")
        for r in ev3_evidence:
            if r["state"] == "seen":
                r["last_seen_ns"] = ns_str(T0_NS - 500_000_000)
        first3 = T0_NS - 312 * sec
        ev3 = {
            # judged local: back to detected, and no satellite relays a local signal (as the server)
            "id": "EV-0041", "state": "detected", "verdict": "local",
            "first_seen_ns": ns_str(first3), "last_seen_ns": ns_str(T0_NS - 500_000_000), "uplink_lo_hz": lo3, "uplink_hi_hz": hi3,
            "satellite_ids": [], "transponders": [],
            "classification": {"label": "modulated", "confidence": 0.84, "features": {"occupied_bw_hz": 3180.0, "spectral_flatness": 0.61, "envelope_kurtosis": 1.71, "sweep_linearity": 0.01, "symbol_line_db": 17.2, "duty_cycle": 1.0}},
            "advisory": {
                "headline": "Local interference at Busan on solo-pac: find or shield the source near the site",
                "detail": ("Only Busan sees this signal. Seoul, Tokyo, Naha and Manila watch the same downlink with enough margin to see it and do not, "
                           "so it is not on the satellite uplink: it enters at the Busan antenna or receiver."),
                "actions": ["Search for a transmitter near the Busan antenna on 260.502 5 MHz", "Check the Busan receive chain for a fault or intermodulation",
                            "Shield or filter the Busan antenna; other sites and users are not affected"],
            },
            "evidence": ev3_evidence,
            "transitions": transitions(first3, [(0, "detected", "undetermined"), (2 * sec, "characterizing", "undetermined"),
                                                (6 * sec, "characterizing", "local"), (6 * sec, "detected", "local")]),
            "solution": None, "solution_t_ns": None, "solution_history": [], "measurements": [], "caf": None,
            "truth": {"emitter_id": "local-busan", "lat_deg": l1.lat, "lon_deg": l1.lon, "miss_m": None, "inside_ellipse": None},
        }
        # undetermined: weak carrier on solo-172e seen at Honolulu only, Hilo offline
        first4 = T0_NS - 95 * sec
        lo4, hi4 = 301.7562e6, 301.7578e6
        ev4_evidence = []
        for site in cfg.sites:
            for ch in site.channels:
                if ch.watched_satellite_ids[0] != "uhf-172e":
                    continue
                state = "seen" if ch.id == "honolulu-172e" else ("stale" if site.id == "hilo" else "not_capable")
                ev4_evidence.append({"channel_id": ch.id, "site_id": site.id, "sat_id": "uhf-172e", "state": state,
                                     "snr_db": 7.4 if state == "seen" else None, "cn0_dbhz": 39.6 if state == "seen" else None,
                                     "predicted_margin_db": {"honolulu-172e": None, "hilo-172e": 2.1}.get(ch.id, round(-3.0 - 0.7 * len(ch.id) % 5, 1)),
                                     "last_seen_ns": ns_str(T0_NS - 2 * sec) if state == "seen" else (ns_str(T0_NS - 1_420 * sec) if state == "stale" else None)})
        ev4 = {
            "id": "EV-0044", "state": "detected", "verdict": "undetermined",
            "first_seen_ns": ns_str(first4), "last_seen_ns": ns_str(T0_NS - 2 * sec), "uplink_lo_hz": lo4, "uplink_hi_hz": hi4,
            "satellite_ids": ["uhf-172e"], "transponders": downlink_ranges(lo4, hi4), "classification": None,
            "advisory": {
                "headline": "Not yet decided: seen at Honolulu only",
                "detail": ("Hilo is the only other site that could see this signal and its node is offline, so nothing tells uplink from local yet. "
                           "The other sites on 172 E are too far down the beam to see a signal this weak."),
                "actions": ["Restore the Hilo node; the verdict follows within 5 s of its first spectrum frame", "Watch solo-172e at Honolulu meanwhile"],
            },
            "evidence": ev4_evidence,
            "transitions": transitions(first4, [(0, "detected", "undetermined")]),
            "solution": None, "solution_t_ns": None, "solution_history": [], "measurements": [], "caf": None, "truth": None,
        }
        # an earlier, lost event and a closed one
        ev5 = {**ev4, "id": "EV-0039", "state": "lost", "verdict": "uplink", "first_seen_ns": ns_str(T0_NS - 5400 * sec), "last_seen_ns": ns_str(T0_NS - 5100 * sec),
               "uplink_lo_hz": 301.9991e6, "uplink_hi_hz": 302.0009e6, "satellite_ids": ["muos-io"], "transponders": downlink_ranges(301.9991e6, 302.0009e6),
               "classification": {"label": "pulsed", "confidence": 0.72, "features": {"occupied_bw_hz": 1650.0, "spectral_flatness": 0.4, "envelope_kurtosis": 4.8, "sweep_linearity": 0.0, "symbol_line_db": 0.0, "duty_cycle": 0.25}},
               "advisory": {"headline": "Uplink interference on solo-io, no longer seen", "detail": "Seen at Manila, Naha and Tokyo for five minutes; only one satellite relays solo-io, so it could not be located.", "actions": ["Note the time in the shift log"]},
               "evidence": [], "transitions": transitions(T0_NS - 5400 * sec, [(0, "detected", "undetermined"), (6 * sec, "detected", "uplink"), (300 * sec, "lost", "uplink")]), "truth": None}
        ev6 = {**ev5, "id": "EV-0038", "state": "closed", "verdict": "local", "first_seen_ns": ns_str(T0_NS - 9000 * sec), "last_seen_ns": ns_str(T0_NS - 8700 * sec),
               "uplink_lo_hz": 301.4998e6, "uplink_hi_hz": 301.5002e6, "satellite_ids": [], "transponders": [],
               "classification": {"label": "cw", "confidence": 0.95, "features": {"occupied_bw_hz": 10.0}},
               "advisory": {"headline": "Local carrier at Naha, closed by operator", "detail": "A test oscillator in the Naha equipment room; removed.", "actions": []},
               "transitions": transitions(T0_NS - 9000 * sec, [(0, "detected", "undetermined"), (6 * sec, "detected", "local"), (240 * sec, "lost", "local"), (300 * sec, "closed", "local")])}
        verdict_reasons = {
            "EV-0042": "Seen at sites 7,400 km apart (Honolulu and Manila) through three satellites.",
            "EV-0043": "Seen at sites 7,400 km apart through three satellites.",
            "EV-0041": "Seen only at Busan; Seoul, 325 km away, watches the same downlink with 14 dB of margin and does not see it.",
            "EV-0044": "Seen at one site only, and the one other capable site (Hilo) is offline.",
            "EV-0039": "Seen at Manila and Tokyo, 3,000 km apart.",
            "EV-0038": "Seen only at Naha; Manila and Tokyo were capable and did not see it.",
        }
        def tasking(meas: list[dict[str, Any]], state: str = "waiting", message: str | None = None,
                    solve_reason: str | None = None, rounds: int | None = None, channels: list[str] | None = None) -> dict[str, Any]:
            """Event.tasking as server/pipeline/tasking.py reports it after the newest round of these measurements."""
            times = sorted({int(m["t_ns"]) for m in meas})
            chans: list[str] = list(channels or [])
            if times and not chans:
                for m in meas:
                    if int(m["t_ns"]) == times[-1]:
                        for end in ("a", "b"):
                            if m["pair"][end]["channel_id"] not in chans:
                                chans.append(m["pair"][end]["channel_id"])
            last = times[-1] - int(PROC.capture.lead_s * sec) if times else None
            return {"state": state, "rounds": rounds if rounds is not None else len(times),
                    "last_round_ns": ns_str(last) if last is not None else None,
                    "next_round_ns": ns_str(last + int(PROC.capture.interval_s * sec)) if last is not None and state == "waiting" else None,
                    "channels": chans, "message": message, "solve_reason": solve_reason}

        ev1["tasking"] = tasking(ev1["measurements"])
        ev2["tasking"] = tasking(ev2["measurements"])
        ev3["tasking"] = None  # local: tasking only works on uplink and undetermined events
        ev4["tasking"] = {"state": "idle", "rounds": 1, "last_round_ns": ns_str(first4 + 7 * sec), "next_round_ns": None, "channels": ["honolulu-172e"],
                          "message": "No second satellite with a usable channel relays this frequency, so the emitter cannot be located.", "solve_reason": None,
                          "unlocatable_reason": UNLOCATABLE_ONE_SATELLITE}
        rchi = ev0["solution"]["reduced_chi2"]
        ev0["tasking"] = tasking(ev0["measurements"], message=(
            f"The measurements do not fit one stationary transmitter (reduced chi-square {_chi2_text(rchi)}, limit {PROC.solver.max_reduced_chi2:g}): "
            "more than one transmitter may share the frequency, the transmitter may be moving, or a site clock or satellite position may be wrong. "
            "The position is shown with its ellipse enlarged and is not a fix."))
        ev5["tasking"] = None
        ev6["tasking"] = None
        verdict_reasons["EV-0040"] = "Seen at sites 7,400 km apart through three satellites."
        all_events = (ev0, ev1, ev2, ev3, ev4, ev5, ev6)
        for e in all_events:
            e["verdict_reason"] = verdict_reasons[e["id"]]
            e["simulated"] = True
            e.setdefault("merged_into", None)
            # what is received at the sites that see it (Event.received_*, seen_site_ids)
            seen_rows = [r for r in e["evidence"] if r["state"] == "seen" and r.get("downlink_lo_hz") is not None]
            e["received_lo_hz"] = min((r["downlink_lo_hz"] for r in seen_rows), default=None)
            e["received_hi_hz"] = max((r["downlink_hi_hz"] for r in seen_rows), default=None)
            e["seen_site_ids"] = list(dict.fromkeys(r["site_id"] for r in e["evidence"] if r["state"] == "seen"))
            e.setdefault("onset_ns", e["first_seen_ns"])
            e.setdefault("not_visible_satellite_ids", [])
            e.setdefault("width_lower_bound", False)
            # the server's own advice for this record (wording, conventions, report actions)
            e["advisory"] = advise(record_of(check(S.Event, e)), cfg, simulated=False).model_dump(mode="json")
        events = [check(S.Event, e) for e in all_events]
        write("events.json", events)
        write("lop.json", {"EV-0042": e1["lop"], "EV-0043": e2["lop"], "EV-0040": e0["lop"]})
        summaries = [check(S.EventSummary, {"id": e["id"], "state": e["state"], "verdict": e["verdict"], "first_seen_ns": e["first_seen_ns"], "last_seen_ns": e["last_seen_ns"],
                                            "uplink_lo_hz": e["uplink_lo_hz"], "uplink_hi_hz": e["uplink_hi_hz"], "satellite_ids": e["satellite_ids"],
                                            "label": e["classification"]["label"] if e["classification"] else None, "headline": e["advisory"]["headline"],
                                            "solution": solution_summary(e["solution"]), "simulated": True, "onset_ns": e["onset_ns"],
                                            "received_lo_hz": e["received_lo_hz"], "received_hi_hz": e["received_hi_hz"], "seen_site_ids": e["seen_site_ids"],
                                            "width_lower_bound": e["width_lower_bound"]}) for e in events]
        write("event_summaries.json", summaries, 2)
        truth = check(S.SimTruth, {
            "version": 3,
            "emitters": [{"id": em.id, "name": next((s.name for s in SCN.emitters if s.id == em.id), cfg.carrier(em.id).name if em.kind == "carrier" else em.id),
                          "kind": em.kind, "lat_deg": em.lat, "lon_deg": em.lon, "freq_hz": em.freq, "eirp_dbw": em.model.eirp_dbw, "bandwidth_hz": bandwidth_of(em),
                          "waveform": em.model.waveform, "enabled": next((s.enabled for s in SCN.emitters if s.id == em.id), True),
                          "active": next((s.enabled for s in SCN.emitters if s.id == em.id), True)} for em in EMITTERS.values()],
            "events": [{"event_id": e["id"], "emitter_id": e["truth"]["emitter_id"] if e["truth"] else None, "miss_m": e["truth"]["miss_m"] if e["truth"] else None,
                        "inside_ellipse": e["truth"]["inside_ellipse"] if e["truth"] else None} for e in events if e["state"] not in ("lost", "closed")],
        })
        write("sim_truth.json", truth, 2)

    if want("sim"):
        scen = check(S.ScenarioEnvelope, {"version": 3, "scenario": SCENARIO})
        write("scenario.json", scen, 2)
        presets = {
            "philippine-sea-exercise": ("The default exercise: noise jammer in the Philippine Sea, carrier east of the Marianas, local interferer at Busan.", SCENARIO),
            "quiet": ("No interference: only the two reference carriers radiate.", {**SCENARIO, "name": "quiet", "description": "SIMULATION. Reference carriers only.", "emitters": [], "errors": {}}),
            "jammer-only": ("The Philippine Sea noise jammer alone, no truth errors.", {**SCENARIO, "name": "jammer-only", "description": "SIMULATION. One noise jammer, perfect ephemerides and clocks.", "emitters": [SCENARIO["emitters"][0]], "errors": {}}),
            "ionosphere": ("The default exercise with a 30 TECU ionosphere and a 2.3 m zenith troposphere (the solver carries both as consider terms).",
                           {**SCENARIO, "name": "ionosphere", "errors": {**SCENARIO["errors"], "iono": {"enabled": True, "vtec_tecu": 30.0}, "tropo": {"enabled": True, "zenith_m": 2.3}}}),
        }
        write("presets.json", {"list": check(list[S.Preset], [{"name": k, "description": v[0]} for k, v in presets.items()]),
                               "scenarios": {k: check(Scenario, v[1]) for k, v in presets.items()}}, 2)
        lb = {}
        for eid in ("jammer-psea", "psk-honshu", "test-chirp"):
            em = EMITTERS[eid]
            lb[eid] = check(S.LinkBudgetResponse, {"t_ns": ns_str(T0_NS), "rows": link_budget_rows(em, t_s)})
        write("link_budget.json", lb)

    if want("status"):
        nodes = []
        drivers = {"honolulu": ("sim", "gpsdo"), "hilo": ("sim", "gpsdo"), "tokyo": ("sim", "gpsdo"), "naha": ("sim", "gpsdo"),
                   "seoul": ("sim", "ntp"), "busan": ("sim", "gpsdo"), "manila": ("sim", "ntp")}
        lat_ms = {"honolulu": 41.0, "hilo": None, "tokyo": 88.0, "naha": 96.0, "seoul": 104.0, "busan": 101.0, "manila": 162.0}
        for k, site in enumerate(cfg.sites):
            online = site.id != "hilo"
            drv, src = drivers[site.id]
            clock = None if not online else {
                "source": src, "locked": site.id != "manila",
                "est_offset_ns": {"manila": 1650.0, "naha": 14.0, "tokyo": -9.0, "seoul": 210.0}.get(site.id, 3.0),
                "holdover_s": 412.0 if site.id == "manila" else 0.0,
            }
            nodes.append({
                "site_id": site.id, "node_id": f"5f0c{k:02d}a2-1b7e-4c19-9d3a-{k:04d}e8c1a0f7" if online else None, "online": online,
                "latency_ms": lat_ms[site.id], "clock": clock, "driver": drv if online else None, "software": "petal-locate 0.1.0" if online else None,
                "channels": [{"channel_id": ch.id, "ok": online, "last_frame_age_s": 0.4 if online else 1420.0,
                              "windows": [{"monitor_id": m.id, "last_frame_age_s": 0.4 if online else 1420.0, "detection_frame_age_s": 0.4 if online else None,
                                           "blind": False, "blind_reason": None, "learning": False, "floor_raised_db": None, "note": None} for m in ch.monitors],
                              "detection_problem": None} for ch in site.channels],
                "connected_since": iso_from_ns(T0_NS - (4 * 3600 + k * 611) * 1_000_000_000) if online else None,
            })
        # site clocks as tasking estimates them from cross-site TDOA calibrations (UX-10): the
        # scenario's truth (labeled minus true time = -site_clock_offset_ns) between the
        # online sites, one calibration per pair through the satellites they share, and the
        # same-satellite clock checks tasking adds (R3: no satellite term, so Manila is seen
        # alike through muos-io and uhf-172e and its clock, not a satellite, is blamed)
        truth_off = {sid: -SCN.errors.site_clock_offset_ns.get(sid, 0.0) * 1e-9 for sid in ("honolulu", "tokyo", "naha", "seoul", "busan", "manila")}
        edges = []
        for a, b, sat_a, sat_b in (("honolulu", "naha", "muos-pac", "uhf-172e"), ("honolulu", "tokyo", "uhf-172e", "muos-pac"), ("naha", "manila", "muos-pac", "muos-io"),
                                   ("tokyo", "manila", "muos-io", "uhf-172e"), ("seoul", "busan", "muos-pac", "uhf-172e"), ("busan", "manila", "uhf-172e", "muos-io"),
                                   ("seoul", "naha", "muos-io", "muos-pac"),
                                   ("naha", "manila", "muos-io", "muos-io"), ("tokyo", "manila", "uhf-172e", "uhf-172e"),
                                   ("seoul", "busan", "muos-pac", "muos-pac")):
            edges.append(ClockEdge(sat_a, a, sat_b, b, truth_off[a] - truth_off[b], eph_s=5e-9))
        site_clocks = [c.model_dump(mode="json") for c in site_clock_offsets(edges, cfg)]
        satellites = [{"sat_id": sat.id, "ephemeris_ok": True, "unavailable_since_ns": None, "message": None} for sat in cfg.satellites]
        status = check(S.Status, {"server_t_ns": ns_str(T0_NS), "config_version": CONFIG_VERSION, "nodes": nodes, "events_active": 5,
                                  "queue": {"captures_pending": 2, "jobs_running": 1, "jobs_waiting": 0},
                                  "satellites": satellites, "site_clocks": site_clocks})
        write("status.json", status, 2)

    if want("accuracy") and not skip_accuracy:
        build_accuracy()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-accuracy", action="store_true")
    ap.add_argument("--only", default=None, help="comma list of: config, geometry, spectrum, events, sim, status, accuracy")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None
    build_all(args.skip_accuracy, only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
