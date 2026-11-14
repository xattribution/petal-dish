"""Tests of the server hub (PROTOCOL.md section A, server side), its settings and app.

The node in these tests is a scripted fake written here with the `websockets` client.
It talks to the real application served by uvicorn on an ephemeral loopback port, in
the test's own event loop, so a test can drive both ends: script the node and call the
hub. Everything the fake sends is built by hand from PROTOCOL.md (JSON dicts and
`struct`-packed PLB1 frames), not with the encoders of the package, so the hub is
checked against an independent construction of the wire format.

A few failure paths that a real socket cannot produce on demand (a node that stops
reading) use an in-memory stand-in for the WebSocket instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np
import pytest
import pytest_asyncio
import websockets
from pydantic import ValidationError

from petal_locate.protocol import ChannelAssignment, MonitorAssignment
from petal_locate.server import app as app_module
from petal_locate.server import hub as hub_module
from petal_locate.server import settings as settings_module
from petal_locate.server.app import HubProviders, build_server, create_app, uvicorn_config
from petal_locate.server.hub import CaptureFailed, Hub, PsdFrame, StatusEvent
from petal_locate.server.settings import (
    Marking,
    Settings,
    generate_node_tokens,
    load_node_tokens,
    node_token_env_var,
    node_tokens_from_env,
    tokens_equal,
)

WAIT_S = 4.0
"""Longest any single step of a test waits before it fails instead of hanging."""

TOKENS = {
    "site-a": "token-a",
    "site-b": "token-b",
    "site-ghost": "token-ghost",  # has a token but is not in the configuration
}
CLOCK = {"source": "gpsdo", "locked": True, "est_offset_ns": 12.5, "holdover_s": 0.0}
CAPS = {
    "min_hz": 1.0e8,
    "max_hz": 3.0e9,
    "max_sample_rate_hz": 2.0e6,
    "lookback_s": 30.0,
    "concurrent_reads": 4,
}
NODE_STATUS_KEYS = {
    "site_id",
    "node_id",
    "online",
    "latency_ms",
    "clock",
    "driver",
    "software",
    "channels",
    "connected_since",
}


# ----------------------------------------------------------------- the application side


def monitor_wire(monitor_id: str = "m0", *, nfft: int = 64, center_hz: float = 2.5e8) -> dict:
    """A monitor assignment exactly as PROTOCOL.md writes it on the wire."""
    return {
        "monitor_id": monitor_id,
        "center_hz": center_hz,
        "span_hz": 25000.0,
        "nfft": nfft,
        "navg": 4,
        "frame_rate_hz": 2.0,
    }


def channel_wire(channel_id: str, monitors: list[dict] | None = None) -> dict:
    """A channel assignment exactly as PROTOCOL.md writes it on the wire."""
    return {
        "channel_id": channel_id,
        "antenna_summary": "1.2 m parabolic",
        "monitors": monitors
        if monitors is not None
        else [monitor_wire("m0"), monitor_wire("m1", center_hz=2.6e8)],
    }


class World:
    """What the application tells the hub; tests edit it to play configuration changes."""

    def __init__(self) -> None:
        self.sites: dict[str, list[dict]] = {
            "site-a": [channel_wire("a1"), channel_wire("a2")],
            "site-b": [channel_wire("b1")],
            "site-untokened": [channel_wire("u1")],  # configured, but holds no token
        }
        self.version = 7
        self.simulation: dict[str, Any] = {"enabled": False}

    def assignments_for(self, site_id: str) -> list[ChannelAssignment] | None:
        channels = self.sites.get(site_id)
        if channels is None:
            return None
        return [
            ChannelAssignment(
                channel_id=c["channel_id"],
                antenna_summary=c["antenna_summary"],
                monitors=[MonitorAssignment(**m) for m in c["monitors"]],
            )
            for c in channels
        ]

    def providers(self) -> HubProviders:
        return HubProviders(
            assignments_for=self.assignments_for,
            config_version=lambda: self.version,
            simulation_info=lambda: self.simulation,
        )


def make_settings(**overrides: Any) -> Settings:
    """Settings for a test server: ephemeral port, known tokens, short capture grace."""
    values: dict[str, Any] = {
        "config_path": "unused.yaml",
        "port": 0,
        "node_tokens": dict(TOKENS),
        "capture_grace_s": 0.05,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class Served:
    """A running server and handles on both of its sides."""

    app: Any
    hub: Hub
    world: World
    port: int

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/node"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@contextlib.asynccontextmanager
async def serving(
    settings: Settings | None = None, world: World | None = None
) -> AsyncIterator[Served]:
    """Serve the application with uvicorn on an ephemeral port in this event loop.

    On exit the server is stopped and the test fails if any task is left running.
    """
    settings = settings if settings is not None else make_settings()
    world = world if world is not None else World()
    app = create_app(settings, hub_providers=world.providers())
    server = build_server(app, settings, log_config=None)
    before = asyncio.all_tasks()
    task = asyncio.create_task(server.serve(), name="test-uvicorn")
    try:
        deadline = time.monotonic() + WAIT_S
        while not server.started:
            if task.done():
                task.result()
                raise AssertionError("uvicorn stopped before it started")
            if time.monotonic() > deadline:
                raise AssertionError("uvicorn did not start")
            await asyncio.sleep(0.002)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield Served(app=app, hub=app.state.hub, world=world, port=port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15.0)
    leftover = [
        t
        for t in asyncio.all_tasks() - before
        if t is not asyncio.current_task() and not t.done()
    ]
    assert not leftover, f"tasks left running: {leftover}"


@pytest_asyncio.fixture
async def served() -> AsyncIterator[Served]:
    """A server with the default settings and world."""
    async with serving() as running:
        yield running


# ------------------------------------------------------------------------ the fake node


def plb1(header: dict, payload: bytes = b"") -> bytes:
    """A binary frame per PROTOCOL.md: magic, header length, JSON header, payload."""
    raw = json.dumps(header).encode("utf-8")
    return b"PLB1" + struct.pack("<I", len(raw)) + raw + payload


def psd_frame(
    channel_id: str,
    monitor_id: str,
    seq: int,
    values: np.ndarray,
    *,
    center_hz: float = 2.5e8,
    t_ns: int | None = None,
) -> bytes:
    """A spectrum frame: header of PROTOCOL.md plus float32[nfft] little-endian."""
    header = {
        "type": "psd",
        "channel_id": channel_id,
        "monitor_id": monitor_id,
        "t_ns": time.time_ns() if t_ns is None else t_ns,
        "center_hz": center_hz,
        "span_hz": 25000.0,
        "nfft": int(values.shape[0]),
        "navg": 4,
        "seq": seq,
        "units": "dB",
    }
    return plb1(header, values.astype("<f4").tobytes())


def capture_frames(
    request: dict,
    iq: np.ndarray,
    *,
    chunk: int = 32768,
    t_first_ns: int | None = None,
    sample_rate_hz: float | None = None,
    center_hz: float | None = None,
    overflow: bool = False,
    clock: dict | None = None,
    total: int | None = None,
    channel_id: str | None = None,
) -> list[bytes]:
    """The `capture_data` frames answering `request`, complex64 interleaved I, Q."""
    total = int(iq.shape[0]) if total is None else total
    frames = []
    for seq, offset in enumerate(range(0, int(iq.shape[0]), chunk)):
        part = iq[offset : offset + chunk]
        header = {
            "type": "capture_data",
            "capture_id": request["capture_id"],
            "channel_id": channel_id or request["channel_id"],
            "seq": seq,
            "last": offset + len(part) == total,
            "t_first_ns": request["t_start_ns"] if t_first_ns is None else t_first_ns,
            "sample_rate_hz": sample_rate_hz or request["sample_rate_hz"],
            "center_hz": center_hz or request["center_hz"],
            "n_samples_total": total,
            "offset": offset,
            "n": len(part),
            "overflow": overflow,
            "clock": CLOCK if clock is None else clock,
        }
        frames.append(plb1(header, part.astype("<c8").tobytes()))
    return frames


def make_iq(n: int, seed: int = 1) -> np.ndarray:
    """Deterministic complex64 noise."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)


class FakeNode:
    """A scripted node: sends exactly what a test tells it to, one frame at a time."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._seq = 0

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(cls, served: Served, **options: Any) -> AsyncIterator[FakeNode]:
        options.setdefault("compression", None)
        async with websockets.connect(
            served.ws_url, proxy=None, open_timeout=WAIT_S, close_timeout=1.0, **options
        ) as ws:
            yield cls(ws)

    async def send(self, message: dict | str | bytes) -> None:
        await self.ws.send(json.dumps(message) if isinstance(message, dict) else message)

    async def recv(self) -> dict:
        raw = await asyncio.wait_for(self.ws.recv(), WAIT_S)
        assert isinstance(raw, str), "the server sends text frames only"
        return json.loads(raw)

    async def expect(self, mtype: str) -> dict:
        message = await self.recv()
        assert message["type"] == mtype, message
        return message

    async def hello(
        self,
        site_id: str = "site-a",
        node_id: str = "node-1",
        *,
        token: str | None = None,
        protocol: int = 1,
    ) -> None:
        await self.send(
            {
                "type": "hello",
                "protocol": protocol,
                "node_id": node_id,
                "site_id": site_id,
                "token": TOKENS.get(site_id, "none") if token is None else token,
                "software": "fake-node 0.1",
                "driver": "noise",
                "clock": CLOCK,
                "t_ns": time.time_ns(),
            }
        )

    async def ack(self, assignment: dict, *, failed: dict[str, str] | None = None) -> None:
        """Send `assign_ack` for a `welcome` or `assign`; `failed` maps channel -> error."""
        failed = failed or {}
        channels = []
        for channel in assignment["channels"]:
            channel_id = channel["channel_id"]
            if channel_id in failed:
                error = failed[channel_id]
                channels.append({"channel_id": channel_id, "ok": False, "error": error})
            else:
                channels.append({"channel_id": channel_id, "ok": True, "capabilities": CAPS})
        await self.send(
            {
                "type": "assign_ack",
                "config_version": assignment["config_version"],
                "ok": not failed,
                "channels": channels,
            }
        )

    async def heartbeat(
        self, *, rtt_ms: float | None = None, clock: dict | None = None
    ) -> dict:
        """Send one heartbeat and return its `heartbeat_ack`."""
        self._seq += 1
        beat = {
            "type": "heartbeat",
            "seq": self._seq,
            "t_ns": time.time_ns(),
            "clock": CLOCK if clock is None else clock,
            "load": {"cpu": 0.25, "queue": 0},
        }
        if rtt_ms is not None:
            beat["rtt_ms"] = rtt_ms
        await self.send(beat)
        ack = await self.expect("heartbeat_ack")
        assert ack["seq"] == self._seq
        return ack

    async def sync(self) -> None:
        """Wait until the server has processed everything sent so far.

        Frames are handled in order, so the ack of a heartbeat sent now proves it.
        """
        await self.heartbeat()

    async def join(
        self, site_id: str = "site-a", node_id: str = "node-1", *, ack: bool = True
    ) -> dict:
        """Handshake: hello, welcome and (unless told not to) assign_ack. Returns welcome."""
        await self.hello(site_id, node_id)
        welcome = await self.expect("welcome")
        if ack:
            await self.ack(welcome)
            await self.sync()
        return welcome

    async def closed(self) -> int:
        """Read until the server closes the socket; returns the close code it sent."""

        async def drain() -> None:
            with contextlib.suppress(websockets.ConnectionClosed):
                while True:
                    await self.ws.recv()

        await asyncio.wait_for(drain(), WAIT_S)
        return self.ws.close_code


@contextlib.contextmanager
def logged(name: str, level: int) -> Any:
    """Collect the messages of one logger at `level` and above, whatever the global
    logging configuration does with them; yields the list of formatted messages."""
    messages: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger(name)
    handler = Collector(level)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield messages
    finally:
        logger.setLevel(previous)
        logger.removeHandler(handler)


async def eventually(predicate: Callable[[], bool], what: str = "condition") -> None:
    """Poll until `predicate()` holds; fail after `WAIT_S`."""
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} not reached within {WAIT_S} s")
        await asyncio.sleep(0.005)


def start_capture(
    hub: Hub,
    channel_id: str = "a1",
    n_samples: int = 1000,
    *,
    lead_s: float = 0.05,
    timeout_s: float = 5.0,
    sample_rate_hz: float = 50e3,
    center_hz: float = 2.5e8,
) -> asyncio.Task:
    """Call `hub.capture` in a task, as the tasking pipeline would."""
    t_start_ns = time.time_ns() + int(lead_s * 1e9)
    deadline_ns = t_start_ns + int(timeout_s * 1e9)
    return asyncio.create_task(
        hub.capture(channel_id, t_start_ns, n_samples, sample_rate_hz, center_hz, deadline_ns)
    )


async def failure_of(task: asyncio.Task) -> CaptureFailed:
    """The `CaptureFailed` a capture task ends with."""
    with pytest.raises(CaptureFailed) as info:
        await asyncio.wait_for(task, WAIT_S)
    return info.value


# ============================================================================ settings


def test_settings_defaults_and_validation(tmp_path) -> None:
    settings = Settings(config_path=tmp_path / "system.yaml")
    assert settings.host == "127.0.0.1"
    assert settings.scenario_path is None and settings.api_token is None
    assert settings.marking is None and settings.node_tokens == {}
    assert settings.heartbeat_s == 2.0  # PROTOCOL.md: heartbeat_s (default 2)

    full = Settings(
        config_path="c.yaml",
        scenario_path="s.yaml",
        data_dir=tmp_path,
        host="0.0.0.0",
        port=9000,
        node_tokens={"tokyo": "secret-tokyo"},
        api_token="secret-api",
        marking={"level": "UNCLASSIFIED", "line": "UNCLASSIFIED // EXERCISE"},
    )
    assert full.marking == Marking(level="UNCLASSIFIED", line="UNCLASSIFIED // EXERCISE")
    assert "secret-tokyo" not in repr(full) and "secret-api" not in repr(full)

    for bad in (
        {"port": 70000},
        {"port": -1},
        {"heartbeat_s": 0.0},
        {"node_tokens": {"tokyo": ""}},
        {"node_tokens": {"": "x"}},
        {"api_token": ""},
        {"marking": {"level": "", "line": "x"}},
        {"no_such_setting": 1},
    ):
        with pytest.raises(ValidationError):
            Settings(config_path="c.yaml", **bad)
    with pytest.raises(ValidationError):
        Settings()  # the configuration path is required


def test_node_token_env_var_name() -> None:
    assert node_token_env_var("tokyo-1") == "PETAL_NODE_TOKEN_TOKYO_1"
    assert node_token_env_var("Site.a b") == "PETAL_NODE_TOKEN_SITE_A_B"
    assert node_token_env_var("sité") == "PETAL_NODE_TOKEN_SIT_"
    with pytest.raises(ValueError, match="site_id"):
        node_token_env_var("")


def test_node_token_lookup_order_and_environment(monkeypatch) -> None:
    settings = make_settings(node_tokens={"tokyo-1": "from-file"})
    monkeypatch.setenv("PETAL_NODE_TOKEN_TOKYO_1", "from-env")
    monkeypatch.setenv("PETAL_NODE_TOKEN_NAHA", "naha-env")
    monkeypatch.setenv("PETAL_NODE_TOKEN_SEOUL", "")
    assert settings.node_token("tokyo-1") == "from-file"  # the explicit map wins
    assert settings.node_token("naha") == "naha-env"  # the environment is the fallback
    assert settings.node_token("seoul") is None  # an empty variable is no token
    assert settings.node_token("busan") is None
    assert settings.check_node_token("naha", "naha-env")
    assert not settings.check_node_token("naha", "naha-env ")
    assert not settings.check_node_token("seoul", "")
    assert not settings.check_node_token("busan", "anything")
    # An explicit environment mapping replaces os.environ.
    assert settings.node_token("naha", {"PETAL_NODE_TOKEN_NAHA": "other"}) == "other"
    assert settings.with_node_tokens({"naha": "n2"}).node_token("naha") == "n2"


def test_token_comparison_is_constant_time(monkeypatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(settings_module.hmac, "compare_digest", spy)
    settings = make_settings(api_token="api-secret")
    assert settings.check_node_token("site-a", "token-a")
    assert not settings.check_node_token("site-a", "token-b")
    assert not settings.check_node_token("nowhere", "token-a")  # compared all the same
    assert settings.check_api_token("api-secret")
    assert not settings.check_api_token("api-secre")
    assert not settings.check_api_token(None)
    assert len(calls) == 6
    # Digests of equal length are compared, so the token length does not show either.
    assert all(len(a) == len(b) == 32 for a, b in calls)
    assert make_settings().check_api_token(None)  # no API token configured: open

    assert tokens_equal("é\udc80", "é\udc80")  # a lone surrogate from JSON must not raise
    assert not tokens_equal("a", "b")
    with pytest.raises(TypeError):
        tokens_equal("a", b"a")  # type: ignore[arg-type]


def test_load_node_tokens_file(tmp_path) -> None:
    good = tmp_path / "tokens.yaml"
    good.write_text("tokyo-1: abc\nnaha: 'def ghi'\n", encoding="utf-8")
    assert load_node_tokens(good) == {"tokyo-1": "abc", "naha": "def ghi"}

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_node_tokens(empty) == {}

    cases = {
        "- a\n- b\n": "must be a map",
        "tokyo: 12345\n": "token of site 'tokyo' must be a non-empty string",
        "tokyo: ''\n": "token of site 'tokyo'",
        "1: abc\n": "site id 1",
        "tokyo: [secret-value\n": "not valid YAML",
    }
    for text, expected in cases.items():
        bad = tmp_path / "bad.yaml"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=expected) as info:
            load_node_tokens(bad)
        assert "secret-value" not in str(info.value)  # token text never leaks
    with pytest.raises(ValueError, match="cannot be read"):
        load_node_tokens(tmp_path / "missing.yaml")


def test_node_tokens_from_env_and_generation() -> None:
    env = {
        "PETAL_NODE_TOKEN_A_B": "shared",
        "PETAL_NODE_TOKEN_TOKYO": "t",
        "PETAL_NODE_TOKEN_X": "",
    }
    assert node_tokens_from_env(["tokyo", "x", "absent"], env) == {"tokyo": "t"}
    with pytest.raises(ValueError, match="'a-b' and 'a_b'"):
        node_tokens_from_env(["a-b", "a_b"], env)

    tokens = generate_node_tokens(["a", "b", "c"])
    assert set(tokens) == {"a", "b", "c"}
    assert len(set(tokens.values())) == 3 and all(len(t) >= 32 for t in tokens.values())
    assert generate_node_tokens(["a"])["a"] != tokens["a"]


# ================================================================================= app


def test_uvicorn_config_follows_the_protocol() -> None:
    settings = make_settings(host="127.0.0.1", port=0)
    app = create_app(settings)
    # log_config=None: building a uvicorn Config otherwise reconfigures global logging.
    config = uvicorn_config(app, settings, log_config=None)
    assert config.ws_per_message_deflate is False
    assert config.ws_max_size >= 1024 * 1024
    assert (config.host, config.port) == ("127.0.0.1", 0)
    with pytest.raises(ValueError, match="ws_per_message_deflate"):
        uvicorn_config(app, settings, ws_per_message_deflate=True)
    with pytest.raises(ValueError, match="ws_max_size"):
        uvicorn_config(app, settings, ws_max_size=65536)
    assert isinstance(app.state.hub, Hub) and app.state.settings is settings


def test_server_package_exports_lazily() -> None:
    import petal_locate.server as server_package

    assert server_package.Hub is Hub
    assert server_package.Settings is Settings
    assert server_package.create_app is create_app
    assert server_package.CaptureFailed is CaptureFailed
    with pytest.raises(AttributeError):
        server_package.no_such_name  # noqa: B018


async def test_healthz_reports_liveness(served: Served) -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=WAIT_S) as client:
        reply = await client.get(f"{served.http_url}/healthz")
        assert reply.status_code == 200
        assert reply.json() == {"status": "ok", "version": "0.1.0", "nodes_online": 0}
        async with FakeNode.connect(served) as node:
            await node.join()
            reply = await client.get(f"{served.http_url}/healthz")
            assert reply.json()["nodes_online"] == 1


async def test_deflate_is_refused_and_the_largest_spectrum_frame_fits() -> None:
    world = World()
    world.sites["site-a"] = [channel_wire("a1", [monitor_wire("wide", nfft=131072)])]
    async with serving(world=world) as served:
        got: list[PsdFrame] = []
        served.hub.subscribe_psd(got.append)
        # The node offers permessage-deflate; the server must not take it.
        async with FakeNode.connect(served, compression="deflate") as node:
            assert node.ws.protocol.extensions == []
            assert "Sec-WebSocket-Extensions" not in node.ws.response.headers
            await node.join()
            values = np.linspace(-120.0, -60.0, 131072, dtype=np.float32)
            frame = psd_frame("a1", "wide", 0, values)
            assert 512 * 1024 < len(frame) < 1024 * 1024  # the largest frame of the protocol
            await node.send(frame)
            await node.sync()
            await eventually(lambda: len(got) == 1, "the 512 KiB spectrum frame")
            assert np.array_equal(got[0].psd_db, values)


def test_run_serves_under_uvicorn_and_closes_nodes_with_1001(monkeypatch) -> None:
    """`run(settings)` end to end in a thread: its own event loop (uvloop when
    installed), no deflate, and 1001 rather than uvicorn's 1012 when the server stops."""
    created: list[app_module.HubServer] = []
    real_build = app_module.build_server

    def spy(app: Any, settings: Settings, **overrides: Any) -> app_module.HubServer:
        server = real_build(app, settings, log_config=None, **overrides)
        created.append(server)
        return server

    monkeypatch.setattr(app_module, "build_server", spy)
    world = World()
    thread = threading.Thread(
        target=app_module.run,
        args=(make_settings(),),
        kwargs={"hub_providers": world.providers()},
        daemon=True,
    )
    thread.start()

    async def node_session(server: app_module.HubServer) -> tuple[dict, int]:
        port = server.servers[0].sockets[0].getsockname()[1]
        served = Served(app=None, hub=None, world=world, port=port)  # type: ignore[arg-type]
        async with FakeNode.connect(served, compression="deflate") as node:
            assert node.ws.protocol.extensions == []
            welcome = await node.join()
            server.should_exit = True
            return welcome, await node.closed()

    try:
        deadline = time.monotonic() + 10.0
        while not (created and created[0].started):
            assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
            time.sleep(0.01)
        welcome, code = asyncio.run(node_session(created[0]))
    finally:
        if created:
            created[0].should_exit = True
        thread.join(15.0)
    assert not thread.is_alive()
    assert welcome["type"] == "welcome" and welcome["config_version"] == 7
    assert code == 1001


# =========================================================================== handshake


async def test_good_handshake(served: Served) -> None:
    hub = served.hub
    before_ns = time.time_ns()
    async with FakeNode.connect(served) as node:
        await node.hello("site-a", "node-1")
        welcome = await node.expect("welcome")
        after_ns = time.time_ns()

        assert set(welcome) == {
            "type",
            "protocol",
            "server_t_ns",
            "heartbeat_s",
            "config_version",
            "channels",
            "simulation",
        }
        assert welcome["protocol"] == 1
        assert type(welcome["server_t_ns"]) is int  # integer nanoseconds on the wire
        assert before_ns <= welcome["server_t_ns"] <= after_ns
        assert welcome["heartbeat_s"] == 2.0
        assert welcome["config_version"] == 7
        assert welcome["channels"] == served.world.sites["site-a"]
        assert welcome["simulation"] == {"enabled": False}

        assert hub.is_online("site-a") and not hub.is_online("site-b")
        assert hub.online_sites() == ["site-a"]
        assert hub.site_of("a1") == "site-a" and hub.site_of("b1") is None
        # Welcomed, but no assign_ack yet: nothing is taskable.
        assert not hub.channel_ready("a1") and hub.channel_capabilities("a1") is None

        await node.ack(welcome)
        await node.sync()
        assert hub.channel_ready("a1") and hub.channel_ready("a2")
        capabilities = hub.channel_capabilities("a1")
        assert capabilities is not None and capabilities.max_sample_rate_hz == 2.0e6
        assert capabilities.lookback_s == 30.0 and capabilities.concurrent_reads == 4

        (status,) = hub.node_status()
        assert set(status) == NODE_STATUS_KEYS
        assert status["site_id"] == "site-a" and status["node_id"] == "node-1"
        assert status["online"] is True
        assert status["clock"] == CLOCK
        assert (status["driver"], status["software"]) == ("noise", "fake-node 0.1")
        no_health = {  # what a window shows without a detection pipeline behind the hub
            "last_frame_age_s": None,
            "detection_frame_age_s": None,
            "blind": False,
            "blind_reason": None,
            "learning": None,
            "floor_raised_db": None,
            "note": None,
        }
        assert status["channels"] == [
            {
                "channel_id": channel_id,
                "ok": True,
                "last_frame_age_s": None,
                "windows": [{"monitor_id": "m0", **no_health}, {"monitor_id": "m1", **no_health}],
                "detection_problem": None,
            }
            for channel_id in ("a1", "a2")
        ]
        # ISO 8601 UTC, as PROTOCOL.md section B wants for times that are not *_ns.
        assert status["connected_since"].endswith("Z") and "T" in status["connected_since"]
        assert status["connected_since"][:4] == time.strftime("%Y", time.gmtime())
        json.dumps(hub.node_status())  # JSON-ready

    await eventually(lambda: not hub.is_online("site-a"), "offline after disconnect")
    (status,) = hub.node_status()
    assert status["online"] is False and status["connected_since"] is None
    assert status["node_id"] == "node-1" and status["clock"] == CLOCK  # last known
    assert [c["ok"] for c in status["channels"]] == [False, False]
    # Asking for named sites includes ones the hub has never seen.
    by_site = {s["site_id"]: s for s in hub.node_status(["site-b", "site-a"])}
    assert list(by_site) == ["site-b", "site-a"]
    assert by_site["site-b"]["online"] is False and by_site["site-b"]["node_id"] is None
    assert set(by_site["site-b"]) == NODE_STATUS_KEYS


async def test_welcome_carries_the_simulation_backplane() -> None:
    world = World()
    world.simulation = {"enabled": True, "scenario_url": "/api/v1/sim/world", "version": 3}
    async with serving(world=world) as served, FakeNode.connect(served) as node:
        welcome = await node.join(ack=False)
        assert welcome["simulation"] == {
            "enabled": True,
            "scenario_url": "/api/v1/sim/world",
            "version": 3,
        }


@pytest.mark.parametrize(
    ("hello", "reason"),
    [
        ({"site_id": "site-a", "token": "wrong"}, "bad_token"),
        ({"site_id": "site-a", "token": ""}, "bad_token"),
        # Configured site for which the server holds no token.
        ({"site_id": "site-untokened", "token": "anything"}, "bad_token"),
        # A site that does not exist is not told apart from a wrong token.
        ({"site_id": "site-nowhere", "token": "token-a"}, "bad_token"),
        # Token held, but the configuration has no such site.
        ({"site_id": "site-ghost", "token": "token-ghost"}, "unknown_site"),
        ({"site_id": "site-a", "token": "token-a", "protocol": 2}, "protocol_mismatch"),
        # A mismatch wins even when the token is wrong: version 2 may carry it elsewhere.
        ({"site_id": "site-a", "token": "wrong", "protocol": 0}, "protocol_mismatch"),
    ],
)
async def test_hello_reject_reasons(served: Served, hello: dict, reason: str) -> None:
    async with FakeNode.connect(served) as node:
        await node.hello(
            hello["site_id"], "node-1", token=hello["token"], protocol=hello.get("protocol", 1)
        )
        assert await node.expect("reject") == {"type": "reject", "reason": reason}
        assert await node.closed() == hub_module.CLOSE_CODE_REJECTED
    assert served.hub.online_sites() == []
    assert served.hub.node_status() == []
    assert served.hub.counters()[f"rejected_{reason}"] == 1


async def test_hello_with_a_token_from_the_environment(served: Served, monkeypatch) -> None:
    # site-untokened is configured but absent from Settings.node_tokens.
    monkeypatch.setenv("PETAL_NODE_TOKEN_SITE_UNTOKENED", "from-the-environment")
    async with FakeNode.connect(served) as node:
        await node.hello("site-untokened", "node-u", token="from-the-environment")
        welcome = await node.expect("welcome")
        assert [c["channel_id"] for c in welcome["channels"]] == ["u1"]
    async with FakeNode.connect(served) as node:
        await node.hello("site-untokened", "node-u", token="from-the-environmenT")
        assert (await node.expect("reject"))["reason"] == "bad_token"


async def test_hello_of_another_version_need_not_fit_version_one(served: Served) -> None:
    async with FakeNode.connect(served) as node:
        await node.send({"type": "hello", "protocol": 2, "identity": {"site": "site-a"}})
        assert await node.expect("reject") == {"type": "reject", "reason": "protocol_mismatch"}
        assert await node.closed() == hub_module.CLOSE_CODE_REJECTED


async def test_different_node_id_is_a_duplicate(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as first, FakeNode.connect(served) as second:
        await first.join("site-a", "node-1")
        pending = start_capture(hub, "a1")
        request = await first.expect("capture_request")

        await second.hello("site-a", "node-2")
        assert await second.expect("reject") == {"type": "reject", "reason": "duplicate_node"}
        assert await second.closed() == hub_module.CLOSE_CODE_REJECTED

        # The session that holds the site is untouched, capture included.
        assert hub.node_status()[0]["node_id"] == "node-1"
        iq = make_iq(1000)
        for frame in capture_frames(request, iq):
            await first.send(frame)
        result = await asyncio.wait_for(pending, WAIT_S)
        assert np.array_equal(result.iq, iq)


async def test_same_node_id_replaces_the_live_session(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as old, FakeNode.connect(served) as new:
        await old.join("site-a", "node-1")
        pending = start_capture(hub, "a1")
        request = await old.expect("capture_request")

        await new.hello("site-a", "node-1")
        welcome = await new.expect("welcome")

        assert await old.closed() == 4001
        failure = await failure_of(pending)
        assert failure.code == "replaced"
        assert failure.capture_id == request["capture_id"] and failure.channel_id == "a1"

        # The new session is the live one, and starts without acknowledged channels.
        assert hub.is_online("site-a")
        assert not hub.channel_ready("a1")
        assert (await failure_of(start_capture(hub, "a1"))).code == "not_ready"
        await new.ack(welcome)
        await new.sync()
        assert hub.channel_ready("a1")

        again = start_capture(hub, "a1", 500)
        request = await new.expect("capture_request")
        iq = make_iq(500, seed=2)
        await new.send(capture_frames(request, iq)[0])
        assert np.array_equal((await asyncio.wait_for(again, WAIT_S)).iq, iq)
        assert hub.counters()["sessions_replaced"] == 1
        assert len(hub.node_status()) == 1


async def test_messages_before_hello(served: Served) -> None:
    async with FakeNode.connect(served) as node:
        await node.send({"type": "heartbeat", "seq": 1})  # malformed and out of place
        error = await node.expect("error")
        assert error["code"] == "bad_message" and isinstance(error["detail"], str)
        await node.send(plb1({"type": "psd"}))  # binary before hello
        assert (await node.expect("error"))["code"] == "bad_message"
        await node.send({"type": "future_feature", "x": 1})  # unknown: ignored
        # Two malformed messages do not close the socket: a correct hello still works.
        welcome = await node.join()
        assert welcome["config_version"] == 7

    async with FakeNode.connect(served) as node:
        for _ in range(3):
            await node.send("{not json")
            assert (await node.expect("error"))["code"] == "bad_message"
        assert await node.closed() == hub_module.CLOSE_CODE_BAD_MESSAGES


async def test_no_hello_in_time_closes_the_socket() -> None:
    async with serving(make_settings(hello_timeout_s=0.15)) as served:
        async with FakeNode.connect(served) as node:
            started = time.monotonic()
            assert await node.closed() == hub_module.CLOSE_CODE_SILENT
            assert 0.1 <= time.monotonic() - started < 2.0
        assert served.hub.counters()["hello_timeouts"] == 1


async def test_a_failing_provider_closes_with_internal_error(caplog) -> None:
    world = World()
    world.simulation = {"enabled": True, "no_such_field": 1}  # an application bug
    async with serving(world=world) as served:
        with caplog.at_level(logging.ERROR, logger="petal_locate.server.hub"):
            async with FakeNode.connect(served) as node:
                await node.hello()
                assert await node.closed() == hub_module.CLOSE_CODE_INTERNAL
        assert not served.hub.is_online("site-a")
        assert any(record.exc_info for record in caplog.records)  # logged with its traceback
        assert "token-a" not in caplog.text  # and without the token


# ======================================================================= assignment ack


async def test_channel_is_not_taskable_before_its_assign_ack(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        welcome = await node.join(ack=False)
        failure = await failure_of(start_capture(hub, "a1"))
        assert failure.code == "not_ready" and "acknowledged" in failure.message

        # An ack for another configuration version does not count.
        await node.ack({**welcome, "config_version": 6})
        await node.sync()
        assert not hub.channel_ready("a1")
        assert hub.counters()["assign_ack_stale"] == 1

        # The real ack: a1 opened, a2 did not.
        await node.ack(welcome, failed={"a2": "no such device"})
        await node.sync()
        assert hub.channel_ready("a1") and not hub.channel_ready("a2")
        assert hub.channel_capabilities("a2") is None
        failure = await failure_of(start_capture(hub, "a2"))
        assert failure.code == "not_ready" and "no such device" in failure.message
        assert [c["ok"] for c in hub.node_status()[0]["channels"]] == [True, False]

        # No capture_request was sent for any of the refused captures.
        task = start_capture(hub, "a1", 10)
        assert (await node.expect("capture_request"))["channel_id"] == "a1"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_ack_that_omits_a_channel_leaves_it_unready(served: Served) -> None:
    async with FakeNode.connect(served) as node:
        welcome = await node.join(ack=False)
        only_a1 = [c for c in welcome["channels"] if c["channel_id"] == "a1"]
        partial = {**welcome, "channels": only_a1}
        await node.ack(partial)
        await node.sync()
        assert served.hub.channel_ready("a1") and not served.hub.channel_ready("a2")
        failure = await failure_of(start_capture(served.hub, "a2"))
        assert failure.code == "not_ready" and "did not mention" in failure.message


# ============================================================================ captures


async def test_capture_multi_chunk_is_reassembled_bit_exactly(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        n = 70000  # three chunks at the protocol's 256 KiB (32768 samples) per chunk
        t_start_ns = time.time_ns() + 50_000_000
        deadline_ns = t_start_ns + 5_000_000_000
        task = asyncio.create_task(
            # numpy scalars, as a caller doing arithmetic with numpy would pass them
            hub.capture(
                "a1", np.int64(t_start_ns), np.int64(n), np.float64(50e3), 2.5e8, deadline_ns
            )
        )
        request = await node.expect("capture_request")
        assert set(request) == {
            "type",
            "capture_id",
            "channel_id",
            "t_start_ns",
            "n_samples",
            "sample_rate_hz",
            "center_hz",
            "deadline_ns",
        }
        assert request["channel_id"] == "a1" and request["n_samples"] == n
        assert request["t_start_ns"] == t_start_ns and type(request["t_start_ns"]) is int
        assert request["deadline_ns"] == deadline_ns and type(request["deadline_ns"]) is int
        assert request["sample_rate_hz"] == 50e3 and request["center_hz"] == 2.5e8

        iq = make_iq(n)
        unlocked = {**CLOCK, "locked": False, "holdover_s": 3.5}
        frames = capture_frames(
            request,
            iq,
            # What the node actually did need not equal the request.
            t_first_ns=t_start_ns + 1234,
            sample_rate_hz=50_000.5,
            center_hz=2.5e8 + 10.0,
            overflow=True,
            clock=unlocked,
        )
        assert len(frames) == 3
        await node.send(frames[0])
        await node.heartbeat()  # heartbeats interleave with the chunks
        await node.send(frames[1])
        await node.send(psd_frame("a1", "m0", 0, np.zeros(64, dtype=np.float32)))
        await node.send(frames[2])

        result = await asyncio.wait_for(task, WAIT_S)
        assert result.iq.dtype == np.complex64 and result.iq.shape == (n,)
        assert result.iq.tobytes() == iq.tobytes()  # bit-exact
        assert result.t_first_ns == t_start_ns + 1234 and type(result.t_first_ns) is int
        assert result.sample_rate_hz == 50_000.5 and result.center_hz == 2.5e8 + 10.0
        assert result.overflow is True
        assert result.clock.locked is False and result.clock.holdover_s == 3.5
        assert result.channel_id == "a1" and result.capture_id == request["capture_id"]
        counters = hub.counters()
        assert counters["capture_completed"] == 1 and counters["capture_requested"] == 1


async def test_capture_error_fails_the_capture(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        task = start_capture(hub, "a1")
        request = await node.expect("capture_request")
        await node.send(
            {
                "type": "capture_error",
                "capture_id": request["capture_id"],
                "channel_id": "a1",
                "code": "too_late",
                "message": "interval no longer buffered",
            }
        )
        failure = await failure_of(task)
        assert (failure.code, failure.message) == ("too_late", "interval no longer buffered")
        assert failure.capture_id == request["capture_id"]

        # After partial data, capture_error voids the capture; a code this version
        # does not know is passed through (a newer node may add codes).
        task = start_capture(hub, "a1", 40000)
        request = await node.expect("capture_request")
        frames = capture_frames(request, make_iq(40000))
        await node.send(frames[0])
        await node.send(
            {
                "type": "capture_error",
                "capture_id": request["capture_id"],
                "channel_id": "a1",
                "code": "thermal_shutdown",
            }
        )
        assert (await failure_of(task)).code == "thermal_shutdown"
        # The rest of the voided capture is stale now and is discarded quietly.
        await node.send(frames[1])
        await node.sync()
        assert hub.counters()["capture_data_stale"] == 1
        assert hub.is_online("site-a")


async def test_node_disconnect_mid_capture_fails_it_at_once(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        task = start_capture(hub, "a1", 70000, timeout_s=30.0)
        request = await node.expect("capture_request")
        await node.send(capture_frames(request, make_iq(70000))[0])
        await node.sync()
        started = time.monotonic()
        node.ws.transport.abort()  # the TCP connection is cut, no close frame
        failure = await failure_of(task)
        assert failure.code == "disconnected"
        assert time.monotonic() - started < 2.0  # at once, not at the 30 s deadline
    await eventually(lambda: not hub.is_online("site-a"))
    assert (await failure_of(start_capture(hub, "a1"))).code == "offline"


async def test_capture_deadline(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        started = time.monotonic()
        task = start_capture(hub, "a1", 40000, lead_s=0.0, timeout_s=0.25)
        request = await node.expect("capture_request")
        frames = capture_frames(request, make_iq(40000))
        await node.send(frames[0])  # the last chunk never comes in time
        failure = await failure_of(task)
        elapsed = time.monotonic() - started
        assert failure.code == "deadline" and "32768 of 40000" in failure.message
        assert 0.25 <= elapsed < 2.0  # deadline plus the 0.05 s grace of these settings
        # The node is told that the capture is no longer wanted.
        assert await node.expect("capture_cancel") == {
            "type": "capture_cancel",
            "capture_id": request["capture_id"],
        }
        # The late chunk is discarded and the session carries on.
        await node.send(frames[1])
        await node.sync()
        assert hub.counters()["capture_data_stale"] == 1

        # A deadline that has already passed fails without tasking the node.
        now = time.time_ns()
        with pytest.raises(CaptureFailed) as info:
            await hub.capture("a1", now - 2_000_000_000, 100, 50e3, 2.5e8, now - 1_000_000_000)
        assert info.value.code == "deadline"
        assert (await node.heartbeat())["type"] == "heartbeat_ack"  # nothing else was sent


async def test_stale_capture_frames_are_discarded(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        ghost = {
            "capture_id": "c-from-an-earlier-session",
            "channel_id": "a1",
            "t_start_ns": time.time_ns(),
            "sample_rate_hz": 50e3,
            "center_hz": 2.5e8,
        }
        # More than three in a row: they are not malformed and must not close the session.
        for frame in capture_frames(ghost, make_iq(500), chunk=100):
            await node.send(frame)
        await node.send(
            {
                "type": "capture_error",
                "capture_id": "c-from-an-earlier-session",
                "channel_id": "a1",
                "code": "cancelled",
                "message": "",
            }
        )
        assert (await node.heartbeat())["type"] == "heartbeat_ack"  # no error came back
        counters = hub.counters()
        assert counters["capture_data_stale"] == 5 and counters["capture_error_stale"] == 1
        assert counters.get("bad_messages", 0) == 0
        assert hub.is_online("site-a")


async def test_ninth_concurrent_capture_waits_for_a_slot(served: Served) -> None:
    """R4: the node's MAX_CONCURRENT_CAPTURES is shared by every caller. Live, two events
    starting together had 4 captures at one node fail 'limit' and one event was then
    solved from 2 rows. A capture over the limit now waits for a slot (no request goes
    out meanwhile) and fails 'limit' only when its deadline passes first."""
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        tasks = [start_capture(hub, "a1" if i % 2 else "a2", 100) for i in range(8)]
        requests = [await node.expect("capture_request") for _ in range(8)]
        assert len({r["capture_id"] for r in requests}) == 8

        # A ninth with a short deadline: no slot frees in time.
        failure = await failure_of(start_capture(hub, "a1", 100, lead_s=0.0, timeout_s=0.3))
        assert failure.code == "limit" and "8 captures pending until the deadline" in failure.message
        assert (await node.heartbeat())["type"] == "heartbeat_ack"  # no ninth request went out

        # A ninth with time to spare waits: still nothing is sent ...
        ninth = start_capture(hub, "a1", 100)
        await asyncio.sleep(0.2)
        assert not ninth.done()
        assert (await node.heartbeat())["type"] == "heartbeat_ack"
        assert hub.counters()["capture_queued"] == 2
        # ... until one finishes: then it goes out at once and completes normally.
        iq = make_iq(100)
        await node.send(capture_frames(requests[0], iq)[0])
        assert np.array_equal((await asyncio.wait_for(tasks[0], WAIT_S)).iq, iq)
        request = await node.expect("capture_request")
        assert request["capture_id"] not in {r["capture_id"] for r in requests}
        await node.send(capture_frames(request, iq)[0])
        assert np.array_equal((await asyncio.wait_for(ninth, WAIT_S)).iq, iq)
        for task in tasks[1:]:
            task.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        # Each withdrawn capture is cancelled at the node (advisory).
        cancelled = {(await node.expect("capture_cancel"))["capture_id"] for _ in range(7)}
        assert len(cancelled) == 7 and hub.counters()["capture_cancelled"] == 7


async def test_capture_requests_that_cannot_be_made(served: Served) -> None:
    hub = served.hub
    now = time.time_ns()
    later = now + 5_000_000_000
    async with FakeNode.connect(served) as node:
        await node.join()
        # 64 MiB per capture: one sample more is refused before anything is sent.
        with pytest.raises(CaptureFailed) as info:
            await hub.capture("a1", now, 8 * 1024 * 1024 + 1, 2e6, 2.5e8, later)
        assert info.value.code == "too_large" and "67108872 bytes" in info.value.message
        with pytest.raises(CaptureFailed) as info:
            await hub.capture("nowhere", now, 100, 50e3, 2.5e8, later)
        assert info.value.code == "unknown_channel"
        with pytest.raises(CaptureFailed) as info:
            await hub.capture("b1", now, 100, 50e3, 2.5e8, later)  # site-b never connected
        assert info.value.code == "unknown_channel"

        # Invalid arguments are the caller's mistake: ValueError naming the field.
        for arguments, name in (
            (("a1", now, 0, 50e3, 2.5e8, later), "n_samples"),
            (("a1", now, 100.5, 50e3, 2.5e8, later), "n_samples"),
            (("a1", now, True, 50e3, 2.5e8, later), "n_samples"),
            (("a1", float(now), 100, 50e3, 2.5e8, later), "t_start_ns"),
            (("a1", now, 100, 0.0, 2.5e8, later), "sample_rate_hz"),
            (("a1", now, 100, "50e3", 2.5e8, later), "sample_rate_hz"),
            (("a1", now, 100, 50e3, -1.0, later), "center_hz"),
            (("a1", later, 100, 50e3, 2.5e8, now), "deadline_ns"),
        ):
            with pytest.raises(ValueError, match=name):
                await hub.capture(*arguments)
        assert (await node.heartbeat())["type"] == "heartbeat_ack"  # nothing was sent


async def test_bad_capture_chunks_void_the_capture(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        # Chunks out of order.
        task = start_capture(hub, "a1", 3000)
        request = await node.expect("capture_request")
        frames = capture_frames(request, make_iq(3000), chunk=1000)
        await node.send(frames[0])
        await node.send(frames[2])
        failure = await failure_of(task)
        assert failure.code == "bad_data" and "out of order" in failure.message
        error = await node.expect("error")
        assert error["code"] == "bad_message" and "capture_data" in error["detail"]
        assert (await node.expect("capture_cancel"))["capture_id"] == request["capture_id"]

        # Chunks for another channel than the one requested.
        task = start_capture(hub, "a1", 1000)
        request = await node.expect("capture_request")
        await node.send(capture_frames(request, make_iq(1000), channel_id="a2")[0])
        failure = await failure_of(task)
        assert failure.code == "bad_data" and "channel_id" in failure.message
        await node.expect("error")
        await node.expect("capture_cancel")

        # A payload shorter than its header says: the third malformed frame in a row,
        # so this time the session is closed as well.
        task = start_capture(hub, "a1", 1000)
        request = await node.expect("capture_request")
        await node.send(capture_frames(request, make_iq(1000))[0][:-8])
        failure = await failure_of(task)
        assert failure.code == "bad_data" and "payload" in failure.message
        await node.expect("error")
        assert await node.closed() == hub_module.CLOSE_CODE_BAD_MESSAGES
    assert hub.counters()["capture_failed_bad_data"] == 3


async def test_capture_of_another_length_than_requested_is_void(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        # More samples than asked for: also how a node would exceed the 64 MiB limit
        # (the request can never ask for more). Fewer is refused just the same. The
        # frames are well-formed, so no `error` is sent and the session stays up
        # however often it happens.
        for total, sent in ((9_000_000, 1000), (9_000_000, 1000), (9_000_000, 1000), (600, 600)):
            task = start_capture(hub, "a1", 1000)
            request = await node.expect("capture_request")
            chunk = capture_frames(request, make_iq(sent), total=total)[0]
            await node.send(chunk)
            failure = await failure_of(task)
            assert failure.code == "bad_data"
            assert f"announced {total} samples, 1000 were requested" in failure.message
            assert await node.expect("capture_cancel") == {
                "type": "capture_cancel",
                "capture_id": request["capture_id"],
            }
        await node.send(chunk)  # more of a voided capture: discarded
        await node.sync()
        counters = hub.counters()
        assert counters["capture_length_mismatch"] == 4 and counters["capture_data_stale"] == 1
        assert counters.get("bad_messages", 0) == 0 and hub.is_online("site-a")


async def test_cancelling_the_caller_withdraws_the_capture(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        task = start_capture(hub, "a1", 1000)
        request = await node.expect("capture_request")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await node.expect("capture_cancel") == {
            "type": "capture_cancel",
            "capture_id": request["capture_id"],
        }
        await node.send(capture_frames(request, make_iq(1000))[0])  # too late: discarded
        await node.sync()
        assert hub.counters()["capture_data_stale"] == 1


# ================================================================ heartbeats, liveness


async def test_heartbeat_ack_and_round_trip_times(served: Served) -> None:
    hub = served.hub
    events: list[StatusEvent] = []
    hub.subscribe_status(events.append)
    async with FakeNode.connect(served) as node:
        await node.join(ack=False)
        assert hub.rtt_ms("site-a") is None  # nothing measured yet
        before_ns = time.time_ns()
        ack = await node.heartbeat()
        assert set(ack) == {"type", "seq", "server_t_ns"}
        assert type(ack["server_t_ns"]) is int
        assert before_ns <= ack["server_t_ns"] <= time.time_ns()
        assert hub.rtt_ms("site-a") is None and hub.node_status()[0]["latency_ms"] is None

        for rtt in (4.0, 31.5, 7.25):
            await node.heartbeat(rtt_ms=rtt)
        assert hub.rtt_ms("site-a") == 31.5  # the worst recent one, for tasking lead time
        assert hub.node_status()[0]["latency_ms"] == 7.25  # the latest, for display
        for _ in range(hub_module.RTT_WINDOW):
            await node.heartbeat(rtt_ms=2.0)
        assert hub.rtt_ms("site-a") == 2.0  # old samples age out
        assert hub.rtt_ms("site-b") is None

        # The clock losing lock is published and shows in the status.
        unlocked = {**CLOCK, "locked": False, "holdover_s": 12.0}
        await node.heartbeat(clock=unlocked)
        assert hub.node_status()[0]["clock"] == unlocked
        await eventually(lambda: any(e.kind == "clock" for e in events), "clock event")
        clock_event = next(e for e in events if e.kind == "clock")
        assert clock_event.status["clock"]["locked"] is False and clock_event.site_id == "site-a"


async def test_three_silent_heartbeat_periods_mark_the_node_offline(caplog) -> None:
    heartbeat_s = 0.3
    async with serving(make_settings(heartbeat_s=heartbeat_s)) as served:
        hub = served.hub
        async with FakeNode.connect(served) as node:
            welcome = await node.join()
            assert welcome["heartbeat_s"] == heartbeat_s
            pending = start_capture(hub, "a1", timeout_s=30.0)
            await node.expect("capture_request")

            # Any frame counts as liveness: spectrum frames alone, no heartbeats, keep
            # the node online for longer than three heartbeat periods.
            quiet = np.full(64, -100.0, dtype=np.float32)
            for seq in range(11):
                await node.send(psd_frame("a1", "m0", seq, quiet))
                await asyncio.sleep(0.1)
            assert hub.is_online("site-a") and not pending.done()

            # Then nothing at all.
            silent_from = time.monotonic()
            with caplog.at_level(logging.WARNING, logger="petal_locate.server.hub"):
                assert await node.closed() == hub_module.CLOSE_CODE_SILENT
            waited_s = time.monotonic() - silent_from
            # The last frame left up to 0.1 s (one sleep) before `silent_from`.
            assert 3 * heartbeat_s - 0.15 <= waited_s < 3 * heartbeat_s + 2.0
            # The server's own measure of the silence: three periods, not two or four.
            (record,) = [r for r in caplog.records if hasattr(r, "silent_s")]
            assert 3 * heartbeat_s <= record.silent_s < 4 * heartbeat_s

            failure = await failure_of(pending)
            assert failure.code == "offline"
            assert not hub.is_online("site-a")
            (status,) = hub.node_status()
            assert status["online"] is False and status["latency_ms"] is None
            assert hub.counters()["sessions_silent"] == 1
            age = hub.last_frame_age_s("a1", "m0")
            assert age is not None and age >= waited_s  # keeps growing while offline


# ================================================================== malformed messages


async def test_three_malformed_messages_in_a_row_close_the_session(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        pending = start_capture(hub, "a1")
        await node.expect("capture_request")
        bad = [
            "this is not JSON",
            json.dumps({"type": "heartbeat", "seq": "one"}),  # known type, wrong fields
            b"XXXX\x00\x00\x00\x00",  # binary frame with the wrong magic
        ]
        for message in bad:
            await node.send(message)
            error = await node.expect("error")
            assert set(error) == {"type", "code", "detail"}
            assert error["code"] == "bad_message" and error["detail"]
        assert await node.closed() == hub_module.CLOSE_CODE_BAD_MESSAGES
        assert (await failure_of(pending)).code == "disconnected"
        assert not hub.is_online("site-a")
        assert hub.counters()["bad_messages"] == 3


async def test_a_good_message_resets_the_malformed_count(served: Served) -> None:
    async with FakeNode.connect(served) as node:
        await node.join()
        malformed = [
            json.dumps({"no_type": True}),
            json.dumps({"type": "welcome", "protocol": 1}),  # known type, missing fields
            plb1({"type": "psd", "channel_id": "a1"}),  # header missing fields
            # A spectrum frame whose payload does not match its nfft.
            psd_frame("a1", "m0", 0, np.zeros(64, dtype=np.float32))[:-4],
            # A well-formed message that only a server sends.
            json.dumps({"type": "heartbeat_ack", "seq": 1, "server_t_ns": time.time_ns()}),
            # hello a second time.
            json.dumps(
                {
                    "type": "hello",
                    "protocol": 1,
                    "node_id": "node-1",
                    "site_id": "site-a",
                    "token": "token-a",
                    "software": "x",
                    "driver": "x",
                    "clock": CLOCK,
                    "t_ns": time.time_ns(),
                }
            ),
        ]
        ghost = {
            "capture_id": "c-not-pending",
            "channel_id": "a1",
            "t_start_ns": time.time_ns(),
            "sample_rate_hz": 50e3,
            "center_hz": 2.5e8,
        }
        # Well-formed frames of every kind, none of which is answered: a spectrum
        # frame, a chunk of a capture that is not pending, a message of unknown type.
        well_formed = [
            psd_frame("a1", "m0", 0, np.zeros(64, dtype=np.float32)),
            capture_frames(ghost, make_iq(10))[0],
            json.dumps({"type": "something_new"}),
        ]
        # Two bad, one good, again and again: never three in a row, never closed.
        rounds = zip(malformed[0::2], malformed[1::2], well_formed, strict=True)
        for first, second, good in rounds:
            await node.send(first)
            assert (await node.expect("error"))["code"] == "bad_message"
            await node.send(second)
            assert (await node.expect("error"))["code"] == "bad_message"
            await node.send(good)
        await node.heartbeat()  # still served; a heartbeat resets the count as well
        await node.send("{")
        await node.expect("error")
        await node.send("{")
        await node.expect("error")
        await node.heartbeat()
        assert served.hub.is_online("site-a")
        assert served.hub.counters()["bad_messages"] == 8


async def test_the_token_reaches_neither_a_reply_nor_the_server_log(
    served: Served, caplog
) -> None:
    # Everything the server side logs, at DEBUG: the hub, the protocol package and
    # uvicorn, whose websockets layer traces frames at this level.
    with (
        caplog.at_level(logging.DEBUG, logger="petal_locate"),
        logged("uvicorn.error", logging.DEBUG) as traces,
    ):
        async with FakeNode.connect(served) as node:
            # A malformed hello (fields missing) whose token sits where a trace shows it.
            await node.send(
                {"type": "hello", "protocol": 1, "site_id": "site-a", "token": "token-a-SECRET"}
            )
            error = await node.expect("error")
            assert "SECRET" not in json.dumps(error)
            await node.hello("site-a", "node-1", token="wrong-SECRET")
            reject = await node.expect("reject")
            assert "SECRET" not in json.dumps(reject)
            await node.closed()
        async with FakeNode.connect(served) as node:
            await node.send(
                {"type": "hello", "protocol": 1, "site_id": "site-a", "token": "token-a-SECRET"}
            )
            await node.expect("error")
            await node.join()
    server_log = caplog.text + "\n".join(traces)
    # Frame tracing was on: what the server sent shows, what the node sent does not.
    assert any(line.startswith("> TEXT") and "welcome" in line for line in traces)
    assert not any(line.startswith("< TEXT") for line in traces)
    assert "malformed message" in server_log and "rejected hello" in server_log
    assert "SECRET" not in server_log and "token-a" not in server_log


def test_inbound_text_trace_filter() -> None:
    drop = app_module.InboundTextTraceFilter().filter

    def record(level: int, message: str, *args: Any) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.error", level, __file__, 1, message, args, None)

    assert not drop(record(logging.DEBUG, "< %s", "TEXT '{\"type\": \"hello\"...' [80 bytes]"))
    assert not drop(record(logging.DEBUG, "< %s", "CONT 'ken\": \"secret\"}' [20 bytes]"))
    assert drop(record(logging.DEBUG, "> %s", "TEXT '{\"type\":\"welcome\"...' [300 bytes]"))
    assert drop(record(logging.DEBUG, "< %s", "BINARY 50 4c 42 31 ... [4096 bytes]"))
    assert drop(record(logging.INFO, "< TEXT is fine above debug"))
    assert drop(record(logging.DEBUG, "%s and %s", "too few arguments"))  # never raises


async def test_unknown_types_are_ignored_with_a_warning(served: Served, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="petal_locate.server.hub"):
        async with FakeNode.connect(served) as node:
            await node.join()
            for _ in range(4):  # more than three: unknown is not malformed
                await node.send({"type": "telemetry_v2", "temperature_c": 41.5})
                await node.send(plb1({"type": "waterfall", "rows": 2}, b"\x00" * 16))
            assert (await node.heartbeat())["type"] == "heartbeat_ack"  # no error replies
            assert served.hub.is_online("site-a")
            # Every one is counted; the warning is logged once per type and socket.
            warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 2
            assert "'telemetry_v2'" in warnings[0] and "site 'site-a'" in warnings[0]
            assert "'waterfall'" in warnings[1]
            assert served.hub.counters()["unknown_messages"] == 8

            # A node cannot flood the log with invented types either.
            caplog.clear()
            for i in range(40):
                await node.send({"type": f"invented_{i}"})
            await node.sync()
            warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 14 + 1  # 16 types per socket, then one closing remark
            assert "further ones" in warnings[-1]
            assert served.hub.counters()["unknown_messages"] == 48
            assert served.hub.is_online("site-a")


# ====================================================================== spectrum frames


async def test_psd_fanout_slow_subscriber_delays_nobody(served: Served) -> None:
    hub = served.hub
    fast: list[tuple[str, int]] = []
    payloads: dict[tuple[str, int], np.ndarray] = {}
    slow: list[tuple[str, int]] = []
    slow_entered = asyncio.Event()
    release_slow = asyncio.Event()

    async def slow_subscriber(frame: PsdFrame) -> None:
        slow.append((frame.monitor_id, frame.header.seq))
        slow_entered.set()
        await release_slow.wait()  # deliberately stuck until the test lets go

    def fast_subscriber(frame: PsdFrame) -> None:
        fast.append((frame.monitor_id, frame.header.seq))
        payloads[(frame.monitor_id, frame.header.seq)] = frame.psd_db

    slow_subscription = hub.subscribe_psd(slow_subscriber, name="slow")
    fast_subscription = hub.subscribe_psd(fast_subscriber, name="fast")

    async with FakeNode.connect(served) as node:
        await node.join()
        rng = np.random.default_rng(5)
        sent: dict[tuple[str, int], np.ndarray] = {}
        for seq in range(20):
            for monitor_id, center_hz in (("m0", 2.5e8), ("m1", 2.6e8)):
                values = rng.uniform(-130.0, -60.0, 64).astype(np.float32)
                sent[(monitor_id, seq)] = values
                await node.send(psd_frame("a1", monitor_id, seq, values, center_hz=center_hz))
        # The session keeps answering while the slow subscriber sits in its first call...
        assert (await node.heartbeat())["type"] == "heartbeat_ack"
        # ...and the fast subscriber has every frame, in order, bit for bit.
        await eventually(lambda: len(fast) == 40, "all frames at the fast subscriber")
        assert fast == [(m, seq) for seq in range(20) for m in ("m0", "m1")]
        assert all(np.array_equal(payloads[key], sent[key]) for key in sent)
        assert not payloads[("m0", 0)].flags.writeable  # shared between subscribers
        assert slow_entered.is_set() and slow == [("m0", 0)]
        assert slow_subscription.waiting == 2  # one slot per (channel, monitor)

        # Let the slow one go: it gets only the newest frame of each window.
        release_slow.set()
        await eventually(lambda: len(slow) == 3, "newest frames at the slow subscriber")
        await asyncio.sleep(0.02)
        assert slow == [("m0", 0), ("m1", 19), ("m0", 19)]
        assert slow_subscription.dropped == 37 and slow_subscription.delivered == 3
        assert fast_subscription.dropped == 0 and fast_subscription.delivered == 40

        age = hub.last_frame_age_s("a1", "m0")
        assert age is not None and 0.0 <= age < WAIT_S
        assert hub.last_frame_age_s("a2", "m0") is None
        channels = {c["channel_id"]: c for c in hub.node_status()[0]["channels"]}
        assert channels["a1"]["last_frame_age_s"] is not None
        assert channels["a2"]["last_frame_age_s"] is None

        # After unsubscribing nothing more is delivered.
        fast_subscription.unsubscribe()
        await node.send(psd_frame("a1", "m0", 99, sent[("m0", 0)]))
        await node.sync()
        await eventually(lambda: ("m0", 99) in slow)
        assert ("m0", 99) not in fast and not fast_subscription.active


async def test_psd_frames_that_do_not_belong_are_dropped(served: Served) -> None:
    hub = served.hub
    got: list[PsdFrame] = []
    hub.subscribe_psd(got.append)
    values = np.full(64, -90.0, dtype=np.float32)
    async with FakeNode.connect(served) as node:
        welcome = await node.join(ack=False)
        await node.send(psd_frame("a1", "m0", 0, values))  # before the assign_ack
        await node.ack(welcome, failed={"a2": "driver"})
        await node.send(psd_frame("a2", "m0", 1, values))  # channel that did not open
        await node.send(psd_frame("b1", "m0", 2, values))  # another site's channel
        await node.send(psd_frame("a1", "m9", 3, values))  # window that is not assigned
        await node.send(psd_frame("a1", "m0", 4, np.zeros(128, dtype=np.float32)))  # wrong nfft
        await node.send(psd_frame("a1", "m0", 5, values))  # this one belongs
        await node.sync()
        await eventually(lambda: len(got) == 1)
        assert got[0].header.seq == 5 and got[0].site_id == "site-a"
        assert got[0].channel_id == "a1" and got[0].monitor_id == "m0"
        assert type(got[0].t_ns) is int and type(got[0].received_ns) is int
        counters = hub.counters()
        assert counters["psd_dropped_not_ready"] == 2
        assert counters["psd_dropped_unassigned"] == 2
        assert counters["psd_dropped_shape"] == 1
        assert counters["psd_frames"] == 1
        assert hub.last_frame_age_s("b1", "m0") is None  # nothing recorded for site-b
        assert hub.is_online("site-a")  # droppable frames are not protocol errors


async def test_a_failing_subscriber_is_logged_and_delivery_goes_on(served: Served, caplog) -> None:
    hub = served.hub
    seen: list[int] = []

    def fragile(frame: PsdFrame) -> None:
        if frame.header.seq == 0:
            raise RuntimeError("subscriber bug")
        seen.append(frame.header.seq)

    subscription = hub.subscribe_psd(fragile)
    values = np.zeros(64, dtype=np.float32)
    with caplog.at_level(logging.ERROR, logger="petal_locate.server.hub"):
        async with FakeNode.connect(served) as node:
            await node.join()
            await node.send(psd_frame("a1", "m0", 0, values))
            await node.sync()
            await eventually(lambda: subscription.errors == 1)
            await node.send(psd_frame("a1", "m0", 1, values))
            await node.sync()
            await eventually(lambda: seen == [1])
    assert "subscriber bug" in caplog.text
    with pytest.raises(TypeError):
        hub.subscribe_psd("not callable")  # type: ignore[arg-type]


async def test_status_events(served: Served) -> None:
    hub = served.hub
    events: list[StatusEvent] = []
    hub.subscribe_status(events.append)
    async with FakeNode.connect(served) as node:
        await node.join()
        await eventually(lambda: len(events) == 2)
    await eventually(lambda: len(events) == 3)
    assert [(e.kind, e.reason) for e in events] == [
        ("online", "hello"),
        ("channels", "assign_ack"),
        ("offline", "disconnected"),
    ]
    assert all(e.site_id == "site-a" and set(e.status) == NODE_STATUS_KEYS for e in events)
    assert all(type(e.t_ns) is int for e in events)
    assert [e.status["online"] for e in events] == [True, True, False]
    assert [c["ok"] for c in events[0].status["channels"]] == [False, False]
    assert [c["ok"] for c in events[1].status["channels"]] == [True, True]


async def test_status_events_of_a_replaced_session_arrive_in_order(served: Served) -> None:
    """The old session going offline and the new one coming online happen in the same
    instant for the same site; a subscriber must still be told both, in that order."""
    hub = served.hub
    events: list[StatusEvent] = []
    subscription = hub.subscribe_status(events.append)
    async with FakeNode.connect(served) as old, FakeNode.connect(served) as new:
        await old.join("site-a", "node-1")
        await new.hello("site-a", "node-1")
        welcome = await new.expect("welcome")
        assert await old.closed() == 4001
        await new.ack(welcome)
        await new.sync()
        await eventually(lambda: len(events) == 5, "five status events")
        assert [(e.kind, e.reason) for e in events] == [
            ("online", "hello"),
            ("channels", "assign_ack"),
            ("offline", "replaced"),
            ("online", "hello"),
            ("channels", "assign_ack"),
        ]
        assert [e.status["online"] for e in events] == [True, True, False, True, True]
        assert [e.t_ns for e in events] == sorted(e.t_ns for e in events)
        assert subscription.dropped == 0


async def test_status_backlog_is_thinned_to_the_newest_event_of_each_site(served: Served) -> None:
    """A status subscriber that has fallen far behind does not grow a queue without
    bound: beyond `STATUS_BACKLOG` waiting events it keeps the newest of each site."""
    hub = served.hub
    seen: list[StatusEvent] = []
    gate = asyncio.Event()

    async def stuck(event: StatusEvent) -> None:
        seen.append(event)
        await gate.wait()

    subscription = hub.subscribe_status(stuck)
    flips = hub_module.STATUS_BACKLOG + 6
    async with FakeNode.connect(served) as a, FakeNode.connect(served) as b:
        await a.join("site-a", "node-a")  # the subscriber is now stuck on `online`
        await b.join("site-b", "node-b")
        locked = True
        for _ in range(flips):  # every heartbeat changes the lock state: one event each
            locked = not locked
            await a.heartbeat(clock={**CLOCK, "locked": locked})
        assert len(seen) == 1 and seen[0].kind == "online"
        assert 2 <= subscription.waiting <= hub_module.STATUS_BACKLOG
        published = 2 + 2 + flips  # online + channels for each site, then the flips
        assert subscription.dropped == published - 1 - subscription.waiting > 0
        waiting = subscription.waiting
        gate.set()
        await eventually(lambda: len(seen) == 1 + waiting, "the backlog to be delivered")
        # whatever was thinned away, each site ends on its newest state, in order
        last = {site: [e for e in seen if e.site_id == site][-1] for site in ("site-a", "site-b")}
        assert (last["site-a"].kind, last["site-a"].status["clock"]["locked"]) == ("clock", locked)
        assert (last["site-b"].kind, last["site-b"].reason) == ("channels", "assign_ack")
        assert [e.t_ns for e in seen] == sorted(e.t_ns for e in seen)
    subscription.unsubscribe()


# ================================================================ configuration changes


async def test_reassign_reaches_the_node_and_fails_affected_captures(served: Served) -> None:
    hub, world = served.hub, served.world
    world.sites["site-a"] = [channel_wire("a1"), channel_wire("a2"), channel_wire("a4")]
    async with FakeNode.connect(served) as node:
        await node.join()
        on_unchanged = start_capture(hub, "a1", 600)
        on_changed = start_capture(hub, "a2")
        on_removed = start_capture(hub, "a4")
        requests = {}
        for _ in range(3):
            request = await node.expect("capture_request")
            requests[request["channel_id"]] = request
        await node.send(psd_frame("a4", "m0", 0, np.zeros(64, dtype=np.float32)))
        await node.sync()
        assert hub.last_frame_age_s("a4", "m0") is not None

        # The configuration changes: a2 gets another window, a4 goes, a3 comes.
        world.sites["site-a"] = [
            channel_wire("a1"),
            channel_wire("a2", [monitor_wire("m0", nfft=128)]),
            channel_wire("a3"),
        ]
        world.version = 8
        assert await hub.reassign() == ["site-a"]

        assign = await node.expect("assign")
        assert assign == {"type": "assign", "config_version": 8, "channels": world.sites["site-a"]}
        for task in (on_changed, on_removed):
            failure = await failure_of(task)
            assert failure.code == "reassigned"
        assert not on_unchanged.done()  # captures on unchanged channels go on

        # Nothing is taskable until the node acknowledges version 8.
        assert not any(hub.channel_ready(c) for c in ("a1", "a2", "a3"))
        assert hub.site_of("a3") == "site-a" and hub.site_of("a4") is None
        assert hub.last_frame_age_s("a4", "m0") is None  # forgotten with its channel
        await node.send({"type": "assign_ack", "config_version": 7, "ok": True, "channels": []})
        await node.sync()
        assert not hub.channel_ready("a1")  # an ack of the old version does not count
        await node.ack(assign)
        await node.sync()
        assert all(hub.channel_ready(c) for c in ("a1", "a2", "a3"))
        assert not hub.channel_ready("a4")

        # The capture that survived the reassignment completes.
        iq = make_iq(600, seed=3)
        await node.send(capture_frames(requests["a1"], iq)[0])
        assert np.array_equal((await asyncio.wait_for(on_unchanged, WAIT_S)).iq, iq)

        # Nothing changed since: nothing is sent, and channels stay ready.
        assert await hub.reassign() == []
        assert await hub.reassign(["site-a", "site-b"]) == []
        assert (await node.heartbeat())["type"] == "heartbeat_ack"
        assert hub.channel_ready("a1")


async def test_reassign_closes_the_session_of_a_removed_site(served: Served) -> None:
    hub, world = served.hub, served.world
    async with FakeNode.connect(served) as a, FakeNode.connect(served) as b:
        await a.join("site-a", "node-a")
        await b.join("site-b", "node-b")
        pending = start_capture(hub, "b1")
        await b.expect("capture_request")
        del world.sites["site-b"]
        world.version = 8
        assert await hub.reassign() == ["site-a"]
        assert (await a.expect("assign"))["config_version"] == 8
        assert await b.closed() == hub_module.CLOSE_CODE_REJECTED
        assert (await failure_of(pending)).code == "reassigned"
        assert hub.online_sites() == ["site-a"]
        assert [s["site_id"] for s in hub.node_status()] == ["site-a"]
        assert hub.site_of("b1") is None
    # The removed site is turned away when it comes back.
    async with FakeNode.connect(served) as b:
        await b.hello("site-b", "node-b")
        assert (await b.expect("reject"))["reason"] == "unknown_site"


async def test_reassign_updates_what_is_known_of_offline_sites(served: Served) -> None:
    hub, world = served.hub, served.world
    async with FakeNode.connect(served) as node:
        await node.join()
    await eventually(lambda: not hub.is_online("site-a"))
    world.sites["site-a"] = [channel_wire("a9")]
    world.version = 8
    assert await hub.reassign() == []  # nobody to send to
    assert hub.site_of("a9") == "site-a" and hub.site_of("a1") is None
    assert [c["channel_id"] for c in hub.node_status()[0]["channels"]] == ["a9"]
    assert (await failure_of(start_capture(hub, "a9"))).code == "offline"


async def test_sim_update_reaches_every_node(served: Served) -> None:
    hub = served.hub
    async with FakeNode.connect(served) as a, FakeNode.connect(served) as b:
        await a.join("site-a", "node-a")
        await b.join("site-b", "node-b", ack=False)
        effective_ns = time.time_ns() + 2_000_000_000
        assert await hub.announce_sim_update(4, effective_ns) == ["site-a", "site-b"]
        for node in (a, b):
            update = await node.expect("sim_update")
            assert update == {"type": "sim_update", "version": 4, "effective_ns": effective_ns}
            assert type(update["effective_ns"]) is int
        with pytest.raises(ValueError, match="effective_ns"):
            await hub.announce_sim_update(5, 1.5e18)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await hub.announce_sim_update(-1, effective_ns)


async def test_a_sim_update_that_meets_unopened_channels_is_sent_again_after_the_ack(served: Served) -> None:
    """K1: a node hands `sim_update` only to channels that are open. One announced while
    the node was still opening them (between welcome and assign_ack, as happens when a
    scenario is loaded in the first seconds of a session) was lost, and the node's radios
    stayed on the old world for good. The hub sends the newest update again after the
    ack; a node that had acknowledged already gets it once."""
    hub = served.hub
    async with FakeNode.connect(served) as a, FakeNode.connect(served) as b:
        welcome = await a.join("site-a", "node-a", ack=False)
        await b.join("site-b", "node-b")
        await hub.announce_sim_update(4, time.time_ns() + 10**9)
        await hub.announce_sim_update(5, time.time_ns() + 2 * 10**9)
        assert (await a.expect("sim_update"))["version"] == 4
        assert (await a.expect("sim_update"))["version"] == 5
        assert [(await b.expect("sim_update"))["version"] for _ in range(2)] == [4, 5]
        await a.ack(welcome)
        again = await a.expect("sim_update")  # the newest, after the ack
        assert again["version"] == 5 and type(again["effective_ns"]) is int
        await a.heartbeat()  # nothing else follows: the next message is the heartbeat's ack
        await b.heartbeat()  # site-b had its channels open: no second copy
        assert hub.counters()["sim_update_resent"] == 1
        # A reassignment closes and reopens channels: the same holds until its ack.
        served.world.version = 8
        await hub.reassign(["site-a"])
        assign = await a.expect("assign")
        await hub.announce_sim_update(6, time.time_ns() + 10**9)
        assert (await a.expect("sim_update"))["version"] == 6
        await a.ack(assign)
        assert (await a.expect("sim_update"))["version"] == 6
        await a.heartbeat()
        assert hub.counters()["sim_update_resent"] == 2


async def test_spectrum_frames_stamped_far_in_the_future_are_dropped_and_warned_once(served: Served) -> None:
    """SRV-1: one frame stamped minutes ahead became the window's newest time, and the
    detection pipeline then refused every correct frame; status still showed a fresh
    frame. The hub now drops such frames before they count."""
    hub = served.hub
    got: list[PsdFrame] = []
    hub.subscribe_psd(got.append)
    values = np.full(64, -90.0, dtype=np.float32)
    with logged("petal_locate.server.hub", logging.WARNING) as messages:
        async with FakeNode.connect(served) as node:
            await node.join()
            ahead = time.time_ns() + 300 * 10**9
            await node.send(psd_frame("a1", "m0", 0, values, t_ns=ahead))
            await node.send(psd_frame("a1", "m1", 1, values, t_ns=ahead + 10**9))
            await node.sync()
            assert hub.last_frame_age_s("a1", "m0") is None  # not counted as the newest frame
            await node.send(psd_frame("a1", "m0", 2, values, t_ns=time.time_ns() + 30 * 10**9))  # within a minute
            await node.send(psd_frame("a1", "m0", 3, values))
            await node.sync()
            await eventually(lambda: len(got) == 2)
    assert [f.header.seq for f in got] == [2, 3]
    assert hub.counters()["psd_dropped_future"] == 2
    future = [m for m in messages if "ahead of the server clock" in m]
    assert len(future) == 1 and "site-a" in future[0]


async def test_the_hub_stamps_and_judges_times_on_the_clock_it_is_given() -> None:
    """K8 with SRV-1: with a simulated start the server clock is shifted; the hub takes it
    (no patching of its module) for welcome, heartbeat acks, frame receipt and the check
    of frames from the future, so a simulated node's frames are not refused."""
    offset_ns = 3 * 86_400 * 10**9
    async with serving(make_settings(sim_time_offset_ns=offset_ns)) as running:
        hub = running.hub
        assert hub_module.time is time  # the module is not patched for a hub that takes a clock
        got: list[PsdFrame] = []
        hub.subscribe_psd(got.append)
        async with FakeNode.connect(running) as node:
            welcome = await node.join()
            assert abs(welcome["server_t_ns"] - (time.time_ns() + offset_ns)) < 5 * 10**9
            ack = await node.heartbeat()
            assert abs(ack["server_t_ns"] - (time.time_ns() + offset_ns)) < 5 * 10**9
            values = np.zeros(64, dtype=np.float32)
            await node.send(psd_frame("a1", "m0", 0, values, t_ns=time.time_ns() + offset_ns))
            await node.sync()
            await eventually(lambda: len(got) == 1)
            assert abs(got[0].received_ns - (time.time_ns() + offset_ns)) < 5 * 10**9
            (status,) = hub.node_status()
            assert status["connected_since"][:10] == time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset_ns / 1e9))
    assert hub.counters().get("psd_dropped_future", 0) == 0


async def test_window_health_reaches_node_status_and_a_blind_window_makes_its_channel_not_ok(served: Served) -> None:
    hub = served.hub

    def health(channel_id: str, monitor_id: str) -> dict[str, Any] | None:
        if channel_id == "a2":
            raise RuntimeError("provider bug")
        if monitor_id == "m0":
            return {"detection_frame_age_s": 12.5, "blind": True, "blind_reason": "out_of_order",
                    "learning": False, "floor_raised_db": None, "note": "its frames are stamped before ones already accepted"}
        return {"detection_frame_age_s": 0.4, "blind": False, "blind_reason": None, "learning": True,
                "floor_raised_db": 9.5, "note": "its noise floor is 9.5 dB above the channel's other windows"}

    with pytest.raises(TypeError):
        hub.set_window_health("not callable")  # type: ignore[arg-type]
    hub.set_window_health(health)
    async with FakeNode.connect(served) as node:
        await node.join()
        (status,) = hub.node_status()
        a1, a2 = status["channels"]
        assert a1["ok"] is False and a2["ok"] is True  # a1 has a blind window
        m0, m1 = a1["windows"]
        assert (m0["monitor_id"], m0["blind"], m0["blind_reason"], m0["detection_frame_age_s"]) == ("m0", True, "out_of_order", 12.5)
        assert (m1["blind"], m1["learning"], m1["floor_raised_db"]) == (False, True, 9.5)
        assert "blind on m0" in a1["detection_problem"] and "m1 (+9.5 dB)" in a1["detection_problem"]
        assert a2["detection_problem"] is None and all(w["detection_frame_age_s"] is None for w in a2["windows"])
        assert hub.counters()["window_health_errors"] >= 2
        from petal_locate.server.api.schemas import NodeStatus

        NodeStatus.model_validate(status)  # the console contract takes it
    (status,) = hub.node_status()
    assert status["online"] is False and all(not c["ok"] for c in status["channels"])
    assert "blind" not in (status["channels"][0]["detection_problem"] or "")  # offline is not blind
    hub.set_window_health(None)


# ============================================================================= shutdown


async def test_close_ends_every_session_with_1001(served: Served) -> None:
    hub = served.hub
    delivered: list[StatusEvent] = []
    hub.subscribe_status(delivered.append)
    async with (
        FakeNode.connect(served) as a,
        FakeNode.connect(served) as b,
        FakeNode.connect(served) as silent,  # connected, has not said hello yet
    ):
        await a.join("site-a", "node-a")
        await b.join("site-b", "node-b")
        pending = start_capture(hub, "a1")
        await a.expect("capture_request")

        await asyncio.wait_for(hub.close(), WAIT_S)
        assert hub.closed
        for node in (a, b, silent):
            assert await node.closed() == 1001
        assert (await failure_of(pending)).code == "shutdown"
        assert hub.online_sites() == []
        assert (await failure_of(start_capture(hub, "a1"))).code == "shutdown"
        await asyncio.wait_for(hub.close(), WAIT_S)  # closing twice is harmless

    # While closed, a node that dials in is sent away with 1001 at once.
    async with FakeNode.connect(served) as late:
        assert await late.closed() == 1001
    async with httpx.AsyncClient(trust_env=False, timeout=WAIT_S) as client:
        assert (await client.get(f"{served.http_url}/healthz")).json()["status"] == "stopping"

    # start() opens the hub again; subscriptions survive the restart.
    hub.start()
    async with FakeNode.connect(served) as again:
        await again.join("site-a", "node-a")
        assert hub.is_online("site-a")
        await eventually(lambda: any(e.kind == "online" for e in delivered[-2:]))


# ============================================== failure paths a real socket cannot stage


@dataclass
class StubSocket:
    """In-memory stand-in for the Starlette WebSocket of a node.

    It behaves like the ASGI server does: `close` delivers a disconnect to `receive`,
    and both `send_text` and `close` wait while the peer is not reading (`reading`
    cleared), as a real socket does once its buffers are full.
    """

    client: tuple[str, int] = ("stub", 1)
    sent: list[dict] = field(default_factory=list)
    closed_with: tuple[int, str | None] | None = None
    broken: bool = False  # when set, sending raises as on a socket that has gone

    def __post_init__(self) -> None:
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        self.reading = asyncio.Event()
        self.reading.set()

    async def accept(self) -> None:
        return None

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def send_text(self, text: str) -> None:
        await self.reading.wait()
        if self.broken:
            raise RuntimeError("stub socket is gone")
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        await self.reading.wait()
        self.closed_with = (code, reason)
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    def feed(self, message: dict) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    def feed_bytes(self, frame: bytes) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": frame})

    def hang_up(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1006})


def stub_hello(site_id: str = "site-a", node_id: str = "node-1") -> dict:
    return {
        "type": "hello",
        "protocol": 1,
        "node_id": node_id,
        "site_id": site_id,
        "token": TOKENS[site_id],
        "software": "stub",
        "driver": "noise",
        "clock": CLOCK,
        "t_ns": time.time_ns(),
    }


def stub_heartbeat(seq: int) -> dict:
    return {
        "type": "heartbeat",
        "seq": seq,
        "t_ns": time.time_ns(),
        "clock": CLOCK,
        "load": {"cpu": 0.0, "queue": 0},
    }


def stub_hub(world: World | None = None) -> Hub:
    world = world or World()
    return Hub(
        make_settings(), world.assignments_for, lambda: world.version, lambda: world.simulation
    )


async def stub_join(hub: Hub, stub: StubSocket) -> asyncio.Task:
    """Run `hub.handle` on a stub and take it through hello, welcome and assign_ack."""
    task = asyncio.create_task(hub.handle(stub))  # type: ignore[arg-type]
    stub.feed(stub_hello())
    await eventually(lambda: bool(stub.sent), "welcome")
    welcome = stub.sent[0]
    assert welcome["type"] == "welcome"
    stub.feed(
        {
            "type": "assign_ack",
            "config_version": welcome["config_version"],
            "ok": True,
            "channels": [
                {"channel_id": c["channel_id"], "ok": True, "capabilities": CAPS}
                for c in welcome["channels"]
            ],
        }
    )
    await eventually(lambda: hub.channel_ready("a1"), "assign_ack processed")
    return task


async def test_a_node_that_stops_reading_is_dropped(monkeypatch, caplog) -> None:
    monkeypatch.setattr(hub_module, "CLOSE_TIMEOUT_S", 0.2)
    hub = stub_hub()
    stub = StubSocket()
    handler = await stub_join(hub, stub)
    pending = start_capture(hub, "a1", timeout_s=30.0)
    await eventually(lambda: stub.sent[-1]["type"] == "capture_request")

    stub.reading.clear()  # the node's buffers are full from here on
    beats = hub_module.OUTBOUND_QUEUE_LIMIT + 50
    acked_before = len(stub.sent)
    with caplog.at_level(logging.WARNING, logger="petal_locate.server.hub"):
        for seq in range(beats):
            stub.feed(stub_heartbeat(seq))
        # Acks pile up unsent; at the limit the hub gives the node up instead of
        # waiting for it or growing without bound.
        failure = await failure_of(pending)
        assert failure.code == "disconnected"
        assert not hub.is_online("site-a")
        await asyncio.wait_for(handler, WAIT_S)  # ends although the close frame cannot go out
    assert stub.closed_with is None and len(stub.sent) == acked_before
    assert "not reading" in caplog.text
    assert hub.counters()["sessions_congested"] == 1
    assert (await failure_of(start_capture(hub, "a1"))).code == "offline"
    await hub.close()


async def test_close_does_not_wait_forever_for_a_stuck_socket(monkeypatch) -> None:
    monkeypatch.setattr(hub_module, "CLOSE_TIMEOUT_S", 0.2)
    hub = stub_hub()
    stuck, healthy = StubSocket(), StubSocket(client=("stub", 2))
    stuck_handler = await stub_join(hub, stuck)
    healthy_handler = asyncio.create_task(hub.handle(healthy))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    stuck.reading.clear()
    started = time.monotonic()
    await hub.close()
    assert time.monotonic() - started < 2.0
    await asyncio.wait_for(asyncio.gather(stuck_handler, healthy_handler), WAIT_S)
    assert healthy.closed_with == (1001, "server shutting down")
    assert stuck.closed_with is None  # could not be told; abandoned to the ASGI server


async def test_a_burst_neither_starves_the_loop_nor_looks_like_congestion() -> None:
    """Frames that are all there at once (one TCP read can hold hundreds) must not
    hold the event loop, and their replies must not be mistaken for a node that
    does not read."""
    hub = stub_hub()
    stub = StubSocket()
    handler = await stub_join(hub, stub)
    turns = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal turns
        while not stop.is_set():
            await asyncio.sleep(0)
            turns += 1

    tick = asyncio.create_task(ticker())

    # Far more heartbeats than the outbound queue holds, all queued before the hub
    # runs. The node is reading, so each one is acked, in order.
    burst = 4 * hub_module.OUTBOUND_QUEUE_LIMIT
    for seq in range(burst):
        stub.feed(stub_heartbeat(seq))
    await eventually(lambda: len(stub.sent) == burst + 1, "every heartbeat acked")
    assert [ack["seq"] for ack in stub.sent[1:]] == list(range(burst))
    assert hub.is_online("site-a") and "sessions_congested" not in hub.counters()

    # Frames that need no reply (chunks of a capture that is not pending): the hub
    # still gives the event loop a turn for each of them.
    ghost = {
        "capture_id": "c-gone",
        "channel_id": "a1",
        "t_start_ns": time.time_ns(),
        "sample_rate_hz": 50e3,
        "center_hz": 2.5e8,
    }
    frames = capture_frames(ghost, make_iq(2000), chunk=1)
    turns_before = turns
    for frame in frames:
        stub.feed_bytes(frame)
    stub.feed(stub_heartbeat(burst))
    await eventually(lambda: stub.sent[-1]["seq"] == burst, "the flood handled")
    assert turns - turns_before >= len(frames)
    assert hub.counters()["capture_data_stale"] == len(frames)

    stop.set()
    await tick
    stub.hang_up()
    await asyncio.wait_for(handler, WAIT_S)
    assert not hub.is_online("site-a")
    await hub.close()


async def test_handle_survives_cancellation() -> None:
    """The ASGI server cancels the handler at a forced shutdown: the session must end
    cleanly, its captures fail and no task stay behind."""
    hub = stub_hub()
    stub = StubSocket()
    before = asyncio.all_tasks()
    handler = await stub_join(hub, stub)
    pending = start_capture(hub, "a1", timeout_s=30.0)
    await eventually(lambda: stub.sent[-1]["type"] == "capture_request")
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert (await failure_of(pending)).code == "disconnected"
    assert not hub.is_online("site-a")
    await hub.close()
    await asyncio.sleep(0)
    me = asyncio.current_task()
    assert not [t for t in asyncio.all_tasks() - before if not t.done() and t is not me]


async def test_a_send_that_fails_ends_the_session(monkeypatch) -> None:
    monkeypatch.setattr(hub_module, "CLOSE_TIMEOUT_S", 0.2)
    hub = stub_hub()
    stub = StubSocket()
    handler = await stub_join(hub, stub)
    pending = start_capture(hub, "a1", timeout_s=30.0)
    await eventually(lambda: stub.sent[-1]["type"] == "capture_request")
    stub.broken = True  # the next send raises; no disconnect is ever delivered
    stub.feed(stub_heartbeat(1))
    assert (await failure_of(pending)).code == "disconnected"
    await asyncio.wait_for(handler, WAIT_S)
    assert not hub.is_online("site-a") and stub.closed_with is None
    await hub.close()


async def test_capture_on_a_socket_that_is_closing_fails_as_disconnected() -> None:
    hub = stub_hub()
    stub = StubSocket()
    handler = await stub_join(hub, stub)
    # The instant between a close being requested and the session being taken down.
    hub._sessions["site-a"].conn.request_close(1001, "test")
    now = time.time_ns()
    with pytest.raises(CaptureFailed) as info:
        await hub.capture("a1", now, 100, 50e3, 2.5e8, now + 5_000_000_000)
    assert info.value.code == "disconnected" and info.value.capture_id is not None
    await asyncio.wait_for(handler, WAIT_S)
    assert stub.closed_with == (1001, "test")
    assert not any(m["type"] == "capture_request" for m in stub.sent)
    await hub.close()


async def test_site_id_arguments_must_be_lists(served: Served) -> None:
    with pytest.raises(ValueError, match="site_ids"):
        await served.hub.reassign("site-a")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="site_ids"):
        served.hub.node_status("site-a")  # type: ignore[arg-type]


def test_hub_and_its_subscriptions_survive_a_new_event_loop() -> None:
    """An application object can be started more than once (each `asyncio.run` is a
    new loop): nothing in the hub may stay bound to the loop it first ran on."""
    import gc

    hub = stub_hub()
    events: list[tuple[str, str]] = []
    problems: list[dict] = []
    hub.subscribe_status(lambda event: events.append((event.kind, event.reason)))

    async def one_run() -> None:
        # Anything a task of the hub dies of ends up here (when it is collected).
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _, context: problems.append(context))
        hub.start()
        stub = StubSocket()
        handler = await stub_join(hub, stub)
        await asyncio.sleep(0.01)
        await hub.close()
        await asyncio.wait_for(handler, WAIT_S)
        assert stub.closed_with == (1001, "server shutting down")
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(one_run())
    first = list(events)
    asyncio.run(one_run())
    assert first[:2] == [("online", "hello"), ("channels", "assign_ack")]
    assert events[len(first) :][:2] == [("online", "hello"), ("channels", "assign_ack")]
    assert problems == []


async def test_node_status_escapes_text_utf8_cannot_carry(served: Served) -> None:
    """SRV-2 defence in depth: node and provider text in a status (software, driver, a
    window note) is escaped where UTF-8 cannot carry it, so the status can be written
    whatever reaches it (the parser already refuses such text at the door)."""
    from petal_locate.server.api import schemas as api_schemas

    hub = served.hub
    async with FakeNode.connect(served) as node:
        await node.join()
        session = hub._sessions["site-a"]
        session.software = "petal-locate/\ud800"
        session.driver = "sim\udfff"
        hub.set_window_health(lambda channel_id, monitor_id: {"blind": True, "note": "no frames \ud800"})
        (status,) = [s for s in hub.node_status() if s["site_id"] == "site-a"]
        assert status["software"] == "petal-locate/\\ud800" and status["driver"] == "sim\\udfff"
        assert "no frames \\ud800" in status["channels"][0]["detection_problem"]
        api_schemas.NodeStatus.model_validate(status).model_dump_json()  # writable
