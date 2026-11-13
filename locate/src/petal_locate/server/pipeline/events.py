"""Interference events: the record the pipeline mutates and its console form.

An *event* is one interfering signal, identified by the uplink range it occupies
(MATH.md section 7): every detection, on any channel, site or satellite, whose mapped
uplink range overlaps it belongs to it. ``EventRecord`` holds what the server knows
about one event; ``EventBook`` owns every record, applies the lifecycle of
ARCHITECTURE.md section 7 and turns records into ``schemas.Event`` documents.

Who changes what:

- The correlator (``pipeline/correlator.py``) raises events, keeps their member
  detections, evidence rows and verdict, and moves them to ``lost`` and back.
- Tasking calls the *mutation API* of ``EventBook`` (``mark_characterizing``,
  ``set_classification``, ``add_measurement``, ``set_caf``, ``set_solution``,
  ``report_cross_satellite_detection``). These are plain synchronous methods for the
  event loop; they never block, never raise for an event that has moved on (they
  return False and log at debug level), and follow merges: an id that was folded into
  another event is redirected to the event that absorbed it.
- The operator closes events (``close``).

Lifecycle. ``state`` is derived: ``closed`` once the operator closed it (or it was
merged into another event); otherwise ``lost`` while no detection holds it; otherwise
its *progress*: ``detected`` (raised) < ``characterizing`` (first capture requested) <
``locating`` (first CAF measurement accepted) < ``located`` (a converged solution
exists). Progress only moves forward and is kept while the event is lost, so an event
that is detected again returns to where it was. Every change of state or verdict is a
``Transition`` with a reason; transitions are stored and pushed.

Publication. Mutations mark a record dirty; ``EventBook.flush`` builds the console
document once and hands it to the publisher (console and store). A *material* change
(state, verdict, evidence states, membership of satellites, classification, a
measurement, a solution, a correlation surface) is published at the next flush;
numeric drift (SNR, C/N0, margins, frequency edges moving by a bin) at most every
``refresh_s`` seconds, so a busy spectrum does not flood the console.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import numpy as np
from pydantic import ValidationError

from petal_locate.core.geodesy import geodesic_distance_m, radii_of_curvature
from petal_locate.core.types import Measurement
from petal_locate.server.api import schemas

__all__ = [
    "ACTIVE_STATES",
    "DEFAULT_REFRESH_S",
    "MAX_HISTORY_ROWS",
    "MAX_INACTIVE_RECORDS",
    "MAX_MEASUREMENT_ROWS",
    "MAX_TRANSITION_ROWS",
    "EventBook",
    "EventNotFound",
    "EventRecord",
    "MeasurementEntry",
    "Publication",
    "Space",
    "TruthEmitter",
    "Unit",
    "UnitKey",
    "caf_display",
    "classification_out",
    "event_truth",
]

logger = logging.getLogger(__name__)

ACTIVE_STATES: Final[tuple[str, ...]] = ("detected", "characterizing", "locating", "located")
"""States of an event that is still being worked on (``lost`` and ``closed`` are not)."""

_PROGRESS: Final[dict[str, int]] = {"detected": 0, "characterizing": 1, "locating": 2, "located": 3}

_MATERIAL_CHANGES: Final[frozenset[str]] = frozenset(
    {"created", "state", "verdict", "members", "classification", "measurement", "caf", "solution"}
)
"""Change kinds that are published at the next flush whatever the signature says;
``evidence`` and ``numeric`` are published at once only when the signature changed."""

DEFAULT_REFRESH_S: Final[float] = 5.0
"""Longest a purely numeric change of an event waits before it is published, s."""

MAX_MEASUREMENT_ROWS: Final[int] = 1000
"""Measurement rows an event keeps for display (oldest dropped first)."""

MAX_HISTORY_ROWS: Final[int] = 500
"""Solution-history rows an event document keeps (the first and the newest; the store
keeps every solution). A solve every 5 s would otherwise add 17 280 rows a day to every
published document."""

MAX_TRANSITION_ROWS: Final[int] = 200
"""Transitions an event document keeps (the first and the newest; the store keeps all)."""

MAX_INACTIVE_RECORDS: Final[int] = 1000
"""Inactive events (closed, lost beyond the re-open window, or history) kept in memory;
older ones are dropped from memory only (they stay in the store)."""

Space = Literal["up", "down"]
"""Frequency space of an event: ``up`` for uplink ranges (mapped through a
transponder), ``down`` for detections outside every transponder passband."""

UnitKey = tuple[str, str, int, int, str]
"""(channel_id, monitor_id, tracker generation, track_id, sat_id or "")."""


class EventNotFound(LookupError):
    """No event has this id."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"no event with id {event_id!r}")
        self.event_id = event_id


@dataclass(eq=False)
class Unit:
    """One current detection of one monitor window, mapped through one satellite.

    A detection maps through every satellite the channel watches whose transponder's
    downlink passband contains its centre (normally one); each mapping is a unit. A
    detection outside every passband is one unit in the ``down`` space.

    key: identity, stable while the tracker keeps the track.
    space, lo_hz, hi_hz: the range used for event identity: uplink Hz (``up``) or
        downlink Hz (``down``).
    down_lo_hz, down_hi_hz: the detection's own range, downlink Hz.
    sat_id, transponder_id: the mapping (None in the ``down`` space).
    snr_db, cn0_dbhz, power_db, first_seen_ns, last_seen_ns, authorized_carrier_id,
        anomaly: copied from ``core.detect.Detection`` (times are labelled node ns).
    received_ns: server time of the frame that last reported it, integer UTC ns.
    """

    key: UnitKey
    channel_id: str
    monitor_id: str
    site_id: str
    sat_id: str | None
    transponder_id: str | None
    space: Space
    lo_hz: float
    hi_hz: float
    down_lo_hz: float
    down_hi_hz: float
    snr_db: float
    cn0_dbhz: float
    power_db: float
    first_seen_ns: int
    last_seen_ns: int
    authorized_carrier_id: str | None
    anomaly: bool
    received_ns: int


@dataclass(eq=False)
class MeasurementEntry:
    """One measurement of an event: its console row and, when tasking supplied it, the
    solver row (``core.types.Measurement``). ``seq`` numbers the entries of an event
    from 1 and is what ``set_solution(used=...)`` refers to."""

    seq: int
    row: schemas.MeasurementRow
    measurement: Measurement | None


@dataclass(frozen=True)
class TruthEmitter:
    """A simulated emitter as needed to match it with events (simulation only).

    kind: ``jammer`` or ``carrier`` radiate on the uplink at ``freq_hz``; ``local``
    radiates straight into nearby receivers at ``freq_hz`` (a downlink frequency).
    """

    id: str
    kind: str
    lat_deg: float
    lon_deg: float
    freq_hz: float
    bandwidth_hz: float
    enabled: bool = True


@dataclass(eq=False)
class EventRecord:
    """Everything the server knows about one interference event (mutable).

    Read freely; change only through ``EventBook`` and the correlator. Times are
    integer UTC ns: ``created_ns`` / ``settle_from_ns`` / ``lost_ns`` on the server
    clock, ``first_seen_ns`` / ``last_seen_ns`` the labelled times of the detections.
    """

    id: str
    seq: int
    created_ns: int
    space: Space
    lo_hz: float
    hi_hz: float
    first_seen_ns: int
    last_seen_ns: int
    settle_from_ns: int
    progress: str = "detected"
    closed: bool = False
    lost: bool = False
    lost_ns: int | None = None
    verdict: str = "undetermined"
    verdict_basis: str = "settling"
    verdict_reason: str = "Gathering evidence."
    verdict_gaps: list[dict[str, Any]] = field(default_factory=list)
    caf_uplink: bool = False
    caf_uplink_pair: schemas.PairRef | None = None
    members: dict[UnitKey, Unit] = field(default_factory=dict)
    evidence: list[schemas.EvidenceRow] = field(default_factory=list)
    transponders: list[schemas.EventTransponder] = field(default_factory=list)
    satellite_ids: list[str] = field(default_factory=list)
    transitions: list[schemas.Transition] = field(default_factory=list)
    classification: schemas.ClassificationOut | None = None
    measurements: list[MeasurementEntry] = field(default_factory=list)
    caf: schemas.CafDisplay | None = None
    solution: Any = None  # core.solver.Solution of the current estimate (None for history)
    solution_out: schemas.SolutionOut | None = None
    solution_t_ns: int | None = None
    solution_used: tuple[int, ...] = ()
    solution_history: list[schemas.SolutionHistoryRow] = field(default_factory=list)
    last_solve_reason: str | None = None
    merged_into: str | None = None
    split_since_ns: int | None = None
    historic: bool = False
    downlink_lo_hz: float | None = None
    downlink_hi_hz: float | None = None
    # publication bookkeeping (EventBook)
    measurement_seq: int = 0
    published_signature: tuple[Any, ...] | None = None
    published_ns: int = 0
    published_evidence: tuple[Any, ...] = ()
    unsaved_transitions: list[schemas.Transition] = field(default_factory=list)
    unsaved_measurements: list[MeasurementEntry] = field(default_factory=list)
    unsaved_solutions: list[tuple[int, bool, str]] = field(default_factory=list)
    stored_doc: schemas.Event | None = None
    tasking: schemas.TaskingStatus | None = None  # set by tasking (set_tasking)

    @property
    def state(self) -> str:
        """``closed``, ``lost`` or the progress (see the module docstring)."""
        if self.closed:
            return "closed"
        if self.lost:
            return "lost"
        return self.progress

    @property
    def active(self) -> bool:
        """Whether the event is still worked on (not lost, closed or history)."""
        return not (self.closed or self.lost or self.historic)

    @property
    def mapped(self) -> bool:
        """Whether the event lies in a transponder passband (``up`` space)."""
        return self.space == "up"

    @property
    def uplink_lo_hz(self) -> float | None:
        """Lower edge of the uplink range, Hz (None outside every passband)."""
        return self.lo_hz if self.space == "up" else None

    @property
    def uplink_hi_hz(self) -> float | None:
        """Upper edge of the uplink range, Hz (None outside every passband)."""
        return self.hi_hz if self.space == "up" else None

    def used_measurements(self) -> list[MeasurementEntry]:
        """Entries whose row is marked ``used`` and that carry a solver row, oldest first."""
        return [e for e in self.measurements if e.row.used and e.measurement is not None]

    def seen_sites(self) -> list[str]:
        """Sites with a ``seen`` evidence row, in row order without repeats."""
        out: list[str] = []
        for row in self.evidence:
            if row.state == "seen" and row.site_id not in out:
                out.append(row.site_id)
        return out


@dataclass(frozen=True)
class Publication:
    """What ``EventBook.flush`` hands to the publisher for one event.

    event: the console document; text: its JSON (built once, shared by every client
    and the store). new_transitions / new_measurements / new_solutions: history not
    stored yet (solutions as ``(t_ns, converged, Solution.as_dict() JSON)``).
    evidence_changed: the evidence states differ from the last publication (store a
    snapshot).
    """

    record: EventRecord
    event: schemas.Event
    text: str
    new_transitions: tuple[schemas.Transition, ...]
    new_measurements: tuple[MeasurementEntry, ...]
    new_solutions: tuple[tuple[int, bool, str], ...]
    evidence_changed: bool


Advisor = Callable[[EventRecord], schemas.Advisory]
TruthProvider = Callable[[EventRecord], schemas.EventTruth | None]
Publisher = Callable[[Publication], None]
Listener = Callable[[EventRecord, frozenset[str]], None]


def _cap_keep_first(rows: list[Any], limit: int) -> None:
    """Trim ``rows`` in place to ``limit`` entries: the first and the newest."""
    excess = len(rows) - limit
    if excess > 0 and limit >= 2:
        del rows[1 : 1 + excess]


def _finite_or_none(value: Any) -> float | None:
    """A finite float, or None for None, NaN and infinities."""
    if value is None:
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _default_advisor(record: EventRecord) -> schemas.Advisory:
    """Advice without configuration names (used when no advisor was given)."""
    from petal_locate.server.pipeline.advisory import advise  # deferred: advisory imports this module

    return advise(record, None)


class EventBook:
    """Every event of the server and the mutation API tasking uses.

    All methods are synchronous and must be called on the event loop.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], int] = time.time_ns,
        start_seq: int = 0,
        id_prefix: str = "EV-",
        refresh_s: float = DEFAULT_REFRESH_S,
        advisor: Advisor | None = None,
        truth: TruthProvider | None = None,
        simulated: Callable[[], bool] = lambda: False,
        settle_s: Callable[[], float] = lambda: 5.0,
    ) -> None:
        """
        clock: server time, integer UTC ns.
        start_seq: largest event sequence number already used (the store's).
        id_prefix: prefix of event ids (``EV-000001``).
        refresh_s: see the module docstring (> 0).
        advisor: builds the advisory of a record (``advisory.advise`` with the
            configuration); a name-less default otherwise.
        truth: simulation truth of a record, or None.
        simulated: whether the server is simulating (labels the documents).
        settle_s: the configured ``verdict_settle_s`` (read at each use).
        """
        if not refresh_s > 0:
            raise ValueError("refresh_s: must be positive")
        self._clock = clock
        self._seq = int(start_seq)
        self._prefix = id_prefix
        self._refresh_ns = int(refresh_s * 1e9)
        self._advisor: Advisor = advisor or _default_advisor
        self._truth = truth
        self._simulated = simulated
        self._settle_s = settle_s
        self._records: dict[str, EventRecord] = {}
        self._dirty: dict[str, set[str]] = {}
        self._publisher: Publisher | None = None
        self._listeners: list[Listener] = []
        self._flush_scheduled = False
        self.reevaluate: Callable[[EventRecord], None] | None = None
        """Hook set by the correlator: re-evaluate the verdict of a record now."""
        self.counters: dict[str, int] = {"published": 0, "publish_errors": 0, "rejected": 0}

    # ------------------------------------------------------------------ wiring

    def set_advisor(self, advisor: Advisor) -> None:
        """Replace the advisory builder (the runtime does this on a config change)."""
        self._advisor = advisor

    def set_truth(self, truth: TruthProvider | None) -> None:
        """Replace the simulation-truth provider."""
        self._truth = truth

    def ensure_seq(self, seq: int) -> None:
        """Never hand out a sequence number at or below ``seq`` (the store's largest)."""
        self._seq = max(self._seq, int(seq))

    def set_publisher(self, publisher: Publisher | None) -> None:
        """Where flushed documents go (the runtime: console and store)."""
        self._publisher = publisher

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(record, changes)`` after each publication of a record;
        ``changes`` names what changed (``created``, ``state``, ``verdict``,
        ``evidence``, ``members``, ``classification``, ``measurement``, ``caf``,
        ``solution``, ``numeric``, ...). Returns a function that removes it. A
        listener that raises is logged and kept."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # ----------------------------------------------------------------- queries

    def get(self, event_id: str) -> EventRecord | None:
        """The record with this id (merged ones included), or None."""
        return self._records.get(event_id)

    def resolve(self, event_id: str) -> EventRecord | None:
        """The record with this id, following merges to the event that absorbed it."""
        record = self._records.get(event_id)
        hops = 0
        while record is not None and record.merged_into is not None and hops < 64:
            record = self._records.get(record.merged_into)
            hops += 1
        return record

    def require(self, event_id: str) -> EventRecord:
        """Like ``get`` but raises ``EventNotFound``."""
        record = self._records.get(event_id)
        if record is None:
            raise EventNotFound(event_id)
        return record

    def records(self) -> list[EventRecord]:
        """Every record, newest first (by sequence number)."""
        return sorted(self._records.values(), key=lambda r: r.seq, reverse=True)

    def active(self) -> list[EventRecord]:
        """Records that are neither lost, closed nor history, oldest first."""
        return sorted((r for r in self._records.values() if r.active), key=lambda r: r.seq)

    def taskable(self) -> list[EventRecord]:
        """Active events tasking should work on: mapped to an uplink range and with a
        verdict of ``uplink`` or ``undetermined`` (ARCHITECTURE.md section 7)."""
        return [r for r in self.active() if r.mapped and r.verdict in ("uplink", "undetermined")]

    def count_active(self) -> int:
        """Number of active events."""
        return sum(1 for r in self._records.values() if r.active)

    # -------------------------------------------------------- correlator side

    def create(
        self,
        *,
        space: Space,
        lo_hz: float,
        hi_hz: float,
        first_seen_ns: int,
        last_seen_ns: int,
        reason: str = "detected",
    ) -> EventRecord:
        """Raise a new event (the correlator calls this). Returns the record, in state
        ``detected`` with verdict ``undetermined`` until the evidence settles."""
        now = self._clock()
        self._seq += 1
        record = EventRecord(
            id=f"{self._prefix}{self._seq:06d}",
            seq=self._seq,
            created_ns=now,
            space=space,
            lo_hz=float(lo_hz),
            hi_hz=float(hi_hz),
            first_seen_ns=int(first_seen_ns),
            last_seen_ns=int(last_seen_ns),
            settle_from_ns=now,
            verdict_reason=self._settling_reason(),
        )
        self._records[record.id] = record
        self.note_transition(record, now, reason)
        self.touch(record, "created")
        logger.info(
            "event %s raised: %s range %.0f to %.0f Hz",
            record.id,
            space,
            record.lo_hz,
            record.hi_hz,
            extra={"event_id": record.id, "space": space, "lo_hz": record.lo_hz, "hi_hz": record.hi_hz},
        )
        return record

    def _settling_reason(self) -> str:
        return (
            f"Gathering evidence: the verdict is formed {self._settle_s():g} s after the "
            "first detection, once every site has had time to report."
        )

    def note_transition(self, record: EventRecord, t_ns: int, reason: str | None, *, force: bool = False) -> None:
        """Append a transition when the state or verdict differs from the last one (or
        always with ``force``, for notes such as merges)."""
        state, verdict = record.state, record.verdict
        last = record.transitions[-1] if record.transitions else None
        if not force and last is not None and last.state == state and last.verdict == verdict:
            return
        transition = schemas.Transition(t_ns=int(t_ns), state=state, verdict=verdict, reason=reason)  # type: ignore[arg-type]
        record.transitions.append(transition)
        _cap_keep_first(record.transitions, MAX_TRANSITION_ROWS)
        record.unsaved_transitions.append(transition)
        logger.info(
            "event %s: %s / %s (%s)",
            record.id,
            state,
            verdict,
            reason,
            extra={"event_id": record.id, "state": state, "verdict": verdict, "reason": reason},
        )

    def mark_lost(self, record: EventRecord, reason: str = "all detections released") -> None:
        """The correlator found no detection left holding the event."""
        if record.lost or record.closed or record.historic:
            return
        now = self._clock()
        record.lost = True
        record.lost_ns = now
        record.split_since_ns = None
        self.note_transition(record, now, reason)
        self.touch(record, "state")

    def reopen(self, record: EventRecord, reason: str = "detected again") -> None:
        """A lost event is detected again: it returns to its progress and its verdict
        is held while the evidence settles anew."""
        if not record.lost or record.closed or record.historic:
            return
        now = self._clock()
        record.lost = False
        record.lost_ns = None
        record.settle_from_ns = now
        self.note_transition(record, now, reason)
        self.touch(record, "state")

    def merge(self, source: EventRecord, target: EventRecord) -> None:
        """Fold ``source`` into ``target`` (the correlator moves the detections). The
        source is closed with ``merged_into`` set; the target keeps the earlier
        first-seen time, the furthest progress and any cross-satellite evidence."""
        now = self._clock()
        target.first_seen_ns = min(target.first_seen_ns, source.first_seen_ns)
        target.last_seen_ns = max(target.last_seen_ns, source.last_seen_ns)
        if _PROGRESS[source.progress] > _PROGRESS[target.progress]:
            target.progress = source.progress
        if source.caf_uplink and not target.caf_uplink:
            target.caf_uplink, target.caf_uplink_pair = True, source.caf_uplink_pair
        if target.classification is None and source.classification is not None:
            target.classification = source.classification
        if source.verdict == "uplink" and target.verdict != "uplink":
            # The evidence that made the source an uplink event holds for the union.
            target.verdict, target.verdict_basis = "uplink", "merged"
            target.verdict_reason = f"{source.id}, merged into this event, was already judged uplink."
        source.closed = True
        source.merged_into = target.id
        self.note_transition(source, now, f"merged into {target.id}")
        self.note_transition(target, now, f"merged with {source.id}", force=True)
        self.touch(source, "state")
        self.touch(target, "members")

    # ---------------------------------------------------------- operator side

    def close(self, event_id: str, reason: str = "closed by operator") -> EventRecord:
        """Close an event (operator). Closing a closed event changes nothing. While its
        detections persist, a closed event keeps absorbing them so that the same signal
        does not immediately raise a new event; once they are released, the next
        detection of that range is a new event. Raises ``EventNotFound``."""
        record = self.require(event_id)
        if record.closed:
            return record
        record.closed = True
        self.note_transition(record, self._clock(), reason)
        self.touch(record, "state")
        self.flush()
        return record

    # ------------------------------------------------------------ tasking API

    def _target(self, event_id: str, what: str) -> EventRecord | None:
        record = self.resolve(event_id)
        if record is None:
            raise EventNotFound(event_id)
        if record.closed or record.historic:
            self.counters["rejected"] += 1
            logger.debug(
                "%s ignored: event %s is %s", what, record.id, record.state, extra={"event_id": record.id}
            )
            return None
        return record

    def _advance(self, record: EventRecord, level: str, reason: str) -> bool:
        if _PROGRESS[level] <= _PROGRESS[record.progress]:
            return False
        record.progress = level
        if not record.lost:
            self.note_transition(record, self._clock(), reason)
        return True

    def mark_characterizing(self, event_id: str, t_ns: int | None = None) -> bool:
        """Tasking requested the event's first capture: ``detected`` -> ``characterizing``.

        t_ns: when (server clock, integer UTC ns); now when None.
        Returns True when the progress moved. Raises ``EventNotFound`` for an unknown
        id; returns False for a closed event.
        """
        record = self._target(event_id, "mark_characterizing")
        if record is None:
            return False
        moved = self._advance(record, "characterizing", "first capture requested")
        if moved:
            self.touch(record, "state")
        return moved

    def set_classification(self, event_id: str, classification: Any) -> bool:
        """Attach the waveform class from the primary capture.

        classification: ``core.detect.Classification`` (or anything with ``label``,
        ``confidence``, ``features``) or a ``schemas.ClassificationOut``.
        Returns False for a closed event or a classification that is not finite.
        """
        record = self._target(event_id, "set_classification")
        if record is None:
            return False
        out = classification_out(classification)
        if out is None:
            self.counters["rejected"] += 1
            logger.warning("event %s: classification rejected (not finite)", record.id, extra={"event_id": record.id})
            return False
        record.classification = out
        self.touch(record, "classification")
        return True

    def add_measurement(
        self, event_id: str, row: schemas.MeasurementRow, measurement: Measurement | None = None
    ) -> int | None:
        """Record one TDOA or FDOA measurement (or a rejected attempt).

        row: the console row; ``used=True`` means it goes to the solver, and the first
            such row moves the event to ``locating``. ``residual`` is filled in by
            ``set_solution``.
        measurement: the solver row (``core.types.Measurement``) for a used row; kept
            so that ``solver_measurements`` can return it.
        Returns the entry's ``seq`` (1, 2, ... per event), or None for a closed event.
        """
        if not isinstance(row, schemas.MeasurementRow):
            raise TypeError("row: must be a schemas.MeasurementRow")
        record = self._target(event_id, "add_measurement")
        if record is None:
            return None
        record.measurement_seq += 1
        entry = MeasurementEntry(seq=record.measurement_seq, row=row, measurement=measurement)
        record.measurements.append(entry)
        record.unsaved_measurements.append(entry)
        if len(record.measurements) > MAX_MEASUREMENT_ROWS:
            del record.measurements[: len(record.measurements) - MAX_MEASUREMENT_ROWS]
        if row.used:
            self._advance(record, "locating", "first measurement accepted")
        self.touch(record, "measurement")
        return entry.seq

    def set_caf(self, event_id: str, caf: schemas.CafDisplay) -> bool:
        """Show the latest correlation surface (see ``caf_display``)."""
        if not isinstance(caf, schemas.CafDisplay):
            raise TypeError("caf: must be a schemas.CafDisplay")
        record = self._target(event_id, "set_caf")
        if record is None:
            return False
        record.caf = caf
        self.touch(record, "caf")
        return True

    def set_solution(
        self,
        event_id: str,
        solution: Any,
        t_ns: int,
        used: Sequence[int] = (),
        reason: str | None = None,
    ) -> bool:
        """Attach the result of a solve.

        solution: ``core.solver.Solution``, or None when the solve failed (then
            ``reason`` says why; the previous estimate stays).
        t_ns: when it was computed (server clock, integer UTC ns).
        used: the ``seq`` of the measurement entries given to the solver, in the order
            given (``Solution.residuals`` is in that order); their rows get the
            residuals, every other row gets null.
        A converged solution moves the event to ``located`` and adds a row to the
        solution history. Returns False for a closed event or a solution that is not
        finite. Raises ValueError when ``used`` does not match the residuals.
        """
        record = self._target(event_id, "set_solution")
        if record is None:
            return False
        if solution is None:
            record.last_solve_reason = reason
            logger.info("event %s: solve gave no position (%s)", record.id, reason, extra={"event_id": record.id})
            return True
        residuals = list(np.asarray(solution.residuals, dtype=np.float64).ravel())
        used_seqs = tuple(int(s) for s in used)
        if used_seqs and len(used_seqs) != len(residuals):
            raise ValueError(f"used: {len(used_seqs)} entries for {len(residuals)} residuals")
        try:
            data = solution.as_dict()
            out = schemas.SolutionOut.model_validate(data)
            text = json.dumps(data, allow_nan=False)
        except (ValidationError, ValueError) as exc:
            self.counters["rejected"] += 1
            logger.error("event %s: solution rejected: %s", record.id, exc, extra={"event_id": record.id})
            return False
        by_seq = dict(zip(used_seqs, residuals))
        for i, entry in enumerate(record.measurements):
            residual = _finite_or_none(by_seq.get(entry.seq))
            if entry.row.residual != residual:
                record.measurements[i] = MeasurementEntry(
                    entry.seq, entry.row.model_copy(update={"residual": residual}), entry.measurement
                )
        record.solution = solution
        record.solution_out = out
        record.solution_t_ns = int(t_ns)
        record.solution_used = used_seqs
        record.last_solve_reason = None
        record.unsaved_solutions.append((int(t_ns), bool(solution.converged), text))
        if solution.converged:
            record.solution_history.append(
                schemas.SolutionHistoryRow(
                    t_ns=int(t_ns),
                    semi_major_m=out.ellipse.semi_major_m,
                    semi_minor_m=out.ellipse.semi_minor_m,
                    n_measurements=out.n_measurements,
                )
            )
            _cap_keep_first(record.solution_history, MAX_HISTORY_ROWS)
            self._advance(record, "located", "position estimated")
        self.touch(record, "solution")
        return True

    def set_tasking(self, event_id: str, status: schemas.TaskingStatus) -> bool:
        """Show what capture tasking is doing for the event. A change is published with
        the next material change, or after ``refresh_s`` at the latest (it is not
        material by itself: a round changes it several times). Returns False for a
        closed event."""
        if not isinstance(status, schemas.TaskingStatus):
            raise TypeError("status: must be a schemas.TaskingStatus")
        record = self._target(event_id, "set_tasking")
        if record is None:
            return False
        if record.tasking != status:
            record.tasking = status
            self.touch(record, "tasking")
        return True

    def report_cross_satellite_detection(
        self, event_id: str, t_ns: int, pair: schemas.PairRef | None = None
    ) -> bool:
        """Tasking found the signal in the cross-correlation of two satellites' copies
        inside the primary window: proof that it reaches both satellites, so the
        verdict becomes ``uplink`` (MATH.md section 7) once the evidence has settled.
        Returns False for a closed event."""
        record = self._target(event_id, "report_cross_satellite_detection")
        if record is None:
            return False
        if not record.caf_uplink:
            record.caf_uplink = True
            record.caf_uplink_pair = pair
            logger.info("event %s: cross-satellite correlation detected", record.id, extra={"event_id": record.id})
        if self.reevaluate is not None:
            self.reevaluate(record)
        self.touch(record, "verdict")
        return True

    def solver_measurements(self, event_id: str, limit: int | None = None) -> list[Measurement]:
        """The solver rows of the used measurements, newest ``limit`` (all when None),
        oldest first. Raises ``EventNotFound``."""
        record = self.resolve(event_id)
        if record is None:
            raise EventNotFound(event_id)
        rows = [e.measurement for e in record.used_measurements() if e.measurement is not None]
        if limit is not None:
            rows = rows[-int(limit):] if limit > 0 else []
        return rows

    # ------------------------------------------------------------ publication

    def touch(self, record: EventRecord, *changes: str) -> None:
        """Mark a record changed (``changes`` as for ``add_listener``) and make sure a
        flush follows on the event loop."""
        self._dirty.setdefault(record.id, set()).update(changes or ("numeric",))
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (synchronous tests): the caller flushes
        self._flush_scheduled = True
        loop.call_soon(self._scheduled_flush)

    def _scheduled_flush(self) -> None:
        self._flush_scheduled = False
        self.flush()

    def _signature(self, record: EventRecord) -> tuple[Any, ...]:
        return (
            record.state,
            record.verdict,
            record.verdict_basis,
            tuple((r.channel_id, r.sat_id, r.state) for r in record.evidence),
            tuple(record.satellite_ids),
            None if record.classification is None else record.classification.label,
            record.measurement_seq,
            record.solution_t_ns,
            None if record.caf is None else record.caf.t_ns,
            record.merged_into,
            len(record.transitions),
        )

    def flush(self, *, force: bool = False) -> list[EventRecord]:
        """Publish the records that changed materially, and those whose numeric changes
        have waited ``refresh_s`` (all dirty ones with ``force``). Returns them."""
        if not self._dirty:
            return []
        now = self._clock()
        published: list[EventRecord] = []
        for event_id in list(self._dirty):
            record = self._records.get(event_id)
            changes = self._dirty.get(event_id, set())
            if record is None:
                self._dirty.pop(event_id, None)
                continue
            signature = self._signature(record)
            material = force or signature != record.published_signature or bool(changes & _MATERIAL_CHANGES)
            if not material and now - record.published_ns < self._refresh_ns:
                continue  # numeric only: wait for the refresh interval (a later flush)
            self._dirty.pop(event_id, None)
            if self._publish(record, now, signature):
                published.append(record)
                for listener in list(self._listeners):
                    try:
                        listener(record, frozenset(changes))
                    except Exception:  # a listener's failure must not stop publication
                        logger.exception("event listener raised", extra={"event_id": record.id})
        return published

    def pending_numeric(self) -> int:
        """Dirty records waiting for their refresh interval."""
        return len(self._dirty)

    def prune(self, reopen_window_s: float, limit: int = MAX_INACTIVE_RECORDS) -> list[str]:
        """Drop the oldest inactive records from memory beyond ``limit`` (they stay in the
        store, which the publisher wrote them to). Only records that can no longer
        change are eligible: closed (or merged) ones holding no detection, history, and
        lost ones lost longer than ``reopen_window_s`` (which cannot re-open), with
        nothing waiting to be published. Returns the dropped ids, oldest first."""
        now = self._clock()
        window_ns = int(float(reopen_window_s) * 1e9)
        eligible = [
            r
            for r in self._records.values()
            if r.id not in self._dirty
            and not r.members
            and (
                r.historic
                or r.closed
                or (r.lost and r.lost_ns is not None and now - r.lost_ns > window_ns)
            )
        ]
        excess = len(eligible) - int(limit)
        if excess <= 0:
            return []
        dropped = [r.id for r in sorted(eligible, key=lambda r: r.seq)[:excess]]
        for event_id in dropped:
            del self._records[event_id]
        logger.info(
            "%d inactive event(s) dropped from memory (kept in the store): %s ... %s",
            len(dropped),
            dropped[0],
            dropped[-1],
            extra={"dropped": len(dropped)},
        )
        return dropped

    def _publish(self, record: EventRecord, now: int, signature: tuple[Any, ...]) -> bool:
        try:
            event = self.to_schema(record)
            text = event.model_dump_json()
        except (ValidationError, ValueError) as exc:
            self.counters["publish_errors"] += 1
            logger.error("event %s could not be published: %s", record.id, exc, extra={"event_id": record.id})
            return False
        evidence_key = tuple((r.channel_id, r.sat_id, r.state) for r in record.evidence)
        publication = Publication(
            record=record,
            event=event,
            text=text,
            new_transitions=tuple(record.unsaved_transitions),
            new_measurements=tuple(record.unsaved_measurements),
            new_solutions=tuple(record.unsaved_solutions),
            evidence_changed=evidence_key != record.published_evidence,
        )
        record.unsaved_transitions.clear()
        record.unsaved_measurements.clear()
        record.unsaved_solutions.clear()
        record.published_signature = signature
        record.published_ns = now
        record.published_evidence = evidence_key
        self.counters["published"] += 1
        if self._publisher is not None:
            try:
                self._publisher(publication)
            except Exception:  # the publisher (console, store) must not break the pipeline
                self.counters["publish_errors"] += 1
                logger.exception("event publisher raised", extra={"event_id": record.id})
        return True

    # --------------------------------------------------------------- documents

    def to_schema(self, record: EventRecord) -> schemas.Event:
        """The console document of a record (``GET /events/{id}`` and ``/ws/ui``)."""
        if record.historic and record.stored_doc is not None:
            return record.stored_doc.model_copy(
                update={
                    "state": record.state,
                    "transitions": list(record.transitions),
                    "advisory": self._advisor(record),
                    "tasking": None,  # history is not tasked
                }
            )
        truth = None
        if self._truth is not None:
            try:
                truth = self._truth(record)
            except Exception:  # truth is a display aid; never let it stop publication
                logger.exception("simulation truth failed", extra={"event_id": record.id})
        return schemas.Event(
            id=record.id,
            state=record.state,  # type: ignore[arg-type]
            verdict=record.verdict,  # type: ignore[arg-type]
            first_seen_ns=record.first_seen_ns,
            last_seen_ns=record.last_seen_ns,
            uplink_lo_hz=record.uplink_lo_hz,
            uplink_hi_hz=record.uplink_hi_hz,
            satellite_ids=list(record.satellite_ids),
            transponders=list(record.transponders),
            classification=record.classification,
            advisory=self._advisor(record),
            evidence=list(record.evidence),
            transitions=list(record.transitions),
            solution=record.solution_out,
            solution_t_ns=record.solution_t_ns,
            solution_history=list(record.solution_history),
            measurements=[e.row for e in record.measurements],
            caf=record.caf,
            truth=truth,
            verdict_reason=record.verdict_reason,
            downlink_lo_hz=record.downlink_lo_hz,
            downlink_hi_hz=record.downlink_hi_hz,
            merged_into=record.merged_into,
            simulated=bool(self._simulated()),
            tasking=record.tasking,
        )

    def summary(self, record: EventRecord) -> schemas.EventSummary:
        """The ``GET /events`` row of a record."""
        doc = self.to_schema(record) if record.historic else None
        solution = record.solution_out if doc is None else doc.solution
        summary_solution = None
        if solution is not None:
            summary_solution = schemas.SolutionSummary(
                lat_deg=solution.lat_deg,
                lon_deg=solution.lon_deg,
                semi_major_m=solution.ellipse.semi_major_m,
                semi_minor_m=solution.ellipse.semi_minor_m,
                azimuth_deg=solution.ellipse.azimuth_deg,
                cep50_m=solution.cep50_m,
                ambiguous=bool(solution.ambiguities),
            )
        if doc is not None:
            return schemas.EventSummary(
                id=doc.id,
                state=doc.state,
                verdict=doc.verdict,
                first_seen_ns=doc.first_seen_ns,
                last_seen_ns=doc.last_seen_ns,
                uplink_lo_hz=doc.uplink_lo_hz,
                uplink_hi_hz=doc.uplink_hi_hz,
                satellite_ids=list(doc.satellite_ids),
                label=None if doc.classification is None else doc.classification.label,
                headline=doc.advisory.headline,
                solution=summary_solution,
                simulated=doc.simulated,
            )
        return schemas.EventSummary(
            id=record.id,
            state=record.state,  # type: ignore[arg-type]
            verdict=record.verdict,  # type: ignore[arg-type]
            first_seen_ns=record.first_seen_ns,
            last_seen_ns=record.last_seen_ns,
            uplink_lo_hz=record.uplink_lo_hz,
            uplink_hi_hz=record.uplink_hi_hz,
            satellite_ids=list(record.satellite_ids),
            label=None if record.classification is None else record.classification.label,
            headline=self._advisor(record).headline,
            solution=summary_solution,
            simulated=bool(self._simulated()),
        )

    # ----------------------------------------------------------------- history

    def load_history(self, docs: Iterable[tuple[int, int, str]]) -> list[EventRecord]:
        """Load events of earlier runs as inactive history.

        docs: ``(seq, created_ns, Event JSON)`` per stored event.
        A stored event that was still active is marked ``lost`` with the reason
        ``server restarted`` (and marked dirty so the change is stored). History never
        takes part in event identity: a signal that comes back raises a new event.
        Documents that no longer validate are skipped and logged. Returns the records
        loaded.
        """
        loaded: list[EventRecord] = []
        now = self._clock()
        for seq, created_ns, text in docs:
            try:
                doc = schemas.Event.model_validate_json(text)
            except ValidationError as exc:
                logger.warning("stored event skipped: it no longer validates (%s)", exc.errors()[:1])
                continue
            if doc.id in self._records:
                continue
            record = EventRecord(
                id=doc.id,
                seq=int(seq),
                created_ns=int(created_ns),
                space="up" if doc.uplink_lo_hz is not None else "down",
                lo_hz=float(doc.uplink_lo_hz if doc.uplink_lo_hz is not None else doc.downlink_lo_hz or 0.0),
                hi_hz=float(doc.uplink_hi_hz if doc.uplink_hi_hz is not None else doc.downlink_hi_hz or 0.0),
                first_seen_ns=doc.first_seen_ns,
                last_seen_ns=doc.last_seen_ns,
                settle_from_ns=int(created_ns),
                progress=doc.state if doc.state in _PROGRESS else "detected",
                closed=doc.state == "closed",
                lost=doc.state != "closed",
                verdict=doc.verdict,
                verdict_basis="history",
                verdict_reason=doc.verdict_reason or "",
                evidence=list(doc.evidence),
                transponders=list(doc.transponders),
                satellite_ids=list(doc.satellite_ids),
                transitions=list(doc.transitions),
                classification=doc.classification,
                caf=doc.caf,
                solution_out=doc.solution,
                solution_t_ns=doc.solution_t_ns,
                solution_history=list(doc.solution_history),
                merged_into=doc.merged_into,
                historic=True,
                downlink_lo_hz=doc.downlink_lo_hz,
                downlink_hi_hz=doc.downlink_hi_hz,
                stored_doc=doc,
            )
            record.progress = _last_progress(doc.transitions)
            if doc.state in _PROGRESS and _PROGRESS[doc.state] > _PROGRESS[record.progress]:
                record.progress = doc.state
            record.measurements = [MeasurementEntry(i + 1, row, None) for i, row in enumerate(doc.measurements)]
            record.measurement_seq = len(record.measurements)
            self._records[record.id] = record
            self._seq = max(self._seq, record.seq)
            if doc.state in ACTIVE_STATES:
                self.note_transition(record, now, "server restarted: kept as history")
                self.touch(record, "state")
            else:
                record.published_signature = self._signature(record)
            loaded.append(record)
        return loaded


def _last_progress(transitions: Sequence[schemas.Transition]) -> str:
    """The furthest progress state among stored transitions (``detected`` if none)."""
    best = "detected"
    for transition in transitions:
        if transition.state in _PROGRESS and _PROGRESS[transition.state] > _PROGRESS[best]:
            best = transition.state
    return best


# ------------------------------------------------------------------ conversions


def classification_out(classification: Any) -> schemas.ClassificationOut | None:
    """``schemas.ClassificationOut`` from ``core.detect.Classification`` (or any object
    with ``label``, ``confidence``, ``features``); None when a number is not finite."""
    if isinstance(classification, schemas.ClassificationOut):
        return classification
    try:
        features = {str(k): float(v) for k, v in dict(getattr(classification, "features", {}) or {}).items()}
        return schemas.ClassificationOut(
            label=str(classification.label),
            confidence=float(classification.confidence),
            features=features,
        )
    except (ValidationError, TypeError, ValueError, AttributeError):
        return None


def _clean(values: Any) -> list[float]:
    """A 1-D float list with non-finite values replaced (dB floors: -inf -> the minimum
    finite value, NaN dropped to that floor)."""
    arr = np.asarray(values, dtype=np.float64).ravel()
    finite = np.isfinite(arr)
    if not finite.all():
        floor = float(arr[finite].min()) if finite.any() else 0.0
        arr = np.where(finite, arr, floor)
    return [float(x) for x in arr]


def caf_display(result: Any, pair: schemas.PairRef, t_ns: int) -> schemas.CafDisplay:
    """The console form of a ``core.caf.CafResult`` (surface patch, axes, cuts, peak).

    Non-finite values in the dB arrays (none are expected) are replaced by the lowest
    finite value of their array, so the payload never carries NaN or infinity.
    """
    surface = np.asarray(result.surface, dtype=np.float64)
    if surface.ndim != 2:
        raise ValueError("result.surface: must be 2-D")
    flat = _clean(surface)
    rows = [flat[i * surface.shape[1] : (i + 1) * surface.shape[1]] for i in range(surface.shape[0])]
    delay_x, delay_db = result.delay_cut
    doppler_x, doppler_db = result.doppler_cut
    return schemas.CafDisplay(
        t_ns=int(t_ns),
        pair=pair,
        surface=rows,
        delay_axis_s=_clean(result.delay_axis_s),
        doppler_axis_hz=_clean(result.doppler_axis_hz),
        delay_cut=schemas.CafCut(x=_clean(delay_x), db=_clean(delay_db)),
        doppler_cut=schemas.CafCut(x=_clean(doppler_x), db=_clean(doppler_db)),
        peak_tdoa_s=_finite_or_none(result.tdoa_s),
        peak_fdoa_hz=_finite_or_none(result.fdoa_hz),
        snr_db=_finite_or_none(result.snr_db) if _finite_or_none(result.snr_db) is not None else 0.0,
        detected=bool(result.detected),
        reason=result.reason,
    )


def event_truth(
    record: EventRecord, emitters: Sequence[TruthEmitter]
) -> schemas.EventTruth | None:
    """Match an event with the simulated emitter behind it (simulation only).

    ``jammer`` and ``carrier`` emitters match an uplink event whose range overlaps
    their occupied band; ``local`` emitters match an event seen at the downlink
    frequency they radiate on (its evidence ranges, or the range of an event outside
    every passband). The nearest centre wins. ``miss_m`` is the geodesic distance
    from the current solution, ``inside_ellipse`` whether the truth lies inside its
    ellipse (Mahalanobis distance in east/north metres against the ellipse's
    confidence). Returns None when no emitter matches.
    """
    best: tuple[float, TruthEmitter] | None = None
    centre = 0.5 * (record.lo_hz + record.hi_hz)
    down_ranges = [
        (r.downlink_lo_hz, r.downlink_hi_hz)
        for r in record.evidence
        if r.state == "seen" and r.downlink_lo_hz is not None and r.downlink_hi_hz is not None
    ]
    if record.space == "down":
        down_ranges.append((record.lo_hz, record.hi_hz))
    for emitter in emitters:
        if not emitter.enabled:
            continue
        half = 0.5 * max(emitter.bandwidth_hz, 0.0)
        e_lo, e_hi = emitter.freq_hz - half, emitter.freq_hz + half
        if emitter.kind == "local":
            for lo, hi in down_ranges:
                assert lo is not None and hi is not None
                if e_hi >= lo and e_lo <= hi:
                    distance = abs(emitter.freq_hz - 0.5 * (lo + hi))
                    if best is None or distance < best[0]:
                        best = (distance, emitter)
        elif record.space == "up" and e_hi >= record.lo_hz and e_lo <= record.hi_hz:
            distance = abs(emitter.freq_hz - centre)
            if best is None or distance < best[0]:
                best = (distance, emitter)
    if best is None:
        return None
    emitter = best[1]
    miss_m: float | None = None
    inside: bool | None = None
    solution = record.solution_out
    if solution is not None:
        miss_m = _finite_or_none(
            geodesic_distance_m(solution.lat_deg, solution.lon_deg, emitter.lat_deg, emitter.lon_deg)
        )
        inside = _inside_ellipse(solution, emitter.lat_deg, emitter.lon_deg)
    return schemas.EventTruth(
        emitter_id=emitter.id,
        lat_deg=emitter.lat_deg,
        lon_deg=emitter.lon_deg,
        miss_m=miss_m,
        inside_ellipse=inside,
    )


def _inside_ellipse(solution: schemas.SolutionOut, lat_deg: float, lon_deg: float) -> bool | None:
    """Whether a point lies inside the solution's error ellipse (local tangent plane)."""
    m, n = radii_of_curvature(solution.lat_deg)
    h = solution.height_m
    dlat = math.radians(lat_deg - solution.lat_deg)
    dlon = math.radians(((lon_deg - solution.lon_deg + 180.0) % 360.0) - 180.0)
    east = (float(n) + h) * math.cos(math.radians(solution.lat_deg)) * dlon
    north = (float(m) + h) * dlat
    cov = np.asarray(solution.cov_en_m2, dtype=np.float64)
    try:
        d2 = float(np.array([east, north]) @ np.linalg.solve(cov, np.array([east, north])))
    except np.linalg.LinAlgError:
        return None
    confidence = solution.ellipse.confidence
    if not (0.0 < confidence < 1.0) or not math.isfinite(d2):
        return None
    return d2 <= -2.0 * math.log(1.0 - confidence)


def measurement_row_json(entry: MeasurementEntry) -> str:
    """JSON of a measurement row as stored (console shape, ``*_ns`` as strings)."""
    return entry.row.model_dump_json()


def evidence_json(rows: Sequence[schemas.EvidenceRow]) -> str:
    """JSON array of evidence rows as stored."""
    return "[" + ",".join(r.model_dump_json() for r in rows) + "]"


def solution_dict(solution: Any) -> Mapping[str, Any]:
    """``Solution.as_dict()`` (a hook for tests and the store)."""
    return dict(solution.as_dict())
