"""Capture tasking, correlation, calibration and solving (ARCHITECTURE.md section 7).

For every active event whose verdict is ``uplink`` or ``undetermined`` and whose range
lies in a transponder passband (``EventBook.taskable``), ``Tasking`` runs one *round*
every ``capture.interval_s``:

1. **Plan.** The primary channel is the event's detection with the highest SNR
   (``Correlator.sightings``; the previous primary stays while within
   ``PRIMARY_HOLD_DB`` of it); for every other satellite whose transponder overlaps
   the uplink range, the channel watching it with the highest ``downlink_quality_db``
   is chosen, preferring a channel at the primary's site within 3 dB (common clock).
   A channel must be online, acknowledged (``hub.channel_capabilities``), able to
   tune the centre frequency, and not excluded by an earlier non-retryable failure.
   ``f_e_nom = round((f_lo + f_hi) / 2)``; every capture of the round has the same
   rate ``min(max(min_sample_rate_hz, oversample * (f_hi - f_lo), Doppler need),
   lowest usable maximum)`` and the same first-sample time
   ``now + max(lead_s, min(3 x worst heartbeat round trip, MAX_LEAD_S))``;
   ``center_hz = f_e_nom + translation_hz``; ``n = min(round(duration_s * fs),
   max_samples)``, lengthened when a delay window needs it;
   ``deadline = t_start + (n / fs + timeout_s)``.
2. **Calibration plan** (``calibration.enabled``). A pair not calibrated for
   ``calibration.interval_s`` also captures a reference carrier relayed by the same
   two transponders, centred on the carrier's own downlink, in a later time slot of
   the same channels (one radio, one tuning at a time).
3. **Capture** everything of the round concurrently through ``hub.capture``.
   Retryable ``CaptureFailed`` codes are retried at the next round; non-retryable
   ones (``NON_RETRYABLE_CODES``) are recorded as the reason of the pair's rows and
   exclude that channel for this event.
4. **Calibrate.** ``core.solver.calibration_correction`` turns each reference
   measurement into a correction keyed by ``(a.sat_id, a.site_id, b.sat_id,
   b.site_id, kind)`` and reference carrier (sign-flipped for the reversed pair; events
   on different reference carriers through the same channels each keep theirs and
   are scheduled separately, every ``calibration.interval_s``), valid for
   ``calibration.max_age_s``, each with its own ``cal_id``; stored in the store.
   All are dropped when the configuration changes (``reconfigure``) or the
   simulated world is replaced (``world_changed``).
5. **Correlate** each pair (``core.caf.caf_estimate`` in a worker thread): windows
   from ``measurement_windows`` + ``search_windows`` (shifted by a valid
   calibration), self-peaks excluded for every satellite heard by both channels (and
   the direct-path point when both channels are at one site with equal
   translations), a mirror window when the two satellites are co-channel,
   ``t0_offset_s`` from the integer difference of the first-sample times,
   centre-frequency differences corrected per MATH.md section 5. A pair whose
   returned rate differs from the request is rejected; an unlocked clock drops the
   TDOA unless both channels are at one site (its FDOA is used). CAF results that
   are not detected, ambiguous, coupled, outside the window or mirror-ambiguous are
   recorded with their reason and never solved.
6. **Confirm.** The estimator has about 2 % false alarms per capture at the default
   threshold in a large window, so a pair's rows enter the solver (and the
   cross-satellite detection is reported to the verdict) only once a detection is
   repeated at a consistent TDOA/FDOA: within ``CONFIRM_SIGMAS`` combined sigmas plus
   the largest change the satellites' motion allows over the interval. Until then
   the rows carry ``REASON_UNCONFIRMED``.
7. **Classify** the primary capture (``core.detect.classify`` in a worker thread)
   over the event's own band (``CLASSIFY_BAND_FACTOR`` times its width, so that a
   neighbouring carrier inside a minimum-rate capture is not classified in its place);
   a swept waveform is captured again over at least ``SWEEP_PERIODS`` sweep periods.
8. **Solve** the newest ``solver.max_measurements`` accepted rows (worker thread):
   the global search on the first solve, every ``GLOBAL_SEARCH_EVERY``-th solve and
   whenever a warm-started solve fails or has ``reduced_chi2 > WARM_RCHI2_LIMIT``;
   warm starts from the previous solution and its ambiguities. The result goes to
   ``EventBook.set_solution`` (solution history, residuals, ``located``); the
   strongest correlation surface of the round to ``set_caf``; the class to
   ``set_classification``. The event moves ``characterizing`` (first capture
   requested) -> ``locating`` (first row used) -> ``located``.

Everything runs on the event loop except the numerical jobs, which go through the
server's ``WorkPool`` (at most 3 at once). Rounds are cancelled when their event is
closed (``EventBook`` listener), when the configuration is replaced and on ``stop``;
a node that disconnects mid-round only costs that round's pairs. Absolute times are
integer UTC nanoseconds throughout; float seconds only for geometry.

Deviations from the documents:

- Search windows of a kind without a valid calibration are also widened by a
  3-sigma ephemeris allowance (``2 |sigma_pos| / c`` per satellite in delay,
  ``2 f |sigma_vel| / c`` in Doppler): MATH.md section 6 lists only clocks, bias and
  oscillators, so a reference carrier (a single point) would otherwise be searched in
  a window narrower than the ephemeris error.
- The sample rate also covers the Doppler window (``fs >= 2.2 max |doppler|``,
  required by ``caf_estimate``; matters in low orbit); the base duration is
  shortened when bounded dynamics would break the CAF's drift assumption, and the
  capture is lengthened when a delay window (plus the estimator's guard) needs it.
- "Consistent within a few sigma" for the repeat detection adds the largest change
  of TDOA/FDOA the satellites' speed and acceleration allow over the interval (a GEO
  pair's TDOA moves by microseconds in 5 s, against sigmas of a fraction of one).
- A reference carrier whose band overlaps the event's uplink range is not used.
- A partner channel with no admissible emitter position for the pair is not captured.
- Partner diversity: from the third round on, every other round replaces a partner by
  another candidate within ``ROTATE_WITHIN_DB`` of the best quality, in turn
  (``Tasking._rotate``). Downlink qualities of a network's channels typically differ by
  fractions of a dB, so the documented choice alone always used the same sites; a
  fault at an unused site (a late clock) was invisible and nothing averaged over sites.
- The status message explains a round whose pairs all failed for one reason of the
  signal itself (``round_explanation``: no TDOA on a narrowband signal, ambiguous
  correlations of a periodic waveform, coupling, no copy found); an event judged
  ``local`` gets an ``idle`` status saying there is nothing to locate by satellite.

Known limits (measured against ``SimWorld``): a CW has no delay to separate copies by,
so when a channel hears a second copy of the tone (a co-channel satellite, or a
self-peak ridge) a few Doppler bins away, the FDOA scatters up to about 5 reported
sigma; the worker threads' pure-Python stretches can delay the loop by 50 to 100 ms
through the GIL although no step of tasking holds the loop for more than about 10 ms.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import numpy as np
from pydantic import ValidationError

from petal_locate.core.caf import (
    REASON_AMBIGUOUS,
    REASON_BELOW_THRESHOLD,
    REASON_COUPLED,
    REASON_NARROWBAND,
    REASON_OUTSIDE,
    CafResult,
    caf_estimate,
)
from petal_locate.core.constants import C
from petal_locate.core.detect import MIN_CLASSIFY_SAMPLES, classify
from petal_locate.core.geometry import elevation_deg, measurement_windows, predict_pair, search_windows, self_peak
from petal_locate.core.solver import Solution, calibration_correction, solve
from petal_locate.core.types import CalRef, Measurement, PathSpec, Uncertainty
from petal_locate.server.api import schemas
from petal_locate.server.hub import CaptureFailed
from petal_locate.server.pipeline.correlator import QualityTable
from petal_locate.server.pipeline.events import (
    EventBook,
    EventNotFound,
    EventRecord,
    TruthEmitter,
    Unit,
    caf_display,
)

__all__ = [
    "CONFIRM_SIGMAS",
    "GLOBAL_SEARCH_EVERY",
    "MAX_LEAD_S",
    "NON_RETRYABLE_CODES",
    "REASONS",
    "REASON_AMBIGUOUS_CAF",
    "REASON_CAPTURE_FAILED",
    "REASON_CLOCK_UNLOCKED",
    "REASON_COUPLED_CAF",
    "REASON_INCONSISTENT",
    "REASON_MIRROR",
    "REASON_NARROWBAND_CAF",
    "REASON_NOT_DETECTED",
    "REASON_OUTSIDE_WINDOW",
    "REASON_OVERFLOW",
    "REASON_RATE_MISMATCH",
    "REASON_UNCONFIRMED",
    "REASON_WINDOW",
    "SWEEP_PERIODS",
    "WARM_RCHI2_LIMIT",
    "Calibration",
    "CaptureHub",
    "PairOutcome",
    "RoundReport",
    "Tasking",
    "truth_emitters",
]

logger = logging.getLogger(__name__)

_NS: Final[int] = 1_000_000_000

# ----------------------------------------------------------------------------- tunables

MAX_LEAD_S: Final[float] = 4.0
"""Cap on the round-trip part of the lead time, s. A node's heartbeat round trip
includes time queued behind sample uploads (174 ms on loopback during a 64 MiB upload,
seconds on a slow uplink), so ``3 x RTT`` is capped here; a request that arrives late
is served from the driver's lookback or fails ``too_late`` (retried)."""

NON_RETRYABLE_CODES: Final[frozenset[str]] = frozenset(
    {"unknown_channel", "too_large", "out_of_range", "invalid_request"}
)
"""``CaptureFailed`` codes that will not succeed when tried again (``invalid_request``
is tasking's own, for a request the hub refused with ValueError); every other code,
including codes a newer node adds, is retried at the next round."""

CONFIRM_SIGMAS: Final[float] = 5.0
"""A repeat detection is consistent within this many combined sigmas (plus drift)."""

RATE_SAFETY: Final[float] = 1.5
"""Factor on the satellite-motion bounds of TDOA / FDOA change (confirmation, drift)."""

GLOBAL_SEARCH_EVERY: Final[int] = 10
"""Every this-many-th solve of an event runs the global search (MATH.md section 9)."""

WARM_RCHI2_LIMIT: Final[float] = 3.0
"""A warm-started solve with a larger reduced chi-square is redone with the global search."""

SWEEP_PERIODS: Final[int] = 5
"""A swept waveform is captured again over at least this many sweep periods."""

CLASSIFY_BAND_FACTOR: Final[float] = 4.0
"""The primary capture is classified over this many times the event's width, centred on
the event (at least ``2 x MIN_CLASSIFY_SAMPLES`` samples, at most the whole capture)."""

PREFER_SAME_SITE_DB: Final[float] = 3.0

PRIMARY_HOLD_DB: Final[float] = 3.0
"""The previous round's primary channel stays primary while its sighting is within this
of the strongest, dB (``Tasking._choose_channels``)."""

ROTATE_WITHIN_DB: Final[float] = 1.0
"""Partners whose downlink quality is within this of the best take turns in the odd
rounds from the third on (``Tasking._rotate``), dB."""
"""A channel at the primary's site is preferred when within this of the best quality, dB."""

RECLASSIFY_S: Final[float] = 60.0
"""A classified event is classified again after this long (server time), s."""

RECLASSIFY_UNKNOWN_S: Final[float] = 15.0
"""An event classified ``unknown`` is tried again after this long, s."""

QUALITY_REFRESH_S: Final[float] = 30.0
"""Age at which the downlink-quality table used for channel choice is rebuilt, s."""

SLOT_GAP_S: Final[float] = 0.05
"""Gap between consecutive capture slots of one round (target, then references), s."""

TICK_S: Final[float] = 0.5
"""Period of the scheduling loop, s."""

MAX_CONCURRENT_ROUNDS: Final[int] = 4
"""Rounds (of different events) in flight at once."""

_GUARD_LAGS: Final[int] = 24  # core.caf searches 24 lags + 8 main lobes beyond each window edge
_DOPPLER_FS_FACTOR: Final[float] = 2.2  # fs must exceed this times the largest |Doppler| searched
_MIN_SIGNAL_BW_HZ: Final[float] = 10.0
_MIN_SAMPLES: Final[int] = MIN_CLASSIFY_SAMPLES  # classify() needs this many
_TDOA_FLOOR_S: Final[float] = 1e-7  # smallest confirmation gate, s
_FDOA_FLOOR_HZ: Final[float] = 1e-3  # smallest confirmation gate, Hz
_CANDIDATES_KEPT: Final[int] = 3  # unconfirmed detections remembered per pair
_RATE_RTOL: Final[float] = 1e-9  # returned sample rate must equal the request to this
_SAVE_TIMEOUT_S: Final[float] = 5.0

# ----------------------------------------------------------------------------- reasons

REASON_NOT_DETECTED: Final[str] = "not_detected"
"""No correlation peak reached ``caf.min_snr_db`` in the window."""
REASON_AMBIGUOUS_CAF: Final[str] = "ambiguous"
"""Several peaks within 3 dB in the window (for example a periodic sweep)."""
REASON_COUPLED_CAF: Final[str] = "delay_doppler_coupled"
"""Delay and Doppler errors correlated by more than 0.9 (for example a single sweep)."""
REASON_NARROWBAND_CAF: Final[str] = "narrowband"
"""The signal is too narrow for a time difference (CW): FDOA only."""
REASON_OUTSIDE_WINDOW: Final[str] = "outside_window"
"""The only strong peak lies just outside the admissible window."""
REASON_MIRROR: Final[str] = "mirror_ambiguous"
"""The swapped co-channel pairing gives a peak as strong: which copy is which is unknown."""
REASON_CLOCK_UNLOCKED: Final[str] = "clock_unlocked"
"""A capture's clock was not disciplined and the channels are at different sites: no TDOA."""
REASON_RATE_MISMATCH: Final[str] = "rate_mismatch"
"""A node returned a sample rate other than the one requested."""
REASON_OVERFLOW: Final[str] = "overflow"
"""A node lost samples during the capture: its timing is void."""
REASON_UNCONFIRMED: Final[str] = "unconfirmed: awaiting a repeat detection at a consistent TDOA/FDOA"
"""First detection of this pair (or of a new track): not solved until repeated."""
REASON_INCONSISTENT: Final[str] = "inconsistent with this pair's earlier detections"
"""A detection that disagrees with the pair's confirmed track (probably a false alarm)."""
REASON_WINDOW: Final[str] = "window_exceeds_capture"
"""The correlator refused the windows (a delay window that does not fit in half the
capture: raise ``capture.max_samples``); the detail follows after a colon."""
REASON_CAPTURE_FAILED: Final[str] = "capture_failed"
"""Prefix of the reason of a non-retryable capture failure (``capture_failed: <code>: ...``)."""

REASONS: Final[dict[str, str]] = {
    REASON_BELOW_THRESHOLD: REASON_NOT_DETECTED,
    REASON_AMBIGUOUS: REASON_AMBIGUOUS_CAF,
    REASON_COUPLED: REASON_COUPLED_CAF,
    REASON_NARROWBAND: REASON_NARROWBAND_CAF,
    REASON_OUTSIDE: REASON_OUTSIDE_WINDOW,
}
"""``core.caf`` reason strings to measurement-row reasons."""


# ----------------------------------------------------------------------------- interfaces


class CaptureHub(Protocol):
    """What tasking needs from ``server.hub.Hub`` (every call on the event loop)."""

    async def capture(
        self,
        channel_id: str,
        t_start_ns: int,
        n_samples: int,
        sample_rate_hz: float,
        center_hz: float,
        deadline_ns: int,
    ) -> Any:
        """``AssembledCapture`` (``iq``, ``t_first_ns``, ``sample_rate_hz``,
        ``center_hz``, ``overflow``, ``clock_locked``); raises ``CaptureFailed``."""
        ...

    def rtt_ms(self, site_id: str) -> float | None:
        """Worst recent heartbeat round trip of the site's node, ms."""
        ...

    def channel_capabilities(self, channel_id: str) -> Any:
        """``Capabilities`` of an acknowledged channel, or None when not ready."""
        ...

    def is_online(self, site_id: str) -> bool:
        """Whether the site's node is connected."""
        ...


class Pool(Protocol):
    """``runtime.WorkPool``: blocking work in a worker thread, bounded."""

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """``fn(*args, **kwargs)`` in a worker thread."""
        ...


# ----------------------------------------------------------------------------- public records


@dataclass(frozen=True)
class Calibration:
    """One reference-carrier calibration of a pair and kind (MATH.md section 9).

    key: ``(a.sat_id, a.site_id, b.sat_id, b.site_id, kind)`` in the orientation it
    was measured. correction: ``c = z_ref - h_ref(x_ref)``, s (TDOA) or Hz (FDOA).
    cal: the ``CalRef`` target rows carry (its ``cal_id`` is unique per reference
    measurement and kind). t_ns: ``t_mid`` of the reference measurement, integer UTC
    ns. carrier_id: the reference carrier. a_transponder_id, b_transponder_id: the
    transponders it went through. f_ref_hz: its uplink centre, Hz. snr_db: its
    post-correlation SNR, dB.
    """

    key: tuple[str, str, str, str, str]
    correction: float
    cal: CalRef
    t_ns: int
    carrier_id: str
    a_transponder_id: str
    b_transponder_id: str
    f_ref_hz: float
    snr_db: float


@dataclass
class PairOutcome:
    """What one pair of one round produced.

    a_channel_id, b_channel_id: the pair (A minus B). status: ``measured`` (rows
    added, at least one used by the solver), ``rejected`` (rows added, none used),
    ``failed`` (a capture failed; rows only for a non-retryable failure) or
    ``skipped``. reason: why it is not ``measured``. result: the CAF result, when one
    was made. tdoa_seq, fdoa_seq: the measurement entries added. confirmed: this
    detection confirmed the pair's track. calibrated: kinds that carry a calibration.
    t_mid_ns: epoch of the measurement (integer UTC ns).
    """

    a_channel_id: str
    b_channel_id: str
    status: str
    reason: str | None = None
    result: CafResult | None = None
    tdoa_seq: int | None = None
    fdoa_seq: int | None = None
    confirmed: bool = False
    calibrated: tuple[str, ...] = ()
    t_mid_ns: int | None = None


@dataclass
class RoundReport:
    """Summary of one round (returned by ``Tasking.run_round``, logged by the loop).

    skipped: why nothing was captured (None when captures were requested).
    cancelled: the round was cancelled (event closed, reconfiguration, stop).
    t_start_ns, sample_rate_hz, n_samples: the target captures requested.
    failures: capture key -> ``(code, message)`` of failed captures (the key is the
    channel id, ``ref:<channel>:<carrier>`` for reference captures).
    calibrations: calibrations made this round. classification: the label attached
    this round, if any. solution, solve_reason: the solve of this round (both None
    when no solve ran). notes: plain-language remarks (what could not be used).
    """

    event_id: str
    started_ns: int
    skipped: str | None = None
    cancelled: bool = False
    t_start_ns: int | None = None
    sample_rate_hz: float | None = None
    n_samples: int | None = None
    primary_channel_id: str | None = None
    partner_channel_ids: list[str] = field(default_factory=list)
    pairs: list[PairOutcome] = field(default_factory=list)
    failures: dict[str, tuple[str, str]] = field(default_factory=dict)
    calibrations: list[Calibration] = field(default_factory=list)
    classification: str | None = None
    solution: Solution | None = None
    solve_reason: str | None = None
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- internal records


@dataclass(frozen=True)
class _Pick:
    """A receive channel chosen for a round."""

    channel_id: str
    site_id: str
    sat_id: str
    transponder_id: str
    translation_hz: float
    group_delay_s: float
    max_rate_hz: float
    quality_db: float | None


@dataclass(frozen=True)
class _Detection:
    """One accepted CAF detection of a pair, raw (uncalibrated) values."""

    t_ns: int
    tdoa_s: float | None
    sigma_tdoa_s: float | None
    fdoa_hz: float | None
    sigma_fdoa_hz: float | None


@dataclass(eq=False)
class _PairTrack:
    """Confirmation state of one pair of one event."""

    confirmed: _Detection | None = None
    candidates: deque[_Detection] = field(default_factory=lambda: deque(maxlen=_CANDIDATES_KEPT))


@dataclass(eq=False)
class _EventState:
    """Tasking's bookkeeping for one event."""

    event_id: str
    task: asyncio.Task[RoundReport] | None = None
    next_due_ns: int = 0
    rounds: int = 0
    solves: int = 0
    last_solved_seq: int = 0
    duration_s: float | None = None
    excluded: dict[str, str] = field(default_factory=dict)
    tracks: dict[tuple[str, str], _PairTrack] = field(default_factory=dict)
    classified_ns: int | None = None
    state: str = "waiting"
    message: str | None = None
    last_round_ns: int | None = None
    channels: tuple[str, ...] = ()
    solve_reason: str | None = None
    primary_channel_id: str | None = None


@dataclass(frozen=True)
class _Kinematics:
    """Bounds on a satellite's ECEF speed (m/s) and acceleration (m/s^2)."""

    speed_mps: float
    accel_mps2: float


@dataclass(frozen=True)
class _CaptureOutcome:
    channel_id: str
    capture: Any | None
    code: str | None = None
    message: str | None = None
    retryable: bool = True


Windows = tuple[tuple[float, float], tuple[float, float]]


@dataclass(frozen=True, eq=False)
class _GeoIn:
    """One pair for the geometry job: two channels, the carrier, and (for a reference)
    the reference position."""

    a: _Pick
    b: _Pick
    f_hz: float
    width_hz: float
    fs_hz: float
    x_ref: np.ndarray | None = None


@dataclass(frozen=True)
class _GeoOut:
    """Geometry of one pair: nominal paths, geometric windows (target: extremes over
    the admissible region; reference: the predicted point), and the swapped co-channel
    pairing with its windows when both cross copies fall in the other channel's band."""

    a_nom: PathSpec
    b_nom: PathSpec
    windows: Windows | None
    mirror_a: PathSpec | None = None
    mirror_b: PathSpec | None = None
    mirror: Windows | None = None
    error: str | None = None


@dataclass(frozen=True, eq=False)
class _GeometryJob:
    pairs: tuple[_GeoIn, ...]
    refs: tuple[_GeoIn, ...]
    t_mid_s: float
    t_ref_s: float
    orbits: Mapping[str, Any]
    sites_xyz: Mapping[str, np.ndarray]
    region: Any
    min_elevation_deg: float
    height_m: float
    sat_ids: tuple[str, ...]
    site_ids: tuple[str, ...]


@dataclass(frozen=True)
class _GeometryResult:
    pairs: tuple[_GeoOut, ...]
    refs: tuple[_GeoOut, ...]
    kinematics: Mapping[str, _Kinematics]
    elevation: Mapping[tuple[str, str], float]


@dataclass(frozen=True, eq=False)
class _CafJob:
    ya: np.ndarray
    yb: np.ndarray
    fs: float
    t0_offset_s: float
    t_mid_s: float
    f_e_hz: float
    path_a: PathSpec  # as tuned: f_tune_hz is the centre the node used
    path_b: PathSpec
    heard: tuple[tuple[str, float], ...]  # (sat_id, translation_hz) heard by both channels
    direct_point: bool
    delay_window: tuple[float, float]
    doppler_window: tuple[float, float]  # raw CAF Doppler (before the centre correction)
    mirror_window: Windows | None
    signal_bw_hz: float | None
    min_snr_db: float
    max_tdoa_sigma_s: float
    orbits: Mapping[str, Any]
    sites_xyz: Mapping[str, np.ndarray]


@dataclass(eq=False)
class _PairCtx:
    """Everything about one correlated pair needed after the CAF."""

    a: _Pick
    b: _Pick
    pair_ref: schemas.PairRef
    path_a: PathSpec
    path_b: PathSpec
    t_mid_ns: int
    shift_hz: float  # z_fdoa = nu_peak + shift_hz (MATH.md section 5)
    tdoa_blocked: bool
    rates: tuple[float, float]
    outcome: PairOutcome


@dataclass(frozen=True, eq=False)
class _RefJob:
    caf: _CafJob
    x_ref: np.ndarray
    shift_hz: float
    t_mid_ns: int
    tdoa_allowed: bool
    cal_ids: tuple[str, str]


@dataclass(frozen=True)
class _RefResult:
    result: CafResult
    corrections: tuple[tuple[str, float, CalRef], ...]
    problems: tuple[str, ...]


@dataclass(eq=False)
class _RefPlan:
    """A reference calibration planned for one pair of a round."""

    a: _Pick
    b: _Pick
    carrier_id: str
    f_ref_hz: float
    bandwidth_hz: float
    x_ref: np.ndarray
    fs: float = 0.0
    n: int = 0
    t_start_ns: int = 0
    geo: _GeoOut | None = None


@dataclass(frozen=True, eq=False)
class _SolveJob:
    measurements: tuple[Measurement, ...]
    orbits: Mapping[str, Any]
    sites_xyz: Mapping[str, np.ndarray]
    solver_cfg: Any
    region: Any
    uncertainty: Uncertainty | None
    starts: tuple[tuple[float, float], ...] | None


# ----------------------------------------------------------------------------- pure helpers


def _finite(value: Any) -> float | None:
    """A finite float, or None."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _overlaps(lo_a: float, hi_a: float, lo_b: float, hi_b: float) -> bool:
    """Whether two ranges share more than an edge."""
    return min(hi_a, hi_b) > max(lo_a, lo_b)


def _in_band(f_centre: float, width_hz: float, fc: float, fs: float) -> bool:
    """Whether a signal of ``width_hz`` centred on ``f_centre`` overlaps a capture band."""
    half = 0.5 * max(width_hz, 0.0)
    return _overlaps(f_centre - half, f_centre + half + 1e-9, fc - 0.5 * fs, fc + 0.5 * fs)


def _classification_band(iq: Any, fs: float, offset_hz: float, width_hz: float) -> tuple[Any, float]:
    """The part of a capture that classification looks at, as ``(iq, fs)``.

    iq, fs: the capture (complex baseband) and its sample rate, Hz.
    offset_hz: centre of the event relative to the capture centre, Hz.
    width_hz: the event's width, Hz.
    The band of ``CLASSIFY_BAND_FACTOR x width_hz`` (at least ``2 x
    MIN_CLASSIFY_SAMPLES`` samples) around the event is cut out of the spectrum and
    returned at baseband at the reduced rate, with the amplitude of signals inside it
    unchanged; the frequency resolution (the capture length) is kept. Returns the
    capture itself when that band is the whole capture. A capture at the minimum rate
    is much wider than a narrow event, and ``core.detect.classify`` classifies the
    strongest signal it sees: without this a neighbouring carrier can take the event's
    place."""
    x = np.asarray(iq)
    n = int(x.shape[0])
    keep_hz = max(CLASSIFY_BAND_FACTOR * max(width_hz, _MIN_SIGNAL_BW_HZ), 2.0 * MIN_CLASSIFY_SAMPLES * fs / n)
    m = int(round(n * keep_hz / fs))
    if m >= n:
        return iq, fs
    spectrum = np.roll(np.fft.fft(x.astype(np.complex128)), -int(round(offset_hz * n / fs)))
    half = m // 2
    kept = np.concatenate((spectrum[: m - half], spectrum[n - half :]))
    return np.fft.ifft(kept) * (m / n), fs * m / n


def _classify_event(iq: Any, fs: float, offset_hz: float, width_hz: float) -> Any:
    """Worker job: ``core.detect.classify`` over the event's band (``_classification_band``)."""
    band, rate = _classification_band(iq, fs, offset_hz, width_hz)
    return classify(band, rate)


def _channel_ref(pick: _Pick) -> schemas.ChannelRef:
    return schemas.ChannelRef(sat_id=pick.sat_id, site_id=pick.site_id, channel_id=pick.channel_id)


def _pair_ref(a: _Pick, b: _Pick) -> schemas.PairRef:
    return schemas.PairRef(a=_channel_ref(a), b=_channel_ref(b))


def _path(pick: _Pick, f_hz: float, translation_hz: float | None = None) -> PathSpec:
    return PathSpec(
        pick.sat_id,
        pick.site_id,
        float(f_hz),
        pick.translation_hz if translation_hz is None else translation_hz,
        pick.group_delay_s,
    )


def _samples_for_window(delay_window: tuple[float, float], fs: float, noise_bw_hz: float, t0_s: float = 0.0) -> int:
    """Smallest capture length whose half holds the delay window plus the CAF guard."""
    reach = max(abs(delay_window[0] - t0_s), abs(delay_window[1] - t0_s))
    lobe = int(math.ceil(fs / max(noise_bw_hz, 1e-9)))
    guard = _GUARD_LAGS + 8 * lobe + 2
    return 2 * (int(math.ceil(reach * fs)) + guard) + 2


def _point_window(
    x: np.ndarray, a: PathSpec, b: PathSpec, t_s: float, orbits: Mapping[str, Any], sites_xyz: Mapping[str, Any]
) -> Windows:
    tdoa, fdoa = predict_pair(x, a, b, t_s, orbits, sites_xyz)
    t = float(np.asarray(tdoa).reshape(-1)[0])
    f = float(np.asarray(fdoa).reshape(-1)[0])
    return (t, t), (f, f)


def _cross_paths(
    elevation: Mapping[tuple[str, str], float], spec: _GeoIn
) -> tuple[PathSpec, PathSpec] | None:
    """The swapped co-channel pairing (b's satellite via a's site against a's
    satellite via b's site), when both satellites are seen from the other site and
    both cross copies fall in the other channel's band; None otherwise."""
    a, b = spec.a, spec.b
    if a.sat_id == b.sat_id:
        return None
    if elevation.get((a.site_id, b.sat_id), -90.0) <= 0.0 or elevation.get((b.site_id, a.sat_id), -90.0) <= 0.0:
        return None
    fc_a, fc_b = spec.f_hz + a.translation_hz, spec.f_hz + b.translation_hz
    if not (
        _in_band(spec.f_hz + b.translation_hz, spec.width_hz, fc_a, spec.fs_hz)
        and _in_band(spec.f_hz + a.translation_hz, spec.width_hz, fc_b, spec.fs_hz)
    ):
        return None
    return (
        PathSpec(b.sat_id, a.site_id, spec.f_hz, b.translation_hz, b.group_delay_s),
        PathSpec(a.sat_id, b.site_id, spec.f_hz, a.translation_hz, a.group_delay_s),
    )


def _geometry(job: _GeometryJob) -> _GeometryResult:
    """Elevations, satellite kinematics, and the windows of every pair and reference
    pair (blocking: about 20 ms per ``measurement_windows``)."""
    elevation: dict[tuple[str, str], float] = {}
    for site_id in job.site_ids:
        for sat_id in job.sat_ids:
            el = elevation_deg(job.sites_xyz[site_id], job.orbits[sat_id], job.t_mid_s)
            elevation[(site_id, sat_id)] = float(np.asarray(el).reshape(-1)[0])
    kinematics: dict[str, _Kinematics] = {}
    for sat_id in job.sat_ids:
        orbit = job.orbits[sat_id]
        velocities = [
            np.asarray(orbit.state_ecef(job.t_mid_s + dt)[1], dtype=np.float64).reshape(3) for dt in (-1.0, 0.0, 1.0)
        ]
        speed = max(float(np.linalg.norm(v)) for v in velocities)
        accel = float(np.linalg.norm(velocities[2] - velocities[0])) / 2.0
        kinematics[sat_id] = _Kinematics(speed, accel)

    def target(spec: _GeoIn) -> _GeoOut:
        a_nom, b_nom = _path(spec.a, spec.f_hz), _path(spec.b, spec.f_hz)
        try:
            windows = measurement_windows(
                a_nom, b_nom, job.t_mid_s, job.orbits, job.sites_xyz, job.region, job.min_elevation_deg, job.height_m
            )
        except ValueError as exc:
            return _GeoOut(a_nom, b_nom, None, error=f"windows: {exc}")
        if windows is None:
            return _GeoOut(a_nom, b_nom, None, error="no admissible emitter position sees both satellites")
        cross = _cross_paths(elevation, spec)
        if cross is None:
            return _GeoOut(a_nom, b_nom, windows)
        try:
            mirror = measurement_windows(
                cross[0], cross[1], job.t_mid_s, job.orbits, job.sites_xyz, job.region,
                job.min_elevation_deg, job.height_m,
            )
        except ValueError:
            mirror = None
        if mirror is None:
            return _GeoOut(a_nom, b_nom, windows)
        return _GeoOut(a_nom, b_nom, windows, cross[0], cross[1], mirror)

    def reference(spec: _GeoIn) -> _GeoOut:
        a_nom, b_nom = _path(spec.a, spec.f_hz), _path(spec.b, spec.f_hz)
        assert spec.x_ref is not None
        for sat_id in {spec.a.sat_id, spec.b.sat_id}:
            el = float(np.asarray(elevation_deg(spec.x_ref, job.orbits[sat_id], job.t_ref_s)).reshape(-1)[0])
            if el < job.min_elevation_deg or el <= 0.0:
                return _GeoOut(a_nom, b_nom, None, error=f"the reference does not see {sat_id}")
        try:
            windows = _point_window(spec.x_ref, a_nom, b_nom, job.t_ref_s, job.orbits, job.sites_xyz)
        except ValueError as exc:
            return _GeoOut(a_nom, b_nom, None, error=f"reference prediction: {exc}")
        cross = _cross_paths(elevation, spec)
        if cross is None:
            return _GeoOut(a_nom, b_nom, windows)
        try:
            mirror = _point_window(spec.x_ref, cross[0], cross[1], job.t_ref_s, job.orbits, job.sites_xyz)
        except ValueError:
            return _GeoOut(a_nom, b_nom, windows)
        return _GeoOut(a_nom, b_nom, windows, cross[0], cross[1], mirror)

    return _GeometryResult(
        pairs=tuple(target(s) for s in job.pairs),
        refs=tuple(reference(s) for s in job.refs),
        kinematics=kinematics,
        elevation=elevation,
    )


def _correlate(job: _CafJob) -> CafResult:
    """Self-peak exclusions and ``caf_estimate`` (blocking, 0.2 to 1.5 s)."""
    exclude: list[tuple[float, float]] = []
    for sat_id, translation in job.heard:
        tdoa, fdoa = self_peak(
            sat_id, translation, job.f_e_hz, job.path_a, job.path_b, job.t_mid_s, job.orbits, job.sites_xyz
        )
        exclude.append((float(tdoa), float(fdoa)))
    if job.direct_point:
        exclude.append((0.0, job.path_b.f_tune_hz - job.path_a.f_tune_hz))
    return caf_estimate(
        job.ya,
        job.yb,
        job.fs,
        delay_window=job.delay_window,
        doppler_window=job.doppler_window,
        signal_bw_hz=job.signal_bw_hz,
        t0_offset_s=job.t0_offset_s,
        exclude=exclude,
        mirror_window=job.mirror_window,
        min_snr_db=job.min_snr_db,
        max_tdoa_sigma_s=job.max_tdoa_sigma_s,
    )


def _calibrate(job: _RefJob, orbits: Mapping[str, Any], sites_xyz: Mapping[str, np.ndarray]) -> _RefResult:
    """Correlate a reference pair and turn each usable kind into a calibration
    (blocking)."""
    result = _correlate(job.caf)
    problems: list[str] = []
    corrections: list[tuple[str, float, CalRef]] = []
    if not result.detected:
        return _RefResult(result, (), (REASONS.get(result.reason or "", REASON_NOT_DETECTED),))
    if result.reason in (REASON_AMBIGUOUS, REASON_COUPLED, REASON_OUTSIDE):
        return _RefResult(result, (), (REASONS[result.reason],))
    if result.mirror_ambiguous:
        return _RefResult(result, (), (REASON_MIRROR,))
    values = {
        "tdoa": (_finite(result.tdoa_s), _finite(result.sigma_tdoa_s)),
        "fdoa": (
            None if _finite(result.fdoa_hz) is None else float(result.fdoa_hz) + job.shift_hz,  # type: ignore[arg-type]
            _finite(result.sigma_fdoa_hz),
        ),
    }
    for kind, cal_id in zip(("tdoa", "fdoa"), job.cal_ids):
        value, sigma = values[kind]
        if kind == "tdoa" and not job.tdoa_allowed:
            problems.append(f"tdoa: {REASON_CLOCK_UNLOCKED}")
            continue
        if value is None or sigma is None or not sigma > 0:
            problems.append(f"{kind}: not measured ({REASONS.get(result.reason or '', result.reason)})")
            continue
        ref_row = Measurement(
            kind=kind,  # type: ignore[arg-type]
            value=value,
            sigma=sigma,
            t_ns=job.t_mid_ns,
            a=job.caf.path_a,
            b=job.caf.path_b,
        )
        try:
            c, cal = calibration_correction(ref_row, job.x_ref, orbits, sites_xyz, cal_id)
        except ValueError as exc:
            problems.append(f"{kind}: {exc}")
            continue
        if _finite(c) is None:
            problems.append(f"{kind}: correction not finite")
            continue
        corrections.append((kind, float(c), cal))
    return _RefResult(result, tuple(corrections), tuple(problems))


def _solve(job: _SolveJob) -> tuple[Solution | None, str | None]:
    """``core.solver.solve`` (blocking); a ValueError becomes ``(None, reason)``."""
    try:
        return solve(
            list(job.measurements),
            job.orbits,
            job.sites_xyz,
            job.solver_cfg,
            job.region,
            job.uncertainty,
            starts=None if job.starts is None else list(job.starts),
        )
    except ValueError as exc:  # numpy's LinAlgError included
        return None, f"solver rejected the measurements: {exc}"


def _consistent(prev: _Detection, new: _Detection, rates: tuple[float, float]) -> bool:
    """Whether two detections of one pair agree: every kind both have within
    ``CONFIRM_SIGMAS`` combined sigmas plus the change the bounded dynamics allow over
    the interval; at least one kind in common."""
    dt = abs(new.t_ns - prev.t_ns) / _NS
    common = 0
    if prev.tdoa_s is not None and new.tdoa_s is not None:
        gate = CONFIRM_SIGMAS * math.hypot(prev.sigma_tdoa_s or 0.0, new.sigma_tdoa_s or 0.0)
        gate += dt * rates[0] + _TDOA_FLOOR_S
        if not abs(new.tdoa_s - prev.tdoa_s) <= gate:
            return False
        common += 1
    if prev.fdoa_hz is not None and new.fdoa_hz is not None:
        gate = CONFIRM_SIGMAS * math.hypot(prev.sigma_fdoa_hz or 0.0, new.sigma_fdoa_hz or 0.0)
        gate += dt * rates[1] + _FDOA_FLOOR_HZ
        if not abs(new.fdoa_hz - prev.fdoa_hz) <= gate:
            return False
        common += 1
    return common > 0


EXPLAIN_NARROWBAND: Final[str] = (
    "No TDOA: the signal is too narrow for a time difference of arrival to be measured "
    "(an unmodulated carrier has no bandwidth to time), so each satellite pair gives a "
    "frequency difference (FDOA) only and the position rests on FDOA alone."
)
EXPLAIN_AMBIGUOUS: Final[str] = (
    "The correlations are ambiguous: several peaks of almost equal height (a periodic "
    "waveform such as a repeating sweep matches itself one period later), so no time or "
    "frequency difference is used and no position is computed from them."
)
EXPLAIN_COUPLED: Final[str] = (
    "Delay and Doppler cannot be told apart in the correlation (a single sweep across the "
    "capture), so no time or frequency difference is used."
)
EXPLAIN_NOT_DETECTED: Final[str] = (
    "The copy relayed by the other satellite was not found in the correlation (below the "
    "detection threshold), so the pair gives no measurement."
)


def round_explanation(pairs: Sequence[PairOutcome]) -> str | None:
    """One plain sentence for the operator when every correlated pair of a round failed
    to give a usable time difference for the same reason (``EXPLAIN_*``), else None.

    pairs: the round's outcomes; only those with a ``CafResult`` count.
    """
    results = [p.result for p in pairs if p.result is not None]
    if not results:
        return None
    reasons = {r.reason if r.detected else REASON_BELOW_THRESHOLD for r in results}
    if len(reasons) != 1:
        return None
    reason = reasons.pop()
    return {
        REASON_NARROWBAND: EXPLAIN_NARROWBAND,
        REASON_AMBIGUOUS: EXPLAIN_AMBIGUOUS,
        REASON_COUPLED: EXPLAIN_COUPLED,
        REASON_BELOW_THRESHOLD: EXPLAIN_NOT_DETECTED,
    }.get(reason)  # type: ignore[arg-type]


def truth_emitters(world: Any) -> list[TruthEmitter]:
    """``TruthEmitter`` records of a ``sim.world.SimWorld``'s resolved emitters, for
    ``events.event_truth`` (simulation only)."""
    return [
        TruthEmitter(
            id=e.id,
            kind=e.kind,
            lat_deg=float(e.lat_deg),
            lon_deg=float(e.lon_deg),
            freq_hz=float(e.freq_hz),
            bandwidth_hz=float(e.bandwidth_hz),
            enabled=bool(e.enabled),
        )
        for e in world.emitters
    ]


# ----------------------------------------------------------------------------- tasking


class Tasking:
    """Capture tasking, correlation, calibration and solving for active events.

    Every method must be called on the server's event loop.
    """

    def __init__(
        self,
        *,
        hub: CaptureHub,
        book: EventBook,
        config: Any,
        pool: Pool,
        sightings: Callable[[str], Sequence[Unit]],
        store: Any | None = None,
        clock: Callable[[], int] = time.time_ns,
        simulated: Callable[[], bool] = lambda: False,
        save_captures: bool = True,
        tick_s: float = TICK_S,
        max_rounds: int = MAX_CONCURRENT_ROUNDS,
    ) -> None:
        """
        hub: the node hub (``capture``, ``rtt_ms``, ``channel_capabilities``,
            ``is_online``). book: the ``EventBook`` (mutation API). config: the
            ``SystemConfig`` in force. pool: the server's ``WorkPool`` (at most 3
            numerical jobs at once). sightings: the current detections of an event,
            strongest SNR first (``Correlator.sightings``).
        store: the ``Store`` (calibrations, IQ captures), or None.
        clock: server time, integer UTC ns. simulated: whether the server simulates
            (recorded with stored captures and calibrations).
        save_captures: write the round's captures to the store.
        tick_s: scheduling period, s (> 0). max_rounds: rounds in flight at once (>= 1).
        Raises ValueError naming a bad argument.
        """
        if not (isinstance(tick_s, (int, float)) and tick_s > 0):
            raise ValueError("tick_s: must be positive")
        if isinstance(max_rounds, bool) or int(max_rounds) < 1:
            raise ValueError("max_rounds: must be at least 1")
        self._hub = hub
        self._book = book
        self._pool = pool
        self._sightings = sightings
        self._store = store
        self._clock = clock
        self._simulated = simulated
        self._save = bool(save_captures)
        self._tick_s = float(tick_s)
        self._max_rounds = int(max_rounds)
        self._states: dict[str, _EventState] = {}
        # calibrations in force by (Calibration.key, carrier_id): one per pair, kind and carrier
        self._calibrations: dict[tuple[tuple[str, str, str, str, str], str], Calibration] = {}
        # last reference attempt per (pair group, carrier): two events on different reference
        # carriers through the same channels must not hold back each other's calibration
        self._cal_attempts: dict[tuple[tuple[tuple[str, str], tuple[str, str]], str], int] = {}
        self._world_since_ns: int | None = None  # calibrations measured before this are not kept
        self._cal_seq = itertools.count(1)
        self._loop_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._stopped = False
        self._generation = 0
        self._quality: QualityTable | None = None
        self._quality_built_ns = 0
        self._captures_in_flight = 0
        self.counters: dict[str, int] = {}
        """Diagnostics: rounds, captures requested / completed / failed per code,
        calibrations, solves, cancellations, errors."""
        self._apply_config(config)
        self._remove_listener: Callable[[], None] | None = book.add_listener(self._on_event_change)

    # ------------------------------------------------------------------ configuration

    def _apply_config(self, config: Any) -> None:
        self._config = config
        self._orbits: dict[str, Any] = config.orbits()
        self._sites_xyz: dict[str, np.ndarray] = config.sites_xyz()
        self._uncertainty: Uncertainty = config.resolved_uncertainty()
        self._channel_site: dict[str, str] = {}
        self._channels: dict[str, Any] = {}
        for site in config.sites:
            for channel in site.channels:
                self._channel_site[channel.id] = site.id
                self._channels[channel.id] = channel

    @property
    def config(self) -> Any:
        """The configuration in force."""
        return self._config

    def reconfigure(self, config: Any) -> None:
        """Take a new configuration. Rounds in flight are cancelled; calibrations,
        confirmation tracks and channel exclusions are dropped (orbits, transponders or
        channels may have changed). Measurements already recorded stay with their
        events and are solved with the new orbits (rows naming a satellite or site
        that no longer exists are left out)."""
        self._generation += 1
        self._apply_config(config)
        self._quality = None
        self._calibrations.clear()
        self._cal_attempts.clear()
        for state in self._states.values():
            if state.task is not None and not state.task.done():
                state.task.cancel()
            state.tracks.clear()
            state.excluded.clear()
            state.next_due_ns = 0
        self._count("reconfigured")
        logger.info("tasking reconfigured (generation %d)", self._generation, extra={"generation": self._generation})

    def world_changed(self, effective_ns: int) -> None:
        """The simulated world was replaced (a scenario load), taking effect at
        ``effective_ns`` (server time, integer ns). A reference calibration describes the
        world it was measured in (its satellites' errors, its sites' clocks), so the
        calibrations held are dropped, any measured before ``effective_ns`` by a round
        still in flight are not kept, and every pair is calibrated again at its next
        round."""
        self._world_since_ns = int(effective_ns)
        self._calibrations.clear()
        self._cal_attempts.clear()
        logger.info("simulated world replaced: calibrations dropped", extra={"effective_ns": str(int(effective_ns))})

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the scheduling loop (once, on the event loop). Raises RuntimeError when
        already started or stopped."""
        if self._stopped:
            raise RuntimeError("start: tasking is stopped")
        if self._loop_task is not None:
            raise RuntimeError("start: tasking is already running")
        self._loop_task = asyncio.get_running_loop().create_task(self._loop(), name="petal-tasking")
        logger.info("tasking started")

    async def stop(self) -> None:
        """Stop the loop, cancel every round and wait for them, then finish (at most
        ``_SAVE_TIMEOUT_S``) the capture writes in progress. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        tasks: list[asyncio.Task[Any]] = []
        if self._loop_task is not None:
            self._loop_task.cancel()
            tasks.append(self._loop_task)
        for state in self._states.values():
            if state.task is not None and not state.task.done():
                state.task.cancel()
                tasks.append(state.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        background = list(self._background)
        if background:
            _, pending = await asyncio.wait(background, timeout=_SAVE_TIMEOUT_S)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning("tasking: %d capture write(s) abandoned at stop", len(pending))
        logger.info("tasking stopped", extra={"counters": dict(self.counters)})

    @property
    def running(self) -> bool:
        """Whether the scheduling loop runs."""
        return self._loop_task is not None and not self._loop_task.done()

    def rounds_in_flight(self) -> int:
        """Rounds running now."""
        return sum(1 for s in self._states.values() if s.task is not None and not s.task.done())

    def calibrations(self) -> list[Calibration]:
        """The newest calibration of every pair and kind."""
        return list(self._calibrations.values())

    @property
    def captures_in_flight(self) -> int:
        """Captures requested from the hub and not yet answered."""
        return self._captures_in_flight

    def _count(self, name: str, by: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + by

    # ------------------------------------------------------------------ scheduling

    async def _loop(self) -> None:
        while True:
            try:
                self._schedule()
            except Exception:  # the loop must survive anything one pass does
                self._count("schedule_errors")
                logger.exception("tasking schedule pass failed")
            await asyncio.sleep(self._tick_s)

    def _schedule(self) -> None:
        now = self._clock()
        for event_id in [eid for eid, s in self._states.items() if s.task is None or s.task.done()]:
            record = self._book.get(event_id)
            if record is None or record.closed or record.historic:
                del self._states[event_id]
        running = self.rounds_in_flight()
        taskable = self._book.taskable()
        for record in taskable:
            state = self._states.setdefault(record.id, _EventState(record.id))
            if state.task is not None and not state.task.done():
                continue
            if now < state.next_due_ns:
                continue
            if running >= self._max_rounds:
                self._count("rounds_deferred")
                break
            self._start_round(state)
            running += 1
        self._idle_untaskable({r.id for r in taskable})

    def _idle_untaskable(self, taskable_ids: set[str]) -> None:
        """Give an active event tasking no longer works on (judged ``local``) an
        ``idle`` status saying why, once, instead of a stale ``waiting`` with a next
        round that will not come."""
        for event_id, state in self._states.items():
            if event_id in taskable_ids or (state.task is not None and not state.task.done()):
                continue
            record = self._book.get(event_id)
            if record is None or not record.active or record.verdict != "local":
                continue
            message = (
                "Judged local to one site: nothing reaches a satellite, so there is nothing to "
                "locate by satellite and no further captures are made."
            )
            if state.state == "idle" and state.message == message:
                continue
            state.state, state.message, state.next_due_ns = "idle", message, 0
            self._publish_status(state)

    def _start_round(self, state: _EventState) -> asyncio.Task[RoundReport]:
        interval_ns = int(float(self._config.processing.capture.interval_s) * _NS)
        state.next_due_ns = self._clock() + interval_ns
        task = asyncio.get_running_loop().create_task(self._round_task(state), name=f"petal-round-{state.event_id}")
        state.task = task
        return task

    async def _round_task(self, state: _EventState) -> RoundReport:
        started = self._clock()
        try:
            report = await self._round(state)
        except asyncio.CancelledError:
            state.state = "waiting"
            self._count("rounds_cancelled")
            logger.info("round of %s cancelled", state.event_id, extra={"event_id": state.event_id})
            raise
        except Exception as exc:  # a failed round must not stop tasking
            self._count("round_errors")
            logger.exception("round of %s failed", state.event_id, extra={"event_id": state.event_id})
            state.state = "waiting"
            state.message = f"The last capture round failed: {exc}"
            self._publish_status(state)
            return RoundReport(state.event_id, started, skipped=f"error: {exc}")
        self._count("rounds")
        logger.info(
            "round of %s: %s",
            state.event_id,
            report.skipped or f"{len(report.pairs)} pair(s), {len(report.failures)} capture failure(s)",
            extra={
                "event_id": state.event_id,
                "pairs": len(report.pairs),
                "failures": len(report.failures),
                "solve_reason": report.solve_reason,
                "skipped": report.skipped,
            },
        )
        return report

    async def run_round(self, event_id: str) -> RoundReport:
        """Run one round for an event now and return its report (tests, an operator
        request). A round cancelled by an event close or ``reconfigure`` returns a
        report with ``cancelled`` set. Raises ``EventNotFound`` for an unknown id and
        RuntimeError when a round of the event is already running or tasking stopped."""
        if self._stopped:
            raise RuntimeError("run_round: tasking is stopped")
        record = self._book.resolve(event_id)
        if record is None:
            raise EventNotFound(event_id)
        state = self._states.setdefault(record.id, _EventState(record.id))
        if state.task is not None and not state.task.done():
            raise RuntimeError(f"run_round: a round of {record.id} is already running")
        task = self._start_round(state)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or current.cancelling() == 0):
                return RoundReport(record.id, self._clock(), cancelled=True)
            raise

    def cancel(self, event_id: str) -> bool:
        """Cancel the round in flight for an event; True when one was cancelled."""
        state = self._states.get(event_id)
        if state is None or state.task is None or state.task.done():
            return False
        state.task.cancel()
        return True

    def _on_event_change(self, record: EventRecord, changes: frozenset[str]) -> None:
        """EventBook listener: a closed (or merged) event loses its round."""
        if not (record.closed or record.historic or record.merged_into is not None):
            return
        if self.cancel(record.id):
            self._count("cancelled_on_close")
            logger.info("round of %s cancelled: the event is closed", record.id, extra={"event_id": record.id})

    def _abort_if_stale(self, generation: int) -> None:
        """Cancel the current round when the configuration changed under it or tasking
        is stopping."""
        if generation != self._generation or self._stopped:
            raise asyncio.CancelledError()

    # ------------------------------------------------------------------ status

    def _publish_status(self, state: _EventState) -> None:
        """Hand the event's tasking status to the book (when it supports it)."""
        setter = getattr(self._book, "set_tasking", None)
        model = getattr(schemas, "TaskingStatus", None)
        if setter is None or model is None:
            return
        try:
            status = model(
                state=state.state,
                rounds=state.rounds,
                last_round_ns=state.last_round_ns,
                next_round_ns=state.next_due_ns if state.next_due_ns > 0 else None,
                channels=list(state.channels),
                message=state.message,
                solve_reason=state.solve_reason,
            )
        except ValidationError as exc:
            logger.warning("tasking status of %s not published: %s", state.event_id, exc)
            return
        setter(state.event_id, status)

    # ------------------------------------------------------------------ channel choice

    async def _quality_table(self) -> QualityTable:
        now = self._clock()
        if self._quality is not None and now - self._quality_built_ns <= int(QUALITY_REFRESH_S * _NS):
            return self._quality
        generation = self._generation
        table = await self._pool.run(QualityTable.build, self._config, now / 1e9)
        if generation == self._generation:
            self._quality, self._quality_built_ns = table, now
        return table

    def _pick(
        self,
        channel_id: str,
        sat_id: str,
        transponder_id: str,
        f_e_hz: float,
        state: _EventState,
        quality: QualityTable,
        *,
        require_quality: bool,
    ) -> tuple[_Pick | None, str | None]:
        """A channel usable for the round, or None with why not."""
        channel = self._channels.get(channel_id)
        site_id = self._channel_site.get(channel_id)
        if channel is None or site_id is None:
            return None, f"{channel_id} is not configured"
        if channel_id in state.excluded:
            return None, f"{channel_id} excluded ({state.excluded[channel_id]})"
        if not self._hub.is_online(site_id):
            return None, f"{channel_id}: the node of {site_id} is offline"
        caps = self._hub.channel_capabilities(channel_id)
        if caps is None:
            return None, f"{channel_id} is not ready"
        try:
            transponder = self._config.satellite(sat_id).transponder(transponder_id)
        except ValueError as exc:
            return None, str(exc)
        centre = f_e_hz + float(transponder.translation_hz)
        if not float(caps.min_hz) <= centre <= float(caps.max_hz):
            return None, f"{channel_id} cannot tune {centre:.0f} Hz"
        q = quality.get(channel_id, sat_id, transponder_id)
        if require_quality and q is None:
            return None, f"{channel_id}: {sat_id} is below the horizon"
        max_rate = min(float(channel.max_sample_rate_hz), float(caps.max_sample_rate_hz))
        pick = _Pick(
            channel_id=channel_id,
            site_id=site_id,
            sat_id=sat_id,
            transponder_id=transponder_id,
            translation_hz=float(transponder.translation_hz),
            group_delay_s=float(transponder.group_delay_s),
            max_rate_hz=max_rate,
            quality_db=q,
        )
        return pick, None

    def _choose_channels(
        self, record: EventRecord, state: _EventState, quality: QualityTable, f_e_hz: float
    ) -> tuple[_Pick | None, list[_Pick], list[str]]:
        """The primary channel and one partner per other satellite (see the module
        docstring), with notes on what could not be used. A channel serves one tuning
        per round: a partner that would need a second centre on a channel already used
        is passed over for the next candidate."""
        notes: list[str] = []
        primary: _Pick | None = None
        sightings = [u for u in self._sightings(record.id) if u.sat_id is not None and u.transponder_id is not None]
        # The previous primary stays while it is within PRIMARY_HOLD_DB of the strongest
        # sighting: SNRs of near-equal channels swap from frame to frame, and a new
        # primary makes every pair new (unconfirmed until it repeats).
        if sightings and state.primary_channel_id is not None:
            strongest = sightings[0].snr_db
            held = [u for u in sightings if u.channel_id == state.primary_channel_id]
            if held and held[0].snr_db >= strongest - PRIMARY_HOLD_DB:
                sightings.remove(held[0])
                sightings.insert(0, held[0])
        for unit in sightings:
            assert unit.sat_id is not None and unit.transponder_id is not None
            pick, why = self._pick(
                unit.channel_id, unit.sat_id, unit.transponder_id, f_e_hz, state, quality, require_quality=False
            )
            if pick is not None:
                primary = pick
                break
            if why:
                notes.append(why)
        if primary is None:
            return None, [], notes
        state.primary_channel_id = primary.channel_id
        tunings: dict[str, float] = {primary.channel_id: primary.translation_hz}
        lo, hi = float(record.lo_hz), float(record.hi_hz)
        best_tp: dict[str, tuple[float, Any]] = {}
        for satellite, transponder in self._config.transponders_covering(lo, hi):
            if satellite.id == primary.sat_id:
                continue
            overlap = min(hi, transponder.uplink_hi_hz) - max(lo, transponder.uplink_lo_hz)
            if satellite.id not in best_tp or overlap > best_tp[satellite.id][0]:
                best_tp[satellite.id] = (overlap, transponder)
        partners: list[_Pick] = []
        for sat_id, (_, transponder) in best_tp.items():
            candidates: list[_Pick] = []
            for _site, channel in self._config.channels_watching(sat_id):
                if channel.id == primary.channel_id:
                    continue  # a channel cannot be correlated with itself
                pick, why = self._pick(channel.id, sat_id, transponder.id, f_e_hz, state, quality, require_quality=True)
                if pick is None:
                    if why:
                        notes.append(why)
                    continue
                tuned = tunings.get(pick.channel_id)
                if tuned is not None and tuned != pick.translation_hz:
                    notes.append(f"{pick.channel_id} is already tuned for another satellite this round")
                    continue
                candidates.append(pick)
            if not candidates:
                notes.append(f"no usable channel watches {sat_id}")
                continue
            best = max(candidates, key=lambda p: p.quality_db if p.quality_db is not None else -math.inf)
            best_q = best.quality_db if best.quality_db is not None else -math.inf
            local = [
                p
                for p in candidates
                if p.site_id == primary.site_id
                and p.quality_db is not None
                and p.quality_db >= best_q - PREFER_SAME_SITE_DB
            ]
            choice = max(local, key=lambda p: p.quality_db or -math.inf) if local else best
            choice = self._rotate(choice, candidates, best_q, state.rounds)
            tunings[choice.channel_id] = choice.translation_hz
            partners.append(choice)
        return primary, partners, notes

    @staticmethod
    def _rotate(choice: _Pick, candidates: Sequence[_Pick], best_q: float, round_index: int) -> _Pick:
        """Partner diversity: from the third round on, every other round takes, in turn,
        one of the other candidates within ``ROTATE_WITHIN_DB`` of the best quality.

        The documented choice keeps the even rounds, so its pair is confirmed (a repeat
        detection) and measured as before; the alternates add other sites (independent
        clocks and geometry, calibrated through their own reference measurements), so
        one channel's error cannot dominate and a fault at one site shows in its pairs'
        calibration corrections. Candidates are ordered by channel id (deterministic).
        """
        if round_index < 2 or round_index % 2 == 0:
            return choice
        others = sorted(
            (
                p
                for p in candidates
                if p.channel_id != choice.channel_id
                and p.quality_db is not None
                and p.quality_db >= best_q - ROTATE_WITHIN_DB
            ),
            key=lambda p: p.channel_id,
        )
        if not others:
            return choice
        return others[(round_index // 2 - 1) % len(others)]

    def _needs_classification(self, record: EventRecord, state: _EventState) -> bool:
        """Whether this round should classify the primary capture: when the event has
        no class, ``RECLASSIFY_UNKNOWN_S`` after an ``unknown``, ``RECLASSIFY_S`` after
        any other class (a class set elsewhere counts from when tasking first saw it)."""
        if record.classification is None:
            return True
        now = self._clock()
        if state.classified_ns is None:
            state.classified_ns = now
        wait_s = RECLASSIFY_UNKNOWN_S if record.classification.label == "unknown" else RECLASSIFY_S
        return now - state.classified_ns >= int(wait_s * _NS)

    def _lead_s(self, picks: Sequence[_Pick]) -> float:
        """``max(lead_s, min(3 x worst round trip, MAX_LEAD_S))`` over the sites involved."""
        lead = float(self._config.processing.capture.lead_s)
        worst = 0.0
        for site_id in {p.site_id for p in picks}:
            rtt = _finite(self._hub.rtt_ms(site_id))
            if rtt is not None and rtt > worst:
                worst = rtt
        return max(lead, min(3.0 * worst / 1000.0, MAX_LEAD_S))

    # ------------------------------------------------------------------ calibration

    @staticmethod
    def _cal_group(a: _Pick, b: _Pick) -> tuple[tuple[str, str], tuple[str, str]]:
        first, second = (a.sat_id, a.site_id), (b.sat_id, b.site_id)
        return (first, second) if first <= second else (second, first)

    def _references_for(self, a: _Pick, b: _Pick, lo_hz: float, hi_hz: float, f_e_hz: float) -> list[Any]:
        """Reference carriers relayed by both transponders of a pair, nearest in
        frequency first, none overlapping the event's band."""
        tp_a = self._config.satellite(a.sat_id).transponder(a.transponder_id)
        tp_b = self._config.satellite(b.sat_id).transponder(b.transponder_id)
        out = []
        for carrier in self._config.reference_carriers():
            if carrier.location is None or carrier.emitter is None:
                continue
            f_ref = float(carrier.uplink_center_hz)
            if not (tp_a.relays(f_ref) and tp_b.relays(f_ref)):
                continue
            half = 0.5 * float(carrier.bandwidth_hz)
            if _overlaps(f_ref - half, f_ref + half, lo_hz, hi_hz):
                continue
            out.append(carrier)
        out.sort(key=lambda c: abs(float(c.uplink_center_hz) - f_e_hz))
        return out

    def _plan_references(
        self, primary: _Pick, partners: Sequence[_Pick], lo: float, hi: float, f_e: float
    ) -> list[_RefPlan]:
        """Reference calibrations due this round, one per pair at most; all channels of
        one carrier share its rate."""
        cfg = self._config
        interval_ns = int(float(cfg.processing.calibration.interval_s) * _NS)
        now = self._clock()
        plans: list[_RefPlan] = []
        for b in partners:
            carriers = self._references_for(primary, b, lo, hi, f_e)
            if not carriers:
                continue
            carrier = carriers[0]
            last = self._cal_attempts.get((self._cal_group(primary, b), carrier.id))
            if last is not None and now - last < interval_ns:
                continue
            bandwidth = float(carrier.emitter.bandwidth_hz) if carrier.emitter.bandwidth_hz > 0 else float(carrier.bandwidth_hz)
            plans.append(
                _RefPlan(
                    a=primary,
                    b=b,
                    carrier_id=carrier.id,
                    f_ref_hz=float(carrier.uplink_center_hz),
                    bandwidth_hz=max(bandwidth, _MIN_SIGNAL_BW_HZ),
                    x_ref=np.asarray(cfg.carrier_xyz(carrier.id), dtype=np.float64),
                )
            )
        capture_cfg = cfg.processing.capture
        for carrier_id in {p.carrier_id for p in plans}:
            group = [p for p in plans if p.carrier_id == carrier_id]
            limit = min([primary.max_rate_hz] + [p.b.max_rate_hz for p in group])
            want = max(float(capture_cfg.min_sample_rate_hz), float(capture_cfg.oversample) * group[0].bandwidth_hz)
            fs = float(math.floor(min(math.ceil(want), limit)))
            for plan in group:
                plan.fs = fs
        return [p for p in plans if p.fs > 0]

    def _calibration_for(
        self, a: PathSpec, b: PathSpec, kind: str, t_ns: int, a_tp: str, b_tp: str
    ) -> tuple[float, CalRef] | None:
        """The valid calibration of a pair and kind at ``t_ns`` as ``(c, CalRef)`` in the
        pair's own orientation (sign-flipped from a reversed measurement), or None. It
        must have been measured on a carrier both of the pair's transponders relay."""
        max_age_ns = int(float(self._config.processing.calibration.max_age_s) * _NS)
        best: tuple[int, float, CalRef] | None = None
        for key, sign, tps in (
            ((a.sat_id, a.site_id, b.sat_id, b.site_id, kind), 1.0, (a_tp, b_tp)),
            ((b.sat_id, b.site_id, a.sat_id, a.site_id, kind), -1.0, (b_tp, a_tp)),
        ):
            try:
                first = self._config.satellite(key[0]).transponder(tps[0])
                second = self._config.satellite(key[2]).transponder(tps[1])
            except ValueError:
                continue
            for (held, _carrier), entry in self._calibrations.items():
                if held != key or abs(t_ns - entry.t_ns) > max_age_ns:
                    continue
                if not (first.relays(entry.f_ref_hz) and second.relays(entry.f_ref_hz)):
                    continue
                if best is None or entry.t_ns > best[0]:
                    best = (entry.t_ns, sign * entry.correction, entry.cal)
        return None if best is None else (best[1], best[2])

    # ------------------------------------------------------------------ windows

    def _eph_pads(self, sat_a: str, sat_b: str, f_hz: float) -> tuple[float, float]:
        """3-sigma ephemeris allowance of a pair's TDOA (s) and FDOA (Hz)."""
        unc = self._uncertainty
        zero = np.zeros(3)
        pos = sum(float(np.linalg.norm(unc.sat_pos_ric_m.get(s, zero))) for s in {sat_a, sat_b})
        vel = sum(float(np.linalg.norm(unc.sat_vel_ric_mps.get(s, zero))) for s in {sat_a, sat_b})
        return 6.0 * pos / C, 6.0 * abs(f_hz) * vel / C

    def _search(self, geo: Windows, a: PathSpec, b: PathSpec, corrections: tuple[float | None, float | None]) -> Windows:
        """CAF windows (z space) from geometric extremes: shifted by valid calibrations
        with the fixed margins, otherwise widened by the systematics and the ephemeris
        allowance."""
        pad_t, pad_f = self._eph_pads(a.sat_id, b.sat_id, a.f_e_hz)
        (t_lo, t_hi), (f_lo, f_hi) = geo
        if corrections[0] is None:
            t_lo, t_hi = t_lo - pad_t, t_hi + pad_t
        if corrections[1] is None:
            f_lo, f_hi = f_lo - pad_f, f_hi + pad_f
        return search_windows(
            ((t_lo, t_hi), (f_lo, f_hi)), a, b, self._config.processing.caf, self._uncertainty, corrections
        )

    @staticmethod
    def _rate_bounds(kin: Mapping[str, _Kinematics], sat_a: str, sat_b: str, f_hz: float) -> tuple[float, float]:
        """Largest |d TDOA / dt| (s/s) and |d FDOA / dt| (Hz/s) the satellites' motion
        allows (uplink and downlink leg each bounded by the satellite's speed and
        acceleration)."""
        ka, kb = kin.get(sat_a), kin.get(sat_b)
        if ka is None or kb is None:
            return math.inf, math.inf
        tdoa_rate = 2.0 * (ka.speed_mps + kb.speed_mps) / C
        fdoa_rate = 2.0 * abs(f_hz) * (ka.accel_mps2 + kb.accel_mps2) / C
        return RATE_SAFETY * tdoa_rate, RATE_SAFETY * fdoa_rate

    def _heard(
        self, elevation: Mapping[tuple[str, str], float], a: _Pick, b: _Pick, f_hz: float, width_hz: float,
        fc_a: float, fc_b: float, fs: float,
    ) -> list[tuple[str, float]]:
        """Satellites whose copy of the signal reaches both channels: above both sites'
        horizons, relaying ``f_hz``, with the downlink band inside both capture bands."""
        out: list[tuple[str, float]] = []
        for satellite in self._config.satellites:
            if elevation.get((a.site_id, satellite.id), -90.0) <= 0.0:
                continue
            if elevation.get((b.site_id, satellite.id), -90.0) <= 0.0:
                continue
            for transponder in satellite.transponders:
                if not transponder.relays(f_hz):
                    continue
                f_down = f_hz + float(transponder.translation_hz)
                if _in_band(f_down, width_hz, fc_a, fs) and _in_band(f_down, width_hz, fc_b, fs):
                    out.append((satellite.id, float(transponder.translation_hz)))
                    break
        return out

    # ------------------------------------------------------------------ captures

    async def _capture(
        self, channel_id: str, t_start_ns: int, n: int, fs: float, fc: float, deadline_ns: int
    ) -> _CaptureOutcome:
        """One ``hub.capture``; failures become an outcome (raises only on cancellation)."""
        self._count("captures_requested")
        self._captures_in_flight += 1
        try:
            capture = await self._hub.capture(channel_id, t_start_ns, n, fs, fc, deadline_ns)
        except CaptureFailed as exc:
            retryable = exc.code not in NON_RETRYABLE_CODES
            self._count(f"capture_failed_{exc.code}")
            (logger.info if retryable else logger.warning)(
                "capture on %s failed (%s, %s): %s",
                channel_id,
                exc.code,
                "retried at the next round" if retryable else "not retried",
                exc.message,
                extra={"channel_id": channel_id, "code": exc.code},
            )
            return _CaptureOutcome(channel_id, None, exc.code, exc.message, retryable)
        except ValueError as exc:
            self._count("capture_invalid")
            logger.error("capture request on %s refused: %s", channel_id, exc, extra={"channel_id": channel_id})
            return _CaptureOutcome(channel_id, None, "invalid_request", str(exc), False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # anything else from the hub: logged, retried next round
            self._count("capture_errors")
            logger.exception("capture on %s raised", channel_id, extra={"channel_id": channel_id})
            return _CaptureOutcome(channel_id, None, "error", str(exc), True)
        finally:
            self._captures_in_flight -= 1
        iq = getattr(capture, "iq", None)
        if iq is None or np.ndim(iq) != 1 or np.shape(iq)[0] != n:
            self._count("capture_bad_data")
            logger.warning("capture on %s returned the wrong number of samples", channel_id)
            return _CaptureOutcome(channel_id, None, "bad_data", f"expected {n} samples", True)
        self._count("captures_completed")
        return _CaptureOutcome(channel_id, capture)

    def _save_capture(self, event_id: str, outcome: _CaptureOutcome) -> None:
        capture, store = outcome.capture, self._store
        if capture is None or store is None:
            return

        async def save() -> None:
            try:
                await store.save_capture(
                    capture.iq,
                    t_first_ns=int(capture.t_first_ns),
                    sample_rate_hz=float(capture.sample_rate_hz),
                    center_hz=float(capture.center_hz),
                    channel_id=outcome.channel_id,
                    capture_id=str(getattr(capture, "capture_id", "") or f"t{capture.t_first_ns}"),
                    event_id=event_id,
                    extra={
                        "simulated": bool(self._simulated()),
                        "clock_locked": bool(getattr(capture, "clock_locked", True)),
                        "overflow": bool(getattr(capture, "overflow", False)),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed write is logged; tasking goes on
                self._count("capture_save_errors")
                logger.warning("capture of %s not saved: %s", outcome.channel_id, exc, extra={"event_id": event_id})

        task = asyncio.get_running_loop().create_task(save(), name=f"petal-save-{outcome.channel_id}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    def _capture_problem(cap_a: Any, cap_b: Any, fs: float) -> str | None:
        """Why a pair of captures cannot be correlated, or None."""
        for cap in (cap_a, cap_b):
            if abs(float(cap.sample_rate_hz) - fs) > _RATE_RTOL * fs:
                return REASON_RATE_MISMATCH
        if bool(getattr(cap_a, "overflow", False)) or bool(getattr(cap_b, "overflow", False)):
            return REASON_OVERFLOW
        return None

    # ------------------------------------------------------------------ CAF preparation

    def _caf_job(
        self,
        a: _Pick,
        b: _Pick,
        cap_a: Any,
        cap_b: Any,
        f_hz: float,
        width_hz: float,
        windows: Windows,
        mirror: Windows | None,
        mirror_paths: tuple[PathSpec | None, PathSpec | None],
        elevation: Mapping[tuple[str, str], float],
        signal_bw_hz: float,
    ) -> tuple[_CafJob, PathSpec, PathSpec, int, float, bool]:
        """The CAF job of a pair of captures and what the rows need: ``(job, path_a,
        path_b, t_mid_ns, shift_hz, tdoa_blocked)``.

        Paths carry the centres the nodes used (``f_e = fc - T``); the Doppler windows
        move from the measurement space into the CAF's raw space by ``-shift_hz``
        (MATH.md section 5: ``z_fdoa = nu_peak + (fc_A - T_A) - (fc_B - T_B)``)."""
        fs = float(cap_b.sample_rate_hz)
        fc_a, fc_b = float(cap_a.center_hz), float(cap_b.center_hz)
        t_a, t_b = int(cap_a.t_first_ns), int(cap_b.t_first_ns)
        n_b = int(np.shape(cap_b.iq)[0])
        t_mid_ns = t_b + int(round(1e9 * (n_b - 1) / (2.0 * fs)))
        path_a = _path(a, fc_a - a.translation_hz)
        path_b = _path(b, fc_b - b.translation_hz)
        shift = (fc_a - a.translation_hz) - (fc_b - b.translation_hz)
        mirror_raw: Windows | None = None
        ma, mb = mirror_paths
        if mirror is not None and ma is not None and mb is not None:
            offset = (ma.f_tune_hz - fc_a) - (mb.f_tune_hz - fc_b)
            mirror_raw = (mirror[0], (mirror[1][0] + offset, mirror[1][1] + offset))
        locked = bool(getattr(cap_a, "clock_locked", True)) and bool(getattr(cap_b, "clock_locked", True))
        tdoa_blocked = not locked and a.site_id != b.site_id
        job = _CafJob(
            ya=cap_a.iq,
            yb=cap_b.iq,
            fs=fs,
            t0_offset_s=(t_a - t_b) * 1e-9,
            t_mid_s=t_mid_ns / 1e9,
            f_e_hz=f_hz,
            path_a=path_a,
            path_b=path_b,
            heard=tuple(self._heard(elevation, a, b, f_hz, width_hz, fc_a, fc_b, fs)),
            direct_point=a.site_id == b.site_id and a.translation_hz == b.translation_hz,
            delay_window=windows[0],
            doppler_window=(windows[1][0] - shift, windows[1][1] - shift),
            mirror_window=mirror_raw,
            signal_bw_hz=signal_bw_hz,
            min_snr_db=float(self._config.processing.caf.min_snr_db),
            max_tdoa_sigma_s=float(self._config.processing.caf.max_tdoa_sigma_s),
            orbits=self._orbits,
            sites_xyz=self._sites_xyz,
        )
        return job, path_a, path_b, t_mid_ns, shift, tdoa_blocked

    # ------------------------------------------------------------------ the round

    async def _round(self, state: _EventState) -> RoundReport:
        started = self._clock()
        report = RoundReport(state.event_id, started)
        generation = self._generation
        record = self._book.resolve(state.event_id)
        if record is None or not record.active or not record.mapped:
            report.skipped = "event not active"
            return report
        event_id = record.id
        cfg = self._config
        capture_cfg = cfg.processing.capture
        solver_cfg = cfg.processing.solver

        lo, hi = float(record.lo_hz), float(record.hi_hz)
        width = max(hi - lo, 0.0)
        f_e = float(round(0.5 * (lo + hi)))
        signal_bw = max(width, _MIN_SIGNAL_BW_HZ)

        # ---- plan --------------------------------------------------------------------------
        quality = await self._quality_table()
        self._abort_if_stale(generation)
        primary, partners, notes = self._choose_channels(record, state, quality, f_e)
        report.notes.extend(notes)
        if primary is None:
            report.skipped = "no usable channel sees the event" + (f" ({'; '.join(notes)})" if notes else "")
            state.state, state.message = "idle", "No channel that sees the event can be tasked now."
            self._publish_status(state)
            return report
        need_class = self._needs_classification(record, state)
        if not partners and not need_class:
            report.skipped = "no second satellite relays the event" + (f" ({'; '.join(notes)})" if notes else "")
            state.state = "idle"
            state.message = (
                "No second satellite with a usable channel relays this frequency, so the emitter cannot be located."
            )
            self._publish_status(state)
            return report

        base_duration = float(capture_cfg.duration_s)
        duration = float(state.duration_s) if state.duration_s else base_duration
        max_rate = min([primary.max_rate_hz] + [p.max_rate_hz for p in partners])
        fs_guess = float(min(max(float(capture_cfg.min_sample_rate_hz), float(capture_cfg.oversample) * width), max_rate))
        ref_plans = (
            self._plan_references(primary, partners, lo, hi, f_e) if cfg.processing.calibration.enabled else []
        )
        lead_s = self._lead_s([primary, *partners])
        t_mid_est_s = (self._clock() + int((lead_s + 0.5 * duration) * _NS)) / 1e9
        geo = await self._pool.run(
            _geometry,
            _GeometryJob(
                pairs=tuple(_GeoIn(primary, b, f_e, width, fs_guess) for b in partners),
                refs=tuple(_GeoIn(p.a, p.b, p.f_ref_hz, p.bandwidth_hz, p.fs, p.x_ref) for p in ref_plans),
                t_mid_s=t_mid_est_s,
                t_ref_s=t_mid_est_s + duration + SLOT_GAP_S,
                orbits=self._orbits,
                sites_xyz=self._sites_xyz,
                region=cfg.processing.region,
                min_elevation_deg=float(solver_cfg.min_elevation_deg),
                height_m=float(solver_cfg.height_m),
                sat_ids=tuple(s.id for s in cfg.satellites),
                site_ids=tuple(sorted({p.site_id for p in [primary, *partners]})),
            ),
        )
        self._abort_if_stale(generation)
        for plan, rg in zip(ref_plans, geo.refs):
            plan.geo = rg
        ref_plans = [p for p in ref_plans if p.geo is not None and p.geo.windows is not None]

        # Search windows per pair (measurement space). A partner without an admissible
        # emitter position for its pair is dropped before anything is captured.
        t_mid_est_ns = int(t_mid_est_s * _NS)
        kept: list[tuple[_Pick, _GeoOut, Windows, Windows | None, tuple[float, float]]] = []
        max_doppler = 0.0
        for b, pg in zip(partners, geo.pairs):
            if pg.windows is None:
                report.notes.append(f"{b.channel_id} not paired: {pg.error}")
                report.pairs.append(PairOutcome(primary.channel_id, b.channel_id, "skipped", pg.error))
                continue
            cal_t = self._calibration_for(pg.a_nom, pg.b_nom, "tdoa", t_mid_est_ns, primary.transponder_id, b.transponder_id)
            cal_f = self._calibration_for(pg.a_nom, pg.b_nom, "fdoa", t_mid_est_ns, primary.transponder_id, b.transponder_id)
            windows = self._search(pg.windows, pg.a_nom, pg.b_nom, (cal_t[0] if cal_t else None, cal_f[0] if cal_f else None))
            mirror = None
            if pg.mirror is not None and pg.mirror_a is not None and pg.mirror_b is not None:
                mirror = self._search(pg.mirror, pg.mirror_a, pg.mirror_b, (None, None))
            rates = self._rate_bounds(geo.kinematics, primary.sat_id, b.sat_id, f_e)
            kept.append((b, pg, windows, mirror, rates))
            max_doppler = max(max_doppler, abs(windows[1][0]), abs(windows[1][1]))
            if mirror is not None:
                max_doppler = max(max_doppler, abs(mirror[1][0]), abs(mirror[1][1]))
        partners = [k[0] for k in kept]
        ref_plans = [p for p in ref_plans if p.b in partners]
        if not partners and not need_class:
            report.skipped = "no pair has an admissible emitter position"
            state.state, state.message = "idle", "No pair of satellites can see a common emitter position in the region."
            self._publish_status(state)
            return report
        report.primary_channel_id = primary.channel_id
        report.partner_channel_ids = [p.channel_id for p in partners]

        # ---- rate, length, timing -----------------------------------------------------------
        max_rate = min([primary.max_rate_hz] + [p.max_rate_hz for p in partners])
        fs_want = max(
            float(capture_cfg.min_sample_rate_hz),
            float(capture_cfg.oversample) * width,
            _DOPPLER_FS_FACTOR * max_doppler,
        )
        fs = float(math.floor(min(math.ceil(fs_want), max_rate)))
        if not fs > 0:
            report.skipped = "no usable sample rate"
            return report
        if fs < fs_want:
            report.notes.append(f"sample rate capped at {fs:.0f} Hz by the channels (wanted {fs_want:.0f} Hz)")
        noise_bw = min(fs, 1.05 * (signal_bw + 2.0 * max_doppler))
        if state.duration_s is None:
            for _, _, _, _, (rt, rf) in kept:  # CAF drift assumption (MATH.md section 6)
                if math.isfinite(rt) and rt > 0:
                    duration = min(duration, 0.5 / (noise_bw * rt))
                if math.isfinite(rf) and rf > 0:
                    duration = min(duration, 1.0 / math.sqrt(rf))
            if duration < base_duration:
                report.notes.append(f"capture shortened to {duration:.3f} s by the predicted dynamics")
        n = int(min(round(duration * fs), int(capture_cfg.max_samples)))
        for _, _, windows, mirror, _ in kept:
            n = max(n, _samples_for_window(windows[0], fs, noise_bw))
            if mirror is not None:
                n = max(n, _samples_for_window(mirror[0], fs, noise_bw))
        if n > int(capture_cfg.max_samples):
            report.notes.append(f"a delay window needs {n} samples; capture.max_samples is {capture_cfg.max_samples}")
            n = int(capture_cfg.max_samples)
        n = max(n, _MIN_SAMPLES)

        lead_s = self._lead_s([primary, *partners])
        t_start = self._clock() + int(round(lead_s * _NS))
        target_end = t_start + int(math.ceil(n / fs * _NS))
        timeout_ns = int(float(capture_cfg.timeout_s) * _NS)
        report.t_start_ns, report.sample_rate_hz, report.n_samples = t_start, fs, n

        # reference slots: one per carrier, after the target captures, all channels of a
        # carrier with the same rate and length
        cursor = target_end + int(SLOT_GAP_S * _NS)
        for carrier_id in dict.fromkeys(p.carrier_id for p in ref_plans):
            group = [p for p in ref_plans if p.carrier_id == carrier_id]
            fs_ref = group[0].fs
            n_ref = int(min(round(base_duration * fs_ref), int(capture_cfg.max_samples)))
            for plan in group:
                assert plan.geo is not None and plan.geo.windows is not None
                ref_windows = self._search(plan.geo.windows, plan.geo.a_nom, plan.geo.b_nom, (None, None))
                ref_noise_bw = min(fs_ref, 1.05 * (plan.bandwidth_hz + 2.0 * max(abs(ref_windows[1][0]), abs(ref_windows[1][1]))))
                n_ref = max(n_ref, _samples_for_window(ref_windows[0], fs_ref, ref_noise_bw))
            n_ref = max(min(n_ref, int(capture_cfg.max_samples)), _MIN_SAMPLES)
            for plan in group:
                plan.n, plan.t_start_ns = n_ref, cursor
            cursor += int(math.ceil(n_ref / fs_ref * _NS)) + int(SLOT_GAP_S * _NS)

        # ---- capture ------------------------------------------------------------------------
        self._abort_if_stale(generation)
        self._book.mark_characterizing(event_id, self._clock())
        now = self._clock()
        for plan in ref_plans:
            self._cal_attempts[(self._cal_group(plan.a, plan.b), plan.carrier_id)] = now
        state.rounds += 1
        state.last_round_ns = started
        state.channels = tuple([primary.channel_id] + [p.channel_id for p in partners])
        state.state = "capturing"  # the last round's message stays until this round has its own
        self._publish_status(state)

        requests: dict[str, Any] = {}
        for pick in [primary, *partners]:
            if pick.channel_id not in requests:
                requests[pick.channel_id] = self._capture(
                    pick.channel_id, t_start, n, fs, f_e + pick.translation_hz, target_end + timeout_ns
                )
        for plan in ref_plans:
            for pick in (plan.a, plan.b):
                key = f"ref:{pick.channel_id}:{plan.carrier_id}"
                if key not in requests:
                    end = plan.t_start_ns + int(math.ceil(plan.n / plan.fs * _NS))
                    requests[key] = self._capture(
                        pick.channel_id, plan.t_start_ns, plan.n, plan.fs, plan.f_ref_hz + pick.translation_hz,
                        end + timeout_ns,
                    )
        names = list(requests)
        outcomes = dict(zip(names, await asyncio.gather(*(requests[k] for k in names))))
        self._abort_if_stale(generation)
        for name, outcome in outcomes.items():
            if outcome.capture is None:
                report.failures[name] = (outcome.code or "error", outcome.message or "")
                if not outcome.retryable and not name.startswith("ref:"):
                    state.excluded[outcome.channel_id] = f"{outcome.code}: {outcome.message}"
            elif self._save and self._store is not None:
                self._save_capture(event_id, outcome)
        state.state = "processing"
        primary_out = outcomes[primary.channel_id]

        # ---- classification and references ----------------------------------------------------
        jobs: list[Any] = []
        if need_class and primary_out.capture is not None:
            cap = primary_out.capture
            offset = f_e + primary.translation_hz - float(cap.center_hz)  # the event within the capture
            jobs.append(self._pool.run(_classify_event, cap.iq, float(cap.sample_rate_hz), offset, signal_bw))
        ref_jobs: list[tuple[_RefPlan, _RefJob]] = []
        for plan in ref_plans:
            job = self._reference_job(plan, outcomes, geo.elevation, report)
            if job is not None:
                ref_jobs.append((plan, job))
                jobs.append(self._pool.run(_calibrate, job, self._orbits, self._sites_xyz))
        results = list(await asyncio.gather(*jobs, return_exceptions=True)) if jobs else []
        self._abort_if_stale(generation)
        if need_class and primary_out.capture is not None:
            self._apply_classification(event_id, state, results.pop(0), primary_out, report)
        for (plan, job), result in zip(ref_jobs, results):
            self._apply_reference(event_id, plan, job, result, report)

        # ---- target pairs -------------------------------------------------------------------
        pair_jobs: list[Any] = []
        contexts: list[_PairCtx] = []
        for b, pg, windows, mirror, rates in kept:
            outcome = PairOutcome(primary.channel_id, b.channel_id, "skipped")
            report.pairs.append(outcome)
            pair_ref = _pair_ref(primary, b)
            out_a, out_b = primary_out, outcomes[b.channel_id]
            failed = next((o for o in (out_a, out_b) if o.capture is None), None)
            if failed is not None:
                outcome.status = "failed"
                outcome.reason = f"{REASON_CAPTURE_FAILED}: {failed.code}: {failed.message}"
                if not failed.retryable:
                    self._record_rejection(event_id, pair_ref, self._clock(), outcome)
                continue
            problem = self._capture_problem(out_a.capture, out_b.capture, fs)
            if problem is not None:
                outcome.status, outcome.reason = "rejected", problem
                self._record_rejection(event_id, pair_ref, int(out_b.capture.t_first_ns), outcome)
                continue
            # calibrations made this round apply to the windows when none was valid before
            cal_t = self._calibration_for(pg.a_nom, pg.b_nom, "tdoa", t_mid_est_ns, primary.transponder_id, b.transponder_id)
            cal_f = self._calibration_for(pg.a_nom, pg.b_nom, "fdoa", t_mid_est_ns, primary.transponder_id, b.transponder_id)
            assert pg.windows is not None
            windows = self._search(pg.windows, pg.a_nom, pg.b_nom, (cal_t[0] if cal_t else None, cal_f[0] if cal_f else None))
            job, path_a, path_b, t_mid_ns, shift, blocked = self._caf_job(
                primary, b, out_a.capture, out_b.capture, f_e, width, windows, mirror,
                (pg.mirror_a, pg.mirror_b), geo.elevation, signal_bw,
            )
            outcome.t_mid_ns = t_mid_ns
            contexts.append(_PairCtx(primary, b, pair_ref, path_a, path_b, t_mid_ns, shift, blocked, rates, outcome))
            pair_jobs.append(self._pool.run(_correlate, job))
        pair_results = list(await asyncio.gather(*pair_jobs, return_exceptions=True)) if pair_jobs else []
        self._abort_if_stale(generation)

        new_used = False
        display: tuple[float, CafResult, schemas.PairRef, int] | None = None
        for ctx, result in zip(contexts, pair_results):
            if isinstance(result, BaseException):
                if isinstance(result, ValueError):
                    ctx.outcome.status, ctx.outcome.reason = "rejected", f"{REASON_WINDOW}: {result}"
                    self._record_rejection(event_id, ctx.pair_ref, ctx.t_mid_ns, ctx.outcome)
                else:
                    ctx.outcome.status, ctx.outcome.reason = "failed", f"correlation error: {result}"
                    self._count("correlation_errors")
                    logger.error(
                        "correlation of %s / %s failed: %s",
                        ctx.a.channel_id,
                        ctx.b.channel_id,
                        result,
                        exc_info=result,
                        extra={"event_id": event_id},
                    )
                continue
            ctx.outcome.result = result
            new_used |= self._add_rows(event_id, record, state, ctx, result)
            snr = _finite(result.snr_db)
            rank = snr if (snr is not None and result.detected) else -math.inf
            if display is None or rank > display[0]:
                display = (rank, result, ctx.pair_ref, ctx.t_mid_ns)
        if display is not None:
            try:
                self._book.set_caf(event_id, caf_display(display[1], display[2], display[3]))
            except (ValidationError, ValueError) as exc:
                logger.warning("correlation surface of %s not shown: %s", event_id, exc, extra={"event_id": event_id})

        # ---- solve --------------------------------------------------------------------------
        if new_used or self._has_unsolved_rows(record, state):
            await self._solve_event(event_id, state, report, generation)

        failed_keys = [k for k in report.failures if not k.startswith("ref:")]
        retried = [k for k in failed_keys if k not in state.excluded]
        dropped = [k for k in failed_keys if k in state.excluded]
        parts = []
        if retried:
            parts.append(
                "Capture failed on "
                + ", ".join(f"{k} ({report.failures[k][0]})" for k in retried)
                + "; retrying at the next round."
            )
        if dropped:
            parts.append(
                "Capture refused on "
                + ", ".join(f"{k} ({report.failures[k][0]})" for k in dropped)
                + "; that channel is no longer used for this event."
            )
        explanation = round_explanation(report.pairs)
        if explanation:
            parts.append(explanation)
        if not failed_keys and any(p.reason in (REASON_UNCONFIRMED, REASON_INCONSISTENT) for p in report.pairs):
            parts.append("Waiting for a repeat detection before the new measurements are used.")
        state.message = " ".join(parts) or None
        state.state = "waiting"
        self._publish_status(state)
        return report

    # ------------------------------------------------------------------ classification

    def _apply_classification(
        self, event_id: str, state: _EventState, result: Any, capture_out: _CaptureOutcome, report: RoundReport
    ) -> None:
        """Attach a classification; schedule a longer capture for a swept waveform."""
        if isinstance(result, BaseException):
            self._count("classify_errors")
            logger.warning("classification of %s failed: %s", event_id, result, extra={"event_id": event_id})
            report.notes.append(f"classification failed: {result}")
            return
        if self._book.set_classification(event_id, result):
            state.classified_ns = self._clock()
            report.classification = str(result.label)
        if str(result.label) != "swept":
            return
        features = dict(getattr(result, "features", {}) or {})
        rate = abs(_finite(features.get("sweep_rate_hz_per_s")) or 0.0)
        bandwidth = _finite(features.get("occupied_bw_hz")) or 0.0
        if not (rate > 0.0 and bandwidth > 0.0):
            return
        period = bandwidth / rate
        capture = capture_out.capture
        length = np.shape(capture.iq)[0] / float(capture.sample_rate_hz)
        if length >= SWEEP_PERIODS * period:
            return
        wanted = SWEEP_PERIODS * period * 1.05
        max_length = int(self._config.processing.capture.max_samples) / float(capture.sample_rate_hz)
        if wanted > max_length:
            report.notes.append(
                f"a {period:.3f} s sweep needs {wanted:.1f} s of capture; capture.max_samples allows {max_length:.1f} s"
            )
            wanted = max_length
        if wanted <= length:
            return
        state.duration_s = wanted
        state.next_due_ns = self._clock()
        report.notes.append(f"swept waveform ({period * 1e3:.1f} ms period): captured again over {wanted:.2f} s")
        logger.info(
            "event %s: swept waveform, period %.4f s; the next capture is %.3f s long",
            event_id,
            period,
            wanted,
            extra={"event_id": event_id},
        )

    # ------------------------------------------------------------------ references

    def _reference_job(
        self,
        plan: _RefPlan,
        outcomes: Mapping[str, _CaptureOutcome],
        elevation: Mapping[tuple[str, str], float],
        report: RoundReport,
    ) -> _RefJob | None:
        out_a = outcomes.get(f"ref:{plan.a.channel_id}:{plan.carrier_id}")
        out_b = outcomes.get(f"ref:{plan.b.channel_id}:{plan.carrier_id}")
        if out_a is None or out_b is None or out_a.capture is None or out_b.capture is None:
            report.notes.append(f"calibration with {plan.carrier_id} skipped: a reference capture failed")
            return None
        problem = self._capture_problem(out_a.capture, out_b.capture, plan.fs)
        if problem is not None:
            report.notes.append(f"calibration with {plan.carrier_id} rejected: {problem}")
            return None
        geo = plan.geo
        assert geo is not None and geo.windows is not None
        windows = self._search(geo.windows, geo.a_nom, geo.b_nom, (None, None))
        mirror = None
        if geo.mirror is not None and geo.mirror_a is not None and geo.mirror_b is not None:
            mirror = self._search(geo.mirror, geo.mirror_a, geo.mirror_b, (None, None))
        job, _path_a, _path_b, t_mid_ns, shift, blocked = self._caf_job(
            plan.a, plan.b, out_a.capture, out_b.capture, plan.f_ref_hz, plan.bandwidth_hz, windows, mirror,
            (geo.mirror_a, geo.mirror_b), elevation, plan.bandwidth_hz,
        )
        seq = next(self._cal_seq)
        cal_ids = tuple(f"cal-{seq}-{kind}:{plan.carrier_id}" for kind in ("tdoa", "fdoa"))
        return _RefJob(job, plan.x_ref, shift, t_mid_ns, not blocked, (cal_ids[0], cal_ids[1]))

    def _apply_reference(self, event_id: str, plan: _RefPlan, job: _RefJob, outcome: Any, report: RoundReport) -> None:
        """Keep and store the calibrations a reference measurement gave."""
        if isinstance(outcome, BaseException):
            self._count("calibration_errors")
            report.notes.append(f"calibration with {plan.carrier_id} failed: {outcome}")
            log = logger.warning if isinstance(outcome, ValueError) else logger.error
            log(
                "calibration of %s / %s with %s failed: %s",
                plan.a.channel_id,
                plan.b.channel_id,
                plan.carrier_id,
                outcome,
                extra={"event_id": event_id, "carrier_id": plan.carrier_id},
            )
            return
        for problem in outcome.problems:
            report.notes.append(f"calibration with {plan.carrier_id}: {problem}")
        snr = _finite(outcome.result.snr_db) or 0.0
        for kind, correction, cal in outcome.corrections:
            if self._world_since_ns is not None and int(cal.t_ns) < self._world_since_ns:
                report.notes.append(f"calibration with {plan.carrier_id} measured before the simulated world changed")
                continue
            key = (cal.a.sat_id, cal.a.site_id, cal.b.sat_id, cal.b.site_id, kind)
            entry = Calibration(
                key=key,
                correction=correction,
                cal=cal,
                t_ns=int(cal.t_ns),
                carrier_id=plan.carrier_id,
                a_transponder_id=plan.a.transponder_id,
                b_transponder_id=plan.b.transponder_id,
                f_ref_hz=plan.f_ref_hz,
                snr_db=snr,
            )
            self._calibrations[(key, plan.carrier_id)] = entry
            report.calibrations.append(entry)
            self._count("calibrations")
            logger.info(
                "calibration %s %s/%s - %s/%s with %s: %.6g (sigma %.3g, %.1f dB)",
                kind,
                key[0],
                key[1],
                key[2],
                key[3],
                plan.carrier_id,
                correction,
                cal.sigma,
                snr,
                extra={"carrier_id": plan.carrier_id, "kind": kind, "cal_id": cal.cal_id},
            )
            if self._store is None:
                continue
            try:
                self._store.add_calibration(
                    t_ns=int(cal.t_ns),
                    carrier_id=plan.carrier_id,
                    kind=kind,
                    a_sat_id=key[0],
                    a_site_id=key[1],
                    b_sat_id=key[2],
                    b_site_id=key[3],
                    correction=correction,
                    sigma=float(cal.sigma),
                    detail={
                        "cal_id": cal.cal_id,
                        "event_id": event_id,
                        "a_channel_id": plan.a.channel_id,
                        "b_channel_id": plan.b.channel_id,
                        "a_transponder_id": plan.a.transponder_id,
                        "b_transponder_id": plan.b.transponder_id,
                        "snr_db": snr,
                        "simulated": bool(self._simulated()),
                    },
                )
            except Exception as exc:  # a calibration not stored is logged; it is still used
                self._count("calibration_store_errors")
                logger.warning("calibration not stored: %s", exc, extra={"carrier_id": plan.carrier_id})

    # ------------------------------------------------------------------ rows

    def _row(
        self,
        kind: str,
        t_ns: int,
        pair: schemas.PairRef,
        value: float | None,
        sigma: float | None,
        snr_db: float | None,
        calibrated: bool,
        used: bool,
        reason: str | None,
        correction: float | None = None,
        reference_carrier_id: str | None = None,
    ) -> schemas.MeasurementRow:
        return schemas.MeasurementRow(
            t_ns=int(t_ns),
            kind=kind,  # type: ignore[arg-type]
            pair=pair,
            value=_finite(value),
            sigma=_finite(sigma),
            residual=None,
            snr_db=_finite(snr_db),
            calibrated=calibrated,
            used=used,
            reason=reason,
            correction=_finite(correction) if calibrated else None,
            reference_carrier_id=reference_carrier_id if calibrated else None,
        )

    def _carrier_of(self, cal: CalRef) -> str | None:
        """The reference carrier of a calibration in force (by its ``cal_id``)."""
        for entry in self._calibrations.values():
            if entry.cal.cal_id == cal.cal_id:
                return entry.carrier_id
        return None

    def _record_rejection(self, event_id: str, pair: schemas.PairRef, t_ns: int, outcome: PairOutcome) -> None:
        """Rows of a pair that produced no measurement (both kinds, with the reason)."""
        for kind in ("tdoa", "fdoa"):
            seq = self._book.add_measurement(
                event_id, self._row(kind, t_ns, pair, None, None, None, False, False, outcome.reason)
            )
            if kind == "tdoa":
                outcome.tdoa_seq = seq
            else:
                outcome.fdoa_seq = seq

    def _confirm(
        self, state: _EventState, key: tuple[str, str], detection: _Detection, rates: tuple[float, float]
    ) -> tuple[bool, str | None]:
        """Whether a detection confirms the pair's track (see ``_consistent``)."""
        track = state.tracks.setdefault(key, _PairTrack())
        if track.confirmed is not None and _consistent(track.confirmed, detection, rates):
            track.confirmed = detection
            track.candidates.clear()
            return True, None
        for candidate in track.candidates:
            if _consistent(candidate, detection, rates):
                track.confirmed = detection
                track.candidates.clear()
                return True, None
        track.candidates.append(detection)
        return False, REASON_INCONSISTENT if track.confirmed is not None else REASON_UNCONFIRMED

    def _add_rows(
        self, event_id: str, record: EventRecord, state: _EventState, ctx: _PairCtx, result: CafResult
    ) -> bool:
        """The TDOA and FDOA rows of one correlated pair; True when one is used."""
        outcome = ctx.outcome
        tdoa = _finite(result.tdoa_s)
        fdoa = None if _finite(result.fdoa_hz) is None else float(result.fdoa_hz) + ctx.shift_hz  # type: ignore[arg-type]
        sig_t, sig_f = _finite(result.sigma_tdoa_s), _finite(result.sigma_fdoa_hz)
        common: str | None = None
        if not result.detected:
            common = REASONS.get(result.reason or "", REASON_NOT_DETECTED)
        elif result.reason in (REASON_AMBIGUOUS, REASON_COUPLED, REASON_OUTSIDE):
            common = REASONS[result.reason]
        elif result.mirror_ambiguous:
            common = REASON_MIRROR
        reason_t = reason_f = common
        if common is None:
            if tdoa is None or sig_t is None or not sig_t > 0:
                reason_t = REASONS.get(result.reason or "", REASON_NARROWBAND_CAF)
            elif ctx.tdoa_blocked:
                reason_t = REASON_CLOCK_UNLOCKED
            if fdoa is None or sig_f is None or not sig_f > 0:
                reason_f = REASONS.get(result.reason or "", REASON_NOT_DETECTED)
        usable_t, usable_f = reason_t is None, reason_f is None
        if usable_t or usable_f:
            detection = _Detection(
                ctx.t_mid_ns,
                tdoa if usable_t else None,
                sig_t if usable_t else None,
                fdoa if usable_f else None,
                sig_f if usable_f else None,
            )
            confirmed, why = self._confirm(state, (ctx.a.channel_id, ctx.b.channel_id), detection, ctx.rates)
            outcome.confirmed = confirmed
            if not confirmed:
                reason_t = why if usable_t else reason_t
                reason_f = why if usable_f else reason_f
                usable_t = usable_f = False
        used_any = False
        calibrated: list[str] = []
        for kind, value, sigma, reason, usable in (
            ("tdoa", tdoa, sig_t, reason_t, usable_t),
            ("fdoa", fdoa, sig_f, reason_f, usable_f),
        ):
            cal = self._calibration_for(
                ctx.path_a, ctx.path_b, kind, ctx.t_mid_ns, ctx.a.transponder_id, ctx.b.transponder_id
            )
            shown = value
            if cal is not None and value is not None:
                shown = value - cal[0]
                calibrated.append(kind)
            measurement = None
            if usable and value is not None and sigma is not None:
                measurement = Measurement(
                    kind=kind,  # type: ignore[arg-type]
                    value=value,
                    sigma=sigma,
                    t_ns=ctx.t_mid_ns,
                    a=ctx.path_a,
                    b=ctx.path_b,
                    correction=cal[0] if cal is not None else 0.0,
                    cal=cal[1] if cal is not None else None,
                    tag=f"{event_id}:{ctx.a.channel_id}/{ctx.b.channel_id}",
                )
            row = self._row(
                kind,
                ctx.t_mid_ns,
                ctx.pair_ref,
                shown,
                sigma,
                result.snr_db,
                cal is not None and value is not None,
                measurement is not None,
                None if measurement is not None else reason,
                correction=cal[0] if cal is not None else None,
                reference_carrier_id=self._carrier_of(cal[1]) if cal is not None else None,
            )
            seq = self._book.add_measurement(event_id, row, measurement)
            if kind == "tdoa":
                outcome.tdoa_seq = seq
            else:
                outcome.fdoa_seq = seq
            used_any |= measurement is not None and seq is not None
        outcome.calibrated = tuple(calibrated)
        if used_any:
            outcome.status, outcome.reason = "measured", None
            self._count("pairs_measured")
            if not record.caf_uplink:
                self._book.report_cross_satellite_detection(event_id, ctx.t_mid_ns, ctx.pair_ref)
        else:
            outcome.status = "rejected"
            outcome.reason = reason_t if reason_t == reason_f else f"tdoa: {reason_t}; fdoa: {reason_f}"
            self._count("pairs_rejected")
        return used_any

    # ------------------------------------------------------------------ solve

    def _solvable_entries(self, record: EventRecord) -> list[Any]:
        """Used entries whose satellites and sites exist in the configuration, newest
        ``solver.max_measurements``, oldest first."""
        sats = set(self._orbits)
        sites = set(self._sites_xyz)
        entries = []
        for entry in record.used_measurements():
            m = entry.measurement
            if m is None or m.a.sat_id not in sats or m.b.sat_id not in sats:
                continue
            if m.a.site_id not in sites or m.b.site_id not in sites:
                continue
            if m.cal is not None and (
                m.cal.a.sat_id not in sats or m.cal.b.sat_id not in sats
                or m.cal.a.site_id not in sites or m.cal.b.site_id not in sites
            ):
                continue
            entries.append(entry)
        limit = int(self._config.processing.solver.max_measurements)
        return entries[-limit:] if limit > 0 else []

    def _has_unsolved_rows(self, record: EventRecord, state: _EventState) -> bool:
        return any(e.seq > state.last_solved_seq for e in record.used_measurements())

    async def _solve_event(self, event_id: str, state: _EventState, report: RoundReport, generation: int) -> None:
        """Solve the event's accepted rows (warm start or global search) and attach the
        result."""
        record = self._book.resolve(event_id)
        if record is None or record.closed:
            return
        entries = self._solvable_entries(record)
        if len(entries) < 2:
            report.solve_reason = "fewer than two accepted measurements"
            state.solve_reason = report.solve_reason
            return
        seqs = tuple(e.seq for e in entries)
        rows = tuple(e.measurement for e in entries)
        cfg = self._config.processing
        state.solves += 1
        previous = record.solution
        use_global = previous is None or state.solves == 1 or state.solves % GLOBAL_SEARCH_EVERY == 0
        starts = None
        if not use_global:
            starts = tuple(
                [(float(previous.lat_deg), float(previous.lon_deg))]
                + [(float(a.lat_deg), float(a.lon_deg)) for a in previous.ambiguities]
            )

        def job(start_points: tuple[tuple[float, float], ...] | None) -> _SolveJob:
            return _SolveJob(rows, self._orbits, self._sites_xyz, cfg.solver, cfg.region, self._uncertainty, start_points)

        solution, reason = await self._pool.run(_solve, job(starts))
        self._abort_if_stale(generation)
        if starts is not None and (
            solution is None or (solution.reduced_chi2 is not None and solution.reduced_chi2 > WARM_RCHI2_LIMIT)
        ):
            self._count("solves_global_retry")
            global_solution, global_reason = await self._pool.run(_solve, job(None))
            self._abort_if_stale(generation)
            if global_solution is not None or solution is None:
                solution, reason = global_solution, global_reason
        self._count("solves")
        t_ns = self._clock()
        try:
            self._book.set_solution(event_id, solution, t_ns, used=seqs if solution is not None else (), reason=reason)
        except ValueError as exc:
            self._count("solve_errors")
            logger.error("solution of %s not attached: %s", event_id, exc, extra={"event_id": event_id})
            reason = f"solution not attached: {exc}"
            solution = None
        state.last_solved_seq = max(seqs)
        state.solve_reason = None if solution is not None else reason
        report.solution, report.solve_reason = solution, reason
        if solution is None:
            self._count("solves_failed")
            logger.info("event %s: no position (%s)", event_id, reason, extra={"event_id": event_id, "reason": reason})
        else:
            logger.info(
                "event %s: %.4f, %.4f, 95 %% semi-major %.0f m from %d rows",
                event_id,
                solution.lat_deg,
                solution.lon_deg,
                solution.ellipse["semi_major_m"],
                solution.n_measurements,
                extra={"event_id": event_id, "n_measurements": solution.n_measurements},
            )
