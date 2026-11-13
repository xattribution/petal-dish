"""Tests of the propagation-media consider model and the other estimation-model terms of
MATH.md section 9: ionosphere and troposphere per ground end (core/media.py, solver,
simulator truth), the noise pairs share through one capture, and the satellite motion
between a reference measurement and a later target row.

Independent references: the simulator's own ionospheric and tropospheric truth
(sim/world.py builds them from core.iono.group_delay_s and its own elevation code), the
noise-free predictors with PerturbedOrbit truth, and dense covariance algebra.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from petal_locate.config.loader import load_config
from petal_locate.config.models import SystemConfig
from petal_locate.core import solver as sv
from petal_locate.core.geodesy import lla_to_ecef
from petal_locate.core.geometry import predict_pair
from petal_locate.core.media import leg_partials, row_media_partials, tropo_delay_s, tropo_mapping
from petal_locate.core.orbits import PerturbedOrbit
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.core.types import CalRef, Measurement, PathSpec, Uncertainty
from petal_locate.sim import Scenario
from petal_locate.sim.world import SimWorld

ROOT = Path(__file__).resolve().parents[1]
T03 = ns_from_iso("2026-01-01T03:00:00Z")
F_J = 301.005e6
TR = {"muos-pac": -41.0e6, "uhf-172e": -33.6e6, "muos-io": -41.0e6}


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(ROOT / "config" / "pacific-uhf.yaml")


def scenario(lat: float, lon: float, iono: float | None = None, tropo: float | None = None) -> Scenario:
    return Scenario.model_validate({
        "name": "media", "seed": 3, "description": "Simulated media probe (not real data).",
        "emitters": [{"id": "j", "name": "probe (simulated)", "kind": "jammer", "lat_deg": lat, "lon_deg": lon,
                      "freq_hz": F_J, "eirp_dbw": 20.0, "waveform": {"type": "noise", "bandwidth_hz": 10e3}}],
        "errors": {"iono": {"enabled": iono is not None, "vtec_tecu": iono or 1.0},
                   "tropo": {"enabled": tropo is not None, "zenith_m": tropo or 1.0}},
    })


# --------------------------------------------------------------------------- partials against simulator truth


def test_mapping_function_and_tropo_delay() -> None:
    assert tropo_mapping(90.0) == pytest.approx(1.0, abs=1e-3)
    assert tropo_mapping(0.0) == pytest.approx(1.001 / math.sqrt(0.002001), rel=1e-12)  # 22.4, finite
    assert tropo_mapping(30.0) == pytest.approx(1.001 / math.sqrt(0.002001 + 0.25), rel=1e-12)
    assert tropo_delay_s(2.3, 90.0) == pytest.approx(2.3 * 1.0 / 299_792_458.0, rel=1e-3)
    with pytest.raises(ValueError):
        tropo_delay_s(-1.0, 45.0)


@pytest.mark.parametrize("pair", [
    (("muos-pac", "honolulu"), ("muos-io", "seoul")),
    (("muos-pac", "naha"), ("uhf-172e", "naha")),
])
def test_row_partials_match_the_simulators_truth(cfg: SystemConfig, pair) -> None:
    """VTEC (TECU) times the ionospheric partials, and zenith delay (m) times the
    tropospheric ones, give the TDOA and FDOA the simulator adds with that ionosphere or
    troposphere switched on, signs included (group delay and phase advance for the
    ionosphere, delay of both for the troposphere)."""
    (sat_a, site_a), (sat_b, site_b) = pair
    a, b = PathSpec(sat_a, site_a, F_J, TR[sat_a]), PathSpec(sat_b, site_b, F_J, TR[sat_b])
    lat, lon = 20.0, 132.0
    x = lla_to_ecef(lat, lon, 0.0)
    base = np.array(SimWorld(cfg, scenario(lat, lon)).truth_measurement("j", a, b, T03))
    for medium, value, kw in (("ionosphere", 30.0, {"iono": 30.0}), ("troposphere", 2.3, {"tropo": 2.3})):
        truth = np.array(SimWorld(cfg, scenario(lat, lon, **kw)).truth_measurement("j", a, b, T03)) - base
        model = []
        for kind in ("tdoa", "fdoa"):
            parts = row_media_partials(kind, a, b, np.array([sec(T03)]), x, cfg.orbits(), cfg.sites_xyz())[medium]
            model.append(value * sum(float(np.asarray(v).reshape(-1)[0]) for v in parts.values()))
        assert abs(truth[0]) > (1e-7 if medium == "ionosphere" else 1e-9)  # the effect is there
        assert model[0] == pytest.approx(truth[0], rel=1e-3)
        assert model[1] == pytest.approx(truth[1], rel=2e-3, abs=1e-7)


def test_leg_partials_scale_with_frequency(cfg: SystemConfig) -> None:
    """Ionosphere 1 / f^2 in delay (434 ns per 30 TECU at 305 MHz, zenith: MATH.md's check),
    the troposphere independent of frequency."""
    site = np.asarray(cfg.sites_xyz()["honolulu"], dtype=np.float64)
    orbit = cfg.orbits()["muos-pac"]
    t = np.array([sec(T03)])
    low, high = leg_partials(site, orbit, t, 300e6), leg_partials(site, orbit, t, 12e9)
    assert low["ionosphere"][0][0] / high["ionosphere"][0][0] == pytest.approx((12e9 / 300e6) ** 2, rel=1e-9)
    assert low["troposphere"][0][0] == pytest.approx(high["troposphere"][0][0], rel=1e-12)


# --------------------------------------------------------------------------- the solver with an ionosphere


def _calibrated_rows(cfg: SystemConfig, world: SimWorld, lat: float, lon: float, rng: np.random.Generator,
                     sig_t: float = 92e-9, sig_f: float = 0.92e-3) -> list[Measurement]:
    """Two tasking pairs, both kinds, calibrated against ref-guam, values from the
    simulator's truth (its ionosphere included) plus CAF noise."""
    orbits, sites = cfg.orbits(), cfg.sites_xyz()
    x_ref = np.asarray(cfg.carrier_xyz("ref-guam"), dtype=np.float64)
    f_ref = float(cfg.carrier("ref-guam").uplink_center_hz)
    rows = []
    for k, ((sa, ga), (sb, gb)) in enumerate(((("muos-pac", "honolulu"), ("uhf-172e", "naha")),
                                              (("muos-pac", "honolulu"), ("muos-io", "seoul")))):
        a, b = PathSpec(sa, ga, F_J, TR[sa]), PathSpec(sb, gb, F_J, TR[sb])
        ra, rb = PathSpec(sa, ga, f_ref, TR[sa]), PathSpec(sb, gb, f_ref, TR[sb])
        z = world.truth_measurement("j", a, b, T03)
        zr = world.truth_measurement("ref-guam", ra, rb, T03)
        for i, (kind, sig) in enumerate((("tdoa", sig_t), ("fdoa", sig_f))):
            ref_row = Measurement(kind, zr[i] + rng.standard_normal() * sig, sig, T03, ra, rb)
            c, cal = sv.calibration_correction(ref_row, x_ref, orbits, sites, f"g{k}{kind}")
            rows.append(Measurement(kind, z[i] + rng.standard_normal() * sig, sig, T03, a, b, correction=c, cal=cal))
    return rows


def _east_north(lat: float, lon: float, sol: sv.Solution) -> np.ndarray:
    from petal_locate.core.geodesy import radii_of_curvature
    m, n = radii_of_curvature(lat)
    return np.array([n * math.cos(math.radians(lat)) * math.radians(sol.lon_deg - lon), m * math.radians(sol.lat_deg - lat)])


def test_calibrated_fix_inside_its_ellipse_with_a_30_tecu_ionosphere(cfg: SystemConfig) -> None:
    """South China Sea, calibrated against Guam, the simulator's 30 TECU ionosphere: the
    uplink delays at the emitter and at the reference differ (-1111 and -954 ns per pair),
    which calibration cannot remove. Without the ionospheric consider term the fixes sit
    about 180 m west, outside their own ellipses (mean NEES above 15); with the default
    term (30 TECU, correlation 0.5) the mean NEES is near 2."""
    lat, lon = 15.0, 115.0
    world = SimWorld(cfg, scenario(lat, lon, iono=30.0))
    unc_on = cfg.resolved_uncertainty()
    assert unc_on.iono_vtec_tecu == 30.0 and unc_on.iono_correlation == 0.5  # the defaults
    unc_off = Uncertainty(**{**unc_on.__dict__, "iono_vtec_tecu": 0.0, "tropo_zenith_m": 0.0})
    rng = np.random.default_rng(2026)
    nees: dict[str, list[float]] = {"on": [], "off": []}
    for _ in range(12):
        rows = _calibrated_rows(cfg, world, lat, lon, rng)
        for name, unc in (("on", unc_on), ("off", unc_off)):
            sol, why = sv.solve(rows, cfg.orbits(), cfg.sites_xyz(), cfg.processing.solver, cfg.processing.region,
                                unc, starts=[(lat, lon)])
            assert sol is not None, why
            e = _east_north(lat, lon, sol)
            nees[name].append(float(e @ np.linalg.solve(sol.cov_en_m2, e)))
    assert np.mean(nees["off"]) > 10.0
    assert np.mean(nees["on"]) < 3.5
    assert sum(v <= 5.991 for v in nees["on"]) >= 10


def test_media_columns_follow_the_ends(cfg: SystemConfig) -> None:
    """Calibrated rows: the downlink (site) columns of target and reference nearly cancel;
    per-end columns carry sigma sqrt(1 - rho), the common column sigma sqrt(rho) times their
    sum; a row's media variance equals the dense covariance of that model."""
    rows = _calibrated_rows(cfg, SimWorld(cfg, scenario(15.0, 115.0)), 15.0, 115.0, np.random.default_rng(1))
    unc = Uncertainty(iono_vtec_tecu=20.0, iono_correlation=0.3)
    r = sv._Rows(rows, cfg.orbits(), cfg.sites_xyz(), unc)
    assert set(r.categories) == {"reference", "ionosphere"}
    x = lla_to_ecef(15.0, 115.0, 0.0)
    gain = r.consider(x[None, :])[0]
    media = gain[:, [c == "ionosphere" for c in r.categories]]
    ends = list(r._media_ends["ionosphere"])
    assert ends[0] == "emitter" and any(e[0] == "ref" for e in ends[1:]) and any(e[0] == "site" for e in ends[1:])
    per_end, common = media[:, :-1] / (20.0 * math.sqrt(0.7)), media[:, -1] / (20.0 * math.sqrt(0.3))
    assert np.allclose(per_end.sum(axis=1), common, rtol=1e-12, atol=1e-18)
    sites = [i for i, e in enumerate(ends) if e[0] == "site"]
    emitter = 0
    # on the TDOA rows the emitter's (and the reference's) uplink terms dominate the sites' downlink residues
    tdoa = [k for k, row in enumerate(rows) if row.kind == "tdoa"]
    assert np.all(np.abs(per_end[tdoa][:, sites]).max(axis=1) < 0.01 * np.abs(per_end[tdoa, emitter]))
    # dense check: per-end variances V(1 - rho) + V rho (sum)^2 against the gain
    dense = 400.0 * (0.7 * per_end @ per_end.T + 0.3 * np.outer(common, common))
    assert np.allclose(media @ media.T, dense, rtol=1e-10, atol=0.0)


def test_uncertainty_validation_of_media_fields(cfg: SystemConfig) -> None:
    rows = _calibrated_rows(cfg, SimWorld(cfg, scenario(15.0, 115.0)), 15.0, 115.0, np.random.default_rng(1))
    for bad in ({"iono_vtec_tecu": -1.0}, {"iono_correlation": 1.5}, {"tropo_zenith_m": float("nan")},
                {"tropo_correlation": -0.1}):
        with pytest.raises(ValueError, match="uncertainty"):
            sv._Rows(rows, cfg.orbits(), cfg.sites_xyz(), Uncertainty(**bad))


def test_config_carries_media_settings(cfg: SystemConfig) -> None:
    unc = cfg.resolved_uncertainty()
    assert (unc.iono_vtec_tecu, unc.iono_correlation, unc.tropo_zenith_m, unc.tropo_correlation) == (30.0, 0.5, 2.4, 0.9)
    data = cfg.export_dict()
    data["processing"]["uncertainty"].update(iono_vtec_tecu=0.0, tropo_correlation=0.2)
    other = SystemConfig.model_validate(data).resolved_uncertainty()
    assert other.iono_vtec_tecu == 0.0 and other.tropo_correlation == 0.2
    data["processing"]["uncertainty"]["iono_correlation"] = 1.2
    with pytest.raises(ValueError):
        SystemConfig.model_validate(data)


# --------------------------------------------------------------------------- noise shared through one capture


def _geo_rows(shares: tuple[float, float] | None) -> tuple[list[Measurement], dict, dict]:
    """MATH.md 12.8 geometry: pairs (sat1 via Honolulu, sat2 via Tokyo) and (sat1 via
    Honolulu, sat3 via Manila), both kinds, sharing the Honolulu (primary) capture."""
    from petal_locate.core.orbits import GeoOrbit
    orbits = {
        "sat1": GeoOrbit(-177.0, 5.0, phase_deg=40.0),
        "sat2": GeoOrbit(172.0, 4.0, phase_deg=200.0),
        "sat3": GeoOrbit(75.0, 5.0, phase_deg=310.0),
    }
    sites = {"hnl": lla_to_ecef(21.31, -157.86, 15.0), "tyo": lla_to_ecef(35.68, 139.77, 50.0),
             "mnl": lla_to_ecef(14.60, 120.98, 20.0)}
    a = PathSpec("sat1", "hnl", 300e6, -41.0e6)
    rows = []
    for k, b in enumerate((PathSpec("sat2", "tyo", 300e6, -33.6e6), PathSpec("sat3", "mnl", 300e6, -41.0e6))):
        for kind, sig in (("tdoa", 1e-6), ("fdoa", 0.05)):
            extra = {} if shares is None else {"capture_a": "primary", "capture_b": f"partner{k}",
                                               "noise_share_a": shares[0], "noise_share_b": shares[1]}
            rows.append(Measurement(kind, 0.0, sig, ns_from_iso("2026-01-01T03:00:00Z"), a, b, **extra))
    return rows, orbits, sites


def test_shared_capture_noise_in_the_weighting() -> None:
    """Rows of two pairs correlated from one capture: S holds sigma_k sigma_l sqrt(s_k s_l)
    between them (same kind), the diagonal stays sigma^2, and the noise part of the
    covariance includes the shared term (MATH.md section 9)."""
    rows, orbits, sites = _geo_rows((0.45, 0.3))
    r = sv._Rows(rows, orbits, sites, None)
    assert r.categories == ["noise", "noise"]  # one column per (capture, kind) used twice
    gain = r.consider(lla_to_ecef(30.37, 140.41, 0.0)[None, :])[0]
    dense = np.diag(r.sigma**2) + gain @ gain.T
    sig = np.array([row.sigma for row in rows])
    assert np.allclose(np.diag(dense), sig**2, rtol=1e-12)
    assert dense[0, 2] == pytest.approx(1e-6 * 1e-6 * 0.45, rel=1e-12)  # TDOA of pair 1 with TDOA of pair 2
    assert dense[1, 3] == pytest.approx(0.05 * 0.05 * 0.45, rel=1e-12)
    assert dense[0, 1] == 0.0 and dense[0, 3] == 0.0  # TDOA and FDOA errors stay uncorrelated
    plain = sv.position_covariance(_geo_rows(None)[0], 30.37, 140.41, orbits, sites)
    shared = sv.position_covariance(rows, 30.37, 140.41, orbits, sites)
    assert shared is not None and plain is not None
    assert np.allclose(shared["cov_en_m2"], shared["cov_noise_en_m2"], rtol=1e-9)  # all of it is CAF noise
    assert not np.allclose(shared["cov_en_m2"], plain["cov_en_m2"], rtol=1e-3)


def test_shared_capture_on_calibrated_uhf_rows(cfg: SystemConfig) -> None:
    """The tasking geometry, calibrated against Guam: both pairs' rows from the primary
    capture, both references from the primary's reference capture, each with a 0.45 share
    of channel A's noise. Against that true covariance the independent-row model expects a
    NEES of 1.76 (it misplaces the shared part) and the shared-capture model 2; the 95 %
    semi-major axis changes by about 10 % (MATH-2 review: 1.77 to 2.21)."""
    import dataclasses
    lat, lon = 15.0, 115.0
    rows = _calibrated_rows(cfg, SimWorld(cfg, scenario(lat, lon)), lat, lon, np.random.default_rng(1))
    unc = Uncertainty(**{**cfg.resolved_uncertainty().__dict__, "iono_vtec_tecu": 0.0, "tropo_zenith_m": 0.0})
    shared = []
    for k, row in enumerate(rows):
        cal = dataclasses.replace(row.cal, capture_a="ref-A", capture_b=f"ref-B{k // 2}", noise_share_a=0.45,
                                  noise_share_b=0.45)
        shared.append(dataclasses.replace(row, cal=cal, capture_a="A", capture_b=f"B{k // 2}", noise_share_a=0.45,
                                          noise_share_b=0.45))
    x = lla_to_ecef(lat, lon, 0.0)

    def model(rs: list[Measurement]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        r = sv._Rows(rs, cfg.orbits(), cfg.sites_xyz(), unc)
        g = r.consider(x[None, :])[0]
        w = sv._Whitener(r.sigma, g)
        _, solved, _, normal = sv._normal_matrix(r, lat, lon, 0.0, w)
        p_total = np.linalg.inv(normal)
        return p_total, (p_total @ solved) * w.inv_sigma[None, :], np.diag(r.sigma**2) + g @ g.T

    p_shared, k_shared, s_true = model(shared)
    p_plain, k_plain, s_plain = model(rows)
    # the true covariance: the reference columns of two references of one capture correlate
    assert np.allclose(np.diag(s_true), np.diag(s_plain), rtol=1e-9)
    assert not np.allclose(s_true, s_plain, rtol=1e-3)
    nees_shared = float(np.trace(np.linalg.solve(p_shared, k_shared @ s_true @ k_shared.T)))
    nees_plain = float(np.trace(np.linalg.solve(p_plain, k_plain @ s_true @ k_plain.T)))
    assert nees_shared == pytest.approx(2.0, abs=1e-6)
    assert nees_plain == pytest.approx(1.76, abs=0.05)


def test_capture_fields_are_validated() -> None:
    rows, orbits, sites = _geo_rows((0.6, 0.5))
    with pytest.raises(ValueError, match="below 1"):
        sv._Rows(rows, orbits, sites, None)
    rows, orbits, sites = _geo_rows((0.2, 0.2))
    rows[0].capture_b = rows[0].capture_a
    with pytest.raises(ValueError, match="same capture"):
        sv._Rows(rows, orbits, sites, None)


# --------------------------------------------------------------------------- reference age


@pytest.mark.parametrize("age_s", [0.0, 300.0])
def test_reference_age_moves_the_satellites(cfg: SystemConfig, age_s: float) -> None:
    """TLE-grade satellite errors, a Guam reference measured ``age_s`` before the target:
    the expected NEES of the solver's covariance against the true linearised error (each
    satellite at position p + v (t - t_target) for both measurements: PerturbedOrbit with
    t_ref at the target epoch, as the simulator draws it) is 2 at any age. Without the
    velocity term in the reference partials it was 7.5 at 300 s."""
    orbits, sites = cfg.orbits(), cfg.sites_xyz()
    pos, vel = np.array([2000.0, 20000.0, 10000.0]), np.array([0.3, 0.3, 0.7])
    unc = Uncertainty(sat_pos_ric_m={s: pos for s in TR}, sat_vel_ric_mps={s: vel for s in TR})
    lat, lon = 15.0, 147.0
    x0 = lla_to_ecef(lat, lon, 0.0)
    x_ref = np.asarray(cfg.carrier_xyz("ref-guam"), dtype=np.float64)
    f_ref = float(cfg.carrier("ref-guam").uplink_center_hz)
    t_ref_ns = T03 - int(age_s * 1e9)
    sig, sig_r = {"tdoa": 91.6e-9, "fdoa": 0.924e-3}, {"tdoa": 115e-9, "fdoa": 0.95e-3}
    rows, specs = [], []
    for k, ((sa, ga), (sb, gb)) in enumerate(((("muos-pac", "honolulu"), ("uhf-172e", "naha")),
                                              (("muos-pac", "honolulu"), ("muos-io", "seoul")))):
        a, b = PathSpec(sa, ga, F_J, TR[sa]), PathSpec(sb, gb, F_J, TR[sb])
        ra, rb = PathSpec(sa, ga, f_ref, TR[sa]), PathSpec(sb, gb, f_ref, TR[sb])
        for kind in ("tdoa", "fdoa"):
            cal = CalRef(f"r{k}{kind}", x_ref, ra, rb, t_ref_ns, sig_r[kind])
            rows.append(Measurement(kind, 0.0, sig[kind], T03, a, b, cal=cal))
            specs.append((kind, a, b, ra, rb))
    r = sv._Rows(rows, orbits, sites, unc)
    gain = r.consider(x0[None, :])[0]
    w = sv._Whitener(r.sigma, gain)
    _, solved, _, normal = sv._normal_matrix(r, lat, lon, 0.0, w)
    p_total = np.linalg.inv(normal)
    k_gain = (p_total @ solved) * w.inv_sigma[None, :]  # K = P J^T S^-1, [2, m]

    # true partials: perturb each satellite's RIC position / velocity with t_ref = target epoch
    def calibrated(orb):
        out = []
        for kind, a, b, ra, rb in specs:
            i = 0 if kind == "tdoa" else 1
            out.append(float(predict_pair(x0, a, b, sec(T03), orb, sites)[i])
                       - float(predict_pair(x_ref, ra, rb, sec(t_ref_ns), orb, sites)[i]))
        return np.array(out)

    base = calibrated(orbits)
    columns = []
    for sat in TR:
        for is_vel, sigmas in ((False, pos), (True, vel)):
            for comp in range(3):
                step = np.zeros(3)
                step[comp] = 0.02 if is_vel else 1000.0
                orb = dict(orbits)
                orb[sat] = PerturbedOrbit(orbits[sat], step * (not is_vel), step * is_vel, sec(T03))
                columns.append((calibrated(orb) - base) / step[comp] * sigmas[comp])
    h_true = np.stack(columns, axis=1)
    noise = np.diag([sig[s[0]] ** 2 for s in specs])
    ref_noise = np.diag([sig_r[s[0]] ** 2 for s in specs])  # one reference measurement per row here
    s_true = noise + ref_noise + h_true @ h_true.T
    expected = float(np.trace(np.linalg.solve(p_total, k_gain @ s_true @ k_gain.T)))
    assert expected == pytest.approx(2.0, abs=0.15)


def test_reference_capture_fields_are_validated(cfg: SystemConfig) -> None:
    import dataclasses
    rows = _calibrated_rows(cfg, SimWorld(cfg, scenario(15.0, 115.0)), 15.0, 115.0, np.random.default_rng(1))
    rows[0] = dataclasses.replace(rows[0], cal=dataclasses.replace(rows[0].cal, noise_share_a=-0.1))
    with pytest.raises(ValueError, match="noise_share_a"):
        sv._Rows(rows, cfg.orbits(), cfg.sites_xyz(), None)
