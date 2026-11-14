"""Node agent, monitor and clock (PROTOCOL.md section A, ARCHITECTURE.md section 6).

The agent is driven by `FakeServer`, a scriptable stand-in for the hub built on
`websockets.serve` (the real hub is tested elsewhere), over real sockets on an
ephemeral loopback port. Drivers are either the real `noise` driver or `FakeDriver`,
whose reads the tests can hold, fail and inspect.

Every test stops its agent and closes its server; `no_leaks` checks that nothing the
test started is still running afterwards.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import random
import socket
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest
from scipy.special import digamma
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from petal_locate.node.agent import (
    DEFAULT_CHUNK_PAYLOAD_BYTES,
    REJECT_POLICY,
    AgentStats,
    NodeAgent,
    NodeRejected,
    backoff_delay,
    default_state_path,
    load_or_create_node_id,
    normalize_server_url,
)
from petal_locate.node.clock import (
    ClockSource,
    DisciplinedClock,
    ManualClock,
    ReferenceReading,
    SystemClock,
)
from petal_locate.node.drivers.base import (
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    WorkerPool,
)
from petal_locate.node.agent import _Outbox  # the writer, unit-tested on its own below
from petal_locate.node.drivers.noise import noise_samples
from petal_locate.node.monitor import FrameMailbox, Monitor, ReadGate, SpectrumFrame
from petal_locate.protocol import (
    MAX_CHUNK_PAYLOAD_BYTES,
    Assign,
    AssignAck,
    CaptureAssembler,
    CaptureCancel,
    CaptureDataHeader,
    CaptureErrorMessage,
    CaptureRequest,
    ChannelAssignment,
    ClockStatus,
    ErrorMessage,
    Heartbeat,
    HeartbeatAck,
    Hello,
    MonitorAssignment,
    PsdHeader,
    Reject,
    SimulationInfo,
    SimUpdate,
    Welcome,
    decode_frame,
    decode_psd,
    dump_control,
    iter_capture_chunks,
    parse_control,
)

NS = 1_000_000_000
TOKEN = "s3cret-token"
SITE = "site-a"


# ------------------------------------------------------------------------ utilities


async def wait_until(
    predicate: Callable[[], Any], timeout: float = 3.0, what: str = "condition"
) -> Any:
    """Poll `predicate` until it is truthy; fail the test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        await asyncio.sleep(0.003)


def monitor(monitor_id: str = "m0", **changes: Any) -> MonitorAssignment:
    values: dict[str, Any] = dict(
        monitor_id=monitor_id, center_hz=250e6, span_hz=1e6, nfft=64, navg=2, frame_rate_hz=20.0
    )
    values.update(changes)
    return MonitorAssignment(**values)


def channel(channel_id: str, *monitors: MonitorAssignment) -> ChannelAssignment:
    return ChannelAssignment(
        channel_id=channel_id, antenna_summary="1.2 m parabolic", monitors=list(monitors)
    )


def fake_iq(channel_id: str, t_start_ns: int, n: int) -> np.ndarray:
    """Deterministic complex64 test samples, different for every channel and time."""
    seed = zlib.crc32(f"{channel_id}/{t_start_ns}".encode())
    rng = np.random.default_rng(seed)
    return (
        rng.standard_normal(n, dtype=np.float32) + 1j * rng.standard_normal(n, dtype=np.float32)
    ).astype(np.complex64)


@pytest.fixture
async def no_leaks() -> AsyncIterator[None]:
    """Fail the test when it leaves an asyncio task running."""
    before = set(asyncio.all_tasks())
    yield
    await asyncio.sleep(0.02)
    mine = asyncio.current_task()
    leaked = [t for t in asyncio.all_tasks() - before if not t.done() and t is not mine]
    assert not leaked, f"tasks left running: {[t.get_name() for t in leaked]}"


# ---------------------------------------------------------------------- fake driver


class FakeDriver:
    """A driver the test controls.

    hold: when set to an `asyncio.Event`, `read` waits for it before returning.
    read_error: raised by `read` instead of returning.
    fast_psd: when not None, `psd` returns it (the fast path); else None (IQ path).
    t_first_offset_ns: `t_first_ns` reported is `t_start_ns` plus this.
    ignore_cancel: a misbehaving driver that swallows cancellation while held and
        returns its samples anyway.
    """

    live_reads = 0
    peak_reads = 0

    def __init__(
        self,
        channel_id: str,
        *,
        capabilities: Capabilities | None = None,
        open_error: Exception | None = None,
        open_delay_s: float = 0.0,
    ) -> None:
        self.channel_id = channel_id
        self.capabilities = capabilities or Capabilities(
            min_hz=100e6, max_hz=400e6, max_sample_rate_hz=2e6, lookback_s=1.0, concurrent_reads=4
        )
        self.open_error = open_error
        self.open_delay_s = open_delay_s
        self.hold: asyncio.Event | None = None
        self.read_error: Exception | None = None
        self.fast_psd: np.ndarray | None = None
        self.t_first_offset_ns = 0
        self.ignore_cancel = False
        self.result_override: Callable[[Capture], Capture] | None = None
        self.opened = 0
        self.closed = 0
        self.ctx: DriverContext | None = None
        self.assignment: ChannelAssignment | None = None
        self.reads: list[tuple[int, int, float, float]] = []
        self.psd_calls = 0
        self.cancelled_reads = 0
        self.returned: dict[int, np.ndarray] = {}
        self.read_started = asyncio.Event()

    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        if self.open_delay_s:
            await asyncio.sleep(self.open_delay_s)
        if self.open_error is not None:
            raise self.open_error
        self.opened += 1
        self.ctx = ctx
        self.assignment = channel

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        self.reads.append((t_start_ns, n, fs, fc))
        FakeDriver.live_reads += 1
        FakeDriver.peak_reads = max(FakeDriver.peak_reads, FakeDriver.live_reads)
        self.read_started.set()
        try:
            if self.hold is not None:
                try:
                    await self.hold.wait()
                except asyncio.CancelledError:
                    self.cancelled_reads += 1
                    if not self.ignore_cancel:
                        raise
                    await asyncio.shield(self.hold.wait())
            if self.read_error is not None:
                raise self.read_error
            if n > 100_000:  # a real driver does not synthesise megasamples on the loop
                iq = await asyncio.to_thread(fake_iq, self.channel_id, t_start_ns, n)
            else:
                iq = fake_iq(self.channel_id, t_start_ns, n)
            self.returned[t_start_ns] = iq
            capture = Capture(iq, t_start_ns + self.t_first_offset_ns, fs, fc, False, True)
            return self.result_override(capture) if self.result_override else capture
        finally:
            FakeDriver.live_reads -= 1

    async def psd(
        self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int
    ) -> np.ndarray | None:
        self.psd_calls += 1
        return self.fast_psd

    async def close(self) -> None:
        self.closed += 1
        if self.hold is not None and not self.ignore_cancel:
            self.hold.set()


class HookedDriver(FakeDriver):
    """A fake with the two optional hooks of `drivers.base`."""

    def __init__(self, channel_id: str, **options: Any) -> None:
        super().__init__(channel_id, **options)
        self.sim_updates: list[tuple[int, int]] = []
        self.assigns: list[ChannelAssignment] = []
        self.assign_error: Exception | None = None

    async def on_sim_update(self, version: int, effective_ns: int) -> None:
        self.sim_updates.append((version, effective_ns))

    async def on_assign(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        if self.assign_error is not None:
            raise self.assign_error
        self.assigns.append(channel)


class Drivers:
    """A driver factory that remembers every driver it built, per channel."""

    def __init__(self, cls: type[FakeDriver] = FakeDriver, **options: Any) -> None:
        self.cls = cls
        self.options = options
        self.built: dict[str, list[FakeDriver]] = {}
        self.prepare: Callable[[FakeDriver], None] | None = None
        FakeDriver.live_reads = 0
        FakeDriver.peak_reads = 0

    def __call__(self, assignment: ChannelAssignment) -> FakeDriver:
        driver = self.cls(assignment.channel_id, **self.options)
        if self.prepare is not None:
            self.prepare(driver)
        self.built.setdefault(assignment.channel_id, []).append(driver)
        return driver

    def __getitem__(self, channel_id: str) -> FakeDriver:
        """The most recent driver built for a channel."""
        return self.built[channel_id][-1]


# ---------------------------------------------------------------------- fake server


@dataclass
class Connection:
    """One accepted socket, with everything that arrived on it in order."""

    ws: ServerConnection
    index: int
    opened_at: float
    hello: Hello | None = None
    welcomed: bool = False
    events: list[tuple[str, Any, Any]] = field(default_factory=list)
    assemblers: dict[str, CaptureAssembler] = field(default_factory=dict)
    captures: dict[str, Any] = field(default_factory=dict)
    reading: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_code: int | None = None
    protocol_errors: list[str] = field(default_factory=list)

    def of(self, kind: str) -> list[Any]:
        """Bodies of the events of one kind: control messages by wire type
        ("heartbeat", "assign_ack", "capture_error", "error"), "psd" (header, array)
        or "capture_data" (header, payload bytes)."""
        if kind in ("psd", "capture_data"):
            return [(a, b) for k, a, b in self.events if k == kind]
        return [a for k, a, _ in self.events if k == kind]

    async def send(self, message: Any) -> None:
        await self.ws.send(dump_control(message))

    async def expect(self, kind: str, count: int = 1, timeout: float = 3.0) -> list[Any]:
        await wait_until(lambda: len(self.of(kind)) >= count, timeout, f"{count} x {kind}")
        return self.of(kind)

    async def capture(self, capture_id: str, timeout: float = 5.0) -> Any:
        await wait_until(lambda: capture_id in self.captures, timeout, f"capture {capture_id}")
        return self.captures[capture_id]

    def stall(self) -> None:
        """Stop reading from the socket (a slow consumer)."""
        self.reading.clear()

    def resume(self) -> None:
        self.reading.set()


class FakeServer:
    """A scriptable server side of PROTOCOL.md section A."""

    def __init__(
        self,
        channels: list[ChannelAssignment] | None = None,
        *,
        heartbeat_s: float = 0.05,
        config_version: int = 7,
        reject: str | Callable[[int], str | None] | None = None,
        ack_delay_s: float = 0.0,
        ack_heartbeats: bool = True,
        chunk_delay_s: float = 0.0,
        simulation: SimulationInfo | None = None,
        welcome_protocol: int = 1,
        max_queue: int = 16,
        rcvbuf: int | None = None,
    ) -> None:
        self.channels = channels if channels is not None else []
        self.heartbeat_s = heartbeat_s
        self.config_version = config_version
        self.reject = reject
        self.ack_delay_s = ack_delay_s
        self.ack_heartbeats = ack_heartbeats
        self.chunk_delay_s = chunk_delay_s
        self.simulation = simulation
        self.welcome_protocol = welcome_protocol
        self.connections: list[Connection] = []
        self._max_queue = max_queue
        self._rcvbuf = rcvbuf
        self._server: Any = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def __aenter__(self) -> FakeServer:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self._rcvbuf is not None:  # inherited by accepted sockets: a small pipe
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._rcvbuf)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self._server = await serve(
            self._handle, sock=sock, compression=None, max_queue=self._max_queue,
            ping_interval=None, close_timeout=0.3,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        for connection in self.connections:
            connection.resume()
        self._server.close()
        await self._server.wait_closed()

    async def session(self, index: int = 0, timeout: float = 3.0) -> Connection:
        """The `index`-th welcomed connection (waits for it)."""
        def welcomed() -> list[Connection]:
            return [c for c in self.connections if c.welcomed]
        await wait_until(lambda: len(welcomed()) > index, timeout, f"session {index}")
        return welcomed()[index]

    async def _handle(self, ws: ServerConnection) -> None:
        connection = Connection(ws, len(self.connections), time.monotonic())
        connection.reading.set()
        self.connections.append(connection)
        try:
            await self._serve(connection)
        except ConnectionClosed:
            pass
        finally:
            connection.close_code = ws.close_code
            connection.closed.set()

    async def _serve(self, connection: Connection) -> None:
        ws = connection.ws
        first = parse_control(await ws.recv())
        assert isinstance(first, Hello), first
        connection.hello = first
        reason = self.reject(connection.index) if callable(self.reject) else self.reject
        if reason is not None:
            await ws.send(json.dumps({"type": "reject", "reason": reason}))
            await ws.close()
            return
        await connection.send(
            Welcome(
                protocol=self.welcome_protocol,
                server_t_ns=time.time_ns(),
                heartbeat_s=self.heartbeat_s,
                config_version=self.config_version,
                channels=self.channels,
                simulation=self.simulation or SimulationInfo(enabled=False),
            )
        )
        connection.welcomed = True
        while True:
            await connection.reading.wait()
            raw = await ws.recv()
            if isinstance(raw, str):
                message = parse_control(raw)
                connection.events.append((message.type, message, None))
                if isinstance(message, Heartbeat) and self.ack_heartbeats:
                    if self.ack_delay_s:
                        await asyncio.sleep(self.ack_delay_s)
                    await connection.send(
                        HeartbeatAck(seq=message.seq, server_t_ns=time.time_ns())
                    )
                continue
            header, payload = decode_frame(raw)
            if header["type"] == "psd":
                psd_header, psd = decode_psd(header, payload)
                connection.events.append(("psd", psd_header, psd.copy()))
            else:
                data_header = CaptureDataHeader.from_wire(header)
                connection.events.append(("capture_data", data_header, bytes(payload)))
                assembler = connection.assemblers.setdefault(
                    data_header.capture_id, CaptureAssembler(data_header.capture_id)
                )
                done = assembler.add(data_header, payload)
                if done is not None:
                    connection.captures[data_header.capture_id] = done
                if self.chunk_delay_s:
                    await asyncio.sleep(self.chunk_delay_s)


@contextlib.asynccontextmanager
async def running_agent(
    server: FakeServer, drivers: Any = None, **options: Any
) -> AsyncIterator[NodeAgent]:
    """A started `NodeAgent` pointed at `server`; stopped and awaited on exit."""
    settings: dict[str, Any] = dict(
        node_id="node-1",
        backoff_base_s=0.02,
        backoff_max_s=0.3,
        close_timeout_s=0.3,
        teardown_timeout_s=1.0,
        proxy=None,
        rng=random.Random(1),
    )
    settings.update(options)
    if drivers is not None:
        settings["driver_factory"] = drivers
    agent = NodeAgent(server.url, SITE, TOKEN, **settings)
    task = agent.start()
    try:
        yield agent
    finally:
        await agent.stop(timeout_s=3.0)
        failure = task.exception() if task.done() and not task.cancelled() else None
        if failure is not None and not isinstance(failure, NodeRejected):
            raise failure


def request(
    capture_id: str,
    channel_id: str,
    n: int = 1000,
    *,
    t_start_ns: int | None = None,
    fs: float = 1e6,
    fc: float = 250e6,
    deadline_s: float = 5.0,
) -> CaptureRequest:
    start = time.time_ns() if t_start_ns is None else t_start_ns
    return CaptureRequest(
        capture_id=capture_id,
        channel_id=channel_id,
        t_start_ns=start,
        n_samples=n,
        sample_rate_hz=fs,
        center_hz=fc,
        deadline_ns=max(start, time.time_ns()) + int(deadline_s * NS),
    )


# ============================================================================ clock


def test_system_clock_reads_utc_and_reports_ntp_unless_told_otherwise() -> None:
    clock = SystemClock()
    before = time.time_ns()
    now = clock.now_ns()
    after = time.time_ns()
    assert isinstance(now, int) and before <= now <= after
    assert clock.status() == ClockStatus(
        source="ntp", locked=True, est_offset_ns=0.0, holdover_s=0.0
    )
    free = SystemClock(source="free", locked=False, est_offset_ns=-125.0)
    status = free.status()
    assert (status.source, status.locked, status.est_offset_ns) == ("free", False, -125.0)
    assert status.holdover_s >= 0.0
    with pytest.raises(ValueError, match="source"):
        SystemClock(source="atomic")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="est_offset_ns"):
        SystemClock(est_offset_ns=float("nan"))
    assert isinstance(clock, ClockSource) and isinstance(ManualClock(), ClockSource)


def test_system_clock_holdover_counts_from_loss_of_lock() -> None:
    clock = SystemClock()
    clock.set_locked(False)
    time.sleep(0.02)
    first = clock.status().holdover_s
    assert 0.02 <= first < 5.0
    clock.set_locked(True, est_offset_ns=40.0)
    assert clock.status() == ClockStatus(
        source="ntp", locked=True, est_offset_ns=40.0, holdover_s=0.0
    )


async def test_system_clock_sleep_until_passes_the_time_asked_for() -> None:
    clock = SystemClock()
    target = clock.now_ns() + 30_000_000
    await clock.sleep_until(target)
    assert target <= clock.now_ns() < target + NS
    started = time.monotonic()
    await clock.sleep_until(clock.now_ns() - NS)  # the past: at once
    assert time.monotonic() - started < 0.5


async def test_manual_clock_moves_only_when_told_and_wakes_sleepers_in_time_order() -> None:
    clock = ManualClock(1_000 * NS)
    woke: list[str] = []

    async def sleeper(name: str, t_ns: int) -> None:
        await clock.sleep_until(t_ns)
        woke.append(name)

    tasks = [
        asyncio.create_task(sleeper("late", 1_000 * NS + 300)),
        asyncio.create_task(sleeper("early", 1_000 * NS + 100)),
    ]
    await asyncio.sleep(0.01)
    assert woke == [] and clock.sleepers == 2 and clock.now_ns() == 1_000 * NS
    assert clock.advance(100) == 1_000 * NS + 100
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woke == ["early"]
    clock.set(1_000 * NS + 10_000)
    await asyncio.gather(*tasks)
    assert woke == ["early", "late"] and clock.sleepers == 0
    await clock.sleep_until(5)  # already past
    with pytest.raises(ValueError, match="before the current time"):
        clock.set(1)
    with pytest.raises(ValueError, match="dt_ns"):
        clock.advance(-1)
    clock.set_status(locked=False, holdover_s=12.5, source="gpsdo")
    assert clock.status() == ClockStatus(
        source="gpsdo", locked=False, est_offset_ns=0.0, holdover_s=12.5
    )


def test_disciplined_clock_extension_point_keeps_the_holdover() -> None:
    class Gpsdo(DisciplinedClock):
        def __init__(self) -> None:
            super().__init__()
            self.locked = True
            self.offset = 3.5
            self.mono = 100.0

        def read_reference(self) -> ReferenceReading:
            return ReferenceReading(locked=self.locked, est_offset_ns=self.offset)

        def _monotonic(self) -> float:
            return self.mono

    clock = Gpsdo()
    assert clock.status() == ClockStatus(
        source="gpsdo", locked=True, est_offset_ns=3.5, holdover_s=0.0
    )
    clock.locked, clock.mono = False, 107.25
    assert clock.status().locked is False
    assert clock.status().holdover_s == pytest.approx(7.25)
    clock.mono = 110.0
    assert clock.status().holdover_s == pytest.approx(10.0)
    clock.locked = True
    assert clock.status().holdover_s == 0.0
    assert abs(clock.now_ns() - time.time_ns()) < NS  # host time unless overridden

    class Broken(DisciplinedClock):
        def read_reference(self) -> ReferenceReading:
            return None  # type: ignore[return-value]

    with pytest.raises(ValueError, match="read_reference"):
        Broken().status()


# ======================================================================== handshake


async def test_handshake_hello_welcome_and_assign_ack_with_capabilities(
    no_leaks: None, tmp_path: Any
) -> None:
    channels = [channel("ch-a", monitor()), channel("ch-b")]
    drivers = Drivers()
    state = tmp_path / "state" / "node.json"
    async with FakeServer(channels) as server:
        async with running_agent(server, drivers, node_id=None, state_path=state) as agent:
            session = await server.session()
            (ack,) = await session.expect("assign_ack")
            hello = session.hello
            assert hello is not None
            assert hello.protocol == 1 and hello.site_id == SITE and hello.token == TOKEN
            assert hello.driver == "noise" and hello.software.startswith("petal-locate/")
            assert hello.clock == ClockStatus(
                source="ntp", locked=True, est_offset_ns=0.0, holdover_s=0.0
            )
            assert abs(hello.t_ns - time.time_ns()) < 2 * NS
            # node_id: generated once, persisted, reported
            assert hello.node_id == agent.node_id == json.loads(state.read_text())["node_id"]
            assert len(hello.node_id) == 36
            # permessage-deflate is not offered
            assert session.ws.request is not None
            assert "Sec-WebSocket-Extensions" not in session.ws.request.headers
            # assign_ack: one entry per channel with the driver's capabilities
            assert isinstance(ack, AssignAck)
            assert ack.config_version == 7 and ack.ok is True
            assert [entry.channel_id for entry in ack.channels] == ["ch-a", "ch-b"]
            for entry in ack.channels:
                assert entry.ok and entry.error is None
                assert entry.capabilities == drivers[entry.channel_id].capabilities.to_wire()
            assert agent.state == "online"
            # each driver was opened once with the session context
            for channel_id in ("ch-a", "ch-b"):
                driver = drivers[channel_id]
                assert driver.opened == 1 and driver.assignment == channels[
                    ["ch-a", "ch-b"].index(channel_id)
                ]
                assert driver.ctx is not None
                assert driver.ctx.site_id == SITE and driver.ctx.token == TOKEN
                assert driver.ctx.server_http_url == f"http://127.0.0.1:{server.port}"
                assert driver.ctx.simulation is None
                assert driver.ctx.clock is agent.clock
                assert isinstance(driver.ctx.workers, WorkerPool)
                assert TOKEN not in repr(driver.ctx)
        assert all(drivers[c].closed == 1 for c in ("ch-a", "ch-b"))

        # a second agent with the same state file keeps the identity
        async with running_agent(server, Drivers(), node_id=None, state_path=state) as again:
            second = await server.session(1)
            assert second.hello is not None and second.hello.node_id == hello.node_id
            assert again.node_id == hello.node_id


async def test_explicit_node_id_and_simulation_context(no_leaks: None) -> None:
    simulation = SimulationInfo(enabled=True, scenario_url="/api/v1/sim/world", version=4)
    drivers = Drivers()
    async with FakeServer([channel("ch-a")], simulation=simulation) as server:
        async with running_agent(server, drivers, node_id="fixed-id", driver="sim"):
            session = await server.session()
            await session.expect("assign_ack")
            assert session.hello is not None
            assert session.hello.node_id == "fixed-id" and session.hello.driver == "sim"
            assert drivers["ch-a"].ctx is not None
            assert drivers["ch-a"].ctx.simulation == {
                "enabled": True,
                "scenario_url": "/api/v1/sim/world",
                "version": 4,
            }


def test_node_id_state_file_is_created_once_and_a_corrupt_one_is_refused(tmp_path: Any) -> None:
    path = tmp_path / "deep" / "node.json"
    first = load_or_create_node_id(path)
    assert load_or_create_node_id(path) == first and len(first) == 36
    assert [p.name for p in path.parent.iterdir()] == ["node.json"]  # no temporary left
    for text in ("not json", "[]", '{"node_id": ""}', '{"node_id": 7}'):
        bad = tmp_path / "bad.json"
        bad.write_text(text)
        with pytest.raises(ValueError, match=r"bad\.json"):
            load_or_create_node_id(bad)
    assert default_state_path("site/../x").name == "node-site_.._x.json"


def test_server_url_normalisation() -> None:
    assert normalize_server_url("ws://10.0.0.5:8000") == (
        "ws://10.0.0.5:8000/ws/node",
        "http://10.0.0.5:8000",
    )
    assert normalize_server_url("https://hq.example/") == (
        "wss://hq.example/ws/node",
        "https://hq.example",
    )
    assert normalize_server_url("wss://[::1]:9/custom/path?x=1") == (
        "wss://[::1]:9/custom/path?x=1",
        "https://[::1]:9",
    )
    for bad in ("", "ftp://host", "ws://", "ws://user:pw@host"):
        with pytest.raises(ValueError, match="server_url"):
            normalize_server_url(bad)
    with pytest.raises(ValueError, match="site_id"):
        NodeAgent("ws://h", "", "t")
    with pytest.raises(ValueError, match="backoff"):
        NodeAgent("ws://h", "s", "t", backoff_base_s=5.0, backoff_max_s=1.0)


# ================================================================ reject and backoff


def test_backoff_delay_doubles_to_the_cap_with_up_to_twenty_percent_jitter() -> None:
    class Fixed(random.Random):
        def __init__(self, value: float) -> None:
            super().__init__()
            self.value = value

        def random(self) -> float:
            return self.value

    nominal = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert [backoff_delay(k, rng=Fixed(0.0)) for k in range(8)] == nominal
    assert [backoff_delay(k, rng=Fixed(0.5)) for k in range(8)] == pytest.approx(
        [d * 1.1 for d in nominal]
    )
    rng = random.Random(3)
    for k in range(12):
        low = min(30.0, 2.0**k)
        draws = [backoff_delay(k, rng=rng) for _ in range(200)]
        assert low <= min(draws) and max(draws) <= low * 1.2
        assert max(draws) - min(draws) > 0.15 * low  # the jitter is really spread out
    assert backoff_delay(10_000, 0.02, 0.3, 0.0) == 0.3  # no overflow far out
    with pytest.raises(ValueError, match="failures"):
        backoff_delay(-1)
    with pytest.raises(ValueError, match="jitter"):
        backoff_delay(0, jitter=2.0)


@pytest.mark.parametrize("reason", ["bad_token", "unknown_site", "duplicate_node", "from_the_future"])
async def test_reject_that_waiting_may_cure_backs_off_to_the_maximum(
    no_leaks: None, reason: str
) -> None:
    """A wrong token or an unknown site must not hammer the server."""
    assert REJECT_POLICY.get(reason, "retry_max") == "retry_max"
    async with FakeServer(reject=reason) as server:
        async with running_agent(server, Drivers(), backoff_base_s=0.01, backoff_max_s=0.25) as agent:
            await wait_until(lambda: len(server.connections) >= 2, 3.0, "second attempt")
            first, second = server.connections[:2]
            gap = second.opened_at - first.opened_at
            assert 0.25 <= gap < 2.0, gap  # the longest wait, not the 10 ms base
            assert agent.last_reject == reason and agent.stats.rejects >= 1
            assert agent.stats.sessions == 0
            assert all(0.25 <= d <= 0.30 for d in agent.stats.backoff_delays)
            assert agent.state in ("backoff", "connecting")
        assert len(server.connections) <= 3


async def test_reject_protocol_mismatch_stops_the_agent(no_leaks: None) -> None:
    async with FakeServer(reject="protocol_mismatch") as server:
        agent = NodeAgent(server.url, SITE, TOKEN, node_id="n", backoff_base_s=0.01, proxy=None)
        with pytest.raises(NodeRejected) as caught:
            await asyncio.wait_for(agent.run(), 3.0)
        assert caught.value.reason == "protocol_mismatch"
        assert agent.state == "stopped"
        await asyncio.sleep(0.1)
        assert len(server.connections) == 1  # it did not come back


async def test_welcome_with_another_protocol_version_stops_the_agent(no_leaks: None) -> None:
    async with FakeServer(welcome_protocol=2) as server:
        agent = NodeAgent(server.url, SITE, TOKEN, node_id="n", backoff_base_s=0.01, proxy=None)
        with pytest.raises(NodeRejected, match="protocol 2"):
            await asyncio.wait_for(agent.run(), 3.0)
        assert agent.stats.sessions == 0


async def test_reject_then_accept_recovers_and_resets_the_backoff(no_leaks: None) -> None:
    async with FakeServer(
        [channel("ch-a")], reject=lambda index: "bad_token" if index == 0 else None
    ) as server:
        async with running_agent(server, Drivers(), backoff_max_s=0.1) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            assert session.index == 1 and agent.stats.rejects == 1
            await session.ws.close()  # drop: the next wait is the short base again
            await server.session(1)
            assert agent.stats.backoff_delays[-1] <= 0.02 * 1.2


async def test_unreachable_server_is_retried_with_doubling_backoff(no_leaks: None) -> None:
    with socket.socket() as probe:  # a loopback port with nothing listening
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    agent = NodeAgent(
        f"ws://127.0.0.1:{port}", SITE, TOKEN, node_id="n", proxy=None,
        backoff_base_s=0.01, backoff_max_s=0.08, backoff_jitter=0.0,
    )
    task = agent.start()
    await wait_until(lambda: len(agent.stats.backoff_delays) >= 6, 3.0, "six attempts")
    assert list(agent.stats.backoff_delays)[:6] == [0.01, 0.02, 0.04, 0.08, 0.08, 0.08]
    assert agent.stats.sessions == 0 and agent.state in ("backoff", "connecting")
    await agent.stop(timeout_s=1.0)  # stop() interrupts the backoff sleep
    assert task.done() and agent.state == "stopped"


async def test_session_replaced_by_the_server_backs_off_to_the_maximum(no_leaks: None) -> None:
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, Drivers(), backoff_max_s=0.25) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            closed_at = time.monotonic()
            await session.ws.close(4001, "replaced")
            again = await server.session(1)
            assert again.opened_at - closed_at >= 0.25
            assert agent.stats.backoff_delays[-1] >= 0.25


SMALL_PIPE: dict[str, Any] = dict(rcvbuf=32 * 1024, max_queue=1)
"""`FakeServer` options that, with the agent option `send_buffer_bytes=32 * 1024`, make
the path from the node's writer to the server's handler hold well under a megabyte:
a server that stops reading then pushes back on the node at once, on any machine."""


# ======================================================================= heartbeats


async def test_heartbeat_cadence_and_round_trip_time(no_leaks: None) -> None:
    period, ack_delay = 0.05, 0.02
    async with FakeServer(heartbeat_s=period, ack_delay_s=ack_delay) as server:
        async with running_agent(server, Drivers()) as agent:
            session = await server.session()
            started = time.monotonic()
            beats: list[Heartbeat] = await session.expect("heartbeat", 10, timeout=3.0)
            elapsed = time.monotonic() - started
            beats = list(beats)
            # one per period, the first at once: 10 beats take 9 periods (a loaded
            # machine may stretch single periods, never shorten them)
            assert 9 * period * 0.9 <= elapsed <= 9 * period * 4, elapsed
            assert [beat.seq for beat in beats] == list(range(len(beats)))
            gaps = np.diff([beat.t_ns for beat in beats]) / 1e9
            assert np.all(gaps > 0) and abs(float(np.median(gaps)) - period) < 0.3 * period
            # rtt_ms: absent on the first, then the measured round trip of the previous one
            assert beats[0].rtt_ms is None
            rtts = [beat.rtt_ms for beat in beats[1:]]
            assert all(rtt is not None and rtt >= ack_delay * 1e3 for rtt in rtts), rtts
            assert float(np.median(rtts)) < (ack_delay + period) * 1e3, rtts
            assert agent.last_rtt_ms is not None and agent.last_rtt_ms >= ack_delay * 1e3
            for beat in beats:
                assert beat.clock.source == "ntp" and beat.clock.locked
                assert beat.load.queue == 0 and 0.0 <= beat.load.cpu < 8.0
                assert abs(beat.t_ns - time.time_ns()) < 5 * NS
            assert agent.stats.heartbeats_sent >= 10


async def test_rtt_reports_a_lower_bound_while_acks_are_missing(no_leaks: None) -> None:
    """With no ack, the node must not keep reporting a latency it no longer has."""
    period = 0.04
    async with FakeServer(heartbeat_s=period, ack_heartbeats=False) as server:
        async with running_agent(server, Drivers(), server_silence_timeout_s=5.0):
            session = await server.session()
            beats: list[Heartbeat] = list(await session.expect("heartbeat", 5))[:5]
            assert beats[0].rtt_ms is None
            for beat in beats[1:]:
                # the age of the oldest unanswered heartbeat (the first one), which is
                # what separates the two send times on the node clock
                assert beat.rtt_ms is not None
                age_ms = (beat.t_ns - beats[0].t_ns) / 1e6
                assert beat.rtt_ms == pytest.approx(age_ms, abs=10.0), (beat.seq, beat.rtt_ms)
            assert beats[4].rtt_ms >= 3 * period * 1e3  # type: ignore[operator]


async def test_silent_server_is_dropped_and_redialled(no_leaks: None) -> None:
    async with FakeServer(ack_heartbeats=False) as server:
        async with running_agent(server, Drivers(), server_silence_timeout_s=0.2) as agent:
            first = await server.session()
            second = await server.session(1, timeout=3.0)
            assert 0.2 <= second.opened_at - first.opened_at < 3.0
            await first.closed.wait()
            assert first.close_code == 1011
            await wait_until(lambda: agent.stats.sessions >= 2, 2.0, "the second welcome")


# ================================================================== spectrum frames


async def test_spectrum_frames_arrive_at_the_requested_rate(no_leaks: None) -> None:
    """Noise driver, IQ path: read nfft*navg samples at fs = span, Welch, send."""
    rate, nfft, navg = 25.0, 64, 4
    window = monitor("uhf", center_hz=251.5e6, span_hz=1e6, nfft=nfft, navg=navg, frame_rate_hz=rate)
    async with FakeServer([channel("ch-a", window)]) as server:
        async with running_agent(server, driver="noise") as agent:
            session = await server.session()
            await session.expect("assign_ack")
            started = time.monotonic()
            await session.expect("psd", 20, timeout=4.0)
            elapsed = time.monotonic() - started
            frames = session.of("psd")[:20]
            assert 19 / rate * 0.9 <= elapsed <= 19 / rate * 4, elapsed
            headers = [header for header, _ in frames]
            assert all(isinstance(header, PsdHeader) for header in headers)
            assert [header.seq for header in headers] == list(range(20))  # nothing dropped
            for header, psd in frames:
                assert (header.channel_id, header.monitor_id) == ("ch-a", "uhf")
                assert (header.center_hz, header.span_hz) == (251.5e6, 1e6)
                assert (header.nfft, header.navg, header.units) == (nfft, navg, "dB")
                assert psd.dtype == np.float32 and psd.shape == (nfft,)
            steps = np.diff([header.t_ns for header in headers]) / 1e9
            assert abs(float(np.median(steps)) - 1 / rate) < 0.25 / rate
            assert abs(headers[-1].t_ns - time.time_ns()) < 2 * NS  # labelled with node time
            # unit noise reads 0 dB per bin in power; the mean of the dB values of a
            # navg-fold average sits at 10 log10(e) (psi(navg) - ln navg) = -0.57 dB
            expected = 10 / np.log(10) * (digamma(navg) - np.log(navg))
            measured = float(np.mean([psd for _, psd in frames]))
            assert abs(measured - expected) < 0.35, (measured, expected)
            assert agent.stats.frames_sent >= 20 and agent.stats.frames_dropped == 0


async def test_stalled_server_gets_the_newest_frame_per_window_not_a_backlog(
    no_leaks: None,
) -> None:
    """Slow consumer: while the socket is blocked each window keeps one frame, the newest."""
    nfft, rate = 4096, 100.0
    windows = [monitor(name, nfft=nfft, navg=1, frame_rate_hz=rate) for name in ("m0", "m1")]
    drivers = Drivers()
    drivers.prepare = lambda driver: setattr(driver, "fast_psd", np.zeros(nfft, np.float32))
    async with FakeServer([channel("ch-a", *windows)], **SMALL_PIPE) as server:
        async with running_agent(
            server, drivers, send_buffer_bytes=32 * 1024, server_silence_timeout_s=30.0
        ) as agent:
            session = await server.session()
            await session.expect("psd", 6)
            assert agent.stats.frames_dropped == 0
            session.stall()
            # the pipe fills, the writer blocks, and from then on frames replace each other
            await wait_until(lambda: agent.stats.frames_dropped >= 40, 6.0, "dropped frames")
            sent_while_stalled = agent.stats.frames_sent
            await asyncio.sleep(0.1)
            assert agent.stats.frames_sent == sent_while_stalled  # really blocked
            before_resume = len(session.of("psd"))
            resumed_ns = time.time_ns()
            session.resume()
            await wait_until(
                lambda: len(session.of("psd")) >= before_resume + 40, 6.0, "frames after the stall"
            )
            for name in ("m0", "m1"):
                headers = [h for h, _ in session.of("psd") if h.monitor_id == name]
                seqs = np.array([h.seq for h in headers])
                jumps = np.diff(seqs)
                assert np.all(jumps >= 1)  # in order, never repeated
                gap_at = int(np.argmax(jumps))
                assert jumps[gap_at] >= 15, jumps  # a run of frames was dropped ...
                assert np.count_nonzero(jumps >= 10) == 1  # ... in one place only
                # ... and what came through after the gap is the newest frame, taken
                # at about the time the server started reading again, not an old one
                first_after = headers[gap_at + 1]
                assert abs(first_after.t_ns - resumed_ns) < 0.5 * NS, (
                    (first_after.t_ns - resumed_ns) / 1e9
                )
            assert server.connections == [session]  # the session survived the stall


# ========================================================================= captures


async def test_large_capture_is_chunked_in_order_and_bit_exact_while_heartbeats_flow(
    no_leaks: None,
) -> None:
    n, period = 1_000_003, 0.02
    window = monitor("m0", nfft=64, navg=1, frame_rate_hz=50.0)
    drivers = Drivers()

    def prepare(driver: FakeDriver) -> None:
        driver.fast_psd = np.zeros(64, np.float32)
        driver.t_first_offset_ns = 1_234_567  # the driver's own first-sample time

    drivers.prepare = prepare
    async with FakeServer(
        [channel("ch-a", window)], heartbeat_s=period, chunk_delay_s=0.008, **SMALL_PIPE
    ) as server:
        async with running_agent(
            server,
            drivers,
            send_buffer_bytes=32 * 1024,
            chunk_payload_bytes=MAX_CHUNK_PAYLOAD_BYTES,  # the largest the protocol allows
        ) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            ask = request("cap-1", "ch-a", n, fs=1e6, fc=251e6, deadline_s=20.0)
            await session.send(ask)
            done = await session.capture("cap-1", timeout=15.0)

            # bit-exact against what the driver returned, with the driver's metadata
            sent = drivers["ch-a"].returned[ask.t_start_ns]
            assert drivers["ch-a"].reads[-1] == (ask.t_start_ns, n, 1e6, 251e6)
            assert done.iq.dtype == np.complex64 and done.iq.shape == (n,)
            assert done.iq.tobytes() == sent.tobytes()
            assert done.t_first_ns == ask.t_start_ns + 1_234_567
            assert (done.sample_rate_hz, done.center_hz, done.overflow) == (1e6, 251e6, False)
            assert done.clock == ClockStatus(
                source="ntp", locked=True, est_offset_ns=0.0, holdover_s=0.0
            )

            # chunks: at most 256 KiB of payload, in order, contiguous, one `last`
            chunks = session.of("capture_data")
            per_chunk = MAX_CHUNK_PAYLOAD_BYTES // 8
            assert len(chunks) == done.n_chunks == -(-n // per_chunk) == 31
            offset = 0
            for seq, (header, payload) in enumerate(chunks):
                assert len(payload) <= MAX_CHUNK_PAYLOAD_BYTES and len(payload) == 8 * header.n
                assert (header.seq, header.offset, header.n_samples_total) == (seq, offset, n)
                assert header.last is (seq == len(chunks) - 1)
                assert header.t_first_ns == ask.t_start_ns + 1_234_567
                offset += header.n
            assert offset == n
            assert b"".join(payload for _, payload in chunks) == sent.tobytes()

            # heartbeats and spectrum frames kept flowing through the upload
            kinds = [kind for kind, _, _ in session.events]
            first = kinds.index("capture_data")
            last = len(kinds) - 1 - kinds[::-1].index("capture_data")
            during = kinds[first:last]
            assert during.count("heartbeat") >= 3, during
            assert during.count("psd") >= 3, during
            beats = [m for k, m, _ in session.events[first:last] if k == "heartbeat"]
            assert all(b.seq - a.seq == 1 for a, b in itertools.pairwise(beats))
            assert any(beat.load.queue == 1 for beat in beats)  # the capture in progress
            assert agent.stats.captures_completed == 1 and agent.stats.captures_failed == 0
            assert agent.stats.chunks_sent == 31
            assert session.of("capture_error") == []


async def expect_error(session: Connection, capture_id: str) -> CaptureErrorMessage:
    await wait_until(
        lambda: any(e.capture_id == capture_id for e in session.of("capture_error")),
        3.0,
        f"capture_error for {capture_id}",
    )
    (error,) = [e for e in session.of("capture_error") if e.capture_id == capture_id]
    return error


async def test_capture_errors_out_of_range_too_late_and_driver(no_leaks: None) -> None:
    drivers = Drivers()
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, drivers) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            driver = drivers["ch-a"]  # 100..400 MHz, 2 MS/s

            # out of range: rate and centre against the capabilities that were acked
            await session.send(request("rate", "ch-a", fs=2.5e6))
            error = await expect_error(session, "rate")
            assert (error.code, error.channel_id) == ("out_of_range", "ch-a")
            assert "sample rate" in error.message
            await session.send(request("centre", "ch-a", fc=450e6))
            assert (await expect_error(session, "centre")).code == "out_of_range"
            assert driver.reads == []  # refused before the driver was bothered

            # out of range as judged by the driver itself
            driver.read_error = CaptureError("out_of_range", "LO cannot reach that")
            await session.send(request("driver-range", "ch-a"))
            error = await expect_error(session, "driver-range")
            assert (error.code, error.message) == ("out_of_range", "LO cannot reach that")

            # too late: the driver no longer holds the interval
            driver.read_error = CaptureError("too_late", "interval is gone")
            await session.send(request("gone", "ch-a"))
            assert (await expect_error(session, "gone")).code == "too_late"

            # too late: the deadline had passed when the request arrived
            driver.read_error = None
            reads = len(driver.reads)
            past = time.time_ns() - 2 * NS
            await session.send(
                CaptureRequest(
                    capture_id="expired", channel_id="ch-a", t_start_ns=past, n_samples=10,
                    sample_rate_hz=1e6, center_hz=250e6, deadline_ns=past + NS,
                )
            )
            error = await expect_error(session, "expired")
            assert error.code == "too_late" and "deadline" in error.message
            assert len(driver.reads) == reads

            # too late: the deadline passes while the driver is still reading
            driver.hold = asyncio.Event()
            await session.send(request("slow", "ch-a", deadline_s=0.15))
            error = await expect_error(session, "slow")
            assert error.code == "too_late" and driver.cancelled_reads == 1
            driver.hold = None

            # overflow reported by the driver
            driver.read_error = CaptureError("overflow", "lost 4096 samples")
            await session.send(request("overflow", "ch-a"))
            assert (await expect_error(session, "overflow")).code == "overflow"

            # any other exception of a driver is a `driver` error, and its own
            # TimeoutError is not mistaken for the deadline
            driver.read_error = RuntimeError("usb transfer failed")
            await session.send(request("boom", "ch-a"))
            error = await expect_error(session, "boom")
            assert error.code == "driver" and "RuntimeError: usb transfer failed" in error.message
            driver.read_error = TimeoutError("tuner did not settle")
            await session.send(request("tuner", "ch-a"))
            error = await expect_error(session, "tuner")
            assert error.code == "driver" and "tuner did not settle" in error.message

            # a channel this node does not have
            await session.send(request("nowhere", "ch-z"))
            error = await expect_error(session, "nowhere")
            assert error.code == "driver" and "ch-z" in error.message

            # unusable results: too few samples, not finite, not complex
            driver.read_error = None
            for name, broken in (
                ("short", lambda c: Capture(c.iq[:-1], c.t_first_ns, c.fs, c.fc)),
                ("nan", lambda c: Capture(c.iq * np.float32("nan"), c.t_first_ns, c.fs, c.fc)),
                ("real", lambda c: Capture(c.iq.real.copy(), c.t_first_ns, c.fs, c.fc)),
            ):
                driver.result_override = broken
                await session.send(request(name, "ch-a"))
                assert (await expect_error(session, name)).code == "driver"
            driver.result_override = None

            # none of this hurt the node: the next request is served, here from a
            # driver that returns more samples than asked for, in double precision
            driver.result_override = lambda c: Capture(
                np.concatenate([c.iq, c.iq]).astype(np.complex128), c.t_first_ns, c.fs, c.fc
            )
            await session.send(request("fine", "ch-a", 500))
            done = await session.capture("fine")
            assert done.iq.dtype == np.complex64 and done.n_samples == 500
            assert done.iq.tobytes() == driver.returned[driver.reads[-1][0]].tobytes()
            assert session.of("capture_data")[-1][0].capture_id == "fine"
            assert (done.overflow, done.clock.locked) == (False, True)

            # what the driver says about lost samples and its time base reaches the server
            driver.result_override = lambda c: Capture(
                c.iq, c.t_first_ns, c.fs, c.fc, overflow=True, clock_locked=False
            )
            await session.send(request("flagged", "ch-a", 64))
            flagged = await session.capture("flagged")
            assert flagged.overflow is True
            assert (flagged.clock.source, flagged.clock.locked) == ("ntp", False)
            driver.result_override = None
            assert agent.stats.captures_failed == 13 and agent.stats.captures_completed == 2
            assert len(server.connections) == 1


async def test_noise_driver_serves_and_refuses_captures_through_the_agent(no_leaks: None) -> None:
    """End to end with a real driver: its own range and lookback checks reach the wire."""
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(
            server, driver="noise", driver_options={"lookback_s": 0.5, "max_sample_rate_hz": 2e6}
        ):
            session = await server.session()
            (ack,) = await session.expect("assign_ack")
            assert ack.channels[0].capabilities.lookback_s == 0.5
            assert ack.channels[0].capabilities.max_sample_rate_hz == 2e6

            start = time.time_ns() + 30_000_000  # slightly in the future: the driver waits
            await session.send(request("ok", "ch-a", 4096, t_start_ns=start))
            done = await session.capture("ok")
            assert time.time_ns() >= start + 4_096_000  # not before the interval had passed
            assert done.t_first_ns == start and done.sample_rate_hz == 1e6
            assert np.array_equal(done.iq, noise_samples(0, "ch-a", start, 4096))

            lookback = time.time_ns() - 200_000_000  # recent past, inside the lookback
            await session.send(request("past", "ch-a", 256, t_start_ns=lookback))
            assert (await session.capture("past")).t_first_ns == lookback

            ancient = time.time_ns() - 3 * NS
            await session.send(request("ancient", "ch-a", 256, t_start_ns=ancient))
            error = await expect_error(session, "ancient")
            assert error.code == "too_late" and "lookback" in error.message

            await session.send(request("fast", "ch-a", fs=3e6))
            assert (await expect_error(session, "fast")).code == "out_of_range"


async def test_capture_cancel_stops_the_read_and_is_acknowledged(no_leaks: None) -> None:
    drivers = Drivers()
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, drivers):
            session = await server.session()
            await session.expect("assign_ack")
            driver = drivers["ch-a"]
            driver.hold = asyncio.Event()
            await session.send(request("c1", "ch-a"))
            await asyncio.wait_for(driver.read_started.wait(), 2.0)
            await session.send(CaptureCancel(capture_id="c1"))
            error = await expect_error(session, "c1")
            assert error.code == "cancelled" and driver.cancelled_reads == 1
            await session.send(CaptureCancel(capture_id="never-existed"))  # advisory: ignored
            driver.hold = None
            await session.send(request("c2", "ch-a"))
            await session.capture("c2")
            assert [h.capture_id for h, _ in session.of("capture_data")] == ["c2"]
            assert len(session.of("capture_error")) == 1


async def test_a_capture_the_server_withdraws_is_logged_at_info_not_as_a_failure(
    no_leaks: None, caplog: pytest.LogCaptureFixture
) -> None:
    """R16: a capture the server cancels on purpose (capture_cancel, or an assign that
    removes the channel) is answered 'cancelled' as before but logged at INFO; a capture
    that really fails is still a WARNING."""
    drivers = Drivers()
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, drivers):
            session = await server.session()
            await session.expect("assign_ack")
            driver = drivers["ch-a"]
            driver.hold = asyncio.Event()
            with caplog.at_level(logging.INFO, logger="petal_locate.node.agent"):
                await session.send(request("c1", "ch-a"))
                await asyncio.wait_for(driver.read_started.wait(), 2.0)
                await session.send(CaptureCancel(capture_id="c1"))
                assert (await expect_error(session, "c1")).code == "cancelled"
                driver.hold = None
                await session.send(request("c2", "ch-a", fc=1e12))  # outside the channel
                assert (await expect_error(session, "c2")).code == "out_of_range"
            records = [r for r in caplog.records if r.name == "petal_locate.node.agent" and "capture" in r.getMessage()]
            withdrawn = [r for r in records if "withdrawn by the server" in r.getMessage()]
            failed = [r for r in records if r.getMessage().startswith("capture failed")]
            assert len(withdrawn) == 1 and withdrawn[0].levelno == logging.INFO
            assert "cancelled by the server" in withdrawn[0].getMessage()
            assert len(failed) == 1 and failed[0].levelno == logging.WARNING
            assert "center 1000000000000.000 Hz is outside" in failed[0].getMessage()


async def test_a_sim_update_that_arrives_while_a_channel_opens_reaches_its_driver(no_leaks: None) -> None:
    """R12: a sim_update that arrives while the channels of an assign are still opening
    met no open driver and was dropped ('no driver wants it'); the node now remembers the
    newest one and hands it to each driver as it opens."""
    built: dict[str, HookedDriver] = {}

    def factory(assignment: ChannelAssignment) -> HookedDriver:
        built[assignment.channel_id] = HookedDriver(assignment.channel_id, open_delay_s=0.5)
        return built[assignment.channel_id]

    async with FakeServer([channel("ch-sim")]) as server:
        async with running_agent(server, factory):
            session = await server.session()
            effective = time.time_ns() + 2 * NS
            await session.send(SimUpdate(version=4, effective_ns=effective - 1))
            await session.send(SimUpdate(version=5, effective_ns=effective))
            await session.expect("assign_ack")
            hooked = built["ch-sim"]
            await wait_until(lambda: len(hooked.sim_updates) >= 1, 2.0, "the sim update")
            assert hooked.sim_updates == [(5, effective)]  # the newest, once
            await session.send(SimUpdate(version=6, effective_ns=effective + 1))  # open now: forwarded as before
            await wait_until(lambda: len(hooked.sim_updates) == 2, 2.0, "the next sim update")
            assert hooked.sim_updates[-1] == (6, effective + 1)


async def test_concurrent_captures_on_two_channels(no_leaks: None) -> None:
    n = 100_000
    per_capture = -(-n * 8 // DEFAULT_CHUNK_PAYLOAD_BYTES)  # 25 chunks each
    drivers = Drivers()
    gate = asyncio.Event()
    drivers.prepare = lambda driver: setattr(driver, "hold", gate)
    async with FakeServer([channel("ch-a"), channel("ch-b")]) as server:
        async with running_agent(server, drivers):
            session = await server.session()
            await session.expect("assign_ack")
            asks = {name: request(f"cap-{name}", f"ch-{name}", n) for name in ("a", "b")}
            for ask in asks.values():
                await session.send(ask)
            # both drivers are reading at the same time
            await wait_until(lambda: FakeDriver.live_reads == 2, 2.0, "two reads in flight")
            gate.set()
            for name, ask in asks.items():
                done = await session.capture(ask.capture_id)
                sent = drivers[f"ch-{name}"].returned[ask.t_start_ns]
                assert done.channel_id == f"ch-{name}" and done.iq.tobytes() == sent.tobytes()
            assert FakeDriver.peak_reads == 2
            # and the two uploads took turns on the socket
            order = [header.capture_id for header, _ in session.of("capture_data")]
            assert len(order) == 2 * per_capture == 50
            assert order.index("cap-b") < 3 and order.index("cap-a") < 3
            assert sorted(order[:2]) == ["cap-a", "cap-b"]
            # strictly alternating: neither upload ever waits for more than one chunk
            assert all(a != b for a, b in itertools.pairwise(order))


async def test_more_captures_than_the_limit_get_busy(no_leaks: None) -> None:
    drivers = Drivers()
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, drivers, max_concurrent_captures=1):
            session = await server.session()
            await session.expect("assign_ack")
            driver = drivers["ch-a"]
            driver.hold = asyncio.Event()
            await session.send(request("first", "ch-a"))
            await asyncio.wait_for(driver.read_started.wait(), 2.0)
            await session.send(request("second", "ch-a"))
            assert (await expect_error(session, "second")).code == "busy"
            await session.send(request("first", "ch-a"))  # a duplicate id is ignored
            driver.hold.set()
            await session.capture("first")
            assert len(driver.reads) == 1 and len(session.of("capture_error")) == 1


async def test_deadline_during_a_stalled_upload_voids_the_capture(no_leaks: None) -> None:
    """Slow consumer on the capture path: partial data, then `too_late`."""
    drivers = Drivers()
    async with FakeServer([channel("ch-a")], **SMALL_PIPE) as server:
        async with running_agent(
            server, drivers, send_buffer_bytes=32 * 1024, server_silence_timeout_s=30.0
        ) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            session.stall()
            n = 12 * 32768
            total = n * 8 // DEFAULT_CHUNK_PAYLOAD_BYTES  # 96 chunks: far more than the pipe holds
            await session.send(request("stuck", "ch-a", n, deadline_s=0.8))
            await wait_until(lambda: agent.stats.captures_failed == 1, 5.0, "deadline")
            # what was sent when the deadline struck; one more chunk may already sit in
            # the blocked socket, and nothing can call that one back
            blocked_at = agent.stats.chunks_sent
            assert 1 <= blocked_at < total - 1  # some went out, the rest was withdrawn
            session.resume()
            error = await expect_error(session, "stuck")
            assert error.code == "too_late" and "deadline" in error.message
            chunks = session.of("capture_data")
            sent = agent.stats.chunks_sent
            assert len(chunks) == sent and sent in (blocked_at, blocked_at + 1)
            assert not any(header.last for header, _ in chunks)
            assert "stuck" not in session.captures
            kinds = [kind for kind, _, _ in session.events]
            assert kinds.index("capture_error") > max(
                i for i, kind in enumerate(kinds) if kind == "capture_data"
            )
            await asyncio.sleep(0.05)
            assert agent.stats.chunks_sent == sent  # nothing more after the error


# ====================================================================== assignments


async def test_assign_that_removes_a_channel_fails_nothing_else(no_leaks: None) -> None:
    window = monitor("m0", nfft=64, navg=1, frame_rate_hz=50.0)
    cha, chb = channel("ch-a", window), channel("ch-b", window)
    drivers = Drivers()
    drivers.prepare = lambda driver: setattr(driver, "fast_psd", np.zeros(64, np.float32))
    async with FakeServer([cha, chb]) as server:
        async with running_agent(server, drivers) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            a, b = drivers["ch-a"], drivers["ch-b"]
            a.hold, b.hold = asyncio.Event(), asyncio.Event()
            ask_a, ask_b = request("on-a", "ch-a", 70_000), request("on-b", "ch-b")
            await session.send(ask_a)
            await session.send(ask_b)
            await wait_until(lambda: FakeDriver.live_reads == 2, 2.0, "both reads in flight")

            await session.send(Assign(config_version=8, channels=[cha]))
            _, ack = await session.expect("assign_ack", 2)
            assert ack.config_version == 8 and ack.ok
            assert [entry.channel_id for entry in ack.channels] == ["ch-a"]

            # the removed channel: driver closed, its capture answered with `cancelled`
            assert (b.closed, b.cancelled_reads) == (1, 1)
            error = await expect_error(session, "on-b")
            assert error.code == "cancelled" and "removed" in error.message
            # the channel that stayed: same driver, never closed or reopened, read intact
            assert drivers.built["ch-a"] == [a] and (a.opened, a.closed) == (1, 0)
            assert a.cancelled_reads == 0 and FakeDriver.live_reads == 1
            a.hold.set()
            done = await session.capture("on-a")
            assert done.iq.tobytes() == a.returned[ask_a.t_start_ns].tobytes()
            assert [e.capture_id for e in session.of("capture_error")] == ["on-b"]

            # spectrum: ch-b stops, ch-a goes on with its sequence unbroken
            mark = len(session.events)
            await wait_until(
                lambda: sum(k == "psd" for k, _, _ in session.events[mark:]) >= 8, 3.0, "psd"
            )
            later = [h for k, h, _ in session.events[mark:] if k == "psd"]
            assert {h.channel_id for h in later} == {"ch-a"}
            seqs = [h.seq for h, _ in session.of("psd") if h.channel_id == "ch-a"]
            assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

            # the removed channel is gone for captures too
            await session.send(request("late-b", "ch-b"))
            assert (await expect_error(session, "late-b")).code == "driver"
            assert agent.stats.sessions == 1


async def test_assign_reopens_changed_channels_and_reports_failed_ones(no_leaks: None) -> None:
    cha, chb = channel("ch-a", monitor("m0")), channel("ch-b", monitor("m0"))
    drivers = Drivers(HookedDriver)

    def prepare(driver: FakeDriver) -> None:
        driver.fast_psd = np.zeros(64, np.float32)
        if driver.channel_id == "ch-c":
            driver.open_error = CaptureError("driver", "no such device")
        if driver.channel_id == "ch-d":
            driver.open_delay_s = 5.0

    drivers.prepare = prepare
    async with FakeServer([cha, chb]) as server:
        async with running_agent(server, drivers, driver_open_timeout_s=0.15):
            session = await server.session()
            await session.expect("assign_ack")
            a, b = drivers["ch-a"], drivers["ch-b"]
            assert isinstance(a, HookedDriver) and a.assigns == []

            changed_b = channel("ch-b", monitor("m0", center_hz=260e6))
            await session.send(
                Assign(
                    config_version=9,
                    channels=[cha, changed_b, channel("ch-c"), channel("ch-d")],
                )
            )
            _, ack = await session.expect("assign_ack", 2)
            assert ack.config_version == 9 and ack.ok is False
            by_id = {entry.channel_id: entry for entry in ack.channels}
            assert list(by_id) == ["ch-a", "ch-b", "ch-c", "ch-d"]
            assert by_id["ch-a"].ok and by_id["ch-b"].ok
            assert not by_id["ch-c"].ok and "no such device" in by_id["ch-c"].error
            assert by_id["ch-c"].capabilities is None
            assert not by_id["ch-d"].ok and "timed out" in by_id["ch-d"].error
            # unchanged channel: kept, and told about the assign through its hook
            assert drivers.built["ch-a"] == [a] and a.closed == 0 and a.assigns == [cha]
            # changed channel: old driver closed, a new one opened on the new assignment
            assert b.closed == 1 and len(drivers.built["ch-b"]) == 2
            assert drivers["ch-b"].assignment == changed_b and drivers["ch-b"].closed == 0
            # failed channels: closed again, so nothing is left half open
            assert drivers["ch-c"].closed == 1 and drivers["ch-d"].closed == 1
            await wait_until(
                lambda: any(h.center_hz == 260e6 for h, _ in session.of("psd")), 2.0, "new window"
            )

            # a failed channel is tried again by the next assign; a hook that fails
            # takes its channel out of service
            drivers.prepare = lambda driver: None
            a.assign_error = RuntimeError("scenario fetch failed")
            await session.send(
                Assign(config_version=10, channels=[cha, changed_b, channel("ch-c")])
            )
            *_, ack = await session.expect("assign_ack", 3)
            by_id = {entry.channel_id: entry for entry in ack.channels}
            assert ack.config_version == 10 and not ack.ok
            assert by_id["ch-c"].ok and len(drivers.built["ch-c"]) == 2
            assert by_id["ch-b"].ok and len(drivers.built["ch-b"]) == 2
            assert not by_id["ch-a"].ok and "scenario fetch failed" in by_id["ch-a"].error
            assert a.closed == 1
        assert drivers["ch-b"].closed == 1 and drivers["ch-c"].closed == 1


async def test_a_driver_that_cannot_be_built_is_reported_in_the_ack(no_leaks: None) -> None:
    """The registry's own failures (unknown option, driver not installed) and a factory
    that raises reach the server as a failed channel; the node stays up."""

    def factory(assignment: ChannelAssignment) -> Any:
        if assignment.channel_id == "ch-bad":
            raise ValueError("options: driver 'noise' refused its options (bogus)")
        if assignment.channel_id == "ch-odd":
            return object()  # not a driver at all
        return FakeDriver(assignment.channel_id)

    channels = [channel("ch-bad"), channel("ch-odd"), channel("ch-ok")]
    async with FakeServer(channels) as server:
        async with running_agent(server, factory) as agent:
            session = await server.session()
            await agent.wait_online(2.0)
            (ack,) = await session.expect("assign_ack")
            by_id = {entry.channel_id: entry for entry in ack.channels}
            assert not ack.ok and by_id["ch-ok"].ok
            assert not by_id["ch-bad"].ok and "refused its options" in by_id["ch-bad"].error
            assert not by_id["ch-odd"].ok and "open" in by_id["ch-odd"].error
            await session.send(request("works", "ch-ok"))
            await session.capture("works")
            await session.send(request("fails", "ch-bad"))
            assert (await expect_error(session, "fails")).code == "driver"

    # the built-in registry path: an option the driver does not take
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, driver="noise", driver_options={"gain_db": 3}):
            session = await server.session()
            (ack,) = await session.expect("assign_ack")
            assert not ack.ok and "refused its options" in ack.channels[0].error


async def test_sim_update_is_forwarded_to_drivers_with_the_hook(no_leaks: None) -> None:
    built: dict[str, FakeDriver] = {}

    def factory(assignment: ChannelAssignment) -> FakeDriver:
        cls = HookedDriver if assignment.channel_id == "ch-sim" else FakeDriver
        built[assignment.channel_id] = cls(assignment.channel_id)
        return built[assignment.channel_id]

    async with FakeServer([channel("ch-sim"), channel("ch-real")]) as server:
        async with running_agent(server, factory):
            session = await server.session()
            await session.expect("assign_ack")
            effective = time.time_ns() + 2 * NS
            await session.send(SimUpdate(version=5, effective_ns=effective))
            await session.send(SimUpdate(version=6, effective_ns=effective + 1))
            hooked = built["ch-sim"]
            assert isinstance(hooked, HookedDriver)
            await wait_until(lambda: len(hooked.sim_updates) == 2, 2.0, "sim updates")
            assert hooked.sim_updates == [(5, effective), (6, effective + 1)]
            assert not hasattr(built["ch-real"], "sim_updates")
            await session.send(request("after", "ch-real"))  # the session is unharmed
            await session.capture("after")


# ================================================================ drops and restarts


def stale(session: Connection, capture_id: str) -> list[Any]:
    """Everything a session received about `capture_id`."""
    return [
        body
        for kind, body, _ in session.events
        if kind in ("capture_data", "capture_error") and body.capture_id == capture_id
    ]


async def test_server_drop_during_a_read_reconnects_without_stale_capture_data(
    no_leaks: None,
) -> None:
    drivers = Drivers()
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, drivers) as agent:
            first = await server.session()
            await first.expect("assign_ack")
            old = drivers["ch-a"]
            old.hold = asyncio.Event()
            await first.send(request("old-capture", "ch-a", 50_000))
            await asyncio.wait_for(old.read_started.wait(), 2.0)
            first.ws.transport.abort()  # the server vanishes mid-capture

            second = await server.session(1)
            await second.expect("assign_ack")
            assert second.hello is not None and first.hello is not None
            assert second.hello.node_id == first.hello.node_id  # resumed from hello
            # the capture in progress was cancelled and its driver closed
            assert old.cancelled_reads == 1 and old.closed == 1
            assert len(drivers.built["ch-a"]) == 2 and agent.stats.sessions == 2
            old.hold.set()  # whatever the old read does now, it is too late

            await second.send(request("new-capture", "ch-a", 50_000))
            await second.capture("new-capture")
            await asyncio.sleep(0.1)
            assert stale(second, "old-capture") == []
            assert {h.capture_id for h, _ in second.of("capture_data")} == {"new-capture"}
            assert agent.stats.captures_completed == 1


async def test_server_drop_during_an_upload_sends_nothing_of_it_afterwards(
    no_leaks: None,
) -> None:
    drivers = Drivers()
    async with FakeServer([channel("ch-a")], chunk_delay_s=0.02, **SMALL_PIPE) as server:
        async with running_agent(server, drivers, send_buffer_bytes=32 * 1024) as agent:
            first = await server.session()
            await first.expect("assign_ack")
            await first.send(request("old-capture", "ch-a", 1_000_003, deadline_s=30.0))
            await first.expect("capture_data", 2, timeout=5.0)
            total = -(-1_000_003 * 8 // DEFAULT_CHUNK_PAYLOAD_BYTES)
            assert 2 <= agent.stats.chunks_sent < total // 4  # the node is in the middle of it
            first.ws.transport.abort()  # a few chunks in, more than two hundred to go

            second = await server.session(1)
            await second.expect("assign_ack")
            await second.send(request("new-capture", "ch-a", 40_000))
            await second.capture("new-capture")
            await asyncio.sleep(0.1)
            assert stale(second, "old-capture") == []
            assert "old-capture" not in first.captures
            assert agent.stats.captures_completed == 1


async def test_a_driver_that_ignores_cancellation_cannot_leak_into_the_next_session(
    no_leaks: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The guarantee does not rest on drivers behaving: an old read that completes
    after the reconnect has nowhere to send."""
    drivers = Drivers()
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, drivers, teardown_timeout_s=0.1) as agent:
            first = await server.session()
            await first.expect("assign_ack")
            rogue = drivers["ch-a"]
            rogue.hold, rogue.ignore_cancel = asyncio.Event(), True
            await first.send(request("old-capture", "ch-a", 50_000))
            await asyncio.wait_for(rogue.read_started.wait(), 2.0)
            with caplog.at_level(logging.ERROR, logger="petal_locate.node.agent"):
                first.ws.transport.abort()
                second = await server.session(1)
            assert "ignored cancellation" in caplog.text  # reported, not silent
            await second.expect("assign_ack")
            assert rogue.cancelled_reads == 1 and FakeDriver.live_reads == 1

            rogue.hold.set()  # the old read now returns its samples, a session too late
            await wait_until(lambda: FakeDriver.live_reads == 0, 2.0, "old read to finish")
            await second.send(request("new-capture", "ch-a", 1000))
            await second.capture("new-capture")
            await asyncio.sleep(0.1)
            assert stale(second, "old-capture") == []
            assert agent.stats.chunks_sent == 1  # only the new capture's single chunk


# ================================================================ malformed messages


async def test_malformed_messages_get_an_error_and_three_in_a_row_end_the_session(
    no_leaks: None, caplog: pytest.LogCaptureFixture
) -> None:
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, Drivers()) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            bad = [
                "this is not json",
                '{"no_type": 1}',
                '{"type": "capture_request", "capture_id": "c", "n_samples": "many"}',
                '{"type": "psd"}',
            ]
            with caplog.at_level(logging.WARNING, logger="petal_locate.node.agent"):
                await session.ws.send(bad[0])
                await session.ws.send(bad[1])
                # a well-formed message in between resets the count, whatever it is
                await session.ws.send('{"type": "telemetry_v2", "x": 1}')
                await session.ws.send(bad[2])
                await session.ws.send(b"PLB1\x02\x00\x00\x00{}")  # binary: not for nodes
                await session.ws.send('{"type": "error", "code": "bad_message", "detail": "hm"}')
                await session.send(  # well-formed, but nothing a server sends: ignored
                    Reject(reason="bad_token")
                )
                await session.ws.send(bad[3])
                errors: list[ErrorMessage] = list(await session.expect("error", 5))
                await asyncio.sleep(0.05)
            assert [error.code for error in session.of("error")] == ["bad_message"] * 5
            assert "invalid JSON" in errors[0].detail and "type" in errors[1].detail
            assert "n_samples" in errors[2].detail and "many" not in errors[2].detail
            assert "binary frame" in errors[3].detail and "psd" in errors[4].detail
            assert "unexpected reject from the server ignored" in caplog.text
            assert "unknown type 'telemetry_v2' ignored" in caplog.text
            assert "server reported an error: bad_message: hm" in caplog.text
            assert agent.stats.bad_messages == 5 and agent.stats.sessions == 1
            assert not session.closed.is_set()  # never three in a row so far

            await session.ws.send('{"type": "telemetry_v2"}')  # start counting afresh
            for text in bad[:3]:
                await session.ws.send(text)
            await asyncio.wait_for(session.closed.wait(), 3.0)
            assert session.close_code == 1002
            assert len(session.of("error")) == 8  # the third one was still answered
            again = await server.session(1)  # and the node resumes from hello
            await again.expect("assign_ack")
            assert agent.stats.sessions == 2


async def test_garbage_instead_of_welcome_is_a_failed_handshake_that_backs_off(
    no_leaks: None,
) -> None:
    async def handler(ws: ServerConnection) -> None:
        attempts.append(time.monotonic())
        await ws.recv()
        await ws.send("definitely not a welcome")
        await ws.wait_closed()

    attempts: list[float] = []
    async with serve(handler, "127.0.0.1", 0, compression=None) as listener:
        port = listener.sockets[0].getsockname()[1]
        agent = NodeAgent(
            f"ws://127.0.0.1:{port}", SITE, TOKEN, node_id="n", proxy=None,
            backoff_base_s=0.03, backoff_max_s=0.12, backoff_jitter=0.0, close_timeout_s=0.2,
        )
        agent.start()
        await wait_until(lambda: len(attempts) >= 4, 3.0, "four attempts")
        await agent.stop(timeout_s=2.0)
    assert list(agent.stats.backoff_delays)[:3] == [0.03, 0.06, 0.12]
    assert agent.stats.sessions == 0


# ============================================================================= stop


async def test_stop_exits_promptly_and_leaves_nothing_running(no_leaks: None) -> None:
    window = monitor("m0", frame_rate_hz=50.0)
    drivers = Drivers()
    drivers.prepare = lambda driver: setattr(driver, "fast_psd", np.zeros(64, np.float32))
    async with FakeServer([channel("ch-a", window), channel("ch-b", window)]) as server:
        agent = NodeAgent(
            server.url, SITE, TOKEN, node_id="n", driver_factory=drivers, proxy=None,
            close_timeout_s=0.5,
        )
        task = agent.start()
        session = await server.session()
        await session.expect("assign_ack")
        await session.expect("psd", 4)
        drivers["ch-a"].hold = asyncio.Event()
        await session.send(request("in-flight", "ch-a", 1_000_000))
        await asyncio.wait_for(drivers["ch-a"].read_started.wait(), 2.0)
        await session.send(request("uploading", "ch-b", 1_000_000))
        await session.expect("capture_data", 1)

        started = time.monotonic()
        await agent.stop(timeout_s=2.0)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, elapsed  # typically a few milliseconds
        assert task.done() and task.exception() is None and agent.state == "stopped"
        await asyncio.wait_for(session.closed.wait(), 1.0)
        assert session.close_code == 1001  # "either side may close with 1001 for shutdown"
        assert drivers["ch-a"].closed == 1 and drivers["ch-b"].closed == 1
        assert drivers["ch-a"].cancelled_reads == 1 and FakeDriver.live_reads == 0
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("petal-node")]
        await agent.stop()  # idempotent
        with pytest.raises(RuntimeError, match="once"):
            await agent.run()
        await asyncio.sleep(0.1)
        assert len(server.connections) == 1  # and it stays stopped


async def test_stop_before_start_and_during_a_handshake_that_never_completes(
    no_leaks: None,
) -> None:
    idle = NodeAgent("ws://127.0.0.1:9", SITE, TOKEN, node_id="n", proxy=None)
    await asyncio.wait_for(idle.stop(), 0.5)  # never started: returns at once

    async def mute(ws: ServerConnection) -> None:  # accepts, then never answers hello
        await ws.wait_closed()

    async with serve(mute, "127.0.0.1", 0, compression=None) as listener:
        port = listener.sockets[0].getsockname()[1]
        agent = NodeAgent(f"ws://127.0.0.1:{port}", SITE, TOKEN, node_id="n", proxy=None)
        task = agent.start()
        await wait_until(lambda: agent.stats.connect_attempts == 1, 2.0, "dial")
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await agent.stop(timeout_s=2.0)
        assert time.monotonic() - started < 1.0 and task.done()


# ========================================================================== monitor


class ScriptedDriver:
    """A minimal driver for monitor tests: returns what the test puts in it."""

    capabilities = Capabilities(0.0, 1e12, 1e9, 0.0, 1)

    def __init__(self) -> None:
        self.fast: Any = None
        self.iq: Callable[[int, int, float, float], Any] | None = None
        self.reads: list[tuple[int, int, float, float]] = []
        self.psd_calls: list[tuple[int, float, float, int, int]] = []
        self.report: Callable[[Capture], Capture] = lambda capture: capture

    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        return None

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        self.reads.append((t_start_ns, n, fs, fc))
        assert self.iq is not None
        result = self.iq(t_start_ns, n, fs, fc)
        if isinstance(result, Exception):
            raise result
        return self.report(Capture(result, t_start_ns, fs, fc))

    async def psd(
        self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int
    ) -> Any:
        self.psd_calls.append((t_start_ns, fc, span_hz, nfft, navg))
        if isinstance(self.fast, Exception):
            raise self.fast
        return self.fast

    async def close(self) -> None:
        return None


def make_monitor(
    driver: Any, spec: MonitorAssignment, clock: ClockSource | None = None
) -> tuple[Monitor, list[SpectrumFrame]]:
    frames: list[SpectrumFrame] = []
    made = Monitor(
        "ch-a", spec, driver, clock=clock or ManualClock(), workers=WorkerPool(2),
        deliver=frames.append,
    )
    return made, frames


async def test_monitor_iq_path_reads_one_snippet_and_applies_welch() -> None:
    nfft, navg, span, center = 256, 4, 2e6, 437.5e6
    spec = monitor("m", center_hz=center, span_hz=span, nfft=nfft, navg=navg)
    driver = ScriptedDriver()
    tone_bin = 40  # a tone 40 bins above the centre: +312.5 kHz at this span
    k = np.arange(nfft * navg)
    driver.iq = lambda t, n, fs, fc: (
        10 * np.exp(2j * np.pi * (tone_bin * span / nfft) * k[:n] / fs)
    ).astype(np.complex64)
    made, _ = make_monitor(driver, spec)
    t0 = 1_800_000_000 * NS + 123
    frame = await made.frame_at(t0)
    # the driver was asked for nfft * navg samples at fs = span, centred on the window
    assert driver.psd_calls == [(t0, center, span, nfft, navg)]
    assert driver.reads == [(t0, nfft * navg, span, center)]
    assert (frame.channel_id, frame.monitor_id, frame.seq) == ("ch-a", "m", 0)
    assert (frame.t_ns, frame.center_hz, frame.span_hz, frame.nfft, frame.navg) == (
        t0, center, span, nfft, navg,
    )
    assert frame.psd_db.dtype == np.float32 and frame.psd_db.shape == (nfft,)
    # independent check of the spectrum: an on-bin tone of amplitude A through a Hann
    # window reads A^2 * (sum w)^2 / sum w^2 = A^2 * nfft / 1.5 in its bin, fft-shifted
    # so that a positive offset lies above the middle
    assert int(np.argmax(frame.psd_db)) == nfft // 2 + tone_bin
    expected_db = 10 * np.log10(100 * nfft / 1.5)
    assert frame.psd_db[nfft // 2 + tone_bin] == pytest.approx(expected_db, abs=0.01)
    assert frame.psd_db[nfft // 2 - tone_bin] < expected_db - 100
    # the wire frame carries exactly this
    header, psd = decode_psd(*decode_frame(frame.encode()))
    assert (header.t_ns, header.seq, header.center_hz) == (t0, 0, center)
    assert np.array_equal(psd, frame.psd_db)
    assert (await made.frame_at(t0 + 5)).seq == 1


async def test_monitor_reports_what_the_driver_actually_measured() -> None:
    spec = monitor("m", center_hz=100e6, span_hz=1e6, nfft=64, navg=2)
    driver = ScriptedDriver()
    driver.iq = lambda t, n, fs, fc: noise_samples(1, "x", t, n + 7)  # extra samples are fine
    driver.report = lambda c: Capture(c.iq, c.t_first_ns + 250, 999_999.0, 100e6 + 12.5)
    made, _ = make_monitor(driver, spec)
    frame = await made.frame_at(5 * NS)
    assert (frame.t_ns, frame.span_hz, frame.center_hz) == (5 * NS + 250, 999_999.0, 100e6 + 12.5)


async def test_monitor_fast_path_uses_the_driver_spectrum_without_reading() -> None:
    spec = monitor("m", nfft=64, navg=3)
    driver = ScriptedDriver()
    driver.fast = np.linspace(-3.0, 3.0, 64)  # float64 from the driver is converted
    made, _ = make_monitor(driver, spec)
    frame = await made.frame_at(9 * NS)
    assert driver.reads == []
    assert frame.psd_db.dtype == np.float32
    assert np.array_equal(frame.psd_db, np.linspace(-3.0, 3.0, 64).astype(np.float32))
    assert (frame.t_ns, frame.center_hz, frame.span_hz) == (9 * NS, spec.center_hz, spec.span_hz)


async def test_monitor_refuses_unusable_driver_output() -> None:
    spec = monitor("m", nfft=64, navg=2)
    driver = ScriptedDriver()
    made, _ = make_monitor(driver, spec)
    for fast in (np.zeros(63, np.float32), np.zeros((64, 1), np.float32), np.zeros(64, np.int32)):
        driver.fast = fast
        with pytest.raises(CaptureError, match="psd\\(\\) returned"):
            await made.frame_at(0)
    driver.fast = np.full(64, np.nan, np.float32)
    with pytest.raises(CaptureError, match="NaN"):
        await made.frame_at(0)
    driver.fast = None
    for bad in (
        lambda t, n, fs, fc: np.zeros(n - 1, np.complex64),  # short read
        lambda t, n, fs, fc: np.zeros(n, np.float32),  # not complex
    ):
        driver.iq = bad
        with pytest.raises(CaptureError, match="read\\(\\) returned"):
            await made.frame_at(0)
    driver.iq = lambda t, n, fs, fc: np.full(n, np.nan, np.complex64)
    with pytest.raises(CaptureError, match="NaN"):
        await made.frame_at(0)
    assert made.stats.frames == 0


async def test_monitor_loop_keeps_the_frame_rate_never_bursts_and_survives_failures(
    no_leaks: None, caplog: pytest.LogCaptureFixture
) -> None:
    spec = monitor("m", nfft=64, navg=1, frame_rate_hz=4.0)
    period = NS // 4
    clock = ManualClock(1_000 * NS)
    driver = ScriptedDriver()
    driver.fast = np.zeros(64, np.float32)
    made, frames = make_monitor(driver, spec, clock)
    assert made.period_ns == period
    task = asyncio.create_task(made.run())

    async def settle() -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    await settle()
    assert [f.t_ns for f in frames] == [1_000 * NS]  # the first frame at once
    for step in (1, 2, 3):
        clock.advance(period - 1)
        await settle()
        assert len(frames) == step  # not before its tick
        clock.advance(1)
        await settle()
        assert frames[-1].t_ns == 1_000 * NS + step * period and frames[-1].seq == step
    # a stall of several periods: one frame for *now*, not one per missed tick
    clock.advance(5 * period + 1234)
    await settle()
    assert len(frames) == 5 and frames[-1].t_ns == clock.now_ns() and made.stats.slipped == 1
    clock.advance(period)
    await settle()
    assert len(frames) == 6 and frames[-1].t_ns == clock.now_ns()

    # failures cost frames, are logged when they start and when they end, and the
    # loop goes on; even an exception that is not a CaptureError does not end it
    with caplog.at_level(logging.INFO, logger="petal_locate.node.monitor"):
        for error in (CaptureError("driver", "tuner unlocked"), KeyError("oops"), OSError("x")):
            driver.fast = error
            clock.advance(period)
            await settle()
        assert len(frames) == 6 and made.stats.failures == 3 and not task.done()
        assert made.stats.last_error == "OSError: x"
        driver.fast = np.ones(64, np.float32)
        clock.advance(period)
        await settle()
    assert len(frames) == 7 and frames[-1].seq == 6  # seq counts frames that exist
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "tuner unlocked" in warnings[0].getMessage()
    assert warnings[0].channel_id == "ch-a" and warnings[0].monitor_id == "m"  # type: ignore[attr-defined]
    assert any("recovered" in r.getMessage() for r in caplog.records)
    task.cancel()
    await asyncio.wait([task])
    assert task.cancelled()


def test_frame_mailbox_keeps_one_frame_per_window_newest_wins() -> None:
    def frame(monitor_id: str, seq: int, channel_id: str = "ch-a") -> SpectrumFrame:
        return SpectrumFrame(channel_id, monitor_id, seq, 1e6, 1e3, 4, 1, seq, np.zeros(4, np.float32))

    woken: list[int] = []
    box = FrameMailbox(on_ready=lambda: woken.append(1))
    assert box.take() is None and len(box) == 0
    assert box.offer(frame("a", 1)) is False
    assert box.offer(frame("b", 1)) is False
    assert box.offer(frame("a", 2)) is True  # replaced the unsent a/1
    assert box.offer(frame("a", 3)) is True
    assert box.offer(frame("c", 1, "ch-b")) is False
    assert len(box) == 3 and box.dropped == 2 and len(woken) == 5
    # served in the order the windows first waited; "a" kept its place and its newest
    taken = [box.take() for _ in range(3)]
    assert [(f.monitor_id, f.seq) for f in taken if f] == [("a", 3), ("b", 1), ("c", 1)]
    assert box.take() is None
    box.offer(frame("a", 4))
    box.offer(frame("c", 2, "ch-b"))
    assert box.discard_channel("ch-b") == 1 and len(box) == 1 and box.dropped == 3
    box.clear()
    assert len(box) == 0 and box.dropped == 4


async def test_read_gate_bounds_concurrency_and_lets_captures_go_first(no_leaks: None) -> None:
    gate = ReadGate(2)
    release = asyncio.Event()
    order: list[str] = []

    async def use(kind: str, name: str) -> None:
        admit = gate.capture() if kind == "capture" else gate.monitor()
        async with admit:
            order.append(name)
            await release.wait()

    tasks = [asyncio.create_task(use("monitor", f"m{i}")) for i in range(2)]
    await asyncio.sleep(0.01)
    assert gate.active == 2 and order == ["m0", "m1"]
    # the gate is full: a monitor queues first, then a capture arrives
    tasks.append(asyncio.create_task(use("monitor", "m2")))
    await asyncio.sleep(0.01)
    tasks.append(asyncio.create_task(use("capture", "c0")))
    tasks.append(asyncio.create_task(use("monitor", "m3")))
    await asyncio.sleep(0.01)
    assert gate.active == 2 and len(order) == 2
    release.set()
    await asyncio.gather(*tasks)
    # m2 was already queued for a slot, so it may run; m3 arrived after the capture
    # started waiting and must come after it
    assert order.index("c0") < order.index("m3")
    assert gate.peak == 2 and gate.active == 0
    with pytest.raises(ValueError, match="limit"):
        ReadGate(0)

    # a capture whose wait is cancelled gives its place up again
    single = ReadGate(1)
    async with single.monitor():
        waiting = asyncio.create_task(use_capture(single))
        await asyncio.sleep(0.01)
        waiting.cancel()
        await asyncio.wait([waiting])
    async with asyncio.timeout(1.0):
        async with single.monitor():  # not blocked by the cancelled capture
            pass


async def use_capture(gate: ReadGate) -> None:
    async with gate.capture():
        pass


# ===================================================== found when node met hub (e2e)


async def test_chunks_carry_32_kib_by_default_and_the_size_is_an_option(no_leaks: None) -> None:
    """Small chunks keep a slow uplink alive: the server counts only whole frames."""
    assert DEFAULT_CHUNK_PAYLOAD_BYTES == 32 * 1024 <= MAX_CHUNK_PAYLOAD_BYTES
    n = 10_000
    drivers = Drivers()
    async with FakeServer([channel("ch-a")]) as server:
        async with running_agent(server, drivers) as agent:
            session = await server.session()
            await session.expect("assign_ack")
            ask = request("default", "ch-a", n)
            await session.send(ask)
            done = await session.capture("default")
            assert done.iq.tobytes() == drivers["ch-a"].returned[ask.t_start_ns].tobytes()
            sizes = [header.n for header, _ in session.of("capture_data")]
            assert sizes == [4096, 4096, 1808] and agent.stats.chunks_sent == 3
        async with running_agent(server, Drivers(), chunk_payload_bytes=8 * 1000) as agent:
            session = await server.session(1)
            await session.expect("assign_ack")
            await session.send(request("small", "ch-a", n))
            done = await session.capture("small")
            assert done.n_chunks == 10
            assert {header.n for header, _ in session.of("capture_data")} == {1000}
    for bad in (0, 7, MAX_CHUNK_PAYLOAD_BYTES + 1, True, 4096.0):
        with pytest.raises(ValueError, match="chunk_payload_bytes"):
            NodeAgent("ws://127.0.0.1:9", SITE, TOKEN, node_id="n", chunk_payload_bytes=bad)


class RecordingSocket:
    """Stands in for the WebSocket under `_Outbox.run`: remembers what was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.after: Callable[[str, Any], None] | None = None

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            entry: tuple[str, Any] = ("control", json.loads(data)["type"])
        else:
            header, _ = decode_frame(data)
            entry = (header["type"], header.get("monitor_id", header.get("seq")))
        self.sent.append(entry)
        await asyncio.sleep(0)  # a send that suspends, as on a link that pushes back
        if self.after is not None:
            self.after(*entry)


def spectrum(monitor_id: str, seq: int = 0) -> SpectrumFrame:
    return SpectrumFrame("ch-a", monitor_id, 1_800_000_000 * NS, 250e6, 1e6, 8, 1, seq,
                         np.zeros(8, np.float32))


async def test_outbox_sends_every_waiting_window_before_each_chunk(no_leaks: None) -> None:
    """On a slow uplink a chunk takes a while. Each pass of the writer must carry the
    waiting frame of every window, not one frame: with one frame per chunk, each of W
    windows would be heard from only every W chunks."""
    windows = ["m0", "m1", "m2", "m3"]
    outbox = _Outbox(AgentStats())
    ws = RecordingSocket()
    produced = itertools.count(1)

    def monitors_tick(kind: str, _: Any) -> None:
        if kind == "capture_data":  # while a chunk was on the wire every window produced
            seq = next(produced)
            for name in windows:
                outbox.offer_frame(spectrum(name, seq))

    ws.after = monitors_tick
    clock = ClockStatus(source="ntp", locked=True, est_offset_ns=0.0, holdover_s=0.0)
    iq = fake_iq("ch-a", 0, 30)
    t_ns = 1_800_000_000 * NS
    chunks = iter_capture_chunks("cap", "ch-a", iq, t_ns, 1e6, 250e6, False, clock, 80)
    for name in windows:
        outbox.offer_frame(spectrum(name))
    writer = asyncio.create_task(outbox.run(ws))
    assert await asyncio.wait_for(outbox.upload("cap", chunks), 2.0) == 3
    outbox.close()
    await asyncio.wait_for(writer, 2.0)
    frames = [("psd", name) for name in windows]
    assert ws.sent == [
        *frames, ("capture_data", 0),
        *frames, ("capture_data", 1),
        *frames, ("capture_data", 2),
        *frames,
    ]


async def test_outbox_lets_a_control_message_pass_the_remaining_frames(no_leaks: None) -> None:
    outbox = _Outbox(AgentStats())
    ws = RecordingSocket()
    posted = False

    def heartbeat_due(kind: str, _: Any) -> None:
        nonlocal posted
        if kind == "psd" and not posted:  # becomes due while the first frame is sent
            posted = True
            outbox.post(CaptureCancel(capture_id="any-control-message"))

    ws.after = heartbeat_due
    for name in ("m0", "m1", "m2"):
        outbox.offer_frame(spectrum(name))
    writer = asyncio.create_task(outbox.run(ws))
    await wait_until(lambda: len(ws.sent) == 4, 2.0, "everything sent")
    outbox.close()
    await asyncio.wait_for(writer, 2.0)
    assert ws.sent == [("psd", "m0"), ("control", "capture_cancel"), ("psd", "m1"), ("psd", "m2")]


async def test_unknown_message_before_the_welcome_is_ignored(
    no_leaks: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Forward compatibility holds during the handshake too (PROTOCOL.md: unknown
    types are ignored with a warning)."""

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "motd", "text": "from a newer server"}))
        await ws.send(
            dump_control(Welcome(server_t_ns=time.time_ns(), config_version=1, channels=[]))
        )
        with contextlib.suppress(ConnectionClosed):
            async for _ in ws:
                pass

    async with serve(handler, "127.0.0.1", 0, compression=None) as listener:
        port = listener.sockets[0].getsockname()[1]
        agent = NodeAgent(
            f"ws://127.0.0.1:{port}", SITE, TOKEN, node_id="n", proxy=None, close_timeout_s=0.2
        )
        agent.start()
        with caplog.at_level(logging.WARNING, logger="petal_locate.node.agent"):
            await agent.wait_online(3.0)
        assert agent.stats.sessions == 1 and agent.stats.connect_attempts == 1
        assert "unknown type 'motd' ignored during the handshake" in caplog.text
        await agent.stop(timeout_s=2.0)


async def test_session_end_during_an_assign_still_closes_the_removed_channels_driver(
    no_leaks: None,
) -> None:
    """Disconnect while an assign is being applied: the channel it removed is no
    longer among the session's channels, and the task that was closing it is
    cancelled. Its driver must be closed all the same, or the radio stays claimed and
    the next session cannot open it."""
    cha, chb = channel("ch-a"), channel("ch-b")
    drivers = Drivers()
    async with FakeServer([cha, chb]) as server:
        async with running_agent(server, drivers, teardown_timeout_s=0.5) as agent:
            first = await server.session()
            await first.expect("assign_ack")
            b = drivers["ch-b"]
            b.hold, b.ignore_cancel = asyncio.Event(), True  # a read that will not die
            await first.send(request("on-b", "ch-b"))
            await asyncio.wait_for(b.read_started.wait(), 2.0)
            await first.send(Assign(config_version=8, channels=[cha]))
            # the assign is now waiting for ch-b's capture to end before closing ch-b
            await wait_until(lambda: b.cancelled_reads == 1, 2.0, "the cancel to reach the read")
            assert b.closed == 0
            first.ws.transport.abort()
            second = await server.session(1)
            await second.expect("assign_ack")
            assert b.closed == 1, "the removed channel's driver was left open"
            assert all(d.closed == 1 for d in drivers.built["ch-a"][:1])
            b.hold.set()  # let the rogue read go
            await wait_until(lambda: FakeDriver.live_reads == 0, 2.0, "the read to finish")
        assert agent.stats.sessions == 2
        # every driver ever built was closed exactly once
        assert [d.closed for ds in drivers.built.values() for d in ds] == [1] * 4


async def test_a_burst_of_assigns_is_applied_newest_only_and_cannot_queue_up(
    no_leaks: None,
) -> None:
    """Assignments are applied one at a time and a driver may take long to open. The
    ones that arrive meanwhile do not queue: only the newest is applied and
    acknowledged, which is the only acknowledgement the server counts anyway."""
    cha = channel("ch-a")
    drivers = Drivers()
    drivers.prepare = lambda driver: setattr(driver, "open_delay_s", 0.25)
    async with FakeServer([cha]) as server:
        async with running_agent(server, drivers) as agent:
            session = await server.session()
            # the welcome's assignment (version 7) is being applied: ch-a opens slowly
            burst = [
                [cha, channel("ch-b")],
                [cha, channel("ch-c")],
                [channel("ch-c"), channel("ch-d")],
                [cha, channel("ch-e", monitor("m9"))],
            ]
            for version, channels in enumerate(burst, start=8):
                await session.send(Assign(config_version=version, channels=channels))
            acks = await session.expect("assign_ack", 2, timeout=4.0)
            await asyncio.sleep(0.3)
            assert [ack.config_version for ack in session.of("assign_ack")] == [7, 11]
            assert [entry.channel_id for entry in acks[1].channels] == ["ch-a", "ch-e"]
            assert acks[1].ok
            # nothing was opened for the versions in between, and ch-a never reopened
            assert sorted(drivers.built) == ["ch-a", "ch-e"]
            assert len(drivers.built["ch-a"]) == 1 and drivers["ch-a"].closed == 0
            assert agent.stats.sessions == 1 and agent.stats.bad_messages == 0
            await session.expect("psd", 2)
            assert {h.channel_id for h, _ in session.of("psd")} <= {"ch-a", "ch-e"}


# ------------------------------------------------- driver text and per-channel options (review)


def test_driver_text_that_utf8_cannot_carry_is_escaped_before_it_is_sent() -> None:
    """A driver error naming a non-UTF-8 file name (`os.fsdecode` makes lone surrogates of
    it) used to make `dump_control` raise inside `_fail_capture`: the capture_error was
    never sent and the server waited for the deadline (review SRV-2)."""
    import os

    from petal_locate.node.agent import _clip

    bad = f"cannot open {os.fsdecode(b'/dev/sdr' + bytes([0xE9]))}"
    with pytest.raises(ValueError):  # what used to happen
        dump_control(CaptureErrorMessage(capture_id="c1", channel_id="a", code="driver", message=bad))
    safe = _clip(bad)
    assert safe == "cannot open /dev/sdr\\udce9"
    text = dump_control(CaptureErrorMessage(capture_id="c1", channel_id="a", code="driver", message=safe))
    assert parse_control(text).message == safe  # and the server accepts it
    assert _clip("x" * 1000).endswith("...") and len(_clip("x" * 1000)) == 400
    agent = NodeAgent("ws://127.0.0.1:9", SITE, TOKEN, node_id="n1", software=f"build {bad}")
    assert agent._software == "build cannot open /dev/sdr\\udce9"


def test_channel_driver_options_reach_only_their_channel() -> None:
    """One radio per channel: each channel's driver gets the common options plus its own
    (review K12)."""
    built: list[dict[str, Any]] = []

    def fake_get_driver(name: str, **options: Any) -> Any:
        built.append({"name": name, **options})
        return object()

    import petal_locate.node.agent as agent_module

    original = agent_module.get_driver
    agent_module.get_driver = fake_get_driver  # type: ignore[assignment]
    try:
        agent = NodeAgent("ws://127.0.0.1:9", SITE, TOKEN, node_id="n1", driver="soapy",
                          driver_options={"device": "driver=uhd", "lookback_s": 5},
                          channel_driver_options={"c2": {"device": "driver=uhd,serial=B2"}})
        agent.driver_factory(channel("c1"))
        agent.driver_factory(channel("c2"))
    finally:
        agent_module.get_driver = original  # type: ignore[assignment]
    assert built == [
        {"name": "soapy", "device": "driver=uhd", "lookback_s": 5},
        {"name": "soapy", "device": "driver=uhd,serial=B2", "lookback_s": 5},
    ]
    with pytest.raises(ValueError, match="channel_driver_options"):
        NodeAgent("ws://127.0.0.1:9", SITE, TOKEN, node_id="n1", channel_driver_options={"c1": 3})  # type: ignore[dict-item]


def test_system_clock_offset_shifts_labelled_time_only_when_asked() -> None:
    """The fixed simulated epoch of `petal-locate demo --start` (review K8): a node clock
    with an offset labels time host clock + offset; the default is the host clock."""
    plain = SystemClock()
    assert plain.offset_ns == 0 and abs(plain.now_ns() - time.time_ns()) < 50_000_000
    shifted = SystemClock(source="sim", offset_ns=-86_400 * NS)
    assert shifted.offset_ns == -86_400 * NS
    assert abs(shifted.now_ns() - (time.time_ns() - 86_400 * NS)) < 50_000_000
    assert shifted.status().source == "sim"
    with pytest.raises(ValueError, match="offset_ns"):
        SystemClock(offset_ns=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SystemClock(offset_ns=-time.time_ns() - NS)  # before 1970


# --------------------------------------------------------------------------- closing S1


def test_two_versions_announced_a_moment_apart_keep_the_worlds_still_needed() -> None:
    """Closing S1: two presets loaded about 100 ms apart announce two versions at once.
    Installing both used to drop the world still in force (at most two are kept), so
    every monitor frame and capture until the first of them took effect failed with
    `too_late`, one WARNING per monitor of every channel. A world in force within
    `keep_ns` of the newest time read now stays until its successor has served that
    long; the timeline is back to two worlds then, and never holds more than
    `MAX_KEPT_WORLDS`."""
    from petal_locate.node.drivers.sim import MAX_KEPT_WORLDS, WorldTimeline, WorldVersion

    def entry(version: int, effective_ns: int) -> WorldVersion:
        return WorldVersion(version, effective_ns, f"world-{version}")  # type: ignore[arg-type]

    timeline = WorldTimeline(max_worlds=2, keep_ns=30)
    timeline.install(entry(1, 100))
    timeline.note_read(150)  # a monitor frame now
    timeline.install(entry(2, 200))
    timeline.install(entry(3, 210))  # 10 units later
    assert [h.version for h in timeline.held] == [1, 2, 3] and timeline.floor_ns is None
    assert timeline.lookup(160).world.version == 1  # type: ignore[union-attr]
    timeline.note_read(215)  # version 3 serves now; 185 (the lookback) still needs version 1
    assert [h.version for h in timeline.held] == [1, 2, 3]
    assert timeline.lookup(190).world.version == 1  # type: ignore[union-attr]
    assert timeline.lookup(205).world.version == 2  # type: ignore[union-attr]
    timeline.note_read(231)  # the lookback starts after version 2 took effect: version 1 goes
    assert [h.version for h in timeline.held] == [2, 3] and timeline.floor_ns == 200
    assert timeline.lookup(160).kind == "gone"
    # without a read the rule is the old one (nothing says version 1 is in use)
    bare = WorldTimeline(max_worlds=2, keep_ns=30)
    for version, effective in ((1, 100), (2, 200), (3, 210)):
        bare.install(entry(version, effective))
    assert [h.version for h in bare.held] == [2, 3]
    # a long lookback never holds more than MAX_KEPT_WORLDS
    busy = WorldTimeline(max_worlds=2, keep_ns=10**6)
    busy.note_read(0)
    for version in range(1, 20):
        busy.install(entry(version, 100 + version))
    assert len(busy.held) == MAX_KEPT_WORLDS and busy.held[-1].version == 19


async def test_a_source_serves_the_lookback_while_two_new_versions_take_over(caplog: pytest.LogCaptureFixture) -> None:
    """Closing S1, through `SimWorldSource` and real worlds: version 1 in force, versions 2
    and 3 announced 100 ms apart and both fetched. Intervals before version 2 takes
    effect are still served from version 1 (they were refused with `too_late`), also by
    a capture asked for after version 3 serves, and no warning is logged; once version
    2 has served for the lookback, version 1 is dropped as before."""
    import httpx

    from petal_locate.node.drivers.sim import DEFAULT_LOOKBACK_S, SimWorldSource, backplane_url
    from test_sim_driver import PATH, T_NOW, document, scenario  # tests/ is on sys.path

    caplog.set_level(logging.WARNING)
    current: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=current["doc"], headers={"content-type": "application/json"})

    effective = {1: T_NOW - 100 * NS, 2: T_NOW + 2 * NS, 3: T_NOW + 2 * NS + 100_000_000}
    worlds = {1: scenario(), 2: scenario(eirp_dbw=26.0), 3: scenario(tone=True)}
    current["doc"] = document(1, effective[1], worlds[1])
    source = SimWorldSource(backplane_url("http://backplane.test", PATH), "t", proxy=None,
                            transport=httpx.MockTransport(handler))
    try:
        newest, problem = await source.refresh(10.0)
        assert newest.version == 1 and problem is None
        assert (await source.world_at(T_NOW - NS, 1.0)).world.version == 1  # type: ignore[union-attr]
        for version in (2, 3):
            current["doc"] = document(version, effective[version], worlds[version])
            assert await source.announce(version, effective[version], 10.0)
        assert [v.version for v in source.versions] == [1, 2, 3]
        for t_ns, version in ((T_NOW, 1), (effective[2] + 50_000_000, 2), (effective[3] + NS, 3),
                              (effective[2] - 160_000_000, 1)):  # a capture just before the change
            assert (await source.world_at(t_ns, 1.0)).world.version == version  # type: ignore[union-attr]
        assert [v.version for v in source.versions] == [1, 2, 3]
        later = effective[2] + round(DEFAULT_LOOKBACK_S * NS) + NS
        assert (await source.world_at(later, 1.0)).world.version == 3  # type: ignore[union-attr]
        assert [v.version for v in source.versions] == [2, 3]
        with pytest.raises(CaptureError) as info:
            await source.world_at(T_NOW, 1.0)  # version 1's time, now beyond the lookback
        assert info.value.code == "too_late"
    finally:
        await source.aclose()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
