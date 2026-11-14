"""The server's runtime: configuration, scenario, store, hub providers and pipeline.

``ServerRuntime`` is the one object the server application holds besides the hub. It

- owns the current ``SystemConfig`` and its integer version (the console's ETag),
  replaces it (``replace_config``: If-Match check, validation with field errors,
  persistence, ``hub.reassign()``, pipeline rebuild, console notification and, when
  simulating, a ``sim_update``);
- owns the simulated world when the server runs with a scenario (``replace_scenario``,
  ``load_preset``, ``reset_scenario``, ``backplane_document`` for the node backplane at
  ``SIM_BACKPLANE_PATH``);
- provides the hub's providers (``hub_providers()``: channel assignments built from
  each site's channels and monitors, the configuration version, the ``simulation``
  object of ``welcome``);
- runs the event pipeline: the detection pipeline subscribed to the hub's spectrum
  frames, the correlator, the event book, the worker pool for CPU-heavy work;
- publishes what the console needs through ``console`` (``ConsoleBus``): ``status``
  about once a second, ``event`` on every published change, ``config`` and ``sim`` on
  replacement, and the binary spectrum frames of the windows a client streams.

Wiring. The runtime must be built on the serving event loop, so inside the
application's lifespan; ``RuntimeHolder`` gives ``create_app`` providers that delegate
to it once it exists::

    holder = RuntimeHolder()
    app = create_app(settings, hub_providers=holder.hub_providers())
    # lifespan, after hub.start():
    holder.runtime = app.state.runtime = await ServerRuntime.from_settings(
        settings, store=store, clock=app.state.clock)   # the server clock (Settings.clock())
    await holder.runtime.start(app.state.hub)
    # lifespan, before hub.close():
    await holder.runtime.stop()

(When the caller already runs on the serving loop it may build the runtime first and
pass ``runtime.hub_providers()`` instead.) It also publishes, with the status, the
satellites whose ephemeris cannot be evaluated (``check_orbits``, every
``ORBIT_CHECK_S``) and the per-site clock estimates of tasking.

Errors for the console API: ``PreconditionRequired`` (428), ``VersionConflict`` (409),
``ConfigInvalid`` / ``ScenarioInvalid`` (422, ``.errors`` is ``[{loc, msg}]``),
``NotSimulating`` / ``PresetNotFound`` / ``EventNotFound`` (404), ``StoreError`` (500).
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextvars
import functools
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TypeVar

import numpy as np
import yaml
from pydantic import ValidationError

from petal_locate import __version__
from petal_locate.config import ConfigError, SystemConfig, field_errors, load_config, loads_config
from petal_locate.core.orbits import orbit_position
from petal_locate.core.timebase import iso_from_ns, ns_from_iso
from petal_locate.protocol.messages import SIM_UPDATE_LEAD_NS, ChannelAssignment, MonitorAssignment
from petal_locate.server.api import schemas
from petal_locate.server.app import HubProviders
from petal_locate.server.pipeline.advisory import advise
from petal_locate.server.pipeline.correlator import REOPEN_WINDOW_S, Correlator, QualityTable
from petal_locate.server.pipeline.detection import (
    DetectionPipeline,
    WindowKey,
    WindowSpec,
    build_trackers,
    build_window_specs,
)
from petal_locate.server.pipeline.events import (
    EventBook,
    EventNotFound,
    EventRecord,
    Publication,
    TruthEmitter,
    evidence_json,
    event_truth,
)
from petal_locate.server.pipeline.tasking import Tasking
from petal_locate.server.settings import Settings
from petal_locate.server.store import Store, StoreError
from petal_locate.sim.scenario import Scenario, ScenarioError, dump_scenario, load_scenario, loads_scenario

__all__ = [
    "CPU_WORKERS",
    "HISTORY_LIMIT",
    "ORBIT_CHECK_S",
    "QUALITY_REFRESH_S",
    "SIM_BACKPLANE_PATH",
    "STATUS_INTERVAL_S",
    "ConfigInvalid",
    "ConsoleBus",
    "ConsoleClosed",
    "ConsoleSubscription",
    "EventNotFound",
    "NotSimulating",
    "PreconditionRequired",
    "PresetNotFound",
    "RuntimeHolder",
    "ScenarioInvalid",
    "ServerRuntime",
    "StoreError",
    "VersionConflict",
    "WorkPool",
    "config_text",
    "default_presets_dir",
    "parse_if_match",
    "resolve_truth_emitters",
]

logger = logging.getLogger(__name__)

SIM_BACKPLANE_PATH: Final[str] = "/node/v1/sim"
"""Path of the simulation backplane document on the server origin (``scenario_url`` of
``welcome``); ``GET`` with ``Authorization: Bearer <node token>``."""

STATUS_INTERVAL_S: Final[float] = 1.0
"""Period of the console ``status`` message and of the correlator's tick, s."""

QUALITY_REFRESH_S: Final[float] = 30.0
"""Period at which the downlink-quality table is recomputed (satellites move), s."""

ORBIT_CHECK_S: Final[float] = 30.0
"""Period of the orbit-validity check whose result ``status`` carries (``satellites``):
an ephemeris table that has ended, or a TLE that no longer propagates, is reported, s."""

HISTORY_LIMIT: Final[int] = 500
"""Events of earlier runs loaded as history at start."""

CPU_WORKERS: Final[int] = 3
"""Worker threads for CPU-heavy jobs (``WorkPool``)."""

LOOP_LAG_PROBE_S: Final[float] = 0.1
"""Period of the event-loop lag probe, s."""

LOOP_LAG_WARN_S: Final[float] = 0.1
"""Event-loop lag logged as a warning, s (twice the 50 ms budget)."""

_PRESET_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PRESET_SUFFIXES: Final[tuple[str, ...]] = (".yaml", ".yml", ".json")
_CONSOLE_MAX_KEYS: Final[int] = 4096

T = TypeVar("T")


# ------------------------------------------------------------------------ errors


class PreconditionRequired(ValueError):
    """``PUT /config`` without ``If-Match`` (HTTP 428)."""


class VersionConflict(Exception):
    """The ``If-Match`` version is not the current one (HTTP 409).

    current_version: the version in force, for the client to reload.
    """

    def __init__(self, current_version: int, detail: str = "") -> None:
        super().__init__(
            detail or f"the configuration is at version {current_version}; reload it and apply your change again"
        )
        self.current_version = current_version


class ConfigInvalid(ValueError):
    """The configuration does not validate (HTTP 422). ``errors``: ``[{loc, msg}]``."""

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        self.errors: list[dict[str, str]] = [{"loc": str(e["loc"]), "msg": str(e["msg"])} for e in errors]
        super().__init__("; ".join(f"{e['loc']}: {e['msg']}" for e in self.errors) or "invalid configuration")


class ScenarioInvalid(ValueError):
    """The scenario does not validate or does not fit the configuration (HTTP 422).
    ``errors``: ``[{loc, msg}]``."""

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        self.errors: list[dict[str, str]] = [{"loc": str(e["loc"]), "msg": str(e["msg"])} for e in errors]
        super().__init__("; ".join(f"{e['loc']}: {e['msg']}" for e in self.errors) or "invalid scenario")


class NotSimulating(LookupError):
    """The server runs without a scenario (HTTP 404 on ``/sim/...``)."""

    def __init__(self) -> None:
        super().__init__("the server is not simulating (it was started without a scenario)")


class PresetNotFound(LookupError):
    """No preset scenario has this name (HTTP 404)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no preset scenario named {name!r}")
        self.name = name


# ------------------------------------------------------------------------ helpers


def config_text(cfg: SystemConfig) -> str:
    """The stored form of a configuration: YAML of ``export_dict()`` without its
    ``version`` (the store's version column is authoritative)."""
    data = cfg.export_dict()
    data.pop("version", None)
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=None, allow_unicode=True, width=100)


def _derived_channels(cfg: SystemConfig) -> list[str]:
    """Ids of the channels whose monitors were derived from the frequency plan."""
    return [channel.id for site in cfg.sites for channel in site.channels if channel.monitors_derived]


def _with_version(cfg: SystemConfig, version: int) -> SystemConfig:
    return cfg if cfg.version == version else cfg.model_copy(update={"version": int(version)})


_ETAG: Final[re.Pattern[str]] = re.compile(r'^\s*(?:W/)?"?(\d+)"?\s*$')


def parse_if_match(value: str | int | None) -> set[int] | None:
    """The versions an ``If-Match`` header accepts.

    value: the header (``3``, ``"3"``, ``W/"3"``, a comma-separated list of those, or
    ``*`` for any version), or an int. Returns the set of versions, or None for ``*``.
    Raises ``PreconditionRequired`` when absent or empty and ``VersionConflict`` (with
    ``current_version`` -1, the caller fills it in) for an entry that is not a version.
    """
    if value is None:
        raise PreconditionRequired("If-Match is required: send the version you loaded (the ETag of GET /config)")
    if isinstance(value, bool):
        raise VersionConflict(-1, f"If-Match {value!r} is not a configuration version")
    if isinstance(value, int):
        return {value}
    text = str(value).strip()
    if not text:
        raise PreconditionRequired("If-Match is required: send the version you loaded (the ETag of GET /config)")
    if text == "*":
        return None
    versions: set[int] = set()
    for part in text.split(","):
        match = _ETAG.match(part)
        if match is None:
            raise VersionConflict(-1, f"If-Match {part.strip()!r} is not a configuration version")
        versions.add(int(match.group(1)))
    return versions


def default_presets_dir() -> Path | None:
    """``config/scenarios`` of a source checkout, when it exists."""
    candidate = Path(__file__).resolve().parents[3] / "config" / "scenarios"
    return candidate if candidate.is_dir() else None


def resolve_truth_emitters(scenario: Scenario, config: SystemConfig) -> list[TruthEmitter]:
    """The simulated emitters with carrier defaults resolved (ARCHITECTURE.md section 4):
    every scenario emitter, plus one per configuration carrier with a location and an
    emitter model that no scenario emitter names. Emitters whose carrier is missing
    are skipped (``validate_against`` reports those)."""
    out: list[TruthEmitter] = []
    named = {e.carrier_id for e in scenario.emitters if e.carrier_id is not None}
    carriers = {c.id: c for c in config.carriers}
    for emitter in scenario.emitters:
        carrier = carriers.get(emitter.carrier_id) if emitter.carrier_id is not None else None
        if emitter.carrier_id is not None and carrier is None:
            continue
        lat = emitter.lat_deg if emitter.lat_deg is not None else (carrier.location.lat_deg if carrier and carrier.location else None)
        lon = emitter.lon_deg if emitter.lon_deg is not None else (carrier.location.lon_deg if carrier and carrier.location else None)
        freq = emitter.freq_hz if emitter.freq_hz is not None else (carrier.uplink_center_hz if carrier else None)
        if lat is None or lon is None or freq is None:
            continue
        bandwidth = 0.0
        waveform = emitter.waveform
        if waveform is not None:
            bandwidth = waveform.bandwidth_hz
            if waveform.type == "psk" and bandwidth <= 0:
                bandwidth = waveform.symbol_rate_hz * (1.0 + waveform.rolloff)
        elif carrier is not None and carrier.emitter is not None:
            bandwidth = carrier.emitter.bandwidth_hz
        out.append(
            TruthEmitter(
                id=emitter.id,
                kind=emitter.kind,
                lat_deg=float(lat),
                lon_deg=float(lon),
                freq_hz=float(freq),
                bandwidth_hz=float(bandwidth),
                enabled=emitter.enabled,
            )
        )
    for carrier in config.carriers:
        if carrier.id in named or carrier.location is None or carrier.emitter is None:
            continue
        out.append(
            TruthEmitter(
                id=carrier.id,
                kind="carrier",
                lat_deg=carrier.location.lat_deg,
                lon_deg=carrier.location.lon_deg,
                freq_hz=carrier.uplink_center_hz,
                bandwidth_hz=carrier.emitter.bandwidth_hz,
            )
        )
    return out


def _table_span_ns(orbit_model: Any) -> tuple[int | None, int | None]:
    """First and last sample time of a ``table`` orbit (integer UTC ns), else (None, None)."""
    if getattr(orbit_model, "type", None) != "table":
        return None, None
    try:
        times = [ns_from_iso(sample.t) for sample in orbit_model.samples]
    except (ValueError, AttributeError, TypeError):
        return None, None
    return (min(times), max(times)) if times else (None, None)


def _scenario_errors(exc: Exception) -> list[dict[str, str]]:
    """``[{loc, msg}]`` from a scenario failure (``ValidationError`` or ``ScenarioError``)."""
    if isinstance(exc, ValidationError):
        return [
            {"loc": ".".join(str(p) for p in e["loc"]) or "(root)", "msg": str(e["msg"])}
            for e in exc.errors(include_url=False, include_context=False, include_input=False)
        ]
    text = str(exc)
    loc, sep, msg = text.partition(": ")
    if sep and " " not in loc.strip():
        return [{"loc": loc.strip(), "msg": msg}]
    return [{"loc": "(root)", "msg": text}]


# ------------------------------------------------------------------------ worker pool


WORK_CLOSE_GRACE_S: Final[float] = 10.0
"""``WorkPool.close`` logs the jobs still running after this long, s."""


class WorkPool:
    """Runs CPU-heavy blocking work (CAF, solve, lines of position, accuracy maps,
    classification, tracker updates of large windows) off the event loop, at most
    ``limit`` jobs at a time; further jobs wait their turn."""

    def __init__(self, limit: int = CPU_WORKERS, *, close_grace_s: float = WORK_CLOSE_GRACE_S) -> None:
        """limit: jobs at once (>= 1). close_grace_s: how long ``close`` waits for the
        jobs running before it logs which they are (it then goes on waiting: a thread
        cannot be stopped), s."""
        if limit < 1:
            raise ValueError("limit: must be at least 1")
        self._limit = int(limit)
        self._close_grace_s = float(close_grace_s)
        self._semaphore = asyncio.Semaphore(self._limit)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self._limit, thread_name_prefix="petal-work")
        self._futures: set[concurrent.futures.Future[Any]] = set()
        self._jobs: dict[concurrent.futures.Future[Any], tuple[str, float]] = {}  # name, monotonic start
        self._running = 0
        self._waiting = 0
        self._closed = False

    @property
    def limit(self) -> int:
        """Jobs that may run at once."""
        return self._limit

    @property
    def running(self) -> int:
        """Jobs running now."""
        return self._running

    @property
    def waiting(self) -> int:
        """Jobs waiting for a slot."""
        return self._waiting

    async def run(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run ``fn(*args, **kwargs)`` in a worker thread and return its result (or raise
        its exception). Cancelling the caller abandons the result; the thread finishes
        its job. Raises RuntimeError after ``close``."""
        if self._closed:
            raise RuntimeError("the worker pool is closed")
        self._waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        try:
            if self._closed:
                raise RuntimeError("the worker pool is closed")
            self._running += 1
            context = contextvars.copy_context()
            future = self._executor.submit(context.run, functools.partial(fn, *args, **kwargs))
            self._futures.add(future)
            self._jobs[future] = (getattr(fn, "__qualname__", None) or repr(fn), time.monotonic())
            future.add_done_callback(self._futures.discard)
            future.add_done_callback(lambda f: self._jobs.pop(f, None))
            try:
                return await asyncio.wrap_future(future)
            finally:
                self._running -= 1
        finally:
            self._semaphore.release()

    async def close(self) -> None:
        """Refuse new jobs, drop queued ones, wait (without blocking the loop) for those
        running, and stop the threads. Jobs still running after ``close_grace_s`` are
        logged by name and age (SRV-6: a stuck job would otherwise hold shutdown silently);
        the wait goes on. Idempotent."""
        if self._closed and not self._futures:
            self._executor.shutdown(wait=False)
            return
        self._closed = True
        pending = [f for f in list(self._futures) if not f.done()]
        for future in pending:
            future.cancel()  # only queued jobs can be cancelled
        running = [f for f in pending if not f.cancelled()]
        if running:
            waiting = asyncio.gather(*(asyncio.wrap_future(f) for f in running), return_exceptions=True)
            done, _ = await asyncio.wait({waiting}, timeout=self._close_grace_s)
            if not done:
                now = time.monotonic()
                stuck = sorted(
                    (self._jobs.get(f, ("unknown job", now)) for f in running if not f.done()), key=lambda j: j[1]
                )
                logger.warning(
                    "shutdown waits for %d worker job(s) still running after %.0f s: %s",
                    len(stuck),
                    self._close_grace_s,
                    ", ".join(f"{name} ({now - started:.0f} s)" for name, started in stuck),
                    extra={"jobs": [name for name, _ in stuck]},
                )
            await waiting
        self._executor.shutdown(wait=True)  # idle by now: returns at once


# ------------------------------------------------------------------------ console bus


class ConsoleClosed(Exception):
    """The console subscription was closed."""


class ConsoleSubscription:
    """One console client's mailbox (newest wins per key).

    Text messages (``status``, ``config``, ``sim``, and ``event`` per event id) and
    binary spectrum frames (per window, only for the windows selected with
    ``set_psd``) wait here until the client's sender takes them with ``get()``. A newer
    message of the same key replaces an older undelivered one (counted in
    ``dropped``), so a slow client never holds up the others or the server.
    """

    def __init__(self, bus: ConsoleBus, psd: Iterable[tuple[str, str]] = ()) -> None:
        self._bus = bus
        self._psd: frozenset[tuple[str, str]] = frozenset()
        self._box: collections.OrderedDict[tuple[str, ...], str | bytes] = collections.OrderedDict()
        self._wake = asyncio.Event()
        self._closed = False
        self.delivered = 0
        self.dropped = 0
        self.set_psd(psd)

    @property
    def psd_windows(self) -> frozenset[tuple[str, str]]:
        """The (channel_id, monitor_id) windows streamed to this client."""
        return self._psd

    @property
    def closed(self) -> bool:
        """Whether ``close`` was called."""
        return self._closed

    @property
    def waiting(self) -> int:
        """Messages not yet taken."""
        return len(self._box)

    def set_psd(self, windows: Iterable[tuple[str, str]]) -> None:
        """Replace the streamed windows (the browser's ``subscribe`` message). Waiting
        frames of windows no longer selected are discarded. Raises ValueError for an
        entry that is not a pair of strings."""
        selected: set[tuple[str, str]] = set()
        for entry in windows:
            pair = tuple(entry)
            if len(pair) != 2 or not all(isinstance(x, str) and x for x in pair):
                raise ValueError(f"psd: each entry must be [channel_id, monitor_id], got {entry!r}")
            selected.add((pair[0], pair[1]))
        self._psd = frozenset(selected)
        for key in [k for k in self._box if k[0] == "psd" and (k[1], k[2]) not in self._psd]:
            del self._box[key]

    def offer(self, key: tuple[str, ...], payload: str | bytes) -> None:
        """Put a message in the mailbox (newest wins per key). Never waits."""
        if self._closed:
            return
        if key in self._box:
            self.dropped += 1
            del self._box[key]
        self._box[key] = payload
        while len(self._box) > _CONSOLE_MAX_KEYS:
            self._box.popitem(last=False)
            self.dropped += 1
        self._wake.set()

    def get_nowait(self) -> str | bytes | None:
        """The oldest waiting message, or None."""
        if not self._box:
            return None
        self.delivered += 1
        return self._box.popitem(last=False)[1]

    async def get(self) -> str | bytes:
        """Wait for the next message: ``str`` (a JSON text frame) or ``bytes`` (a
        binary spectrum frame). Raises ``ConsoleClosed`` once closed."""
        while True:
            item = self.get_nowait()
            if item is not None:
                return item
            if self._closed:
                raise ConsoleClosed()
            self._wake.clear()
            await self._wake.wait()

    def close(self) -> None:
        """Stop receiving; a waiting ``get`` raises ``ConsoleClosed``. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._box.clear()
        self._wake.set()
        self._bus._remove(self)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        try:
            return await self.get()
        except ConsoleClosed:
            raise StopAsyncIteration from None


class ConsoleBus:
    """Fan-out of console messages to every connected client (``/ws/ui``).

    The newest ``status`` message is kept and handed to every new client at once, so a
    console that connects does not wait for the next tick to know the state of the
    network.
    """

    STICKY_KEYS: Final[frozenset[str]] = frozenset({"status"})

    def __init__(self) -> None:
        self._subscriptions: list[ConsoleSubscription] = []
        self._sticky: dict[str, str] = {}
        self.published = 0

    def subscribe(self, psd: Iterable[tuple[str, str]] = ()) -> ConsoleSubscription:
        """A new client mailbox, streaming the given spectrum windows; it starts with
        the newest ``status`` message when there is one."""
        subscription = ConsoleSubscription(self, psd)
        self._subscriptions.append(subscription)
        for key, text in self._sticky.items():
            subscription.offer(("text", key), text)
        return subscription

    @property
    def subscribers(self) -> int:
        """Connected clients."""
        return len(self._subscriptions)

    def publish_text(self, key: str, text: str) -> None:
        """Offer a JSON text message to every client; ``key`` decides what a newer
        message replaces (``status``, ``config``, ``sim``, ``event:<id>``)."""
        self.published += 1
        if key in self.STICKY_KEYS:
            self._sticky[key] = text
        for subscription in list(self._subscriptions):
            subscription.offer(("text", key), text)

    def wants_psd(self, channel_id: str, monitor_id: str) -> bool:
        """Whether any client streams this window."""
        window = (channel_id, monitor_id)
        return any(window in s.psd_windows for s in self._subscriptions)

    def publish_psd(self, channel_id: str, monitor_id: str, frame: bytes) -> None:
        """Offer a binary spectrum frame to the clients streaming its window."""
        window = (channel_id, monitor_id)
        for subscription in list(self._subscriptions):
            if window in subscription.psd_windows:
                subscription.offer(("psd", channel_id, monitor_id), frame)

    def close(self) -> None:
        """Close every client mailbox."""
        for subscription in list(self._subscriptions):
            subscription.close()

    def _remove(self, subscription: ConsoleSubscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)


# ------------------------------------------------------------------------ runtime


class RuntimeHolder:
    """Hub providers for an application whose runtime is created later.

    The runtime must be built on the serving event loop (inside the application's
    lifespan), but ``create_app`` needs the hub's providers first. Pass
    ``holder.hub_providers()`` to ``create_app`` and set ``holder.runtime`` in the
    lifespan. Until a runtime is attached no site is known (every ``hello`` is
    rejected), the configuration version is 0 and nothing is simulated.
    """

    def __init__(self) -> None:
        self.runtime: ServerRuntime | None = None

    def hub_providers(self) -> HubProviders:
        """Providers that delegate to ``self.runtime`` once it is set."""
        return HubProviders(
            assignments_for=lambda site_id: None if self.runtime is None else self.runtime.assignments_for(site_id),
            config_version=lambda: 0 if self.runtime is None else self.runtime.config_version,
            simulation_info=lambda: {"enabled": False} if self.runtime is None else self.runtime.simulation_info(),
        )


class ServerRuntime:
    """Configuration, scenario, store, hub providers and the event pipeline.

    Every method must be called on the server's event loop.
    """

    def __init__(
        self,
        settings: Settings,
        config: SystemConfig,
        scenario: Scenario | None,
        store: Store,
        *,
        config_source: str = "file",
        scenario_version: int | None = None,
        presets_dir: Path | None = None,
        pool: WorkPool | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        """
        settings: deployment settings (marking, API token).
        config: the configuration to run; its ``version`` is taken as the current
            version (``start`` stores it when the store does not hold it yet).
        scenario: the simulated world, or None when not simulating. It must fit the
            configuration (``ScenarioError`` otherwise).
        store: an opened ``Store``; the runtime closes it in ``stop``.
        config_source: ``file`` or ``api``: where the configuration came from.
        scenario_version: version of ``scenario`` in the store (``start`` stores it
            when None or not matching).
        presets_dir: directory of preset scenarios (``<name>.yaml``).
        pool: worker pool (a new one with ``CPU_WORKERS`` threads when None).
        clock: server time, integer UTC ns.
        """
        if scenario is not None:
            scenario.validate_against(config)
        self._settings = settings
        self._config = config
        self._config_version = int(config.version)
        self._config_source = config_source
        self._scenario = scenario
        self._initial_scenario = scenario
        self._scenario_version = int(scenario_version) if scenario_version is not None else (1 if scenario else 0)
        self._scenario_stored = scenario_version is not None
        self._presets_dir = presets_dir
        self._clock = clock
        self._sim_effective_ns = clock()
        self._store = store
        self._pool = pool or WorkPool(CPU_WORKERS)
        self._console = ConsoleBus()
        self._book = EventBook(
            clock=clock,
            advisor=self._advise,
            truth=self._truth,
            simulated=lambda: self.simulating,
            settle_s=lambda: float(self._config.processing.detection.verdict_settle_s),
        )
        self._book.set_publisher(self._publish_event)
        self._correlator = Correlator(config, self._book, windows={}, quality=None, is_online=self._is_online, clock=clock)
        self._spectrum = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="petal-spectrum")
        self._detection = DetectionPipeline(
            sink=self._correlator, console=self._console, pool=self._pool, spectrum_executor=self._spectrum
        )
        self._assignments = self._build_assignments(config)
        self._hub: Any = None
        self._subscriptions: list[Any] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._truth_cache: tuple[tuple[int, int], list[TruthEmitter]] | None = None
        self._backplane_cache: tuple[int, dict[str, Any]] | None = None
        self._state = "new"
        self._status_failures = 0
        self._tasking: Tasking | None = None
        self._lag: dict[str, float] = {"samples": 0, "max_s": 0.0, "over_50ms": 0}
        self._satellites: list[schemas.SatelliteStatus] = []  # orbit-validity check (ADAPT-7)
        self._orbit_down: dict[str, int] = {}  # sat_id -> since when its orbit cannot be evaluated
        self._orbit_checked: tuple[int, int] | None = None  # (config version, server time) of the last check

    # ------------------------------------------------------------------ construction

    @classmethod
    async def from_settings(
        cls,
        settings: Settings,
        *,
        presets_dir: Path | None = None,
        store: Store | None = None,
        clock: Callable[[], int] = time.time_ns,
    ) -> ServerRuntime:
        """Open the data directory and build the runtime the settings describe.

        Resuming: when the newest stored configuration was saved by the operator
        through the API, it is used instead of ``settings.config_path`` (logged; the
        file is ignored until the operator saves its content again or the data
        directory is replaced). Otherwise the file is used, under the newest stored
        version when its content is unchanged, else under a new version. The scenario
        always comes from ``settings.scenario_path`` (stored under a new version when it
        changed). Raises ``ConfigError`` / ``ScenarioError`` for files that do not load
        or fit, ``StoreError`` for an unusable data directory.
        """
        pool = WorkPool(CPU_WORKERS)
        own_store = store is None
        store = store or Store(settings.data_dir)
        try:
            if not store.is_open:
                await store.open()
            file_cfg = await pool.run(load_config, settings.config_path)
            latest = await store.latest_config()
            source = "file"
            cfg: SystemConfig | None = None
            if latest is not None and latest.source == "api":
                try:
                    stored = await pool.run(
                        loads_config, latest.text, latest.fmt, f"stored configuration version {latest.version}"
                    )
                    cfg = _with_version(stored, latest.version)
                    source = "api"
                    logger.warning(
                        "resuming the configuration version %d the operator saved through the API; the file %s "
                        "is ignored (save its content through the API, or use a new data directory, to go back to it)",
                        latest.version,
                        settings.config_path,
                        extra={"config_version": latest.version, "config_source": "api"},
                    )
                except ConfigError as exc:
                    logger.error(
                        "the stored operator configuration version %d no longer loads (%s); using the file %s",
                        latest.version,
                        exc,
                        settings.config_path,
                        extra={"config_version": latest.version},
                    )
            if cfg is None:
                text = await pool.run(config_text, file_cfg)
                if latest is not None and latest.text == text:
                    version = latest.version
                else:
                    version = await store.save_config(text, "yaml", "file")
                cfg = _with_version(file_cfg, version)
                logger.info(
                    "configuration %r from %s, version %d",
                    cfg.name,
                    settings.config_path,
                    version,
                    extra={"config_version": version, "config_source": "file"},
                )
            scenario: Scenario | None = None
            scenario_version: int | None = None
            if settings.scenario_path is not None:
                scenario = await pool.run(load_scenario, settings.scenario_path)
                scenario.validate_against(cfg)
                text = dump_scenario(scenario)
                latest_scenario = await store.latest_scenario()
                if latest_scenario is not None and latest_scenario.text == text:
                    scenario_version = latest_scenario.version
                else:
                    scenario_version = await store.save_scenario(text, scenario.name, "file")
                logger.info(
                    "simulating scenario %r from %s, version %d",
                    scenario.name,
                    settings.scenario_path,
                    scenario_version,
                    extra={"sim_version": scenario_version},
                )
        except BaseException:
            if own_store:
                await store.close()
            await pool.close()
            raise
        return cls(
            settings,
            cfg,
            scenario,
            store,
            config_source=source,
            scenario_version=scenario_version,
            presets_dir=presets_dir if presets_dir is not None else default_presets_dir(),
            pool=pool,
            clock=clock,
        )

    # ------------------------------------------------------------------ properties

    @property
    def settings(self) -> Settings:
        """Deployment settings."""
        return self._settings

    @property
    def config(self) -> SystemConfig:
        """The configuration in force (its ``version`` equals ``config_version``)."""
        return self._config

    @property
    def config_version(self) -> int:
        """Version of the configuration in force."""
        return self._config_version

    @property
    def config_source(self) -> str:
        """``file`` (the command-line file) or ``api`` (saved by the operator)."""
        return self._config_source

    @property
    def scenario(self) -> Scenario | None:
        """The simulated world, or None when not simulating."""
        return self._scenario

    @property
    def scenario_version(self) -> int:
        """Version of the simulated world (scenario plus the configuration it runs on);
        0 when not simulating."""
        return self._scenario_version

    @property
    def simulating(self) -> bool:
        """Whether the server runs a simulated world."""
        return self._scenario is not None

    @property
    def store(self) -> Store:
        """The store."""
        return self._store

    @property
    def pool(self) -> WorkPool:
        """The worker pool for CPU-heavy work (tasking and the API use it too)."""
        return self._pool

    @property
    def console(self) -> ConsoleBus:
        """Console fan-out (the ``/ws/ui`` handler subscribes here)."""
        return self._console

    @property
    def events(self) -> EventBook:
        """The event book (tasking's mutation API)."""
        return self._book

    @property
    def correlator(self) -> Correlator:
        """The correlator (``sightings(event_id)`` for tasking)."""
        return self._correlator

    @property
    def tasking(self) -> Tasking | None:
        """Capture tasking, correlation and solving (created by ``start``; None before)."""
        return self._tasking

    @property
    def detection(self) -> DetectionPipeline:
        """The detection pipeline."""
        return self._detection

    @property
    def hub(self) -> Any:
        """The hub given to ``start`` (None before)."""
        return self._hub

    @property
    def presets_dir(self) -> Path | None:
        """Directory of preset scenarios."""
        return self._presets_dir

    # ------------------------------------------------------------------ hub providers

    def hub_providers(self) -> HubProviders:
        """The providers ``create_app`` passes to the hub."""
        return HubProviders(
            assignments_for=self.assignments_for,
            config_version=lambda: self._config_version,
            simulation_info=self.simulation_info,
        )

    @staticmethod
    def _build_assignments(cfg: SystemConfig) -> dict[str, list[ChannelAssignment]]:
        try:
            return {
                site.id: [
                    ChannelAssignment(
                        channel_id=channel.id,
                        antenna_summary=channel.antenna_summary,
                        monitors=[
                            MonitorAssignment(
                                monitor_id=m.id,
                                center_hz=m.center_hz,
                                span_hz=m.span_hz,
                                nfft=m.nfft,
                                navg=m.navg,
                                frame_rate_hz=m.frame_rate_hz,
                            )
                            for m in channel.monitors
                        ],
                    )
                    for channel in site.channels
                ]
                for site in cfg.sites
            }
        except ValidationError as exc:
            raise ConfigInvalid(
                [{"loc": "sites", "msg": f"cannot be sent to the nodes: {e['msg']}"} for e in exc.errors()]
            ) from exc

    def assignments_for(self, site_id: str) -> list[ChannelAssignment] | None:
        """The channels the node of a site must open, or None for a site the
        configuration does not have."""
        channels = self._assignments.get(site_id)
        return None if channels is None else list(channels)

    def simulation_info(self) -> dict[str, Any]:
        """The ``simulation`` object of ``welcome``."""
        if self._scenario is None:
            return {"enabled": False}
        return {"enabled": True, "scenario_url": SIM_BACKPLANE_PATH, "version": self._scenario_version}

    def backplane_document(self) -> dict[str, Any]:
        """The simulation backplane document nodes fetch at ``SIM_BACKPLANE_PATH``:
        ``{version, effective_ns (decimal string), config, scenario}``. Raises
        ``NotSimulating``."""
        if self._scenario is None:
            raise NotSimulating()
        cached = self._backplane_cache
        if cached is not None and cached[0] == self._scenario_version:
            return cached[1]
        document = {
            "version": self._scenario_version,
            "effective_ns": str(self._sim_effective_ns),
            "config": self._config.export_dict(),
            "scenario": self._scenario.model_dump(mode="json"),
        }
        self._backplane_cache = (self._scenario_version, document)
        return document

    def node_site_for_token(self, token: str | None) -> str | None:
        """The configured site whose node token ``token`` is, or None (for the
        backplane route, whose bearer token names no site). Every site is compared, in
        constant time each, so the answer's timing does not reveal which matched."""
        if not isinstance(token, str) or not token:
            return None
        found: str | None = None
        for site in self._config.sites:
            if self._settings.check_node_token(site.id, token) and found is None:
                found = site.id
        return found

    def _is_online(self, site_id: str) -> bool:
        hub = self._hub
        return bool(hub is not None and hub.is_online(site_id))

    # ------------------------------------------------------------------ lifecycle

    async def start(self, hub: Any) -> None:
        """Attach the hub and start the pipeline: store reconciliation, event history,
        detection windows, hub subscriptions, the status and quality loops. Call once,
        on the serving event loop, after ``hub.start()``."""
        if self._state != "new":
            raise RuntimeError(f"start: the runtime is {self._state}")
        self._state = "starting"
        self._hub = hub
        await self._reconcile_store()
        await self._load_history()
        await self._rebuild_pipeline(self._config, self._config_version)
        self._tasking = Tasking(
            hub=hub,
            book=self._book,
            config=self._config,
            pool=self._pool,
            sightings=self._correlator.sightings,
            store=self._store,
            clock=self._clock,
            simulated=lambda: self.simulating,
        )
        self._tasking.start()
        self._subscriptions.append(hub.subscribe_psd(self._detection.on_psd, name="detection"))
        self._subscriptions.append(hub.subscribe_status(self._on_node_status, name="runtime"))
        if callable(getattr(hub, "set_window_health", None)):
            hub.set_window_health(self._detection.window_health)  # node status shows blind windows
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._status_loop(), name="petal-runtime-status"),
            loop.create_task(self._quality_loop(), name="petal-runtime-quality"),
            loop.create_task(self._lag_loop(), name="petal-runtime-lag"),
        ]
        self._state = "running"
        try:
            await hub.reassign()
        except Exception:  # nodes get the configuration at their next hello anyway
            logger.exception("reassign at start failed")
        logger.info(
            "runtime started: configuration %r version %d (%s), %s",
            self._config.name,
            self._config_version,
            self._config_source,
            f"simulating version {self._scenario_version}" if self.simulating else "not simulating",
            extra={"config_version": self._config_version, "simulating": self.simulating},
        )

    async def stop(self) -> None:
        """Stop the pipeline, publish pending event changes, close the pool and the
        store. Idempotent. Call before ``hub.close()``."""
        if self._state in ("stopping", "stopped"):
            return
        self._state = "stopping"
        for subscription in self._subscriptions:
            task = getattr(subscription, "_task", None)
            subscription.unsubscribe()
            if isinstance(task, asyncio.Task):
                await asyncio.gather(task, return_exceptions=True)
        self._subscriptions.clear()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # No spectrum frame is in flight any more (the subscription ended above): the
        # thread is idle, so this returns at once.
        await asyncio.to_thread(self._spectrum.shutdown, wait=True)
        if self._tasking is not None:
            await self._tasking.stop()  # cancels rounds before the pool and store close
        try:
            self._book.flush(force=True)
        except Exception:  # shutdown must go on
            logger.exception("final event flush failed")
        await self._pool.close()
        await self._store.close()
        self._console.close()
        self._state = "stopped"
        lag = self.loop_lag_stats()
        logger.info(
            "runtime stopped (event loop lag: worst %.0f ms, %d of %d probes over 50 ms)",
            lag["max_s"] * 1e3,
            lag["over_50ms"],
            lag["samples"],
            extra={"loop_lag": lag},
        )

    async def _reconcile_store(self) -> None:
        """Make sure the store holds the configuration (and scenario) in force under
        their versions; store them under new versions otherwise."""
        text = await self._pool.run(config_text, self._config)
        latest = await self._store.latest_config()
        if latest is None or latest.version != self._config_version or latest.text != text:
            source = self._config_source if self._config_source in ("file", "api") else "file"
            version = await self._store.save_config(text, "yaml", source)  # type: ignore[arg-type]
            self._config = _with_version(self._config, version)
            self._config_version = version
        if self._scenario is not None:
            scenario_text = dump_scenario(self._scenario)
            latest_scenario = await self._store.latest_scenario()
            if (
                latest_scenario is None
                or latest_scenario.version != self._scenario_version
                or latest_scenario.text != scenario_text
            ):
                self._scenario_version = await self._store.save_scenario(scenario_text, self._scenario.name, "file")

    async def _load_history(self) -> None:
        stored = await self._store.load_events(HISTORY_LIMIT)
        highest = await self._store.max_event_seq()
        self._book.ensure_seq(highest)
        loaded = self._book.load_history((e.seq, e.created_ns, e.doc) for e in reversed(stored))
        if loaded:
            logger.info("%d event(s) of earlier runs loaded as history", len(loaded), extra={"events": len(loaded)})
        self._book.flush()

    async def _build_inputs(
        self, cfg: SystemConfig
    ) -> tuple[dict[WindowKey, WindowSpec], QualityTable, dict[WindowKey, Any]]:
        t_s = self._clock() / 1e9
        specs = await self._pool.run(build_window_specs, cfg, t_s)
        quality = await self._pool.run(QualityTable.build, cfg, t_s)
        detection_cfg = cfg.processing.detection
        trackers = await self._pool.run(build_trackers, detection_cfg, self._detection.needs_tracker(detection_cfg, specs))
        return specs, quality, trackers

    def _install(
        self,
        cfg: SystemConfig,
        specs: Mapping[WindowKey, WindowSpec],
        quality: QualityTable,
        trackers: Mapping[WindowKey, Any],
    ) -> None:
        self._detection.install(cfg.processing.detection, specs, trackers)
        self._correlator.reconfigure(cfg, specs, quality)

    async def _rebuild_pipeline(self, cfg: SystemConfig, version: int) -> None:
        specs, quality, trackers = await self._build_inputs(cfg)
        if version != self._config_version:
            return  # a newer configuration arrived meanwhile; it builds its own
        self._install(cfg, specs, quality, trackers)

    # ------------------------------------------------------------------ loops

    async def _status_loop(self) -> None:
        while True:
            try:
                self._correlator.tick()
                self._book.prune(REOPEN_WINDOW_S)
                self._maybe_check_orbits()
                self._publish_status()
                self._status_failures = 0
            except Exception:  # keep the console informed whatever one tick did
                self._status_failures += 1
                if self._status_failures <= 3:
                    logger.exception("status tick failed")
            await asyncio.sleep(STATUS_INTERVAL_S)

    async def _lag_loop(self) -> None:
        """Measure how late the event loop wakes a 100 ms sleep: the time something held
        the loop (or the GIL). Kept in ``loop_lag_stats()``; a lag beyond
        ``LOOP_LAG_WARN_S`` is logged (at most once a minute, with the count since)."""
        loop = asyncio.get_running_loop()
        reported_at = -math.inf
        unreported = 0
        while True:
            started = loop.time()
            await asyncio.sleep(LOOP_LAG_PROBE_S)
            lag = max(0.0, loop.time() - started - LOOP_LAG_PROBE_S)
            stats = self._lag
            stats["samples"] += 1
            stats["max_s"] = max(stats["max_s"], lag)
            if lag > 0.05:
                stats["over_50ms"] += 1
            if lag > LOOP_LAG_WARN_S:
                unreported += 1
                if loop.time() - reported_at >= 60.0:
                    logger.warning(
                        "event loop held for %.0f ms (%d time(s) over %.0f ms since the last report)",
                        lag * 1e3,
                        unreported,
                        LOOP_LAG_WARN_S * 1e3,
                        extra={"lag_ms": round(lag * 1e3, 1), "count": unreported},
                    )
                    reported_at, unreported = loop.time(), 0

    def loop_lag_stats(self) -> dict[str, float]:
        """Event-loop lag seen by the probe: ``samples``, ``max_s``, ``over_50ms``."""
        return dict(self._lag)

    async def _quality_loop(self) -> None:
        while True:
            await asyncio.sleep(QUALITY_REFRESH_S)
            version, cfg = self._config_version, self._config
            try:
                table = await self._pool.run(QualityTable.build, cfg, self._clock() / 1e9)
            except RuntimeError:
                return  # the pool closed: shutting down
            except Exception:
                logger.exception("downlink quality refresh failed")
                continue
            if version == self._config_version:
                self._correlator.set_quality(table)

    def _on_node_status(self, event: Any) -> None:
        """Hub status subscriber: node sessions to the store, verdicts re-evaluated. A new
        session also resets the frame times of the site's detection windows (its clock
        may have been corrected while it was away)."""
        status = dict(event.status or {})
        if event.kind == "online":
            self._detection.reset_site(event.site_id)
        try:
            if event.kind == "online":
                self._store.node_session_opened(
                    event.site_id, status.get("node_id"), int(event.t_ns), status.get("driver"), status.get("software")
                )
            elif event.kind == "offline":
                self._store.node_session_closed(event.site_id, int(event.t_ns), str(event.reason))
        except StoreError as exc:
            logger.warning("node session of %s not stored: %s", event.site_id, exc, extra={"site_id": event.site_id})
        self._correlator.refresh()
        self._publish_status()

    # ------------------------------------------------------------------ publication

    def _publish_event(self, publication: Publication) -> None:
        record = publication.record
        self._console.publish_text(f"event:{record.id}", '{"type":"event","event":' + publication.text + "}")
        event = publication.event
        now = self._clock()
        try:
            self._store.put_event(
                event_id=record.id,
                seq=record.seq,
                created_ns=record.created_ns,
                updated_ns=now,
                state=event.state,
                verdict=event.verdict,
                first_seen_ns=event.first_seen_ns,
                last_seen_ns=event.last_seen_ns,
                uplink_lo_hz=event.uplink_lo_hz,
                uplink_hi_hz=event.uplink_hi_hz,
                doc=publication.text,
            )
            if publication.new_transitions:
                self._store.add_transitions(
                    record.id, [(t.t_ns, t.state, t.verdict, t.reason) for t in publication.new_transitions]
                )
            if publication.evidence_changed and record.evidence:
                self._store.add_evidence_snapshot(record.id, now, evidence_json(record.evidence))
            for entry in publication.new_measurements:
                self._store.add_measurement(
                    record.id, entry.seq, entry.row.t_ns, entry.row.kind, entry.row.used, entry.row.model_dump_json()
                )
            for t_ns, converged, text in publication.new_solutions:
                self._store.add_solution(record.id, t_ns, converged, text)
        except StoreError as exc:
            logger.warning("event %s not stored: %s", record.id, exc, extra={"event_id": record.id})

    def _nodes(self) -> list[schemas.NodeStatus]:
        site_ids = [site.id for site in self._config.sites]
        raw: list[dict[str, Any]]
        if self._hub is None:
            raw = [
                {
                    "site_id": s,
                    "node_id": None,
                    "online": False,
                    "latency_ms": None,
                    "clock": None,
                    "driver": None,
                    "software": None,
                    "channels": [],
                    "connected_since": None,
                }
                for s in site_ids
            ]
        else:
            raw = self._hub.node_status(site_ids)
        nodes: list[schemas.NodeStatus] = []
        for entry in raw:
            try:
                nodes.append(schemas.NodeStatus.model_validate(entry))
            except ValidationError as exc:
                logger.warning("node status of %s not reportable: %s", entry.get("site_id"), exc.errors()[:1])
                nodes.append(
                    schemas.NodeStatus(
                        site_id=str(entry.get("site_id")),
                        node_id=None,
                        online=bool(entry.get("online")),
                        latency_ms=None,
                        clock=None,
                        driver=None,
                        software=None,
                        channels=[],
                        connected_since=None,
                    )
                )
        return nodes

    def _queue(self) -> schemas.QueueStatus:
        pending = 0
        if self._tasking is not None:
            # Exact: captures tasking requested and the hub has not answered yet. (The
            # hub's counters also count refusals that never became requests.)
            pending = self._tasking.captures_in_flight
        elif self._hub is not None:
            counters = self._hub.counters()
            finished = counters.get("capture_completed", 0) + counters.get("capture_cancelled", 0)
            finished += sum(v for k, v in counters.items() if k.startswith("capture_failed_"))
            pending = max(0, counters.get("capture_requested", 0) - finished)
        return schemas.QueueStatus(
            captures_pending=pending, jobs_running=self._pool.running, jobs_waiting=self._pool.waiting
        )

    def _marking(self) -> schemas.Marking | None:
        marking = self._settings.marking
        return None if marking is None else schemas.Marking(level=marking.level, line=marking.line)

    def status(self) -> schemas.Status:
        """``GET /status``."""
        return schemas.Status(
            server_t_ns=self._clock(),
            config_version=self._config_version,
            nodes=self._nodes(),
            events_active=self._book.count_active(),
            queue=self._queue(),
            satellites=list(self._satellites),
            site_clocks=self._site_clocks(),
        )

    def status_message(self) -> schemas.WsStatusMessage:
        """The ``status`` message of ``/ws/ui``."""
        return schemas.WsStatusMessage(
            server_t_ns=self._clock(),
            config_version=self._config_version,
            marking=self._marking(),
            simulation=self.simulating,
            nodes=self._nodes(),
            events_active=self._book.count_active(),
            queue=self._queue(),
            satellites=list(self._satellites),
            site_clocks=self._site_clocks(),
        )

    def _site_clocks(self) -> list[schemas.SiteClockEstimate]:
        """Clock offsets estimated from the calibrations tasking holds (UX-10)."""
        if self._tasking is None:
            return []
        try:
            return self._tasking.site_clock_estimates()
        except (ValueError, ValidationError, np.linalg.LinAlgError) as exc:  # a diagnostic: never break status
            logger.warning("site clock estimates not available: %s", exc)
            return []

    def _maybe_check_orbits(self) -> None:
        now = self._clock()
        last = self._orbit_checked
        if last is None or last[0] != self._config_version or now - last[1] >= int(ORBIT_CHECK_S * 1e9):
            self.check_orbits(now)

    def check_orbits(self, now_ns: int | None = None) -> list[schemas.SatelliteStatus]:
        """Evaluate every configured satellite's orbit at the server time (ADAPT-7) and
        keep the result for ``status``: a satellite whose orbit cannot be evaluated (an
        ephemeris table that ended or has not started, a TLE that fails to propagate) gets
        ``ephemeris_ok = false`` and a message; becoming unavailable is logged at WARNING
        once, coming back at INFO. Runs every ``ORBIT_CHECK_S`` and on a configuration
        change. Returns the rows."""
        now = self._clock() if now_ns is None else int(now_ns)
        cfg, version = self._config, self._config_version
        rows: list[schemas.SatelliteStatus] = []
        for satellite in cfg.satellites:
            try:
                orbit_position(cfg.orbit(satellite.id), now / 1e9)
            except ValueError as exc:
                start, end = _table_span_ns(satellite.orbit)
                before = start is not None and now < start
                since = self._orbit_down.get(satellite.id)
                if since is None:
                    since = end if end is not None and now > end else now
                    self._orbit_down[satellite.id] = since
                    logger.warning(
                        "satellite %s: ephemeris not available (%s)",
                        satellite.id,
                        exc,
                        extra={"sat_id": satellite.id, "since_ns": str(since)},
                    )
                when = f"until {iso_from_ns(start)}" if before and start is not None else f"since {iso_from_ns(since)}"
                rows.append(
                    schemas.SatelliteStatus(
                        sat_id=satellite.id,
                        ephemeris_ok=False,
                        unavailable_since_ns=None if before else since,
                        message=(
                            f"satellite {satellite.name} ({satellite.id}): ephemeris not available {when} ({exc}); "
                            "its pairs are not tasked and nothing is located through it until the ephemeris is updated"
                        ),
                    )
                )
                continue
            if self._orbit_down.pop(satellite.id, None) is not None:
                logger.info("satellite %s: ephemeris available again", satellite.id, extra={"sat_id": satellite.id})
            rows.append(schemas.SatelliteStatus(sat_id=satellite.id, ephemeris_ok=True))
        known = {s.id for s in cfg.satellites}
        for sat_id in [s for s in self._orbit_down if s not in known]:
            del self._orbit_down[sat_id]
        self._satellites = rows
        self._orbit_checked = (version, now)
        return rows

    def _publish_status(self) -> None:
        self._console.publish_text("status", self.status_message().model_dump_json())

    def meta(self) -> schemas.Meta:
        """``GET /meta``."""
        return schemas.Meta(
            version=__version__,
            marking=self._marking(),
            simulation=self.simulating,
            server_t_ns=self._clock(),
            config_name=self._config.name,
            api_token_required=self._settings.api_token is not None,
        )

    # ------------------------------------------------------------------ advisory, truth

    def _advise(self, record: EventRecord) -> schemas.Advisory:
        return advise(record, self._config, simulated=self.simulating)

    def _truth(self, record: EventRecord) -> schemas.EventTruth | None:
        if self._scenario is None:
            return None
        key = (self._scenario_version, self._config_version)
        if self._truth_cache is None or self._truth_cache[0] != key:
            self._truth_cache = (key, resolve_truth_emitters(self._scenario, self._config))
        return event_truth(record, self._truth_cache[1])

    # ------------------------------------------------------------------ configuration

    def config_envelope(self) -> schemas.ConfigEnvelope:
        """``GET /config`` (the ETag is ``config_version``; warnings are added by
        ``replace_config``)."""
        return schemas.ConfigEnvelope(
            version=self._config_version,
            config=self._config,
            warnings=[],
            derived_monitors=_derived_channels(self._config),
        )

    @staticmethod
    def _parse_config(new: SystemConfig | Mapping[str, Any] | str, fmt: str) -> SystemConfig:
        """Validate a configuration (blocking). Raises ``ConfigInvalid``."""
        if isinstance(new, SystemConfig):
            return new
        if isinstance(new, str):
            try:
                return loads_config(new, fmt=fmt, source="request")
            except ConfigError as exc:
                raise ConfigInvalid(exc.errors or [{"loc": "(root)", "msg": str(exc)}]) from exc
        if isinstance(new, Mapping):
            try:
                return SystemConfig.model_validate(dict(new))
            except ValidationError as exc:
                raise ConfigInvalid(field_errors(exc)) from exc
        raise ConfigInvalid([{"loc": "(root)", "msg": f"expected a configuration object, got {type(new).__name__}"}])

    def _scenario_fits(self, cfg: SystemConfig) -> list[dict[str, str]]:
        if self._scenario is None:
            return []
        try:
            self._scenario.validate_against(cfg)
        except ScenarioError as exc:
            return [
                {
                    "loc": "(scenario)",
                    "msg": f"the running simulation scenario does not fit this configuration: {exc}",
                }
            ]
        return []

    async def validate_config(
        self, new: SystemConfig | Mapping[str, Any] | str, *, fmt: str = "yaml"
    ) -> schemas.ValidateResult:
        """``POST /config/validate``: errors and warnings, nothing changed."""
        try:
            cfg = await self._pool.run(self._parse_config, new, fmt)
        except ConfigInvalid as exc:
            return schemas.ValidateResult(
                ok=False, errors=[schemas.FieldProblem(**e) for e in exc.errors], warnings=[]
            )
        errors = self._scenario_fits(cfg)
        try:
            self._build_assignments(cfg)
        except ConfigInvalid as exc:
            errors.extend(exc.errors)
        warnings = await self._pool.run(cfg.warnings, self._clock() / 1e9)
        return schemas.ValidateResult(
            ok=not errors,
            errors=[schemas.FieldProblem(**e) for e in errors],
            warnings=[schemas.FieldProblem(**w) for w in warnings],
        )

    async def replace_config(
        self,
        new: SystemConfig | Mapping[str, Any] | str,
        if_match: str | int | None,
        *,
        fmt: str = "yaml",
    ) -> schemas.ConfigEnvelope:
        """``PUT /config``: replace the configuration.

        new: a ``SystemConfig``, its JSON object, or YAML / JSON text (``fmt``). Its own
            ``version`` field is ignored: the new version is one more than the newest
            stored.
        if_match: the ``If-Match`` header (see ``parse_if_match``).
        Steps: version check, validation (and the running scenario must still fit),
        pipeline inputs built off the loop, persistence, then the swap, ``hub.reassign``,
        the console ``config`` message and, when simulating, a new world version
        announced with ``sim_update`` (effective 2 s from now) and the ``sim`` message.
        Returns the new envelope with the configuration's warnings.
        Raises ``PreconditionRequired``, ``VersionConflict``, ``ConfigInvalid``,
        ``StoreError`` (nothing changed in each case).
        """
        try:
            accepted = parse_if_match(if_match)
        except VersionConflict as exc:
            raise VersionConflict(self._config_version, str(exc)) from None
        async with self._lock:
            current = self._config_version
            if accepted is not None and current not in accepted:
                raise VersionConflict(current)
            cfg = await self._pool.run(self._parse_config, new, fmt)
            problems = self._scenario_fits(cfg)
            if problems:
                raise ConfigInvalid(problems)
            assignments = self._build_assignments(cfg)
            text = await self._pool.run(config_text, cfg)
            specs, quality, trackers = await self._build_inputs(cfg)
            warnings = await self._pool.run(cfg.warnings, self._clock() / 1e9)
            scenario_version: int | None = None
            if self._scenario is not None:
                scenario_version = await self._store.save_scenario(
                    dump_scenario(self._scenario), self._scenario.name, "config"
                )
            version = await self._store.save_config(text, "yaml", "api")
            cfg = _with_version(cfg, version)
            # The swap: no await from here until every part sees the new configuration.
            self._config = cfg
            self._config_version = version
            self._config_source = "api"
            self._assignments = assignments
            self._truth_cache = None
            self._backplane_cache = None
            if scenario_version is not None:
                self._scenario_version = scenario_version
                self._sim_effective_ns = self._clock() + SIM_UPDATE_LEAD_NS
            self._install(cfg, specs, quality, trackers)
            if self._tasking is not None:
                self._tasking.reconfigure(cfg)
            logger.info(
                "configuration replaced: version %d (%r)",
                version,
                cfg.name,
                extra={"config_version": version, "config_source": "api"},
            )
            await self._after_config_change(scenario_version)
        return schemas.ConfigEnvelope(
            version=version,
            config=cfg,
            warnings=[schemas.FieldProblem(**w) for w in warnings],
            derived_monitors=_derived_channels(cfg),
        )

    async def _after_config_change(self, scenario_version: int | None) -> None:
        if self._hub is not None:
            try:
                await self._hub.reassign()
            except Exception:  # the change is applied; nodes catch up at their next hello
                logger.exception("reassign after the configuration change failed")
        self._console.publish_text(
            "config", schemas.WsConfigMessage(config_version=self._config_version).model_dump_json()
        )
        if scenario_version is not None:
            await self._announce_sim()

    # ------------------------------------------------------------------ scenario

    def scenario_envelope(self) -> schemas.ScenarioEnvelope:
        """``GET /sim/scenario``. Raises ``NotSimulating``."""
        if self._scenario is None:
            raise NotSimulating()
        return schemas.ScenarioEnvelope(version=self._scenario_version, scenario=self._scenario)

    async def _announce_sim(self) -> None:
        if self._hub is not None:
            try:
                await self._hub.announce_sim_update(self._scenario_version, self._sim_effective_ns)
            except Exception:  # nodes fetch the world again at their next hello anyway
                logger.exception("sim_update announcement failed")
        self._console.publish_text("sim", schemas.WsSimMessage(version=self._scenario_version).model_dump_json())
        for record in self._book.active():
            self._book.touch(record, "numeric")  # truth may have changed

    @staticmethod
    def _parse_scenario(new: Scenario | Mapping[str, Any] | str) -> Scenario:
        if isinstance(new, Scenario):
            return new
        try:
            if isinstance(new, str):
                return loads_scenario(new)
            if isinstance(new, Mapping):
                return Scenario.model_validate(dict(new))
        except (ValidationError, ScenarioError) as exc:
            raise ScenarioInvalid(_scenario_errors(exc)) from exc
        raise ScenarioInvalid([{"loc": "(root)", "msg": f"expected a scenario object, got {type(new).__name__}"}])

    async def replace_scenario(
        self, new: Scenario | Mapping[str, Any] | str, *, source: str = "api"
    ) -> schemas.ScenarioEnvelope:
        """``PUT /sim/scenario``: replace the simulated world.

        new: a ``Scenario``, its JSON object or YAML / JSON text. source: what to
        record as the cause (``api``, ``preset``, ``reset``).
        The scenario is validated and checked against the configuration, stored under
        a new version and announced to the nodes with ``sim_update`` (effective 2 s from
        now); the console gets ``sim``. Raises ``NotSimulating``, ``ScenarioInvalid``,
        ``StoreError``.
        """
        if self._scenario is None:
            raise NotSimulating()
        scenario = self._parse_scenario(new)
        try:
            scenario.validate_against(self._config)
        except ScenarioError as exc:
            raise ScenarioInvalid(_scenario_errors(exc)) from exc
        async with self._lock:
            try:
                scenario.validate_against(self._config)  # the configuration may have changed meanwhile
            except ScenarioError as exc:
                raise ScenarioInvalid(_scenario_errors(exc)) from exc
            text = dump_scenario(scenario)
            version = await self._store.save_scenario(text, scenario.name, source)
            self._scenario = scenario
            self._scenario_version = version
            self._sim_effective_ns = self._clock() + SIM_UPDATE_LEAD_NS
            self._truth_cache = None
            self._backplane_cache = None
            if self._tasking is not None:  # calibrations of the old world must not correct the new one
                self._tasking.world_changed(self._sim_effective_ns)
            logger.info(
                "scenario replaced (%s): %r, version %d",
                source,
                scenario.name,
                version,
                extra={"sim_version": version, "source": source},
            )
            await self._announce_sim()
        return schemas.ScenarioEnvelope(version=version, scenario=scenario)

    def _preset_path(self, name: str) -> Path:
        if not isinstance(name, str) or not _PRESET_NAME.match(name) or self._presets_dir is None:
            raise PresetNotFound(str(name))
        for suffix in _PRESET_SUFFIXES:
            path = self._presets_dir / f"{name}{suffix}"
            if path.is_file():
                return path
        raise PresetNotFound(name)

    async def presets(self) -> list[schemas.Preset]:
        """``GET /sim/presets``: every scenario file in the presets directory (name = file
        stem). A file that does not load is listed with its error as description.
        Raises ``NotSimulating``."""
        if self._scenario is None:
            raise NotSimulating()
        directory = self._presets_dir
        if directory is None:
            return []

        def scan() -> list[schemas.Preset]:
            out: list[schemas.Preset] = []
            for path in sorted(directory.iterdir()):
                if path.suffix not in _PRESET_SUFFIXES or not _PRESET_NAME.match(path.stem):
                    continue
                try:
                    scenario = load_scenario(path)
                    description = scenario.description.strip() or scenario.name
                except ScenarioError as exc:
                    description = f"cannot be loaded: {exc}"
                out.append(schemas.Preset(name=path.stem, description=description))
            return out

        try:
            return await self._pool.run(scan)
        except OSError as exc:
            logger.warning("presets directory %s cannot be read: %s", directory, exc)
            return []

    async def load_preset(self, name: str) -> schemas.ScenarioEnvelope:
        """``POST /sim/presets/{name}/load``. Raises ``NotSimulating``,
        ``PresetNotFound``, ``ScenarioInvalid``, ``StoreError``."""
        if self._scenario is None:
            raise NotSimulating()
        path = self._preset_path(name)
        try:
            scenario = await self._pool.run(load_scenario, path)
        except ScenarioError as exc:
            raise ScenarioInvalid([{"loc": "(root)", "msg": str(exc)}]) from exc
        return await self.replace_scenario(scenario, source="preset")

    async def reset_scenario(self) -> schemas.ScenarioEnvelope:
        """``POST /sim/reset``: back to the scenario the server started with."""
        if self._initial_scenario is None:
            raise NotSimulating()
        return await self.replace_scenario(self._initial_scenario, source="reset")

    # ------------------------------------------------------------------ events

    _STATES: Final[frozenset[str]] = frozenset(
        {"detected", "characterizing", "locating", "located", "lost", "closed", "active"}
    )

    def list_events(self, state: str | None = None, limit: int = 100) -> list[schemas.EventSummary]:
        """``GET /events?state=&limit=``: newest first. ``state`` is an event state or
        ``active`` (every active state). Raises ValueError naming a bad argument."""
        if state is not None and state not in self._STATES:
            raise ValueError(f"state: must be one of {sorted(self._STATES)}, got {state!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit: must be an integer from 1 to 1000")
        out: list[schemas.EventSummary] = []
        for record in self._book.records():
            if state == "active":
                if not record.active:
                    continue
            elif state is not None and record.state != state:
                continue
            out.append(self._book.summary(record))
            if len(out) >= limit:
                break
        return out

    def get_event(self, event_id: str) -> schemas.Event:
        """``GET /events/{id}``. Raises ``EventNotFound``."""
        return self._book.to_schema(self._book.require(event_id))

    def close_event(self, event_id: str) -> schemas.Event:
        """``POST /events/{id}/close``. Raises ``EventNotFound``."""
        record = self._book.close(event_id)
        return self._book.to_schema(record)
