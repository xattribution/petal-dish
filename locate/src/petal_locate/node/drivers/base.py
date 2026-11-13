"""Driver interface of the node agent (ARCHITECTURE.md section 6) and shared helpers.

A driver is one receive channel's radio: it returns time-stamped IQ for an interval of
labelled time and, optionally, a spectrum without the detour through IQ. The agent
opens one driver per assigned channel and never looks behind this interface.

Contents:

* `Capture`, `Capabilities`, `DriverContext`, `CaptureError` and the `SdrDriver`
  protocol, with the fields and signatures ARCHITECTURE.md section 6 fixes.
* Two optional hooks a driver may add (`SimUpdateHook`, `AssignHook`).
* Helpers every driver needs and should not reinvent: exact conversion between sample
  indices and integer-nanosecond times, digital retuning and rational resampling
  (`plan_resample`, `retune_resample`), and `WorkerPool`, the bounded doorway to
  worker threads.

Time. Absolute time is integer UTC nanoseconds of the node's labelled clock. Sample
`k` of a stream that started at `t0_ns` with rate `fs` is at `t0_ns + k / fs`; the
conversions here use rational arithmetic so that an index of 1e15 does not lose
nanoseconds the way `t0 + k / fs * 1e9` in float64 would.

Threads. Nothing in a driver may hold the event loop for more than 50 ms. CPU work
(synthesis, filtering, file reads) goes through `WorkerPool.run`, which is
`asyncio.to_thread` behind a semaphore; the agent hands every driver the node's pool
in `DriverContext.workers` (two workers per node).
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar, runtime_checkable

import numpy as np

from petal_locate.protocol.messages import CAPTURE_ERROR_CODES, ChannelAssignment
from petal_locate.protocol.messages import Capabilities as WireCapabilities

if TYPE_CHECKING:  # the clock module imports nothing from here; this is for type checkers
    from petal_locate.node.clock import ClockSource

__all__ = [
    "DEFAULT_WORKERS",
    "AssignHook",
    "Capabilities",
    "Capture",
    "CaptureError",
    "DriverContext",
    "ResamplePlan",
    "SdrDriver",
    "SimUpdateHook",
    "WorkerPool",
    "duration_ns",
    "mixer_phase_cycles",
    "plan_resample",
    "retune_resample",
    "sample_index",
    "sample_time_ns",
]

DEFAULT_WORKERS: Final[int] = 2
"""Worker threads a node uses for CPU work at once (ARCHITECTURE.md section 2)."""

_NS: Final[int] = 1_000_000_000
_T = TypeVar("_T")


# ------------------------------------------------------------------------ data types


@dataclass
class Capture:
    """Samples a driver returned for one `read`.

    iq: complex64[n], unit = the driver's amplitude scale (unit-variance thermal noise
        for the synthetic drivers).
    t_first_ns: labelled time of `iq[0]`, integer UTC ns. The true time of the first
        sample, which need not equal the `t_start_ns` asked for.
    fs: sample rate actually used, Hz.
    fc: centre frequency actually used, Hz.
    overflow: whether samples were lost inside the interval.
    clock_locked: whether the time labels of this capture come from a disciplined
        clock.
    """

    iq: np.ndarray
    t_first_ns: int
    fs: float
    fc: float
    overflow: bool = False
    clock_locked: bool = True


@dataclass(frozen=True)
class Capabilities:
    """What an opened driver can do (reported to the server in `assign_ack`).

    min_hz, max_hz: range of centre frequencies it can serve, Hz.
    max_sample_rate_hz: highest complex sample rate, Hz.
    lookback_s: how far into the past a read may start, seconds (0 for none).
    concurrent_reads: how many `read`/`psd` calls it serves at once (at least 1); the
        agent never exceeds it.

    Raises `ValueError` naming the field for a value that cannot be true.
    """

    min_hz: float
    max_hz: float
    max_sample_rate_hz: float
    lookback_s: float
    concurrent_reads: int

    def __post_init__(self) -> None:
        for name in ("min_hz", "max_hz", "lookback_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name}: must be a number, got {type(value).__name__}")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name}: must be finite and not negative, got {value!r}")
        rate = self.max_sample_rate_hz
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ValueError(f"max_sample_rate_hz: must be a number, got {type(rate).__name__}")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"max_sample_rate_hz: must be finite and positive, got {rate!r}")
        if self.max_hz < self.min_hz:
            raise ValueError(f"max_hz: {self.max_hz!r} is below min_hz {self.min_hz!r}")
        reads = self.concurrent_reads
        if isinstance(reads, bool) or not isinstance(reads, int) or reads < 1:
            raise ValueError(f"concurrent_reads: must be an integer of at least 1, got {reads!r}")

    def to_wire(self) -> WireCapabilities:
        """The `capabilities` object of an `assign_ack` channel entry."""
        return WireCapabilities(
            min_hz=float(self.min_hz),
            max_hz=float(self.max_hz),
            max_sample_rate_hz=float(self.max_sample_rate_hz),
            lookback_s=float(self.lookback_s),
            concurrent_reads=self.concurrent_reads,
        )


@dataclass
class DriverContext:
    """What the agent tells a driver about the session it is opened in.

    site_id: the site this node serves.
    server_http_url: origin of the server for plain HTTP requests, `http(s)://host:port`
        (the node's `ws`/`wss` URL with the scheme mapped), no trailing slash.
    token: the site's bearer token (for the simulation backplane). Kept out of `repr`.
    simulation: the `simulation` object of the `welcome` as a dict
        (`{"enabled": true, "scenario_url": ..., "version": ...}`) when the server runs
        a scenario, else None. Real drivers ignore it.

    The two fields below are not in ARCHITECTURE.md section 6; they are optional and a
    driver that ignores them is still correct:

    clock: the node's `ClockSource`. A driver that waits for wall-clock time or needs
        "now" should use it rather than `time.time_ns()`, so that the whole node runs
        on one clock (and on a `ManualClock` in tests). None means the system clock.
    workers: the node's `WorkerPool`. A driver should run its CPU work through it so
        that the node as a whole stays at two worker threads. None means the driver
        makes its own.
    """

    site_id: str
    server_http_url: str
    token: str = field(repr=False)
    simulation: dict[str, Any] | None = None
    clock: ClockSource | None = None
    workers: WorkerPool | None = None


class CaptureError(Exception):
    """A driver cannot serve a read; becomes a `capture_error` on the wire.

    code: one of the protocol's capture error codes: "out_of_range" (frequency or
        rate), "too_late" (interval no longer buffered), "overflow" (samples lost in
        the interval), "driver" (anything wrong with the driver or its hardware),
        "busy", "cancelled".
    message: human-readable explanation, sent to the server as it is.

    Raises `ValueError` for a code the protocol does not define, at the `raise` site
    of the driver, so that a typing mistake cannot reach the wire.
    """

    def __init__(self, code: str, message: str = "") -> None:
        if code not in CAPTURE_ERROR_CODES:
            raise ValueError(
                f"code: unknown capture error code {code!r}; "
                f"the protocol defines {', '.join(sorted(CAPTURE_ERROR_CODES))}"
            )
        super().__init__(code, message)
        self.code: str = code
        self.message: str = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


class SdrDriver(Protocol):
    """One receive channel's radio.

    Life cycle: constructed by the registry or a factory, `open` once, any number of
    `read` and `psd` calls (up to `capabilities.concurrent_reads` at a time), `close`
    once. `close` must be safe after a failed `open` and must make pending reads fail
    or finish promptly. Every coroutine must honour cancellation: the agent cancels
    reads when a capture is cancelled, a channel is removed or the session ends.

    capabilities: what the driver can do. Must be valid from the moment `open`
        returns; a driver whose limits depend on its hardware or file may raise
        `CaptureError("driver")` when it is read earlier.
    """

    capabilities: Capabilities

    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        """Prepare to serve `channel`. Raises `CaptureError("driver")` when it cannot."""
        ...

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        """Return `n` complex samples at rate `fs` (Hz) centred on `fc` (Hz, RF).

        t_start_ns: labelled time of the first sample wanted, UTC ns. It may be
            slightly in the future (the driver waits until the interval has passed)
            or in the recent past (served from the driver's lookback buffer).
        Raises `CaptureError(code, message)` when it cannot serve the interval.
        """
        ...

    async def psd(
        self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int
    ) -> np.ndarray | None:
        """Fast-path spectrum of the window: float32[nfft] in `welch_psd_db` units
        (dB, fft-shifted, bin k at fc + (k - nfft/2) * span_hz / nfft), or None when
        the driver has no fast path and the monitor should read IQ instead."""
        ...

    async def close(self) -> None:
        """Release the radio. Idempotent."""
        ...


@runtime_checkable
class SimUpdateHook(Protocol):
    """Optional driver hook: the simulated world changed (PROTOCOL.md, backplane).

    The agent awaits `on_sim_update` on every open driver that has it, as soon as a
    `sim_update` arrives. The driver fetches the new world and uses it for samples at
    or after `effective_ns`.
    """

    async def on_sim_update(self, version: int, effective_ns: int) -> None:
        """version: scenario version announced. effective_ns: labelled UTC ns from
        which the new world applies."""
        ...


@runtime_checkable
class AssignHook(Protocol):
    """Optional driver hook: an `assign` arrived and this channel is unchanged.

    A channel whose `ChannelAssignment` is the same as before keeps its open driver
    (its captures in progress must not fail). The agent awaits `on_assign` on such a
    driver before it sends the `assign_ack`, so that a driver whose behaviour depends
    on server configuration it cannot see in the assignment (the simulation driver)
    can refresh it: "a simulated node also refetches before sending assign_ack".
    An exception here marks the channel as failed in the ack and closes the driver.
    """

    async def on_assign(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        """channel: the (unchanged) assignment. ctx: the session context."""
        ...


# ----------------------------------------------------------------------- worker pool


class WorkerPool:
    """`asyncio.to_thread` behind a semaphore: at most `limit` jobs at a time.

    One pool is shared by a node's monitors and drivers. Jobs queue in arrival order.
    When the awaiting task is cancelled the thread cannot be interrupted: it finishes
    in the background, its result is dropped and its slot is given up at once, so
    during a teardown the limit can be exceeded for the remainder of one job.

    Bind a pool to one event loop (create it inside the loop that uses it).
    """

    def __init__(self, limit: int = DEFAULT_WORKERS) -> None:
        """limit: jobs that may run at once, at least 1."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit: must be an integer of at least 1, got {limit!r}")
        self._limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._active = 0

    @property
    def limit(self) -> int:
        """Jobs that may run at once."""
        return self._limit

    @property
    def active(self) -> int:
        """Jobs awaited right now (running in a thread)."""
        return self._active

    async def run(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run `fn(*args, **kwargs)` in a worker thread and return its result.

        Exceptions of `fn` propagate to the caller.
        """
        async with self._semaphore:
            self._active += 1
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            finally:
                self._active -= 1


# -------------------------------------------------------------- sample index and time


def _positive_rate(fs: float, name: str = "fs") -> Fraction:
    """`fs` as an exact fraction; `ValueError` unless it is finite and positive."""
    if isinstance(fs, bool) or not isinstance(fs, (int, float)):
        raise ValueError(f"{name}: must be a number, got {type(fs).__name__}")
    if not math.isfinite(fs) or fs <= 0:
        raise ValueError(f"{name}: must be finite and positive, got {fs!r}")
    return Fraction(fs)


def _round_half_up(value: Fraction) -> int:
    """Nearest integer to a fraction, halves towards plus infinity."""
    return math.floor(value + Fraction(1, 2))


def duration_ns(n: int, fs: float) -> int:
    """Length of `n` samples at rate `fs` (Hz), integer ns, rounded up."""
    rate = _positive_rate(fs)
    if n < 0:
        raise ValueError(f"n: must not be negative, got {n}")
    return math.ceil(Fraction(n * _NS) / rate)


def sample_index(t_ns: int, t0_ns: int, fs: float) -> int:
    """Index of the sample nearest to labelled time `t_ns`.

    t_ns: the time looked up, UTC ns. t0_ns: labelled time of sample 0, UTC ns.
    fs: sample rate, Hz. The result is negative for times before `t0_ns`. Exact.
    """
    rate = _positive_rate(fs)
    return _round_half_up(Fraction(int(t_ns) - int(t0_ns)) * rate / _NS)


def sample_time_ns(index: int, t0_ns: int, fs: float) -> int:
    """Labelled time of sample `index`, UTC ns rounded to the nearest nanosecond.

    index: sample index (may be negative). t0_ns: labelled time of sample 0, UTC ns.
    fs: sample rate, Hz. Exact rational arithmetic, whatever the size of `index`.
    """
    rate = _positive_rate(fs)
    return int(t0_ns) + _round_half_up(Fraction(int(index) * _NS) / rate)


def mixer_phase_cycles(shift_hz: float, index: int, fs: float) -> float:
    """Phase of a free-running mixer at sample `index`, cycles in [0, 1).

    The mixer started at phase 0 on sample 0 and advances `shift_hz / fs` cycles per
    sample. Computed exactly, so that the phase is right for an index of any size
    (a frequency is never multiplied by a large time in floating point).
    """
    rate = _positive_rate(fs)
    if not math.isfinite(shift_hz):
        raise ValueError(f"shift_hz: must be finite, got {shift_hz!r}")
    return float((Fraction(shift_hz) * int(index) / rate) % 1)


# ---------------------------------------------------------- retuning and resampling

_MAX_RATIO_TERM: Final[int] = 4096
"""Largest numerator or denominator of the resampling ratio before it is approximated."""

_FILTER_HALF_WIDTH: Final[int] = 10
"""Half length of the anti-alias filter of `scipy.signal.resample_poly`, in samples of
the lower of the two rates (its default design: 2 * 10 * max(up, down) + 1 taps)."""

_BAND_TOLERANCE: Final[float] = 1e-9
"""Slack of the band-edge check, as a fraction of the source rate."""

_MIX_BLOCK: Final[int] = 1 << 18
"""Samples mixed per block, to bound the temporary arrays of `retune_resample`."""


@dataclass(frozen=True)
class ResamplePlan:
    """How to turn a stream at (`fs_in`, `fc_in`) into one at (`fs_out`, `fc_out`).

    up, down: the output rate is `fs_in * up / down` (lowest terms).
    shift_hz: `fc_out - fc_in`; the stream is multiplied by exp(-j 2 pi shift t).
    fs_in, fc_in: rate (Hz) and centre (Hz) of the source.
    fs_out: the rate actually produced, Hz: the one asked for whenever the ratio is
        exact, else `fs_in * up / down`.
    fc_out: the centre produced, Hz (always the one asked for).
    margin: source samples of context needed before and after the wanted interval so
        that the filter never sees an edge; a multiple of `down`.
    """

    up: int
    down: int
    shift_hz: float
    fs_in: float
    fc_in: float
    fs_out: float
    fc_out: float
    margin: int

    @property
    def identity(self) -> bool:
        """Whether the source can be passed through untouched."""
        return self.up == self.down and self.shift_hz == 0.0

    def core_length(self, n_out: int) -> int:
        """Source samples spanned by `n_out` output samples (without the margins)."""
        return -(-n_out * self.down // self.up)

    def input_length(self, n_out: int) -> int:
        """Source samples `retune_resample` needs for `n_out` output samples."""
        return self.core_length(n_out) + 2 * self.margin


def plan_resample(fs_in: float, fc_in: float, fs_out: float, fc_out: float) -> ResamplePlan:
    """Plan the conversion of a source at (`fs_in`, `fc_in`) to (`fs_out`, `fc_out`).

    fs_in, fc_in: sample rate and centre of the source, Hz. fs_out, fc_out: wanted.
    Raises `CaptureError("out_of_range")` when the wanted band
    [fc_out - fs_out/2, fc_out + fs_out/2] is not inside the source band, when the
    wanted rate is not finite and positive, or when it is too far below the source
    rate to reach with a ratio of terms up to 4096. Raises `ValueError` for a source
    rate or centre that is not a usable number (a driver bug, not a bad request).

    A ratio that is not exactly `up / down` with small terms is approximated; the plan
    then carries the rate really produced in `fs_out` and the caller must report it.
    """
    rate_in = _positive_rate(fs_in, "fs_in")
    if not math.isfinite(fc_in):
        raise ValueError(f"fc_in: must be finite, got {fc_in!r}")
    if not (
        isinstance(fs_out, (int, float)) and math.isfinite(fs_out) and fs_out > 0
    ) or not (isinstance(fc_out, (int, float)) and math.isfinite(fc_out)):
        raise CaptureError("out_of_range", "sample rate and centre must be finite and positive")
    tolerance = _BAND_TOLERANCE * fs_in
    lo_in, hi_in = fc_in - fs_in / 2, fc_in + fs_in / 2
    lo_out, hi_out = fc_out - fs_out / 2, fc_out + fs_out / 2
    if fs_out > fs_in + tolerance or lo_out < lo_in - tolerance or hi_out > hi_in + tolerance:
        raise CaptureError(
            "out_of_range",
            f"requested band {lo_out:.3f}..{hi_out:.3f} Hz ({fs_out:g} Hz at {fc_out:.3f} Hz) "
            f"is outside the available band {lo_in:.3f}..{hi_in:.3f} Hz",
        )
    exact = Fraction(fs_out) / rate_in
    ratio = min(exact, Fraction(1))
    if ratio.denominator > _MAX_RATIO_TERM:
        ratio = ratio.limit_denominator(_MAX_RATIO_TERM)
        if ratio <= 0:
            raise CaptureError(
                "out_of_range",
                f"sample rate {fs_out:g} Hz is too far below the source rate {fs_in:g} Hz",
            )
    up, down = ratio.numerator, ratio.denominator
    produced = float(rate_in * ratio)
    fs_actual = float(fs_out) if abs(produced - fs_out) <= 1e-12 * fs_out else produced
    shift = float(fc_out) - float(fc_in)
    if up == down:
        margin = 0
    else:
        context = math.ceil(_FILTER_HALF_WIDTH * max(up, down) / up) + 1
        margin = down * math.ceil(context / down)
    return ResamplePlan(
        up=up,
        down=down,
        shift_hz=shift,
        fs_in=float(fs_in),
        fc_in=float(fc_in),
        fs_out=fs_actual,
        fc_out=float(fc_out),
        margin=margin,
    )


def retune_resample(
    x: np.ndarray, plan: ResamplePlan, n_out: int, phase0_cycles: float = 0.0
) -> np.ndarray:
    """Retune and resample a stretch of source samples according to `plan`.

    x: complex source samples, exactly `plan.input_length(n_out)` of them: the
        `plan.margin` samples before the wanted interval, the interval itself
        (`plan.core_length(n_out)` samples, the first of them aligned with output
        sample 0) and `plan.margin` samples after it.
    n_out: output samples wanted.
    phase0_cycles: phase of the mixer on the first sample of the interval, cycles
        (`mixer_phase_cycles(plan.shift_hz, index_of_that_sample, plan.fs_in)` keeps a
        free-running mixer continuous from one call to the next).
    Returns complex64[n_out] at `plan.fs_out` centred on `plan.fc_out`; output sample
    `i` is the source at source index `i * down / up` of the interval, so its first
    sample has the labelled time of the interval's first source sample.

    The mixer multiplies by exp(-j 2 pi (phase0 + shift_hz * k / fs_in)), which moves
    RF frequency `fc_out` to 0 Hz. The resampler is `scipy.signal.resample_poly`
    (zero-phase Kaiser FIR, unit pass-band gain, -6 dB at the new band edge): a tone
    in the inner 80 % of the new band keeps its amplitude to 0.2 %, and white noise of
    variance 1 comes out with variance about `up / down` (4 % less for 1/4: the
    filter's transition band). CPU-bound: call it through a `WorkerPool`.
    """
    if n_out < 1:
        raise ValueError(f"n_out: must be at least 1, got {n_out}")
    samples = np.asarray(x)
    if samples.ndim != 1 or samples.dtype.kind != "c":
        raise ValueError(f"x: must be a one-dimensional complex array, got {samples.dtype} "
                         f"with shape {samples.shape}")
    expected = plan.input_length(n_out)
    if samples.shape[0] != expected:
        raise ValueError(f"x: has {samples.shape[0]} samples, the plan needs {expected}")
    if plan.identity:
        return np.array(samples[:n_out], dtype=np.complex64)
    mixed = np.empty(expected, dtype=np.complex64)
    if plan.shift_hz == 0.0:
        mixed[:] = samples
    else:
        step = plan.shift_hz / plan.fs_in  # cycles per source sample
        for start in range(0, expected, _MIX_BLOCK):
            stop = min(start + _MIX_BLOCK, expected)
            k = np.arange(start - plan.margin, stop - plan.margin, dtype=np.float64)
            phase = (phase0_cycles + step * k) % 1.0
            mixed[start:stop] = samples[start:stop] * np.exp(-2j * np.pi * phase)
    if plan.up == plan.down:
        return mixed[:n_out]
    # A heavy import, made only when something is actually resampled.
    from scipy.signal import resample_poly  # type: ignore[import-untyped]

    resampled = resample_poly(mixed, plan.up, plan.down)
    first = plan.margin * plan.up // plan.down
    out = np.ascontiguousarray(resampled[first : first + n_out], dtype=np.complex64)
    if out.shape[0] != n_out:
        raise ValueError(
            f"resampler returned {out.shape[0]} of {n_out} samples (internal error)"
        )
    return out
