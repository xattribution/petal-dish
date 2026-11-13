"""Tests of the console WebSocket ``/ws/ui`` (``server/api/ws.py``) over real sockets:
the application of ``create_app`` served by uvicorn on an ephemeral loopback port,
browsers played by ``websockets`` clients, a real ``NodeAgent`` (noise driver) where the
spectrum must come through the pipeline.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from petal_locate.config import dump_config
from petal_locate.protocol.framing import decode_console_psd, decode_frame, encode_console_psd
from petal_locate.server.api import schemas, ws
from petal_locate.server.app import build_server, create_app
from petal_locate.server.settings import Marking, Settings
from petal_locate.sim.scenario import dump_scenario

from test_api import make_config, make_event, make_scenario  # tests/ is on sys.path (pytest prepend mode)

WAIT_S = 8.0


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    config = tmp_path / "config.yaml"
    config.write_text(dump_config(make_config()))
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(dump_scenario(make_scenario()))
    presets = tmp_path / "presets"
    presets.mkdir()
    return {"config": config, "scenario": scenario, "presets": presets, "data": tmp_path / "data"}


def settings_for(files: dict[str, Path], **extra: Any) -> Settings:
    values: dict[str, Any] = {
        "config_path": files["config"],
        "scenario_path": files["scenario"],
        "data_dir": files["data"],
        "port": 0,
        "heartbeat_s": 0.5,
        "node_tokens": {"alpha": "token-alpha", "bravo": "token-bravo"},
        "marking": Marking(level="UNCLASSIFIED", line="UNCLASSIFIED // TEST"),
    }
    values.update(extra)
    return Settings(**values)


class Served:
    def __init__(self, app: Any, server: Any, port: int) -> None:
        self.app = app
        self.server = server
        self.port = port

    @property
    def runtime(self) -> Any:
        return self.app.state.runtime

    def ws_url(self, query: str = "") -> str:
        return f"ws://127.0.0.1:{self.port}/ws/ui{query}"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@contextlib.asynccontextmanager
async def serving(settings: Settings, presets: Path | None = None) -> AsyncIterator[Served]:
    """The whole server on an ephemeral port; asserts that it leaves no task behind."""
    before = asyncio.all_tasks()
    app = create_app(settings, presets_dir=presets)
    server = build_server(app, settings, log_config=None, capture_signals=False)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(int(WAIT_S / 0.01)):
            if server.started or task.done():
                break
            await asyncio.sleep(0.01)
        assert server.started, "server did not start"
        yield Served(app, server, server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20.0)
    await asyncio.sleep(0)
    leftover = [t for t in asyncio.all_tasks() - before if not t.done() and t is not asyncio.current_task()]
    assert not leftover, leftover


async def console(served: Served, query: str = "", **options: Any) -> ClientConnection:
    return await connect(served.ws_url(query), proxy=None, compression=None, max_size=4 * 1024 * 1024, **options)


async def next_text(client: ClientConnection, kind: str | None = None, timeout: float = WAIT_S) -> dict[str, Any]:
    """The next text message (of type ``kind``), skipping everything else."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"no {kind or 'text'} message within {timeout} s")
        item = await asyncio.wait_for(client.recv(), remaining)
        if isinstance(item, str):
            message = json.loads(item)
            if kind is None or message.get("type") == kind:
                return message


async def next_frame(client: ClientConnection, timeout: float = WAIT_S) -> bytes:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"no spectrum frame within {timeout} s")
        item = await asyncio.wait_for(client.recv(), remaining)
        if isinstance(item, bytes):
            return item


async def frames_during(client: ClientConnection, seconds: float) -> list[bytes]:
    """Everything binary that arrives within ``seconds``."""
    out: list[bytes] = []
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            item = await asyncio.wait_for(client.recv(), remaining)
        except TimeoutError:
            break
        if isinstance(item, bytes):
            out.append(item)
    return out


async def close_code(client: ClientConnection, timeout: float = WAIT_S) -> int | None:
    """Read until the server closes; returns the close code it sent."""
    with contextlib.suppress(ConnectionClosed):
        while True:
            await asyncio.wait_for(client.recv(), timeout)
    return client.close_code


def big_frame(seq: int, nfft: int = 131072, monitor: str = "big") -> bytes:
    psd = np.full(nfft, float(seq % 50), dtype=np.float32)
    return encode_console_psd("alpha-a", monitor, 1_794_000_000 * 10**9 + seq, 260e6, 31250.0, nfft, seq, psd)


# --------------------------------------------------------------------------- text messages


async def test_status_event_config_and_sim_messages(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as served:
        client = await console(served)
        try:
            first = schemas.WsStatusMessage.model_validate(await next_text(client, timeout=3.0))
            assert first.marking is not None and first.marking.level == "UNCLASSIFIED"
            assert first.simulation is True and first.config_version == 1
            assert [n.site_id for n in first.nodes] == ["alpha", "bravo", "charlie"]
            second = schemas.WsStatusMessage.model_validate(await next_text(client, "status", timeout=3.0))
            assert second.server_t_ns > first.server_t_ns  # about once a second

            event_id = make_event(served.runtime)
            message = await next_text(client, "event")
            event = schemas.WsEventMessage.model_validate(message).event
            assert event.id == event_id and event.state == "detected" and event.simulated

            async with httpx.AsyncClient(base_url=served.http_url, trust_env=False, timeout=WAIT_S) as http:
                current = (await http.get("/api/v1/config")).json()["config"]
                current["description"] = "changed"
                reply = await http.put("/api/v1/config", json=current, headers={"If-Match": '"1"'})
                assert reply.status_code == 200
                assert await next_text(client, "config") == {"type": "config", "config_version": 2}
                sim = await next_text(client, "sim")  # the simulated world follows the configuration
                assert sim["version"] >= 2

                reply = await http.put("/api/v1/sim/scenario", json=make_scenario("moved", 25.0).model_dump(mode="json"))
                assert reply.status_code == 200
                assert await next_text(client, "sim") == {"type": "sim", "version": reply.json()["version"]}

                assert (await http.post(f"/api/v1/events/{event_id}/close")).status_code == 200
                while True:
                    closed = (await next_text(client, "event"))["event"]
                    if closed["id"] == event_id and closed["state"] == "closed":
                        break
        finally:
            await client.close()


async def test_malformed_console_messages_are_ignored(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as served:
        client = await console(served)
        try:
            await next_text(client, "status")
            for bad in ("not json", '{"type": "hello"}', '[1, 2]', '{"type": "subscribe", "psd": [["only-one"]]}',
                        '{"type": "subscribe", "psd": "x"}'):
                await client.send(bad)
            await client.send(b"\x00\x01binary")
            await client.send(json.dumps({"type": "subscribe", "psd": [["alpha-a", "big"]]}))
            await asyncio.sleep(0.2)
            served.runtime.console.publish_psd("alpha-a", "big", big_frame(1, nfft=1024))
            header, payload = decode_frame(await next_frame(client))
            console_header, psd, baseline = decode_console_psd(header, payload)
            assert console_header.monitor_id == "big" and psd.shape == (1024,) and baseline is None
        finally:
            await client.close()


# --------------------------------------------------------------------------- spectrum frames


async def test_spectrum_frames_follow_the_subscription(files: dict[str, Path]) -> None:
    """A real node (noise driver) streams spectra; the console gets the PLB1 frames of
    the windows it subscribed to, and none of the others."""
    from petal_locate.node.agent import NodeAgent

    async with serving(settings_for(files), files["presets"]) as served:
        agent = NodeAgent(served.ws_url().replace("/ws/ui", ""), "alpha", "token-alpha", node_id="node-alpha",
                          driver="noise", proxy=None, backoff_base_s=0.05, backoff_max_s=0.4, close_timeout_s=1.0)
        agent.start()
        client = await console(served)
        try:
            await asyncio.wait_for(agent.wait_online(), WAIT_S)
            assert await frames_during(client, 1.2) == []  # nothing streamed before a subscription

            await client.send(json.dumps({"type": "subscribe", "psd": [["alpha-a", "sat-a:t1"]]}))
            header, payload = decode_frame(await next_frame(client))
            console_header, psd, baseline = decode_console_psd(header, payload)
            assert (console_header.channel_id, console_header.monitor_id) == ("alpha-a", "sat-a:t1")
            assert console_header.nfft == 1024 and psd.shape == (1024,) and np.all(np.isfinite(psd))
            assert console_header.span_hz == pytest.approx(31250.0) and console_header.t_ns > 0
            assert isinstance(header["t_ns"], str)  # nanoseconds as a decimal string
            assert baseline is None or baseline.shape == (1024,)
            frames = await frames_during(client, 1.5)
            assert frames and all(decode_frame(f)[0]["monitor_id"] == "sat-a:t1" for f in frames)

            await client.send(json.dumps({"type": "subscribe", "psd": []}))
            await frames_during(client, 0.3)  # what was already on its way
            assert await frames_during(client, 1.2) == []
        finally:
            await client.close()
            await agent.stop(5.0)


async def test_a_slow_console_loses_old_frames_and_holds_up_nobody(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as served:
        slow = await console(served, max_queue=1)  # stops reading the socket after one frame
        fast = await console(served)
        subscribe = json.dumps({"type": "subscribe", "psd": [["alpha-a", "big"]]})
        try:
            await slow.send(subscribe)
            await fast.send(subscribe)
            await next_text(fast, "status")
            await asyncio.sleep(0.2)
            count = 40
            seen: list[int] = []

            async def reader() -> None:
                while not seen or seen[-1] != count - 1:
                    seen.append(int(decode_frame(await next_frame(fast))[0]["seq"]))

            reading = asyncio.create_task(reader())
            started = time.monotonic()
            for seq in range(count):
                served.runtime.console.publish_psd("alpha-a", "big", big_frame(seq))
                await asyncio.sleep(0.02)
            await asyncio.wait_for(reading, WAIT_S)
            assert seen[-1] == count - 1 and time.monotonic() - started < WAIT_S

            mailboxes = served.app.state.ui_sockets.subscriptions
            assert len(mailboxes) == 2
            assert max(m.dropped for m in mailboxes) > 0  # the slow one lost intermediate frames
            assert all(m.waiting <= 3 for m in mailboxes)  # newest wins: one per kind of message

            received = [int(decode_frame(f)[0]["seq"]) for f in await frames_during(slow, 3.0)]
            assert received and received[-1] == count - 1 and len(received) < count
            assert received == sorted(received)
        finally:
            await slow.close()
            await fast.close()


async def test_a_console_that_stops_reading_is_closed(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "SEND_TIMEOUT_S", 0.5)
    async with serving(settings_for(files), files["presets"]) as served:
        stuck = await console(served, max_queue=1)
        try:
            await stuck.send(json.dumps({"type": "subscribe", "psd": [["alpha-a", "big"]]}))
            await asyncio.sleep(0.2)
            assert served.app.state.ui_sockets.open == 1
            for seq in range(30):
                served.runtime.console.publish_psd("alpha-a", "big", big_frame(seq))
                await asyncio.sleep(0.01)
            for _ in range(int(WAIT_S / 0.05)):
                if served.app.state.ui_sockets.open == 0:
                    break
                await asyncio.sleep(0.05)
            assert served.app.state.ui_sockets.open == 0
            assert served.runtime.console.subscribers == 0
            other = await console(served)  # the server is fine
            await next_text(other, "status")
            await other.close()
            assert await close_code(stuck) in (ws.CLOSE_NOT_READING, 1006, None)
        finally:
            await stuck.close()


# --------------------------------------------------------------------------- authentication and shutdown


async def test_api_token_on_the_handshake_and_out_of_the_log(
    files: dict[str, Path], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    # The assertion below needs uvicorn's INFO records to reach the root handler, whatever
    # an earlier test's uvicorn.Config did to these loggers.
    for name in ("uvicorn.error", "uvicorn.access"):
        caplog.set_level(logging.DEBUG, logger=name)
        monkeypatch.setattr(logging.getLogger(name), "propagate", True)
    secret = "tok-5ecr3t-value"
    async with serving(settings_for(files, api_token=secret), files["presets"]) as served:
        for query in ("", "?token=wrong"):
            client = await console(served, query)
            assert await close_code(client) == ws.CLOSE_POLICY
            assert "API token" in (client.close_reason or "")
        client = await console(served, f"?token={secret}")
        try:
            message = await next_text(client, "status")
            assert message["marking"]["line"] == "UNCLASSIFIED // TEST"
        finally:
            await client.close()
        assert served.app.state.ui_sockets.refused == 2
    # Server-side records only: the test's own client library logs its request line.
    messages = [r.getMessage() for r in caplog.records if not r.name.startswith("websockets.client")]
    assert not any(secret in m for m in messages)
    assert any("/ws/ui?token=[redacted]" in m for m in messages)


async def test_shutdown_closes_consoles_with_1001(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as served:
        client = await console(served)
        await next_text(client, "status")
        served.server.should_exit = True
        assert await close_code(client) == ws.CLOSE_GOING_AWAY
        await client.close()


async def test_a_console_is_refused_while_the_server_closes(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as served:
        await served.app.state.ui_sockets.close_all()
        client = await console(served)
        assert await close_code(client) == ws.CLOSE_GOING_AWAY
        await client.close()
