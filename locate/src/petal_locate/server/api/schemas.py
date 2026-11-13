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
- Frequencies are Hz, angles degrees, distances metres unless the name says otherwise.
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
    "AccuracyRequest",
    "Advisory",
    "AmbiguitySummary",
    "CafCandidate",
    "CafCut",
    "CafDisplay",
    "ChannelRef",
    "ChannelStatus",
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
    "ScenarioEnvelope",
    "SimTruth",
    "SimTruthEmitter",
    "SimTruthEvent",
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


class ChannelStatus(_Out):
    channel_id: str
    ok: bool
    last_frame_age_s: float | None


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


class Status(_Out):
    """``GET /status``."""

    server_t_ns: ConsoleTimeNs
    config_version: int
    nodes: list[NodeStatus]
    events_active: int
    queue: QueueStatus


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
    ``tdoa_bias``, ``reference`` (``core.solver.ERROR_SOURCES``).
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
    as well (``SolutionOut.ambiguities``), so a map can draw all of them."""

    lat_deg: float
    lon_deg: float
    semi_major_m: float
    semi_minor_m: float
    azimuth_deg: float
    cep50_m: float
    ambiguous: bool
    ambiguities: list[AmbiguitySummary] = Field(default_factory=list)


class SolutionHistoryRow(_Out):
    """How the ellipse shrank as measurements accumulated."""

    t_ns: ConsoleTimeNs
    semi_major_m: float
    semi_minor_m: float
    n_measurements: int


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
    saw it.
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
    """

    state: Literal["waiting", "capturing", "processing", "idle"]
    rounds: int
    last_round_ns: ConsoleTimeNs | None
    next_round_ns: ConsoleTimeNs | None
    channels: list[str]
    message: str | None
    solve_reason: str | None


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
    from the simulator."""

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
    the satellite at ``freq_hz`` (the downlink centre of the satellite's first
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
    assumes calibration against that reference carrier. ``integration_s`` defaults to
    the configured capture. ``uncertainty`` replaces ``processing.uncertainty`` for this
    map only (what-if planning; the configuration is not changed): satellites with their
    own ``ephemeris_sigma`` keep it, and site clocks keep ``Site.clock.sigma_ns``.

    The server refuses (422) a grid of more than 20 000 cells (choose a coarser
    ``step_deg`` or a smaller ``region``) and gives up (504) after 120 s."""

    pairs: list[tuple[str, str]] = Field(min_length=1)
    emitter: EmitterModel
    f_e_hz: float = Field(gt=0)
    t: str | None = None
    region: Region | None = None
    step_deg: float = Field(default=2.0, ge=0.25, le=10.0)
    kinds: list[Literal["tdoa", "fdoa"]] = Field(default_factory=lambda: ["tdoa", "fdoa"], min_length=1)
    reference_carrier_id: str | None = None
    integration_s: float | None = Field(default=None, gt=0)
    uncertainty: UncertaintyConfig | None = None


class AccuracyGrid(_Out):
    """Predicted accuracy over a latitude-longitude grid.

    lon_deg is increasing and unwrapped (it may exceed 180 when the region crosses
    the antimeridian). Grids are ``[n_lat][n_lon]``; a cell is null where no estimate
    is possible (satellite below the horizon, signal too weak, or geometry
    unobservable). ``dominant`` names the largest error source of each cell.
    """

    t_ns: ConsoleTimeNs
    lat_deg: list[float]
    lon_deg: list[float]
    semi_major_95_m: list[list[float | None]]
    cep50_m: list[list[float | None]]
    dominant: list[list[str | None]]


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
    """About once a second."""

    type: Literal["status"] = "status"
    server_t_ns: ConsoleTimeNs
    config_version: int
    marking: Marking | None
    simulation: bool
    nodes: list[NodeStatus]
    events_active: int
    queue: QueueStatus


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
