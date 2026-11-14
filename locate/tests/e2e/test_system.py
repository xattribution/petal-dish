"""End-to-end tests of the whole system, driven only through its console interface.

`petal-locate demo` runs here as it does for an operator: one server process plus one
`petal-locate node` subprocess per configured site, each with the simulated radio
driver, talking over real WebSockets on a loopback port chosen by the operating system.
The tests then use nothing but the HTTP API under `/api/v1` and the console WebSocket
`/ws/ui` (PROTOCOL.md section B), exactly as the browser console does: presets are
loaded with `POST /sim/presets/{name}/load`, events are read with `GET /events` and
`GET /events/{id}`, the simulated truth with `GET /sim/truth`.

One demo serves the whole module. It runs the simulated world from a fixed epoch
(`--start`, 03 UTC, where the scenarios' geometry gives one fix), so the outcome does not
depend on the time of day the tests run, and it carries the random API token the demo
prints in its console address. It starts on the `quiet` scenario (nothing but the
two reference carriers), so every interferer appears after the trackers have learned
the noise floor, as in operation. Each scenario test loads its preset, waits for what
the operator should see, checks it, and then returns the world to `quiet`, waits for
its events to be released and closes them (a closed event is never re-opened, so the
next scenario starts with fresh events).

What the scenarios must show (config/scenarios/*.yaml):

- quiet: no event at all; the reference carriers are authorized, not interference.
- pacific-demo: an uplink verdict and a located jammer with the truth inside the 95 %
  error ellipse (and the event survives a configuration replaced mid-round); no site
  clock is flagged.
- uplink-cw: FDOA only, and the operator is told in words why there is no TDOA; a
  solution without redundancy is not a fix (never located), a fix holds the truth.
- local-interferer: a local verdict at one site; never located; tasking stands down.
- clock-fault: calibration at work: every solved row carries a reference-carrier
  correction, pairs through the late Naha clock and a rotated partner (Manila) are used,
  and the same rows solved without their corrections land far from the truth; the
  site clock estimate of `/status` flags Naha about 10 us behind and no other site.
- ephemeris-error: two satellites kilometres off in-track (TLE-class) while the solver
  assumes 1 km: the calibrated fix holds the truth inside its ellipse, and the same rows
  solved without their corrections land more than 10 km off; no healthy clock is blamed
  with confidence (a caution names the satellite alternative); the clock checks name the
  two satellites, the solves widen their position sigma, and a fix from 50 rows still
  holds the truth, its advice saying that the ellipse is widened and by how much.
- uplink-swept: ambiguous correlations and no false position.
- two-jammers: two separate events, each located near its own jammer.
- pacific-demo, then ephemeris-error loaded with no break (another jammer on the same
  frequency, 1,000 km away): the rows of the first are set aside (or a new event is
  raised) and the second is located afresh, never from a mix of the two.

The scenario tests take 30 to 90 s each and are marked slow; `test_smoke` is the fast
check of the stack. Nothing is left behind: the fixture stops the demo with SIGINT,
checks that every node process it started has exited, and fails on any traceback in
the server's log.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import websockets

from petal_locate.config import loads_config
from petal_locate.core.geodesy import geodesic_distance_m
from petal_locate.core.solver import solve
from petal_locate.core.timebase import ns_from_iso
from petal_locate.core.types import Measurement, PathSpec
from petal_locate.protocol.framing import decode_console_psd, decode_frame

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "config" / "scenarios"
START_SCENARIO = "quiet"
START_EPOCH = "2026-11-13T03:00:00Z"
"""Simulated time the demo starts from (`petal-locate demo --start`)."""
API = "/api/v1"
NS = 1_000_000_000
STARTUP_S = 90.0
"""Longest the demo may take to print its console address and bring every node online."""


# --------------------------------------------------------------------------- the demo process


@dataclass
class Demo:
    """A running `petal-locate demo`: its process, console address and log."""

    process: subprocess.Popen[str]
    base_url: str
    log_path: Path
    token: str = ""

    @property
    def headers(self) -> dict[str, str]:
        """The API token, as every console request carries it."""
        return {"Authorization": f"Bearer {self.token}"}

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


def _children_of(pid: int) -> list[int]:
    """Node processes started by the demo with ``--exit-with-parent <pid>``."""
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"--exit-with-parent" in argv:
            index = argv.index(b"--exit-with-parent")
            if index + 1 < len(argv) and argv[index + 1] == str(pid).encode():
                out.append(int(entry.name))
    return out


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Demo]:
    """The demo on the quiet scenario, on a free loopback port, with its own data
    directory; stopped (and checked for leftovers and tracebacks) after the module."""
    work = tmp_path_factory.mktemp("demo")
    log_path = work / "demo.log"
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", PYTHONUNBUFFERED="1")
    argv = [
        sys.executable, "-m", "petal_locate.cli", "demo",
        "--port", "0",
        "--data-dir", str(work / "data"),
        "--scenario", str(SCENARIOS / f"{START_SCENARIO}.yaml"),
        "--start", START_EPOCH,
        "--log-level", "info",
        "--node-log-level", "warning",
    ]
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=log, text=True, env=env, cwd=str(ROOT), start_new_session=True
        )
    try:
        base_url, token = _console_address(process, log_path)
        running = Demo(process, base_url, log_path, token)
        _wait_nodes_online(running)
        yield running
    finally:
        pid = process.pid
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(pid, signal.SIGKILL)
                process.wait()
        deadline = time.monotonic() + 15.0
        leftovers = _children_of(pid)
        while leftovers and time.monotonic() < deadline:
            time.sleep(0.2)
            leftovers = _children_of(pid)
        for child in leftovers:  # never leave a node running, even when the check fails
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        if process.stdout is not None:
            process.stdout.close()
        text = log_path.read_text(encoding="utf-8", errors="replace")
    assert process.returncode == 0, f"the demo exited with {process.returncode}"
    assert not leftovers, f"node processes left running after the demo stopped: {leftovers}"
    assert "Traceback" not in text, "the server logged a traceback:\n" + _excerpt(text, "Traceback")
    # The runtime's own event-loop probe (every 100 ms), reported when it stops. The
    # budget is 50 ms; this machine is shared and runs seven node processes beside the
    # server, so the test fails only on gross blocking and reports the numbers.
    lag = re.search(r"event loop lag: worst (\d+) ms, (\d+) of (\d+) probes over 50 ms", text)
    assert lag is not None, "the runtime did not report its event-loop lag"
    worst_ms, over, probes = (int(g) for g in lag.groups())
    print(f"\n[demo] event loop lag: worst {worst_ms} ms, {over} of {probes} probes over 50 ms")
    assert worst_ms < 500 and over <= 0.05 * probes


def _console_address(process: subprocess.Popen[str], log_path: Path) -> tuple[str, str]:
    """The console's base URL and the API token its printed address carries."""
    assert process.stdout is not None
    deadline = time.monotonic() + STARTUP_S
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            raise AssertionError(f"the demo exited before serving:\n{log_path.read_text()[-4000:]}")
        match = re.search(r"Console:\s+(http://[^/\s]+)/\?token=(\S+)\s*$", line)
        if match:
            # After the banner the demo prints two short lines at most (nodes online,
            # stopped), far below a pipe's buffer, so nothing needs to read them.
            return match.group(1), match.group(2)
    raise AssertionError("the demo printed no console address")


def _wait_nodes_online(demo: Demo) -> None:
    deadline = time.monotonic() + STARTUP_S
    with httpx.Client(base_url=demo.base_url, timeout=10.0, headers=demo.headers) as client:
        while time.monotonic() < deadline:
            status = client.get(f"{API}/status").json()
            if status["nodes"] and all(n["online"] for n in status["nodes"]):
                return
            time.sleep(0.5)
    raise AssertionError("not every node came online")


def _excerpt(text: str, needle: str, before: int = 2000, after: int = 3000) -> str:
    at = text.find(needle)
    return text[max(0, at - before): at + after]


# --------------------------------------------------------------------------- console helpers


@dataclass
class ConsoleRecorder:
    """The console WebSocket, recorded: every text message with its arrival time and
    every binary spectrum frame of the subscribed windows (decoded)."""

    base_url: str
    token: str = ""
    psd: list[tuple[str, str]] = field(default_factory=list)
    events: dict[str, list[tuple[float, dict[str, Any]]]] = field(default_factory=dict)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    others: list[dict[str, Any]] = field(default_factory=list)
    spectra: list[tuple[Any, np.ndarray, np.ndarray | None]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ConsoleRecorder:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        url = self.base_url.replace("http://", "ws://") + "/ws/ui" + (f"?token={self.token}" if self.token else "")
        async with websockets.connect(url, max_size=32 * 2**20, compression=None) as ws:
            await ws.send(json.dumps({"type": "subscribe", "psd": [list(w) for w in self.psd]}))
            async for message in ws:
                now = time.monotonic()
                if isinstance(message, bytes):
                    header, payload = decode_frame(message)
                    self.spectra.append(decode_console_psd(header, payload))
                    continue
                if re.search(r"\bNaN\b|Infinity", message):
                    self.problems.append(f"non-finite number in {message[:120]}")
                data = json.loads(message)
                self.problems.extend(_ns_not_strings(data))
                if data["type"] == "event":
                    self.events.setdefault(data["event"]["id"], []).append((now, data["event"]))
                elif data["type"] == "status":
                    self.statuses.append(data)
                else:
                    self.others.append(data)


_NOT_A_TIME: frozenset[str] = frozenset({"est_offset_ns"})
"""``*_ns`` names that are not absolute times: a node's estimated clock offset is a small
signed number of nanoseconds (``ClockStatus``, PROTOCOL.md section A), safe as a number."""


def _ns_not_strings(obj: Any, path: str = "") -> list[str]:
    """Every absolute ``*_ns`` time that is not a decimal string (PROTOCOL.md section B)."""
    bad: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _NOT_A_TIME:
                continue
            if key.endswith("_ns") and value is not None and not (isinstance(value, str) and value.isdigit()):
                bad.append(f"{path}.{key} = {value!r}")
            bad.extend(_ns_not_strings(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            bad.extend(_ns_not_strings(value, f"{path}[{i}]"))
    return bad


async def _wait_for(check: Callable[[], Awaitable[Any]], timeout_s: float, what: str, every_s: float = 1.0) -> Any:
    """Poll ``check`` until it returns something truthy; fail naming ``what``."""
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(every_s)
    raise AssertionError(f"timed out after {timeout_s:.0f} s waiting for {what} (last: {last!r})")


async def _events(client: httpx.AsyncClient, state: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": 1000}
    if state is not None:
        params["state"] = state
    response = await client.get(f"{API}/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _event(client: httpx.AsyncClient, event_id: str) -> dict[str, Any]:
    response = await client.get(f"{API}/events/{event_id}")
    assert response.status_code == 200, response.text
    document = response.json()
    assert _ns_not_strings(document) == []
    return document


async def _settle_quiet(client: httpx.AsyncClient) -> None:
    """Back to the quiet world: load it, wait for every event to be released, close all."""
    response = await client.post(f"{API}/sim/presets/{START_SCENARIO}/load")
    assert response.status_code == 200, response.text

    async def released() -> bool:
        return not await _events(client, "active")

    await _wait_for(released, 60.0, "every event to be released in the quiet world")
    for summary in await _events(client):
        if summary["state"] != "closed":
            closed = await client.post(f"{API}/events/{summary['id']}/close")
            assert closed.status_code == 200 and closed.json()["state"] == "closed"


@contextlib.asynccontextmanager
async def _scenario(demo: Demo, name: str, psd: list[tuple[str, str]] | None = None):  # type: ignore[no-untyped-def]
    """Load a preset with a console attached; afterwards return to the quiet world."""
    async with httpx.AsyncClient(base_url=demo.base_url, timeout=60.0, headers=demo.headers) as client:
        await _settle_quiet(client)
        known = {s["id"] for s in await _events(client)}
        async with ConsoleRecorder(demo.base_url, token=demo.token, psd=psd or []) as console:
            loaded = await client.post(f"{API}/sim/presets/{name}/load")
            assert loaded.status_code == 200, loaded.text
            assert loaded.json()["scenario"]["name"] == name
            try:
                yield client, console, known
            finally:
                await _settle_quiet(client)
            assert console.problems == [], console.problems[:5]


async def _site_clocks(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """``/status`` ``site_clocks`` by site id."""
    response = await client.get(f"{API}/status")
    assert response.status_code == 200, response.text
    return {row["site_id"]: row for row in response.json()["site_clocks"]}


async def _new_events(client: httpx.AsyncClient, known: set[str]) -> list[dict[str, Any]]:
    return [s for s in await _events(client) if s["id"] not in known and s["state"] != "closed"]


def _t(transitions: list[dict[str, Any]], state: str | None = None, verdict: str | None = None) -> float | None:
    """Seconds from the first transition to the first one with this state / verdict."""
    t0 = int(transitions[0]["t_ns"])
    for t in transitions:
        if (state is None or t["state"] == state) and (verdict is None or t["verdict"] == verdict):
            return (int(t["t_ns"]) - t0) / NS
    return None


def _report(name: str, event: dict[str, Any]) -> None:
    """One line per scenario on stdout (shown with ``pytest -s``)."""
    solution = event.get("solution") or {}
    truth = event.get("truth") or {}
    print(
        f"\n[{name}] {event['id']} {event['state']} {event['verdict']} "
        f"verdict@{_t(event['transitions'], verdict=event['verdict'])}s located@{_t(event['transitions'], 'located')}s "
        f"rows={len(event['measurements'])} used={sum(m['used'] for m in event['measurements'])} "
        f"semi_major={solution.get('ellipse', {}).get('semi_major_m')} fix={solution.get('is_fix')} "
        f"miss={truth.get('miss_m')} "
        f"inside={truth.get('inside_ellipse')} class={(event.get('classification') or {}).get('label')} "
        f"tasking={(event.get('tasking') or {}).get('message')}"
    )


# --------------------------------------------------------------------------- fast smoke test


async def test_smoke(demo: Demo) -> None:
    """The stack as the browser console meets it: meta and marking, every node online,
    the status message on the socket within a second, presets, configuration with its
    ETag, the node backplane closed to strangers, and a quiet world without events."""
    async with httpx.AsyncClient(base_url=demo.base_url, timeout=30.0, headers=demo.headers) as client:
        meta = (await client.get(f"{API}/meta")).json()
        assert meta["simulation"] is True and meta["marking"]["level"] == "UNCLASSIFIED"
        assert meta["server_t_ns"].isdigit()
        # the simulated world runs from the fixed epoch, whatever the time of day
        assert 0 <= int(meta["server_t_ns"]) - ns_from_iso(START_EPOCH) < 2 * 3600 * NS
        assert (await client.get(f"{API}/status", headers={"Authorization": "Bearer wrong"})).status_code == 401
        async with ConsoleRecorder(demo.base_url, token=demo.token) as console:
            first = await _wait_for(lambda: _first(console.statuses), 5.0, "a status message", every_s=0.05)
        assert first["simulation"] is True and len(first["nodes"]) == 7
        assert all(n["online"] for n in first["nodes"]) and console.problems == []
        assert all(n["clock"]["source"] == "sim" for n in first["nodes"])  # simulated sites say so
        presets = {p["name"] for p in (await client.get(f"{API}/sim/presets")).json()}
        assert presets == {p.stem for p in SCENARIOS.glob("*.yaml")}
        config = await client.get(f"{API}/config")
        assert config.headers["etag"] == f'"{config.json()["version"]}"'
        assert (await client.get("/node/v1/sim")).status_code == 401
        assert (await client.get("/node/v1/sim", headers={"Authorization": "Bearer wrong"})).status_code == 401
        subpoints = (await client.get(f"{API}/geometry/subpoints")).json()
        assert {s["sat_id"] for s in subpoints} == {"muos-pac", "uhf-172e", "muos-io"}
        assert await _events(client, "active") == []
        index = await client.get("/")
        assert index.status_code == 200 and "text/html" in index.headers["content-type"]


async def _first(items: list[Any]) -> Any:
    return items[0] if items else None


# --------------------------------------------------------------------------- scenarios


@pytest.mark.slow
async def test_quiet_world_raises_no_event_and_carriers_stay_authorized(demo: Demo) -> None:
    """Only the reference carriers are on the air. They move by tens of hertz with the
    satellites' Doppler; neither they nor their edges may become an event."""
    windows = [("tokyo-pac", "muos-pac:shared-2"), ("seoul-pac", "muos-pac:shared-2"), ("honolulu-172e", "uhf-172e:shared-1")]
    async with httpx.AsyncClient(base_url=demo.base_url, timeout=30.0, headers=demo.headers) as client:
        await _settle_quiet(client)
        known = {s["id"] for s in await _events(client)}
        async with ConsoleRecorder(demo.base_url, token=demo.token, psd=windows) as console:
            await asyncio.sleep(40.0)
        assert await _new_events(client, known) == []
        assert console.events == {}, list(console.events)
        assert len(console.spectra) > 3 * 20 and console.problems == []
        for header, psd, baseline in console.spectra:
            assert (header.channel_id, header.monitor_id) in windows and psd.shape == (header.nfft,)
            assert np.isfinite(psd).all() and (baseline is None or np.isfinite(baseline).all())
            assert all(d.authorized_carrier_id is not None for d in header.detections), header.detections
        assert any(header.has_baseline for header, _, _ in console.spectra)


@pytest.mark.slow
async def test_pacific_demo_is_judged_uplink_and_located_with_truth_inside_the_ellipse(demo: Demo) -> None:
    async with _scenario(demo, "pacific-demo") as (client, console, known):

        async def located() -> list[dict[str, Any]]:
            return [s for s in await _new_events(client, known) if s["state"] == "located"]

        (summary,) = await _wait_for(located, 120.0, "the jammer to be located")
        event_id = summary["id"]
        assert summary["simulated"] is True and summary["verdict"] == "uplink"
        assert summary["headline"].startswith("Simulation: ")

        async def accumulated() -> dict[str, Any] | None:
            event = await _event(client, event_id)
            return event if (event["solution"] or {}).get("n_measurements", 0) >= 12 else None

        event = await _wait_for(accumulated, 90.0, "12 accepted measurements")
        # A configuration replaced while rounds are in flight: tasking restarts, the
        # event goes on (its detections are carried across the tracker rebuild).
        current = await client.get(f"{API}/config")
        envelope = current.json()
        body = {**envelope["config"], "sites": [
            {**site, "channels": [{**ch, "monitors": [] if ch["id"] in envelope["derived_monitors"] else ch["monitors"]}
                                  for ch in site["channels"]]}
            for site in envelope["config"]["sites"]
        ]}
        replaced = await client.put(f"{API}/config", json=body, headers={"If-Match": current.headers["etag"]})
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["version"] == envelope["version"] + 1
        stale = await client.put(f"{API}/config", json=body, headers={"If-Match": current.headers["etag"]})
        assert stale.status_code == 409 and stale.json()["current_version"] == envelope["version"] + 1
        n_before = len(event["measurements"])

        async def resumed() -> dict[str, Any] | None:
            again = await _event(client, event_id)
            return again if len(again["measurements"]) >= n_before + 4 else None

        event = await _wait_for(resumed, 60.0, "tasking to resume after the configuration change")
        _report("pacific-demo", event)
        assert event["state"] == "located" and event["verdict"] == "uplink"
        assert set(event["satellite_ids"]) == {"muos-pac", "uhf-172e", "muos-io"}
        transitions = event["transitions"]
        assert [t["state"] for t in transitions if t["state"] != "lost"][:1] == ["detected"]
        assert "lost" not in {t["state"] for t in transitions}
        verdict_s = _t(transitions, verdict="uplink")
        assert verdict_s is not None and verdict_s <= 5.0 + 10.0  # settle time plus a few frames
        located_s = _t(transitions, "located")
        assert located_s is not None and located_s <= 90.0
        truth = event["truth"]
        assert truth["emitter_id"] == "jammer-philsea" and truth["inside_ellipse"] is True
        assert truth["miss_m"] < 5_000.0
        solution = event["solution"]
        assert solution["converged"] and solution["ellipse"]["semi_major_m"] < 20_000.0
        assert solution["ambiguity_resolved_by"] in ("none", "region", "measurements")
        used = [m for m in event["measurements"] if m["used"]]
        assert used and all(m["calibrated"] and m["correction"] is not None for m in used)
        assert event["caf"]["detected"] and event["caf"]["snr_db"] > 20.0
        assert len(event["solution_history"]) >= 2
        assert event["solution_history"][-1]["semi_major_m"] <= event["solution_history"][0]["semi_major_m"]
        sim_truth = (await client.get(f"{API}/sim/truth")).json()
        (match,) = [e for e in sim_truth["events"] if e["event_id"] == event_id]
        assert match["emitter_id"] == "jammer-philsea" and match["inside_ellipse"] is True
        lop = (await client.get(f"{API}/events/{event_id}/lop")).json()
        assert lop["type"] == "FeatureCollection" and lop["features"]
        assert {f["properties"]["role"] for f in lop["features"]} <= {"line", "band"}
        # The console followed the event through its states.
        seen_states = [doc["state"] for _, doc in console.events[event_id]]
        assert "located" in seen_states and seen_states.index("located") > 0
        assert any(m.get("type") == "config" for m in console.others)
        # healthy sites: the calibrations flag no clock (R3)
        clocks = await _site_clocks(client)
        assert not any(row["caution"] for row in clocks.values()), clocks
        assert solution["is_fix"] is True


@pytest.mark.slow
async def test_uplink_cw_gives_fdoa_only_and_says_why_there_is_no_tdoa(demo: Demo) -> None:
    async with _scenario(demo, "uplink-cw") as (client, console, known):

        async def measured() -> dict[str, Any] | None:
            for summary in await _new_events(client, known):
                event = await _event(client, summary["id"])
                tdoa = [m for m in event["measurements"] if m["kind"] == "tdoa"]
                if len(tdoa) >= 6 and any(m["used"] for m in event["measurements"]):
                    return event
            return None

        event = await _wait_for(measured, 120.0, "FDOA measurements of the CW jammer")
        _report("uplink-cw", event)
        assert event["verdict"] == "uplink"
        assert (event["classification"] or {}).get("label") == "cw"
        tdoa = [m for m in event["measurements"] if m["kind"] == "tdoa"]
        fdoa = [m for m in event["measurements"] if m["kind"] == "fdoa"]
        assert all(not m["used"] and m["value"] is None and m["reason"] == "narrowband" for m in tdoa)
        assert any(m["used"] for m in fdoa)
        message = event["tasking"]["message"] or ""
        assert "No TDOA: the signal is too narrow" in message and "FDOA" in message
        assert event["caf"]["reason"] == "narrowband" and event["caf"]["peak_tdoa_s"] is None
        # R5: an FDOA-only solution from two rows has no redundancy (it once missed by 136 km
        # outside an 84 km ellipse): it is shown but is not a fix, never located or reported.
        # A fix (redundant, consistent, bounded) is long and thin, but holds the truth.
        solution = event["solution"]
        if solution is not None and solution["is_fix"]:
            assert event["truth"]["inside_ellipse"] is True
        elif solution is not None:
            assert event["state"] != "located" and solution["not_fix_reason"]
            assert not any("Report the estimated transmitter position" in a for a in event["advisory"]["actions"])
        if event["state"] == "located":
            assert solution is not None and solution["is_fix"] and solution["dof"] >= 1


@pytest.mark.slow
async def test_local_interferer_is_judged_local_and_never_located(demo: Demo) -> None:
    async with _scenario(demo, "local-interferer") as (client, console, known):

        async def judged() -> dict[str, Any] | None:
            for summary in await _new_events(client, known):
                if summary["verdict"] == "local":
                    return await _event(client, summary["id"])
            return None

        event = await _wait_for(judged, 60.0, "a local verdict")
        await asyncio.sleep(15.0)  # three more capture intervals: nothing may be tasked or solved
        event = await _event(client, event["id"])
        _report("local-interferer", event)
        assert event["verdict"] == "local" and event["state"] != "located"
        assert event["solution"] is None and event["solution_history"] == []
        assert not any(m["used"] for m in event["measurements"])
        seen = {r["site_id"] for r in event["evidence"] if r["state"] == "seen"}
        assert seen == {"busan"}
        far = [r for r in event["evidence"] if r["state"] == "not_seen_capable"]
        assert far and all(r["site_id"] != "busan" for r in far)
        assert "Local" in event["advisory"]["headline"] and "Busan" in event["advisory"]["headline"]
        tasking = event["tasking"]
        assert tasking is not None and tasking["state"] == "idle" and tasking["next_round_ns"] is None
        assert "local" in tasking["message"]
        others = [s for s in await _new_events(client, known) if s["id"] != event["id"]]
        assert others == []


@pytest.mark.slow
async def test_clock_fault_shows_the_calibration_at_work(demo: Demo) -> None:
    """Naha's clock runs 10 us late. Pairs through Naha carry it in their TDOA; the
    reference carrier, measured through the same two sites, carries it too, so the
    calibrated rows agree with the others while the raw ones do not. The test waits for
    a located event that also uses a rotated partner (a pair through Manila), so rows of
    several site pairs are mixed."""
    async with _scenario(demo, "clock-fault") as (client, console, known):

        async def through_manila() -> dict[str, Any] | None:
            for summary in await _new_events(client, known):
                if summary["state"] != "located":
                    continue
                event = await _event(client, summary["id"])
                if any(m["used"] and m["kind"] == "tdoa" and "manila" in (m["pair"]["a"]["site_id"], m["pair"]["b"]["site_id"])
                       for m in event["measurements"]):
                    return event
            return None

        event = await _wait_for(through_manila, 150.0, "a located event using a pair through Manila")
        config_text = (await client.get(f"{API}/config", params={"format": "yaml"})).text
        _report("clock-fault", event)
        truth = event["truth"]
        assert truth["emitter_id"] == "jammer-philsea" and truth["inside_ellipse"] is True
        used = [m for m in event["measurements"] if m["used"]]
        assert used and all(m["calibrated"] and m["reference_carrier_id"] for m in used)
        assert all(m["correction"] is not None and math.isfinite(m["correction"]) for m in used)
        # Solve the very rows the server used, with and without their corrections.
        cfg = loads_config(config_text, fmt="yaml", source="GET /config")
        f_e = float(round(0.5 * (event["uplink_lo_hz"] + event["uplink_hi_hz"])))

        def rows(calibrated: bool) -> list[Measurement]:
            out = []
            for m in used:
                ends = []
                for end in (m["pair"]["a"], m["pair"]["b"]):
                    transponder = next(t for t in cfg.satellite(end["sat_id"]).transponders if t.relays(f_e))
                    ends.append(PathSpec(end["sat_id"], end["site_id"], f_e, transponder.translation_hz))
                value = m["value"] if calibrated else m["value"] + m["correction"]
                out.append(Measurement(m["kind"], value, m["sigma"], int(m["t_ns"]), ends[0], ends[1]))
            return out

        def miss_and_fit(calibrated: bool) -> tuple[float, float]:
            solution, reason = solve(rows(calibrated), cfg.orbits(), cfg.sites_xyz(), cfg.processing.solver,
                                     cfg.processing.region, cfg.resolved_uncertainty())
            assert solution is not None, reason
            miss = float(geodesic_distance_m(solution.lat_deg, solution.lon_deg, truth["lat_deg"], truth["lon_deg"]))
            return miss, float(solution.chi2) / max(solution.dof, 1)

        raw_miss, raw_fit = miss_and_fit(False)
        print(f"[clock-fault] calibrated miss {truth['miss_m']:.0f} m; same rows uncorrected: miss {raw_miss:.0f} m, "
              f"reduced chi2 {raw_fit:.1f}")
        # How far the uncorrected rows land depends on how many of the mixed rows go
        # through Naha (time of day, partner rotation): 1.6 km and more has been seen live.
        # Whatever the mix, they must land well outside the calibrated fix's 95 % ellipse.
        semi_major = event["solution"]["ellipse"]["semi_major_m"]
        assert raw_miss > 2.0 * truth["miss_m"] and raw_miss > 2.0 * semi_major, (raw_miss, semi_major)

        # R3: the calibrations also show which clock is off. Same-satellite clock checks
        # separate Naha's clock from the satellite it is calibrated through.
        async def naha_flagged() -> dict[str, dict[str, Any]] | None:
            clocks = await _site_clocks(client)
            naha = clocks.get("naha")
            return clocks if naha is not None and naha["caution"] else None

        clocks = await _wait_for(naha_flagged, 90.0, "the Naha clock to be flagged in /status")
        naha = clocks["naha"]
        print(f"[clock-fault] site clocks: " + ", ".join(f"{k} {v['offset_us']:+.2f} us" for k, v in sorted(clocks.items())))
        assert naha["offset_us"] == pytest.approx(-10.0, abs=1.5)
        assert "Naha" in naha["message"] and "behind" in naha["message"]
        assert not any(row["caution"] for site, row in clocks.items() if site != "naha")


def _solve_used_rows(config_text: str, event: dict[str, Any], calibrated: bool) -> tuple[float, float, Any]:
    """Solve the rows the server used, with or without their reference corrections:
    (miss from the truth m, reduced chi-square, solution)."""
    cfg = loads_config(config_text, fmt="yaml", source="GET /config")
    f_e = float(round(0.5 * (event["uplink_lo_hz"] + event["uplink_hi_hz"])))
    truth = event["truth"]
    rows = []
    for m in (m for m in event["measurements"] if m["used"]):
        ends = []
        for end in (m["pair"]["a"], m["pair"]["b"]):
            transponder = next(t for t in cfg.satellite(end["sat_id"]).transponders if t.relays(f_e))
            ends.append(PathSpec(end["sat_id"], end["site_id"], f_e, transponder.translation_hz))
        value = m["value"] if calibrated else m["value"] + m["correction"]
        rows.append(Measurement(m["kind"], value, m["sigma"], int(m["t_ns"]), ends[0], ends[1]))
    solution, reason = solve(rows, cfg.orbits(), cfg.sites_xyz(), cfg.processing.solver, cfg.processing.region,
                             cfg.resolved_uncertainty())
    assert solution is not None, reason
    miss = float(geodesic_distance_m(solution.lat_deg, solution.lon_deg, truth["lat_deg"], truth["lon_deg"]))
    return miss, float(solution.chi2) / max(solution.dof, 1), solution


@pytest.mark.slow
async def test_ephemeris_error_is_removed_by_the_reference_calibration(demo: Demo) -> None:
    """MUOS Pacific is 8 km ahead and the 172 E satellite 6 km behind in-track while the
    server assumes the operator-grade 1 km. The Guam reference, 560 km from the jammer,
    carries nearly the same satellite errors: calibrated, the truth is inside the
    ellipse; the very same rows without their corrections land more than 10 km off. The
    part of the errors the calibration leaves outgrew the ellipse once tens of rows had
    averaged the noise down: the clock checks size an allowance for both satellites and
    a fix from 50 rows holds the truth too."""
    async with _scenario(demo, "ephemeris-error") as (client, console, known):

        async def located() -> dict[str, Any] | None:
            for summary in await _new_events(client, known):
                if summary["state"] != "located":
                    continue
                event = await _event(client, summary["id"])
                if (event["solution"] or {}).get("n_measurements", 0) >= 8:
                    return event
            return None

        event = await _wait_for(located, 150.0, "a located event with 8 accepted measurements")
        config_text = (await client.get(f"{API}/config", params={"format": "yaml"})).text
        _report("ephemeris-error", event)
        assert event["verdict"] == "uplink"
        truth = event["truth"]
        assert truth["emitter_id"] == "jammer-philsea" and truth["inside_ellipse"] is True
        assert truth["miss_m"] < 2_000.0
        used = [m for m in event["measurements"] if m["used"]]
        assert used and all(m["calibrated"] and m["reference_carrier_id"] == "ref-guam" for m in used)
        raw_miss, raw_fit, _ = _solve_used_rows(config_text, event, calibrated=False)
        print(f"[ephemeris-error] calibrated miss {truth['miss_m']:.0f} m; same rows uncorrected: miss {raw_miss:.0f} m, "
              f"reduced chi2 {raw_fit:.2f}")
        assert raw_miss > 10_000.0 and raw_miss > 5.0 * truth["miss_m"]
        # R3: satellites kilometres off must not be blamed on a healthy clock: no site is
        # called a clock fault with confidence, and any caution names the satellite too.
        clocks = await _site_clocks(client)
        print(f"[ephemeris-error] site clocks: " + ", ".join(
            f"{k} {v['offset_us']:+.2f} us{' CAUTION' if v['caution'] else ''}" for k, v in sorted(clocks.items())))
        assert clocks, "no site clock estimate from the calibrations"
        for site, row in clocks.items():
            assert "its clock is probably off" not in (row["message"] or ""), (site, row["message"])
            if row["caution"]:
                assert "or the position of" in row["message"] or "or the positions of" in row["message"], row

        # D1: the satellites the clock checks put off are named, the solves widen their
        # position sigma by a scale sized from the calibrations, and the ellipse keeps the
        # truth as the rows pile up (it used to fall outside from 46 rows on, the advice
        # only warning that the transmitter may lie outside)
        async def widened() -> dict[str, Any] | None:
            again = await _event(client, event["id"])
            solution = again.get("solution") or {}
            if again["state"] != "located" or solution.get("n_measurements", 0) < 50:
                return None
            detail = again["advisory"]["detail"]
            return again if "The ellipse is widened for the position" in detail else None

        later = await _wait_for(widened, 240.0, "a located fix from 50 rows whose ellipse is widened")
        _report("ephemeris-error later", later)
        suspects = {s for row in (await _site_clocks(client)).values() for s in row.get("suspect_satellite_ids", [])}
        assert suspects <= {"muos-pac", "uhf-172e"}, suspects
        assert later["truth"]["inside_ellipse"] is True, later["truth"]
        assert later["solution"]["ellipse"]["semi_major_m"] < 1_000.0  # widened, not blown up
        assert "may lie outside" not in later["advisory"]["detail"]
        assert "times larger, about" in later["advisory"]["detail"]  # the factor on the sigma and its size
        # and how much longer the 95 % ellipse's major axis is
        assert "Widened, the 95 % ellipse's major axis is" in later["advisory"]["detail"]
        assert any("the ellipse is widened for the ephemeris of" in a or "the ellipse is widened for the ephemerides of"
                   in a for a in later["advisory"]["actions"])
        assert any((t["reason"] or "").startswith("ellipse widened: the reference calibrations put the position")
                   for t in later["transitions"])
        assert "The ellipse is widened for the position" in later["tasking"]["message"]
        assert "than with the configured uncertainty" in later["tasking"]["message"]


@pytest.mark.slow
async def test_uplink_swept_is_ambiguous_and_gives_no_false_position(demo: Demo) -> None:
    async with _scenario(demo, "uplink-swept") as (client, console, known):

        async def correlated() -> dict[str, Any] | None:
            for summary in await _new_events(client, known):
                event = await _event(client, summary["id"])
                if len(event["measurements"]) >= 8:
                    return event
            return None

        event = await _wait_for(correlated, 90.0, "correlations of the swept jammer")
        _report("uplink-swept", event)
        assert event["verdict"] == "uplink" and (event["classification"] or {}).get("label") == "swept"
        assert event["solution"] is None and event["state"] != "located"
        assert not any(m["used"] for m in event["measurements"])
        assert {m["reason"] for m in event["measurements"]} <= {"ambiguous", "delay_doppler_coupled"}
        assert "The correlations are ambiguous" in (event["tasking"]["message"] or "")


@pytest.mark.slow
async def test_two_jammers_are_two_events_each_located_near_its_own(demo: Demo) -> None:
    async with _scenario(demo, "two-jammers") as (client, console, known):

        async def both_located() -> list[dict[str, Any]] | None:
            located = [s for s in await _new_events(client, known) if s["state"] == "located"]
            return located if len(located) >= 2 else None

        await _wait_for(both_located, 150.0, "both jammers to be located")
        events = [await _event(client, s["id"]) for s in await _new_events(client, known)]
        for event in events:
            _report("two-jammers", event)
        assert len(events) == 2
        assert {e["truth"]["emitter_id"] for e in events} == {"jammer-a", "jammer-b"}
        for event in events:
            assert event["verdict"] == "uplink" and event["truth"]["inside_ellipse"] is True
            assert event["truth"]["miss_m"] < 5_000.0
        lo = sorted((e["uplink_lo_hz"], e["uplink_hi_hz"]) for e in events)
        assert lo[0][1] < lo[1][0]  # separate ranges, not merged


@pytest.mark.slow
async def test_another_jammer_on_the_same_frequency_is_located_afresh(demo: Demo) -> None:
    """K2 / UX-5: the pacific-demo jammer (20 N 132 E) is located; then the world is
    replaced by ephemeris-error, whose jammer radiates the same signal on the same
    frequency from 17 N 141 E, with no break. The event must not mix the two: the rows
    measured before the swap are set aside (or a new event is raised), and the jammer is
    located afresh, near 17 N 141 E, with the truth inside the ellipse."""
    async with _scenario(demo, "pacific-demo") as (client, console, known):

        async def located() -> list[dict[str, Any]]:
            return [s for s in await _new_events(client, known) if s["state"] == "located"]

        (first,) = await _wait_for(located, 120.0, "the first jammer to be located")
        first_event = await _event(client, first["id"])
        assert first_event["truth"]["miss_m"] < 5_000.0
        swap_ns = int((await client.get(f"{API}/meta")).json()["server_t_ns"])
        loaded = await client.post(f"{API}/sim/presets/ephemeris-error/load")
        assert loaded.status_code == 200, loaded.text

        async def relocated() -> dict[str, Any] | None:
            for summary in await _new_events(client, known):
                event = await _event(client, summary["id"])
                solution, truth = event.get("solution"), event.get("truth") or {}
                if event["state"] != "located" or solution is None or event["solution_t_ns"] is None:
                    continue
                if int(event["solution_t_ns"]) <= swap_ns:
                    continue
                if truth.get("miss_m") is not None and truth["miss_m"] < 10_000.0:
                    return event
            return None

        event = await _wait_for(relocated, 180.0, "the second jammer to be located near 17 N 141 E")
        _report("another-jammer", event)
        solution = event["solution"]
        assert solution["fit_consistent"] is True
        assert geodesic_distance_m(solution["lat_deg"], solution["lon_deg"], 17.0, 141.0) < 10_000.0
        assert event["truth"]["inside_ellipse"] is True
        used = [m for m in event["measurements"] if m["used"]]
        assert used and all(int(m["t_ns"]) > swap_ns for m in used), "rows of the first jammer are still used"
        if event["id"] == first["id"]:
            assert int(event["onset_ns"]) > swap_ns - 10 * NS
            reasons = " | ".join(t.get("reason") or "" for t in event["transitions"])
            assert "another transmission" in reasons or "set aside" in reasons, reasons
            assert any(r["set_aside"] for r in event["solution_history"])
        # never a consistent position between the two jammers
        for _, doc in console.events.get(event["id"], []):
            sol = doc.get("solution")
            if sol and sol.get("fit_consistent", True) and doc["state"] == "located":
                nearest = min(
                    geodesic_distance_m(sol["lat_deg"], sol["lon_deg"], 20.0, 132.0),
                    geodesic_distance_m(sol["lat_deg"], sol["lon_deg"], 17.0, 141.0),
                )
                assert nearest < 20_000.0, sol
