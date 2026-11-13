"""Tests of the wire protocol: PROTOCOL.md section A and the console spectrum frame.

Expected wire shapes (field names, byte layout) are written out here from PROTOCOL.md
rather than derived from the code under test, and binary payloads are checked against
`struct` packing, so that the encoders are compared with an independent construction.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import struct
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from petal_locate import protocol as P
from petal_locate.protocol import framing, messages
from petal_locate.protocol import (
    Assign,
    AssignAck,
    Capabilities,
    CaptureAssembler,
    CaptureCancel,
    CaptureDataHeader,
    CaptureErrorMessage,
    CaptureRequest,
    ChannelAck,
    ChannelAssignment,
    ClockStatus,
    ConsoleDetection,
    ConsolePsdHeader,
    ErrorMessage,
    Heartbeat,
    HeartbeatAck,
    Hello,
    MonitorAssignment,
    NodeLoad,
    ProtocolError,
    PsdHeader,
    Reject,
    SimUpdate,
    SimulationInfo,
    UnknownMessage,
    Welcome,
    decode_capture_data,
    decode_console_psd,
    decode_frame,
    decode_psd,
    dump_control,
    dump_json,
    encode_console_psd,
    encode_frame,
    encode_psd,
    iter_capture_chunks,
    parse_binary_header,
    parse_control,
)

# A 2026 timestamp with nanosecond detail. It is above 2**53 and is not a double:
# any float on its way would change it.
T_NS = 1_800_000_000_123_456_789
assert T_NS > 2**53 and int(float(T_NS)) != T_NS

SECRET = "tok-9f3c-SECRET"
CHUNK_SAMPLES = 32768  # 256 KiB of complex64

CLOCK = ClockStatus(source="gpsdo", locked=True, est_offset_ns=-12.5, holdover_s=0.0)
CLOCK_WIRE = {"source": "gpsdo", "locked": True, "est_offset_ns": -12.5, "holdover_s": 0.0}
MONITOR = MonitorAssignment(
    monitor_id="mon-1", center_hz=255.5e6, span_hz=31250.0, nfft=1024, navg=8, frame_rate_hz=2.0
)
CHANNEL = ChannelAssignment(channel_id="ch-a", antenna_summary="1.2m-parabolic", monitors=[MONITOR])
CAPABILITIES = Capabilities(
    min_hz=70e6, max_hz=6e9, max_sample_rate_hz=2e6, lookback_s=30.0, concurrent_reads=2
)

EXAMPLES: dict[str, Any] = {
    "hello": Hello(
        node_id="0b7c4f5e-1d2a-4c3b-9e8f-112233445566",
        site_id="honolulu",
        token=SECRET,
        software="petal-locate/0.1.0",
        driver="sim",
        clock=CLOCK,
        t_ns=T_NS,
    ),
    "welcome": Welcome(
        server_t_ns=T_NS + 1,
        heartbeat_s=2.5,
        config_version=7,
        channels=[CHANNEL],
        simulation=SimulationInfo(enabled=True, scenario_url="/api/v1/sim/world", version=3),
    ),
    "reject": Reject(reason="bad_token"),
    "assign": Assign(config_version=8, channels=[CHANNEL]),
    "assign_ack": AssignAck(
        config_version=8,
        ok=False,
        channels=[
            ChannelAck(channel_id="ch-a", ok=True, capabilities=CAPABILITIES),
            ChannelAck(channel_id="ch-b", ok=False, error="device-not-found", capabilities=CAPABILITIES),
        ],
    ),
    "heartbeat": Heartbeat(
        seq=41, t_ns=T_NS + 2, rtt_ms=3.25, clock=CLOCK, load=NodeLoad(cpu=0.37, queue=2)
    ),
    "heartbeat_ack": HeartbeatAck(seq=41, server_t_ns=T_NS + 3),
    "capture_request": CaptureRequest(
        capture_id="cap-1",
        channel_id="ch-a",
        t_start_ns=T_NS + 600_000_001,
        n_samples=262144,
        sample_rate_hz=31250.0,
        center_hz=255.5e6,
        deadline_ns=T_NS + 24_000_000_003,
    ),
    "capture_error": CaptureErrorMessage(
        capture_id="cap-1", channel_id="ch-a", code="too_late", message="interval-no-longer-buffered"
    ),
    "capture_cancel": CaptureCancel(capture_id="cap-1"),
    "sim_update": SimUpdate(version=4, effective_ns=T_NS + 2_000_000_000),
    "error": ErrorMessage.bad_message("heartbeat:seq:not-an-integer"),
}

# Field names straight from PROTOCOL.md section A.
SPEC_FIELDS: dict[str, set[str]] = {
    "hello": {"type", "protocol", "node_id", "site_id", "token", "software", "driver", "clock", "t_ns"},
    "welcome": {
        "type", "protocol", "server_t_ns", "heartbeat_s", "config_version", "channels", "simulation",
    },
    "reject": {"type", "reason"},
    "assign": {"type", "config_version", "channels"},
    "assign_ack": {"type", "config_version", "ok", "channels"},
    "heartbeat": {"type", "seq", "t_ns", "rtt_ms", "clock", "load"},
    "heartbeat_ack": {"type", "seq", "server_t_ns"},
    "capture_request": {
        "type", "capture_id", "channel_id", "t_start_ns", "n_samples", "sample_rate_hz", "center_hz",
        "deadline_ns",
    },
    "capture_error": {"type", "capture_id", "channel_id", "code", "message"},
    "capture_cancel": {"type", "capture_id"},
    "sim_update": {"type", "version", "effective_ns"},
    "error": {"type", "code", "detail"},
}
SPEC_CLOCK = {"source", "locked", "est_offset_ns", "holdover_s"}
SPEC_CHANNEL = {"channel_id", "antenna_summary", "monitors"}
SPEC_MONITOR = {"monitor_id", "center_hz", "span_hz", "nfft", "navg", "frame_rate_hz"}
SPEC_CAPABILITIES = {"min_hz", "max_hz", "max_sample_rate_hz", "lookback_s", "concurrent_reads"}
SPEC_PSD = {
    "type", "channel_id", "monitor_id", "t_ns", "center_hz", "span_hz", "nfft", "navg", "seq", "units",
}
SPEC_CAPTURE_DATA = {
    "type", "capture_id", "channel_id", "seq", "last", "t_first_ns", "sample_rate_hz", "center_hz",
    "n_samples_total", "offset", "n", "overflow", "clock",
}
SPEC_CONSOLE_PSD = {
    "type", "channel_id", "monitor_id", "t_ns", "center_hz", "span_hz", "nfft", "seq", "has_baseline",
    "detections",
}
SPEC_DETECTION = {"track_id", "f_lo_hz", "f_hi_hz", "authorized_carrier_id", "anomaly"}

_MISSING = object()


def wire(name: str) -> dict[str, Any]:
    """The example message `name` as the JSON object a peer would send."""
    return json.loads(dump_control(EXAMPLES[name]))


def altered(obj: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """A deep copy of `obj` with the dotted `path` set to `value` (or removed)."""
    out = copy.deepcopy(obj)
    node: Any = out
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if value is _MISSING:
        del node[leaf]
    else:
        node[leaf] = value
    return out


def parse_obj(obj: Any) -> Any:
    """`parse_control` of the JSON text of `obj`."""
    return parse_control(json.dumps(obj))


def manual_frame(header_json: bytes, payload: bytes = b"") -> bytes:
    """A PLB1 frame assembled by hand from PROTOCOL.md, independent of `encode_frame`."""
    return b"PLB1" + struct.pack("<I", len(header_json)) + header_json + payload


def header_json_of(frame: bytes) -> bytes:
    """The raw header bytes of a frame, read by hand."""
    (length,) = struct.unpack_from("<I", frame, 4)
    return frame[8 : 8 + length]


def noise(n: int, seed: int = 7) -> np.ndarray:
    """Deterministic complex64[n] test samples."""
    rng = np.random.default_rng(seed)
    out = np.empty(n, dtype=np.complex64)
    out.real = rng.standard_normal(n, dtype=np.float32)
    out.imag = rng.standard_normal(n, dtype=np.float32)
    return out


def chunk_header(seq: int, offset: int, n: int, total: int, **overrides: Any) -> dict[str, Any]:
    """A `capture_data` header dict written out by hand."""
    header: dict[str, Any] = {
        "type": "capture_data",
        "capture_id": "cap-1",
        "channel_id": "ch-a",
        "seq": seq,
        "last": offset + n == total,
        "t_first_ns": T_NS,
        "sample_rate_hz": 31250.0,
        "center_hz": 255.5e6,
        "n_samples_total": total,
        "offset": offset,
        "n": n,
        "overflow": False,
        "clock": dict(CLOCK_WIRE),
    }
    header.update(overrides)
    return header


def le_bytes(iq: np.ndarray) -> bytes:
    """Interleaved little-endian float32 I, Q of `iq`, packed with `struct`."""
    flat = np.stack([np.real(iq), np.imag(iq)], axis=1).reshape(-1).tolist()
    return struct.pack(f"<{len(flat)}f", *flat)


def chunks_of(iq: np.ndarray, per_chunk: int, **overrides: Any) -> list[tuple[dict[str, Any], bytes]]:
    """Hand-built (header, payload) chunks of `iq`, `per_chunk` samples each."""
    total = len(iq)
    out = []
    for seq, offset in enumerate(range(0, total, per_chunk)):
        part = iq[offset : offset + per_chunk]
        out.append((chunk_header(seq, offset, len(part), total, **overrides), le_bytes(part)))
    return out


# ============================================================ control messages


def test_examples_cover_every_message_of_section_a() -> None:
    assert set(EXAMPLES) == set(SPEC_FIELDS)
    assert {name: message.type for name, message in EXAMPLES.items()} == {n: n for n in SPEC_FIELDS}
    assert P.PROTOCOL_VERSION == 1
    assert EXAMPLES["hello"].protocol == 1 and EXAMPLES["welcome"].protocol == 1


@pytest.mark.parametrize("name", sorted(SPEC_FIELDS))
def test_control_message_round_trip(name: str) -> None:
    message = EXAMPLES[name]
    text = dump_control(message)
    parsed = parse_control(text)
    assert type(parsed) is type(message)
    assert not isinstance(parsed, UnknownMessage)
    assert parsed == message
    assert dump_control(parsed) == text
    # Compact: the examples hold no whitespace of their own, so none may appear.
    assert not re.search(r"\s", text)
    assert json.loads(text) == message.to_wire()
    assert message.to_json() == text


@pytest.mark.parametrize("name", sorted(SPEC_FIELDS))
def test_wire_fields_are_those_of_the_protocol_document(name: str) -> None:
    obj = wire(name)
    assert set(obj) == SPEC_FIELDS[name]
    assert obj["type"] == name


def test_embedded_types_have_the_documented_fields() -> None:
    assert set(wire("hello")["clock"]) == SPEC_CLOCK
    assert set(wire("heartbeat")["clock"]) == SPEC_CLOCK
    assert set(wire("heartbeat")["load"]) == {"cpu", "queue"}
    channel = wire("welcome")["channels"][0]
    assert set(channel) == SPEC_CHANNEL
    assert set(channel["monitors"][0]) == SPEC_MONITOR
    assert set(wire("assign")["channels"][0]) == SPEC_CHANNEL
    assert set(wire("welcome")["simulation"]) == {"enabled", "scenario_url", "version"}
    acks = wire("assign_ack")["channels"]
    assert set(acks[0]) == {"channel_id", "ok", "capabilities"}  # error? is absent when there is none
    assert set(acks[1]) == {"channel_id", "ok", "error", "capabilities"}
    assert set(acks[0]["capabilities"]) == SPEC_CAPABILITIES


def _ns_items(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every absolute-time field of a JSON value, as (path, value)."""
    found: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.endswith("_ns") and key != "est_offset_ns":
                found.append((prefix + key, value))
            found.extend(_ns_items(value, f"{prefix}{key}."))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(_ns_items(value, f"{prefix}{index}."))
    return found


def test_every_absolute_time_is_a_json_integer() -> None:
    seen: set[str] = set()
    for name, message in EXAMPLES.items():
        text = dump_control(message)
        for path, value in _ns_items(json.loads(text)):
            assert type(value) is int, (name, path)
            assert re.search(rf'"{path.split(".")[-1]}":{value}[,}}]', text), (name, path)
            seen.add(f"{name}.{path}")
    assert seen == {
        "hello.t_ns",
        "welcome.server_t_ns",
        "heartbeat.t_ns",
        "heartbeat_ack.server_t_ns",
        "capture_request.t_start_ns",
        "capture_request.deadline_ns",
        "sim_update.effective_ns",
    }


@pytest.mark.parametrize("t_ns", [2**53 + 1, T_NS, 2**63 - 1])
def test_nanoseconds_above_2_53_survive_exactly(t_ns: int) -> None:
    beat = Heartbeat(seq=0, t_ns=t_ns, clock=CLOCK, load=NodeLoad(cpu=0.0, queue=0))
    text = dump_control(beat)
    assert f'"t_ns":{t_ns}' in text
    back = parse_control(text)
    assert isinstance(back, Heartbeat)
    assert back.t_ns == t_ns and type(back.t_ns) is int

    ack = parse_control(dump_control(HeartbeatAck(seq=0, server_t_ns=t_ns)))
    assert isinstance(ack, HeartbeatAck) and ack.server_t_ns == t_ns
    request = CaptureRequest(
        capture_id="c", channel_id="ch", t_start_ns=t_ns - 1, n_samples=1, sample_rate_hz=1.0,
        center_hz=1.0, deadline_ns=t_ns,
    )
    back_request = parse_control(dump_control(request))
    assert isinstance(back_request, CaptureRequest)
    assert (back_request.t_start_ns, back_request.deadline_ns) == (t_ns - 1, t_ns)
    update = parse_control(dump_control(SimUpdate(version=1, effective_ns=t_ns)))
    assert isinstance(update, SimUpdate) and update.effective_ns == t_ns


@pytest.mark.parametrize(
    "bad",
    [1.8e18, float(T_NS), str(T_NS), True, -1, 2**63, 10**30, None, [T_NS]],
    ids=["float", "float-of-int", "string", "bool", "negative", "2^63", "10^30", "null", "array"],
)
def test_absolute_time_must_be_a_64_bit_integer(bad: Any) -> None:
    with pytest.raises(ProtocolError) as err:
        parse_obj(altered(wire("heartbeat"), "t_ns", bad))
    assert "t_ns" in err.value.detail
    with pytest.raises(ProtocolError, match="effective_ns"):
        parse_obj(altered(wire("sim_update"), "effective_ns", bad))


def test_unknown_fields_are_ignored_at_every_level() -> None:
    hello = wire("hello")
    hello["added_in_v2"] = {"anything": [1, 2, 3]}
    hello["clock"]["stratum"] = 1
    assert parse_obj(hello) == EXAMPLES["hello"]

    welcome = wire("welcome")
    welcome["motd"] = "hi"
    welcome["channels"][0]["polarization"] = "rhcp"
    welcome["channels"][0]["monitors"][0]["window"] = "hann"
    welcome["simulation"]["seed"] = 1
    assert parse_obj(welcome) == EXAMPLES["welcome"]

    ack = wire("assign_ack")
    ack["channels"][0]["capabilities"]["gain_db"] = 30
    assert parse_obj(ack) == EXAMPLES["assign_ack"]


def test_unknown_type_is_surfaced_with_the_raw_object(caplog: pytest.LogCaptureFixture) -> None:
    raw = {"type": "telemetry_v2", "t_ns": T_NS, "nested": {"a": [1, 2.5, None]}}
    with caplog.at_level(logging.DEBUG, logger="petal_locate.protocol.messages"):
        message = parse_obj(raw)
    assert isinstance(message, UnknownMessage)
    assert message.type == "telemetry_v2"
    assert message.raw == raw and message.raw["t_ns"] == T_NS
    # Types are case-sensitive: "Hello" is not "hello".
    assert isinstance(parse_obj(altered(wire("hello"), "type", "Hello")), UnknownMessage)
    records = [r for r in caplog.records if r.name == "petal_locate.protocol.messages"]
    assert [getattr(r, "protocol_type", None) for r in records] == ["telemetry_v2"]


def test_unknown_reasons_and_codes_parse_but_cannot_be_built() -> None:
    reject = parse_obj({"type": "reject", "reason": "maintenance"})
    assert isinstance(reject, Reject) and reject.reason == "maintenance"
    assert reject.reason not in P.REJECT_REASONS
    error = parse_obj(altered(wire("capture_error"), "code", "thermal"))
    assert isinstance(error, CaptureErrorMessage) and error.code == "thermal"

    with pytest.raises(ValueError, match="unknown reject reason"):
        Reject(reason="bad-token")
    with pytest.raises(ValueError, match="unknown capture error code"):
        CaptureErrorMessage(capture_id="c", channel_id="ch", code="toolate", message="")
    for reason in ("bad_token", "unknown_site", "protocol_mismatch", "duplicate_node"):
        assert Reject(reason=reason).reason == reason
    for code in ("out_of_range", "too_late", "overflow", "driver", "busy", "cancelled"):
        assert CaptureErrorMessage(capture_id="c", channel_id="ch", code=code).code == code
    assert set(P.REJECT_REASONS) == {"bad_token", "unknown_site", "protocol_mismatch", "duplicate_node"}
    assert set(P.CAPTURE_ERROR_CODES) == {
        "out_of_range", "too_late", "overflow", "driver", "busy", "cancelled",
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"type":"heartbeat",',
        "[1,2]",
        '"hello"',
        "42",
        "null",
        "{}",
        '{"type":5}',
        '{"type":""}',
        '{"type":null}',
        '{"type":"heartbeat","seq":NaN}',
        '{"type":"heartbeat","seq":Infinity}',
        '{"type":"x","v":1e999}',
        '{"type":"x","v":' + "[" * 100_000 + "]" * 100_000 + "}",
        '{"type":"x","v":' + "9" * 5000 + "}",
        '{"type":"psd","channel_id":"ch-a"}',
        '{"type":"capture_data"}',
    ],
    ids=[
        "empty", "garbage", "truncated", "array", "string", "number", "null", "no-type", "type-number",
        "type-empty", "type-null", "nan", "infinity", "float-overflow", "too-deep", "huge-integer",
        "psd-as-text", "capture-data-as-text",
    ],
)
def test_malformed_control_text_raises_protocol_error(text: str) -> None:
    with pytest.raises(ProtocolError) as err:
        parse_control(text)
    assert err.value.detail and len(err.value.detail) <= P.MAX_ERROR_DETAIL_CHARS
    assert str(err.value) == err.value.detail
    assert isinstance(err.value, ValueError)
    reply = err.value.to_message()
    assert reply.code == "bad_message" and reply.detail == err.value.detail
    assert parse_control(dump_control(reply)) == reply


def test_control_message_must_be_text() -> None:
    with pytest.raises(ProtocolError, match="text frame"):
        parse_control(dump_control(EXAMPLES["reject"]).encode())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [
        ("hello", "node_id", _MISSING),
        ("hello", "node_id", ""),
        ("hello", "site_id", 5),
        ("hello", "token", None),
        ("hello", "protocol", "1"),
        ("hello", "protocol", 1.0),
        ("hello", "clock", "gpsdo"),
        ("hello", "clock.source", "atomic"),
        ("hello", "clock.locked", 1),
        ("hello", "clock.locked", "true"),
        ("hello", "clock.est_offset_ns", "3"),
        ("hello", "clock.holdover_s", -1.0),
        ("welcome", "heartbeat_s", 0),
        ("welcome", "heartbeat_s", "2"),
        ("welcome", "config_version", 7.0),
        ("welcome", "channels", {}),
        ("welcome", "channels.0.channel_id", ""),
        ("welcome", "channels.0.antenna_summary", {"type": "parabolic"}),
        ("welcome", "channels.0.monitors.0.nfft", 1024.0),
        ("welcome", "channels.0.monitors.0.nfft", 1),
        ("welcome", "channels.0.monitors.0.nfft", P.MAX_PSD_BINS + 1),
        ("welcome", "channels.0.monitors.0.navg", 0),
        ("welcome", "channels.0.monitors.0.span_hz", 0.0),
        ("welcome", "channels.0.monitors.0.frame_rate_hz", True),
        ("welcome", "simulation.enabled", "yes"),
        ("welcome", "simulation.version", -1),
        ("reject", "reason", ""),
        ("reject", "reason", 3),
        ("assign", "channels", _MISSING),
        ("assign_ack", "ok", "true"),
        ("assign_ack", "channels.0.capabilities.concurrent_reads", 0),
        ("assign_ack", "channels.0.capabilities.concurrent_reads", 2.0),
        ("assign_ack", "channels.0.capabilities.max_sample_rate_hz", 0),
        ("assign_ack", "channels.0.capabilities.lookback_s", -1),
        ("heartbeat", "seq", "41"),
        ("heartbeat", "seq", -1),
        ("heartbeat", "seq", True),
        ("heartbeat", "rtt_ms", -0.5),
        ("heartbeat", "rtt_ms", "3"),
        ("heartbeat", "load", _MISSING),
        ("heartbeat", "load.cpu", "0.3"),
        ("heartbeat", "load.queue", 1.5),
        ("heartbeat_ack", "seq", None),
        ("capture_request", "capture_id", 17),
        ("capture_request", "n_samples", 0),
        ("capture_request", "n_samples", 262144.0),
        ("capture_request", "n_samples", P.MAX_CAPTURE_SAMPLES + 1),
        ("capture_request", "sample_rate_hz", "31250"),
        ("capture_request", "sample_rate_hz", 0),
        ("capture_request", "center_hz", -1.0),
        ("capture_error", "code", None),
        ("capture_error", "message", 5),
        ("capture_cancel", "capture_id", _MISSING),
        ("sim_update", "version", "4"),
        ("error", "detail", ["x"]),
    ],
)
def test_bad_field_of_a_known_message_is_named(name: str, path: str, value: Any) -> None:
    with pytest.raises(ProtocolError) as err:
        parse_obj(altered(wire(name), path, value))
    assert err.value.message_type == name
    assert err.value.detail.startswith(f"{name}: ")
    assert path in err.value.detail


def test_error_detail_and_repr_never_quote_the_token() -> None:
    broken = altered(wire("hello"), "t_ns", "soon")
    del broken["clock"]
    broken["site_id"] = 12345
    with pytest.raises(ProtocolError) as err:
        parse_obj(broken)
    assert {"t_ns", "clock", "site_id"} <= set(re.findall(r"(\w+):", err.value.detail))
    for text in (err.value.detail, str(err.value), repr(err.value)):
        assert SECRET not in text and "12345" not in text and "soon" not in text
    # The pydantic error (whose text quotes inputs) must not be chained into tracebacks.
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert SECRET not in repr(EXAMPLES["hello"]) and SECRET not in str(EXAMPLES["hello"])
    # The token itself still travels.
    assert json.loads(dump_control(EXAMPLES["hello"]))["token"] == SECRET
    # A wrong token type is reported without echoing it.
    with pytest.raises(ProtocolError) as err2:
        parse_obj(altered(wire("hello"), "token", 987654321))
    assert "token" in err2.value.detail and "987654321" not in err2.value.detail


def test_empty_token_parses_so_the_server_can_answer_bad_token() -> None:
    hello = parse_obj(altered(wire("hello"), "token", ""))
    assert isinstance(hello, Hello) and hello.token == ""


def test_hello_of_another_protocol_version_parses_for_protocol_mismatch() -> None:
    hello = parse_obj(altered(wire("hello"), "protocol", 2))
    assert isinstance(hello, Hello) and hello.protocol == 2


def test_json_integers_are_accepted_where_numbers_are_expected() -> None:
    with_integers = altered(wire("capture_request"), "sample_rate_hz", 2_000_000)
    request = parse_obj(altered(with_integers, "center_hz", 255_500_000))
    assert isinstance(request, CaptureRequest)
    assert request.sample_rate_hz == 2e6 and type(request.sample_rate_hz) is float
    assert request.center_hz == 255.5e6 and type(request.center_hz) is float


@pytest.mark.parametrize(
    "value", [0.1 + 0.2, 1 / 3, 2.4e9 + 0.1, 1e-7, 5e-324, 1.7976931348623157e308, 123456789.123456789]
)
def test_floats_round_trip_to_the_same_double(value: float) -> None:
    clock = ClockStatus(source="ntp", locked=False, est_offset_ns=-value, holdover_s=value)
    beat = Heartbeat(seq=1, t_ns=T_NS, clock=clock, load=NodeLoad(cpu=value, queue=0))
    back = parse_control(dump_control(beat))
    assert isinstance(back, Heartbeat)
    assert back.clock.est_offset_ns == -value and back.clock.holdover_s == value and back.load.cpu == value


def test_non_finite_numbers_are_refused_when_building() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError, match="est_offset_ns"):
            ClockStatus(source="free", locked=False, est_offset_ns=bad, holdover_s=0.0)
        with pytest.raises(ValidationError, match="center_hz"):
            MONITOR.model_validate({**MONITOR.model_dump(), "center_hz": bad})


def test_building_refuses_unknown_keywords_and_wrong_types() -> None:
    with pytest.raises(ValueError, match="heartbeat"):
        Welcome(server_t_ns=T_NS, config_version=1, channels=[], heartbeat=5.0)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="rtt"):
        Heartbeat(seq=0, t_ns=T_NS, rtt=1.0, clock=CLOCK, load=NodeLoad(cpu=0.0, queue=0))  # type: ignore[call-arg]
    newer_clock: Any = {**CLOCK_WIRE, "stratum": 1}
    with pytest.raises(ValueError, match="stratum"):
        Heartbeat(seq=0, t_ns=T_NS, clock=newer_clock, load=NodeLoad(cpu=0.0, queue=0))
    with pytest.raises(ValueError, match="stratum"):
        ClockStatus.model_validate({**CLOCK_WIRE, "stratum": 1})
    assert ClockStatus.from_wire({**CLOCK_WIRE, "stratum": 1}) == CLOCK
    with pytest.raises(ValidationError, match="t_ns"):
        HeartbeatAck(seq=0, server_t_ns=np.int64(T_NS))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="t_ns"):
        HeartbeatAck(seq=0, server_t_ns=float(T_NS))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="seq"):
        HeartbeatAck(seq=True, server_t_ns=T_NS)
    with pytest.raises(ProtocolError, match="JSON object"):
        ClockStatus.from_wire([1, 2])  # type: ignore[arg-type]


def test_models_are_immutable() -> None:
    with pytest.raises(ValidationError):
        EXAMPLES["heartbeat"].seq = 0
    with pytest.raises(ValidationError):
        CLOCK.locked = False  # type: ignore[misc]
    changed = EXAMPLES["heartbeat"].model_copy(update={"seq": 42})
    assert changed.seq == 42 and EXAMPLES["heartbeat"].seq == 41


def test_defaults_and_optional_fields() -> None:
    welcome = parse_obj({"type": "welcome", "protocol": 1, "server_t_ns": T_NS, "config_version": 0, "channels": []})
    assert isinstance(welcome, Welcome)
    assert welcome.heartbeat_s == 2.0 == P.DEFAULT_HEARTBEAT_S
    assert welcome.simulation == SimulationInfo(enabled=False)
    assert wire("welcome")["simulation"]["scenario_url"] == "/api/v1/sim/world"
    assert json.loads(dump_control(welcome))["simulation"] == {"enabled": False}

    first = Heartbeat(seq=0, t_ns=T_NS, clock=CLOCK, load=NodeLoad(cpu=0.0, queue=0))
    assert first.rtt_ms is None
    assert "rtt_ms" not in json.loads(dump_control(first))
    assert parse_control(dump_control(first)) == first
    assert parse_obj({**json.loads(dump_control(first)), "rtt_ms": None}) == first

    failed = ChannelAck(channel_id="ch-b", ok=False, error="no device")
    assert json.loads(dump_json(failed)) == {"channel_id": "ch-b", "ok": False, "error": "no device"}
    ack = parse_obj(
        {"type": "assign_ack", "config_version": 1, "ok": False, "channels": [{"channel_id": "ch-b", "ok": False}]}
    )
    assert isinstance(ack, AssignAck) and ack.channels[0].capabilities is None and ack.channels[0].error is None

    error = parse_obj({"type": "capture_error", "capture_id": "c", "channel_id": "ch", "code": "busy"})
    assert isinstance(error, CaptureErrorMessage) and error.message == ""
    assert ErrorMessage().code == "bad_message"


def test_cross_field_rules() -> None:
    with pytest.raises(ProtocolError, match="capabilities is required when ok"):
        parse_obj(
            {"type": "assign_ack", "config_version": 1, "ok": True, "channels": [{"channel_id": "ch-a", "ok": True}]}
        )
    with pytest.raises(ProtocolError, match="deadline_ns must not be before t_start_ns"):
        parse_obj(altered(wire("capture_request"), "deadline_ns", T_NS))
    with pytest.raises(ProtocolError, match="max_hz must not be below min_hz"):
        parse_obj(altered(wire("assign_ack"), "channels.0.capabilities.max_hz", 1.0))
    twice = wire("welcome")
    twice["channels"].append(copy.deepcopy(twice["channels"][0]))
    with pytest.raises(ProtocolError, match="channels repeats a channel_id"):
        parse_obj(twice)
    assign_twice = wire("assign")
    assign_twice["channels"].append(copy.deepcopy(assign_twice["channels"][0]))
    with pytest.raises(ProtocolError, match="channels repeats a channel_id"):
        parse_obj(assign_twice)
    with pytest.raises(ProtocolError, match="channels repeats a channel_id"):
        parse_obj(altered(wire("assign_ack"), "channels.1.channel_id", "ch-a"))
    monitors_twice = wire("assign")
    monitors_twice["channels"][0]["monitors"].append(copy.deepcopy(monitors_twice["channels"][0]["monitors"][0]))
    with pytest.raises(ProtocolError, match="monitors repeats a monitor_id"):
        parse_obj(monitors_twice)
    # The largest capture the server accepts can be requested; one sample more cannot.
    assert CaptureRequest(
        capture_id="c", channel_id="ch", t_start_ns=1, n_samples=P.MAX_CAPTURE_SAMPLES, sample_rate_hz=1.0,
        center_hz=0.0, deadline_ns=1,
    ).n_samples * 8 == 64 * 1024 * 1024 == P.MAX_CAPTURE_BYTES


def test_long_error_detail_is_clipped() -> None:
    error = ProtocolError("x" * 5000)
    assert len(error.detail) == P.MAX_ERROR_DETAIL_CHARS == 512 and error.detail.endswith("...")
    assert len(ErrorMessage.bad_message("y" * 5000).detail) == 512
    assert ProtocolError("short").detail == "short"
    # A detail built from peer data is clipped too: 400 bad fields cannot produce a huge reply.
    many = wire("welcome")
    many["channels"] = [{"channel_id": 5}] * 400
    with pytest.raises(ProtocolError) as err:
        parse_obj(many)
    assert len(err.value.detail) == 512 and err.value.detail.startswith("welcome: channels.0.channel_id")


def test_identifiers_are_bounded() -> None:
    assert isinstance(parse_obj(altered(wire("hello"), "site_id", "s" * P.MAX_ID_CHARS)), Hello)
    with pytest.raises(ProtocolError, match="site_id"):
        parse_obj(altered(wire("hello"), "site_id", "s" * (P.MAX_ID_CHARS + 1)))


def test_lone_surrogate_in_an_identifier_is_refused() -> None:
    with pytest.raises(ProtocolError, match="site_id"):
        parse_control(json.dumps(altered(wire("hello"), "site_id", "\ud800")))


def test_dump_control_takes_control_messages_only() -> None:
    header = PsdHeader(channel_id="ch-a", monitor_id="m", t_ns=T_NS, center_hz=1.0, span_hz=1.0, nfft=2, navg=1, seq=0)
    with pytest.raises(TypeError, match="control message"):
        dump_control(header)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="control message"):
        dump_control(CLOCK)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="wire model"):
        dump_json({"type": "hello"})  # type: ignore[arg-type]
    assert json.loads(dump_json(header))["t_ns"] == T_NS


def test_parse_binary_header() -> None:
    psd = {
        "type": "psd", "channel_id": "ch-a", "monitor_id": "mon-1", "t_ns": T_NS, "center_hz": 255.5e6,
        "span_hz": 31250.0, "nfft": 1024, "navg": 8, "seq": 3, "units": "dB", "future": 1,
    }
    header = parse_binary_header(psd)
    assert isinstance(header, PsdHeader) and header.t_ns == T_NS and header.nfft == 1024
    assert set(json.loads(dump_json(header))) == SPEC_PSD
    chunk = parse_binary_header(chunk_header(0, 0, 4, 4))
    assert isinstance(chunk, CaptureDataHeader) and chunk.last and chunk.clock == CLOCK
    assert set(json.loads(dump_json(chunk))) == SPEC_CAPTURE_DATA
    unknown = parse_binary_header({"type": "waterfall", "rows": 4})
    assert unknown == UnknownMessage(type="waterfall", raw={"type": "waterfall", "rows": 4})
    with pytest.raises(ProtocolError, match="control message, not a binary-frame header"):
        parse_binary_header(wire("heartbeat"))
    with pytest.raises(ProtocolError, match="missing 'type'"):
        parse_binary_header({"nfft": 4})
    with pytest.raises(ProtocolError, match="units"):
        parse_binary_header({**psd, "units": "dBm"})
    with pytest.raises(ProtocolError, match="t_ns"):
        parse_binary_header({**psd, "t_ns": str(T_NS)})
    with pytest.raises(ProtocolError, match="JSON object"):
        parse_binary_header(["psd"])  # type: ignore[arg-type]


def test_package_re_exports_both_modules() -> None:
    assert set(P.__all__) == set(messages.__all__) | set(framing.__all__)
    for name in P.__all__:
        source = messages if name in messages.__all__ else framing
        assert getattr(P, name) is getattr(source, name), name


# ====================================================================== PLB1 frames


def test_frame_layout_is_that_of_the_protocol_document() -> None:
    header = {"type": "psd", "t_ns": T_NS, "name": "Hafen-Süd", "n": [1, 2.5, None, True]}
    payload = bytes(range(256)) * 3
    frame = encode_frame(header, payload)
    assert isinstance(frame, bytes)
    assert frame[:4] == b"PLB1"
    (length,) = struct.unpack("<I", frame[4:8])
    assert frame[4:8] == length.to_bytes(4, "little")
    raw_header = frame[8 : 8 + length]
    assert json.loads(raw_header.decode("utf-8")) == header
    assert frame[8 + length :] == payload
    # Compact JSON, integers as integers, then only alignment padding.
    assert raw_header.rstrip(b" ") == json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert f'"t_ns":{T_NS}'.encode() in raw_header
    assert (8 + length) % 8 == 0 and len(raw_header) - len(raw_header.rstrip(b" ")) < 8

    # A frame written by hand from the document decodes, padded or not.
    by_hand = manual_frame(b'{"type":"psd","t_ns":%d}' % T_NS, payload)
    decoded, view = decode_frame(by_hand)
    assert decoded == {"type": "psd", "t_ns": T_NS} and bytes(view) == payload
    decoded2, view2 = decode_frame(frame)
    assert decoded2 == header and bytes(view2) == payload


@pytest.mark.parametrize("extra", range(8))
def test_payload_always_starts_on_an_8_byte_boundary(extra: int) -> None:
    frame = encode_frame({"type": "x", "pad": "p" * extra}, b"\x01\x02")
    (length,) = struct.unpack_from("<I", frame, 4)
    assert (8 + length) % 8 == 0
    header, payload = decode_frame(frame)
    assert header == {"type": "x", "pad": "p" * extra} and bytes(payload) == b"\x01\x02"


def test_decode_frame_does_not_copy_the_payload() -> None:
    frame = encode_frame({"type": "x"}, b"abcdefgh")
    _, payload = decode_frame(frame)
    assert isinstance(payload, memoryview) and payload.obj is frame and payload.readonly
    assert payload.format == "B" and payload.ndim == 1

    mutable = bytearray(frame)
    _, live = decode_frame(mutable)
    mutable[-1] = ord("Z")
    assert bytes(live) == b"abcdefgZ" and not live.readonly
    _, from_view = decode_frame(memoryview(frame))
    assert bytes(from_view) == b"abcdefgh"
    _, empty = decode_frame(encode_frame({"type": "x"}))
    assert len(empty) == 0


def test_encode_frame_accepts_models_and_any_contiguous_buffer() -> None:
    header = PsdHeader(channel_id="ch-a", monitor_id="m", t_ns=T_NS, center_hz=1.0, span_hz=2.0, nfft=2, navg=1, seq=0)
    values = np.array([1.5, -2.25], dtype="<f4")
    for payload in (values.tobytes(), bytearray(values.tobytes()), memoryview(values), memoryview(values.tobytes())):
        decoded, view = decode_frame(encode_frame(header, payload))
        assert decoded == json.loads(dump_json(header))
        assert bytes(view) == struct.pack("<2f", 1.5, -2.25)
    complex_view = memoryview(np.array([1 + 2j], dtype="<c8"))
    assert bytes(decode_frame(encode_frame({"type": "x"}, complex_view))[1]) == struct.pack("<2f", 1.0, 2.0)
    # A raw payload is taken byte for byte: making it little-endian is the caller's business.
    big_endian = memoryview(np.array([[1.5, -2.25]], dtype=">f4"))
    assert bytes(decode_frame(encode_frame({"type": "x"}, big_endian))[1]) == struct.pack(">2f", 1.5, -2.25)


_OK_HEADER = b'{"type":"x"}'


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"", "truncated"),
        (b"PLB1\x00\x00\x00", "truncated"),
        (b"PLB2" + manual_frame(_OK_HEADER)[4:], "bad magic"),
        (b"plb1" + manual_frame(_OK_HEADER)[4:], "bad magic"),
        (b"\x00\x00\x00\x00" + manual_frame(_OK_HEADER)[4:], "bad magic"),
        (b"PLB1" + struct.pack("<I", 65537) + _OK_HEADER, "exceeds the limit"),
        (b"PLB1" + struct.pack("<I", 65537) + _OK_HEADER + b" " * (65537 - len(_OK_HEADER)), "exceeds the limit"),
        (b"PLB1" + struct.pack("<I", 0xFFFFFFFF) + _OK_HEADER, "exceeds the limit"),
        (b"PLB1" + struct.pack(">I", len(_OK_HEADER)) + _OK_HEADER, "exceeds the limit"),
        (b"PLB1" + struct.pack("<I", len(_OK_HEADER) + 1) + _OK_HEADER, "truncated header"),
        (b"PLB1" + struct.pack("<I", 200) + _OK_HEADER, "truncated header"),
        (manual_frame(b""), "invalid JSON"),
        (manual_frame(b"   "), "invalid JSON"),
        (manual_frame(b'{"type":"x"'), "invalid JSON"),
        (manual_frame(b'{"type":"x","v":NaN}'), "invalid JSON"),
        (manual_frame(b'{"type":"x","v":1e999}'), "invalid JSON"),
        (manual_frame(b'{"type":"x"}{"type":"y"}'), "invalid JSON"),
        (manual_frame(b'{"type":"\xff\xfe"}'), "UTF-8"),
        (manual_frame(b"[]"), "JSON object, got an array"),
        (manual_frame(b'["type","x"]'), "JSON object, got an array"),
        (manual_frame(b'"type"'), "JSON object, got a string"),
        (manual_frame(b"17"), "JSON object, got a number"),
        (manual_frame(b"null"), "JSON object, got null"),
        (manual_frame(b"true"), "JSON object, got a boolean"),
        (manual_frame(b"{}"), "missing 'type'"),
        (manual_frame(b'{"Type":"x"}'), "missing 'type'"),
        (manual_frame(b'{"type":7}'), "non-empty string"),
        (manual_frame(b'{"type":""}'), "non-empty string"),
        (manual_frame(b'{"type":null}'), "non-empty string"),
    ],
    ids=[
        "empty", "seven-bytes", "magic-PLB2", "magic-lowercase", "magic-zero", "header-65537-absent",
        "header-65537-present", "header-4GiB", "length-big-endian", "header-one-byte-short",
        "header-mostly-missing", "header-empty", "header-blank", "json-truncated", "json-nan",
        "json-float-overflow", "json-two-objects", "not-utf8", "array-empty", "array", "string", "number",
        "null", "boolean", "type-missing", "type-wrong-case", "type-number", "type-empty", "type-null",
    ],
)
def test_decode_frame_rejections(data: bytes, match: str) -> None:
    with pytest.raises(ProtocolError, match=match):
        decode_frame(data)
    with pytest.raises(ProtocolError, match=match):
        decode_frame(memoryview(bytearray(data)))


def test_header_limit_is_exactly_64_kib() -> None:
    assert P.MAX_HEADER_BYTES == 64 * 1024
    at_limit = _OK_HEADER + b" " * (65536 - len(_OK_HEADER))
    header, payload = decode_frame(manual_frame(at_limit, b"\x07"))
    assert header == {"type": "x"} and bytes(payload) == b"\x07"
    with pytest.raises(ProtocolError, match="65537 bytes exceeds the limit"):
        decode_frame(manual_frame(at_limit + b" ", b"\x07"))

    # The encoder refuses to build what a receiver would reject, and can reach the limit.
    empty = len(json.dumps({"type": "x", "v": ""}, separators=(",", ":")))
    assert len(header_json_of(encode_frame({"type": "x", "v": "a" * (65536 - empty)}))) == 65536
    with pytest.raises(ValueError, match="exceeds the limit"):
        encode_frame({"type": "x", "v": "a" * (65536 - empty + 1)})


def test_decode_frame_refuses_text() -> None:
    with pytest.raises(ProtocolError, match="expected bytes, got text"):
        decode_frame('{"type":"psd"}')  # type: ignore[arg-type]
    with pytest.raises(ProtocolError, match="bytes-like"):
        decode_frame(12345)  # type: ignore[arg-type]
    with pytest.raises(ProtocolError, match="C-contiguous"):
        decode_frame(memoryview(encode_frame({"type": "x"}, b"12345678"))[::2])


@pytest.mark.parametrize(
    ("header", "payload", "match"),
    [
        ({}, b"", "'type' must be a non-empty string"),
        ({"type": 3}, b"", "'type' must be a non-empty string"),
        ({"type": ""}, b"", "'type' must be a non-empty string"),
        ({"type": "x", "v": float("nan")}, b"", "cannot be written as JSON"),
        ({"type": "x", "v": float("inf")}, b"", "cannot be written as JSON"),
        ({"type": "x", "v": {1, 2}}, b"", "cannot be written as JSON"),
        ({"type": "x", "v": np.float32(1.0)}, b"", "cannot be written as JSON"),
        ({"type": "x", "v": "\ud800"}, b"", "cannot be written as UTF-8"),
        ([("type", "x")], b"", "must be a mapping or a wire model"),
        ('{"type":"x"}', b"", "must be a mapping or a wire model"),
        (CLOCK, b"", "has no 'type' field"),
        ({"type": "x"}, "text", "expected bytes, got text"),
        ({"type": "x"}, 5, "bytes-like"),
        ({"type": "x"}, memoryview(b"12345678")[::2], "C-contiguous"),
        ({"type": "x"}, memoryview(np.zeros((0, 4), dtype=np.float32)), "cannot be viewed as bytes"),
    ],
    ids=[
        "no-type", "type-number", "type-empty", "nan", "inf", "set", "numpy-scalar", "surrogate", "list", "string",
        "model-without-type", "text-payload", "int-payload", "strided-payload", "empty-2d-payload",
    ],
)
def test_encode_frame_rejections(header: Any, payload: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match) as err:
        encode_frame(header, payload)
    assert not isinstance(err.value, ProtocolError)  # our own mistake, not the peer's


# ================================================================== spectrum frames


def _psd_values(nfft: int = 64) -> np.ndarray:
    values = np.linspace(-131.7, 12.3, nfft).astype(np.float32)
    f32 = np.finfo(np.float32)
    values[:5] = np.array([0.0, -0.0, f32.max, f32.tiny / 4, -300.0], dtype=np.float32)  # zero signs, subnormal
    return values


def _psd_frame(values: np.ndarray, **overrides: Any) -> bytes:
    args: dict[str, Any] = {
        "channel_id": "ch-a", "monitor_id": "mon-1", "t_ns": T_NS, "center_hz": 255.5e6, "span_hz": 31250.0,
        "nfft": len(values), "navg": 8, "seq": 12, "psd_db": values,
    }
    args.update(overrides)
    return encode_psd(**args)


def test_psd_round_trip_is_bit_exact_and_zero_copy() -> None:
    values = _psd_values()
    frame = _psd_frame(values)
    raw_header, payload = decode_frame(frame)
    assert set(raw_header) == SPEC_PSD
    assert raw_header == {
        "type": "psd", "channel_id": "ch-a", "monitor_id": "mon-1", "t_ns": T_NS, "center_hz": 255.5e6,
        "span_hz": 31250.0, "nfft": 64, "navg": 8, "seq": 12, "units": "dB",
    }
    assert type(raw_header["t_ns"]) is int
    # float32 little-endian, checked against struct.
    assert bytes(payload) == struct.pack("<64f", *values.tolist())

    header, psd = decode_psd(raw_header, payload)
    assert isinstance(header, PsdHeader) and header.t_ns == T_NS and header.nfft == 64
    assert psd.dtype == np.float32 and psd.shape == (64,)
    assert psd.tobytes() == values.tobytes()  # bit-exact, signed zero and subnormal included
    # The array is a view of the received frame: no copy, read-only, aligned.
    assert np.shares_memory(psd, np.frombuffer(frame, dtype=np.uint8))
    assert not psd.flags.writeable and psd.flags.aligned
    assert parse_binary_header(raw_header) == header
    again, _ = decode_psd(header, payload)
    assert again is header


def test_largest_frames_fit_one_websocket_message() -> None:
    """A node frame that the transport would refuse must not be constructible."""
    assert P.WS_MAX_SIZE_BYTES == 1024 * 1024
    long_id = "x" * P.MAX_ID_CHARS
    widest = np.full(P.MAX_PSD_BINS, -123.456, dtype=np.float32)
    frame = encode_psd(long_id, long_id, 2**63 - 1, 1e11, 1e9, P.MAX_PSD_BINS, 1000, 2**40, widest)
    assert len(frame) < P.WS_MAX_SIZE_BYTES
    header, psd = decode_psd(*decode_frame(frame))
    assert header.nfft == P.MAX_PSD_BINS == 131072
    assert psd.tobytes() == widest.tobytes()
    with pytest.raises(ValidationError, match="nfft"):
        encode_psd("c", "m", 0, 1.0, 1.0, P.MAX_PSD_BINS + 1, 1, 0, np.zeros(P.MAX_PSD_BINS + 1, dtype=np.float32))
    with pytest.raises(ProtocolError, match="nfft"):
        decode_psd(altered(decode_frame(frame)[0], "nfft", P.MAX_PSD_BINS + 1), b"")
    assert MonitorAssignment(**{**MONITOR.model_dump(), "nfft": P.MAX_PSD_BINS}).nfft == 131072
    with pytest.raises(ValidationError, match="nfft"):
        MonitorAssignment(**{**MONITOR.model_dump(), "nfft": P.MAX_PSD_BINS + 1})

    slow_clock = ClockStatus(source="free", locked=False, est_offset_ns=-1.5e15, holdover_s=1e9)
    chunk = next(
        iter_capture_chunks(long_id, long_id, noise(CHUNK_SAMPLES + 1), 2**63 - 1, 1e9, 1e11, True, slow_clock)
    )
    assert 256 * 1024 < len(chunk) < P.WS_MAX_SIZE_BYTES


def test_protocol_constants_are_those_of_the_document() -> None:
    assert (P.DEFAULT_HEARTBEAT_S, P.HEARTBEAT_MISS_LIMIT, P.BAD_MESSAGE_LIMIT) == (2.0, 3, 3)
    assert (P.CLOSE_CODE_SHUTDOWN, P.CLOSE_CODE_REPLACED) == (1001, 4001)
    assert (P.RECONNECT_BACKOFF_MIN_S, P.RECONNECT_BACKOFF_MAX_S, P.RECONNECT_JITTER) == (1.0, 30.0, 0.2)
    assert P.MAX_CONCURRENT_CAPTURES == 8 and P.MAX_CAPTURE_BYTES == 64 * 1024 * 1024
    assert P.MAX_CAPTURE_SAMPLES == 8_388_608 and P.MAX_CHUNK_PAYLOAD_BYTES == 256 * 1024 == 8 * CHUNK_SAMPLES
    assert P.MAX_HEADER_BYTES == 64 * 1024 and P.MAGIC == b"PLB1" and P.FRAME_PREFIX_BYTES == 8
    assert P.SIM_UPDATE_LEAD_NS == 2 * 10**9 and P.ERROR_BAD_MESSAGE == "bad_message"


def test_psd_is_little_endian_whatever_the_input_byte_order() -> None:
    values = _psd_values()
    big = values.astype(">f4")
    assert big.tobytes() != values.astype("<f4").tobytes()
    assert _psd_frame(big) == _psd_frame(values.astype("<f4")) == _psd_frame(values)
    strided = np.repeat(values, 2)[::2]
    assert not strided.flags.c_contiguous
    assert _psd_frame(strided) == _psd_frame(values)


def test_payload_of_the_other_byte_order_is_converted_to_native() -> None:
    """What a big-endian host does with a little-endian payload, mirrored on this host."""
    foreign = np.dtype(">f4") if np.dtype("<f4").isnative else np.dtype("<f4")
    values = [1.5, -2.25, 2.0**100, 0.0]  # exact in float32
    payload = np.array(values, dtype=foreign).tobytes()
    assert payload != np.array(values, dtype=np.float32).tobytes()
    decoded = framing._array_from_payload(memoryview(payload), foreign)
    assert decoded.dtype.isnative and decoded.dtype == np.float32 and decoded.tolist() == values
    foreign_complex = np.dtype(foreign.byteorder + "c8")
    assert framing._all_finite(np.array([1 + 2j, 3 - 4j], dtype=foreign_complex))
    assert not framing._all_finite(np.array([1 + 2j, complex(0, np.nan)], dtype=foreign_complex))
    assert not framing._all_finite(np.array([1.0, np.inf], dtype=foreign))


@pytest.mark.parametrize("delta", [-256, -4, -1, 1, 4, 256], ids=lambda d: f"{d:+d}-bytes")
def test_psd_payload_length_must_match_nfft(delta: int) -> None:
    raw_header, payload = decode_frame(_psd_frame(_psd_values()))
    body = bytes(payload)
    wrong = body[:delta] if delta < 0 else body + b"\x00" * delta
    with pytest.raises(ProtocolError, match=rf"payload is {256 + delta} bytes.*nfft 64.*requires 256") as err:
        decode_psd(raw_header, wrong)
    assert err.value.message_type == "psd"
    # The same through a complete frame, as it would arrive.
    header2, payload2 = decode_frame(manual_frame(json.dumps(raw_header).encode(), wrong))
    with pytest.raises(ProtocolError, match="payload is"):
        decode_psd(header2, payload2)


def test_psd_decode_rejections() -> None:
    raw_header, payload = decode_frame(_psd_frame(_psd_values()))
    with pytest.raises(ProtocolError, match="payload is 0 bytes"):
        decode_psd(raw_header, b"")
    with pytest.raises(ProtocolError, match="nfft"):
        decode_psd(altered(raw_header, "nfft", "64"), payload)
    with pytest.raises(ProtocolError, match="monitor_id"):
        decode_psd(altered(raw_header, "monitor_id", _MISSING), payload)
    with pytest.raises(ProtocolError, match="type"):
        decode_psd(altered(raw_header, "type", "capture_data"), payload)
    for bad in (float("nan"), float("inf"), float("-inf")):
        poisoned = bytearray(payload)
        struct.pack_into("<f", poisoned, 4 * 17, bad)
        with pytest.raises(ProtocolError, match="NaN or infinite"):
            decode_psd(raw_header, poisoned)


def test_psd_encode_rejections() -> None:
    values = _psd_values()
    with pytest.raises(ValueError, match="psd_db: has 64 bins but nfft is 128"):
        _psd_frame(values, nfft=128)
    with pytest.raises(ValueError, match="psd_db: has 63 bins but nfft is 64"):
        _psd_frame(values[:63], nfft=64)
    with pytest.raises(ValueError, match="psd_db: must be float32, got float64"):
        _psd_frame(values.astype(np.float64))
    with pytest.raises(ValueError, match="psd_db: must be float32, got complex64"):
        _psd_frame(values.astype(np.complex64))
    with pytest.raises(ValueError, match="psd_db: must be float32, got int32"):
        _psd_frame(np.zeros(64, dtype=np.int32))
    with pytest.raises(ValueError, match="psd_db: must be one-dimensional"):
        _psd_frame(values.reshape(8, 8), nfft=64)
    with pytest.raises(ValueError, match="psd_db: must be a numpy array"):
        _psd_frame(values.tolist())
    for bad in (np.nan, np.inf, -np.inf):
        poisoned = values.copy()
        poisoned[40] = bad
        with pytest.raises(ValueError, match="psd_db: holds NaN or infinite"):
            _psd_frame(poisoned)
    with pytest.raises(ValidationError, match="t_ns"):
        _psd_frame(values, t_ns=float(T_NS))
    with pytest.raises(ValidationError, match="t_ns"):
        _psd_frame(values, t_ns=np.int64(T_NS))
    with pytest.raises(ValidationError, match="span_hz"):
        _psd_frame(values, span_hz=0.0)
    with pytest.raises(ValidationError, match="channel_id"):
        _psd_frame(values, channel_id="")


# ================================================================== capture chunks


@pytest.mark.parametrize("n", [1, 32768, 32769, 1_000_003])
def test_chunking_and_reassembly_are_bit_exact(n: int) -> None:
    iq = noise(n)
    frames = list(iter_capture_chunks("cap-1", "ch-a", iq, T_NS, 31250.0, 255.5e6, False, CLOCK))
    offsets = list(range(0, n, CHUNK_SAMPLES))  # 256 KiB / 8 bytes = 32768 samples per chunk
    assert len(frames) == len(offsets) == -(-n // CHUNK_SAMPLES)
    assert len(frames) == {1: 1, 32768: 1, 32769: 2, 1_000_003: 31}[n]

    assembler = CaptureAssembler("cap-1", "ch-a", expected_n_samples=n)
    result = None
    for seq, (frame, offset) in enumerate(zip(frames, offsets, strict=True)):
        assert result is None and not assembler.complete
        header, payload = decode_frame(frame)
        count = min(CHUNK_SAMPLES, n - offset)
        assert header == {
            "type": "capture_data", "capture_id": "cap-1", "channel_id": "ch-a", "seq": seq,
            "last": seq == len(frames) - 1, "t_first_ns": T_NS, "sample_rate_hz": 31250.0,
            "center_hz": 255.5e6, "n_samples_total": n, "offset": offset, "n": count, "overflow": False,
            "clock": CLOCK_WIRE,
        }
        assert type(header["t_first_ns"]) is int
        assert len(payload) == 8 * count <= 256 * 1024
        assert (len(frame) - len(payload)) % 8 == 0
        if seq in (0, len(frames) - 1):  # payload bytes against struct, on the edge chunks
            assert bytes(payload[:64]) == le_bytes(iq[offset : offset + 8])[: len(payload[:64])]
        result = assembler.add(header, payload)
        assert assembler.received_samples == offset + count
    assert result is not None and assembler.complete and not assembler.failed
    assert result.iq.dtype == np.complex64 and result.iq.shape == (n,)
    assert result.iq.tobytes() == iq.tobytes()
    assert (result.capture_id, result.channel_id) == ("cap-1", "ch-a")
    assert result.t_first_ns == T_NS and type(result.t_first_ns) is int
    assert (result.sample_rate_hz, result.center_hz) == (31250.0, 255.5e6)
    assert result.overflow is False and result.clock == CLOCK
    assert result.n_chunks == len(frames) and result.n_samples == n
    assert assembler.n_samples_total == n
    # the field names of the driver `Capture` of ARCHITECTURE.md section 6 work too
    assert (result.fs, result.fc) == (result.sample_rate_hz, result.center_hz)
    assert result.clock_locked is result.clock.locked is CLOCK.locked


def test_chunk_layout_with_a_small_chunk_size() -> None:
    iq = noise(10)
    iq[:3] = np.array([0.0 - 0.0j, np.finfo(np.float32).max * (1 - 1j), 1e-42 + 3e-45j], dtype=np.complex64)
    frames = list(iter_capture_chunks("cap-1", "ch-a", iq, T_NS, 31250.0, 255.5e6, True, CLOCK_WIRE, max_payload=24))
    decoded = [decode_frame(frame) for frame in frames]
    assert [(h["seq"], h["offset"], h["n"], h["last"]) for h, _ in decoded] == [
        (0, 0, 3, False), (1, 3, 3, False), (2, 6, 3, False), (3, 9, 1, True),
    ]
    assert all(h["n_samples_total"] == 10 and h["overflow"] is True for h, _ in decoded)
    assert b"".join(bytes(p) for _, p in decoded) == le_bytes(iq)
    # max_payload is rounded down to whole samples.
    def counts(max_payload: int) -> list[int]:
        frames = iter_capture_chunks("c", "ch", iq, 0, 1.0, 0.0, False, CLOCK, max_payload=max_payload)
        return [decode_frame(frame)[0]["n"] for frame in frames]

    assert counts(47) == [5, 5]
    assert counts(8) == [1] * 10

    header, chunk = decode_capture_data(*decoded[1])
    assert isinstance(header, CaptureDataHeader) and header.clock == CLOCK
    assert chunk.dtype == np.complex64 and chunk.tobytes() == iq[3:6].tobytes()
    assert np.shares_memory(chunk, np.frombuffer(frames[1], dtype=np.uint8)) and not chunk.flags.writeable
    assert chunk.flags.aligned


def test_chunks_are_little_endian_whatever_the_input_byte_order() -> None:
    iq = noise(10)
    args = ("cap-1", "ch-a", T_NS, 31250.0, 255.5e6, False, CLOCK)

    def frames_of(samples: np.ndarray) -> list[bytes]:
        return list(iter_capture_chunks(args[0], args[1], samples, *args[2:], max_payload=32))

    reference = frames_of(iq)
    assert frames_of(iq.astype(">c8")) == reference
    assert frames_of(np.repeat(iq, 2)[::2]) == reference
    assert b"".join(bytes(decode_frame(f)[1]) for f in reference) == le_bytes(iq)


def test_chunks_read_the_callers_array_without_copying_it() -> None:
    iq = noise(8)
    frames = iter_capture_chunks("cap-1", "ch-a", iq, T_NS, 31250.0, 255.5e6, False, CLOCK, max_payload=32)
    first = next(frames)
    iq[4:] = 0  # documented aliasing: later chunks are read from the caller's array
    rest = list(frames)
    assert bytes(decode_frame(first)[1]) == le_bytes(noise(8)[:4])
    assert bytes(decode_frame(rest[0])[1]) == bytes(32)


def test_iter_capture_chunks_checks_everything_before_yielding() -> None:
    iq = noise(16)

    def call(samples: Any = iq, **overrides: Any) -> Any:
        args: dict[str, Any] = {
            "capture_id": "cap-1", "channel_id": "ch-a", "iq": samples, "t_first_ns": T_NS,
            "sample_rate_hz": 31250.0, "center_hz": 255.5e6, "overflow": False, "clock": CLOCK,
        }
        args.update(overrides)
        return iter_capture_chunks(**args)  # not iterated: errors must come from the call

    with pytest.raises(ValueError, match="iq: must hold at least one sample"):
        call(iq[:0])
    with pytest.raises(ValueError, match="iq: must be complex64, got complex128"):
        call(iq.astype(np.complex128))
    with pytest.raises(ValueError, match="iq: must be complex64, got float32"):
        call(np.real(iq).copy())
    with pytest.raises(ValueError, match="iq: must be one-dimensional"):
        call(iq.reshape(4, 4))
    with pytest.raises(ValueError, match="iq: must be a numpy array"):
        call(iq.tolist())
    for bad in (complex(np.nan, 0), complex(0, np.inf), complex(-np.inf, 0)):
        poisoned = iq.copy()
        poisoned[11] = bad
        with pytest.raises(ValueError, match="iq: holds NaN or infinite"):
            call(poisoned)
    # One sample over 64 MiB, without allocating it.
    too_long = np.broadcast_to(np.complex64(1), (P.MAX_CAPTURE_SAMPLES + 1,))
    with pytest.raises(ValueError, match="iq: 8388609 samples exceeds the limit of 8388608"):
        call(too_long)
    for bad_size in (0, 7, -8, 256 * 1024 + 1, 1 << 30):
        with pytest.raises(ValueError, match="max_payload: must be 8 to 262144 bytes"):
            call(max_payload=bad_size)
    for bad_kind in (8.0, True, "8", None):
        with pytest.raises(ValueError, match="max_payload: must be an integer"):
            call(max_payload=bad_kind)
    with pytest.raises(ValidationError, match="capture_id"):
        call(capture_id="")
    with pytest.raises(ValidationError, match="t_first_ns"):
        call(t_first_ns=1.8e18)
    with pytest.raises(ValidationError, match="sample_rate_hz"):
        call(sample_rate_hz=0.0)
    with pytest.raises(ValidationError, match="overflow"):
        call(overflow=1)
    with pytest.raises(ValidationError, match="source"):
        call(clock={**CLOCK_WIRE, "source": "atomic"})
    with pytest.raises(ValueError, match="clock: must be a ClockStatus or a mapping"):
        call(clock="gpsdo")
    assert len(list(call())) == 1


def test_assembler_accepts_chunks_built_by_hand() -> None:
    iq = noise(10)
    assembler = CaptureAssembler()
    assert assembler.capture_id is None and assembler.n_samples_total is None
    # Fields a newer node might add are ignored, in the header and in its clock.
    newer = [
        (altered({**header, "gain_db": 30.0}, "clock.stratum", 1), payload)
        for header, payload in chunks_of(iq, 4)
    ]
    results = [assembler.add(header, payload) for header, payload in newer]
    assert results[0] is None and results[1] is None
    final = results[2]
    assert final is not None and final.iq.tobytes() == iq.tobytes()
    assert (assembler.capture_id, assembler.channel_id) == ("cap-1", "ch-a")
    # The result is the assembler's own storage: writable, aliasing no frame.
    assert final.iq.flags.writeable and final.iq.flags.owndata and final.n_chunks == 3
    # Validated headers are accepted as well as dicts, bytearrays as well as bytes.
    second = CaptureAssembler("cap-1", "ch-a")
    outcomes = [
        second.add(CaptureDataHeader.from_wire(header), bytearray(payload))
        for header, payload in chunks_of(iq, 4)
    ]
    assert outcomes[:2] == [None, None]
    assert outcomes[2] is not None and outcomes[2].iq.tobytes() == iq.tobytes()


def _feed(assembler: CaptureAssembler, chunks: list[tuple[dict[str, Any], bytes]]) -> Any:
    result = None
    for header, payload in chunks:
        result = assembler.add(header, payload)
    return result


def test_assembler_rejects_out_of_order_chunks() -> None:
    chunks = chunks_of(noise(10), 4)
    assembler = CaptureAssembler()
    assembler.add(*chunks[0])
    with pytest.raises(ProtocolError, match="chunk out of order: seq 2, expected 1") as err:
        assembler.add(*chunks[2])
    assert err.value.message_type == "capture_data"
    # The capture is void: even the chunk that would have been next is refused.
    assert assembler.failed and not assembler.complete
    assert assembler.failure == err.value.detail and assembler.received_samples == 4
    with pytest.raises(ProtocolError, match="voided by an earlier error"):
        assembler.add(*chunks[1])

    late_start = CaptureAssembler()
    with pytest.raises(ProtocolError, match="chunk out of order: seq 1, expected 0"):
        late_start.add(*chunks[1])
    swapped = CaptureAssembler()
    with pytest.raises(ProtocolError, match="chunk out of order: seq 2, expected 0"):
        _feed(swapped, [chunks[2], chunks[1], chunks[0]])


def test_assembler_rejects_duplicate_chunks() -> None:
    chunks = chunks_of(noise(10), 4)
    assembler = CaptureAssembler()
    assembler.add(*chunks[0])
    with pytest.raises(ProtocolError, match="duplicate chunk: seq 0, expected 1"):
        assembler.add(*chunks[0])
    assert assembler.failed

    later = CaptureAssembler()
    _feed(later, chunks[:2])
    with pytest.raises(ProtocolError, match="duplicate chunk: seq 1, expected 2"):
        later.add(*chunks[1])


def test_assembler_rejects_overlapping_and_skipping_chunks() -> None:
    iq = noise(10)
    chunks = chunks_of(iq, 4)
    overlapping = CaptureAssembler()
    overlapping.add(*chunks[0])
    with pytest.raises(ProtocolError, match="overlaps samples already received: offset 2, expected 4"):
        overlapping.add(chunk_header(1, 2, 4, 10), le_bytes(iq[2:6]))
    assert overlapping.failed

    skipping = CaptureAssembler()
    skipping.add(*chunks[0])
    with pytest.raises(ProtocolError, match="skips samples: offset 6, expected 4"):
        skipping.add(chunk_header(1, 6, 4, 10), le_bytes(iq[6:10]))

    # The right offset with a length that would overlap the next chunk shows up there.
    longer = CaptureAssembler()
    longer.add(*chunks[0])
    longer.add(chunk_header(1, 4, 5, 10), le_bytes(iq[4:9]))
    with pytest.raises(ProtocolError, match="overlaps samples already received: offset 8, expected 9"):
        longer.add(*chunks[2])


def test_assembler_enforces_the_capture_size_limit() -> None:
    limit = P.MAX_CAPTURE_SAMPLES
    assert limit * 8 == 64 * 1024 * 1024
    one = le_bytes(noise(1))
    oversized = CaptureAssembler()
    with pytest.raises(ProtocolError, match="capture of 67108872 bytes exceeds the limit of 67108864"):
        oversized.add(chunk_header(0, 0, 1, limit + 1), one)
    assert oversized.failed and oversized.n_samples_total is None

    # Exactly 64 MiB is accepted (storage is reserved, only the first chunk is written).
    at_limit = CaptureAssembler()
    assert at_limit.add(chunk_header(0, 0, 1, limit), one) is None
    assert at_limit.n_samples_total == limit and at_limit.received_samples == 1

    small = CaptureAssembler(max_bytes=72)
    with pytest.raises(ProtocolError, match="capture of 80 bytes exceeds the limit of 72"):
        small.add(*chunks_of(noise(10), 4)[0])
    assert _feed(CaptureAssembler(max_bytes=80), chunks_of(noise(10), 4)) is not None


def test_assembler_enforces_the_chunk_size_limit() -> None:
    assert P.MAX_CHUNK_PAYLOAD_BYTES == 256 * 1024
    iq = noise(CHUNK_SAMPLES + 1)
    whole = CaptureAssembler()
    with pytest.raises(ProtocolError, match="chunk payload of 262152 bytes exceeds the limit of 262144"):
        whole.add(chunk_header(0, 0, len(iq), len(iq)), iq.astype("<c8").tobytes())
    assert whole.failed
    exact = CaptureAssembler()
    assert exact.add(chunk_header(0, 0, CHUNK_SAMPLES, len(iq)), iq[:CHUNK_SAMPLES].astype("<c8").tobytes()) is None

    tight = CaptureAssembler(max_chunk_payload=24)
    with pytest.raises(ProtocolError, match="chunk payload of 32 bytes exceeds the limit of 24"):
        tight.add(*chunks_of(noise(10), 4)[0])
    assert _feed(CaptureAssembler(max_chunk_payload=32), chunks_of(noise(10), 4)) is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capture_id", "cap-2"),
        ("channel_id", "ch-b"),
        ("t_first_ns", T_NS + 1),
        ("sample_rate_hz", 31250.000000001),
        ("center_hz", 255.5e6 + 1.0),
        ("n_samples_total", 11),
    ],
)
def test_assembler_rejects_metadata_that_changes_between_chunks(field: str, value: Any) -> None:
    iq = noise(10)
    chunks = chunks_of(iq, 4)
    expected = (
        f"{field} is not that of this capture"
        if field in ("capture_id", "channel_id")
        else f"{field} changed between chunks"
    )
    assembler = CaptureAssembler()
    assembler.add(*chunks[0])
    with pytest.raises(ProtocolError, match=expected):
        assembler.add(altered(chunks[1][0], field, value), chunks[1][1])
    assert assembler.failed and assembler.received_samples == 4
    # On the final chunk too (n_samples_total there would also move `last`).
    if field != "n_samples_total":
        final = CaptureAssembler()
        _feed(final, chunks[:2])
        with pytest.raises(ProtocolError, match=expected):
            final.add(altered(chunks[2][0], field, value), chunks[2][1])


def test_assembler_checks_chunks_against_the_request() -> None:
    chunks = chunks_of(noise(10), 4)
    with pytest.raises(ProtocolError, match="capture_id is not that of this capture"):
        CaptureAssembler("cap-9", "ch-a").add(*chunks[0])
    with pytest.raises(ProtocolError, match="channel_id is not that of this capture"):
        CaptureAssembler("cap-1", "ch-z").add(*chunks[0])
    with pytest.raises(ProtocolError, match="n_samples_total is 10, expected 12"):
        CaptureAssembler("cap-1", "ch-a", expected_n_samples=12).add(*chunks[0])
    assert _feed(CaptureAssembler("cap-1", "ch-a", expected_n_samples=10), chunks) is not None
    bad_arguments: list[dict[str, Any]] = [
        {"expected_n_samples": 0}, {"expected_n_samples": 2.0}, {"max_bytes": 0}, {"max_chunk_payload": True},
    ]
    for kwargs in bad_arguments:
        with pytest.raises(ValueError, match=next(iter(kwargs))):
            CaptureAssembler(**kwargs)


def test_assembler_rejects_malformed_chunks() -> None:
    iq = noise(10)
    chunks = chunks_of(iq, 4)
    cases: list[tuple[dict[str, Any], bytes, str]] = [
        (chunks[0][0], chunks[0][1][:-8], r"payload is 24 bytes, the header \(n 4\) requires 32"),
        (chunks[0][0], chunks[0][1] + bytes(8), r"payload is 40 bytes, the header \(n 4\) requires 32"),
        (chunks[0][0], chunks[0][1][:-1], "payload is 31 bytes"),
        (chunks[0][0], b"", "payload is 0 bytes"),
        (altered(chunks[0][0], "last", True), chunks[0][1], "last must be true on the chunk that ends"),
        (chunk_header(0, 0, 10, 10, last=False), le_bytes(iq), "last must be true on the chunk that ends"),
        (chunk_header(0, 0, 4, 3), chunks[0][1], "offset \\+ n exceeds n_samples_total"),
        (chunk_header(0, 4, 4, 10), chunks[1][1], "offset is impossible for this seq"),
        (chunk_header(5, 4, 4, 10), chunks[1][1], "offset is impossible for this seq"),
        (altered(chunks[0][0], "n", 0), b"", "n: "),
        (altered(chunks[0][0], "n", 4.0), chunks[0][1], "n: "),
        (altered(chunks[0][0], "offset", -1), chunks[0][1], "offset"),
        (altered(chunks[0][0], "seq", "0"), chunks[0][1], "seq"),
        (altered(chunks[0][0], "t_first_ns", float(T_NS)), chunks[0][1], "t_first_ns"),
        (altered(chunks[0][0], "t_first_ns", str(T_NS)), chunks[0][1], "t_first_ns"),
        (altered(chunks[0][0], "overflow", 0), chunks[0][1], "overflow"),
        (altered(chunks[0][0], "clock.locked", "yes"), chunks[0][1], "clock.locked"),
        (altered(chunks[0][0], "clock", _MISSING), chunks[0][1], "clock"),
        (altered(chunks[0][0], "type", "psd"), chunks[0][1], "type"),
        (altered(chunks[0][0], "sample_rate_hz", 0), chunks[0][1], "sample_rate_hz"),
    ]
    for header, payload, match in cases:
        assembler = CaptureAssembler()
        with pytest.raises(ProtocolError, match=match):
            assembler.add(header, payload)
        assert assembler.failed and assembler.received_samples == 0, match
        with pytest.raises(ProtocolError, match="voided"):
            assembler.add(*chunks[0])

    for bad in (float("nan"), float("inf")):
        poisoned = bytearray(chunks[1][1])
        struct.pack_into("<f", poisoned, 12, bad)
        assembler = CaptureAssembler()
        assembler.add(*chunks[0])
        with pytest.raises(ProtocolError, match="NaN or infinite samples"):
            assembler.add(chunks[1][0], poisoned)
    with pytest.raises(ProtocolError, match="expected bytes, got text"):
        CaptureAssembler().add(chunks[0][0], "payload")  # type: ignore[arg-type]


def test_assembler_refuses_chunks_after_completion() -> None:
    chunks = chunks_of(noise(10), 4)
    assembler = CaptureAssembler()
    assert _feed(assembler, chunks) is not None
    for extra in (chunks[2], chunks[0]):
        with pytest.raises(ProtocolError, match="chunk after the capture was complete"):
            assembler.add(*extra)
    assert assembler.complete and not assembler.failed


def test_incomplete_capture_never_completes() -> None:
    """A node that disconnects mid-capture leaves an assembler that reports how far it got."""
    chunks = chunks_of(noise(10), 4)
    assembler = CaptureAssembler("cap-1", "ch-a", expected_n_samples=10)
    assert _feed(assembler, chunks[:2]) is None
    assert not assembler.complete and not assembler.failed and assembler.failure is None
    assert assembler.received_samples == 8 and assembler.n_samples_total == 10


def test_assembler_merges_overflow_and_clock_conservatively() -> None:
    iq = noise(10)
    unlocked = {"source": "gpsdo", "locked": False, "est_offset_ns": 250.0, "holdover_s": 1.5}
    chunks = chunks_of(iq, 4)
    chunks[1] = (altered(altered(chunks[1][0], "overflow", True), "clock", unlocked), chunks[1][1])
    result = _feed(CaptureAssembler(), chunks)
    assert result.overflow is True  # any chunk
    assert result.clock == ClockStatus(**unlocked)  # the first report of a lost lock wins
    assert result.iq.tobytes() == iq.tobytes()

    clean = _feed(CaptureAssembler(), chunks_of(iq, 4))
    assert clean.overflow is False and clean.clock == CLOCK
    # A varying offset estimate while locked is not an inconsistency.
    drifting = chunks_of(iq, 4)
    drifting[2] = (altered(drifting[2][0], "clock.est_offset_ns", 99.0), drifting[2][1])
    assert _feed(CaptureAssembler(), drifting).clock == CLOCK


def test_assembler_failure_is_logged_with_structured_fields(caplog: pytest.LogCaptureFixture) -> None:
    chunks = chunks_of(noise(10), 4)
    assembler = CaptureAssembler("cap-1", "ch-a")
    assembler.add(*chunks[0])
    with caplog.at_level(logging.DEBUG, logger="petal_locate.protocol.framing"), pytest.raises(ProtocolError):
        assembler.add(*chunks[2])
    (record,) = [r for r in caplog.records if r.name == "petal_locate.protocol.framing"]
    assert record.capture_id == "cap-1" and record.channel_id == "ch-a"  # type: ignore[attr-defined]
    assert record.received_samples == 4  # type: ignore[attr-defined]
    assert "out of order" in record.protocol_detail  # type: ignore[attr-defined]


def test_wire_work_stays_far_inside_the_event_loop_budget() -> None:
    """No single call may block the loop for 50 ms; best of three guards against a busy host."""
    iq = noise(1_000_003)
    worst_call = float("inf")
    for _ in range(3):
        slowest = 0.0
        start = time.perf_counter()
        chunks = iter_capture_chunks("cap-1", "ch-a", iq, T_NS, 31250.0, 255.5e6, False, CLOCK)
        slowest = max(slowest, time.perf_counter() - start)
        assembler = CaptureAssembler()
        while True:
            start = time.perf_counter()
            frame = next(chunks, None)
            slowest = max(slowest, time.perf_counter() - start)
            if frame is None:
                break
            start = time.perf_counter()
            assembler.add(*decode_frame(frame))
            slowest = max(slowest, time.perf_counter() - start)
        assert assembler.complete
        worst_call = min(worst_call, slowest)
    assert worst_call < 0.050


# ================================================= nothing but ProtocolError escapes

_JUNK: list[Any] = [
    None, True, False, 0, -1, 1, 1.5, -0.0, 10**400, -(10**400), 2**63, 2**64, 1e308, "", "x", "\ud800",
    "9" * 30, [], {}, [1], {"a": 1}, [[]], {"type": "hello"}, "NaN",
]


def _paths(obj: Any, prefix: str = "") -> list[str]:
    """Dotted path of every value inside a JSON object, containers included."""
    found: list[str] = []
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    for key, value in items:
        found.append(f"{prefix}{key}")
        found.extend(_paths(value, f"{prefix}{key}."))
    return found


def _set_path(obj: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(obj)
    node: Any = out
    *parents, leaf = path.split(".")
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if isinstance(node, list):
        node[int(leaf)] = value
    else:
        node[leaf] = value
    return out


def _decode_any(kind: str, header: Any, payload: Any) -> None:
    if kind == "psd":
        decode_psd(header, payload)
    elif kind == "capture_data":
        CaptureAssembler().add(header, payload)
    else:
        decode_console_psd(header, payload)


def _frames_for_fuzzing() -> dict[str, bytes]:
    values = _psd_values()
    return {
        "psd": _psd_frame(values),
        "capture_data": next(
            iter_capture_chunks("cap-1", "ch-a", noise(16), T_NS, 31250.0, 255.5e6, False, CLOCK)
        ),
        "console": _console_frame(values, baseline_db=values, detections=DETECTIONS),
    }


def test_no_field_value_makes_a_parser_raise_anything_but_protocol_error() -> None:
    """Every value of every message replaced by junk: a message or ProtocolError, nothing else."""
    accepted = refused = 0
    details: set[str] = set()
    for name in SPEC_FIELDS:
        base = wire(name)
        for path in _paths(base):
            for junk in _JUNK:
                try:
                    parse_control(json.dumps(_set_path(base, path, junk)))
                    accepted += 1
                except ProtocolError as exc:
                    details.add(exc.detail)
                    refused += 1
    assert not [detail for detail in details if SECRET in detail or len(detail) > P.MAX_ERROR_DETAIL_CHARS]
    for kind, frame in _frames_for_fuzzing().items():
        header, payload = decode_frame(frame)
        for path in _paths(header):
            for junk in _JUNK:
                try:
                    _decode_any(kind, _set_path(header, path, junk), payload)
                    accepted += 1
                except ProtocolError:
                    refused += 1
    assert accepted > 300 and refused > 2500  # both outcomes are exercised


def test_no_damaged_frame_makes_a_decoder_raise_anything_but_protocol_error() -> None:
    """Seeded byte-level damage to valid frames: flips, truncation, insertions, deletions."""
    rng = np.random.default_rng(20261113)
    refused = 0
    for kind, frame in _frames_for_fuzzing().items():
        head = min(len(frame), 400)  # damage the prefix and header, where the structure is
        for _ in range(700):
            damaged = bytearray(frame)
            action = int(rng.integers(4))
            at = int(rng.integers(head))
            if action == 0:
                damaged[at] = int(rng.integers(256))
            elif action == 1:
                del damaged[int(rng.integers(len(damaged))) :]
            elif action == 2:
                damaged[at:at] = rng.integers(256, size=int(rng.integers(1, 9)), dtype=np.uint8).tobytes()
            else:
                del damaged[at : at + int(rng.integers(1, 9))]
            try:
                _decode_any(kind, *decode_frame(bytes(damaged)))
            except ProtocolError:
                refused += 1
    assert refused > 1000


# ============================================================ console spectrum frame


def _console_frame(values: np.ndarray, **overrides: Any) -> bytes:
    args: dict[str, Any] = {
        "channel_id": "ch-a", "monitor_id": "mon-1", "t_ns": T_NS, "center_hz": 255.5e6, "span_hz": 31250.0,
        "nfft": len(values), "seq": 12, "psd_db": values,
    }
    args.update(overrides)
    return encode_console_psd(**args)


DETECTIONS = [
    ConsoleDetection(track_id=4, f_lo_hz=255.49e6, f_hi_hz=255.51e6, authorized_carrier_id=None, anomaly=False),
    {"track_id": 9, "f_lo_hz": 255.4e6, "f_hi_hz": 255.42e6, "authorized_carrier_id": "ref-guam", "anomaly": True},
]


def test_console_frame_round_trip_without_baseline() -> None:
    values = _psd_values()
    frame = _console_frame(values, detections=DETECTIONS)
    raw_header, payload = decode_frame(frame)
    assert set(raw_header) == SPEC_CONSOLE_PSD
    assert all(set(d) == SPEC_DETECTION for d in raw_header["detections"])
    # Console times are decimal strings: JavaScript numbers cannot hold nanoseconds.
    assert raw_header["t_ns"] == str(T_NS) and f'"t_ns":"{T_NS}"'.encode() in header_json_of(frame)
    assert raw_header["has_baseline"] is False
    assert raw_header["detections"] == [
        {"track_id": 4, "f_lo_hz": 255.49e6, "f_hi_hz": 255.51e6, "authorized_carrier_id": None, "anomaly": False},
        {"track_id": 9, "f_lo_hz": 255.4e6, "f_hi_hz": 255.42e6, "authorized_carrier_id": "ref-guam", "anomaly": True},
    ]
    assert b'"authorized_carrier_id":null' in header_json_of(frame)  # null, not left out
    assert bytes(payload) == struct.pack("<64f", *values.tolist())
    assert (len(frame) - len(payload)) % 4 == 0  # a Float32Array view needs a 4-byte offset

    header, psd, baseline = decode_console_psd(raw_header, payload)
    assert isinstance(header, ConsolePsdHeader)
    assert header.t_ns == T_NS and type(header.t_ns) is int
    assert baseline is None and psd.tobytes() == values.tobytes()
    assert np.shares_memory(psd, np.frombuffer(frame, dtype=np.uint8))
    assert [d.track_id for d in header.detections] == [4, 9]
    assert header.detections[0].authorized_carrier_id is None
    assert _console_frame(values) != frame and decode_frame(_console_frame(values))[0]["detections"] == []


def test_console_frame_round_trip_with_baseline() -> None:
    values = _psd_values()
    baseline_in = (values - np.float32(3.5)).astype(">f4")  # byte order of the input must not matter
    baseline_in[2] = values[2]
    frame = _console_frame(values, baseline_db=baseline_in, detections=DETECTIONS[:1])
    raw_header, payload = decode_frame(frame)
    assert raw_header["has_baseline"] is True and raw_header["nfft"] == 64
    native_baseline = baseline_in.astype(np.float32)
    assert bytes(payload) == struct.pack("<64f", *values.tolist()) + struct.pack("<64f", *native_baseline.tolist())

    header, psd, baseline = decode_console_psd(raw_header, payload)
    assert header.has_baseline and baseline is not None
    assert psd.tobytes() == values.tobytes() and baseline.tobytes() == native_baseline.tobytes()
    assert psd.shape == baseline.shape == (64,)
    frame_bytes = np.frombuffer(frame, dtype=np.uint8)
    assert np.shares_memory(psd, frame_bytes) and np.shares_memory(baseline, frame_bytes)
    assert decode_console_psd(header, payload)[0] is header


def test_console_detections_from_tracker_objects() -> None:
    tracked = SimpleNamespace(
        track_id=np.int64(11), f_lo_hz=np.float32(255.25e6), f_hi_hz=np.float64(255.75e6), power_db=-91.5,
        snr_db=14.0, cn0_dbhz=58.0, first_seen_ns=T_NS, last_seen_ns=T_NS + 5, authorized_carrier_id=None,
        anomaly=np.bool_(True),
    )
    header = decode_frame(_console_frame(_psd_values(), detections=[tracked]))[0]
    assert header["detections"] == [
        {
            "track_id": 11, "f_lo_hz": float(np.float32(255.25e6)), "f_hi_hz": 255.75e6,
            "authorized_carrier_id": None, "anomaly": True,
        }
    ]
    assert ConsoleDetection.from_detection(tracked).track_id == 11
    with pytest.raises(ValueError, match="detection lacks attribute 'anomaly'"):
        ConsoleDetection.from_detection(
            SimpleNamespace(track_id=1, f_lo_hz=1.0, f_hi_hz=2.0, authorized_carrier_id=None)
        )
    with pytest.raises(ValueError, match="detection has a field of the wrong kind"):
        ConsoleDetection.from_detection(
            SimpleNamespace(track_id=None, f_lo_hz=1.0, f_hi_hz=2.0, authorized_carrier_id=None, anomaly=False)
        )
    with pytest.raises(ValueError, match="f_hi_hz must not be below f_lo_hz"):
        ConsoleDetection(track_id=1, f_lo_hz=2.0, f_hi_hz=1.0, anomaly=False)
    misnamed = {"track_id": 1, "f_lo_hz": 1.0, "f_hi_hz": 2.0, "carrier": "x", "anomaly": False}
    with pytest.raises(ValueError, match="carrier"):
        _console_frame(_psd_values(), detections=[misnamed])


def test_console_frame_rejections() -> None:
    values = _psd_values()
    plain_header, plain_payload = decode_frame(_console_frame(values))
    with_header, with_payload = decode_frame(_console_frame(values, baseline_db=values))
    with pytest.raises(ProtocolError, match=r"payload is 256 bytes.*with baseline.*requires 512"):
        decode_console_psd(with_header, plain_payload)
    with pytest.raises(ProtocolError, match=r"payload is 512 bytes.*no baseline.*requires 256"):
        decode_console_psd(plain_header, with_payload)
    with pytest.raises(ProtocolError, match="payload is 508 bytes"):
        decode_console_psd(with_header, bytes(with_payload)[:-4])
    # On this interface t_ns is a decimal string and nothing else.
    for bad in (T_NS, float(T_NS), "1.8e18", "-5", " 12", "0x10", "", "9" * 20, str(2**63), None):
        with pytest.raises(ProtocolError, match="t_ns"):
            decode_console_psd(altered(plain_header, "t_ns", bad), plain_payload)
    assert decode_console_psd(altered(plain_header, "t_ns", str(2**63 - 1)), plain_payload)[0].t_ns == 2**63 - 1
    bad_detection = {"track_id": "4", "f_lo_hz": 1.0, "f_hi_hz": 2.0, "authorized_carrier_id": None, "anomaly": False}
    with pytest.raises(ProtocolError, match=r"detections\.0\.track_id"):
        decode_console_psd(altered(plain_header, "detections", [bad_detection]), plain_payload)
    poisoned = bytearray(with_payload)
    struct.pack_into("<f", poisoned, 4 * 100, float("nan"))
    with pytest.raises(ProtocolError, match="NaN or infinite"):
        decode_console_psd(with_header, poisoned)

    with pytest.raises(ValueError, match="baseline_db: has 32 bins but nfft is 64"):
        _console_frame(values, baseline_db=values[:32])
    with pytest.raises(ValueError, match="baseline_db: must be float32, got float64"):
        _console_frame(values, baseline_db=values.astype(np.float64))
    not_learned = values.copy()
    not_learned[:] = np.nan
    with pytest.raises(ValueError, match="baseline_db: holds NaN or infinite"):
        _console_frame(values, baseline_db=not_learned)
    with pytest.raises(ValueError, match="psd_db: has 64 bins but nfft is 32"):
        _console_frame(values, nfft=32)
    with pytest.raises(ValidationError, match="t_ns"):
        _console_frame(values, t_ns=1.8e18)
    # Building takes the integer or its decimal string, and always writes the string.
    assert decode_frame(_console_frame(values, t_ns=str(T_NS)))[0]["t_ns"] == str(T_NS)


# ============================================================= over a real WebSocket


async def test_messages_and_frames_cross_a_real_websocket() -> None:
    """Text and binary frames interleave over `websockets` as the node and hub will use it."""
    # Only this test needs the transport (its asyncio API, websockets 13 or later).
    connect = pytest.importorskip("websockets.asyncio.client").connect
    serve = pytest.importorskip("websockets.asyncio.server").serve
    connection_closed = pytest.importorskip("websockets.exceptions").ConnectionClosed
    websockets_version = pytest.importorskip("websockets").__version__
    n = 2 * CHUNK_SAMPLES + 4321
    iq = noise(n, seed=11)
    outcome: dict[str, Any] = {}

    async def hub(ws: Any) -> None:
        try:
            hello = parse_control(await ws.recv())
            assert isinstance(hello, Hello) and hello.token == SECRET
            await ws.send(dump_control(EXAMPLES["welcome"]))
            request = CaptureRequest(
                capture_id="cap-ws", channel_id="ch-a", t_start_ns=T_NS, n_samples=n, sample_rate_hz=31250.0,
                center_hz=255.5e6, deadline_ns=T_NS + 10**10,
            )
            await ws.send(dump_control(request))
            assembler = CaptureAssembler(request.capture_id, request.channel_id, expected_n_samples=n)
            kinds: list[str] = []
            async for raw in ws:
                if isinstance(raw, str):
                    beat = parse_control(raw)
                    assert isinstance(beat, Heartbeat)
                    kinds.append("heartbeat")
                    await ws.send(dump_control(HeartbeatAck(seq=beat.seq, server_t_ns=beat.t_ns + 1)))
                    continue
                header, payload = decode_frame(raw)
                kinds.append(header["type"])
                if header["type"] == "psd":
                    outcome["psd"] = decode_psd(header, payload)
                    continue
                capture = assembler.add(header, payload)
                if capture is not None:
                    outcome["capture"] = capture
                    break
            outcome["kinds"] = kinds
        except (AssertionError, ProtocolError, connection_closed) as exc:
            outcome["error"] = exc  # reported by the test below; a handler must not fail unseen

    psd_values = _psd_values()
    connect_kwargs: dict[str, Any] = {"compression": None, "max_size": P.WS_MAX_SIZE_BYTES}
    if int(websockets_version.split(".")[0]) >= 15:  # never route the loopback through a proxy
        connect_kwargs["proxy"] = None
    async with asyncio.timeout(20):
        async with serve(hub, "127.0.0.1", 0, compression=None, max_size=P.WS_MAX_SIZE_BYTES) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}", **connect_kwargs) as ws:
                await ws.send(dump_control(EXAMPLES["hello"]))
                welcome = parse_control(await ws.recv())
                assert welcome == EXAMPLES["welcome"]
                request = parse_control(await ws.recv())
                assert isinstance(request, CaptureRequest) and request.t_start_ns == T_NS
                acks: list[int] = []
                frames = iter_capture_chunks(
                    request.capture_id, request.channel_id, iq, request.t_start_ns + 7, request.sample_rate_hz,
                    request.center_hz, False, CLOCK,
                )
                for seq, frame in enumerate(frames):
                    await ws.send(frame)
                    if seq == 0:
                        await ws.send(_psd_frame(psd_values))
                        beat = Heartbeat(seq=seq, t_ns=T_NS + seq, clock=CLOCK, load=NodeLoad(cpu=0.1, queue=1))
                        await ws.send(dump_control(beat))
                        ack = parse_control(await ws.recv())
                        assert isinstance(ack, HeartbeatAck)
                        acks.append(ack.server_t_ns)
                    await asyncio.sleep(0)
                await ws.wait_closed()
    assert "error" not in outcome, outcome.get("error")
    assert acks == [T_NS + 1]
    assert outcome["kinds"] == ["capture_data", "psd", "heartbeat", "capture_data", "capture_data"]
    capture = outcome["capture"]
    assert capture.iq.tobytes() == iq.tobytes() and capture.t_first_ns == T_NS + 7
    assert capture.n_chunks == 3 and capture.clock == CLOCK
    assert outcome["psd"][1].tobytes() == psd_values.tobytes() and outcome["psd"][0].t_ns == T_NS
