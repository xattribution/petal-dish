"""The node's time reference: where labelled time comes from and how good it is.

Every absolute time a node puts on the wire (`hello.t_ns`, `heartbeat.t_ns`, the
`t_ns` of spectrum frames, `t_first_ns` of captures, the comparison with
`deadline_ns`) is *labelled* time: integer UTC nanoseconds read from a `ClockSource`.
The same object reports the protocol `ClockStatus` that travels with heartbeats and
captures, so the server knows whether a TDOA from this node can be trusted.

Sources shipped here:

* `SystemClock`: the operating system's UTC clock. Reported as source "ntp" and
  locked unless the caller says otherwise; this module does not ask the operating
  system whether NTP is actually synchronised, so a deployment that knows better must
  say so (`SystemClock(source="free", locked=False)`).
* `ManualClock`: time that moves only when told to; for tests.
* `DisciplinedClock`: the extension point for a GPS-disciplined source (below).

Extension point: a GPS-disciplined clock
----------------------------------------
A GPSDO-backed node needs two things: labelled time that is the disciplined time, and
a truthful lock state with holdover. Subclass `DisciplinedClock` and implement

    def read_reference(self) -> ReferenceReading

returning whether the reference is locked right now and its own estimate of
`true time - labelled time` in nanoseconds (for example from the receiver's
"gps_locked"/"ref_locked" sensor and its reported time-pulse offset). The base class
turns that into a `ClockStatus` with source "gpsdo" and keeps the holdover time
(seconds since lock was last seen, 0 while locked). Override `now_ns` as well when
labelled time does not come from the host clock (for example an SDR's hardware time
set on the PPS edge: return the device time and let the driver stamp samples with
it). `read_reference` is called from the event loop for every heartbeat and capture:
it must return in well under a millisecond, so poll slow hardware elsewhere and
return the cached state.

Any object with the three members of the `ClockSource` protocol works; subclassing is
a convenience, not a requirement.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

from petal_locate.protocol.messages import MAX_TIME_NS, ClockStatus

__all__ = [
    "ClockSource",
    "ClockSourceName",
    "DisciplinedClock",
    "ManualClock",
    "ReferenceReading",
    "SystemClock",
]

logger = logging.getLogger(__name__)

ClockSourceName = Literal["gpsdo", "ntp", "sim", "free"]
"""The `source` values of the protocol's ClockStatus."""

_SOURCES: Final[frozenset[str]] = frozenset({"gpsdo", "ntp", "sim", "free"})
_NS: Final[int] = 1_000_000_000
_MAX_SLEEP_S: Final[float] = 1.0
"""Longest single sleep of `SystemClock.sleep_until`; the wall clock is looked at again
after it, so a clock step is noticed within a second."""


@runtime_checkable
class ClockSource(Protocol):
    """Where a node gets labelled time and what it says about its quality."""

    def now_ns(self) -> int:
        """Labelled time now, integer UTC nanoseconds since the Unix epoch."""
        ...

    def status(self) -> ClockStatus:
        """The protocol ClockStatus of this clock right now."""
        ...

    async def sleep_until(self, t_ns: int) -> None:
        """Return once `now_ns() >= t_ns` (at once when it already is)."""
        ...


def _check_source(source: str) -> None:
    if source not in _SOURCES:
        raise ValueError(f"source: must be one of {', '.join(sorted(_SOURCES))}, got {source!r}")


def _check_offset(est_offset_ns: float) -> float:
    if isinstance(est_offset_ns, bool) or not isinstance(est_offset_ns, (int, float)):
        raise ValueError(f"est_offset_ns: must be a number, got {type(est_offset_ns).__name__}")
    if not math.isfinite(est_offset_ns):
        raise ValueError(f"est_offset_ns: must be finite, got {est_offset_ns!r}")
    return float(est_offset_ns)


def _check_time(t_ns: int, name: str = "t_ns") -> int:
    if isinstance(t_ns, bool) or not isinstance(t_ns, int):
        raise ValueError(f"{name}: must be an integer of nanoseconds, got {type(t_ns).__name__}")
    if not 0 <= t_ns <= MAX_TIME_NS:
        raise ValueError(f"{name}: {t_ns} is outside 0..2**63-1 ns")
    return t_ns


class _WallClock:
    """Labelled time from the host's UTC clock, with a sleep that follows it."""

    _offset_ns: int = 0
    """Added to the host clock (simulation only: a world run from a fixed epoch)."""

    @property
    def offset_ns(self) -> int:
        """What is added to the host clock, integer ns (0 but for simulation)."""
        return self._offset_ns

    def now_ns(self) -> int:
        """Host UTC time, integer ns since the Unix epoch (`time.time_ns`), plus the
        clock's offset (0 except for a simulated node run from a fixed epoch)."""
        return time.time_ns() + self._offset_ns

    async def sleep_until(self, t_ns: int) -> None:
        """Sleep until labelled time `t_ns` (UTC ns) has passed.

        Sleeps in steps of at most one second and looks at the clock again after
        each, so that a stepped wall clock cannot leave the caller asleep for long.
        """
        while True:
            remaining_ns = t_ns - self.now_ns()
            if remaining_ns <= 0:
                return
            await asyncio.sleep(min(remaining_ns / _NS, _MAX_SLEEP_S))


class SystemClock(_WallClock):
    """The operating system's UTC clock.

    source: what disciplines it; "ntp" unless told otherwise.
    locked: whether it is disciplined; True unless told otherwise. Not measured here.
    est_offset_ns: estimate of true time minus labelled time, ns (default 0).
    offset_ns: integer ns added to the host clock to make labelled time (default 0).
        Only for simulation: every process of a simulated deployment run from a fixed
        epoch (`petal-locate demo --start`) uses the same offset, so that the server and
        the simulated nodes agree on the simulated time. A real node keeps 0.

    While `locked` is false the status reports the holdover as the time since the
    clock was last marked locked (`set_locked`), or since construction when it never
    was.
    """

    def __init__(
        self,
        source: ClockSourceName = "ntp",
        locked: bool = True,
        est_offset_ns: float = 0.0,
        offset_ns: int = 0,
    ) -> None:
        _check_source(source)
        if not isinstance(locked, bool):
            raise ValueError(f"locked: must be a bool, got {type(locked).__name__}")
        if isinstance(offset_ns, bool) or not isinstance(offset_ns, int):
            raise ValueError(f"offset_ns: must be an integer of nanoseconds, got {type(offset_ns).__name__}")
        _check_time(time.time_ns() + offset_ns, "time.time_ns() + offset_ns")
        self._offset_ns = offset_ns
        self._source: ClockSourceName = source
        self._locked = locked
        self._est_offset_ns = _check_offset(est_offset_ns)
        self._unlocked_since = time.monotonic()

    def set_locked(self, locked: bool, est_offset_ns: float | None = None) -> None:
        """Record a change of discipline state (and optionally of the offset estimate)."""
        if not isinstance(locked, bool):
            raise ValueError(f"locked: must be a bool, got {type(locked).__name__}")
        if self._locked and not locked:
            self._unlocked_since = time.monotonic()
        self._locked = locked
        if est_offset_ns is not None:
            self._est_offset_ns = _check_offset(est_offset_ns)

    def status(self) -> ClockStatus:
        """ClockStatus with the configured source, lock state and offset estimate."""
        holdover = 0.0 if self._locked else max(0.0, time.monotonic() - self._unlocked_since)
        return ClockStatus(
            source=self._source,
            locked=self._locked,
            est_offset_ns=self._est_offset_ns,
            holdover_s=holdover,
        )


class ManualClock:
    """A clock that moves only when the test moves it.

    t_ns: the starting time, UTC ns.
    source, locked, est_offset_ns, holdover_s: what `status()` reports; change them
        with `set_status`.

    `sleep_until` returns when `advance` or `set` has moved the clock to or past the
    time waited for. Use it from one event loop.
    """

    def __init__(
        self,
        t_ns: int = 1_800_000_000 * _NS,
        *,
        source: ClockSourceName = "sim",
        locked: bool = True,
        est_offset_ns: float = 0.0,
        holdover_s: float = 0.0,
    ) -> None:
        self._now_ns = _check_time(t_ns)
        self._status = self._make_status(source, locked, est_offset_ns, holdover_s)
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    @staticmethod
    def _make_status(
        source: str, locked: bool, est_offset_ns: float, holdover_s: float
    ) -> ClockStatus:
        _check_source(source)
        if not isinstance(locked, bool):
            raise ValueError(f"locked: must be a bool, got {type(locked).__name__}")
        if (
            not isinstance(holdover_s, (int, float))
            or not math.isfinite(holdover_s)
            or holdover_s < 0
        ):
            raise ValueError(f"holdover_s: must be finite and not negative, got {holdover_s!r}")
        return ClockStatus(
            source=source,  # type: ignore[arg-type]  # checked by _check_source
            locked=locked,
            est_offset_ns=_check_offset(est_offset_ns),
            holdover_s=float(holdover_s),
        )

    def now_ns(self) -> int:
        """The time last set, UTC ns."""
        return self._now_ns

    def status(self) -> ClockStatus:
        """The status last set."""
        return self._status

    def set_status(
        self,
        *,
        source: ClockSourceName | None = None,
        locked: bool | None = None,
        est_offset_ns: float | None = None,
        holdover_s: float | None = None,
    ) -> None:
        """Change what `status()` reports; arguments left out keep their value."""
        current = self._status
        self._status = self._make_status(
            current.source if source is None else source,
            current.locked if locked is None else locked,
            current.est_offset_ns if est_offset_ns is None else est_offset_ns,
            current.holdover_s if holdover_s is None else holdover_s,
        )

    def set(self, t_ns: int) -> None:
        """Move the clock to `t_ns` (UTC ns; never backwards) and wake due sleepers."""
        _check_time(t_ns)
        if t_ns < self._now_ns:
            raise ValueError(f"t_ns: {t_ns} is before the current time {self._now_ns}")
        self._now_ns = t_ns
        while self._waiters and self._waiters[0][0] <= t_ns:
            _, _, waiter = heapq.heappop(self._waiters)
            if not waiter.done():
                waiter.set_result(None)

    def advance(self, dt_ns: int) -> int:
        """Move the clock forward by `dt_ns` nanoseconds; returns the new time."""
        if isinstance(dt_ns, bool) or not isinstance(dt_ns, int) or dt_ns < 0:
            raise ValueError(f"dt_ns: must be a non-negative integer, got {dt_ns!r}")
        self.set(self._now_ns + dt_ns)
        return self._now_ns

    @property
    def sleepers(self) -> int:
        """Callers waiting in `sleep_until` right now."""
        return sum(1 for _, _, waiter in self._waiters if not waiter.done())

    async def sleep_until(self, t_ns: int) -> None:
        """Wait until the clock has been moved to or past `t_ns` (UTC ns)."""
        if t_ns <= self._now_ns:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (t_ns, next(self._order), waiter))
        try:
            await waiter
        finally:
            if not waiter.done():
                waiter.cancel()


@dataclass(frozen=True)
class ReferenceReading:
    """What a disciplining reference says about itself right now.

    locked: whether the reference disciplines the clock at this moment.
    est_offset_ns: its estimate of true time minus labelled time, ns (signed).
    """

    locked: bool
    est_offset_ns: float = 0.0


class DisciplinedClock(_WallClock, ABC):
    """Base for a clock steered by an external reference (GPSDO, PTP grandmaster).

    Subclasses implement `read_reference`; see the module docstring. Labelled time
    defaults to the host clock, which is right when the reference steers the host
    (chrony or ptp4l fed by the receiver); override `now_ns` when it does not.

    source: the ClockStatus source to report, "gpsdo" by default.
    """

    def __init__(self, source: ClockSourceName = "gpsdo") -> None:
        _check_source(source)
        self._source: ClockSourceName = source
        self._last_locked_monotonic: float | None = None
        self._created_monotonic = time.monotonic()
        self._was_locked: bool | None = None

    @abstractmethod
    def read_reference(self) -> ReferenceReading:
        """Lock state and offset estimate of the reference, without blocking."""

    def _monotonic(self) -> float:
        """Monotonic seconds used for the holdover; a seam for tests."""
        return time.monotonic()

    def status(self) -> ClockStatus:
        """ClockStatus from `read_reference`, with the holdover kept here.

        `holdover_s` is 0 while locked, else the seconds since the reference was last
        seen locked by this method (since construction when it never was).
        """
        reading = self.read_reference()
        if not isinstance(reading, ReferenceReading) or not isinstance(reading.locked, bool):
            raise ValueError(
                f"read_reference: must return a ReferenceReading, got {type(reading).__name__}"
            )
        now = self._monotonic()
        if reading.locked:
            self._last_locked_monotonic = now
            holdover = 0.0
        else:
            since = self._last_locked_monotonic
            holdover = max(0.0, now - (self._created_monotonic if since is None else since))
        if self._was_locked is not None and self._was_locked != reading.locked:
            logger.warning(
                "clock reference %s",
                "locked" if reading.locked else "lost lock, in holdover",
                extra={"clock_source": self._source, "clock_locked": reading.locked},
            )
        self._was_locked = reading.locked
        return ClockStatus(
            source=self._source,
            locked=reading.locked,
            est_offset_ns=_check_offset(reading.est_offset_ns),
            holdover_s=holdover,
        )
