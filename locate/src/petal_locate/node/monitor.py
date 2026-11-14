"""Spectrum monitoring: one loop per assigned window, newest-wins hand-over to the sender.

A `Monitor` produces one spectrum frame per period of its window. It asks the driver
for a spectrum directly (`driver.psd`, the fast path); a driver without one returns
None, and the monitor then reads `nfft * navg` samples at `fs = span_hz` centred on
the window and computes `core.spectrum.welch_psd_db` in a worker thread. A frame
carries what was really measured: the first-sample time, centre and span the driver
reported.

Frames are droppable. The `FrameMailbox` between the monitors and the socket keeps at
most one unsent frame per window; a newer frame replaces it (PROTOCOL.md: "the node
keeps at most one unsent frame per monitor (newest wins)"). A stalled server
therefore costs a node one frame of memory per window and nothing else.

`ReadGate` keeps the number of simultaneous driver calls on a channel within the
driver's `concurrent_reads` and lets capture requests go ahead of monitor reads.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from petal_locate.core.spectrum import welch_psd_db
from petal_locate.node.clock import ClockSource
from petal_locate.node.drivers.base import CaptureError, SdrDriver, WorkerPool
from petal_locate.protocol.framing import encode_psd
from petal_locate.protocol.messages import MonitorAssignment

__all__ = ["FrameMailbox", "Monitor", "MonitorStats", "ReadGate", "SpectrumFrame"]

logger = logging.getLogger(__name__)

_NS: Final[int] = 1_000_000_000
_FAILURE_LOG_EVERY: Final[int] = 100
"""While a window keeps failing, one warning is logged per this many failures."""


@dataclass(frozen=True, eq=False)
class SpectrumFrame:
    """One spectrum of one window, ready to be sent.

    channel_id, monitor_id: the window.
    t_ns: labelled time of the first sample of the snippet, UTC ns.
    center_hz, span_hz: centre and width actually measured, Hz.
    nfft: bins. navg: Welch segments averaged. seq: frame counter of the window.
    psd_db: float32[nfft], dB, fft-shifted (bin k at center_hz + (k - nfft/2) *
        span_hz / nfft).
    """

    channel_id: str
    monitor_id: str
    t_ns: int
    center_hz: float
    span_hz: float
    nfft: int
    navg: int
    seq: int
    psd_db: np.ndarray

    @property
    def key(self) -> tuple[str, str]:
        """(channel_id, monitor_id): the window this frame belongs to."""
        return (self.channel_id, self.monitor_id)

    def encode(self) -> bytes:
        """The PLB1 `psd` frame for the wire. Raises `ValueError` for a frame that
        does not fit the protocol (wrong length, NaN or infinite bins)."""
        return encode_psd(
            self.channel_id,
            self.monitor_id,
            self.t_ns,
            self.center_hz,
            self.span_hz,
            self.nfft,
            self.navg,
            self.seq,
            self.psd_db,
        )


class FrameMailbox:
    """At most one unsent frame per window; a newer frame replaces an unsent one.

    Windows are served in the order in which they first got an unsent frame, and a
    replacement keeps its window's place, so no window can starve another.
    """

    def __init__(self, on_ready: Callable[[], None] | None = None) -> None:
        """on_ready: called (from `offer`) whenever a frame is waiting to be taken."""
        self._frames: dict[tuple[str, str], SpectrumFrame] = {}
        self._on_ready = on_ready
        self._dropped = 0

    def __len__(self) -> int:
        """Frames waiting (never more than the number of windows)."""
        return len(self._frames)

    @property
    def dropped(self) -> int:
        """Frames replaced or discarded before they were taken."""
        return self._dropped

    def offer(self, frame: SpectrumFrame) -> bool:
        """Hand over the newest frame of a window.

        Returns True when it replaced a frame that had not been taken (one drop).
        """
        replaced = frame.key in self._frames
        if replaced:
            self._dropped += 1
        self._frames[frame.key] = frame
        if self._on_ready is not None:
            self._on_ready()
        return replaced

    def take(self) -> SpectrumFrame | None:
        """Remove and return the frame of the window that has waited longest, or None."""
        if not self._frames:
            return None
        key = next(iter(self._frames))
        return self._frames.pop(key)

    def discard_channel(self, channel_id: str) -> int:
        """Drop the unsent frames of every window of `channel_id`; returns how many."""
        keys = [key for key in self._frames if key[0] == channel_id]
        for key in keys:
            del self._frames[key]
        self._dropped += len(keys)
        return len(keys)

    def clear(self) -> None:
        """Drop everything that is waiting."""
        self._dropped += len(self._frames)
        self._frames.clear()


class ReadGate:
    """Admission to one channel's driver: at most `limit` calls at once, captures first.

    A capture waits only for calls already in progress: while one is waiting, no new
    monitor read starts. Monitor reads are short (one snippet), so a capture gets its
    slot within a snippet even on a driver that serves a single read at a time.
    """

    def __init__(self, limit: int) -> None:
        """limit: the driver's `concurrent_reads`, at least 1."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit: must be an integer of at least 1, got {limit!r}")
        self._limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._captures_waiting = 0
        self._no_capture_waiting = asyncio.Event()
        self._no_capture_waiting.set()
        self._active = 0
        self._peak = 0

    @property
    def limit(self) -> int:
        """Calls admitted at once."""
        return self._limit

    @property
    def active(self) -> int:
        """Calls inside the gate right now."""
        return self._active

    @property
    def peak(self) -> int:
        """Most calls that were ever inside the gate at once."""
        return self._peak

    @asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        await self._semaphore.acquire()
        self._active += 1
        self._peak = max(self._peak, self._active)
        try:
            yield
        finally:
            self._active -= 1
            self._semaphore.release()

    @asynccontextmanager
    async def capture(self) -> AsyncIterator[None]:
        """Admit a capture read; it queues ahead of monitor reads not yet started."""
        self._captures_waiting += 1
        self._no_capture_waiting.clear()
        released = False

        def release_claim() -> None:
            nonlocal released
            if not released:
                released = True
                self._captures_waiting -= 1
                if self._captures_waiting == 0:
                    self._no_capture_waiting.set()

        try:
            async with self._slot():
                release_claim()
                yield
        finally:
            release_claim()

    @asynccontextmanager
    async def monitor(self) -> AsyncIterator[None]:
        """Admit a monitor read, after any capture that is waiting."""
        while self._captures_waiting:
            await self._no_capture_waiting.wait()
        async with self._slot():
            yield


@dataclass
class MonitorStats:
    """Counters of one monitor loop.

    frames: frames produced and handed over. failures: ticks that produced nothing
    because the driver or the spectrum failed. slipped: ticks started late because
    the previous frame took longer than the frame period.
    last_error: text of the most recent failure, or None.
    """

    frames: int = 0
    failures: int = 0
    slipped: int = 0
    last_error: str | None = field(default=None)


class Monitor:
    """The loop of one spectrum window."""

    def __init__(
        self,
        channel_id: str,
        spec: MonitorAssignment,
        driver: SdrDriver,
        *,
        clock: ClockSource,
        workers: WorkerPool,
        deliver: Callable[[SpectrumFrame], object],
        gate: ReadGate | None = None,
    ) -> None:
        """
        channel_id: the channel the window belongs to.
        spec: the window (centre and span in Hz, nfft, navg, frame rate in Hz).
        driver: the channel's open driver.
        clock: the node clock; frame ticks are labelled and paced with it.
        workers: where `welch_psd_db` runs.
        deliver: called with every frame produced (normally `FrameMailbox.offer`).
        gate: admission to the driver; None admits every call.
        """
        self.channel_id = channel_id
        self.spec = spec
        self.stats = MonitorStats()
        self._driver = driver
        self._clock = clock
        self._workers = workers
        self._deliver = deliver
        self._gate = gate
        self._seq = 0
        self._consecutive_failures = 0
        self._period_ns = max(1, round(_NS / spec.frame_rate_hz))

    @property
    def period_ns(self) -> int:
        """Frame period, ns."""
        return self._period_ns

    async def run(self) -> None:
        """Produce frames at the window's rate until cancelled.

        Ticks lie on a grid of one frame period. At each tick a snippet is asked for
        that starts at the node-clock time of that moment (so a driver sees a start
        that is at most the scheduling latency in the past). A frame that takes
        longer than a period delays the next tick; when the loop is a whole period or
        more behind, the grid is moved to now, so the rate degrades and never bursts
        and the driver is never asked for a stale interval. A failed tick is logged
        (once when the failures start, then every 100th, and once on recovery) and
        the loop goes on with the next tick: nothing a driver does stops monitoring.
        """
        next_ns = self._clock.now_ns()
        while True:
            await self._clock.sleep_until(next_ns)
            now_ns = self._clock.now_ns()
            if now_ns - next_ns >= self._period_ns:
                self.stats.slipped += 1
                next_ns = now_ns
            await self._tick(now_ns)
            next_ns += self._period_ns

    async def _tick(self, t_start_ns: int) -> None:
        """One frame, with the failure bookkeeping of `run`."""
        try:
            frame = await self.frame_at(t_start_ns)
        except CaptureError as exc:
            self._failed(f"{exc.code}: {exc.message}", None)
        except Exception as exc:
            # Deliberately broad: a driver is foreign code. Whatever it raises costs
            # one frame, is logged with its traceback when the failures start, and
            # must not end the loop. Cancellation is not an Exception and passes.
            self._failed(f"{type(exc).__name__}: {exc}", exc)
        else:
            if self._consecutive_failures:
                logger.info(
                    "monitor recovered",
                    extra=self._log_extra(failures=self._consecutive_failures),
                )
                self._consecutive_failures = 0
            self.stats.frames += 1
            self._deliver(frame)

    def _failed(self, text: str, exc: BaseException | None) -> None:
        self.stats.failures += 1
        self.stats.last_error = text
        if self._consecutive_failures % _FAILURE_LOG_EVERY == 0:
            logger.warning(
                "monitor frame failed: %s",
                text,
                extra=self._log_extra(failures=self.stats.failures),
                exc_info=exc if self._consecutive_failures == 0 else None,
            )
        self._consecutive_failures += 1

    def _log_extra(self, **more: object) -> dict[str, object]:
        return {"channel_id": self.channel_id, "monitor_id": self.spec.monitor_id, **more}

    async def frame_at(self, t_start_ns: int) -> SpectrumFrame:
        """Measure one frame whose snippet starts at `t_start_ns` (labelled UTC ns).

        Returns the frame with the next `seq` of this window.
        Raises `CaptureError` when the driver cannot serve the window or returns
        something unusable (wrong length, not finite).
        """
        spec = self.spec
        capture = None
        async with self._admission():
            fast = await self._driver.psd(
                t_start_ns, spec.center_hz, spec.span_hz, spec.nfft, spec.navg
            )
            if fast is None:
                capture = await self._driver.read(
                    t_start_ns, spec.nfft * spec.navg, spec.span_hz, spec.center_hz
                )
        if capture is None:
            psd = np.asarray(fast)
            if psd.shape != (spec.nfft,) or psd.dtype.kind != "f":
                raise CaptureError(
                    "driver",
                    f"psd() returned {psd.dtype} with shape {psd.shape}, expected "
                    f"float32[{spec.nfft}]",
                )
            psd = np.ascontiguousarray(psd, dtype=np.float32)
            t_ns, center_hz, span_hz = t_start_ns, spec.center_hz, spec.span_hz
        else:
            iq = np.asarray(capture.iq)
            needed = spec.nfft * spec.navg
            if iq.ndim != 1 or iq.dtype.kind != "c" or iq.shape[0] < needed:
                raise CaptureError(
                    "driver",
                    f"read() returned {iq.dtype} with shape {iq.shape}, expected at least "
                    f"{needed} complex samples",
                )
            psd = await self._workers.run(
                welch_psd_db, iq, float(capture.fs), spec.nfft, spec.navg
            )
            t_ns, center_hz, span_hz = int(capture.t_first_ns), float(capture.fc), float(capture.fs)
        if not bool(np.isfinite(psd).all()):
            raise CaptureError("driver", "spectrum holds NaN or infinite values")
        frame = SpectrumFrame(
            channel_id=self.channel_id,
            monitor_id=spec.monitor_id,
            t_ns=t_ns,
            center_hz=center_hz,
            span_hz=span_hz,
            nfft=spec.nfft,
            navg=spec.navg,
            seq=self._seq,
            psd_db=psd,
        )
        self._seq += 1
        return frame

    @asynccontextmanager
    async def _admission(self) -> AsyncIterator[None]:
        if self._gate is None:
            yield
        else:
            async with self._gate.monitor():
                yield
