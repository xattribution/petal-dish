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
   the same channels (one radio, one tuning at a time). What is left of
   ``CLOCK_CHECKS_PER_ROUND`` reference pairs goes to same-satellite *clock checks*
   (``_plan_clock_checks``): one satellite's copy of a reference at the primary's site
   and at another site, whose correction is the two clocks' difference (the
   satellite's own error is common to both), so ``site_clock_offsets`` can tell a late
   clock from a satellite position error.
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
   the rows carry ``REASON_UNCONFIRMED``. **Another transmitter** (K2): the round's
   confirmations are decided together (``Tasking._track_decisions``). A pair whose new
   detections repeat at a TDOA/FDOA its confirmed track cannot reach has *jumped*; with
   no pair of the round agreeing with its own track, another transmitter (or the same
   one, moved) holds the frequency and ``EventBook.transmission_changed`` sets the old
   rows and position aside (the jumped pairs' new tracks are used at once). While an
   event is re-checked (``EventRecord.recheck``: back after a short break, or its
   waveform class, level by ``POWER_STEP_DB`` or width by ``WIDTH_STEP_RATIO`` changed),
   one agreeing pair clears the re-check and one disagreeing detection with none
   agreeing sets everything aside.
7. **Classify** the primary capture (``core.detect.classify`` in a worker thread)
   over the event's own band (``CLASSIFY_BAND_FACTOR`` times its width, so that a
   neighbouring carrier inside a minimum-rate capture is not classified in its place);
   a swept waveform is captured again over at least ``SWEEP_PERIODS`` sweep periods.
8. **Solve** the newest ``solver.max_measurements`` accepted rows (worker thread):
   the global search on the first solve, every ``GLOBAL_SEARCH_EVERY``-th solve and
   whenever a warm-started solve fails or has ``reduced_chi2 > WARM_RCHI2_LIMIT``;
   warm starts from the previous solution and its ambiguities. The consider uncertainty
   is the configured one, except that a satellite the clock checks put wrong beyond its
   configured position sigma has that sigma widened by a scale sized from the
   calibrations (*ephemeris allowance*, ``_update_allowance``, MATH.md section 9),
   from the solve after the round that sized it. The result goes to
   ``EventBook.set_solution`` with ``solver.max_reduced_chi2`` and the region's fix
   bound (``events.fix_semi_major_limit_m``): solution history, residuals, ``located``
   only for a fix (``SolutionOut.is_fix``: consistent, ``dof >= 1``, the ellipse within
   the bound); an inconsistent one is published flagged, its ellipse enlarged by
   sqrt(reduced chi-square), one that is otherwise not a fix with its
   ``not_fix_reason``, and the status message says so; the strongest correlation
   surface of the round to ``set_caf``; the class to ``set_classification``. When rows
   added to a consistent fit with redundancy break it (no solution, or an inconsistent
   one) and the rows added since (at least ``SPLIT_MIN_ROWS``) fit one transmitter by
   themselves, the earlier rows are set aside as another transmission's and the new
   rows' fix is attached (``Tasking._split_test``). When a solve over a superset of the
   published position's rows finds no position at all (and no split explains it), the
   position is withdrawn (``EventBook.withdraw_solution``). The event moves
   ``characterizing`` (first capture requested) -> ``locating`` (first row used) ->
   ``located``.

Satellites. A round's geometry evaluates only the satellites it uses (the picks and
every satellite relaying the event's range or a planned reference), each on its own:
one whose orbit cannot be evaluated (an ephemeris table that ended) only fails the pairs
through it, and the status message names it. Once the event has a consistent fix, a
satellite that cannot see any point of its ellipse (``EventRecord.
not_visible_satellite_ids``, worked out by the correlator) is not paired, and the
reference carrier nearest the fix is calibrated against (nearest in frequency before a
fix). ``site_clock_estimates`` turns the calibrations held (the clock checks included)
into per-site clock offsets for ``/status``; the satellites it names as mispositioned
are the event's ``doubted_satellite_ids``, and ``satellite_position_evidence`` reads the
same calibrations as evidence about each satellite's position error, which sizes the
ephemeris allowance (``ephemeris_scale``, ``ephemeris_allowance``).

Everything runs on the event loop except the numerical jobs, which go through the
server's ``WorkPool`` (at most 3 at once). Rounds are cancelled when their event is
closed (``EventBook`` listener), when the configuration is replaced and on ``stop``;
a node that disconnects mid-round only costs that round's pairs. Absolute times are
integer UTC nanoseconds throughout; float seconds only for geometry.

Deviations from the documents:

- Search windows of a kind without a valid calibration are also widened by a
  3-sigma ephemeris allowance on both legs (``6 |sigma_pos| / c`` per satellite in
  delay, ``6 f |sigma_vel| / c`` in Doppler, ``Tasking._eph_pads``): MATH.md section 6
  lists only clocks, bias and oscillators, so a reference carrier (a single point)
  would otherwise be searched in a window narrower than the ephemeris error.
- The sample rate also covers the Doppler window (``fs >= 2.2 max |doppler|``,
  required by ``caf_estimate``; matters in low orbit); the base duration is
  shortened when bounded dynamics would break the CAF's drift assumption, and the
  capture is lengthened when a delay window (plus the estimator's guard) needs it.
- "Consistent within a few sigma" for the repeat detection adds the largest change
  of TDOA/FDOA the satellites' motion allows over the interval (a GEO pair's TDOA
  moves by microseconds in 5 s, against sigmas of a fraction of one). Each leg's range
  rate is bounded by the satellite's speed and its range acceleration by
  ``|a| + |v|^2 / r_min`` (the geometric term dominates in low orbit; ``_Kinematics``),
  the downlink legs weighted by their own frequency (``Tasking._rate_bounds``).
- A reference carrier whose band overlaps the event's uplink range is not used.
- A partner channel with no admissible emitter position for the pair is not captured.
- Partner diversity: from the third round on, every other round replaces a partner by
  another candidate within ``ROTATE_WITHIN_DB`` of the best quality, in turn
  (``Tasking._rotate``). Downlink qualities of a network's channels typically differ by
  fractions of a dB, so the documented choice alone always used the same sites; a
  fault at an unused site (a late clock) was invisible and nothing averaged over sites.
- The status message explains a round whose pairs all failed for one reason of the
  signal itself (``round_explanation``: no TDOA on a narrowband signal, ambiguous
  correlations of a periodic waveform, coupling, no copy found); a waveform that cannot
  be located at all (ambiguous or coupled correlations with no row accepted, or one
  relaying satellite) also sets ``TaskingStatus.unlocatable_reason``; an event judged
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
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

import numpy as np
import numpy.typing as npt
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
from petal_locate.core.geodesy import geodesic_distance_m
from petal_locate.core.detect import MIN_CLASSIFY_SAMPLES, classify
from petal_locate.core.geometry import elevation_deg, measurement_windows, predict_pair, search_windows, self_peak
from petal_locate.core.orbits import orbit_position
from petal_locate.core.media import row_media_partials
from petal_locate.core.solver import Solution, calibration_correction, reference_position_partials, solve
from petal_locate.core.types import CalRef, Measurement, PathSpec, Uncertainty
from petal_locate.server.api import schemas
from petal_locate.server.hub import CaptureFailed
from petal_locate.server.pipeline.correlator import QualityTable
from petal_locate.server.pipeline.events import (
    EphemerisAllowance,
    EventBook,
    EventNotFound,
    EventRecord,
    TruthEmitter,
    Unit,
    assess_fit,
    caf_display,
    fix_semi_major_limit_m,
)

__all__ = [
    "CLOCK_CAUTION_NS",
    "CLOCK_CHECKS_PER_ROUND",
    "CONFIRM_SIGMAS",
    "DOUBT_MARGIN_SIGMAS",
    "DOUBT_MIN_SCALE",
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
    "SCALE_STEPS_PER_OCTAVE",
    "SWEEP_PERIODS",
    "WARM_RCHI2_LIMIT",
    "Calibration",
    "CaptureHub",
    "ClockEdge",
    "EvidenceEdge",
    "PairOutcome",
    "PositionEvidence",
    "RoundReport",
    "Tasking",
    "ephemeris_scale",
    "satellite_position_evidence",
    "site_clock_offsets",
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

RECHECK_WAIT_ROUNDS: Final[int] = 3
"""Rounds a re-check waits for a pair measured before the change while only pairs first
detected since agree; then it is cleared, saying that the change could not be tested."""

POWER_STEP_DB: Final[float] = 6.0
"""A change of the primary sighting's SNR on the same channel by at least this much
between rounds makes the event re-checked (another transmitter may have taken the
frequency), dB."""

WIDTH_STEP_RATIO: Final[float] = 2.0
WIDTH_STEP_MIN_HZ: Final[float] = 1000.0
"""A change of the primary sighting's width by at least this factor and this many Hz
between rounds makes the event re-checked."""

SPLIT_MIN_ROWS: Final[int] = 4
"""Rows added since the last consistent solve needed before an inconsistent fit is split
into 'earlier' and 'newest' (``Tasking._split_test``)."""

CLASS_CHANGE_CONFIDENCE: Final[float] = 0.5
"""A change between two definite waveform classes (not ``unknown``), each at least this
confident, makes the event re-checked."""

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
    position_partials: ``(sat_id, (d/d radial, d/d in-track, d/d cross-track))`` of the
    reference measurement with respect to its satellites' RIC position offsets, s/m
    (``core.solver.reference_position_partials``; TDOA only, empty when they could not
    be worked out). media_partials: ``(medium, ground end, partial)`` of the reference
    measurement per TECU or per metre of zenith delay (``core.media.row_media_partials``;
    the reference's own uplink end is ``("ref", x_ref bytes)``). Both let the calibrations
    held be read as evidence about the satellites' positions
    (``satellite_position_evidence``).
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
    position_partials: tuple[tuple[str, tuple[float, float, float]], ...] = ()
    media_partials: tuple[tuple[str, Any, float], ...] = ()


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
    """Confirmation state of one pair of one event. since_ns: labelled time of the first
    detection of the confirmed track (the candidate it was confirmed from; None for a
    track confirmed before this was kept, which counts as old)."""

    confirmed: _Detection | None = None
    candidates: deque[_Detection] = field(default_factory=lambda: deque(maxlen=_CANDIDATES_KEPT))
    since_ns: int | None = None


@dataclass(frozen=True)
class _TrackCheck:
    """How one detection relates to its pair's track (``Tasking._track_check``).

    agrees: consistent with the confirmed track. disagrees: a confirmed track exists and
    the detection is not consistent with it. jumped: it disagrees and repeats an earlier
    unconfirmed detection (a new track, confirmed). onset_ns: time of the first detection
    of that new track (the detection's own when none)."""

    agrees: bool
    disagrees: bool
    jumped: bool
    onset_ns: int


@dataclass(frozen=True)
class _PairEval:
    """One correlated pair's values and per-kind reasons before confirmation
    (``_evaluate_pair``); ``detection`` is set when at least one kind is usable."""

    tdoa: float | None
    fdoa: float | None
    sig_t: float | None
    sig_f: float | None
    reason_t: str | None
    reason_f: str | None
    detection: _Detection | None


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
    unlocatable_reason: str | None = None
    fit_note: str | None = None  # why the last solution is flagged inconsistent
    unavailable: dict[str, str] = field(default_factory=dict)  # satellites whose orbit failed last round
    aside_seen: int = 0  # EventRecord.set_aside_count the tracks belong to
    level: tuple[str, float, float] | None = None  # primary (channel, SNR dB, width Hz) of the last round
    level_ns: int | None = None  # when ``level`` was read (server clock, UTC ns)
    class_capture_end_ns: int | None = None  # end of the capture that gave the current class (labelled)
    # the re-check tasking raised: (recheck_since_ns, tie_ns, what changed, whether the
    # class now shown is the new transmission's); a track first detected at or before
    # tie_ns was measured before the change (``Tasking._recheck_tie``)
    recheck_tie: tuple[int, int, str, bool] | None = None
    recheck_waits: int = 0  # rounds the re-check waited for a pair measured before the change


@dataclass(frozen=True)
class _Kinematics:
    """Bounds on a satellite's ECEF speed (m/s) and acceleration (m/s^2), and the
    shortest range from any ground point of the problem to it, m (its geocentric
    distance less the equatorial radius and the highest site or emitter height)."""

    speed_mps: float
    accel_mps2: float
    min_range_m: float = math.inf

    def range_accel_bound(self) -> float:
        """Largest |d^2 range / dt^2| from a fixed ground point, m/s^2:
        ``(|v|^2 - rdot^2) / r + a.u <= |v|^2 / r_min + |a|`` (MATH.md section 6). The
        first term is the geometric one: negligible in GEO (the ECEF speed is metres per
        second), dominant in low orbit (about 60 m/s^2 at 800 km)."""
        geometric = self.speed_mps**2 / self.min_range_m if self.min_range_m > 0 else math.inf
        return self.accel_mps2 + geometric


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
    ground_height_m: float = 0.0  # highest site or emitter height (kinematic range bound)


@dataclass(frozen=True)
class _GeometryResult:
    pairs: tuple[_GeoOut, ...]
    refs: tuple[_GeoOut, ...]
    kinematics: Mapping[str, _Kinematics]
    elevation: Mapping[tuple[str, str], float]
    unavailable: Mapping[str, str] = field(default_factory=dict)  # sat_id -> why its orbit failed


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
    capture_a: str = ""  # identities of the two captures (rows of one capture share its noise)
    capture_b: str = ""


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
    capture_a: str = ""  # identities of the two captures (``_capture_id``)
    capture_b: str = ""


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
    # (kind, correction, CalRef, position partials, media partials): see ``Calibration``
    corrections: tuple[tuple[str, float, CalRef, tuple[Any, ...], tuple[Any, ...]], ...]
    problems: tuple[str, ...]


@dataclass(eq=False)
class _RefPlan:
    """A reference calibration planned for one pair of a round (or a clock check)."""

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
    clock: bool = False  # a same-satellite clock check (``Tasking._plan_clock_checks``)


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


_EQUATORIAL_RADIUS_M: Final[float] = 6_378_137.0
_MIN_RANGE_FLOOR_M: Final[float] = 100e3  # kinematic range bound never below this


def _geometry(job: _GeometryJob) -> _GeometryResult:
    """Elevations, satellite kinematics, and the windows of every pair and reference
    pair (blocking: about 20 ms per ``measurement_windows``).

    Each satellite is evaluated on its own: one whose orbit cannot be evaluated (an
    ephemeris table that has ended, a TLE that fails to propagate) is listed in
    ``unavailable`` with the reason, counts as below every horizon and has no
    kinematics, and only the pairs and references through it fail."""
    elevation: dict[tuple[str, str], float] = {}
    unavailable: dict[str, str] = {}
    kinematics: dict[str, _Kinematics] = {}
    for sat_id in job.sat_ids:
        orbit = job.orbits.get(sat_id)
        if orbit is None:
            unavailable[sat_id] = "not in the configuration"
            continue
        try:
            row = {
                (site_id, sat_id): float(np.asarray(elevation_deg(job.sites_xyz[site_id], orbit, job.t_mid_s)).reshape(-1)[0])
                for site_id in job.site_ids
            }
            states = [orbit.state_ecef(job.t_mid_s + dt) for dt in (-1.0, 0.0, 1.0)]
        except ValueError as exc:
            unavailable[sat_id] = str(exc)
            continue
        elevation.update(row)
        velocities = [np.asarray(state[1], dtype=np.float64).reshape(3) for state in states]
        speed = max(float(np.linalg.norm(v)) for v in velocities)
        accel = float(np.linalg.norm(velocities[2] - velocities[0])) / 2.0
        radius = float(np.linalg.norm(np.asarray(states[1][0], dtype=np.float64).reshape(3)))
        min_range = max(radius - _EQUATORIAL_RADIUS_M - max(job.ground_height_m, 0.0), _MIN_RANGE_FLOOR_M)
        kinematics[sat_id] = _Kinematics(speed, accel, min_range)

    def lost(spec: _GeoIn) -> str | None:
        for sat_id in (spec.a.sat_id, spec.b.sat_id):
            if sat_id in unavailable:
                return f"satellite {sat_id}: orbit not available ({unavailable[sat_id]})"
        return None

    def target(spec: _GeoIn) -> _GeoOut:
        a_nom, b_nom = _path(spec.a, spec.f_hz), _path(spec.b, spec.f_hz)
        why = lost(spec)
        if why is not None:
            return _GeoOut(a_nom, b_nom, None, error=why)
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
        why = lost(spec)
        if why is not None:
            return _GeoOut(a_nom, b_nom, None, error=why)
        for sat_id in {spec.a.sat_id, spec.b.sat_id}:
            try:
                el = float(np.asarray(elevation_deg(spec.x_ref, job.orbits[sat_id], job.t_ref_s)).reshape(-1)[0])
            except ValueError as exc:
                return _GeoOut(a_nom, b_nom, None, error=f"satellite {sat_id}: orbit not available ({exc})")
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
        unavailable=unavailable,
    )


def _capture_id(channel_id: str, capture: Any) -> str:
    """Identity of one capture: channel, labelled first-sample time, centre and rate."""
    return (f"{channel_id}|{int(capture.t_first_ns)}|{round(float(capture.center_hz))}"
            f"|{round(float(capture.sample_rate_hz))}")


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
    corrections: list[tuple[str, float, CalRef, tuple[Any, ...], tuple[Any, ...]]] = []
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
        share = result.noise_share_tdoa if kind == "tdoa" else result.noise_share_fdoa
        ref_row = Measurement(
            kind=kind,  # type: ignore[arg-type]
            value=value,
            sigma=sigma,
            t_ns=job.t_mid_ns,
            a=job.caf.path_a,
            b=job.caf.path_b,
            capture_a=job.caf.capture_a,  # references of one capture share its noise (MATH.md section 9)
            capture_b=job.caf.capture_b,
            noise_share_a=float(share[0]),
            noise_share_b=float(share[1]),
        )
        try:
            c, cal = calibration_correction(ref_row, job.x_ref, orbits, sites_xyz, cal_id)
        except ValueError as exc:
            problems.append(f"{kind}: {exc}")
            continue
        if _finite(c) is None:
            problems.append(f"{kind}: correction not finite")
            continue
        position, media = _reference_partials(kind, cal, orbits, sites_xyz)
        corrections.append((kind, float(c), cal, position, media))
    return _RefResult(result, tuple(corrections), tuple(problems))


def _reference_partials(
    kind: str, cal: CalRef, orbits: Mapping[str, Any], sites_xyz: Mapping[str, np.ndarray]
) -> tuple[tuple[tuple[str, tuple[float, float, float]], ...], tuple[tuple[str, Any, float], ...]]:
    """``Calibration.position_partials`` and ``media_partials`` of a TDOA reference
    measurement (blocking; empty for FDOA, and when they cannot be worked out or are not
    finite)."""
    if kind != "tdoa":
        return (), ()
    try:
        position = reference_position_partials(kind, cal, orbits, sites_xyz)
        x_ref = np.asarray(cal.x_ref, dtype=np.float64)
        media = row_media_partials(
            kind, cal.a, cal.b, np.array([int(cal.t_ns) / _NS]), x_ref, orbits, sites_xyz, ("ref", x_ref.tobytes())
        )
    except Exception as exc:  # optional evidence: never lose the calibration over it
        logger.warning("calibration %s: satellite partials not worked out: %s", cal.cal_id, exc)
        return (), ()
    pos_out = tuple(
        (sat, (float(p[0]), float(p[1]), float(p[2]))) for sat, p in position.items() if np.all(np.isfinite(p))
    )
    media_out = tuple(
        (medium, end, float(np.ravel(values)[0]))
        for medium, table in media.items()
        for end, values in table.items()
        if np.isfinite(np.ravel(values)[0])
    )
    return pos_out, media_out


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


def _chi2_text(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "unknown"
    return f"{value:.1e}" if value >= 1e4 else f"{value:.3g}"


def _evaluate_pair(ctx: _PairCtx, result: CafResult) -> _PairEval:
    """The measured values of a pair (FDOA with the centre correction) and why each kind
    cannot be used, before the repeat confirmation."""
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
    detection = None
    if usable_t or usable_f:
        detection = _Detection(
            ctx.t_mid_ns,
            tdoa if usable_t else None,
            sig_t if usable_t else None,
            fdoa if usable_f else None,
            sig_f if usable_f else None,
        )
    return _PairEval(tdoa, fdoa, sig_t, sig_f, reason_t, reason_f, detection)


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
UNLOCATABLE_AMBIGUOUS: Final[str] = (
    "It cannot be located by comparing satellite copies: the waveform repeats itself (for example "
    "a periodic sweep), so every correlation has several peaks of almost equal height and gives no "
    "single time or frequency difference."
)
UNLOCATABLE_COUPLED: Final[str] = (
    "It cannot be located by comparing satellite copies: delay and Doppler cannot be told apart in "
    "the correlation of this waveform (a single sweep across the capture)."
)
UNLOCATABLE_ONE_SATELLITE: Final[str] = (
    "Only one satellite relays this frequency (or can see the transmitter), so it cannot be located "
    "from the differences between satellite copies."
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


CLOCK_CAUTION_NS: Final[float] = 1000.0
"""Smallest calibration-estimated clock offset that raises a caution, ns (calibration
corrections of healthy sites stay well under a microsecond: they also carry the
satellites' ephemeris errors)."""

CLOCK_CHECKS_PER_ROUND: Final[int] = 2
"""Reference pairs a round may calibrate in all, counting its pairs' own calibrations;
what is left goes to same-satellite clock checks (``Tasking._plan_clock_checks``), so the
round that calibrates the pairs adds none and the next rounds catch up."""

DOUBT_MIN_SCALE: Final[float] = 3.0
"""Smallest factor on the position sigma of a satellite whose position the calibrations
put wrong beyond its configured uncertainty (``ephemeris_scale``): the doubt itself
(views through two satellites differing by at least three times the configured
site-dependent ephemeris allowance, ``site_clock_offsets``) means an error of about three
configured sigmas or more, even where the calibrations cannot size it better."""

DOUBT_MARGIN_SIGMAS: Final[float] = 2.0
"""The scale covers the error the calibrations show plus this many of its one-sigma
uncertainties (``ephemeris_scale``)."""

SCALE_STEPS_PER_OCTAVE: Final[int] = 4
"""A scale is rounded up to the next of these steps per factor of two (1, 1.19, 1.41,
1.68, 2, ...), so that small changes of the evidence do not change the solve."""


def _duration_ns_text(ns: float) -> str:
    """``9.7 µs`` or ``850 ns``."""
    return f"{abs(ns) / 1e3:.1f} µs" if abs(ns) >= 1000.0 else f"{abs(ns):.0f} ns"


def _and_list(items: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _signed_ns_text(ns: float) -> str:
    """``9.7 µs behind`` or ``850 ns ahead``."""
    return f"{_duration_ns_text(ns)} {'ahead' if ns > 0 else 'behind'}"


@dataclass(frozen=True)
class ClockEdge:
    """One cross-site TDOA calibration as clock evidence (``site_clock_offsets``).

    The correction holds ``c ~ (o_a + e_a) - (o_b + e_b)``: the two sites' clock offsets
    ``o`` and a term ``e`` per satellite (its ephemeris and transponder errors, common to
    every site that hears it). A same-satellite edge (``sat_a == sat_b``, a clock check:
    one satellite's copy of the reference at two sites) has no satellite term at all and
    measures ``o_a - o_b`` directly. eph_s: one-sigma of what that model leaves out, the
    site-dependent part of the satellites' ephemeris errors (configured position sigma
    times the angle between the two sites seen from the satellite, over c), s.
    sat_sigma_a_s, sat_sigma_b_s: prior one-sigma of each satellite's common term
    (configured position sigma over c), s: what the data cannot tell apart from a site's
    clock is given to the satellites, whose errors are expected to be far larger."""

    sat_a: str
    site_a: str
    sat_b: str
    site_b: str
    c_s: float
    eph_s: float = 0.0
    sat_sigma_a_s: float = 1e-6
    sat_sigma_b_s: float = 1e-6


def site_clock_offsets(
    edges: Sequence[ClockEdge], config: Any, site_name: Callable[[str], str] | None = None
) -> list[schemas.SiteClockEstimate]:
    """Per-site clock offsets from cross-site TDOA calibration corrections
    (``Tasking.site_clock_estimates``).

    For each connected group of sites, the least-squares solution of
    ``c = o_a - o_b + e_sat_a - e_sat_b`` over every edge with the smallest weighted
    norm (clocks weighted by their configured sigma, satellites by ``sat_sigma``: a
    combination the edges cannot resolve goes to the satellites). Sites whose clock
    differences the edges determine (``_determined_clusters``: independent of that
    split) form a cluster, and each cluster is shifted so its own median is zero (most
    sites are assumed right); a site in no cluster is shifted by the group's median. A
    cluster reached through one satellite term (sites on spokes through the same
    satellite) would otherwise be centered on its mean, spreading one site's clock step
    onto the others (seen live when a clock fault started mid-run). A site is judged when
    ``|o|`` reaches the largest of ``CLOCK_CAUTION_NS``, ten times its configured clock
    sigma and three times the RMS ``eph_s`` of its edges (the *limit*), and the largest
    disagreement between satellites any site of the group shows (below).

    Same-satellite edges (clock checks) also give the site's offset *through each
    satellite* separately (the edge's correction plus the other site's fitted offset). A
    clock offset shows alike through every satellite; a satellite position error beyond
    its configured uncertainty shows through that satellite only. So:

    - views through two or more satellites that differ by the limit or more: the clock is
      not blamed (``caution`` false); ``message`` names the satellite(s) whose view is off.
      Such satellite errors reach the other sites' offsets through the fit too, so no site
      of the group is judged on an offset smaller than the largest such difference;
    - an offset at the limit seen alike through two or more satellites: a clock fault
      (``caution``, the message says the satellites agree);
    - an offset at the limit with fewer views: ``caution``, and the message names both
      explanations (the clock, or the position of the satellites it was seen through).

    Two sites whose difference alone is known (a cluster of two) are both judged on
    that difference (which one is off cannot be told).

    config: the ``SystemConfig`` (clock allowances; site names unless ``site_name`` is
    given). Returns one estimate per site that appears, sorted by site id.
    """
    if not edges:
        return []
    if site_name is None:

        def site_name(site_id: str) -> str:
            try:
                return str(config.site(site_id).name)
            except (ValueError, AttributeError):
                return site_id

    neighbours: dict[str, set[str]] = {}
    for edge in edges:
        if edge.site_a != edge.site_b:  # a same-site edge only ties its two satellites' terms
            neighbours.setdefault(edge.site_a, set()).add(edge.site_b)
            neighbours.setdefault(edge.site_b, set()).add(edge.site_a)
    out: list[schemas.SiteClockEstimate] = []
    seen: set[str] = set()
    for start in sorted(neighbours):
        if start in seen:
            continue
        group, todo = [], [start]
        while todo:
            site = todo.pop()
            if site in seen:
                continue
            seen.add(site)
            group.append(site)
            todo.extend(neighbours[site] - seen)
        group.sort()
        rows = [e for e in edges if e.site_a in group and e.site_b in group]
        sats = sorted({e.sat_a for e in rows} | {e.sat_b for e in rows})
        site_index = {s: i for i, s in enumerate(group)}
        sat_index = {s: len(group) + i for i, s in enumerate(sats)}
        design = np.zeros((len(rows), len(group) + len(sats)))
        target = np.zeros(len(rows))
        for k, e in enumerate(rows):
            design[k, site_index[e.site_a]] += 1.0
            design[k, site_index[e.site_b]] -= 1.0
            design[k, sat_index[e.sat_a]] += 1.0  # a same-satellite edge: the two cancel
            design[k, sat_index[e.sat_b]] -= 1.0
            target[k] = e.c_s * 1e9
        scale = np.ones(len(group) + len(sats))
        for site, i in site_index.items():
            try:
                scale[i] = max(float(config.site(site).clock.sigma_ns), 1.0)
            except (ValueError, AttributeError):
                scale[i] = 30.0
        for e in rows:
            scale[sat_index[e.sat_a]] = max(scale[sat_index[e.sat_a]], e.sat_sigma_a_s * 1e9)
            scale[sat_index[e.sat_b]] = max(scale[sat_index[e.sat_b]], e.sat_sigma_b_s * 1e9)
        scaled, *_ = np.linalg.lstsq(design * scale, target, rcond=None)  # weighted minimum norm
        solution = scaled * scale
        # Sites whose clock differences the edges determine (independent of how the fit
        # splits what it cannot resolve between clocks and satellite terms) form a
        # cluster; each cluster is centered on its own median (most sites right). A
        # site in no such cluster keeps the group median (its offset is entangled with a
        # satellite term and mostly given to it).
        clusters = _determined_clusters(design, len(group))
        median = float(np.median(solution[: len(group)]))
        center = np.full(len(group), median)
        cluster_of: dict[str, list[str]] = {}
        for members in clusters:
            if len(members) >= 2:
                center[members] = float(np.median(solution[members]))
            for i in members:
                cluster_of[group[i]] = [group[j] for j in members]
        offsets = solution[: len(group)] - center
        fitted = design @ solution
        residuals: dict[str, list[float]] = {s: [] for s in group}
        eph: dict[str, list[float]] = {s: [] for s in group}
        views_raw: dict[str, dict[str, list[float]]] = {s: {} for s in group}
        for k, e in enumerate(rows):
            if e.site_a == e.site_b:
                continue
            for site in (e.site_a, e.site_b):
                residuals[site].append(float(target[k] - fitted[k]))
                eph[site].append(e.eph_s * 1e9)
            if e.sat_a == e.sat_b:  # each end's offset through this satellite alone (one cluster)
                ia, ib = site_index[e.site_a], site_index[e.site_b]
                views_raw[e.site_a].setdefault(e.sat_a, []).append(float(target[k] + solution[ib] - center[ib]))
                views_raw[e.site_b].setdefault(e.sat_a, []).append(float(solution[ia] - target[k] - center[ia]))
        cross = [e for e in rows if e.site_a != e.site_b]

        def limit_of(site: str) -> float:
            try:
                allowance = float(config.site(site).clock.sigma_ns)
            except (ValueError, AttributeError):
                allowance = 0.0
            eph_rms = float(np.sqrt(np.mean(np.square(eph[site])))) if eph[site] else 0.0
            return max(CLOCK_CAUTION_NS, 10.0 * allowance, 3.0 * eph_rms)

        def views_of(site: str) -> dict[str, float]:
            return {sat: float(np.mean(v)) for sat, v in sorted(views_raw[site].items())}

        def through_of(site: str) -> list[str]:
            return sorted(
                {e.sat_a for e in cross if e.site_a == site} | {e.sat_b for e in cross if e.site_b == site}
            )

        def suspects(site: str, limit: float) -> list[str]:
            """The satellites whose view of the site is off, when its views disagree."""
            views = views_of(site)
            if len(views) < 2 or max(views.values()) - min(views.values()) < limit:
                return []
            return [sat for sat, v in views.items() if abs(v) >= limit] or [max(views, key=lambda s: abs(views[s]))]

        def disagreement(site: str, limit: float) -> str | None:
            """The note when the site's views through different satellites disagree."""
            views = views_of(site)
            off = suspects(site, limit)
            if not off:
                return None
            shown = "; ".join(f"through {sat} about {_signed_ns_text(v)}" for sat, v in views.items())
            which = (
                f"the position of {off[0]} is probably wrong beyond its configured uncertainty (check its ephemeris)"
                if len(off) == 1
                else f"the positions of {_and_list(off)} are probably wrong beyond their configured uncertainty "
                "(check their ephemerides)"
            )
            return (
                f"Reference calibrations put the timing of {site_name(site)} differently through different "
                f"satellites ({shown}). A clock offset would show alike through every satellite, so the clock of "
                f"{site_name(site)} is probably fine and {which}."
            )

        def evidence(site: str) -> tuple[bool, str]:
            """(confident, clause): whether two or more satellites agree, and the
            clause naming the alternative explanation when they do not."""
            views = views_of(site)
            if len(views) >= 2:
                return True, f"calibrations through {_and_list(list(views))} agree, so it is not a satellite position error"
            through = sorted(set(views) | set(through_of(site)))
            if len(through) == 1:
                sats = f"the position of {through[0]} is wrong beyond its configured uncertainty"
            elif through:
                sats = f"the positions of {_and_list(through)} are wrong beyond their configured uncertainty"
            else:
                sats = "a satellite position is wrong beyond its configured uncertainty"
            return False, (
                f"or {sats} (seen from this site it would look the same); a calibration through a second satellite "
                "will tell them apart"
            )

        # Satellite position errors beyond their configured uncertainty that the group
        # itself shows (one site's offset differing between satellites) also reach every
        # other site's offset through the fit: an offset smaller than that is not judged.
        shown_spread = max(
            (max(v.values()) - min(v.values()) for v in (views_of(s) for s in group) if len(v) >= 2), default=0.0
        )
        for site in group:
            o = float(offsets[site_index[site]])
            others = sorted(neighbours[site])
            n = len(residuals[site])
            sigma = float(np.sqrt(np.mean(np.square(residuals[site])))) if n >= 2 else None
            limit = limit_of(site)
            judged = max(limit, shown_spread)
            message: str | None = None
            caution = False
            pair = cluster_of.get(site, [site])
            if len(pair) == 2:
                # two sites whose difference alone is known: which one is off cannot be told
                difference = 2.0 * o
                note = disagreement(site, limit)
                if note is not None:
                    message = note
                elif abs(difference) >= judged:
                    caution = True
                    confident, clause = evidence(site)
                    first, second = site_name(pair[0]), site_name(pair[1])
                    message = (
                        f"Reference calibrations show the timing of {first} and {second} differs by about "
                        f"{_duration_ns_text(difference)}: one of the two clocks is probably off ({clause}); check "
                        "both sites' timing receivers (GPS, PPS and 10 MHz reference)."
                        if confident
                        else f"Reference calibrations show the timing of {first} and {second} differs by about "
                        f"{_duration_ns_text(difference)}: one of the two clocks may be off (check both sites' timing "
                        f"receivers: GPS, PPS and 10 MHz reference), {clause}."
                    )
            else:
                note = disagreement(site, limit)
                if note is not None:
                    message = note
                elif abs(o) >= judged:
                    caution = True
                    confident, clause = evidence(site)
                    head = (
                        f"Reference calibrations against {', '.join(site_name(s) for s in others)} put the timing of "
                        f"{site_name(site)} about {_duration_ns_text(o)} {'ahead of' if o > 0 else 'behind'} the "
                        "other sites"
                    )
                    message = (
                        f"{head}: its clock is probably off ({clause}); check its timing receiver (GPS, PPS and "
                        "10 MHz reference)."
                        if confident
                        else f"{head}: either its clock is off (check its timing receiver: GPS, PPS and 10 MHz "
                        f"reference), {clause}."
                    )
            out.append(
                schemas.SiteClockEstimate(
                    site_id=site,
                    offset_us=o / 1e3,
                    sigma_us=None if sigma is None else sigma / 1e3,
                    n_calibrations=n,
                    compared_with=others,
                    caution=caution,
                    message=message,
                    suspect_satellite_ids=[] if caution else suspects(site, limit),
                )
            )
    out.sort(key=lambda e: e.site_id)
    return out


def _determined_clusters(design: npt.NDArray[np.float64], n_sites: int) -> list[list[int]]:
    """Groups of site columns (the first ``n_sites`` of ``design``) whose pairwise
    differences the rows determine: ``e_i - e_j`` lies in the row space, so every null
    vector has equal entries at ``i`` and ``j``. Returns index lists covering every site."""
    if n_sites == 0:
        return []
    _, sv, vt = np.linalg.svd(design, full_matrices=True)
    tol = max(design.shape) * float(np.finfo(np.float64).eps) * (float(sv[0]) if sv.size else 1.0) * 1e3
    rank = int(np.count_nonzero(sv > tol))
    null = vt[rank:].T[:n_sites]  # each site's entries in the null vectors
    clusters: list[list[int]] = []
    for i in range(n_sites):
        for members in clusters:
            if null.shape[1] == 0 or float(np.max(np.abs(null[i] - null[members[0]]))) < 1e-6:
                members.append(i)
                break
        else:
            clusters.append([i])
    return clusters


@dataclass(frozen=True)
class EvidenceEdge:
    """One TDOA reference calibration as evidence about the satellites' positions
    (``satellite_position_evidence``).

    The correction holds ``c = o_a - o_b + e_a - e_b + sum_s P_s dr_s + shared + noise``:
    the two sites' clock offsets ``o``, a delay ``e`` per satellite common to every site
    that hears it (transponder, and the part of an ephemeris error common to all
    sites), each satellite's RIC position error ``dr_s`` through the reference
    measurement's partials ``P_s``, error terms several calibrations share (the
    propagation media per ground end) and the edge's own noise.
    c_s: the correction, s. sigma_s: its own noise one-sigma (the reference measurement's
    CAF noise and the pair bias), s. partials: ``(sat_id, (d c/d radial, d c/d in-track,
    d c/d cross-track))``, s/m. shared: ``(key, value)``: an error term shared by every
    edge that names the same key, as this edge's partial times the term's one-sigma, s.
    """

    sat_a: str
    site_a: str
    sat_b: str
    site_b: str
    c_s: float
    sigma_s: float
    partials: tuple[tuple[str, tuple[float, float, float]], ...]
    shared: tuple[tuple[Any, float], ...] = ()


@dataclass(frozen=True)
class PositionEvidence:
    """What the reference calibrations say about one satellite's position
    (``satellite_position_evidence``).

    error_ric_m: the position error they show, radial / in-track / cross-track, m: the
    smallest error (in units of the configured sigma) that explains them, so directions
    they do not determine are left near zero. norm: its size in configured sigmas,
    ``sqrt(sum_i (error_i / sigma_i)^2)``. norm_sigma: one-sigma uncertainty of ``norm``
    along the error's direction (1 when the calibrations do not determine the position at
    all). n_edges: the calibrations through the satellite.
    """

    sat_id: str
    error_ric_m: tuple[float, float, float]
    norm: float
    norm_sigma: float
    n_edges: int


def satellite_position_evidence(
    edges: Sequence[EvidenceEdge], sigma_ric_m: Mapping[str, Any]
) -> dict[str, PositionEvidence]:
    """Each satellite's position error as the TDOA calibrations held show it (MATH.md
    section 9, *ephemeris allowance*).

    One linear least-squares fit of every edge (``EvidenceEdge``): the site clocks and the
    satellites' common delays are free (a clock fault or a transponder delay explains
    nothing about a position: whatever a per-site offset or a per-satellite delay can
    absorb is given to them); each satellite's RIC position error has the prior
    ``N(0, diag(sigma^2))`` of its configured sigma (``sigma_ric_m``, m; a satellite not
    given has none to show); the edges' covariance is their own noise plus the shared
    terms. Returns the posterior mean of each position error and the uncertainty of its
    size (``PositionEvidence``) for every satellite the edges name, or an empty dict
    when there are no edges or the fit fails. Pure.
    """
    if not edges:
        return {}
    sites = sorted({e.site_a for e in edges} | {e.site_b for e in edges})
    sats = sorted({e.sat_a for e in edges} | {e.sat_b for e in edges})
    sigma = {
        sat: np.abs(np.nan_to_num(np.asarray(sigma_ric_m.get(sat, (0.0, 0.0, 0.0)), dtype=np.float64).reshape(3)))
        for sat in sats
    }
    m = len(edges)
    free = np.zeros((m, len(sites) + len(sats)))
    position = np.zeros((m, 3 * len(sats)))  # per configured sigma, ns
    target = np.zeros(m)
    own = np.zeros(m)
    shared: dict[Any, np.ndarray] = {}
    counts = dict.fromkeys(sats, 0)
    for j, e in enumerate(edges):
        free[j, sites.index(e.site_a)] += 1.0
        free[j, sites.index(e.site_b)] -= 1.0
        free[j, len(sites) + sats.index(e.sat_a)] += 1.0  # a same-satellite edge: the two cancel
        free[j, len(sites) + sats.index(e.sat_b)] -= 1.0
        for sat in {e.sat_a, e.sat_b}:
            counts[sat] += 1
        for sat, partial in e.partials:
            if sat in counts:
                i = sats.index(sat)
                position[j, 3 * i : 3 * i + 3] += np.asarray(partial, dtype=np.float64) * 1e9 * sigma[sat]
        for key, value in e.shared:
            shared.setdefault(key, np.zeros(m))[j] += float(value) * 1e9
        target[j] = float(e.c_s) * 1e9
        own[j] = max(abs(float(e.sigma_s)) * 1e9, 1e-3)
    try:
        columns = np.stack(list(shared.values()), axis=1) if shared else np.zeros((m, 0))
        covariance = np.diag(own**2) + columns @ columns.T
        if not (np.all(np.isfinite(covariance)) and np.all(np.isfinite(position)) and np.all(np.isfinite(target))):
            return {}
        lower = np.linalg.cholesky(covariance)
        w_free = np.linalg.solve(lower, free)
        w_pos = np.linalg.solve(lower, position)
        w_target = np.linalg.solve(lower, target)
        # the free parameters absorb what they can: keep the part of the data orthogonal to them
        basis, sv, _ = np.linalg.svd(w_free, full_matrices=False)
        rank = int(np.count_nonzero(sv > (float(sv[0]) if sv.size else 0.0) * 1e-9))
        q = basis[:, :rank]
        design = w_pos - q @ (q.T @ w_pos)
        data = w_target - q @ (q.T @ w_target)
        normal = design.T @ design + np.eye(3 * len(sats))  # prior N(0, I) per configured sigma
        posterior = np.linalg.inv(normal)
        estimate = posterior @ (design.T @ data)
    except np.linalg.LinAlgError:
        return {}
    if not (np.all(np.isfinite(estimate)) and np.all(np.isfinite(posterior))):
        return {}
    out: dict[str, PositionEvidence] = {}
    for i, sat in enumerate(sats):
        u = estimate[3 * i : 3 * i + 3]
        block = posterior[3 * i : 3 * i + 3, 3 * i : 3 * i + 3]
        norm = float(np.linalg.norm(u))
        if norm > 0.0:
            direction = u / norm
        else:
            direction = np.linalg.eigh(block)[1][:, -1]
        spread = float(np.sqrt(max(float(direction @ block @ direction), 0.0)))
        out[sat] = PositionEvidence(
            sat_id=sat,
            error_ric_m=(float(u[0] * sigma[sat][0]), float(u[1] * sigma[sat][1]), float(u[2] * sigma[sat][2])),
            norm=norm,
            norm_sigma=spread,
            n_edges=counts[sat],
        )
    return out


def ephemeris_scale(evidence: PositionEvidence | None, max_scale: float) -> tuple[float, bool]:
    """The factor on a doubted satellite's configured position sigma for the solve, and
    whether it was capped: ``(scale, capped)`` (MATH.md section 9, *ephemeris allowance*).

    ``max(DOUBT_MIN_SCALE, norm + DOUBT_MARGIN_SIGMAS x norm_sigma)``: the widened sigma puts
    the error the calibrations show (with its margin) on or inside the one-sigma surface
    of the widened prior, rounded up to the next of ``SCALE_STEPS_PER_OCTAVE`` steps per
    octave and at most ``max_scale``; ``capped`` when the evidence asks for more than
    ``max_scale``. Without evidence (None, or numbers that are not finite) the error
    cannot be sized: ``(max_scale, True)``. ``max_scale <= 1`` gives ``(1.0, True)``
    (no allowance)."""
    cap = float(max_scale)
    if not (math.isfinite(cap) and cap > 1.0):
        return 1.0, True
    if evidence is None or not (math.isfinite(evidence.norm) and math.isfinite(evidence.norm_sigma)):
        return cap, True
    raw = max(DOUBT_MIN_SCALE, float(evidence.norm) + DOUBT_MARGIN_SIGMAS * float(evidence.norm_sigma))
    steps = math.ceil(SCALE_STEPS_PER_OCTAVE * math.log2(raw) - 1e-9)
    scale = 2.0 ** (steps / SCALE_STEPS_PER_OCTAVE)
    return min(max(scale, 1.0), cap), raw > cap


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
        # ephemeris allowance: per doubted satellite, the scales sized at round ends (t_ns, scale, capped)
        self._allowance_targets: dict[str, list[tuple[int, float, bool]]] = {}
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
        # what an ephemeris allowance scales: the configured position sigma, a component
        # configured as zero taking the operator-grade default (MATH.md section 9)
        operator = np.asarray(config.processing.uncertainty.ephemeris.operator.pos_m, dtype=np.float64)
        self._allowance_base: dict[str, np.ndarray] = {
            sat: np.where(np.asarray(sigma, dtype=np.float64) > 0.0, np.asarray(sigma, dtype=np.float64), operator)
            for sat, sigma in self._uncertainty.sat_pos_ric_m.items()
        }
        self._channel_site: dict[str, str] = {}
        self._channels: dict[str, Any] = {}
        for site in config.sites:
            for channel in site.channels:
                self._channel_site[channel.id] = site.id
                self._channels[channel.id] = channel
        # highest ground point of the problem: sites (with their masts) and the emitter height
        self._ground_height_m = max(
            [float(config.processing.solver.height_m)]
            + [float(site.alt_m) + float(site.antenna_height_m) for site in config.sites]
        )

    @property
    def config(self) -> Any:
        """The configuration in force."""
        return self._config

    def reconfigure(self, config: Any) -> None:
        """Take a new configuration. Rounds in flight are cancelled; calibrations (with
        the ephemeris allowance they sized), confirmation tracks and channel exclusions
        are dropped (orbits, transponders, channels or sigmas may have changed). Measurements already recorded stay with their
        events and are solved with the new orbits (rows naming a satellite or site
        that no longer exists are left out)."""
        self._generation += 1
        self._apply_config(config)
        self._quality = None
        self._calibrations.clear()
        self._cal_attempts.clear()
        self._allowance_targets.clear()
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
        round. The ephemeris allowances they sized go with them."""
        self._world_since_ns = int(effective_ns)
        self._calibrations.clear()
        self._cal_attempts.clear()
        self._allowance_targets.clear()
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

    def site_clock_estimates(self) -> list[schemas.SiteClockEstimate]:
        """Each site's clock offset as the reference calibrations see it (UX-10).

        A TDOA calibration of a cross-site pair measures ``c = z_ref - h_ref``, which
        holds the two sites' clock offsets as ``o_a - o_b``, plus a term per satellite
        (ephemeris, transponder) and the site-dependent remainder of the ephemeris error
        (``ClockEdge``); same-site pairs (no clock term) tie the satellites' terms, and
        same-satellite clock checks (``_plan_clock_checks``: no satellite term) measure
        clock differences directly and show each site's offset through each satellite.
        Only calibrations within ``calibration.max_age_s`` count; see
        ``site_clock_offsets`` for the fit and the caution rule."""
        now = self._clock()
        max_age_ns = int(float(self._config.processing.calibration.max_age_s) * _NS)
        zero = np.zeros(3)
        edges: list[ClockEdge] = []
        for (key, _carrier), entry in self._calibrations.items():
            sat_a, site_a, sat_b, site_b, kind = key
            if kind != "tdoa" or abs(now - entry.t_ns) > max_age_ns:
                continue
            if _finite(entry.correction) is None:
                continue
            eph = 0.0
            sat_sigma: list[float] = []
            legs = ((sat_a, site_a, site_b), (sat_b, site_b, site_a))
            for i, (sat, here, there) in enumerate(legs):
                sigma_m = float(np.linalg.norm(self._uncertainty.sat_pos_ric_m.get(sat, zero)))
                sat_sigma.append(max(sigma_m, 1.0) / C)
                if i == 1 and sat_a == sat_b:
                    continue  # one satellite seen from two sites: one site-dependent term
                try:
                    r_sat = np.asarray(orbit_position(self._orbits[sat], entry.t_ns / _NS), dtype=np.float64).reshape(3)
                    u, v = self._sites_xyz[here] - r_sat, self._sites_xyz[there] - r_sat
                    cos = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v)))
                    angle = math.acos(max(-1.0, min(1.0, cos)))
                except (KeyError, ValueError):
                    angle = 0.0
                eph += (sigma_m * angle / C) ** 2
            edges.append(
                ClockEdge(sat_a, site_a, sat_b, site_b, float(entry.correction), math.sqrt(eph), sat_sigma[0], sat_sigma[1])
            )
        return site_clock_offsets(edges, self._config)

    def satellite_position_evidence(self) -> dict[str, PositionEvidence]:
        """Each satellite's position error as the TDOA calibrations held (within
        ``calibration.max_age_s``, the clock checks included) show it
        (``satellite_position_evidence``): the calibrations' own noise plus the pair bias
        (``uncertainty.tdoa_bias_ns``), and the propagation media shared per ground end
        as in the solver (MATH.md section 9), against the configured position sigmas."""
        now = self._clock()
        max_age_ns = int(float(self._config.processing.calibration.max_age_s) * _NS)
        unc = self._uncertainty
        media = [
            (medium, float(sigma), float(rho))
            for medium, sigma, rho in (
                ("ionosphere", unc.iono_vtec_tecu, unc.iono_correlation),
                ("troposphere", unc.tropo_zenith_m, unc.tropo_correlation),
            )
            if float(sigma) > 0.0
        ]
        edges: list[EvidenceEdge] = []
        for (key, _carrier), entry in self._calibrations.items():
            sat_a, site_a, sat_b, site_b, kind = key
            if kind != "tdoa" or abs(now - entry.t_ns) > max_age_ns or not entry.position_partials:
                continue
            if _finite(entry.correction) is None:
                continue
            shared: list[tuple[Any, float]] = []
            for medium, sigma, rho in media:
                total = 0.0
                for name, end, value in entry.media_partials:
                    if name != medium:
                        continue
                    total += value
                    if rho < 1.0:
                        shared.append(((medium, end), value * sigma * math.sqrt(1.0 - rho)))
                if rho > 0.0:
                    shared.append(((medium, "common"), total * sigma * math.sqrt(rho)))
            edges.append(
                EvidenceEdge(
                    sat_a, site_a, sat_b, site_b, float(entry.correction),
                    math.hypot(float(entry.cal.sigma), float(unc.tdoa_bias_s)), entry.position_partials, tuple(shared),
                )
            )
        return satellite_position_evidence(edges, self._allowance_base)

    def ephemeris_allowance(self) -> dict[str, tuple[float, bool]]:
        """The ephemeris allowance in force: ``{sat_id: (scale, capped)}`` for each
        satellite whose configured position sigma the solves widen because the reference
        calibrations put its position wrong beyond it (``_update_allowance``). The scale is
        the largest sized within the last ``calibration.max_age_s`` (the life of the
        calibrations that showed the error), so it rises at once and falls back, or ends,
        only when that evidence has aged out."""
        now = self._clock()
        hold_ns = int(float(self._config.processing.calibration.max_age_s) * _NS)
        out: dict[str, tuple[float, bool]] = {}
        for sat, targets in self._allowance_targets.items():
            live = [t for t in targets if now - t[0] <= hold_ns]
            if not live:
                continue
            scale = max(t[1] for t in live)
            out[sat] = (scale, any(t[2] for t in live if t[1] == scale))
        return out

    def _update_allowance(self, doubted: set[str]) -> None:
        """Size the ephemeris allowance of every doubted satellite
        (``SiteClockEstimate.suspect_satellite_ids``) from the calibrations held
        (``satellite_position_evidence``, ``ephemeris_scale``) and forget scales older than
        ``calibration.max_age_s``. A satellite in no doubt is never widened, so with no
        doubt the solves use the configured uncertainty unchanged."""
        now = self._clock()
        cal_cfg = self._config.processing.calibration
        hold_ns = int(float(cal_cfg.max_age_s) * _NS)
        before = self.ephemeris_allowance()
        for sat in list(self._allowance_targets):
            kept = [t for t in self._allowance_targets[sat] if now - t[0] <= hold_ns]
            if kept:
                self._allowance_targets[sat] = kept
            else:
                del self._allowance_targets[sat]
        max_scale = float(cal_cfg.ephemeris_allowance_max_scale)
        widen = sorted(s for s in doubted if s in self._allowance_base)
        if widen and max_scale > 1.0:
            evidence = self.satellite_position_evidence()
            for sat in widen:
                scale, capped = ephemeris_scale(evidence.get(sat), max_scale)
                if scale > 1.0:
                    self._allowance_targets.setdefault(sat, []).append((now, scale, capped))
                shown = evidence.get(sat)
                logger.debug(
                    "ephemeris evidence %s: %s", sat,
                    "none" if shown is None else f"{shown.norm:.2f} +/- {shown.norm_sigma:.2f} sigma, "
                    f"RIC {shown.error_ric_m[0]:.0f} / {shown.error_ric_m[1]:.0f} / {shown.error_ric_m[2]:.0f} m",
                    extra={"sat_id": sat},
                )
        after = self.ephemeris_allowance()
        if after != before:
            self._count("allowance_changes")
            logger.info(
                "ephemeris allowance: %s",
                ", ".join(f"{s} x{k:.3g}{' (capped)' if c else ''}" for s, (k, c) in sorted(after.items())) or "none",
                extra={"allowance": {s: k for s, (k, _) in after.items()}},
            )

    def _solve_uncertainty(self, rows: Sequence[Measurement]) -> tuple[Uncertainty, tuple[EphemerisAllowance, ...]]:
        """The consider uncertainty for solving ``rows``: the configured one (the very same
        object) unless the ephemeris allowance (``ephemeris_allowance``) widens a satellite
        the rows or their calibrations go through; then a copy with those satellites'
        position sigmas multiplied by their scales, and the allowance applied."""
        allowance = self.ephemeris_allowance()
        if not allowance:
            return self._uncertainty, ()
        sats: set[str] = set()
        for row in rows:
            sats.update((row.a.sat_id, row.b.sat_id))
            if row.cal is not None:
                sats.update((row.cal.a.sat_id, row.cal.b.sat_id))
        used = {s: v for s, v in allowance.items() if s in sats and s in self._allowance_base}
        if not used:
            return self._uncertainty, ()
        positions = dict(self._uncertainty.sat_pos_ric_m)
        applied: list[EphemerisAllowance] = []
        for sat, (scale, capped) in sorted(used.items()):
            widened = self._allowance_base[sat] * scale
            positions[sat] = widened
            sigma = (float(widened[0]), float(widened[1]), float(widened[2]))
            applied.append(EphemerisAllowance(sat, float(scale), sigma, capped))
        return replace(self._uncertainty, sat_pos_ric_m=positions), tuple(applied)

    def _allowance_changed(self, record: EventRecord) -> bool:
        """Whether the published solution of ``record`` was solved with another ephemeris
        allowance than the one in force (its rows are then solved again)."""
        if record.solution_out is None:
            return False
        entries = self._solvable_entries(record)
        if len(entries) < 2:
            return False
        _, allowance = self._solve_uncertainty(tuple(e.measurement for e in entries))
        return allowance != record.solution_allowance

    @staticmethod
    def _allowance_text(allowance: Sequence[EphemerisAllowance]) -> str | None:
        """The status sentence for a published solution solved with an ephemeris
        allowance, or None."""
        if not allowance:
            return None
        listed = _and_list(
            [
                f"{a.sat_id} ({a.scale:.2g} times its configured sigma, about "
                f"{max(a.sigma_ric_m) / 1000.0:.2g} km{', the largest scale allowed' if a.capped else ''})"
                for a in allowance
            ]
        )
        which = "position" if len(allowance) == 1 else "positions"
        tail = (
            " The allowance may still be too small: the transmitter may lie outside the ellipse."
            if any(a.capped for a in allowance)
            else ""
        )
        return (
            f"The ellipse includes an allowance for the {which} of {listed}: the reference calibrations put "
            f"{'it' if len(allowance) == 1 else 'them'} wrong beyond the configured uncertainty.{tail}"
        )

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
                unlocatable_reason=state.unlocatable_reason,
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
        # Once a consistent position exists, a satellite below the horizon of all of its
        # ellipse cannot relay the signal (the correlator works this out, ADAPT-1).
        for sat_id in record.not_visible_satellite_ids:
            if best_tp.pop(sat_id, None) is not None:
                notes.append(f"{sat_id} cannot see the transmitter's position, so it is not paired")
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

    def _check_level(self, record: EventRecord, state: _EventState, primary: _Pick) -> None:
        """Power or width step of the primary sighting on the same channel between rounds:
        another transmitter may have taken the frequency, so the event is re-checked
        (``EventBook.mark_recheck``; the next pair detections decide)."""
        unit = next((u for u in self._sightings(record.id) if u.channel_id == primary.channel_id), None)
        if unit is None:
            return
        snr, width = float(unit.snr_db), max(float(unit.down_hi_hz) - float(unit.down_lo_hz), 0.0)
        if not (math.isfinite(snr) and math.isfinite(width)):
            return
        previous, state.level = state.level, (primary.channel_id, snr, width)
        previous_ns, state.level_ns = state.level_ns, self._clock()
        if previous is None or previous[0] != primary.channel_id:
            return
        step = snr - previous[1]
        narrow, wide = sorted((previous[2], width))
        reason = None
        if abs(step) >= POWER_STEP_DB:
            reason = f"its level at {primary.channel_id} {'rose' if step > 0 else 'fell'} by {abs(step):.0f} dB between rounds"
        elif wide - narrow >= WIDTH_STEP_MIN_HZ and wide >= WIDTH_STEP_RATIO * max(narrow, 1e-9):
            reason = f"its width at {primary.channel_id} changed from {previous[2] / 1e3:.1f} kHz to {width / 1e3:.1f} kHz"
        if reason is not None:
            # the earlier level was read at the previous round: a track first detected by then
            # was measured before the step
            self._mark_recheck(record.id, state, reason, previous_ns if previous_ns is not None else self._clock())

    def _needs_classification(self, record: EventRecord, state: _EventState) -> bool:
        """Whether this round should classify the primary capture: when the event has
        no class, ``RECLASSIFY_UNKNOWN_S`` after an ``unknown``, ``RECLASSIFY_S`` after
        any other class (a class set elsewhere counts from when tasking first saw it),
        and once while the event is re-checked (a class change is evidence)."""
        if record.classification is None:
            return True
        now = self._clock()
        if record.recheck is not None and record.recheck_since_ns is not None and (
            state.classified_ns is None or state.classified_ns < record.recheck_since_ns
        ):
            return True
        if state.class_capture_end_ns is not None:
            sinces = [t.since_ns for t in state.tracks.values() if t.confirmed is not None and t.since_ns is not None]
            if sinces and min(sinces) > state.class_capture_end_ns:
                # every track measured now began after the class was measured: the class may
                # describe an earlier signal on the frequency (a swept jammer replaced by another)
                return True
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

    def _satellites_needed(
        self, primary: _Pick, partners: Sequence[_Pick], plans: Sequence[_RefPlan], lo_hz: float, hi_hz: float
    ) -> tuple[str, ...]:
        """The satellites a round's geometry must place: the picks', every satellite
        relaying the event's range or a planned reference (their copies may reach a
        channel), in configuration order. Satellites nothing of the round uses are not
        evaluated, so one whose ephemeris has ended cannot stop other events' rounds."""
        wanted = {primary.sat_id, *(p.sat_id for p in partners)}
        wanted.update(sat.id for sat, _ in self._config.transponders_covering(lo_hz, hi_hz))
        for plan in plans:
            wanted.update(sat.id for sat, _ in self._config.transponders_covering(plan.f_ref_hz, plan.f_ref_hz))
        return tuple(sat.id for sat in self._config.satellites if sat.id in wanted)

    # ------------------------------------------------------------------ calibration

    @staticmethod
    def _cal_group(a: _Pick, b: _Pick) -> tuple[tuple[str, str], tuple[str, str]]:
        first, second = (a.sat_id, a.site_id), (b.sat_id, b.site_id)
        return (first, second) if first <= second else (second, first)

    def _references_for(
        self, a: _Pick, b: _Pick, lo_hz: float, hi_hz: float, f_e_hz: float, near: tuple[float, float] | None = None
    ) -> list[Any]:
        """Reference carriers relayed by both transponders of a pair, none overlapping
        the event's band: nearest to ``near`` (the event's current fix, latitude and
        longitude) first when given, then nearest in frequency. A reference near the
        emitter shares more of its ephemeris and propagation errors (MATH.md section 9)."""
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
        def distance(carrier: Any) -> float:
            if near is None or carrier.location is None:
                return 0.0
            d = _finite(geodesic_distance_m(near[0], near[1], carrier.location.lat_deg, carrier.location.lon_deg))
            return d if d is not None else math.inf

        out.sort(key=lambda c: (distance(c), abs(float(c.uplink_center_hz) - f_e_hz)))
        return out

    def _plan_references(
        self,
        primary: _Pick,
        partners: Sequence[_Pick],
        lo: float,
        hi: float,
        f_e: float,
        near: tuple[float, float] | None = None,
    ) -> list[_RefPlan]:
        """Reference calibrations due this round, one per pair at most (the first
        carrier of ``_references_for``); all channels of one carrier share its rate."""
        cfg = self._config
        interval_ns = int(float(cfg.processing.calibration.interval_s) * _NS)
        now = self._clock()
        plans: list[_RefPlan] = []
        for b in partners:
            carriers = self._references_for(primary, b, lo, hi, f_e, near)
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
        return self._set_reference_rates(plans)

    def _set_reference_rates(self, plans: Sequence[_RefPlan]) -> list[_RefPlan]:
        """One sample rate per carrier for every channel of its slot (the lowest
        maximum of them caps it); plans left without a rate are dropped."""
        capture_cfg = self._config.processing.capture
        for carrier_id in {p.carrier_id for p in plans}:
            group = [p for p in plans if p.carrier_id == carrier_id]
            limit = min([p.a.max_rate_hz for p in group] + [p.b.max_rate_hz for p in group])
            want = max(float(capture_cfg.min_sample_rate_hz), float(capture_cfg.oversample) * group[0].bandwidth_hz)
            fs = float(math.floor(min(math.ceil(want), limit)))
            for plan in group:
                plan.fs = fs
        return [p for p in plans if p.fs > 0]

    def _watching(
        self, sat_id: str, transponder_id: str, site_id: str, f_e_hz: float, state: _EventState, quality: QualityTable
    ) -> _Pick | None:
        """The usable channel at a site that watches a satellite (best downlink quality)."""
        best: _Pick | None = None
        for site, channel in self._config.channels_watching(sat_id):
            if site.id != site_id:
                continue
            pick, _ = self._pick(channel.id, sat_id, transponder_id, f_e_hz, state, quality, require_quality=True)
            if pick is not None and (best is None or (pick.quality_db or -math.inf) > (best.quality_db or -math.inf)):
                best = pick
        return best

    def _plan_clock_checks(
        self,
        primary: _Pick,
        partners: Sequence[_Pick],
        planned: Sequence[_RefPlan],
        lo: float,
        hi: float,
        f_e: float,
        state: _EventState,
        quality: QualityTable,
        near: tuple[float, float] | None = None,
    ) -> list[_RefPlan]:
        """Same-satellite clock checks due this round (R3, ``site_clock_offsets``).

        A cross-satellite calibration holds two sites' clock difference plus the two
        satellites' errors; when every calibration of a site goes through the same
        satellite there, its clock cannot be told from that satellite. A clock check
        calibrates one satellite's copy of a reference at two sites: the satellite's own
        error is common to both, so the correction is the clock difference (plus the
        site-dependent remainder of the ephemeris error), and checks through two
        satellites tell a clock fault (alike through both) from a satellite position
        error (through one only).

        Each check links the primary's site with another site through one of the round's
        satellites (the round's own channel on it where it is at one of the two sites,
        otherwise the best usable channel there watching it). The other sites are the
        partners' first, then every site of a cross-site TDOA calibration still valid (a
        partner of an earlier round, for example a rotated one). Each check is due every
        ``calibration.interval_s`` like a pair's calibration; what a site lacks comes first
        (a satellite it has no valid check through, then sites checked through fewer than
        two satellites), then the never-tried and the longest since tried;
        ``CLOCK_CHECKS_PER_ROUND`` less the round's own calibrations are taken (none in a
        round that calibrates two pairs: later rounds catch up, and a rotated partner's
        site stays a candidate while its calibration is valid). A channel already tuned
        for another satellite in the carrier's slot is not used.
        """
        budget = CLOCK_CHECKS_PER_ROUND - len(planned)
        if budget <= 0:
            return []
        cal_cfg = self._config.processing.calibration
        interval_ns = int(float(cal_cfg.interval_s) * _NS)
        max_age_ns = int(float(cal_cfg.max_age_s) * _NS)
        now = self._clock()
        tuned: dict[tuple[str, str], float] = {}
        for plan in planned:
            for pick in (plan.a, plan.b):
                tuned.setdefault((pick.channel_id, plan.carrier_id), pick.translation_hz)
        sites: list[str] = []
        for b in partners:
            if b.site_id != primary.site_id and b.site_id not in sites:
                sites.append(b.site_id)
        held = sorted(self._calibrations.items(), key=lambda item: (-item[1].t_ns, item[0]))
        checked: dict[str, set[str]] = {}  # site -> satellites it has a valid clock check through
        for (key, _carrier), entry in held:
            if key[4] != "tdoa" or key[1] == key[3] or abs(now - entry.t_ns) > max_age_ns:
                continue
            for site_id in (key[1], key[3]):
                if key[0] == key[2]:
                    checked.setdefault(site_id, set()).add(key[0])
                if site_id != primary.site_id and site_id in self._sites_xyz and site_id not in sites:
                    sites.append(site_id)
        on_sat = [primary, *(b for b in partners if b.sat_id != primary.sat_id)]

        def end_at(mine: _Pick, site_id: str) -> _Pick | None:
            if mine.site_id == site_id:
                return mine
            return self._watching(mine.sat_id, mine.transponder_id, site_id, f_e, state, quality)

        candidates: list[tuple[tuple[bool, bool], int, int, _RefPlan]] = []
        groups: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        for site_id in sites:
            for mine in on_sat:
                a_end, b_end = end_at(mine, primary.site_id), end_at(mine, site_id)
                if a_end is None or b_end is None or a_end.channel_id == b_end.channel_id:
                    continue
                group = self._cal_group(a_end, b_end)
                if group in groups:
                    continue
                carriers = self._references_for(a_end, b_end, lo, hi, f_e, near)
                if not carriers:
                    continue
                carrier = carriers[0]
                last = self._cal_attempts.get((group, carrier.id))
                if last is not None and now - last < interval_ns:
                    continue
                groups.add(group)
                bandwidth = (
                    float(carrier.emitter.bandwidth_hz) if carrier.emitter.bandwidth_hz > 0 else float(carrier.bandwidth_hz)
                )
                plan = _RefPlan(
                    a=a_end,
                    b=b_end,
                    carrier_id=carrier.id,
                    f_ref_hz=float(carrier.uplink_center_hz),
                    bandwidth_hz=max(bandwidth, _MIN_SIGNAL_BW_HZ),
                    x_ref=np.asarray(self._config.carrier_xyz(carrier.id), dtype=np.float64),
                    clock=True,
                )
                # what a site lacks first: a satellite it was not checked through, then sites
                # checked through fewer than two satellites, then the longest since tried
                have = checked.get(site_id, set())
                need = (mine.sat_id in have, len(have) >= 2)
                candidates.append((need, -1 if last is None else last, len(candidates), plan))
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))
        chosen: list[_RefPlan] = []
        for _, _, _, plan in candidates:
            if len(chosen) >= budget:
                break
            if any(
                tuned.get((pick.channel_id, plan.carrier_id), pick.translation_hz) != pick.translation_hz
                for pick in (plan.a, plan.b)
            ):
                continue
            for pick in (plan.a, plan.b):
                tuned.setdefault((pick.channel_id, plan.carrier_id), pick.translation_hz)
            chosen.append(plan)
        return chosen

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
        """3-sigma ephemeris allowance of a pair's TDOA (s) and FDOA (Hz), both legs:
        ``6 sum |sigma_pos| / c`` and ``6 f sum |sigma_vel| / c`` over the pair's satellites."""
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
    def _rate_bounds(
        kin: Mapping[str, _Kinematics],
        sat_a: str,
        sat_b: str,
        f_hz: float,
        translation_a_hz: float = 0.0,
        translation_b_hz: float = 0.0,
    ) -> tuple[float, float]:
        """Largest |d TDOA / dt| (s/s) and |d FDOA / dt| (Hz/s) the satellites' motion
        allows, times ``RATE_SAFETY``. Each leg's range rate is bounded by the
        satellite's speed; its range acceleration by ``|a| + |v|^2 / r_min``
        (``_Kinematics.range_accel_bound``), which holds for every orbit. The FDOA
        bound weights the uplink legs by ``|f|`` and each downlink leg by its own
        downlink frequency ``|f + T|`` (a downlink above the uplink, as on MUOS, moves
        more)."""
        ka, kb = kin.get(sat_a), kin.get(sat_b)
        if ka is None or kb is None:
            return math.inf, math.inf
        tdoa_rate = 2.0 * (ka.speed_mps + kb.speed_mps) / C
        ra, rb = ka.range_accel_bound(), kb.range_accel_bound()
        fdoa_rate = (abs(f_hz) * (ra + rb) + abs(f_hz + translation_a_hz) * ra + abs(f_hz + translation_b_hz) * rb) / C
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
            # a same-satellite pair (clock check) measures that satellite's own copies: not excluded
            heard=tuple(
                h for h in self._heard(elevation, a, b, f_hz, width_hz, fc_a, fc_b, fs)
                if not (a.sat_id == b.sat_id and h[0] == a.sat_id)
            ),
            direct_point=a.site_id == b.site_id and a.translation_hz == b.translation_hz,
            delay_window=windows[0],
            doppler_window=(windows[1][0] - shift, windows[1][1] - shift),
            mirror_window=mirror_raw,
            signal_bw_hz=signal_bw_hz,
            min_snr_db=float(self._config.processing.caf.min_snr_db),
            max_tdoa_sigma_s=float(self._config.processing.caf.max_tdoa_sigma_s),
            orbits=self._orbits,
            sites_xyz=self._sites_xyz,
            capture_a=_capture_id(a.channel_id, cap_a),
            capture_b=_capture_id(b.channel_id, cap_b),
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
        self._check_level(record, state, primary)
        if partners and state.unlocatable_reason == UNLOCATABLE_ONE_SATELLITE:
            state.unlocatable_reason = None
        need_class = self._needs_classification(record, state)
        if not partners and not need_class:
            report.skipped = "no second satellite relays the event" + (f" ({'; '.join(notes)})" if notes else "")
            state.state = "idle"
            state.message = (
                "No second satellite with a usable channel relays this frequency, so the emitter cannot be located."
            )
            relaying = {sat.id for sat, _ in cfg.transponders_covering(lo, hi)} - set(record.not_visible_satellite_ids)
            if len(relaying) < 2:  # the frequency plan itself, not a channel that is down
                state.unlocatable_reason = UNLOCATABLE_ONE_SATELLITE
            self._publish_status(state)
            return report

        base_duration = float(capture_cfg.duration_s)
        duration = float(state.duration_s) if state.duration_s else base_duration
        max_rate = min([primary.max_rate_hz] + [p.max_rate_hz for p in partners])
        fs_guess = float(min(max(float(capture_cfg.min_sample_rate_hz), float(capture_cfg.oversample) * width), max_rate))
        fix = record.solution_out
        near = (fix.lat_deg, fix.lon_deg) if fix is not None and fix.is_fix else None
        ref_plans = (
            self._plan_references(primary, partners, lo, hi, f_e, near) if cfg.processing.calibration.enabled else []
        )
        clock_plans = (
            self._plan_clock_checks(primary, partners, ref_plans, lo, hi, f_e, state, quality, near)
            if cfg.processing.calibration.enabled
            else []
        )
        ref_plans = self._set_reference_rates([*ref_plans, *clock_plans])
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
                sat_ids=self._satellites_needed(primary, partners, ref_plans, lo, hi),
                site_ids=tuple(
                    sorted({p.site_id for p in [primary, *partners]} | {k.site_id for p in ref_plans for k in (p.a, p.b)})
                ),
                ground_height_m=self._ground_height_m,
            ),
        )
        self._abort_if_stale(generation)
        for sat_id, why in geo.unavailable.items():
            report.notes.append(f"satellite {sat_id}: orbit not available ({why})")
        state.unavailable = dict(geo.unavailable)
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
            rates = self._rate_bounds(
                geo.kinematics, primary.sat_id, b.sat_id, f_e, primary.translation_hz, b.translation_hz
            )
            kept.append((b, pg, windows, mirror, rates))
            max_doppler = max(max_doppler, abs(windows[1][0]), abs(windows[1][1]))
            if mirror is not None:
                max_doppler = max(max_doppler, abs(mirror[1][0]), abs(mirror[1][1]))
        partners = [k[0] for k in kept]
        # a clock check stands on its own; a pair's calibration goes with its pair
        ref_plans = [p for p in ref_plans if p.clock or p.b in partners]
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
                    # the FDOA-rate bound is already a worst case: no safety factor here
                    duration = min(duration, 1.0 / math.sqrt(rf / RATE_SAFETY))
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

        lead_s = self._lead_s([primary, *partners, *(pick for p in ref_plans for pick in (p.a, p.b))])
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
            contexts.append(_PairCtx(primary, b, pair_ref, path_a, path_b, t_mid_ns, shift, blocked, rates, outcome,
                                     job.capture_a, job.capture_b))
            pair_jobs.append(self._pool.run(_correlate, job))
        pair_results = list(await asyncio.gather(*pair_jobs, return_exceptions=True)) if pair_jobs else []
        self._abort_if_stale(generation)

        new_used = False
        display: tuple[float, CafResult, schemas.PairRef, int] | None = None
        assessed: list[tuple[_PairCtx, CafResult, _PairEval]] = []
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
            assessed.append((ctx, result, _evaluate_pair(ctx, result)))
            snr = _finite(result.snr_db)
            rank = snr if (snr is not None and result.detected) else -math.inf
            if display is None or rank > display[0]:
                display = (rank, result, ctx.pair_ref, ctx.t_mid_ns)
        # Track decisions first (has another transmitter taken the frequency?), so that a
        # set-aside happens before this round's rows are added (MATH.md section 8).
        decisions = self._track_decisions(event_id, record, state, assessed)
        for (ctx, result, evaluation), (confirmed, why) in zip(assessed, decisions):
            new_used |= self._add_rows(event_id, record, state, ctx, result, evaluation, confirmed, why)
        if display is not None:
            try:
                self._book.set_caf(event_id, caf_display(display[1], display[2], display[3]))
            except (ValidationError, ValueError) as exc:
                logger.warning("correlation surface of %s not shown: %s", event_id, exc, extra={"event_id": event_id})

        # ---- solve --------------------------------------------------------------------------
        if new_used or self._has_unsolved_rows(record, state) or self._allowance_changed(record):
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
        doubted = {s for e in self.site_clock_estimates() for s in e.suspect_satellite_ids}
        self._book.set_satellite_doubt(event_id, doubted)
        self._update_allowance(doubted)  # the next solve uses it
        if new_used:
            state.unlocatable_reason = None
        elif explanation in (EXPLAIN_AMBIGUOUS, EXPLAIN_COUPLED):
            state.unlocatable_reason = (
                UNLOCATABLE_AMBIGUOUS if explanation == EXPLAIN_AMBIGUOUS else UNLOCATABLE_COUPLED
            )
        if not failed_keys and any(p.reason in (REASON_UNCONFIRMED, REASON_INCONSISTENT) for p in report.pairs):
            parts.append("Waiting for a repeat detection before the new measurements are used.")
        for sat_id, why in state.unavailable.items():
            parts.append(f"Satellite {sat_id}: orbit not available ({why}); its pairs are not tasked.")
        if state.fit_note:
            parts.append(state.fit_note)
        allowance_note = self._allowance_text(record.solution_allowance) if record.solution_out is not None else None
        if allowance_note:
            parts.append(allowance_note)
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
        record = self._book.resolve(event_id)
        previous = record.classification if record is not None else None
        previous_end = state.class_capture_end_ns
        if self._book.set_classification(event_id, result):
            state.classified_ns = self._clock()
            capture = capture_out.capture
            if capture is not None:
                state.class_capture_end_ns = int(capture.t_first_ns) + int(
                    round(np.shape(capture.iq)[0] / float(capture.sample_rate_hz) * _NS)
                )
            report.classification = str(result.label)
            label, confidence = str(result.label), _finite(getattr(result, "confidence", None)) or 0.0
            if (
                previous is not None
                and previous.label != label
                and "unknown" not in (previous.label, label)
                and min(previous.confidence, confidence) >= CLASS_CHANGE_CONFIDENCE
            ):
                # the earlier class came from the capture that ended at previous_end: a track
                # first detected by then was measured while the waveform was the earlier one
                self._mark_recheck(
                    event_id, state, f"its waveform changed from {previous.label} to {label}",
                    previous_end if previous_end is not None else self._clock(), keep_class=True,
                )
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
        for kind, correction, cal, position, media in outcome.corrections:
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
                position_partials=position,
                media_partials=media,
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

    def _mark_recheck(
        self, event_id: str, state: _EventState, reason: str, tie_ns: int, *, keep_class: bool = False
    ) -> bool:
        """``EventBook.mark_recheck`` remembering what the re-check compares: a track first
        detected at or before ``tie_ns`` (labelled UTC ns) was measured before the change.
        keep_class: the event's class was measured on the new signal (a class change)."""
        if not self._book.mark_recheck(event_id, reason):
            return False
        record = self._book.resolve(event_id)
        if record is not None and record.recheck_since_ns is not None:
            state.recheck_tie = (int(record.recheck_since_ns), int(tie_ns), reason, keep_class)
            state.recheck_waits = 0
        return True

    @staticmethod
    def _recheck_tie(record: EventRecord, state: _EventState) -> tuple[int, str, bool]:
        """``(tie_ns, what changed, keep_class)`` of the event's re-check. A re-check the
        book raised (detected again after a short break) ties tracks first detected
        before the event was lost."""
        tie = state.recheck_tie
        if tie is not None and record.recheck_since_ns is not None and tie[0] == int(record.recheck_since_ns):
            return tie[1], tie[2], tie[3]
        if record.resumed_ns is not None and record.break_s is not None:
            lost_ns = int(record.resumed_ns) - int(round(float(record.break_s) * _NS))
        else:
            lost_ns = int(record.onset) - 1
        return lost_ns, "it was detected again after a break", False

    @staticmethod
    def _track_check(
        state: _EventState, key: tuple[str, str], detection: _Detection, rates: tuple[float, float]
    ) -> _TrackCheck:
        """How a detection relates to its pair's track, without changing it."""
        track = state.tracks.get(key)
        if track is None or track.confirmed is None:
            return _TrackCheck(False, False, False, detection.t_ns)
        if _consistent(track.confirmed, detection, rates):
            return _TrackCheck(True, False, False, detection.t_ns)
        matches = [c.t_ns for c in track.candidates if _consistent(c, detection, rates)]
        return _TrackCheck(False, True, bool(matches), min(matches, default=detection.t_ns))

    def _track_decisions(
        self,
        event_id: str,
        record: EventRecord,
        state: _EventState,
        assessed: Sequence[tuple[_PairCtx, CafResult, _PairEval]],
    ) -> list[tuple[bool, str | None]]:
        """Confirmation of each pair's detection, with the event-level check for another
        transmitter on the frequency (K2). Returns ``(confirmed, reason)`` per pair.

        - A pair whose new detections repeat at a TDOA/FDOA its confirmed track cannot
          reach under the satellites' motion has *jumped*. If no pair of the round agrees
          with its own track, another transmitter (or the same one, moved) holds the
          frequency: ``EventBook.transmission_changed`` sets everything aside, the jumped
          pairs keep their new track and are used, every other track starts again.
        - A jump while another pair agrees is a conflict: the jumped pair is not used and
          the event is re-checked.
        - While the event is re-checked (``EventRecord.recheck``: back after a short
          break, or its waveform, power or width changed), one agreeing pair keeps the
          position; a disagreeing detection with none agreeing sets it aside.
        """
        if record.set_aside_count != state.aside_seen:
            # set aside elsewhere (a long break, a local signal): the tracks describe the
            # signal measured before
            state.tracks.clear()
            state.aside_seen = record.set_aside_count
        checks: list[_TrackCheck | None] = []
        for ctx, _, evaluation in assessed:
            if evaluation.detection is None:
                checks.append(None)
                continue
            checks.append(self._track_check(state, (ctx.a.channel_id, ctx.b.channel_id), evaluation.detection, ctx.rates))
        agree = [i for i, c in enumerate(checks) if c is not None and c.agrees]
        jumped = [i for i, c in enumerate(checks) if c is not None and c.jumped]
        disagree = [i for i, c in enumerate(checks) if c is not None and c.disagrees]

        def name(i: int) -> str:
            ctx = assessed[i][0]
            return f"{ctx.a.channel_id}/{ctx.b.channel_id}"

        def old(track: _PairTrack | None, tie_ns: int) -> bool:
            return track is not None and track.confirmed is not None and (
                track.since_ns is None or track.since_ns <= tie_ns
            )

        mode = "normal"
        if record.recheck is None:
            state.recheck_waits = 0
        tie_ns, changed, keep_class = self._recheck_tie(record, state) if record.recheck is not None else (0, "", False)
        if jumped and agree:
            mode = "conflict"
        elif record.recheck is not None and agree:
            # Only a track measured before the change can tell the two apart: one first
            # detected since agrees with itself whichever transmitter it measures.
            tied = [i for i in agree if old(state.tracks.get((assessed[i][0].a.channel_id, assessed[i][0].b.channel_id)), tie_ns)]
            if tied:
                self._book.clear_recheck(
                    event_id, f"pair {name(tied[0])} measures the same time and frequency differences as before"
                )
            elif disagree:
                mode = "change"
            elif not any(old(track, tie_ns) for track in state.tracks.values()):
                mode = "renewed"
            else:
                # an earlier track, not detected in this round, decides later; if none comes
                # back, the change cannot be tested and the position is kept as before
                state.recheck_waits += 1
                if state.recheck_waits >= RECHECK_WAIT_ROUNDS:
                    self._book.clear_recheck(
                        event_id,
                        f"pair {name(agree[0])} agrees with its own measurements, and no pair measured before the change "
                        f"was measured again in {state.recheck_waits} rounds, so the change could not be tested",
                    )
                    state.recheck_waits = 0
        elif record.recheck is not None and disagree:
            mode = "change"
        elif jumped:
            mode = "change"

        if mode == "renewed":
            # Nothing measured before the change ties it to what is measured now: the current
            # transmission is the one the tracks measure, from the first detection of the
            # oldest of them; the rows measured since stay used.
            sinces = [t.since_ns for t in state.tracks.values() if t.confirmed is not None and t.since_ns is not None]
            onset = min(sinces)
            keep = [e.seq for e in record.used_measurements() if int(e.row.t_ns) >= onset]
            why = (
                f"{changed}, and every pair measured now was first detected after that, so nothing ties the "
                "signal now to the one measured before"
            )
            self._count("transmission_changes")
            self._book.transmission_changed(event_id, why, onset_ns=onset, keep=keep, keep_classification=keep_class)
            state.aside_seen = record.set_aside_count
            state.recheck_tie = None
            state.solves = 0
            mode = "normal"

        out: list[tuple[bool, str | None]] = []
        if mode == "change":
            first = min(c.onset_ns for i, c in enumerate(checks) if c is not None and i in disagree)
            why = (
                f"the time and frequency differences of pair {name(disagree[0])} moved further than the "
                "satellites' motion allows"
            )
            if record.recheck is not None:
                why += " while the event was being re-checked"
            self._count("transmission_changes")
            self._book.transmission_changed(event_id, why, onset_ns=first)
            state.tracks.clear()
            state.aside_seen = record.set_aside_count
            state.recheck_tie = None
            state.solves = 0
            for i, ((ctx, _, evaluation), check) in enumerate(zip(assessed, checks)):
                if check is None or evaluation.detection is None:
                    out.append((False, None))
                    continue
                track = state.tracks.setdefault((ctx.a.channel_id, ctx.b.channel_id), _PairTrack())
                if i in jumped:
                    track.confirmed = evaluation.detection  # the new transmitter's track, already repeated
                    track.since_ns = check.onset_ns
                    out.append((True, None))
                else:
                    track.candidates.append(evaluation.detection)
                    out.append((False, REASON_UNCONFIRMED))
            return out
        if mode == "conflict":
            self._mark_recheck(
                event_id,
                state,
                f"pair {name(jumped[0])} measures new time and frequency differences while pair {name(agree[0])} "
                "agrees with the earlier ones",
                self._clock(),
            )
        for i, ((ctx, _, evaluation), check) in enumerate(zip(assessed, checks)):
            if check is None or evaluation.detection is None:
                out.append((False, None))
            elif mode == "conflict" and i in jumped:
                state.tracks[(ctx.a.channel_id, ctx.b.channel_id)].candidates.append(evaluation.detection)
                out.append((False, REASON_INCONSISTENT))
            else:
                out.append(self._confirm(state, (ctx.a.channel_id, ctx.b.channel_id), evaluation.detection, ctx.rates))
        return out

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
                track.since_ns = int(candidate.t_ns)
                track.candidates.clear()
                return True, None
        track.candidates.append(detection)
        return False, REASON_INCONSISTENT if track.confirmed is not None else REASON_UNCONFIRMED

    def _add_rows(
        self,
        event_id: str,
        record: EventRecord,
        state: _EventState,
        ctx: _PairCtx,
        result: CafResult,
        evaluation: _PairEval,
        confirmed: bool,
        why: str | None,
    ) -> bool:
        """The TDOA and FDOA rows of one correlated pair (``_evaluate_pair``, then the
        confirmation ``_track_decisions`` gave); True when one is used."""
        outcome = ctx.outcome
        tdoa, fdoa, sig_t, sig_f = evaluation.tdoa, evaluation.fdoa, evaluation.sig_t, evaluation.sig_f
        reason_t, reason_f = evaluation.reason_t, evaluation.reason_f
        usable_t, usable_f = reason_t is None, reason_f is None
        if evaluation.detection is not None:
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
                share = result.noise_share_tdoa if kind == "tdoa" else result.noise_share_fdoa
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
                    # every pair of a round shares the primary capture: its noise is common to their rows
                    capture_a=ctx.capture_a,
                    capture_b=ctx.capture_b,
                    noise_share_a=float(share[0]),
                    noise_share_b=float(share[1]),
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

    async def _split_test(
        self,
        event_id: str,
        record: EventRecord,
        state: _EventState,
        entries: Sequence[Any],
        cut_seq: int,
        limit: float,
        generation: int,
    ) -> Solution | None:
        """The fit broke: the rows up to ``cut_seq`` fitted one transmitter, all rows now
        do not. When the rows added since (at least ``SPLIT_MIN_ROWS``) fit a transmitter
        of their own, they belong to another transmitter (or the same one, moved): the
        earlier rows are set aside (``EventBook.transmission_changed``, keeping the new
        ones) and the new rows' solution is attached. Returns it, or None (the new rows
        are not consistent by themselves: the inconsistent fit stays published)."""
        new = [e for e in entries if e.seq > cut_seq]
        old = [e for e in entries if e.seq <= cut_seq]
        if len(new) < SPLIT_MIN_ROWS or len(old) < 2:
            return None
        cfg = self._config.processing
        new_rows = tuple(e.measurement for e in new)
        uncertainty, allowance = self._solve_uncertainty(new_rows)
        job = _SolveJob(new_rows, self._orbits, self._sites_xyz, cfg.solver, cfg.region, uncertainty, None)
        solution, _ = await self._pool.run(_solve, job)
        self._abort_if_stale(generation)
        if solution is None or not assess_fit(solution.chi2, solution.dof, solution.reduced_chi2, limit).consistent:
            return None
        onset = min(int(e.row.t_ns) for e in new)
        self._count("transmission_changes")
        self._book.transmission_changed(
            event_id,
            "the newest measurements fit one transmitter of their own but not together with the earlier ones",
            onset_ns=onset,
            keep=[e.seq for e in new],
        )
        # tracks confirmed on the new transmission stay; the others start again
        state.tracks = {
            key: track
            for key, track in state.tracks.items()
            if track.confirmed is not None and track.confirmed.t_ns >= onset
        }
        state.aside_seen = record.set_aside_count
        state.solves = 1
        new_seqs = tuple(e.seq for e in new)
        try:
            self._book.set_solution(
                event_id, solution, self._clock(), used=new_seqs, max_reduced_chi2=limit,
                max_semi_major_m=fix_semi_major_limit_m(self._config.processing.region), allowance=allowance,
            )
        except ValueError as exc:
            logger.error("solution of %s not attached: %s", event_id, exc, extra={"event_id": event_id})
            return None
        state.last_solved_seq = max(state.last_solved_seq, max(new_seqs))
        return solution

    @staticmethod
    def _fitted_one_transmitter(solution: schemas.SolutionOut | None) -> bool:
        """Whether a published solution showed that its rows fit one transmitter: a
        consistent fit with redundancy (``dof >= 1``). Rows that merely determine a
        position (no redundancy) were never checked, so a later break says nothing about
        another transmitter (``_split_test``)."""
        return solution is not None and solution.fit_consistent and solution.dof >= 1

    @staticmethod
    def _withdrawal_text(published: Sequence[int], solved: Sequence[int], reason: str | None) -> str | None:
        """Why a published position is withdrawn after a failed solve over ``solved``
        (entry seqs, oldest first), or None when the failure says nothing about it (R6).

        The solve covers the newest ``solver.max_measurements`` rows. It speaks about the
        published position when every row that position rests on is among the rows
        solved, or older than all of them (it slid out of the solver window): the rows
        solved are then the published ones still in reach plus everything newer. A
        published row that is newer than the oldest row solved and still not among them
        (for example a row whose satellite left the configuration) means the solve was
        over other rows, and nothing is withdrawn."""
        if not published or not solved:
            return None
        window = set(solved)
        oldest = min(solved)
        if any(seq not in window and seq >= oldest for seq in published):
            return None
        kept = sum(1 for seq in published if seq in window)
        if kept == len(published):
            return (
                f"the newest measurements, added to the {len(published)} it rested on, admit no position ({reason})"
            )
        if kept:
            return (
                f"the newest {len(solved)} measurements (the solver window), which hold every measurement since the "
                f"position and {kept} of the {len(published)} it rested on, admit no position ({reason})"
            )
        return (
            f"the newest {len(solved)} measurements (the solver window), all newer than the {len(published)} it "
            f"rested on, admit no position ({reason})"
        )

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

        uncertainty, allowance = self._solve_uncertainty(rows)

        def job(start_points: tuple[tuple[float, float], ...] | None) -> _SolveJob:
            return _SolveJob(rows, self._orbits, self._sites_xyz, cfg.solver, cfg.region, uncertainty, start_points)

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
        limit = float(cfg.solver.max_reduced_chi2)
        before, before_used = record.solution_out, record.solution_used
        try:
            self._book.set_solution(
                event_id, solution, t_ns, used=seqs if solution is not None else (), reason=reason,
                max_reduced_chi2=limit, max_semi_major_m=fix_semi_major_limit_m(cfg.region), allowance=allowance,
            )
        except ValueError as exc:
            self._count("solve_errors")
            logger.error("solution of %s not attached: %s", event_id, exc, extra={"event_id": event_id})
            reason = f"solution not attached: {exc}"
            solution = None
        state.last_solved_seq = max(seqs)
        state.solve_reason = None if solution is not None else reason
        report.solution, report.solve_reason = solution, reason
        broke = solution is None or (record.solution_out is not None and not record.solution_out.fit_consistent)
        if self._fitted_one_transmitter(before) and before_used and broke:
            # The fit broke (no solution, or an inconsistent one) when rows were added to
            # rows that fitted one transmitter: test the newest rows on their own (K2).
            split = await self._split_test(event_id, record, state, entries, max(before_used), limit, generation)
            if split is not None:
                solution, reason = split, None
                state.solve_reason = None
                report.solution, report.solve_reason = solution, None
        withdraw = self._withdrawal_text(record.solution_used, seqs, reason) if solution is None else None
        if withdraw is not None and record.solution_out is not None:
            # R6: the rows the published position rests on, with the newest added, admit no
            # position: it no longer fits what is measured
            self._count("solutions_withdrawn")
            self._book.withdraw_solution(event_id, withdraw)
            state.fit_note = None
        current = record.solution_out
        if solution is not None and current is not None and not current.fit_consistent:
            self._count("solves_inconsistent")
            state.fit_note = (
                f"The measurements do not fit one stationary transmitter (reduced chi-square "
                f"{_chi2_text(current.reduced_chi2)}, limit {limit:g}): more than one transmitter may share the "
                "frequency, the transmitter may be moving, or a site clock or satellite position may be wrong. The "
                "position is shown with its ellipse enlarged and is not a fix."
            )
        elif solution is not None and current is not None and not current.is_fix:
            self._count("solves_not_fix")
            state.fit_note = f"The position is shown but is not a fix: {current.not_fix_reason}."
        elif solution is not None:
            state.fit_note = None
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
