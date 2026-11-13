"""Accuracy prediction: the planning map of MATH.md section 10.

For every cell of a latitude/longitude grid this module predicts how well an emitter
there would be located: link budgets of the emitter through each satellite of each
listed channel pair (``linkbudget.path_budget``), input SNRs in the correlation band,
post-correlation SNR and Stein sigmas (``crlb``) with the noise bandwidth ``B`` and
integration time ``T`` the cross-ambiguity estimator will actually use, one TDOA and
one FDOA row per pair, and the east/north covariance ``P_total`` of MATH.md section 9
with the configured systematic (consider) uncertainties and, optionally, differential
calibration against a reference carrier.

It also holds the capture and search-window rules of ARCHITECTURE.md section 7 that
the prediction depends on (sample rate, number of samples, search windows, exclusion
zones, mirror windows), so that tasking, the Monte Carlo driver and the planning map
share one implementation: ``capture_plan``, ``pair_search``,
``correlation_bandwidth_hz`` and ``integration_time_s``.

Conventions: positions ECEF (WGS84) metres; angles degrees at the interface; absolute
time integer UTC nanoseconds (``t_ns``), float Unix seconds only for geometry;
TDOA rows in seconds, FDOA rows in hertz (A minus B, MATH.md section 5); covariances
2x2 in local east/north metres, ordered ``[east, north]``, with
``dE = (N(lat) + h) cos(lat) dlon`` and ``dN = (M(lat) + h) dlat``.

The configuration is read by attribute (``processing``, ``channel(id)``,
``satellite(id)``, ``carrier(id)``, ``carrier_xyz(id)``, ``orbits()``,
``sites_xyz()``, ``resolved_uncertainty()``, ``carriers``, ``satellites``); nothing
here imports ``petal_locate.config``. Nothing keeps state, reads a clock or draws
random numbers.
"""
from __future__ import annotations

import math
import operator
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import fft as sfft

from .caf import _band_filter
from .crlb import post_correlation_snr, stein_sigmas
from .geodesy import lla_to_ecef, radii_of_curvature
from .geometry import elevation_deg, measurement_windows, predict_pair, search_windows, self_peak
from .linkbudget import emitter_boresight, path_budget, uplink_budget
from .media import row_media_partials
from .orbits import Orbit, PerturbedOrbit, orbit_position
from .signals import waveform_from_spec
from .solver import ERROR_SOURCES, above_mask
from .timebase import sec
from .types import PathSpec, Uncertainty

__all__ = [
    "ACCURACY_CONFIDENCE",
    "MIN_SIGNAL_BW_HZ",
    "CapturePlan",
    "PairSearch",
    "relaying_transponder",
    "split_channel_ref",
    "channel_satellite",
    "pair_satellites",
    "capture_plan",
    "pair_search",
    "correlation_bandwidth_hz",
    "integration_time_s",
    "model_rms_bandwidth_hz",
    "covariance_batch",
    "cep50_batch",
    "accuracy_map",
]

ACCURACY_CONFIDENCE: float = 0.95
"""Probability of the ellipse whose semi-major axis the map reports (``semi_major_95_m``)."""

MIN_SIGNAL_BW_HZ: float = 10.0
"""The estimator's band filter is never narrower than this (Hz): tasking and the Monte
Carlo driver pass ``max(occupied band, MIN_SIGNAL_BW_HZ)``, so a CW is band-limited too."""

_ELLIPSE_SCALE = math.sqrt(-2.0 * math.log(1.0 - ACCURACY_CONFIDENCE))  # 2.4477
_JAC_STEP_M = 100.0  # central-difference step of J, metres (MATH.md section 9)
_RIC_POS_STEP_M = 1000.0  # one-sided satellite position step of the consider partials (as the solver)
_RIC_VEL_STEP_MPS = 0.02  # one-sided satellite velocity step of the consider partials (as the solver)
_MAX_SEMI_MAJOR_M = 1e7  # observability rule: 1-sigma semi-major axis limit (MATH.md section 9)
_WINDOW_STEP_DEG = 2.0  # grid step of measurement_windows (its default, as tasking uses it)
_MIN_OVERLAP = 32  # fewest overlapping samples the estimator accepts at a lag (caf._Aligned)
_CEP_NODES = 512  # midpoint nodes of the CEP50 integral (as solver.cep50)
_CATEGORY = {
    "clock": "clock",
    "site_osc": "oscillator",
    "sat_osc": "oscillator",
    "bias": "tdoa_bias",
    "ref": "reference",
    "eph_pos": "ephemeris_position",
    "eph_vel": "ephemeris_velocity",
    "cap": "noise",
    "refcap": "reference",
}
REASON_NOT_ADMISSIBLE = "not admissible"
REASON_UNOBSERVABLE = "unobservable"


# --------------------------------------------------------------------------- transponders and channels


def relaying_transponder(satellite: Any, f_e_hz: float) -> Any | None:
    """The transponder of ``satellite`` that relays a carrier centred on ``f_e_hz`` (Hz).

    MATH.md section 4: relayed if and only if the centre lies in the closed uplink
    passband ``uplink_center_hz +/- bandwidth_hz / 2``. When passbands overlap the one
    with the nearest uplink centre is chosen (the earlier one on a tie). Returns None
    when no transponder relays it. ``satellite`` is read by attribute (``transponders``).
    """
    best = None
    for tp in satellite.transponders:
        half = 0.5 * float(tp.bandwidth_hz)
        centre = float(tp.uplink_center_hz)
        if centre - half <= f_e_hz <= centre + half:
            if best is None or abs(f_e_hz - centre) < abs(f_e_hz - float(best.uplink_center_hz)):
                best = tp
    return best


def split_channel_ref(text: str) -> tuple[str, str | None]:
    """``"channel_id"`` or ``"channel_id@sat_id"`` -> ``(channel_id, sat_id or None)``.

    The explicit form names the satellite a pair end correlates on, for a channel that
    watches several satellites relaying the same carrier (a fixed or wide-beam
    pointing). Identifiers never contain ``@``. Raises ValueError for an empty part.
    """
    if not isinstance(text, str):
        raise ValueError(f"a pair end must be a channel id string, got {type(text).__name__}")
    channel_id, sep, sat_id = text.partition("@")
    if not channel_id or (sep and not sat_id) or "@" in sat_id:
        raise ValueError(f"{text!r} is not a channel id or 'channel_id@sat_id'")
    return channel_id, (sat_id if sep else None)


def channel_satellite(
    config: Any,
    channel_id: str,
    f_e_hz: float,
    name: str = "channel_id",
    *,
    exclude: str | None = None,
    sat_id: str | None = None,
) -> tuple[str, Any] | None:
    """The satellite (and its relaying transponder) a channel correlates a carrier on.

    The channel's watched satellites (``watched_satellite_ids``: its tracked satellite,
    or the list of a fixed pointing) are searched for one that relays ``f_e_hz`` (Hz).
    ``channel_id`` may be ``"channel_id@sat_id"`` (``split_channel_ref``), which is the
    same as passing ``sat_id``: that satellite is used, and it must be watched by the
    channel. exclude: a satellite to leave out (the one the other end of the pair uses),
    applied only when more than one watched satellite relays the carrier.
    Returns ``(sat_id, transponder)``, or None when no (chosen) watched satellite relays
    it. Raises ValueError naming ``name`` when the channel is unknown, the named
    satellite is not watched by it, or two watched satellites relay the carrier and
    ``exclude`` does not leave exactly one (the pair would be ambiguous).
    """
    channel_id, named = split_channel_ref(channel_id)
    if sat_id is not None and named is not None and sat_id != named:
        raise ValueError(f"{name}: two satellites named for channel {channel_id!r} ({named!r}, {sat_id!r})")
    sat_id = sat_id if sat_id is not None else named
    _, channel = config.channel(channel_id)
    watched = list(channel.watched_satellite_ids)
    if sat_id is not None:
        if sat_id not in watched:
            raise ValueError(f"{name}: channel {channel_id!r} does not watch satellite {sat_id!r} (it watches {watched})")
        tp = relaying_transponder(config.satellite(sat_id), f_e_hz)
        return None if tp is None else (sat_id, tp)
    found = []
    for watched_id in watched:
        tp = relaying_transponder(config.satellite(watched_id), f_e_hz)
        if tp is not None:
            found.append((watched_id, tp))
    if len(found) > 1 and exclude is not None:
        found = [item for item in found if item[0] != exclude] or found
    if len(found) > 1:
        raise ValueError(
            f"{name}: channel {channel_id!r} watches satellites {[s for s, _ in found]} that all relay "
            f"{f_e_hz:.0f} Hz; which one a pair means is ambiguous (name it as '{channel_id}@<sat_id>')"
        )
    return found[0] if found else None


def pair_satellites(
    config: Any, end_a: str, end_b: str, f_e_hz: float, name: str = "pair"
) -> tuple[tuple[str, Any] | None, tuple[str, Any] | None]:
    """The satellites (and transponders) both ends of a channel pair correlate a carrier on.

    end_a, end_b: ``"channel_id"`` or ``"channel_id@sat_id"``. Both ends are resolved
    together: an end whose channel watches several satellites relaying ``f_e_hz`` takes
    the one the other end does not use (as tasking pairs a multi-satellite channel), and
    raises (naming ``name[0]`` / ``name[1]``) only when that still leaves a choice.
    Returns ``(found_a, found_b)``, each ``(sat_id, transponder)`` or None as
    ``channel_satellite``.
    """
    def attempt(end: str, label: str, exclude: str | None) -> tuple[str, Any] | None | ValueError:
        try:
            return channel_satellite(config, end, f_e_hz, label, exclude=exclude)
        except ValueError as exc:
            return exc

    label_a, label_b = f"{name}[0]", f"{name}[1]"
    found_a, found_b = attempt(end_a, label_a, None), attempt(end_b, label_b, None)
    if isinstance(found_a, ValueError) and not isinstance(found_b, ValueError) and found_b is not None:
        found_a = attempt(end_a, label_a, found_b[0])
    elif isinstance(found_b, ValueError) and not isinstance(found_a, ValueError) and found_a is not None:
        found_b = attempt(end_b, label_b, found_a[0])
    for found in (found_a, found_b):
        if isinstance(found, ValueError):
            raise found
    return found_a, found_b  # type: ignore[return-value]


# --------------------------------------------------------------------------- capture plan


@dataclass(frozen=True)
class CapturePlan:
    """How a pair of channels is captured for correlation (ARCHITECTURE.md section 7).

    sample_rate_hz: identical in both requests, Hz. n_samples: samples per channel.
    f_e_nom_hz: nominal uplink centre ``round((f_lo + f_hi) / 2)``, Hz; each channel is
    tuned to ``f_e_nom_hz + translation_hz`` of its transponder. occupied_bw_hz:
    ``f_hi - f_lo``, Hz, the band the estimator filters to (``signal_bw_hz``).
    """

    sample_rate_hz: float
    n_samples: int
    f_e_nom_hz: float
    occupied_bw_hz: float

    @property
    def duration_s(self) -> float:
        """Capture length ``n_samples / sample_rate_hz``, s."""
        return self.n_samples / self.sample_rate_hz

    @property
    def signal_bw_hz(self) -> float | None:
        """``signal_bw_hz`` argument of ``caf_estimate``: the occupied band, or None
        (no band filter) for a carrier of zero width."""
        return self.occupied_bw_hz if self.occupied_bw_hz > 0.0 else None

    def t_mid_ns(self, t_start_ns: int) -> int:
        """``t_mid`` (MATH.md section 5) of a channel-B capture starting at ``t_start_ns``
        (labelled, integer UTC ns): ``t_start + round(1e9 (N - 1) / (2 fs))``."""
        return int(t_start_ns) + round(1e9 * (self.n_samples - 1) / (2.0 * self.sample_rate_hz))


def capture_plan(
    config: Any,
    channel_ids: Sequence[str],
    f_lo_hz: float,
    f_hi_hz: float,
    *,
    duration_s: float | None = None,
    sample_rate_hz: float | None = None,
) -> CapturePlan:
    """Sample rate and capture length for correlating ``channel_ids`` (ARCHITECTURE.md section 7).

    f_lo_hz, f_hi_hz: uplink range of the signal, Hz (``f_lo <= f_hi``).
    ``sample_rate_hz = min(max(capture.min_sample_rate_hz, capture.oversample * (f_hi - f_lo)),
    lowest max_sample_rate_hz of the channels)``,
    ``n_samples = min(round(capture.duration_s * fs), capture.max_samples)``.
    duration_s: replaces ``capture.duration_s`` (s); the ``max_samples`` cap is then not
    applied (a what-if integration time). sample_rate_hz: replaces the rule's rate (Hz);
    it must not exceed the channels' maximum.
    Raises ValueError naming the argument for invalid input.
    """
    lo, hi = _finite(f_lo_hz, "f_lo_hz"), _finite(f_hi_hz, "f_hi_hz")
    if hi < lo:
        raise ValueError(f"f_hi_hz ({f_hi_hz!r}) must not be below f_lo_hz ({f_lo_hz!r})")
    ids = list(channel_ids)
    if not ids:
        raise ValueError("channel_ids must name at least one channel")
    cap = config.processing.capture
    max_rate = min(float(config.channel(ch)[1].max_sample_rate_hz) for ch in ids)
    if sample_rate_hz is None:
        fs = min(max(float(cap.min_sample_rate_hz), float(cap.oversample) * (hi - lo)), max_rate)
    else:
        fs = _finite(sample_rate_hz, "sample_rate_hz")
        if not fs > 0.0:
            raise ValueError(f"sample_rate_hz must be positive, got {sample_rate_hz!r}")
        if fs > max_rate:
            raise ValueError(f"sample_rate_hz {fs:g} Hz exceeds the channels' max_sample_rate_hz {max_rate:g} Hz")
    if duration_s is None:
        n = min(int(round(float(cap.duration_s) * fs)), int(cap.max_samples))
    else:
        d = _finite(duration_s, "duration_s")
        if not d > 0.0:
            raise ValueError(f"duration_s must be positive, got {duration_s!r}")
        n = int(round(d * fs))
    if n < 2:
        raise ValueError(f"duration_s: the capture has {n} samples at {fs:g} Hz")
    return CapturePlan(sample_rate_hz=fs, n_samples=n, f_e_nom_hz=float(round(0.5 * (lo + hi))), occupied_bw_hz=hi - lo)


# --------------------------------------------------------------------------- search windows


@dataclass(frozen=True)
class PairSearch:
    """Geometry of one channel pair for the estimator (ARCHITECTURE.md section 7, MATH.md section 6).

    a, b: the pair's paths (``f_tune_hz`` of each is its capture centre). t_mid_s: the
    epoch, Unix s. geo_windows: ``((tdoa_min, tdoa_max), (fdoa_min, fdoa_max))``
    predicted over the admissible region (or at a known point), s and Hz.
    exclude: predicted self-peaks ``(tdoa_s, fdoa_hz)`` of every satellite heard by both
    channels, plus the direct-path point when both channels are at one site with equal
    translations. heard_a, heard_b: satellites whose copy of the carrier falls in each
    capture band (above that site's horizon, centre relayed, band not wholly outside).
    mirror_a, mirror_b, mirror_geo_windows: the swapped pairing (satellite b via site a
    against satellite a via site b) of co-channel satellites, or None.
    mirror_doppler_offset_hz: ``2 (T_b - T_a)``, the retune between the swapped
    pairing's own tunings and the channels' actual tunings, added to its Doppler window.
    """

    a: PathSpec
    b: PathSpec
    t_mid_s: float
    geo_windows: tuple[tuple[float, float], tuple[float, float]]
    exclude: tuple[tuple[float, float], ...]
    heard_a: tuple[str, ...]
    heard_b: tuple[str, ...]
    mirror_a: PathSpec | None = None
    mirror_b: PathSpec | None = None
    mirror_geo_windows: tuple[tuple[float, float], tuple[float, float]] | None = None
    mirror_doppler_offset_hz: float = 0.0

    @property
    def co_channel(self) -> bool:
        """Each channel also hears the other pair member's satellite (MATH.md section 6 item 6)."""
        return self.mirror_geo_windows is not None

    def windows(
        self, caf_cfg: Any, uncertainty: Uncertainty | None, correction: Any = None
    ) -> tuple[tuple[float, float], tuple[float, float], tuple[tuple[float, float], tuple[float, float]] | None]:
        """``(delay_window, doppler_window, mirror_window)`` for ``caf_estimate``.

        ``geometry.search_windows`` of the geometric windows with ``caf_cfg`` margins and
        the one-sigma systematics of ``uncertainty``; ``correction`` (``(tdoa_s | None,
        fdoa_hz | None)`` or a mapping) shifts a calibrated kind and leaves it only the
        fixed margin. The mirror window is always widened in full (no calibration
        applies to the wrong pairing) and shifted by ``mirror_doppler_offset_hz``.
        """
        delay, doppler = search_windows(self.geo_windows, self.a, self.b, caf_cfg, uncertainty, correction)
        mirror = None
        if self.mirror_geo_windows is not None and self.mirror_a is not None and self.mirror_b is not None:
            m_delay, m_doppler = search_windows(self.mirror_geo_windows, self.mirror_a, self.mirror_b, caf_cfg, uncertainty)
            off = self.mirror_doppler_offset_hz
            mirror = (m_delay, (m_doppler[0] + off, m_doppler[1] + off))
        return delay, doppler, mirror


def pair_search(
    config: Any,
    a: PathSpec,
    b: PathSpec,
    t_mid_s: float,
    sample_rate_hz: float,
    occupied_bw_hz: float,
    *,
    point_xyz: Any | None = None,
) -> PairSearch | None:
    """Search geometry of a channel pair at epoch ``t_mid_s`` (Unix s).

    a, b: the paths (satellite, site, nominal uplink ``f_e_hz``, translation and group
    delay of the relaying transponder). sample_rate_hz, occupied_bw_hz: the capture
    rate and the signal's occupied band, Hz (decide which satellites each capture
    hears). point_xyz: a known emitter position (ECEF m, a reference carrier's phase
    centre): the geometric windows are then the predictions there; otherwise they are
    ``geometry.measurement_windows`` over ``config.processing.region`` at
    ``solver.height_m`` above ``solver.min_elevation_deg``.

    Returns None when no admissible emitter position exists for the pair. Raises
    ValueError when both paths use one satellite (its copy in both channels is a
    self-peak that is excluded, so nothing would be left to measure).
    """
    if a.sat_id == b.sat_id:
        raise ValueError(f"a.sat_id and b.sat_id are both {a.sat_id!r}: a pair needs two satellites")
    t = _finite(t_mid_s, "t_mid_s")
    fs = _finite(sample_rate_hz, "sample_rate_hz")
    width = _finite(occupied_bw_hz, "occupied_bw_hz")
    orbits = config.orbits()
    sites = config.sites_xyz()
    f_e = float(a.f_e_hz)
    heard_a = _heard(config, orbits, sites, a.site_id, a.f_tune_hz, f_e, fs, width, t)
    heard_b = _heard(config, orbits, sites, b.site_id, b.f_tune_hz, f_e, fs, width, t)
    both = [s for s in heard_a if s in heard_b]
    exclude: list[tuple[float, float]] = []
    for sat_id in both:
        tp = relaying_transponder(config.satellite(sat_id), f_e)
        tdoa, fdoa = self_peak(sat_id, float(tp.translation_hz), f_e, a, b, t, orbits, sites)
        exclude.append((float(tdoa), float(fdoa)))
    if a.site_id == b.site_id and a.translation_hz == b.translation_hz:
        exclude.append((0.0, float(b.f_tune_hz - a.f_tune_hz)))  # a direct path is identical in both

    solver_cfg = config.processing.solver
    region = config.processing.region

    def windows(pa: PathSpec, pb: PathSpec) -> tuple[tuple[float, float], tuple[float, float]] | None:
        if point_xyz is not None:
            tdoa, fdoa = predict_pair(np.asarray(point_xyz, dtype=np.float64), pa, pb, t, orbits, sites)
            return (float(tdoa), float(tdoa)), (float(fdoa), float(fdoa))
        return measurement_windows(
            pa, pb, t, orbits, sites, region,
            min_elevation_deg=float(solver_cfg.min_elevation_deg), height_m=float(solver_cfg.height_m),
            step_deg=_WINDOW_STEP_DEG,
        )

    geo = windows(a, b)
    if geo is None:
        return None
    mirror_a = mirror_b = mirror_geo = None
    offset = 0.0
    if a.sat_id in heard_b and b.sat_id in heard_a:
        mirror_a = PathSpec(b.sat_id, a.site_id, f_e, b.translation_hz, b.tau_x_s)
        mirror_b = PathSpec(a.sat_id, b.site_id, f_e, a.translation_hz, a.tau_x_s)
        mirror_geo = windows(mirror_a, mirror_b)
        if mirror_geo is None:
            mirror_a = mirror_b = None
        offset = 2.0 * (float(b.translation_hz) - float(a.translation_hz))
    return PairSearch(
        a=a, b=b, t_mid_s=t, geo_windows=geo, exclude=tuple(exclude), heard_a=tuple(heard_a), heard_b=tuple(heard_b),
        mirror_a=mirror_a, mirror_b=mirror_b, mirror_geo_windows=mirror_geo,
        mirror_doppler_offset_hz=offset if mirror_geo is not None else 0.0,
    )


def _heard(
    config: Any, orbits: Mapping[str, Orbit], sites: Mapping[str, Any], site_id: str, f_tune: float, f_e: float,
    fs: float, width: float, t_s: float,
) -> list[str]:
    """Satellites (configuration order) whose copy of a carrier at ``f_e`` reaches a capture
    at ``f_tune`` +/- ``fs / 2`` of ``site_id``: relayed, above the site's horizon, and not
    wholly outside the capture band (MATH.md section 4, channel filter)."""
    out = []
    for satellite in config.satellites:
        tp = relaying_transponder(satellite, f_e)
        if tp is None:
            continue
        if abs(f_e + float(tp.translation_hz) - f_tune) - 0.5 * width >= 0.5 * fs:
            continue
        if float(elevation_deg(np.asarray(sites[site_id], dtype=np.float64), orbits[satellite.id], t_s)) > 0.0:
            out.append(satellite.id)
    return out


# --------------------------------------------------------------------------- correlation band and time


def correlation_bandwidth_hz(
    sample_rate_hz: float, signal_bw_hz: float | None, doppler_reach_hz: float, n_samples: int
) -> tuple[float, int]:
    """Noise bandwidth ``B`` (Hz) the estimator correlates over, and its filter length.

    ``caf_estimate`` filters both channels to ``signal_bw_hz + 2 * doppler_reach_hz``
    (``doppler_reach_hz``: the largest Doppler magnitude of its windows, mirror window
    included) with one linear-phase FIR of at most ``n_samples // 8`` taps, or not at
    all when that band is within 1 % of ``fs`` or ``signal_bw_hz`` is None. Returns
    ``(B, n_taps)``: the FIR's noise bandwidth (``fs`` unfiltered) and its length (1
    unfiltered); the filtered channels hold ``n_samples - n_taps + 1`` samples. This
    is the estimator's own filter design (``CafResult.noise_bandwidth_hz``).
    """
    fs = _finite(sample_rate_hz, "sample_rate_hz")
    if signal_bw_hz is None:
        return fs, 1
    reach = abs(_finite(doppler_reach_hz, "doppler_reach_hz"))
    design = _band_filter(fs, float(signal_bw_hz) + 2.0 * reach, max(17, int(n_samples) // 8))
    if design is None:
        return fs, 1
    taps, noise_bw = design
    return float(noise_bw), int(taps.size)


def integration_time_s(
    n_samples: int, n_taps: int, sample_rate_hz: float, tdoa_s: Any, t0_offset_s: float = 0.0
) -> Any:
    """Integration time ``T`` (s) of the estimator's sum at a peak at ``tdoa_s``.

    The estimator sums over channel-B samples symmetric about their midpoint: of the
    ``n_f = n_samples - n_taps + 1`` filtered samples it drops ``|lag|`` at each end
    (``lag = round((tdoa - t0_offset) fs)``) and keeps the largest FFT-friendly length
    not above ``n_f - 2 |lag|`` (``scipy.fft.prev_fast_len``), so
    ``T = prev_fast_len(n_f - 2 |lag|) / fs`` (``CafResult.integration_s``). Vectorised
    over ``tdoa_s``; 0 where fewer than 32 samples overlap.
    """
    fs = _finite(sample_rate_hz, "sample_rate_hz")
    n_f = int(n_samples) - int(n_taps) + 1
    tdoa = np.asarray(tdoa_s, dtype=np.float64)
    lag = np.floor((tdoa - float(t0_offset_s)) * fs + 0.5)
    usable = np.where(np.isfinite(lag), n_f - 2.0 * np.abs(lag), 0.0).astype(np.int64)
    values, inverse = np.unique(usable, return_inverse=True)
    lengths = np.array([sfft.prev_fast_len(int(v), real=False) if v >= _MIN_OVERLAP else 0 for v in values])
    out = lengths[inverse].reshape(usable.shape) / fs
    return float(out) if out.ndim == 0 else out


def model_rms_bandwidth_hz(emitter: Any) -> float:
    """RMS bandwidth (Hz) of the waveform an ``EmitterModel`` describes (``beta = 2 pi B_rms``).

    Built with ``core.signals.waveform_from_spec`` from ``waveform`` and ``bandwidth_hz``,
    so the band edges of the synthesised noise (0.9934 W / sqrt(12)) and the psk symbol
    rate rule are the simulator's; 0 for ``cw`` (delay unobservable).
    """
    spec = {"type": getattr(emitter.waveform, "value", emitter.waveform), "bandwidth_hz": float(emitter.bandwidth_hz)}
    return float(waveform_from_spec(spec, "accuracy|emitter-model").rms_bandwidth_hz)


# --------------------------------------------------------------------------- batched covariance


def covariance_batch(
    jacobian: np.ndarray,
    sigma: np.ndarray,
    active: np.ndarray,
    gain: np.ndarray | None,
    categories: Sequence[str],
) -> dict[str, np.ndarray]:
    """East/north covariance of a fix for many positions at once (MATH.md section 9).

    jacobian: ``J`` ``[P, m, 2]``, row units per metre east and north. sigma: CAF
    one-sigmas ``[P, m]`` (row units). active: ``[P, m]`` bool, rows that exist at each
    position (the others are ignored). gain: ``G = H_c S_c^(1/2)`` ``[P, m, n]`` (one
    column per consider parameter scaled by its one-sigma) or None. categories: the
    error source (one of ``solver.ERROR_SOURCES``) of each column of ``gain``; columns
    of category ``"noise"`` are CAF noise shared by the rows of one capture and count
    in ``P_noise``.

    With ``S = R + G G^T`` over the active rows: ``P_total = (J^T S^-1 J)^-1``,
    ``K = P_total J^T S^-1``, ``P_noise = K (R + G_noise G_noise^T) K^T``,
    ``P_consider = K G_other G_other^T K^T``. Works
    in sigma-normalised form (``Jn = D^-1 J``, ``Gn = D^-1 G``, ``Sn = I + Gn Gn^T``).
    Returns ``total`` (``P_noise + P_consider``), ``noise``, ``consider`` ``[P, 2, 2]``
    m^2; ``budget`` ``[P, len(ERROR_SOURCES)]`` (each source's share of the trace);
    ``observable`` ``[P]`` bool: at least two active rows, ``J^T S^-1 J`` positive
    definite and its one-sigma semi-major axis at most 1e7 m (where False the
    matrices hold NaN).
    """
    jac = np.asarray(jacobian, dtype=np.float64)
    act = np.asarray(active, dtype=bool)
    p, m = act.shape
    sig = np.where(act, np.asarray(sigma, dtype=np.float64), 1.0)
    if np.any(~np.isfinite(sig[act])) or np.any(sig[act] <= 0.0):
        raise ValueError("sigma must be finite and positive on active rows")
    inv = np.where(act, 1.0 / sig, 0.0)
    jn = jac * inv[..., None]
    jn = np.where(np.isfinite(jn), jn, 0.0)
    sn = np.broadcast_to(np.eye(m), (p, m, m)).copy()
    gn = None
    if gain is not None and gain.shape[-1]:
        gn = np.asarray(gain, dtype=np.float64) * inv[..., None]
        sn += gn @ np.swapaxes(gn, -1, -2)
    solved = np.linalg.solve(sn, jn)  # Sn^-1 Jn, [P, m, 2]
    normal = np.swapaxes(jn, -1, -2) @ solved  # J^T S^-1 J, [P, 2, 2]
    normal = 0.5 * (normal + np.swapaxes(normal, -1, -2))
    a, b, d = normal[:, 0, 0], normal[:, 0, 1], normal[:, 1, 1]
    half_tr, half_diff = 0.5 * (a + d), 0.5 * (a - d)
    lam_min = half_tr - np.hypot(half_diff, b)
    with np.errstate(divide="ignore", invalid="ignore"):
        observable = (np.sum(act, axis=1) >= 2) & np.isfinite(lam_min) & (lam_min > 0.0)
        observable &= 1.0 / np.sqrt(np.where(observable, lam_min, 1.0)) <= _MAX_SEMI_MAJOR_M
        det = np.where(observable, a * d - b * b, 1.0)
        p_total = np.empty((p, 2, 2))
        p_total[:, 0, 0] = d / det
        p_total[:, 1, 1] = a / det
        p_total[:, 0, 1] = p_total[:, 1, 0] = -b / det
    k_n = p_total @ np.swapaxes(solved, -1, -2)  # K D, [P, 2, m]
    noise = k_n @ np.swapaxes(k_n, -1, -2)
    noise = 0.5 * (noise + np.swapaxes(noise, -1, -2))
    budget = np.zeros((p, len(ERROR_SOURCES)))
    budget[:, ERROR_SOURCES.index("noise")] = noise[:, 0, 0] + noise[:, 1, 1]
    consider = np.zeros((p, 2, 2))
    if gn is not None:
        projected = k_n @ gn  # K G, [P, 2, n]
        is_noise = np.array([name == "noise" for name in categories], dtype=bool)
        shared = projected[:, :, is_noise]  # CAF noise shared by rows of one capture
        noise = noise + shared @ np.swapaxes(shared, -1, -2)
        noise = 0.5 * (noise + np.swapaxes(noise, -1, -2))
        rest = projected[:, :, ~is_noise]
        consider = rest @ np.swapaxes(rest, -1, -2)
        consider = 0.5 * (consider + np.swapaxes(consider, -1, -2))
        share = np.sum(projected * projected, axis=1)  # [P, n]
        for col, name in enumerate(categories):
            budget[:, ERROR_SOURCES.index(name)] += share[:, col]
    total = noise + consider
    nan = ~observable
    for arr in (total, noise, consider):
        arr[nan] = np.nan
    budget[nan] = np.nan
    return {"total": total, "noise": noise, "consider": consider, "budget": budget, "observable": observable}


def cep50_batch(cov: np.ndarray) -> np.ndarray:
    """CEP50 (m) of ``N(0, P)`` for a stack of 2x2 covariances ``[P, 2, 2]`` (m^2).

    The integral of MATH.md section 9 in the form ``solver.cep50`` evaluates (midpoint
    rule over 512 nodes after the substitution ``tan(theta) = (s2 / s1) tan(phi)``),
    solved for every matrix at once by safeguarded Newton iteration inside the bracket
    ``[0.67, 1.18] s1``. NaN where the input holds NaN.
    """
    c = np.asarray(cov, dtype=np.float64)
    a, b, d = c[..., 0, 0], c[..., 0, 1], c[..., 1, 1]
    half_tr, root = 0.5 * (a + d), np.hypot(0.5 * (a - d), b)
    lam_max = np.clip(half_tr + root, 0.0, None)
    lam_min = np.clip(half_tr - root, 0.0, None)
    ok = np.isfinite(lam_max) & (lam_max > 0.0)
    ratio = np.where(ok, lam_min / np.where(ok, lam_max, 1.0), 1.0)  # (s2 / s1)^2
    phi = (np.arange(_CEP_NODES) + 0.5) * (0.5 * np.pi / _CEP_NODES)
    cos2, sin2 = np.cos(phi) ** 2, np.sin(phi) ** 2
    flat_ratio = ratio.reshape(-1)
    r = 0.6745 + 0.5029 * np.sqrt(flat_ratio)  # within 1 % of the root, exact at both ends
    lo = np.full(r.shape, 0.67)
    hi = np.full(r.shape, 1.18)
    todo = np.arange(r.size)
    for _ in range(60):
        if todo.size == 0:
            break
        var = cos2 + flat_ratio[todo, None] * sin2  # variances along phi, normalised to s1 = 1
        rt = r[todo]
        with np.errstate(under="ignore"):
            e = np.exp(-(rt * rt)[:, None] / (2.0 * var))
        excess = np.mean(e, axis=-1) - 0.5  # decreasing in r; zero at the CEP50
        slope = -np.mean((rt[:, None] / var) * e, axis=-1)
        lo[todo] = np.where(excess > 0.0, rt, lo[todo])
        hi[todo] = np.where(excess > 0.0, hi[todo], rt)
        new = rt - excess / slope
        new = np.where((new > lo[todo]) & (new < hi[todo]), new, 0.5 * (lo[todo] + hi[todo]))
        r[todo] = new
        todo = todo[np.abs(new - rt) > 1e-13]
    out = r.reshape(ratio.shape) * np.sqrt(lam_max)
    return np.where(ok, out, np.where(np.isfinite(lam_max), 0.0, np.nan))


# --------------------------------------------------------------------------- partials


def _analytic_partials(kind: str, a: PathSpec, b: PathSpec) -> dict[tuple[Any, ...], float]:
    """Partials of one row with respect to the clock, oscillator and bias parameters (MATH.md section 9)."""
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
        add(("site_osc", a.site_id), -float(a.f_tune_hz))
        add(("site_osc", b.site_id), float(b.f_tune_hz))
        add(("sat_osc", a.sat_id), float(a.translation_hz))
        add(("sat_osc", b.sat_id), -float(b.translation_hz))
    return out


def _analytic_sigma(key: tuple[Any, ...], unc: Uncertainty) -> float:
    if key[0] == "clock":
        return float(unc.site_clock_s.get(key[1], 0.0))
    if key[0] == "site_osc":
        return float(unc.site_osc_frac.get(key[1], 0.0))
    if key[0] == "sat_osc":
        return float(unc.sat_osc_frac.get(key[1], 0.0))
    if key[0] == "bias":
        return float(unc.tdoa_bias_s)
    raise ValueError(f"unknown consider parameter {key!r}")


def _ephemeris_params(sats: Sequence[str], unc: Uncertainty) -> list[tuple[str, bool, int, float]]:
    """``(sat_id, is_velocity, component, sigma)`` of every non-zero ephemeris sigma."""
    out = []
    for sat in sats:
        for is_vel, table in ((False, unc.sat_pos_ric_m), (True, unc.sat_vel_ric_mps)):
            if sat not in table:
                continue
            for comp, sigma in enumerate(np.asarray(table[sat], dtype=np.float64)):
                if sigma > 0.0:
                    out.append((sat, is_vel, comp, float(sigma)))
    return out


def _ephemeris_partials(
    x: np.ndarray,
    a: PathSpec,
    b: PathSpec,
    t_s: float,
    params: Sequence[tuple[str, bool, int, float]],
    orbits: Mapping[str, Orbit],
    sites: Mapping[str, Any],
    base: tuple[np.ndarray, np.ndarray],
) -> np.ndarray:
    """``d(tdoa, fdoa) / d param`` of one pair at positions ``x`` (``[..., 3]``): ``[..., 2, n]``.

    One-sided differences of ``predict_pair`` with the satellite replaced by
    ``PerturbedOrbit(base, ric_pos, ric_vel, t_ref = t_s)`` (1 km, 0.02 m/s steps, the
    velocity parameter defined with ``t_ref`` at the row's epoch as in MATH.md
    section 9); zero for satellites the pair does not use. Units: row units per metre
    or per m/s.
    """
    out = np.zeros(x.shape[:-1] + (2, len(params)))
    for col, (sat, is_vel, comp, _) in enumerate(params):
        if sat not in (a.sat_id, b.sat_id):
            continue
        step = _RIC_VEL_STEP_MPS if is_vel else _RIC_POS_STEP_M
        offset = np.zeros(3)
        offset[comp] = step
        perturbed = dict(orbits)
        perturbed[sat] = (
            PerturbedOrbit(orbits[sat], np.zeros(3), offset, t_s) if is_vel else PerturbedOrbit(orbits[sat], offset, np.zeros(3), t_s)
        )
        tdoa, fdoa = predict_pair(x, a, b, t_s, perturbed, sites)
        out[..., 0, col] = (tdoa - base[0]) / step
        out[..., 1, col] = (fdoa - base[1]) / step
    return out


# --------------------------------------------------------------------------- the map


@dataclass
class _Pair:
    """Everything the map needs about one listed pair."""

    index: int
    a_channel: str  # as listed (possibly "channel@sat")
    b_channel: str
    a_id: str = ""  # bare channel ids
    b_id: str = ""
    a: PathSpec | None = None
    b: PathSpec | None = None
    tp_a: Any = None
    tp_b: Any = None
    plan: CapturePlan | None = None
    noise_bw_hz: float | None = None
    n_taps: int = 1
    windows: Any = None
    cal_kinds: tuple[str, ...] = ()
    ref: dict[str, Any] | None = None
    reason: str | None = None
    notes: list[str] = field(default_factory=list)
    capture_a: tuple[Any, ...] | None = None  # identity of each capture (channel, rate, length, centre)
    capture_b: tuple[Any, ...] | None = None
    shares: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)  # kind -> per-cell shares


def accuracy_map(
    config: Any,
    pairs: Sequence[tuple[str, str]],
    emitter: Any,
    f_e_hz: float,
    t_ns: int,
    region: Any,
    step_deg: float,
    kinds: Sequence[str] = ("tdoa", "fdoa"),
    reference_carrier_id: str | None = None,
    integration_s: float | None = None,
) -> dict[str, Any]:
    """Predicted location accuracy over a grid: the planning map (MATH.md section 10).

    config: the system configuration (read by attribute). pairs: ``[(channel_a,
    channel_b), ...]``; each channel correlates the carrier on the satellite it watches
    that relays ``f_e_hz``. An end may be ``"channel_id@sat_id"`` to name that satellite
    for a channel watching several that relay the carrier; otherwise both ends are
    resolved together (``pair_satellites``: an ambiguous end takes the satellite the
    other end does not use). emitter: an ``EmitterModel`` (``eirp_dbw`` on
    boresight, ``antenna``, ``pointing``, ``bandwidth_hz``, ``waveform``). f_e_hz:
    uplink carrier, Hz. t_ns: epoch of every row (``t_mid``), integer UTC ns. region:
    the grid's region (``grid(step_deg)``, ``contains``). step_deg: grid step,
    degrees. kinds: rows to form, any of ``"tdoa"``, ``"fdoa"``.
    reference_carrier_id: a configuration carrier with ``location`` and ``emitter``;
    each pair whose two transponders also relay it is differenced against it.
    integration_s: capture length, s, replacing ``processing.capture.duration_s``
    (without the ``max_samples`` cap); None uses the tasking rule.

    Per cell and pair (ARCHITECTURE.md section 7 tasking rules): the capture plan
    (``capture_plan`` with the band ``f_e_hz +/- bandwidth_hz / 2``), the search windows
    (``pair_search``; calibrated kinds keep only the fixed margins), ``B`` and the
    filter of the estimator (``correlation_bandwidth_hz`` of ``max(occupied band,
    MIN_SIGNAL_BW_HZ)``, as tasking passes it, so a CW is band-limited too), ``T`` at the
    predicted TDOA (``integration_time_s``), the two path budgets (with the configured
    carriers that have a location and an emitter model sharing the transponder), input
    SNRs in ``B`` against each channel's thermal noise, the relayed noise of every
    satellite the channel hears and the copies of the carrier those other satellites
    relay into it (co-channel copies are noise to the pair), the post-correlation SNR
    ``B T C`` and the Stein sigmas, plus the estimator's coherent co-channel term for a
    pair at one site whose channels both hear both satellites (``caf.CafResult.
    leak_excess``, taken with the copies in phase). A pair contributes only where both
    satellites are at or above ``solver.min_elevation_deg``, both paths exist and
    ``10 log10(B T C) >= caf.min_snr_db``; its TDOA row only where ``sigma_tdoa <=
    caf.max_tdoa_sigma_s``. ``P_total`` follows MATH.md section 9 with
    ``config.resolved_uncertainty()``, the propagation media included, and the noise of
    a channel's capture shared by every listed pair that uses it (each channel's share
    from the SNRs, as ``CafResult.noise_share_*``). With a reference: its own budgets,
    plan and windows give ``sigma_ref`` (the reference measurement becomes a consider
    parameter with that sigma; references of one channel share its reference capture's
    noise), and every consider partial becomes ``H_c(x) - H_c,ref(x_ref)`` at the
    reference's ``f_e`` (clock, pair bias and satellite oscillator terms cancel exactly;
    site oscillators leave the difference of the tunings; the media leave the uplinks at
    the emitter and at the reference).

    Returns a JSON-ready dict: ``lat_deg[n_lat]``, ``lon_deg[n_lon]`` (unwrapped, as
    ``region.grid``), and ``[n_lat][n_lon]`` grids ``semi_major_95_m``,
    ``semi_minor_95_m``, ``azimuth_deg`` (of the 95 % ellipse), ``cep50_m``,
    ``dominant`` (one of ``solver.ERROR_SOURCES``), ``n_rows``; ``null`` where a cell
    is not admissible (outside the region, or no pair has both satellites above the
    mask) or unobservable (fewer than two rows, or the section 9 observability rule
    fails); ``reason[n_lat][n_lon]`` says which (``"not admissible"``,
    ``"unobservable"``) or is null. Also ``confidence`` (0.95), ``t_ns``, ``f_e_hz``,
    ``step_deg``, ``kinds``, ``reference_carrier_id`` and ``pairs``: per pair the
    channels, satellites, transponders, ``sample_rate_hz``, ``n_samples``,
    ``noise_bandwidth_hz``, ``calibrated`` kinds, the reference's predicted sigmas
    and SNR, and a ``reason`` when the pair contributes nothing anywhere.

    Raises ValueError naming the argument for invalid input.
    """
    t = _integer_ns(t_ns, "t_ns")
    t_s = sec(t)
    f_e = _finite(f_e_hz, "f_e_hz")
    if not f_e > 0.0:
        raise ValueError(f"f_e_hz must be positive, got {f_e_hz!r}")
    step = _finite(step_deg, "step_deg")
    if not step > 0.0:
        raise ValueError(f"step_deg must be positive, got {step_deg!r}")
    kind_list = _kinds(kinds)
    pair_list = _pairs(pairs)
    bandwidth = _finite(emitter.bandwidth_hz, "emitter.bandwidth_hz")
    if bandwidth < 0.0:
        raise ValueError(f"emitter.bandwidth_hz must not be negative, got {emitter.bandwidth_hz!r}")
    rms_bw = model_rms_bandwidth_hz(emitter)

    processing = config.processing
    caf_cfg, solver_cfg = processing.caf, processing.solver
    height = float(solver_cfg.height_m)
    mask_deg = float(solver_cfg.min_elevation_deg)
    min_snr_db = float(caf_cfg.min_snr_db)
    max_tdoa_sigma = float(caf_cfg.max_tdoa_sigma_s)
    unc = config.resolved_uncertainty()
    orbits = config.orbits()
    sites = config.sites_xyz()

    lat_axis, lon_axis = (np.asarray(v, dtype=np.float64).reshape(-1) for v in region.grid(step))
    lat2, lon2 = np.meshgrid(lat_axis, lon_axis, indexing="ij")
    shape = lat2.shape
    x = lla_to_ecef(lat2, lon2, height)  # [n_lat, n_lon, 3]
    inside = np.asarray(region.contains(lat2, lon2), dtype=bool)

    reference = None
    x_ref = None
    if reference_carrier_id is not None:
        reference = config.carrier(reference_carrier_id)
        if reference.location is None or reference.emitter is None:
            raise ValueError(
                f"reference_carrier_id: carrier {reference_carrier_id!r} needs a location and an emitter model"
            )
        x_ref = np.asarray(config.carrier_xyz(reference_carrier_id), dtype=np.float64)

    # ---- per pair: paths, plan, windows, budgets, sigmas
    models: list[_Pair] = []
    m = 2 * len(pair_list)
    sigma = np.ones(shape + (m,))
    active = np.zeros(shape + (m,), dtype=bool)
    usable_any = np.zeros(shape, dtype=bool)
    for k, (ch_a, ch_b) in enumerate(pair_list):
        pm = _Pair(k, ch_a, ch_b, split_channel_ref(ch_a)[0], split_channel_ref(ch_b)[0])
        models.append(pm)
        found_a, found_b = pair_satellites(config, ch_a, ch_b, f_e, f"pairs[{k}]")
        if found_a is None or found_b is None:
            pm.reason = f"channel {ch_a if found_a is None else ch_b!r} watches no satellite relaying {f_e:.0f} Hz"
            continue
        (sat_a, tp_a), (sat_b, tp_b) = found_a, found_b
        if sat_a == sat_b:
            raise ValueError(f"pairs[{k}]: both channels use satellite {sat_a!r}; a pair needs two satellites")
        id_a, id_b = pm.a_id, pm.b_id
        site_a, site_b = config.channel(id_a)[0].id, config.channel(id_b)[0].id
        plan = capture_plan(config, [id_a, id_b], f_e - 0.5 * bandwidth, f_e + 0.5 * bandwidth, duration_s=integration_s)
        a = PathSpec(sat_a, site_a, plan.f_e_nom_hz, float(tp_a.translation_hz), float(tp_a.group_delay_s))
        b = PathSpec(sat_b, site_b, plan.f_e_nom_hz, float(tp_b.translation_hz), float(tp_b.group_delay_s))
        pm.a, pm.b, pm.tp_a, pm.tp_b, pm.plan = a, b, tp_a, tp_b, plan
        pm.capture_a = (id_a, plan.sample_rate_hz, plan.n_samples, round(a.f_tune_hz))
        pm.capture_b = (id_b, plan.sample_rate_hz, plan.n_samples, round(b.f_tune_hz))

        if reference is not None:
            pm.ref = _reference_model(config, reference, x_ref, pm, t, kind_list, unc, orbits, sites, integration_s)
            if pm.ref.get("reason") is None:
                pm.cal_kinds = tuple(kd for kd in kind_list if pm.ref["sigma"].get(kd) is not None)
            else:
                pm.notes.append(f"not calibrated: {pm.ref['reason']}")

        search = pair_search(config, a, b, t_s, plan.sample_rate_hz, plan.occupied_bw_hz)
        if search is None:
            pm.reason = "no admissible emitter position for this pair"
            continue
        correction = (0.0 if "tdoa" in pm.cal_kinds else None, 0.0 if "fdoa" in pm.cal_kinds else None)
        pm.windows = search.windows(caf_cfg, unc, correction if pm.cal_kinds else None)
        reach = _doppler_reach(pm.windows)
        # as tasking: the band filter is never narrower than MIN_SIGNAL_BW_HZ, so a CW is filtered too
        pm.noise_bw_hz, pm.n_taps = correlation_bandwidth_hz(
            plan.sample_rate_hz, max(plan.occupied_bw_hz, MIN_SIGNAL_BW_HZ), reach, plan.n_samples
        )

        channel_a = _channel_power(config, emitter, f_e, x, id_a, sat_a, tp_a, search.heard_a, t_s, orbits, None)
        channel_b = _channel_power(config, emitter, f_e, x, id_b, sat_b, tp_b, search.heard_b, t_s, orbits, None)
        if channel_a is None or channel_b is None:
            pm.reason = f"satellite {sat_a if channel_a is None else sat_b!r} is below the horizon of site {site_a if channel_a is None else site_b!r}"
            continue
        visible = (
            above_mask(lat2, lon2, x, orbit_position(orbits[sat_a], t_s), mask_deg)
            & above_mask(lat2, lon2, x, orbit_position(orbits[sat_b], t_s), mask_deg)
            & inside
        )
        visible &= (channel_a.signal > 0.0) & (channel_b.signal > 0.0)
        usable_any |= visible
        g_a = np.where(visible, channel_a.snr(pm.noise_bw_hz), 0.0)
        g_b = np.where(visible, channel_b.snr(pm.noise_bw_hz), 0.0)
        tdoa_pred, _ = predict_pair(x, a, b, t_s, orbits, sites)
        t_int = np.asarray(integration_time_s(plan.n_samples, pm.n_taps, plan.sample_rate_hz, tdoa_pred))
        ok_t = t_int > 0.0
        t_safe = np.where(ok_t, t_int, 1.0)
        rho = np.asarray(post_correlation_snr(pm.noise_bw_hz, t_safe, g_a, g_b))
        with np.errstate(divide="ignore"):
            detected = visible & ok_t & (10.0 * np.log10(np.where(rho > 0.0, rho, 1e-300)) >= min_snr_db)
        g_a_d, g_b_d = np.where(detected, g_a, 1.0), np.where(detected, g_b, 1.0)
        sig_t, sig_f = stein_sigmas(pm.noise_bw_hz, rms_bw, t_safe, g_a_d, g_b_d)
        sig_t, sig_f = np.broadcast_to(sig_t, shape), np.broadcast_to(sig_f, shape)
        # co-channel copies at one site: their self-peak sidelobes add in amplitude (caf._leak_excess)
        excess = np.zeros(shape)
        if site_a == site_b and sat_b in search.heard_a and sat_a in search.heard_b:
            with np.errstate(divide="ignore", invalid="ignore"):
                excess = np.where(
                    detected,
                    2.0 * np.sqrt(channel_a.copies.get(sat_b, 0.0) * channel_b.copies.get(sat_a, 0.0)
                                  / np.where(detected, channel_a.signal * channel_b.signal, 1.0)),
                    0.0,
                )
            t_e2 = (math.pi * t_safe) ** 2 / 3.0
            sig_f = np.sqrt(sig_f**2 + excess / (2.0 * pm.noise_bw_hz * t_safe * t_e2))
            if rms_bw > 0.0:
                sig_t = np.sqrt(sig_t**2 + excess / (2.0 * pm.noise_bw_hz * t_safe * (2.0 * math.pi * rms_bw) ** 2))
        # each channel's own noise share of the variances (rows correlated from one capture share it)
        q = max(1.0, pm.noise_bw_hz**2 / (12.0 * rms_bw**2)) if rms_bw > 0.0 else 1.0
        inv_a, inv_b = 1.0 / g_a_d, 1.0 / g_b_d
        for kind, q_kind in (("tdoa", q), ("fdoa", 1.0)):
            total = inv_a + inv_b + q_kind * inv_a * inv_b + excess
            pm.shares[kind] = (np.where(detected, inv_a / total, 0.0), np.where(detected, inv_b / total, 0.0))
        if "tdoa" in kind_list:
            row_t = detected & np.isfinite(sig_t) & (sig_t <= max_tdoa_sigma)
            active[..., 2 * k] = row_t
            sigma[..., 2 * k] = np.where(row_t, sig_t, 1.0)
        if "fdoa" in kind_list:
            row_f = detected & np.isfinite(sig_f)
            active[..., 2 * k + 1] = row_f
            sigma[..., 2 * k + 1] = np.where(row_f, sig_f, 1.0)
        if not np.any(detected):
            pm.notes.append(f"post-correlation SNR below caf.min_snr_db = {min_snr_db:g} dB everywhere")

    admissible = inside & usable_any
    flat = admissible.reshape(-1)
    cells = np.flatnonzero(flat & np.any(active.reshape(-1, m), axis=1))
    n_cells = shape[0] * shape[1]
    total = np.full((n_cells, 2, 2), np.nan)
    budget = np.full((n_cells, len(ERROR_SOURCES)), np.nan)
    observable = np.zeros(n_cells, dtype=bool)
    rows_per_cell = np.sum(active.reshape(-1, m), axis=1)
    if cells.size:
        xc = x.reshape(-1, 3)[cells]
        cell_sigma = sigma.reshape(-1, m)[cells]
        jac, gain, categories, own_sigma = _design(
            models, xc, lat2.reshape(-1)[cells], lon2.reshape(-1)[cells], t_s, height, unc, orbits, sites, m,
            cell_sigma, cells, reference_carrier_id,
        )
        out = covariance_batch(jac, own_sigma, active.reshape(-1, m)[cells], gain, categories)
        total[cells] = out["total"]
        budget[cells] = out["budget"]
        observable[cells] = out["observable"]

    # ---- ellipse, CEP50, dominant source
    a11, a12, a22 = total[:, 0, 0], total[:, 0, 1], total[:, 1, 1]
    half_tr, root = 0.5 * (a11 + a22), np.hypot(0.5 * (a11 - a22), a12)
    semi_major = _ELLIPSE_SCALE * np.sqrt(np.clip(half_tr + root, 0.0, None))
    semi_minor = _ELLIPSE_SCALE * np.sqrt(np.clip(half_tr - root, 0.0, None))
    azimuth = np.mod(0.5 * np.degrees(np.arctan2(2.0 * a12, a22 - a11)), 180.0)
    azimuth = np.where(azimuth >= 180.0, 0.0, azimuth)
    cep = np.full(n_cells, np.nan)
    if np.any(observable):
        cep[observable] = cep50_batch(total[observable])
    dominant_idx = np.argmax(np.where(np.isfinite(budget), budget, -np.inf), axis=1)

    reason = np.where(flat, REASON_UNOBSERVABLE, REASON_NOT_ADMISSIBLE).astype(object)
    reason[observable] = None
    valid = observable

    def grid(values: np.ndarray, cast: Any = float) -> list[list[Any]]:
        out_rows = []
        for i in range(shape[0]):
            row = []
            for j in range(shape[1]):
                c = i * shape[1] + j
                row.append(cast(values[c]) if valid[c] else None)
            out_rows.append(row)
        return out_rows

    return {
        "lat_deg": [float(v) for v in lat_axis],
        "lon_deg": [float(v) for v in lon_axis],
        "semi_major_95_m": grid(semi_major),
        "semi_minor_95_m": grid(semi_minor),
        "azimuth_deg": grid(azimuth),
        "cep50_m": grid(cep),
        "dominant": grid(np.array([ERROR_SOURCES[i] for i in dominant_idx], dtype=object), str),
        "n_rows": grid(rows_per_cell, int),
        "reason": [[reason[i * shape[1] + j] for j in range(shape[1])] for i in range(shape[0])],
        "confidence": ACCURACY_CONFIDENCE,
        "t_ns": t,
        "f_e_hz": f_e,
        "step_deg": step,
        "kinds": list(kind_list),
        "reference_carrier_id": reference_carrier_id,
        "pairs": [_pair_summary(pm) for pm in models],
    }


def _stencil(lat: np.ndarray, lon: np.ndarray, height: float) -> np.ndarray:
    """ECEF points ``[P, 4, 3]`` 100 m east, west, north and south of each cell at ``height``.

    Metres are converted at the cell's latitude exactly as the solver does
    (``dE = (N + h) cos(lat) dlon``, ``dN = (M + h) dlat``); a step over a pole is
    continued onto the other side.
    """
    meridional, prime = radii_of_curvature(lat)
    cos_lat = np.maximum(np.cos(np.radians(lat)), 1e-6)
    deg_e = np.degrees(_JAC_STEP_M / ((prime + height) * cos_lat))
    deg_n = np.degrees(_JAC_STEP_M / (meridional + height))
    d_lat = np.stack([np.zeros_like(lat), np.zeros_like(lat), deg_n, -deg_n], axis=-1)
    d_lon = np.stack([deg_e, -deg_e, np.zeros_like(lat), np.zeros_like(lat)], axis=-1)
    lat4 = lat[:, None] + d_lat
    lon4 = lon[:, None] + d_lon
    over = np.abs(lat4) > 90.0
    lat4 = np.where(over, np.sign(lat4) * 180.0 - lat4, lat4)
    lon4 = np.where(over, lon4 + 180.0, lon4)
    return lla_to_ecef(lat4, lon4, height)


def _design(
    models: Sequence[_Pair],
    x: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    t_s: float,
    height: float,
    unc: Uncertainty,
    orbits: Mapping[str, Orbit],
    sites: Mapping[str, Any],
    m: int,
    sigma: np.ndarray,
    cells: np.ndarray,
    reference_id: str | None,
) -> tuple[np.ndarray, np.ndarray | None, list[str], np.ndarray]:
    """``J`` ``[P, m, 2]``, ``G = H_c S_c^(1/2)`` ``[P, m, n]`` and the rows' own sigmas at
    cells ``x`` (``[P, 3]``; ``cells`` their flat grid indices, ``sigma`` ``[P, m]`` the
    rows' CAF sigmas).

    Rows ``2k`` (TDOA) and ``2k + 1`` (FDOA) belong to pair ``k``. ``J`` by central
    differences of ``predict_pair`` over 100 m east and north; ``G`` from the analytic
    clock / oscillator / bias partials, the ephemeris partials, one reference column
    per calibrated row, the propagation media (``core.media``: one VTEC and one zenith
    delay per ground end, ``sigma sqrt(1 - rho)`` each, plus a common column
    ``sigma sqrt(rho)``; calibrated rows subtract the reference's), each scaled by its
    one-sigma, and the noise of captures used by two or more pairs (category
    ``"noise"``; the rows' own sigmas returned keep the rest, as in the solver). A
    capture is a channel at one plan (rate, length, centre); reference captures shared
    by two references split the same way (category ``"reference"``).
    """
    p = x.shape[0]
    stencil = _stencil(lat, lon, height)  # [P, 4, 3]: 100 m east, west, north, south
    jac = np.zeros((p, m, 2))
    keys: dict[tuple[Any, ...], int] = {}
    sigmas: list[float] = []
    categories: list[str] = []
    const_rows: list[tuple[int, int, float]] = []  # (row, column, value) of position-independent entries
    cell_entries: list[tuple[int, int, np.ndarray]] = []  # (row, column, [P] values) of per-cell entries

    def column(key: tuple[Any, ...], sigma_c: float, category: str) -> int:
        if key not in keys:
            keys[key] = len(sigmas)
            sigmas.append(sigma_c)
            categories.append(category)
        return keys[key]

    used = [pm for pm in models if pm.windows is not None and pm.a is not None and pm.b is not None]
    sats: list[str] = []
    for pm in used:
        for sat in (pm.a.sat_id, pm.b.sat_id):  # type: ignore[union-attr]
            if sat not in sats:
                sats.append(sat)
    eph = _ephemeris_params(sats, unc)
    eph_cols = [column(("eph", sat, v, c), sig, "ephemeris_velocity" if v else "ephemeris_position") for sat, v, c, sig in eph]
    eph_entries: list[tuple[int, np.ndarray]] = []  # (row, [P, n_eph] partial times sigma)

    # captures (and reference captures) used by two or more pairs share their noise
    capture_users: dict[tuple[Any, ...], int] = {}
    ref_users: dict[tuple[Any, ...], int] = {}
    for pm in used:
        for cap in (pm.capture_a, pm.capture_b):
            capture_users[cap] = capture_users.get(cap, 0) + 1  # type: ignore[index]
        if pm.cal_kinds and pm.ref is not None:
            for cap in (pm.ref["capture_a"], pm.ref["capture_b"]):
                ref_users[cap] = ref_users.get(cap, 0) + 1
    own = sigma.copy()

    sig_eph = np.array([sig for *_, sig in eph])
    media = [(name, sig, rho) for name, sig, rho in (
        ("ionosphere", float(unc.iono_vtec_tecu), float(unc.iono_correlation)),
        ("troposphere", float(unc.tropo_zenith_m), float(unc.tropo_correlation)),
    ) if sig > 0.0]
    media_rows: dict[str, dict[Any, np.ndarray]] = {name: {} for name, _, _ in media}  # end -> [P, m] partials

    def media_add(name: str, end: Any, row: int, values: np.ndarray) -> None:
        table = media_rows[name]
        if end not in table:
            table[end] = np.zeros((p, m))
        table[end][:, row] += values

    for pm in used:
        a, b, k = pm.a, pm.b, pm.index
        assert a is not None and b is not None
        tdoa_s, fdoa_s = predict_pair(stencil, a, b, t_s, orbits, sites)  # [P, 4]
        jac[:, 2 * k, 0] = (tdoa_s[:, 0] - tdoa_s[:, 1]) / (2.0 * _JAC_STEP_M)
        jac[:, 2 * k, 1] = (tdoa_s[:, 2] - tdoa_s[:, 3]) / (2.0 * _JAC_STEP_M)
        jac[:, 2 * k + 1, 0] = (fdoa_s[:, 0] - fdoa_s[:, 1]) / (2.0 * _JAC_STEP_M)
        jac[:, 2 * k + 1, 1] = (fdoa_s[:, 2] - fdoa_s[:, 3]) / (2.0 * _JAC_STEP_M)
        base = predict_pair(x, a, b, t_s, orbits, sites)
        partial = _ephemeris_partials(x, a, b, t_s, eph, orbits, sites, (np.asarray(base[0]), np.asarray(base[1])))
        ref = pm.ref if pm.cal_kinds else None
        ref_partial = None
        if ref is not None and eph:
            # H_c,ref: the same parameters at the reference's position, frequency and epoch
            x_ref = np.asarray(ref["x_ref"], dtype=np.float64)[None, :]
            ref_base = predict_pair(x_ref, ref["a"], ref["b"], t_s, orbits, sites)
            ref_partial = _ephemeris_partials(
                x_ref, ref["a"], ref["b"], t_s, eph, orbits, sites, (np.asarray(ref_base[0]), np.asarray(ref_base[1]))
            )[0]  # [2, n_eph]
        for kind_i, kind in enumerate(("tdoa", "fdoa")):
            row = 2 * k + kind_i
            entries = _analytic_partials(kind, a, b)
            calibrated = ref is not None and kind in pm.cal_kinds
            values = partial[:, kind_i, :]
            if calibrated:
                assert ref is not None
                for key, value in _analytic_partials(kind, ref["a"], ref["b"]).items():
                    entries[key] = entries.get(key, 0.0) - value
                if ref_partial is not None:
                    values = values - ref_partial[kind_i][None, :]
                ref_sigma = float(ref["sigma"][kind])
                own_fraction = 1.0
                for cap, share, side in ((ref["capture_a"], ref["shares"][kind][0], 1.0),
                                         (ref["capture_b"], ref["shares"][kind][1], -1.0)):
                    if share > 0.0 and ref_users.get(cap, 0) >= 2:
                        col = column(("refcap", cap, kind), 1.0, "reference")
                        const_rows.append((row, col, side * ref_sigma * math.sqrt(share)))
                        own_fraction -= share
                own_sigma = ref_sigma * math.sqrt(max(own_fraction, 0.01))
                col = column(("ref", k, kind), own_sigma, "reference")
                const_rows.append((row, col, own_sigma))
            for key, value in entries.items():
                sig = _analytic_sigma(key, unc)
                if sig > 0.0 and value != 0.0:
                    col = column(key, sig, _CATEGORY[key[0]])
                    const_rows.append((row, col, value * sig))
            if eph:
                eph_entries.append((row, values * sig_eph))
            # this pair's captures, shared with other pairs: the noise columns and the rows' own rest
            shares = pm.shares.get(kind)
            if shares is not None:
                fraction = np.zeros(p)
                for cap, share, side in ((pm.capture_a, shares[0], 1.0), (pm.capture_b, shares[1], -1.0)):
                    if capture_users.get(cap, 0) >= 2:  # type: ignore[arg-type]
                        share_c = np.asarray(share).reshape(-1)[cells]
                        col = column(("cap", cap, kind), 1.0, "noise")
                        cell_entries.append((row, col, side * sigma[:, row] * np.sqrt(share_c)))
                        fraction += share_c
                own[:, row] = sigma[:, row] * np.sqrt(np.maximum(1.0 - fraction, 0.01))
            # propagation media: the emitter's uplinks, the sites' downlinks, minus the reference's
            if media:
                parts = row_media_partials(kind, a, b, np.array([t_s]), x, orbits, sites)
                for name, _, _ in media:
                    for end, part in parts[name].items():
                        media_add(name, end, row, np.broadcast_to(np.asarray(part)[..., 0], (p,)))
                if calibrated:
                    assert ref is not None
                    ref_parts = row_media_partials(kind, ref["a"], ref["b"], np.array([t_s]), np.asarray(ref["x_ref"]),
                                                   orbits, sites, ("ref", reference_id))
                    for name, _, _ in media:
                        for end, part in ref_parts[name].items():
                            media_add(name, end, row, np.full(p, -float(np.asarray(part)[0])))

    media_cols: list[tuple[int, np.ndarray]] = []
    for name, sig, rho in media:
        ends = list(media_rows[name].values())
        if not ends:
            continue
        if rho < 1.0:
            for end_k, values in enumerate(ends):
                media_cols.append((column(("media", name, end_k), 1.0, name), values * (sig * math.sqrt(1.0 - rho))))
        if rho > 0.0:
            media_cols.append((column(("media", name, "common"), 1.0, name), np.sum(ends, axis=0) * (sig * math.sqrt(rho))))

    n = len(sigmas)
    if n == 0:
        return jac, None, [], own
    gain = np.zeros((p, m, n))
    for row, col, value in const_rows:
        gain[:, row, col] += value
    for row, col, cell_values in cell_entries:
        gain[:, row, col] += cell_values
    for row, values in eph_entries:
        gain[:, row, eph_cols] += values
    for col, values in media_cols:
        gain[:, :, col] += values
    return jac, gain, categories, own


def _reference_model(
    config: Any,
    carrier: Any,
    x_ref: np.ndarray,
    pm: _Pair,
    t_ns: int,
    kinds: Sequence[str],
    unc: Uncertainty,
    orbits: Mapping[str, Orbit],
    sites: Mapping[str, Any],
    integration_s: float | None,
) -> dict[str, Any]:
    """Predicted reference measurement on a pair: its paths, sigmas, SNR and captures.

    The reference must be relayed by the pair's two transponders (MATH.md section 9: same
    satellites, sites and transponders). Its capture plan, windows (prediction at its
    surveyed position, uncalibrated widening), ``B`` (never narrower than
    ``MIN_SIGNAL_BW_HZ``), ``T`` and budgets follow the same rules as the target's, with
    the carrier's own ``EmitterModel`` and frequency; each channel's noise includes the
    copies of the reference relayed by the other satellites that channel hears. Returns
    ``{"reason": str}`` when it cannot calibrate the pair.
    """
    assert pm.a is not None and pm.b is not None
    f_ref = float(carrier.uplink_center_hz)
    model = carrier.emitter
    tp_a = relaying_transponder(config.satellite(pm.a.sat_id), f_ref)
    tp_b = relaying_transponder(config.satellite(pm.b.sat_id), f_ref)
    if tp_a is None or tp_b is None or tp_a.id != pm.tp_a.id or tp_b.id != pm.tp_b.id:
        return {"reason": f"reference {carrier.id!r} is not relayed by the pair's transponders"}
    width = float(model.bandwidth_hz)
    plan = capture_plan(config, [pm.a_id, pm.b_id], f_ref - 0.5 * width, f_ref + 0.5 * width, duration_s=integration_s)
    t_s = sec(t_ns)
    ra = PathSpec(pm.a.sat_id, pm.a.site_id, plan.f_e_nom_hz, pm.a.translation_hz, pm.a.tau_x_s)
    rb = PathSpec(pm.b.sat_id, pm.b.site_id, plan.f_e_nom_hz, pm.b.translation_hz, pm.b.tau_x_s)
    search = pair_search(config, ra, rb, t_s, plan.sample_rate_hz, plan.occupied_bw_hz, point_xyz=x_ref)
    if search is None:
        return {"reason": f"reference {carrier.id!r}: no window"}
    windows = search.windows(config.processing.caf, unc)
    noise_bw, n_taps = correlation_bandwidth_hz(
        plan.sample_rate_hz, max(plan.occupied_bw_hz, MIN_SIGNAL_BW_HZ), _doppler_reach(windows), plan.n_samples
    )
    powers = []
    for sat, tp, ch, heard in ((ra.sat_id, tp_a, pm.a_id, search.heard_a), (rb.sat_id, tp_b, pm.b_id, search.heard_b)):
        power = _channel_power(config, model, f_ref, x_ref, ch, sat, tp, heard, t_s, orbits, carrier.id)
        if power is None or not float(power.signal) > 0.0:
            return {"reason": f"reference {carrier.id!r} has no path through {sat!r} into {ch!r}"}
        powers.append(power)
    g_a, g_b = (float(pw.snr(noise_bw)) for pw in powers)
    tdoa_ref, _ = predict_pair(x_ref, ra, rb, t_s, orbits, sites)
    t_int = float(integration_time_s(plan.n_samples, n_taps, plan.sample_rate_hz, float(tdoa_ref)))
    if not t_int > 0.0:
        return {"reason": f"reference {carrier.id!r}: delay leaves no overlap"}
    snr_db = 10.0 * math.log10(post_correlation_snr(noise_bw, t_int, g_a, g_b))
    if snr_db < float(config.processing.caf.min_snr_db):
        return {"reason": f"reference {carrier.id!r}: predicted post-correlation SNR {snr_db:.1f} dB below threshold",
                "snr_db": snr_db}
    rms_ref = model_rms_bandwidth_hz(model)
    sig_t, sig_f = stein_sigmas(noise_bw, rms_ref, t_int, g_a, g_b)
    excess = 0.0
    if ra.site_id == rb.site_id and rb.sat_id in search.heard_a and ra.sat_id in search.heard_b:
        excess = 2.0 * math.sqrt(float(powers[0].copies.get(rb.sat_id, 0.0)) * float(powers[1].copies.get(ra.sat_id, 0.0))
                                 / (float(powers[0].signal) * float(powers[1].signal)))
        sig_f = math.sqrt(sig_f**2 + excess / (2.0 * noise_bw * t_int * (math.pi * t_int) ** 2 / 3.0))
        if rms_ref > 0.0:
            sig_t = math.sqrt(sig_t**2 + excess / (2.0 * noise_bw * t_int * (2.0 * math.pi * rms_ref) ** 2))
    sigma: dict[str, float | None] = {
        "tdoa": float(sig_t) if math.isfinite(sig_t) and sig_t <= float(config.processing.caf.max_tdoa_sigma_s) else None,
        "fdoa": float(sig_f) if math.isfinite(sig_f) else None,
    }
    sigma = {kd: v for kd, v in sigma.items() if kd in kinds}
    q = max(1.0, noise_bw**2 / (12.0 * rms_ref**2)) if rms_ref > 0.0 else 1.0
    shares = {}
    for kind, q_kind in (("tdoa", q), ("fdoa", 1.0)):
        total = 1.0 / g_a + 1.0 / g_b + q_kind / (g_a * g_b) + excess
        shares[kind] = ((1.0 / g_a) / total, (1.0 / g_b) / total)
    return {
        "reason": None,
        "a": ra,
        "b": rb,
        "x_ref": x_ref,
        "sigma": sigma,
        "snr_db": snr_db,
        "noise_bandwidth_hz": noise_bw,
        "integration_s": t_int,
        "sample_rate_hz": plan.sample_rate_hz,
        "n_samples": plan.n_samples,
        "capture_a": ("ref", pm.a_id, plan.sample_rate_hz, plan.n_samples, round(ra.f_tune_hz)),
        "capture_b": ("ref", pm.b_id, plan.sample_rate_hz, plan.n_samples, round(rb.f_tune_hz)),
        "shares": shares,
    }


@dataclass
class _ChannelPower:
    """What one receive channel holds of a carrier: linear powers, W and W/Hz.

    signal: the carrier through the pair's satellite (0 where that path does not exist).
    noise_density: the channel's thermal noise plus the relayed noise of every
    satellite it hears in the capture band. copies: the carrier relayed by each other
    satellite the channel hears (a co-channel copy, noise to this pair).
    """

    signal: Any
    noise_density: Any
    copies: dict[str, Any]

    def snr(self, noise_bw_hz: float) -> Any:
        """Input SNR in ``noise_bw_hz``: signal over noise and the co-channel copies."""
        rest = self.noise_density * noise_bw_hz + sum(self.copies.values())
        return np.asarray(self.signal) / rest


def _channel_power(
    config: Any, emitter: Any, f_e: float, x: np.ndarray, channel_id: str, sat_id: str, tp: Any,
    heard: Sequence[str], t_s: float, orbits: Mapping[str, Orbit], skip: str | None,
) -> _ChannelPower | None:
    """Budgets of a carrier at ``x`` (``[..., 3]`` or ``[3]``) into ``channel_id`` (bare id).

    The pair's path through ``sat_id`` (None when the satellite is below the site's
    horizon), plus, for every other satellite in ``heard`` (those whose copy reaches the
    capture), that copy's power and that transponder's relayed noise (MATH.md section
    4: a channel hears every visible satellite's downlink). ``skip``: a carrier to leave
    out of the power robbing (the one being budgeted).
    """
    x = np.asarray(x, dtype=np.float64)
    single = x.ndim == 1
    xb = x[None, :] if single else x  # a batch: a path the emitter cannot see still brings its relayed noise

    def finish(value: Any) -> Any:
        arr = np.asarray(value, dtype=np.float64)
        return float(arr.reshape(-1)[0]) if single and arr.ndim else (float(arr) if arr.ndim == 0 else arr)

    main = path_budget(config, emitter, f_e, xb, sat_id, tp.id, channel_id, t_s, _other_pfd(config, sat_id, tp, t_s, orbits, skip))
    if main is None:
        return None
    down = main.downlink
    signal = _lin_finite(down.c_rx_dbw)
    density = 10.0 ** (float(down.site_n0_dbw_hz) / 10.0) + _lin_finite(down.relayed_n0_dbw_hz)
    copies: dict[str, Any] = {}
    for other in heard:
        if other == sat_id:
            continue
        tp_o = relaying_transponder(config.satellite(other), f_e)
        if tp_o is None:
            continue
        budget = path_budget(config, emitter, f_e, xb, other, tp_o.id, channel_id, t_s,
                             _other_pfd(config, other, tp_o, t_s, orbits, skip))
        if budget is None:  # below the site's horizon: the channel does not hear it
            continue
        copies[other] = finish(_lin_finite(budget.downlink.c_rx_dbw))
        density = density + _lin_finite(budget.downlink.relayed_n0_dbw_hz)
    return _ChannelPower(finish(signal), finish(density), copies)


def _lin_finite(db: Any) -> Any:
    """``10^(dB / 10)``, 0 where the dB value is -inf (absent)."""
    arr = np.asarray(db, dtype=np.float64)
    out = np.where(np.isfinite(arr), 10.0 ** (np.where(np.isfinite(arr), arr, 0.0) / 10.0), 0.0)
    return float(out) if out.ndim == 0 else out


def _other_pfd(config: Any, sat_id: str, tp: Any, t_s: float, orbits: Mapping[str, Orbit], skip: str | None) -> list[float]:
    """Flux densities (dBW/m^2) at ``sat_id`` of the configured carriers relayed by ``tp``.

    Carriers with a location and an emitter model radiate in the configured world and
    share the transponder's power (power robbing, MATH.md section 4); ``skip`` names a
    carrier to leave out (the one being budgeted). Carriers that cannot see the
    satellite contribute nothing.
    """
    out = []
    sat_r = orbit_position(orbits[sat_id], t_s)
    for carrier in config.carriers:
        if carrier.id == skip or carrier.location is None or carrier.emitter is None:
            continue
        f_c = float(carrier.uplink_center_hz)
        found = relaying_transponder(config.satellite(sat_id), f_c)
        if found is None or found.id != tp.id:
            continue
        x_c = np.asarray(config.carrier_xyz(carrier.id), dtype=np.float64)
        up = uplink_budget(
            carrier.emitter.eirp_dbw, carrier.emitter.antenna, emitter_boresight(carrier.emitter, x_c, orbits, t_s),
            x_c, sat_r, f_c, tp,
        )
        if float(up.elevation_deg) > 0.0:
            out.append(float(up.pfd_dbw_m2))
    return out


def _doppler_reach(windows: Any) -> float:
    """Largest Doppler magnitude of ``(delay_window, doppler_window, mirror_window)``, Hz."""
    _, doppler, mirror = windows
    reach = max(abs(doppler[0]), abs(doppler[1]))
    if mirror is not None:
        reach = max(reach, abs(mirror[1][0]), abs(mirror[1][1]))
    return float(reach)


def _pair_summary(pm: _Pair) -> dict[str, Any]:
    out: dict[str, Any] = {
        "a_channel": pm.a_channel,
        "b_channel": pm.b_channel,
        "a_sat_id": None if pm.a is None else pm.a.sat_id,
        "b_sat_id": None if pm.b is None else pm.b.sat_id,
        "a_transponder_id": None if pm.tp_a is None else pm.tp_a.id,
        "b_transponder_id": None if pm.tp_b is None else pm.tp_b.id,
        "sample_rate_hz": None if pm.plan is None else pm.plan.sample_rate_hz,
        "n_samples": None if pm.plan is None else pm.plan.n_samples,
        "noise_bandwidth_hz": pm.noise_bw_hz,
        "calibrated": list(pm.cal_kinds),
        "reference": None,
        "reason": pm.reason,
        "notes": list(pm.notes),
    }
    if pm.ref is not None:
        out["reference"] = {
            "reason": pm.ref.get("reason"),
            "snr_db": pm.ref.get("snr_db"),
            "sigma_tdoa_s": (pm.ref.get("sigma") or {}).get("tdoa"),
            "sigma_fdoa_hz": (pm.ref.get("sigma") or {}).get("fdoa"),
            "noise_bandwidth_hz": pm.ref.get("noise_bandwidth_hz"),
            "integration_s": pm.ref.get("integration_s"),
        }
    return out


# --------------------------------------------------------------------------- validation helpers


def _finite(value: Any, name: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def _integer_ns(value: Any, name: str) -> int:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be integer nanoseconds, got {value!r}")
    try:
        return operator.index(value)
    except TypeError:
        raise ValueError(f"{name} must be integer nanoseconds (int), got {type(value).__name__}") from None


def _kinds(kinds: Any) -> tuple[str, ...]:
    if isinstance(kinds, str):
        raise ValueError("kinds must be a sequence such as ('tdoa', 'fdoa'), not a string")
    out = tuple(dict.fromkeys(kinds))
    if not out or any(kd not in ("tdoa", "fdoa") for kd in out):
        raise ValueError(f"kinds must be a non-empty subset of ('tdoa', 'fdoa'), got {kinds!r}")
    return out


def _pairs(pairs: Any) -> list[tuple[str, str]]:
    try:
        items = list(pairs)
    except TypeError:
        raise ValueError("pairs must be a sequence of (channel_a, channel_b)") from None
    if not items:
        raise ValueError("pairs must hold at least one (channel_a, channel_b)")
    out = []
    for k, item in enumerate(items):
        try:
            ch_a, ch_b = item
        except (TypeError, ValueError):
            raise ValueError(f"pairs[{k}] must be (channel_a, channel_b)") from None
        if not isinstance(ch_a, str) or not isinstance(ch_b, str):
            raise ValueError(f"pairs[{k}] must hold two channel ids")
        try:
            id_a, id_b = split_channel_ref(ch_a)[0], split_channel_ref(ch_b)[0]
        except ValueError as exc:
            raise ValueError(f"pairs[{k}]: {exc}") from None
        if id_a == id_b:
            raise ValueError(f"pairs[{k}]: a channel cannot be paired with itself")
        out.append((ch_a, ch_b))
    return out
