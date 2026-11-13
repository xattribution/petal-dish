"""End-to-end tests of the connection layer: the real `NodeAgent` against the real app.

Nothing here is scripted or faked on either side of the socket (the one exception is
the rogue client of the last test, which talks garbage to the real hub next to a real
node). The server is the application of `create_app` served by uvicorn
(`build_server`) on an ephemeral loopback port; the nodes are `NodeAgent`s with the
`noise` driver, two channels with two monitor windows each; both run in the test's
event loop, so a test can call the hub and look at the agents. The noise driver is
deterministic (its samples depend only on the seed, the channel and the first-sample
time), so everything that crosses the wire is checked bit for bit against an
independent regeneration.

Two tools make failures of the link reproducible:

* `Served.abort_node_sockets()` aborts the server's end of every node socket: a TCP
  reset without a closing handshake, which is what a killed server process (or a
  dropped NAT binding) looks like from the node.
* `Link` is a TCP relay with a rate limit from the node to the server: a slow uplink.

Every test stops its agents, links and servers; `serving` fails the test when a task
is still running afterwards.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import struct
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest
import websockets

from petal_locate.core.spectrum import welch_psd_db
from petal_locate.node.agent import DEFAULT_CHUNK_PAYLOAD_BYTES, NodeAgent
from petal_locate.node.clock import SystemClock
from petal_locate.node.drivers import get_driver
from petal_locate.node.drivers.base import Capture, CaptureError
from petal_locate.node.drivers.noise import NoiseDriver, noise_samples
from petal_locate.protocol import (
    CLOSE_CODE_REPLACED,
    MAX_CONCURRENT_CAPTURES,
    ChannelAssignment,
    MonitorAssignment,
)
from petal_locate.server.app import HubProviders, HubServer, build_server, create_app
from petal_locate.server.hub import CaptureFailed, Hub, PsdFrame, StatusEvent
from petal_locate.server.settings import Settings

NS = 1_000_000_000
WAIT_S = 8.0
"""Longest any single step of a test waits before it fails instead of hanging."""

HEARTBEAT_S = 0.5
"""Heartbeat period of the test servers, seconds (silence limit: three periods)."""

FRAME_RATE_HZ = 10.0
"""Frame rate of every monitor window, Hz."""

TOKENS = {
    "site-a": "token-of-site-a-9f31c2",
    "site-b": "token-of-site-b-5a07e4",
    "site-c": "token-of-site-c-c88d19",
    "site-ghost": "token-of-site-ghost-17b2",  # holds a token, is not in the configuration
}
SEEDS = {"site-a": 11, "site-b": 22, "site-c": 33}
"""Noise seed of each site's node: samples prove which node answered."""


# ------------------------------------------------------------------------ utilities


async def wait_until(
    predicate: Callable[[], Any], timeout: float = WAIT_S, what: str = "condition"
) -> Any:
    """Poll `predicate` until it is truthy; fail the test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        await asyncio.sleep(0.005)


def monitor(monitor_id: str, center_hz: float, **changes: Any) -> MonitorAssignment:
    """A monitor window: 1 MHz wide, 256 bins, 4 averages, 10 frames a second."""
    values: dict[str, Any] = dict(
        monitor_id=monitor_id,
        center_hz=center_hz,
        span_hz=1.0e6,
        nfft=256,
        navg=4,
        frame_rate_hz=FRAME_RATE_HZ,
    )
    values.update(changes)
    return MonitorAssignment(**values)


def channel(channel_id: str, *monitors: MonitorAssignment) -> ChannelAssignment:
    """A channel with the given windows; by default `m0` at 250 MHz and `m1` at 260 MHz."""
    return ChannelAssignment(
        channel_id=channel_id,
        antenna_summary="1.2 m parabolic",
        monitors=list(monitors) or [monitor("m0", 250.0e6), monitor("m1", 260.0e6)],
    )


class World:
    """What the application tells the hub; tests edit it to play configuration changes."""

    def __init__(self, sites: dict[str, list[ChannelAssignment]] | None = None) -> None:
        self.sites: dict[str, list[ChannelAssignment]] = (
            sites if sites is not None else {"site-a": [channel("a1"), channel("a2")]}
        )
        self.version = 5

    def providers(self) -> HubProviders:
        return HubProviders(
            assignments_for=lambda site_id: self.sites.get(site_id),
            config_version=lambda: self.version,
            simulation_info=lambda: {"enabled": False},
        )


@dataclass
class Served:
    """A running server and handles on it."""

    app: Any
    hub: Hub
    world: World
    server: HubServer
    port: int

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def abort_node_sockets(self) -> int:
        """Abort the server's end of every open socket; returns how many.

        The transport is torn down without a closing handshake (the peer sees a TCP
        reset), as when the server process is killed.
        """
        aborted = 0
        for protocol in list(self.server.server_state.connections):
            transport = getattr(protocol, "transport", None)
            if transport is not None:
                transport.abort()
                aborted += 1
        return aborted


@contextlib.asynccontextmanager
async def serving(
    world: World | None = None, *, check_tasks: bool = True, **settings: Any
) -> AsyncIterator[Served]:
    """Serve the real application with uvicorn in this event loop.

    settings: overrides of the `Settings` (port 0, the test tokens, a 0.5 s heartbeat
    and a short capture grace by default). On exit the server is shut down; with
    `check_tasks` the test fails when a task started inside is still running.
    """
    values: dict[str, Any] = {
        "config_path": "unused.yaml",
        "port": 0,
        "node_tokens": dict(TOKENS),
        "heartbeat_s": HEARTBEAT_S,
        "capture_grace_s": 0.25,
    }
    values.update(settings)
    config = Settings(**values)
    world = world if world is not None else World()
    app = create_app(config, hub_providers=world.providers())
    server = build_server(app, config, log_config=None)
    before = asyncio.all_tasks()
    task = asyncio.create_task(server.serve(), name="e2e-uvicorn")
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
        yield Served(app=app, hub=app.state.hub, world=world, server=server, port=port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20.0)
    if check_tasks:
        await asyncio.sleep(0.02)
        leftover = [
            t
            for t in asyncio.all_tasks() - before
            if t is not asyncio.current_task() and not t.done()
        ]
        assert not leftover, f"tasks left running: {[t.get_name() for t in leftover]}"


def make_agent(
    served: Served | str, site_id: str = "site-a", **options: Any
) -> NodeAgent:
    """A real agent with the noise driver, aimed at `served` (or at a URL).

    Defaults: the site's token and seed, node id `node-<site>`, no proxy and a quick
    reconnect schedule (0.05 s doubling to 0.4 s) so that tests need not wait.
    """
    values: dict[str, Any] = {
        "node_id": f"node-{site_id}",
        "driver": "noise",
        "driver_options": {"seed": SEEDS.get(site_id, 0)},
        "proxy": None,
        "backoff_base_s": 0.05,
        "backoff_max_s": 0.4,
        "close_timeout_s": 1.0,
    }
    values.update(options)
    token = values.pop("token", TOKENS.get(site_id, "no-token"))
    url = served if isinstance(served, str) else served.url
    return NodeAgent(url, site_id, token, **values)


@contextlib.asynccontextmanager
async def running(*agents: NodeAgent) -> AsyncIterator[None]:
    """Start the agents; stop every one of them on exit, whatever happened."""
    for agent in agents:
        agent.start()
    try:
        yield
    finally:
        results = await asyncio.gather(
            *(agent.stop(WAIT_S) for agent in agents), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def ready(served: Served, *channel_ids: str, timeout: float = WAIT_S) -> None:
    """Wait until the hub may task every one of the channels."""
    await wait_until(
        lambda: all(served.hub.channel_ready(cid) for cid in channel_ids),
        timeout,
        f"channels {channel_ids} ready",
    )


@dataclass
class Spectra:
    """Every spectrum frame a hub subscriber received, with its arrival time."""

    frames: list[PsdFrame] = field(default_factory=list)
    arrived: list[float] = field(default_factory=list)

    def __call__(self, frame: PsdFrame) -> None:
        self.frames.append(frame)
        self.arrived.append(time.monotonic())

    def of(self, channel_id: str, monitor_id: str, start: int = 0) -> list[PsdFrame]:
        return [
            frame
            for frame in self.frames[start:]
            if (frame.channel_id, frame.monitor_id) == (channel_id, monitor_id)
        ]

    def windows(self, start: int = 0) -> set[tuple[str, str]]:
        return {(frame.channel_id, frame.monitor_id) for frame in self.frames[start:]}

    def longest_gap_s(self, key: tuple[str, str], since: float, until: float) -> float:
        """Longest time without a frame of window `key` between two monotonic times."""
        times = [
            t
            for frame, t in zip(self.frames, self.arrived, strict=True)
            if (frame.channel_id, frame.monitor_id) == key and since <= t <= until
        ]
        edges = [since, *times, until]
        return max(b - a for a, b in zip(edges, edges[1:], strict=False))


@contextlib.contextmanager
def spectra(hub: Hub) -> Any:
    """Subscribe a collector to the hub's spectrum frames for the duration."""
    collector = Spectra()
    subscription = hub.subscribe_psd(collector, name="e2e-collector")
    try:
        yield collector
    finally:
        subscription.unsubscribe()


@contextlib.contextmanager
def statuses(hub: Hub) -> Any:
    """Collect the hub's status events for the duration."""
    events: list[StatusEvent] = []
    subscription = hub.subscribe_status(events.append, name="e2e-status")
    try:
        yield events
    finally:
        subscription.unsubscribe()


def capture_times(lead_s: float, n: int, fs: float, timeout_s: float = 6.0) -> tuple[int, int]:
    """`(t_start_ns, deadline_ns)` for a capture starting `lead_s` from now."""
    t_start_ns = time.time_ns() + round(lead_s * NS)
    return t_start_ns, max(t_start_ns, time.time_ns()) + round((n / fs + timeout_s) * NS)


class Link:
    """A TCP relay in front of the server with a rate limit on the node's uplink.

    up_bytes_per_s: throughput from the node to the server, bytes per second; None
        for no limit. The other direction is never limited.

    The relay reads from the node only as fast as it forwards and keeps its own
    buffers small, so the node sees the back-pressure of a slow link. `cut()` aborts
    every relayed connection (both ends get a reset); `freeze()` stops forwarding on
    the connections that exist, in both directions and without closing anything (a
    dropped NAT binding, a pulled cable). New connections work normally after either.
    """

    def __init__(self, target_port: int, up_bytes_per_s: float | None = None) -> None:
        self._target_port = target_port
        self._rate = up_bytes_per_s
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._flowing = asyncio.Event()  # of the connections accepted since the last freeze
        self._flowing.set()
        self.port = 0
        self.up_bytes = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def __aenter__(self) -> Link:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Inherited by accepted sockets: a small window, so that the node's kernel
        # cannot park megabytes in the relay.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024)
        listener.bind(("127.0.0.1", 0))
        self._server = await asyncio.start_server(self._relay, sock=listener, limit=8 * 1024)
        self.port = listener.getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        self.cut()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._server.wait_closed()

    def cut(self) -> int:
        """Abort every relayed connection; returns how many transports were aborted."""
        writers, self._writers = self._writers, set()
        for writer in writers:
            writer.transport.abort()
        return len(writers)

    def freeze(self) -> None:
        """Stop forwarding on every existing connection, silently and for good."""
        self._flowing.clear()
        self._flowing = asyncio.Event()
        self._flowing.set()

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._writers.add(writer)
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self._target_port)
        except OSError:
            writer.transport.abort()
            return
        self._writers.add(up_writer)
        flowing = self._flowing
        pumps = [
            asyncio.create_task(self._pump(reader, up_writer, self._rate, flowing, count=True)),
            asyncio.create_task(self._pump(up_reader, writer, None, flowing, count=False)),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for end in (writer, up_writer):
                self._writers.discard(end)
                end.transport.abort()

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        rate: float | None,
        flowing: asyncio.Event,
        *,
        count: bool,
    ) -> None:
        piece = 2048 if rate is not None else 65536
        try:
            while data := await reader.read(piece):
                await flowing.wait()  # frozen: what was read is never delivered
                writer.write(data)
                await writer.drain()
                if count:
                    self.up_bytes += len(data)
                if rate is not None:
                    await asyncio.sleep(len(data) / rate)
        except (ConnectionError, OSError):
            return


def close_codes(caplog: pytest.LogCaptureFixture, agent: NodeAgent) -> list[int | None]:
    """The WebSocket close codes with which the agent's sessions ended, in order.

    Read from the agent's own log: its "reconnecting" line carries the outcome and
    the close code as structured fields.
    """
    return [
        record.code
        for record in caplog.records
        if record.name == "petal_locate.node.agent"
        and getattr(record, "node_id", None) == agent.node_id
        and getattr(record, "outcome", None) == "closed"
    ]


class Records(logging.Handler):
    """Collects every log record of the root logger, formatted, at any level."""

    def __init__(self) -> None:
        super().__init__(level=1)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        }
        self.lines.append(f"{record.name} {record.getMessage()} {extras!r}")


# ------------------------------------------------------------- handshake to ready


async def test_handshake_to_ready_with_capabilities_status_and_latency() -> None:
    async with serving() as served:
        hub = served.hub
        agent = make_agent(served)
        with statuses(hub) as events:
            async with running(agent):
                await agent.wait_online(WAIT_S)
                await ready(served, "a1", "a2")
                assert hub.is_online("site-a") and hub.online_sites() == ["site-a"]
                assert hub.site_of("a1") == hub.site_of("a2") == "site-a"
                for channel_id in ("a1", "a2"):
                    capabilities = hub.channel_capabilities(channel_id)
                    assert capabilities is not None
                    # what NoiseDriver advertises, carried by assign_ack
                    assert capabilities.min_hz == 0.0 and capabilities.max_hz == 100e9
                    assert capabilities.max_sample_rate_hz == 4e6
                    assert capabilities.lookback_s == 30.0
                    assert capabilities.concurrent_reads == 4
                # the round trip of a heartbeat is measured by the node and reported
                # with the next one
                await wait_until(lambda: hub.rtt_ms("site-a") is not None, what="a round trip")
                assert 0.0 <= hub.rtt_ms("site-a") < 500.0
                assert agent.last_rtt_ms is not None and agent.last_rtt_ms < 500.0
                (status,) = hub.node_status()
                assert status["site_id"] == "site-a" and status["node_id"] == "node-site-a"
                assert status["online"] is True
                assert status["driver"] == "noise"
                assert status["software"].startswith("petal-locate/")
                assert status["clock"] == {
                    "source": "ntp",
                    "locked": True,
                    "est_offset_ns": 0.0,
                    "holdover_s": 0.0,
                }
                assert [c["channel_id"] for c in status["channels"]] == ["a1", "a2"]
                assert all(c["ok"] for c in status["channels"])
                assert status["connected_since"].endswith("Z")
                assert 0.0 <= status["latency_ms"] < 500.0
                assert agent.state == "online" and agent.stats.sessions == 1
                await wait_until(lambda: len(events) >= 2, what="status events")
            await wait_until(lambda: not hub.is_online("site-a"), what="the node to leave")
            await wait_until(lambda: events[-1].kind == "offline", what="the offline event")
        assert [(e.kind, e.reason) for e in events] == [
            ("online", "hello"),
            ("channels", "assign_ack"),
            ("offline", "disconnected"),
        ]
        assert not hub.channel_ready("a1")
        (status,) = hub.node_status()
        assert status["online"] is False and status["connected_since"] is None
        counters = hub.counters()
        assert counters["sessions_started"] == counters["sessions_ended"] == 1
        assert "bad_messages" not in counters and "unknown_messages" not in counters
        assert agent.stats.bad_messages == 0 and agent.state == "stopped"


# ------------------------------------------------------------------ spectrum frames


async def test_spectrum_frames_of_every_monitor_reach_a_subscriber_at_the_configured_rate() -> None:
    async with serving() as served:
        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            with spectra(served.hub) as seen:
                started_ns = time.time_ns()
                duration_s = 1.5
                await asyncio.sleep(duration_s)
                frames = list(seen.frames)
            windows = {("a1", "m0"), ("a1", "m1"), ("a2", "m0"), ("a2", "m1")}
            assert {(f.channel_id, f.monitor_id) for f in frames} == windows
            centers = {"m0": 250.0e6, "m1": 260.0e6}
            for channel_id, monitor_id in sorted(windows):
                key = (channel_id, monitor_id)
                mine = [f for f in frames if (f.channel_id, f.monitor_id) == key]
                # about the configured rate: never faster, and not much slower
                expected = FRAME_RATE_HZ * duration_s
                assert 0.6 * expected <= len(mine) <= expected + 2, len(mine)
                seqs = [f.header.seq for f in mine]
                assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False)), seqs
                steps = np.diff([f.t_ns for f in mine]) / 1e9
                assert abs(float(np.median(steps)) - 1 / FRAME_RATE_HZ) < 0.25 / FRAME_RATE_HZ
                for frame in mine:
                    header = frame.header
                    assert frame.site_id == "site-a"
                    assert (header.type, header.units) == ("psd", "dB")
                    assert header.center_hz == centers[monitor_id] and header.span_hz == 1.0e6
                    assert (header.nfft, header.navg) == (256, 4)
                    assert isinstance(header.t_ns, int)
                    # labelled with the node's clock at the snippet, received after it
                    assert started_ns - NS < header.t_ns <= frame.received_ns < header.t_ns + NS
                    assert frame.psd_db.dtype == np.float32 and frame.psd_db.shape == (256,)
                    # bit-exact: the spectrum of exactly the samples the noise driver
                    # generates for this channel and first-sample time
                    iq = noise_samples(SEEDS["site-a"], channel_id, header.t_ns, 256 * 4)
                    assert np.array_equal(frame.psd_db, welch_psd_db(iq, 1.0e6, 256, 4))
                age = served.hub.last_frame_age_s(channel_id, monitor_id)
                assert age is not None and age < 1.0
            # unit-variance noise sits near 0 dB in every bin
            level = float(np.mean([f.psd_db for f in frames]))
            assert abs(level + 0.57) < 0.5, level
            assert served.hub.counters()["psd_frames"] >= len(frames)
            assert agent.stats.frames_sent >= len(frames)


async def test_a_slow_subscriber_sees_fewer_frames_and_delays_nobody() -> None:
    """Slow consumer on the hub: newest wins per window; a prompt one gets everything."""
    async with serving() as served:
        slow_seen: list[PsdFrame] = []

        async def slow(frame: PsdFrame) -> None:
            slow_seen.append(frame)
            await asyncio.sleep(0.35)

        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            subscription = served.hub.subscribe_psd(slow, name="e2e-slow")
            with spectra(served.hub) as seen:
                await asyncio.sleep(1.2)
                n = 20_000
                t_start_ns, deadline_ns = capture_times(0.05, n, 1.0e6)
                capture = await served.hub.capture("a1", t_start_ns, n, 1.0e6, 250.0e6, deadline_ns)
                assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, n))
                prompt = len(seen.frames)
            subscription.unsubscribe()
            assert prompt >= 4 * 0.6 * FRAME_RATE_HZ * 1.2
            assert 2 <= len(slow_seen) <= 6
            assert subscription.dropped >= prompt - len(slow_seen) - 4 > 0
            # each window keeps only its newest frame for the slow subscriber
            assert subscription.waiting == 0 and not subscription.active
            assert served.hub.is_online("site-a")


# ------------------------------------------------------------------------- captures


async def test_simultaneous_captures_on_both_channels_are_bit_exact() -> None:
    async with serving() as served:
        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            n, fs = 30_000, 1.0e6
            t_start_ns, deadline_ns = capture_times(0.15, n, fs)
            started = time.monotonic()
            one, two = await asyncio.wait_for(
                asyncio.gather(
                    served.hub.capture("a1", t_start_ns, n, fs, 251.0e6, deadline_ns),
                    served.hub.capture("a2", t_start_ns, n, fs, 247.5e6, deadline_ns),
                ),
                WAIT_S,
            )
            elapsed = time.monotonic() - started
            for channel_id, center_hz, capture in (("a1", 251.0e6, one), ("a2", 247.5e6, two)):
                assert capture.channel_id == channel_id
                assert capture.t_first_ns == t_start_ns and isinstance(capture.t_first_ns, int)
                assert capture.sample_rate_hz == fs and capture.center_hz == center_hz
                assert capture.overflow is False
                assert capture.clock.locked is True and capture.clock.source == "ntp"
                assert capture.iq.dtype == np.complex64 and capture.n_samples == n
                expected = noise_samples(SEEDS["site-a"], channel_id, t_start_ns, n)
                assert np.array_equal(capture.iq, expected)  # bit-exact
            assert not np.array_equal(one.iq, two.iq)  # each channel hears its own noise
            # a read returns when its interval has passed (lead + n/fs = 0.18 s), and
            # the two were in the node at the same time
            assert 0.15 + n / fs <= elapsed < 2.0, elapsed
            assert agent.stats.captures_started == 2
            counters = served.hub.counters()
            assert counters["capture_requested"] == counters["capture_completed"] == 2
            assert agent.stats.captures_completed == 2 and agent.stats.captures_failed == 0


async def test_million_sample_capture_completes_while_heartbeats_and_spectra_flow() -> None:
    async with serving() as served:
        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            await wait_until(lambda: served.hub.rtt_ms("site-a") is not None, what="a round trip")
            n, fs = 1_000_000, 2.0e6
            with spectra(served.hub) as seen:
                await asyncio.sleep(0.3)
                heartbeats_before = agent.stats.heartbeats_sent
                frames_before = len(seen.frames)
                t_start_ns, deadline_ns = capture_times(0.1, n, fs, timeout_s=10.0)
                began = time.monotonic()
                capture = await asyncio.wait_for(
                    served.hub.capture("a1", t_start_ns, n, fs, 255.0e6, deadline_ns), 15.0
                )
                ended = time.monotonic()
                await asyncio.sleep(HEARTBEAT_S + 0.1)  # the round trips of the upload are reported
            assert capture.t_first_ns == t_start_ns and capture.n_samples == n
            assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, n))
            assert capture.n_chunks == -(-n * 8 // DEFAULT_CHUNK_PAYLOAD_BYTES) == 245
            assert agent.stats.chunks_sent == capture.n_chunks
            # heartbeats kept their cadence through the read and the 8 MB upload ...
            elapsed = ended - began
            assert elapsed >= 0.1 + n / fs
            assert agent.stats.heartbeats_sent - heartbeats_before >= int(elapsed / HEARTBEAT_S)
            # ... were answered without waiting behind the samples ...
            assert served.hub.rtt_ms("site-a") < 250.0, served.hub.rtt_ms("site-a")
            # ... and every window kept streaming, none quiet for half a second
            assert len(seen.frames) - frames_before >= 4 * 0.6 * FRAME_RATE_HZ * elapsed
            for key in (("a1", "m0"), ("a1", "m1"), ("a2", "m0"), ("a2", "m1")):
                assert seen.longest_gap_s(key, began, ended) < 0.5, key
            assert served.hub.is_online("site-a") and agent.stats.sessions == 1


async def test_capture_in_the_recent_past_uses_lookback_and_an_old_one_is_too_late() -> None:
    async with serving() as served:
        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            n, fs = 8_192, 1.0e6
            # 50 ms ago: inside the 30 s the noise driver advertises as lookback
            t_start_ns, deadline_ns = capture_times(-0.05, n, fs)
            assert t_start_ns < time.time_ns()
            capture = await asyncio.wait_for(
                served.hub.capture("a2", t_start_ns, n, fs, 250.0e6, deadline_ns), WAIT_S
            )
            assert capture.t_first_ns == t_start_ns
            assert np.array_equal(capture.iq, noise_samples(11, "a2", t_start_ns, n))
            # 10 minutes ago: no longer buffered
            t_start_ns, deadline_ns = capture_times(-600.0, n, fs)
            began = time.monotonic()
            with pytest.raises(CaptureFailed) as failure:
                await asyncio.wait_for(
                    served.hub.capture("a2", t_start_ns, n, fs, 250.0e6, deadline_ns), WAIT_S
                )
            assert failure.value.code == "too_late"
            assert "lookback" in failure.value.message
            assert failure.value.channel_id == "a2" and failure.value.capture_id is not None
            assert time.monotonic() - began < 1.0  # refused at once, not at the deadline
            counters = served.hub.counters()
            assert counters["capture_failed_too_late"] == 1 and counters["capture_completed"] == 1
            assert agent.stats.captures_failed == 1
            # the node is none the worse for it
            assert served.hub.is_online("site-a") and served.hub.channel_ready("a2")


async def test_out_of_range_and_unknown_channel_requests_fail_with_their_codes() -> None:
    async with serving() as served:
        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            n = 1_000
            t_start_ns, deadline_ns = capture_times(0.05, n, 8.0e6)
            with pytest.raises(CaptureFailed) as failure:  # above the driver's 4 MHz
                await served.hub.capture("a1", t_start_ns, n, 8.0e6, 250.0e6, deadline_ns)
            assert failure.value.code == "out_of_range"
            with pytest.raises(CaptureFailed) as failure:
                await served.hub.capture("zz", t_start_ns, n, 1.0e6, 250.0e6, deadline_ns)
            assert failure.value.code == "unknown_channel"
            assert served.hub.is_online("site-a")


async def test_cancelled_and_late_captures_free_the_node_and_more_than_eight_are_refused() -> None:
    # a long grace: the hub's own deadline timer must not beat the node's `too_late`
    async with serving(capture_grace_s=1.0) as served:
        hub = served.hub
        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            n, fs = 4_096, 1.0e6
            # the caller gives up: capture_cancel stops the node's read
            t_start_ns, deadline_ns = capture_times(3.0, n, fs)
            waiting = asyncio.ensure_future(
                hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
            )
            await wait_until(lambda: agent.stats.captures_started == 1, what="the request")
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            await wait_until(lambda: agent.stats.captures_failed == 1, what="the node to stop")
            await wait_until(
                lambda: hub.counters().get("capture_error_stale", 0) == 1, what="the cancel echo"
            )
            assert hub.counters()["capture_cancelled"] == 1
            # the deadline passes while the node is still waiting for the interval:
            # the node answers `too_late` at its deadline, the hub's own timer is behind
            long_n = 2_000_000  # one second of samples
            t_start_ns = time.time_ns() + round(0.05 * NS)
            began = time.monotonic()
            with pytest.raises(CaptureFailed) as failure:
                await hub.capture("a1", t_start_ns, long_n, 2.0e6, 250.0e6, t_start_ns + NS // 4)
            assert failure.value.code == "too_late" and "deadline" in failure.value.message
            assert 0.25 <= time.monotonic() - began < 0.25 + 0.75  # at its deadline
            # eight at once is the limit of a node; the ninth is refused by the hub
            t_start_ns, deadline_ns = capture_times(0.3, n, fs)
            eight = [
                asyncio.ensure_future(
                    hub.capture(("a1", "a2")[i % 2], t_start_ns, n, fs, 250.0e6, deadline_ns)
                )
                for i in range(MAX_CONCURRENT_CAPTURES)
            ]
            await asyncio.sleep(0)
            with pytest.raises(CaptureFailed) as failure:
                await hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
            assert failure.value.code == "limit"
            done = await asyncio.wait_for(asyncio.gather(*eight), WAIT_S)
            for i, capture in enumerate(done):
                expected = noise_samples(11, ("a1", "a2")[i % 2], t_start_ns, n)
                assert np.array_equal(capture.iq, expected)
            assert agent.stats.captures_completed == 8 and agent.stats.captures_failed == 2
            assert hub.is_online("site-a") and agent.stats.sessions == 1


async def test_unlocked_clock_and_a_driver_that_cannot_open_are_reported_to_the_hub() -> None:
    def factory(assignment: ChannelAssignment) -> Any:
        if assignment.channel_id == "a2":
            raise CaptureError("driver", "no such device: serial 31F2")
        return get_driver("noise", seed=11, lookback_s=5.0, max_sample_rate_hz=1.0e6)

    async with serving() as served:
        hub = served.hub
        agent = make_agent(
            served,
            driver_factory=factory,
            clock=SystemClock(source="free", locked=False, est_offset_ns=-250.0),
        )
        with statuses(hub) as events:
            async with running(agent):
                await ready(served, "a1")
                await wait_until(lambda: len(events) >= 2, what="the assign_ack event")
                # the channel whose driver failed is known, and not taskable
                assert hub.site_of("a2") == "site-a" and not hub.channel_ready("a2")
                assert hub.channel_capabilities("a2") is None
                t_start_ns, deadline_ns = capture_times(0.05, 1_000, 1.0e6)
                with pytest.raises(CaptureFailed) as failure:
                    await hub.capture("a2", t_start_ns, 1_000, 1.0e6, 250.0e6, deadline_ns)
                assert failure.value.code == "not_ready"
                assert "no such device: serial 31F2" in failure.value.message
                # the other channel works, with the capabilities of its own driver ...
                capabilities = hub.channel_capabilities("a1")
                assert capabilities is not None
                assert (capabilities.lookback_s, capabilities.max_sample_rate_hz) == (5.0, 1.0e6)
                capture = await hub.capture("a1", t_start_ns, 1_000, 1.0e6, 250.0e6, deadline_ns)
                assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, 1_000))
                # ... and every capture says that the node's clock is not disciplined
                assert capture.clock.locked is False and capture.clock.source == "free"
                assert capture.clock.est_offset_ns == -250.0 and capture.clock_locked is False
                (status,) = hub.node_status()
                assert status["clock"]["locked"] is False and status["clock"]["source"] == "free"
                assert [(c["channel_id"], c["ok"]) for c in status["channels"]] == [
                    ("a1", True),
                    ("a2", False),
                ]
                # only the channel that opened streams
                with spectra(hub) as seen:
                    await wait_until(lambda: len(seen.frames) >= 8, what="frames")
                    assert {f.channel_id for f in seen.frames} == {"a1"}
            assert [c["ok"] for c in events[1].status["channels"]] == [True, False]


class BrokenRadio(NoiseDriver):
    """Noise for the monitors; what it returns for a capture is wrong in a chosen way."""

    fault = "nan"

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        capture = await super().read(t_start_ns, n, fs, fc)
        if n == 256 * 4:  # a monitor snippet: left alone
            return capture
        if self.fault == "nan":
            capture.iq[n // 2] = complex("nan")
        elif self.fault == "short":
            capture.iq = capture.iq[: n - 1]
        elif self.fault == "raise":
            raise RuntimeError("USB transfer failed")
        return capture


async def test_a_driver_that_returns_garbage_costs_a_capture_never_a_malformed_frame() -> None:
    """Whatever a driver does, the node answers with `capture_error`; nothing reaches
    the wire that the hub would have to refuse as malformed."""
    radios: dict[str, BrokenRadio] = {}

    def factory(assignment: ChannelAssignment) -> BrokenRadio:
        radios[assignment.channel_id] = BrokenRadio(seed=11)
        return radios[assignment.channel_id]

    async with serving() as served:
        hub = served.hub
        agent = make_agent(served, driver_factory=factory)
        with spectra(hub) as seen:
            async with running(agent):
                await ready(served, "a1", "a2")
                n, fs = 1_000, 1.0e6
                expected = {
                    "nan": "NaN or infinite",
                    "short": "999 of 1000 samples",
                    "raise": "RuntimeError: USB transfer failed",
                }
                for fault, text in expected.items():
                    radios["a1"].fault = fault
                    t_start_ns, deadline_ns = capture_times(0.02, n, fs)
                    with pytest.raises(CaptureFailed) as failure:
                        await hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
                    assert failure.value.code == "driver", fault
                    assert text in failure.value.message, failure.value.message
                counters = hub.counters()
                assert counters["capture_failed_driver"] == 3
                assert "bad_messages" not in counters and "capture_completed" not in counters
                assert agent.stats.captures_failed == 3 and agent.stats.chunks_sent == 0
                # the session and the spectra are untouched
                assert hub.is_online("site-a") and agent.stats.sessions == 1
                mark = len(seen.frames)
                await wait_until(lambda: len(seen.windows(mark)) == 4, what="all four windows")


# ----------------------------------------------------------- losing the connection


async def test_killed_socket_fails_pending_captures_at_once_and_the_agent_comes_back() -> None:
    async with serving() as served:
        hub = served.hub
        agent = make_agent(served)
        with statuses(hub) as events:
            async with running(agent):
                await ready(served, "a1", "a2")
                n, fs = 4_096, 1.0e6
                # a capture that is still waiting for its interval when the socket dies
                t_start_ns, deadline_ns = capture_times(3.0, n, fs)
                pending = asyncio.ensure_future(
                    hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
                )
                await wait_until(lambda: agent.stats.captures_started == 1, what="the request")
                killed_at = time.monotonic()
                assert served.abort_node_sockets() == 1
                with pytest.raises(CaptureFailed) as failure:
                    await asyncio.wait_for(pending, WAIT_S)
                assert failure.value.code == "disconnected"
                assert time.monotonic() - killed_at < 0.5  # promptly, not at the deadline
                assert not hub.is_online("site-a")
                # the agent notices, backs off and comes back with a fresh session
                await wait_until(lambda: agent.stats.sessions == 2, what="the reconnect")
                await ready(served, "a1", "a2")
                assert time.monotonic() - killed_at < 3.0
                assert agent.state == "online" and hub.is_online("site-a")
                assert len(agent.stats.backoff_delays) == 1
                assert 0.05 <= agent.stats.backoff_delays[0] <= 0.05 * 1.2  # first step
                # the new session serves captures; nothing of the old one leaks into it
                t_start_ns, deadline_ns = capture_times(0.05, n, fs)
                capture = await asyncio.wait_for(
                    hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns), WAIT_S
                )
                assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, n))
                await asyncio.sleep(0.2)
                counters = hub.counters()
                assert counters.get("capture_data_stale", 0) == 0
                assert counters.get("capture_error_stale", 0) == 0
                assert counters["capture_failed_disconnected"] == 1
                assert agent.stats.captures_completed == 1 and agent.stats.chunks_sent == 1
            kinds = [(e.kind, e.reason) for e in events]
        assert kinds[:5] == [
            ("online", "hello"),
            ("channels", "assign_ack"),
            ("offline", "disconnected"),
            ("online", "hello"),
            ("channels", "assign_ack"),
        ]


async def test_link_cut_between_chunks_voids_the_capture_and_nothing_stale_follows() -> None:
    """Disconnect mid-capture: part of the samples has arrived when the link dies."""
    async with serving() as served:
        hub = served.hub
        async with Link(served.port, up_bytes_per_s=400_000) as link:
            agent = make_agent(link.url, send_buffer_bytes=32 * 1024)
            async with running(agent):
                await ready(served, "a1", "a2")
                n, fs = 250_000, 2.0e6  # 2 MB: five seconds on this link
                t_start_ns, deadline_ns = capture_times(0.05, n, fs, timeout_s=20.0)
                pending = asyncio.ensure_future(
                    hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
                )
                await wait_until(lambda: agent.stats.chunks_sent >= 3, what="some chunks")
                assert not pending.done()
                cut_at = time.monotonic()
                assert link.cut() == 2
                with pytest.raises(CaptureFailed) as failure:
                    await asyncio.wait_for(pending, WAIT_S)
                assert failure.value.code == "disconnected"
                assert time.monotonic() - cut_at < 0.5
                chunks_of_dead_capture = agent.stats.chunks_sent
                # back through the same link; the old capture is not resumed
                await wait_until(lambda: agent.stats.sessions == 2, what="the reconnect")
                await ready(served, "a1", "a2")
                await asyncio.sleep(0.3)
                assert agent.stats.chunks_sent == chunks_of_dead_capture
                assert hub.counters().get("capture_data_stale", 0) == 0
                assert agent.stats.captures_completed == 0
                # and a fresh capture goes through
                n = 2_000
                t_start_ns, deadline_ns = capture_times(0.05, n, fs)
                capture = await asyncio.wait_for(
                    hub.capture("a2", t_start_ns, n, fs, 250.0e6, deadline_ns), WAIT_S
                )
                assert np.array_equal(capture.iq, noise_samples(11, "a2", t_start_ns, n))


async def test_slow_uplink_carries_a_capture_without_the_node_being_declared_silent() -> None:
    """A link on which 256 KiB take longer than three heartbeat periods.

    Liveness is "any frame received": a chunk counts only when all of it has arrived,
    and heartbeats queue behind it in the same stream. The node must therefore send
    chunks small enough for a slow uplink, or every capture would knock it offline.
    """
    heartbeat_s = 0.25  # silence limit 0.75 s
    rate = 200_000  # bytes per second: 256 KiB would take 1.3 s
    async with serving(heartbeat_s=heartbeat_s) as served:
        hub = served.hub
        async with Link(served.port, up_bytes_per_s=rate) as link:
            agent = make_agent(link.url, send_buffer_bytes=32 * 1024)
            with statuses(hub) as events, spectra(hub) as seen:
                async with running(agent):
                    await ready(served, "a1", "a2")
                    n, fs = 60_000, 1.0e6  # 480 kB: 2.4 s on this link
                    t_start_ns, deadline_ns = capture_times(0.05, n, fs, timeout_s=20.0)
                    began = time.monotonic()
                    capture = await asyncio.wait_for(
                        hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns), 20.0
                    )
                    ended = time.monotonic()
                    assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, n))
                    assert ended - began >= 0.8 * n * 8 / rate  # the link really was slow
                    assert agent.stats.sessions == 1 and hub.is_online("site-a")
                    assert "sessions_silent" not in hub.counters()
                    # Spectra kept coming while the samples went up: every window was
                    # heard from several times during the upload. (The first frames
                    # after its start wait behind the chunks that were already in the
                    # socket buffers, some 100 kB here, hence the generous bound.)
                    for key in (("a1", "m0"), ("a1", "m1"), ("a2", "m0"), ("a2", "m1")):
                        assert seen.longest_gap_s(key, began, ended) < 1.5, key
                        during = [
                            t
                            for f, t in zip(seen.frames, seen.arrived, strict=True)
                            if (f.channel_id, f.monitor_id) == key and began <= t <= ended
                        ]
                        assert len(during) >= 5, (key, len(during))
                assert [e.reason for e in events if e.kind == "offline"] == ["disconnected"]


async def test_silent_link_is_noticed_by_both_ends_and_the_session_is_rebuilt() -> None:
    """A link that stops carrying data without closing: no reset, no close frame.

    The hub hears nothing for three heartbeat periods and fails the node's captures;
    the node hears nothing for `server_silence_timeout_s`, gives the socket up and
    dials again. Whichever notices first, the result is one fresh session.
    """
    async with serving() as served:
        hub = served.hub
        async with Link(served.port) as link:
            agent = make_agent(link.url, server_silence_timeout_s=1.0, close_timeout_s=0.3)
            async with running(agent):
                await ready(served, "a1", "a2")
                n, fs = 4_096, 1.0e6
                t_start_ns, deadline_ns = capture_times(6.0, n, fs)
                pending = asyncio.ensure_future(
                    hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
                )
                await wait_until(lambda: agent.stats.captures_started == 1, what="the request")
                frozen_at = time.monotonic()
                link.freeze()
                with pytest.raises(CaptureFailed) as failure:
                    await asyncio.wait_for(pending, WAIT_S)
                # silence at the hub (three periods of 0.5 s), or the node's new hello
                assert failure.value.code in ("offline", "replaced")
                assert time.monotonic() - frozen_at < 3 * HEARTBEAT_S + 1.0
                await wait_until(lambda: agent.stats.sessions == 2, what="the new session")
                await ready(served, "a1", "a2")
                assert time.monotonic() - frozen_at < 5.0
                assert hub.online_sites() == ["site-a"] and agent.state == "online"
                t_start_ns, deadline_ns = capture_times(0.05, n, fs)
                capture = await asyncio.wait_for(
                    hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns), WAIT_S
                )
                assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, n))
                assert hub.counters().get("capture_data_stale", 0) == 0
                assert hub.counters()["sessions_started"] == 2


async def test_server_shutdown_fails_pending_captures_and_the_agent_finds_the_new_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The server process goes away (close code 1001) and a new one takes its port."""
    caplog.set_level(logging.INFO, logger="petal_locate.node.agent")
    agent: NodeAgent | None = None
    try:
        async with serving(check_tasks=False) as served:  # the agent outlives this server
            port = served.port
            agent = make_agent(served)
            agent.start()
            await ready(served, "a1", "a2")
            n, fs = 4_096, 1.0e6
            t_start_ns, deadline_ns = capture_times(3.0, n, fs)
            pending = asyncio.ensure_future(
                served.hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
            )
            await wait_until(lambda: agent.stats.captures_started == 1, what="the request")
            # leaving the block shuts the server down with the capture pending
        with pytest.raises(CaptureFailed) as failure:
            await asyncio.wait_for(pending, WAIT_S)
        assert failure.value.code == "shutdown"
        await wait_until(lambda: agent.state == "backoff", what="the agent to notice")
        assert agent.stats.sessions == 1
        assert close_codes(caplog, agent) == [1001]  # an orderly shutdown, as the protocol says
        await wait_until(lambda: len(agent.stats.backoff_delays) >= 5, what="five waits")
        # nobody listens on the port: the waits double from the base to the cap, each
        # with up to 20 % of jitter on top
        delays = list(agent.stats.backoff_delays)
        for delay, nominal in zip(delays, (0.05, 0.1, 0.2, 0.4, 0.4), strict=False):
            assert nominal <= delay <= nominal * 1.2, delays
        assert agent.stats.connect_attempts >= 5
        async with serving(port=port, check_tasks=False) as again:
            await ready(again, "a1", "a2")
            assert agent.stats.sessions == 2 and agent.state == "online"
            t_start_ns, deadline_ns = capture_times(0.05, n, fs)
            capture = await asyncio.wait_for(
                again.hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns), WAIT_S
            )
            assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, n))
            assert again.hub.counters().get("capture_data_stale", 0) == 0
            await agent.stop(WAIT_S)
    finally:
        if agent is not None:
            await agent.stop(WAIT_S)


# --------------------------------------------------------- who may hold a site


async def test_second_agent_with_the_same_node_id_replaces_the_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="petal_locate.node.agent")
    async with serving() as served:
        hub = served.hub
        # once it is replaced, the first process keeps away for the rest of the test
        first = make_agent(served, backoff_max_s=30.0)
        second = make_agent(served, driver_options={"seed": 77})
        assert first.node_id == second.node_id == "node-site-a"
        with statuses(hub) as events:
            async with running(first):
                await ready(served, "a1", "a2")
                n, fs = 4_096, 1.0e6
                t_start_ns, deadline_ns = capture_times(3.0, n, fs)
                pending = asyncio.ensure_future(
                    hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
                )
                await wait_until(lambda: first.stats.captures_started == 1, what="the request")
                async with running(second):
                    await second.wait_online(WAIT_S)
                    # the old session's captures fail as the new hello is admitted
                    with pytest.raises(CaptureFailed) as failure:
                        await asyncio.wait_for(pending, WAIT_S)
                    assert failure.value.code == "replaced"
                    # the first agent is told with close code 4001 and stays away
                    await wait_until(lambda: first.state == "backoff", what="the first to back off")
                    assert first.stats.backoff_delays[-1] >= 30.0
                    assert CLOSE_CODE_REPLACED in close_codes(caplog, first)
                    await ready(served, "a1", "a2")
                    assert hub.online_sites() == ["site-a"]
                    assert hub.counters()["sessions_replaced"] == 1
                    assert hub.counters()["sessions_started"] == 2
                    # captures are now served by the second process (its noise seed)
                    t_start_ns, deadline_ns = capture_times(0.05, n, fs)
                    capture = await asyncio.wait_for(
                        hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns), WAIT_S
                    )
                    assert np.array_equal(capture.iq, noise_samples(77, "a1", t_start_ns, n))
                    assert first.stats.captures_completed == 0
                    assert second.stats.sessions == 1 and first.stats.sessions == 1
                    kinds = [(e.kind, e.reason) for e in events]
                    assert kinds[:4] == [
                        ("online", "hello"),
                        ("channels", "assign_ack"),
                        ("offline", "replaced"),
                        ("online", "hello"),
                    ]
        assert CLOSE_CODE_REPLACED == 4001


async def test_second_agent_with_another_node_id_is_rejected_until_the_first_leaves() -> None:
    async with serving() as served:
        hub = served.hub
        first = make_agent(served)
        intruder = make_agent(served, node_id="node-other", driver_options={"seed": 99})
        async with running(first):
            await ready(served, "a1", "a2")
            async with running(intruder):
                await wait_until(lambda: intruder.stats.rejects >= 2, what="two rejects")
                assert intruder.last_reject == "duplicate_node"
                assert intruder.stats.sessions == 0
                # it retries at the longest backoff, never faster
                assert all(delay >= 0.4 for delay in intruder.stats.backoff_delays)
                assert hub.counters()["rejected_duplicate_node"] >= 2
                # the holder of the site is undisturbed
                assert first.stats.sessions == 1 and first.state == "online"
                assert hub.is_online("site-a") and hub.channel_ready("a1")
                (status,) = hub.node_status()
                assert status["node_id"] == "node-site-a"
                # once the first node goes away the other one gets the site
                await first.stop(WAIT_S)
                await wait_until(lambda: intruder.stats.sessions == 1, what="the takeover")
                await ready(served, "a1", "a2")
                (status,) = hub.node_status()
                assert status["node_id"] == "node-other" and status["online"] is True
                n, fs = 2_000, 1.0e6
                t_start_ns, deadline_ns = capture_times(0.05, n, fs)
                capture = await hub.capture("a2", t_start_ns, n, fs, 250.0e6, deadline_ns)
                assert np.array_equal(capture.iq, noise_samples(99, "a2", t_start_ns, n))


async def test_wrong_token_is_rejected_and_the_agent_backs_off() -> None:
    async with serving() as served:
        hub = served.hub
        wrong = make_agent(served, token="not-the-token")
        async with running(wrong):
            await wait_until(lambda: wrong.stats.rejects >= 3, what="three rejects")
            assert wrong.last_reject == "bad_token"
            assert wrong.stats.sessions == 0 and not hub.is_online("site-a")
            assert hub.node_status() == []  # nothing is learnt about the site
            # every wait is the longest one (0.4 s here), plus at most 20 % jitter
            delays = list(wrong.stats.backoff_delays)
            assert len(delays) >= 2 and all(0.4 <= delay <= 0.4 * 1.2 for delay in delays)
            assert wrong.stats.connect_attempts <= len(delays) + 1
            assert hub.counters()["rejected_bad_token"] == wrong.stats.rejects
        # the token of another site does not open this one
        borrowed = make_agent(served, token=TOKENS["site-b"])
        # a site the server holds no token for looks the same from outside
        untokened = make_agent(served, "site-nowhere", token=TOKENS["site-a"])
        # a valid token for a site the configuration does not have
        ghost = make_agent(served, "site-ghost")
        async with running(borrowed, untokened, ghost):
            await wait_until(
                lambda: all(a.stats.rejects >= 1 for a in (borrowed, untokened, ghost)),
                what="the rejects",
            )
            assert borrowed.last_reject == "bad_token"
            assert untokened.last_reject == "bad_token"
            assert ghost.last_reject == "unknown_site"
        assert hub.online_sites() == []
        assert hub.counters().get("sessions_started", 0) == 0


async def test_tokens_never_reach_a_log_even_at_debug_level() -> None:
    records = Records()
    root = logging.getLogger()
    levels = {name: logging.getLogger(name).level for name in ("", "websockets", "uvicorn")}
    root.addHandler(records)
    for name in levels:
        logging.getLogger(name).setLevel(1)
    try:
        async with serving() as served:
            good = make_agent(served, node_id="n")  # short ids: the token is early in hello
            bad = make_agent(served, "site-b", token="wrong-token-3c9a11", node_id="m")
            async with running(good, bad):
                await ready(served, "a1", "a2")
                await wait_until(lambda: bad.stats.rejects >= 1, what="the reject")
                await asyncio.sleep(HEARTBEAT_S)
    finally:
        root.removeHandler(records)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
    text = "\n".join(records.lines)
    assert "welcomed by the server" in text and "rejected hello" in text  # logs were on
    assert "> TEXT" in text or "< TEXT" in text  # including the frame traces
    for secret in (TOKENS["site-a"], "wrong-token-3c9a11", TOKENS["site-b"]):
        assert secret not in text


# ------------------------------------------------------------- configuration changes


async def test_reassign_with_a_changed_monitor_list_changes_what_the_node_streams() -> None:
    async with serving() as served:
        hub, world = served.hub, served.world
        agent = make_agent(served)
        with spectra(hub) as seen:
            async with running(agent):
                await ready(served, "a1", "a2")
                await wait_until(
                    lambda: len(seen.windows()) == 4 and len(seen.of("a2", "m0")) >= 3,
                    what="frames of every window",
                )
                # a1 loses m1 and gains m2 (another centre and resolution); a2 is untouched
                world.sites["site-a"] = [
                    channel("a1", monitor("m0", 250.0e6), monitor("m2", 270.0e6, nfft=128, navg=2)),
                    channel("a2"),
                ]
                world.version = 6
                assert await hub.reassign() == ["site-a"]
                # no channel may be tasked before the node has acknowledged version 6
                assert not hub.channel_ready("a1") and not hub.channel_ready("a2")
                await ready(served, "a1", "a2")
                switched = len(seen.frames)
                await wait_until(
                    lambda: len(seen.of("a1", "m2", switched)) >= 5, what="frames of the new window"
                )
                after = seen.frames[switched:]
                assert seen.windows(switched) == {
                    ("a1", "m0"),
                    ("a1", "m2"),
                    ("a2", "m0"),
                    ("a2", "m1"),
                }
                for frame in seen.of("a1", "m2", switched):
                    header = frame.header
                    assert header.center_hz == 270.0e6 and (header.nfft, header.navg) == (128, 2)
                    iq = noise_samples(11, "a1", header.t_ns, 128 * 2)
                    assert np.array_equal(frame.psd_db, welch_psd_db(iq, 1.0e6, 128, 2))
                assert not [f for f in after if (f.channel_id, f.monitor_id) == ("a1", "m1")]
                # the changed channel was reopened (its counters start again); the
                # unchanged one kept its driver and its monitors never stopped
                assert seen.of("a1", "m0", switched)[0].header.seq <= 2
                before_a2 = [f.header.seq for f in seen.of("a2", "m0")[: 3]]
                after_a2 = [f.header.seq for f in seen.of("a2", "m0", switched)]
                assert after_a2[0] > before_a2[-1]
                assert hub.last_frame_age_s("a1", "m1") is None  # forgotten with the window
                assert hub.last_frame_age_s("a1", "m2") is not None
                assert agent.stats.sessions == 1  # same session throughout
                # an unchanged configuration sends nothing
                assert await hub.reassign() == []
                # removing a channel stops its frames and makes it unknown
                world.sites["site-a"] = [channel("a2")]
                world.version = 7
                assert await hub.reassign(["site-a"]) == ["site-a"]
                await ready(served, "a2")
                removed = len(seen.frames)
                await wait_until(
                    lambda: len(seen.of("a2", "m1", removed)) >= 3, what="frames after the removal"
                )
                assert {f.channel_id for f in seen.frames[removed:]} == {"a2"}
                assert hub.site_of("a1") is None and not hub.channel_ready("a1")


async def test_assign_during_captures_fails_the_changed_channel_only() -> None:
    async with serving() as served:
        hub, world = served.hub, served.world
        agent = make_agent(served)
        async with running(agent):
            await ready(served, "a1", "a2")
            n, fs = 4_096, 1.0e6
            t_start_ns, deadline_ns = capture_times(0.8, n, fs)
            on_changed = asyncio.ensure_future(
                hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
            )
            on_kept = asyncio.ensure_future(
                hub.capture("a2", t_start_ns, n, fs, 250.0e6, deadline_ns)
            )
            await wait_until(lambda: agent.stats.captures_started == 2, what="both requests")
            world.sites["site-a"] = [channel("a1", monitor("m0", 251.0e6)), channel("a2")]
            world.version = 6
            assert await hub.reassign() == ["site-a"]
            with pytest.raises(CaptureFailed) as failure:
                await asyncio.wait_for(on_changed, 0.5)  # at once, with the assign
            assert failure.value.code == "reassigned"
            # the capture on the untouched channel goes on through the reassignment
            kept = await asyncio.wait_for(on_kept, WAIT_S)
            assert kept.t_first_ns == t_start_ns
            assert np.array_equal(kept.iq, noise_samples(11, "a2", t_start_ns, n))
            await ready(served, "a1", "a2")
            # the node cancelled its side of the failed capture; the hub discards that
            await wait_until(
                lambda: hub.counters().get("capture_error_stale", 0) == 1, what="the cancel echo"
            )
            assert agent.stats.captures_completed == 1 and agent.stats.captures_failed == 1
            t_start_ns, deadline_ns = capture_times(0.05, n, fs)
            again = await hub.capture("a1", t_start_ns, n, fs, 251.0e6, deadline_ns)
            assert np.array_equal(again.iq, noise_samples(11, "a1", t_start_ns, n))


async def test_site_removed_from_the_configuration_is_closed_and_then_rejected() -> None:
    sites = {"site-a": [channel("a1"), channel("a2")], "site-b": [channel("b1")]}
    async with serving(World(sites)) as served:
        hub, world = served.hub, served.world
        a, b = make_agent(served, "site-a"), make_agent(served, "site-b")
        async with running(a, b):
            await ready(served, "a1", "a2", "b1")
            # a sim_update is announced to every node; the noise driver has no use for it
            assert await hub.announce_sim_update(3, time.time_ns() + 2 * NS) == ["site-a", "site-b"]
            t_start_ns, deadline_ns = capture_times(3.0, 1_000, 1.0e6)
            pending = asyncio.ensure_future(
                hub.capture("b1", t_start_ns, 1_000, 1.0e6, 250.0e6, deadline_ns)
            )
            await wait_until(lambda: b.stats.captures_started == 1, what="the request")
            del world.sites["site-b"]
            world.version = 6
            # site-a's channels did not change, but the version did: it is told too
            assert await hub.reassign() == ["site-a"]
            with pytest.raises(CaptureFailed) as failure:
                await asyncio.wait_for(pending, WAIT_S)
            assert failure.value.code == "reassigned"
            assert hub.site_of("b1") is None and hub.online_sites() == ["site-a"]
            assert [s["site_id"] for s in hub.node_status()] == ["site-a"]
            # the node of the removed site comes back, is refused, and stays away
            await wait_until(lambda: b.last_reject == "unknown_site", what="the reject")
            assert b.stats.sessions == 1 and b.stats.backoff_delays[-1] >= 0.4
            await ready(served, "a1", "a2")
            assert a.stats.sessions == 1 and a.stats.bad_messages == 0
            # and when the site returns to the configuration, so does its node
            world.sites["site-b"] = [channel("b1")]
            world.version = 7
            await hub.reassign()
            await ready(served, "a1", "a2", "b1", timeout=WAIT_S)
            assert b.stats.sessions == 2 and hub.online_sites() == ["site-a", "site-b"]


# ---------------------------------------------------------------------- many nodes


async def test_three_nodes_for_three_sites_at_once() -> None:
    sites = {
        "site-a": [channel("a1"), channel("a2")],
        "site-b": [channel("b1"), channel("b2")],
        "site-c": [channel("c1"), channel("c2")],
    }
    async with serving(World(sites)) as served:
        hub = served.hub
        agents = [make_agent(served, site_id) for site_id in sites]
        with spectra(hub) as seen:
            async with running(*agents):
                await ready(served, "a1", "a2", "b1", "b2", "c1", "c2")
                assert hub.online_sites() == ["site-a", "site-b", "site-c"]
                assert [s["node_id"] for s in hub.node_status()] == [
                    "node-site-a",
                    "node-site-b",
                    "node-site-c",
                ]
                for site_id in sites:
                    assert {hub.site_of(c.channel_id) for c in sites[site_id]} == {site_id}
                # one capture per site, all with the same first-sample time
                n, fs = 20_000, 1.0e6
                t_start_ns, deadline_ns = capture_times(0.15, n, fs)
                wanted = {"a1": "site-a", "b2": "site-b", "c1": "site-c"}
                captures = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            hub.capture(channel_id, t_start_ns, n, fs, 250.0e6, deadline_ns)
                            for channel_id in wanted
                        )
                    ),
                    WAIT_S,
                )
                for (channel_id, site_id), capture in zip(wanted.items(), captures, strict=True):
                    assert capture.t_first_ns == t_start_ns
                    expected = noise_samples(SEEDS[site_id], channel_id, t_start_ns, n)
                    assert np.array_equal(capture.iq, expected)
                # every one of the twelve windows streams, attributed to its site
                await wait_until(
                    lambda: len(seen.windows()) == 12
                    and min(len(seen.of(c, m)) for c, m in seen.windows()) >= 5,
                    what="frames of all twelve windows",
                )
                for frame in seen.frames:
                    assert frame.site_id == f"site-{frame.channel_id[0]}"
                # one node leaving does not disturb the others
                await agents[1].stop(WAIT_S)
                await wait_until(lambda: not hub.is_online("site-b"), what="site-b offline")
                t_start_ns, deadline_ns = capture_times(0.05, n, fs)
                with pytest.raises(CaptureFailed) as failure:
                    await hub.capture("b1", t_start_ns, n, fs, 250.0e6, deadline_ns)
                assert failure.value.code == "offline"
                capture = await hub.capture("c2", t_start_ns, n, fs, 250.0e6, deadline_ns)
                assert np.array_equal(capture.iq, noise_samples(33, "c2", t_start_ns, n))
                assert hub.online_sites() == ["site-a", "site-c"]


# ------------------------------------------------------------------- hostile peers


def raw_frame(header: dict[str, Any] | bytes, payload: bytes = b"") -> bytes:
    """A PLB1 frame built by hand: magic, header length, header, payload."""
    raw = header if isinstance(header, bytes) else json.dumps(header).encode()
    return b"PLB1" + struct.pack("<I", len(raw)) + raw + payload


async def test_garbage_from_a_rogue_client_disturbs_neither_the_hub_nor_a_real_node() -> None:
    """Malformed frames, forged spectra and stale chunks next to a working session."""
    sites = {"site-a": [channel("a1"), channel("a2")], "site-b": [channel("b1")]}
    clock = {"source": "free", "locked": False, "est_offset_ns": 0.0, "holdover_s": 0.0}
    psd_header = {
        "type": "psd", "channel_id": "a1", "monitor_id": "m0", "t_ns": time.time_ns(),
        "center_hz": 250.0e6, "span_hz": 1.0e6, "nfft": 256, "navg": 4, "seq": 0, "units": "dB",
    }
    chunk_header = {
        "type": "capture_data", "capture_id": "not-pending", "channel_id": "b1", "seq": 0,
        "last": True, "t_first_ns": 1, "sample_rate_hz": 1.0e6, "center_hz": 250.0e6,
        "n_samples_total": 4, "offset": 0, "n": 4, "overflow": False, "clock": clock,
    }
    garbage: list[str | bytes] = [
        "",
        "[" * 100_000,
        '{"type":"heartbeat","seq":' + "9" * 5000 + "}",
        '{"type":"heartbeat","seq":NaN}',
        b"PLB1\xff\xff\xff\xff",
        raw_frame(b"\xff\xfe not utf-8"),
        raw_frame({**psd_header, "channel_id": "b1"}, b"\x00" * 1000),  # wrong payload length
        raw_frame({**chunk_header, "offset": 3}, b"\x00" * 32),  # runs past its own total
    ]

    async def until_closed(ws: Any) -> tuple[int, list[dict[str, Any]]]:
        """Everything the server still says, and the code it closes with."""
        said: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(WAIT_S):
                while True:
                    said.append(json.loads(await ws.recv()))
        except websockets.ConnectionClosed as closed:
            assert closed.rcvd is not None
            return closed.rcvd.code, said

    async with serving(World(sites)) as served:
        hub = served.hub
        agent = make_agent(served)
        url = f"{served.url}/ws/node"
        with spectra(hub) as seen:
            async with running(agent):
                await ready(served, "a1", "a2")
                # 1. before any hello: every message is answered, the third ends it
                async with websockets.connect(url, compression=None, proxy=None) as ws:
                    for message in garbage[:3]:
                        await ws.send(message)
                    code, said = await until_closed(ws)
                assert code == 1002 and [m["type"] for m in said] == ["error"] * 3
                assert all(m["code"] == "bad_message" for m in said)
                # 2. a peer that holds the token of site-b and misuses its session
                async with websockets.connect(url, compression=None, proxy=None) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello", "protocol": 1, "node_id": "rogue",
                                "site_id": "site-b", "token": TOKENS["site-b"],
                                "software": "rogue", "driver": "none", "clock": clock,
                                "t_ns": time.time_ns(),
                            }
                        )
                    )
                    welcome = json.loads(await ws.recv())
                    assert welcome["type"] == "welcome"
                    # spectra for a channel of another site, in a perfectly valid frame
                    forged = np.full(256, 99.0, dtype="<f4").tobytes()
                    await ws.send(raw_frame(psd_header, forged))
                    # a chunk of a capture nobody asked for
                    await ws.send(raw_frame(chunk_header, b"\x00" * 32))
                    await ws.send(json.dumps({"type": "from_the_future", "x": 1}))
                    for message in garbage[3:]:
                        await ws.send(message)
                    code, said = await until_closed(ws)
                # the forged frame, the stale chunk and the unknown type are dropped
                # without a word; of the five malformed ones the third closed the socket
                assert code == 1002 and [m["type"] for m in said][-3:] == ["error"] * 3
                counters = hub.counters()
                assert "internal_errors" not in counters
                assert counters["psd_dropped_unassigned"] == 1
                assert counters["capture_data_stale"] == 1
                assert counters["unknown_messages"] == 1
                assert counters["sessions_bad_messages"] == 2
                assert counters["bad_messages"] == 6
                # the real node never noticed
                assert agent.stats.sessions == 1 and agent.stats.bad_messages == 0
                assert not any(float(f.psd_db[0]) == 99.0 for f in seen.frames)
                assert {f.site_id for f in seen.frames} == {"site-a"}
                n, fs = 2_000, 1.0e6
                t_start_ns, deadline_ns = capture_times(0.05, n, fs)
                capture = await hub.capture("a1", t_start_ns, n, fs, 250.0e6, deadline_ns)
                assert np.array_equal(capture.iq, noise_samples(11, "a1", t_start_ns, n))
                assert hub.online_sites() == ["site-a"]
