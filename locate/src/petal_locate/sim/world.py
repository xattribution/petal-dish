"""The simulated world: truth geometry plus IQ and PSD synthesis (ARCHITECTURE.md section 4).

``SimWorld(config, scenario)`` is what the simulated radios of the node agent read
from, what the console shows as truth, and what the Monte Carlo tests drive. It
implements the signal model of MATH.md section 4:

- **Emitters.** Every scenario emitter, with the defaults of the configuration carrier
  it names, plus one emitter per configuration carrier that has a ``location`` and an
  ``emitter`` and that no scenario emitter names (``ResolvedEmitter``).
- **Truth.** The true orbit of each satellite is the believed (configured) orbit
  displaced by ``errors.ephemeris`` (``PerturbedOrbit``). True time at a site is the
  labelled time plus ``delta_g`` (``errors.site_clock_offset_ns``); a site tuned to
  ``f_tune`` is really tuned to ``f_tune (1 + eps_g)``; a transponder translates by
  ``T_nom (1 + eps_sat)``; the optional thin-shell ionosphere delays envelopes and
  advances carrier phase.
- **Paths.** A channel hears, through its antenna pattern, every satellite above its
  horizon (MATH.md section 4 "Which paths a channel hears": co-channel satellites
  inside a wide beam give several copies). Each emitter whose centre frequency lies in
  a transponder's uplink passband, and which sees that satellite, is relayed by it.
  Each transponder also relays its own noise. Emitters inside a site's radio horizon
  are heard directly. Power robbing follows ``core.linkbudget``.
- **Channel filter.** Paths wholly inside the capture band are rendered at ``fs``;
  straddling ``cw``/``chirp`` paths (and ``noise``/``psk`` up to ``m = 4``) at ``m fs``
  and decimated; wider ``noise``/``psk`` paths become in-band slices; paths wholly
  outside are omitted.

Units: SI everywhere (metres, seconds, hertz, watts); powers in the link budget are
dB as in ``core.linkbudget``. Positions are ECEF (WGS84). Absolute time is integer
nanoseconds (``t_ns``); float seconds only for orbit and geometry evaluation.

Everything is deterministic: emitter waveforms, relayed transponder noise and in-band
slices are pure functions of absolute time and a key derived from the scenario seed,
so repeat calls agree sample for sample, two node processes render the same emitters,
and overlapping captures agree on their overlap. Site thermal noise is keyed per
capture exactly as MATH.md section 4 prescribes
(``thermal|seed|channel|t_start_ns|fs|fc``): identical for a repeated request,
independent between two different captures.
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from numpy.polynomial import chebyshev as _cheb
from scipy.special import fresnel as _fresnel

from petal_locate.config.models import (
    Carrier,
    IsotropicAntenna,
    ReceiveChannel,
    Satellite,
    Site,
    SystemConfig,
    TrackPointing,
    Transponder,
)
from petal_locate.core.channel import decimate, decimate_margin, decimation_fir, thermal_noise
from petal_locate.core.constants import C, K_DBW, T_REF_S, WGS84_A
from petal_locate.core.geodesy import azel_to_ecef, ecef_to_lla, lla_to_ecef
from petal_locate.core.geometry import (
    STENCIL_STEP_S,
    baseband_frequency,
    bent_pipe,
    bent_pipe_rates,
    downlink_delay,
)
from petal_locate.core.iono import group_delay_s
from petal_locate.core.linkbudget import (
    RADIO_HORIZON_M_PER_SQRT_M,
    DownlinkBudget,
    PathBudget,
    UplinkBudget,
    direct_budget,
    downlink_budget,
    emitter_boresight,
    transponder_noise_eirp_dbw_hz,
    transponder_outputs,
    uplink_budget,
)
from petal_locate.core.orbits import Orbit, PerturbedOrbit, orbit_position
from petal_locate.core.signals import (
    ChirpWaveform,
    NoiseWaveform,
    PskWaveform,
    Waveform,
    key128,
    waveform_from_spec,
)
from petal_locate.core.timebase import iso_from_ns, ns_from_iso, sec
from petal_locate.core.types import PathSpec
from petal_locate.sim.scenario import EmitterSpec, Scenario, ScenarioError, WaveformSpec

__all__ = [
    "PHASE_FIT_TOL_CYCLES",
    "PathInfo",
    "ResolvedEmitter",
    "SimWorld",
]

PHASE_FIT_TOL_CYCLES: float = 1e-4
"""Largest error of the polynomial fit of a path's carrier phase, cycles (ARCHITECTURE.md
section 4); the same tolerance, expressed as ``1e-4 / f`` seconds, applies to the
envelope delay."""

_FIT_NODES = 12  # Chebyshev nodes per fitted piece (degree 11)
_FIT_MAX_DEPTH = 12  # a window may be halved this many times (4096 pieces)
_NS = 1_000_000_000
_SLOW_NODE_STEP_NS = 10 * _NS  # budget/Doppler node spacing, satellites above 20 000 km
_FAST_NODE_STEP_NS = 1 * _NS  # ... with any satellite below
_SLOW_ORBIT_RADIUS_M = WGS84_A + 20_000e3
_WAVEFORM_CACHE_BLOCKS = 128  # 64 KiB blocks: at most 8 MiB per grid waveform (one long capture plus margin)
_NODE_CACHE = 64  # transponder-state nodes kept
_CHANNEL_NODE_CACHE = 512  # (channel, node) entries kept
_SLICE_CACHE = 64  # in-band slice waveforms kept
_MAX_FINE_SAMPLES = 1 << 26  # m * (n + margins) above this is refused (memory)
_IONO_STENCIL = np.array([-2.0, -1.0, 1.0, 2.0]) * STENCIL_STEP_S
_IONO_WEIGHTS = np.array([1.0, -8.0, 8.0, -1.0]) / (12.0 * STENCIL_STEP_S)
_CHIRP_KERNEL_BINS = 8  # Hann kernel half-width per chirp line, bins (sidelobes below 3e-7 beyond)
_CHIRP_SHAPE_CACHE = 256  # cached chirp PSD shapes
_CHIRP_SHIFT_STEPS = 256  # chirp shapes are cached for baseband centres quantised to bin / 256

PathKind = Literal["relay", "noise", "direct"]
Treatment = Literal["inside", "decimated", "slice", "omitted"]


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, eq=False)
class ResolvedEmitter:
    """One emitter of the simulated world after carrier defaults are applied.

    id, name, kind: as in the scenario (``kind`` is ``"carrier"`` for emitters created
    from a configuration carrier). carrier_id: the configuration carrier realised, or
    None. from_config: True when the emitter was created from a configuration carrier
    that no scenario emitter names.
    lat_deg, lon_deg: geodetic position, degrees. alt_m: ground height above the
    ellipsoid, m. antenna_height_m: antenna above ground, m (sets the radio horizon).
    xyz: antenna phase centre, ECEF m, ``ndarray[3]`` (read-only), at
    ``alt_m + antenna_height_m``.
    freq_hz: RF centre frequency radiated, Hz. eirp_dbw: EIRP on boresight, dBW.
    antenna: antenna model (configuration ``Antenna``). pointing: ``"omni"``,
    ``TrackPointing`` (aimed at a satellite's true position) or ``AzElPointing``.
    waveform_spec: the ``WaveformSpec``. waveform: the ``core.signals.Waveform`` built
    from it, kept for the life of the world. key_label: the label hashed to the
    waveform key, ``emitter|seed|id|waveform.seed``.
    enabled: False means the emitter never radiates. t_start_ns, t_stop_ns: on and off
    times, integer UTC ns, or None (always).
    """

    id: str
    name: str
    kind: str
    carrier_id: str | None
    from_config: bool
    lat_deg: float
    lon_deg: float
    alt_m: float
    antenna_height_m: float
    xyz: npt.NDArray[np.float64]
    freq_hz: float
    eirp_dbw: float
    antenna: Any
    pointing: Any
    waveform_spec: WaveformSpec
    waveform: Waveform
    key_label: str
    enabled: bool
    t_start_ns: int | None
    t_stop_ns: int | None

    @property
    def bandwidth_hz(self) -> float:
        """Occupied bandwidth of the waveform actually synthesised, Hz (0 for cw)."""
        return float(self.waveform.bandwidth_hz)

    def active_at(self, t_ns: int) -> bool:
        """Whether the emitter radiates at ``t_ns`` (enabled, ``t_start <= t < t_stop``)."""
        if not self.enabled:
            return False
        if self.t_start_ns is not None and t_ns < self.t_start_ns:
            return False
        if self.t_stop_ns is not None and t_ns >= self.t_stop_ns:
            return False
        return True

    def active_fraction(self, start_ns: int, duration_s: float) -> float:
        """Fraction of ``[start_ns, start_ns + duration_s)`` during which the emitter is on
        by its enabled flag and on/off times (gating by the waveform duty not included)."""
        if not self.enabled:
            return 0.0
        if duration_s <= 0.0:
            return 1.0 if self.active_at(start_ns) else 0.0
        lo = 0.0 if self.t_start_ns is None else (self.t_start_ns - start_ns) * 1e-9
        hi = duration_s if self.t_stop_ns is None else (self.t_stop_ns - start_ns) * 1e-9
        return min(max(min(hi, duration_s) - max(lo, 0.0), 0.0) / duration_s, 1.0)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready description (for the console's ``/sim/truth``). Simulated truth."""
        pointing = self.pointing
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "carrier_id": self.carrier_id,
            "from_config": self.from_config,
            "simulated": True,
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "alt_m": self.alt_m,
            "antenna_height_m": self.antenna_height_m,
            "xyz_m": [float(v) for v in self.xyz],
            "freq_hz": self.freq_hz,
            "eirp_dbw": self.eirp_dbw,
            "antenna": self.antenna.model_dump(mode="json"),
            "pointing": pointing if isinstance(pointing, str) else pointing.model_dump(mode="json"),
            "waveform": self.waveform_spec.model_dump(mode="json"),
            "bandwidth_hz": self.bandwidth_hz,
            "rms_bandwidth_hz": float(self.waveform.rms_bandwidth_hz),
            "enabled": self.enabled,
            "t_start": None if self.t_start_ns is None else iso_from_ns(self.t_start_ns),
            "t_stop": None if self.t_stop_ns is None else iso_from_ns(self.t_stop_ns),
        }


@dataclass(frozen=True)
class PathInfo:
    """One path into a channel, as ``SimWorld.channel_paths`` reports it (truth).

    kind: ``relay`` (emitter through a transponder), ``noise`` (a transponder's own
    noise) or ``direct`` (terrestrial). emitter_id, sat_id, transponder_id: what the
    path goes through (None where not applicable).
    f_rx_hz: centre frequency arriving at the antenna, Hz (true translation, Doppler).
    f_bb_hz: centre in the capture's baseband, ``f_rx - fc (1 + eps_g)``, Hz.
    bandwidth_hz: occupied bandwidth, Hz. power_ratio_dbhz: ``C_rx / N0_site`` (relay,
    direct) or ``N0_relay B / N0_site`` (noise), dB-Hz; None when the path carries no
    power at this instant. snr_per_sample_db: power the capture receives from the path
    over unit thermal noise per sample at ``fs``, dB; None when omitted. treatment: how
    the receiver channel filter handles it (MATH.md section 4): ``inside`` (rendered at ``fs``),
    ``decimated`` (rendered at ``m fs`` and decimated), ``slice`` (replaced by its
    in-band slice) or ``omitted``. decimation: ``m`` for decimated paths and slices
    (2), 1 for paths rendered at ``fs``, 0 when omitted.
    """

    kind: PathKind
    emitter_id: str | None
    sat_id: str | None
    transponder_id: str | None
    f_rx_hz: float
    f_bb_hz: float
    bandwidth_hz: float
    power_ratio_dbhz: float | None
    snr_per_sample_db: float | None
    treatment: Treatment
    decimation: int


# --------------------------------------------------------------------------- internals


@dataclass(frozen=True)
class _TpState:
    """Transponders at one instant: truth satellite positions and power sharing."""

    t_ns: int
    t_s: float
    sat_r: dict[str, npt.NDArray[np.float64]]
    up: dict[tuple[str, str], UplinkBudget]  # (emitter_id, sat_id), visible legs only
    eirp_c: dict[tuple[str, str, str], float]  # (emitter_id, sat_id, tp_id) -> dBW after robbing
    relay_n0: dict[tuple[str, str], float]  # (sat_id, tp_id) -> EIRP density dBW/Hz after robbing


@dataclass(frozen=True)
class _PathNode:
    """A path into one channel at one budget node (static description plus budget)."""

    key: tuple[str, ...]
    kind: PathKind
    emitter: ResolvedEmitter | None
    sat_id: str | None
    tp_id: str | None
    f_c_hz: float  # carrier in the source frame (uplink frame for relay and noise), Hz
    t_nom_hz: float  # nominal translation (0 for direct)
    t_true_small_hz: float  # T_nom * eps_sat (0 for direct)
    width_hz: float  # occupied bandwidth
    wave_kind: str  # cw / noise / chirp / psk
    waveform: Waveform
    label: str  # key label of the source (slices append to it)
    p_hz: float  # C_rx / N0_site (relay, direct) or N0_relay B / N0_site (noise), Hz
    f_rx_hz: float  # received centre frequency, Hz
    delay_s: float  # envelope delay at the node (true time), s


@dataclass(frozen=True)
class _Plan:
    """How one path is rendered into one capture."""

    node: _PathNode
    treatment: Treatment
    m: int
    f_c_hz: float  # carrier actually rendered (slice centre for slices), source frame
    width_hz: float
    power_fraction: float
    waveform: Waveform


class _Fit:
    """Piecewise Chebyshev interpolant of a path's envelope delay (s) and carrier phase
    (cycles, integer part removed per piece) over labelled time relative to the capture
    start (s). Each piece interpolates the exact functions at 12 Chebyshev nodes and is
    accepted when it matches them at the 13 interleaved extrema to within the
    tolerances; otherwise the piece is halved."""

    def __init__(
        self,
        fn: Callable[[npt.NDArray[np.float64]], tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]],
        t_lo: float,
        t_hi: float,
        delay_tol_s: float,
        phase_tol_cycles: float,
    ) -> None:
        self._fn = fn
        self._delay_tol = delay_tol_s
        self._phase_tol = phase_tol_cycles
        self.breaks: list[float] = []
        self._pieces: list[tuple[float, float, npt.NDArray[np.float64], npt.NDArray[np.float64]]] = []
        k = _FIT_NODES
        self._u_nodes = np.cos(np.pi * (np.arange(k) + 0.5) / k)
        self._u_check = np.cos(np.pi * np.arange(k + 1) / k)
        self.max_delay_error_s = 0.0
        self.max_phase_error_cycles = 0.0
        if t_hi <= t_lo:
            t_hi = t_lo + 1e-9
        self._build(t_lo, t_hi, 0)
        self._pieces.sort(key=lambda p: p[0])
        self.breaks = [p[0] for p in self._pieces] + [self._pieces[-1][1]]

    def _build(self, lo: float, hi: float, depth: int) -> None:
        k = _FIT_NODES
        u = np.concatenate([self._u_nodes, self._u_check])
        t = 0.5 * (lo + hi) + 0.5 * (hi - lo) * u
        env, phase = self._fn(t)
        phase = phase - np.round(phase[0])  # only the fractional part matters
        c_env = _cheb.chebfit(self._u_nodes, env[:k], k - 1)
        c_ph = _cheb.chebfit(self._u_nodes, phase[:k], k - 1)
        err_env = float(np.max(np.abs(_cheb.chebval(self._u_check, c_env) - env[k:])))
        err_ph = float(np.max(np.abs(_cheb.chebval(self._u_check, c_ph) - phase[k:])))
        if err_env <= self._delay_tol and err_ph <= self._phase_tol:
            self._pieces.append((lo, hi, c_env, c_ph))
            self.max_delay_error_s = max(self.max_delay_error_s, err_env)
            self.max_phase_error_cycles = max(self.max_phase_error_cycles, err_ph)
            return
        if depth >= _FIT_MAX_DEPTH:
            raise RuntimeError(
                f"path delay/phase could not be fitted to {self._phase_tol} cycles over "
                f"[{lo}, {hi}] s after {depth} halvings (error {err_ph:.3g} cycles, {err_env:.3g} s)"
            )
        mid = 0.5 * (lo + hi)
        self._build(lo, mid, depth + 1)
        self._build(mid, hi, depth + 1)

    def __call__(self, t: npt.NDArray[np.float64]) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Envelope delay (s) and phase (cycles) at labelled times ``t`` (s, relative)."""
        if len(self._pieces) == 1:
            lo, hi, c_env, c_ph = self._pieces[0]
            u = (2.0 * t - (lo + hi)) / (hi - lo)
            return _cheb.chebval(u, c_env), _cheb.chebval(u, c_ph)
        env = np.empty(t.shape)
        phase = np.empty(t.shape)
        index = np.clip(np.searchsorted(self.breaks, t, side="right") - 1, 0, len(self._pieces) - 1)
        for i, (lo, hi, c_env, c_ph) in enumerate(self._pieces):
            sel = index == i
            if not np.any(sel):
                continue
            u = (2.0 * t[sel] - (lo + hi)) / (hi - lo)
            env[sel] = _cheb.chebval(u, c_env)
            phase[sel] = _cheb.chebval(u, c_ph)
        return env, phase


def _frac_cycles(freq_hz: float, t_ns: int) -> float:
    """``frac(freq_hz * t_ns * 1e-9)`` computed exactly (float frequency, integer ns)."""
    q = Fraction(freq_hz) * t_ns / _NS
    return float(q - math.floor(q))


def _db(x: float) -> float:
    return 10.0 * math.log10(x) if x > 0.0 else -math.inf


def _lin(x_db: float) -> float:
    return 10.0 ** (0.1 * x_db) if math.isfinite(x_db) else 0.0


def _up_vector(xyz: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    lat, lon, _ = ecef_to_lla(xyz)
    la, lo = math.radians(float(lat)), math.radians(float(lon))
    return np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])


def _elevation(xyz: npt.NDArray[np.float64], up: npt.NDArray[np.float64], target: Any) -> Any:
    """Elevation (deg) of ``target`` (``[..., 3]`` ECEF m) from ``xyz`` with vertical ``up``."""
    los = np.asarray(target, dtype=np.float64) - xyz
    sine = np.sum(los * up, axis=-1) / np.linalg.norm(los, axis=-1)
    return np.degrees(np.arcsin(np.clip(sine, -1.0, 1.0)))


def _as_ns(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be integer nanoseconds (int), got {type(value).__name__}")
    return int(value)


def _as_count(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return int(value)


def _as_positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number, got {value!r}") from None
    if not (math.isfinite(out) and out > 0.0):
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return out


def _bin_freqs(nfft: int, span_hz: float) -> npt.NDArray[np.float64]:
    """Offsets from the centre of the fft-shifted Welch bins, Hz (``welch_psd_db`` order)."""
    return np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / span_hz))


def _flat_binned(f: npt.NDArray[np.float64], width: float, res: float) -> npt.NDArray[np.float64]:
    """Bin-averaged density (1/Hz) of a flat spectrum of ``width`` Hz centred on 0."""
    hi = np.clip((f + 0.5 * res) / width + 0.5, 0.0, 1.0)
    lo = np.clip((f - 0.5 * res) / width + 0.5, 0.0, 1.0)
    return (hi - lo) / res


def _chirp_lines(width_hz: float, period_s: float, f_lo_hz: float, f_hi_hz: float
                 ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Fourier lines of the unit-power sawtooth chirp of ``core.signals.ChirpWaveform``.

    The envelope ``exp(j 2 pi (W / 2P) u (u - P))`` (``u`` the time since the start of
    the sweep, period ``P``, sweep width ``W``) is periodic, so its spectrum is a line
    spectrum at ``l / P`` Hz with powers (completing the square, ``u_l = P/2 + l/W``,
    ``x = sqrt(2W/P) (u - u_l)``, Fresnel integrals ``C + jS``)::

        |c_l|^2 = |F(sqrt(2W/P) (P - u_l)) - F(-sqrt(2W/P) u_l)|^2 / (2 W P)

    which sum to 1 (Parseval) and include the Fresnel skirts beyond the sweep edges.
    Returns the frequencies (Hz, relative to the chirp centre) and powers of the lines
    in ``[f_lo_hz, f_hi_hz]``.
    """
    l = np.arange(math.floor(f_lo_hz * period_s), math.ceil(f_hi_hz * period_s) + 1, dtype=np.float64)
    u_l = 0.5 * period_s + l / width_hz
    scale = math.sqrt(2.0 * width_hz / period_s)
    s1, c1 = _fresnel(scale * (period_s - u_l))
    s0, c0 = _fresnel(-scale * u_l)
    power = ((c1 - c0) ** 2 + (s1 - s0) ** 2) / (2.0 * width_hz * period_s)
    return l / period_s, power


def _hann_kernel(d: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Fraction of a tone's power that a Hann-window Welch bin ``d`` bins away receives
    (the cw kernel of ``core.signals``: sums to 1 over bins)."""
    amp = np.sinc(d) + 0.5 * np.sinc(d - 1.0) + 0.5 * np.sinc(d + 1.0)
    return (2.0 / 3.0) * amp * amp


# --------------------------------------------------------------------------- the world


class SimWorld:
    """Truth and signal synthesis for one ``SystemConfig`` and one ``Scenario``.

    config: the system configuration (what the operator believes). scenario: what is
    true (``sim.scenario.Scenario``); it is validated against the configuration and a
    ``ScenarioError`` (a ValueError) naming the field is raised when it does not fit.
    epoch_ns: reference time (integer UTC ns) of ephemeris errors that give a velocity
    offset but no ``t_ref``; when None such an error is a ValueError naming the field
    (an offset that grows from an arbitrary epoch would be meaningless).

    The world is immutable once built; build a new one (``rebuild``) when the scenario
    or configuration changes. Methods may be called from several threads at once.
    Internal caches (per-path budgets and Doppler on a coarse time grid, waveform
    blocks, in-band slices) never change a value, only how fast it is produced.
    """

    def __init__(self, config: SystemConfig, scenario: Scenario, *, epoch_ns: int | None = None,
                 _reuse: SimWorld | None = None) -> None:
        if not isinstance(config, SystemConfig):
            raise ValueError(f"config must be a SystemConfig, got {type(config).__name__}")
        if not isinstance(scenario, Scenario):
            raise ValueError(f"scenario must be a Scenario, got {type(scenario).__name__}")
        scenario.validate_against(config)
        self.config = config
        self.scenario = scenario
        self.epoch_ns = None if epoch_ns is None else _as_ns(epoch_ns, "epoch_ns")
        self._lock = threading.RLock()

        self._sats: dict[str, Satellite] = {s.id: s for s in config.satellites}
        self._tps: dict[tuple[str, str], Transponder] = {
            (s.id, tp.id): tp for s in config.satellites for tp in s.transponders
        }
        self._channels: dict[str, tuple[Site, ReceiveChannel]] = {
            ch.id: (site, ch) for site in config.sites for ch in site.channels
        }
        self._site_xyz: dict[str, npt.NDArray[np.float64]] = {}
        self._site_up: dict[str, npt.NDArray[np.float64]] = {}
        for site in config.sites:
            xyz = np.asarray(config.site_xyz(site.id), dtype=np.float64)
            xyz.flags.writeable = False
            self._site_xyz[site.id] = xyz
            self._site_up[site.id] = _up_vector(xyz)

        errors = scenario.errors
        self._delta_s = {sid: float(v) * 1e-9 for sid, v in errors.site_clock_offset_ns.items()}
        self._eps_site = {sid: float(v) for sid, v in errors.site_osc_frac.items()}
        self._eps_sat = {sid: float(v) for sid, v in errors.sat_osc_frac.items()}
        self._iono_on = bool(errors.iono.enabled)
        self._vtec = float(errors.iono.vtec_tecu)

        self._believed: dict[str, Orbit] = config.orbits()
        self._truth: dict[str, Orbit] = {}
        for sat_id, base in self._believed.items():
            err = errors.ephemeris.get(sat_id)
            if err is None:
                self._truth[sat_id] = base
                continue
            vel = np.asarray(err.ric_vel_mps, dtype=np.float64)
            if err.t_ref is not None:
                t_ref_s = sec(ns_from_iso(err.t_ref))
            elif not np.any(vel != 0.0):
                t_ref_s = T_REF_S  # irrelevant: the offset does not grow
            elif self.epoch_ns is not None:
                t_ref_s = sec(self.epoch_ns)
            else:
                raise ScenarioError(
                    f"errors.ephemeris.{sat_id}.t_ref: required when ric_vel_mps is non-zero "
                    "(or build the SimWorld with epoch_ns)"
                )
            self._truth[sat_id] = PerturbedOrbit(base, err.ric_pos_m, vel, t_ref_s)
        self._node_step_ns = (
            _SLOW_NODE_STEP_NS if all(self._is_slow(s) for s in config.satellites) else _FAST_NODE_STEP_NS
        )

        old_waves: dict[tuple[str, str], Waveform] = {}
        old_relay: dict[str, Waveform] = {}
        if _reuse is not None:
            for e in _reuse._emitters:
                old_waves[(e.key_label, e.waveform_spec.model_dump_json())] = e.waveform
            old_relay = dict(_reuse._relay_waves)
        self._emitters: tuple[ResolvedEmitter, ...] = self._resolve_emitters(old_waves)
        self._by_id: dict[str, ResolvedEmitter] = {e.id: e for e in self._emitters}
        self._relays: dict[str, list[tuple[str, str]]] = {
            e.id: [(sid, tp.id) for sid, sat in self._sats.items() for tp in sat.transponders if tp.relays(e.freq_hz)]
            for e in self._emitters
        }
        self._relay_waves: dict[str, Waveform] = {}
        for (sat_id, tp_id), tp in self._tps.items():
            label = self._relay_label(sat_id, tp_id)
            wave = old_relay.get(label)
            if wave is None or abs(wave.bandwidth_hz - tp.bandwidth_hz) > 0.0:
                wave = NoiseWaveform(key128(label), tp.bandwidth_hz, cache_blocks=_WAVEFORM_CACHE_BLOCKS)
            self._relay_waves[label] = wave
        self._slices: OrderedDict[tuple[str, float, float, float], Waveform] = OrderedDict()
        self._tp_cache: OrderedDict[int, _TpState] = OrderedDict()
        self._chan_cache: OrderedDict[tuple[str, int], dict[tuple[str, ...], _PathNode]] = OrderedDict()
        self._h2_cache: dict[tuple[int, int], npt.NDArray[np.float64]] = {}
        self._chirp_cache: OrderedDict[tuple[float, float, int, float, bool, int], npt.NDArray[np.float64]] = OrderedDict()

    # ------------------------------------------------------------------ construction helpers

    def rebuild(self, config: SystemConfig | None = None, scenario: Scenario | None = None,
                *, epoch_ns: int | None = None) -> SimWorld:
        """A new world for a changed scenario and/or configuration (either may be None =
        unchanged). Waveform objects whose definition is unchanged are carried over with
        their block caches, so a rebuild costs little and renders identical samples."""
        return SimWorld(
            self.config if config is None else config,
            self.scenario if scenario is None else scenario,
            epoch_ns=self.epoch_ns if epoch_ns is None else epoch_ns,
            _reuse=self,
        )

    @staticmethod
    def _is_slow(sat: Satellite) -> bool:
        """Whether the satellite stays above 20 000 km (budgets and Doppler then change
        slowly enough for a 10 s node grid)."""
        orbit = sat.orbit
        if orbit.type == "geo":
            return True
        if orbit.type == "kepler":
            return orbit.a_m * (1.0 - orbit.e) > _SLOW_ORBIT_RADIUS_M
        if orbit.type == "tle":
            revs_per_day = float(orbit.line2[52:63])
            return revs_per_day < 2.0 and revs_per_day > 0.0
        radii = [math.sqrt(sum(c * c for c in s.r_m)) for s in orbit.samples]
        return min(radii) > _SLOW_ORBIT_RADIUS_M

    def _relay_label(self, sat_id: str, tp_id: str) -> str:
        return f"relay|{self.scenario.seed}|{sat_id}|{tp_id}"

    def _build_waveform(self, spec: WaveformSpec, label: str, where: str) -> Waveform:
        """``waveform_from_spec`` with a bounded block cache; ValueError naming ``where``."""
        try:
            wave = waveform_from_spec(spec, label)
        except ValueError as exc:
            raise ScenarioError(f"{where}.waveform: {exc}") from None
        period = wave.gate_period_s or 0.0
        if isinstance(wave, NoiseWaveform):
            return NoiseWaveform(wave.key, wave.bandwidth_hz, duty=wave.duty, period_s=period,
                                 cache_blocks=_WAVEFORM_CACHE_BLOCKS)
        if isinstance(wave, PskWaveform):
            return PskWaveform(wave.key, wave.symbol_rate_hz, wave.order, wave.rolloff, duty=wave.duty,
                               period_s=period, cache_blocks=_WAVEFORM_CACHE_BLOCKS)
        return wave

    def _resolve_emitters(self, old_waves: Mapping[tuple[str, str], Waveform]) -> tuple[ResolvedEmitter, ...]:
        carriers = {c.id: c for c in self.config.carriers}
        named = {e.carrier_id for e in self.scenario.emitters if e.carrier_id is not None}
        out: list[ResolvedEmitter] = []
        for i, spec in enumerate(self.scenario.emitters):
            carrier = carriers.get(spec.carrier_id) if spec.carrier_id is not None else None
            out.append(self._resolve(spec, carrier, f"emitters[{i}] ({spec.id})", old_waves, from_config=False))
        for carrier in self.config.carriers:
            if carrier.id in named or carrier.location is None or carrier.emitter is None:
                continue
            spec = EmitterSpec(id=carrier.id, name=carrier.name, kind="carrier", carrier_id=carrier.id)
            out.append(self._resolve(spec, carrier, f"carriers ({carrier.id})", old_waves, from_config=True))
        ids = [e.id for e in out]
        for e in out:
            if ids.count(e.id) > 1:
                raise ScenarioError(f"emitters: id {e.id!r} is used by more than one emitter of the world")
        return tuple(out)

    def _resolve(self, spec: EmitterSpec, carrier: Carrier | None, where: str,
                 old_waves: Mapping[tuple[str, str], Waveform], from_config: bool) -> ResolvedEmitter:
        loc = carrier.location if carrier is not None else None
        model = carrier.emitter if carrier is not None else None

        def pick(value: Any, fallback: Any, name: str) -> Any:
            if value is not None:
                return value
            if fallback is None:
                raise ScenarioError(f"{where}.{name}: required (the carrier does not provide it)")
            return fallback

        lat = float(pick(spec.lat_deg, loc.lat_deg if loc else None, "lat_deg"))
        lon = float(pick(spec.lon_deg, loc.lon_deg if loc else None, "lon_deg"))
        alt = float(pick(spec.alt_m, loc.alt_m if loc else 0.0, "alt_m"))
        height = float(pick(spec.antenna_height_m, loc.antenna_height_m if loc else 10.0, "antenna_height_m"))
        freq = float(pick(spec.freq_hz, carrier.uplink_center_hz if carrier else None, "freq_hz"))
        eirp = float(pick(spec.eirp_dbw, model.eirp_dbw if model else None, "eirp_dbw"))
        antenna = pick(spec.antenna, model.antenna if model else IsotropicAntenna(), "antenna")
        pointing = pick(spec.pointing, model.pointing if model else "omni", "pointing")
        wspec = spec.waveform
        if wspec is None:
            if model is None:
                raise ScenarioError(f"{where}.waveform: required (the carrier has no emitter model)")
            wspec = WaveformSpec(type=model.waveform, bandwidth_hz=model.bandwidth_hz)
        label = f"emitter|{self.scenario.seed}|{spec.id}|{wspec.seed}"
        wave = old_waves.get((label, wspec.model_dump_json()))
        if wave is None:
            wave = self._build_waveform(wspec, label, where)
        xyz = lla_to_ecef(lat, lon, alt + height)
        xyz = np.asarray(xyz, dtype=np.float64)
        xyz.flags.writeable = False
        return ResolvedEmitter(
            id=spec.id,
            name=spec.name or (carrier.name if carrier is not None else ""),
            kind=spec.kind,
            carrier_id=spec.carrier_id,
            from_config=from_config,
            lat_deg=lat,
            lon_deg=lon,
            alt_m=alt,
            antenna_height_m=height,
            xyz=xyz,
            freq_hz=freq,
            eirp_dbw=eirp,
            antenna=antenna,
            pointing=pointing,
            waveform_spec=wspec,
            waveform=wave,
            key_label=label,
            enabled=spec.enabled,
            t_start_ns=None if spec.t_start is None else ns_from_iso(spec.t_start),
            t_stop_ns=None if spec.t_stop is None else ns_from_iso(spec.t_stop),
        )

    # ------------------------------------------------------------------ public lookups

    @property
    def emitters(self) -> tuple[ResolvedEmitter, ...]:
        """Every emitter of the world (scenario emitters first, then those created from
        configuration carriers), including disabled ones."""
        return self._emitters

    def emitter(self, emitter_id: str) -> ResolvedEmitter:
        """The resolved emitter with this id; ValueError naming it when there is none."""
        try:
            return self._by_id[emitter_id]
        except KeyError:
            raise ValueError(f"emitter_id {emitter_id!r} is not an emitter of this world") from None

    def emitters_at(self, t_ns: int) -> list[ResolvedEmitter]:
        """Emitters radiating at ``t_ns`` (integer UTC ns): enabled and within
        ``[t_start, t_stop)``."""
        t = _as_ns(t_ns, "t_ns")
        return [e for e in self._emitters if e.active_at(t)]

    def truth_orbit(self, sat_id: str) -> Orbit:
        """True orbit of a satellite: the configured orbit displaced by
        ``errors.ephemeris`` (``PerturbedOrbit``), or the configured orbit itself."""
        try:
            return self._truth[sat_id]
        except KeyError:
            raise ValueError(f"sat_id {sat_id!r} is not a satellite of this world") from None

    def truth_orbits(self) -> dict[str, Orbit]:
        """``dict[sat_id, Orbit]`` of every true orbit (a new dict; the orbits are shared)."""
        return dict(self._truth)

    def site_clock_offset_s(self, site_id: str) -> float:
        """``delta_g`` of a site: true time minus labelled time, seconds."""
        self.config.site(site_id)
        return self._delta_s.get(site_id, 0.0)

    def site_osc_frac(self, site_id: str) -> float:
        """Fractional error ``eps_g`` of a site's reference oscillator."""
        self.config.site(site_id)
        return self._eps_site.get(site_id, 0.0)

    def sat_osc_frac(self, sat_id: str) -> float:
        """Fractional error ``eps_sat`` of a satellite's translation oscillator."""
        self.truth_orbit(sat_id)
        return self._eps_sat.get(sat_id, 0.0)

    def true_translation_hz(self, sat_id: str, transponder_id: str) -> float:
        """Translation a transponder really applies, ``T_nom (1 + eps_sat)``, Hz."""
        tp = self._transponder(sat_id, transponder_id)
        return tp.translation_hz + tp.translation_hz * self._eps_sat.get(sat_id, 0.0)

    def truth_summary(self, t_ns: int) -> dict[str, Any]:
        """JSON-ready truth at ``t_ns`` for the console (everything here is simulated):
        emitters with an ``active`` flag, satellites with believed and true positions,
        and the scenario's truth errors."""
        t = _as_ns(t_ns, "t_ns")
        t_s = sec(t)
        sats = []
        for sat_id in self._sats:
            believed = orbit_position(self._believed[sat_id], t_s)
            true = orbit_position(self._truth[sat_id], t_s)
            sats.append({
                "id": sat_id,
                "believed_ecef_m": [float(v) for v in believed],
                "truth_ecef_m": [float(v) for v in true],
                "offset_m": float(np.linalg.norm(true - believed)),
                "sat_osc_frac": self._eps_sat.get(sat_id, 0.0),
            })
        return {
            "simulated": True,
            "scenario": self.scenario.name,
            "description": self.scenario.description,
            "seed": self.scenario.seed,
            "t_ns": str(t),
            "emitters": [dict(e.as_dict(), active=e.active_at(t)) for e in self._emitters],
            "satellites": sats,
            "errors": self.scenario.errors.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------ small helpers

    def _transponder(self, sat_id: str, tp_id: str) -> Transponder:
        tp = self._tps.get((sat_id, tp_id))
        if tp is None:
            if sat_id not in self._sats:
                raise ValueError(f"sat_id {sat_id!r} is not a satellite of this world")
            raise ValueError(f"transponder_id {tp_id!r} is not a transponder of satellite {sat_id!r}")
        return tp

    def _channel(self, channel_id: str) -> tuple[Site, ReceiveChannel]:
        try:
            return self._channels[channel_id]
        except (KeyError, TypeError):
            raise ValueError(f"channel_id {channel_id!r} is not a channel of this world") from None

    def _boresight(self, site: Site, channel: ReceiveChannel, t_s: float) -> npt.NDArray[np.float64]:
        """Channel boresight (unit ECEF) as ``SystemConfig.boresight_ecef``: a tracking
        antenna points at the *believed* position of its satellite."""
        xyz = self._site_xyz[site.id]
        if isinstance(channel.pointing, TrackPointing):
            los = orbit_position(self._believed[channel.pointing.satellite_id], t_s) - xyz
            return los / np.linalg.norm(los)
        return np.asarray(
            azel_to_ecef(site.lat_deg, site.lon_deg, channel.pointing.az_deg, channel.pointing.el_deg),
            dtype=np.float64,
        )

    def _site_n0_dbw_hz(self, channel: ReceiveChannel) -> float:
        return K_DBW + 10.0 * math.log10(channel.system_noise_temp_k)

    def _lru_put(self, cache: OrderedDict[Any, Any], key: Any, value: Any, limit: int) -> None:
        with self._lock:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > limit:
                cache.popitem(last=False)

    def _lru_get(self, cache: OrderedDict[Any, Any], key: Any) -> Any:
        with self._lock:
            hit = cache.get(key)
            if hit is not None:
                cache.move_to_end(key)
            return hit

    # ------------------------------------------------------------------ transponder state

    def _tp_state(self, t_ns: int) -> _TpState:
        """Satellite positions and transponder power sharing at ``t_ns`` (truth).

        Every enabled emitter whose centre lies in a transponder's uplink passband and
        which sees the satellite (elevation > 0) has a downlink EIRP; the power-robbing
        factor of each transponder is set by the emitters active at ``t_ns``.
        """
        t_s = sec(t_ns)
        sat_r = {sid: orbit_position(orbit, t_s) for sid, orbit in self._truth.items()}
        up: dict[tuple[str, str], UplinkBudget] = {}
        for e in self._emitters:
            if not e.enabled or not self._relays[e.id]:
                continue
            boresight = emitter_boresight(e, e.xyz, self._truth, t_s)
            for sat_id in {s for s, _ in self._relays[e.id]}:
                tp0 = self._tps[next((s, t) for s, t in self._relays[e.id] if s == sat_id)]
                budget = uplink_budget(e.eirp_dbw, e.antenna, boresight, e.xyz, sat_r[sat_id], e.freq_hz, tp0)
                if float(budget.elevation_deg) > 0.0:
                    up[(e.id, sat_id)] = budget
        eirp_c: dict[tuple[str, str, str], float] = {}
        relay_n0: dict[tuple[str, str], float] = {}
        for (sat_id, tp_id), tp in self._tps.items():
            relayed = [e for e in self._emitters if (sat_id, tp_id) in self._relays[e.id] and (e.id, sat_id) in up]
            active = [float(up[(e.id, sat_id)].pfd_dbw_m2) for e in relayed if e.active_at(t_ns)]
            out = transponder_outputs(np.asarray(active, dtype=np.float64), tp)
            n0_db = float(out.relayed_n0_eirp_dbw_hz)
            relay_n0[(sat_id, tp_id)] = n0_db
            scale_db = n0_db - transponder_noise_eirp_dbw_hz(tp)
            for e in relayed:
                pfd = float(up[(e.id, sat_id)].pfd_dbw_m2)
                eirp_c[(e.id, sat_id, tp_id)] = tp.eirp_sat_dbw - tp.sfd_dbw_m2 + pfd + scale_db
        return _TpState(t_ns, t_s, sat_r, up, eirp_c, relay_n0)

    def _tp_node(self, k: int) -> _TpState:
        hit = self._lru_get(self._tp_cache, k)
        if hit is None:
            hit = self._tp_state(k * self._node_step_ns)
            self._lru_put(self._tp_cache, k, hit, _NODE_CACHE)
        return hit

    # ------------------------------------------------------------------ channel paths at a node

    def _channel_node(self, channel_id: str, k: int) -> dict[tuple[str, ...], _PathNode]:
        key = (channel_id, k)
        hit = self._lru_get(self._chan_cache, key)
        if hit is None:
            hit = self._compute_channel_node(channel_id, self._tp_node(k))
            self._lru_put(self._chan_cache, key, hit, _CHANNEL_NODE_CACHE)
        return hit

    def _compute_channel_node(self, channel_id: str, state: _TpState) -> dict[tuple[str, ...], _PathNode]:
        """Every path into the channel at the state's instant, with its budget and the
        received centre frequency (truth geometry, true translations)."""
        site, channel = self._channels[channel_id]
        xg = self._site_xyz[site.id]
        up_g = self._site_up[site.id]
        t_s = state.t_s
        boresight = self._boresight(site, channel, t_s)
        n0_site = self._site_n0_dbw_hz(channel)
        paths: dict[tuple[str, ...], _PathNode] = {}
        for sat_id, sat in self._sats.items():
            r = state.sat_r[sat_id]
            if not float(_elevation(xg, up_g, r)) > 0.0:
                continue  # below the site's horizon: nothing from this satellite
            orbit = self._truth[sat_id]
            eps = self._eps_sat.get(sat_id, 0.0)
            # one batch per satellite: entry j is the noise of a transponder (emitter None)
            # or an emitter relayed by it
            entries: list[tuple[Transponder, ResolvedEmitter | None]] = []
            for tp in sat.transponders:
                entries.append((tp, None))
                entries.extend((tp, e) for e in self._emitters if (e.id, sat_id, tp.id) in state.eirp_c)
            t_true = np.array([tp.translation_hz * (1.0 + eps) for tp, _ in entries])
            f_c = np.array([tp.uplink_center_hz if e is None else e.freq_hz for tp, e in entries])
            eirp = np.array([-np.inf if e is None else state.eirp_c[(e.id, sat_id, tp.id)] for tp, e in entries])
            relay_n0 = np.array([state.relay_n0[(sat_id, tp.id)] for tp, _ in entries])
            down = downlink_budget(eirp, relay_n0, f_c + t_true, r, xg, channel, boresight)
            c_rx = np.broadcast_to(np.asarray(down.c_rx_dbw, dtype=np.float64), eirp.shape)
            n0_relay = np.broadcast_to(np.asarray(down.relayed_n0_dbw_hz, dtype=np.float64), eirp.shape)
            # Doppler and delay, batched per transponder group delay (row 0: the site, which
            # only supplies the downlink rate shared by every entry)
            tau_d_dot = np.empty(len(entries))
            tau_u_dot = np.zeros(len(entries))
            delay = np.empty(len(entries))
            for tau_x in sorted({tp.group_delay_s for tp, _ in entries}):
                idx = [j for j, (tp, _) in enumerate(entries) if tp.group_delay_s == tau_x]
                pts = np.stack([xg] + [entries[j][1].xyz for j in idx if entries[j][1] is not None])
                _, d_dot, u_dot = bent_pipe_rates(pts, orbit, xg, t_s, tau_x)
                bp = bent_pipe(pts, orbit, xg, t_s, tau_x)
                row = 1
                for j in idx:
                    tau_d_dot[j] = float(d_dot)
                    if entries[j][1] is None:
                        delay[j] = float(np.asarray(bp.tau_d)) + tau_x
                    else:
                        tau_u_dot[j] = float(np.asarray(u_dot)[row])
                        delay[j] = float(np.asarray(bp.D)[row])
                        row += 1
            f_rx = np.asarray(baseband_frequency(f_c, t_true, 0.0, tau_d_dot, tau_u_dot), dtype=np.float64)
            # the transponder's own noise is generated in the satellite: no uplink Doppler
            noise_rows = np.array([e is None for _, e in entries])
            f_rx[noise_rows] = ((f_c + t_true) * (1.0 - tau_d_dot))[noise_rows]
            for j, (tp, e) in enumerate(entries):
                small = tp.translation_hz * eps
                if e is None:
                    label = self._relay_label(sat_id, tp.id)
                    key: tuple[str, ...] = ("noise", sat_id, tp.id)
                    paths[key] = _PathNode(
                        key=key, kind="noise", emitter=None, sat_id=sat_id, tp_id=tp.id,
                        f_c_hz=tp.uplink_center_hz, t_nom_hz=tp.translation_hz, t_true_small_hz=small,
                        width_hz=tp.bandwidth_hz, wave_kind="noise", waveform=self._relay_waves[label],
                        label=label, p_hz=_lin(float(n0_relay[j]) - n0_site) * tp.bandwidth_hz,
                        f_rx_hz=float(f_rx[j]), delay_s=float(delay[j]),
                    )
                else:
                    key = ("relay", e.id, sat_id, tp.id)
                    paths[key] = _PathNode(
                        key=key, kind="relay", emitter=e, sat_id=sat_id, tp_id=tp.id, f_c_hz=e.freq_hz,
                        t_nom_hz=tp.translation_hz, t_true_small_hz=small, width_hz=e.bandwidth_hz,
                        wave_kind=e.waveform.kind, waveform=e.waveform, label=e.key_label,
                        p_hz=_lin(float(c_rx[j]) - n0_site), f_rx_hz=float(f_rx[j]), delay_s=float(delay[j]),
                    )
        for e in self._emitters:
            if not e.enabled:
                continue
            dist = float(np.linalg.norm(xg - e.xyz))
            if dist >= RADIO_HORIZON_M_PER_SQRT_M * (math.sqrt(e.antenna_height_m) + math.sqrt(site.antenna_height_m)):
                continue
            try:
                db = direct_budget(e.eirp_dbw, e.antenna, emitter_boresight(e, e.xyz, self._truth, t_s), e.xyz,
                                   e.antenna_height_m, xg, site.antenna_height_m, channel, boresight, e.freq_hz)
            except ValueError as exc:
                raise ValueError(f"emitter {e.id!r} and site {site.id!r}: {exc}") from None
            if not db.in_horizon:
                continue
            key = ("direct", e.id)
            paths[key] = _PathNode(
                key=key, kind="direct", emitter=e, sat_id=None, tp_id=None, f_c_hz=e.freq_hz, t_nom_hz=0.0,
                t_true_small_hz=0.0, width_hz=e.bandwidth_hz, wave_kind=e.waveform.kind, waveform=e.waveform,
                label=e.key_label, p_hz=_lin(float(db.c_rx_dbw) - n0_site), f_rx_hz=e.freq_hz, delay_s=dist / C,
            )
        return paths

    # ------------------------------------------------------------------ capture planning

    def _plan(self, node: _PathNode, fc: float, fs: float, eps_g: float, f_rx: float | None = None) -> _Plan:
        """Receiver channel filter rule of MATH.md section 4 for one path and capture
        (``f_rx``: the path's received centre if not the node's own, Hz)."""
        off = (node.f_rx_hz if f_rx is None else f_rx) - fc * (1.0 + eps_g)
        width = node.width_hz
        half = 0.5 * fs
        if abs(off) + 0.5 * width <= half:
            return _Plan(node, "inside", 1, node.f_c_hz, width, 1.0, node.waveform)
        if abs(off) - 0.5 * width >= half:
            return _Plan(node, "omitted", 0, node.f_c_hz, width, 0.0, node.waveform)
        m = max(2, math.ceil(2.0 * (abs(off) + 0.5 * width) / fs - 1e-9))
        if node.wave_kind in ("cw", "chirp") or m <= 4:
            return _Plan(node, "decimated", m, node.f_c_hz, width, 1.0, node.waveform)
        # in-band slice, defined in the source frame from nominal quantities only, so both
        # channels of a pair (same uplink centre fc - T_nom and rate) get the same slice
        fc_src = fc - node.t_nom_hz
        lo = max(node.f_c_hz - 0.5 * width, fc_src - half)
        hi = min(node.f_c_hz + 0.5 * width, fc_src + half)
        if hi <= lo:
            return _Plan(node, "omitted", 0, node.f_c_hz, width, 0.0, node.waveform)
        f_s = 0.5 * (lo + hi)
        w_s = hi - lo
        frac = float(node.waveform.spectral_shape(np.array([f_s - node.f_c_hz]), resolution_hz=w_s)[0] * w_s)
        label = f"{node.label}|{round(fc_src)}|{round(fs)}"
        return _Plan(node, "slice", 2, f_s, w_s, frac, self._slice_wave(label, w_s, node.waveform))

    def _slice_wave(self, label: str, width: float, parent: Waveform) -> Waveform:
        key = (label, width, parent.duty, parent.gate_period_s or 0.0)
        hit = self._lru_get(self._slices, key)
        if hit is None:
            hit = NoiseWaveform(key128(label), width, duty=parent.duty, period_s=parent.gate_period_s or 0.0,
                                cache_blocks=_WAVEFORM_CACHE_BLOCKS)
            self._lru_put(self._slices, key, hit, _SLICE_CACHE)
        return hit

    def _nodes_for(self, channel_id: str, t_lo_ns: int, t_hi_ns: int) -> tuple[list[int], list[dict[tuple[str, ...], _PathNode]]]:
        step = self._node_step_ns
        k0 = t_lo_ns // step
        k1 = -(-t_hi_ns // step)
        ks = list(range(k0, max(k1, k0 + 1) + 1))
        return ks, [self._channel_node(channel_id, k) for k in ks]

    @staticmethod
    def _union(nodes: Sequence[Mapping[tuple[str, ...], _PathNode]]) -> list[tuple[str, ...]]:
        keys: dict[tuple[str, ...], None] = {}
        for node in nodes:
            for key in node:
                keys.setdefault(key, None)
        return list(keys)

    def channel_paths(self, channel_id: str, t_ns: int, fc: float, fs: float) -> list[PathInfo]:
        """Every path into ``channel_id`` at ``t_ns`` (integer UTC ns) and how a capture
        centred on ``fc`` (Hz) at ``fs`` (Hz) treats it. Truth; for display and tests."""
        site, _ = self._channel(channel_id)
        t = _as_ns(t_ns, "t_ns")
        fc = _as_positive(fc, "fc")
        fs = _as_positive(fs, "fs")
        eps_g = self._eps_site.get(site.id, 0.0)
        ks, nodes = self._nodes_for(channel_id, t, t)
        w = (t - ks[0] * self._node_step_ns) / self._node_step_ns
        out: list[PathInfo] = []
        for key in self._union(nodes[:2]):
            a, b = nodes[0].get(key), nodes[1].get(key)
            ref = a if a is not None else b
            assert ref is not None
            node = self._interpolated(a, b, w)
            plan = self._plan(node, fc, fs, eps_g)
            out.append(PathInfo(
                kind=ref.kind,
                emitter_id=None if ref.emitter is None else ref.emitter.id,
                sat_id=ref.sat_id,
                transponder_id=ref.tp_id,
                f_rx_hz=node.f_rx_hz,
                f_bb_hz=node.f_rx_hz - fc * (1.0 + eps_g),
                bandwidth_hz=ref.width_hz,
                power_ratio_dbhz=_db(node.p_hz) if node.p_hz > 0.0 else None,
                snr_per_sample_db=(_db(node.p_hz * plan.power_fraction / fs)
                                   if plan.treatment != "omitted" and node.p_hz > 0.0 else None),
                treatment=plan.treatment,
                decimation=plan.m,
            ))
        return out

    @staticmethod
    def _interpolated(a: _PathNode | None, b: _PathNode | None, w: float) -> _PathNode:
        """Linear interpolation of a path's power and centre frequency between two nodes
        (a path missing at one node has zero power there)."""
        if a is not None and b is not None:
            return _PathNode(**{**a.__dict__, "p_hz": (1.0 - w) * a.p_hz + w * b.p_hz,
                                "f_rx_hz": (1.0 - w) * a.f_rx_hz + w * b.f_rx_hz})
        ref = a if a is not None else b
        assert ref is not None
        scale = (1.0 - w) if a is not None else w
        return _PathNode(**{**ref.__dict__, "p_hz": scale * ref.p_hz})

    # ------------------------------------------------------------------ exact path functions

    def _relay_exact(
        self, node: _PathNode, f_c: float, site_id: str, t_base_s: float, dt_true: npt.NDArray[np.float64]
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """True-time envelope delay (s) and carrier-phase delay term (cycles) of a path,
        for reception at ``t_base_s + dt_true`` (true time).

        relay:  delay = D + iono_u + iono_d;  phase = -(f + T)(tau_d + tau_x - iono_d) - f (tau_u - iono_u)
        noise:  delay = tau_d + iono_d;        phase = -(f + T)(tau_d - iono_d)
        direct: delay = |x_g - x_e| / C;       phase = -f delay
        with ``f`` the carrier in the source frame and ``T`` the true translation.
        """
        xg = self._site_xyz[site_id]
        t_true = node.t_nom_hz + node.t_true_small_hz
        if node.kind == "direct":
            assert node.emitter is not None
            d = float(np.linalg.norm(xg - node.emitter.xyz)) / C
            delay = np.full(dt_true.shape, d)
            return delay, -f_c * delay
        assert node.sat_id is not None and node.tp_id is not None
        orbit = self._truth[node.sat_id]
        tp = self._tps[(node.sat_id, node.tp_id)]
        tau_x = tp.group_delay_s
        f_down = f_c + t_true
        if node.kind == "noise":
            tau_d = np.asarray(downlink_delay(orbit, xg, t_base_s, dt_true), dtype=np.float64)
            iono_d = self._iono_leg(xg, orbit, t_base_s + dt_true, f_down) if self._iono_on else 0.0
            return tau_d + iono_d, -f_down * (tau_d - iono_d)
        assert node.emitter is not None
        bp = bent_pipe(node.emitter.xyz, orbit, xg, t_base_s, tau_x, dt_true)
        tau_d = np.asarray(bp.tau_d, dtype=np.float64)
        tau_u = np.asarray(bp.tau_u, dtype=np.float64)
        if self._iono_on:
            t_abs = t_base_s + dt_true
            iono_d = self._iono_leg(xg, orbit, t_abs, f_down)
            iono_u = self._iono_leg(node.emitter.xyz, orbit, t_abs - tau_d - tau_x, f_c)
        else:
            iono_d = iono_u = 0.0
        delay = np.asarray(bp.D, dtype=np.float64) + iono_u + iono_d
        phase = -f_down * (tau_d + tau_x - iono_d) - f_c * (tau_u - iono_u)
        return delay, phase

    def _iono_leg(self, ground: npt.NDArray[np.float64], orbit: Orbit, t_s: Any, freq: float) -> npt.NDArray[np.float64]:
        """Thin-shell group delay of one leg (s); the elevation is clipped to [0, 90] at
        the instant a satellite sets (the path vanishes there anyway)."""
        up = _up_vector(ground)
        el = _elevation(ground, up, orbit_position(orbit, np.asarray(t_s, dtype=np.float64)))
        return np.asarray(group_delay_s(self._vtec, np.clip(el, 0.0, 90.0), freq), dtype=np.float64)

    # ------------------------------------------------------------------ IQ

    def render_iq(self, channel_id: str, t_start_ns: int, n: int, fs: float, fc: float) -> npt.NDArray[np.complex64]:
        """Complex baseband samples of a channel (MATH.md section 4), complex64[n].

        channel_id: receive channel. t_start_ns: labelled time of the first sample,
        integer UTC ns. n: number of samples. fs: sample rate, Hz. fc: requested centre
        (downlink) frequency, Hz; the site oscillator error makes the true tuning
        ``fc (1 + eps_g)``.

        Thermal noise has unit variance per complex sample (the 0 dB reference of
        ``welch_psd_db``); every path is scaled by ``sqrt(C_rx / (N0_site fs))``.
        """
        site, channel = self._channel(channel_id)
        t0 = _as_ns(t_start_ns, "t_start_ns")
        n = _as_count(n, "n", 0)
        fs = _as_positive(fs, "fs")
        fc = _as_positive(fc, "fc")
        out = np.zeros(n, dtype=np.complex128)
        if n == 0:
            return out.astype(np.complex64)
        duration = n / fs
        delta = self._delta_s.get(site.id, 0.0)
        eps_g = self._eps_site.get(site.id, 0.0)
        step = self._node_step_ns
        ks, nodes = self._nodes_for(channel_id, t0, t0 + math.ceil(duration * 1e9))
        node_rel = np.array([(k * step - t0) * 1e-9 for k in ks])
        # the path's centre frequency and power at the window midpoint decide its treatment
        t_mid = t0 + int(round(0.5 * duration * 1e9))
        i_mid = min(t_mid // step - ks[0], len(ks) - 2)
        w_mid = (t_mid - ks[i_mid] * step) / step
        for key in self._union(nodes):
            p_nodes = np.array([node[key].p_hz if key in node else 0.0 for node in nodes])
            if not np.any(p_nodes > 0.0):
                continue
            a, b = nodes[i_mid].get(key), nodes[i_mid + 1].get(key)
            if a is None and b is None:
                ref = next(node[key] for node in nodes if key in node)
            else:
                ref = self._interpolated(a, b, w_mid)
            if ref.emitter is not None:
                e = ref.emitter
                if e.active_fraction(t0 - math.ceil((ref.delay_s - delta + 1.0) * 1e9), duration + 2.0) <= 0.0:
                    continue
            plan = self._plan(ref, fc, fs, eps_g)
            if plan.treatment == "omitted":
                continue
            out += self._render_path(plan, site.id, t0, n, fs, fc, delta, eps_g, node_rel, p_nodes)
        out += thermal_noise(n, f"thermal|{self.scenario.seed}|{channel_id}|{t0}|{fs}|{fc}")
        return out.astype(np.complex64)

    def _render_path(self, plan: _Plan, site_id: str, t0: int, n: int, fs: float, fc: float, delta: float,
                     eps_g: float, node_rel: npt.NDArray[np.float64], p_nodes: npt.NDArray[np.float64]
                     ) -> npt.NDArray[np.complex128]:
        node = plan.node
        m = plan.m
        margin = decimate_margin(m) if m > 1 else 0
        n_fine = (n + 2 * margin) * m
        if n_fine > _MAX_FINE_SAMPLES:
            raise ValueError(
                f"n: rendering this capture needs {n_fine} samples at {m} x fs for path {node.key}; "
                "use a shorter capture or a wider sample rate"
            )
        t_rel = (np.arange(n_fine, dtype=np.float64) - margin * m) / (m * fs)
        t_base_s = float(t0 // _NS)
        frac_s = (t0 % _NS) * 1e-9
        f_c = plan.f_c_hz

        def exact(t: npt.NDArray[np.float64]) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
            delay, phase = self._relay_exact(node, f_c, site_id, t_base_s, frac_s + t + delta)
            return delay - delta, phase  # envelope delay in labelled time

        f_scale = abs(f_c + node.t_nom_hz) or 1.0
        fit = _Fit(exact, float(t_rel[0]), float(t_rel[-1]), PHASE_FIT_TOL_CYCLES / f_scale, PHASE_FIT_TOL_CYCLES)
        env_delay, phase_geo = fit(t_rel)
        # (f + T - f_tune (1 + eps_g)) t' with t' = t0 + t_rel + delta; the t0 part exactly
        f_off = (f_c + node.t_nom_hz - fc) + node.t_true_small_hz - fc * eps_g
        cycles = phase_geo + f_off * (t_rel + delta) + _frac_cycles(f_off, t0)
        amp = np.sqrt(np.interp(t_rel, node_rel, p_nodes) * (plan.power_fraction / fs))
        emit_rel = t_rel - env_delay
        y = plan.waveform.evaluate(t0, emit_rel)
        y *= amp
        y *= np.exp(2j * np.pi * (cycles - np.rint(cycles)))
        e = node.emitter
        if e is not None and (e.t_start_ns is not None or e.t_stop_ns is not None):
            on = np.ones(n_fine, dtype=bool)
            if e.t_start_ns is not None:
                on &= emit_rel >= (e.t_start_ns - t0) * 1e-9
            if e.t_stop_ns is not None:
                on &= emit_rel < (e.t_stop_ns - t0) * 1e-9
            y *= on
        if m > 1:
            y = decimate(y, m)[margin: margin + n]
        return y

    # ------------------------------------------------------------------ PSD

    def render_psd(self, channel_id: str, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int,
                   *, expected: bool = False) -> npt.NDArray[np.float32]:
        """Expected Welch PSD of a channel without synthesising IQ, float32[nfft] dB.

        The units and bin order of ``welch_psd_db`` of ``render_iq(channel_id,
        t_start_ns, nfft * navg, span_hz, fc)``: linear power per bin is 1 (thermal) plus,
        for every path, ``(P / (N0_site span)) nfft S[k]`` with ``S`` its spectral shape
        (``Waveform.spectral_shape`` binned at ``span / nfft``, sum 1) centred on its
        baseband frequency, times the decimation filter's response for decimated paths
        and slices, times the fraction of the snippet the emitter is on (gate duty and
        on/off times); then every bin is multiplied by a ``Gamma(navg, 1/navg)`` draw
        keyed by the scenario seed, channel, centre and ``t_start_ns`` (the scatter of an
        ``navg``-segment Welch average). ``expected=True`` leaves that draw out and
        returns the expectation itself (for planning displays and tests).
        """
        site, _ = self._channel(channel_id)
        t0 = _as_ns(t_start_ns, "t_start_ns")
        fc = _as_positive(fc, "fc")
        span = _as_positive(span_hz, "span_hz")
        nfft = _as_count(nfft, "nfft", 2)
        navg = _as_count(navg, "navg", 1)
        eps_g = self._eps_site.get(site.id, 0.0)
        delta = self._delta_s.get(site.id, 0.0)
        duration = nfft * navg / span
        t_mid = t0 + int(round(0.5 * duration * 1e9))
        step = self._node_step_ns
        k = t_mid // step
        w = (t_mid - k * step) / step
        n_a, n_b = self._channel_node(channel_id, k), self._channel_node(channel_id, k + 1)
        res = span / nfft
        bins = _bin_freqs(nfft, span)
        lin = np.ones(nfft, dtype=np.float64)
        tune = fc * (1.0 + eps_g)
        for key in self._union((n_a, n_b)):
            a, b = n_a.get(key), n_b.get(key)
            node = a if a is not None else b
            assert node is not None
            p_hz = (1.0 - w) * (a.p_hz if a is not None else 0.0) + w * (b.p_hz if b is not None else 0.0)
            f_rx = (1.0 - w) * a.f_rx_hz + w * b.f_rx_hz if a is not None and b is not None else node.f_rx_hz
            if p_hz <= 0.0 or abs(f_rx - tune) - 0.5 * node.width_hz >= 0.5 * span:
                continue  # silent, or wholly outside the window
            plan = self._plan(node, fc, span, eps_g, f_rx)
            if plan.treatment == "omitted":
                continue
            weight = 1.0
            emit_start = t0 + int(round((delta - node.delay_s) * 1e9))  # emission time of the first sample
            if node.emitter is not None:
                weight = node.emitter.active_fraction(emit_start, duration)
                if weight <= 0.0:
                    continue
            weight *= plan.waveform.gate_fraction(emit_start, duration)
            f_bb = f_rx + (plan.f_c_hz - node.f_c_hz) - tune
            if plan.treatment == "slice":
                shape = _flat_binned(bins - f_bb, plan.width_hz, res) * res
            elif isinstance(plan.waveform, ChirpWaveform):
                shape = self._chirp_shape(plan.waveform, f_bb, nfft, span, fold=plan.m == 1)
            else:
                shape = plan.waveform.spectral_shape(bins - f_bb, resolution_hz=res) * res
            if plan.m > 1:
                shape = shape * self._decimation_response(plan.m, nfft)
            lin += (p_hz * plan.power_fraction * weight / span) * nfft * shape
        if not expected:
            rng = np.random.Generator(np.random.Philox(
                key=key128(f"psd|{self.scenario.seed}|{channel_id}|{round(fc)}|{t0}")))
            lin *= rng.gamma(float(navg), 1.0 / navg, nfft)
        return (10.0 * np.log10(np.maximum(lin, 1e-30))).astype(np.float32)

    def _chirp_shape(self, wave: ChirpWaveform, f_bb: float, nfft: int, span: float, fold: bool
                     ) -> npt.NDArray[np.float64]:
        """Expected Welch bin fractions (fft-shifted, ``[nfft]``) of a chirp centred on
        ``f_bb`` Hz in a window of ``span`` Hz: its Fourier lines (``_chirp_lines``, with
        the Fresnel skirts beyond the sweep edges) each spread over the bins by the Hann
        kernel (circular, as the DFT is). ``fold``: the path is rendered directly at
        ``span`` (channel filter ``inside``), so lines beyond ``+/- span / 2`` alias into
        the window exactly as they do in ``render_iq``; otherwise (decimated) they are
        removed by the channel filter. Shapes are cached for ``f_bb`` quantised to
        ``bin / 256``."""
        res = span / nfft
        q = int(round(f_bb * _CHIRP_SHIFT_STEPS / res))
        key = (float(wave.bandwidth_hz), float(wave.sweep_period_s), nfft, float(span), fold, q)
        hit = self._lru_get(self._chirp_cache, key)
        if hit is not None:
            return hit
        f0 = q * res / _CHIRP_SHIFT_STEPS
        width, period, half = float(wave.bandwidth_hz), float(wave.sweep_period_s), 0.5 * span
        if fold:  # the skirt beyond one more span on each side carries < 1e-4 of the power
            f_l, power = _chirp_lines(width, period, -0.5 * width - span, 0.5 * width + span)
            x = ((f0 + f_l + half) % span - half) / res
        else:
            f_l, power = _chirp_lines(width, period, -half - f0, half - f0)
            x = (f0 + f_l) / res
        x = x + nfft // 2  # fractional index into the fft-shifted bins
        centre = np.rint(x)
        offsets = np.arange(-_CHIRP_KERNEL_BINS, _CHIRP_KERNEL_BINS + 1, dtype=np.float64)
        idx = centre[:, None] + offsets[None, :]
        weights = power[:, None] * _hann_kernel(idx - x[:, None])
        shape = np.bincount((idx.astype(np.int64) % nfft).ravel(), weights=weights.ravel(), minlength=nfft)
        shape.flags.writeable = False
        self._lru_put(self._chirp_cache, key, shape, _CHIRP_SHAPE_CACHE)
        return shape

    def _decimation_response(self, m: int, nfft: int) -> npt.NDArray[np.float64]:
        """``|H(f)|^2`` of the decimate-by-m channel filter at the Welch bins (any span)."""
        key = (m, nfft)
        hit = self._h2_cache.get(key)
        if hit is None:
            taps = decimation_fir(m)
            nu = np.fft.fftshift(np.fft.fftfreq(nfft)) / m  # cycles per input sample
            j = np.arange(taps.size) - (taps.size - 1) / 2.0
            h = np.cos(2.0 * np.pi * nu[:, None] * j[None, :]) @ taps  # symmetric taps: real response
            hit = h * h
            with self._lock:
                self._h2_cache[key] = hit
        return hit

    # ------------------------------------------------------------------ budgets

    def path_budget(self, emitter_id: str, sat_id: str, transponder_id: str, channel_id: str, t_s: float
                    ) -> PathBudget | None:
        """Budget of emitter -> transponder -> channel at Unix seconds ``t_s`` (truth).

        Uses the true satellite position, the true translation for the downlink
        frequency, the channel's boresight toward the believed satellite position, and
        power robbing by the emitters active at ``t_s``. Returns None when the path does
        not exist: the emitter is disabled or off at ``t_s``, its centre frequency is
        outside the transponder's uplink passband, or the satellite is below the
        emitter's or the site's horizon.
        """
        e = self.emitter(emitter_id)
        tp = self._transponder(sat_id, transponder_id)
        site, channel = self._channel(channel_id)
        try:
            t = float(t_s)
        except (TypeError, ValueError):
            raise ValueError(f"t_s must be Unix seconds (float), got {t_s!r}") from None
        if not math.isfinite(t) or abs(t) > 1e11:
            raise ValueError(f"t_s must be finite Unix seconds, got {t_s!r}")
        t_ns = int(round(t * 1e9))
        if not e.active_at(t_ns) or not tp.relays(e.freq_hz):
            return None
        state = self._tp_state(t_ns)
        up = state.up.get((e.id, sat_id))
        if up is None:
            return None
        xg = self._site_xyz[site.id]
        r = state.sat_r[sat_id]
        if not float(_elevation(xg, self._site_up[site.id], r)) > 0.0:
            return None
        eirp_c = state.eirp_c[(e.id, sat_id, transponder_id)]
        relay_n0 = state.relay_n0[(sat_id, transponder_id)]
        f_d = e.freq_hz + self.true_translation_hz(sat_id, transponder_id)
        down: DownlinkBudget = downlink_budget(eirp_c, relay_n0, f_d, r, xg, channel,
                                               self._boresight(site, channel, state.t_s))
        uplink = uplink_budget(e.eirp_dbw, e.antenna, emitter_boresight(e, e.xyz, self._truth, state.t_s),
                               e.xyz, r, e.freq_hz, tp)
        return PathBudget(
            uplink=uplink,
            transponder_eirp_dbw=eirp_c,
            downlink=down,
            cn0_total_dbhz=float(down.c_rx_dbw) - float(down.n0_total_dbw_hz),
        )

    def direct_power_ratio_dbhz(self, emitter_id: str, channel_id: str, t_s: float) -> float | None:
        """``C_rx / N0_site`` (dB-Hz) of the direct terrestrial path from an emitter into a
        channel, or None when the emitter is outside the site's radio horizon or not
        radiating at ``t_s``."""
        e = self.emitter(emitter_id)
        site, channel = self._channel(channel_id)
        t_ns = int(round(float(t_s) * 1e9))
        if not e.active_at(t_ns):
            return None
        xg = self._site_xyz[site.id]
        db = direct_budget(e.eirp_dbw, e.antenna, emitter_boresight(e, e.xyz, self._truth, float(t_s)), e.xyz,
                           e.antenna_height_m, xg, site.antenna_height_m, channel,
                           self._boresight(site, channel, float(t_s)), e.freq_hz)
        if not db.in_horizon:
            return None
        return float(db.c_rx_dbw) - self._site_n0_dbw_hz(channel)

    # ------------------------------------------------------------------ truth measurements

    def _measure_path(self, e: ResolvedEmitter, path: PathSpec, name: str) -> tuple[_PathNode, Transponder]:
        """The relay path of ``e`` that a ``PathSpec`` names (its satellite and site; the
        transponder relaying the emitter, preferring the PathSpec's translation)."""
        if path.sat_id not in self._sats:
            raise ValueError(f"{name}.sat_id {path.sat_id!r} is not a satellite of this world")
        self.config.site(path.site_id)
        candidates = [self._tps[(path.sat_id, t)] for s, t in self._relays[e.id] if s == path.sat_id]
        if not candidates:
            raise ValueError(
                f"{name}.sat_id: satellite {path.sat_id!r} has no transponder relaying emitter {e.id!r} "
                f"at {e.freq_hz:.0f} Hz"
            )
        tp = next((c for c in candidates if c.translation_hz == path.translation_hz), candidates[0])
        small = tp.translation_hz * self._eps_sat.get(path.sat_id, 0.0)
        node = _PathNode(
            key=("relay", e.id, path.sat_id, tp.id), kind="relay", emitter=e, sat_id=path.sat_id, tp_id=tp.id,
            f_c_hz=e.freq_hz, t_nom_hz=tp.translation_hz, t_true_small_hz=small, width_hz=e.bandwidth_hz,
            wave_kind=e.waveform.kind, waveform=e.waveform, label=e.key_label, p_hz=0.0, f_rx_hz=0.0, delay_s=0.0,
        )
        return node, tp

    def _labelled_delay(self, node: _PathNode, site_id: str, t_base_s: float, offset_s: float) -> float:
        delta = self._delta_s.get(site_id, 0.0)
        delay, _ = self._relay_exact(node, node.f_c_hz, site_id, t_base_s, np.array([offset_s + delta]))
        return float(delay[0]) - delta

    def _labelled_fbb(self, node: _PathNode, tp: Transponder, path: PathSpec, t_base_s: float, offset_s: float) -> float:
        """Baseband frequency (Hz) of a path at labelled time ``t_base_s + offset_s`` in a
        channel tuned to ``path.f_tune_hz`` (MATH.md section 4), with the ionospheric
        phase-advance rate when the ionosphere is on."""
        site_id = path.site_id
        delta = self._delta_s.get(site_id, 0.0)
        eps_g = self._eps_site.get(site_id, 0.0)
        e = node.emitter
        assert e is not None and node.sat_id is not None
        xg = self._site_xyz[site_id]
        orbit = self._truth[node.sat_id]
        t_true = node.t_nom_hz + node.t_true_small_hz
        dt = offset_s + delta
        _, tau_d_dot, tau_u_dot = bent_pipe_rates(e.xyz, orbit, xg, t_base_s, tp.group_delay_s, dt)
        f_bb = float(baseband_frequency(e.freq_hz, t_true, path.f_tune_hz, tau_d_dot, tau_u_dot, eps_g))
        if self._iono_on:
            dts = dt + _IONO_STENCIL
            bp = bent_pipe(e.xyz, orbit, xg, t_base_s, tp.group_delay_s, dts)
            t_abs = t_base_s + dts
            iono_d = self._iono_leg(xg, orbit, t_abs, e.freq_hz + t_true)
            iono_u = self._iono_leg(e.xyz, orbit, t_abs - np.asarray(bp.tau_d) - tp.group_delay_s, e.freq_hz)
            f_bb += float((e.freq_hz + t_true) * (iono_d @ _IONO_WEIGHTS) + e.freq_hz * (iono_u @ _IONO_WEIGHTS))
        return f_bb

    def truth_measurement(self, emitter_id: str, a: PathSpec, b: PathSpec, t_ns: int) -> tuple[float, float]:
        """True TDOA (s) and FDOA (Hz) of channel A minus channel B (MATH.md section 5).

        a, b: the two paths (satellite, site, and the capture tuning ``f_tune_hz =
        f_e_hz + translation_hz``). t_ns: ``t_mid``, the labelled time of the midpoint
        of the channel-B samples, integer UTC ns. All truth errors apply: true orbits,
        site clock offsets (``TDOA = (D_A - delta_a) - (D_B - delta_b)`` in labelled
        time), site and satellite oscillators, ionosphere. The emitter radiates its own
        ``freq_hz``; the PathSpecs' ``f_e_hz`` only set the tuning.

            TDOA = D'_A(t_mid + TDOA) - D'_B(t_mid)      (solved to convergence)
            FDOA = f_bb,A(t_mid + TDOA) - f_bb,B(t_mid)

        with ``D'`` the envelope delay in labelled time. This is what ``caf_estimate``
        measures from ``render_iq`` captures of the two channels.
        """
        e = self.emitter(emitter_id)
        t = _as_ns(t_ns, "t_ns")
        node_a, tp_a = self._measure_path(e, a, "a")
        node_b, tp_b = self._measure_path(e, b, "b")
        t_base = float(t // _NS)
        frac = (t % _NS) * 1e-9
        d_b = self._labelled_delay(node_b, b.site_id, t_base, frac)
        tdoa = self._labelled_delay(node_a, a.site_id, t_base, frac) - d_b
        for _ in range(4):
            tdoa = self._labelled_delay(node_a, a.site_id, t_base, frac + tdoa) - d_b
        f_a = self._labelled_fbb(node_a, tp_a, a, t_base, frac + tdoa)
        f_b = self._labelled_fbb(node_b, tp_b, b, t_base, frac)
        return float(tdoa), float(f_a - f_b)
