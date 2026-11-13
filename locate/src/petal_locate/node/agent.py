"""The node agent: one site's side of PROTOCOL.md section A.

`NodeAgent.run()` dials out to the server, says `hello`, and for as long as the
session lasts: heartbeats, opens one driver per assigned channel and acknowledges the
assignment, streams one spectrum window per monitor, and serves capture requests. When
the connection goes away it cancels what was in flight, waits (exponential backoff
with jitter) and starts again from `hello`. Nothing a server sends, and nothing a
driver does, ends the loop; only `stop()` and a `reject` that cannot be cured by
waiting do.

Structure
---------
* `NodeAgent` owns identity (the persistent `node_id`), the reconnect loop and its
  backoff, and shutdown.
* One `_Session` per connection owns everything that must not outlive it: the open
  drivers, the monitor loops, the capture tasks and the `_Outbox`. A capture task can
  only send through the outbox of the session that received its request, and that
  outbox refuses everything once its session has ended: a `capture_id` of an earlier
  session can never produce `capture_data` on a later connection, whatever its driver
  does.
* `_Outbox` is the single writer of the socket. Control messages go first, then the
  waiting spectrum frame of every window (at most one each), then one capture chunk
  (captures take turns), and the loop yields to the event loop after every pass. That
  is what keeps heartbeats and spectrum frames flowing through an 8 MiB upload, also
  on an uplink that carries only a few chunks a second, and what makes "at most one
  unsent frame per window" true while a server stalls.

Capture requests
----------------
Each request runs in its own task: admission by the channel's `ReadGate`,
`driver.read`, chunking with `protocol.iter_capture_chunks`, upload through the
outbox. The answer is `capture_data` frames or exactly one `capture_error`:

  out_of_range  rate or centre outside the driver's capabilities, or the driver says so
  too_late      `deadline_ns` has passed (on arrival, during the read or during the
                upload), or the driver no longer holds the interval
  overflow      the driver lost samples in the interval
  driver        no open driver for the channel, or the driver failed in any other way
  busy          the node already serves `max_concurrent_captures` requests
  cancelled     `capture_cancel`, or an `assign` removed or changed the channel

Chunk size
----------
A chunk carries `chunk_payload_bytes` of samples, 32 KiB by default, an eighth of the
256 KiB the protocol allows. The reason is liveness on a slow uplink: the server
counts a frame only when all of it has arrived, heartbeats travel behind the chunk in
the same stream, and three heartbeat periods without a frame mark the node offline.
A 256 KiB chunk needs 350 kbit/s to arrive within the 6 s of the default heartbeat; a
32 KiB chunk needs 44 kbit/s, less than a capture needs to meet its deadline at all.
It also bounds how long a heartbeat or a spectrum frame waits behind samples.

Reconnecting
------------
The wait before attempt k (counting failures since the last `welcome`) is
`min(backoff_base_s * 2**k, backoff_max_s)` plus up to 20 % jitter: 1, 2, 4 ... 30 s
by default. A `reject` with `bad_token`, `unknown_site`, `duplicate_node` or a reason
this version does not know cannot be cured by retrying quickly, so the agent goes
straight to the longest wait and stays there; so does a session closed with code 4001
(another process is using this `node_id`). `protocol_mismatch` stops the agent:
`run()` raises `NodeRejected`.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import random
import re
import socket
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, TypeVar
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from petal_locate import __version__
from petal_locate.node.clock import ClockSource, SystemClock
from petal_locate.node.drivers import get_driver
from petal_locate.node.drivers.base import (
    DEFAULT_WORKERS,
    AssignHook,
    CaptureError,
    DriverContext,
    SdrDriver,
    SimUpdateHook,
    WorkerPool,
)
from petal_locate.node.monitor import FrameMailbox, Monitor, ReadGate, SpectrumFrame
from petal_locate.protocol.framing import MAX_CHUNK_PAYLOAD_BYTES, iter_capture_chunks
from petal_locate.protocol.messages import (
    BAD_MESSAGE_LIMIT,
    CAPTURE_ERROR_CODES,
    CLOSE_CODE_REPLACED,
    CLOSE_CODE_SHUTDOWN,
    MAX_CONCURRENT_CAPTURES,
    MAX_ID_CHARS,
    PROTOCOL_VERSION,
    RECONNECT_BACKOFF_MAX_S,
    RECONNECT_BACKOFF_MIN_S,
    RECONNECT_JITTER,
    WS_MAX_SIZE_BYTES,
    Assign,
    AssignAck,
    CaptureCancel,
    CaptureErrorMessage,
    CaptureRequest,
    ChannelAck,
    ChannelAssignment,
    ControlMessage,
    ErrorMessage,
    Heartbeat,
    HeartbeatAck,
    Hello,
    NodeLoad,
    ProtocolError,
    Reject,
    SimUpdate,
    UnknownMessage,
    Welcome,
    dump_control,
    parse_control,
)
from petal_locate.protocol.messages import Capabilities as WireCapabilities

__all__ = [
    "DEFAULT_CHUNK_PAYLOAD_BYTES",
    "REJECT_POLICY",
    "AgentStats",
    "DriverFactory",
    "NodeAgent",
    "NodeRejected",
    "backoff_delay",
    "default_state_path",
    "load_or_create_node_id",
    "normalize_server_url",
]

logger = logging.getLogger(__name__)

DriverFactory = Callable[[ChannelAssignment], SdrDriver]
"""Builds the unopened driver for one assigned channel."""

REJECT_POLICY: Final[dict[str, Literal["stop", "retry_max"]]] = {
    "bad_token": "retry_max",
    "unknown_site": "retry_max",
    "duplicate_node": "retry_max",
    "protocol_mismatch": "stop",
}
"""What the agent does after a `reject`, by reason. "retry_max": keep trying at the
longest backoff (the cure is on the server: a token file, the configuration, the other
node going away). "stop": give up (`run()` raises `NodeRejected`). Reasons not listed
here are treated as "retry_max"."""

DEFAULT_CHUNK_PAYLOAD_BYTES: Final[int] = 32 * 1024
"""Payload of one `capture_data` frame unless the agent is told otherwise, bytes
(4096 complex samples). See "Chunk size" in the module docstring."""

_NODE_PATH: Final[str] = "/ws/node"
_CLOSE_PROTOCOL_ERROR: Final[int] = 1002
_CLOSE_INTERNAL_ERROR: Final[int] = 1011
_MAX_PENDING_HEARTBEATS: Final[int] = 8
_MAX_ERROR_TEXT: Final[int] = 400
_T = TypeVar("_T")


class NodeRejected(Exception):
    """The server refused this node for a reason that waiting cannot cure.

    reason: the `reject` reason ("protocol_mismatch").
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"server rejected the node: {reason}" + (f" ({detail})" if detail else ""))
        self.reason: str = reason
        self.detail: str = detail


class _SessionClosed(Exception):
    """Something tried to send on a session that has ended."""


class _Stopped(Exception):
    """`stop()` was requested while waiting for something else."""


# ------------------------------------------------------------------- pure helpers


def backoff_delay(
    failures: int,
    base_s: float = RECONNECT_BACKOFF_MIN_S,
    max_s: float = RECONNECT_BACKOFF_MAX_S,
    jitter: float = RECONNECT_JITTER,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before the next connection attempt.

    failures: attempts that failed since the last `welcome` (0 for the first wait).
    base_s: the first wait, seconds; it doubles with every failure. max_s: its cap,
    seconds. jitter: the wait grows by a random fraction of itself between 0 and this
    (0.2 = "up to 20 %"). rng: source of the jitter; None uses the module `random`.
    Returns a value in [d, d * (1 + jitter)] with d = min(base_s * 2**failures, max_s).
    """
    if failures < 0:
        raise ValueError(f"failures: must not be negative, got {failures}")
    if not (base_s > 0 and max_s >= base_s):
        raise ValueError(f"base_s, max_s: need 0 < base_s <= max_s, got {base_s!r}, {max_s!r}")
    if not 0 <= jitter <= 1:
        raise ValueError(f"jitter: must be between 0 and 1, got {jitter!r}")
    delay = min(max_s, base_s * 2.0 ** min(failures, 62))
    draw = (rng or random).random()
    return delay * (1.0 + jitter * draw)


def normalize_server_url(server_url: str) -> tuple[str, str]:
    """The WebSocket URL to dial and the HTTP origin of the same server.

    server_url: `ws://`, `wss://`, `http://` or `https://` plus host and optional
    port; a URL without a path gets the node endpoint `/ws/node`.
    Returns `(ws_url, http_origin)`, for example
    `("wss://hq.example:8443/ws/node", "https://hq.example:8443")`.
    Raises `ValueError` naming `server_url` for anything else.
    """
    if not isinstance(server_url, str) or not server_url.strip():
        raise ValueError("server_url: must be a non-empty URL")
    parts = urlsplit(server_url.strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme.lower())
    if scheme is None:
        raise ValueError(f"server_url: scheme must be ws, wss, http or https, got {parts.scheme!r}")
    try:
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise ValueError(f"server_url: {exc}") from exc
    if not host:
        raise ValueError("server_url: no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("server_url: must not carry credentials; the token is a separate option")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = parts.path if parts.path not in ("", "/") else _NODE_PATH
    http_scheme = "https" if scheme == "wss" else "http"
    return urlunsplit((scheme, netloc, path, parts.query, "")), f"{http_scheme}://{netloc}"


def _is_loopback(host: str) -> bool:
    """Whether `host` is this machine (a loopback address or `localhost`)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def default_state_path(site_id: str) -> Path:
    """Where a node keeps its `node_id` when no state file is named.

    `$XDG_STATE_HOME/petal-locate/node-<site>.json`, with `~/.local/state` standing
    in for an unset `XDG_STATE_HOME`; characters of the site id that are awkward in
    a file name are replaced by `_`.
    """
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", site_id) or "site"
    return Path(root) / "petal-locate" / f"node-{safe}.json"


def load_or_create_node_id(path: Path) -> str:
    """The node id kept in the state file `path`, generated and saved on first use.

    The file is JSON `{"node_id": "<uuid>", ...}`. A missing file is created (with
    its directory) atomically with a fresh UUID4. Blocking file I/O.
    Raises `ValueError` naming the file when it exists but does not hold a usable
    id: a node that silently changed identity would be refused as a duplicate.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        node_id = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps({"node_id": node_id, "created_unix_ns": time.time_ns()}) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        logger.info("generated a node id", extra={"node_id": node_id, "state_path": str(path)})
        return node_id
    except OSError as exc:
        raise ValueError(f"{path}: state file cannot be read ({exc.strerror or exc})") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: state file is not valid JSON ({exc})") from exc
    stored = data.get("node_id") if isinstance(data, dict) else None
    if not isinstance(stored, str) or not 1 <= len(stored) <= MAX_ID_CHARS:
        raise ValueError(f"{path}: state file has no usable 'node_id'")
    return stored


def _wire_capabilities(capabilities: Any) -> WireCapabilities:
    """A driver's capabilities as the wire model; `ValueError` when they are unusable."""
    try:
        return WireCapabilities(
            min_hz=float(capabilities.min_hz),
            max_hz=float(capabilities.max_hz),
            max_sample_rate_hz=float(capabilities.max_sample_rate_hz),
            lookback_s=float(capabilities.lookback_s),
            concurrent_reads=int(capabilities.concurrent_reads),
        )
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"driver capabilities are unusable: {exc}") from exc


def _clip(text: str, limit: int = _MAX_ERROR_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ----------------------------------------------------------------------- statistics


@dataclass
class AgentStats:
    """Counters of a `NodeAgent` since it was created (for logs, tests and status).

    connect_attempts: connections dialled. sessions: of those, how many reached
    `welcome`. rejects: `reject` messages received. heartbeats_sent.
    frames_sent: spectrum frames written to the socket. frames_dropped: spectrum
    frames replaced or discarded unsent (newest wins).
    captures_started / captures_completed / captures_failed: capture requests
    accepted, fully uploaded, answered with `capture_error`.
    chunks_sent: `capture_data` frames written. bad_messages: malformed messages
    received. backoff_delays: the most recent reconnect waits, seconds.
    """

    connect_attempts: int = 0
    sessions: int = 0
    rejects: int = 0
    heartbeats_sent: int = 0
    frames_sent: int = 0
    frames_dropped: int = 0
    captures_started: int = 0
    captures_completed: int = 0
    captures_failed: int = 0
    chunks_sent: int = 0
    bad_messages: int = 0
    backoff_delays: deque[float] = field(default_factory=lambda: deque(maxlen=64))


@dataclass(frozen=True)
class _Outcome:
    """How a connection attempt or session ended.

    kind: "stopped" (stop() was called), "connect_failed", "handshake_failed",
    "rejected", "closed" (the socket closed after a welcome), "silent" (the server
    sent nothing for too long), "bad_peer" (three malformed messages in a row),
    "error" (an internal failure, logged where it happened).
    """

    kind: str
    welcomed: bool = False
    reason: str | None = None
    code: int | None = None
    detail: str = ""


# --------------------------------------------------------------------------- outbox


@dataclass(eq=False)
class _Upload:
    """A capture on its way out: its frames, how many went, and who waits for it."""

    capture_id: str
    frames: Iterator[bytes]
    done: asyncio.Future[int]
    sent: int = 0
    withdrawn: bool = False


class _Outbox:
    """Everything a session wants to send, and the one task that sends it.

    Three classes of traffic, in this order of precedence: control messages (FIFO),
    spectrum frames (at most one unsent per window, newest wins; a pass sends the
    frame of every window that had one waiting when it began), capture chunks (one
    per pass, captures in rotation). `run` is the only caller of `ws.send`.

    A pass is therefore bounded: what was waiting when it began, and one chunk. On a
    slow uplink every window still gets a frame out per chunk, where sending a single
    frame per pass would let each window wait for as many chunks as there are windows.
    """

    def __init__(self, stats: AgentStats) -> None:
        self._stats = stats
        self._control: deque[tuple[str, asyncio.Future[None] | None]] = deque()
        self._frames = FrameMailbox()
        self._uploads: deque[_Upload] = deque()
        self._wake = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether the session has ended; nothing is accepted or sent any more."""
        return self._closed

    @property
    def frames(self) -> FrameMailbox:
        """The spectrum-frame mailbox."""
        return self._frames

    def post(self, message: ControlMessage) -> bool:
        """Queue a control message without waiting. Returns False (and drops it) when
        the session has ended."""
        if self._closed:
            logger.debug(
                "control message dropped, session ended", extra={"wire_type": message.type}
            )
            return False
        self._control.append((dump_control(message), None))
        self._wake.set()
        return True

    async def send(self, message: ControlMessage) -> None:
        """Queue a control message and wait until it has been written to the socket.

        Raises `_SessionClosed` when the session ends first.
        """
        if self._closed:
            raise _SessionClosed
        written: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._control.append((dump_control(message), written))
        self._wake.set()
        await written

    def offer_frame(self, frame: SpectrumFrame) -> None:
        """Hand over the newest frame of a window (replaces an unsent older one)."""
        if self._closed:
            return
        if self._frames.offer(frame):
            self._stats.frames_dropped += 1
        self._wake.set()

    def discard_channel(self, channel_id: str) -> None:
        """Forget unsent spectrum frames of a channel that is going away."""
        self._stats.frames_dropped += self._frames.discard_channel(channel_id)

    async def upload(self, capture_id: str, frames: Iterator[bytes]) -> int:
        """Send the frames of one capture in order; returns how many were sent.

        Chunks are produced only when their turn comes, so a queued capture costs no
        memory beyond its samples. Cancelling the caller withdraws what has not been
        written yet. Raises `_SessionClosed` when the session ends first, and
        whatever `frames` raises.
        """
        if self._closed:
            raise _SessionClosed
        item = _Upload(capture_id, frames, asyncio.get_running_loop().create_future())
        self._uploads.append(item)
        self._wake.set()
        try:
            return await item.done
        finally:
            item.withdrawn = True
            if item in self._uploads:
                self._uploads.remove(item)

    def close(self) -> None:
        """End of session: fail everyone who waits and refuse everything from now on."""
        if self._closed:
            return
        self._closed = True
        for _, written in self._control:
            if written is not None and not written.done():
                written.set_exception(_SessionClosed())
        self._control.clear()
        for item in self._uploads:
            if not item.done.done():
                item.done.set_exception(_SessionClosed())
        self._uploads.clear()
        self._frames.clear()
        self._wake.set()

    async def run(self, ws: Any) -> None:
        """Write to `ws` until the session ends (the writer task).

        Raises what `ws.send` raises (`ConnectionClosed` when the socket is gone).
        """
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            busy = True
            while busy and not self._closed:
                busy = False
                while self._control:
                    text, written = self._control.popleft()
                    try:
                        await ws.send(text)
                    except ConnectionClosed:
                        if written is not None and not written.done():
                            written.set_exception(_SessionClosed())
                        raise
                    if written is not None and not written.done():
                        written.set_result(None)
                    busy = True
                # Only as many frames as wait now: windows that produce while these are
                # sent come in the next pass, so the chunk below always gets its turn.
                for _ in range(len(self._frames)):
                    if self._control or self._closed:
                        break  # a heartbeat goes ahead of the remaining frames
                    frame = self._frames.take()
                    if frame is None:
                        break
                    busy = True
                    try:
                        data = frame.encode()
                    except ValueError:
                        logger.exception(
                            "spectrum frame cannot be encoded; dropped",
                            extra={"channel_id": frame.channel_id, "monitor_id": frame.monitor_id},
                        )
                    else:
                        await ws.send(data)
                        self._stats.frames_sent += 1
                if self._uploads and not self._control and not self._closed:
                    busy = True
                    await self._send_chunk(ws, self._uploads[0])
                if busy:
                    # ws.send only suspends when the write buffer is full. Yield every
                    # pass regardless, so that heartbeats, the reader and the monitors
                    # run between chunks even on a link that never pushes back.
                    await asyncio.sleep(0)

    async def _send_chunk(self, ws: Any, item: _Upload) -> None:
        """Send the next chunk of `item` and move it to the back of the rotation."""
        self._uploads.rotate(-1)
        try:
            chunk = next(item.frames, None)
        except ValueError as exc:  # header validation of a chunk; voids the capture
            self._finish(item, error=exc)
            return
        if chunk is None:
            self._finish(item)
            return
        await ws.send(chunk)
        item.sent += 1
        self._stats.chunks_sent += 1

    def _finish(self, item: _Upload, error: BaseException | None = None) -> None:
        if item in self._uploads:
            self._uploads.remove(item)
        if item.done.done():
            return
        if error is None:
            item.done.set_result(item.sent)
        else:
            item.done.set_exception(error)


# -------------------------------------------------------------------------- session


@dataclass(eq=False)
class _Channel:
    """One assigned channel in a session: its driver (when it opened) and its loops."""

    assignment: ChannelAssignment
    driver: SdrDriver | None = None
    capabilities: WireCapabilities | None = None
    gate: ReadGate | None = None
    error: str | None = None
    monitors: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def channel_id(self) -> str:
        return self.assignment.channel_id

    def ack(self) -> ChannelAck:
        """This channel's entry of the `assign_ack`."""
        if self.driver is not None and self.capabilities is not None:
            return ChannelAck(channel_id=self.channel_id, ok=True, capabilities=self.capabilities)
        return ChannelAck(
            channel_id=self.channel_id, ok=False, error=_clip(self.error or "driver did not open")
        )


@dataclass(eq=False)
class _CaptureJob:
    """A capture request being served."""

    request: CaptureRequest
    task: asyncio.Task[None]


class _Session:
    """Everything that lives exactly as long as one welcomed connection."""

    def __init__(self, agent: NodeAgent, ws: Any, welcome: Welcome) -> None:
        self._agent = agent
        self._ws = ws
        self._welcome = welcome
        self._clock = agent.clock
        self._stats = agent.stats
        self._outbox = _Outbox(agent.stats)
        self._channels: dict[str, _Channel] = {}
        # Every driver built in this session whose `close` has not run to its end,
        # with its channel id. The teardown closes what is left here, so a driver
        # cannot stay open because the task that was closing it was cancelled.
        self._drivers: list[tuple[SdrDriver, str]] = []
        self._captures: dict[str, _CaptureJob] = {}
        self._cancel_reasons: dict[str, str] = {}
        # The newest assignment not yet applied. One slot, not a queue: an assign that
        # arrives while an older one still waits replaces it (see `_assign`).
        self._assignment: tuple[int, list[ChannelAssignment]] | None = None
        self._assignment_waiting = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._heartbeats_pending: dict[int, float] = {}
        self._rtt_ms: float | None = None
        self._last_rx = time.monotonic()
        self._closed = False
        simulation = welcome.simulation
        self._ctx = DriverContext(
            site_id=agent.site_id,
            server_http_url=agent.server_http_url,
            token=agent.token,
            simulation=simulation.to_wire() if simulation.enabled else None,
            clock=agent.clock,
            workers=agent.workers,
        )

    # ------------------------------------------------------------------ life cycle

    def _extra(self, **more: Any) -> dict[str, Any]:
        return {"site_id": self._agent.site_id, "node_id": self._agent.node_id, **more}

    def _spawn(self, coro: Coroutine[Any, Any, _T], name: str) -> asyncio.Task[_T]:
        """Start a task that belongs to this session (teardown cancels and awaits it)."""
        task = asyncio.get_running_loop().create_task(coro, name=f"petal-node:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()  # also marks it retrieved
        if exc is not None and not isinstance(exc, (ConnectionClosed, _SessionClosed)):
            logger.error(
                "session task %s failed", task.get_name(), exc_info=exc, extra=self._extra()
            )

    async def run(self) -> _Outcome:
        """Serve the session until it ends; always leaves nothing running behind."""
        self._assign(self._welcome.config_version, list(self._welcome.channels))
        core = [
            self._spawn(self._reader(), "reader"),
            self._spawn(self._outbox.run(self._ws), "writer"),
            self._spawn(self._watchdog(), "watchdog"),
            self._spawn(self._heartbeat_loop(), "heartbeat"),
            self._spawn(self._assigner(), "assigner"),
        ]
        stop_waiter = asyncio.ensure_future(self._agent.stop_requested.wait())
        outcome: _Outcome | None = None  # stays None when this coroutine is cancelled
        try:
            await asyncio.wait([*core, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
            outcome = self._outcome(core)
            return outcome
        finally:
            stop_waiter.cancel()
            await self._teardown(outcome)

    def _outcome(self, core: list[asyncio.Task[Any]]) -> _Outcome:
        """Why the session is ending, from the first core task that finished."""
        if self._agent.stop_requested.is_set():
            return _Outcome("stopped", welcomed=True)
        for task in core:
            if not task.done() or task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, ConnectionClosed):
                return self._closed_outcome()
            if isinstance(exc, _SessionClosed):
                continue
            if exc is not None:
                return _Outcome("error", welcomed=True, detail=f"{type(exc).__name__}: {exc}")
            result = task.result()
            if isinstance(result, _Outcome):
                return result
            return _Outcome("error", welcomed=True, detail=f"{task.get_name()} ended early")
        return _Outcome("error", welcomed=True, detail="no session task reported an outcome")

    def _closed_outcome(self) -> _Outcome:
        code, reason = self._ws.close_code, self._ws.close_reason
        return _Outcome("closed", welcomed=True, code=code, reason=reason or None)

    async def _teardown(self, outcome: _Outcome | None) -> None:
        """Stop every task of the session, close the socket, close the drivers.

        outcome: why the session ends; None when its task was cancelled from outside.
        """
        self._closed = True
        self._outbox.close()
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            _, stuck = await asyncio.wait(tasks, timeout=self._agent.teardown_timeout_s)
            if stuck:
                logger.error(
                    "session tasks ignored cancellation and were abandoned: %s",
                    ", ".join(sorted(task.get_name() for task in stuck)),
                    extra=self._extra(),
                )
        if outcome is None or outcome.kind == "stopped":
            code, reason = CLOSE_CODE_SHUTDOWN, "shutdown"
        elif outcome.kind == "bad_peer":
            code, reason = _CLOSE_PROTOCOL_ERROR, "bad_message"
        elif outcome.kind in ("error", "silent"):
            code, reason = _CLOSE_INTERNAL_ERROR, outcome.kind
        else:
            code, reason = 1000, ""
        await self._agent.close_socket(self._ws, code, reason)
        for channel in self._channels.values():
            channel.driver = None
        self._channels = {}
        # Not only the drivers of the current channels: also one whose channel an
        # assign had just removed, or whose open was still in flight, when the tasks
        # above were cancelled.
        drivers = list(self._drivers)
        if drivers:
            await asyncio.gather(
                *(self._close_quietly(driver, channel_id) for driver, channel_id in drivers)
            )

    # ---------------------------------------------------------------------- reader

    async def _reader(self) -> _Outcome:
        """Receive until the socket closes or the server sends three bad messages."""
        bad_in_a_row = 0
        while True:
            try:
                raw = await self._ws.recv()
            except ConnectionClosed:
                return self._closed_outcome()
            self._last_rx = time.monotonic()
            try:
                if not isinstance(raw, str):
                    raise ProtocolError(
                        "binary frame: a node accepts only control messages (text frames)"
                    )
                message = parse_control(raw)
            except ProtocolError as exc:
                bad_in_a_row += 1
                self._stats.bad_messages += 1
                logger.warning(
                    "malformed message from the server: %s",
                    exc.detail,
                    extra=self._extra(bad_in_a_row=bad_in_a_row),
                )
                reply = exc.to_message()
                if bad_in_a_row >= BAD_MESSAGE_LIMIT:
                    try:
                        async with asyncio.timeout(self._agent.close_timeout_s):
                            await self._outbox.send(reply)
                    except (TimeoutError, _SessionClosed):
                        logger.debug("last error reply was not sent", extra=self._extra())
                    return _Outcome(
                        "bad_peer", welcomed=True, detail="three malformed messages in a row"
                    )
                self._outbox.post(reply)
                continue
            bad_in_a_row = 0
            self._dispatch(message)

    def _dispatch(self, message: ControlMessage | UnknownMessage) -> None:
        """Act on one well-formed message from the server (never blocks)."""
        if isinstance(message, HeartbeatAck):
            self._on_heartbeat_ack(message)
        elif isinstance(message, CaptureRequest):
            self._on_capture_request(message)
        elif isinstance(message, CaptureCancel):
            self._on_capture_cancel(message)
        elif isinstance(message, Assign):
            self._assign(message.config_version, list(message.channels))
        elif isinstance(message, SimUpdate):
            self._on_sim_update(message)
        elif isinstance(message, ErrorMessage):
            logger.warning(
                "server reported an error: %s: %s",
                message.code,
                message.detail,
                extra=self._extra(error_code=message.code),
            )
        elif isinstance(message, UnknownMessage):
            logger.warning(
                "message of unknown type %r ignored",
                _clip(message.type, 64),
                extra=self._extra(),
            )
        else:
            logger.warning(
                "unexpected %s from the server ignored", message.type, extra=self._extra()
            )

    # ------------------------------------------------------- heartbeats and liveness

    def _on_heartbeat_ack(self, ack: HeartbeatAck) -> None:
        sent_at = self._heartbeats_pending.pop(ack.seq, None)
        if sent_at is None:
            logger.debug("heartbeat_ack for an unknown seq", extra=self._extra(seq=ack.seq))
            return
        self._rtt_ms = max(0.0, (time.monotonic() - sent_at) * 1e3)
        self._agent.last_rtt_ms = self._rtt_ms
        for seq in [seq for seq in self._heartbeats_pending if seq < ack.seq]:
            del self._heartbeats_pending[seq]  # their acks were lost; stop waiting

    def _rtt_to_report(self, now: float) -> float | None:
        """`rtt_ms` for a heartbeat sent at monotonic time `now` (seconds).

        The round trip measured last, unless a heartbeat is still unanswered and has
        been for longer than that: then its age, a lower bound of its round trip, so
        that the server is never told a latency the node no longer has.
        """
        if not self._heartbeats_pending:
            return self._rtt_ms
        age_ms = max(0.0, now - min(self._heartbeats_pending.values())) * 1e3
        return age_ms if self._rtt_ms is None else max(self._rtt_ms, age_ms)

    async def _heartbeat_loop(self) -> None:
        """Send `heartbeat` every `heartbeat_s`, the first one at once."""
        period = self._welcome.heartbeat_s
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        cpu_mark, wall_mark = time.process_time(), time.monotonic()
        seq = 0
        while True:
            cpu_now, wall_now = time.process_time(), time.monotonic()
            elapsed = wall_now - wall_mark
            cpu = max(0.0, (cpu_now - cpu_mark) / elapsed) if elapsed > 1e-3 else 0.0
            cpu_mark, wall_mark = cpu_now, wall_now
            heartbeat = Heartbeat(
                seq=seq,
                t_ns=self._clock.now_ns(),
                rtt_ms=self._rtt_to_report(wall_now),
                clock=self._clock.status(),
                load=NodeLoad(cpu=cpu, queue=len(self._captures)),
            )
            self._heartbeats_pending[seq] = wall_now  # the round trip starts here
            while len(self._heartbeats_pending) > _MAX_PENDING_HEARTBEATS:
                del self._heartbeats_pending[min(self._heartbeats_pending)]
            await self._outbox.send(heartbeat)
            self._stats.heartbeats_sent += 1
            seq += 1
            next_at += period
            delay = next_at - loop.time()
            if delay <= 0:
                next_at = loop.time()  # the socket was blocked; do not send a burst
            else:
                await asyncio.sleep(delay)

    async def _watchdog(self) -> _Outcome:
        """End the session when the server has been silent for too long."""
        limit = self._agent.server_silence_timeout_s
        if limit is None:
            limit = max(10.0, 5.0 * self._welcome.heartbeat_s)
        while True:
            silent = time.monotonic() - self._last_rx
            if silent > limit:
                logger.warning(
                    "server silent for %.1f s; reconnecting", silent, extra=self._extra()
                )
                return _Outcome("silent", welcomed=True, detail=f"silent for {silent:.1f} s")
            await asyncio.sleep(max(0.01, limit - silent))

    # ------------------------------------------------------------------ assignment

    def _assign(self, version: int, channels: list[ChannelAssignment]) -> None:
        """Hand an assignment (of the welcome, or of an `assign`) to the assigner.

        Assignments are applied one at a time, and applying one can take as long as a
        driver takes to open. An assignment that arrives while another still waits
        its turn replaces it: the server counts only the acknowledgement of the
        version it assigned last, so the older one would open drivers for nothing,
        and a server that reassigns in a burst cannot grow a queue here.
        """
        superseded = self._assignment
        if superseded is not None:
            logger.info(
                "assignment superseded before it was applied",
                extra=self._extra(config_version=superseded[0], superseded_by=version),
            )
        self._assignment = (version, channels)
        self._assignment_waiting.set()

    async def _assigner(self) -> None:
        """Apply the welcome's assignment and later ones, one at a time; of several
        that wait, only the newest (see `_assign`). Each one applied is answered with
        an `assign_ack`."""
        while True:
            await self._assignment_waiting.wait()
            self._assignment_waiting.clear()
            waiting, self._assignment = self._assignment, None
            if waiting is None:
                continue
            version, wanted = waiting
            try:
                await self._apply_assignment(version, wanted)
            except _SessionClosed:
                return
            except Exception:
                # Deliberately broad: an assignment must be answered whatever went
                # wrong while applying it, and the session must go on.
                logger.exception(
                    "applying an assignment failed", extra=self._extra(config_version=version)
                )
                self._outbox.post(AssignAck(config_version=version, ok=False, channels=[]))

    async def _apply_assignment(self, version: int, wanted: list[ChannelAssignment]) -> None:
        """Make the open channels match `wanted`, then send the `assign_ack`.

        A channel whose assignment is unchanged and whose driver is open is kept as
        it is: its monitors and captures go on. Removed and changed channels are
        closed (their captures answered with `cancelled`); new and changed ones get a
        fresh driver from the factory. Monitors start after the ack is queued.
        """
        wanted_by_id = {assignment.channel_id: assignment for assignment in wanted}
        for channel_id, channel in list(self._channels.items()):
            new = wanted_by_id.get(channel_id)
            if new is not None and new == channel.assignment and channel.driver is not None:
                continue
            reason = f"channel {'removed' if new is None else 'changed'} by an assign"
            del self._channels[channel_id]
            await self._close_channel(channel, reason)
        kept = [self._channels[cid] for cid in wanted_by_id if cid in self._channels]
        opening = [
            self._spawn(self._open_channel(assignment), f"open:{assignment.channel_id}")
            for assignment in wanted
            if assignment.channel_id not in self._channels
        ]
        refreshing = [
            self._spawn(self._refresh_channel(channel), f"refresh:{channel.channel_id}")
            for channel in kept
        ]
        opened: list[_Channel] = list(await asyncio.gather(*opening)) if opening else []
        if refreshing:
            await asyncio.gather(*refreshing)
        acks = [self._channels[assignment.channel_id].ack() for assignment in wanted]
        ack = AssignAck(
            config_version=version, ok=all(entry.ok for entry in acks), channels=acks
        )
        logger.info(
            "assignment applied",
            extra=self._extra(
                config_version=version,
                channels_ok=[entry.channel_id for entry in acks if entry.ok],
                channels_failed=[entry.channel_id for entry in acks if not entry.ok],
            ),
        )
        await self._outbox.send(ack)
        for channel in opened:
            self._start_monitors(channel)

    async def _open_channel(self, assignment: ChannelAssignment) -> _Channel:
        """Build and open the driver of one channel; a failure is recorded, not raised.

        The channel is entered in `self._channels` here, in the same step in which
        its driver becomes open, so that a teardown at any moment finds and closes it.
        """
        channel = _Channel(assignment=assignment)
        driver: SdrDriver | None = None
        limit: asyncio.Timeout | None = None
        try:
            driver = self._agent.driver_factory(assignment)
            self._drivers.append((driver, assignment.channel_id))
            async with asyncio.timeout(self._agent.driver_open_timeout_s) as limit:
                await driver.open(assignment, self._ctx)
            capabilities = _wire_capabilities(driver.capabilities)
        except asyncio.CancelledError:
            if driver is not None:
                await self._close_quietly(driver, assignment.channel_id)
            raise
        except Exception as exc:
            # Deliberately broad: a driver is foreign code and may fail in any way.
            # The channel is reported as failed in the ack; the node goes on.
            text = (
                f"open timed out after {self._agent.driver_open_timeout_s:g} s"
                if limit is not None and limit.expired()
                else str(exc) or type(exc).__name__
            )
            channel.error = text
            logger.error(
                "driver failed to open: %s",
                text,
                exc_info=not isinstance(exc, (CaptureError, TimeoutError)),
                extra=self._extra(channel_id=assignment.channel_id),
            )
            if driver is not None:
                await self._close_quietly(driver, assignment.channel_id)
            self._channels[assignment.channel_id] = channel
            return channel
        channel.driver = driver
        channel.capabilities = capabilities
        channel.gate = ReadGate(capabilities.concurrent_reads)
        self._channels[assignment.channel_id] = channel
        return channel

    async def _refresh_channel(self, channel: _Channel) -> None:
        """Give a kept driver its `on_assign` hook; a failure closes the channel."""
        driver = channel.driver
        if not isinstance(driver, AssignHook):
            return
        try:
            async with asyncio.timeout(self._agent.driver_open_timeout_s):
                await driver.on_assign(channel.assignment, self._ctx)
        except Exception as exc:
            # Deliberately broad, as in _open_channel.
            text = f"on_assign failed: {str(exc) or type(exc).__name__}"
            logger.error(
                "driver refresh failed: %s",
                text,
                exc_info=not isinstance(exc, (CaptureError, TimeoutError)),
                extra=self._extra(channel_id=channel.channel_id),
            )
            await self._close_channel(channel, "driver failed to refresh for an assign")
            channel.driver, channel.capabilities, channel.gate = None, None, None
            channel.error = text

    def _start_monitors(self, channel: _Channel) -> None:
        if channel.driver is None:
            return
        for spec in channel.assignment.monitors:
            monitor = Monitor(
                channel.channel_id,
                spec,
                channel.driver,
                clock=self._clock,
                workers=self._agent.workers,
                deliver=self._outbox.offer_frame,
                gate=channel.gate,
            )
            channel.monitors.append(
                self._spawn(monitor.run(), f"monitor:{channel.channel_id}/{spec.monitor_id}")
            )

    async def _close_channel(self, channel: _Channel, reason: str) -> None:
        """Stop a channel's monitors, cancel its captures and close its driver."""
        tasks: list[asyncio.Task[Any]] = list(channel.monitors)
        channel.monitors.clear()
        for job in list(self._captures.values()):
            if job.request.channel_id == channel.channel_id and not job.task.done():
                self._cancel_reasons[job.request.capture_id] = reason
                tasks.append(job.task)
        for task in tasks:
            task.cancel()
        if tasks:
            _, stuck = await asyncio.wait(tasks, timeout=self._agent.teardown_timeout_s)
            if stuck:
                logger.error(
                    "tasks of a closing channel ignored cancellation: %s",
                    ", ".join(sorted(task.get_name() for task in stuck)),
                    extra=self._extra(channel_id=channel.channel_id),
                )
        self._outbox.discard_channel(channel.channel_id)
        await self._close_driver(channel)

    async def _close_driver(self, channel: _Channel) -> None:
        driver, channel.driver = channel.driver, None
        if driver is not None:
            await self._close_quietly(driver, channel.channel_id)

    async def _close_quietly(self, driver: SdrDriver, channel_id: str) -> None:
        """Close a driver within the time limit; failures are logged, never raised.

        Once `close` has returned, failed or timed out the driver leaves the session's
        list of drivers to close. When this coroutine is cancelled first it stays on
        the list, and the teardown closes it (`close` is idempotent).
        """
        try:
            async with asyncio.timeout(self._agent.driver_close_timeout_s):
                await driver.close()
        except TimeoutError:
            logger.error(
                "driver did not close within %g s",
                self._agent.driver_close_timeout_s,
                extra=self._extra(channel_id=channel_id),
            )
        except Exception:
            # Deliberately broad: closing must go on whatever a driver raises.
            logger.exception("driver failed to close", extra=self._extra(channel_id=channel_id))
        self._drivers = [entry for entry in self._drivers if entry[0] is not driver]

    # -------------------------------------------------------------------- captures

    def _on_capture_request(self, request: CaptureRequest) -> None:
        if request.capture_id in self._captures:
            logger.warning(
                "duplicate capture_request ignored",
                extra=self._extra(capture_id=request.capture_id),
            )
            return
        if len(self._captures) >= self._agent.max_concurrent_captures:
            self._fail_capture(
                request,
                "busy",
                f"already serving {len(self._captures)} captures",
            )
            return
        self._stats.captures_started += 1
        task = self._spawn(self._serve_capture(request), f"capture:{request.capture_id}")
        self._captures[request.capture_id] = _CaptureJob(request, task)
        task.add_done_callback(lambda _: self._capture_finished(request.capture_id, task))

    def _capture_finished(self, capture_id: str, task: asyncio.Task[None]) -> None:
        job = self._captures.get(capture_id)
        if job is not None and job.task is task:
            del self._captures[capture_id]
        self._cancel_reasons.pop(capture_id, None)

    def _on_capture_cancel(self, cancel: CaptureCancel) -> None:
        job = self._captures.get(cancel.capture_id)
        if job is None or job.task.done():
            logger.debug(
                "capture_cancel for a capture that is not in progress",
                extra=self._extra(capture_id=cancel.capture_id),
            )
            return
        self._cancel_reasons[cancel.capture_id] = "cancelled by the server"
        job.task.cancel()

    def _fail_capture(self, request: CaptureRequest, code: str, message: str) -> None:
        """Answer a request with `capture_error` (dropped when the session has ended)."""
        self._stats.captures_failed += 1
        if code not in CAPTURE_ERROR_CODES:
            code, message = "driver", f"{code}: {message}"
        logger.warning(
            "capture failed: %s: %s",
            code,
            message,
            extra=self._extra(
                capture_id=request.capture_id, channel_id=request.channel_id, code=code
            ),
        )
        self._outbox.post(
            CaptureErrorMessage(
                capture_id=request.capture_id,
                channel_id=request.channel_id,
                code=code,
                message=_clip(message),
            )
        )

    async def _serve_capture(self, request: CaptureRequest) -> None:
        """Read, chunk and upload one capture; every failure becomes a `capture_error`."""
        deadline: asyncio.Timeout | None = None
        try:
            channel = self._channels.get(request.channel_id)
            if channel is None or channel.driver is None or channel.gate is None:
                raise CaptureError(
                    "driver", f"channel {request.channel_id!r} has no open driver on this node"
                )
            self._check_request(request, channel)
            remaining_s = (request.deadline_ns - self._clock.now_ns()) / 1e9
            if remaining_s <= 0:
                raise CaptureError(
                    "too_late",
                    f"deadline_ns passed {-remaining_s:.3f} s before the request arrived",
                )
            async with asyncio.timeout(remaining_s) as deadline:
                async with channel.gate.capture():
                    capture = await channel.driver.read(
                        request.t_start_ns,
                        request.n_samples,
                        request.sample_rate_hz,
                        request.center_hz,
                    )
                if self._closed:
                    raise _SessionClosed
                frames = await self._chunks(request, capture)
                chunks = await self._outbox.upload(request.capture_id, frames)
            self._stats.captures_completed += 1
            logger.info(
                "capture sent",
                extra=self._extra(
                    capture_id=request.capture_id,
                    channel_id=request.channel_id,
                    n_samples=request.n_samples,
                    chunks=chunks,
                    t_first_ns=int(capture.t_first_ns),
                ),
            )
        except _SessionClosed:
            logger.debug(
                "capture abandoned, session ended", extra=self._extra(capture_id=request.capture_id)
            )
        except CaptureError as exc:
            self._fail_capture(request, exc.code, exc.message)
        except asyncio.CancelledError:
            reason = self._cancel_reasons.pop(request.capture_id, None)
            if reason is not None:
                self._fail_capture(request, "cancelled", reason)
            raise
        except TimeoutError as exc:
            if deadline is not None and deadline.expired():
                self._fail_capture(
                    request, "too_late", "deadline_ns passed before the capture was sent"
                )
            else:
                self._fail_capture(request, "driver", f"TimeoutError: {exc}")
        except Exception as exc:
            # Deliberately broad: a driver is foreign code. Whatever it raises is
            # logged with its traceback and reported as a `driver` error.
            logger.exception(
                "driver failed during a capture",
                extra=self._extra(capture_id=request.capture_id, channel_id=request.channel_id),
            )
            self._fail_capture(request, "driver", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _check_request(request: CaptureRequest, channel: _Channel) -> None:
        """Refuse a request outside the capabilities the channel was acked with."""
        capabilities = channel.capabilities
        assert capabilities is not None
        if request.sample_rate_hz > capabilities.max_sample_rate_hz * (1 + 1e-9):
            raise CaptureError(
                "out_of_range",
                f"sample rate {request.sample_rate_hz:g} Hz is above the channel maximum "
                f"{capabilities.max_sample_rate_hz:g} Hz",
            )
        if not capabilities.min_hz <= request.center_hz <= capabilities.max_hz:
            raise CaptureError(
                "out_of_range",
                f"centre {request.center_hz:.3f} Hz is outside "
                f"{capabilities.min_hz:.3f}..{capabilities.max_hz:.3f} Hz",
            )

    async def _chunks(self, request: CaptureRequest, capture: Any) -> Iterator[bytes]:
        """The `capture_data` frames for what a driver returned.

        Raises `CaptureError("driver")` when the driver returned something that cannot
        be sent: not a complex vector, fewer samples than asked for, NaN or infinity.
        Samples that are already contiguous complex64 are not copied; anything else is
        converted in a worker thread (tens of milliseconds for a large capture).
        Each frame carries the agent's `chunk_payload_bytes` of samples.
        """
        returned = getattr(capture, "iq", None)
        if (
            not isinstance(returned, np.ndarray)
            or returned.ndim != 1
            or returned.dtype.kind != "c"
        ):
            raise CaptureError("driver", "driver returned no complex sample vector")
        if returned.shape[0] < request.n_samples:
            raise CaptureError(
                "driver", f"driver returned {returned.shape[0]} of {request.n_samples} samples"
            )
        iq: np.ndarray = returned[: request.n_samples]
        if iq.dtype != np.complex64 or not iq.flags.c_contiguous:
            iq = await self._agent.workers.run(np.ascontiguousarray, iq, np.complex64)
        status = self._clock.status()
        if status.locked and not bool(capture.clock_locked):
            status = status.model_copy(update={"locked": False})
        try:
            return iter_capture_chunks(
                request.capture_id,
                request.channel_id,
                iq,
                int(capture.t_first_ns),
                float(capture.fs),
                float(capture.fc),
                bool(capture.overflow),
                status,
                self._agent.chunk_payload_bytes,
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise CaptureError("driver", f"driver returned an unusable capture: {exc}") from exc

    # ------------------------------------------------------------------ simulation

    def _on_sim_update(self, update: SimUpdate) -> None:
        targets: list[tuple[str, SimUpdateHook]] = []
        for channel in self._channels.values():
            driver = channel.driver
            if isinstance(driver, SimUpdateHook):
                targets.append((channel.channel_id, driver))
        if not targets:
            logger.debug(
                "sim_update: no driver wants it", extra=self._extra(version=update.version)
            )
            return
        self._spawn(self._forward_sim_update(update, targets), f"sim_update:{update.version}")

    async def _forward_sim_update(
        self, update: SimUpdate, targets: list[tuple[str, SimUpdateHook]]
    ) -> None:
        """Await `on_sim_update` on every driver that has it, all at once."""

        async def call(channel_id: str, driver: SimUpdateHook) -> None:
            try:
                await driver.on_sim_update(update.version, update.effective_ns)
            except Exception:
                # Deliberately broad: one driver's failure must not stop the others.
                logger.exception(
                    "driver failed to take a sim_update",
                    extra=self._extra(channel_id=channel_id, version=update.version),
                )

        await asyncio.gather(*(call(channel_id, driver) for channel_id, driver in targets))


# ---------------------------------------------------------------------------- agent


class NodeAgent:
    """A site's node: connects, serves, reconnects (see the module docstring).

    Use it from one event loop: `await agent.run()` until `stop()` is called from
    another task or a signal handler, or `agent.start()` and later `await
    agent.stop()`.
    """

    def __init__(
        self,
        server_url: str,
        site_id: str,
        token: str,
        *,
        driver: str = "noise",
        driver_options: Mapping[str, Any] | None = None,
        driver_factory: DriverFactory | None = None,
        node_id: str | None = None,
        state_path: str | Path | None = None,
        clock: ClockSource | None = None,
        software: str | None = None,
        backoff_base_s: float = RECONNECT_BACKOFF_MIN_S,
        backoff_max_s: float = RECONNECT_BACKOFF_MAX_S,
        backoff_jitter: float = RECONNECT_JITTER,
        open_timeout_s: float = 10.0,
        handshake_timeout_s: float = 10.0,
        close_timeout_s: float = 2.0,
        teardown_timeout_s: float = 5.0,
        driver_open_timeout_s: float = 30.0,
        driver_close_timeout_s: float = 5.0,
        server_silence_timeout_s: float | None = None,
        ping_interval_s: float | None = 20.0,
        ping_timeout_s: float | None = 20.0,
        max_concurrent_captures: int = MAX_CONCURRENT_CAPTURES,
        chunk_payload_bytes: int = DEFAULT_CHUNK_PAYLOAD_BYTES,
        workers: int = DEFAULT_WORKERS,
        send_buffer_bytes: int | None = None,
        proxy: str | Literal[True] | None = True,
        connect_options: Mapping[str, Any] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """
        server_url: where the server listens, `ws(s)://host[:port]` (`/ws/node` is
            added when the URL has no path; `http(s)` is accepted and mapped).
        site_id: the configured site this node serves.
        token: the site's bearer token. Never logged.
        driver, driver_options: name and keyword options for `drivers.get_driver`;
            one driver is built per assigned channel. Ignored with `driver_factory`,
            except that `driver` is still the name reported in `hello`.
        driver_factory: called with each `ChannelAssignment` to build its unopened
            driver (tests inject fakes here).
        node_id: this node's identity; None reads it from `state_path`.
        state_path: file that persists the generated `node_id`; None uses
            `default_state_path(site_id)`. Unused when `node_id` is given.
        clock: the node clock; None is `SystemClock()` (source "ntp", locked).
        software: the `software` string of `hello`; None is "petal-locate/<version>".
        backoff_base_s, backoff_max_s, backoff_jitter: the reconnect schedule,
            seconds (`backoff_delay`). Tests shrink the base.
        open_timeout_s: limit for TCP connect plus WebSocket handshake, seconds.
        handshake_timeout_s: limit for `welcome` or `reject` after `hello`, seconds.
        close_timeout_s: limit for the closing handshake, seconds; the socket is
            aborted after it.
        teardown_timeout_s: how long a session waits for its cancelled tasks, seconds.
        driver_open_timeout_s, driver_close_timeout_s: limits for `open`/`on_assign`
            and for `close` of a driver, seconds.
        server_silence_timeout_s: reconnect when nothing arrives from the server for
            this long, seconds; None is `max(10, 5 * heartbeat_s)`.
        ping_interval_s, ping_timeout_s: WebSocket keepalive, seconds (None disables).
        max_concurrent_captures: capture requests served at once; more get `busy`.
        chunk_payload_bytes: sample bytes per `capture_data` frame, 8 to 262144 (the
            protocol's limit); 32768 by default. The uplink must carry one chunk in
            well under three heartbeat periods or the server takes the node for
            silent, so lower it for a very slow link; raising it saves a little
            overhead on a fast one.
        workers: worker threads for CPU work (spectra, driver DSP), 2 per node.
        send_buffer_bytes: `SO_SNDBUF` of the node's socket, bytes; None leaves the
            operating system's autotuned default (megabytes). Whatever sits in that
            buffer is ahead of the next heartbeat, so on a slow uplink a bound of a
            chunk or two (for example 262144) keeps control messages from waiting
            behind megabytes of samples that are already in the kernel.
        proxy: as `websockets.connect`: True honours the environment, None forbids a
            proxy, a URL names one.
        connect_options: further keyword arguments for `websockets.connect`, for
            example `ssl=` with a private CA.
        rng: source of the backoff jitter (tests pass a seeded one).
        Raises `ValueError` naming the argument at fault.
        """
        self._ws_url, self._http_url = normalize_server_url(server_url)
        if not isinstance(site_id, str) or not 1 <= len(site_id) <= MAX_ID_CHARS:
            raise ValueError(f"site_id: must be 1..{MAX_ID_CHARS} characters")
        if not isinstance(token, str):
            raise ValueError("token: must be a string")
        if node_id is not None and (
            not isinstance(node_id, str) or not 1 <= len(node_id) <= MAX_ID_CHARS
        ):
            raise ValueError(f"node_id: must be 1..{MAX_ID_CHARS} characters")
        if driver_factory is not None and not callable(driver_factory):
            raise ValueError("driver_factory: must be callable")
        if not isinstance(driver, str) or not driver:
            raise ValueError("driver: must be a non-empty driver name")
        for name, value in (
            ("open_timeout_s", open_timeout_s),
            ("handshake_timeout_s", handshake_timeout_s),
            ("close_timeout_s", close_timeout_s),
            ("teardown_timeout_s", teardown_timeout_s),
            ("driver_open_timeout_s", driver_open_timeout_s),
            ("driver_close_timeout_s", driver_close_timeout_s),
        ):
            if not (isinstance(value, (int, float)) and value > 0):
                raise ValueError(f"{name}: must be a positive number of seconds, got {value!r}")
        if server_silence_timeout_s is not None and not server_silence_timeout_s > 0:
            raise ValueError("server_silence_timeout_s: must be positive or None")
        if (
            isinstance(max_concurrent_captures, bool)
            or not isinstance(max_concurrent_captures, int)
            or max_concurrent_captures < 1
        ):
            raise ValueError("max_concurrent_captures: must be an integer of at least 1")
        if (
            isinstance(chunk_payload_bytes, bool)
            or not isinstance(chunk_payload_bytes, int)
            or not 8 <= chunk_payload_bytes <= MAX_CHUNK_PAYLOAD_BYTES
        ):
            raise ValueError(
                f"chunk_payload_bytes: must be an integer from 8 to {MAX_CHUNK_PAYLOAD_BYTES}"
            )
        if send_buffer_bytes is not None and (
            isinstance(send_buffer_bytes, bool)
            or not isinstance(send_buffer_bytes, int)
            or send_buffer_bytes < 1024
        ):
            raise ValueError("send_buffer_bytes: must be an integer of at least 1024, or None")
        try:
            backoff_delay(0, backoff_base_s, backoff_max_s, backoff_jitter)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"backoff_base_s, backoff_max_s, backoff_jitter: {exc}"
            ) from exc
        self.site_id: str = site_id
        self.token: str = token
        self.clock: ClockSource = clock if clock is not None else SystemClock()
        self.stats = AgentStats()
        self.last_rtt_ms: float | None = None
        self.last_reject: str | None = None
        self.stop_requested = asyncio.Event()
        self.teardown_timeout_s = float(teardown_timeout_s)
        self.close_timeout_s = float(close_timeout_s)
        self.driver_open_timeout_s = float(driver_open_timeout_s)
        self.driver_close_timeout_s = float(driver_close_timeout_s)
        self.server_silence_timeout_s = server_silence_timeout_s
        self.max_concurrent_captures = max_concurrent_captures
        self.chunk_payload_bytes = chunk_payload_bytes
        self._driver_name = driver
        options = dict(driver_options or {})
        self.driver_factory: DriverFactory = (
            driver_factory
            if driver_factory is not None
            else (lambda assignment: get_driver(driver, **options))
        )
        self._node_id = node_id
        self._state_path = Path(state_path) if state_path is not None else None
        self._software = software or f"petal-locate/{__version__}"
        self._backoff = (float(backoff_base_s), float(backoff_max_s), float(backoff_jitter))
        self._open_timeout_s = float(open_timeout_s)
        self._handshake_timeout_s = float(handshake_timeout_s)
        self._ping = (ping_interval_s, ping_timeout_s)
        self._worker_limit = workers
        self._send_buffer_bytes = send_buffer_bytes
        self._workers: WorkerPool | None = None
        self._proxy = proxy
        self._connect_options = dict(connect_options or {})
        self._rng = rng or random.Random()
        self._state = "idle"
        self._online = asyncio.Event()
        self._finished = asyncio.Event()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ properties

    @property
    def node_id(self) -> str | None:
        """This node's identity; None until `run()` has read or generated it."""
        return self._node_id

    @property
    def server_ws_url(self) -> str:
        """The WebSocket URL the agent dials."""
        return self._ws_url

    @property
    def server_http_url(self) -> str:
        """HTTP origin of the server (given to drivers in `DriverContext`)."""
        return self._http_url

    @property
    def workers(self) -> WorkerPool:
        """The node's worker pool (exists while `run()` is running)."""
        if self._workers is None:
            raise RuntimeError("the worker pool exists only while the agent runs")
        return self._workers

    @property
    def state(self) -> str:
        """"idle", "connecting", "online" (welcomed), "backoff" or "stopped"."""
        return self._state

    def _extra(self, **more: Any) -> dict[str, Any]:
        return {"site_id": self.site_id, "node_id": self._node_id, **more}

    def _set_state(self, state: str) -> None:
        if state != self._state:
            logger.debug("agent state %s -> %s", self._state, state, extra=self._extra())
            self._state = state

    # --------------------------------------------------------------------- control

    def start(self) -> asyncio.Task[None]:
        """Run the agent in a task of the current loop and return that task."""
        if self._task is not None:
            raise RuntimeError("the agent was already started")
        self._task = asyncio.get_running_loop().create_task(self.run(), name="petal-node:agent")
        return self._task

    def request_stop(self) -> None:
        """Ask the agent to shut down; returns at once (usable from a signal handler
        installed with `loop.add_signal_handler`)."""
        self.stop_requested.set()

    async def stop(self, timeout_s: float | None = None) -> None:
        """Shut down and wait until `run()` has finished and nothing is left running.

        The server gets close code 1001. Raises `TimeoutError` when `timeout_s`
        (seconds) passes first. Safe to call more than once and before `run()`.
        """
        self.request_stop()
        if self._task is None and not self._running and not self._finished.is_set():
            return  # never started
        async with asyncio.timeout(timeout_s):
            if self._task is not None:
                await asyncio.wait([self._task])
                if not self._task.cancelled():
                    self._task.exception()  # retrieved here; awaiting the task still raises it
            else:
                await self._finished.wait()

    async def wait_online(self, timeout_s: float | None = None) -> None:
        """Return when a session has been welcomed. Raises `TimeoutError` after
        `timeout_s` seconds."""
        async with asyncio.timeout(timeout_s):
            await self._online.wait()

    # -------------------------------------------------------------------- main loop

    async def run(self) -> None:
        """Connect and serve until `stop()`; reconnect for ever in between.

        Raises `NodeRejected` when the server refuses the node for good
        (`protocol_mismatch`), `ValueError` when the node id state file is unusable,
        and `RuntimeError` when called twice.
        """
        if self._running or self._finished.is_set():
            raise RuntimeError("NodeAgent.run() may be called once")
        self._running = True
        try:
            if self._node_id is None:
                path = self._state_path or default_state_path(self.site_id)
                self._node_id = await asyncio.to_thread(load_or_create_node_id, path)
            self._workers = WorkerPool(self._worker_limit)
            host = urlsplit(self._ws_url).hostname or ""
            if self._ws_url.startswith("ws://") and not _is_loopback(host):
                logger.warning(
                    "the node token travels in clear text over ws://; use wss:// outside a lab",
                    extra=self._extra(server=host),
                )
            logger.info(
                "node agent starting",
                extra=self._extra(server=self._ws_url, driver=self._driver_name),
            )
            failures = 0
            base_s, max_s, jitter = self._backoff
            while not self.stop_requested.is_set():
                outcome = await self._attempt()
                if outcome.kind == "stopped" or self.stop_requested.is_set():
                    break
                at_max = False
                if outcome.kind == "rejected":
                    reason = outcome.reason or "unknown"
                    self.last_reject = reason
                    self.stats.rejects += 1
                    if REJECT_POLICY.get(reason, "retry_max") == "stop":
                        logger.error(
                            "server rejected the node for good: %s",
                            reason,
                            extra=self._extra(reason=reason),
                        )
                        raise NodeRejected(reason, outcome.detail)
                    logger.error(
                        "server rejected the node: %s; retrying at the longest backoff",
                        reason,
                        extra=self._extra(reason=reason),
                    )
                    at_max = True
                elif outcome.kind == "closed" and outcome.code == CLOSE_CODE_REPLACED:
                    logger.error(
                        "session replaced: another process uses this node_id; "
                        "retrying at the longest backoff",
                        extra=self._extra(),
                    )
                    at_max = True
                elif outcome.welcomed:
                    failures = 0
                if at_max:
                    failures = max(failures, 62)
                delay = backoff_delay(failures, base_s, max_s, jitter, self._rng)
                failures += 1
                self.stats.backoff_delays.append(delay)
                logger.info(
                    "reconnecting in %.2f s (%s)",
                    delay,
                    outcome.kind,
                    extra=self._extra(
                        outcome=outcome.kind, code=outcome.code, detail=outcome.detail
                    ),
                )
                self._set_state("backoff")
                try:
                    await self._or_stop(asyncio.sleep(delay))
                except _Stopped:
                    break
        finally:
            self._set_state("stopped")
            self._online.clear()
            self._running = False
            self._finished.set()
            logger.info("node agent stopped", extra=self._extra())

    async def _or_stop(self, awaitable: Awaitable[_T]) -> _T:
        """Await something unless `stop()` is requested first.

        Raises `_Stopped` (after cancelling and awaiting the thing) when it is.
        """
        task: asyncio.Future[_T] = asyncio.ensure_future(awaitable)
        stop_waiter = asyncio.ensure_future(self.stop_requested.wait())
        racing: list[asyncio.Future[Any]] = [task, stop_waiter]
        try:
            await asyncio.wait(racing, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_waiter.cancel()
            if not task.done():
                task.cancel()
                await asyncio.wait([task])
        if task.cancelled():
            raise _Stopped
        return task.result()

    async def _attempt(self) -> _Outcome:
        """One connection: dial, handshake, and the session when welcomed."""
        self._set_state("connecting")
        self.stats.connect_attempts += 1
        try:
            ws = await self._or_stop(self._dial())
        except _Stopped:
            return _Outcome("stopped")
        except (OSError, TimeoutError, WebSocketException) as exc:
            logger.warning(
                "cannot connect to the server: %s: %s",
                type(exc).__name__,
                exc,
                extra=self._extra(server=self._ws_url),
            )
            return _Outcome("connect_failed", detail=f"{type(exc).__name__}: {exc}")
        try:
            answer = await self._or_stop(self._handshake(ws))
        except _Stopped:
            await self.close_socket(ws, CLOSE_CODE_SHUTDOWN, "shutdown")
            return _Outcome("stopped")
        except (ConnectionClosed, OSError, TimeoutError, ProtocolError) as exc:
            text = exc.detail if isinstance(exc, ProtocolError) else f"{type(exc).__name__}: {exc}"
            logger.warning("handshake failed: %s", text, extra=self._extra())
            await self.close_socket(ws, _CLOSE_PROTOCOL_ERROR, "handshake failed")
            return _Outcome("handshake_failed", detail=text)
        if isinstance(answer, _Outcome):
            await self.close_socket(ws, 1000, "")
            return answer
        self.stats.sessions += 1
        self._set_state("online")
        self._online.set()
        logger.info(
            "welcomed by the server",
            extra=self._extra(
                config_version=answer.config_version,
                heartbeat_s=answer.heartbeat_s,
                channels=[channel.channel_id for channel in answer.channels],
                simulation=answer.simulation.enabled,
            ),
        )
        session = _Session(self, ws, answer)
        try:
            return await session.run()
        except Exception as exc:
            # Deliberately broad: the reconnect loop must survive any failure of a
            # session ("never crashes on a server error"). It is logged in full.
            logger.exception("session failed", extra=self._extra())
            return _Outcome("error", welcomed=True, detail=f"{type(exc).__name__}: {exc}")
        finally:
            self._online.clear()

    async def _dial(self) -> Any:
        """Open the WebSocket (permessage-deflate off, 1 MiB messages)."""
        options: dict[str, Any] = {
            "compression": None,
            "max_size": WS_MAX_SIZE_BYTES,
            "open_timeout": self._open_timeout_s,
            "close_timeout": self.close_timeout_s,
            "ping_interval": self._ping[0],
            "ping_timeout": self._ping[1],
            "proxy": self._proxy,
        }
        options.update(self._connect_options)
        options["compression"] = None  # PROTOCOL.md: disabled on both ends, always
        ws = await websockets.connect(self._ws_url, **options)
        if self._send_buffer_bytes is not None:
            raw = ws.transport.get_extra_info("socket")
            try:
                if raw is None:
                    raise OSError("the transport exposes no socket")
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self._send_buffer_bytes)
            except OSError as exc:
                logger.warning(
                    "cannot set the socket send buffer: %s",
                    exc,
                    extra=self._extra(send_buffer_bytes=self._send_buffer_bytes),
                )
        return ws

    async def _handshake(self, ws: Any) -> Welcome | _Outcome:
        """Send `hello`; return the `welcome`, or the outcome of a `reject`.

        Messages of unknown type before the answer are ignored. Raises `ProtocolError`
        for anything else, `TimeoutError` when no answer comes within
        `handshake_timeout_s`.
        """
        assert self._node_id is not None
        hello = Hello(
            node_id=self._node_id,
            site_id=self.site_id,
            token=self.token,
            software=self._software,
            driver=self._driver_name,
            clock=self.clock.status(),
            t_ns=self.clock.now_ns(),
        )
        await ws.send(dump_control(hello))
        async with asyncio.timeout(self._handshake_timeout_s):
            while True:
                raw = await ws.recv()
                if not isinstance(raw, str):
                    raise ProtocolError("handshake: expected a text frame, got a binary one")
                answer = parse_control(raw)
                if not isinstance(answer, UnknownMessage):
                    break
                # PROTOCOL.md: an unknown type is ignored with a warning, also here,
                # so that a newer server may say more before its welcome.
                logger.warning(
                    "message of unknown type %r ignored during the handshake",
                    _clip(answer.type, 64),
                    extra=self._extra(),
                )
        if isinstance(answer, Welcome):
            if answer.protocol != PROTOCOL_VERSION:
                return _Outcome(
                    "rejected",
                    reason="protocol_mismatch",
                    detail=(
                        f"server speaks protocol {answer.protocol}, this node {PROTOCOL_VERSION}"
                    ),
                )
            return answer
        if isinstance(answer, Reject):
            return _Outcome("rejected", reason=answer.reason)
        raise ProtocolError(f"handshake: expected welcome or reject, got {answer.type}")

    async def close_socket(self, ws: Any, code: int, reason: str) -> None:
        """Close a WebSocket within `close_timeout_s`, aborting it when the peer does
        not cooperate (a peer that stopped reading would block the close for ever)."""
        try:
            async with asyncio.timeout(self.close_timeout_s * 1.5 + 0.1):
                await ws.close(code, reason)
        except TimeoutError:
            logger.debug("closing handshake timed out; aborting the socket", extra=self._extra())
            transport = getattr(ws, "transport", None)
            if transport is not None:
                transport.abort()
        except OSError as exc:
            logger.debug("socket close failed: %s", exc, extra=self._extra())
