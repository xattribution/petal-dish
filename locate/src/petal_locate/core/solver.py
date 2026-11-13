"""Position solver: emitter latitude/longitude from TDOA and FDOA rows (MATH.md section 9).

Unknown: geodetic latitude and longitude of a stationary emitter at a fixed height
above the WGS84 ellipsoid. Rows are TDOA (seconds) or FDOA (hertz) measurements, any mix
of satellite pairs, sites and epochs, each with its CAF noise ``sigma`` and an optional
reference-emitter calibration.

    R = diag(sigma_k^2)                    CAF noise only
    S = R + H_c S_c H_c^T                  total measurement covariance
    minimise  r^T S^-1 r,   r_k = z_k - c_k - h_k(lat, lon)

``h`` comes from ``geometry.predict_tdoa`` / ``predict_fdoa`` and nowhere else, so the
solver, the simulator and the search windows share one model. ``H_c S_c H_c^T`` has
low rank (one column per consider parameter), so the weighting works through the small
Gram matrix of those columns; the reported residuals are the Cholesky-whitened
``L^-1 r`` (``S = L L^T``) and ``chi2 = r^T S^-1 r`` (see ``_Whitener``). Conventions:

- positions are ECEF (WGS84) metres, angles degrees at the interface, absolute time
  integer nanoseconds (``Measurement.t_ns``, evaluated as float seconds for geometry);
- covariances are 2x2 in local east/north metres, ordered ``[east, north]``, with
  ``dE = (N(lat) + h) cos(lat) dlon`` and ``dN = (M(lat) + h) dlat``;
- point longitudes are normalised to ``[-180, 180)``;
- ``solver_cfg`` and ``region`` are read by attribute (``height_m``,
  ``min_elevation_deg``, ``confidence``, ``grid_step_deg``, ``n_starts``,
  ``chi2_ambiguity``; ``region.contains`` and ``region.grid``); nothing is imported
  from ``petal_locate.config``.

Nothing here keeps state, reads a clock or draws random numbers.
"""
from __future__ import annotations

import operator
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from scipy.linalg import solve_triangular
from scipy.optimize import brentq, least_squares

from .geodesy import (
    ecef_to_lla,
    geodesic_distance_m,
    lla_to_ecef,
    radii_of_curvature,
    wrap_lon_deg,
)
from .geometry import elevation_deg, predict_fdoa, predict_pair, predict_tdoa
from .orbits import Orbit, PerturbedOrbit, orbit_position
from .timebase import sec
from .types import CalRef, Measurement, PathSpec, Uncertainty

__all__ = [
    "CalRef",
    "Measurement",
    "Uncertainty",
    "Solution",
    "solve",
    "calibration_correction",
    "error_ellipse",
    "cep50",
    "position_covariance",
    "above_mask",
    "ERROR_SOURCES",
]

ERROR_SOURCES: tuple[str, ...] = (
    "noise",
    "ephemeris_position",
    "ephemeris_velocity",
    "clock",
    "oscillator",
    "tdoa_bias",
    "reference",
)
"""Values of ``Solution.dominant_error``, in tie-breaking order."""

_JAC_STEP_M = 100.0  # central-difference step of the covariance Jacobian, metres
_LM_JAC_STEP_M = 1000.0  # central-difference step of the Jacobian handed to LM, metres
_RIC_POS_STEP_M = 1000.0  # satellite position step of the consider partials, metres
_RIC_VEL_STEP_MPS = 0.02  # satellite velocity step of the consider partials, m/s
_DUPLICATE_M = 1000.0  # refined points closer than this (geodesic) are one minimum
_CHI2_TIE = 1e-6  # chi2 values closer than this are ordered by descending latitude
_MAX_SEMI_MAJOR_M = 1e7  # observability rule: 1-sigma semi-major axis limit
_LM_OFFSET_M = 1e7  # constant added to the LM variables, see _levenberg_marquardt
_LM_MAX_NFEV = 100
_LM_FLAT_EVALUATIONS = 5  # consecutive unobservable iterates after which a refinement is abandoned
_ESCAPE_M = 10_000.0  # restart distance either side of an unobservable converged point, metres
_CEP_NODES = 512
_ZERO3 = np.zeros(3)


# --------------------------------------------------------------------------- results


@dataclass
class Solution:
    """One position estimate (the fix, or one of its ambiguities).

    lat_deg, lon_deg: geodetic position, degrees (longitude in ``[-180, 180)``).
    height_m: the fixed height above the WGS84 ellipsoid the fit was made at, m.
    cov_en_m2: total 2x2 covariance ``P_total = (J^T S^-1 J)^-1``, m^2, ``[east, north]``.
    cov_noise_en_m2: the part due to CAF noise, ``K R K^T``.
    cov_consider_en_m2: the part due to the consider parameters, ``K H_c S_c H_c^T K^T``
    (the two parts sum to ``cov_en_m2``).
    ellipse: ``{semi_major_m, semi_minor_m, azimuth_deg, confidence}`` of ``cov_en_m2``
    (``error_ellipse``).
    cep50_m: radius containing half the probability of ``N(0, cov_en_m2)``, m.
    chi2: ``r^T S^-1 r``. dof: ``m - 2``. reduced_chi2: ``chi2 / dof`` or ``None`` when
    ``dof < 1``.
    residuals: whitened residuals ``L^-1 r`` (``S = L L^T``, lower Cholesky factor, with
    ``S`` the weighting the fit ended with), dimensionless, input order; their sum of
    squares is ``chi2``.
    ambiguities: the other admissible minima within ``chi2_ambiguity`` of this one
    (empty on the ambiguities themselves).
    ambiguity_resolved_by: ``"unresolved"`` (``ambiguities`` is non-empty), otherwise
    ``"region"`` (a second minimum within ``chi2_ambiguity`` of this one was discarded
    as inadmissible: outside the region or below the elevation mask), otherwise
    ``"measurements"`` (a second minimum was found and exceeds ``chi2_ambiguity``,
    wherever it lies), otherwise ``"none"`` (no second minimum found).
    converged: Levenberg-Marquardt converged (always true for a returned solution).
    dominant_error: the largest contributor to the trace of ``cov_en_m2``, one of
    ``ERROR_SOURCES``.
    n_measurements: number of rows used.
    """

    lat_deg: float
    lon_deg: float
    height_m: float
    cov_en_m2: np.ndarray
    cov_noise_en_m2: np.ndarray
    cov_consider_en_m2: np.ndarray
    ellipse: dict[str, float]
    cep50_m: float
    chi2: float
    dof: int
    reduced_chi2: float | None
    residuals: np.ndarray
    ambiguities: list["Solution"]
    ambiguity_resolved_by: Literal["none", "region", "measurements", "unresolved"]
    converged: bool
    dominant_error: str
    n_measurements: int

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form: matrices and residuals as nested lists, ambiguities recursively."""
        return {
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "height_m": self.height_m,
            "cov_en_m2": np.asarray(self.cov_en_m2).tolist(),
            "cov_noise_en_m2": np.asarray(self.cov_noise_en_m2).tolist(),
            "cov_consider_en_m2": np.asarray(self.cov_consider_en_m2).tolist(),
            "ellipse": dict(self.ellipse),
            "cep50_m": self.cep50_m,
            "chi2": self.chi2,
            "dof": self.dof,
            "reduced_chi2": self.reduced_chi2,
            "residuals": np.asarray(self.residuals).tolist(),
            "ambiguities": [s.as_dict() for s in self.ambiguities],
            "ambiguity_resolved_by": self.ambiguity_resolved_by,
            "converged": self.converged,
            "dominant_error": self.dominant_error,
            "n_measurements": self.n_measurements,
        }


# --------------------------------------------------------------------------- public API


def solve(
    measurements: Sequence[Measurement],
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    solver_cfg: Any,
    region: Any,
    uncertainty: Uncertainty | None = None,
    starts: Sequence[tuple[float, float]] | None = None,
) -> tuple[Solution | None, str | None]:
    """Locate a stationary emitter from TDOA/FDOA rows (MATH.md section 9).

    measurements: at least two rows; ``value`` in s (TDOA) or Hz (FDOA), ``sigma`` the
    CAF noise of that row, ``t_ns`` its ``t_mid``, ``correction`` the calibration term
    subtracted from ``value`` and ``cal`` the reference measurement it came from.
    orbits: ``dict[sat_id, Orbit]`` (ECEF). sites_xyz: ``dict[site_id, ndarray[3]]``,
    antenna phase centres, ECEF m.
    solver_cfg: object (or mapping) with ``height_m`` (m above the ellipsoid),
    ``min_elevation_deg``, ``confidence`` (ellipse probability), ``grid_step_deg``,
    ``n_starts`` and ``chi2_ambiguity``.
    region: object with ``contains(lat_deg, lon_deg) -> bool array`` and
    ``grid(step_deg) -> (lat_deg[n_lat], lon_deg[n_lon])`` (longitudes increasing,
    unwrapped).
    uncertainty: one-sigma consider parameters; ``None`` means none. Reference
    measurements (``Measurement.cal``) are consider parameters either way.
    starts: ``[(lat_deg, lon_deg), ...]`` to refine instead of running the global grid
    search (the previous solution and its ambiguities).

    Returns ``(solution, None)``, or ``(None, reason)`` when no estimate can be made:
    ``"unobservable"`` (``J^T S^-1 J`` is not positive definite or the one-sigma
    semi-major axis exceeds 1e7 m, at the solution or along every refinement),
    ``"no admissible region"`` (no grid cell is inside the region with every satellite
    above the mask), ``"not converged"`` (no start converged) or
    ``"no admissible solution"`` (every converged point lies outside the region or
    below the elevation mask).

    Procedure. Admissible points are those inside ``region`` from which every satellite
    of every row is at or above ``min_elevation_deg`` at that row's epoch. With
    ``starts=None`` the cost of the newest two rows of each (pair, kind) is evaluated
    on the region grid, weighted by the total covariance with the consider partials
    taken at the centroid of the admissible cells; the starts are the ``n_starts``
    lowest cells that are not above any of their 8 neighbours. Each start is refined
    with all rows by Levenberg-Marquardt (``scipy.optimize.least_squares``, method
    ``lm``, numeric Jacobian) on the whitened residuals, the consider partials taken at
    the start and again at the converged point, followed by one more refinement. The
    equatorial mirror ``(-lat, lon)`` of every refined minimum is refined as well,
    whether or not it is admissible, so that a mirror excluded only by the region is
    known to exist (``ambiguity_resolved_by == "region"``). A converged point where
    ``J^T S^-1 J`` fails the observability rule is refined again from 10 km either side
    of it along its unobservable axis; if that reaches a lower chi2 the point was a
    saddle (Gauss-Newton stops where the residual gradient vanishes, e.g. on the equator
    under equatorial satellites, where every row is symmetric in latitude) and is
    dropped. Points that did not converge or are not admissible are discarded, points
    within 1 km of a lower-cost point are duplicates, candidates are ordered by chi2
    (ties within 1e-6 by descending latitude); the first is the solution and the others
    within ``chi2_ambiguity`` of it are its ``ambiguities``. If the solution or one of
    those ambiguities fails the
    observability rule the result is ``(None, "unobservable")``.

    Raises ValueError (naming the field) for fewer than two rows or any invalid input.
    """
    cfg = _SolverCfg.read(solver_cfg)
    rows = _Rows(measurements, orbits, sites_xyz, uncertainty)
    if rows.m < 2:
        raise ValueError(f"measurements: at least 2 rows are needed, got {rows.m}")
    _check_region(region)

    if starts is None:
        start_points = _grid_starts(rows, rows.measurements, orbits, sites_xyz, uncertainty, cfg, region)
        if not start_points:
            return None, "no admissible region"
    else:
        start_points = _check_starts(starts)

    refined = _escape_saddles(rows, _refine_with_mirrors(rows, start_points, cfg.height_m), cfg.height_m)
    fits = [f for f in refined if f.converged]
    if not fits:
        return None, "unobservable" if any(f.flat for f in refined) else "not converged"
    ok = _admissible(rows, np.array([f.lat for f in fits]), np.array([f.lon for f in fits]), cfg, region)
    return _select(rows, fits, ok, cfg)


def calibration_correction(
    ref_measurement: Measurement,
    x_ref: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    cal_id: str,
) -> tuple[float, CalRef]:
    """Calibration term of a reference-emitter measurement: ``c = z_ref - h_ref(x_ref)``.

    ref_measurement: the raw TDOA (s) or FDOA (Hz) row measured on the reference
    carrier (its own ``f_e_hz`` in the paths; ``correction`` must be 0 and ``cal``
    ``None``). x_ref: surveyed phase centre of the reference emitter, ECEF m ``[3]``.
    cal_id: identifier of this reference measurement; target rows that carry the same
    ``cal_id`` (and kind) share its noise as one consider parameter.

    Returns ``(c, CalRef)``: ``c`` in the units of the row, to be stored in
    ``Measurement.correction`` of target rows on the same pair, with the ``CalRef`` in
    ``Measurement.cal``. For a target row on the reversed pair (B, A) use ``-c`` with
    the same ``CalRef``; the solver recognises the reversal from the paths.
    """
    if not isinstance(ref_measurement, Measurement):
        raise ValueError("ref_measurement must be a Measurement")
    if not isinstance(cal_id, str) or not cal_id:
        raise ValueError("cal_id must be a non-empty string")
    if ref_measurement.cal is not None or float(ref_measurement.correction) != 0.0:
        raise ValueError("ref_measurement must be a raw row (correction 0 and cal None)")
    x = _vector3(x_ref, "x_ref")
    rows = _Rows([ref_measurement], orbits, sites_xyz, None, label="ref_measurement")
    predicted = float(rows.predict(x)[0])
    cal = CalRef(
        cal_id=cal_id,
        x_ref=x.copy(),
        a=ref_measurement.a,
        b=ref_measurement.b,
        t_ns=int(ref_measurement.t_ns),
        sigma=float(ref_measurement.sigma),
    )
    return float(ref_measurement.value) - predicted, cal


def error_ellipse(P_2x2: Any, confidence: float) -> dict[str, float]:
    """Error ellipse of a 2x2 east/north covariance.

    P_2x2: covariance, m^2, ordered ``[east, north]``. confidence: probability enclosed,
    in (0, 1).

    Returns ``{semi_major_m, semi_minor_m, azimuth_deg, confidence}``: semi-axes
    ``sqrt(-2 ln(1 - p)) * sqrt(eigenvalues)`` in metres and the bearing of the
    semi-major axis clockwise from true north, degrees in ``[0, 180)`` (0 for a circle).
    """
    cov = _covariance2(P_2x2, "P_2x2")
    try:
        p = float(confidence)
    except (TypeError, ValueError):
        raise ValueError(f"confidence must be a number in (0, 1), got {confidence!r}") from None
    if not 0.0 < p < 1.0:
        raise ValueError(f"confidence must be within (0, 1), got {confidence!r}")
    lam = np.clip(np.linalg.eigvalsh(cov), 0.0, None)
    scale = float(np.sqrt(-2.0 * np.log1p(-p)))
    # major axis direction (e, n) satisfies tan(2 az) = 2 P_en / (P_nn - P_ee), az from north
    azimuth = 0.5 * np.degrees(np.arctan2(2.0 * cov[0, 1], cov[1, 1] - cov[0, 0]))
    azimuth = float(np.mod(azimuth, 180.0))
    if azimuth >= 180.0:
        azimuth = 0.0
    return {
        "semi_major_m": scale * float(np.sqrt(lam[1])),
        "semi_minor_m": scale * float(np.sqrt(lam[0])),
        "azimuth_deg": azimuth,
        "confidence": p,
    }


def cep50(P_2x2: Any) -> float:
    """Radius containing half the probability of ``N(0, P)``, metres (exact CEP50).

    P_2x2: 2x2 covariance, m^2. With ``s1 >= s2`` the square roots of its eigenvalues,
    the radius ``r`` solves (MATH.md section 9)

        (2 / (pi s1 s2)) int_0^{pi/2} (1 - exp(-r^2 a(theta) / 2)) / a(theta) dtheta = 0.5,
        a(theta) = cos^2(theta) / s1^2 + sin^2(theta) / s2^2.

    Evaluated after the substitution ``tan(theta) = (s2 / s1) tan(phi)``, which turns it
    into ``1 - (2 / pi) int_0^{pi/2} exp(-r^2 / (2 (s1^2 cos^2 phi + s2^2 sin^2 phi))) dphi``:
    a smooth periodic integrand for any axis ratio (including ``s2 = 0``, where
    ``r = 0.6745 s1``), integrated by the midpoint rule.
    """
    cov = _covariance2(P_2x2, "P_2x2")
    lam = np.clip(np.linalg.eigvalsh(cov), 0.0, None)
    var_major, var_minor = float(lam[1]), float(lam[0])
    if var_major == 0.0:
        return 0.0
    s1 = float(np.sqrt(var_major))
    phi = (np.arange(_CEP_NODES) + 0.5) * (0.5 * np.pi / _CEP_NODES)
    variance = var_major * np.cos(phi) ** 2 + var_minor * np.sin(phi) ** 2

    def excess(r: float) -> float:
        with np.errstate(under="ignore"):  # exp underflows to 0 across the minor axis of a thin ellipse
            return 0.5 - float(np.mean(np.exp(-(r * r) / (2.0 * variance))))

    # the radius grows with s2 from 0.67449 s1 (s2 = 0) to 1.17741 s1 (s2 = s1)
    return float(brentq(excess, 0.67 * s1, 1.18 * s1, xtol=1e-13 * s1, rtol=1e-13))


def position_covariance(
    measurements: Sequence[Measurement],
    lat_deg: float,
    lon_deg: float,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    height_m: float = 0.0,
    uncertainty: Uncertainty | None = None,
    confidence: float = 0.95,
) -> dict[str, Any] | None:
    """Covariance a fix at a given position would have with a given measurement set.

    The accuracy-prediction building block (MATH.md section 10): the same ``J``, ``S``,
    ``P_total`` and observability rule as ``solve``, without solving. ``value`` and
    ``correction`` of the rows are not used; ``kind``, ``sigma``, ``t_ns``, the paths
    and ``cal`` are.

    lat_deg, lon_deg: candidate emitter position, degrees. height_m: its height above
    the ellipsoid, m. Other arguments as for ``solve`` (at least two rows).

    Returns ``None`` when the position is unobservable from these rows, otherwise
    ``{cov_en_m2, cov_noise_en_m2, cov_consider_en_m2, ellipse, cep50_m, dominant_error,
    budget_m2}`` with 2x2 arrays in m^2 ordered ``[east, north]`` and ``budget_m2`` the
    contribution of each of ``ERROR_SOURCES`` to the trace of ``cov_en_m2``.
    """
    rows = _Rows(measurements, orbits, sites_xyz, uncertainty)
    if rows.m < 2:
        raise ValueError(f"measurements: at least 2 rows are needed, got {rows.m}")
    lat, lon = _point(lat_deg, lon_deg, "lat_deg", "lon_deg")
    height = _finite(height_m, "height_m")
    probability = _finite(confidence, "confidence")
    if not 0.0 < probability < 1.0:
        raise ValueError(f"confidence must be within (0, 1), got {confidence!r}")
    gain = rows.consider(lla_to_ecef(lat, lon, height)[None, :])
    whitener = _Whitener(rows.sigma, None if gain is None else gain[0])
    stats = _covariance_at(rows, lat, lon, height, whitener)
    if stats is None:
        return None
    return {
        "cov_en_m2": stats.total,
        "cov_noise_en_m2": stats.noise,
        "cov_consider_en_m2": stats.consider,
        "ellipse": error_ellipse(stats.total, probability),
        "cep50_m": cep50(stats.total),
        "dominant_error": stats.dominant,
        "budget_m2": dict(stats.budget),
    }


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class _SolverCfg:
    height_m: float
    min_elevation_deg: float
    confidence: float
    grid_step_deg: float
    n_starts: int
    chi2_ambiguity: float

    @classmethod
    def read(cls, cfg: Any) -> "_SolverCfg":
        def get(name: str) -> Any:
            if isinstance(cfg, Mapping):
                if name in cfg:
                    return cfg[name]
            elif hasattr(cfg, name):
                return getattr(cfg, name)
            raise ValueError(f"solver_cfg.{name} is required")

        height = _finite(get("height_m"), "solver_cfg.height_m")
        min_el = _finite(get("min_elevation_deg"), "solver_cfg.min_elevation_deg")
        if not -90.0 <= min_el < 90.0:
            raise ValueError(f"solver_cfg.min_elevation_deg must be within [-90, 90), got {min_el!r}")
        confidence = _finite(get("confidence"), "solver_cfg.confidence")
        if not 0.0 < confidence < 1.0:
            raise ValueError(f"solver_cfg.confidence must be within (0, 1), got {confidence!r}")
        step = _finite(get("grid_step_deg"), "solver_cfg.grid_step_deg")
        if not step > 0.0:
            raise ValueError(f"solver_cfg.grid_step_deg must be positive, got {step!r}")
        n_starts = get("n_starts")
        if isinstance(n_starts, bool) or not isinstance(n_starts, (int, np.integer)) or n_starts < 1:
            raise ValueError(f"solver_cfg.n_starts must be a positive integer, got {n_starts!r}")
        chi2_ambiguity = _finite(get("chi2_ambiguity"), "solver_cfg.chi2_ambiguity")
        if chi2_ambiguity < 0.0:
            raise ValueError(f"solver_cfg.chi2_ambiguity must not be negative, got {chi2_ambiguity!r}")
        return cls(height, min_el, confidence, step, int(n_starts), chi2_ambiguity)


def _check_region(region: Any) -> None:
    for name in ("contains", "grid"):
        if not callable(getattr(region, name, None)):
            raise ValueError(f"region.{name} is required")


def _check_starts(starts: Any) -> list[tuple[float, float]]:
    try:
        items = list(starts)
    except TypeError:
        raise ValueError("starts must be a sequence of (lat_deg, lon_deg)") from None
    if not items:
        raise ValueError("starts must hold at least one (lat_deg, lon_deg); pass None for the global search")
    out = []
    for k, item in enumerate(items):
        try:
            lat, lon = item
        except (TypeError, ValueError):
            raise ValueError(f"starts[{k}] must be (lat_deg, lon_deg)") from None
        out.append(_point(lat, lon, f"starts[{k}][0]", f"starts[{k}][1]"))
    return out


# --------------------------------------------------------------------------- rows


@dataclass
class _Group:
    """Rows sharing one path pair: evaluated together over their distinct epochs."""

    a: PathSpec
    b: PathSpec
    t_s: np.ndarray  # distinct epochs, Unix s, [M]
    rows_t: np.ndarray  # row indices of the TDOA rows
    cols_t: np.ndarray  # their epoch indices
    rows_f: np.ndarray  # row indices of the FDOA rows
    cols_f: np.ndarray  # their epoch indices
    rows: np.ndarray  # all row indices of the group


class _Rows:
    """The rows of one solve, prepared for vectorised prediction and weighting.

    Predictions are evaluated per path pair ``(a, b)`` over that pair's distinct
    epochs and any number of emitter positions at once (one geometry call returns both
    kinds), which covers the per-(kind, a, b) batches of MATH.md section 9.
    """

    def __init__(
        self,
        measurements: Sequence[Measurement],
        orbits: Mapping[str, Orbit],
        sites_xyz: Mapping[str, Any],
        uncertainty: Uncertainty | None,
        label: str = "measurements",
    ) -> None:
        try:
            rows = list(measurements)
        except TypeError:
            raise ValueError(f"{label} must be a sequence of Measurement") from None
        if not isinstance(orbits, Mapping):
            raise ValueError("orbits must be a mapping of sat_id to Orbit")
        if not isinstance(sites_xyz, Mapping):
            raise ValueError("sites_xyz must be a mapping of site_id to ECEF position")
        self.measurements = rows
        self.orbits = orbits
        self.sites_xyz = sites_xyz
        self.m = len(rows)
        self.z = np.empty(self.m)
        self.sigma = np.empty(self.m)
        self.t_s = np.empty(self.m)
        self.kinds: list[str] = []
        for k, row in enumerate(rows):
            name = f"{label}[{k}]" if label == "measurements" else label
            if not isinstance(row, Measurement):
                raise ValueError(f"{name} must be a Measurement")
            if row.kind not in ("tdoa", "fdoa"):
                raise ValueError(f"{name}.kind must be 'tdoa' or 'fdoa', got {row.kind!r}")
            value = _finite(row.value, f"{name}.value")
            correction = _finite(row.correction, f"{name}.correction")
            sigma = _finite(row.sigma, f"{name}.sigma")
            if not sigma > 0.0:
                raise ValueError(f"{name}.sigma must be positive, got {row.sigma!r}")
            for end in ("a", "b"):
                _check_path(getattr(row, end), f"{name}.{end}", orbits, sites_xyz)
            self.kinds.append(row.kind)
            self.z[k] = value - correction
            self.sigma[k] = sigma
            self.t_s[k] = sec(_integer(row.t_ns, f"{name}.t_ns"))
        self.groups = _make_groups(self.kinds, [r.a for r in rows], [r.b for r in rows], self.t_s)
        # epochs at which each satellite is used (for the elevation mask)
        epochs: dict[str, list[float]] = {}
        for group in self.groups:
            for sat in dict.fromkeys((group.a.sat_id, group.b.sat_id)):
                epochs.setdefault(sat, []).extend(group.t_s.tolist())
        self.sat_epochs = {sat: np.unique(np.array(ts)) for sat, ts in epochs.items()}
        self._build_consider(_check_uncertainty(uncertainty), label)

    # ---- prediction

    def predict(
        self,
        x: np.ndarray,
        orbits: Mapping[str, Orbit] | None = None,
        groups: Sequence[_Group] | None = None,
    ) -> np.ndarray:
        """Predicted rows at emitter position(s) ``x`` (ECEF m, ``[..., 3]``): ``[..., m]``.

        TDOA rows in seconds and FDOA rows in hertz, in row order. ``orbits`` replaces
        the solve's orbits (perturbed copies); with ``groups`` only those groups are
        evaluated and the other rows are left at zero.
        """
        return _predict_groups(
            x, self.groups if groups is None else groups, self.m, self.orbits if orbits is None else orbits, self.sites_xyz
        )

    # ---- consider parameters

    def _build_consider(self, unc: Uncertainty | None, label: str) -> None:
        """Columns of ``H_c S_c^(1/2)``: analytic ones now, satellite RIC ones per position.

        Analytic partials (MATH.md section 9): site clock (TDOA, -1 for site a, +1 for
        site b), site oscillator (FDOA, ``-f_tune,A`` / ``+f_tune,B``), satellite
        oscillator (FDOA, ``+T_nom,i`` / ``-T_nom,j``), pair TDOA bias (1, sign
        following the orientation of the pair), and one column per distinct reference
        measurement. A calibrated row carries ``H_c(x) - s H_c,ref(x_ref)`` with ``s``
        the orientation of the reference pair relative to the row (+1 same, -1
        reversed) and ``s`` on its reference column.
        """
        m = self.m
        columns: dict[tuple[Any, ...], np.ndarray] = {}
        sigmas: dict[tuple[Any, ...], float] = {}

        def add(key: tuple[Any, ...], k: int, value: float) -> None:
            if key not in columns:
                columns[key] = np.zeros(m)
            columns[key][k] += value

        ref_index: dict[tuple[str, str], int] = {}
        refs: list[tuple[str, CalRef]] = []
        self._row_ref = np.full(m, -1, dtype=np.intp)
        self._row_sign = np.zeros(m)
        for k, row in enumerate(self.measurements):
            name = f"{label}[{k}]" if label == "measurements" else label
            for key, value in _analytic_partials(row.kind, row.a, row.b).items():
                add(key, k, value)
            cal = row.cal
            if cal is None:
                continue
            _check_cal(cal, f"{name}.cal", self.orbits, self.sites_xyz)
            sign = _cal_sign(row, cal, f"{name}.cal")
            for key, value in _analytic_partials(row.kind, cal.a, cal.b).items():
                add(key, k, -sign * value)
            ref_key = (cal.cal_id, row.kind)
            if ref_key not in ref_index:
                ref_index[ref_key] = len(refs)
                refs.append((row.kind, cal))
            else:
                first = refs[ref_index[ref_key]][1]
                if first is not cal and not _same_cal(first, cal):
                    raise ValueError(
                        f"{name}.cal: cal_id {cal.cal_id!r} names two different reference measurements"
                    )
            add(("ref", cal.cal_id, row.kind), k, sign)
            sigmas[("ref", cal.cal_id, row.kind)] = float(cal.sigma)
            self._row_ref[k] = ref_index[ref_key]
            self._row_sign[k] = sign

        if unc is not None:
            for key in columns:
                if key[0] == "clock":
                    sigmas[key] = float(unc.site_clock_s.get(key[1], 0.0))
                elif key[0] == "site_osc":
                    sigmas[key] = float(unc.site_osc_frac.get(key[1], 0.0))
                elif key[0] == "sat_osc":
                    sigmas[key] = float(unc.sat_osc_frac.get(key[1], 0.0))
                elif key[0] == "bias":
                    sigmas[key] = float(unc.tdoa_bias_s)
        category = {"clock": "clock", "site_osc": "oscillator", "sat_osc": "oscillator", "bias": "tdoa_bias", "ref": "reference"}
        const: list[np.ndarray] = []
        self._categories: list[str] = []
        for key, column in columns.items():
            sigma = sigmas.get(key, 0.0)
            if sigma > 0.0 and np.any(column != 0.0):
                const.append(column * sigma)
                self._categories.append(category[key[0]])
        self._const = np.stack(const, axis=1) if const else np.zeros((m, 0))

        # satellite position / velocity (RIC) parameters with a non-zero sigma
        self._eph: list[tuple[str, bool, int]] = []
        eph_sigma: list[float] = []
        if unc is not None:
            sats = list(self.sat_epochs)
            for _, cal in refs:
                for sat in (cal.a.sat_id, cal.b.sat_id):
                    if sat not in sats:
                        sats.append(sat)
            for sat in sats:
                for is_vel, table in ((False, unc.sat_pos_ric_m), (True, unc.sat_vel_ric_mps)):
                    if sat not in table:
                        continue
                    for comp, sigma in enumerate(np.asarray(table[sat], dtype=np.float64)):
                        if sigma > 0.0:
                            self._eph.append((sat, is_vel, comp))
                            eph_sigma.append(float(sigma))
                            self._categories.append("ephemeris_velocity" if is_vel else "ephemeris_position")
        self._eph_sigma = np.array(eph_sigma)
        self._eph_ref = np.zeros((m, len(self._eph)))
        if self._eph and refs:
            # H_c,ref at each reference position, at the reference's own f_e and epoch
            by_position: dict[bytes, list[int]] = {}
            for j, (_, cal) in enumerate(refs):
                by_position.setdefault(np.asarray(cal.x_ref, dtype=np.float64).tobytes(), []).append(j)
            ref_partials = np.zeros((len(refs), len(self._eph)))
            for members in by_position.values():
                kinds = [refs[j][0] for j in members]
                cals = [refs[j][1] for j in members]
                t_s = np.array([sec(int(c.t_ns)) for c in cals])
                groups = _make_groups(kinds, [c.a for c in cals], [c.b for c in cals], t_s)
                x_ref = np.asarray(cals[0].x_ref, dtype=np.float64)[None, :]
                partials = _ephemeris_partials(
                    x_ref, groups, len(members), t_s, self._eph, self.orbits, self.sites_xyz, None
                )
                ref_partials[members] = partials[0]
            used = self._row_ref >= 0
            self._eph_ref[used] = (
                -self._row_sign[used, None] * ref_partials[self._row_ref[used]] * self._eph_sigma
            )
        self.n_consider = self._const.shape[1] + len(self._eph)
        self.position_dependent = bool(self._eph)

    @property
    def constant_gain(self) -> np.ndarray | None:
        """``H_c S_c^(1/2)`` ``[m, n]`` when no column depends on the emitter position, else ``None``."""
        if self.n_consider == 0 or self.position_dependent:
            return None
        return self._const

    @property
    def categories(self) -> list[str]:
        """Error source of each consider column (order of ``consider``'s last axis)."""
        return self._categories

    def consider(self, x: np.ndarray, h_base: np.ndarray | None = None) -> np.ndarray | None:
        """``H_c S_c^(1/2)`` at emitter position(s) ``x`` (``[P, 3]`` ECEF m): ``[P, m, n]`` or ``None``.

        Each column is one consider parameter scaled by its one-sigma, in the units of
        the rows (s or Hz), so ``H_c S_c H_c^T = G G^T``. ``None`` when there are no
        consider parameters. h_base: ``predict(x)`` if the caller already has it.
        """
        if self.n_consider == 0:
            return None
        n_const = self._const.shape[1]
        gain = np.empty((x.shape[0], self.m, self.n_consider))
        gain[:, :, :n_const] = self._const
        if self._eph:
            partials = _ephemeris_partials(
                x, self.groups, self.m, self.t_s, self._eph, self.orbits, self.sites_xyz, h_base
            )
            gain[:, :, n_const:] = partials * self._eph_sigma + self._eph_ref
        return gain


def _make_groups(
    kinds: Sequence[str], a: Sequence[PathSpec], b: Sequence[PathSpec], t_s: np.ndarray
) -> list[_Group]:
    """Group rows by path pair; each group lists its distinct epochs and where its rows sit."""
    members: dict[tuple[PathSpec, PathSpec], list[int]] = {}
    for k in range(len(kinds)):
        members.setdefault((a[k], b[k]), []).append(k)
    groups = []
    for (path_a, path_b), idx in members.items():
        rows = np.array(idx, dtype=np.intp)
        epochs, cols = np.unique(t_s[rows], return_inverse=True)
        is_t = np.array([kinds[k] == "tdoa" for k in idx])
        groups.append(
            _Group(path_a, path_b, epochs, rows[is_t], cols[is_t], rows[~is_t], cols[~is_t], rows)
        )
    return groups


def _predict_groups(
    x: np.ndarray,
    groups: Sequence[_Group],
    m: int,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
) -> np.ndarray:
    """Predictions ``[..., m]`` of the rows in ``groups`` at ``x`` (``[..., 3]``); other rows zero."""
    out = np.zeros(x.shape[:-1] + (m,))
    for g in groups:
        if g.rows_f.size == 0:
            tdoa = predict_tdoa(x, g.a, g.b, g.t_s, orbits, sites_xyz)
            out[..., g.rows_t] = tdoa[..., g.cols_t]
        elif g.rows_t.size == 0:
            fdoa = predict_fdoa(x, g.a, g.b, g.t_s, orbits, sites_xyz)
            out[..., g.rows_f] = fdoa[..., g.cols_f]
        else:
            tdoa, fdoa = predict_pair(x, g.a, g.b, g.t_s, orbits, sites_xyz)
            out[..., g.rows_t] = tdoa[..., g.cols_t]
            out[..., g.rows_f] = fdoa[..., g.cols_f]
    return out


def _ephemeris_partials(
    x: np.ndarray,
    groups: Sequence[_Group],
    m: int,
    t_s: np.ndarray,
    params: Sequence[tuple[str, bool, int]],
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    h_base: np.ndarray | None,
) -> np.ndarray:
    """Partials of the rows with respect to satellite RIC position and velocity offsets.

    x: emitter positions ``[P, 3]``, ECEF m. params: ``(sat_id, is_velocity, component)``
    with component 0/1/2 = radial / in-track / cross-track. t_s: row epochs ``[m]``.
    Returns ``[P, m, len(params)]`` in row units per metre (position) or per m/s
    (velocity).

    Every partial is a difference of the predictors evaluated with
    ``PerturbedOrbit(base, ric_pos, ric_vel, t_ref)`` in place of the satellite's orbit.
    The velocity parameter is defined with ``t_ref`` = the row's own epoch ``t_k``
    (MATH.md section 9). Because the perturbation is
    ``Q(t) ric_vel (t - t_k) = Q(t) ric_vel (t - t_0) - Q(t) ric_vel (t_k - t_0)``,
    i.e. the same velocity offset referred to a common ``t_0`` plus a position offset of
    ``-ric_vel (t_k - t_0)``, and the rows are linear in offsets this small,

        d h_k / d vel |t_ref = t_k  =  d h_k / d vel |t_ref = t_0  -  (t_k - t_0) d h_k / d pos,

    so one perturbed orbit per parameter serves the rows of every epoch (``t_0`` is the
    row epoch nearest the mean, so the correction vanishes for single-epoch sets).

    The differences are one-sided with steps of 1 km and 0.02 m/s, about the size of
    the sigmas they will be multiplied by. Larger velocity steps are not harmless: the
    step displaces the satellite by ``step * (t_k - t_0)``, and the product of that
    displacement with the velocity step is a second-order FDOA term of relative size
    ``step * (t_k - t_0) / range`` (4e-6 for rows two hours from ``t_0`` at
    geosynchronous range; 2e-4 with a 1 m/s step). The FDOA predictor's rounding noise
    of about 1e-8 Hz bounds the steps from below (1e-11 Hz/m, 6e-7 Hz per m/s).
    """
    out = np.zeros((x.shape[0], m, len(params)))
    if not params:
        return out
    if h_base is None:
        h_base = _predict_groups(x, groups, m, orbits, sites_xyz)
    by_sat: dict[str, list[tuple[int, bool, int]]] = {}
    for j, (sat, is_vel, comp) in enumerate(params):
        by_sat.setdefault(sat, []).append((j, is_vel, comp))
    for sat, items in by_sat.items():
        used = [g for g in groups if sat in (g.a.sat_id, g.b.sat_id)]
        if not used:
            continue
        rows = np.concatenate([g.rows for g in used])
        epochs = t_s[rows]
        t_0 = float(epochs[np.argmin(np.abs(epochs - np.mean(epochs)))])
        dt = epochs - t_0
        base = orbits[sat]
        position: dict[int, np.ndarray] = {}

        def shifted(ric_pos: np.ndarray, ric_vel: np.ndarray, step: float) -> np.ndarray:
            perturbed = dict(orbits)
            perturbed[sat] = PerturbedOrbit(base, ric_pos, ric_vel, t_0)
            h = _predict_groups(x, used, m, perturbed, sites_xyz)
            return (h[:, rows] - h_base[:, rows]) / step

        def position_partial(comp: int) -> np.ndarray:
            if comp not in position:
                offset = np.zeros(3)
                offset[comp] = _RIC_POS_STEP_M
                position[comp] = shifted(offset, _ZERO3, _RIC_POS_STEP_M)
            return position[comp]

        for j, is_vel, comp in items:
            if not is_vel:
                out[:, rows, j] = position_partial(comp)
                continue
            offset = np.zeros(3)
            offset[comp] = _RIC_VEL_STEP_MPS
            partial = shifted(_ZERO3, offset, _RIC_VEL_STEP_MPS)
            if np.any(dt != 0.0):
                partial = partial - dt * position_partial(comp)
            out[:, rows, j] = partial
    return out


def _analytic_partials(kind: str, a: PathSpec, b: PathSpec) -> dict[tuple[Any, ...], float]:
    """Partials of one row with respect to the clock, oscillator and bias parameters."""
    out: dict[tuple[Any, ...], float] = {}

    def add(key: tuple[Any, ...], value: float) -> None:
        out[key] = out.get(key, 0.0) + value

    if kind == "tdoa":
        add(("clock", a.site_id), -1.0)
        add(("clock", b.site_id), 1.0)
        end_a, end_b = (a.sat_id, a.site_id), (b.sat_id, b.site_id)
        if end_a <= end_b:
            add(("bias", end_a, end_b), 1.0)
        else:
            add(("bias", end_b, end_a), -1.0)
    else:
        add(("site_osc", a.site_id), -a.f_tune_hz)
        add(("site_osc", b.site_id), b.f_tune_hz)
        add(("sat_osc", a.sat_id), a.translation_hz)
        add(("sat_osc", b.sat_id), -b.translation_hz)
    return out


def _cal_sign(row: Measurement, cal: CalRef, name: str) -> float:
    """Orientation of a reference measurement relative to its target row: +1 same, -1 reversed."""
    sats_row = (row.a.sat_id, row.b.sat_id)
    sats_cal = (cal.a.sat_id, cal.b.sat_id)
    same = sats_cal == sats_row
    reversed_ = sats_cal == sats_row[::-1]
    if same and reversed_:  # one satellite on both paths: the sites tell the orientation
        sites_row = (row.a.site_id, row.b.site_id)
        sites_cal = (cal.a.site_id, cal.b.site_id)
        return -1.0 if (sites_cal == sites_row[::-1] and sites_cal != sites_row) else 1.0
    if same:
        return 1.0
    if reversed_:
        return -1.0
    raise ValueError(
        f"{name}: the reference was measured on satellites {sats_cal}, the row uses {sats_row}"
    )


def _same_cal(first: CalRef, second: CalRef) -> bool:
    return (
        first.a == second.a
        and first.b == second.b
        and int(first.t_ns) == int(second.t_ns)
        and float(first.sigma) == float(second.sigma)
        and np.array_equal(np.asarray(first.x_ref), np.asarray(second.x_ref))
    )


# --------------------------------------------------------------------------- weighting


class _Whitener:
    """Weighting by the total covariance ``S = R + G G^T`` of one set of rows.

    sigma: CAF one-sigmas ``[m]``. gain: ``G = H_c S_c^(1/2)`` ``[m, n]`` or ``None``.

    Everything works in sigma-normalised form, ``S = D (I + Gn Gn^T) D`` with
    ``D = diag(sigma)`` and ``Gn = D^-1 G``, and through the ``n x n`` Gram matrix
    ``Gn^T Gn = V diag(lam) V^T`` (``n``, the number of consider parameters, is small).
    With ``U = Gn V`` (orthogonal columns of squared length ``lam``):

        S^-1   = D^-1 (I - U diag(1 / (1 + lam)) U^T) D^-1
        S^-1/2 = (I - U diag(1 / (q (1 + q))) U^T) D^-1,      q = sqrt(1 + lam)

    ``S^-1/2`` is the symmetric whitener handed to Levenberg-Marquardt. It differs from
    the Cholesky whitener ``L^-1`` (``S = L L^T``) of MATH.md section 9 by an orthogonal
    matrix only, so the cost ``|S^-1/2 r|^2 = |L^-1 r|^2 = r^T S^-1 r``, its minimiser
    and ``J^T S^-1 J`` are identical, while no ``m x m`` factorisation is needed inside
    the iteration. ``cholesky`` returns ``L^-1 r`` itself for the reported residuals.

    (``U`` is kept as a factor rather than multiplied out to ``Gn V diag(.) V^T``:
    redundant consider parameters give ``lam = 0`` directions whose weight of 1/2 would
    swamp the ``1 / lam`` weights of strongly constrained ones in a dense product.)
    """

    def __init__(self, sigma: np.ndarray, gain: np.ndarray | None) -> None:
        self.inv_sigma = 1.0 / sigma
        self.gain_n: np.ndarray | None = None
        if gain is not None and gain.shape[1]:
            gain_n = gain * self.inv_sigma[:, None]
            lam, vec = np.linalg.eigh(gain_n.T @ gain_n)
            lam = np.clip(lam, 0.0, None)
            root = np.sqrt(1.0 + lam)
            self.gain_n = gain_n
            self._u = gain_n @ vec  # [m, n]
            self._half = 1.0 / (root * (1.0 + root))
            self._full = 1.0 / (1.0 + lam)

    def __call__(self, r: np.ndarray) -> np.ndarray:
        """``S^-1/2 r`` along the last axis of ``r`` (``[..., m]``, row units): dimensionless."""
        y = r * self.inv_sigma
        if self.gain_n is None:
            return y
        return y - ((y @ self._u) * self._half) @ self._u.T

    def solve_normalised(self, y: np.ndarray) -> np.ndarray:
        """``(I + Gn Gn^T)^-1 y`` along the last axis of the sigma-normalised ``y`` (``[..., m]``)."""
        if self.gain_n is None:
            return y
        return y - ((y @ self._u) * self._full) @ self._u.T

    def cholesky(self, r: np.ndarray) -> np.ndarray:
        """``L^-1 r`` for one residual vector ``r`` (``[m]``), with ``S = L L^T`` (lower Cholesky)."""
        y = r * self.inv_sigma
        if self.gain_n is None:
            return y
        normal = self.gain_n @ self.gain_n.T
        normal[np.diag_indices_from(normal)] += 1.0
        return solve_triangular(np.linalg.cholesky(normal), y, lower=True, check_finite=False)


# --------------------------------------------------------------------------- global search


def _newest_two(measurements: Sequence[Measurement]) -> list[int]:
    """Indices of the newest two rows of each (pair, kind), in input order."""
    buckets: dict[tuple[PathSpec, PathSpec, str], list[int]] = {}
    for k, row in enumerate(measurements):
        buckets.setdefault((row.a, row.b, row.kind), []).append(k)
    keep: list[int] = []
    for idx in buckets.values():
        idx.sort(key=lambda k: (int(measurements[k].t_ns), k))
        keep.extend(idx[-2:])
    return sorted(keep)


def _full_circle(region: Any, lon: np.ndarray, step: float) -> bool:
    span = getattr(region, "lon_span_deg", None)
    if callable(span):
        return bool(float(span()) >= 360.0 - 1e-9)
    return bool(lon.size > 1 and (lon[-1] - lon[0]) + step >= 360.0 - 1e-9)


def _contains(region: Any, lat: np.ndarray, lon: np.ndarray, as_given: bool = False) -> np.ndarray:
    """``region.contains`` whatever longitude convention its implementation expects.

    Asked with the longitudes normalised to ``[-180, 180)`` and, when the region
    exposes ``lon_min``, unwrapped to ``[lon_min, lon_min + 360)`` (the convention of
    ``region.grid``); with ``as_given`` also with ``lon`` untouched (grid longitudes).
    A point counts as inside if any of these says so.
    """
    inside = np.asarray(region.contains(lat, np.asarray(wrap_lon_deg(lon), dtype=np.float64)), dtype=bool)
    lon_min = getattr(region, "lon_min", None)
    if isinstance(lon_min, (int, float, np.integer, np.floating)) and np.isfinite(lon_min):
        unwrapped = float(lon_min) + np.mod(np.asarray(lon, dtype=np.float64) - float(lon_min), 360.0)
        inside = inside | np.asarray(region.contains(lat, unwrapped), dtype=bool)
    if as_given:
        inside = inside | np.asarray(region.contains(lat, lon), dtype=bool)
    return inside


def above_mask(
    lat_deg: np.ndarray, lon_deg: np.ndarray, x: np.ndarray, r_sat: np.ndarray, min_elevation_deg: float
) -> np.ndarray:
    """Whether a satellite at ``r_sat`` is at or above an elevation mask from ground points.

    lat_deg, lon_deg: geodetic coordinates of the points (any common shape ``S``);
    x: the same points in ECEF m, ``S + (3,)``; r_sat: satellite position, ECEF m
    ``[3]``. Elevation is measured from the plane normal to the WGS84 vertical, with
    no refraction (the definition of ``geometry.elevation_deg``), tested as
    ``d . up >= sin(mask) |d|`` without forming the angle.
    """
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    cos_lat = np.cos(lat)
    dx = r_sat[0] - x[..., 0]
    dy = r_sat[1] - x[..., 1]
    dz = r_sat[2] - x[..., 2]
    vertical = dx * (cos_lat * np.cos(lon)) + dy * (cos_lat * np.sin(lon)) + dz * np.sin(lat)
    return vertical >= np.sin(np.radians(min_elevation_deg)) * np.sqrt(dx * dx + dy * dy + dz * dz)


def _local_minima(cost: np.ndarray, wrap_lon: bool) -> np.ndarray:
    """Cells of a ``[n_lat, n_lon]`` cost grid not above any of their 8 neighbours (inf = no cell)."""
    n_lat, n_lon = cost.shape
    padded = np.full((n_lat + 2, n_lon + 2), np.inf)
    padded[1:-1, 1:-1] = cost
    if wrap_lon and n_lon > 1:
        padded[1:-1, 0] = cost[:, -1]
        padded[1:-1, -1] = cost[:, 0]
    out = np.isfinite(cost)
    for di in range(3):
        for dj in range(3):
            if di != 1 or dj != 1:
                out &= cost <= padded[di : di + n_lat, dj : dj + n_lon]
    return out


def _grid_starts(
    rows: _Rows,
    measurements: Sequence[Measurement],
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    uncertainty: Uncertainty | None,
    cfg: _SolverCfg,
    region: Any,
) -> list[tuple[float, float]]:
    """Global search: local minima of the grid cost, lowest first (MATH.md section 9, Start)."""
    measurements = list(measurements)
    keep = _newest_two(measurements)
    sub = rows if len(keep) == rows.m else _Rows([measurements[k] for k in keep], orbits, sites_xyz, uncertainty)
    lat, lon = (np.asarray(v, dtype=np.float64).reshape(-1) for v in region.grid(cfg.grid_step_deg))
    if lat.size == 0 or lon.size == 0:
        return []
    lat2, lon2 = np.meshgrid(lat, lon, indexing="ij")
    points = lla_to_ecef(lat2, lon2, cfg.height_m)
    admissible = _contains(region, lat2, lon2, as_given=True)
    for sat, epochs in sub.sat_epochs.items():
        for r_sat in orbit_position(orbits[sat], epochs):
            admissible &= above_mask(lat2, lon2, points, r_sat, cfg.min_elevation_deg)
    if not np.any(admissible):
        return []
    x = points[admissible]

    gain = None
    if sub.n_consider:
        centroid = np.mean(x, axis=0)
        if float(np.linalg.norm(centroid)) < 1e3:  # cells spread evenly round the globe
            centroid = x[x.shape[0] // 2]
        c_lat, c_lon, _ = ecef_to_lla(centroid)
        gain = sub.consider(lla_to_ecef(float(c_lat), float(c_lon), cfg.height_m)[None, :])[0]
    whitener = _Whitener(sub.sigma, gain)
    cost = np.full(admissible.shape, np.inf)
    cost[admissible] = np.sum(whitener(sub.z - sub.predict(x)) ** 2, axis=-1)

    minima = np.flatnonzero(_local_minima(cost, _full_circle(region, lon, cfg.grid_step_deg)))
    order = minima[np.argsort(cost.reshape(-1)[minima], kind="stable")][: cfg.n_starts]
    lat_flat, lon_flat = lat2.reshape(-1), lon2.reshape(-1)
    return [(float(lat_flat[k]), float(wrap_lon_deg(lon_flat[k]))) for k in order]


# --------------------------------------------------------------------------- refinement


@dataclass
class _Fit:
    """One refined start: where it ended and the weighting it ended with."""

    lat: float
    lon: float
    chi2: float
    converged: bool
    whitener: _Whitener
    flat: bool = False  # abandoned: the position stayed unobservable along the iteration


def _fold(lat: Any, lon: Any) -> tuple[np.ndarray, np.ndarray]:
    """Continue (lat, lon) smoothly over the poles: latitude back into [-90, 90]."""
    lat = np.mod(np.asarray(lat, dtype=np.float64) + 90.0, 360.0) - 90.0
    lon = np.asarray(lon, dtype=np.float64)
    over = lat > 90.0
    return np.where(over, 180.0 - lat, lat), np.where(over, lon + 180.0, lon)


def _metres_to_degrees(lat: float, height: float) -> tuple[float, float]:
    """Degrees of longitude per metre east and of latitude per metre north at ``lat``."""
    meridional, prime_vertical = radii_of_curvature(lat)
    cos_lat = max(float(np.cos(np.radians(lat))), 1e-6)
    return (
        float(np.degrees(1.0 / ((prime_vertical + height) * cos_lat))),
        float(np.degrees(1.0 / (meridional + height))),
    )


_STENCIL_E = np.array([0.0, 1.0, -1.0, 0.0, 0.0])  # the point, then one step east, west,
_STENCIL_N = np.array([0.0, 0.0, 0.0, 1.0, -1.0])  # north and south of it


def _stencil_predictions(rows: _Rows, lat: float, lon: float, height: float, step_m: float) -> np.ndarray:
    """Predictions ``[5, m]`` at ``(lat, lon)`` and ``step_m`` east, west, north and south of it.

    Metres are converted at ``lat``: ``dE = (N + h) cos(lat) dlon``, ``dN = (M + h) dlat``.
    """
    deg_e, deg_n = _metres_to_degrees(lat, height)
    lat5, lon5 = _fold(lat + deg_n * step_m * _STENCIL_N, lon + deg_e * step_m * _STENCIL_E)
    return rows.predict(lla_to_ecef(lat5, lon5, height))


class _Flat(Exception):
    """Raised inside a refinement whose iterates stay unobservable."""


def _levenberg_marquardt(
    rows: _Rows, lat0: float, lon0: float, height: float, whitener: _Whitener
) -> _Fit:
    """Refine one start with all rows under the weighting ``whitener``.

    ``scipy.optimize.least_squares(method="lm")`` on the whitened residuals
    ``S^-1/2 r`` (see ``_Whitener``; twice the final cost is ``r^T S^-1 r``).
    The variables are metres east and north of the start (converted to degrees with
    the radii of curvature at the start) plus a constant 1e7 m, which only gives the
    relative step tolerance an absolute meaning (``xtol`` 1e-10 stops at millimetre
    steps wherever the start is). The Jacobian is numeric: central differences of the
    predictors over +/- 1 km, taken in the same vectorised call as the residual. (The
    FDOA predictor carries about 1e-8 Hz of rounding noise; over the 0.2 m forward step
    scipy would choose, that is tens of percent of a geosynchronous FDOA gradient.)

    When the rows do not determine the position (``J^T S^-1 J`` fails the
    observability rule) the cost has a flat valley along which LM creeps for hundreds
    of evaluations. A refinement whose last 5 evaluations were all unobservable is
    abandoned and reported with ``flat`` true and ``converged`` false.
    """
    deg_e, deg_n = _metres_to_degrees(lat0, height)
    last: dict[str, np.ndarray] = {}
    flat_run = 0
    min_eigenvalue = 1.0 / _MAX_SEMI_MAJOR_M**2

    def evaluate(u: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        east, north = float(u[0]) - _LM_OFFSET_M, float(u[1]) - _LM_OFFSET_M
        lat5, lon5 = _fold(
            lat0 + deg_n * (north + _LM_JAC_STEP_M * _STENCIL_N),
            lon0 + deg_e * (east + _LM_JAC_STEP_M * _STENCIL_E),
        )
        w = whitener(rows.z - rows.predict(lla_to_ecef(lat5, lon5, height)))
        jac = np.stack([w[1] - w[2], w[3] - w[4]], axis=1) / (2.0 * _LM_JAC_STEP_M)
        return w[0], jac

    def fun(u: np.ndarray) -> np.ndarray:
        nonlocal flat_run
        f, jac = evaluate(u)
        last["u"], last["jac"] = np.array(u, dtype=np.float64), jac
        a, b, d = float(jac[:, 0] @ jac[:, 0]), float(jac[:, 0] @ jac[:, 1]), float(jac[:, 1] @ jac[:, 1])
        smallest = 0.5 * (a + d) - float(np.hypot(0.5 * (a - d), b))  # of J^T S^-1 J
        flat_run = 0 if smallest > min_eigenvalue else flat_run + 1
        if flat_run >= _LM_FLAT_EVALUATIONS:
            raise _Flat
        return f

    def jac_fn(u: np.ndarray) -> np.ndarray:
        if "u" in last and np.array_equal(last["u"], u):
            return last["jac"]
        return evaluate(u)[1]

    try:
        result = least_squares(
            fun,
            np.array([_LM_OFFSET_M, _LM_OFFSET_M]),
            jac=jac_fn,
            method="lm",
            xtol=1e-10,
            ftol=1e-12,
            gtol=1e-12,
            max_nfev=_LM_MAX_NFEV,
        )
    except _Flat:
        return _Fit(lat0, lon0, float("inf"), False, whitener, flat=True)
    east, north = float(result.x[0]) - _LM_OFFSET_M, float(result.x[1]) - _LM_OFFSET_M
    lat, lon = _fold(lat0 + deg_n * north, lon0 + deg_e * east)
    chi2 = 2.0 * float(result.cost)
    converged = bool(result.status > 0 and np.isfinite(chi2) and np.isfinite(lat) and np.isfinite(lon))
    return _Fit(float(lat), float(wrap_lon_deg(lon)), chi2, converged, whitener)


def _distinct(lat: Sequence[float], lon: Sequence[float], chi2: Sequence[float]) -> list[int]:
    """Indices of points that are not within 1 km (geodesic) of a lower-cost point, lowest cost first."""
    order = np.argsort(np.asarray(chi2, dtype=np.float64), kind="stable")
    kept: list[int] = []
    for k in order:
        if kept:
            d = geodesic_distance_m(lat[k], lon[k], np.array([lat[j] for j in kept]), np.array([lon[j] for j in kept]))
            if float(np.min(d)) <= _DUPLICATE_M:
                continue
        kept.append(int(k))
    return kept


def _mirror_starts(fits: Sequence[_Fit]) -> list[tuple[float, float]]:
    """Equatorial mirrors of the distinct refined minima that no refined point already covers."""
    good = [f for f in fits if f.converged]
    if not good:
        return []
    lat = [f.lat for f in good]
    lon = [f.lon for f in good]
    out: list[tuple[float, float]] = []
    for k in _distinct(lat, lon, [f.chi2 for f in good]):
        m_lat, m_lon = -lat[k], lon[k]
        others_lat = np.array(lat + [p[0] for p in out])
        others_lon = np.array(lon + [p[1] for p in out])
        if float(np.min(geodesic_distance_m(m_lat, m_lon, others_lat, others_lon))) > _DUPLICATE_M:
            out.append((m_lat, m_lon))
    return out


def _refine_with_mirrors(rows: _Rows, starts: Sequence[tuple[float, float]], height: float) -> list[_Fit]:
    """Refine every start and the mirror of every refined minimum (MATH.md section 9).

    With position-dependent consider partials (satellite ephemeris terms) each start
    is refined twice: with ``H_c`` taken at the start, then with ``H_c`` taken at the
    converged point. The partials of all points of a stage are evaluated in one
    vectorised pass; the mirrors (taken from the first-stage minima) share the pass
    of the second stage. Abandoned refinements (``flat``) are returned as they are.
    """

    def stage(points: Sequence[tuple[float, float]], whiteners: Sequence[_Whitener]) -> list[_Fit]:
        return [
            _levenberg_marquardt(rows, lat, lon, height, whitener)
            for (lat, lon), whitener in zip(points, whiteners)
        ]

    def whiteners_at(points: Sequence[tuple[float, float]]) -> list[_Whitener]:
        if not points:
            return []
        lat, lon = np.array(points).T
        gain = rows.consider(lla_to_ecef(lat, lon, height))
        assert gain is not None
        return [_Whitener(rows.sigma, g) for g in gain]

    if not rows.position_dependent:
        shared = _Whitener(rows.sigma, rows.constant_gain)
        fits = stage(starts, [shared] * len(starts))
        mirrors = _mirror_starts(fits)
        return fits + stage(mirrors, [shared] * len(mirrors))

    first = stage(starts, whiteners_at(starts))
    again = [(f.lat, f.lon) for f in first if not f.flat]
    mirrors = _mirror_starts(first)
    whiteners = whiteners_at(again + mirrors)
    mirror_first = stage(mirrors, whiteners[len(again) :])
    mirror_again = [(f.lat, f.lon) for f in mirror_first if not f.flat]
    return (
        [f for f in first + mirror_first if f.flat]
        + stage(again, whiteners[: len(again)])
        + stage(mirror_again, whiteners_at(mirror_again))
    )


# --------------------------------------------------------------------------- selection


def _admissible(rows: _Rows, lat: np.ndarray, lon: np.ndarray, cfg: _SolverCfg, region: Any) -> np.ndarray:
    """Inside the region with every satellite at or above the mask at every epoch it is used."""
    ok = _contains(region, lat, lon)
    x = lla_to_ecef(lat, lon, cfg.height_m)
    for sat, epochs in rows.sat_epochs.items():
        elevation = np.asarray(elevation_deg(x, rows.orbits[sat], epochs))
        ok = ok & np.all(elevation >= cfg.min_elevation_deg, axis=-1)
    return ok


def _normal_matrix(
    rows: _Rows, lat: float, lon: float, height: float, whitener: _Whitener
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(h, (D S^-1 J)^T, D^-1 J, J^T S^-1 J)`` at ``(lat, lon)``.

    ``h``: the stencil predictions ``[5, m]`` (the point first); ``J``: central
    differences of ``h`` over 100 m east and north, ``[m, 2]``; ``D = diag(sigma)``.
    """
    h = _stencil_predictions(rows, lat, lon, height, _JAC_STEP_M)
    jac = np.stack([h[1] - h[2], h[3] - h[4]], axis=1) / (2.0 * _JAC_STEP_M)  # d h / d (east, north)
    jac_n = jac * whitener.inv_sigma[:, None]  # D^-1 J, [m, 2]
    solved = whitener.solve_normalised(jac_n.T)  # (D S^-1 J)^T, [2, m]
    return h, solved, jac_n, solved @ jac_n


def _escape_saddles(rows: _Rows, fits: Sequence[_Fit], height: float) -> list[_Fit]:
    """Restart from either side of converged points that fail the observability rule.

    Levenberg-Marquardt stops wherever the gradient of the cost vanishes, which is also
    true at a saddle whose descent direction the rows' Jacobian cannot see: on the
    equator under equatorial satellites every row is symmetric in latitude, so a start
    on the equator converges to the equator, where ``J`` has no north component, while
    the minima lie north and south of it. For each such point (``J^T S^-1 J`` not
    positive definite or its one-sigma semi-major above 1e7 m) the start is repeated
    10 km either side along the unobservable axis (with mirrors, as any start). Those
    refinements join the candidates; the point itself is dropped when one of them
    reaches a chi2 lower by more than the tie tolerance, and kept otherwise (a minimum
    the rows cannot pin down, to which the observability rule then applies).
    """
    out: list[_Fit] = []
    for fit in fits:
        if not fit.converged:
            out.append(fit)
            continue
        normal = _normal_matrix(rows, fit.lat, fit.lon, height, fit.whitener)[3]
        if not np.all(np.isfinite(normal)):
            out.append(fit)
            continue
        lam, vec = np.linalg.eigh(0.5 * (normal + normal.T))
        if lam[0] > 0.0 and 1.0 / np.sqrt(lam[0]) <= _MAX_SEMI_MAJOR_M:
            out.append(fit)
            continue
        deg_e, deg_n = _metres_to_degrees(fit.lat, height)
        east, north = vec[0, 0], vec[1, 0]  # the weakest direction, metres east / north
        starts = []
        for sign in (1.0, -1.0):
            lat, lon = _fold(fit.lat + sign * _ESCAPE_M * north * deg_n, fit.lon + sign * _ESCAPE_M * east * deg_e)
            starts.append((float(lat), float(wrap_lon_deg(lon))))
        escaped = _refine_with_mirrors(rows, starts, height)
        out.extend(escaped)
        if not any(e.converged and e.chi2 < fit.chi2 - _CHI2_TIE for e in escaped):
            out.append(fit)
    return out


@dataclass
class _Covariance:
    total: np.ndarray
    noise: np.ndarray
    consider: np.ndarray
    budget: dict[str, float]
    dominant: str
    residuals: np.ndarray  # L^-1 r (Cholesky-whitened), [m]
    chi2: float  # r^T S^-1 r


def _covariance_at(
    rows: _Rows, lat: float, lon: float, height: float, whitener: _Whitener
) -> _Covariance | None:
    """East/north covariance of a fix at ``(lat, lon)``; ``None`` when unobservable.

    ``J`` by central differences with 100 m steps in east and north,
    ``P_total = (J^T S^-1 J)^-1``, ``K = P_total J^T S^-1``, ``P_noise = K R K^T``,
    ``P_consider = K H_c S_c H_c^T K^T``; the total returned is their sum, which equals
    ``P_total`` to rounding. The observability rule of MATH.md section 9 is applied to
    ``J^T S^-1 J``.
    """
    h, solved, jac_n, normal = _normal_matrix(rows, lat, lon, height, whitener)
    if not np.all(np.isfinite(normal)):
        return None
    lam, vec = np.linalg.eigh(0.5 * (normal + normal.T))
    if not lam[0] > 0.0 or 1.0 / np.sqrt(lam[0]) > _MAX_SEMI_MAJOR_M:
        return None
    k_n = ((vec / lam) @ vec.T) @ solved  # K D with K = P_total J^T S^-1, [2, m]
    noise = k_n @ k_n.T  # K R K^T
    noise = 0.5 * (noise + noise.T)
    budget = dict.fromkeys(ERROR_SOURCES, 0.0)
    if whitener.gain_n is None:
        consider = np.zeros((2, 2))
    else:
        projected = k_n @ whitener.gain_n  # K H_c S_c^(1/2), [2, n]
        consider = projected @ projected.T
        consider = 0.5 * (consider + consider.T)
        for name, column in zip(rows.categories, projected.T):
            budget[name] += float(column @ column)
    budget["noise"] = float(np.trace(noise))
    dominant = max(ERROR_SOURCES, key=lambda name: budget[name])
    residual = rows.z - h[0]
    symmetric = whitener(residual)  # chi2 from the well-conditioned form; |L^-1 r|^2 equals it
    chi2 = float(symmetric @ symmetric)
    return _Covariance(noise + consider, noise, consider, budget, dominant, whitener.cholesky(residual), chi2)


def _build(rows: _Rows, fit: _Fit, cfg: _SolverCfg) -> Solution | None:
    stats = _covariance_at(rows, fit.lat, fit.lon, cfg.height_m, fit.whitener)
    if stats is None:
        return None
    dof = rows.m - 2
    return Solution(
        lat_deg=fit.lat,
        lon_deg=fit.lon,
        height_m=cfg.height_m,
        cov_en_m2=stats.total,
        cov_noise_en_m2=stats.noise,
        cov_consider_en_m2=stats.consider,
        ellipse=error_ellipse(stats.total, cfg.confidence),
        cep50_m=cep50(stats.total),
        chi2=stats.chi2,
        dof=dof,
        reduced_chi2=stats.chi2 / dof if dof >= 1 else None,
        residuals=stats.residuals,
        ambiguities=[],
        ambiguity_resolved_by="none",
        converged=True,
        dominant_error=stats.dominant,
        n_measurements=rows.m,
    )


def _select(
    rows: _Rows, fits: Sequence[_Fit], ok: np.ndarray, cfg: _SolverCfg
) -> tuple[Solution | None, str | None]:
    """Order the converged points, pick the solution and its ambiguities (MATH.md section 9)."""
    inside = [f for f, good in zip(fits, ok) if good]
    outside = [f for f, good in zip(fits, ok) if not good]
    if not inside:
        return None, "no admissible solution"
    keep = _distinct([f.lat for f in inside], [f.lon for f in inside], [f.chi2 for f in inside])
    candidates = [inside[k] for k in keep]  # ascending chi2
    # ties within 1e-6 in chi2 are ordered by descending latitude
    ordered: list[_Fit] = []
    start = 0
    while start < len(candidates):
        stop = start + 1
        while stop < len(candidates) and candidates[stop].chi2 - candidates[start].chi2 <= _CHI2_TIE:
            stop += 1
        ordered.extend(sorted(candidates[start:stop], key=lambda f: -f.lat))
        start = stop
    best = ordered[0]
    rivals = [f for f in ordered[1:] if f.chi2 - best.chi2 <= cfg.chi2_ambiguity]

    solution = _build(rows, best, cfg)
    if solution is None:
        return None, "unobservable"
    for fit in rivals:
        ambiguity = _build(rows, fit, cfg)
        if ambiguity is None:
            # the rows are as consistent with a place they do not pin down as with the fix
            return None, "unobservable"
        ambiguity.ambiguity_resolved_by = "unresolved"
        solution.ambiguities.append(ambiguity)

    # second minima that were discarded as inadmissible (not duplicates of a kept candidate)
    kept_lat = np.array([f.lat for f in ordered])
    kept_lon = np.array([f.lon for f in ordered])
    discarded = [
        f
        for f in outside
        if float(np.min(geodesic_distance_m(f.lat, f.lon, kept_lat, kept_lon))) > _DUPLICATE_M
    ]
    if solution.ambiguities:
        solution.ambiguity_resolved_by = "unresolved"
    elif any(f.chi2 - best.chi2 <= cfg.chi2_ambiguity for f in discarded):
        solution.ambiguity_resolved_by = "region"
    elif len(ordered) > 1 or discarded:
        solution.ambiguity_resolved_by = "measurements"
    else:
        solution.ambiguity_resolved_by = "none"
    return solution, None


# --------------------------------------------------------------------------- validation helpers


def _finite(value: Any, name: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not np.isfinite(out):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def _integer(value: Any, name: str) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be an integer number of nanoseconds, got {value!r}")
    try:
        return operator.index(value)
    except TypeError:
        raise ValueError(
            f"{name} must be an integer number of nanoseconds, got {type(value).__name__}"
        ) from None


def _vector3(value: Any, name: str) -> np.ndarray:
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be 3 finite ECEF coordinates") from None
    if arr.shape != (3,) or not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be 3 finite ECEF coordinates, got shape {arr.shape}")
    return arr


def _point(lat: Any, lon: Any, lat_name: str, lon_name: str) -> tuple[float, float]:
    lat_f = _finite(lat, lat_name)
    lon_f = _finite(lon, lon_name)
    if not -90.0 <= lat_f <= 90.0:
        raise ValueError(f"{lat_name} must be within [-90, 90], got {lat!r}")
    return lat_f, float(wrap_lon_deg(lon_f))


def _check_path(path: Any, name: str, orbits: Mapping[str, Any], sites_xyz: Mapping[str, Any]) -> None:
    if not isinstance(path, PathSpec):
        raise ValueError(f"{name} must be a PathSpec")
    if path.sat_id not in orbits:
        raise ValueError(f"{name}.sat_id={path.sat_id!r} is not in orbits")
    if path.site_id not in sites_xyz:
        raise ValueError(f"{name}.site_id={path.site_id!r} is not in sites_xyz")
    _finite(path.f_e_hz, f"{name}.f_e_hz")
    _finite(path.translation_hz, f"{name}.translation_hz")
    _finite(path.tau_x_s, f"{name}.tau_x_s")


def _check_cal(cal: Any, name: str, orbits: Mapping[str, Any], sites_xyz: Mapping[str, Any]) -> None:
    if not isinstance(cal, CalRef):
        raise ValueError(f"{name} must be a CalRef or None")
    if not isinstance(cal.cal_id, str) or not cal.cal_id:
        raise ValueError(f"{name}.cal_id must be a non-empty string")
    _vector3(cal.x_ref, f"{name}.x_ref")
    _check_path(cal.a, f"{name}.a", orbits, sites_xyz)
    _check_path(cal.b, f"{name}.b", orbits, sites_xyz)
    _integer(cal.t_ns, f"{name}.t_ns")
    if _finite(cal.sigma, f"{name}.sigma") < 0.0:
        raise ValueError(f"{name}.sigma must not be negative, got {cal.sigma!r}")


def _check_uncertainty(unc: Any) -> Uncertainty | None:
    if unc is None:
        return None
    if not isinstance(unc, Uncertainty):
        raise ValueError("uncertainty must be an Uncertainty or None")
    for field_name in ("sat_pos_ric_m", "sat_vel_ric_mps"):
        table = getattr(unc, field_name)
        if not isinstance(table, Mapping):
            raise ValueError(f"uncertainty.{field_name} must be a mapping of sat_id to 3 sigmas")
        for sat, value in table.items():
            arr = np.asarray(value, dtype=np.float64)
            if arr.shape != (3,) or not np.all(np.isfinite(arr)) or np.any(arr < 0.0):
                raise ValueError(
                    f"uncertainty.{field_name}[{sat!r}] must be 3 finite non-negative sigmas (radial, in-track, cross-track)"
                )
    for field_name in ("site_clock_s", "site_osc_frac", "sat_osc_frac"):
        table = getattr(unc, field_name)
        if not isinstance(table, Mapping):
            raise ValueError(f"uncertainty.{field_name} must be a mapping of id to sigma")
        for key, value in table.items():
            if _finite(value, f"uncertainty.{field_name}[{key!r}]") < 0.0:
                raise ValueError(f"uncertainty.{field_name}[{key!r}] must not be negative")
    if _finite(unc.tdoa_bias_s, "uncertainty.tdoa_bias_s") < 0.0:
        raise ValueError("uncertainty.tdoa_bias_s must not be negative")
    return unc


def _covariance2(value: Any, name: str) -> np.ndarray:
    try:
        cov = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a 2x2 covariance matrix") from None
    if cov.shape != (2, 2) or not np.all(np.isfinite(cov)):
        raise ValueError(f"{name} must be a finite 2x2 covariance matrix, got shape {cov.shape}")
    scale = max(abs(cov[0, 0]), abs(cov[1, 1]))
    if abs(cov[0, 1] - cov[1, 0]) > 1e-9 * scale:
        raise ValueError(f"{name} must be symmetric")
    cov = 0.5 * (cov + cov.T)
    if cov[0, 0] < 0.0 or cov[1, 1] < 0.0 or np.linalg.eigvalsh(cov)[0] < -1e-9 * scale:
        raise ValueError(f"{name} must be positive semi-definite")
    return cov
