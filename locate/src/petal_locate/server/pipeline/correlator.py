"""Event identity, evidence, capability and the uplink-or-local verdict (MATH.md section 7).

The correlator receives the detections of every monitor window after each frame
(``WindowReport`` from ``pipeline/detection.py``) and keeps the events of
``pipeline/events.py`` up to date:

Mapping. A detection maps to the uplink through the transponder (of a satellite the
channel watches) whose downlink passband contains its centre (``config.uplink_for``):
``f_up = f_down - translation_hz``. One mapping per watched satellite that contains
it is a *unit*. A detection outside every such passband is one unit in the downlink
space: it can only be local or undetermined. A detection on an authorised carrier
counts only while the tracker flags it anomalous.

Identity. Units on any channels, sites and satellites whose ranges overlap (edges
touching included) belong to one event. A new unit joins the oldest overlapping
active event; when it overlaps several, they merge into the oldest (the others are
closed with ``merged into``). When the units of an event fall apart into groups that
no longer overlap, and stay apart for ``split_hold_s``, the largest group keeps the
event and every other group becomes a new event (``split from``). A unit that
overlaps no active event re-opens the most recent ``lost`` event with an overlapping
range lost less than ``reopen_window_s`` ago, else raises a new event. While a closed
event still holds detections it absorbs units of its range, so closing an event that
is still on the air does not raise it again at once.

Evidence. One row per receive channel watching a satellite whose transponder relays
the event's range and whose monitor window contains the mapped downlink centre (for an
event outside every passband: every channel whose window contains it):

- ``stale``: the node is offline or the window's newest frame is older than
  ``stale_frames`` frame periods;
- ``seen``: the channel has a detection of the event;
- ``not_capable``: another detection overlaps the range on that window;
- ``not_seen_capable`` when ``cn0_A + q_B - q_A - 10 log10(max(f_hi - f_lo, bin_B))
  >= threshold_B + sensitivity_margin_db``, with ``A`` the strongest sighting (highest
  C/N0) of the same satellite and ``q = downlink_quality_db``; otherwise
  ``not_capable``. ``predicted_margin_db`` is the left side minus ``threshold_B``
  (null when there is no sighting on that satellite or ``q`` is unknown). For an event
  outside every passband ``q_B - q_A`` is replaced by ``10 log10(T_A / T_B)`` (system
  noise temperatures: receiver sensitivity only).

Verdict (``decide_verdict``), evaluated once ``verdict_settle_s`` has passed since the
event was raised (or re-opened), then on every change: ``uplink`` when ``seen`` at two
sites farther apart than ``min_site_separation_km`` or when tasking reported a
cross-satellite CAF detection; ``local`` when every ``seen`` row lies within
``min_site_separation_km`` of one site and a row at a site farther than that from all
of them is ``not_seen_capable``; ``undetermined`` otherwise, with what is missing.
Hysteresis: ``uplink`` is kept for the life of the event once established (the
evidence for it does not expire), and ``local`` is kept until the signal is seen at a
distant site (which makes it ``uplink``), so that a site dropping out of view does not
flip the verdict back and forth.

Expiry. A window that delivers no frame for ``max(stale_frames, release_frames)``
frame periods releases its detections (its node went away); ``tick()`` checks this and
the settle times once a second.
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from petal_locate.core.geodesy import geodesic_distance_m
from petal_locate.core.linkbudget import downlink_quality_db
from petal_locate.server.api import schemas
from petal_locate.server.pipeline.detection import WindowKey, WindowReport, WindowSpec
from petal_locate.server.pipeline.events import EventBook, EventRecord, Unit, UnitKey

__all__ = [
    "REOPEN_WINDOW_S",
    "SPLIT_HOLD_S",
    "STALE_FRAMES",
    "Correlator",
    "QualityTable",
    "VerdictDecision",
    "decide_verdict",
    "site_distances_km",
]

logger = logging.getLogger(__name__)

REOPEN_WINDOW_S: Final[float] = 900.0
"""A lost event whose range is detected again within this time re-opens, s."""

SPLIT_HOLD_S: Final[float] = 5.0
"""Groups of an event's detections must stay apart this long before the event splits, s."""

STALE_FRAMES: Final[int] = 3
"""A window whose newest frame is older than this many frame periods is stale."""

_NS: Final[int] = 1_000_000_000


# --------------------------------------------------------------------- quality


@dataclass(frozen=True)
class QualityTable:
    """Downlink quality ``q`` (MATH.md section 7, dB) per (channel, satellite,
    transponder) at one time; None where the satellite is below the site's horizon."""

    t_s: float
    values: Mapping[tuple[str, str, str], float | None]

    def get(self, channel_id: str, sat_id: str, transponder_id: str) -> float | None:
        """``q`` of one channel on one transponder, or None."""
        return self.values.get((channel_id, sat_id, transponder_id))

    @classmethod
    def empty(cls) -> QualityTable:
        """A table that knows nothing (every ``q`` unknown)."""
        return cls(0.0, {})

    @classmethod
    def build(cls, config: Any, t_s: float) -> QualityTable:
        """Evaluate ``downlink_quality_db`` for every channel and every transponder of
        every satellite it watches (blocking, about 1 ms per entry: run it in a worker
        thread). Failures are logged and stored as None."""
        values: dict[tuple[str, str, str], float | None] = {}
        for site in config.sites:
            for channel in site.channels:
                for sat_id in channel.watched_satellite_ids:
                    for transponder in config.satellite(sat_id).transponders:
                        try:
                            q = downlink_quality_db(config, channel.id, sat_id, transponder.id, t_s)
                        except ValueError as exc:
                            logger.warning(
                                "downlink quality of %s on %s/%s failed: %s",
                                channel.id,
                                sat_id,
                                transponder.id,
                                exc,
                                extra={"channel_id": channel.id, "sat_id": sat_id},
                            )
                            q = None
                        values[(channel.id, sat_id, transponder.id)] = (
                            q if q is not None and math.isfinite(q) else None
                        )
        return cls(float(t_s), values)


def site_distances_km(config: Any) -> dict[tuple[str, str], float]:
    """Geodesic distance between every pair of sites (both orders), km."""
    sites = list(config.sites)
    out: dict[tuple[str, str], float] = {}
    for i, a in enumerate(sites):
        out[(a.id, a.id)] = 0.0
        for b in sites[i + 1 :]:
            d = float(geodesic_distance_m(a.lat_deg, a.lon_deg, b.lat_deg, b.lon_deg)) / 1000.0
            out[(a.id, b.id)] = d
            out[(b.id, a.id)] = d
    return out


# --------------------------------------------------------------------- verdict


@dataclass(frozen=True)
class VerdictDecision:
    """Result of ``decide_verdict``.

    verdict: ``uplink``, ``local`` or ``undetermined``.
    basis: short code of what it rests on: ``two_sites``, ``caf``, ``kept``,
        ``capable_not_seen`` or ``missing``.
    reason: one plain sentence for the operator.
    gaps: for ``undetermined``, what is missing, as dicts with ``kind``
        (``no_sighting``, ``no_far_site``, ``far_stale``, ``far_not_sensitive``,
        ``far_unpredictable``) and ``site_id``, ``sat_id``, ``near_site_id``,
        ``margin_db`` where they apply.
    """

    verdict: str
    basis: str
    reason: str
    gaps: tuple[dict[str, Any], ...] = ()


def decide_verdict(
    rows: Sequence[schemas.EvidenceRow],
    distance_km: Callable[[str, str], float],
    min_separation_km: float,
    *,
    caf_uplink: bool = False,
    previous: str = "undetermined",
    previous_reason: str = "",
    site_name: Callable[[str], str] = lambda s: s,
    sat_name: Callable[[str | None], str] = lambda s: s or "no satellite",
) -> VerdictDecision:
    """The verdict of MATH.md section 7 from evidence rows (pure; see the module
    docstring for the rules and the hysteresis).

    rows: the event's evidence rows. distance_km: geodesic distance between two site
    ids, km. min_separation_km: ``detection.min_site_separation_km``. caf_uplink:
    tasking reported a cross-satellite CAF detection. previous, previous_reason: the
    verdict in force and its reason. site_name, sat_name: display names.
    """
    seen_sites: list[str] = []
    for row in rows:
        if row.state == "seen" and row.site_id not in seen_sites:
            seen_sites.append(row.site_id)

    best: tuple[str, str, float] | None = None
    for i, a in enumerate(seen_sites):
        for b in seen_sites[i + 1 :]:
            d = distance_km(a, b)
            if d > min_separation_km and (best is None or d > best[2]):
                best = (a, b, d)
    if best is not None:
        a, b, d = best
        return VerdictDecision(
            "uplink",
            "two_sites",
            f"It is seen at {site_name(a)} and {site_name(b)}, {d:,.0f} km apart, which no single "
            "nearby source could explain.",
        )
    if caf_uplink:
        return VerdictDecision(
            "uplink",
            "caf",
            "The copies of the signal relayed by two satellites match each other, so one transmitter "
            "reaches both satellites.",
        )
    if previous == "uplink":
        kept = previous_reason.replace("It is seen at", "It was seen at", 1) if previous_reason else ""
        return VerdictDecision("uplink", "kept", kept or "It was judged uplink earlier.")

    def far_from_all_seen(site_id: str) -> bool:
        return all(distance_km(site_id, s) > min_separation_km for s in seen_sites)

    if seen_sites:
        anchors = [s for s in seen_sites if all(distance_km(s, o) <= min_separation_km for o in seen_sites)]
        capable = [r for r in rows if r.state == "not_seen_capable" and far_from_all_seen(r.site_id)]
        if anchors and capable:
            witness = max(capable, key=lambda r: r.predicted_margin_db if r.predicted_margin_db is not None else -math.inf)
            anchor = anchors[0]
            margin = witness.predicted_margin_db
            spare = f" with {margin:.0f} dB to spare" if margin is not None else ""
            through = f" watches {sat_name(witness.sat_id)} and" if witness.sat_id is not None else ""
            return VerdictDecision(
                "local",
                "capable_not_seen",
                f"{site_name(witness.site_id)}, {distance_km(witness.site_id, anchor):,.0f} km away,{through} "
                f"would see it{spare} (dB, decibels, compare signal strengths) if it came from the "
                "satellite, but does not.",
            )
    if previous == "local":
        return VerdictDecision("local", "kept", previous_reason or "It was judged local earlier.")

    gaps: list[dict[str, Any]] = []
    if not seen_sites:
        return VerdictDecision(
            "undetermined", "missing", "No site sees the signal at the moment.", ({"kind": "no_sighting"},)
        )
    near = seen_sites[0]
    seen_sats = [r.sat_id for r in rows if r.state == "seen"]
    far_rows = [r for r in rows if r.state != "seen" and far_from_all_seen(r.site_id)]
    where = _join([site_name(s) for s in seen_sites])
    if not far_rows:
        sat = seen_sats[0] if seen_sats else None
        gaps.append({"kind": "no_far_site", "sat_id": sat, "near_site_id": near})
        reason = (
            f"It is seen only at {where}, and no other site more than {min_separation_km:.0f} km away "
            "monitors this frequency on a satellite that relays it, so there is nothing to compare with."
        )
        return VerdictDecision("undetermined", "missing", reason, tuple(gaps))
    parts: list[str] = []
    done: set[str] = set()
    for row in far_rows:
        if row.site_id in done:
            continue
        done.add(row.site_id)
        name = site_name(row.site_id)
        if row.state == "stale":
            gaps.append({"kind": "far_stale", "site_id": row.site_id, "sat_id": row.sat_id})
            parts.append(f"the spectrum feed of {name} is offline or out of date")
        elif row.predicted_margin_db is not None:
            gaps.append(
                {
                    "kind": "far_not_sensitive",
                    "site_id": row.site_id,
                    "sat_id": row.sat_id,
                    "margin_db": float(row.predicted_margin_db),
                }
            )
            if row.predicted_margin_db < 0:
                parts.append(f"{name} would not be sensitive enough to see it at all")
            else:
                parts.append(
                    f"{name} would see it only {row.predicted_margin_db:.0f} dB above its detection "
                    "threshold, too little for its silence to count"
                )
        else:
            gaps.append({"kind": "far_unpredictable", "site_id": row.site_id, "sat_id": row.sat_id})
            parts.append(
                f"no site sees it through {sat_name(row.sat_id)}, so the silence of {name} cannot be judged"
            )
    reason = f"It is seen only at {where}; " + "; ".join(parts) + "."
    return VerdictDecision("undetermined", "missing", reason, tuple(gaps))


def _join(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------- correlator


@dataclass(eq=False)
class _RowContext:
    site_id: str
    channel_id: str
    sat_id: str | None
    transponder_id: str | None
    window: WindowSpec
    down_lo: float
    down_hi: float


@dataclass(eq=False)
class _WindowState:
    spec: WindowSpec
    units: dict[UnitKey, Unit] = field(default_factory=dict)
    last_received_ns: int | None = None
    threshold_db: float | None = None


class Correlator:
    """Turns window reports into events (see the module docstring).

    Methods are synchronous and run on the event loop; ``update`` costs well under a
    millisecond for a handful of events.
    """

    def __init__(
        self,
        config: Any,
        book: EventBook,
        *,
        windows: Mapping[WindowKey, WindowSpec] | None = None,
        quality: QualityTable | None = None,
        is_online: Callable[[str], bool] = lambda site_id: True,
        clock: Callable[[], int] = time.time_ns,
        reopen_window_s: float = REOPEN_WINDOW_S,
        split_hold_s: float = SPLIT_HOLD_S,
        stale_frames: int = STALE_FRAMES,
    ) -> None:
        """
        config: the ``SystemConfig``. book: the ``EventBook`` events live in.
        windows: window specs (``build_window_specs``). quality: ``QualityTable``.
        is_online: whether the node of a site is connected. clock: server time,
        integer UTC ns. reopen_window_s, split_hold_s, stale_frames: see the module
        docstring.
        """
        self._book = book
        self._is_online = is_online
        self._clock = clock
        self._reopen_ns = int(reopen_window_s * _NS)
        self._split_hold_ns = int(split_hold_s * _NS)
        self._stale_frames = int(stale_frames)
        self._windows: dict[WindowKey, _WindowState] = {}
        self._unit_event: dict[UnitKey, str] = {}
        self._quality = quality or QualityTable.empty()
        self._counters: Counter[str] = Counter()
        self._config: Any = None
        book.reevaluate = self._reevaluate
        self.reconfigure(config, windows or {}, quality)

    # ---------------------------------------------------------------- config

    def reconfigure(
        self, config: Any, windows: Mapping[WindowKey, WindowSpec], quality: QualityTable | None = None
    ) -> None:
        """Take a new configuration and window set. Windows that disappeared release
        their detections; every current detection is mapped again; every active
        event's evidence is rebuilt."""
        self._config = config
        if quality is not None:
            self._quality = quality
        detection = config.processing.detection
        self._settle_ns = int(float(detection.verdict_settle_s) * _NS)
        self._min_sep_km = float(detection.min_site_separation_km)
        self._threshold_db = float(detection.threshold_db)
        self._sensitivity_db = float(detection.sensitivity_margin_db)
        self._release_frames = int(detection.release_frames)
        self._distances = site_distances_km(config)
        self._channel_site: dict[str, str] = {}
        self._watched: dict[str, tuple[str, ...]] = {}
        self._noise_k: dict[str, float] = {}
        self._watchers: dict[str, list[tuple[str, str]]] = {}
        for site in config.sites:
            for channel in site.channels:
                self._channel_site[channel.id] = site.id
                self._watched[channel.id] = tuple(channel.watched_satellite_ids)
                self._noise_k[channel.id] = float(channel.system_noise_temp_k)
                for sat_id in channel.watched_satellite_ids:
                    self._watchers.setdefault(sat_id, []).append((site.id, channel.id))
        self._site_names = {site.id: site.name for site in config.sites}
        self._sat_names = {sat.id: sat.name for sat in config.satellites}

        now = self._clock()
        affected: set[EventRecord] = set()
        new_windows: dict[WindowKey, _WindowState] = {}
        for key, spec in windows.items():
            state = self._windows.get(key)
            if state is None:
                new_windows[key] = _WindowState(spec=spec)
            else:
                state.spec = spec
                new_windows[key] = state
        for key, state in self._windows.items():
            if key not in new_windows:
                for unit in list(state.units.values()):
                    self._remove_unit(unit, affected)
        self._windows = new_windows
        self._windows_by_channel: dict[str, list[WindowSpec]] = {}
        for state in new_windows.values():
            self._windows_by_channel.setdefault(state.spec.channel_id, []).append(state.spec)
        # Re-map what is currently detected with the new frequency plan.
        for key, state in new_windows.items():
            if state.units:
                self._replace_units(state, self._remap(state), affected, now)
        affected.update(self._book.active())
        self._settle(affected, now)
        self._book.flush()

    def set_quality(self, quality: QualityTable) -> None:
        """Replace the downlink-quality table (refreshed periodically) and re-evaluate."""
        self._quality = quality
        self.refresh()

    # ---------------------------------------------------------------- queries

    def counters(self) -> dict[str, int]:
        """Diagnostics: reports, units mapped and unmapped, merges, splits, re-opens."""
        return dict(self._counters)

    def sightings(self, event_id: str) -> list[Unit]:
        """The current detections (units) of an event, strongest SNR first. Tasking
        picks its primary channel from these. Unknown ids give an empty list."""
        record = self._book.resolve(event_id)
        if record is None:
            return []
        return sorted(record.members.values(), key=lambda u: u.snr_db, reverse=True)

    def window_fresh(self, key: WindowKey) -> bool:
        """Whether a window's node is online and its newest frame recent."""
        state = self._windows.get(key)
        return state is not None and self._fresh(state, self._clock())

    # ---------------------------------------------------------------- input

    def update(self, report: WindowReport) -> None:
        """One accepted frame of one window (the detection pipeline's sink)."""
        key = report.spec.key
        state = self._windows.get(key)
        if state is None:
            # A window the correlator was not told about (a race with reconfigure).
            state = _WindowState(spec=report.spec)
            self._windows[key] = state
            self._windows_by_channel.setdefault(report.spec.channel_id, []).append(report.spec)
        state.spec = report.spec
        state.last_received_ns = int(report.received_ns)
        state.threshold_db = float(report.threshold_db)
        self._counters["reports"] += 1
        now = self._clock()
        units: dict[UnitKey, Unit] = {}
        for generation, detection in report.detections:
            for unit in self._units_for(report.spec, generation, detection, int(report.received_ns)):
                units[unit.key] = unit
        affected: set[EventRecord] = set()
        self._replace_units(state, units, affected, now)
        self._settle(affected, now)
        self._book.flush()

    def drop_window(self, key: WindowKey) -> None:
        """A window left the configuration: release its detections."""
        state = self._windows.pop(key, None)
        if state is None:
            return
        affected: set[EventRecord] = set()
        for unit in list(state.units.values()):
            self._remove_unit(unit, affected)
        channel_windows = self._windows_by_channel.get(key[0], [])
        self._windows_by_channel[key[0]] = [w for w in channel_windows if w.key != key]
        self._settle(affected, self._clock())
        self._book.flush()

    def tick(self) -> None:
        """Release detections of windows that stopped delivering, and re-evaluate every
        active event (freshness and settle times move with the clock). Call about once
        a second."""
        now = self._clock()
        affected: set[EventRecord] = set()
        for state in self._windows.values():
            if state.units and self._expired(state, now):
                self._counters["expired_windows"] += 1
                logger.info(
                    "window %s/%s silent for %d frame periods: its detections are released",
                    state.spec.channel_id,
                    state.spec.monitor_id,
                    max(self._stale_frames, self._release_frames),
                    extra={"channel_id": state.spec.channel_id, "monitor_id": state.spec.monitor_id},
                )
                for unit in list(state.units.values()):
                    self._remove_unit(unit, affected)
                state.units = {}
        affected.update(self._book.active())
        self._settle(affected, now)
        self._book.flush()

    def refresh(self) -> None:
        """Re-evaluate every active event now (for example after a node status change)."""
        now = self._clock()
        self._settle(set(self._book.active()), now)
        self._book.flush()

    # ---------------------------------------------------------------- mapping

    def _units_for(self, spec: WindowSpec, generation: int, detection: Any, received_ns: int) -> list[Unit]:
        if detection.authorized_carrier_id is not None and not detection.anomaly:
            self._counters["authorized_ignored"] += 1
            return []
        f_lo, f_hi = float(detection.f_lo_hz), float(detection.f_hi_hz)
        if not (math.isfinite(f_lo) and math.isfinite(f_hi)) or f_hi < f_lo:
            self._counters["bad_detection"] += 1
            return []
        centre = 0.5 * (f_lo + f_hi)
        common = dict(
            channel_id=spec.channel_id,
            monitor_id=spec.monitor_id,
            site_id=spec.site_id,
            down_lo_hz=f_lo,
            down_hi_hz=f_hi,
            snr_db=float(detection.snr_db),
            cn0_dbhz=float(detection.cn0_dbhz),
            power_db=float(detection.power_db),
            first_seen_ns=int(detection.first_seen_ns),
            last_seen_ns=int(detection.last_seen_ns),
            authorized_carrier_id=detection.authorized_carrier_id,
            anomaly=bool(detection.anomaly),
            received_ns=received_ns,
        )
        units: list[Unit] = []
        for sat_id in self._watched.get(spec.channel_id, spec.watched):
            try:
                mapping = self._config.uplink_for(sat_id, centre)
            except ValueError:
                mapping = None
            if mapping is None:
                continue
            transponder, _ = mapping
            shift = float(transponder.translation_hz)
            units.append(
                Unit(
                    key=(spec.channel_id, spec.monitor_id, int(generation), int(detection.track_id), sat_id),
                    sat_id=sat_id,
                    transponder_id=transponder.id,
                    space="up",
                    lo_hz=f_lo - shift,
                    hi_hz=f_hi - shift,
                    **common,  # type: ignore[arg-type]
                )
            )
        if units:
            self._counters["units_mapped"] += len(units)
            return units
        self._counters["units_unmapped"] += 1
        return [
            Unit(
                key=(spec.channel_id, spec.monitor_id, int(generation), int(detection.track_id), ""),
                sat_id=None,
                transponder_id=None,
                space="down",
                lo_hz=f_lo,
                hi_hz=f_hi,
                **common,  # type: ignore[arg-type]
            )
        ]

    def _remap(self, state: _WindowState) -> dict[UnitKey, Unit]:
        """The window's current units mapped again with the current configuration."""
        out: dict[UnitKey, Unit] = {}
        seen: set[tuple[str, str, int, int]] = set()
        for unit in state.units.values():
            base = unit.key[:4]
            if base in seen:
                continue
            seen.add(base)  # type: ignore[arg-type]
            detection = _UnitDetection(unit)
            for new in self._units_for(state.spec, unit.key[2], detection, unit.received_ns):
                out[new.key] = new
        return out

    # ---------------------------------------------------------------- membership

    def _replace_units(
        self, state: _WindowState, units: dict[UnitKey, Unit], affected: set[EventRecord], now: int
    ) -> None:
        old = state.units
        for key in [k for k in old if k not in units]:
            self._remove_unit(old[key], affected)
        state.units = units
        for key, unit in units.items():
            event_id = self._unit_event.get(key)
            record = self._book.get(event_id) if event_id is not None else None
            if record is not None and key in record.members:
                record.members[key] = unit
                affected.add(record)
            else:
                self._add_unit(unit, affected, now)

    def _remove_unit(self, unit: Unit, affected: set[EventRecord]) -> None:
        event_id = self._unit_event.pop(unit.key, None)
        if event_id is None:
            return
        record = self._book.get(event_id)
        if record is not None:
            record.members.pop(unit.key, None)
            affected.add(record)

    def _attach(self, unit: Unit, record: EventRecord, affected: set[EventRecord]) -> None:
        record.members[unit.key] = unit
        self._unit_event[unit.key] = record.id
        affected.add(record)

    def _add_unit(self, unit: Unit, affected: set[EventRecord], now: int) -> None:
        candidates = [
            r
            for r in self._book.active()
            if r.space == unit.space and _overlap(r.lo_hz, r.hi_hz, unit.lo_hz, unit.hi_hz)
        ]
        if candidates:
            target = min(candidates, key=lambda r: r.seq)
            self._attach(unit, target, affected)
            for other in candidates:
                if other is not target:
                    self._merge(other, target, affected)
            return
        for record in self._book.records():
            if (
                record.closed
                and not record.historic
                and record.merged_into is None
                and record.members
                and record.space == unit.space
                and _overlap(record.lo_hz, record.hi_hz, unit.lo_hz, unit.hi_hz)
            ):
                self._counters["absorbed_by_closed"] += 1
                self._attach(unit, record, affected)
                return
        lost = [
            r
            for r in self._book.records()
            if r.lost
            and not r.closed
            and not r.historic
            and r.space == unit.space
            and r.lost_ns is not None
            and now - r.lost_ns <= self._reopen_ns
            and _overlap(r.lo_hz, r.hi_hz, unit.lo_hz, unit.hi_hz)
        ]
        if lost:
            record = max(lost, key=lambda r: r.lost_ns or 0)
            self._counters["reopened"] += 1
            self._attach(unit, record, affected)
            self._book.reopen(record)
            return
        record = self._book.create(
            space=unit.space,
            lo_hz=unit.lo_hz,
            hi_hz=unit.hi_hz,
            first_seen_ns=unit.first_seen_ns,
            last_seen_ns=unit.last_seen_ns,
        )
        self._counters["created"] += 1
        self._attach(unit, record, affected)

    def _merge(self, source: EventRecord, target: EventRecord, affected: set[EventRecord]) -> None:
        for key, unit in list(source.members.items()):
            target.members[key] = unit
            self._unit_event[key] = target.id
        source.members.clear()
        target.lo_hz = min(target.lo_hz, source.lo_hz)
        target.hi_hz = max(target.hi_hz, source.hi_hz)
        self._book.merge(source, target)
        self._counters["merged"] += 1
        affected.discard(source)
        affected.add(target)

    # ---------------------------------------------------------------- settle

    def _settle(self, affected: set[EventRecord], now: int) -> None:
        """Bring the affected events up to date: ranges, lost, merges, splits, evidence,
        verdict."""
        pending = list(affected)
        done: set[str] = set()
        while pending:
            record = pending.pop()
            if record.id in done or record.historic:
                continue
            if record.merged_into is not None:
                continue
            if record.members:
                self._update_range(record)
            if record.closed:
                done.add(record.id)
                continue
            if not record.members:
                if not record.lost:
                    self._book.mark_lost(record)
                done.add(record.id)
                continue
            if record.lost:
                # Units can only reach a lost event through re-opening; be safe.
                self._book.reopen(record)
            # Merges: the range may have grown into another active event.
            others = [
                r
                for r in self._book.active()
                if r is not record and r.space == record.space and _overlap(r.lo_hz, r.hi_hz, record.lo_hz, record.hi_hz)
            ]
            if others:
                group = sorted([record, *others], key=lambda r: r.seq)
                target = group[0]
                for other in group[1:]:
                    self._merge(other, target, set())
                    done.add(other.id)
                self._update_range(target)
                record = target
            for new in self._split(record, now):
                pending.append(new)
            self._evaluate(record, now)
            done.add(record.id)

    def _update_range(self, record: EventRecord) -> None:
        units = list(record.members.values())
        lo = min(u.lo_hz for u in units)
        hi = max(u.hi_hz for u in units)
        if (lo, hi) != (record.lo_hz, record.hi_hz):
            record.lo_hz, record.hi_hz = lo, hi
            self._book.touch(record, "numeric")
        record.first_seen_ns = min(record.first_seen_ns, min(u.first_seen_ns for u in units))
        record.last_seen_ns = max(record.last_seen_ns, max(u.last_seen_ns for u in units))
        if record.space == "down":
            record.downlink_lo_hz, record.downlink_hi_hz = lo, hi
        else:
            record.downlink_lo_hz = record.downlink_hi_hz = None

    def _split(self, record: EventRecord, now: int) -> list[EventRecord]:
        groups = _components(list(record.members.values()))
        if len(groups) <= 1:
            record.split_since_ns = None
            return []
        if record.split_since_ns is None:
            record.split_since_ns = now
            return []
        if now - record.split_since_ns < self._split_hold_ns:
            return []
        groups.sort(key=lambda g: (-len(g), min(u.first_seen_ns for u in g)))
        keep, rest = groups[0], groups[1:]
        created: list[EventRecord] = []
        for group in rest:
            new = self._book.create(
                space=record.space,
                lo_hz=min(u.lo_hz for u in group),
                hi_hz=max(u.hi_hz for u in group),
                first_seen_ns=min(u.first_seen_ns for u in group),
                last_seen_ns=max(u.last_seen_ns for u in group),
                reason=f"split from {record.id}",
            )
            for unit in group:
                record.members.pop(unit.key, None)
                new.members[unit.key] = unit
                self._unit_event[unit.key] = new.id
            self._update_range(new)
            self._book.note_transition(record, now, f"split: {new.id} took a separate signal", force=True)
            created.append(new)
            self._counters["split"] += 1
        record.split_since_ns = None
        record.first_seen_ns = min(u.first_seen_ns for u in keep)
        self._update_range(record)
        self._book.touch(record, "members")
        return created

    # ---------------------------------------------------------------- evidence

    def _fresh(self, state: _WindowState, now: int) -> bool:
        if state.last_received_ns is None or not self._is_online(state.spec.site_id):
            return False
        return now - state.last_received_ns <= self._stale_frames * state.spec.frame_period_s * _NS

    def _expired(self, state: _WindowState, now: int) -> bool:
        if state.last_received_ns is None:
            return True
        limit = max(self._stale_frames, self._release_frames) * state.spec.frame_period_s * _NS
        return now - state.last_received_ns > limit

    def _window_for(self, channel_id: str, f_hz: float) -> WindowSpec | None:
        best: WindowSpec | None = None
        for spec in self._windows_by_channel.get(channel_id, []):
            if spec.contains(f_hz) and (best is None or abs(spec.center_hz - f_hz) < abs(best.center_hz - f_hz)):
                best = spec
        return best

    def _row_contexts(self, record: EventRecord) -> list[_RowContext]:
        rows: list[_RowContext] = []
        keys: set[tuple[str, str | None]] = set()
        if record.space == "up":
            sats_done: set[str] = set()
            for satellite, transponder in self._config.transponders_covering(record.lo_hz, record.hi_hz):
                if satellite.id in sats_done:
                    continue
                sats_done.add(satellite.id)
                shift = float(transponder.translation_hz)
                d_lo, d_hi = record.lo_hz + shift, record.hi_hz + shift
                centre = 0.5 * (d_lo + d_hi)
                for site_id, channel_id in self._watchers.get(satellite.id, []):
                    window = self._window_for(channel_id, centre)
                    if window is None or (channel_id, satellite.id) in keys:
                        continue
                    keys.add((channel_id, satellite.id))
                    rows.append(_RowContext(site_id, channel_id, satellite.id, transponder.id, window, d_lo, d_hi))
        else:
            centre = 0.5 * (record.lo_hz + record.hi_hz)
            for channel_id, site_id in self._channel_site.items():
                window = self._window_for(channel_id, centre)
                if window is None:
                    continue
                keys.add((channel_id, None))
                rows.append(_RowContext(site_id, channel_id, None, None, window, record.lo_hz, record.hi_hz))
        # Every member must have a row even if the event's centre left its window.
        for unit in record.members.values():
            if (unit.channel_id, unit.sat_id) in keys:
                continue
            state = self._windows.get((unit.channel_id, unit.monitor_id))
            if state is None:
                continue
            keys.add((unit.channel_id, unit.sat_id))
            shift = unit.down_lo_hz - unit.lo_hz
            rows.append(
                _RowContext(
                    unit.site_id,
                    unit.channel_id,
                    unit.sat_id,
                    unit.transponder_id,
                    state.spec,
                    record.lo_hz + shift,
                    record.hi_hz + shift,
                )
            )
        return rows

    def _evaluate(self, record: EventRecord, now: int) -> None:
        """Rebuild the evidence rows of an active event and decide its verdict."""
        contexts = self._row_contexts(record)
        best_by_row: dict[tuple[str, str | None], Unit] = {}
        strongest: dict[str | None, Unit] = {}
        for unit in record.members.values():
            key = (unit.channel_id, unit.sat_id)
            if key not in best_by_row or unit.snr_db > best_by_row[key].snr_db:
                best_by_row[key] = unit
            if unit.sat_id not in strongest or unit.cn0_dbhz > strongest[unit.sat_id].cn0_dbhz:
                strongest[unit.sat_id] = unit
        width = max(record.hi_hz - record.lo_hz, 0.0)
        rows: list[schemas.EvidenceRow] = []
        for ctx in contexts:
            state = self._windows.get(ctx.window.key)
            fresh = state is not None and self._fresh(state, now)
            threshold = state.threshold_db if state is not None and state.threshold_db is not None else self._threshold_db
            unit = best_by_row.get((ctx.channel_id, ctx.sat_id))
            margin = self._margin(ctx, strongest.get(ctx.sat_id), width, threshold)
            if not fresh:
                row_state = "stale"
            elif unit is not None:
                row_state = "seen"
            elif state is not None and any(
                u.key not in record.members and _overlap(u.down_lo_hz, u.down_hi_hz, ctx.down_lo, ctx.down_hi)
                for u in state.units.values()
            ):
                row_state = "not_capable"
            elif margin is not None and margin >= self._sensitivity_db:
                row_state = "not_seen_capable"
            else:
                row_state = "not_capable"
            seen = row_state == "seen" and unit is not None
            rows.append(
                schemas.EvidenceRow(
                    channel_id=ctx.channel_id,
                    site_id=ctx.site_id,
                    sat_id=ctx.sat_id,
                    state=row_state,  # type: ignore[arg-type]
                    snr_db=_finite(unit.snr_db) if seen and unit is not None else None,
                    cn0_dbhz=_finite(unit.cn0_dbhz) if seen and unit is not None else None,
                    predicted_margin_db=margin,
                    last_seen_ns=unit.last_seen_ns if unit is not None else None,
                    monitor_id=ctx.window.monitor_id,
                    downlink_lo_hz=unit.down_lo_hz if seen and unit is not None else ctx.down_lo,
                    downlink_hi_hz=unit.down_hi_hz if seen and unit is not None else ctx.down_hi,
                )
            )
        rows.sort(key=lambda r: (r.site_id, r.channel_id, r.sat_id or ""))
        record.evidence = rows
        transponders: list[schemas.EventTransponder] = []
        sats: list[str] = []
        if record.space == "up":
            for satellite, transponder in self._config.transponders_covering(record.lo_hz, record.hi_hz):
                shift = float(transponder.translation_hz)
                transponders.append(
                    schemas.EventTransponder(
                        sat_id=satellite.id,
                        transponder_id=transponder.id,
                        downlink_lo_hz=record.lo_hz + shift,
                        downlink_hi_hz=record.hi_hz + shift,
                    )
                )
                if satellite.id not in sats:
                    sats.append(satellite.id)
        record.transponders = transponders
        record.satellite_ids = sats
        self._decide(record, now)
        self._book.touch(record, "numeric")

    def _margin(self, ctx: _RowContext, strongest: Unit | None, width_hz: float, threshold_db: float) -> float | None:
        if strongest is None:
            return None
        bandwidth = max(width_hz, ctx.window.bin_hz)
        if bandwidth <= 0:
            return None
        if ctx.sat_id is None:
            t_a = self._noise_k.get(strongest.channel_id)
            t_b = self._noise_k.get(ctx.channel_id)
            if not t_a or not t_b:
                return None
            delta = 10.0 * math.log10(t_a / t_b)
        else:
            if strongest.transponder_id is None or ctx.transponder_id is None:
                return None
            q_a = self._quality.get(strongest.channel_id, ctx.sat_id, strongest.transponder_id)
            q_b = self._quality.get(ctx.channel_id, ctx.sat_id, ctx.transponder_id)
            if q_a is None or q_b is None:
                return None
            delta = q_b - q_a
        return _finite(strongest.cn0_dbhz + delta - 10.0 * math.log10(bandwidth) - threshold_db)

    def _decide(self, record: EventRecord, now: int) -> None:
        if now - record.settle_from_ns < self._settle_ns:
            if record.verdict == "undetermined":
                record.verdict_basis = "settling"
                record.verdict_gaps = [{"kind": "settling"}]
            return
        decision = decide_verdict(
            record.evidence,
            self._distance,
            self._min_sep_km,
            caf_uplink=record.caf_uplink,
            previous=record.verdict,
            previous_reason=record.verdict_reason,
            site_name=lambda s: self._site_names.get(s, s),
            sat_name=lambda s: self._sat_names.get(s, s) if s is not None else "no satellite",
        )
        changed = decision.verdict != record.verdict
        record.verdict = decision.verdict
        record.verdict_basis = decision.basis
        record.verdict_reason = decision.reason
        record.verdict_gaps = [dict(g) for g in decision.gaps]
        if changed:
            self._book.note_transition(record, now, f"verdict: {decision.reason}")
            self._book.touch(record, "verdict")

    def _distance(self, a: str, b: str) -> float:
        d = self._distances.get((a, b))
        return d if d is not None else math.inf

    def _reevaluate(self, record: EventRecord) -> None:
        if record.active:
            self._evaluate(record, self._clock())


# --------------------------------------------------------------------- helpers


def _overlap(lo_a: float, hi_a: float, lo_b: float, hi_b: float) -> bool:
    """Whether two closed ranges share at least one point."""
    return min(hi_a, hi_b) >= max(lo_a, lo_b)


def _components(units: Sequence[Unit]) -> list[list[Unit]]:
    """Groups of units whose ranges overlap (transitively)."""
    ordered = sorted(units, key=lambda u: u.lo_hz)
    groups: list[list[Unit]] = []
    hi = -math.inf
    for unit in ordered:
        if groups and unit.lo_hz <= hi:
            groups[-1].append(unit)
            hi = max(hi, unit.hi_hz)
        else:
            groups.append([unit])
            hi = unit.hi_hz
    return groups


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    out = float(value)
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class _UnitDetection:
    """A unit read back as a ``Detection`` (for re-mapping after a configuration change)."""

    unit: Unit

    @property
    def track_id(self) -> int:
        return self.unit.key[3]

    @property
    def f_lo_hz(self) -> float:
        return self.unit.down_lo_hz

    @property
    def f_hi_hz(self) -> float:
        return self.unit.down_hi_hz

    @property
    def power_db(self) -> float:
        return self.unit.power_db

    @property
    def snr_db(self) -> float:
        return self.unit.snr_db

    @property
    def cn0_dbhz(self) -> float:
        return self.unit.cn0_dbhz

    @property
    def first_seen_ns(self) -> int:
        return self.unit.first_seen_ns

    @property
    def last_seen_ns(self) -> int:
        return self.unit.last_seen_ns

    @property
    def authorized_carrier_id(self) -> str | None:
        return self.unit.authorized_carrier_id

    @property
    def anomaly(self) -> bool:
        return self.unit.anomaly
