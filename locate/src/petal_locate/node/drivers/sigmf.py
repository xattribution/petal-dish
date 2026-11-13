"""`sigmf`: replays a SigMF recording as if it were a radio, with its timestamps.

A recording is a pair of files: `<name>.sigmf-data` (here: `cf32_le`, interleaved
little-endian float32 I and Q) and `<name>.sigmf-meta` (JSON). Three metadata fields
are honoured: `global.core:sample_rate` (Hz), `captures[0].core:frequency` (centre,
Hz) and `captures[0].core:datetime` (ISO 8601 UTC time of the capture segment's first
sample, read to the nanosecond). `captures[0].core:sample_start` is respected when
the first segment does not start at sample 0. Later capture segments are assumed to
continue the first one without a gap.

Time. Sample `k` of the recording has labelled time `t0 + k / fs_rec`, with `t0`
from `core:datetime` (or the `t0_ns` option). A read for `t_start_ns` gets the sample
nearest to that time as its first sample and reports that sample's own time in
`t_first_ns`. Without `loop`, an interval that is not wholly inside the recording
fails with `too_late`. With `loop=True` the recording repeats for ever in both
directions, so any time maps onto it; the join between the last and the first sample
is a discontinuity the replay does not hide.

Frequency. A request for the recording's own rate and centre returns its samples
bit for bit. Any other request whose band lies inside the recorded band is retuned
(complex mixer) and resampled (`scipy.signal.resample_poly`); one that reaches
outside fails with `out_of_range`.

Like a radio, a read returns only once the node clock has passed the end of the
interval (`realtime=True`, the default).
"""
from __future__ import annotations

import calendar
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from petal_locate.node.clock import ClockSource, SystemClock
from petal_locate.node.drivers.base import (
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    ResamplePlan,
    WorkerPool,
    duration_ns,
    mixer_phase_cycles,
    plan_resample,
    retune_resample,
    sample_index,
    sample_time_ns,
)
from petal_locate.protocol.messages import MAX_CAPTURE_SAMPLES, MAX_TIME_NS, ChannelAssignment

__all__ = [
    "SigMFDriver",
    "SigMFRecording",
    "load_recording",
    "ns_from_sigmf_datetime",
    "recording_slice",
]

logger = logging.getLogger(__name__)

META_SUFFIX: Final[str] = ".sigmf-meta"
DATA_SUFFIX: Final[str] = ".sigmf-data"
_DATATYPE: Final[str] = "cf32_le"
_SAMPLE_BYTES: Final[int] = 8
_NS: Final[int] = 1_000_000_000

_DATETIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?"
    r"(?:[Zz]|([+-])(\d{2}):?(\d{2}))?$"
)


def ns_from_sigmf_datetime(text: str) -> int:
    """UTC nanoseconds since the Unix epoch of a SigMF `core:datetime`.

    text: ISO 8601, for example "2026-03-01T12:00:00.123456789Z". Fractional seconds
    are read to the nanosecond (further digits are dropped); a missing zone means
    UTC, as SigMF requires; a numeric offset is applied.
    Raises `ValueError` for anything else, and for a time before 1970.
    """
    if not isinstance(text, str):
        raise ValueError(f"core:datetime: must be a string, got {type(text).__name__}")
    match = _DATETIME.match(text.strip())
    if match is None:
        raise ValueError(f"core:datetime: {text!r} is not an ISO 8601 date and time")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if not (1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]):
        raise ValueError(f"core:datetime: {text!r} is not a calendar date")
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"core:datetime: {text!r} is not a time of day")
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    if match.group(8) is not None:
        offset_s = int(match.group(9)) * 3600 + int(match.group(10)) * 60
        seconds -= offset_s if match.group(8) == "+" else -offset_s
    fraction = (match.group(7) or "")[:9].ljust(9, "0")
    t_ns = seconds * _NS + int(fraction)
    if not 0 <= t_ns <= MAX_TIME_NS:
        raise ValueError(f"core:datetime: {text!r} is outside the supported range")
    return t_ns


@dataclass(frozen=True)
class SigMFRecording:
    """An opened recording.

    samples: complex64[n], a read-only memory map of the data file.
    sample_rate_hz: `global.core:sample_rate`, Hz.
    center_hz: `captures[0].core:frequency`, Hz.
    t0_ns: labelled time of sample 0, UTC ns.
    meta_path, data_path: the two files.
    """

    samples: np.ndarray
    sample_rate_hz: float
    center_hz: float
    t0_ns: int
    meta_path: Path
    data_path: Path

    @property
    def n_samples(self) -> int:
        """Samples in the recording."""
        return int(self.samples.shape[0])

    @property
    def duration_s(self) -> float:
        """Length of the recording, seconds."""
        return self.n_samples / self.sample_rate_hz


def _number(obj: dict[str, Any], key: str, where: str, *, positive: bool) -> float:
    """A finite JSON number field; `ValueError` naming it otherwise."""
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}.{key}: missing or not a number")
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{where}.{key}: must be finite and "
                         f"{'positive' if positive else 'not negative'}, got {value!r}")
    return float(value)


def load_recording(path: str | Path, t0_ns: int | None = None) -> SigMFRecording:
    """Open a SigMF recording (blocking file I/O; call it in a worker thread).

    path: the `.sigmf-meta` file, the `.sigmf-data` file, or their common name
        without a suffix.
    t0_ns: labelled time of sample 0, UTC ns, overriding `core:datetime` (required
        when the metadata has none).
    Raises `ValueError` naming the file and field for missing files, metadata that is
    not valid, a datatype other than `cf32_le`, or an empty or truncated data file.
    """
    given = Path(path)
    if given.name.endswith(META_SUFFIX) or given.name.endswith(DATA_SUFFIX):
        base = given.with_name(given.name.rsplit(".", 1)[0])
    else:
        base = given
    meta_path = base.with_name(base.name + META_SUFFIX)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{meta_path}: cannot be read ({exc.strerror or exc})") from exc
    except ValueError as exc:
        raise ValueError(f"{meta_path}: not valid JSON ({exc})") from exc
    if not isinstance(meta, dict) or not isinstance(meta.get("global"), dict):
        raise ValueError(f"{meta_path}: no 'global' object")
    info: dict[str, Any] = meta["global"]
    datatype = info.get("core:datatype")
    if datatype != _DATATYPE:
        raise ValueError(
            f"{meta_path}: global.core:datatype is {datatype!r}; only {_DATATYPE!r} is supported"
        )
    captures = meta.get("captures")
    if not isinstance(captures, list) or not captures or not isinstance(captures[0], dict):
        raise ValueError(f"{meta_path}: 'captures' must hold at least one segment")
    first: dict[str, Any] = captures[0]
    try:
        sample_rate = _number(info, "core:sample_rate", "global", positive=True)
        center = _number(first, "core:frequency", "captures[0]", positive=False)
        sample_start = first.get("core:sample_start", 0)
        if isinstance(sample_start, bool) or not isinstance(sample_start, int) or sample_start < 0:
            raise ValueError("captures[0].core:sample_start: must be a non-negative integer")
        if first.get("core:header_bytes", 0) != 0:
            raise ValueError("captures[0].core:header_bytes: headers inside the data file "
                             "are not supported")
        if t0_ns is None:
            if "core:datetime" not in first:
                raise ValueError(
                    "captures[0].core:datetime: missing; give the driver option t0_ns instead"
                )
            start_ns = ns_from_sigmf_datetime(first["core:datetime"])
            t0 = sample_time_ns(-sample_start, start_ns, sample_rate)
        else:
            if isinstance(t0_ns, bool) or not isinstance(t0_ns, int):
                raise ValueError(f"t0_ns: must be an integer, got {type(t0_ns).__name__}")
            t0 = t0_ns
        if not 0 <= t0 <= MAX_TIME_NS:
            raise ValueError("t0_ns: sample 0 falls outside the supported time range")
    except ValueError as exc:
        raise ValueError(f"{meta_path}: {exc}") from exc
    for index, segment in enumerate(captures[1:], start=1):
        if isinstance(segment, dict) and segment.get("core:frequency", center) != center:
            logger.warning(
                "sigmf capture segment retunes; the replay keeps the first centre",
                extra={"meta_path": str(meta_path), "segment": index},
            )
            break
    dataset = info.get("core:dataset")
    if dataset is not None:
        if not isinstance(dataset, str) or not dataset or Path(dataset).name != dataset:
            raise ValueError(f"{meta_path}: global.core:dataset must be a bare file name")
        data_path = meta_path.with_name(dataset)
    else:
        data_path = base.with_name(base.name + DATA_SUFFIX)
    try:
        size = data_path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{data_path}: cannot be read ({exc.strerror or exc})") from exc
    if size < _SAMPLE_BYTES or size % _SAMPLE_BYTES:
        raise ValueError(
            f"{data_path}: {size} bytes is not a whole, positive number of cf32_le samples"
        )
    try:
        samples = np.memmap(data_path, dtype=np.dtype("<c8"), mode="r")
    except (OSError, ValueError) as exc:
        raise ValueError(f"{data_path}: cannot be mapped ({exc})") from exc
    return SigMFRecording(
        samples=samples,
        sample_rate_hz=sample_rate,
        center_hz=center,
        t0_ns=t0,
        meta_path=meta_path,
        data_path=data_path,
    )


def recording_slice(samples: np.ndarray, start: int, count: int, loop: bool) -> np.ndarray:
    """`count` samples of a recording from index `start`, as a new complex64 array.

    samples: the recording, complex64[n]. start: index of the first sample wanted;
    may be negative or beyond the end. With `loop` the recording repeats in both
    directions; without it, indices outside the recording read as zero.
    """
    total = int(samples.shape[0])
    out = np.zeros(count, dtype=np.complex64)
    if loop:
        filled, position = 0, start % total
        while filled < count:
            take = min(total - position, count - filled)
            out[filled : filled + take] = samples[position : position + take]
            filled += take
            position = 0
        return out
    lo, hi = max(start, 0), min(start + count, total)
    if hi > lo:
        out[lo - start : hi - start] = samples[lo:hi]
    return out


def _extract(
    recording: SigMFRecording, plan: ResamplePlan, first_index: int, n: int, loop: bool
) -> np.ndarray:
    """Samples for one read: slice, retune and resample (runs in a worker thread)."""
    if plan.identity:
        return recording_slice(recording.samples, first_index, n, loop)
    source = recording_slice(
        recording.samples, first_index - plan.margin, plan.input_length(n), loop
    )
    phase0 = mixer_phase_cycles(plan.shift_hz, first_index, recording.sample_rate_hz)
    return retune_resample(source, plan, n, phase0)


class SigMFDriver:
    """Replays one SigMF recording (see the module docstring).

    Options (keyword-only):
      path: the recording: its `.sigmf-meta`, its `.sigmf-data` or their common name.
      loop: repeat the recording for ever (default False).
      t0_ns: labelled time of sample 0, UTC ns, instead of `core:datetime`.
      realtime: return a read only when the node clock has passed the end of its
        interval (default True).
      clock: the clock to wait on; None takes the node's clock from the context, or
        the system clock.
      clock_locked: what `Capture.clock_locked` reports: whether the recording's
        timestamps came from a disciplined clock (default True).
      concurrent_reads: reads served at once (default 4).
    """

    def __init__(
        self,
        *,
        path: str | Path,
        loop: bool = False,
        t0_ns: int | None = None,
        realtime: bool = True,
        clock: ClockSource | None = None,
        clock_locked: bool = True,
        concurrent_reads: int = 4,
    ) -> None:
        if not isinstance(path, (str, Path)) or not str(path):
            raise ValueError("path: must name a SigMF recording")
        for name, value in (("loop", loop), ("realtime", realtime), ("clock_locked", clock_locked)):
            if not isinstance(value, bool):
                raise ValueError(f"{name}: must be a bool, got {type(value).__name__}")
        if isinstance(concurrent_reads, bool) or not isinstance(concurrent_reads, int) \
                or concurrent_reads < 1:
            raise ValueError(f"concurrent_reads: must be an integer of at least 1, "
                             f"got {concurrent_reads!r}")
        self._path = Path(path)
        self._loop = loop
        self._t0_ns = t0_ns
        self._realtime = realtime
        self._clock = clock
        self._clock_locked = clock_locked
        self._concurrent_reads = concurrent_reads
        self._workers: WorkerPool | None = None
        self._recording: SigMFRecording | None = None
        self._capabilities: Capabilities | None = None

    @property
    def capabilities(self) -> Capabilities:
        """Limits of the opened recording: its band, its rate, its length as lookback.

        Raises `CaptureError("driver")` before `open`, because they come from the file.
        """
        if self._capabilities is None:
            raise CaptureError("driver", "sigmf driver is not open")
        return self._capabilities

    @property
    def recording(self) -> SigMFRecording | None:
        """The opened recording, or None."""
        return self._recording

    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        """Read the metadata and map the data file.

        Raises `CaptureError("driver")` naming the file and field when the recording
        cannot be used.
        """
        if self._clock is None:
            self._clock = ctx.clock if ctx.clock is not None else SystemClock()
        self._workers = ctx.workers if ctx.workers is not None else WorkerPool()
        try:
            recording = await self._workers.run(load_recording, self._path, self._t0_ns)
        except ValueError as exc:
            raise CaptureError("driver", str(exc)) from exc
        half = recording.sample_rate_hz / 2
        self._capabilities = Capabilities(
            min_hz=max(0.0, recording.center_hz - half),
            max_hz=recording.center_hz + half,
            max_sample_rate_hz=recording.sample_rate_hz,
            lookback_s=recording.duration_s,
            concurrent_reads=self._concurrent_reads,
        )
        self._recording = recording
        logger.info(
            "sigmf recording open",
            extra={
                "channel_id": channel.channel_id,
                "meta_path": str(recording.meta_path),
                "sample_rate_hz": recording.sample_rate_hz,
                "center_hz": recording.center_hz,
                "t0_ns": recording.t0_ns,
                "n_samples": recording.n_samples,
                "loop": self._loop,
            },
        )

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        """`n` samples of the recording from the sample nearest `t_start_ns`.

        t_start_ns: labelled time wanted for the first sample, UTC ns. n: samples.
        fs, fc: rate and centre wanted, Hz.
        Returns a `Capture` whose `t_first_ns` is the recording's own time of the
        first sample used, `fc` the centre asked for and `fs` the rate produced (the
        one asked for whenever it is a ratio of small integers to the recording's).
        Raises `CaptureError`: "driver" when not open, "out_of_range" when the band
        asked for is not inside the recorded band, "too_late" when the interval is
        not inside the recording (never with `loop`).
        """
        recording, clock, workers = self._recording, self._clock, self._workers
        if recording is None or clock is None or workers is None:
            raise CaptureError("driver", "sigmf driver is not open")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_CAPTURE_SAMPLES:
            raise CaptureError(
                "out_of_range", f"n must be 1..{MAX_CAPTURE_SAMPLES} samples, got {n!r}"
            )
        plan = plan_resample(recording.sample_rate_hz, recording.center_hz, fs, fc)
        first_index = sample_index(t_start_ns, recording.t0_ns, recording.sample_rate_hz)
        span = plan.core_length(n)
        if not self._loop and (first_index < 0 or first_index + span > recording.n_samples):
            where = "before its start" if first_index < 0 else "past its end"
            raise CaptureError(
                "too_late",
                f"interval is not inside the recording ({where}): it holds "
                f"{recording.duration_s:.6f} s from t0_ns {recording.t0_ns}",
            )
        if self._realtime:
            await clock.sleep_until(t_start_ns + duration_ns(n, plan.fs_out))
            if self._recording is not recording:
                raise CaptureError("driver", "sigmf driver was closed during the read")
        try:
            iq = await workers.run(_extract, recording, plan, first_index, n, self._loop)
        except (OSError, ValueError) as exc:
            raise CaptureError("driver", f"{recording.data_path}: read failed ({exc})") from exc
        return Capture(
            iq=iq,
            t_first_ns=sample_time_ns(first_index, recording.t0_ns, recording.sample_rate_hz),
            fs=plan.fs_out,
            fc=plan.fc_out,
            overflow=False,
            clock_locked=self._clock_locked,
        )

    async def psd(
        self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int
    ) -> np.ndarray | None:
        """No fast path: the monitor reads IQ and computes the spectrum."""
        return None

    async def close(self) -> None:
        """Let go of the recording. Idempotent."""
        if self._recording is not None:
            logger.info(
                "sigmf recording closed", extra={"meta_path": str(self._recording.meta_path)}
            )
        self._recording = None
        self._capabilities = None
