"""solver: position fixes from TDOA/FDOA rows (MATH.md section 9, section 12 items 8, 9, 10).

References are independent of the code under test: closed forms, Monte Carlo draws,
scalar predictor calls combined with dense numpy algebra straight from the text of
MATH.md section 9 (``brute_force``), brute-force loops, or the known truth of a
synthetic scenario. `solver_cfg` and `region` are small stand-ins for the pydantic
models.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from scipy import integrate, stats

from petal_locate.core import geometry as gm
from petal_locate.core import orbits as ob
from petal_locate.core import solver as sv
from petal_locate.core import types as core_types
from petal_locate.core.geodesy import (
    destination,
    geodesic_distance_m,
    lla_to_ecef,
    radii_of_curvature,
)
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.core.types import CalRef, Measurement, PathSpec, Uncertainty

# ----------------------------------------------------------------------------- stand-ins


class Region:
    """Stand-in for the configuration Region (ARCHITECTURE.md section 3)."""

    def __init__(self, lat_min: float = -70.0, lat_max: float = 70.0, lon_min: float = -180.0, lon_max: float = 180.0) -> None:
        self.lat_min, self.lat_max, self.lon_min, self.lon_max = lat_min, lat_max, lon_min, lon_max
        self.grid_calls = 0

    def lon_span_deg(self) -> float:
        if self.lon_min == -180.0 and self.lon_max == 180.0:
            return 360.0
        return (self.lon_max - self.lon_min) % 360.0

    def contains(self, lat_deg: Any, lon_deg: Any) -> np.ndarray:
        lat, lon = np.asarray(lat_deg, dtype=float), np.asarray(lon_deg, dtype=float)
        in_lat = (lat >= self.lat_min) & (lat <= self.lat_max)
        return in_lat & (np.mod(lon - self.lon_min, 360.0) <= self.lon_span_deg())

    def grid(self, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
        self.grid_calls += 1
        lat = self.lat_min + step_deg * np.arange(int(np.floor((self.lat_max - self.lat_min) / step_deg + 1e-9)) + 1)
        span = self.lon_span_deg()
        n_lon = int(np.floor(span / step_deg + 1e-9)) + (0 if span >= 360.0 else 1)
        return lat, self.lon_min + step_deg * np.arange(n_lon)


class BareRegion:
    """Only the two documented methods; contains() understands [-180, 180) longitudes only."""

    def __init__(self, inner: Region) -> None:
        self._inner = inner

    def contains(self, lat_deg: Any, lon_deg: Any) -> np.ndarray:
        lon = np.asarray(lon_deg, dtype=float)
        return self._inner.contains(lat_deg, lon) & (lon >= -180.0) & (lon < 180.0)

    def grid(self, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
        return self._inner.grid(step_deg)


def make_cfg(**overrides: Any) -> SimpleNamespace:
    """Stand-in for ProcessingConfig.solver with the documented defaults."""
    values = dict(height_m=0.0, min_elevation_deg=0.0, confidence=0.95, grid_step_deg=1.0, n_starts=16, chi2_ambiguity=9.0)
    values.update(overrides)
    return SimpleNamespace(**values)


CFG = make_cfg()

# ----------------------------------------------------------------------------- scenario (MATH 12.8)

T_NS = ns_from_iso("2026-01-01T03:00:00Z")  # epoch + 3 h
ORBITS: dict[str, Any] = {
    "sat1": ob.GeoOrbit(-177.0, 5.0, phase_deg=40.0),
    "sat2": ob.GeoOrbit(172.0, 4.0, phase_deg=200.0),
    "sat3": ob.GeoOrbit(75.0, 5.0, phase_deg=310.0),
}
ORBITS_EQ: dict[str, Any] = {"sat1": ob.GeoOrbit(-177.0), "sat2": ob.GeoOrbit(172.0), "sat3": ob.GeoOrbit(75.0)}
SITES: dict[str, np.ndarray] = {
    "hnl": lla_to_ecef(21.31, -157.86, 15.0),
    "tyo": lla_to_ecef(35.68, 139.77, 50.0),
    "mnl": lla_to_ecef(14.60, 120.98, 20.0),
}
F_E = 300e6
PATH1 = PathSpec("sat1", "hnl", F_E, -41.0e6)
PATH2 = PathSpec("sat2", "tyo", F_E, -33.6e6)
PATH3 = PathSpec("sat3", "mnl", F_E, -41.0e6)
PAIRS = ((PATH1, PATH2), (PATH1, PATH3))
LAT, LON = 30.37, 140.41
SIGMA_T, SIGMA_F = 1e-6, 0.05

FULL_UNCERTAINTY = Uncertainty(
    sat_pos_ric_m={s: np.array([100.0, 1000.0, 1000.0]) for s in ORBITS},
    sat_vel_ric_mps={s: np.array([0.02, 0.02, 0.05]) for s in ORBITS},
    site_clock_s={g: 30e-9 for g in SITES},
    site_osc_frac={g: 1e-11 for g in SITES},
    sat_osc_frac={s: 1e-6 for s in ORBITS},
    tdoa_bias_s=50e-9,
)


def truth_rows(
    lat: float = LAT,
    lon: float = LON,
    orbits: dict[str, Any] = ORBITS,
    t_ns: int = T_NS,
    kinds: tuple[str, ...] = ("tdoa", "fdoa"),
    pairs: tuple[tuple[PathSpec, PathSpec], ...] = PAIRS,
    sigma_t: float = SIGMA_T,
    sigma_f: float = SIGMA_F,
    height: float = 0.0,
) -> list[Measurement]:
    """Noise-free rows for an emitter at (lat, lon, height), from scalar predictor calls."""
    x = lla_to_ecef(lat, lon, height)
    rows = []
    for a, b in pairs:
        if "tdoa" in kinds:
            rows.append(Measurement("tdoa", float(gm.predict_tdoa(x, a, b, sec(t_ns), orbits, SITES)), sigma_t, t_ns, a, b))
        if "fdoa" in kinds:
            rows.append(Measurement("fdoa", float(gm.predict_fdoa(x, a, b, sec(t_ns), orbits, SITES)), sigma_f, t_ns, a, b))
    return rows


def with_noise(rows: list[Measurement], noise: np.ndarray) -> list[Measurement]:
    return [
        Measurement(r.kind, r.value + float(n), r.sigma, r.t_ns, r.a, r.b, r.correction, r.cal, r.tag)
        for r, n in zip(rows, noise)
    ]


def miss_m(sol: sv.Solution, lat: float = LAT, lon: float = LON) -> float:
    return float(geodesic_distance_m(sol.lat_deg, sol.lon_deg, lat, lon))


def en_error(sol: sv.Solution, lat: float = LAT, lon: float = LON, height: float = 0.0) -> np.ndarray:
    """Estimate minus truth in east/north metres (MATH.md section 9 definition)."""
    m_rad, n_rad = radii_of_curvature(lat)
    dlon = (sol.lon_deg - lon + 180.0) % 360.0 - 180.0
    return np.array(
        [(n_rad + height) * np.cos(np.radians(lat)) * np.radians(dlon), (m_rad + height) * np.radians(sol.lat_deg - lat)]
    )


def nees(e: np.ndarray, cov: np.ndarray) -> float:
    return float(e @ np.linalg.solve(cov, e))


def cov_close(got: np.ndarray, ref: np.ndarray, rel: float = 3e-3, scale: np.ndarray | None = None) -> bool:
    """Covariances agree element by element to `rel` of sqrt(P_ii P_jj) of the reference total.

    (The brute-force Jacobian carries the FDOA predictor's 1e-8 Hz rounding noise over a
    200 m baseline, 2e-4 of a geosynchronous FDOA gradient, so a small off-diagonal
    element cannot be compared relative to itself.)
    """
    d = np.sqrt(np.diag(ref if scale is None else scale))
    return bool(np.all(np.abs(np.asarray(got) - ref) <= rel * np.outer(d, d)))


# ----------------------------------------------------------------------------- brute-force reference


def _h(row_kind: str, a: PathSpec, b: PathSpec, t_ns: int, x: np.ndarray, orbits: dict[str, Any]) -> float:
    f = gm.predict_tdoa if row_kind == "tdoa" else gm.predict_fdoa
    return float(f(x, a, b, sec(t_ns), orbits, SITES))


def _row_partials(kind: str, a: PathSpec, b: PathSpec, t_ns: int, x: np.ndarray, orbits: dict[str, Any]) -> dict[tuple, float]:
    """Partials of one measurement with respect to every consider parameter (MATH.md section 9).

    Satellite RIC position and velocity by central differences through a
    ``PerturbedOrbit`` whose ``t_ref`` is this measurement's own epoch; clock, oscillator
    and bias partials as written in the document.
    """
    out: dict[tuple, float] = {}
    for sat in dict.fromkeys((a.sat_id, b.sat_id)):
        for c in range(3):
            for name, step, is_vel in (("pos", 500.0, False), ("vel", 0.5, True)):
                e = np.zeros(3)
                e[c] = step
                values = []
                for sign in (1.0, -1.0):
                    args = (np.zeros(3), sign * e) if is_vel else (sign * e, np.zeros(3))
                    shifted = dict(orbits)
                    shifted[sat] = ob.PerturbedOrbit(orbits[sat], *args, sec(t_ns))
                    values.append(_h(kind, a, b, t_ns, x, shifted))
                out[(name, sat, c)] = (values[0] - values[1]) / (2.0 * step)
    if kind == "tdoa":
        out[("clock", a.site_id)] = out.get(("clock", a.site_id), 0.0) - 1.0
        out[("clock", b.site_id)] = out.get(("clock", b.site_id), 0.0) + 1.0
        ends = ((a.sat_id, a.site_id), (b.sat_id, b.site_id))
        out[("bias",) + tuple(sorted(ends))] = 1.0 if ends[0] <= ends[1] else -1.0
    else:
        out[("site_osc", a.site_id)] = out.get(("site_osc", a.site_id), 0.0) - a.f_tune_hz
        out[("site_osc", b.site_id)] = out.get(("site_osc", b.site_id), 0.0) + b.f_tune_hz
        out[("sat_osc", a.sat_id)] = out.get(("sat_osc", a.sat_id), 0.0) + a.translation_hz
        out[("sat_osc", b.sat_id)] = out.get(("sat_osc", b.sat_id), 0.0) - b.translation_hz
    return out


def _variance(key: tuple, unc: Uncertainty | None) -> float:
    if unc is None:
        return 0.0
    if key[0] == "pos":
        return float(np.asarray(unc.sat_pos_ric_m.get(key[1], np.zeros(3)))[key[2]]) ** 2
    if key[0] == "vel":
        return float(np.asarray(unc.sat_vel_ric_mps.get(key[1], np.zeros(3)))[key[2]]) ** 2
    if key[0] == "clock":
        return float(unc.site_clock_s.get(key[1], 0.0)) ** 2
    if key[0] == "site_osc":
        return float(unc.site_osc_frac.get(key[1], 0.0)) ** 2
    if key[0] == "sat_osc":
        return float(unc.sat_osc_frac.get(key[1], 0.0)) ** 2
    return float(unc.tdoa_bias_s) ** 2  # bias


CATEGORY = {
    "pos": "ephemeris_position",
    "vel": "ephemeris_velocity",
    "clock": "clock",
    "site_osc": "oscillator",
    "sat_osc": "oscillator",
    "bias": "tdoa_bias",
    "ref": "reference",
}


def brute_force(
    rows: list[Measurement],
    lat: float,
    lon: float,
    unc: Uncertainty | None,
    orbits: dict[str, Any] = ORBITS,
    cal_sign: float = 1.0,
) -> dict[str, Any]:
    """Dense J, S, covariances and whitened residuals at (lat, lon, 0), row by row."""
    m = len(rows)
    x = lla_to_ecef(lat, lon, 0.0)
    jac = np.zeros((m, 2))
    columns: dict[tuple, np.ndarray] = {}
    variances: dict[tuple, float] = {}
    residual = np.zeros(m)
    for k, row in enumerate(rows):
        residual[k] = row.value - row.correction - _h(row.kind, row.a, row.b, row.t_ns, x, orbits)
        for col, bearing in ((0, 90.0), (1, 0.0)):  # 100 m east, then 100 m north, along geodesics
            plus = lla_to_ecef(*destination(lat, lon, bearing, 100.0), 0.0)
            minus = lla_to_ecef(*destination(lat, lon, bearing, -100.0), 0.0)
            jac[k, col] = (
                _h(row.kind, row.a, row.b, row.t_ns, plus, orbits) - _h(row.kind, row.a, row.b, row.t_ns, minus, orbits)
            ) / 200.0
        partials = _row_partials(row.kind, row.a, row.b, row.t_ns, x, orbits)
        if row.cal is not None:
            cal = row.cal
            for key, value in _row_partials(row.kind, cal.a, cal.b, cal.t_ns, np.asarray(cal.x_ref), orbits).items():
                partials[key] = partials.get(key, 0.0) - cal_sign * value
            partials[("ref", cal.cal_id, row.kind)] = cal_sign
            variances[("ref", cal.cal_id, row.kind)] = cal.sigma**2
        for key, value in partials.items():
            columns.setdefault(key, np.zeros(m))[k] = value
            if key[0] != "ref":
                variances[key] = _variance(key, unc)
    r_mat = np.diag([r.sigma**2 for r in rows])
    s_mat = r_mat.copy()
    for key, column in columns.items():
        s_mat += variances[key] * np.outer(column, column)
    s_inv = np.linalg.inv(s_mat)
    total = np.linalg.inv(jac.T @ s_inv @ jac)
    gain = total @ jac.T @ s_inv
    budget = dict.fromkeys(sv.ERROR_SOURCES, 0.0)
    budget["noise"] = float(np.trace(gain @ r_mat @ gain.T))
    for key, column in columns.items():
        budget[CATEGORY[key[0]]] += variances[key] * float((gain @ column) @ (gain @ column))
    return {
        "J": jac,
        "S": s_mat,
        "total": total,
        "noise": gain @ r_mat @ gain.T,
        "consider": gain @ (s_mat - r_mat) @ gain.T,
        "budget": budget,
        "whitened": np.linalg.solve(np.linalg.cholesky(s_mat), residual),
        "chi2": float(residual @ s_inv @ residual),
    }


# ----------------------------------------------------------------------------- API surface


def test_reexports_the_shared_types() -> None:
    assert sv.CalRef is core_types.CalRef
    assert sv.Measurement is core_types.Measurement
    assert sv.Uncertainty is core_types.Uncertainty
    for name in ("Solution", "solve", "calibration_correction", "error_ellipse", "cep50"):
        assert name in sv.__all__


# ----------------------------------------------------------------------------- MATH 12.8


def test_math12_8a_noise_free_solution() -> None:
    sol, reason = sv.solve(truth_rows(), ORBITS, SITES, CFG, Region(), uncertainty=None)
    assert reason is None and sol is not None
    assert miss_m(sol) < 1.0
    assert sol.chi2 < 1e-6
    assert sol.ambiguities == []
    assert sol.ambiguity_resolved_by != "unresolved"
    assert sol.converged and sol.n_measurements == 4 and sol.dof == 2
    assert sol.reduced_chi2 == pytest.approx(sol.chi2 / 2.0)
    assert sol.height_m == 0.0 and -180.0 <= sol.lon_deg < 180.0
    assert sol.residuals.shape == (4,) and np.all(np.abs(sol.residuals) < 1e-3)
    assert sol.dominant_error == "noise"
    assert np.allclose(sol.cov_consider_en_m2, 0.0) and np.allclose(sol.cov_en_m2, sol.cov_noise_en_m2)


def test_math12_8a_at_a_nonzero_solver_height() -> None:
    cfg = make_cfg(height_m=850.0)
    sol, reason = sv.solve(truth_rows(height=850.0), ORBITS, SITES, cfg, Region())
    assert reason is None and sol is not None
    assert miss_m(sol) < 1.0 and sol.chi2 < 1e-6 and sol.height_m == 850.0
    # solved at the wrong height the same rows land somewhere else (the height matters)
    wrong, _ = sv.solve(truth_rows(height=850.0), ORBITS, SITES, CFG, Region())
    assert wrong is not None and miss_m(wrong) > 100.0


@pytest.mark.slow
def test_math12_8b_monte_carlo_nees_and_chi2() -> None:
    """500 seeded noise trials; every trial goes through solve().

    Trials are warm-started from the noise-free position the global search returns
    (the pipeline's normal mode); the first 25 are also solved with the global search
    and must give the same point.
    """
    truth = truth_rows()
    sigma = np.array([r.sigma for r in truth])
    rng = np.random.default_rng(3)
    start, _ = sv.solve(truth, ORBITS, SITES, CFG, Region())
    assert start is not None
    values_nees, values_chi2 = [], []
    for trial in range(500):
        rows = with_noise(truth, rng.standard_normal(4) * sigma)
        sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=None, starts=[(start.lat_deg, start.lon_deg)])
        assert sol is not None, reason
        if trial < 25:
            cold, _ = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=None)
            assert cold is not None and miss_m(cold, sol.lat_deg, sol.lon_deg) < 0.5
            assert cold.chi2 == pytest.approx(sol.chi2, abs=1e-5)
            assert cold.ambiguities == []
        values_nees.append(nees(en_error(sol), sol.cov_noise_en_m2))
        values_chi2.append(sol.chi2)
        assert sol.dof == 2 and sol.reduced_chi2 == pytest.approx(sol.chi2 / 2.0)
    values = np.array(values_nees)
    assert 1.73 <= values.mean() <= 2.27
    assert 0.92 <= np.mean(values <= 5.991) <= 0.98
    assert 1.73 <= np.mean(values_chi2) <= 2.27


# ----------------------------------------------------------------------------- MATH 12.9


def _points(sol: sv.Solution) -> list[tuple[float, float, float]]:
    return [(s.lat_deg, s.lon_deg, s.chi2) for s in [sol] + sol.ambiguities]


@pytest.mark.parametrize("kinds", [("tdoa",), ("tdoa", "fdoa")], ids=["two-tdoa-rows", "plus-two-fdoa-rows"])
def test_math12_9_equatorial_mirror_is_unresolved(kinds: tuple[str, ...]) -> None:
    rows = truth_rows(30.0, 140.0, ORBITS_EQ, kinds=kinds)
    sol, reason = sv.solve(rows, ORBITS_EQ, SITES, CFG, Region())
    assert reason is None and sol is not None
    points = _points(sol)
    assert len(points) == 2
    north = [p for p in points if p[0] > 0]
    south = [p for p in points if p[0] < 0]
    assert len(north) == 1 and len(south) == 1
    assert geodesic_distance_m(north[0][0], north[0][1], 30.0, 140.0) < 1.0
    assert geodesic_distance_m(south[0][0], south[0][1], -30.0, 140.0) < 1.0
    assert abs(points[0][2] - points[1][2]) < 1e-6
    assert sol.ambiguity_resolved_by == "unresolved"
    # ties are ordered by descending latitude: the northern point is the solution
    assert sol.lat_deg > 0 and sol.ambiguities[0].lat_deg < 0
    assert sol.ambiguities[0].ambiguities == [] and sol.ambiguities[0].ambiguity_resolved_by == "unresolved"
    # the ambiguity is a full solution: by symmetry its ellipse is the mirror image
    assert sol.ambiguities[0].ellipse["semi_major_m"] == pytest.approx(sol.ellipse["semi_major_m"], rel=1e-6)
    assert sol.ambiguities[0].ellipse["azimuth_deg"] == pytest.approx(180.0 - sol.ellipse["azimuth_deg"], abs=1e-4)


def test_math12_9_fdoa_rows_change_nothing() -> None:
    tdoa_only, _ = sv.solve(truth_rows(30.0, 140.0, ORBITS_EQ, kinds=("tdoa",)), ORBITS_EQ, SITES, CFG, Region())
    both, _ = sv.solve(truth_rows(30.0, 140.0, ORBITS_EQ), ORBITS_EQ, SITES, CFG, Region())
    assert tdoa_only is not None and both is not None
    # zero-velocity satellites: the FDOA rows are identically zero and carry no position information
    assert all(r.value == 0.0 for r in truth_rows(30.0, 140.0, ORBITS_EQ, kinds=("fdoa",)))
    for a, b in zip(_points(tdoa_only), _points(both)):
        assert geodesic_distance_m(a[0], a[1], b[0], b[1]) < 1e-3
    assert np.allclose(tdoa_only.cov_en_m2, both.cov_en_m2, rtol=1e-9)
    assert both.ambiguity_resolved_by == "unresolved" and both.dof == 2


def test_math12_9_region_resolves_the_mirror() -> None:
    rows = truth_rows(30.0, 140.0, ORBITS_EQ, kinds=("tdoa",))
    sol, reason = sv.solve(rows, ORBITS_EQ, SITES, CFG, Region(lat_min=0.0))
    assert reason is None and sol is not None
    assert sol.ambiguities == []
    assert geodesic_distance_m(sol.lat_deg, sol.lon_deg, 30.0, 140.0) < 1.0
    assert sol.ambiguity_resolved_by == "region"
    # the southern half alone returns the other point
    south, _ = sv.solve(rows, ORBITS_EQ, SITES, CFG, Region(lat_max=0.0))
    assert south is not None and south.ambiguities == [] and south.ambiguity_resolved_by == "region"
    assert geodesic_distance_m(south.lat_deg, south.lon_deg, -30.0, 140.0) < 1.0


# ----------------------------------------------------------------------------- MATH 12.10

# One TDOA and both FDOAs: the position needs the FDOA rows, so a satellite oscillator
# bias of a few FDOA sigmas moves the fix and must show up in P_total.
OSC_FRAC = 1e-9
OSC_UNCERTAINTY = Uncertainty(sat_osc_frac={s: OSC_FRAC for s in ORBITS})


def _osc_rows() -> list[Measurement]:
    full = truth_rows(sigma_f=0.005)
    return [full[0], full[1], full[3]]


@pytest.mark.slow
def test_math12_10_consider_covariance_monte_carlo() -> None:
    truth = _osc_rows()
    rng = np.random.default_rng(1)
    total, noise_only = [], []
    n_trials = 300
    for _ in range(n_trials):
        eps = {s: rng.standard_normal() * OSC_FRAC for s in ORBITS}
        rows = []
        for r in truth:
            value = r.value + rng.standard_normal() * r.sigma
            if r.kind == "fdoa":  # MATH 12.6: raising eps of satellite i raises FDOA_AB by eps * T_nom,i
                value += eps[r.a.sat_id] * r.a.translation_hz - eps[r.b.sat_id] * r.b.translation_hz
            rows.append(Measurement(r.kind, value, r.sigma, r.t_ns, r.a, r.b))
        sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=OSC_UNCERTAINTY, starts=[(LAT, LON)])
        assert sol is not None, reason
        e = en_error(sol)
        total.append(nees(e, sol.cov_en_m2))
        noise_only.append(nees(e, sol.cov_noise_en_m2))
        assert sol.dominant_error == "oscillator"
        assert np.allclose(sol.cov_noise_en_m2 + sol.cov_consider_en_m2, sol.cov_en_m2, rtol=1e-9)
    assert abs(np.mean(total) - 2.0) < 3.0 * 2.0 / np.sqrt(n_trials)  # chi-square(2): sd 2
    assert np.mean(noise_only) > 20.0  # "far larger"
    # what the noise-only NEES should average if the error really is N(0, P_total)
    expected = float(np.trace(np.linalg.solve(sol.cov_noise_en_m2, sol.cov_en_m2)))
    assert np.mean(noise_only) == pytest.approx(expected, rel=0.3)


def test_consider_covariance_matches_dense_algebra_for_oscillator_bias() -> None:
    rows = with_noise(_osc_rows(), np.array([0.4e-6, -0.003, 0.004]))
    sol, _ = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=OSC_UNCERTAINTY, starts=[(LAT, LON)])
    assert sol is not None
    ref = brute_force(rows, sol.lat_deg, sol.lon_deg, OSC_UNCERTAINTY)
    assert cov_close(sol.cov_en_m2, ref["total"])
    assert cov_close(sol.cov_noise_en_m2, ref["noise"], scale=ref["total"])
    assert cov_close(sol.cov_consider_en_m2, ref["consider"], scale=ref["total"])
    assert sol.chi2 == pytest.approx(ref["chi2"], rel=1e-6)
    assert np.allclose(sol.residuals, ref["whitened"], rtol=1e-5, atol=1e-7)  # L^-1 r, Cholesky, input order
    assert sol.dof == 1 and sol.reduced_chi2 == pytest.approx(sol.chi2)


def test_total_covariance_weighting_keeps_a_biased_fdoa_row_from_spoiling_the_fix() -> None:
    """MATH.md section 9: noise-only weights let an uncalibrated oscillator offset drag the fix."""
    full = truth_rows()
    offset_hz = 1e-6 * 41.0e6  # satellite 1 oscillator off by its 1-sigma: 41 Hz on the FDOA row
    rows = [full[0], full[2], Measurement("fdoa", full[1].value + offset_hz, SIGMA_F, T_NS, PATH1, PATH2)]
    naive, _ = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=None)
    weighted, _ = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=Uncertainty(sat_osc_frac={s: 1e-6 for s in ORBITS}))
    tdoa_only, _ = sv.solve([full[0], full[2]], ORBITS, SITES, CFG, Region())
    assert naive is not None and weighted is not None and tdoa_only is not None
    assert miss_m(naive) > 20e3 and naive.chi2 > 1e4
    assert miss_m(weighted) < 5.0 and weighted.chi2 < 9.0
    assert miss_m(tdoa_only) < 1.0
    # with the oscillator sigma the FDOA row is known to be nearly useless: same ellipse as TDOA alone
    assert weighted.ellipse["semi_major_m"] == pytest.approx(tdoa_only.ellipse["semi_major_m"], rel=0.01)


# ----------------------------------------------------------------------------- row mixes


def test_fdoa_only_two_rows() -> None:
    rows = truth_rows(kinds=("fdoa",))
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region())
    assert reason is None and sol is not None
    assert miss_m(sol) < 1.0 and sol.chi2 < 1e-6
    assert sol.dof == 0 and sol.reduced_chi2 is None and sol.n_measurements == 2
    ref = brute_force(rows, sol.lat_deg, sol.lon_deg, None)
    assert cov_close(sol.cov_en_m2, ref["total"])


def test_fdoa_only_several_epochs() -> None:
    rows = [r for hours in (0, 2, 4) for r in truth_rows(kinds=("fdoa",), t_ns=T_NS + int(hours * 3600e9))]
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region())
    assert reason is None and sol is not None
    assert miss_m(sol) < 1.0 and sol.chi2 < 1e-6 and sol.dof == 4
    ref = brute_force(rows, sol.lat_deg, sol.lon_deg, None)
    assert cov_close(sol.cov_en_m2, ref["total"])
    # more epochs, tighter fix than one epoch
    single, _ = sv.solve(rows[:2], ORBITS, SITES, CFG, Region())
    assert single is not None and sol.ellipse["semi_major_m"] < single.ellipse["semi_major_m"]


@pytest.mark.parametrize(
    "picks",
    [(0, 3), (0, 1), (0, 2), (1, 2)],
    ids=["tdoa12+fdoa13", "tdoa12+fdoa12", "tdoa12+tdoa13", "fdoa12+tdoa13"],
)
def test_exactly_two_rows_mixed_kinds(picks: tuple[int, int]) -> None:
    full = truth_rows()
    rows = [full[k] for k in picks]
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region())
    assert reason is None and sol is not None
    assert miss_m(sol) < 1.0 and sol.chi2 < 1e-6
    assert sol.dof == 0 and sol.reduced_chi2 is None and sol.n_measurements == 2
    # m = 2: every admissible intersection has chi2 = 0 and all are returned
    for other in sol.ambiguities:
        assert other.chi2 < 1e-6 and other.reduced_chi2 is None
        x = lla_to_ecef(other.lat_deg, other.lon_deg, 0.0)
        for r in rows:
            assert _h(r.kind, r.a, r.b, r.t_ns, x, ORBITS) == pytest.approx(r.value, abs=1e-3 * r.sigma)


def test_mixed_rows_over_epochs_with_noise_agree_with_dense_algebra() -> None:
    rng = np.random.default_rng(11)
    truth = [r for k in range(3) for r in truth_rows(t_ns=T_NS + int(k * 5e9))]
    rows = with_noise(truth, rng.standard_normal(len(truth)) * np.array([r.sigma for r in truth]))
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region())
    assert reason is None and sol is not None and sol.dof == 10
    ref = brute_force(rows, sol.lat_deg, sol.lon_deg, None)
    assert cov_close(sol.cov_en_m2, ref["total"])
    assert sol.chi2 == pytest.approx(ref["chi2"], rel=1e-6)
    assert np.allclose(sol.residuals, ref["whitened"], rtol=1e-5, atol=1e-7)
    assert sol.reduced_chi2 == pytest.approx(ref["chi2"] / 10.0, rel=1e-6)
    # the minimum is a stationary point of the dense cost: J^T S^-1 r = 0 (in units of the 1-sigma step)
    gradient = ref["J"].T @ np.linalg.solve(ref["S"], np.array([r.value for r in rows]) - np.array(
        [_h(r.kind, r.a, r.b, r.t_ns, lla_to_ecef(sol.lat_deg, sol.lon_deg, 0.0), ORBITS) for r in rows]))
    assert np.all(np.abs(ref["total"] @ gradient) < 1e-3 * np.sqrt(np.diag(ref["total"])))


def test_fewer_than_two_rows_raises() -> None:
    with pytest.raises(ValueError, match="measurements"):
        sv.solve(truth_rows()[:1], ORBITS, SITES, CFG, Region())
    with pytest.raises(ValueError, match="measurements"):
        sv.solve([], ORBITS, SITES, CFG, Region())


# ----------------------------------------------------------------------------- low Earth orbit

T_LEO_NS = ns_from_iso("2026-11-13T07:00:00Z")
LEO_ORBITS: dict[str, Any] = {
    "leo1": ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 10.0, epoch="2026-11-13T00:00:00Z"),
    "leo2": ob.KeplerOrbit(7.0e6, 0.001, 98.0, 40.0, 30.0, 12.5, epoch="2026-11-13T00:00:00Z"),
}
_LEO_LAT, _LEO_LON, _ = (float(v) for v in ob.subsatellite_point(LEO_ORBITS["leo1"], sec(T_LEO_NS)))
LEO_EMITTER = (_LEO_LAT + 3.0, _LEO_LON - 4.0)
SITES["leo-gs"] = lla_to_ecef(_LEO_LAT - 2.0, _LEO_LON + 5.0, 30.0)
LEO_A = PathSpec("leo1", "leo-gs", 400e6, -20e6)
LEO_B = PathSpec("leo2", "leo-gs", 400e6, -20e6)


def _leo_rows(offsets_s: tuple[float, ...]) -> list[Measurement]:
    rows = []
    for dt in offsets_s:
        rows += truth_rows(*LEO_EMITTER, orbits=LEO_ORBITS, t_ns=T_LEO_NS + int(dt * 1e9), pairs=((LEO_A, LEO_B),), sigma_t=1e-7, sigma_f=1.0)
    return rows


def test_low_orbit_pair_single_epoch_is_ambiguous_and_more_epochs_resolve_it() -> None:
    """Two satellites 2.5 degrees apart on one 630 km orbit: kHz of FDOA, fast geometry."""
    region = Region(_LEO_LAT - 20.0, _LEO_LAT + 20.0, _LEO_LON - 25.0, _LEO_LON + 25.0)
    rows = _leo_rows((0.0,))
    assert abs(rows[1].value) > 1e3  # FDOA of several kHz
    sol, reason = sv.solve(rows, LEO_ORBITS, SITES, CFG, region)
    assert reason is None and sol is not None
    points = [sol] + sol.ambiguities
    assert len(points) == 2 and sol.ambiguity_resolved_by == "unresolved"  # the two crossings of the TDOA and FDOA lines
    assert min(miss_m(p, *LEO_EMITTER) for p in points) < 1.0
    for p in points:
        x = lla_to_ecef(p.lat_deg, p.lon_deg, 0.0)
        for r in rows:
            assert _h(r.kind, r.a, r.b, r.t_ns, x, LEO_ORBITS) == pytest.approx(r.value, abs=1e-3 * r.sigma)
        assert min(float(gm.elevation_deg(x, LEO_ORBITS[s], sec(T_LEO_NS))) for s in LEO_ORBITS) > 0.0
    # 20 seconds of track: the wrong crossing no longer fits
    rows = _leo_rows((0.0, 10.0, 20.0))
    sol, reason = sv.solve(rows, LEO_ORBITS, SITES, CFG, region)
    assert reason is None and sol is not None
    assert miss_m(sol, *LEO_EMITTER) < 1.0 and sol.chi2 < 1e-6
    assert sol.ambiguities == [] and sol.ambiguity_resolved_by == "measurements" and sol.dof == 4
    # the default region works as well (each satellite sees a small part of it)
    wide, _ = sv.solve(rows, LEO_ORBITS, SITES, CFG, Region())
    assert wide is not None and miss_m(wide, *LEO_EMITTER) < 1.0


def test_low_orbit_covariance_with_ephemeris_uncertainty_matches_dense_algebra() -> None:
    rows = _leo_rows((-30.0, 0.0, 30.0))
    unc = Uncertainty(
        sat_pos_ric_m={s: np.array([20.0, 100.0, 50.0]) for s in LEO_ORBITS},
        sat_vel_ric_mps={s: np.array([0.02, 0.02, 0.05]) for s in LEO_ORBITS},
        sat_osc_frac={s: 1e-9 for s in LEO_ORBITS},
        site_clock_s={"leo-gs": 1e-6},  # one site on both paths: cancels
    )
    region = Region(_LEO_LAT - 20.0, _LEO_LAT + 20.0, _LEO_LON - 25.0, _LEO_LON + 25.0)
    sol, reason = sv.solve(rows, LEO_ORBITS, SITES, CFG, region, uncertainty=unc)
    assert reason is None and sol is not None and miss_m(sol, *LEO_EMITTER) < 1.0
    ref = brute_force(rows, sol.lat_deg, sol.lon_deg, unc, orbits=LEO_ORBITS)
    assert cov_close(sol.cov_en_m2, ref["total"], rel=1e-2)
    assert cov_close(sol.cov_consider_en_m2, ref["consider"], rel=1e-2, scale=ref["total"])
    assert sol.dominant_error == max(ref["budget"], key=ref["budget"].get)
    assert ref["budget"]["clock"] == 0.0 and ref["budget"]["ephemeris_position"] > 0.0
    plain, _ = sv.solve(rows, LEO_ORBITS, SITES, CFG, region)
    assert plain is not None and sol.ellipse["semi_major_m"] > 1.5 * plain.ellipse["semi_major_m"]


# ----------------------------------------------------------------------------- observability


def test_unobservable_two_tdoa_rows_of_one_pair_at_one_epoch() -> None:
    row = truth_rows(kinds=("tdoa",))[0]
    twin = Measurement("tdoa", row.value, 2e-6, row.t_ns, row.a, row.b)
    for rows in ([row, row], [row, twin]):
        sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region())
        assert sol is None and reason == "unobservable"
        assert sv.position_covariance(rows, LAT, LON, ORBITS, SITES) is None


def test_unobservable_fdoa_rows_of_zero_velocity_geostationary_satellites() -> None:
    rows = truth_rows(30.0, 140.0, ORBITS_EQ, kinds=("fdoa",))
    sol, reason = sv.solve(rows, ORBITS_EQ, SITES, CFG, Region())
    assert sol is None and reason == "unobservable"
    sol, reason = sv.solve(rows, ORBITS_EQ, SITES, CFG, Region(), starts=[(30.0, 140.0)])
    assert sol is None and reason == "unobservable"
    assert sv.position_covariance(rows, 30.0, 140.0, ORBITS_EQ, SITES) is None


def test_unobservable_when_the_only_fdoa_row_is_swamped_by_oscillator_uncertainty() -> None:
    """One pair, TDOA + FDOA, uncalibrated 1e-6 oscillators: the 1-sigma axis exceeds 1e7 m."""
    rows = truth_rows(pairs=((PATH1, PATH2),))
    plain, _ = sv.solve(rows, ORBITS, SITES, CFG, Region())
    assert plain is not None and miss_m(plain) < 1.0
    t0 = time.perf_counter()
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=FULL_UNCERTAINTY)
    elapsed = time.perf_counter() - t0
    assert sol is None and reason == "unobservable"
    assert elapsed < 2.0  # abandoned early instead of creeping along the flat valley (0.15 s unloaded)
    ref = brute_force(rows, LAT, LON, FULL_UNCERTAINTY)
    assert np.sqrt(np.linalg.eigvalsh(ref["total"])[-1]) > 1e7


def test_no_admissible_region_and_no_admissible_solution() -> None:
    rows = truth_rows()
    # satellite 3 is 12.3 degrees above the emitter's horizon
    x = lla_to_ecef(LAT, LON, 0.0)
    assert float(gm.elevation_deg(x, ORBITS["sat3"], sec(T_NS))) == pytest.approx(12.34, abs=0.01)
    ok, _ = sv.solve(rows, ORBITS, SITES, make_cfg(min_elevation_deg=12.0), Region())
    assert ok is not None and miss_m(ok) < 1.0
    sol, reason = sv.solve(rows, ORBITS, SITES, make_cfg(min_elevation_deg=13.0), Region())
    assert sol is None and reason == "no admissible solution"
    sol, reason = sv.solve(rows, ORBITS, SITES, make_cfg(min_elevation_deg=13.0), Region(), starts=[(LAT, LON)])
    assert sol is None and reason == "no admissible solution"
    # a region none of whose cells sees all three satellites
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region(lat_min=-10.0, lat_max=10.0, lon_min=-60.0, lon_max=-20.0))
    assert sol is None and reason == "no admissible region"
    # the true position outside the region of interest
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region(lat_min=35.0, lat_max=60.0, lon_min=120.0, lon_max=160.0))
    assert sol is None or miss_m(sol) > 100e3


# ----------------------------------------------------------------------------- start procedure


def test_warm_start_skips_the_grid_and_finds_the_same_fix() -> None:
    rng = np.random.default_rng(5)
    truth = truth_rows()
    rows = with_noise(truth, rng.standard_normal(4) * np.array([r.sigma for r in truth]))
    region = Region()
    cold, _ = sv.solve(rows, ORBITS, SITES, CFG, region)
    assert cold is not None and region.grid_calls == 1
    region = Region()
    warm, _ = sv.solve(rows, ORBITS, SITES, CFG, region, starts=[(LAT + 0.3, LON - 0.4)])
    assert warm is not None and region.grid_calls == 0
    assert miss_m(warm, cold.lat_deg, cold.lon_deg) < 0.5
    assert warm.chi2 == pytest.approx(cold.chi2, abs=1e-5)
    assert np.allclose(warm.cov_en_m2, cold.cov_en_m2, rtol=1e-5)


def test_warm_start_from_the_mirror_recovers_both_equatorial_solutions() -> None:
    rows = truth_rows(30.0, 140.0, ORBITS_EQ, kinds=("tdoa",))
    sol, _ = sv.solve(rows, ORBITS_EQ, SITES, CFG, Region(), starts=[(-29.0, 141.0)])
    assert sol is not None
    points = sorted(_points(sol))
    assert len(points) == 2 and sol.ambiguity_resolved_by == "unresolved"
    assert geodesic_distance_m(points[0][0], points[0][1], -30.0, 140.0) < 1.0
    assert geodesic_distance_m(points[1][0], points[1][1], 30.0, 140.0) < 1.0
    assert sol.lat_deg > 0  # tie on chi2: descending latitude, whatever the start


def test_ambiguity_resolved_by_measurements_and_none() -> None:
    rows = truth_rows()
    cold, _ = sv.solve(rows, ORBITS, SITES, CFG, Region())
    assert cold is not None and cold.ambiguity_resolved_by == "measurements"  # a far minimum with chi2 ~ 1e6 exists
    warm, _ = sv.solve(rows, ORBITS, SITES, CFG, Region(), starts=[(LAT, LON)])
    assert warm is not None and warm.ambiguity_resolved_by == "none"  # start and mirror both lead to the fix
    # a tight chi2 gate does not turn the far minimum into an ambiguity; a loose one would list it if admissible
    loose, _ = sv.solve(rows, ORBITS, SITES, make_cfg(chi2_ambiguity=1e9), Region())
    assert loose is not None and loose.ambiguities == [] and loose.ambiguity_resolved_by == "region"


def test_chi2_ambiguity_gate_lists_or_drops_a_worse_minimum() -> None:
    """Exact equatorial TDOA mirror plus one FDOA row through an inclined satellite.

    The TDOA rows pin both mirror points to a few hundred metres, so the chi2 of the
    southern one is the squared FDOA misfit there, ((f_north - f_south) / sigma)^2,
    which the test sets by choosing sigma.
    """
    orbits = dict(ORBITS_EQ)
    orbits["sat4"] = ob.GeoOrbit(150.0, 5.0, phase_deg=310.0)
    path4 = PathSpec("sat4", "tyo", F_E, -33.6e6)
    tdoa = truth_rows(30.0, 140.0, orbits, kinds=("tdoa",))
    f_north = _h("fdoa", PATH1, path4, T_NS, lla_to_ecef(30.0, 140.0, 0.0), orbits)
    f_south = _h("fdoa", PATH1, path4, T_NS, lla_to_ecef(-30.0, 140.0, 0.0), orbits)
    assert abs(f_north - f_south) > 0.1  # the inclined satellite tells the hemispheres apart

    def rows_with(misfit_sigmas: float) -> list[Measurement]:
        return tdoa + [Measurement("fdoa", f_north, abs(f_north - f_south) / misfit_sigmas, T_NS, PATH1, path4)]

    # mirror 2 sigma off: chi2 = 4, inside the default gate of 9
    sol, _ = sv.solve(rows_with(2.0), orbits, SITES, CFG, Region())
    assert sol is not None and geodesic_distance_m(sol.lat_deg, sol.lon_deg, 30.0, 140.0) < 1.0
    assert sol.chi2 < 1e-6 and sol.dof == 1
    assert len(sol.ambiguities) == 1 and sol.ambiguity_resolved_by == "unresolved"
    mirror = sol.ambiguities[0]
    assert mirror.chi2 == pytest.approx(4.0, rel=1e-2) and mirror.reduced_chi2 == pytest.approx(mirror.chi2)
    assert geodesic_distance_m(mirror.lat_deg, mirror.lon_deg, -30.0, 140.0) < 2000.0
    # the same rows with a gate of 3: the mirror is a second minimum excluded by the measurements
    sol, _ = sv.solve(rows_with(2.0), orbits, SITES, make_cfg(chi2_ambiguity=3.0), Region())
    assert sol is not None and sol.ambiguities == [] and sol.ambiguity_resolved_by == "measurements"
    # mirror 5 sigma off: chi2 = 25, outside the default gate
    sol, _ = sv.solve(rows_with(5.0), orbits, SITES, CFG, Region())
    assert sol is not None and sol.ambiguities == [] and sol.ambiguity_resolved_by == "measurements"
    assert geodesic_distance_m(sol.lat_deg, sol.lon_deg, 30.0, 140.0) < 1.0
    sol, _ = sv.solve(rows_with(5.0), orbits, SITES, make_cfg(chi2_ambiguity=30.0), Region())
    assert sol is not None and len(sol.ambiguities) == 1 and sol.ambiguities[0].chi2 == pytest.approx(25.0, rel=1e-2)
    # ... and with the southern hemisphere outside the region, the 2-sigma mirror is resolved by the region
    sol, _ = sv.solve(rows_with(2.0), orbits, SITES, CFG, Region(lat_min=0.0))
    assert sol is not None and sol.ambiguities == [] and sol.ambiguity_resolved_by == "region"
    # while the 5-sigma mirror is excluded by the measurements wherever the region ends
    sol, _ = sv.solve(rows_with(5.0), orbits, SITES, CFG, Region(lat_min=0.0))
    assert sol is not None and sol.ambiguities == [] and sol.ambiguity_resolved_by == "measurements"


def test_grid_uses_only_the_newest_two_rows_of_each_pair_and_kind() -> None:
    epochs = [T_NS + int(k * 5e9) for k in (3, 0, 2, 1)]
    rows = [r for t in epochs for r in truth_rows(t_ns=t)]  # 16 rows, 4 per (pair, kind), shuffled in time
    keep = sv._newest_two(rows)
    assert len(keep) == 8 and keep == sorted(keep)
    newest = {T_NS + int(3 * 5e9), T_NS + int(2 * 5e9)}
    assert {rows[k].t_ns for k in keep} == newest
    for a, b in PAIRS:
        for kind in ("tdoa", "fdoa"):
            assert sum(1 for k in keep if rows[k].kind == kind and rows[k].a == a and rows[k].b == b) == 2
    # corrupting an old row cannot move the grid starts (it is not used there) ...
    cfg = sv._SolverCfg.read(CFG)
    clean = sv._grid_starts(sv._Rows(rows, ORBITS, SITES, None), rows, ORBITS, SITES, None, cfg, Region())
    spoiled = list(rows)
    old = next(k for k, r in enumerate(rows) if r.t_ns == T_NS and r.kind == "tdoa")
    spoiled[old] = Measurement("tdoa", rows[old].value + 5e-3, SIGMA_T, rows[old].t_ns, rows[old].a, rows[old].b)
    assert sv._grid_starts(sv._Rows(spoiled, ORBITS, SITES, None), spoiled, ORBITS, SITES, None, cfg, Region()) == clean
    # ... while corrupting a newest row does
    new = next(k for k, r in enumerate(rows) if r.t_ns == T_NS + int(3 * 5e9) and r.kind == "tdoa")
    spoiled = list(rows)
    spoiled[new] = Measurement("tdoa", rows[new].value + 5e-3, SIGMA_T, rows[new].t_ns, rows[new].a, rows[new].b)
    assert sv._grid_starts(sv._Rows(spoiled, ORBITS, SITES, None), spoiled, ORBITS, SITES, None, cfg, Region()) != clean


def test_grid_starts_are_the_lowest_local_minima_of_a_brute_force_cost() -> None:
    rows = truth_rows()
    cfg = sv._SolverCfg.read(make_cfg(grid_step_deg=5.0, n_starts=2))
    region = Region()
    starts = sv._grid_starts(sv._Rows(rows, ORBITS, SITES, None), rows, ORBITS, SITES, None, cfg, region)
    lat, lon = region.grid(5.0)
    cost = np.full((lat.size, lon.size), np.inf)
    for i, la in enumerate(lat):  # brute force, cell by cell, through the public geometry functions
        for j, lo in enumerate(lon):
            x = lla_to_ecef(la, lo, 0.0)
            if min(float(gm.elevation_deg(x, ORBITS[s], sec(T_NS))) for s in ORBITS) < 0.0:
                continue
            cost[i, j] = sum(((r.value - _h(r.kind, r.a, r.b, r.t_ns, x, ORBITS)) / r.sigma) ** 2 for r in rows)
    minima = []
    for i in range(lat.size):
        for j in range(lon.size):
            if not np.isfinite(cost[i, j]):
                continue
            neighbours = [
                cost[i + di, (j + dj) % lon.size]
                for di in (-1, 0, 1)
                for dj in (-1, 0, 1)
                if (di or dj) and 0 <= i + di < lat.size
            ]
            if all(cost[i, j] <= c for c in neighbours):
                minima.append((cost[i, j], float(lat[i]), float((lon[j] + 180.0) % 360.0 - 180.0)))
    assert len(minima) > 2  # n_starts really truncates
    expected = [(la, lo) for _, la, lo in sorted(minima)[:2]]
    assert starts == expected
    assert starts[0] == (30.0, 140.0)


def test_local_minima_against_a_loop() -> None:
    rng = np.random.default_rng(2)
    for wrap in (False, True):
        cost = rng.integers(0, 6, size=(7, 9)).astype(float)  # plenty of ties
        cost[rng.random(cost.shape) < 0.2] = np.inf
        got = sv._local_minima(cost, wrap)
        for i in range(7):
            for j in range(9):
                neighbours = []
                for di in (-1, 0, 1):
                    for dj in (-1, 0, 1):
                        if not (di or dj) or not 0 <= i + di < 7:
                            continue
                        jj = j + dj
                        if wrap:
                            jj %= 9
                        elif not 0 <= jj < 9:
                            continue
                        neighbours.append(cost[i + di, jj])
                expected = bool(np.isfinite(cost[i, j]) and all(cost[i, j] <= c for c in neighbours))
                assert got[i, j] == expected


def test_above_mask_agrees_with_elevation_deg() -> None:
    rng = np.random.default_rng(4)
    lat = rng.uniform(-80.0, 80.0, 400)
    lon = rng.uniform(-180.0, 360.0, 400)
    x = lla_to_ecef(lat, lon, 120.0)
    r_sat = ob.orbit_position(ORBITS["sat2"], sec(T_NS))
    elevation = np.asarray(gm.elevation_deg(x, ORBITS["sat2"], sec(T_NS)))
    for mask in (-5.0, 0.0, 10.0, 35.0):
        clear = np.abs(elevation - mask) > 1e-6
        assert np.array_equal(sv.above_mask(lat, lon, x, r_sat, mask)[clear], (elevation >= mask)[clear])
    assert 50 < np.count_nonzero(elevation >= 10.0) < 350  # the comparison is not vacuous


def test_solution_near_the_antimeridian_and_wrapped_regions() -> None:
    pair = ((PATH1, PATH2),)
    for lat, lon in ((10.0, 179.95), (-12.0, -179.9), (25.0, -180.0)):
        rows = truth_rows(lat, lon, pairs=pair)
        for region in (Region(), Region(-60.0, 60.0, 60.0, -120.0), BareRegion(Region(-60.0, 60.0, 60.0, -120.0))):
            sol, reason = sv.solve(rows, ORBITS, SITES, CFG, region)
            assert sol is not None, reason
            assert miss_m(sol, lat, lon) < 1.0
            assert -180.0 <= sol.lon_deg < 180.0
            for other in sol.ambiguities:
                assert -180.0 <= other.lon_deg < 180.0
    # a region on the other side of the world excludes it
    sol, reason = sv.solve(truth_rows(10.0, 179.95, pairs=pair), ORBITS, SITES, CFG, Region(-60.0, 60.0, -100.0, 20.0))
    assert sol is None or miss_m(sol, 10.0, 179.95) > 1e6


def test_duplicates_within_one_kilometre_collapse_to_the_lower_cost_point() -> None:
    lat = [10.0, 10.005, 10.0, -30.0]
    lon = [20.0, 20.0, 20.02, 20.0]
    # 10.005 N is 553 m from the first point (duplicate); 0.02 deg of longitude is 2.2 km (distinct)
    assert geodesic_distance_m(lat[0], lon[0], lat[1], lon[1]) == pytest.approx(553.0, abs=1.0)
    assert geodesic_distance_m(lat[0], lon[0], lat[2], lon[2]) > 1000.0
    assert sv._distinct(lat, lon, [3.0, 1.0, 2.0, 5.0]) == [1, 2, 3]
    assert sv._distinct(lat, lon, [1.0, 3.0, 2.0, 0.5]) == [3, 0, 2]


def test_pole_fold_is_a_continuous_continuation() -> None:
    lat, lon = sv._fold(np.array([89.0, 91.0, -91.0, 45.0, 269.0]), np.array([10.0, 10.0, 10.0, 200.0, 0.0]))
    expected = [(89.0, 10.0), (89.0, 190.0), (-89.0, 190.0), (45.0, 200.0), (-89.0, 180.0)]
    for la, lo, (ela, elo) in zip(lat, lon, expected):
        assert np.allclose(lla_to_ecef(la, lo, 0.0), lla_to_ecef(ela, elo, 0.0), atol=1e-6)


# ----------------------------------------------------------------------------- calibration

X_REF = lla_to_ecef(13.44, 144.79, 30.0)  # reference carrier (generic city-centre coordinates)
F_REF = 300.5e6


def _ref_paths(a: PathSpec, b: PathSpec) -> tuple[PathSpec, PathSpec]:
    return (PathSpec(a.sat_id, a.site_id, F_REF, a.translation_hz), PathSpec(b.sat_id, b.site_id, F_REF, b.translation_hz))


def _ref_rows(orbits: dict[str, Any], t_ns: int, sigma_t: float = SIGMA_T, sigma_f: float = SIGMA_F) -> list[Measurement]:
    """Noise-free reference measurements on both pairs (TDOA, FDOA per pair), reference's own f_e."""
    rows = []
    for a, b in PAIRS:
        ra, rb = _ref_paths(a, b)
        rows.append(Measurement("tdoa", _h("tdoa", ra, rb, t_ns, X_REF, orbits), sigma_t, t_ns, ra, rb))
        rows.append(Measurement("fdoa", _h("fdoa", ra, rb, t_ns, X_REF, orbits), sigma_f, t_ns, ra, rb))
    return rows


def _calibrate(target: list[Measurement], ref: list[Measurement], prefix: str = "cal") -> list[Measurement]:
    """Attach to each target row the correction from the reference row of the same pair and kind."""
    out = []
    for row in target:
        j = next(k for k, r in enumerate(ref) if r.kind == row.kind and r.a.sat_id == row.a.sat_id and r.b.sat_id == row.b.sat_id)
        c, cal = sv.calibration_correction(ref[j], X_REF, ORBITS, SITES, f"{prefix}-{j}")
        out.append(Measurement(row.kind, row.value, row.sigma, row.t_ns, row.a, row.b, correction=c, cal=cal))
    return out


def test_calibration_correction_is_measured_minus_predicted() -> None:
    ref = _ref_rows(ORBITS, T_NS)[0]
    offset = 2.5e-7
    measured = Measurement("tdoa", ref.value + offset, 3e-7, ref.t_ns, ref.a, ref.b, tag="guam")
    c, cal = sv.calibration_correction(measured, X_REF, ORBITS, SITES, "cal-7")
    assert c == pytest.approx(offset, abs=1e-15)
    assert isinstance(cal, CalRef) and cal.cal_id == "cal-7" and cal.sigma == 3e-7 and cal.t_ns == ref.t_ns
    assert cal.a == ref.a and cal.b == ref.b and np.array_equal(cal.x_ref, X_REF)
    fdoa = _ref_rows(ORBITS, T_NS)[1]
    c, _ = sv.calibration_correction(Measurement("fdoa", fdoa.value - 0.3, 0.02, fdoa.t_ns, fdoa.a, fdoa.b), X_REF, ORBITS, SITES, "f")
    assert c == pytest.approx(-0.3, abs=1e-9)
    with pytest.raises(ValueError, match="ref_measurement"):
        sv.calibration_correction(Measurement("tdoa", 0.0, 1e-6, T_NS, ref.a, ref.b, correction=1e-9), X_REF, ORBITS, SITES, "x")
    with pytest.raises(ValueError, match="cal_id"):
        sv.calibration_correction(ref, X_REF, ORBITS, SITES, "")
    with pytest.raises(ValueError, match="x_ref"):
        sv.calibration_correction(ref, [1.0, 2.0], ORBITS, SITES, "x")


def test_calibration_cancels_clock_and_oscillator_offsets_exactly() -> None:
    """Truth errors common to target and reference (MATH 12.6 signs) vanish from calibrated rows."""
    clock = {"hnl": 180e-9, "tyo": -90e-9, "mnl": 40e-9}  # delta_g: lowers TDOA_AB by delta_a, raises it by delta_b
    eps_sat = {"sat1": 2e-7, "sat2": -3e-7, "sat3": 1e-7}  # raises FDOA_AB by eps_i T_i, lowers it by eps_j T_j

    def distort(row: Measurement) -> Measurement:
        if row.kind == "tdoa":
            shift = -clock[row.a.site_id] + clock[row.b.site_id]
        else:
            shift = eps_sat[row.a.sat_id] * row.a.translation_hz - eps_sat[row.b.sat_id] * row.b.translation_hz
        return Measurement(row.kind, row.value + shift, row.sigma, row.t_ns, row.a, row.b)

    target = [distort(r) for r in truth_rows()]
    ref = [distort(r) for r in _ref_rows(ORBITS, T_NS - int(20e9))]
    raw, _ = sv.solve(target, ORBITS, SITES, CFG, Region())
    assert raw is not None and miss_m(raw) > 10e3  # 270 ns and 15 Hz of bias
    calibrated = _calibrate(target, ref)
    assert calibrated[0].correction == pytest.approx(-clock["hnl"] + clock["tyo"], abs=1e-15)
    assert calibrated[1].correction == pytest.approx(eps_sat["sat1"] * -41.0e6 - eps_sat["sat2"] * -33.6e6, abs=1e-9)
    sol, reason = sv.solve(calibrated, ORBITS, SITES, CFG, Region())
    assert reason is None and sol is not None
    assert miss_m(sol) < 1.0 and sol.chi2 < 1e-6
    # with every clock / oscillator / bias sigma configured, those terms cancel in the covariance too:
    # what is left is the CAF noise and the reference noise
    unc = Uncertainty(
        site_clock_s={g: 200e-9 for g in SITES},
        site_osc_frac={g: 1e-11 for g in SITES},
        sat_osc_frac={s: 1e-6 for s in ORBITS},
        tdoa_bias_s=300e-9,
    )
    with_unc, _ = sv.solve(calibrated, ORBITS, SITES, CFG, Region(), uncertainty=unc)
    assert with_unc is not None and miss_m(with_unc) < 1.0
    # (only the site oscillators leave a trace: the reference is tuned 0.5 MHz away, 5e-6 Hz here)
    assert np.allclose(with_unc.cov_en_m2, sol.cov_en_m2, rtol=1e-4)
    assert with_unc.dominant_error in ("noise", "reference")
    # each row has its own reference of equal sigma: variance doubles, split evenly
    plain, _ = sv.solve(truth_rows(), ORBITS, SITES, CFG, Region())
    assert plain is not None
    assert np.allclose(sol.cov_en_m2, 2.0 * plain.cov_en_m2, rtol=1e-4)
    assert np.allclose(sol.cov_noise_en_m2, sol.cov_consider_en_m2, rtol=1e-4)
    # uncalibrated, the same sigmas at least make the biased fix consistent with its own covariance
    loose, _ = sv.solve(target, ORBITS, SITES, CFG, Region(), uncertainty=unc)
    assert loose is not None
    assert nees(en_error(loose), loose.cov_en_m2) < 13.8  # chi-square(2) at 99.9 %
    assert nees(en_error(raw), raw.cov_en_m2) > 100.0


def test_calibration_removes_a_common_ephemeris_offset() -> None:
    """Satellite 1 is 5 km from where its ephemeris says: kilometres of bias, removed differentially."""
    truth_orbits = dict(ORBITS)
    truth_orbits["sat1"] = ob.PerturbedOrbit(ORBITS["sat1"], [300.0, 4000.0, -3000.0], [0.0, 0.0, 0.0], sec(T_NS))
    target = truth_rows(orbits=truth_orbits)  # measured with the true orbit, solved with the nominal one
    ref = _ref_rows(truth_orbits, T_NS - int(20e9))
    raw, _ = sv.solve(target, ORBITS, SITES, CFG, Region())
    assert raw is not None and miss_m(raw) > 3000.0
    calibrated = _calibrate(target, ref)
    sol, reason = sv.solve(calibrated, ORBITS, SITES, CFG, Region())
    assert reason is None and sol is not None
    assert miss_m(sol) < 700.0 and miss_m(sol) < 0.15 * miss_m(raw)
    # with the ephemeris sigmas configured: the uncalibrated ellipse is inflated by them, the calibrated one hardly
    unc = Uncertainty(sat_pos_ric_m={s: np.array([300.0, 4000.0, 3000.0]) for s in ORBITS})
    raw_unc, _ = sv.solve(target, ORBITS, SITES, CFG, Region(), uncertainty=unc)
    cal_unc, _ = sv.solve(calibrated, ORBITS, SITES, CFG, Region(), uncertainty=unc)
    assert raw_unc is not None and cal_unc is not None
    assert raw_unc.dominant_error == "ephemeris_position"
    assert raw_unc.ellipse["semi_major_m"] > 1.5 * cal_unc.ellipse["semi_major_m"]
    assert cal_unc.ellipse["semi_major_m"] < 1.1 * sol.ellipse["semi_major_m"]
    assert miss_m(cal_unc) < 700.0
    # both fixes are statistically consistent with their own total covariance (noise-free rows: pure bias)
    assert nees(en_error(raw_unc), raw_unc.cov_en_m2) < 9.0
    assert nees(en_error(cal_unc), cal_unc.cov_en_m2) < 9.0


@pytest.mark.slow
def test_rows_sharing_one_reference_measurement_nees_monte_carlo() -> None:
    """Three epochs of target rows per (pair, kind) share one reference measurement each.

    The reference noise (three times the row noise) is common to the rows that use it.
    Treated as one consider parameter per reference, NEES with P_total averages 2;
    P_noise alone is far too small.
    """
    epochs = [T_NS + int(k * 5e9) for k in range(3)]
    target = [r for t in epochs for r in truth_rows(t_ns=t)]
    ref_truth = _ref_rows(ORBITS, T_NS - int(20e9), sigma_t=3e-6, sigma_f=0.15)
    rng = np.random.default_rng(1)
    total, noise_only, chi2 = [], [], []
    n_trials = 200
    for _ in range(n_trials):
        ref = with_noise(ref_truth, rng.standard_normal(4) * np.array([r.sigma for r in ref_truth]))
        rows = _calibrate(with_noise(target, rng.standard_normal(12) * np.array([r.sigma for r in target])), ref)
        sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region(), starts=[(LAT, LON)])
        assert sol is not None, reason
        e = en_error(sol)
        total.append(nees(e, sol.cov_en_m2))
        noise_only.append(nees(e, sol.cov_noise_en_m2))
        chi2.append(sol.chi2)
        assert sol.dominant_error == "reference" and sol.dof == 10
    assert abs(np.mean(total) - 2.0) < 3.0 * 2.0 / np.sqrt(n_trials)
    assert np.mean(noise_only) > 20.0
    assert abs(np.mean(chi2) - 10.0) < 3.0 * np.sqrt(20.0 / n_trials)  # chi-square(10): variance 20
    assert len({r.cal.cal_id for r in rows}) == 4 and len(rows) == 12


def test_calibrated_multi_epoch_covariance_matches_dense_algebra() -> None:
    """Every consider term at once: the low-rank weighting and the t_ref rule against dense algebra."""
    epochs = [T_NS + int(k * 37e9) for k in range(3)]
    target = [r for t in epochs for r in truth_rows(t_ns=t)]
    for unc, rows in (
        (FULL_UNCERTAINTY, target),
        (FULL_UNCERTAINTY, _calibrate(target, _ref_rows(ORBITS, T_NS - int(150e9), sigma_t=2e-6, sigma_f=0.08))),
        (None, _calibrate(target, _ref_rows(ORBITS, T_NS - int(150e9), sigma_t=2e-6, sigma_f=0.08))),
    ):
        got = sv.position_covariance(rows, LAT + 0.4, LON - 0.7, ORBITS, SITES, 0.0, unc)
        ref = brute_force(rows, LAT + 0.4, LON - 0.7, unc)
        assert got is not None
        scale = np.trace(ref["total"])
        assert cov_close(got["cov_en_m2"], ref["total"])
        assert cov_close(got["cov_noise_en_m2"], ref["noise"], scale=ref["total"])
        assert cov_close(got["cov_consider_en_m2"], ref["consider"], scale=ref["total"])
        assert np.allclose(got["cov_noise_en_m2"] + got["cov_consider_en_m2"], got["cov_en_m2"], rtol=1e-12)
        for name in sv.ERROR_SOURCES:
            assert got["budget_m2"][name] == pytest.approx(ref["budget"][name], rel=5e-3, abs=1e-3 * scale)
        assert got["dominant_error"] == max(ref["budget"], key=ref["budget"].get)
        assert sum(got["budget_m2"].values()) == pytest.approx(scale, rel=3e-3)
    # the solve reports the same numbers at its own solution
    rows = _calibrate(target, _ref_rows(ORBITS, T_NS - int(150e9), sigma_t=2e-6, sigma_f=0.08))
    sol, _ = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=FULL_UNCERTAINTY, starts=[(LAT, LON)])
    assert sol is not None and miss_m(sol) < 1.0
    ref = brute_force(rows, sol.lat_deg, sol.lon_deg, FULL_UNCERTAINTY)
    assert cov_close(sol.cov_en_m2, ref["total"])
    assert sol.dominant_error == max(ref["budget"], key=ref["budget"].get)


def test_uncalibrated_error_budget_names_the_dominant_source() -> None:
    """Each consider family in turn, on rows that cannot route around it (TDOA-only or FDOA-only)."""
    tdoa_rows = truth_rows(kinds=("tdoa",))
    fdoa_rows = truth_rows(kinds=("fdoa",), sigma_f=0.002)
    cases = [
        ("noise", tdoa_rows, Uncertainty()),
        ("noise", tdoa_rows, Uncertainty(sat_osc_frac={s: 1e-6 for s in ORBITS})),  # no FDOA row to act on
        ("ephemeris_position", tdoa_rows, Uncertainty(sat_pos_ric_m={s: np.array([300.0, 4000.0, 3000.0]) for s in ORBITS})),
        ("clock", tdoa_rows, Uncertainty(site_clock_s={g: 5e-6 for g in SITES})),
        ("tdoa_bias", tdoa_rows, Uncertainty(tdoa_bias_s=5e-6)),
        ("ephemeris_velocity", fdoa_rows, Uncertainty(sat_vel_ric_mps={s: np.array([0.3, 0.3, 0.7]) for s in ORBITS})),
        ("oscillator", fdoa_rows, Uncertainty(site_osc_frac={g: 2e-10 for g in SITES})),
        ("oscillator", fdoa_rows, Uncertainty(sat_osc_frac={s: 2e-10 for s in ORBITS})),
        ("noise", fdoa_rows, Uncertainty(site_clock_s={g: 5e-6 for g in SITES}, tdoa_bias_s=5e-6)),  # no TDOA row
    ]
    for expected, rows, unc in cases:
        got = sv.position_covariance(rows, LAT, LON, ORBITS, SITES, 0.0, unc)
        ref = brute_force(rows, LAT, LON, unc)
        assert got is not None and got["dominant_error"] == expected == max(ref["budget"], key=ref["budget"].get)
        assert cov_close(got["cov_en_m2"], ref["total"])
        assert cov_close(got["cov_consider_en_m2"], ref["consider"], scale=ref["total"])
        if expected == "noise":
            assert np.allclose(got["cov_consider_en_m2"], 0.0)
    # FDOA rows spread over four hours: a constant oscillator offset is common to all epochs and
    # largely drops out, a velocity error does not (the t_ref rule is exercised over +/- 2 h)
    spread = [r for hours in (0, 2, 4) for r in truth_rows(kinds=("fdoa",), sigma_f=0.002, t_ns=T_NS + int(hours * 3600e9))]
    for unc in (
        Uncertainty(sat_vel_ric_mps={s: np.array([0.3, 0.3, 0.7]) for s in ORBITS}),
        Uncertainty(sat_pos_ric_m={s: np.array([300.0, 4000.0, 3000.0]) for s in ORBITS}),
        Uncertainty(site_osc_frac={g: 2e-10 for g in SITES}, sat_osc_frac={s: 2e-10 for s in ORBITS}),
    ):
        got = sv.position_covariance(spread, LAT, LON, ORBITS, SITES, 0.0, unc)
        ref = brute_force(spread, LAT, LON, unc)
        assert got is not None and got["dominant_error"] == max(ref["budget"], key=ref["budget"].get)
        assert cov_close(got["cov_en_m2"], ref["total"])
        assert cov_close(got["cov_consider_en_m2"], ref["consider"], scale=ref["total"])
    # with total-covariance weighting a mixed set leans on the rows a systematic does not touch:
    # huge clock sigmas leave the TDOA + FDOA fix noise-dominated (through its FDOA rows)
    mixed = sv.position_covariance(truth_rows(), LAT, LON, ORBITS, SITES, 0.0, Uncertainty(site_clock_s={g: 5e-6 for g in SITES}))
    assert mixed is not None and mixed["dominant_error"] == "noise"


def test_reference_on_the_reversed_pair_is_recognised() -> None:
    """A calibration measured as (A, B) applied, sign-flipped, to a row measured as (B, A)."""
    clock = {"hnl": 180e-9, "tyo": -90e-9}
    x = lla_to_ecef(LAT, LON, 0.0)
    fwd_ref = _ref_rows(ORBITS, T_NS - int(20e9))[0]  # TDOA of (sat1 via hnl, sat2 via tyo)
    fwd_ref = Measurement("tdoa", fwd_ref.value - clock["hnl"] + clock["tyo"], 2e-6, fwd_ref.t_ns, fwd_ref.a, fwd_ref.b)
    c, cal = sv.calibration_correction(fwd_ref, X_REF, ORBITS, SITES, "ab")
    other = truth_rows(kinds=("tdoa",))[1]  # (sat1, sat3), untouched
    fwd_value = _h("tdoa", PATH1, PATH2, T_NS, x, ORBITS) - clock["hnl"] + clock["tyo"]
    rev_value = _h("tdoa", PATH2, PATH1, T_NS, x, ORBITS) - clock["tyo"] + clock["hnl"]
    forward = [Measurement("tdoa", fwd_value, SIGMA_T, T_NS, PATH1, PATH2, correction=c, cal=cal), other]
    backward = [Measurement("tdoa", rev_value, SIGMA_T, T_NS, PATH2, PATH1, correction=-c, cal=cal), other]
    unc = Uncertainty(site_clock_s={"hnl": 1e-6, "tyo": 1e-6}, tdoa_bias_s=1e-6)
    sol_f, _ = sv.solve(forward, ORBITS, SITES, CFG, Region(), uncertainty=unc)
    sol_b, _ = sv.solve(backward, ORBITS, SITES, CFG, Region(), uncertainty=unc)
    assert sol_f is not None and sol_b is not None
    # h_AB + h_BA is not exactly zero (path A is evaluated at the shifted epoch): a few metres at most
    assert miss_m(sol_f) < 1.0 and miss_m(sol_b) < 20.0
    assert np.allclose(sol_f.cov_en_m2, sol_b.cov_en_m2, rtol=1e-4)
    ref = brute_force(forward, sol_f.lat_deg, sol_f.lon_deg, unc)
    assert cov_close(sol_f.cov_en_m2, ref["total"])
    ref_b = brute_force(backward, sol_b.lat_deg, sol_b.lon_deg, unc, cal_sign=-1.0)
    assert cov_close(sol_b.cov_en_m2, ref_b["total"])
    # treated as same-orientation, the reversed row's site terms would add instead of cancelling
    wrong = brute_force(backward, sol_b.lat_deg, sol_b.lon_deg, unc, cal_sign=1.0)
    assert np.trace(wrong["total"]) > 2.0 * np.trace(ref_b["total"])
    # a reference on other satellites is rejected
    stray = CalRef("zz", X_REF, PathSpec("sat2", "tyo", F_REF, -33.6e6), PathSpec("sat3", "mnl", F_REF, -41.0e6), T_NS, 1e-6)
    with pytest.raises(ValueError, match=r"measurements\[0\]\.cal"):
        sv.solve([Measurement("tdoa", fwd_value, SIGMA_T, T_NS, PATH1, PATH2, cal=stray), other], ORBITS, SITES, CFG, Region())


def test_one_cal_id_cannot_name_two_reference_measurements() -> None:
    ref = _ref_rows(ORBITS, T_NS - int(20e9))[0]
    _, cal_a = sv.calibration_correction(ref, X_REF, ORBITS, SITES, "same")
    later = Measurement("tdoa", ref.value, ref.sigma, ref.t_ns + 10**9, ref.a, ref.b)
    _, cal_b = sv.calibration_correction(later, X_REF, ORBITS, SITES, "same")
    full = truth_rows()
    rows = [
        Measurement("tdoa", full[0].value, SIGMA_T, T_NS, PATH1, PATH2, cal=cal_a),
        Measurement("tdoa", full[0].value, SIGMA_T, T_NS + 10**9, PATH1, PATH2, cal=cal_b),
        full[2],
    ]
    with pytest.raises(ValueError, match="cal_id"):
        sv.solve(rows, ORBITS, SITES, CFG, Region())
    # an equal copy of the same reference (e.g. rebuilt from storage) is the same reference
    copy = CalRef(cal_a.cal_id, cal_a.x_ref.copy(), cal_a.a, cal_a.b, cal_a.t_ns, cal_a.sigma)
    rows[1] = Measurement("tdoa", full[0].value, SIGMA_T, T_NS + 10**9, PATH1, PATH2, cal=copy)
    sol, reason = sv.solve(rows, ORBITS, SITES, CFG, Region())
    assert sol is not None, reason


# ----------------------------------------------------------------------------- ellipse and CEP


@pytest.mark.parametrize("bearing", [0.0, 12.5, 45.0, 90.0, 133.0, 179.5])
def test_ellipse_azimuth_is_the_bearing_of_the_major_axis_clockwise_from_north(bearing: float) -> None:
    s_major, s_minor = 900.0, 150.0
    b = np.radians(bearing)
    major = np.array([np.sin(b), np.cos(b)])  # [east, north] unit vector at that bearing
    minor = np.array([np.cos(b), -np.sin(b)])
    cov = s_major**2 * np.outer(major, major) + s_minor**2 * np.outer(minor, minor)
    for p in (0.5, 0.95):
        ellipse = sv.error_ellipse(cov, p)
        scale = np.sqrt(stats.chi2.ppf(p, 2))  # independent of the -2 ln(1 - p) form
        assert ellipse["semi_major_m"] == pytest.approx(scale * s_major, rel=1e-9)
        assert ellipse["semi_minor_m"] == pytest.approx(scale * s_minor, rel=1e-9)
        assert ellipse["azimuth_deg"] == pytest.approx(bearing, abs=1e-7)
        assert 0.0 <= ellipse["azimuth_deg"] < 180.0 and ellipse["confidence"] == p
    assert sv.error_ellipse(cov, 0.95)["semi_major_m"] == pytest.approx(2.4477468 * s_major, rel=1e-7)


def test_ellipse_conventions_at_the_edges() -> None:
    # pure east-west and north-south
    assert sv.error_ellipse(np.diag([4.0, 1.0]), 0.9)["azimuth_deg"] == pytest.approx(90.0)
    assert sv.error_ellipse(np.diag([1.0, 4.0]), 0.9)["azimuth_deg"] == pytest.approx(0.0)
    # positive east-north correlation points north-east, negative north-west (135 deg)
    assert sv.error_ellipse(np.array([[2.0, 1.0], [1.0, 2.0]]), 0.9)["azimuth_deg"] == pytest.approx(45.0)
    assert sv.error_ellipse(np.array([[2.0, -1.0], [-1.0, 2.0]]), 0.9)["azimuth_deg"] == pytest.approx(135.0)
    circle = sv.error_ellipse(np.eye(2) * 9.0, 0.5)
    assert circle["azimuth_deg"] == 0.0 and circle["semi_major_m"] == circle["semi_minor_m"]
    # a bearing just below 180 stays below 180; 180 itself is 0
    b = np.radians(179.999999)
    v = np.array([np.sin(b), np.cos(b)])
    az = sv.error_ellipse(4.0 * np.outer(v, v) + np.eye(2), 0.9)["azimuth_deg"]
    assert 0.0 <= az < 180.0 and min(az, 180.0 - az) < 1e-5
    for bad in (np.eye(3), [[1.0, 2.0], [0.0, 1.0]], [[1.0, 0.0], [0.0, -1.0]], [[np.nan, 0.0], [0.0, 1.0]]):
        with pytest.raises(ValueError, match="P_2x2"):
            sv.error_ellipse(bad, 0.95)
    for bad_p in (0.0, 1.0, -0.2, "x"):
        with pytest.raises(ValueError, match="confidence"):
            sv.error_ellipse(np.eye(2), bad_p)


def test_ellipse_contains_the_stated_probability() -> None:
    rng = np.random.default_rng(8)
    cov = np.array([[5.0e5, -2.1e5], [-2.1e5, 1.6e5]])
    samples = rng.multivariate_normal(np.zeros(2), cov, size=200_000)
    for p in (0.5, 0.95):
        ellipse = sv.error_ellipse(cov, p)
        az = np.radians(ellipse["azimuth_deg"])
        along = samples @ np.array([np.sin(az), np.cos(az)])
        across = samples @ np.array([np.cos(az), -np.sin(az)])
        inside = (along / ellipse["semi_major_m"]) ** 2 + (across / ellipse["semi_minor_m"]) ** 2 <= 1.0
        assert np.mean(inside) == pytest.approx(p, abs=4.0 * np.sqrt(p * (1 - p) / samples.shape[0]))
        assert np.std(along) > np.std(across)  # the azimuth is the major axis, not the minor one


def test_cep50_against_monte_carlo_for_an_axis_ratio_of_0_05() -> None:
    rng = np.random.default_rng(6)
    s1, s2 = 2000.0, 100.0
    rot = np.radians(27.0)
    major = np.array([np.sin(rot), np.cos(rot)])
    minor = np.array([np.cos(rot), -np.sin(rot)])
    cov = s1**2 * np.outer(major, major) + s2**2 * np.outer(minor, minor)
    radius = np.hypot(*rng.multivariate_normal(np.zeros(2), cov, size=2_000_000).T)
    cep = sv.cep50(cov)
    # sampling error of the median: sqrt(0.25 / n) / pdf; pdf of r near the median is about 0.64 / s1
    assert cep == pytest.approx(np.median(radius), rel=3e-3)
    assert np.mean(radius <= cep) == pytest.approx(0.5, abs=2e-3)
    # the usual shortcuts are far off for this shape (MATH.md section 9)
    assert abs(0.589 * (s1 + s2) - cep) / cep > 0.05
    assert cep / (1.1774 * np.sqrt(s1 * s2)) > 2.0
    assert 0.6745 * s1 < cep < 0.70 * s1


@pytest.mark.parametrize("ratio", [1.0, 0.6, 0.2, 0.05, 0.003])
def test_cep50_solves_the_documented_integral(ratio: float) -> None:
    s1 = 730.0
    s2 = ratio * s1
    cep = sv.cep50(np.diag([s2**2, s1**2]))

    def integrand(theta: float) -> float:
        a = np.cos(theta) ** 2 / s1**2 + np.sin(theta) ** 2 / s2**2
        return (1.0 - np.exp(-cep * cep * a / 2.0)) / a

    value, _ = integrate.quad(integrand, 0.0, np.pi / 2.0, points=[min(5.0 * ratio, 1.5)], limit=400, epsabs=0.0, epsrel=1e-12)
    assert 2.0 / (np.pi * s1 * s2) * value == pytest.approx(0.5, abs=1e-8)


def test_cep50_closed_forms_and_invariance() -> None:
    assert sv.cep50(np.eye(2) * 250.0**2) == pytest.approx(250.0 * np.sqrt(2.0 * np.log(2.0)), rel=1e-10)
    assert sv.cep50(np.diag([640.0**2, 0.0])) == pytest.approx(640.0 * stats.norm.ppf(0.75), rel=1e-8)  # 1-D: 0.6745 sigma
    assert sv.cep50(np.zeros((2, 2))) == 0.0
    cov = np.array([[9.0e4, 3.5e4], [3.5e4, 2.5e4]])
    lam = np.linalg.eigvalsh(cov)
    assert sv.cep50(cov) == pytest.approx(sv.cep50(np.diag(lam)), rel=1e-10)  # rotation invariant
    assert sv.cep50(4.0 * cov) == pytest.approx(2.0 * sv.cep50(cov), rel=1e-10)  # scales with sigma
    with pytest.raises(ValueError, match="P_2x2"):
        sv.cep50(np.eye(3))


def test_solution_ellipse_and_cep_are_those_of_its_covariance() -> None:
    sol, _ = sv.solve(truth_rows(), ORBITS, SITES, make_cfg(confidence=0.5), Region(), starts=[(LAT, LON)])
    assert sol is not None
    assert sol.ellipse == sv.error_ellipse(sol.cov_en_m2, 0.5)
    assert sol.cep50_m == sv.cep50(sol.cov_en_m2)
    assert sol.ellipse["semi_minor_m"] < sol.cep50_m < sol.ellipse["semi_major_m"]
    payload = json.loads(json.dumps(sol.as_dict(), allow_nan=False))
    assert payload["cov_en_m2"] == sol.cov_en_m2.tolist() and payload["ambiguities"] == []
    assert payload["residuals"] == sol.residuals.tolist() and payload["reduced_chi2"] == sol.reduced_chi2
    two, _ = sv.solve(truth_rows(30.0, 140.0, ORBITS_EQ, kinds=("tdoa",)), ORBITS_EQ, SITES, CFG, Region())
    assert two is not None
    payload = json.loads(json.dumps(two.as_dict(), allow_nan=False))
    assert payload["reduced_chi2"] is None and len(payload["ambiguities"]) == 1
    assert payload["ambiguities"][0]["lat_deg"] == two.ambiguities[0].lat_deg


# ----------------------------------------------------------------------------- consider partials


def test_velocity_partials_use_each_rows_own_epoch_as_t_ref() -> None:
    """The one-orbit-per-parameter shortcut against a PerturbedOrbit per row with t_ref = row epoch."""
    epochs = [T_NS + int(k * 600e9) for k in range(4)]  # 30 minutes of rows
    rows = [r for t in epochs for r in truth_rows(t_ns=t)]
    unc = Uncertainty(
        sat_pos_ric_m={s: np.array([100.0, 1000.0, 1000.0]) for s in ORBITS},
        sat_vel_ric_mps={s: np.array([0.02, 0.02, 0.05]) for s in ORBITS},
    )
    prepared = sv._Rows(rows, ORBITS, SITES, unc)
    x = lla_to_ecef(LAT - 1.0, LON + 2.0, 0.0)
    gain = prepared.consider(np.stack([x, x + 5e4]))
    assert gain is not None and gain.shape == (2, 16, 18)
    # columns: per satellite (order of first use), position RIC then velocity RIC
    expected = np.zeros((16, 18))
    for k, row in enumerate(rows):
        partials = _row_partials(row.kind, row.a, row.b, row.t_ns, x, ORBITS)
        for j, sat in enumerate(("sat1", "sat2", "sat3")):
            for c in range(3):
                expected[k, 6 * j + c] = partials.get(("pos", sat, c), 0.0) * unc.sat_pos_ric_m[sat][c]
                expected[k, 6 * j + 3 + c] = partials.get(("vel", sat, c), 0.0) * unc.sat_vel_ric_mps[sat][c]
    sigma = np.array([r.sigma for r in rows])[:, None]
    # compared in units of each row's own sigma: every column to 1e-4 sigma or 0.2 %
    assert np.allclose(gain[0] / sigma, expected / sigma, rtol=2e-3, atol=1e-4)
    assert prepared.categories == (["ephemeris_position"] * 3 + ["ephemeris_velocity"] * 3) * 3
    # with t_ref at the row's epoch a velocity offset barely touches TDOA rows, whatever the epoch
    tdoa = np.array([r.kind == "tdoa" for r in rows])
    velocity_columns = [6 * j + 3 + c for j in range(3) for c in range(3)]
    assert np.max(np.abs(gain[0][tdoa][:, velocity_columns])) < 1e-4 * SIGMA_T
    assert np.max(np.abs(gain[0][~tdoa][:, velocity_columns])) > 0.3 * SIGMA_F


def test_weighting_matches_independent_algebra() -> None:
    """S = R + G G^T through the n x n Gram matrix, against two other routes to S^-1.

    Reference 1 (any conditioning): S^-1 r is the residual of the least-squares problem that
    estimates the consider parameters as nuisance parameters with unit prior,
    min_p |D^-1 (r - G p)|^2 + |p|^2, solved by ``lstsq`` on the stacked system.
    Reference 2 (well-conditioned cases): the dense inverse of S.

    LM is handed S^-1/2 r (symmetric square root), which must have the cost, normal
    matrix and gradient of the Cholesky-whitened L^-1 r; the reported residuals are
    L^-1 r itself.
    """
    rng = np.random.default_rng(31)
    for m, n, scale in ((6, 0, 1.0), (6, 2, 1.0), (40, 7, 30.0), (5, 12, 3.0), (30, 4, 1e5), (25, 3, 1e-6)):
        sigma = rng.uniform(0.5, 2.0, m) * 10.0 ** rng.integers(-7, 1, m)  # seconds and hertz mixed
        gain = None if n == 0 else rng.standard_normal((m, n)) * sigma[:, None] * scale * rng.uniform(0.1, 1.0, n)
        if gain is not None and n >= 3:
            gain[:, 2] = gain[:, 0]  # a redundant parameter: rank-deficient G
        whitener = sv._Whitener(sigma, gain)
        gain_n = np.zeros((m, 0)) if gain is None else gain / sigma[:, None]

        def s_inv_times(v: np.ndarray) -> np.ndarray:
            y = v / sigma
            stacked = np.vstack([gain_n, np.eye(gain_n.shape[1])])
            p_hat = np.linalg.lstsq(stacked, np.concatenate([y, np.zeros(gain_n.shape[1])]), rcond=None)[0]
            return (y - gain_n @ p_hat) / sigma

        r = rng.standard_normal(m) * sigma * (1.0 + scale)
        jac = rng.standard_normal((m, 2)) * sigma[:, None]
        chi2 = float(r @ s_inv_times(r))
        normal = jac.T @ np.stack([s_inv_times(jac[:, 0]), s_inv_times(jac[:, 1])], axis=1)
        w = whitener(r)
        jw = whitener(jac.T).T
        assert w @ w == pytest.approx(chi2, rel=1e-9)
        assert np.allclose(jw.T @ jw, normal, rtol=1e-8, atol=1e-10 * np.trace(normal))
        assert np.allclose(jw.T @ w, jac.T @ s_inv_times(r), rtol=1e-7, atol=1e-9 * np.linalg.norm(jw) * np.linalg.norm(w))
        stacked = whitener(np.stack([r, 2.0 * r, jac[:, 0]]))  # batched application
        assert np.allclose(stacked[0], w) and np.allclose(stacked[1], 2.0 * w) and np.allclose(stacked[2], jw[:, 0])
        solved = whitener.solve_normalised(r / sigma) / sigma  # S^-1 r
        assert np.allclose(solved, s_inv_times(r), rtol=1e-7, atol=1e-9 * np.max(np.abs(solved)))
        # the reported residuals: Cholesky whitening, same sum of squares
        reported = whitener.cholesky(r)
        # (a dense Cholesky factor of S loses about eps * cond(S) on the 1e5-sigma case)
        loose = scale > 30.0
        assert reported @ reported == pytest.approx(chi2, rel=1e-4 if loose else 1e-9)
        normalised = np.eye(m) + gain_n @ gain_n.T
        back = np.linalg.cholesky(normalised) @ reported  # L (L^-1 r) = r
        assert np.allclose(back, r / sigma, rtol=1e-8, atol=(1e-5 if loose else 1e-9) * np.max(np.abs(r / sigma)))
        if n and scale >= 1.0:
            assert not np.allclose(reported, w)  # a different square root of the same S^-1
        if scale <= 30.0:
            s_mat = np.diag(sigma**2) + gain_n @ gain_n.T * np.outer(sigma, sigma)
            d = np.sqrt(np.diag(s_mat))
            dense_inverse = np.linalg.inv(s_mat / np.outer(d, d)) / np.outer(d, d)
            assert w @ w == pytest.approx(r @ dense_inverse @ r, rel=1e-7)
            assert np.allclose(jw.T @ jw, jac.T @ dense_inverse @ jac, rtol=1e-7)
            assert np.allclose(reported, np.linalg.solve(np.linalg.cholesky(s_mat / np.outer(sigma, sigma)), r / sigma), rtol=1e-8, atol=1e-12)


# ----------------------------------------------------------------------------- invalid input


def test_invalid_measurements_name_the_field() -> None:
    good = truth_rows()

    def swap(k: int, **changes: Any) -> list[Measurement]:
        rows = list(good)
        fields = dict(kind=rows[k].kind, value=rows[k].value, sigma=rows[k].sigma, t_ns=rows[k].t_ns, a=rows[k].a, b=rows[k].b)
        fields.update(changes)
        rows[k] = Measurement(**fields)
        return rows

    cases = [
        (swap(1, kind="doppler"), r"measurements\[1\]\.kind"),
        (swap(2, sigma=0.0), r"measurements\[2\]\.sigma"),
        (swap(2, sigma=-1e-6), r"measurements\[2\]\.sigma"),
        (swap(0, sigma=float("nan")), r"measurements\[0\]\.sigma"),
        (swap(3, value=float("inf")), r"measurements\[3\]\.value"),
        (swap(0, t_ns=1.7e18), r"measurements\[0\]\.t_ns"),
        (swap(0, t_ns=True), r"measurements\[0\]\.t_ns"),
        (swap(1, a=PathSpec("nosat", "hnl", F_E, -41e6)), r"measurements\[1\]\.a\.sat_id"),
        (swap(1, b=PathSpec("sat2", "nosite", F_E, -41e6)), r"measurements\[1\]\.b\.site_id"),
        (swap(1, a=PathSpec("sat1", "hnl", float("nan"), -41e6)), r"measurements\[1\]\.a\.f_e_hz"),
        (swap(0, correction=float("nan")), r"measurements\[0\]\.correction"),
        (good[:3] + ["not a row"], r"measurements\[3\]"),
    ]
    for rows, pattern in cases:
        with pytest.raises(ValueError, match=pattern):
            sv.solve(rows, ORBITS, SITES, CFG, Region())
    with pytest.raises(ValueError, match="measurements"):
        sv.solve(None, ORBITS, SITES, CFG, Region())  # type: ignore[arg-type]


def test_invalid_configuration_names_the_field() -> None:
    rows = truth_rows()
    for name in ("height_m", "min_elevation_deg", "confidence", "grid_step_deg", "n_starts", "chi2_ambiguity"):
        cfg = make_cfg()
        delattr(cfg, name)
        with pytest.raises(ValueError, match=f"solver_cfg.{name}"):
            sv.solve(rows, ORBITS, SITES, cfg, Region())
    for overrides, pattern in (
        ({"confidence": 1.0}, "solver_cfg.confidence"),
        ({"grid_step_deg": 0.0}, "solver_cfg.grid_step_deg"),
        ({"n_starts": 0}, "solver_cfg.n_starts"),
        ({"n_starts": 2.5}, "solver_cfg.n_starts"),
        ({"chi2_ambiguity": -1.0}, "solver_cfg.chi2_ambiguity"),
        ({"min_elevation_deg": 95.0}, "solver_cfg.min_elevation_deg"),
        ({"height_m": float("nan")}, "solver_cfg.height_m"),
    ):
        with pytest.raises(ValueError, match=pattern):
            sv.solve(rows, ORBITS, SITES, make_cfg(**overrides), Region())
    # a mapping works as well as an attribute object
    sol, _ = sv.solve(rows, ORBITS, SITES, vars(make_cfg()), Region(), starts=[(LAT, LON)])
    assert sol is not None and miss_m(sol) < 1.0
    with pytest.raises(ValueError, match="region.grid"):
        sv.solve(rows, ORBITS, SITES, CFG, SimpleNamespace(contains=lambda la, lo: True))
    with pytest.raises(ValueError, match="region.contains"):
        sv.solve(rows, ORBITS, SITES, CFG, SimpleNamespace(grid=lambda step: ([0.0], [0.0])))
    for starts, pattern in (([], "starts"), ([(95.0, 0.0)], r"starts\[0\]\[0\]"), ([(10.0, float("nan"))], r"starts\[0\]\[1\]"), ([(1.0, 2.0, 3.0)], r"starts\[0\]")):
        with pytest.raises(ValueError, match=pattern):
            sv.solve(rows, ORBITS, SITES, CFG, Region(), starts=starts)
    for unc, pattern in (
        (Uncertainty(sat_pos_ric_m={"sat1": np.array([1.0, 2.0])}), "uncertainty.sat_pos_ric_m"),
        (Uncertainty(sat_vel_ric_mps={"sat1": np.array([1.0, -2.0, 0.0])}), "uncertainty.sat_vel_ric_mps"),
        (Uncertainty(site_clock_s={"hnl": -1e-9}), "uncertainty.site_clock_s"),
        (Uncertainty(sat_osc_frac={"sat1": float("nan")}), "uncertainty.sat_osc_frac"),
        (Uncertainty(tdoa_bias_s=-1.0), "uncertainty.tdoa_bias_s"),
        ({"tdoa_bias_s": 0.0}, "uncertainty"),
    ):
        with pytest.raises(ValueError, match=pattern):
            sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=unc)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="measurements"):
        sv.position_covariance(rows[:1], LAT, LON, ORBITS, SITES)
    with pytest.raises(ValueError, match="lat_deg"):
        sv.position_covariance(rows, 91.0, LON, ORBITS, SITES)


# ----------------------------------------------------------------------------- performance


def _best_of(n: int, fn: Any) -> float:
    best = np.inf
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def test_performance_of_the_global_grid_stage() -> None:
    """Default region at 1 degree: about 0.03 s measured (target 0.15 s); bound loose for a shared machine."""
    pair = ((PathSpec("sat1", "hnl", F_E, -41.0e6), PathSpec("sat2", "hnl", F_E, -33.6e6)),)
    rows = [r for k in range(2) for r in truth_rows(10.0, 170.0, pairs=pair, t_ns=T_NS + int(k * 5e9))]
    cfg = sv._SolverCfg.read(CFG)
    for unc in (None, FULL_UNCERTAINTY):
        prepared = sv._Rows(rows, ORBITS, SITES, unc)
        region = Region()
        lat, lon = region.grid(1.0)
        assert lat.size * lon.size == 141 * 360
        elapsed = _best_of(3, lambda: sv._grid_starts(prepared, rows, ORBITS, SITES, unc, cfg, region))
        assert elapsed < 0.45
        starts = sv._grid_starts(prepared, rows, ORBITS, SITES, unc, cfg, region)
        assert len(starts) == 16 and starts[0] == (10.0, 170.0)


def test_performance_of_refinement_and_warm_start() -> None:
    """400 rows: about 0.3 s per warm solve with every consider term (target 1 s per refinement).
    40 rows warm-started: about 0.13 s (target well under 0.3 s). Bounds are 3x for a shared machine."""
    rng = np.random.default_rng(9)

    def rows_for(n_epochs: int) -> list[Measurement]:
        truth = [r for k in range(n_epochs) for r in truth_rows(t_ns=T_NS + int(k * 5e9))]
        return with_noise(truth, rng.standard_normal(len(truth)) * np.array([r.sigma for r in truth]))

    big, small = rows_for(100), rows_for(10)
    assert len(big) == 400 and len(small) == 40
    result: dict[str, Any] = {}

    def run(rows: list[Measurement], unc: Uncertainty | None, key: str) -> None:
        result[key] = sv.solve(rows, ORBITS, SITES, CFG, Region(), uncertainty=unc, starts=[(LAT + 0.02, LON - 0.02)])

    assert _best_of(2, lambda: run(big, FULL_UNCERTAINTY, "big")) < 3.0
    assert _best_of(3, lambda: run(small, FULL_UNCERTAINTY, "small")) < 0.6
    assert _best_of(3, lambda: run(small, None, "plain")) < 0.3
    for key, rows in (("big", big), ("small", small), ("plain", small)):
        sol, reason = result[key]
        assert sol is not None, reason
        assert sol.n_measurements == len(rows) and sol.dof == len(rows) - 2
        # the fix is within its own 99.99 % ellipse of the truth (chi-square(2) < 18.4)
        assert nees(en_error(sol), sol.cov_en_m2) < 18.4
    assert result["big"][0].dominant_error == "ephemeris_position"
