"""The simulated radio driver (node/drivers/sim.py): ARCHITECTURE.md section 6 and the
simulation backplane of PROTOCOL.md section A.

The backplane is a real HTTP server: Starlette on uvicorn, in a thread with its own
event loop, on an ephemeral loopback port, serving the world document behind bearer
authentication. The end-to-end test adds the node WebSocket to the same origin and runs
the real `NodeAgent` with driver "sim" against it.

Expected samples and spectra come from `SimWorld` objects built here from the same
configuration and scenario, independently of the driver's fetch, parse and rebuild
path, and are compared bit for bit. Time is a `ManualClock` wherever waiting matters.
The scenarios are simulated; their emitters sit at fictional open-ocean points.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from petal_locate.config.loader import load_config
from petal_locate.config.models import SystemConfig
from petal_locate.node.agent import NodeAgent
from petal_locate.node.clock import ManualClock
from petal_locate.node.drivers import get_driver
from petal_locate.node.drivers.base import CaptureError, DriverContext
from petal_locate.node.drivers.sim import (
    BackplaneError,
    SimDriver,
    SimWorldSource,
    WorldTimeline,
    WorldVersion,
    active_sources,
    backplane_url,
    channel_capabilities,
    parse_backplane_document,
    sim_clock,
)
from petal_locate.protocol import (
    MAX_CAPTURE_SAMPLES,
    AssembledCapture,
    CaptureAssembler,
    CaptureDataHeader,
    ChannelAssignment,
    ClockStatus,
    MonitorAssignment,
    decode_frame,
    decode_psd,
)
from petal_locate.sim import Scenario, SimWorld

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "pacific-uhf.yaml"
NS = 1_000_000_000
TOKEN = "sim-node-token-1"
PATH = "/node/v1/sim"
SITE = "tokyo"
T_NOW = 1_794_000_000 * NS + 123_456_789  # node clock in the ManualClock tests (Nov 2026)
FS = 31250.0
FC = 260.0e6  # downlink of shared-1 (301.000 MHz - 41.0 MHz) on muos-pac and muos-io
F_J = 301.005e6  # jammer uplink frequency, inside shared-1
LOG = "petal_locate.node.drivers.sim"

CFG: SystemConfig = load_config(CONFIG_PATH)


# --------------------------------------------------------------------------- scenarios


def scenario(*, eirp_dbw: float = 20.0, tone: bool = False, seed: int = 7) -> Scenario:
    """A simulated world: one noise jammer at a fictional open-ocean point (Philippine
    Sea), optionally a CW tone from another open-ocean point."""
    emitters: list[dict[str, Any]] = [
        dict(
            id="jammer-a",
            name="Uplink noise jammer (simulated, open ocean)",
            kind="jammer",
            lat_deg=20.0,
            lon_deg=132.0,
            freq_hz=F_J,
            eirp_dbw=eirp_dbw,
            waveform={"type": "noise", "bandwidth_hz": 10e3, "seed": 0},
        )
    ]
    if tone:
        emitters.append(
            dict(
                id="tone-b",
                name="CW tone (simulated, open ocean)",
                kind="jammer",
                lat_deg=25.0,
                lon_deg=140.0,
                freq_hz=301.008e6,
                eirp_dbw=15.0,
                waveform={"type": "cw"},
            )
        )
    return Scenario.model_validate(
        {
            "name": "sim-driver-test",
            "seed": seed,
            "description": "Simulated scenario (not real data) for the sim driver tests; "
            "fictional open-ocean emitters.",
            "emitters": emitters,
        }
    )


def config_with(channel_id: str, *, remove: bool = False, **changes: Any) -> SystemConfig:
    """The example configuration with one channel removed or changed."""
    data = CFG.export_dict()
    for site in data["sites"]:
        kept = []
        for channel in site["channels"]:
            if channel["id"] == channel_id:
                if remove:
                    continue
                channel.update(changes)
            kept.append(channel)
        site["channels"] = kept
    return SystemConfig.model_validate(data)


def document(
    version: int, effective_ns: int, world_scenario: Scenario, config: SystemConfig | None = None
) -> bytes:
    """A backplane document exactly as PROTOCOL.md shapes it."""
    return json.dumps(
        {
            "version": version,
            "effective_ns": str(effective_ns),
            "config": (config or CFG).export_dict(),
            "scenario": world_scenario.model_dump(mode="json"),
        }
    ).encode()


def assignment(channel_id: str, *, nfft: int = 256, navg: int = 4, rate: float = 4.0) -> ChannelAssignment:
    return ChannelAssignment(
        channel_id=channel_id,
        antenna_summary="simulated",
        monitors=[
            MonitorAssignment(
                monitor_id="m0", center_hz=FC, span_hz=FS, nfft=nfft, navg=navg, frame_rate_hz=rate
            )
        ],
    )


# ---------------------------------------------------------------------- the backplane


class FakeBackplane:
    """The server side of the simulation backplane, plus a scripted node WebSocket.

    Runs Starlette on uvicorn in a thread with its own event loop. The test thread
    sets `document`, `fail_remaining`, `channels` and `capture_plan`, and reads
    `requests`, `events` and `captures` (appends from the server thread are atomic).
    """

    def __init__(self) -> None:
        self.token = TOKEN
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.reset()

    def reset(self) -> None:
        self.document: bytes | None = None
        self.requests: list[tuple[float, str | None]] = []
        self.fail_remaining = 0
        self.fail_status = 503
        self.channels: list[dict[str, Any]] = []
        self.capture_plan: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any, Any]] = []
        self.captures: dict[str, AssembledCapture] = {}
        self._assemblers: dict[str, CaptureAssembler] = {}
        self._ws: WebSocket | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_lock: asyncio.Lock | None = None

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    @property
    def authorizations(self) -> list[str | None]:
        return [auth for _, auth in self.requests]

    def publish(
        self, version: int, effective_ns: int, world_scenario: Scenario, config: SystemConfig | None = None
    ) -> None:
        self.document = document(version, effective_ns, world_scenario, config)

    def of(self, kind: str) -> list[Any]:
        return [body for k, body, _ in self.events if k == kind]

    def psd_frames(self) -> list[tuple[Any, np.ndarray]]:
        return [(header, psd) for k, header, psd in self.events if k == "psd"]

    # ------------------------------------------------------------------ server thread

    async def _backplane(self, request: Request) -> Response:
        auth = request.headers.get("authorization")
        self.requests.append((time.monotonic(), auth))
        if auth != f"Bearer {self.token}":
            return JSONResponse({"detail": "unknown token"}, status_code=401)
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            return JSONResponse({"detail": "temporarily unavailable"}, status_code=self.fail_status)
        if self.document is None:
            return JSONResponse({"detail": "not simulating"}, status_code=404)
        return Response(self.document, media_type="application/json")

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._ws is not None and self._ws_lock is not None
        async with self._ws_lock:
            await self._ws.send_text(json.dumps(message))

    async def _node(self, websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            hello = json.loads(await websocket.receive_text())
            self.events.append(("hello", hello, None))
            if hello.get("token") != self.token:
                await websocket.send_text(json.dumps({"type": "reject", "reason": "bad_token"}))
                await websocket.close(1008)
                return
            self._ws, self._ws_loop, self._ws_lock = websocket, asyncio.get_running_loop(), asyncio.Lock()
            await self._send(
                {
                    "type": "welcome",
                    "protocol": 1,
                    "server_t_ns": time.time_ns(),
                    "heartbeat_s": 0.5,
                    "config_version": 1,
                    "channels": self.channels,
                    "simulation": {"enabled": True, "scenario_url": PATH, "version": 1},
                }
            )
            asked = False
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    obj = json.loads(message["text"])
                    self.events.append((obj["type"], obj, None))
                    if obj["type"] == "heartbeat":
                        await self._send(
                            {"type": "heartbeat_ack", "seq": obj["seq"], "server_t_ns": time.time_ns()}
                        )
                    elif obj["type"] == "assign_ack" and obj["ok"] and not asked:
                        asked = True
                        for plan in self.capture_plan:
                            start = time.time_ns() + int(plan["lead_s"] * NS)
                            request = {
                                "type": "capture_request",
                                "capture_id": plan["capture_id"],
                                "channel_id": plan["channel_id"],
                                "t_start_ns": start,
                                "n_samples": plan["n"],
                                "sample_rate_hz": plan["fs"],
                                "center_hz": plan["fc"],
                                "deadline_ns": start + 10 * NS,
                            }
                            self.events.append(("capture_request", request, None))
                            await self._send(request)
                    continue
                data = message.get("bytes")
                if data is None:
                    continue
                header, payload = decode_frame(data)
                if header["type"] == "psd":
                    psd_header, psd = decode_psd(header, payload)
                    self.events.append(("psd", psd_header, psd.copy()))
                elif header["type"] == "capture_data":
                    chunk = CaptureDataHeader.from_wire(header)
                    assembler = self._assemblers.setdefault(
                        chunk.capture_id, CaptureAssembler(chunk.capture_id)
                    )
                    done = assembler.add(chunk, payload)
                    if done is not None:
                        self.captures[chunk.capture_id] = done
        except WebSocketDisconnect:
            return
        finally:
            self._ws = None

    def send_ws(self, message: dict[str, Any]) -> None:
        """Send a control message to the connected node (from the test thread)."""
        loop = self._ws_loop
        assert loop is not None and self._ws is not None, "no node connected"
        asyncio.run_coroutine_threadsafe(self._send(message), loop).result(timeout=3)

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        app = Starlette(
            routes=[Route(PATH, self._backplane), WebSocketRoute("/ws/node", self._node)]
        )
        config = uvicorn.Config(
            app,
            log_level=None,
            log_config=None,
            access_log=False,
            lifespan="off",
            ws_per_message_deflate=False,
            ws_max_size=16 * 1024 * 1024,
            timeout_graceful_shutdown=1,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [sock]}, daemon=True, name="fake-backplane"
        )
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the fake backplane did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        if self._server is not None and self._thread is not None:
            self._server.should_exit = True
            self._thread.join(timeout=10.0)


@pytest.fixture(scope="module")
def backplane_server() -> Iterator[FakeBackplane]:
    server = FakeBackplane()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def backplane(backplane_server: FakeBackplane) -> FakeBackplane:
    backplane_server.reset()
    return backplane_server


@pytest.fixture
async def no_leftovers() -> AsyncIterator[None]:
    """Every shared world source of the test's event loop is released at its end."""
    yield
    assert active_sources() == []


# ------------------------------------------------------------------------- helpers


def context(
    server: FakeBackplane,
    *,
    clock: Any = None,
    token: str = TOKEN,
    site: str = SITE,
    simulation: dict[str, Any] | None | str = "default",
) -> DriverContext:
    sim = {"enabled": True, "scenario_url": PATH, "version": 1} if simulation == "default" else simulation
    return DriverContext(
        site_id=site,
        server_http_url=server.http_url,
        token=token,
        simulation=cast("dict[str, Any] | None", sim),
        clock=clock,
    )


def make_driver(**options: Any) -> SimDriver:
    settings: dict[str, Any] = dict(
        proxy=None,
        retry_base_s=0.05,
        retry_max_s=0.4,
        open_timeout_s=5.0,
        refresh_timeout_s=3.0,
        world_wait_s=3.0,
        fetch_timeout_s=5.0,
    )
    settings.update(options)
    return SimDriver(**settings)


@contextlib.asynccontextmanager
async def open_driver(
    server: FakeBackplane, channel_id: str = "tokyo-pac", *, clock: Any = None, **options: Any
) -> AsyncIterator[SimDriver]:
    driver = make_driver(**options)
    try:
        await driver.open(assignment(channel_id), context(server, clock=clock))
        yield driver
    finally:
        await driver.close()


async def wait_until(predicate: Callable[[], Any], timeout: float = 5.0, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        await asyncio.sleep(0.005)


def world(world_scenario: Scenario, effective_ns: int, config: SystemConfig | None = None) -> SimWorld:
    """The reference world, built here rather than by the driver."""
    return SimWorld(config or CFG, world_scenario, epoch_ns=effective_ns)


def entry(version: int, effective_ns: int) -> WorldVersion:
    """A timeline entry with a stand-in world (the timeline never looks inside it)."""
    return WorldVersion(version, effective_ns, cast(SimWorld, f"world-{version}"))


# ====================================================================== pure pieces


def test_backplane_document_parses_to_typed_models() -> None:
    sc = scenario()
    parsed = parse_backplane_document(document(3, T_NOW, sc))
    assert parsed.version == 3
    assert parsed.effective_ns == T_NOW
    assert parsed.scenario == sc
    assert parsed.config == CFG  # derived monitors come back derived


def _broken(change: Callable[[dict[str, Any]], None]) -> bytes:
    obj = json.loads(document(3, T_NOW, scenario()))
    change(obj)
    return json.dumps(obj).encode()


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (_broken(lambda d: d.update(version="3")), "version"),
        (_broken(lambda d: d.update(version=True)), "version"),
        (_broken(lambda d: d.update(version=-1)), "version"),
        (_broken(lambda d: d.update(effective_ns=T_NOW)), "effective_ns"),
        (_broken(lambda d: d.update(effective_ns="1.794e18")), "effective_ns"),
        (_broken(lambda d: d.pop("effective_ns")), "effective_ns"),
        (_broken(lambda d: d.update(effective_ns=str(2**63))), "effective_ns"),
        (_broken(lambda d: d.pop("config")), "config"),
        (_broken(lambda d: d["config"]["sites"][0].update(lat_deg=95.0)), "config.sites.0.lat_deg"),
        (_broken(lambda d: d["scenario"]["emitters"][0].pop("lat_deg")), "scenario.emitters.0"),
        (
            _broken(lambda d: d["scenario"]["errors"].update(sat_osc_frac={"no-such-sat": 1e-7})),
            "scenario.errors.sat_osc_frac",
        ),
        (b"{not json", "document"),
        (b"[1, 2]", "document"),
    ],
)
def test_backplane_document_errors_name_the_field(body: bytes, field: str) -> None:
    with pytest.raises(ValueError, match=f"^{field.replace('.', '[.]')}"):
        parse_backplane_document(body)


def test_backplane_url_joins_origin_and_path_and_refuses_anything_else() -> None:
    assert backplane_url("http://127.0.0.1:8080", PATH) == "http://127.0.0.1:8080/node/v1/sim"
    assert backplane_url("https://hq.example:8443/", "/x?y=1") == "https://hq.example:8443/x?y=1"
    with pytest.raises(ValueError, match="scenario_url"):
        backplane_url("http://hq", "http://elsewhere/node/v1/sim")
    with pytest.raises(ValueError, match="scenario_url"):
        backplane_url("http://hq", "//elsewhere/x")
    with pytest.raises(ValueError, match="scenario_url"):
        backplane_url("http://hq", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="server_http_url"):
        backplane_url("ws://hq", PATH)


def test_timeline_switches_at_effective_time_and_keeps_two_worlds() -> None:
    timeline = WorldTimeline(max_worlds=2)
    assert timeline.lookup(0).kind == "none" and timeline.needs_fetch()

    assert timeline.install(entry(1, 100))
    early = timeline.lookup(50)  # before the first world: nothing older exists
    assert (early.kind, early.world.version, early.before_first) == ("world", 1, True)  # type: ignore[union-attr]
    assert timeline.lookup(150).next_effective_ns is None

    assert timeline.announce(2, 200) and timeline.needs_fetch()
    assert timeline.lookup(150).next_effective_ns == 200  # a capture to 250 straddles 200
    pending = timeline.lookup(250)
    assert (pending.kind, pending.version, pending.effective_ns) == ("pending", 2, 200)

    assert timeline.install(entry(2, 200)) and not timeline.needs_fetch()
    assert timeline.lookup(199).world.version == 1  # type: ignore[union-attr]
    assert timeline.lookup(200).world.version == 2  # type: ignore[union-attr]
    assert not timeline.install(entry(2, 200)) and not timeline.install(entry(1, 100))

    assert timeline.install(entry(3, 300))
    assert [h.version for h in timeline.held] == [2, 3] and timeline.floor_ns == 200
    gone = timeline.lookup(199)
    assert (gone.kind, gone.effective_ns) == ("gone", 200)
    assert timeline.lookup(250).world.version == 2  # type: ignore[union-attr]
    assert timeline.lookup(250).next_effective_ns == 300


def test_timeline_marks_versions_replaced_before_they_were_fetched() -> None:
    timeline = WorldTimeline()
    timeline.install(entry(4, 100))
    timeline.announce(5, 400)
    timeline.announce(6, 500)
    timeline.install(entry(6, 500))  # the server already served version 6
    superseded = timeline.lookup(450)
    assert (superseded.kind, superseded.version, superseded.effective_ns) == ("superseded", 5, 400)
    assert timeline.lookup(550).world.version == 6  # type: ignore[union-attr]
    assert timeline.lookup(350).world.version == 4  # type: ignore[union-attr]
    assert timeline.lookup(350).next_effective_ns == 400


def test_timeline_drops_versions_that_never_take_effect_and_tracks_required_versions() -> None:
    timeline = WorldTimeline()
    timeline.install(entry(1, 100))
    timeline.install(entry(2, 300))
    timeline.install(entry(3, 250))  # newer, takes effect earlier: version 2 never does
    assert [h.version for h in timeline.held] == [1, 3]
    assert timeline.lookup(320).world.version == 3  # type: ignore[union-attr]
    assert timeline.floor_ns is None
    assert timeline.require(5) and timeline.wanted_version() == 5
    timeline.install(entry(5, 600))
    assert not timeline.needs_fetch()


def test_capabilities_cover_every_watched_downlink_and_monitor_with_the_rate_as_margin() -> None:
    caps = channel_capabilities(CFG, "tokyo-pac")
    # muos-pac downlinks: (301.000, 301.125, 301.500) MHz - 41 MHz, each 25 kHz wide; the
    # derived monitors span 1.25 * 25 kHz = 31.25 kHz around them, so the outermost edges
    # are 260.000 MHz - 15.625 kHz and 260.500 MHz + 15.625 kHz; margin = 2 MHz (max rate).
    assert caps.min_hz == pytest.approx(259_984_375.0 - 2e6, rel=1e-15)
    assert caps.max_hz == pytest.approx(260_515_625.0 + 2e6, rel=1e-15)
    assert (caps.max_sample_rate_hz, caps.lookback_s, caps.concurrent_reads) == (2e6, 30.0, 8)

    # A fixed antenna watching two satellites with different translations covers both
    # plans: uhf-172e downlinks are (301.000, 301.125, 301.750) MHz - 33.6 MHz.
    fixed = config_with(
        "tokyo-pac", pointing={"az_deg": 120.0, "el_deg": 40.0, "satellite_ids": ["muos-pac", "uhf-172e"]}
    )
    caps = channel_capabilities(fixed, "tokyo-pac", tuning_margin_hz=0.0)
    assert caps.min_hz == pytest.approx(259_984_375.0, rel=1e-15)
    assert caps.max_hz == pytest.approx(268_150_000.0 + 15_625.0, rel=1e-15)

    # An assigned monitor beyond the configured ones widens the range.
    wide = MonitorAssignment(
        monitor_id="w", center_hz=270e6, span_hz=1e6, nfft=64, navg=1, frame_rate_hz=1.0
    )
    caps = channel_capabilities(CFG, "tokyo-pac", tuning_margin_hz=1e5, monitors=[wide])
    assert caps.max_hz == pytest.approx(270.5e6 + 1e5, rel=1e-15)
    with pytest.raises(ValueError, match="no-such-channel"):
        channel_capabilities(CFG, "no-such-channel")


def test_registry_builds_the_sim_driver_and_the_sim_clock_reports_sim() -> None:
    driver = get_driver("sim", proxy=None, lookback_s=10.0)
    assert isinstance(driver, SimDriver)
    with pytest.raises(ValueError, match="options"):
        get_driver("sim", no_such_option=1)
    with pytest.raises(ValueError, match="world_wait_s"):
        SimDriver(world_wait_s=0.0)
    assert sim_clock().status() == ClockStatus(source="sim", locked=True, est_offset_ns=0.0, holdover_s=0.0)
    assert SimDriver.clock_status() == sim_clock().status()


# ========================================================== fetching the world


async def test_open_fetches_the_world_with_the_bearer_token(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    backplane.publish(1, T_NOW - 100 * NS, scenario())
    async with open_driver(backplane) as driver:
        assert backplane.authorizations == [f"Bearer {TOKEN}"]
        assert driver.world_version == 1
        assert driver.capabilities == channel_capabilities(
            CFG, "tokyo-pac", monitors=assignment("tokyo-pac").monitors
        )
        assert driver.source is not None and active_sources() == [driver.source]


async def test_a_wrong_token_is_refused_and_open_fails_with_the_reason(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    backplane.publish(1, T_NOW - 100 * NS, scenario())
    driver = make_driver(open_timeout_s=0.5)
    with pytest.raises(CaptureError) as info:
        await driver.open(assignment("tokyo-pac"), context(backplane, token="WRONG-token-77"))
    await driver.close()
    assert info.value.code == "driver"
    assert "HTTP 401" in info.value.message and "token" in info.value.message
    assert "WRONG-token-77" not in info.value.message
    assert backplane.authorizations and set(backplane.authorizations) == {"Bearer WRONG-token-77"}
    with pytest.raises(CaptureError) as unopened:
        _ = driver.capabilities
    assert unopened.value.code == "driver"


async def test_failed_fetches_are_retried_with_backoff_and_logged(
    backplane: FakeBackplane, caplog: pytest.LogCaptureFixture, no_leftovers: None
) -> None:
    caplog.set_level(logging.WARNING, logger=LOG)
    backplane.publish(1, T_NOW - 100 * NS, scenario())
    backplane.fail_remaining = 2
    async with open_driver(backplane, retry_base_s=0.1, retry_max_s=1.0) as driver:
        assert driver.world_version == 1
        arrivals = [t for t, _ in backplane.requests]
        assert len(arrivals) == 3
        gaps = np.diff(arrivals)
        # waits of base * 2**(k-1) plus up to 20 % jitter: 0.1..0.12 s, then 0.2..0.24 s
        assert 0.1 <= gaps[0] < 0.12 + 0.15
        assert 0.2 <= gaps[1] < 0.24 + 0.15
        failures = [r.getMessage() for r in caplog.records if "fetch failed" in r.getMessage()]
        assert len(failures) == 2 and all("HTTP 503" in text for text in failures)
        assert driver.source is not None
        assert (driver.source.failures, driver.source.last_error) == (2, None)


async def test_without_a_world_reads_and_spectra_fail_with_driver(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    driver = make_driver()
    for call in (driver.read(T_NOW, 16, FS, FC), driver.psd(T_NOW, FC, FS, 64, 1)):
        with pytest.raises(CaptureError) as info:
            await call
        assert info.value.code == "driver"

    for simulation, reason in (
        (None, "runs no simulation"),
        ({"enabled": False}, "runs no simulation"),
        ({"enabled": True}, "scenario_url"),
        ({"enabled": True, "scenario_url": "http://elsewhere/x"}, "scenario_url"),
    ):
        driver = make_driver()
        with pytest.raises(CaptureError, match=reason) as info:
            await driver.open(assignment("tokyo-pac"), context(backplane, simulation=simulation))
        assert info.value.code == "driver"
        await driver.close()

    driver = make_driver(open_timeout_s=0.3)  # no document published: 404
    with pytest.raises(CaptureError, match="HTTP 404") as info:
        await driver.open(assignment("tokyo-pac"), context(backplane))
    await driver.close()


async def test_a_channel_of_another_site_or_missing_from_the_world_fails_open(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    backplane.publish(1, T_NOW - 100 * NS, scenario())
    for channel_id, reason in (("seoul-pac", "belongs to site 'seoul'"), ("nowhere", "not in the configuration")):
        driver = make_driver()
        with pytest.raises(CaptureError, match=reason) as info:
            await driver.open(assignment(channel_id), context(backplane))
        assert info.value.code == "driver"
        await driver.close()


async def test_drivers_of_one_node_share_one_world_source(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc = scenario()
    backplane.publish(1, T_NOW - 100 * NS, sc)
    a, b, other = make_driver(), make_driver(), make_driver()
    ctx = context(backplane)
    try:
        await asyncio.gather(a.open(assignment("tokyo-pac"), ctx), b.open(assignment("tokyo-io"), ctx))
        assert a.source is b.source and active_sources() == [a.source]
        assert len(backplane.requests) == 1  # one fetch served both opens

        backplane.publish(2, T_NOW, scenario(tone=True))
        await asyncio.gather(a.on_sim_update(2, T_NOW), b.on_sim_update(2, T_NOW))
        assert len(backplane.requests) == 2  # one fetch per announcement, not per driver
        assert a.world_version == b.world_version == 2

        await other.open(assignment("seoul-pac"), context(backplane, site="seoul"))
        assert other.source is not a.source and len(active_sources()) == 2
        await a.close()
        assert len(active_sources()) == 2  # b still holds the tokyo source
    finally:
        for driver in (a, b, other):
            await driver.close()


# ============================================================ samples and spectra


async def test_read_returns_render_iq_only_after_the_interval_has_passed(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc, effective = scenario(), T_NOW - 100 * NS
    backplane.publish(1, effective, sc)
    clock = ManualClock(T_NOW)
    async with open_driver(backplane, clock=clock) as driver:
        t0, n = T_NOW + NS // 2, 4096
        end = t0 + 131_072_000  # 4096 / 31250 Hz = 0.131072 s
        task = asyncio.ensure_future(driver.read(t0, n, FS, FC))
        clock.set(end - 1)
        await asyncio.sleep(0.05)
        assert not task.done(), "a read returned before its last sample existed"
        clock.set(end)
        capture = await asyncio.wait_for(task, 5.0)
        expected = world(sc, effective).render_iq("tokyo-pac", t0, n, FS, FC)
        assert capture.iq.dtype == np.complex64 and np.array_equal(capture.iq, expected)
        assert (capture.t_first_ns, capture.fs, capture.fc) == (t0, FS, FC)
        assert (capture.overflow, capture.clock_locked) == (False, True)
        # the relayed jammer is in the samples, well above the unit thermal floor
        assert float(np.mean(np.abs(capture.iq) ** 2)) > 2.0


async def test_psd_equals_render_psd_and_waits_for_its_snippet(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc, effective = scenario(), T_NOW - 100 * NS
    backplane.publish(1, effective, sc)
    clock = ManualClock(T_NOW)
    async with open_driver(backplane, clock=clock) as driver:
        reference = world(sc, effective)
        past = T_NOW - 2 * NS
        psd = await driver.psd(past, FC, FS, 1024, 8)
        assert psd.dtype == np.float32 and psd.shape == (1024,)
        assert np.array_equal(psd, reference.render_psd("tokyo-pac", past, FC, FS, 1024, 8))

        task = asyncio.ensure_future(driver.psd(T_NOW, FC, FS, 1024, 8))
        clock.set(T_NOW + 262_144_000 - 1)  # 8 x 1024 samples at 31250 Hz = 0.262144 s
        await asyncio.sleep(0.05)
        assert not task.done()
        clock.set(T_NOW + 262_144_000)
        psd = await asyncio.wait_for(task, 5.0)
        assert np.array_equal(psd, reference.render_psd("tokyo-pac", T_NOW, FC, FS, 1024, 8))


async def test_lookback_serves_the_last_30_s_and_refuses_older_intervals(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc, effective = scenario(), T_NOW - 100 * NS
    backplane.publish(1, effective, sc)
    clock = ManualClock(T_NOW)
    async with open_driver(backplane, clock=clock) as driver:
        reference = world(sc, effective)
        for t0 in (T_NOW - 30 * NS, T_NOW - 29_900_000_000, T_NOW - 5 * NS):
            capture = await driver.read(t0, 1024, FS, FC)
            assert np.array_equal(capture.iq, reference.render_iq("tokyo-pac", t0, 1024, FS, FC))
        for t0 in (T_NOW - 30 * NS - 1, T_NOW - 31 * NS):
            with pytest.raises(CaptureError) as info:
                await driver.read(t0, 1024, FS, FC)
            assert info.value.code == "too_late"
        with pytest.raises(CaptureError) as info:
            await driver.psd(T_NOW - 31 * NS, FC, FS, 64, 1)
        assert info.value.code == "too_late"


async def test_requests_outside_the_capabilities_are_out_of_range(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc, effective = scenario(), T_NOW - 100 * NS
    backplane.publish(1, effective, sc)
    clock = ManualClock(T_NOW)
    async with open_driver(backplane, clock=clock) as driver:
        caps = driver.capabilities
        past = T_NOW - NS
        bad_reads = [
            (past, 64, 2e6 * 1.01, FC),
            (past, 64, FS, caps.max_hz + 1.0),
            (past, 64, FS, caps.min_hz - 1.0),
            (past, 0, FS, FC),
            (past, MAX_CAPTURE_SAMPLES + 1, FS, FC),
        ]
        for args in bad_reads:
            with pytest.raises(CaptureError) as info:
                await driver.read(*args)
            assert info.value.code == "out_of_range", args
        for psd_args in ((past, FC, 2.1e6, 64, 1), (past, FC, FS, 1, 1), (past, FC, FS, 64, 0)):
            with pytest.raises(CaptureError) as info:
                await driver.psd(*psd_args)
            assert info.value.code == "out_of_range", psd_args
        # the edges of the capabilities are served
        capture = await driver.read(past, 256, 2e6, caps.max_hz)
        expected = world(sc, effective).render_iq("tokyo-pac", past, 256, 2e6, caps.max_hz)
        assert np.array_equal(capture.iq, expected)


# ============================================================== version switching


async def test_version_switch_serves_each_capture_from_the_world_in_force_at_its_first_sample(
    backplane: FakeBackplane, caplog: pytest.LogCaptureFixture, no_leftovers: None
) -> None:
    caplog.set_level(logging.INFO, logger=LOG)
    sc1, sc2 = scenario(), scenario(eirp_dbw=26.0, tone=True)
    e1, e2 = T_NOW - 100 * NS, T_NOW
    backplane.publish(1, e1, sc1)
    clock = ManualClock(T_NOW - 2 * NS)  # the announcement comes 2 s ahead
    async with open_driver(backplane, clock=clock) as driver:
        backplane.publish(2, e2, sc2)
        await driver.on_sim_update(2, e2)
        assert driver.world_version == 2 and len(backplane.requests) == 2
        clock.set(T_NOW + 5 * NS)
        n = 4096  # 0.131 s
        starts = {
            "before": e2 - 500_000_000,  # ends 0.369 s before the change
            "straddling": e2 - 50_000_000,  # first sample before, last after
            "after": e2 + 100_000_000,
        }
        caplog.clear()
        got = {name: (await driver.read(t0, n, FS, FC)).iq for name, t0 in starts.items()}
        w1, w2 = world(sc1, e1), world(sc2, e2)
        assert np.array_equal(got["before"], w1.render_iq("tokyo-pac", starts["before"], n, FS, FC))
        assert np.array_equal(got["straddling"], w1.render_iq("tokyo-pac", starts["straddling"], n, FS, FC))
        assert np.array_equal(got["after"], w2.render_iq("tokyo-pac", starts["after"], n, FS, FC))
        # the two worlds really differ there, so the switch is visible in the samples
        assert not np.array_equal(got["after"], w1.render_iq("tokyo-pac", starts["after"], n, FS, FC))
        straddles = [r.getMessage() for r in caplog.records if "straddles" in r.getMessage()]
        assert len(straddles) == 1 and str(e2) in straddles[0] and "version 1" in straddles[0]
        # spectra follow the same rule
        psd = await driver.psd(starts["after"], FC, FS, 256, 4)
        assert np.array_equal(psd, w2.render_psd("tokyo-pac", starts["after"], FC, FS, 256, 4))


async def test_at_most_two_worlds_are_kept_and_older_intervals_are_too_late(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    scs = {1: scenario(), 2: scenario(eirp_dbw=26.0), 3: scenario(tone=True)}
    effective = {1: T_NOW - 100 * NS, 2: T_NOW, 3: T_NOW + 10 * NS}
    backplane.publish(1, effective[1], scs[1])
    clock = ManualClock(T_NOW - 2 * NS)
    async with open_driver(backplane, clock=clock) as driver:
        for version in (2, 3):
            backplane.publish(version, effective[version], scs[version])
            await driver.on_sim_update(version, effective[version])
        assert driver.source is not None
        assert [v.version for v in driver.source.versions] == [2, 3]
        clock.set(T_NOW + 12 * NS)
        with pytest.raises(CaptureError) as info:
            await driver.read(T_NOW - NS, 1024, FS, FC)  # version 1's time, 13 s ago
        assert info.value.code == "too_late"
        for version, t0 in ((2, T_NOW + NS), (3, T_NOW + 11 * NS)):
            capture = await driver.read(t0, 1024, FS, FC)
            expected = world(scs[version], effective[version]).render_iq("tokyo-pac", t0, 1024, FS, FC)
            assert np.array_equal(capture.iq, expected)


async def test_a_read_after_an_announced_version_waits_for_it(
    backplane: FakeBackplane, caplog: pytest.LogCaptureFixture, no_leftovers: None
) -> None:
    caplog.set_level(logging.WARNING, logger=LOG)
    sc1, sc2 = scenario(), scenario(tone=True)
    e1, e2 = T_NOW - 100 * NS, T_NOW
    backplane.publish(1, e1, sc1)
    clock = ManualClock(T_NOW + 3 * NS)
    async with open_driver(backplane, clock=clock, world_wait_s=0.4, refresh_timeout_s=0.2) as driver:
        await driver.on_sim_update(2, e2)  # the server still serves version 1
        assert driver.world_version == 1
        with pytest.raises(CaptureError, match="version 2") as info:
            await driver.read(e2 + NS, 1024, FS, FC)
        assert info.value.code == "driver"
        before = await driver.read(e2 - NS, 1024, FS, FC)  # version 1's time is still served
        assert np.array_equal(before.iq, world(sc1, e1).render_iq("tokyo-pac", e2 - NS, 1024, FS, FC))

        waiting = asyncio.ensure_future(driver.read(e2 + NS, 1024, FS, FC))
        await asyncio.sleep(0.15)
        assert not waiting.done()
        backplane.publish(2, e2, sc2)  # the retry picks it up
        capture = await asyncio.wait_for(waiting, 5.0)
        assert np.array_equal(capture.iq, world(sc2, e2).render_iq("tokyo-pac", e2 + NS, 1024, FS, FC))
        assert any("older version than announced" in r.getMessage() for r in caplog.records)


async def test_assign_refetches_before_the_ack_and_follows_the_new_configuration(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc = scenario()
    backplane.publish(1, T_NOW - 100 * NS, sc)
    async with open_driver(backplane) as driver:
        assert len(backplane.requests) == 1 and driver.capabilities.max_sample_rate_hz == 2e6
        backplane.publish(2, T_NOW, sc, config_with("tokyo-pac", max_sample_rate_hz=1.5e6))
        await driver.on_assign(assignment("tokyo-pac"), context(backplane))
        assert len(backplane.requests) == 2 and driver.world_version == 2
        assert driver.capabilities.max_sample_rate_hz == 1.5e6

        backplane.fail_remaining = 10  # a failing refetch keeps the world it has
        await driver.on_assign(assignment("tokyo-pac"), context(backplane))
        assert driver.world_version == 2 and len(backplane.requests) >= 3


async def test_a_world_without_the_channel_fails_it_cleanly(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc, e1, e2 = scenario(), T_NOW - 100 * NS, T_NOW
    backplane.publish(1, e1, sc)
    clock = ManualClock(T_NOW - 2 * NS)
    async with open_driver(backplane, "tokyo-io", clock=clock) as driver:
        backplane.publish(2, e2, sc, config_with("tokyo-io", remove=True))
        await driver.on_sim_update(2, e2)
        clock.set(T_NOW + 3 * NS)
        before = await driver.read(e2 - NS, 1024, FS, FC)
        assert np.array_equal(before.iq, world(sc, e1).render_iq("tokyo-io", e2 - NS, 1024, FS, FC))
        for call in (driver.read(e2 + NS, 1024, FS, FC), driver.psd(e2 + NS, FC, FS, 64, 1)):
            with pytest.raises(CaptureError) as info:
                await call
            assert info.value.code == "driver"
            assert "tokyo-io" in info.value.message and "version 2" in info.value.message
        with pytest.raises(CaptureError, match="not in the configuration") as info:
            await driver.on_assign(assignment("tokyo-io"), context(backplane))
        assert info.value.code == "driver"


# ============================================================== the event loop


FRESH_NODE_SCRIPT = r"""
# A cold sim node in a process of its own: nothing heavy is imported beforehand, so the
# first open pays every one-off cost (httpx and its TLS context, the simulator and scipy
# imported in a worker thread, the first world build) while the loop is watched.
import asyncio, json, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

from petal_locate.node.drivers.base import DriverContext
from petal_locate.node.drivers.sim import SimDriver
from petal_locate.protocol.messages import ChannelAssignment, MonitorAssignment

NS = 10**9
NOW = time.time_ns()
CONFIG = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))  # the document form of SystemConfig


def body(version, effective_ns, emitters):
    scenario = {"name": "lag", "seed": 3, "description": "Simulated scenario (not real data).",
                "emitters": emitters}
    return json.dumps({"version": version, "effective_ns": str(effective_ns),
                       "config": CONFIG, "scenario": scenario}).encode()


JAMMER = {"id": "j", "kind": "jammer", "lat_deg": 20.0, "lon_deg": 132.0, "freq_hz": 301.005e6,
          "eirp_dbw": 20.0, "waveform": {"type": "noise", "bandwidth_hz": 10e3}}
TONE = {"id": "t", "kind": "jammer", "lat_deg": 25.0, "lon_deg": 140.0, "freq_hz": 301.008e6,
        "eirp_dbw": 15.0, "waveform": {"type": "cw"}}
DOC = [body(1, NOW - 100 * NS, [JAMMER])]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = DOC[0]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
WATCH = ("httpx", "anyio", "scipy", "petal_locate.sim")
cold = {name: name in sys.modules for name in WATCH}


async def main():
    worst, phase, ticks = {}, ["idle"], [0]

    async def ticker():
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(0.002)
            lag = loop.time() - start - 0.002
            worst[phase[0]] = max(worst.get(phase[0], 0.0), lag)
            ticks[0] += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    driver = SimDriver(proxy=None)
    channel = ChannelAssignment(channel_id="tokyo-pac", antenna_summary="simulated", monitors=[
        MonitorAssignment(monitor_id="m", center_hz=260e6, span_hz=31250.0, nfft=1024, navg=8,
                          frame_rate_hz=2.0)])
    ctx = DriverContext(site_id="tokyo", server_http_url=f"http://127.0.0.1:{server.server_port}",
                        token="t", simulation={"enabled": True, "scenario_url": "/node/v1/sim",
                                               "version": 1})
    phase[0] = "open"
    await driver.open(channel, ctx)
    phase[0] = "rebuild"
    DOC[0] = body(2, NOW - 50 * NS, [JAMMER, TONE])
    await driver.on_sim_update(2, NOW - 50 * NS)
    phase[0] = "render"
    started = time.perf_counter()
    capture = await driver.read(time.time_ns() - 5 * NS, 262144, 250e3, 260e6)
    render_s = time.perf_counter() - started
    phase[0] = "psd"
    await driver.psd(time.time_ns() - NS, 260e6, 31250.0, 1024, 8)
    phase[0] = "after"
    await asyncio.sleep(0.05)  # the ticker wakes: a block just before shows only then
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await driver.close()
    print(json.dumps({"cold": cold, "loaded": {name: name in sys.modules for name in WATCH},
                      "version": 2 if capture.iq.shape == (262144,) else None,
                      "render_s": render_s, "ticks": ticks[0], "worst_s": worst}))


asyncio.run(main())
"""


def test_the_event_loop_is_never_blocked_while_worlds_are_built_or_captures_rendered(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fresh_node.py"
    script.write_text(FRESH_NODE_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(CONFIG_PATH)],
        capture_output=True, text=True, timeout=120, env=dict(os.environ), cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert not any(report["cold"].values()), f"not a cold start: {report['cold']}"
    assert all(report["loaded"].values()), f"one-off costs not exercised: {report['loaded']}"
    assert report["render_s"] > 0.1, "the render was too quick to show anything about the loop"
    assert report["ticks"] > 100
    assert set(report["worst_s"]) >= {"open", "rebuild", "render"}  # psd may beat one tick
    worst_phase = max(report["worst_s"], key=report["worst_s"].get)
    worst = report["worst_s"][worst_phase]
    assert worst < 0.050, f"event loop held for {worst * 1e3:.1f} ms during {worst_phase}"


# ========================================================== the whole node path


async def test_node_agent_runs_the_sim_driver_end_to_end(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    sc = scenario()
    effective = time.time_ns() - 60 * NS
    backplane.publish(1, effective, sc)
    backplane.channels = [assignment("tokyo-pac").to_wire(), assignment("tokyo-io").to_wire()]
    backplane.capture_plan = [
        dict(capture_id="cap-1", channel_id="tokyo-pac", n=3125, fs=FS, fc=FC, lead_s=0.4)
    ]
    agent = NodeAgent(
        backplane.ws_url,
        SITE,
        TOKEN,
        driver="sim",
        driver_options={"proxy": None, "retry_base_s": 0.05, "retry_max_s": 0.5},
        node_id="node-tokyo-test",
        clock=sim_clock(),
        proxy=None,
        backoff_base_s=0.05,
        backoff_max_s=0.5,
        close_timeout_s=0.5,
        teardown_timeout_s=2.0,
    )
    task = agent.start()
    try:
        acks = await wait_until(lambda: backplane.of("assign_ack"), 10.0, "assign_ack")
        ack = acks[0]
        assert ack["ok"] and [c["channel_id"] for c in ack["channels"]] == ["tokyo-pac", "tokyo-io"]
        for entry_ in ack["channels"]:
            expected_caps = channel_capabilities(
                CFG, entry_["channel_id"], monitors=assignment(entry_["channel_id"]).monitors
            ).to_wire()
            assert entry_["ok"] and entry_["capabilities"] == expected_caps.model_dump(mode="json")
        assert len(active_sources()) == 1  # both channels read one world

        reference = world(sc, effective)
        capture = await wait_until(lambda: backplane.captures.get("cap-1"), 10.0, "capture cap-1")
        request = backplane.of("capture_request")[0]
        expected = reference.render_iq("tokyo-pac", request["t_start_ns"], 3125, FS, FC)
        assert np.array_equal(capture.iq, expected)
        assert capture.t_first_ns == request["t_start_ns"]
        assert (capture.sample_rate_hz, capture.center_hz) == (FS, FC)
        assert (capture.clock.source, capture.clock.locked, capture.overflow) == ("sim", True, False)

        await wait_until(
            lambda: {h.channel_id for h, _ in backplane.psd_frames()} >= {"tokyo-pac", "tokyo-io"},
            10.0,
            "spectrum frames of both channels",
        )
        for header, psd in backplane.psd_frames()[:4]:
            expected_psd = reference.render_psd(
                header.channel_id, header.t_ns, header.center_hz, header.span_hz, header.nfft, header.navg
            )
            assert np.array_equal(psd, expected_psd)
        assert backplane.of("hello")[0]["driver"] == "sim"
        assert backplane.of("hello")[0]["clock"]["source"] == "sim"
        assert all(h["clock"]["source"] == "sim" for h in backplane.of("heartbeat"))

        fetched = len(backplane.requests)
        e2 = time.time_ns() + 2 * NS
        sc2 = scenario(tone=True)
        backplane.publish(2, e2, sc2)
        backplane.send_ws({"type": "sim_update", "version": 2, "effective_ns": e2})
        await wait_until(lambda: len(backplane.requests) > fetched, 5.0, "fetch after sim_update")
        assert set(backplane.authorizations) == {f"Bearer {TOKEN}"}
        # through the real agent, captures on either side of the change come from the
        # world in force at their first sample
        starts = {"cap-before": e2 - 600_000_000, "cap-after": e2 + 200_000_000}
        for cap_id, t0 in starts.items():
            backplane.send_ws({"type": "capture_request", "capture_id": cap_id, "channel_id": "tokyo-pac",
                               "t_start_ns": t0, "n_samples": 3125, "sample_rate_hz": FS, "center_hz": FC,
                               "deadline_ns": t0 + 10 * NS})
        await wait_until(lambda: all(c in backplane.captures for c in starts), 10.0, "captures around the change")
        newer = world(sc2, e2)
        before = backplane.captures["cap-before"]
        after = backplane.captures["cap-after"]
        assert before.t_first_ns == starts["cap-before"] and after.t_first_ns == starts["cap-after"]
        assert np.array_equal(before.iq, reference.render_iq("tokyo-pac", starts["cap-before"], 3125, FS, FC))
        assert np.array_equal(after.iq, newer.render_iq("tokyo-pac", starts["cap-after"], 3125, FS, FC))
        assert not np.array_equal(after.iq, reference.render_iq("tokyo-pac", starts["cap-after"], 3125, FS, FC))
    finally:
        await agent.stop(timeout_s=5.0)
    assert task.done() and task.exception() is None


# ============================================================== direct source use


async def test_an_injected_source_is_used_and_left_open(backplane: FakeBackplane, no_leftovers: None) -> None:
    sc, effective = scenario(), T_NOW - 100 * NS
    backplane.publish(1, effective, sc)
    source = SimWorldSource(backplane_url(backplane.http_url, PATH), TOKEN, proxy=None, retry_base_s=0.05)
    try:
        driver = make_driver(source=source)
        await driver.open(assignment("tokyo-pac"), context(backplane, clock=ManualClock(T_NOW)))
        assert driver.source is source and active_sources() == []
        capture = await driver.read(T_NOW - NS, 512, FS, FC)
        assert np.array_equal(capture.iq, world(sc, effective).render_iq("tokyo-pac", T_NOW - NS, 512, FS, FC))
        await driver.close()
        assert not source.closed and source.newest is not None and source.attempts == 1
    finally:
        await source.aclose()
    assert source.closed
    with pytest.raises(BackplaneError, match="closed"):
        await source.refresh(0.1)


async def test_a_fetch_loop_that_dies_is_logged_and_restarted_by_the_next_request(
    backplane: FakeBackplane, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    no_leftovers: None,
) -> None:
    caplog.set_level(logging.ERROR, logger=LOG)
    backplane.publish(1, T_NOW - 100 * NS, scenario())
    install = WorldTimeline.install
    calls = [0]

    def broken_once(self: WorldTimeline, item: WorldVersion) -> bool:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("injected fault")
        return install(self, item)

    monkeypatch.setattr(WorldTimeline, "install", broken_once)
    source = SimWorldSource(backplane_url(backplane.http_url, PATH), TOKEN, proxy=None, retry_base_s=0.05)
    try:
        with pytest.raises(BackplaneError, match="injected fault"):
            await source.refresh(0.5)
        assert any("fetch loop failed" in r.getMessage() for r in caplog.records)
        newest, problem = await source.refresh(3.0)
        assert newest.version == 1 and problem is None
    finally:
        await source.aclose()


async def test_the_version_named_in_welcome_is_fetched_in_the_background(
    backplane: FakeBackplane, caplog: pytest.LogCaptureFixture, no_leftovers: None
) -> None:
    caplog.set_level(logging.WARNING, logger=LOG)
    sc1, sc2 = scenario(), scenario(tone=True)
    backplane.publish(1, T_NOW - 100 * NS, sc1)  # the server lags behind its own welcome
    driver = make_driver(open_timeout_s=0.3)
    ctx = context(backplane, simulation={"enabled": True, "scenario_url": PATH, "version": 2})
    try:
        await driver.open(assignment("tokyo-pac"), ctx)  # opens on what it has, and says so
        assert driver.world_version == 1
        assert any("refetch failed" in r.getMessage() for r in caplog.records)
        backplane.publish(2, T_NOW - 50 * NS, sc2)
        await wait_until(lambda: driver.world_version == 2, 5.0, "version 2 by retry alone")
        assert len(backplane.requests) >= 2
    finally:
        await driver.close()


async def test_velocity_ephemeris_errors_grow_from_the_version_effective_time(
    backplane: FakeBackplane, no_leftovers: None
) -> None:
    # An in-track drift of 0.05 m/s with no t_ref: SimWorld refuses it without an epoch;
    # the driver anchors it at the document's effective_ns.
    data = scenario().model_dump(mode="json")
    data["errors"]["ephemeris"] = {"muos-pac": {"ric_pos_m": [0.0, 0.0, 0.0], "ric_vel_mps": [0.0, 0.05, 0.0]}}
    drifting = Scenario.model_validate(data)
    effective = T_NOW - 100 * NS
    backplane.publish(1, effective, drifting)
    async with open_driver(backplane, clock=ManualClock(T_NOW)) as driver:
        t0 = T_NOW - NS
        capture = await driver.read(t0, 2048, FS, FC)
        assert np.array_equal(capture.iq, world(drifting, effective).render_iq("tokyo-pac", t0, 2048, FS, FC))
        # 100 s more drift is 5 m of range, several carrier cycles: the anchor matters
        other = world(drifting, effective - 100 * NS).render_iq("tokyo-pac", t0, 2048, FS, FC)
        assert not np.array_equal(capture.iq, other)


def test_sim_clock_reports_sim_and_can_carry_the_demo_offset() -> None:
    """A simulated site reports source 'sim' (PROTOCOL.md); `petal-locate demo --start`
    shifts its labelled time like the server's (review K7, K8)."""
    import time as _time

    from petal_locate.node.drivers.sim import sim_clock

    plain = sim_clock()
    assert plain.status().source == "sim" and plain.status().locked and plain.offset_ns == 0
    shifted = sim_clock(offset_ns=-3_600 * 10**9)
    assert abs(shifted.now_ns() - (_time.time_ns() - 3_600 * 10**9)) < 10**9
