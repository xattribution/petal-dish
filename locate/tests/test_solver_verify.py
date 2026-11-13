"""Independent verification of the solver and lines of position (MATH.md sections 9, 11, 12).

Written by the unit's verifier, separately from ``test_solver.py`` / ``test_lop.py``.
References never come from the code under test:

- truth measurements are built from MATH.md sections 3 to 5 directly (``bent_pipe``,
  ``bent_pipe_rates``, ``baseband_frequency``, the TDOA fixed point iterated to
  convergence, exact integer-nanosecond epochs), with satellite RIC offsets applied
  through ``PerturbedOrbit`` and clock / oscillator / bias errors applied as the
  physics says, not through ``predict_tdoa`` / ``predict_fdoa`` or the solver's partials;
- consider covariances are checked by propagating each systematic error through the
  truth and then through ``solve`` (one perturbation at a time) and by Monte Carlo
  trials in which every systematic error is drawn;
- CEP50 against a one-dimensional integral of the bivariate normal, ellipses against an
  eigen decomposition and the chi-square quantile, lines of position against bisection
  along meridians;
- the configuration objects are the real pydantic ``Region`` / ``SolverConfig`` /
  ``ProcessingConfig`` and the shipped ``pacific-uhf.yaml``.

Geometry: three inclined ``geo`` satellites at 100 W / 60 W / 20 W, sites at Bogota,
Santiago and Recife (generic city-centre coordinates), an emitter in central Brazil.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy import integrate, optimize, stats

from petal_locate.config.loader import load_config
from petal_locate.config.models import ProcessingConfig, Region, SolverConfig
from petal_locate.core import geometry as gm
from petal_locate.core import orbits as ob
from petal_locate.core import solver as sv
from petal_locate.core.geodesy import geodesic_distance_m, lla_to_ecef, radii_of_curvature
from petal_locate.core.lop import lop_geojson
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.core.types import CalRef, Measurement, PathSpec, Uncertainty

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

# ----------------------------------------------------------------------------- scenario

T_NS = ns_from_iso("2026-01-01T05:00:00Z") + 123_456_789  # not a whole number of 240 ns float steps
ORB: dict[str, Any] = {
    "w100": ob.GeoOrbit(-100.0, 4.0, phase_deg=70.0),
    "w60": ob.GeoOrbit(-60.0, 3.0, phase_deg=250.0),
    "w20": ob.GeoOrbit(-20.0, 5.0, phase_deg=130.0),
}
ORB_EQ: dict[str, Any] = {"w100": ob.GeoOrbit(-100.0), "w60": ob.GeoOrbit(-60.0), "w20": ob.GeoOrbit(-20.0)}
SITES: dict[str, np.ndarray] = {
    "bog": lla_to_ecef(4.71, -74.07, 2650.0),
    "scl": lla_to_ecef(-33.45, -70.67, 580.0),
    "rec": lla_to_ecef(-8.05, -34.90, 10.0),
}
F_E = 305e6
PA = PathSpec("w100", "bog", F_E, -41.0e6)
PB = PathSpec("w60", "scl", F_E, -33.6e6)
PC = PathSpec("w20", "rec", F_E, -41.0e6, tau_x_s=2.0e-6)  # a transponder group delay on one path
PAIRS = ((PA, PB), (PA, PC))
LAT, LON = -15.8, -47.9
ST, SF = 1e-6, 0.05  # CAF sigmas of the target rows, s and Hz
SOUTH_AMERICA = Region(lat_min=-40.0, lat_max=-5.0, lon_min=-80.0, lon_max=-30.0)

# reference emitter on the same satellites, sites and transponders, its own frequency
X_REF = lla_to_ecef(-23.55, -46.63, 770.0)
F_REF = F_E + 0.5e6
T_REF_NS = T_NS - 20_000_000_000
RST, RSF = 2e-6, 0.1  # CAF sigmas of the reference measurements
EPOCHS = (T_NS, T_NS + 10_000_000_007)


def ref_paths(a: PathSpec, b: PathSpec) -> tuple[PathSpec, PathSpec]:
    return (
        PathSpec(a.sat_id, a.site_id, F_REF, a.translation_hz, a.tau_x_s),
        PathSpec(b.sat_id, b.site_id, F_REF, b.translation_hz, b.tau_x_s),
    )


def truth_pair(
    x_e: np.ndarray,
    a: PathSpec,
    b: PathSpec,
    t_ns: int,
    orbits: dict[str, Any],
    sites: dict[str, np.ndarray],
    eps_sat: dict[str, float] | None = None,
    eps_site: dict[str, float] | None = None,
    clock: dict[str, float] | None = None,
    bias: float = 0.0,
) -> tuple[float, float]:
    """True (TDOA s, FDOA Hz) of path a minus path b at labelled t_mid = t_ns (MATH.md sections 3 to 5).

    eps_sat: true translation ``T = T_nom (1 + eps)``. eps_site: tuning ``f_tune (1 + eps)``.
    clock: delta_g (true = labelled + delta), which makes the apparent delay ``D - delta``.
    bias: added to the TDOA. The epoch is split into float seconds plus the exact
    remainder, and the fixed point is iterated to convergence.
    """
    eps_sat, eps_site, clock = eps_sat or {}, eps_site or {}, clock or {}
    t = sec(t_ns)
    rem = (t_ns - round(t * 1e9)) * 1e-9
    d_b = float(gm.bent_pipe(x_e, orbits[b.sat_id], sites[b.site_id], t, b.tau_x_s, dt_s=rem).D)
    tdoa = 0.0
    for _ in range(6):
        tdoa = float(gm.bent_pipe(x_e, orbits[a.sat_id], sites[a.site_id], t, a.tau_x_s, dt_s=rem + tdoa).D) - d_b

    def f_bb(p: PathSpec, dt: float) -> float:
        _, tau_d_dot, tau_u_dot = gm.bent_pipe_rates(x_e, orbits[p.sat_id], sites[p.site_id], t, p.tau_x_s, dt_s=rem + dt)
        translation = p.translation_hz * (1.0 + eps_sat.get(p.sat_id, 0.0))
        return float(
            gm.baseband_frequency(p.f_e_hz, translation, p.f_e_hz + p.translation_hz, tau_d_dot, tau_u_dot, eps_site.get(p.site_id, 0.0))
        )

    fdoa = f_bb(a, tdoa) - f_bb(b, 0.0)
    return tdoa - clock.get(a.site_id, 0.0) + clock.get(b.site_id, 0.0) + bias, fdoa


def truth_rows(lat: float = LAT, lon: float = LON, orbits: dict[str, Any] = ORB, kinds: tuple[str, ...] = ("tdoa", "fdoa"),
               t_list: tuple[int, ...] = (T_NS,)) -> list[Measurement]:
    x = lla_to_ecef(lat, lon, 0.0)
    rows = []
    for t in t_list:
        for a, b in PAIRS:
            tdoa, fdoa = truth_pair(x, a, b, t, orbits, SITES)
            if "tdoa" in kinds:
                rows.append(Measurement("tdoa", tdoa, ST, t, a, b))
            if "fdoa" in kinds:
                rows.append(Measurement("fdoa", fdoa, SF, t, a, b))
    return rows


def en_error(lat_hat: float, lon_hat: float, lat: float = LAT, lon: float = LON) -> np.ndarray:
    """Estimate minus truth, east/north metres at the truth (MATH.md section 9)."""
    m_rad, n_rad = radii_of_curvature(lat)
    dlon = (lon_hat - lon + 180.0) % 360.0 - 180.0
    return np.array([n_rad * np.cos(np.radians(lat)) * np.radians(dlon), m_rad * np.radians(lat_hat - lat)])


def nees(e: np.ndarray, cov: np.ndarray) -> float:
    return float(e @ np.linalg.solve(cov, e))


def miss_m(sol: sv.Solution, lat: float = LAT, lon: float = LON) -> float:
    return float(geodesic_distance_m(sol.lat_deg, sol.lon_deg, lat, lon))


# ----------------------------------------------------------------------------- systematic errors


def config_sigmas(sat_osc_frac: float | None = None) -> dict[str, Any]:
    """One-sigma systematic errors from the real configuration defaults (ProcessingConfig, Site.clock)."""
    unc = ProcessingConfig().uncertainty
    return {
        "pos": np.array(unc.ephemeris.operator.pos_m, dtype=float),
        "vel": np.array(unc.ephemeris.operator.vel_mps, dtype=float),
        "sat_osc": unc.sat_osc_frac if sat_osc_frac is None else sat_osc_frac,
        "site_osc": unc.site_osc_frac,
        "clock": 30e-9,  # Site.clock.sigma_ns default
        "bias": unc.tdoa_bias_ns * 1e-9,
    }


def uncertainty_of(sig: dict[str, Any]) -> Uncertainty:
    return Uncertainty(
        sat_pos_ric_m={s: sig["pos"].copy() for s in ORB},
        sat_vel_ric_mps={s: sig["vel"].copy() for s in ORB},
        site_clock_s={g: sig["clock"] for g in SITES},
        site_osc_frac={g: sig["site_osc"] for g in SITES},
        sat_osc_frac={s: sig["sat_osc"] for s in ORB},
        tdoa_bias_s=sig["bias"],
    )


def parameters(sig: dict[str, Any]) -> list[tuple[str, tuple[Any, ...], float]]:
    """``(error source, parameter, one sigma)`` for every systematic error of the scenario."""
    out: list[tuple[str, tuple[Any, ...], float]] = []
    for s in ORB:
        for c in range(3):
            out.append(("ephemeris_position", ("pos", s, c), float(sig["pos"][c])))
            out.append(("ephemeris_velocity", ("vel", s, c), float(sig["vel"][c])))
        out.append(("oscillator", ("sat_osc", s), sig["sat_osc"]))
    for g in SITES:
        out.append(("clock", ("clock", g), sig["clock"]))
        out.append(("oscillator", ("site_osc", g), sig["site_osc"]))
    for a, b in PAIRS:
        out.append(("tdoa_bias", ("bias", a.sat_id, b.sat_id), sig["bias"]))
    return out


def measure(
    values: dict[tuple[Any, ...], float],
    calibrated: bool,
    row_noise: np.ndarray | None = None,
    ref_noise: np.ndarray | None = None,
) -> list[Measurement]:
    """Target rows (TDOA + FDOA of both pairs at both EPOCHS) under the truth errors ``values``.

    Satellite RIC offsets are applied to the truth orbit with ``t_ref`` = the first epoch.
    Calibrated rows carry the correction of one reference measurement per (pair, kind),
    measured 20 s before the first epoch under the same errors and shared by both epochs.
    """
    pos = {s: np.zeros(3) for s in ORB}
    vel = {s: np.zeros(3) for s in ORB}
    eps_sat: dict[str, float] = {}
    eps_site: dict[str, float] = {}
    clock: dict[str, float] = {}
    bias: dict[tuple[str, str], float] = {}
    for key, v in values.items():
        if key[0] == "pos":
            pos[key[1]][key[2]] += v
        elif key[0] == "vel":
            vel[key[1]][key[2]] += v
        elif key[0] == "sat_osc":
            eps_sat[key[1]] = v
        elif key[0] == "site_osc":
            eps_site[key[1]] = v
        elif key[0] == "clock":
            clock[key[1]] = v
        else:
            bias[(key[1], key[2])] = v
    truth_orbits = dict(ORB)
    for s in ORB:
        if np.any(pos[s]) or np.any(vel[s]):
            truth_orbits[s] = ob.PerturbedOrbit(ORB[s], pos[s], vel[s], sec(T_NS))
    corrections: dict[tuple[str, str, str], tuple[float, CalRef]] = {}
    if calibrated:
        j = 0
        for a, b in PAIRS:
            ra, rb = ref_paths(a, b)
            tdoa, fdoa = truth_pair(X_REF, ra, rb, T_REF_NS, truth_orbits, SITES, eps_sat, eps_site, clock, bias.get((a.sat_id, b.sat_id), 0.0))
            for kind, value, sigma in (("tdoa", tdoa, RST), ("fdoa", fdoa, RSF)):
                noise = 0.0 if ref_noise is None else float(ref_noise[j])
                ref_row = Measurement(kind, value + noise, sigma, T_REF_NS, ra, rb)
                corrections[(a.sat_id, b.sat_id, kind)] = sv.calibration_correction(ref_row, X_REF, ORB, SITES, f"ref-{j}")
                j += 1
    x = lla_to_ecef(LAT, LON, 0.0)
    rows = []
    k = 0
    for t in EPOCHS:
        for a, b in PAIRS:
            tdoa, fdoa = truth_pair(x, a, b, t, truth_orbits, SITES, eps_sat, eps_site, clock, bias.get((a.sat_id, b.sat_id), 0.0))
            for kind, value, sigma in (("tdoa", tdoa, ST), ("fdoa", fdoa, SF)):
                value += 0.0 if row_noise is None else float(row_noise[k])
                k += 1
                if calibrated:
                    c, cal = corrections[(a.sat_id, b.sat_id, kind)]
                    rows.append(Measurement(kind, value, sigma, t, a, b, correction=c, cal=cal))
                else:
                    rows.append(Measurement(kind, value, sigma, t, a, b))
    return rows


def row_sigmas() -> np.ndarray:
    return np.array([ST, SF] * (2 * len(EPOCHS)))


REF_SIGMAS = np.array([RST, RSF, RST, RSF])


# ----------------------------------------------------------------------------- the reference itself


def test_truth_generator_agrees_with_the_predictors() -> None:
    """The MATH section 5 truth used below and the solver's predictor describe the same rows."""
    x = lla_to_ecef(LAT, LON, 0.0)
    for a, b in PAIRS:
        tdoa, fdoa = truth_pair(x, a, b, T_NS, ORB, SITES)
        assert tdoa == pytest.approx(float(gm.predict_tdoa(x, a, b, sec(T_NS), ORB, SITES)), abs=1e-13)
        assert fdoa == pytest.approx(float(gm.predict_fdoa(x, a, b, sec(T_NS), ORB, SITES)), abs=1e-6)
    # MATH 12.6 signs, from the truth: a clock offset at site a lowers the TDOA, a satellite
    # oscillator offset of satellite i raises the FDOA by eps T_nom,i
    t0, f0 = truth_pair(x, PA, PB, T_NS, ORB, SITES)
    t1, _ = truth_pair(x, PA, PB, T_NS, ORB, SITES, clock={"bog": 1e-6})
    _, f1 = truth_pair(x, PA, PB, T_NS, ORB, SITES, eps_sat={"w100": 1e-7})
    assert t1 - t0 == pytest.approx(-1e-6, abs=1e-15)
    assert f1 - f0 == pytest.approx(1e-7 * PA.translation_hz, rel=1e-5)


# ----------------------------------------------------------------------------- MATH 12.8, other geometry


def test_south_american_noise_free_fix_with_the_real_config_models() -> None:
    """12.8(a) on another geometry, rows from the independent truth, real SolverConfig and Region."""
    rows = truth_rows()
    for cfg, region in ((SolverConfig(), Region()), (ProcessingConfig().solver, ProcessingConfig().region)):
        sol, reason = sv.solve(rows, ORB, SITES, cfg, region)
        assert reason is None and sol is not None
        assert miss_m(sol) < 1.0 and sol.chi2 < 1e-6
        assert sol.ambiguities == [] and sol.ambiguity_resolved_by in ("none", "measurements")
        assert sol.dof == 2 and sol.reduced_chi2 == pytest.approx(sol.chi2 / 2.0)
        assert np.all(np.abs(sol.residuals) < 1e-3) and sol.n_measurements == 4
        assert sol.ellipse["confidence"] == cfg.confidence
        assert sol.ellipse == sv.error_ellipse(sol.cov_en_m2, cfg.confidence)
        assert sol.cep50_m == sv.cep50(sol.cov_en_m2)
        json.dumps(sol.as_dict(), allow_nan=False)


@pytest.mark.slow
def test_south_american_math12_8b_monte_carlo() -> None:
    """12.8(b): 500 seeded noise trials on the other geometry and another seed."""
    truth = truth_rows()
    sigma = np.array([r.sigma for r in truth])
    rng = np.random.default_rng(2026)
    values_nees, values_chi2 = [], []
    for trial in range(500):
        rows = [Measurement(r.kind, r.value + float(n), r.sigma, r.t_ns, r.a, r.b) for r, n in zip(truth, rng.standard_normal(4) * sigma)]
        sol, reason = sv.solve(rows, ORB, SITES, SolverConfig(), Region(), starts=[(LAT, LON)])
        assert sol is not None, reason
        if trial < 15:  # the global search lands on the same minimum
            cold, _ = sv.solve(rows, ORB, SITES, SolverConfig(), Region())
            assert cold is not None and miss_m(cold, sol.lat_deg, sol.lon_deg) < 2.0
            assert cold.chi2 == pytest.approx(sol.chi2, abs=1e-4) and cold.ambiguities == []
        values_nees.append(nees(en_error(sol.lat_deg, sol.lon_deg), sol.cov_noise_en_m2))
        values_chi2.append(sol.chi2)
    values = np.array(values_nees)
    assert 1.73 <= values.mean() <= 2.27
    assert 0.92 <= np.mean(values <= stats.chi2.ppf(0.95, 2)) <= 0.98
    assert 1.73 <= np.mean(values_chi2) <= 2.27


# ----------------------------------------------------------------------------- MATH 12.9, other geometry


def test_south_american_math12_9_equatorial_mirror() -> None:
    lat, lon = -20.0, -50.0
    for kinds in (("tdoa",), ("tdoa", "fdoa")):
        rows = truth_rows(lat, lon, ORB_EQ, kinds=kinds)
        sol, reason = sv.solve(rows, ORB_EQ, SITES, SolverConfig(), Region())
        assert reason is None and sol is not None
        points = [sol] + sol.ambiguities
        assert len(points) == 2 and sol.ambiguity_resolved_by == "unresolved"
        assert miss_m(points[0], -lat, lon) < 1.0 and miss_m(points[1], lat, lon) < 1.0  # tie: north first
        assert abs(points[0].chi2 - points[1].chi2) < 1e-6
    rows = truth_rows(lat, lon, ORB_EQ, kinds=("tdoa", "fdoa"))
    for region, expected in ((Region(lat_max=0.0), (lat, lon)), (Region(lat_min=0.0), (-lat, lon))):
        sol, reason = sv.solve(rows, ORB_EQ, SITES, SolverConfig(), region)
        assert reason is None and sol is not None
        assert sol.ambiguities == [] and sol.ambiguity_resolved_by == "region"
        assert miss_m(sol, *expected) < 1.0


@pytest.mark.parametrize("lat", [0.4, 0.6, 0.05])
def test_emitter_near_the_equator_under_equatorial_satellites_is_located(lat: float) -> None:
    """Regression: the grid cell on the equator used to converge to the symmetric saddle there.

    Every row is even in latitude, so Levenberg-Marquardt started on the equator stays on
    it and stops where the north derivative vanishes (chi2 0.1 here, inside the gate),
    with no north information; that point was taken for an ambiguity and the whole solve
    returned "unobservable" although both true minima are observable (one-sigma
    semi-major about 100 km at 0.4 deg, 1200 km at 0.05 deg, both under 1e7 m).
    """
    rows = truth_rows(lat, -50.0, ORB_EQ, kinds=("tdoa",))
    for x_lat in (lat, -lat):
        helper = sv.position_covariance(rows, x_lat, -50.0, ORB_EQ, SITES)
        assert helper is not None and np.sqrt(np.max(np.linalg.eigvalsh(helper["cov_en_m2"]))) < 1e7
    unc = uncertainty_of(config_sigmas())
    for u, starts in ((None, None), (unc, None), (None, [(0.0, -50.0)])):
        sol, reason = sv.solve(rows, ORB_EQ, SITES, SolverConfig(), Region(), uncertainty=u, starts=starts)
        assert reason is None and sol is not None
        points = [sol] + sol.ambiguities
        assert len(points) == 2 and sol.ambiguity_resolved_by == "unresolved"
        assert miss_m(points[0], lat, -50.0) < 1.0 and miss_m(points[1], -lat, -50.0) < 1.0
    # on the equator itself the rows really carry no north information
    sol, reason = sv.solve(truth_rows(0.0, -50.0, ORB_EQ, kinds=("tdoa",)), ORB_EQ, SITES, SolverConfig(), Region())
    assert sol is None and reason == "unobservable"


# ----------------------------------------------------------------------------- consider covariance


def test_consider_columns_reproduce_the_truth_response_to_each_error() -> None:
    """``G G^T`` (G = H_c S_c^1/2) equals the row covariance built by perturbing the truth.

    Every systematic error is applied, one at a time at one sigma, to the truth (the
    orbit through PerturbedOrbit, the clocks, oscillators and bias as MATH.md sections 4
    and 5 say), calibrations included; the change of ``z - c`` is that error's column.
    Compared in units of the row sigmas, ordering-free.
    """
    sig = config_sigmas(sat_osc_frac=2e-9)
    unc = uncertainty_of(sig)
    x = lla_to_ecef(LAT, LON, 0.0)
    for calibrated in (False, True):
        base = measure({}, calibrated)
        z0 = np.array([r.value - r.correction for r in base])
        columns = []
        for _, key, s in parameters(sig):
            rows = measure({key: s}, calibrated)
            columns.append(np.array([r.value - r.correction for r in rows]) - z0)
        if calibrated:  # each reference measurement's noise enters through its correction
            for j, s in enumerate(REF_SIGMAS):
                noise = np.zeros(4)
                noise[j] = s
                rows = measure({}, True, ref_noise=noise)
                columns.append(np.array([r.value - r.correction for r in rows]) - z0)
        truth_gain = np.stack(columns, axis=1) / row_sigmas()[:, None]
        gain = sv._Rows(base, ORB, SITES, unc).consider(x[None, :])[0] / row_sigmas()[:, None]
        expected = truth_gain @ truth_gain.T
        got = gain @ gain.T
        scale = np.sqrt(np.outer(np.diag(expected), np.diag(expected))) + 1e-6
        assert np.all(np.abs(got - expected) <= 0.01 * scale), np.max(np.abs(got - expected) / scale)


def _sensitivity(calibrated: bool, sig: dict[str, Any]) -> tuple[sv.Solution, dict[str, float], np.ndarray, np.ndarray]:
    """Propagate every error, one at a time, through the truth and through ``solve``."""
    unc = uncertainty_of(sig)
    base, reason = sv.solve(measure({}, calibrated), ORB, SITES, SolverConfig(), SOUTH_AMERICA, uncertainty=unc, starts=[(LAT, LON)])
    assert base is not None, reason
    assert miss_m(base) < 1.0

    def shift(rows: list[Measurement]) -> np.ndarray:
        sol, why = sv.solve(rows, ORB, SITES, SolverConfig(), SOUTH_AMERICA, uncertainty=unc, starts=[(base.lat_deg, base.lon_deg)])
        assert sol is not None, why
        return en_error(sol.lat_deg, sol.lon_deg, base.lat_deg, base.lon_deg)

    budget: dict[str, float] = dict.fromkeys(sv.ERROR_SOURCES, 0.0)
    consider = np.zeros((2, 2))
    for source, key, s in parameters(sig):
        e = shift(measure({key: s}, calibrated))
        budget[source] += float(e @ e)
        consider += np.outer(e, e)
    noise = np.zeros((2, 2))
    m = 4 * len(EPOCHS)
    for k in range(m):
        n = np.zeros(m)
        n[k] = row_sigmas()[k]
        e = shift(measure({}, calibrated, row_noise=n))
        noise += np.outer(e, e)
    budget["noise"] = float(np.trace(noise))
    if calibrated:
        for j, s in enumerate(REF_SIGMAS):
            n = np.zeros(4)
            n[j] = s
            e = shift(measure({}, True, ref_noise=n))
            budget["reference"] += float(e @ e)
            consider += np.outer(e, e)
    return base, budget, noise, consider


@pytest.mark.parametrize("calibrated", [True, pytest.param(False, marks=pytest.mark.slow)], ids=["calibrated", "uncalibrated"])
def test_consider_covariance_equals_errors_propagated_through_solve(calibrated: bool) -> None:
    """Each systematic error (and each row's and reference's noise) at one sigma, through truth and solve.

    For the linearised estimator the fix moves by ``K dz``; summing the outer products
    of those moves over all parameters gives ``P_consider`` and over all noise terms
    ``P_noise``, and per source the error budget, independently of the solver's own
    partials. Real configuration sigmas, except a satellite oscillator sigma of 2e-9 on
    the uncalibrated rows so that the FDOA rows still carry information (with 1e-6 they
    are weighted out and the oscillator term is invisible).
    """
    sig = config_sigmas(sat_osc_frac=None if calibrated else 2e-9)
    base, budget, noise, consider = _sensitivity(calibrated, sig)
    rows = measure({}, calibrated)
    helper = sv.position_covariance(rows, base.lat_deg, base.lon_deg, ORB, SITES, 0.0, uncertainty_of(sig), 0.95)
    assert helper is not None
    total = noise + consider
    scale = np.sqrt(np.outer(np.diag(total), np.diag(total)))
    for got in (base.cov_noise_en_m2, helper["cov_noise_en_m2"]):
        assert np.all(np.abs(got - noise) <= 0.02 * scale)
    for got in (base.cov_consider_en_m2, helper["cov_consider_en_m2"]):
        assert np.all(np.abs(got - consider) <= 0.02 * scale)
    assert np.all(np.abs(base.cov_en_m2 - total) <= 0.02 * scale)
    trace = float(np.trace(total))
    for name in sv.ERROR_SOURCES:
        assert abs(helper["budget_m2"][name] - budget[name]) <= 0.03 * budget[name] + 2e-3 * trace, name
    assert base.dominant_error == helper["dominant_error"] == max(budget, key=budget.get)
    if calibrated:  # common-mode terms cancel exactly in the differenced rows
        assert budget["clock"] < 1e-6 * trace and budget["tdoa_bias"] < 1e-6 * trace
        assert budget["reference"] > 0.1 * trace
    else:
        for name in ("ephemeris_position", "clock", "oscillator", "tdoa_bias"):
            assert budget[name] > 1e-4 * trace, name  # each family is really exercised


@pytest.mark.slow
@pytest.mark.parametrize("calibrated, seed", [(True, 22), (False, 21)], ids=["calibrated", "uncalibrated"])
def test_consider_nees_with_every_systematic_error_drawn(calibrated: bool, seed: int) -> None:
    """MATH 12.10 widened: ephemeris RIC position and velocity, satellite and site oscillators,
    site clocks, pair biases and the shared reference measurements are all drawn in every
    trial and propagated through the truth and through ``solve``.

    NEES with ``P_total`` averages 2 (chi-square(2)) while NEES with ``P_noise`` is far
    larger, and chi2 averages its dof (the total covariance also describes the residuals).
    """
    sig = config_sigmas(sat_osc_frac=None if calibrated else 2e-9)
    unc = uncertainty_of(sig)
    plist = parameters(sig)
    rng = np.random.default_rng(seed)
    n_trials = 100
    total, noise_only, chi2 = [], [], []
    dof = 0
    for _ in range(n_trials):
        values = {key: float(rng.standard_normal()) * s for _, key, s in plist}
        rows = measure(
            values,
            calibrated,
            row_noise=rng.standard_normal(4 * len(EPOCHS)) * row_sigmas(),
            ref_noise=rng.standard_normal(4) * REF_SIGMAS,
        )
        sol, reason = sv.solve(rows, ORB, SITES, SolverConfig(), SOUTH_AMERICA, uncertainty=unc, starts=[(LAT, LON)])
        assert sol is not None, reason
        e = en_error(sol.lat_deg, sol.lon_deg)
        total.append(nees(e, sol.cov_en_m2))
        noise_only.append(nees(e, sol.cov_noise_en_m2))
        chi2.append(sol.chi2)
        dof = sol.dof
    assert abs(np.mean(total) - 2.0) < 3.0 * 2.0 / np.sqrt(n_trials)
    assert 0.88 <= np.mean(np.array(total) <= stats.chi2.ppf(0.95, 2)) <= 1.0
    assert np.mean(noise_only) > 2.0 * np.mean(total) + 2.0
    assert abs(np.mean(chi2) - dof) < 3.0 * np.sqrt(2.0 * dof / n_trials)


# ----------------------------------------------------------------------------- shipped configuration


def _pacific() -> tuple[Any, dict[str, Any], dict[str, np.ndarray]]:
    cfg = load_config(CONFIG_DIR / "pacific-uhf.yaml")
    return cfg, cfg.orbits(), cfg.sites_xyz()


@pytest.mark.parametrize("lat, lon", [(12.0, 178.5), (-8.0, -176.0)], ids=["west-of-180", "east-of-180"])
def test_pacific_config_calibrated_fix_across_the_antimeridian(lat: float, lon: float) -> None:
    """Real config end to end: wrapped Region, SolverConfig, resolved Uncertainty, reference carrier.

    The rows and the reference measurement carry clock offsets at both sites and a
    satellite oscillator offset; the calibration removes them exactly.
    """
    cfg, orbits, sites = _pacific()
    region, solver_cfg, unc = cfg.processing.region, cfg.processing.solver, cfg.resolved_uncertainty()
    carrier = next(c for c in cfg.carriers if c.id == "ref-sapporo")
    loc = carrier.location
    x_ref = lla_to_ecef(loc.lat_deg, loc.lon_deg, loc.alt_m + loc.antenna_height_m)
    sat_a, sat_b = cfg.satellite("muos-pac"), cfg.satellite("uhf-172e")
    t_a = next(t for t in sat_a.transponders if t.id == carrier.transponder_id)
    t_b = next(t for t in sat_b.transponders if t.id == carrier.transponder_id)
    f_e = carrier.uplink_center_hz + 6.0e3  # an interferer inside the same 25 kHz channel
    a, b = PathSpec(sat_a.id, "honolulu", f_e, t_a.translation_hz), PathSpec(sat_b.id, "tokyo", f_e, t_b.translation_hz)
    ra = PathSpec(sat_a.id, "honolulu", carrier.uplink_center_hz, t_a.translation_hz)
    rb = PathSpec(sat_b.id, "tokyo", carrier.uplink_center_hz, t_b.translation_hz)
    clock = {"honolulu": 70e-9, "tokyo": -40e-9}
    eps_sat = {sat_a.id: 3e-7, sat_b.id: -2e-7}
    t0 = ns_from_iso("2026-03-01T10:00:00Z") + 777
    t_ref = t0 - 60_000_000_000
    corrections = {}
    for kind, sigma in (("tdoa", 2e-6), ("fdoa", 0.1)):
        tdoa, fdoa = truth_pair(x_ref, ra, rb, t_ref, orbits, sites, eps_sat, None, clock)
        ref_row = Measurement(kind, tdoa if kind == "tdoa" else fdoa, sigma, t_ref, ra, rb)
        c, cal = sv.calibration_correction(ref_row, x_ref, orbits, sites, f"sapporo-{kind}")
        assert isinstance(cal, CalRef) and cal.t_ns == t_ref and cal.sigma == sigma and np.array_equal(cal.x_ref, x_ref)
        if kind == "tdoa":
            assert c == pytest.approx(-clock["honolulu"] + clock["tokyo"], abs=1e-13)
        else:
            assert c == pytest.approx(eps_sat[sat_a.id] * t_a.translation_hz - eps_sat[sat_b.id] * t_b.translation_hz, abs=1e-4)
        corrections[kind] = (c, cal)
    x = lla_to_ecef(lat, lon, 0.0)
    rows = []
    for k in range(3):
        t = t0 + k * 300_000_000_000
        tdoa, fdoa = truth_pair(x, a, b, t, orbits, sites, eps_sat, None, clock)
        for kind, value, sigma in (("tdoa", tdoa, 1e-6), ("fdoa", fdoa, 0.05)):
            c, cal = corrections[kind]
            rows.append(Measurement(kind, value, sigma, t, a, b, correction=c, cal=cal))
    for u in (None, unc):
        sol, reason = sv.solve(rows, orbits, sites, solver_cfg, region, uncertainty=u)
        assert reason is None and sol is not None
        assert miss_m(sol, lat, lon) < 1.0 and sol.chi2 < 1e-6 and -180.0 <= sol.lon_deg < 180.0
        assert sol.ambiguities == []
        helper = sv.position_covariance(rows, sol.lat_deg, sol.lon_deg, orbits, sites, solver_cfg.height_m, u, solver_cfg.confidence)
        assert helper is not None
        # (the helper takes H_c at the fix itself, the solve at the point its last refinement started from)
        assert np.allclose(helper["cov_en_m2"], sol.cov_en_m2, rtol=1e-6)
        assert sol.ellipse == sv.error_ellipse(sol.cov_en_m2, solver_cfg.confidence)
        assert helper["ellipse"] == pytest.approx(sol.ellipse, rel=1e-6)
        assert sol.cep50_m == pytest.approx(_cep50_reference(sol.cov_en_m2), rel=1e-9)
        assert sol.dominant_error == "reference"  # its sigma is twice the rows' and shared by all epochs
        json.dumps(sol.as_dict(), allow_nan=False)


def test_pacific_config_math12_9_mirror_inside_the_wrapped_region() -> None:
    """Both equatorial mirrors lie inside the shipped region (lat -60..60, lon 60..-120)."""
    cfg, _, sites = _pacific()
    orbits = {"pac": ob.GeoOrbit(-177.0), "172e": ob.GeoOrbit(172.0), "io": ob.GeoOrbit(75.0)}
    pa = PathSpec("pac", "honolulu", 301e6, -41e6)
    pb = PathSpec("172e", "tokyo", 301e6, -33.6e6)
    pc = PathSpec("io", "manila", 301e6, -41e6)
    t = ns_from_iso("2026-01-01T03:00:00Z")
    x = lla_to_ecef(30.0, 140.0, 0.0)
    rows = [Measurement("tdoa", truth_pair(x, p, q, t, orbits, sites)[0], 1e-6, t, p, q) for p, q in ((pa, pb), (pa, pc))]
    sol, reason = sv.solve(rows, orbits, sites, cfg.processing.solver, cfg.processing.region)
    assert reason is None and sol is not None
    assert [round(p.lat_deg, 6) for p in [sol] + sol.ambiguities] == [30.0, -30.0]
    assert all(geodesic_distance_m(p.lat_deg, p.lon_deg, np.sign(p.lat_deg) * 30.0, 140.0) < 1.0 for p in [sol] + sol.ambiguities)
    assert sol.ambiguity_resolved_by == "unresolved"


def _crossings(lines: list[np.ndarray], lon: float) -> list[float]:
    out = []
    for line in lines:
        for p, q in zip(line[:-1], line[1:]):
            if (p[0] - lon) * (q[0] - lon) < 0.0:
                w = (lon - p[0]) / (q[0] - p[0])
                out.append(float(p[1] + w * (q[1] - p[1])))
    return sorted(out)


def _inside(rings: list[list[list[float]]], lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Even-odd ray casting over the rings of one polygon (vectorised over points and edges)."""
    inside = np.zeros(lon.shape, dtype=bool)
    for ring in rings:
        v = np.asarray(ring)
        x0, y0, x1, y1 = v[:-1, 0], v[:-1, 1], v[1:, 0], v[1:, 1]
        dy = np.where(y1 == y0, np.inf, y1 - y0)
        crosses = (y0[None] > lat[:, None]) != (y1[None] > lat[:, None])
        x_at = x0[None] + (lat[:, None] - y0[None]) * (x1 - x0)[None] / dy[None]
        inside ^= (np.count_nonzero(crosses & (lon[:, None] < x_at), axis=1) % 2) == 1
    return inside


@pytest.mark.parametrize("kind", ["tdoa", "fdoa"])
def test_lop_across_the_antimeridian_on_the_shipped_pacific_region(kind: str) -> None:
    cfg, orbits, sites = _pacific()
    region = cfg.processing.region
    assert region.lon_min == 60.0 and region.lon_max == -120.0  # wraps the antimeridian
    a = PathSpec("muos-pac", "honolulu", 301.0e6, -41.0e6)
    b = PathSpec("uhf-172e", "tokyo", 301.0e6, -33.6e6)
    t_ns = ns_from_iso("2026-03-01T10:00:00Z") + 777
    f = gm.predict_tdoa if kind == "tdoa" else gm.predict_fdoa
    sigma, k_sigma = (200e-6, 2.0) if kind == "tdoa" else (3.0, 1.5)
    for lat, lon in ((8.0, 179.6), (-5.0, -178.0)):
        x = lla_to_ecef(lat, lon, 0.0)
        z = truth_pair(x, a, b, t_ns, orbits, sites)[0 if kind == "tdoa" else 1]
        collection = lop_geojson(Measurement(kind, z, sigma, t_ns, a, b), orbits, sites, region, k_sigma=k_sigma)
        roles = {feat["properties"]["role"]: feat for feat in collection["features"]}
        lines = [np.array(line) for line in roles["line"]["geometry"]["coordinates"]]
        every = np.concatenate(lines)
        assert np.all(every[:, 0] >= 60.0) and np.all(every[:, 0] < 420.0)  # [lon_min, lon_min + 360)
        assert np.all(every[:, 0] <= 240.0 + 1e-9)  # inside the region's 180 degree span
        assert max(float(np.max(np.abs(np.diff(line[:, 0])))) for line in lines) <= 0.25 + 1e-9
        assert np.min(every[:, 0]) < 180.0 < np.max(every[:, 0])  # continuous across 180, unwrapped
        # crossings of meridians either side of 180 against bisection along the meridian
        for meridian in (179.8, 180.3):

            def g(phi: float) -> float:
                return float(f(lla_to_ecef(phi, meridian, 0.0), a, b, sec(t_ns), orbits, sites)) - z

            grid = np.linspace(-60.0, 60.0, 121)
            values = np.array([g(phi) for phi in grid])
            roots = [optimize.brentq(g, grid[i], grid[i + 1], xtol=1e-10) for i in np.flatnonzero(np.sign(values[:-1]) != np.sign(values[1:]))]
            assert len(roots) >= 1
            assert np.allclose(_crossings(lines, meridian), roots, atol=2e-3)
        # the band: points with |h - z| <= k sigma, both satellites visible, against the polygons
        polygons = roles["band"]["geometry"]["coordinates"]
        for rings in polygons:
            v = np.asarray(rings[0])
            assert 0.5 * float(np.sum(v[:-1, 0] * v[1:, 1] - v[1:, 0] * v[:-1, 1])) > 0.0  # exterior counter-clockwise
        rng = np.random.default_rng(7)
        p_lat, p_lon = rng.uniform(-60.0, 60.0, 3000), rng.uniform(60.0, 240.0, 3000)
        pts = lla_to_ecef(p_lat, p_lon, 0.0)
        value = (np.asarray(f(pts, a, b, sec(t_ns), orbits, sites)) - z) / sigma
        visible = np.ones(p_lat.size, dtype=bool)
        for sat in (a.sat_id, b.sat_id):
            visible &= np.asarray(gm.elevation_deg(pts, orbits[sat], sec(t_ns))) >= 0.5  # clear of the mask edge
        member = np.zeros(p_lat.size, dtype=bool)
        for rings in polygons:
            member |= _inside(rings, p_lon, p_lat)
        clear = visible & (np.abs(np.abs(value) - k_sigma) > 0.05 * k_sigma)
        assert np.count_nonzero(clear & (np.abs(value) <= k_sigma)) > 100
        assert np.array_equal(member[clear], (np.abs(value) <= k_sigma)[clear])


# ----------------------------------------------------------------------------- ellipse, CEP50, helpers


def _cep50_reference(cov: np.ndarray) -> float:
    """CEP50 from P(|e| <= r) = int phi(u) [2 Phi(sqrt(r^2 - s1^2 u^2) / s2) - 1] du (|u| <= r / s1)."""
    lam = np.linalg.eigvalsh(np.asarray(cov, dtype=float))
    s1, s2 = float(np.sqrt(lam[1])), float(np.sqrt(max(lam[0], 0.0)))

    def inside(r: float) -> float:
        if s2 == 0.0:
            return 2.0 * stats.norm.cdf(r / s1) - 1.0
        e = r / s1

        def integrand(u: float) -> float:
            return stats.norm.pdf(u) * (2.0 * stats.norm.cdf(np.sqrt(max(r * r - (s1 * u) ** 2, 0.0)) / s2) - 1.0)

        cuts = [0.0, 0.99 * e, (1 - 1e-3) * e, (1 - 1e-4) * e, (1 - 1e-5) * e, e]
        return 2.0 * sum(integrate.quad(integrand, lo, hi, epsabs=1e-15, epsrel=1e-13, limit=400)[0] for lo, hi in zip(cuts[:-1], cuts[1:]))

    return float(optimize.brentq(lambda r: inside(r) - 0.5, 0.6 * s1, 1.2 * s1, xtol=1e-13 * s1, rtol=1e-14))


@pytest.mark.parametrize("s1, s2", [(1000.0, 1000.0), (1000.0, 600.0), (2e6, 6000.0), (50.0, 0.0), (8159.0, 3130.0)])
def test_cep50_against_a_one_dimensional_integral_of_the_normal(s1: float, s2: float) -> None:
    reference = _cep50_reference(np.diag([s2**2, s1**2]))
    for bearing in (0.0, 33.0, 101.0):
        th = np.radians(bearing)
        major, minor = np.array([np.sin(th), np.cos(th)]), np.array([np.cos(th), -np.sin(th)])
        cov = s1**2 * np.outer(major, major) + s2**2 * np.outer(minor, minor)
        assert sv.cep50(cov) == pytest.approx(reference, rel=1e-9)


def test_error_ellipse_against_an_eigen_decomposition() -> None:
    rng = np.random.default_rng(12)
    for _ in range(50):
        a = rng.standard_normal((2, 2)) * 10.0 ** rng.uniform(0, 4)
        cov = a @ a.T
        p = float(rng.uniform(0.05, 0.999))
        lam, vec = np.linalg.eigh(cov)
        k = np.sqrt(stats.chi2.ppf(p, 2))
        ellipse = sv.error_ellipse(cov, p)
        assert ellipse["semi_major_m"] == pytest.approx(k * np.sqrt(lam[1]), rel=1e-9)
        assert ellipse["semi_minor_m"] == pytest.approx(k * np.sqrt(lam[0]), rel=1e-9)
        bearing = np.degrees(np.arctan2(vec[0, 1], vec[1, 1])) % 180.0  # east, north components of the major axis
        diff = abs(ellipse["azimuth_deg"] - bearing)
        assert min(diff, 180.0 - diff) < 1e-6 and 0.0 <= ellipse["azimuth_deg"] < 180.0


def test_position_covariance_rejects_a_bad_confidence_by_name() -> None:
    rows = truth_rows()
    for bad in (None, "x", float("nan"), 0.0, 1.0, True):
        with pytest.raises(ValueError, match="confidence"):
            sv.position_covariance(rows, LAT, LON, ORB, SITES, 0.0, None, bad)  # type: ignore[arg-type]
    good = sv.position_covariance(rows, LAT, LON, ORB, SITES, 0.0, None, SolverConfig().confidence)
    assert good is not None and good["ellipse"]["confidence"] == 0.95
