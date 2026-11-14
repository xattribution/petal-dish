"""Binary frames of PROTOCOL.md: the PLB1 container, spectrum frames, capture chunks.

```
bytes 0..3    magic  b"PLB1"
bytes 4..7    header length H, uint32 little-endian
bytes 8..8+H  header, UTF-8 JSON object with a "type" field
remainder     payload, little-endian
```

Everything on the wire is little-endian whatever the host: lengths go through
`struct` with an explicit `<`, sample data through the numpy dtypes `<f4` and `<c8`.

Copies. Encoding copies the payload once, into the frame. Decoding copies nothing:
`decode_frame` returns a view of the frame, and `decode_psd`, `decode_capture_data`
and `decode_console_psd` return arrays that **alias the frame they were given** (and
are read-only when the frame is `bytes`). Keep the frame alive and unchanged for as
long as such an array is in use, or copy the array. `CaptureAssembler` is the
exception: it copies every chunk into storage of its own, so its result aliases
nothing.

Frames built here pad the header JSON with trailing spaces until the payload starts
on an 8-byte boundary. That is still a valid JSON object and receivers need not know
about it, but it lets a receiver lay a typed array straight over the payload (numpy
gets an aligned array; a browser `Float32Array` view requires a 4-byte offset).

Cost. Every call here is far below the 50 ms event-loop budget and none needs a
worker thread: a chunk or spectrum frame costs tens of microseconds, and the one
whole-capture pass (`iter_capture_chunks` checking its input for NaN) takes about one
millisecond per million samples, 10 ms at the 64 MiB limit.

Errors. Data that arrived from a peer and breaks the protocol raises `ProtocolError`.
Arguments of the encoders that are wrong raise `ValueError` naming the argument
(a pydantic `ValidationError`, which is a `ValueError`, for header fields).
"""
from __future__ import annotations

import json
import logging
import struct
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, TypeAlias, Union

import numpy as np

from petal_locate.protocol.messages import (
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_SAMPLES,
    CaptureDataHeader,
    ClockStatus,
    ConsoleDetection,
    ConsolePsdHeader,
    ProtocolError,
    PsdHeader,
    WireModel,
    dump_json,
    loads_json_object,
    message_type,
)

__all__ = [
    "FRAME_PREFIX_BYTES",
    "MAGIC",
    "MAX_CHUNK_PAYLOAD_BYTES",
    "MAX_HEADER_BYTES",
    "PAYLOAD_ALIGNMENT",
    "AssembledCapture",
    "BytesLike",
    "CaptureAssembler",
    "decode_capture_data",
    "decode_console_psd",
    "decode_frame",
    "decode_psd",
    "encode_console_psd",
    "encode_frame",
    "encode_psd",
    "iter_capture_chunks",
]

logger = logging.getLogger(__name__)

MAGIC: Final[bytes] = b"PLB1"
"""First four bytes of every binary frame."""

FRAME_PREFIX_BYTES: Final[int] = 8
"""Bytes before the header: the magic and the header length."""

MAX_HEADER_BYTES: Final[int] = 64 * 1024
"""Largest header a receiver accepts, bytes (64 KiB)."""

MAX_CHUNK_PAYLOAD_BYTES: Final[int] = 256 * 1024
"""Largest payload of one capture chunk, bytes (256 KiB = 32 768 complex samples)."""

PAYLOAD_ALIGNMENT: Final[int] = 8
"""Frames built here start their payload on a multiple of this many bytes."""

BytesLike: TypeAlias = Union[bytes, bytearray, memoryview]
"""What frames and payloads may be passed as."""

_F32_LE: Final[np.dtype[Any]] = np.dtype("<f4")
_C64_LE: Final[np.dtype[Any]] = np.dtype("<c8")
_SAMPLE_BYTES: Final[int] = 8
_BIN_BYTES: Final[int] = 4
_FINITE_BLOCK: Final[int] = 1 << 18
_LENGTH: Final[struct.Struct] = struct.Struct("<I")


# ---------------------------------------------------------------------------- helpers


def _byte_view(data: Any, name: str, error: type[ValueError]) -> memoryview:
    """A flat unsigned-byte view of a bytes-like object, without copying.

    Raises `error` when `data` is text, not bytes-like, or not C-contiguous.
    """
    if isinstance(data, str):
        raise error(f"{name}: expected bytes, got text")
    try:
        view = memoryview(data)
    except TypeError:
        raise error(f"{name}: must be bytes-like, got {type(data).__name__}") from None
    if not view.c_contiguous:
        raise error(f"{name}: must be C-contiguous")
    if view.ndim == 1 and view.format == "B":
        return view
    try:
        return view.cast("B")  # any C-contiguous buffer casts to bytes, whatever its format
    except TypeError:  # the one exception: a multi-dimensional view with a zero in its shape
        raise error(f"{name}: cannot be viewed as bytes (shape {view.shape})") from None


def _header_bytes(header: Mapping[str, Any] | WireModel) -> bytes:
    """Header as compact UTF-8 JSON, space-padded so that the payload is aligned.

    Raises `ValueError` when the header has no usable `type`, cannot be written as
    JSON, or is longer than `MAX_HEADER_BYTES`.
    """
    if isinstance(header, WireModel):
        if not isinstance(getattr(header, "type", None), str):
            raise ValueError(f"header: {type(header).__name__} has no 'type' field")
        text = dump_json(header)
    elif isinstance(header, Mapping):
        mtype = header.get("type")
        if not isinstance(mtype, str) or not mtype:
            raise ValueError("header: 'type' must be a non-empty string")
        try:
            text = json.dumps(
                dict(header), separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError(f"header: cannot be written as JSON ({exc})") from exc
    else:
        raise ValueError(
            f"header: must be a mapping or a wire model, got {type(header).__name__}"
        )
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError as exc:  # a lone surrogate in a string of a mapping header
        raise ValueError(f"header: cannot be written as UTF-8 ({exc.reason})") from exc
    raw += b" " * (-(FRAME_PREFIX_BYTES + len(raw)) % PAYLOAD_ALIGNMENT)
    if len(raw) > MAX_HEADER_BYTES:
        raise ValueError(f"header: {len(raw)} bytes exceeds the limit of {MAX_HEADER_BYTES}")
    return raw


def _frame(header: bytes, parts: Iterable[Any]) -> bytes:
    """Magic, header length, header and payload parts joined with one copy of each."""
    return b"".join((MAGIC, _LENGTH.pack(len(header)), header, *parts))


def _check_array(values: Any, name: str, dtype: np.dtype[Any], what: str) -> None:
    """Raise `ValueError` unless `values` is a one-dimensional array of the wire type.

    Either byte order is accepted; anything that would need a lossy or widening
    conversion (float64, complex128, integers) is not.
    """
    if not isinstance(values, np.ndarray):
        raise ValueError(f"{name}: must be a numpy array of {what}, got {type(values).__name__}")
    if values.dtype.kind != dtype.kind or values.dtype.itemsize != dtype.itemsize:
        raise ValueError(f"{name}: must be {what}, got {values.dtype}")
    if values.ndim != 1:
        raise ValueError(f"{name}: must be one-dimensional, got shape {values.shape}")


def _all_finite(values: np.ndarray) -> bool:
    """Whether a float32 or complex64 array holds no NaN and no infinity.

    Works in blocks so that a 64 MiB capture needs no 16 MiB temporary.
    """
    flat = values.view(np.dtype(values.dtype.byteorder + "f4"))
    for start in range(0, flat.shape[0], _FINITE_BLOCK):
        if not bool(np.isfinite(flat[start : start + _FINITE_BLOCK]).all()):
            return False
    return True


def _array_from_payload(view: memoryview, dtype: np.dtype[Any]) -> np.ndarray:
    """Little-endian payload as a native array; zero-copy on little-endian hosts."""
    values = np.frombuffer(view, dtype=dtype)
    if not values.dtype.isnative:  # big-endian host: one byte-swapping copy
        values = values.astype(values.dtype.newbyteorder("="))
    return values


def _expect_payload(view: memoryview, expected: int, what: str, shape: str) -> None:
    """Raise `ProtocolError` unless the payload has exactly `expected` bytes."""
    if view.nbytes != expected:
        raise ProtocolError(
            f"{what}: payload is {view.nbytes} bytes, the header ({shape}) requires {expected}",
            message_type=what,
        )


# ------------------------------------------------------------------------ the container


def encode_frame(header: Mapping[str, Any] | WireModel, payload: BytesLike = b"") -> bytes:
    """Build one PLB1 binary frame.

    header: a wire model with a `type` field, or a JSON-ready mapping with a non-empty
        string `type`. Written as compact UTF-8 JSON (integers as integers), padded
        with trailing spaces so that the payload starts on an 8-byte boundary.
    payload: the bytes after the header, copied once into the frame; must already be
        little-endian. Its length is not recorded: it is the remainder of the frame.
    Returns the frame as `bytes`.
    Raises `ValueError` for a header without a usable `type`, one that is not JSON or
    exceeds 64 KiB, and for a payload that is not C-contiguous bytes.
    """
    return _frame(_header_bytes(header), (_byte_view(payload, "payload", ValueError),))


def decode_frame(data: BytesLike) -> tuple[dict[str, Any], memoryview]:
    """Split one PLB1 binary frame into its header and payload.

    data: the whole frame as received.
    Returns `(header, payload)`: the header as a dict whose `type` is a non-empty
    string, and the payload as a flat byte `memoryview` that **aliases `data`** (no
    copy; read-only when `data` is `bytes`).
    Raises `ProtocolError` when the frame is shorter than its 8-byte prefix, the magic
    is not `b"PLB1"`, the declared header length exceeds 64 KiB or the bytes present,
    or the header is not UTF-8, not JSON, not a JSON object, or lacks a string `type`.
    Whether the payload length fits the header depends on the frame type and is
    checked by `decode_psd`, `decode_capture_data` and `decode_console_psd`.
    """
    view = _byte_view(data, "binary frame", ProtocolError)
    total = view.nbytes
    if total < FRAME_PREFIX_BYTES:
        raise ProtocolError(
            f"binary frame: truncated, {total} bytes is shorter than the "
            f"{FRAME_PREFIX_BYTES}-byte prefix"
        )
    if view[:4] != MAGIC:
        raise ProtocolError(f"binary frame: bad magic 0x{bytes(view[:4]).hex()}, expected PLB1")
    (header_len,) = _LENGTH.unpack_from(view, 4)
    if header_len > MAX_HEADER_BYTES:
        raise ProtocolError(
            f"binary frame: header of {header_len} bytes exceeds the limit of {MAX_HEADER_BYTES}"
        )
    header_end = FRAME_PREFIX_BYTES + header_len
    if header_end > total:
        raise ProtocolError(
            f"binary frame: truncated header, {header_len} bytes declared but "
            f"{total - FRAME_PREFIX_BYTES} present"
        )
    try:
        text = str(view[FRAME_PREFIX_BYTES:header_end], "utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("binary frame header: not valid UTF-8") from None
    header = loads_json_object(text, what="binary frame header")
    message_type(header, what="binary frame header")
    return header, view[header_end:]


# ------------------------------------------------------------------- spectrum frames


def encode_psd(
    channel_id: str,
    monitor_id: str,
    t_ns: int,
    center_hz: float,
    span_hz: float,
    nfft: int,
    navg: int,
    seq: int,
    psd_db: np.ndarray,
) -> bytes:
    """Build a node -> server spectrum frame (`type: "psd"`).

    channel_id, monitor_id: the window the frame belongs to.
    t_ns: labelled time of the first sample of the snippet, integer UTC ns.
    center_hz, span_hz: centre and width of the window, Hz.
    nfft: number of bins; `psd_db` must hold exactly this many.
    navg: Welch segments averaged.
    seq: frame counter of the window.
    psd_db: float32[nfft], dB, fft-shifted (bin k at center_hz + (k - nfft/2) *
        span_hz / nfft), finite. Either byte order; written little-endian.
    Returns the frame. Raises `ValueError` naming the argument at fault.
    """
    header = PsdHeader(
        channel_id=channel_id,
        monitor_id=monitor_id,
        t_ns=t_ns,
        center_hz=center_hz,
        span_hz=span_hz,
        nfft=nfft,
        navg=navg,
        seq=seq,
    )
    return _frame(_header_bytes(header), (_bins(psd_db, "psd_db", header.nfft),))


def decode_psd(
    header: Mapping[str, Any] | PsdHeader, payload: BytesLike
) -> tuple[PsdHeader, np.ndarray]:
    """Validate a node -> server spectrum frame split by `decode_frame`.

    header: the header dict (or an already validated `PsdHeader`).
    payload: the frame's payload.
    Returns `(header, psd_db)` with `psd_db` float32[nfft] in dB. The array **aliases
    `payload`** (no copy on little-endian hosts) and is read-only when the frame was
    `bytes`.
    Raises `ProtocolError` for a bad header, a payload that is not exactly 4 * nfft
    bytes, or NaN or infinite values.
    """
    hdr = header if isinstance(header, PsdHeader) else PsdHeader.from_wire(header)
    view = _byte_view(payload, "psd payload", ProtocolError)
    _expect_payload(view, hdr.nfft * _BIN_BYTES, "psd", f"nfft {hdr.nfft}")
    psd = _array_from_payload(view, _F32_LE)
    if not _all_finite(psd):
        raise ProtocolError("psd: payload holds NaN or infinite values", message_type="psd")
    return hdr, psd


def _bins(values: Any, name: str, nfft: int) -> np.ndarray:
    """A finite float32[nfft] argument as the little-endian bytes of its payload.

    Returns a uint8 view: of the caller's array when that is already contiguous and
    little-endian, of a converted copy otherwise.
    """
    _check_array(values, name, _F32_LE, "float32")
    if values.shape[0] != nfft:
        raise ValueError(f"{name}: has {values.shape[0]} bins but nfft is {nfft}")
    out = np.ascontiguousarray(values, dtype=_F32_LE)
    if not _all_finite(out):
        raise ValueError(f"{name}: holds NaN or infinite values")
    return out.view(np.uint8)


def encode_console_psd(
    channel_id: str,
    monitor_id: str,
    t_ns: int,
    center_hz: float,
    span_hz: float,
    nfft: int,
    seq: int,
    psd_db: np.ndarray,
    baseline_db: np.ndarray | None = None,
    detections: Iterable[ConsoleDetection | Mapping[str, Any] | Any] = (),
) -> bytes:
    """Build a server -> console spectrum frame (PROTOCOL.md section B).

    channel_id, monitor_id: the window the frame belongs to.
    t_ns: labelled time of the first sample, integer UTC ns; written into the header
        as a decimal string because JavaScript numbers cannot hold it.
    center_hz, span_hz: centre and width of the window, Hz.
    nfft: number of bins.
    seq: frame counter of the window.
    psd_db: float32[nfft], dB, finite.
    baseline_db: the learned baseline, float32[nfft], dB, finite, appended after the
        PSD; None sets `has_baseline` false and sends the PSD alone.
    detections: confirmed detections of the window, each a `ConsoleDetection`, a
        mapping with its fields, or any object with the attributes `track_id`,
        `f_lo_hz`, `f_hi_hz`, `authorized_carrier_id`, `anomaly` (such as
        `core.detect.Detection`).
    Returns the frame. Raises `ValueError` naming the argument at fault.
    """
    header = ConsolePsdHeader(
        channel_id=channel_id,
        monitor_id=monitor_id,
        t_ns=t_ns,
        center_hz=center_hz,
        span_hz=span_hz,
        nfft=nfft,
        seq=seq,
        has_baseline=baseline_db is not None,
        detections=[_console_detection(item) for item in detections],
    )
    parts = [_bins(psd_db, "psd_db", header.nfft)]
    if baseline_db is not None:
        parts.append(_bins(baseline_db, "baseline_db", header.nfft))
    return _frame(_header_bytes(header), parts)


def _console_detection(item: Any) -> ConsoleDetection:
    """One detection argument of `encode_console_psd` as a `ConsoleDetection`."""
    if isinstance(item, ConsoleDetection):
        return item
    if isinstance(item, Mapping):
        return ConsoleDetection(**item)
    return ConsoleDetection.from_detection(item)


def decode_console_psd(
    header: Mapping[str, Any] | ConsolePsdHeader, payload: BytesLike
) -> tuple[ConsolePsdHeader, np.ndarray, np.ndarray | None]:
    """Validate a server -> console spectrum frame split by `decode_frame`.

    Returns `(header, psd_db, baseline_db)`: float32[nfft] in dB each, `baseline_db`
    None unless the header says `has_baseline`. Both arrays **alias `payload`**.
    Raises `ProtocolError` for a bad header (`t_ns` must be a decimal string), a
    payload that is not 4 * nfft bytes (8 * nfft with a baseline), or NaN or infinite
    values.
    """
    hdr = header if isinstance(header, ConsolePsdHeader) else ConsolePsdHeader.from_wire(header)
    view = _byte_view(payload, "psd payload", ProtocolError)
    one = hdr.nfft * _BIN_BYTES
    shape = f"nfft {hdr.nfft}, {'with' if hdr.has_baseline else 'no'} baseline"
    _expect_payload(view, one * (2 if hdr.has_baseline else 1), "psd", shape)
    values = _array_from_payload(view, _F32_LE)
    if not _all_finite(values):
        raise ProtocolError("psd: payload holds NaN or infinite values", message_type="psd")
    if hdr.has_baseline:
        return hdr, values[: hdr.nfft], values[hdr.nfft :]
    return hdr, values, None


# ------------------------------------------------------------------------- captures


def iter_capture_chunks(
    capture_id: str,
    channel_id: str,
    iq: np.ndarray,
    t_first_ns: int,
    sample_rate_hz: float,
    center_hz: float,
    overflow: bool,
    clock: ClockStatus | Mapping[str, Any],
    max_payload: int = MAX_CHUNK_PAYLOAD_BYTES,
) -> Iterator[bytes]:
    """Encode a capture as `capture_data` frames, ready to send in order.

    capture_id, channel_id: the request being answered.
    iq: complex64[n], 1 <= n <= `MAX_CAPTURE_SAMPLES`, finite. Either byte order;
        written little-endian as interleaved float32 I, Q. Not copied when it is
        already contiguous and little-endian: the iterator then reads from the
        caller's array, which must stay unchanged until the iterator is exhausted.
    t_first_ns: labelled time of `iq[0]`, integer UTC ns.
    sample_rate_hz, center_hz: rate and centre actually used, Hz.
    overflow: whether the driver lost samples during the capture.
    clock: state of the node clock at the capture.
    max_payload: most payload bytes per frame, 8 to `MAX_CHUNK_PAYLOAD_BYTES`
        (256 KiB, the protocol limit); each frame carries `max_payload // 8` samples
        except the last, which carries the rest.
    Yields one frame per chunk with `seq` counting from 0, `offset` the index of the
    chunk's first sample, `n` its length, `n_samples_total` the length of `iq`, and
    `last` true on the final frame only.

    Every argument is checked before the iterator is returned, so a bad argument
    raises `ValueError` here and never after part of a capture has been sent. The
    check reads all of `iq` once (about 1 ms per million samples). Await between
    frames so that heartbeats and spectrum frames interleave.
    """
    if isinstance(max_payload, bool) or not isinstance(max_payload, int):
        raise ValueError(f"max_payload: must be an integer, got {type(max_payload).__name__}")
    if not _SAMPLE_BYTES <= max_payload <= MAX_CHUNK_PAYLOAD_BYTES:
        raise ValueError(
            f"max_payload: must be {_SAMPLE_BYTES} to {MAX_CHUNK_PAYLOAD_BYTES} bytes, "
            f"got {max_payload}"
        )
    _check_array(iq, "iq", _C64_LE, "complex64")
    total = int(iq.shape[0])
    if total < 1:
        raise ValueError("iq: must hold at least one sample")
    if total > MAX_CAPTURE_SAMPLES:
        raise ValueError(f"iq: {total} samples exceeds the limit of {MAX_CAPTURE_SAMPLES}")
    samples = np.ascontiguousarray(iq, dtype=_C64_LE)
    if not _all_finite(samples):
        raise ValueError("iq: holds NaN or infinite samples")
    if isinstance(clock, ClockStatus):
        status = clock
    elif isinstance(clock, Mapping):
        status = ClockStatus(**clock)
    else:
        raise ValueError(f"clock: must be a ClockStatus or a mapping, got {type(clock).__name__}")
    per_chunk = max_payload // _SAMPLE_BYTES
    first = CaptureDataHeader(
        capture_id=capture_id,
        channel_id=channel_id,
        seq=0,
        last=total <= per_chunk,
        t_first_ns=t_first_ns,
        sample_rate_hz=sample_rate_hz,
        center_hz=center_hz,
        n_samples_total=total,
        offset=0,
        n=min(per_chunk, total),
        overflow=overflow,
        clock=status,
    )
    return _capture_frames(samples, first, per_chunk)


def _capture_frames(
    samples: np.ndarray, first: CaptureDataHeader, per_chunk: int
) -> Iterator[bytes]:
    """The frames of `iter_capture_chunks`, given validated input."""
    total = first.n_samples_total
    raw = samples.view(np.uint8)
    header = first
    for seq, offset in enumerate(range(0, total, per_chunk)):
        count = min(per_chunk, total - offset)
        if seq:
            header = CaptureDataHeader(
                capture_id=first.capture_id,
                channel_id=first.channel_id,
                seq=seq,
                last=offset + count == total,
                t_first_ns=first.t_first_ns,
                sample_rate_hz=first.sample_rate_hz,
                center_hz=first.center_hz,
                n_samples_total=total,
                offset=offset,
                n=count,
                overflow=first.overflow,
                clock=first.clock,
            )
        chunk = raw[offset * _SAMPLE_BYTES : (offset + count) * _SAMPLE_BYTES]
        yield _frame(_header_bytes(header), (chunk,))


def decode_capture_data(
    header: Mapping[str, Any] | CaptureDataHeader, payload: BytesLike
) -> tuple[CaptureDataHeader, np.ndarray]:
    """Validate one capture chunk split by `decode_frame`.

    header: the header dict (or an already validated `CaptureDataHeader`).
    payload: the frame's payload.
    Returns `(header, iq)` with `iq` complex64[n]. The array **aliases `payload`** (no
    copy on little-endian hosts) and is read-only when the frame was `bytes`.
    Raises `ProtocolError` for a bad header, a payload that is not exactly 8 * n
    bytes, or NaN or infinite samples. Continuity across chunks is the business of
    `CaptureAssembler`.
    """
    hdr = header if isinstance(header, CaptureDataHeader) else CaptureDataHeader.from_wire(header)
    view = _byte_view(payload, "capture_data payload", ProtocolError)
    _expect_payload(view, hdr.n * _SAMPLE_BYTES, "capture_data", f"n {hdr.n}")
    iq = _array_from_payload(view, _C64_LE)
    if not _all_finite(iq):
        raise ProtocolError(
            "capture_data: payload holds NaN or infinite samples", message_type="capture_data"
        )
    return hdr, iq


@dataclass(frozen=True, eq=False)
class AssembledCapture:
    """A complete capture put together from its chunks.

    capture_id, channel_id: the request answered.
    iq: complex64[n], native byte order, writable, owned by this object (it aliases
        no frame).
    t_first_ns: labelled time of `iq[0]`, integer UTC ns.
    sample_rate_hz, center_hz: rate and centre the node actually used, Hz.
    overflow: true when any chunk reported lost samples.
    clock: node clock state: that of the first chunk, or of the first chunk reporting
        `locked = false` when the lock was lost on the way.
    n_chunks: frames the capture arrived in.
    """

    capture_id: str
    channel_id: str
    iq: np.ndarray
    t_first_ns: int
    sample_rate_hz: float
    center_hz: float
    overflow: bool
    clock: ClockStatus
    n_chunks: int

    @property
    def n_samples(self) -> int:
        """Number of complex samples in `iq`."""
        return int(self.iq.shape[0])

    # The three properties below give this object the field names of the driver
    # `Capture` of ARCHITECTURE.md section 6 (iq, t_first_ns, fs, fc, overflow,
    # clock_locked), which is what `hub.capture` is documented to return there.

    @property
    def fs(self) -> float:
        """Sample rate the node actually used, Hz (same as `sample_rate_hz`)."""
        return self.sample_rate_hz

    @property
    def fc(self) -> float:
        """Centre frequency the node actually used, Hz (same as `center_hz`)."""
        return self.center_hz

    @property
    def clock_locked(self) -> bool:
        """Whether the node's clock was disciplined for the whole capture
        (`clock.locked`). False means: no TDOA from this capture unless both channels
        of the pair are at one site."""
        return self.clock.locked


@dataclass
class _Progress:
    """State of a capture that has received at least one chunk."""

    first: CaptureDataHeader
    iq: np.ndarray
    clock: ClockStatus
    received: int = 0
    next_seq: int = 0
    overflow: bool = False


_CONSTANT_FIELDS: Final[tuple[str, ...]] = (
    "capture_id",
    "channel_id",
    "t_first_ns",
    "sample_rate_hz",
    "center_hz",
    "n_samples_total",
)


class CaptureAssembler:
    """Puts the `capture_data` chunks of one capture back together.

    Feed every chunk of the capture to `add` in arrival order. `add` returns None
    until the chunk marked `last` arrives and then returns the `AssembledCapture`.
    Any inconsistency raises `ProtocolError` and voids the capture: the storage is
    released and every later `add` raises as well.

    A capture that stops arriving (the node disconnected, or sent `capture_error`)
    simply never completes: drop the assembler; `complete` is false and
    `received_samples` says how far it got.

    Not thread-safe; use it from one task. Each `add` copies at most one chunk
    (256 KiB) and takes tens of microseconds. Storage for the whole capture is
    reserved when the first chunk arrives, bounded by `max_bytes`.
    """

    def __init__(
        self,
        capture_id: str | None = None,
        channel_id: str | None = None,
        *,
        expected_n_samples: int | None = None,
        max_bytes: int = MAX_CAPTURE_BYTES,
        max_chunk_payload: int = MAX_CHUNK_PAYLOAD_BYTES,
    ) -> None:
        """
        capture_id, channel_id: what every chunk must carry; None accepts whatever the
            first chunk carries (later chunks must still agree with it).
        expected_n_samples: the `n_samples_total` every chunk must announce (the
            `n_samples` of the request); None accepts the node's figure.
        max_bytes: largest capture accepted, bytes of payload (default 64 MiB).
        max_chunk_payload: largest chunk accepted, bytes of payload (default 256 KiB).
        Raises `ValueError` for a non-positive limit or expectation.
        """
        for name, value in (("max_bytes", max_bytes), ("max_chunk_payload", max_chunk_payload)):
            if isinstance(value, bool) or not isinstance(value, int) or value < _SAMPLE_BYTES:
                raise ValueError(
                    f"{name}: must be an integer of at least {_SAMPLE_BYTES}, got {value!r}"
                )
        if expected_n_samples is not None and (
            isinstance(expected_n_samples, bool)
            or not isinstance(expected_n_samples, int)
            or expected_n_samples < 1
        ):
            raise ValueError(
                f"expected_n_samples: must be a positive integer, got {expected_n_samples!r}"
            )
        self._capture_id = capture_id
        self._channel_id = channel_id
        self._expected_n_samples = expected_n_samples
        self._max_bytes = max_bytes
        self._max_chunk_payload = max_chunk_payload
        self._progress: _Progress | None = None
        self._received = 0
        self._n_samples_total: int | None = None
        self._state: Literal["open", "complete", "failed"] = "open"
        self._failure: str | None = None

    @property
    def capture_id(self) -> str | None:
        """Capture this assembler is for; None until known."""
        return self._capture_id

    @property
    def channel_id(self) -> str | None:
        """Channel this assembler is for; None until known."""
        return self._channel_id

    @property
    def complete(self) -> bool:
        """Whether the last chunk has arrived and the capture was returned."""
        return self._state == "complete"

    @property
    def failed(self) -> bool:
        """Whether a chunk broke the protocol and voided the capture."""
        return self._state == "failed"

    @property
    def failure(self) -> str | None:
        """Detail of the error that voided the capture, or None."""
        return self._failure

    @property
    def received_samples(self) -> int:
        """Complex samples accepted so far."""
        return self._received

    @property
    def n_samples_total(self) -> int | None:
        """Length of the whole capture as the node announced it; None before a chunk."""
        return self._n_samples_total

    def add(
        self, header: Mapping[str, Any] | CaptureDataHeader, payload: BytesLike
    ) -> AssembledCapture | None:
        """Accept the next chunk.

        header, payload: as `decode_frame` returned them (the header may also be an
            already validated `CaptureDataHeader`). The payload is copied; nothing
            keeps a reference to it afterwards.
        Returns the `AssembledCapture` when this chunk completes the capture, else None.
        Raises `ProtocolError` when the chunk is malformed (bad header, payload not
        8 * n bytes, NaN or infinite samples), belongs to another capture or channel,
        arrives out of order or twice, overlaps or skips samples, changes
        `t_first_ns`, `sample_rate_hz`, `center_hz` or `n_samples_total`, exceeds the
        chunk or capture size limit, or arrives after the capture completed or failed.
        """
        if self._state == "complete":
            raise ProtocolError(
                "capture_data: chunk after the capture was complete", message_type="capture_data"
            )
        if self._state == "failed":
            raise ProtocolError(
                f"capture_data: capture was voided by an earlier error ({self._failure})",
                message_type="capture_data",
            )
        try:
            return self._accept(header, payload)
        except ProtocolError as exc:
            self._state = "failed"
            self._failure = exc.detail
            self._progress = None
            logger.debug(
                "capture voided by a bad chunk",
                extra={
                    "capture_id": self._capture_id,
                    "channel_id": self._channel_id,
                    "received_samples": self._received,
                    "protocol_detail": exc.detail,
                },
            )
            raise

    def _accept(
        self, header: Mapping[str, Any] | CaptureDataHeader, payload: BytesLike
    ) -> AssembledCapture | None:
        """`add` without the state bookkeeping."""
        hdr = (
            header
            if isinstance(header, CaptureDataHeader)
            else CaptureDataHeader.from_wire(header)
        )
        if self._capture_id is not None and hdr.capture_id != self._capture_id:
            raise self._error("capture_id is not that of this capture")
        if self._channel_id is not None and hdr.channel_id != self._channel_id:
            raise self._error("channel_id is not that of this capture")
        progress = self._progress
        if progress is None:
            if (
                self._expected_n_samples is not None
                and hdr.n_samples_total != self._expected_n_samples
            ):
                raise self._error(
                    f"n_samples_total is {hdr.n_samples_total}, expected {self._expected_n_samples}"
                )
            if hdr.n_samples_total * _SAMPLE_BYTES > self._max_bytes:
                raise self._error(
                    f"capture of {hdr.n_samples_total * _SAMPLE_BYTES} bytes exceeds the limit "
                    f"of {self._max_bytes}"
                )
            next_seq, received = 0, 0
        else:
            for name in _CONSTANT_FIELDS:
                if getattr(hdr, name) != getattr(progress.first, name):
                    raise self._error(f"{name} changed between chunks")
            next_seq, received = progress.next_seq, progress.received
        if hdr.seq != next_seq:
            kind = "duplicate chunk" if hdr.seq < next_seq else "chunk out of order"
            raise self._error(f"{kind}: seq {hdr.seq}, expected {next_seq}")
        if hdr.offset != received:
            kind = "overlaps samples already received" if hdr.offset < received else "skips samples"
            raise self._error(f"chunk {kind}: offset {hdr.offset}, expected {received}")
        view = _byte_view(payload, "capture_data payload", ProtocolError)
        if view.nbytes > self._max_chunk_payload:
            raise self._error(
                f"chunk payload of {view.nbytes} bytes exceeds the limit "
                f"of {self._max_chunk_payload}"
            )
        hdr, chunk = decode_capture_data(hdr, view)

        if progress is None:
            # Reserved, not touched: the pages are committed as chunks are copied in.
            progress = _Progress(
                first=hdr, iq=np.empty(hdr.n_samples_total, dtype=np.complex64), clock=hdr.clock
            )
            self._progress = progress
            self._capture_id = hdr.capture_id
            self._channel_id = hdr.channel_id
            self._n_samples_total = hdr.n_samples_total
        elif progress.clock.locked and not hdr.clock.locked:
            progress.clock = hdr.clock
        progress.iq[hdr.offset : hdr.offset + hdr.n] = chunk
        progress.received += hdr.n
        progress.next_seq += 1
        progress.overflow = progress.overflow or hdr.overflow
        self._received = progress.received
        if not hdr.last:
            return None

        self._progress = None
        self._state = "complete"
        return AssembledCapture(
            capture_id=hdr.capture_id,
            channel_id=hdr.channel_id,
            iq=progress.iq,
            t_first_ns=hdr.t_first_ns,
            sample_rate_hz=hdr.sample_rate_hz,
            center_hz=hdr.center_hz,
            overflow=progress.overflow,
            clock=progress.clock,
            n_chunks=progress.next_seq,
        )

    @staticmethod
    def _error(detail: str) -> ProtocolError:
        """A `ProtocolError` for a chunk that does not fit the capture."""
        return ProtocolError(f"capture_data: {detail}", message_type="capture_data")
