"""`soapy`: a SoapySDR receiver with hardware timestamps and a lookback buffer.

**UNTESTED ON HARDWARE.** This driver is written against the documented SoapySDR
Python API (`SoapySDR.Device`, `setupStream`, `readStream`, `setHardwareTime`) and has
never been run against a radio: SoapySDR is not installed where this was developed.
What is tested are the parts that need no radio: the ring buffer and its time lookup
(`SampleRing`), the choice of stream centre and rate (`choose_stream`), the PPS
arithmetic (`next_pps_time_ns`) and the digital down-conversion shared with the SigMF
replay (`drivers.base.retune_resample`), plus the control flow against a software
stand-in that mimics the API. Expect to adjust device arguments, gain handling and
timing options for a particular radio, and validate timestamps against a known signal
before trusting a TDOA from it.

How it works
------------
`open` tunes the device once, to a centre and rate that cover every monitor window of
the channel (or to the `center_hz` / `sample_rate_hz` options), and starts a thread
that calls `readStream` continuously. Each block goes into a ring buffer of the last
`buffer_s` seconds together with its hardware time (`StreamResult.timeNs` when the
`SOAPY_SDR_HAS_TIME` flag is set; by sample count from the previous block otherwise).
A block whose time does not follow the previous one, or a `SOAPY_SDR_OVERFLOW`, opens
a new *segment*: the lookup never returns an interval that spans lost samples.

`read(t_start_ns, n, fs, fc)` finds the stream sample nearest `t_start_ns`, waits
until the ring holds the whole interval (so `t_start_ns` may be in the future, or up
to `capabilities.lookback_s` in the past), copies it out and converts it digitally to
the centre and rate asked for. `t_first_ns` is the hardware time of that stream
sample. Monitors and captures on a channel share the one stream, which is why any
number of them can be served at once.

Time
----
`time_sync` says where the device clock gets UTC from:

* "pps": wait for a PPS edge, then `setHardwareTime(next whole second, "PPS")` so
  that the device latches UTC on the following edge. Needs a 1 PPS input aligned to
  UTC (GPSDO) and a node clock good to well under half a second.
* "host": `setHardwareTime(node clock now)` at once; only as good as the host clock
  and the latency of the call (milliseconds).
* "device": leave the device time alone (it is already UTC, for example from an
  internal GPSDO whose time was set by other means).

A device without hardware time is labelled from the node clock when each stream
segment starts and by sample count after that; its captures say `clock_locked =
False`. With `lock_sensor` set, `clock_locked` follows that device sensor (for
example "ref_locked" or "gps_locked"); without it, "pps" and "device" are taken as
locked and "host" follows the node clock's own status.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np

from petal_locate.node.clock import ClockSource, SystemClock
from petal_locate.node.drivers.base import (
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    WorkerPool,
    mixer_phase_cycles,
    plan_resample,
    retune_resample,
    sample_index,
    sample_time_ns,
)
from petal_locate.protocol.messages import (
    MAX_CAPTURE_SAMPLES,
    ChannelAssignment,
    MonitorAssignment,
)

__all__ = [
    "RingSpan",
    "SampleRing",
    "SoapyDriver",
    "choose_stream",
    "next_pps_time_ns",
]

logger = logging.getLogger(__name__)

_NS: Final[int] = 1_000_000_000
_STREAM_HEADROOM: Final[float] = 1.25
"""Stream rate wanted, as a multiple of the span the monitors cover."""
_USABLE_FRACTION: Final[float] = 0.4
"""Share of the ring advertised as lookback, and the longest read as a share of it;
the remaining fifth keeps a read that starts at the lookback limit and has the
longest duration inside the ring while it is copied out."""
_READ_TIMEOUT_US: Final[int] = 200_000
_STALL_S: Final[float] = 2.0
"""A read gives up when the stream delivers nothing for this long, seconds."""
_PPS_TIMEOUT_S: Final[float] = 3.0
_PPS_SETTLE_S: float = 1.1
"""Wait after `setHardwareTime(t, "PPS")` for the edge that latches the time, seconds."""
_SENSOR_PERIOD_S: Final[float] = 1.0

TimeSync = Literal["pps", "host", "device"]


# -------------------------------------------------------------------- pure helpers


def next_pps_time_ns(now_ns: int) -> int:
    """UTC time of the PPS edge after the one that has just happened, integer ns.

    now_ns: node clock just after a PPS edge was seen, UTC ns; it may be off by up to
    half a second either way. Returns the whole second following the edge.
    """
    return ((now_ns + _NS // 2) // _NS + 1) * _NS


def choose_stream(
    monitors: Sequence[MonitorAssignment],
    available_rates_hz: Iterable[float],
    center_hz: float | None = None,
    sample_rate_hz: float | None = None,
) -> tuple[float, float]:
    """Centre and sample rate to run the stream at.

    monitors: the channel's spectrum windows (centre and span, Hz).
    available_rates_hz: the rates the device lists; empty when it lists none.
    center_hz, sample_rate_hz: explicit choices (driver options); either may be None.
    Returns `(center_hz, sample_rate_hz)`. Without an explicit centre it is the middle
    of the band the monitors cover; without an explicit rate it is the lowest listed
    rate that is at least 1.25 times that band, or the highest listed rate when none
    is that high (the monitors at the edges are then out of range and say so when
    read), or 1.25 times the band when the device lists no rates.
    Raises `ValueError` when there are no monitors and no explicit centre or rate.
    """
    if monitors:
        lo = min(m.center_hz - m.span_hz / 2 for m in monitors)
        hi = max(m.center_hz + m.span_hz / 2 for m in monitors)
    else:
        lo = hi = math.nan
    if center_hz is None:
        if not monitors:
            raise ValueError("center_hz: required when the channel has no monitors")
        center_hz = (lo + hi) / 2
    if not math.isfinite(center_hz) or center_hz < 0:
        raise ValueError(f"center_hz: must be finite and not negative, got {center_hz!r}")
    if sample_rate_hz is None:
        if not monitors:
            raise ValueError("sample_rate_hz: required when the channel has no monitors")
        wanted = _STREAM_HEADROOM * 2 * max(center_hz - lo, hi - center_hz)
        rates = sorted(float(r) for r in available_rates_hz if r > 0)
        if not rates:
            sample_rate_hz = wanted
        else:
            enough = [r for r in rates if r >= wanted]
            sample_rate_hz = enough[0] if enough else rates[-1]
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError(f"sample_rate_hz: must be finite and positive, got {sample_rate_hz!r}")
    return float(center_hz), float(sample_rate_hz)


@dataclass(frozen=True)
class RingSpan:
    """Where an interval of labelled time lies in a `SampleRing`.

    index: absolute index (count since the stream started) of the sample nearest the
        time asked for.
    t_first_ns: labelled time of that sample, UTC ns.
    ready: whether every sample of the interval, context included, has been written.
    """

    index: int
    t_first_ns: int
    ready: bool


@dataclass
class _Segment:
    """A run of samples without a gap: `index` is its first sample, at `t0_ns`."""

    index: int
    t0_ns: int


class SampleRing:
    """The most recent stream samples with their labelled times. Thread-safe.

    Samples are numbered from 0 in arrival order; the ring keeps the last `capacity`
    of them. Time is kept per *segment*: a run of blocks whose timestamps follow one
    another to within `tolerance_samples`. Inside a segment the time of a sample is
    the segment's first timestamp plus its offset over the rate (exact integer
    arithmetic, so rounding in the per-block timestamps never accumulates).
    """

    def __init__(self, capacity: int, fs: float, tolerance_samples: float = 0.5) -> None:
        """
        capacity: samples kept, at least 1. fs: stream sample rate, Hz.
        tolerance_samples: how far a block's timestamp may be from the time expected
            by sample count before it starts a new segment, in samples.
        """
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError(f"capacity: must be an integer of at least 1, got {capacity!r}")
        if not math.isfinite(fs) or fs <= 0:
            raise ValueError(f"fs: must be finite and positive, got {fs!r}")
        if not math.isfinite(tolerance_samples) or tolerance_samples <= 0:
            raise ValueError(f"tolerance_samples: must be positive, got {tolerance_samples!r}")
        self._capacity = capacity
        self._fs = float(fs)
        self._tolerance_ns = max(1, round(tolerance_samples * _NS / fs))
        self._buffer = np.zeros(capacity, dtype=np.complex64)
        self._written = 0
        self._segments: list[_Segment] = []
        self._gap_pending = False
        self._gaps = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """Samples the ring holds."""
        return self._capacity

    @property
    def fs(self) -> float:
        """Stream sample rate, Hz."""
        return self._fs

    @property
    def written(self) -> int:
        """Samples appended since the start (the index the next sample will get)."""
        with self._lock:
            return self._written

    @property
    def oldest(self) -> int:
        """Index of the oldest sample still held."""
        with self._lock:
            return max(0, self._written - self._capacity)

    @property
    def gaps(self) -> int:
        """Discontinuities seen so far (segments started after the first)."""
        with self._lock:
            return self._gaps

    def mark_gap(self) -> None:
        """Declare that samples were lost: the next block starts a new segment and
        must carry a time."""
        with self._lock:
            self._gap_pending = True

    def needs_time(self) -> bool:
        """Whether the next `append` must be given a time (first block, or after a gap)."""
        with self._lock:
            return self._gap_pending or not self._segments

    def append(self, samples: np.ndarray, t_ns: int | None) -> bool:
        """Add a block of stream samples.

        samples: complex64[m] (any m). t_ns: labelled time of `samples[0]`, UTC ns, or
        None when the block carries no timestamp and simply continues the previous
        one. Returns True when the block started a new segment after a gap.
        Raises `ValueError` when `t_ns` is None for the first block or after
        `mark_gap` (there is nothing to continue from).
        """
        block = np.asarray(samples, dtype=np.complex64)
        if block.ndim != 1:
            raise ValueError(f"samples: must be one-dimensional, got shape {block.shape}")
        count = int(block.shape[0])
        if count == 0:
            return False
        with self._lock:
            first = self._written
            new_segment = False
            if self._gap_pending or not self._segments:
                if t_ns is None:
                    raise ValueError("t_ns: a block that starts a segment needs a time")
                new_segment = True
            elif t_ns is not None:
                last = self._segments[-1]
                expected = sample_time_ns(first - last.index, last.t0_ns, self._fs)
                new_segment = abs(t_ns - expected) > self._tolerance_ns
            started_after_gap = new_segment and bool(self._segments)
            if new_segment:
                assert t_ns is not None
                self._segments.append(_Segment(index=first, t0_ns=int(t_ns)))
                self._gap_pending = False
                if started_after_gap:
                    self._gaps += 1
            if count > self._capacity:  # only the tail of an oversized block survives
                block = block[count - self._capacity :]
            start = (first + count - block.shape[0]) % self._capacity
            head = min(self._capacity - start, block.shape[0])
            self._buffer[start : start + head] = block[:head]
            self._buffer[: block.shape[0] - head] = block[head:]
            self._written = first + count
            oldest = max(0, self._written - self._capacity)
            while len(self._segments) > 1 and self._segments[1].index <= oldest:
                self._segments.pop(0)
            return started_after_gap

    def time_of(self, index: int) -> int | None:
        """Labelled time of sample `index`, UTC ns; None when no segment covers it."""
        with self._lock:
            for segment in reversed(self._segments):
                if index >= segment.index:
                    return sample_time_ns(index - segment.index, segment.t0_ns, self._fs)
            return None

    def locate(
        self, t_start_ns: int, count: int, before: int = 0, after: int = 0
    ) -> RingSpan | None:
        """Find the samples for an interval of labelled time.

        t_start_ns: time wanted for the first sample, UTC ns. count: samples wanted
        from there. before, after: extra samples of context needed on either side.
        Returns None while the ring is empty; otherwise the span, whose `ready` says
        whether everything up to `index + count + after` has arrived (the interval
        may still lie in the future of the newest segment).
        Raises `CaptureError("too_late")` when the interval starts before the oldest
        sample held, and `CaptureError("overflow")` when samples were lost at or
        inside the interval.
        """
        if count < 1 or before < 0 or after < 0:
            raise ValueError("count must be at least 1 and before/after not negative")
        with self._lock:
            if not self._segments:
                return None
            oldest = max(0, self._written - self._capacity)
            for position in range(len(self._segments) - 1, -1, -1):
                segment = self._segments[position]
                offset = sample_index(t_start_ns, segment.t0_ns, self._fs)
                if offset < 0:
                    continue  # earlier than this segment: look at the one before it
                index = segment.index + offset
                is_last = position == len(self._segments) - 1
                end = None if is_last else self._segments[position + 1].index
                if end is not None and index >= end:
                    raise CaptureError(
                        "overflow", "samples at the requested time were lost by the stream"
                    )
                if index - before < oldest:
                    raise CaptureError(
                        "too_late",
                        f"interval starts {(oldest - index + before) / self._fs:.3f} s before "
                        "the oldest sample still buffered",
                    )
                if index - before < segment.index:
                    raise CaptureError(
                        "overflow", "samples just before the requested time were lost"
                    )
                if end is not None and index + count + after > end:
                    raise CaptureError(
                        "overflow", "samples inside the requested interval were lost"
                    )
                return RingSpan(
                    index=index,
                    t_first_ns=sample_time_ns(offset, segment.t0_ns, self._fs),
                    ready=index + count + after <= self._written,
                )
            raise CaptureError(
                "too_late", "interval starts before the oldest sample still buffered"
            )

    def read(self, index: int, count: int) -> np.ndarray:
        """Copy `count` samples starting at absolute `index`: complex64[count].

        Raises `CaptureError("too_late")` when the start has been overwritten since it
        was located, and `ValueError` when the end has not been written yet.
        """
        if count < 1:
            raise ValueError(f"count: must be at least 1, got {count}")
        with self._lock:
            if index < max(0, self._written - self._capacity):
                raise CaptureError("too_late", "interval was overwritten before it was copied")
            if index + count > self._written:
                raise ValueError("index + count: beyond the samples written so far")
            start = index % self._capacity
            head = min(self._capacity - start, count)
            out = np.empty(count, dtype=np.complex64)
            out[:head] = self._buffer[start : start + head]
            out[head:] = self._buffer[: count - head]
            return out


# ------------------------------------------------------------------------ the driver


def _load_soapy() -> Any:
    """Import the SoapySDR Python module, or explain why the driver cannot open."""
    try:
        import SoapySDR  # type: ignore[import-not-found]  # optional hardware library
    except ImportError as exc:
        raise CaptureError(
            "driver",
            "the SoapySDR Python bindings are not installed (system package "
            "python3-soapysdr, or SoapySDR built from source; they are not on PyPI); the soapy driver "
            f"cannot open ({exc})",
        ) from exc
    return SoapySDR


@dataclass
class _Stream:
    """An activated receive stream and the thread that drains it."""

    soapy: Any
    device: Any
    handle: Any
    ring: SampleRing
    fs: float
    fc: float
    has_hardware_time: bool
    mtu: int
    thread: threading.Thread | None = None


class SoapyDriver:
    """One SoapySDR receive channel (see the module docstring; untested on hardware).

    Options (keyword-only):
      device: SoapySDR device arguments, a string ("driver=uhd,serial=...") or a dict.
      rf_channel: receive channel index on the device (default 0).
      center_hz, sample_rate_hz: stream centre and rate, Hz; by default chosen to
        cover the channel's monitor windows (`choose_stream`).
      bandwidth_hz: analogue filter bandwidth, Hz (default: left to the device).
      gain_db: receive gain, dB; None enables the device's automatic gain when it has
        one and otherwise leaves the gain alone.
      antenna: antenna port name (default: left to the device).
      clock_source, time_source: device reference selections, for example "external"
        or "gpsdo" (default: left to the device).
      time_sync: "pps", "host" or "device" (module docstring). Default "host".
      lock_sensor: device sensor whose value "true" means the reference is locked.
      buffer_s: seconds of stream kept for lookback (default 4.0); reads may start up
        to 0.4 of it in the past and last up to 0.4 of it.
      concurrent_reads: reads served at once (default 4).
      clock: the node clock, used by "host"/"pps" sync and for devices without
        hardware time; None takes the one in the context, or the system clock.
    """

    def __init__(
        self,
        *,
        device: str | Mapping[str, str] = "",
        rf_channel: int = 0,
        center_hz: float | None = None,
        sample_rate_hz: float | None = None,
        bandwidth_hz: float | None = None,
        gain_db: float | None = None,
        antenna: str | None = None,
        clock_source: str | None = None,
        time_source: str | None = None,
        time_sync: TimeSync = "host",
        lock_sensor: str | None = None,
        buffer_s: float = 4.0,
        concurrent_reads: int = 4,
        clock: ClockSource | None = None,
    ) -> None:
        if time_sync not in ("pps", "host", "device"):
            raise ValueError(f"time_sync: must be 'pps', 'host' or 'device', got {time_sync!r}")
        if isinstance(rf_channel, bool) or not isinstance(rf_channel, int) or rf_channel < 0:
            raise ValueError(f"rf_channel: must be a non-negative integer, got {rf_channel!r}")
        if not math.isfinite(buffer_s) or buffer_s <= 0:
            raise ValueError(f"buffer_s: must be finite and positive, got {buffer_s!r}")
        if isinstance(concurrent_reads, bool) or not isinstance(concurrent_reads, int) \
                or concurrent_reads < 1:
            raise ValueError(f"concurrent_reads: must be an integer of at least 1, "
                             f"got {concurrent_reads!r}")
        self._device_args = dict(device) if isinstance(device, Mapping) else device
        self._rf_channel = rf_channel
        self._center_hz = center_hz
        self._sample_rate_hz = sample_rate_hz
        self._bandwidth_hz = bandwidth_hz
        self._gain_db = gain_db
        self._antenna = antenna
        self._clock_source = clock_source
        self._time_source = time_source
        self._time_sync: TimeSync = time_sync
        self._lock_sensor = lock_sensor
        self._buffer_s = float(buffer_s)
        self._concurrent_reads = concurrent_reads
        self._clock = clock
        self._workers: WorkerPool | None = None
        self._channel_id: str | None = None
        self._stream: _Stream | None = None
        self._capabilities: Capabilities | None = None
        self._stop = threading.Event()
        self._failure: str | None = None
        self._reference_locked: bool | None = None
        self._overflows = 0

    @property
    def capabilities(self) -> Capabilities:
        """Limits of the running stream: its band, its rate, 0.4 of the ring as lookback.

        Raises `CaptureError("driver")` before `open`, because they come from the device.
        """
        if self._capabilities is None:
            raise CaptureError("driver", "soapy driver is not open")
        return self._capabilities

    @property
    def overflows(self) -> int:
        """Stream overflows reported by the device since `open`."""
        return self._overflows

    # ---------------------------------------------------------------- open and close

    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        """Open the device, set its time, start the stream.

        Raises `CaptureError("driver")` when SoapySDR is not installed, the device
        cannot be opened or configured, or the time cannot be set as asked.
        """
        if self._stream is not None:
            raise CaptureError("driver", "soapy driver is already open")
        if self._clock is None:
            self._clock = ctx.clock if ctx.clock is not None else SystemClock()
        self._workers = ctx.workers if ctx.workers is not None else WorkerPool()
        self._channel_id = channel.channel_id
        self._stop.clear()
        self._failure = None
        # Opening blocks (USB enumeration, a PPS wait of over a second) but uses no
        # CPU, so it gets a thread of its own instead of a worker-pool slot.
        stream = await asyncio.to_thread(self._open_blocking, list(channel.monitors))
        half = stream.fs / 2
        self._capabilities = Capabilities(
            min_hz=max(0.0, stream.fc - half),
            max_hz=stream.fc + half,
            max_sample_rate_hz=stream.fs,
            lookback_s=_USABLE_FRACTION * stream.ring.capacity / stream.fs,
            concurrent_reads=self._concurrent_reads,
        )
        thread = threading.Thread(
            target=self._stream_loop,
            args=(stream,),
            name=f"soapy-{channel.channel_id}",
            daemon=True,
        )
        stream.thread = thread
        self._stream = stream
        thread.start()
        logger.info(
            "soapy stream running",
            extra={
                "channel_id": channel.channel_id,
                "center_hz": stream.fc,
                "sample_rate_hz": stream.fs,
                "hardware_time": stream.has_hardware_time,
                "time_sync": self._time_sync,
            },
        )

    def _open_blocking(self, monitors: list[MonitorAssignment]) -> _Stream:
        """Everything `open` does with the device (runs in a thread)."""
        soapy = _load_soapy()
        clock = self._clock
        assert clock is not None
        device: Any = None
        handle: Any = None
        try:
            device = soapy.Device(self._device_args)
            rx, channel = soapy.SOAPY_SDR_RX, self._rf_channel
            if self._clock_source is not None:
                device.setClockSource(self._clock_source)
            if self._time_source is not None:
                device.setTimeSource(self._time_source)
            center, rate = choose_stream(
                monitors,
                [float(r) for r in device.listSampleRates(rx, channel)],
                self._center_hz,
                self._sample_rate_hz,
            )
            device.setSampleRate(rx, channel, rate)
            device.setFrequency(rx, channel, center)
            fs = float(device.getSampleRate(rx, channel))
            fc = float(device.getFrequency(rx, channel))
            if not (math.isfinite(fs) and fs > 0 and math.isfinite(fc) and fc >= 0):
                raise CaptureError("driver", f"device reports rate {fs!r} Hz at {fc!r} Hz")
            if self._bandwidth_hz is not None:
                device.setBandwidth(rx, channel, self._bandwidth_hz)
            if self._antenna is not None:
                device.setAntenna(rx, channel, self._antenna)
            if self._gain_db is not None:
                device.setGain(rx, channel, self._gain_db)
            elif device.hasGainMode(rx, channel):
                device.setGainMode(rx, channel, True)
            has_time = bool(device.hasHardwareTime(""))
            self._sync_time(device, clock, has_time)
            handle = device.setupStream(rx, soapy.SOAPY_SDR_CF32, [channel])
            mtu = max(1024, int(device.getStreamMTU(handle)))
            capacity = max(mtu, math.ceil(self._buffer_s * fs))
            status = device.activateStream(handle)
            if isinstance(status, int) and status != 0:
                raise CaptureError(
                    "driver", f"activateStream failed: {soapy.errToStr(status)} ({status})"
                )
            return _Stream(
                soapy=soapy,
                device=device,
                handle=handle,
                ring=SampleRing(capacity, fs),
                fs=fs,
                fc=fc,
                has_hardware_time=has_time,
                mtu=mtu,
            )
        except Exception as exc:  # SoapySDR raises RuntimeError and others without a base
            self._release(soapy, device, handle)
            if isinstance(exc, CaptureError):
                raise
            raise CaptureError("driver", f"SoapySDR: {type(exc).__name__}: {exc}") from exc

    def _sync_time(self, device: Any, clock: ClockSource, has_time: bool) -> None:
        """Set the device time as `time_sync` asks (blocking; over a second for "pps")."""
        if self._time_sync == "device":
            return
        if not has_time:
            if self._time_sync == "pps":
                raise CaptureError("driver", "time_sync 'pps' needs a device with hardware time")
            return  # "host" on a device without a clock: samples are labelled on arrival
        if self._time_sync == "host":
            device.setHardwareTime(clock.now_ns(), "")
            return
        latched = device.getHardwareTime("PPS")
        give_up = time.monotonic() + _PPS_TIMEOUT_S
        while device.getHardwareTime("PPS") == latched:
            if time.monotonic() > give_up or self._stop.is_set():
                raise CaptureError(
                    "driver", f"no PPS edge within {_PPS_TIMEOUT_S:g} s; check the 1 PPS input"
                )
            time.sleep(0.01)
        target_ns = next_pps_time_ns(clock.now_ns())
        device.setHardwareTime(target_ns, "PPS")
        time.sleep(_PPS_SETTLE_S)  # the edge that latches the time
        error_ns = int(device.getHardwareTime("")) - clock.now_ns()
        if abs(error_ns) > _NS // 2:
            raise CaptureError(
                "driver",
                f"device time is {error_ns / _NS:+.3f} s from the node clock after the PPS set",
            )

    @staticmethod
    def _release(soapy: Any, device: Any, handle: Any) -> None:
        """Deactivate and close what was opened; failures are logged, never raised."""
        if device is None:
            return
        if handle is not None:
            for step in ("deactivateStream", "closeStream"):
                try:
                    getattr(device, step)(handle)
                except Exception:  # third-party code; cleanup must go on
                    logger.warning("soapy %s failed during release", step, exc_info=True)
        unmake = getattr(getattr(soapy, "Device", None), "unmake", None)
        if callable(unmake):
            try:
                unmake(device)
            except Exception:  # third-party code; cleanup must go on
                logger.warning("soapy Device.unmake failed during release", exc_info=True)

    async def close(self) -> None:
        """Stop the stream thread and release the device. Idempotent."""
        stream, self._stream = self._stream, None
        self._capabilities = None
        self._stop.set()
        if stream is None:
            return
        await asyncio.to_thread(self._close_blocking, stream)
        logger.info(
            "soapy stream closed",
            extra={"channel_id": self._channel_id, "overflows": self._overflows},
        )

    def _close_blocking(self, stream: _Stream) -> None:
        """Join the reader and release the device (runs in a thread)."""
        if stream.thread is not None:
            stream.thread.join(timeout=2.0)
            if stream.thread.is_alive():
                logger.error(
                    "soapy stream thread did not stop; releasing the device anyway",
                    extra={"channel_id": self._channel_id},
                )
        self._release(stream.soapy, stream.device, stream.handle)

    # ----------------------------------------------------------------- stream thread

    def _stream_loop(self, stream: _Stream) -> None:
        """Drain `readStream` into the ring until told to stop (runs in its thread)."""
        soapy, device, ring = stream.soapy, stream.device, stream.ring
        clock = self._clock
        assert clock is not None
        block = np.empty(stream.mtu, dtype=np.complex64)
        has_time_flag = int(soapy.SOAPY_SDR_HAS_TIME)
        next_sensor = 0.0
        try:
            while not self._stop.is_set():
                result = device.readStream(
                    stream.handle, [block], block.shape[0], timeoutUs=_READ_TIMEOUT_US
                )
                count = int(result.ret)
                if count > 0:
                    t_ns: int | None = None
                    if stream.has_hardware_time and int(result.flags) & has_time_flag:
                        t_ns = int(result.timeNs)
                    elif ring.needs_time():
                        # No timestamp to start a segment from: label the block by
                        # the time of its arrival (device time when there is one, else
                        # the node clock). Good to the transport latency.
                        arrived_ns = (
                            int(device.getHardwareTime(""))
                            if stream.has_hardware_time
                            else clock.now_ns()
                        )
                        t_ns = sample_time_ns(-count, arrived_ns, stream.fs)
                    if ring.append(block[:count], t_ns):
                        logger.warning(
                            "soapy stream discontinuity",
                            extra={"channel_id": self._channel_id, "gaps": ring.gaps},
                        )
                elif count == int(soapy.SOAPY_SDR_OVERFLOW):
                    self._overflows += 1
                    ring.mark_gap()
                elif count != int(soapy.SOAPY_SDR_TIMEOUT) and count != 0:
                    raise RuntimeError(f"readStream failed: {soapy.errToStr(count)} ({count})")
                if self._lock_sensor is not None and time.monotonic() >= next_sensor:
                    next_sensor = time.monotonic() + _SENSOR_PERIOD_S
                    value = str(device.readSensor(self._lock_sensor)).strip().lower()
                    self._reference_locked = value in ("true", "1", "locked")
        except Exception as exc:  # third-party code: any failure ends the stream, loudly
            self._failure = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "soapy stream thread failed", extra={"channel_id": self._channel_id}
            )

    def _clock_locked(self, stream: _Stream) -> bool:
        """Whether time labels of the stream come from a disciplined clock right now."""
        if not stream.has_hardware_time:
            return False
        if self._lock_sensor is not None:
            return bool(self._reference_locked)
        if self._time_sync == "host":
            clock = self._clock
            return clock is not None and clock.status().locked
        return True

    # ------------------------------------------------------------------------- reads

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        """`n` samples at (`fs`, `fc`) from the stream sample nearest `t_start_ns`.

        t_start_ns: labelled (hardware) time wanted for the first sample, UTC ns; in
        the future or up to `capabilities.lookback_s` in the past. n: samples.
        fs, fc: rate and centre wanted, Hz, inside the stream's band.
        Returns a `Capture` with the hardware time of its first sample.
        Raises `CaptureError`: "driver" when not open or the stream has failed or
        stalled, "out_of_range" for a band outside the stream or an interval longer
        than 0.4 of the ring, "too_late" for an interval no longer buffered,
        "overflow" when samples were lost in it.
        """
        stream, workers = self._stream, self._workers
        if stream is None or workers is None:
            raise CaptureError("driver", "soapy driver is not open")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_CAPTURE_SAMPLES:
            raise CaptureError(
                "out_of_range", f"n must be 1..{MAX_CAPTURE_SAMPLES} samples, got {n!r}"
            )
        plan = plan_resample(stream.fs, stream.fc, fs, fc)
        ring = stream.ring
        core, margin = plan.core_length(n), plan.margin
        if core + 2 * margin > _USABLE_FRACTION * ring.capacity:
            raise CaptureError(
                "out_of_range",
                f"interval of {core / stream.fs:.3f} s is longer than the "
                f"{_USABLE_FRACTION * ring.capacity / stream.fs:.3f} s this stream can serve",
            )
        span = await self._wait_for(stream, t_start_ns, core, margin)
        source = await workers.run(ring.read, span.index - margin, core + 2 * margin)
        if plan.identity:
            iq = source[:n]
        else:
            phase0 = mixer_phase_cycles(plan.shift_hz, span.index, stream.fs)
            iq = await workers.run(retune_resample, source, plan, n, phase0)
        return Capture(
            iq=iq,
            t_first_ns=span.t_first_ns,
            fs=plan.fs_out,
            fc=plan.fc_out,
            overflow=False,
            clock_locked=self._clock_locked(stream),
        )

    async def _wait_for(
        self, stream: _Stream, t_start_ns: int, core: int, margin: int
    ) -> RingSpan:
        """Wait until the ring holds the interval; returns where it is."""
        ring = stream.ring
        last_written, last_progress = -1, time.monotonic()
        while True:
            if self._stream is not stream:
                raise CaptureError("driver", "soapy driver was closed during the read")
            if self._failure is not None:
                raise CaptureError("driver", f"stream failed: {self._failure}")
            span = ring.locate(t_start_ns, core, before=margin, after=margin)
            if span is not None and span.ready:
                return span
            written = ring.written
            now = time.monotonic()
            if written != last_written:
                last_written, last_progress = written, now
            elif now - last_progress > _STALL_S:
                raise CaptureError(
                    "driver", f"stream delivered no samples for {_STALL_S:g} s"
                )
            missing = 0 if span is None else span.index + core + margin - written
            await asyncio.sleep(min(0.25, max(0.005, missing / stream.fs)))

    async def psd(
        self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int
    ) -> np.ndarray | None:
        """No fast path: the monitor reads IQ from the ring and computes the spectrum."""
        return None
