"""Console API contract: the JSON shapes of PROTOCOL.md section B as pydantic models.

This module is the single source of truth for what the server sends to, and accepts
from, the operator console. The REST handlers use these models as their response and
request types (so `/openapi.json` matches them), and the console's TypeScript types
are generated from ``console_json_schema()``:

    python -m petal_locate.server.api.schemas > schema.json

Conventions:

- Every absolute time ending in ``_ns`` is integer nanoseconds since the Unix epoch
  in Python and a **decimal string** in JSON (``ConsoleTimeNs``).
- Other times are ISO 8601 UTC text ending in ``Z``.
- Nothing is ever NaN or infinite: a value that could not be computed is ``null``.
- Frequencies are Hz, angles degrees, distances meters unless the name says otherwise.
- Models are tolerant on input only where stated; responses are exact.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from petal_locate.config.models import EmitterModel, Region, SystemConfig, UncertaintyConfig
from petal_locate.protocol.messages import ClockStatus, ConsoleTimeNs
from petal_locate.sim.scenario import Scenario

__all__ = [
    "API_PREFIX",
    "AccuracyGrid",
    "AccuracyPairSummary",
    "AccuracyRequest",
    "Advisory",
    "AmbiguitySummary",
    "CafCandidate",
    "CafCut",
    "CafDisplay",
    "ChannelRef",
    "ChannelStatus",
    "WindowStatus",
    "ClassificationOut",
    "ConfigEnvelope",
    "ConflictResponse",
    "EllipseOut",
    "ErrorResponse",
    "Event",
    "EventState",
    "EventSummary",
    "EventTransponder",
    "EventTruth",
    "EvidenceRow",
    "EvidenceState",
    "FieldErrorResponse",
    "FieldProblem",
    "Footprint",
    "LinkBudgetRequest",
    "LinkBudgetResponse",
    "LinkBudgetRow",
    "LookAngle",
    "LopCollection",
    "LopFeature",
    "LopProperties",
    "Marking",
    "MeasurementRow",
    "Meta",
    "NodeStatus",
    "PairRef",
    "Preset",
    "QueueStatus",
    "SatelliteStatus",
    "ScenarioEnvelope",
    "SimTruth",
    "SimTruthEmitter",
    "SimTruthEvent",
    "SiteClockEstimate",
    "SolutionHistoryRow",
    "SolutionOut",
    "SolutionSummary",
    "Status",
    "Subpoint",
    "TaskingStatus",
    "Transition",
    "ValidateResult",
    "Verdict",
    "WsConfigMessage",
    "WsEventMessage",
    "WsSimMessage",
    "WsStatusMessage",
    "WsSubscribe",
    "console_json_schema",
]

API_PREFIX = "/api/v1"

EventState = Literal["detected", "characterizing", "locating", "located", "lost", "closed"]
"""Event lifecycle (ARCHITECTURE.md section 7). ``lost`` and ``closed`` are inactive."""

Verdict = Literal["uplink", "local", "undetermined"]
"""Where the interference enters: the satellite uplink, one site's own receiver, or
not yet decidable (MATH.md section 7)."""

EvidenceState = Literal["seen", "not_seen_capable", "not_capable", "stale"]
"""Per receive channel: it sees the signal; it would see it but does not; it could
not see it anyway (below sensitivity or wrong satellite); its spectrum is too old."""


class _Out(BaseModel):
    """Base of every console model: unknown fields are errors, numbers are finite."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# --------------------------------------------------------------------------- meta, status


class Marking(_Out):
    """Marking banner from deployment configuration. ``level`` is the short level
    (for example ``UNCLASSIFIED`` or ``CUI``); ``line`` is the full banner text."""

    level: str
    line: str


class Meta(_Out):
    """``GET /meta``."""

    version: str
    marking: Marking | None
    simulation: bool
    server_t_ns: ConsoleTimeNs
    config_name: str
    api_token_required: bool = False


class FieldProblem(_Out):
    """One validation message. ``loc`` is a dotted path such as
    ``sites.2.channels.0.antenna.diameter_m`` (empty for the whole document)."""

    loc: str
    msg: str


class ValidateResult(_Out):
    """``POST /config/validate`` (always HTTP 200; ``ok`` says whether it validates)."""

    ok: bool
    errors: list[FieldProblem]
    warnings: list[FieldProblem]


class ErrorResponse(_Out):
    """Body of every error reply (4xx, 5xx): ``detail`` says what went wrong in plain
    words. No stack trace or internal state is ever included."""

    detail: str


class ConflictResponse(_Out):
    """HTTP 409 from ``PUT /config``: the ``If-Match`` version is not the one in force.
    ``current_version`` is the version to reload (also sent as the ``ETag`` header)."""

    detail: str
    current_version: int


class FieldErrorResponse(_Out):
    """HTTP 422: the request does not validate. ``errors`` name each field at fault
    (``loc`` is a dotted path from the document root, or from the query parameter);
    ``warnings`` are non-blocking findings when the server got that far."""

    detail: str
    errors: list[FieldProblem]
    warnings: list[FieldProblem] = Field(default_factory=list)


class ConfigEnvelope(_Out):
    """``GET /config`` and the reply to ``PUT /config``. ``version`` is also the ETag
    (quoted) and must be sent back in ``If-Match`` when saving. ``warnings`` are
    non-blocking findings of the same kind ``/config/validate`` returns.

    ``derived_monitors``: ids of the channels whose ``monitors`` the server derived from
    the frequency plan (one window per transponder of each watched satellite). They are
    listed in ``config`` so the console can show and stream them; send these channels
    back with ``monitors: []`` to keep them derived (they then follow transponder
    edits), or with the windows to make them explicit."""

    version: int
    config: SystemConfig
    warnings: list[FieldProblem] = Field(default_factory=list)
    derived_monitors: list[str] = Field(default_factory=list)


class WindowStatus(_Out):
    """Detection health of one monitor window of a channel.

    last_frame_age_s: seconds since the newest spectrum frame of the window reached the
    server (null before any). detection_frame_age_s: seconds since the detection
    pipeline last accepted one (null before any): it grows while frames arrive but are
    refused. blind: frames arrive but none has been accepted for ``BLIND_FRAMES`` frame
    periods; the channel is then not ``ok``. blind_reason: the drop reason of the
    latest refused frame (``out_of_order``, ``retuned``, ``nfft_mismatch``,
    ``non_finite``, ``tracker_error``, ``no_tracker``, ...), null when not blind.
    learning: the window's tracker still judges most bins against its start-up prior
    (the noise floor plus the relayed-noise rise) rather than their own history.
    floor_raised_db: how far the window's noise floor estimate lies above the lowest
    one among the channel's other windows of the same span, dB, when that exceeds the
    detection threshold: a signal filling the window may have been taken for its noise
    (null otherwise, or when the channel has no comparable window). note: one sentence
    for the operator when blind or floor_raised_db is set, else null.
    """

    monitor_id: str
    last_frame_age_s: float | None = None
    detection_frame_age_s: float | None = None
    blind: bool = False
    blind_reason: str | None = None
    learning: bool | None = None
    floor_raised_db: float | None = None
    note: str | None = None


class ChannelStatus(_Out):
    """channel_id, ok (ready, and no window blind to detection), last_frame_age_s (the
    newest frame over its windows). windows: one ``WindowStatus`` per assigned monitor
    window. detection_problem: a sentence naming blind or floor-raised windows, null
    when there are none."""

    channel_id: str
    ok: bool
    last_frame_age_s: float | None
    windows: list[WindowStatus] = Field(default_factory=list)
    detection_problem: str | None = None


class NodeStatus(_Out):
    """One row per configured site, connected or not."""

    site_id: str
    node_id: str | None
    online: bool
    latency_ms: float | None
    clock: ClockStatus | None
    driver: str | None
    software: str | None
    channels: list[ChannelStatus]
    connected_since: str | None


class QueueStatus(_Out):
    """Work the server has in hand."""

    captures_pending: int
    jobs_running: int
    jobs_waiting: int


class SatelliteStatus(_Out):
    """Whether the server can place one configured satellite now (its orbit can be
    evaluated at the server's time; checked every ``ORBIT_CHECK_S``).

    ephemeris_ok: the orbit evaluates now. unavailable_since_ns: when it stopped (the
    end of an ephemeris table when the orbit says so, otherwise the first check that
    failed); null while it is available. message: one sentence for the operator, for
    example "satellite MUOS Pacific (muos-pac): ephemeris not available since
    2026-11-13 03:12:00Z (t_s outside the ephemeris table span ...); its pairs are not
    tasked"; null while available.
    """

    sat_id: str
    ephemeris_ok: bool
    unavailable_since_ns: ConsoleTimeNs | None = None
    message: str | None = None


class SiteClockEstimate(_Out):
    """A site's clock offset estimated from reference-carrier calibrations (the TDOA
    corrections of cross-site pairs; MATH.md section 9).

    offset_us: estimated offset of the site's sample timestamps, labeled time minus true
    time, microseconds (positive: its clock runs ahead; negative: behind, "late"), relative to the
    median of the sites whose clock differences from it the calibrations determine (else
    of its group of compared sites; the least-squares fit of the cross-site TDOA
    corrections ``c ~ o_a - o_b``, including same-satellite clock checks, which carry no
    satellite term). sigma_us: RMS residual of the corrections it rests on, microseconds
    (null with a single correction). n_calibrations: TDOA calibrations used.
    compared_with: the other sites of those calibrations. caution: a clock fault is
    likely or cannot be ruled out: ``|offset_us|`` reaches the largest of
    ``tasking.CLOCK_CAUTION_NS`` (1000 ns), ``10 x sites[].clock.sigma_ns``, three times
    the site-dependent ephemeris allowance of its calibrations, and the largest difference
    between satellites any site of its group shows; two sites whose difference alone is
    known are both flagged on it (which one is off cannot be told). message: plain
    sentence, or null. With caution it says whether calibrations through two or more
    satellites agree (a clock fault) or names the satellite-position alternative (fewer
    satellites). Without caution it may say that the site's offset differs between
    satellites, so its clock is probably fine and a satellite's position is wrong beyond
    its configured uncertainty (that satellite is named). suspect_satellite_ids: the
    satellites that message names (empty otherwise); an event whose fix rests on them is
    told that its ellipse does not include that error.
    """

    site_id: str
    offset_us: float
    sigma_us: float | None
    n_calibrations: int
    compared_with: list[str]
    caution: bool
    message: str | None = None
    suspect_satellite_ids: list[str] = Field(default_factory=list)


class Status(_Out):
    """``GET /status``. satellites: one row per configured satellite (empty before the
    first orbit check). site_clocks: clock offsets estimated from calibrations, one row
    per site that took part in a cross-site TDOA calibration still valid (empty without
    calibrations)."""

    server_t_ns: ConsoleTimeNs
    config_version: int
    nodes: list[NodeStatus]
    events_active: int
    queue: QueueStatus
    satellites: list[SatelliteStatus] = Field(default_factory=list)
    site_clocks: list[SiteClockEstimate] = Field(default_factory=list)


# --------------------------------------------------------------------------- events


class ChannelRef(_Out):
    """One end of a measurement pair: a satellite heard through a receive channel."""

    sat_id: str
    site_id: str
    channel_id: str


class PairRef(_Out):
    """Measurements are A minus B."""

    a: ChannelRef
    b: ChannelRef


class EllipseOut(_Out):
    """Error ellipse of the horizontal position. ``azimuth_deg`` is the bearing of the
    major axis clockwise from true north, 0..180. ``confidence`` is 0..1."""

    semi_major_m: float
    semi_minor_m: float
    azimuth_deg: float
    confidence: float


class SolutionOut(_Out):
    """A position estimate (``core.solver.Solution.as_dict()``).

    Covariances are 2x2, m^2, ordered [east, north]. ``residuals`` are whitened and
    in the order of the measurements used. ``ambiguities`` are the other admissible
    solutions (for example the mirror image across the equator); they carry no
    ambiguities of their own. ``dominant_error`` is one of ``noise``,
    ``ephemeris_position``, ``ephemeris_velocity``, ``clock``, ``oscillator``,
    ``tdoa_bias``, ``reference``, ``ionosphere``, ``troposphere``
    (``core.solver.ERROR_SOURCES``).

    Fit consistency (set by the server when it publishes a solve; the defaults describe
    a solution the server did not assess). fit_p_value: probability that a chi-square
    with ``dof`` degrees of freedom reaches ``chi2`` (null when ``dof < 1``).
    fit_consistent: false when ``reduced_chi2`` exceeds ``processing.solver.
    max_reduced_chi2`` and ``fit_p_value`` is below 0.001: the measurements do not fit
    one stationary transmitter (rows of two transmitters, a moving one, a clock or
    ephemeris fault), so the position must not be reported as a fix; the event is then
    never ``located``. ellipse_scale: factor already applied to ``ellipse`` axes and
    ``cep50_m`` (and its square to the three covariances): ``sqrt(reduced_chi2)`` for an
    inconsistent fit, else 1. An ambiguity is assessed on its own chi-square.

    is_fix: whether the position may be reported as a fix: converged, ``fit_consistent``,
    with redundancy (``dof >= 1``: more measurements than the two unknowns of a position,
    so the fit can be checked against itself) and a 95 % semi-major axis within the
    server's sanity bound (``events.fix_semi_major_limit_m``: 1,000 km, or a quarter of
    the region's smaller extent when that is less). An event is ``located`` only while its
    solution is a fix. not_fix_reason: one plain clause saying why it is not (for example
    "no redundancy: 2 measurements for the two unknowns of a position, so the fit cannot
    be checked against itself"); null for a fix.
    """

    lat_deg: float
    lon_deg: float
    height_m: float
    cov_en_m2: list[list[float]]
    cov_noise_en_m2: list[list[float]]
    cov_consider_en_m2: list[list[float]]
    ellipse: EllipseOut
    cep50_m: float
    chi2: float
    dof: int
    reduced_chi2: float | None
    residuals: list[float]
    ambiguities: list[SolutionOut]
    ambiguity_resolved_by: Literal["none", "region", "measurements", "unresolved"]
    converged: bool
    dominant_error: str
    n_measurements: int
    fit_consistent: bool = True
    fit_p_value: float | None = None
    ellipse_scale: float = 1.0
    is_fix: bool = True
    not_fix_reason: str | None = None


class AmbiguitySummary(_Out):
    """Another position that fits the measurements as well as the fix (an unresolved
    ambiguity, for example the mirror across the equator), with its 95 % ellipse."""

    lat_deg: float
    lon_deg: float
    semi_major_m: float
    semi_minor_m: float
    azimuth_deg: float


class SolutionSummary(_Out):
    """Enough of a solution to draw it on the map and list it. ``ambiguous``: the
    ambiguity is unresolved; ``ambiguities`` then lists every other position that fits
    as well (``SolutionOut.ambiguities``), so a map can draw all of them.
    fit_consistent: ``SolutionOut.fit_consistent`` (false: the measurements are
    inconsistent, the ellipse is enlarged and the position is not a fix). is_fix:
    ``SolutionOut.is_fix`` (false: not to be reported as a fix, for any reason)."""

    lat_deg: float
    lon_deg: float
    semi_major_m: float
    semi_minor_m: float
    azimuth_deg: float
    cep50_m: float
    ambiguous: bool
    ambiguities: list[AmbiguitySummary] = Field(default_factory=list)
    fit_consistent: bool = True
    is_fix: bool = True


class SolutionHistoryRow(_Out):
    """How the ellipse shrank as measurements accumulated: the 95 % semi-axes of each
    converged solve. fit_consistent: false for an inconsistent fit (its semi-axes are
    the enlarged ones, ``SolutionOut.ellipse_scale``). is_fix: ``SolutionOut.is_fix`` of
    that solve. set_aside: the solve belongs to measurements set aside later
    (``EventBook.set_aside``: a break, another transmitter, a local signal), so later
    rows start afresh."""

    t_ns: ConsoleTimeNs
    semi_major_m: float
    semi_minor_m: float
    n_measurements: int
    fit_consistent: bool = True
    is_fix: bool = True
    set_aside: bool = False


class ClassificationOut(_Out):
    """Waveform class: ``cw``, ``swept``, ``noise``, ``modulated``, ``pulsed`` or
    ``unknown``; ``confidence`` 0..1; ``features`` are the numbers it was decided on."""

    label: str
    confidence: float
    features: dict[str, float]


class Advisory(_Out):
    """Plain-language guidance for the operator."""

    headline: str
    detail: str
    actions: list[str]


class EvidenceRow(_Out):
    """What one receive channel contributes to the uplink-or-local verdict.

    sat_id: the satellite through which this channel would see the event; null for an
    event outside every transponder passband (nothing relays it, so the row is about
    the channel's own receiver).
    snr_db, cn0_dbhz: of its detection, when ``state`` is ``seen``.
    predicted_margin_db: how far above this channel's detection threshold the signal
    is expected to be, given the strongest sighting (null when it cannot be
    predicted). Negative means the channel could not see it.
    last_seen_ns: newest frame in which this channel's detection of the event was above
    threshold, also after the detection ended (for ``stale`` and ``not_seen_capable``
    rows it says how old the channel's last sighting is); null when the channel never
    saw the current transmission (sightings of a transmission set aside, for example an
    uplink signal a local one took over from, are forgotten).
    monitor_id: the monitor window of the channel that covers the event.
    downlink_lo_hz, downlink_hi_hz: where the channel sees the signal (its detection,
    when seen) or would see it (the event's range through ``sat_id``), Hz.
    """

    channel_id: str
    site_id: str
    sat_id: str | None
    state: EvidenceState
    snr_db: float | None
    cn0_dbhz: float | None
    predicted_margin_db: float | None
    last_seen_ns: ConsoleTimeNs | None = None
    monitor_id: str | None = None
    downlink_lo_hz: float | None = None
    downlink_hi_hz: float | None = None


class Transition(_Out):
    """A change of an event's state or verdict (or a merge or split noted against it).
    ``reason`` says why in plain words, for example ``all detections released``,
    ``detected again``, ``closed by operator`` or ``merged into EV-000012``."""

    t_ns: ConsoleTimeNs
    state: EventState
    verdict: Verdict
    reason: str | None = None


class MeasurementRow(_Out):
    """One TDOA (seconds) or FDOA (hertz) measurement of an event.

    value: the measured difference, A minus B, after calibration when ``calibrated``.
    sigma: its one-sigma measurement noise in the same unit.
    residual: whitened residual against the current solution; null when the
    measurement is not in the current solution.
    used: whether the solver was given it. reason: why not, when ``used`` is false
    (for example ``not_detected``, ``ambiguous``, ``narrowband`` (a signal too narrow
    for a time difference: TDOA only), ``clock_unlocked``).
    correction: when ``calibrated``, the reference-carrier correction already
    subtracted from ``value`` (s or Hz; the raw measurement is ``value + correction``),
    else null. It holds everything the reference measured on the same pair: clock
    offsets between the two sites, ephemeris and oscillator errors.
    reference_carrier_id: the reference carrier of that correction, else null.
    """

    t_ns: ConsoleTimeNs
    kind: Literal["tdoa", "fdoa"]
    pair: PairRef
    value: float | None
    sigma: float | None
    residual: float | None
    snr_db: float | None
    calibrated: bool
    used: bool
    reason: str | None
    correction: float | None = None
    reference_carrier_id: str | None = None


class CafCut(_Out):
    """A cut through the correlation surface at the peak: ``x`` in seconds (delay cut)
    or hertz (Doppler cut), ``db`` relative to the noise floor."""

    x: list[float]
    db: list[float]


class CafCandidate(_Out):
    """One distinct correlation peak: delay (s; null when meaningless, as on the
    Doppler ridge of a CW carrier), Doppler (Hz) and post-correlation SNR (dB)."""

    tdoa_s: float | None
    fdoa_hz: float
    snr_db: float


class CafDisplay(_Out):
    """The latest correlation surface of an event, for display.

    surface: dB relative to the noise floor, ``surface[i][j]`` at ``delay_axis_s[i]``,
    ``doppler_axis_hz[j]`` (at most 65 x 65). peak_tdoa_s / peak_fdoa_hz: the
    measured peak (null when not detected). candidates: the distinct peaks the
    correlator found, strongest first (up to 8; ``core.caf.CafResult.candidates``):
    several of almost equal height make the result ``ambiguous``, and they may lie
    outside the displayed patch.
    """

    t_ns: ConsoleTimeNs
    pair: PairRef
    surface: list[list[float]]
    delay_axis_s: list[float]
    doppler_axis_hz: list[float]
    delay_cut: CafCut
    doppler_cut: CafCut
    peak_tdoa_s: float | None
    peak_fdoa_hz: float | None
    snr_db: float
    detected: bool
    reason: str | None
    candidates: list[CafCandidate] = Field(default_factory=list)


class EventTransponder(_Out):
    """A transponder the event's uplink range falls on, with the downlink range the
    interference occupies there."""

    sat_id: str
    transponder_id: str
    downlink_lo_hz: float
    downlink_hi_hz: float


class TaskingStatus(_Out):
    """What capture tasking is doing for an event (null until tasking has looked at it).

    state: ``waiting`` (the next round is scheduled), ``capturing`` (IQ captures
    requested from the nodes), ``processing`` (correlating, calibrating, solving) or
    ``idle`` (nothing to capture: no usable channel sees the event, no second
    satellite with a usable channel relays it, or the event was judged ``local``, so it
    cannot be located by satellite; ``message`` says which).
    rounds: capture rounds started for the event. last_round_ns / next_round_ns: server
    time of the last round's start and of the next one (null when none is planned).
    channels: the receive channels of the last round, primary first.
    message: plain sentences about the last completed round, kept while the next one
    runs: what failed and is being retried, what is awaited, and why the signal itself
    gives no usable measurement when every pair failed for one reason (for example
    "No TDOA: the signal is too narrow ..." for a CW carrier, or "The correlations are
    ambiguous ..." for a repeating sweep); null when all went well.
    solve_reason: why the last solve gave no position (for example ``unobservable``
    or ``fewer than two accepted measurements``); null after a successful solve.
    unlocatable_reason: set when the signal itself cannot be located by satellite
    correlation, whatever more rounds do: every pair of the last round failed for a
    reason of the waveform (a periodic waveform whose correlation is ambiguous, delay
    and Doppler coupled) with no row accepted since, or no second satellite with a usable
    channel relays the frequency. One plain sentence; null otherwise (and as soon as a
    row is accepted).
    """

    state: Literal["waiting", "capturing", "processing", "idle"]
    rounds: int
    last_round_ns: ConsoleTimeNs | None
    next_round_ns: ConsoleTimeNs | None
    channels: list[str]
    message: str | None
    solve_reason: str | None
    unlocatable_reason: str | None = None


class EventTruth(_Out):
    """Simulation only: the emitter this event corresponds to and how far off the
    estimate is. ``miss_m`` and ``inside_ellipse`` are null without a solution."""

    emitter_id: str
    lat_deg: float
    lon_deg: float
    miss_m: float | None
    inside_ellipse: bool | None


class EventSummary(_Out):
    """Row of ``GET /events``. ``uplink_lo_hz`` / ``uplink_hi_hz`` are null for an event
    outside every transponder passband (see ``Event``). ``simulated``: the event comes
    from the simulator. ``onset_ns``, ``received_lo_hz``, ``received_hi_hz``,
    ``seen_site_ids``, ``width_lower_bound``: as in ``Event`` (for a ``local`` event the
    queue shows the received range and the sites, not the uplink range)."""

    id: str
    state: EventState
    verdict: Verdict
    first_seen_ns: ConsoleTimeNs
    last_seen_ns: ConsoleTimeNs
    uplink_lo_hz: float | None
    uplink_hi_hz: float | None
    satellite_ids: list[str]
    label: str | None
    headline: str
    solution: SolutionSummary | None
    simulated: bool = False
    onset_ns: ConsoleTimeNs | None = None
    received_lo_hz: float | None = None
    received_hi_hz: float | None = None
    seen_site_ids: list[str] = Field(default_factory=list)
    width_lower_bound: bool = False


class Event(_Out):
    """``GET /events/{id}`` and the ``event`` WebSocket message (the whole object on
    every change).

    uplink_lo_hz, uplink_hi_hz: the uplink range the interference occupies; null when
    it lies outside every transponder passband (no satellite relays it: it can only be
    local or undetermined, and ``downlink_lo_hz`` / ``downlink_hi_hz`` give where it
    was seen). verdict_reason: one sentence saying what the verdict rests on, or what
    is missing. merged_into: set when this event was folded into another one (it is
    then ``closed``). simulated: the event comes from the simulator. tasking: what
    capture tasking is doing for it (null until tasking has looked at it).

    satellite_ids / transponders: the satellites (and transponders) that relay the
    interference. Before a consistent position exists this is the frequency plan
    (every transponder covering the uplink range: some may not see the transmitter);
    once one exists, only satellites above ``solver.min_elevation_deg`` (and the
    horizon) at the fix, at an end of one of its 95 % ellipse axes or at an ambiguity
    (never none). not_visible_satellite_ids: satellites of the plan left out because
    they cannot see the transmitter's position (empty before a consistent fix). All
    three are empty while the verdict is ``local`` (no satellite relays a local signal).
    onset_ns: start of the current transmission, labeled time: the first detection
    after the latest break, the first detection that showed another transmitter
    took the frequency (for a re-check that only pairs first detected since the change
    could answer, the first detection of the oldest of them), when a local signal took
    over an uplink event's frequency the last sighting of the uplink at the sites that no
    longer see anything, or when an uplink signal took over a local event's frequency
    its first detection at a site that did not see the local signal; equal to
    ``first_seen_ns`` while none of these happened. Report this as the start time. resumed_ns: server time the event was detected again after its
    latest loss (null if it was never lost); break_s: the length of that break, s.
    recheck: why the position and measurements shown are being checked against new
    measurements (the event came back after a short break, or its waveform, power or
    width changed); a disagreeing detection sets them aside. Null otherwise.
    received_lo_hz / received_hi_hz: union of the downlink ranges of the ``seen``
    evidence rows (what is on the air at the sites that see it; null while nothing is
    seen). seen_site_ids: sites with a ``seen`` row, in row order.
    width_lower_bound: in every monitor window holding the event, its detections together
    reach both edges of the window (within 8 % of the span, where receiver filters roll
    off), so the signal may be wider than its range shows: its width is a lower bound
    ("at least N wide").
    """

    id: str
    state: EventState
    verdict: Verdict
    first_seen_ns: ConsoleTimeNs
    last_seen_ns: ConsoleTimeNs
    uplink_lo_hz: float | None
    uplink_hi_hz: float | None
    satellite_ids: list[str]
    transponders: list[EventTransponder]
    classification: ClassificationOut | None
    advisory: Advisory
    evidence: list[EvidenceRow]
    transitions: list[Transition]
    solution: SolutionOut | None
    solution_t_ns: ConsoleTimeNs | None
    solution_history: list[SolutionHistoryRow]
    measurements: list[MeasurementRow]
    caf: CafDisplay | None
    truth: EventTruth | None
    verdict_reason: str | None = None
    downlink_lo_hz: float | None = None
    downlink_hi_hz: float | None = None
    merged_into: str | None = None
    simulated: bool = False
    tasking: TaskingStatus | None = None
    not_visible_satellite_ids: list[str] = Field(default_factory=list)
    onset_ns: ConsoleTimeNs | None = None
    resumed_ns: ConsoleTimeNs | None = None
    break_s: float | None = None
    recheck: str | None = None
    received_lo_hz: float | None = None
    received_hi_hz: float | None = None
    seen_site_ids: list[str] = Field(default_factory=list)
    width_lower_bound: bool = False


class LopProperties(_Out):
    """What one line-of-position feature shows (``GET /events/{id}/lop``).

    role: ``line`` (the measurement satisfied exactly, a MultiLineString) or ``band``
    (between ``-k_sigma`` and ``+k_sigma`` standard deviations, a MultiPolygon).
    kind, a_* / b_*: the measurement and its pair (A minus B). seq: the measurement's
    number within the event (``MeasurementRow`` order, from 1). value: as measured
    (s or Hz); correction: the calibration subtracted before contouring; sigma: its
    own noise; sigma_total: what the band uses (the reference's noise added in
    quadrature when calibrated). height_m: height of the surface contoured.
    """

    role: Literal["line", "band"]
    kind: Literal["tdoa", "fdoa"]
    event_id: str
    seq: int
    tag: str = ""
    a_sat_id: str
    a_site_id: str
    a_channel_id: str
    b_sat_id: str
    b_site_id: str
    b_channel_id: str
    t_ns: ConsoleTimeNs
    value: float
    correction: float
    calibrated: bool
    sigma: float
    sigma_total: float
    k_sigma: float
    height_m: float


class LopFeature(_Out):
    """A GeoJSON feature. ``geometry`` is ``{"type": "MultiLineString", "coordinates":
    [[[lon, lat], ...], ...]}`` or ``{"type": "MultiPolygon", "coordinates": [[[[lon,
    lat], ...], ...], ...]}`` (exterior rings counter-clockwise, holes clockwise).
    Longitudes follow the region's grid: increasing from its ``lon_min`` and unwrapped
    (they may exceed 180 for a region crossing the antimeridian); wrap for display."""

    type: Literal["Feature"] = "Feature"
    geometry: dict[str, Any]
    properties: LopProperties


class LopCollection(_Out):
    """``GET /events/{id}/lop``: the lines of position of the newest used measurement of
    each pair and kind (two features each at most; none when the event has no usable
    measurement)."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[LopFeature]


# --------------------------------------------------------------------------- geometry


class Subpoint(_Out):
    sat_id: str
    lat_deg: float
    lon_deg: float
    alt_m: float


class Footprint(_Out):
    """Edge of the area from which the satellite is above ``min_elevation_deg``.
    ``ring`` is closed, ``[lon_deg, lat_deg]`` pairs, longitudes continuous around the
    sub-satellite longitude (so they may leave -180..180)."""

    sat_id: str
    min_elevation_deg: float
    ring: list[list[float]]


class LookAngle(_Out):
    """One receive channel looking at one satellite (every channel by every
    satellite). ``visible`` is elevation above 0. ``watched`` is whether the channel's
    pointing includes the satellite. ``gain_dbi`` is the channel antenna's gain toward
    the satellite at ``freq_hz`` (the downlink center of the satellite's first
    transponder); ``peak_gain_dbi`` its boresight gain there. ``electrically_small``
    marks a parabolic antenna under three wavelengths across at ``freq_hz``."""

    site_id: str
    channel_id: str
    sat_id: str
    az_deg: float
    el_deg: float
    range_km: float
    off_axis_deg: float
    gain_dbi: float
    peak_gain_dbi: float
    freq_hz: float
    visible: bool
    watched: bool
    electrically_small: bool


# --------------------------------------------------------------------------- analysis


class AccuracyRequest(_Out):
    """``POST /analysis/accuracy``. ``pairs`` are receive-channel id pairs [A, B].
    ``t`` (ISO 8601) defaults to now (the start of the current minute, so that a
    repeated request is answered from the server's cache), ``region`` to the configured
    region. ``kinds`` chooses the measurement types assumed. ``reference_carrier_id``
    assumes calibration against that reference carrier; ``"nearest"``
    (``core.accuracy.NEAREST_REFERENCE``) calibrates each cell's pairs against the
    reference carrier tasking would use for an emitter in that cell (relayed by both
    transponders of the pair, clear of the emitter's band, nearest the cell, then nearest
    in frequency; a configured carrier of that id takes precedence); null assumes no
    calibration. ``integration_s`` defaults to
    the configured capture. ``uncertainty`` replaces ``processing.uncertainty`` for this
    map only (what-if planning; the configuration is not changed): satellites with their
    own ``ephemeris_sigma`` keep it, and site clocks keep ``Site.clock.sigma_ns``.

    The server refuses (422) a grid of more than 20 000 cells (choose a coarser
    ``step_deg`` or a smaller ``region``) or more than 128 pairs, and gives up (504)
    after 120 s."""

    pairs: list[tuple[str, str]] = Field(min_length=1, max_length=128)  # rest.ACCURACY_MAX_PAIRS
    emitter: EmitterModel
    f_e_hz: float = Field(gt=0)
    t: str | None = None
    region: Region | None = None
    step_deg: float = Field(default=2.0, ge=0.25, le=10.0)
    kinds: list[Literal["tdoa", "fdoa"]] = Field(default_factory=lambda: ["tdoa", "fdoa"], min_length=1)
    reference_carrier_id: str | None = None
    integration_s: float | None = Field(default=None, gt=0)
    uncertainty: UncertaintyConfig | None = None


class AccuracyPairSummary(_Out):
    """One listed pair of an accuracy map: its ends, what calibrates it and why it may
    give nothing, over the whole grid.

    The server computes a map in latitude bands (``core.accuracy.accuracy_map`` once
    per band, each returning a summary per pair) and merges the bands' summaries
    (``rest._merge_accuracy``); the command line merges them the same way. "Cells" are
    grid cells, admissible or not, so a pair's ``reference_cells`` add up to
    ``n_lat * n_lon`` whenever a reference was requested.

    - ``a_channel``, ``b_channel``: the ends as listed (``channel`` or
      ``channel@sat``). ``a_sat_id``, ``b_sat_id``: the satellite each end correlates
      on (the first band that resolved them; null when the pair cannot be resolved).
    - ``calibrated``: the kinds calibrated in at least one cell (union over bands and
      reference carriers, in the request's order); empty when no cell is calibrated.
    - ``reference_carrier_id``: the carrier the request named, or null (no
      reference, or ``"nearest"``).
    - ``reference_cells``: cells by the reference applied to them, summed over bands.
      For ``"nearest"``: carrier id -> cells for which that carrier is the nearest
      eligible one (it may still fail to calibrate the pair: ``uncalibrated_cells``),
      ``"none"`` -> cells with no eligible carrier (none relayed by both transponders
      and clear of the target band). For a named carrier: ``{id: cells}`` where it
      calibrates the pair and ``{"none": cells}`` where it does not. Empty when no
      reference was requested.
    - ``uncalibrated_cells``: cells in which no reference calibrates the pair: the
      ``"none"`` cells plus, for ``"nearest"``, the cells of a chosen carrier that
      cannot calibrate it (every cell when no reference was requested).
    - ``reference_reason``: why the reference does not calibrate the pair in those
      cells, null when it does everywhere (or when no reference was requested, or the
      pair itself cannot be resolved). For a carrier: core's reason (for example
      ``"reference 'ref-x' is not relayed by the pair's transponders"``). For
      ``"nearest"`` cells with no eligible carrier: ``"no reference carrier clear of
      the target band is relayed by both transponders"``, followed in parentheses by
      why each reference carrier is not eligible. Several causes are joined with
      ``"; "``, each followed by its number of cells, the largest first.
    - ``reason``: why the pair gives nothing (core's reason; the first band's when
      bands differ, with a note per reason saying the latitudes it holds for).
    - ``notes``: the bands' notes, each once, in first-seen order; a note that not
      every band gives starts with the latitudes it holds for (``"latitudes -30 to
      -21: ..."``). core's ``"not calibrated: <reason>"`` notes are left out (the reason
      is in ``reference_reason``). A named carrier whose band overlaps the target band
      adds ``"<id> overlaps the target band, so tasking would not calibrate against it;
      this map assumes it does"`` to every pair it calibrates.

    For ``"nearest"``, whether a chosen carrier calibrates a pair does not depend on
    the cell; when no band's summary shows it (core reports the reference of the most
    common choice only), the server asks core for a map of one cell against that carrier.
    """

    a_channel: str
    b_channel: str
    a_sat_id: str | None = None
    b_sat_id: str | None = None
    calibrated: list[Literal["tdoa", "fdoa"]] = Field(default_factory=list)
    reference_carrier_id: str | None = None
    reference_cells: dict[str, int] = Field(default_factory=dict)
    uncalibrated_cells: int = Field(default=0, ge=0)
    reference_reason: str | None = None
    reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class AccuracyGrid(_Out):
    """Predicted accuracy over a latitude-longitude grid.

    lon_deg is increasing and unwrapped (it may exceed 180 when the region crosses
    the antimeridian). Grids are ``[n_lat][n_lon]``; a cell is null where no estimate
    is possible (satellite below the horizon, signal too weak, or geometry
    unobservable). ``dominant`` names the largest error source of each cell.
    ``pairs`` holds one ``AccuracyPairSummary`` per requested pair, in request order
    (empty only when the engine returned no summaries).
    """

    t_ns: ConsoleTimeNs
    lat_deg: list[float]
    lon_deg: list[float]
    semi_major_95_m: list[list[float | None]]
    cep50_m: list[list[float | None]]
    dominant: list[list[str | None]]
    pairs: list[AccuracyPairSummary] = Field(default_factory=list)


class LinkBudgetRequest(_Out):
    """``POST /analysis/link-budget``: an emitter at a position and frequency."""

    emitter: EmitterModel
    f_e_hz: float = Field(gt=0)
    lat_deg: float = Field(ge=-90, le=90)
    lon_deg: float = Field(ge=-180, le=180)
    alt_m: float = 0.0
    t: str | None = None


class LinkBudgetRow(_Out):
    """The emitter relayed by one transponder into one receive channel.

    Present only for paths that exist (satellite above both horizons and the
    frequency inside the transponder). ``snr_db`` is in the emitter's bandwidth (in
    1 Hz for a carrier of zero bandwidth). ``detectable`` applies the configured
    detection threshold to the rise above the noise floor in the bins of the monitor
    window that covers ``f_down_hz`` (``monitor_id``; null, and not detectable, when no
    window of the channel covers it): ``10 log10(1 + C / (N0 * max(bandwidth, bin)))``
    must reach ``processing.detection.threshold_db``.
    """

    sat_id: str
    transponder_id: str
    site_id: str
    channel_id: str
    f_down_hz: float
    uplink_elevation_deg: float
    eirp_toward_sat_dbw: float
    pfd_dbw_m2: float
    cn0_up_dbhz: float
    transponder_eirp_dbw: float
    downlink_elevation_deg: float
    off_axis_deg: float
    c_rx_dbw: float
    cn0_total_dbhz: float
    snr_db: float
    detectable: bool
    monitor_id: str | None = None


class LinkBudgetResponse(_Out):
    t_ns: ConsoleTimeNs
    rows: list[LinkBudgetRow]


# --------------------------------------------------------------------------- simulation


class ScenarioEnvelope(_Out):
    """``GET /sim/scenario`` and the reply to ``PUT /sim/scenario`` (whose request body
    is a bare ``Scenario``)."""

    version: int
    scenario: Scenario


class Preset(_Out):
    name: str
    description: str


class SimTruthEmitter(_Out):
    """A simulated emitter with every default resolved. ``active`` is enabled and
    inside its on/off times now."""

    id: str
    name: str
    kind: Literal["jammer", "carrier", "local"]
    lat_deg: float
    lon_deg: float
    freq_hz: float
    eirp_dbw: float
    bandwidth_hz: float
    waveform: str
    enabled: bool
    active: bool


class SimTruthEvent(_Out):
    event_id: str
    emitter_id: str | None
    miss_m: float | None
    inside_ellipse: bool | None


class SimTruth(_Out):
    """``GET /sim/truth``: simulated truth (fictional; label it SIMULATION wherever it
    is shown). ``events`` covers the active events."""

    version: int
    emitters: list[SimTruthEmitter]
    events: list[SimTruthEvent]
    simulated: bool = True


# --------------------------------------------------------------------------- /ws/ui


class WsStatusMessage(_Out):
    """About once a second. ``satellites`` and ``site_clocks`` as in ``Status``."""

    type: Literal["status"] = "status"
    server_t_ns: ConsoleTimeNs
    config_version: int
    marking: Marking | None
    simulation: bool
    nodes: list[NodeStatus]
    events_active: int
    queue: QueueStatus
    satellites: list[SatelliteStatus] = Field(default_factory=list)
    site_clocks: list[SiteClockEstimate] = Field(default_factory=list)


class WsEventMessage(_Out):
    type: Literal["event"] = "event"
    event: Event


class WsConfigMessage(_Out):
    type: Literal["config"] = "config"
    config_version: int


class WsSimMessage(_Out):
    type: Literal["sim"] = "sim"
    version: int


class WsSubscribe(BaseModel):
    """Browser to server: which spectrum windows to stream, as
    ``[channel_id, monitor_id]`` pairs (replaces the previous selection)."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["subscribe"] = "subscribe"
    psd: list[tuple[str, str]] = Field(default_factory=list, max_length=256)


# --------------------------------------------------------------------------- schema export

_EXPORTED: tuple[type[BaseModel], ...] = (
    Meta,
    ValidateResult,
    ErrorResponse,
    ConflictResponse,
    FieldErrorResponse,
    ConfigEnvelope,
    Status,
    EventSummary,
    Event,
    LopCollection,
    Subpoint,
    Footprint,
    LookAngle,
    AccuracyRequest,
    AccuracyGrid,
    LinkBudgetRequest,
    LinkBudgetResponse,
    ScenarioEnvelope,
    Preset,
    SimTruth,
    WsStatusMessage,
    WsEventMessage,
    WsConfigMessage,
    WsSimMessage,
    WsSubscribe,
)


def console_json_schema() -> dict[str, Any]:
    """One JSON Schema document whose ``$defs`` hold every console model (and the
    configuration and scenario models they embed), in serialisation mode, so
    nanosecond times appear as strings."""
    from pydantic.json_schema import models_json_schema

    _, schema = models_json_schema(
        [(model, "serialization") for model in _EXPORTED],
        ref_template="#/$defs/{model}",
        title="PETAL Locate console API",
    )
    return schema


if __name__ == "__main__":  # pragma: no cover - developer tool
    print(json.dumps(console_json_schema(), indent=2, sort_keys=True))
