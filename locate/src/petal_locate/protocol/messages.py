"""Wire messages of PROTOCOL.md version 1: models, parsing and dumping.

Section A (node to server) is covered completely: every control message, the embedded
types and the headers of the two binary frames. The console spectrum frame of section
B has its header here as well (`ConsolePsdHeader`); its payload handling, like that of
every binary frame, lives in `framing.py`.

Conventions that hold for every model in this module:

* **Strict types.** Nothing is coerced: an integer field refuses `1.0`, `"1"` and
  `true`; a float field takes any JSON number but no string and no boolean; NaN and
  the infinities are refused everywhere.
* **Absolute time is an integer.** Every absolute-time field (`t_ns`, `server_t_ns`,
  `t_first_ns`, `t_start_ns`, `deadline_ns`, `effective_ns`) is a Python `int` of UTC
  nanoseconds since the Unix epoch, limited to `0 .. 2**63 - 1` so that it fits a
  signed 64-bit column, and is written as a JSON integer. `ClockStatus.est_offset_ns`
  is not an absolute time: it is a small signed estimate and is a JSON number.
* **Tolerant when parsing, exact when building.** A message read off the wire
  (`parse_control`, `parse_binary_header`, `WireModel.from_wire`) ignores fields it
  does not know and accepts reject reasons and capture error codes it does not know,
  so that a newer peer can add to the protocol. The same models built in code refuse
  unknown keyword arguments and unknown reasons or codes, so that a typing mistake
  cannot disappear into a default.
* **Frozen.** A field cannot be reassigned; `model_copy(update=...)` derives a changed
  message (without re-validating, as pydantic documents).

Malformed input never raises anything but `ProtocolError`, whose `detail` is safe to
send back in an `error` message: it names the fields at fault and never quotes their
values (a `hello` carries the bearer token).
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Final, Literal, Self, TypeAlias, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

__all__ = [
    "BAD_MESSAGE_LIMIT",
    "CAPTURE_ERROR_CODES",
    "CLOSE_CODE_REPLACED",
    "CLOSE_CODE_SHUTDOWN",
    "DEFAULT_HEARTBEAT_S",
    "ERROR_BAD_MESSAGE",
    "HEARTBEAT_MISS_LIMIT",
    "MAX_CAPTURE_BYTES",
    "MAX_CAPTURE_SAMPLES",
    "MAX_CONCURRENT_CAPTURES",
    "MAX_ERROR_DETAIL_CHARS",
    "MAX_ID_CHARS",
    "MAX_PSD_BINS",
    "MAX_TIME_NS",
    "PROTOCOL_VERSION",
    "RECONNECT_BACKOFF_MAX_S",
    "RECONNECT_BACKOFF_MIN_S",
    "RECONNECT_JITTER",
    "REJECT_REASONS",
    "SIM_UPDATE_LEAD_NS",
    "WS_MAX_SIZE_BYTES",
    "Assign",
    "AssignAck",
    "BinaryHeader",
    "Capabilities",
    "CaptureCancel",
    "CaptureDataHeader",
    "CaptureErrorMessage",
    "CaptureRequest",
    "ChannelAck",
    "ChannelAssignment",
    "ClockStatus",
    "ConsoleDetection",
    "ConsolePsdHeader",
    "ConsoleTimeNs",
    "ControlMessage",
    "ErrorMessage",
    "Heartbeat",
    "HeartbeatAck",
    "Hello",
    "MonitorAssignment",
    "NodeLoad",
    "ProtocolError",
    "PsdHeader",
    "Reject",
    "SimUpdate",
    "SimulationInfo",
    "TimeNs",
    "UnknownMessage",
    "Welcome",
    "WireModel",
    "dump_control",
    "dump_json",
    "loads_json_object",
    "message_type",
    "parse_binary_header",
    "parse_control",
]

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- constants

PROTOCOL_VERSION: Final[int] = 1
"""Value of `protocol` in `hello` and `welcome`."""

DEFAULT_HEARTBEAT_S: Final[float] = 2.0
"""Heartbeat period a `welcome` announces unless the server says otherwise, seconds."""

HEARTBEAT_MISS_LIMIT: Final[int] = 3
"""Heartbeat periods of silence after which a node is offline."""

BAD_MESSAGE_LIMIT: Final[int] = 3
"""Consecutive malformed messages after which a session is closed."""

MAX_CONCURRENT_CAPTURES: Final[int] = 8
"""Captures a server keeps pending on one node at a time."""

MAX_CAPTURE_BYTES: Final[int] = 64 * 1024 * 1024
"""Largest capture a server accepts, bytes of complex64 payload (64 MiB)."""

MAX_CAPTURE_SAMPLES: Final[int] = MAX_CAPTURE_BYTES // 8
"""`MAX_CAPTURE_BYTES` in complex64 samples (8 388 608)."""

WS_MAX_SIZE_BYTES: Final[int] = 1024 * 1024
"""WebSocket message size both ends must accept (1 MiB); every frame built by this
package fits in it. Compression is off on both ends (`compression=None`)."""

MAX_PSD_BINS: Final[int] = 131072
"""Largest `nfft` of a monitor window: its float32 payload (512 KiB) plus the header
still fits one WebSocket message of `WS_MAX_SIZE_BYTES`."""

RECONNECT_BACKOFF_MIN_S: Final[float] = 1.0
"""First delay before a node reconnects, seconds; doubles after every failure."""

RECONNECT_BACKOFF_MAX_S: Final[float] = 30.0
"""Longest delay before a node reconnects, seconds."""

RECONNECT_JITTER: Final[float] = 0.2
"""Random extra delay added to each reconnect wait, as a fraction of it (0 to 20 %)."""

SIM_UPDATE_LEAD_NS: Final[int] = 2_000_000_000
"""How far ahead of server time `sim_update.effective_ns` is set, ns (2 s)."""

CLOSE_CODE_SHUTDOWN: Final[int] = 1001
"""WebSocket close code either side uses for an orderly shutdown."""

CLOSE_CODE_REPLACED: Final[int] = 4001
"""WebSocket close code of a session replaced by a new `hello` with the same node_id."""

REJECT_REASONS: Final[frozenset[str]] = frozenset(
    {"bad_token", "unknown_site", "protocol_mismatch", "duplicate_node"}
)
"""Reasons a version 1 server gives in `reject`."""

CAPTURE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"out_of_range", "too_late", "overflow", "driver", "busy", "cancelled"}
)
"""Codes a version 1 node gives in `capture_error`."""

ERROR_BAD_MESSAGE: Final[str] = "bad_message"
"""The `error` code for a malformed message."""

MAX_TIME_NS: Final[int] = 2**63 - 1
"""Largest absolute time accepted, ns (the signed 64-bit limit, year 2262)."""

MAX_ID_CHARS: Final[int] = 256
"""Longest identifier (node, site, channel, monitor, capture) accepted, characters."""

MAX_ERROR_DETAIL_CHARS: Final[int] = 512
"""Longest `ProtocolError.detail`, characters; longer details are cut with an ellipsis."""

_WIRE_FLAG: Final[str] = "petal_wire"
_WIRE_CONTEXT: Final[dict[str, bool]] = {_WIRE_FLAG: True}
_MAX_NS_DIGITS: Final[int] = len(str(MAX_TIME_NS))


# ------------------------------------------------------------------------------ errors


class ProtocolError(ValueError):
    """A message or frame received from a peer violates PROTOCOL.md.

    detail: what is wrong, naming the message type and fields, never their values;
        at most `MAX_ERROR_DETAIL_CHARS` characters; safe to log and to send back.
    message_type: the `type` of the offending message when it could be read.
    """

    def __init__(self, detail: str, *, message_type: str | None = None) -> None:
        detail = _clip(detail, MAX_ERROR_DETAIL_CHARS)
        super().__init__(detail)
        self.detail: str = detail
        self.message_type: str | None = message_type

    def to_message(self) -> ErrorMessage:
        """The `error {code: "bad_message", detail}` reply for this failure."""
        return ErrorMessage.bad_message(self.detail)


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, the last three an ellipsis when cut."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_wire(info: ValidationInfo) -> bool:
    """True while validating something read off the wire rather than built in code."""
    context = info.context
    return isinstance(context, dict) and context.get(_WIRE_FLAG) is True


# ---------------------------------------------------------------------- field types


def _console_ns_from_wire(value: Any, info: ValidationInfo) -> Any:
    """Turn the console's decimal-string nanoseconds into an int before validation."""
    if isinstance(value, str):
        if not (value.isascii() and value.isdigit()) or len(value) > _MAX_NS_DIGITS:
            raise ValueError("must be a decimal string of at most 19 digits")
        return int(value)
    if _is_wire(info):
        raise ValueError("must be a decimal string on the console interface")
    return value


def _ns_to_string(value: int) -> str:
    """Decimal string form of integer nanoseconds (console interface)."""
    return str(value)


TimeNs = Annotated[int, Field(ge=0, le=MAX_TIME_NS)]
"""Absolute UTC time, integer ns since the Unix epoch; a JSON integer on the wire.
Usable as a field type in any strict pydantic model."""

ConsoleTimeNs = Annotated[
    int,
    BeforeValidator(_console_ns_from_wire),
    Field(ge=0, le=MAX_TIME_NS),
    PlainSerializer(_ns_to_string, return_type=str, when_used="json"),
]
"""Absolute UTC time, integer ns in Python and a decimal string in JSON (section B).
Usable as a field type in any pydantic model of the console interface: it validates
from an int or a decimal string and serialises to the string in JSON mode."""

Ident = Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS)]
"""An identifier: 1 to `MAX_ID_CHARS` characters."""

Counter = Annotated[int, Field(ge=0)]
"""A non-negative integer counter (sequence number, version, offset)."""

PositiveInt = Annotated[int, Field(ge=1)]
"""An integer of at least one."""

Bins = Annotated[int, Field(ge=2, le=MAX_PSD_BINS)]
"""Number of spectrum bins of a window, 2 to `MAX_PSD_BINS`."""

PositiveFloat = Annotated[float, Field(gt=0)]
"""A finite number above zero."""

NonNegativeFloat = Annotated[float, Field(ge=0)]
"""A finite number of at least zero."""


# -------------------------------------------------------------------------- base model


class WireModel(BaseModel):
    """Base of every wire model: strict, frozen, finite, tolerant of unknown fields.

    Unknown fields are ignored only for data read off the wire (`from_wire`,
    `parse_control`, `parse_binary_header`). Building a model in code, or calling
    `model_validate` directly, refuses them with a `ValueError` naming the fields.
    """

    model_config = ConfigDict(
        extra="ignore",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )

    _OMIT_NONE: ClassVar[bool] = True
    """Whether `dump_json` leaves out fields that are None (the `?` fields of the
    protocol) or writes them as null."""

    @model_validator(mode="before")
    @classmethod
    def _refuse_unknown_fields_unless_wire(cls, data: Any, info: ValidationInfo) -> Any:
        if isinstance(data, dict) and not _is_wire(info):
            unknown = sorted(str(key) for key in data.keys() - cls.model_fields.keys())
            if unknown:
                raise ValueError(f"unexpected field(s) for {cls.__name__}: {', '.join(unknown)}")
        return data

    @classmethod
    def from_wire(cls, obj: Mapping[str, Any]) -> Self:
        """Validate a decoded JSON object as this model, ignoring unknown fields.

        obj: the JSON object as a mapping (as `json.loads` or `decode_frame` gives it).
        Raises `ProtocolError` naming every field at fault.
        """
        if not isinstance(obj, Mapping):
            raise ProtocolError(f"{_label(cls)}: must be a JSON object")
        try:
            data = obj if isinstance(obj, dict) else dict(obj)
            return cls.model_validate(data, context=_WIRE_CONTEXT)
        except ValidationError as exc:
            label = _label(cls)
            detail = _validation_detail(label, exc)
            logger.debug(
                "wire object failed validation",
                extra={"protocol_type": label, "protocol_detail": detail},
            )
            # `from None`: the ValidationError text quotes input values (the token of a
            # hello among them) and must not ride along in a logged traceback.
            raise ProtocolError(detail, message_type=label) from None

    def to_json(self) -> str:
        """Compact JSON text of this model (see `dump_json`)."""
        return dump_json(self)

    def to_wire(self) -> dict[str, Any]:
        """The JSON-ready dict of this model: what `json.loads(self.to_json())` gives."""
        return self.model_dump(mode="json", exclude_none=type(self)._OMIT_NONE)


def _label(cls: type[WireModel]) -> str:
    """Wire `type` of a message class, or the class name for an embedded type."""
    field = cls.model_fields.get("type")
    if field is not None and isinstance(field.default, str):
        return field.default
    return cls.__name__


def _validation_detail(label: str, exc: ValidationError) -> str:
    """One line naming each failing field of `exc`, without any input value."""
    parts: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "(message)"
        parts.append(f"{location}: {error['msg']}")
    return f"{label}: " + "; ".join(parts)


# --------------------------------------------------------------------- embedded types


class ClockStatus(WireModel):
    """State of a node's time reference.

    source: what disciplines the clock: "gpsdo", "ntp", "sim" (simulated) or "free"
        (free running).
    locked: whether the clock is disciplined right now. A capture taken unlocked
        yields no TDOA unless both channels of the pair are at one site.
    est_offset_ns: the node's estimate of true time minus labelled time, ns (signed;
        MATH.md section 4 `delta_g`). Displayed, never applied to measurements.
    holdover_s: seconds since the reference was lost, 0 while locked.
    """

    source: Literal["gpsdo", "ntp", "sim", "free"]
    locked: bool
    est_offset_ns: float
    holdover_s: NonNegativeFloat


class MonitorAssignment(WireModel):
    """One spectrum window a node streams for a channel.

    monitor_id: unique within the channel.
    center_hz: centre of the window, Hz (downlink RF).
    span_hz: width of the window and sample rate of its snippets, Hz.
    nfft: bins per frame, 2 to `MAX_PSD_BINS` (a frame must fit one WebSocket message).
    navg: Welch segments averaged per frame (at least 1).
    frame_rate_hz: frames per second.
    """

    monitor_id: Ident
    center_hz: NonNegativeFloat
    span_hz: PositiveFloat
    nfft: Bins
    navg: PositiveInt
    frame_rate_hz: PositiveFloat


class ChannelAssignment(WireModel):
    """One receive channel a node is told to open, with its monitor windows.

    channel_id: unique across the whole system configuration.
    antenna_summary: human-readable description of the antenna, for display and logs.
    monitors: spectrum windows; monitor ids are unique within the channel.
    """

    channel_id: Ident
    antenna_summary: str
    monitors: list[MonitorAssignment]

    @model_validator(mode="after")
    def _monitor_ids_unique(self) -> Self:
        _require_unique((m.monitor_id for m in self.monitors), "monitors", "monitor_id")
        return self


class Capabilities(WireModel):
    """What an opened driver can do (reported in `assign_ack`).

    min_hz, max_hz: tunable range of centre frequencies, Hz (min_hz <= max_hz).
    max_sample_rate_hz: highest complex sample rate, Hz.
    lookback_s: how far into the past a capture may start, seconds (0 for none).
    concurrent_reads: how many reads the driver serves at once (at least 1).
    """

    min_hz: NonNegativeFloat
    max_hz: NonNegativeFloat
    max_sample_rate_hz: PositiveFloat
    lookback_s: NonNegativeFloat
    concurrent_reads: PositiveInt

    @model_validator(mode="after")
    def _range_ordered(self) -> Self:
        if self.max_hz < self.min_hz:
            raise ValueError("max_hz must not be below min_hz")
        return self


class ChannelAck(WireModel):
    """Outcome of opening one channel (an entry of `assign_ack.channels`).

    channel_id: the channel answered for.
    ok: whether its driver opened.
    error: why it did not, when `ok` is false.
    capabilities: required when `ok` is true; a driver that failed to open has none.
    """

    channel_id: Ident
    ok: bool
    error: str | None = None
    capabilities: Capabilities | None = None

    @model_validator(mode="after")
    def _capabilities_when_ok(self) -> Self:
        if self.ok and self.capabilities is None:
            raise ValueError("capabilities is required when ok is true")
        return self


class SimulationInfo(WireModel):
    """The `simulation` object of `welcome`.

    enabled: whether the server runs a simulation scenario.
    scenario_url: path on the server origin of the simulation backplane document.
    version: scenario version at the time of the welcome.
    """

    enabled: bool
    scenario_url: str | None = None
    version: Counter | None = None


class NodeLoad(WireModel):
    """The `load` object of `heartbeat`.

    cpu: processor load of the node agent as a fraction of one core (1.0 = one core
        fully busy; may exceed 1 on several cores).
    queue: capture requests the node holds that are waiting or in progress.
    """

    cpu: NonNegativeFloat
    queue: Counter


# ------------------------------------------------------------------- control messages


class Hello(WireModel):
    """Node -> server, first message of a session.

    protocol: protocol version the node speaks. Any integer parses, so that a server
        can answer a mismatch with `reject protocol_mismatch`.
    node_id: identifier the node generated once and persists (a UUID by default).
    site_id: the configured site this node serves.
    token: bearer secret of the site. Kept out of `repr` and of every error detail.
    software: node software name and version, free text.
    driver: name of the SDR driver in use.
    clock: state of the node clock.
    t_ns: node clock when the message was sent, UTC ns (labelled time).
    """

    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    node_id: Ident
    site_id: Ident
    token: str = Field(repr=False)
    software: str
    driver: str
    clock: ClockStatus
    t_ns: TimeNs


class Welcome(WireModel):
    """Server -> node, accepts a `hello`.

    protocol: protocol version the server speaks.
    server_t_ns: server clock when the message was sent, UTC ns.
    heartbeat_s: period at which the node must heartbeat, seconds (default 2).
    config_version: version of the configuration the channels come from.
    channels: channels to open; channel ids are unique.
    simulation: simulation backplane description (disabled when absent).
    """

    type: Literal["welcome"] = "welcome"
    protocol: int = PROTOCOL_VERSION
    server_t_ns: TimeNs
    heartbeat_s: PositiveFloat = DEFAULT_HEARTBEAT_S
    config_version: Counter
    channels: list[ChannelAssignment]
    simulation: SimulationInfo = Field(default_factory=lambda: SimulationInfo(enabled=False))

    @model_validator(mode="after")
    def _channel_ids_unique(self) -> Self:
        _require_unique((c.channel_id for c in self.channels), "channels", "channel_id")
        return self


class Reject(WireModel):
    """Server -> node, refuses a `hello`; the server closes the socket afterwards.

    reason: one of `REJECT_REASONS` when built here; any non-empty string is accepted
        from the wire so that a newer server can add reasons.
    """

    type: Literal["reject"] = "reject"
    reason: Ident

    @field_validator("reason")
    @classmethod
    def _reason_known_when_built(cls, value: str, info: ValidationInfo) -> str:
        return _require_known(value, REJECT_REASONS, "reject reason", info)


class Assign(WireModel):
    """Server -> node, replaces the channel assignment after a configuration change.

    config_version: version of the configuration the channels come from.
    channels: the complete new set of channels; channel ids are unique.
    """

    type: Literal["assign"] = "assign"
    config_version: Counter
    channels: list[ChannelAssignment]

    @model_validator(mode="after")
    def _channel_ids_unique(self) -> Self:
        _require_unique((c.channel_id for c in self.channels), "channels", "channel_id")
        return self


class AssignAck(WireModel):
    """Node -> server, sent after `welcome` and after every `assign`.

    config_version: the version being acknowledged.
    ok: whether the assignment as a whole was applied.
    channels: one entry per channel the node tried to open; channel ids are unique.
    """

    type: Literal["assign_ack"] = "assign_ack"
    config_version: Counter
    ok: bool
    channels: list[ChannelAck]

    @model_validator(mode="after")
    def _channel_ids_unique(self) -> Self:
        _require_unique((c.channel_id for c in self.channels), "channels", "channel_id")
        return self


class Heartbeat(WireModel):
    """Node -> server, every `heartbeat_s`.

    seq: heartbeat counter of the session, from 0.
    t_ns: node clock when the message was sent, UTC ns (labelled time).
    rtt_ms: round trip the node measured for its previous heartbeat, milliseconds;
        None (left out of the JSON) until one has been measured.
    clock: state of the node clock.
    load: how busy the node is.
    """

    type: Literal["heartbeat"] = "heartbeat"
    seq: Counter
    t_ns: TimeNs
    rtt_ms: NonNegativeFloat | None = None
    clock: ClockStatus
    load: NodeLoad


class HeartbeatAck(WireModel):
    """Server -> node, answers one heartbeat.

    seq: the `seq` of the heartbeat answered.
    server_t_ns: server clock when the answer was sent, UTC ns.
    """

    type: Literal["heartbeat_ack"] = "heartbeat_ack"
    seq: Counter
    server_t_ns: TimeNs


class CaptureRequest(WireModel):
    """Server -> node, asks for a time-stamped IQ capture.

    capture_id: identifier the server chose; unique within the session.
    channel_id: the channel to read.
    t_start_ns: labelled time of the first sample wanted, UTC ns. Usually a little in
        the future; may be in the past when the driver advertises `lookback_s`.
    n_samples: complex samples wanted, 1 to `MAX_CAPTURE_SAMPLES`.
    sample_rate_hz: complex sample rate, Hz.
    center_hz: tuning centre, Hz.
    deadline_ns: node-clock time by which the last chunk must be sent, UTC ns; never
        before `t_start_ns`.
    """

    type: Literal["capture_request"] = "capture_request"
    capture_id: Ident
    channel_id: Ident
    t_start_ns: TimeNs
    n_samples: Annotated[int, Field(ge=1, le=MAX_CAPTURE_SAMPLES)]
    sample_rate_hz: PositiveFloat
    center_hz: NonNegativeFloat
    deadline_ns: TimeNs

    @model_validator(mode="after")
    def _deadline_after_start(self) -> Self:
        if self.deadline_ns < self.t_start_ns:
            raise ValueError("deadline_ns must not be before t_start_ns")
        return self


class CaptureErrorMessage(WireModel):
    """Node -> server, a capture failed; after partial data it voids the capture.

    capture_id, channel_id: the request that failed.
    code: one of `CAPTURE_ERROR_CODES` when built here; any non-empty string is
        accepted from the wire so that a newer node can add codes.
    message: human-readable explanation.

    Named `...Message` to stay clear of the driver exception `CaptureError`.
    """

    type: Literal["capture_error"] = "capture_error"
    capture_id: Ident
    channel_id: Ident
    code: Ident
    message: str = ""

    @field_validator("code")
    @classmethod
    def _code_known_when_built(cls, value: str, info: ValidationInfo) -> str:
        return _require_known(value, CAPTURE_ERROR_CODES, "capture error code", info)


class CaptureCancel(WireModel):
    """Server -> node, advisory: the capture is no longer wanted.

    capture_id: the request to abandon.
    """

    type: Literal["capture_cancel"] = "capture_cancel"
    capture_id: Ident


class SimUpdate(WireModel):
    """Server -> node, the simulated world changed (simulation backplane).

    version: version of the new scenario document.
    effective_ns: labelled time from which samples use the new world, UTC ns. A JSON
        integer here, like every time on the node interface (the HTTP document the
        node then fetches carries the same instant as a decimal string).
    """

    type: Literal["sim_update"] = "sim_update"
    version: Counter
    effective_ns: TimeNs


class ErrorMessage(WireModel):
    """Either direction, the peer sent something malformed.

    code: `"bad_message"` in version 1.
    detail: what was wrong.
    """

    type: Literal["error"] = "error"
    code: Ident = ERROR_BAD_MESSAGE
    detail: str = ""

    @classmethod
    def bad_message(cls, detail: str) -> ErrorMessage:
        """The reply to a malformed message, `detail` cut to `MAX_ERROR_DETAIL_CHARS`."""
        return cls(code=ERROR_BAD_MESSAGE, detail=_clip(detail, MAX_ERROR_DETAIL_CHARS))


# ---------------------------------------------------------------- binary-frame headers


class PsdHeader(WireModel):
    """Header of a node -> server spectrum frame; payload float32[nfft], little-endian.

    channel_id, monitor_id: the window the frame belongs to.
    t_ns: labelled time of the first sample of the snippet, UTC ns.
    center_hz, span_hz: bin k sits at center_hz + (k - nfft/2) * span_hz / nfft, Hz.
    nfft: number of bins (2 to `MAX_PSD_BINS`), which fixes the payload length at
        4 * nfft bytes.
    navg: Welch segments averaged.
    seq: frame counter of the window; gaps are normal because frames are droppable.
    units: always "dB".
    """

    type: Literal["psd"] = "psd"
    channel_id: Ident
    monitor_id: Ident
    t_ns: TimeNs
    center_hz: NonNegativeFloat
    span_hz: PositiveFloat
    nfft: Bins
    navg: PositiveInt
    seq: Counter
    units: Literal["dB"] = "dB"


class CaptureDataHeader(WireModel):
    """Header of one chunk of a capture; payload complex64[n], little-endian I, Q.

    capture_id, channel_id: the request answered.
    seq: chunk counter, from 0.
    last: true on the chunk that ends at `n_samples_total`, and only there.
    t_first_ns: labelled time of sample 0 of the whole capture, UTC ns.
    sample_rate_hz, center_hz: rate and centre actually used, Hz.
    n_samples_total: samples in the whole capture.
    offset: index of this chunk's first sample within the capture.
    n: samples in this chunk (at least 1), which fixes the payload at 8 * n bytes.
    overflow: whether the driver lost samples during the capture.
    clock: state of the node clock at the capture.

    A header is refused when its own numbers disagree: `offset + n` beyond the total,
    `last` set anywhere but on the final chunk, or an `offset` that `seq` earlier
    chunks of at least one sample each could not have reached.
    """

    type: Literal["capture_data"] = "capture_data"
    capture_id: Ident
    channel_id: Ident
    seq: Counter
    last: bool
    t_first_ns: TimeNs
    sample_rate_hz: PositiveFloat
    center_hz: NonNegativeFloat
    n_samples_total: PositiveInt
    offset: Counter
    n: PositiveInt
    overflow: bool
    clock: ClockStatus

    @model_validator(mode="after")
    def _extent_consistent(self) -> Self:
        end = self.offset + self.n
        if end > self.n_samples_total:
            raise ValueError("offset + n exceeds n_samples_total")
        if self.last != (end == self.n_samples_total):
            raise ValueError(
                "last must be true on the chunk that ends at n_samples_total and only there"
            )
        if self.offset < self.seq or (self.seq == 0 and self.offset != 0):
            raise ValueError("offset is impossible for this seq")
        return self


class ConsoleDetection(WireModel):
    """One confirmed detection drawn over a console spectrum window.

    track_id: tracker-assigned identifier of the detection.
    f_lo_hz, f_hi_hz: occupied band, downlink Hz (f_lo_hz <= f_hi_hz).
    authorized_carrier_id: the authorised carrier it matches, or None (JSON null).
    anomaly: whether an authorised carrier is behaving abnormally.
    """

    _OMIT_NONE: ClassVar[bool] = False

    track_id: int
    f_lo_hz: float
    f_hi_hz: float
    authorized_carrier_id: Ident | None = None
    anomaly: bool

    @model_validator(mode="after")
    def _band_ordered(self) -> Self:
        if self.f_hi_hz < self.f_lo_hz:
            raise ValueError("f_hi_hz must not be below f_lo_hz")
        return self

    @classmethod
    def from_detection(cls, detection: Any) -> ConsoleDetection:
        """Build from any object with the attributes of `core.detect.Detection`.

        Reads `track_id`, `f_lo_hz`, `f_hi_hz`, `authorized_carrier_id` and `anomaly`
        and converts numpy scalars to Python numbers. Raises `ValueError` naming the
        attribute that is missing or of the wrong kind.
        """
        try:
            carrier = detection.authorized_carrier_id
            return cls(
                track_id=int(detection.track_id),
                f_lo_hz=float(detection.f_lo_hz),
                f_hi_hz=float(detection.f_hi_hz),
                authorized_carrier_id=None if carrier is None else str(carrier),
                anomaly=bool(detection.anomaly),
            )
        except AttributeError as exc:
            raise ValueError(f"detection lacks attribute {exc.name!r}") from exc
        except TypeError as exc:
            raise ValueError(f"detection has a field of the wrong kind: {exc}") from exc


class ConsolePsdHeader(WireModel):
    """Header of a server -> console spectrum frame (PROTOCOL.md section B).

    channel_id, monitor_id: the window the frame belongs to.
    t_ns: labelled time of the first sample, UTC ns: an int here, a decimal string in
        JSON because JavaScript numbers cannot hold it.
    center_hz, span_hz: bin k sits at center_hz + (k - nfft/2) * span_hz / nfft, Hz.
    nfft: number of bins. The payload is float32[nfft] of PSD, followed by
        float32[nfft] of baseline when `has_baseline`.
    seq: frame counter of the window.
    has_baseline: whether the learned baseline follows the PSD.
    detections: confirmed detections in the window.
    """

    _OMIT_NONE: ClassVar[bool] = False

    type: Literal["psd"] = "psd"
    channel_id: Ident
    monitor_id: Ident
    t_ns: ConsoleTimeNs
    center_hz: NonNegativeFloat
    span_hz: PositiveFloat
    nfft: Bins
    seq: Counter
    has_baseline: bool
    detections: list[ConsoleDetection]


# ------------------------------------------------------------------------- validators


def _require_unique(values: Any, field: str, key: str) -> None:
    """Raise `ValueError` naming the first repeated identifier in `values`."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{field} repeats a {key}")
        seen.add(value)


def _require_known(value: str, known: frozenset[str], what: str, info: ValidationInfo) -> str:
    """Refuse a value outside `known` unless it was read off the wire."""
    if value not in known and not _is_wire(info):
        raise ValueError(f"unknown {what} {value!r}; version 1 defines {', '.join(sorted(known))}")
    return value


# -------------------------------------------------------------- parsing and dumping

ControlMessage: TypeAlias = Union[
    Hello,
    Welcome,
    Reject,
    Assign,
    AssignAck,
    Heartbeat,
    HeartbeatAck,
    CaptureRequest,
    CaptureErrorMessage,
    CaptureCancel,
    SimUpdate,
    ErrorMessage,
]
"""Every text-frame message of the node interface."""

BinaryHeader: TypeAlias = Union[PsdHeader, CaptureDataHeader]
"""Every binary-frame header of the node interface."""

_CONTROL_CLASSES: Final[tuple[type[ControlMessage], ...]] = (
    Hello,
    Welcome,
    Reject,
    Assign,
    AssignAck,
    Heartbeat,
    HeartbeatAck,
    CaptureRequest,
    CaptureErrorMessage,
    CaptureCancel,
    SimUpdate,
    ErrorMessage,
)
_BINARY_CLASSES: Final[tuple[type[BinaryHeader], ...]] = (PsdHeader, CaptureDataHeader)
_CONTROL_TYPES: Final[dict[str, type[ControlMessage]]] = {
    _label(cls): cls for cls in _CONTROL_CLASSES
}
_BINARY_TYPES: Final[dict[str, type[BinaryHeader]]] = {
    _label(cls): cls for cls in _BINARY_CLASSES
}


@dataclass(frozen=True)
class UnknownMessage:
    """A well-formed message or header whose `type` this version does not define.

    PROTOCOL.md: ignore it and log a warning (the caller does both; it knows which
    session the message came from).

    type: the unknown `type`.
    raw: the whole JSON object as received.
    """

    type: str
    raw: dict[str, Any]


def _refuse_constant(name: str) -> Any:
    """`json.loads` hook: NaN and the infinities are not JSON."""
    raise ValueError(f"non-finite constant {name} is not JSON")


def _finite_float(text: str) -> float:
    """`json.loads` hook: a number that overflows a double is refused, not made inf."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


def loads_json_object(text: str, *, what: str) -> dict[str, Any]:
    """Parse JSON text that must hold one object.

    text: the JSON document.
    what: name of the thing being parsed, for the error detail.
    Integers keep full precision (Python ints). Raises `ProtocolError` for invalid
    JSON, NaN or infinite numbers, nesting deeper than the interpreter allows,
    integers of absurd length, and any document that is not an object.
    """
    try:
        obj = json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float)
    except RecursionError:
        raise ProtocolError(f"{what}: JSON nested too deeply") from None
    except ValueError as exc:  # json.JSONDecodeError and the hooks above
        raise ProtocolError(f"{what}: invalid JSON ({exc})") from None
    if not isinstance(obj, dict):
        raise ProtocolError(f"{what}: must be a JSON object, got {_json_kind(obj)}")
    return obj


def _json_kind(value: Any) -> str:
    """JSON name of the kind of a decoded value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    return "an array"


def message_type(obj: Mapping[str, Any], *, what: str) -> str:
    """The `type` of a decoded message or header.

    Raises `ProtocolError` when `type` is missing, not a string, or empty.
    """
    try:
        value = obj["type"]
    except KeyError:
        raise ProtocolError(f"{what}: missing 'type'") from None
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{what}: 'type' must be a non-empty string")
    return value


def parse_control(text: str) -> ControlMessage | UnknownMessage:
    """Parse one text frame of the node interface.

    text: the frame, a JSON object with a `type` field.
    Returns the model for that `type` (unknown fields ignored), or `UnknownMessage`
    with the raw object when the `type` is not one this version defines.
    Raises `ProtocolError` (detail suitable for an `error` reply) when the text is not
    a JSON object, has no usable `type`, names a binary-frame type, or is a known
    message with missing or mistyped fields.
    """
    if not isinstance(text, str):
        raise ProtocolError(
            f"control message: must be a text frame, got {type(text).__name__}"
        )
    obj = loads_json_object(text, what="control message")
    mtype = message_type(obj, what="control message")
    cls = _CONTROL_TYPES.get(mtype)
    if cls is not None:
        return cls.from_wire(obj)
    if mtype in _BINARY_TYPES:
        raise ProtocolError(
            f"{mtype}: is a binary-frame header, not a control message", message_type=mtype
        )
    logger.debug(
        "control message of unknown type", extra={"protocol_type": _clip(mtype, 64)}
    )
    return UnknownMessage(type=mtype, raw=obj)


def parse_binary_header(header: Mapping[str, Any]) -> BinaryHeader | UnknownMessage:
    """Validate the header of a node-interface binary frame (as `decode_frame` gives it).

    Returns `PsdHeader` or `CaptureDataHeader` (unknown fields ignored), or
    `UnknownMessage` for a `type` this version does not define. Raises `ProtocolError`
    for a missing `type`, a control-message type, or missing or mistyped fields.
    The payload length is not checked here; `decode_psd` and `decode_capture_data` do.
    """
    if not isinstance(header, Mapping):
        raise ProtocolError("binary frame header: must be a JSON object")
    mtype = message_type(header, what="binary frame header")
    cls = _BINARY_TYPES.get(mtype)
    if cls is not None:
        return cls.from_wire(header)
    if mtype in _CONTROL_TYPES:
        raise ProtocolError(
            f"{mtype}: is a control message, not a binary-frame header", message_type=mtype
        )
    logger.debug(
        "binary frame of unknown type", extra={"protocol_type": _clip(mtype, 64)}
    )
    return UnknownMessage(type=mtype, raw=dict(header))


def dump_json(model: WireModel) -> str:
    """Compact JSON text of any wire model.

    No whitespace between tokens; integers (every node-interface `*_ns` time among
    them) as JSON integers of full precision; floats in the shortest form that reads
    back to the same double; None fields left out, except in the console models where
    they are written as null. `ConsolePsdHeader.t_ns` is written as a decimal string.
    """
    if not isinstance(model, WireModel):
        raise TypeError(f"expected a wire model, got {type(model).__name__}")
    return model.model_dump_json(exclude_none=type(model)._OMIT_NONE)


def dump_control(message: ControlMessage) -> str:
    """Text-frame content for a control message of the node interface (see `dump_json`).

    Raises `TypeError` for anything that is not one of the control messages, so that a
    binary-frame header cannot be sent as text by mistake.
    """
    if type(message) not in _CONTROL_CLASSES:
        raise TypeError(
            f"expected a control message, got {type(message).__name__}"
        )
    return dump_json(message)
