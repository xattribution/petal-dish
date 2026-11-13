"""`noise`: a radio that hears nothing but unit-variance thermal noise.

For protocol tests and for bringing a node up without hardware. It serves any centre
and rate inside its advertised limits, behaves like a real receiver in time (a read
returns only once the wall clock has passed the end of the interval, and the recent
past is available up to `lookback_s`), and is deterministic: the samples depend only
on the seed, the channel id and `t_start_ns`, so the same request always returns the
same samples and a shorter read is a prefix of a longer one.

`psd()` returns None on purpose: the monitor then exercises the IQ path
(`read` followed by `welch_psd_db`), where this noise reads 0 dB in every bin.
"""
from __future__ import annotations

import hashlib
import logging
import math

import numpy as np

from petal_locate.node.clock import ClockSource, SystemClock
from petal_locate.node.drivers.base import (
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    WorkerPool,
    duration_ns,
)
from petal_locate.protocol.messages import MAX_CAPTURE_SAMPLES, ChannelAssignment

__all__ = ["NoiseDriver", "noise_samples"]

logger = logging.getLogger(__name__)

_NS = 1_000_000_000


def noise_samples(seed: int, channel_id: str, t_start_ns: int, n: int) -> np.ndarray:
    """Complex white Gaussian noise keyed by (seed, channel_id, t_start_ns).

    seed: integer that separates independent noise worlds.
    channel_id: the receive channel. t_start_ns: labelled time of the first sample,
    UTC ns. n: number of samples.
    Returns complex64[n] with E|x|^2 = 1 (real and imaginary parts independent, each
    of variance 1/2). The first `m` samples of a call with `n >= m` equal the result
    of a call with `n = m`. CPU-bound (about 15 ms per million samples).
    """
    if n < 0:
        raise ValueError(f"n: must not be negative, got {n}")
    digest = hashlib.blake2b(
        f"{seed}\x1f{channel_id}\x1f{t_start_ns}".encode(), digest_size=16
    ).digest()
    rng = np.random.Generator(np.random.PCG64(int.from_bytes(digest, "little")))
    parts = rng.standard_normal(2 * n, dtype=np.float32)
    parts *= np.float32(math.sqrt(0.5))
    return parts.view(np.complex64)


class NoiseDriver:
    """Unit-variance complex noise at any rate and centre (see the module docstring).

    Options (all keyword-only):
      max_sample_rate_hz: highest rate served, Hz (default 4e6).
      min_hz, max_hz: range of centre frequencies served, Hz (default 0 to 100 GHz).
      lookback_s: how far in the past a read may start, seconds (default 30).
      concurrent_reads: reads served at once (default 4).
      seed: separates noise worlds (default 0).
      clock: the clock to wait on; None takes the node's clock from the context given
        to `open`, or the system clock.
      clock_locked: what `Capture.clock_locked` reports; None follows the clock's own
        status.
    """

    def __init__(
        self,
        *,
        max_sample_rate_hz: float = 4e6,
        min_hz: float = 0.0,
        max_hz: float = 100e9,
        lookback_s: float = 30.0,
        concurrent_reads: int = 4,
        seed: int = 0,
        clock: ClockSource | None = None,
        clock_locked: bool | None = None,
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError(f"seed: must be an integer, got {type(seed).__name__}")
        self.capabilities = Capabilities(
            min_hz=min_hz,
            max_hz=max_hz,
            max_sample_rate_hz=max_sample_rate_hz,
            lookback_s=lookback_s,
            concurrent_reads=concurrent_reads,
        )
        self._seed = seed
        self._clock = clock
        self._clock_locked = clock_locked
        self._workers: WorkerPool | None = None
        self._channel_id: str | None = None
        self._open = False

    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        """Bind to `channel`; takes the clock and worker pool of `ctx` when it has them."""
        if self._clock is None:
            self._clock = ctx.clock if ctx.clock is not None else SystemClock()
        self._workers = ctx.workers if ctx.workers is not None else WorkerPool()
        self._channel_id = channel.channel_id
        self._open = True
        logger.info(
            "noise driver open",
            extra={"channel_id": channel.channel_id, "site_id": ctx.site_id},
        )

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        """`n` noise samples labelled from `t_start_ns` at exactly the rate and centre
        asked for.

        t_start_ns: labelled time of the first sample, UTC ns. n: samples, 1 to the
        protocol's capture limit. fs: rate, Hz. fc: centre, Hz.
        Returns when the clock has passed `t_start_ns + n / fs`; `t_first_ns` equals
        `t_start_ns`.
        Raises `CaptureError`: "driver" when not open, "out_of_range" for a rate,
        centre or length outside the limits, "too_late" for an interval that starts
        more than `lookback_s` in the past.
        """
        clock, workers, channel_id = self._clock, self._workers, self._channel_id
        if not self._open or clock is None or workers is None or channel_id is None:
            raise CaptureError("driver", "noise driver is not open")
        caps = self.capabilities
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_CAPTURE_SAMPLES:
            raise CaptureError(
                "out_of_range", f"n must be 1..{MAX_CAPTURE_SAMPLES} samples, got {n!r}"
            )
        if not (math.isfinite(fs) and 0 < fs <= caps.max_sample_rate_hz):
            raise CaptureError(
                "out_of_range",
                f"sample rate {fs!r} Hz is outside 0..{caps.max_sample_rate_hz:g} Hz",
            )
        if not (math.isfinite(fc) and caps.min_hz <= fc <= caps.max_hz):
            raise CaptureError(
                "out_of_range",
                f"centre {fc!r} Hz is outside {caps.min_hz:g}..{caps.max_hz:g} Hz",
            )
        now_ns = clock.now_ns()
        oldest_ns = now_ns - round(caps.lookback_s * _NS)
        if t_start_ns < oldest_ns:
            raise CaptureError(
                "too_late",
                f"interval starts {(now_ns - t_start_ns) / _NS:.3f} s in the past, "
                f"lookback is {caps.lookback_s:g} s",
            )
        await clock.sleep_until(t_start_ns + duration_ns(n, fs))
        if not self._open:
            raise CaptureError("driver", "noise driver was closed during the read")
        iq = await workers.run(noise_samples, self._seed, channel_id, t_start_ns, n)
        locked = clock.status().locked if self._clock_locked is None else self._clock_locked
        return Capture(
            iq=iq,
            t_first_ns=t_start_ns,
            fs=float(fs),
            fc=float(fc),
            overflow=False,
            clock_locked=locked,
        )

    async def psd(
        self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int
    ) -> np.ndarray | None:
        """No fast path: always None, so that the monitor reads IQ."""
        return None

    async def close(self) -> None:
        """Stop serving. Idempotent; reads still waiting fail with "driver"."""
        if self._open:
            logger.info("noise driver closed", extra={"channel_id": self._channel_id})
        self._open = False
