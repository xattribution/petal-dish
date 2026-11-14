"""`sim`: a simulated radio, fed by the server's simulation backplane.

A node started with `--driver sim` hears the physics simulator instead of an antenna.
Each of its channels gets a `SimDriver`; all of them read from one `SimWorld`
(`petal_locate.sim`) built from the world the server publishes on the simulation
backplane of PROTOCOL.md section A:

    GET <server http origin><welcome.simulation.scenario_url>
    Authorization: Bearer <node token>
    -> {"version": int, "effective_ns": "<decimal string>", "config": SystemConfig,
        "scenario": Scenario}

Everything rendered is **simulated** and is labelled so by the server and console.

The pieces
----------
* `parse_backplane_document` and `build_world`: the document as typed objects, and the
  `SimWorld` for it. Pure functions, run in a worker thread (a world takes tens of
  milliseconds to build and must never hold the event loop).
* `WorldTimeline`: which world version is in force at each labelled time. Pure; no
  I/O, no asyncio.
* `SimWorldSource`: fetches documents (httpx, on the event loop), builds worlds (in a
  worker thread), keeps at most two of them in a `WorldTimeline`, retries failed
  fetches with exponential backoff and logs each failure. One source serves every sim
  driver of a node: drivers acquire it from a process-wide registry keyed by event
  loop, server origin, backplane path, site and token (or are handed one explicitly).
* `SimDriver`: the `SdrDriver` of one channel (ARCHITECTURE.md section 6).

Version switching
-----------------
The server announces every change of scenario or configuration with
`sim_update {version, effective_ns}`, `effective_ns` about 2 s ahead. The driver hook
`on_sim_update` fetches at once. Samples labelled at or after a version's
`effective_ns` come from that version, earlier ones from the version before it; a
capture is served whole from the world in force at its **first** sample, and one that
straddles a change says so in a log line. A read whose first sample falls after an
announced version that has not been fetched yet waits for it (at most `world_wait_s`).
At most two worlds are kept: once a third arrives the oldest is dropped, and an
interval that only it could serve is answered with `too_late`. A world in force within
`lookback_s` of the newest interval asked for is not dropped, though (two scenario
changes a moment apart announce two versions at once): it stays above the limit until
its successor has served for `lookback_s`, and at most `MAX_KEPT_WORLDS` are held. The first world a
source ever fetches also serves earlier times (there is nothing older to serve them
from). A version that was replaced on the server before this node could fetch it
cannot be rendered: intervals in its time are refused with `driver`. Opening a driver
(after `welcome` and for new channels of an `assign`) and the `on_assign` hook of a
kept channel both refetch before the node sends its `assign_ack`.

Truth errors whose ephemeris velocity offset has no `t_ref` grow from the version's
`effective_ns` (the `epoch_ns` of the `SimWorld`), which every node of the deployment
receives identically.

Time and samples
----------------
`read(t_start_ns, n, fs, fc)` returns exactly `SimWorld.render_iq(channel_id,
t_start_ns, n, fs, fc)` of the world in force at `t_start_ns`, and only once the node
clock has passed `t_start_ns + n / fs` (the samples would not exist earlier). Rendering
is deterministic, so any interval that started within the last `lookback_s` (30 s) is
served. `psd(...)` returns `SimWorld.render_psd` the same way and never None. Both
render in the node's `WorkerPool`.

Clock
-----
The simulated site does not know its own clock error (the truth offset lives in the
scenario), so a sim node reports `ClockStatus {source: "sim", locked: true,
est_offset_ns: 0}`: run the agent with `clock=sim_clock()`. Every capture is reported
clock-locked.
"""
from __future__ import annotations

import asyncio
import bisect
import hashlib
import importlib
import json
import logging
import math
import os
import random
import re
import ssl
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar

import numpy as np

from petal_locate.node.clock import ClockSource, SystemClock
from petal_locate.node.drivers.base import (
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    WorkerPool,
    duration_ns,
)
from petal_locate.protocol.messages import (
    MAX_CAPTURE_SAMPLES,
    MAX_PSD_BINS,
    MAX_TIME_NS,
    ChannelAssignment,
    ClockStatus,
)

if TYPE_CHECKING:  # heavy modules, imported in a worker thread at run time
    import httpx

    from petal_locate.config.models import SystemConfig
    from petal_locate.sim.scenario import Scenario
    from petal_locate.sim.world import SimWorld

__all__ = [
    "DEFAULT_CONCURRENT_READS",
    "DEFAULT_LOOKBACK_S",
    "MAX_DOCUMENT_BYTES",
    "MAX_KEPT_WORLDS",
    "MAX_WORLDS",
    "BackplaneDocument",
    "BackplaneError",
    "Lookup",
    "SimDriver",
    "SimWorldSource",
    "WorldTimeline",
    "WorldVersion",
    "active_sources",
    "backplane_url",
    "build_world",
    "channel_capabilities",
    "parse_backplane_document",
    "sim_clock",
    "sim_clock_status",
]

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_S: Final[float] = 30.0
"""How far into the past a read may start, seconds (ARCHITECTURE.md section 6)."""

DEFAULT_CONCURRENT_READS: Final[int] = 8
"""Reads served at once per channel. A read mostly waits for its interval to pass and
renders in the node's worker pool, so a capture waiting for its end must not hold up
the spectrum frames of the same channel."""

MAX_WORLDS: Final[int] = 2
"""World versions a source keeps: the one in force and the one before it (more while an
older one still serves the lookback: `WorldTimeline.note_read`)."""

MAX_KEPT_WORLDS: Final[int] = 8
"""World versions a source holds at most, whatever the lookback still needs."""

MAX_DOCUMENT_BYTES: Final[int] = 32 * 1024 * 1024
"""Largest backplane document accepted, bytes."""

_NS: Final[int] = 1_000_000_000
_DECIMAL_NS = re.compile(r"-?[0-9]{1,20}")
_RETRY_JITTER: Final[float] = 0.2
_T = TypeVar("_T")


class BackplaneError(Exception):
    """The simulation backplane did not deliver a usable world (HTTP failure, refused
    token, malformed document, scenario that does not fit its configuration)."""


# ------------------------------------------------------------------------------- clock


def sim_clock_status() -> ClockStatus:
    """The ClockStatus of a simulated site: source "sim", locked, offset estimate 0 ns
    (the true offset is a scenario truth error the site cannot know), no holdover."""
    return ClockStatus(source="sim", locked=True, est_offset_ns=0.0, holdover_s=0.0)


def sim_clock(offset_ns: int = 0) -> SystemClock:
    """The node clock of a simulated site: the host's UTC clock reporting
    `sim_clock_status()`. Pass it as `NodeAgent(clock=...)` with `driver="sim"`
    (`petal-locate node --driver sim` does).

    offset_ns: integer ns added to the host clock, for a simulated world run from a
    fixed epoch (`petal-locate demo --start`): the server's time base carries the same
    offset, so labelled times agree across the deployment. 0 (the default) is the host
    clock itself."""
    return SystemClock(source="sim", locked=True, est_offset_ns=0.0, offset_ns=offset_ns)


# ----------------------------------------------------------------- backplane document


@dataclass(frozen=True)
class BackplaneDocument:
    """One simulation backplane document.

    version: scenario version (server counter, increases with every change).
    effective_ns: labelled time from which this world applies, integer UTC ns.
    config: the server's `SystemConfig` (what the operator believes).
    scenario: the `Scenario` (what is true; simulated).
    """

    version: int
    effective_ns: int
    config: SystemConfig
    scenario: Scenario


def _decode_json(data: bytes | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, (bytes, bytearray, memoryview)):
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"document: {len(data)} bytes exceeds {MAX_DOCUMENT_BYTES}")
        text = bytes(data).decode("utf-8")
    elif isinstance(data, str):
        text = data
    else:
        raise ValueError(f"document: expected JSON text or an object, got {type(data).__name__}")
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"document: not valid JSON ({exc})") from None
    if not isinstance(obj, dict):
        raise ValueError(f"document: must be a JSON object, got {type(obj).__name__}")
    return obj


def _header(obj: Mapping[str, Any]) -> tuple[int, int]:
    """`version` and `effective_ns` of a decoded document; ValueError naming the field."""
    version = obj.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError(f"version: must be a non-negative integer, got {version!r}")
    raw = obj.get("effective_ns")
    if not isinstance(raw, str) or not _DECIMAL_NS.fullmatch(raw):
        raise ValueError(
            f"effective_ns: must be a decimal string of integer UTC nanoseconds, got {raw!r}"
        )
    effective_ns = int(raw)
    if not 0 <= effective_ns <= MAX_TIME_NS:
        raise ValueError(f"effective_ns: {effective_ns} is outside 0..2**63-1 ns")
    return version, effective_ns


def parse_backplane_document(data: bytes | str | Mapping[str, Any]) -> BackplaneDocument:
    """Validate a backplane document (PROTOCOL.md section A, simulation backplane).

    data: the HTTP body (UTF-8 JSON) or the decoded object.
    Returns the document with `config` and `scenario` as validated models; fields the
    protocol does not define are ignored. Raises `ValueError` naming the field at fault
    (`version`, `effective_ns`, `config.<path>`, `scenario.<path>`). The scenario is
    also checked against the configuration (`Scenario.validate_against`).
    Imports the configuration and simulation packages: call it from a worker thread.
    """
    from pydantic import ValidationError

    from petal_locate.config.models import SystemConfig, field_errors
    from petal_locate.sim.scenario import Scenario, ScenarioError

    obj = _decode_json(data)
    version, effective_ns = _header(obj)
    for name in ("config", "scenario"):
        if not isinstance(obj.get(name), dict):
            raise ValueError(f"{name}: must be a JSON object, got {type(obj.get(name)).__name__}")
    try:
        config = SystemConfig.model_validate(obj["config"])
    except ValidationError as exc:
        problems = "; ".join(f"config.{p['loc']}: {p['msg']}" for p in field_errors(exc)[:5])
        raise ValueError(problems or "config: invalid") from None
    try:
        scenario = Scenario.model_validate(obj["scenario"])
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        loc = ".".join(str(part) for part in first["loc"]) or "(root)"
        raise ValueError(f"scenario.{loc}: {first['msg']}") from None
    try:
        scenario.validate_against(config)
    except ScenarioError as exc:
        raise ValueError(f"scenario.{exc}") from None
    return BackplaneDocument(
        version=version, effective_ns=effective_ns, config=config, scenario=scenario
    )


def build_world(document: BackplaneDocument, previous: SimWorld | None = None) -> SimWorld:
    """The `SimWorld` of a document, with `epoch_ns = document.effective_ns`.

    previous: a world of an earlier version; its unchanged waveforms (and their block
    caches) are reused through `SimWorld.rebuild`, which renders identical samples.
    Raises `ValueError` (a `ScenarioError`) naming the field when the world cannot be
    built. CPU-bound (tens of milliseconds): call it from a worker thread.
    """
    from petal_locate.sim.world import SimWorld

    if previous is not None:
        return previous.rebuild(
            document.config, document.scenario, epoch_ns=document.effective_ns
        )
    return SimWorld(document.config, document.scenario, epoch_ns=document.effective_ns)


# ----------------------------------------------------------------------- the timeline


@dataclass(frozen=True, eq=False)
class WorldVersion:
    """A world version a node holds.

    version: scenario version of the backplane document.
    effective_ns: labelled time from which it applies, integer UTC ns.
    world: the `SimWorld` built from it (immutable, thread-safe).
    """

    version: int
    effective_ns: int
    world: SimWorld

    @property
    def config(self) -> SystemConfig:
        """The configuration of this version."""
        return self.world.config


@dataclass(frozen=True)
class Lookup:
    """Which world serves a labelled time (`WorldTimeline.lookup`).

    kind: "world" (`world` serves it), "pending" (version `version`, in force from
        `effective_ns`, is announced and not fetched yet), "superseded" (version
        `version` was in force then but was replaced before it could be fetched),
        "gone" (only a world that is no longer held could serve it; `effective_ns` is
        the earliest time still served) or "none" (no world held at all).
    world: the world, for kind "world".
    version, effective_ns: see `kind`.
    next_effective_ns: for kind "world", when the next known version takes over (None
        when none is known), so a caller can tell whether an interval straddles it.
    before_first: for kind "world", the time is earlier than the `effective_ns` of the
        first world this source ever held, which therefore serves it as well.
    """

    kind: Literal["world", "pending", "superseded", "gone", "none"]
    world: WorldVersion | None = None
    version: int | None = None
    effective_ns: int | None = None
    next_effective_ns: int | None = None
    before_first: bool = False


class WorldTimeline:
    """The world versions a node holds and announced versions it still has to fetch.

    Pure bookkeeping: no I/O, no asyncio, not thread-safe (use it from one thread).
    Versions are expected to increase with `effective_ns`; a newer version that takes
    effect no later than an older one cancels the older one.

    max_worlds: how many built worlds are kept (at least 1).
    keep_ns: how far back from the newest time read (`note_read`) intervals must stay
        servable, ns: a world in force then is kept above `max_worlds` (never above
        `MAX_KEPT_WORLDS`). Without a read nothing is kept above `max_worlds`.
    """

    def __init__(self, max_worlds: int = MAX_WORLDS, keep_ns: int = 0) -> None:
        if isinstance(max_worlds, bool) or not isinstance(max_worlds, int) or max_worlds < 1:
            raise ValueError(f"max_worlds: must be an integer of at least 1, got {max_worlds!r}")
        if isinstance(keep_ns, bool) or not isinstance(keep_ns, int) or keep_ns < 0:
            raise ValueError(f"keep_ns: must be an integer of at least 0, got {keep_ns!r}")
        self._max = max_worlds
        self._keep_ns = keep_ns
        self._held: list[WorldVersion] = []  # ascending effective_ns and version
        self._announced: dict[int, int] = {}  # version -> effective_ns, not held
        self._required = -1  # a version known to exist, time unknown (welcome)
        self._floor_ns: int | None = None
        self._latest_ns: int | None = None  # the newest labelled time read (note_read)

    @property
    def held(self) -> tuple[WorldVersion, ...]:
        """The worlds held, oldest first."""
        return tuple(self._held)

    @property
    def newest(self) -> WorldVersion | None:
        """The newest world held, or None."""
        return self._held[-1] if self._held else None

    @property
    def floor_ns(self) -> int | None:
        """Earliest labelled time still served, UTC ns; None while no world was dropped."""
        return self._floor_ns

    def wanted_version(self) -> int:
        """Highest version announced or required, -1 when none."""
        return max([self._required, *self._announced])

    def needs_fetch(self) -> bool:
        """Whether a fetch is due: no world held, or a newer version is known to exist."""
        newest = self.newest
        return newest is None or self.wanted_version() > newest.version

    def require(self, version: int) -> bool:
        """Note that `version` exists (its effective time unknown, as in `welcome`).
        Returns whether this makes a fetch due."""
        self._required = max(self._required, int(version))
        return self.needs_fetch()

    def announce(self, version: int, effective_ns: int) -> bool:
        """Record a `sim_update`. Returns whether the version is not held yet.

        A version older than the oldest held one is ignored; one that is held keeps
        the `effective_ns` of its document.
        """
        version, effective_ns = int(version), int(effective_ns)
        if any(h.version == version for h in self._held):
            return False
        if self._held and version < self._held[0].version:
            return False
        self._announced[version] = effective_ns
        return True

    def install(self, entry: WorldVersion) -> bool:
        """Add a world. Returns False (and changes nothing) unless it is newer than
        every world held. Worlds and announcements that would only take effect at or
        after it but are older are dropped (they never take effect). When more than
        `max_worlds` remain, the oldest is dropped and the floor moves to the
        `effective_ns` of the next one, except that a world in force within `keep_ns`
        of the newest time read (`note_read`) is kept: two versions announced a moment
        apart must not push out the world serving now and the lookback."""
        if self._held and entry.version <= self._held[-1].version:
            return False
        self._held = [h for h in self._held if h.effective_ns < entry.effective_ns]
        self._held.append(entry)
        self._announced = {
            v: e
            for v, e in self._announced.items()
            if v != entry.version and not (v < entry.version and e >= entry.effective_ns)
        }
        self._trim()
        return True

    def note_read(self, t_ns: int) -> None:
        """Record that a reader asked for labelled time `t_ns` (UTC ns). A world in force
        within `keep_ns` before the newest such time is not dropped; one kept above
        `max_worlds` is dropped once its successor took effect `keep_ns` before it."""
        t_ns = int(t_ns)
        if self._latest_ns is None or t_ns > self._latest_ns:
            self._latest_ns = t_ns
            self._trim()

    def _trim(self) -> None:
        limit = None if self._latest_ns is None else self._latest_ns - self._keep_ns
        while len(self._held) > self._max and (
            limit is None or self._held[1].effective_ns <= limit or len(self._held) > MAX_KEPT_WORLDS
        ):
            self._held.pop(0)
            oldest = self._held[0]
            self._floor_ns = oldest.effective_ns
            self._announced = {v: e for v, e in self._announced.items() if v > oldest.version}

    def lookup(self, t_ns: int) -> Lookup:
        """Which world serves labelled time `t_ns` (UTC ns); see `Lookup`."""
        if not self._held and not self._announced:
            return Lookup("none")
        if self._floor_ns is not None and t_ns < self._floor_ns:
            return Lookup("gone", effective_ns=self._floor_ns)
        entries: list[tuple[int, int, WorldVersion | None]] = [
            (h.effective_ns, h.version, h) for h in self._held
        ]
        entries += [(e, v, None) for v, e in self._announced.items()]
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        index = bisect.bisect_right([entry[0] for entry in entries], t_ns) - 1
        if index < 0:
            if not self._held:
                return Lookup("none")
            first = self._held[0]
            later = [e for e, _, w in entries if w is not first and e > t_ns]
            return Lookup(
                "world",
                world=first,
                next_effective_ns=min(later) if later else None,
                before_first=True,
            )
        effective_ns, version, held = entries[index]
        following = entries[index + 1][0] if index + 1 < len(entries) else None
        if held is not None:
            return Lookup("world", world=held, next_effective_ns=following)
        newest = self.newest
        if newest is not None and newest.version > version:
            return Lookup("superseded", version=version, effective_ns=effective_ns)
        return Lookup("pending", version=version, effective_ns=effective_ns)


# ----------------------------------------------------------------------- capabilities


def channel_capabilities(
    config: SystemConfig,
    channel_id: str,
    *,
    lookback_s: float = DEFAULT_LOOKBACK_S,
    concurrent_reads: int = DEFAULT_CONCURRENT_READS,
    tuning_margin_hz: float | None = None,
    monitors: Iterable[Any] = (),
) -> Capabilities:
    """What a simulated radio on `channel_id` can do, from the configuration.

    The centre-frequency range covers the downlink passband of every transponder of
    every satellite the channel watches, and every monitor window (the configured
    ones and `monitors`, objects with `center_hz` and `span_hz`), widened by
    `tuning_margin_hz` on both sides (default: the channel's `max_sample_rate_hz`) and
    clipped at 0 Hz. `max_sample_rate_hz` is the channel's. All in Hz.
    Raises `ValueError` naming the channel when the configuration does not have it.
    """
    _, channel = config.channel(channel_id)
    rate = float(channel.max_sample_rate_hz)
    margin = rate if tuning_margin_hz is None else float(tuning_margin_hz)
    if not math.isfinite(margin) or margin < 0:
        raise ValueError(f"tuning_margin_hz: must be finite and not negative, got {margin!r}")
    edges: list[float] = []
    for sat_id in channel.watched_satellite_ids:
        for transponder in config.satellite(sat_id).transponders:
            edges += [float(transponder.downlink_lo_hz), float(transponder.downlink_hi_hz)]
    for window in [*channel.monitors, *monitors]:
        half = 0.5 * float(window.span_hz)
        edges += [float(window.center_hz) - half, float(window.center_hz) + half]
    if not edges:
        raise ValueError(f"channel_id {channel_id!r}: watches no transponder and has no monitor")
    return Capabilities(
        min_hz=max(0.0, min(edges) - margin),
        max_hz=max(edges) + margin,
        max_sample_rate_hz=rate,
        lookback_s=float(lookback_s),
        concurrent_reads=int(concurrent_reads),
    )


# ------------------------------------------------------------------------- the source


def backplane_url(server_http_url: str, scenario_url: str) -> str:
    """The URL of the backplane document: the server's HTTP origin plus the path that
    `welcome.simulation.scenario_url` names. Raises `ValueError` naming the argument
    for an origin that is not `http(s)://host[:port]` or a path that is not absolute
    on that origin."""
    if not isinstance(server_http_url, str) or not re.fullmatch(
        r"https?://[^/?#\s]+", server_http_url.rstrip("/")
    ):
        raise ValueError(f"server_http_url: must be http(s)://host[:port], got {server_http_url!r}")
    if (
        not isinstance(scenario_url, str)
        or not scenario_url.startswith("/")
        or scenario_url.startswith("//")
        or any(ch.isspace() for ch in scenario_url)
    ):
        raise ValueError(
            f"scenario_url: must be an absolute path on the server origin, got {scenario_url!r}"
        )
    return server_http_url.rstrip("/") + scenario_url


def _describe(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, (BackplaneError, ValueError)) and text:
        return text
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _status_hint(status: int) -> str:
    if status in (401, 403):
        return " (the server refused the node token)"
    if status == 404:
        return " (no simulation document there: is the server running a scenario?)"
    return ""


_HTTP_WARM_MODULES: Final[tuple[str, ...]] = (
    "anyio",
    "anyio._backends._asyncio",
    "httpcore._backends.anyio",
)
"""Modules httpx otherwise imports on the event loop during its first request (about
60 ms here). Private module names: importing them is only a warm-up, so one that a
later release renames is skipped and then simply loads during the first request."""


def _make_http_client(options: dict[str, Any]) -> httpx.AsyncClient:
    """Worker-thread job: import httpx and the async backend it loads lazily, and build
    the client (its constructor builds a TLS context: about 60 ms). Nothing of it binds
    to a thread or loop: httpcore creates its locks on first use."""
    module = importlib.import_module("httpx")
    verify = options.get("verify")
    if isinstance(verify, str):  # httpx deprecates a path here; build the context ourselves
        if os.path.isdir(verify):
            options = {**options, "verify": ssl.create_default_context(capath=verify)}
        else:
            options = {**options, "verify": ssl.create_default_context(cafile=verify)}
    for name in _HTTP_WARM_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            logger.debug("httpx warm-up module %s not found; it loads on first use", name)
    return module.AsyncClient(**options)  # type: ignore[no-any-return]


def _decode_and_build(
    body: bytes, previous: SimWorld | None, newest_version: int
) -> tuple[int, int, WorldVersion | None]:
    """Worker-thread job: `(version, effective_ns, world or None)` of a document body;
    the world is built only when the version is newer than `newest_version`."""
    obj = _decode_json(body)
    version, effective_ns = _header(obj)
    if version <= newest_version:
        return version, effective_ns, None
    document = parse_backplane_document(obj)
    world = build_world(document, previous)
    return version, effective_ns, WorldVersion(version, effective_ns, world)


class SimWorldSource:
    """The simulated world of one node, fetched from the simulation backplane.

    url: the backplane document's URL (`backplane_url`).
    token: the site's bearer token. Never logged, kept out of `repr`.
    workers: where documents are decoded and worlds built; None makes a private pool
        of one thread.
    proxy: as `NodeAgent`: True honours the environment's proxy settings, None uses no
        proxy, a URL names one.
    verify: TLS verification of an https origin: True (system / certifi roots),
        False, a CA bundle file or directory path, or an `ssl.SSLContext`.
    transport: an httpx transport to use instead of the network (tests).
    fetch_timeout_s: limit of one HTTP request, seconds.
    retry_base_s, retry_max_s: the wait before retry k after consecutive failures is
        `min(retry_base_s * 2**(k-1), retry_max_s)` plus up to 20 % jitter, seconds.
        An explicit request (`refresh`, `announce`) cuts the wait short.
    max_worlds: world versions kept (2).
    lookback_s: how far back from the newest time asked for intervals stay servable
        across version changes, seconds (`WorldTimeline` keep_ns).
    jitter_key: seeds the retry jitter (the site id), so that the nodes of a
        deployment do not retry in step and every run retries alike.

    Use it from one event loop. Fetches run in one background task, started on first
    use; concurrent requests that arrive before a fetch starts share it.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        workers: WorkerPool | None = None,
        proxy: str | Literal[True] | None = True,
        verify: bool | str | ssl.SSLContext = True,
        transport: httpx.AsyncBaseTransport | None = None,
        fetch_timeout_s: float = 10.0,
        retry_base_s: float = 0.5,
        retry_max_s: float = 15.0,
        max_worlds: int = MAX_WORLDS,
        lookback_s: float = DEFAULT_LOOKBACK_S,
        jitter_key: str = "",
    ) -> None:
        if not isinstance(url, str) or not re.match(r"https?://", url):
            raise ValueError(f"url: must be an http(s) URL, got {url!r}")
        if not isinstance(token, str):
            raise ValueError("token: must be a string")
        if not (proxy is None or proxy is True or isinstance(proxy, str)):
            raise ValueError(f"proxy: must be True, None or a proxy URL, got {proxy!r}")
        if not isinstance(verify, (bool, str, ssl.SSLContext)):
            raise ValueError(f"verify: must be a bool, a CA path or an ssl.SSLContext, got {verify!r}")
        for name, value in (
            ("fetch_timeout_s", fetch_timeout_s),
            ("retry_base_s", retry_base_s),
            ("retry_max_s", retry_max_s),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
                math.isfinite(value) and value > 0
            ):
                raise ValueError(f"{name}: must be a positive number of seconds, got {value!r}")
        if retry_max_s < retry_base_s:
            raise ValueError(f"retry_max_s: {retry_max_s!r} is below retry_base_s {retry_base_s!r}")
        self.url: str = url
        self._token = token
        self._workers = workers
        self._proxy: str | Literal[True] | None = proxy
        self._verify = verify
        self._transport = transport
        self._fetch_timeout_s = float(fetch_timeout_s)
        self._retry = (float(retry_base_s), float(retry_max_s))
        seed = hashlib.blake2b(f"sim-retry|{jitter_key}".encode(), digest_size=8).digest()
        self._rng = random.Random(int.from_bytes(seed, "little"))
        if isinstance(lookback_s, bool) or not isinstance(lookback_s, (int, float)) or not (
            math.isfinite(lookback_s) and lookback_s >= 0
        ):
            raise ValueError(f"lookback_s: must be finite and not negative, got {lookback_s!r}")
        self._timeline = WorldTimeline(max_worlds, keep_ns=round(lookback_s * _NS))
        self._requested = 0  # explicit fetch requests made
        self._attempted = 0  # highest request covered by a finished attempt
        self._completed = 0  # highest request covered by a successful attempt
        self._failures = 0  # consecutive failed (or stale) attempts
        self._last_error: str | None = None
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._closed = False
        self._noted_before_first: set[int] = set()
        self.attempts: int = 0
        """HTTP fetches started (for logs and tests)."""
        self.failures: int = 0
        """Fetches that failed or returned an older version than announced."""

    def __repr__(self) -> str:
        newest = self._timeline.newest
        return (
            f"SimWorldSource(url={self.url!r}, versions="
            f"{[h.version for h in self._timeline.held]}, newest="
            f"{None if newest is None else newest.version})"
        )

    # ------------------------------------------------------------------ state

    @property
    def newest(self) -> WorldVersion | None:
        """The newest world held, or None before the first successful fetch."""
        return self._timeline.newest

    @property
    def versions(self) -> tuple[WorldVersion, ...]:
        """The worlds held, oldest first (at most `max_worlds`, more while an older one
        still serves intervals within the lookback of the newest asked for)."""
        return self._timeline.held

    @property
    def last_error(self) -> str | None:
        """Why the most recent fetch failed, or None after a success."""
        return self._last_error

    @property
    def closed(self) -> bool:
        """Whether `aclose` has been called."""
        return self._closed

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def _wait_changed(self, timeout_s: float) -> None:
        changed = self._changed
        try:
            async with asyncio.timeout(max(0.0, timeout_s)):
                await changed.wait()
        except TimeoutError:
            pass

    def _check_open(self) -> None:
        if self._closed:
            raise BackplaneError("the simulated world source is closed")

    def _ensure_task(self) -> None:
        """Start the fetch loop, or restart it after an unexpected death (logged)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="petal-node:sim-backplane"
            )
            self._task.add_done_callback(self._task_ended)

    def _task_ended(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()  # also marks it retrieved
        if exc is not None:
            self._last_error = f"fetch loop failed: {_describe(exc)}"
            logger.error(
                "simulation backplane fetch loop failed; it restarts on the next request",
                exc_info=exc,
                extra={"backplane_url": self.url},
            )
            self._notify()

    def _request_fetch(self) -> int:
        self._check_open()
        self._requested += 1
        self._ensure_task()
        self._wake.set()
        return self._requested

    # ------------------------------------------------------------------ requests

    def require_version(self, version: int) -> None:
        """Note that the server holds at least `version` (the `welcome`'s
        `simulation.version`): fetches are retried until a document that new arrives."""
        self._check_open()
        if self._timeline.require(version):
            self._ensure_task()
            self._wake.set()

    async def refresh(self, timeout_s: float) -> tuple[WorldVersion, str | None]:
        """Fetch the document anew (a request that starts after this call) and wait.

        timeout_s: how long to wait, seconds.
        Returns `(newest world, None)` after a successful fetch, or `(newest world,
        reason)` when the fetch failed (or did not finish in time) but a world is held,
        which then stays in use while fetches are retried. Raises `BackplaneError` when
        no world is held by the end of `timeout_s` (fetches go on being retried).
        """
        mine = self._request_fetch()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self._closed:
            newest = self._timeline.newest
            if newest is not None:
                if self._completed >= mine and not self._timeline.needs_fetch():
                    return newest, None
                if self._attempted >= mine:
                    return newest, self._last_error or "the refetch returned no newer world"
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await self._wait_changed(remaining)
        self._check_open()
        newest = self._timeline.newest
        reason = self._last_error or "no answer from the simulation backplane"
        if newest is None:
            raise BackplaneError(
                f"no simulated world from {self.url} within {timeout_s:g} s: {reason}"
            )
        return newest, f"refetch did not finish within {timeout_s:g} s ({reason})"

    async def announce(self, version: int, effective_ns: int, timeout_s: float) -> bool:
        """Take a `sim_update`: fetch at once unless the version is already held.

        version, effective_ns: as announced (effective_ns in labelled UTC ns).
        timeout_s: how long to wait for the version, seconds.
        Returns True once that version (or a newer one) is held, False when it is not
        by the end of `timeout_s`; fetching then goes on in the background, and reads
        after `effective_ns` wait for it.
        """
        self._check_open()
        newest = self._timeline.newest
        if self._timeline.announce(version, effective_ns) and (
            newest is None or version > newest.version
        ):
            self._request_fetch()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self._closed:
            newest = self._timeline.newest
            if newest is not None and newest.version >= version:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await self._wait_changed(remaining)
        return False

    async def world_at(self, t_ns: int, wait_s: float) -> Lookup:
        """The world in force at labelled time `t_ns` (UTC ns).

        Waits up to `wait_s` seconds when that time belongs to an announced version
        that is still being fetched. Returns a `Lookup` of kind "world". Raises
        `CaptureError`: "too_late" when only a world that is no longer held could
        serve the time; "driver" when no world is held, when the version in force
        then was replaced before it could be fetched, or when the announced version
        did not arrive within `wait_s`.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        self._timeline.note_read(t_ns)  # worlds serving the lookback of this time are kept
        while True:
            if self._closed:
                raise CaptureError("driver", "the simulated world source is closed")
            found = self._timeline.lookup(t_ns)
            if found.kind == "world":
                assert found.world is not None
                if found.before_first and found.world.version not in self._noted_before_first:
                    self._noted_before_first.add(found.world.version)
                    logger.info(
                        "interval before the first simulated world fetched by this node; "
                        "served from version %d",
                        found.world.version,
                        extra={"sim_version": found.world.version, "t_ns": t_ns},
                    )
                return found
            if found.kind == "gone":
                raise CaptureError(
                    "too_late",
                    f"the simulated world in force at {t_ns} ns is no longer held; the "
                    f"oldest world held applies from {found.effective_ns} ns",
                )
            if found.kind == "superseded":
                raise CaptureError(
                    "driver",
                    f"simulated world version {found.version}, in force from "
                    f"{found.effective_ns} ns, was replaced on the server before this node "
                    "could fetch it",
                )
            error = f": {self._last_error}" if self._last_error else ""
            if found.kind == "none":
                raise CaptureError("driver", f"no simulated world has been fetched yet{error}")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CaptureError(
                    "driver",
                    f"simulated world version {found.version}, in force from "
                    f"{found.effective_ns} ns, was not fetched within {wait_s:g} s{error}",
                )
            self._ensure_task()  # the fetch is due; make sure something is fetching
            await self._wait_changed(remaining)

    # ------------------------------------------------------------------ fetching

    def _due(self) -> bool:
        return self._requested > self._attempted or self._timeline.needs_fetch()

    def _retry_delay(self) -> float:
        base, cap = self._retry
        delay = min(cap, base * 2.0 ** min(self._failures - 1, 62))
        return delay * (1.0 + _RETRY_JITTER * self._rng.random())

    async def _run(self) -> None:
        """The fetch loop: one fetch at a time, retried with backoff while due."""
        while not self._closed:
            if not self._due():
                self._wake.clear()
                await self._wake.wait()
                continue
            seq = self._requested
            self._wake.clear()
            ok = await self._attempt(seq)
            if not ok and self._due() and not self._closed:
                try:
                    async with asyncio.timeout(self._retry_delay()):
                        await self._wake.wait()  # an explicit request cuts the wait short
                except TimeoutError:
                    pass

    async def _attempt(self, seq: int) -> bool:
        """One fetch covering requests up to `seq`; returns whether it was good."""
        self.attempts += 1
        try:
            version, effective_ns, entry = await self._fetch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Deliberately broad: network errors, HTTP errors, malformed documents and
            # worlds that cannot be built all mean "no new world"; the loop retries.
            self._failures += 1
            self.failures += 1
            self._last_error = _describe(exc)
            self._attempted = max(self._attempted, seq)
            logger.warning(
                "simulation backplane fetch failed (attempt %d in a row): %s",
                self._failures,
                self._last_error,
                extra={"backplane_url": self.url, "failures": self._failures},
            )
            self._notify()
            return False
        self._attempted = max(self._attempted, seq)
        self._completed = max(self._completed, seq)
        if entry is not None and self._timeline.install(entry):
            logger.info(
                "simulated world version %d fetched, in force from %d ns",
                entry.version,
                entry.effective_ns,
                extra={
                    "sim_version": entry.version,
                    "effective_ns": entry.effective_ns,
                    "held_versions": [h.version for h in self._timeline.held],
                },
            )
        good = not self._timeline.needs_fetch()
        if good:
            self._failures = 0
            self._last_error = None
        else:
            self._failures += 1
            self.failures += 1
            self._last_error = (
                f"the server returned version {version} but version "
                f"{self._timeline.wanted_version()} was announced"
            )
            logger.warning(
                "simulation backplane returned an older version than announced: %s",
                self._last_error,
                extra={"backplane_url": self.url, "sim_version": version},
            )
        self._notify()
        return good

    async def _fetch(self) -> tuple[int, int, WorldVersion | None]:
        client = await self._http()
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        async with client.stream("GET", self.url, headers=headers) as response:
            if response.status_code != 200:
                raise BackplaneError(
                    f"GET {self.url} returned HTTP {response.status_code}"
                    f"{_status_hint(response.status_code)}"
                )
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > MAX_DOCUMENT_BYTES:
                raise BackplaneError(f"document of {declared} bytes exceeds {MAX_DOCUMENT_BYTES}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_DOCUMENT_BYTES:
                    raise BackplaneError(f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
        newest = self._timeline.newest
        return await self._run_job(
            _decode_and_build,
            bytes(body),
            None if newest is None else newest.world,
            -1 if newest is None else newest.version,
        )

    async def _run_job(self, fn: Callable[..., _T], *args: Any) -> _T:
        if self._workers is None:
            self._workers = WorkerPool(1)
        return await self._workers.run(fn, *args)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            options: dict[str, Any] = {
                "timeout": self._fetch_timeout_s,
                "follow_redirects": False,
                "verify": self._verify,
            }
            if self._transport is not None:
                options["transport"] = self._transport
            elif isinstance(self._proxy, str):
                options.update(proxy=self._proxy, trust_env=False)
            else:
                options["trust_env"] = self._proxy is True
            client = await asyncio.to_thread(_make_http_client, options)
            if self._client is None:  # only the fetch task calls this; kept for safety
                self._client = client
            else:
                await client.aclose()
        assert self._client is not None
        return self._client

    async def aclose(self) -> None:
        """Stop fetching and release the HTTP client. Idempotent. Waiters fail."""
        if self._closed:
            return
        self._closed = True
        self._notify()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


# ------------------------------------------------------------------ shared sources


@dataclass(eq=False)
class _Shared:
    source: SimWorldSource
    refs: int


class _Registry:
    """The sources shared by the sim drivers of a process, per event loop."""

    def __init__(self) -> None:
        self._by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[tuple[str, ...], _Shared]
        ] = weakref.WeakKeyDictionary()

    def acquire(
        self, key: tuple[str, ...], factory: Callable[[], SimWorldSource]
    ) -> SimWorldSource:
        shared = self._by_loop.setdefault(asyncio.get_running_loop(), {})
        entry = shared.get(key)
        if entry is None:
            entry = shared[key] = _Shared(factory(), 0)
        entry.refs += 1
        return entry.source

    async def release(self, source: SimWorldSource) -> None:
        shared = self._by_loop.get(asyncio.get_running_loop(), {})
        for key, entry in list(shared.items()):
            if entry.source is source:
                entry.refs -= 1
                if entry.refs <= 0:
                    del shared[key]  # before awaiting, so a new driver gets a fresh one
                    await source.aclose()
                return

    def sources(self) -> list[SimWorldSource]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return []
        return [entry.source for entry in self._by_loop.get(loop, {}).values()]


_REGISTRY = _Registry()


def active_sources() -> list[SimWorldSource]:
    """The shared world sources open in the running event loop (one per node that has
    an open sim driver). For diagnostics and tests."""
    return _REGISTRY.sources()


# ------------------------------------------------------------------------- the driver


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (
        math.isfinite(value) and value > 0
    ):
        raise ValueError(f"{name}: must be a positive number, got {value!r}")
    return float(value)


class SimDriver:
    """The simulated radio of one channel (see the module docstring).

    Options (all keyword-only, the `--driver sim` options of a node):
      lookback_s: how far in the past a read may start, seconds (30).
      concurrent_reads: reads served at once (8).
      open_timeout_s: how long `open` waits for the first world, seconds (20; below
        the agent's 30 s limit so that the reason reaches the `assign_ack`).
      refresh_timeout_s: how long `on_assign` and `on_sim_update` wait for the
        refetch, seconds (10).
      world_wait_s: how long a read waits for an announced version that is still
        being fetched, seconds (10).
      fetch_timeout_s: limit of one HTTP request, seconds (10).
      retry_base_s, retry_max_s: backoff of failed fetches, seconds (0.5, 15).
      tuning_margin_hz: margin of the centre-frequency range beyond the watched
        downlinks, Hz (None: the channel's `max_sample_rate_hz`).
      proxy: True honours the environment's proxy settings, None uses none, a URL
        names one (as `NodeAgent`).
      verify: TLS verification of an https server: True, False, a CA bundle file or
        directory path, or an `ssl.SSLContext`.
      clock: the clock to wait on; None takes the node's clock from the context.
      source: a `SimWorldSource` to read from; None shares the node's source from the
        process-wide registry (the options of the first driver to open it apply).
    Raises `ValueError` naming an option that cannot be used.
    """

    def __init__(
        self,
        *,
        lookback_s: float = DEFAULT_LOOKBACK_S,
        concurrent_reads: int = DEFAULT_CONCURRENT_READS,
        open_timeout_s: float = 20.0,
        refresh_timeout_s: float = 10.0,
        world_wait_s: float = 10.0,
        fetch_timeout_s: float = 10.0,
        retry_base_s: float = 0.5,
        retry_max_s: float = 15.0,
        tuning_margin_hz: float | None = None,
        proxy: str | Literal[True] | None = True,
        verify: bool | str | ssl.SSLContext = True,
        clock: ClockSource | None = None,
        source: SimWorldSource | None = None,
    ) -> None:
        if isinstance(lookback_s, bool) or not isinstance(lookback_s, (int, float)) or not (
            math.isfinite(lookback_s) and lookback_s >= 0
        ):
            raise ValueError(f"lookback_s: must be finite and not negative, got {lookback_s!r}")
        if (
            isinstance(concurrent_reads, bool)
            or not isinstance(concurrent_reads, int)
            or concurrent_reads < 1
        ):
            raise ValueError(f"concurrent_reads: must be an integer of at least 1, got {concurrent_reads!r}")
        self._open_timeout_s = _positive(open_timeout_s, "open_timeout_s")
        self._refresh_timeout_s = _positive(refresh_timeout_s, "refresh_timeout_s")
        self._world_wait_s = _positive(world_wait_s, "world_wait_s")
        self._fetch_timeout_s = _positive(fetch_timeout_s, "fetch_timeout_s")
        self._retry_base_s = _positive(retry_base_s, "retry_base_s")
        self._retry_max_s = _positive(retry_max_s, "retry_max_s")
        if self._retry_max_s < self._retry_base_s:
            raise ValueError(f"retry_max_s: {retry_max_s!r} is below retry_base_s {retry_base_s!r}")
        if tuning_margin_hz is not None and (
            isinstance(tuning_margin_hz, bool)
            or not isinstance(tuning_margin_hz, (int, float))
            or not (math.isfinite(tuning_margin_hz) and tuning_margin_hz >= 0)
        ):
            raise ValueError(
                f"tuning_margin_hz: must be finite and not negative, got {tuning_margin_hz!r}"
            )
        if not (proxy is None or proxy is True or isinstance(proxy, str)):
            raise ValueError(f"proxy: must be True, None or a proxy URL, got {proxy!r}")
        if not isinstance(verify, (bool, str, ssl.SSLContext)):
            raise ValueError(f"verify: must be a bool, a CA path or an ssl.SSLContext, got {verify!r}")
        if source is not None and not isinstance(source, SimWorldSource):
            raise ValueError(f"source: must be a SimWorldSource, got {type(source).__name__}")
        self._lookback_s = float(lookback_s)
        self._concurrent_reads = concurrent_reads
        self._tuning_margin_hz = tuning_margin_hz
        self._proxy: str | Literal[True] | None = proxy
        self._verify = verify
        self._clock = clock
        self._given_source = source
        self._source: SimWorldSource | None = None
        self._shared = False
        self._workers: WorkerPool | None = None
        self._capabilities: Capabilities | None = None
        self._channel_id: str | None = None
        self._site_id: str | None = None
        self._open = False
        self._closed = False

    # ------------------------------------------------------------------ properties

    @property
    def capabilities(self) -> Capabilities:
        """What the channel can do, from the configuration of the newest world.
        Raises `CaptureError("driver")` before `open` has succeeded."""
        if self._capabilities is None:
            raise CaptureError("driver", "sim driver is not open: capabilities unknown")
        return self._capabilities

    @property
    def source(self) -> SimWorldSource | None:
        """The world source this driver reads from (None before `open`)."""
        return self._source

    @property
    def channel_id(self) -> str | None:
        """The channel served (None before `open`)."""
        return self._channel_id

    @property
    def world_version(self) -> int | None:
        """Version of the newest world held, or None."""
        source = self._source
        newest = None if source is None else source.newest
        return None if newest is None else newest.version

    @staticmethod
    def clock_status() -> ClockStatus:
        """The clock status a simulated site reports (`sim_clock_status()`)."""
        return sim_clock_status()

    def _extra(self, **more: Any) -> dict[str, Any]:
        return {"channel_id": self._channel_id, "site_id": self._site_id, **more}

    # ------------------------------------------------------------------ life cycle

    async def open(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        """Fetch the world (shared with the node's other sim drivers) and derive the
        channel's capabilities from its configuration.

        Raises `CaptureError("driver")` when the server runs no simulation, the
        backplane does not deliver a world within `open_timeout_s`, or the world's
        configuration has no such channel at this site.
        """
        if self._open or self._closed:
            raise CaptureError("driver", "sim driver: open may be called once")
        simulation = ctx.simulation
        if not simulation or not simulation.get("enabled"):
            raise CaptureError(
                "driver",
                "the server runs no simulation (welcome.simulation is not enabled); "
                "the sim driver needs the simulation backplane",
            )
        try:
            url = backplane_url(ctx.server_http_url, simulation.get("scenario_url"))  # type: ignore[arg-type]
        except ValueError as exc:
            raise CaptureError("driver", f"simulation backplane: {exc}") from None
        self._channel_id = channel.channel_id
        self._site_id = ctx.site_id
        self._clock = self._clock or ctx.clock or sim_clock()
        self._workers = ctx.workers if ctx.workers is not None else WorkerPool()
        if self._given_source is not None:
            self._source, self._shared = self._given_source, False
        else:
            token_digest = hashlib.blake2b(ctx.token.encode(), digest_size=16).hexdigest()
            workers = self._workers
            self._source = _REGISTRY.acquire(
                (url, ctx.site_id, token_digest),
                lambda: SimWorldSource(
                    url,
                    ctx.token,
                    workers=workers,
                    proxy=self._proxy,
                    verify=self._verify,
                    fetch_timeout_s=self._fetch_timeout_s,
                    retry_base_s=self._retry_base_s,
                    retry_max_s=self._retry_max_s,
                    lookback_s=self._lookback_s,
                    jitter_key=ctx.site_id,
                ),
            )
            self._shared = True
        try:
            version = simulation.get("version")
            if isinstance(version, int) and not isinstance(version, bool):
                self._source.require_version(version)
            newest, problem = await self._source.refresh(self._open_timeout_s)
            if problem is not None:
                logger.warning(
                    "sim driver opened on simulated world version %d; refetch failed: %s",
                    newest.version,
                    problem,
                    extra=self._extra(sim_version=newest.version),
                )
            self._adopt(channel, ctx.site_id, newest)
        except BackplaneError as exc:
            await self._release()
            raise CaptureError("driver", str(exc)) from None
        except BaseException:
            await self._release()
            raise
        self._open = True
        logger.info(
            "sim driver open on simulated world version %d",
            newest.version,
            extra=self._extra(sim_version=newest.version, backplane_url=url),
        )

    def _adopt(self, channel: ChannelAssignment, site_id: str, entry: WorldVersion) -> None:
        """Check the channel against a world's configuration and take its capabilities."""
        config = entry.config
        try:
            site, _ = config.channel(channel.channel_id)
        except ValueError:
            raise CaptureError(
                "driver",
                f"channel {channel.channel_id!r} is not in the configuration of simulated "
                f"world version {entry.version}",
            ) from None
        if site.id != site_id:
            raise CaptureError(
                "driver",
                f"channel {channel.channel_id!r} belongs to site {site.id!r} in simulated world "
                f"version {entry.version}; this node serves {site_id!r}",
            )
        try:
            self._capabilities = channel_capabilities(
                config,
                channel.channel_id,
                lookback_s=self._lookback_s,
                concurrent_reads=self._concurrent_reads,
                tuning_margin_hz=self._tuning_margin_hz,
                monitors=channel.monitors,
            )
        except ValueError as exc:
            raise CaptureError("driver", f"capabilities of channel {channel.channel_id!r}: {exc}") from None

    async def on_assign(self, channel: ChannelAssignment, ctx: DriverContext) -> None:
        """Refetch the world before the node acknowledges an `assign` (the channel's
        assignment is unchanged). Raises `CaptureError("driver")` when the newest
        world's configuration no longer has the channel at this site, which makes the
        agent report the channel failed and close the driver."""
        source = self._require_source()
        try:
            newest, problem = await source.refresh(self._refresh_timeout_s)
        except BackplaneError as exc:
            raise CaptureError("driver", str(exc)) from None
        if problem is not None:
            logger.warning(
                "assign: refetch of the simulated world failed, keeping version %d: %s",
                newest.version,
                problem,
                extra=self._extra(sim_version=newest.version),
            )
        self._adopt(channel, ctx.site_id, newest)

    async def on_sim_update(self, version: int, effective_ns: int) -> None:
        """Fetch the world announced by `sim_update` (shared: one fetch per node)."""
        source = self._require_source()
        held = await source.announce(version, effective_ns, self._refresh_timeout_s)
        if not held:
            logger.warning(
                "simulated world version %d (in force from %d ns) not fetched within %g s; "
                "still trying",
                version,
                effective_ns,
                self._refresh_timeout_s,
                extra=self._extra(sim_version=version, last_error=source.last_error),
            )

    async def _release(self) -> None:
        source, self._source = self._source, None
        if source is not None and self._shared:
            await _REGISTRY.release(source)

    async def close(self) -> None:
        """Stop serving and release the shared world source. Idempotent; a read still
        waiting for its interval fails with "driver"."""
        if self._closed:
            return
        self._closed = True
        was_open, self._open = self._open, False
        await self._release()
        if was_open:
            logger.info("sim driver closed", extra=self._extra())

    def _require_source(self) -> SimWorldSource:
        source = self._source
        if not self._open or source is None:
            raise CaptureError("driver", "sim driver is not open")
        return source

    # ------------------------------------------------------------------ samples

    def _check_time_and_band(self, t_start_ns: int, fs: float, fc: float, what: str) -> None:
        caps = self.capabilities
        if isinstance(t_start_ns, bool) or not isinstance(t_start_ns, int):
            raise CaptureError("out_of_range", f"t_start_ns must be integer ns, got {t_start_ns!r}")
        if isinstance(fs, bool) or not isinstance(fs, (int, float)) or not (
            math.isfinite(fs) and 0 < fs <= caps.max_sample_rate_hz * (1 + 1e-9)
        ):
            raise CaptureError(
                "out_of_range",
                f"{what} rate {fs!r} Hz is outside 0..{caps.max_sample_rate_hz:g} Hz",
            )
        if isinstance(fc, bool) or not isinstance(fc, (int, float)) or not (
            math.isfinite(fc) and caps.min_hz <= fc <= caps.max_hz
        ):
            raise CaptureError(
                "out_of_range",
                f"center {fc!r} Hz is outside {caps.min_hz:.3f}..{caps.max_hz:.3f} Hz",
            )
        assert self._clock is not None
        now_ns = self._clock.now_ns()
        if t_start_ns < now_ns - round(caps.lookback_s * _NS):
            raise CaptureError(
                "too_late",
                f"interval starts {(now_ns - t_start_ns) / _NS:.3f} s in the past, "
                f"lookback is {caps.lookback_s:g} s",
            )

    async def _world_for(self, t_start_ns: int, t_end_ns: int, what: str) -> WorldVersion:
        """Wait until the interval has passed, then the world in force at its start."""
        assert self._clock is not None
        await self._clock.sleep_until(t_end_ns)
        source = self._require_source()
        found = await source.world_at(t_start_ns, self._world_wait_s)
        entry = found.world
        assert entry is not None
        change = found.next_effective_ns
        if change is not None and t_start_ns < change < t_end_ns:
            logger.info(
                "%s straddles the simulated world change at %d ns; served from version %d, "
                "in force at its first sample",
                what,
                change,
                entry.version,
                extra=self._extra(
                    sim_version=entry.version, t_start_ns=t_start_ns, t_end_ns=t_end_ns,
                    effective_ns=change,
                ),
            )
        channel_id = self._channel_id
        assert channel_id is not None
        try:
            entry.config.channel(channel_id)
        except ValueError:
            raise CaptureError(
                "driver",
                f"channel {channel_id!r} is not in the configuration of simulated world "
                f"version {entry.version}, in force at {t_start_ns} ns",
            ) from None
        return entry

    async def _render(self, fn: Callable[..., _T], *args: Any) -> _T:
        workers = self._workers
        assert workers is not None
        try:
            return await workers.run(fn, *args)
        except ValueError as exc:
            raise CaptureError("driver", f"simulator refused the request: {exc}") from None

    async def read(self, t_start_ns: int, n: int, fs: float, fc: float) -> Capture:
        """`n` samples at `fs` Hz centred on `fc` Hz (downlink RF), the first labelled
        `t_start_ns` (UTC ns): `SimWorld.render_iq` of the world in force then.

        Returns once the node clock has passed `t_start_ns + n / fs`; `t_first_ns`
        equals `t_start_ns`, `fs` and `fc` are those asked for, the clock is locked.
        Raises `CaptureError`: "driver" when not open or no world serves the interval,
        "out_of_range" for a length, rate or centre outside the capabilities,
        "too_late" for an interval that started more than `lookback_s` ago or that
        only a world no longer held could serve.
        """
        self._require_source()
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_CAPTURE_SAMPLES:
            raise CaptureError("out_of_range", f"n must be 1..{MAX_CAPTURE_SAMPLES} samples, got {n!r}")
        self._check_time_and_band(t_start_ns, fs, fc, "sample")
        t_end_ns = t_start_ns + duration_ns(n, fs)
        entry = await self._world_for(t_start_ns, t_end_ns, "capture")
        iq = await self._render(
            entry.world.render_iq, self._channel_id, t_start_ns, n, float(fs), float(fc)
        )
        return Capture(
            iq=iq,
            t_first_ns=t_start_ns,
            fs=float(fs),
            fc=float(fc),
            overflow=False,
            clock_locked=True,
        )

    async def psd(
        self, t_start_ns: int, fc: float, span_hz: float, nfft: int, navg: int
    ) -> np.ndarray:
        """Spectrum of the window whose snippet starts at `t_start_ns` (UTC ns):
        `SimWorld.render_psd` of the world in force then, float32[nfft] in dB,
        fft-shifted (bin k at fc + (k - nfft/2) * span_hz / nfft). Never None.

        Returns once the node clock has passed the end of the snippet,
        `t_start_ns + nfft * navg / span_hz`. Raises `CaptureError` like `read`.
        """
        self._require_source()
        if isinstance(nfft, bool) or not isinstance(nfft, int) or not 2 <= nfft <= MAX_PSD_BINS:
            raise CaptureError("out_of_range", f"nfft must be 2..{MAX_PSD_BINS}, got {nfft!r}")
        if isinstance(navg, bool) or not isinstance(navg, int) or navg < 1:
            raise CaptureError("out_of_range", f"navg must be at least 1, got {navg!r}")
        self._check_time_and_band(t_start_ns, span_hz, fc, "span")
        t_end_ns = t_start_ns + duration_ns(nfft * navg, span_hz)
        entry = await self._world_for(t_start_ns, t_end_ns, "spectrum")
        return await self._render(
            entry.world.render_psd,
            self._channel_id,
            t_start_ns,
            float(fc),
            float(span_hz),
            nfft,
            navg,
        )
