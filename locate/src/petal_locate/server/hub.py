"""Node sessions of the server: PROTOCOL.md section A seen from the server side.

`Hub` owns every node socket. For each one it validates the `hello`, sends `welcome`,
waits for `assign_ack` before a channel may be tasked, answers heartbeats, watches
liveness, fans spectrum frames out to subscribers and runs captures:

    capture = await hub.capture(channel_id, t_start_ns, n_samples, fs, fc, deadline_ns)

The hub knows nothing about the configuration models. The application gives it three
providers: `assignments_for(site_id)` (the channels of a site, None for an unknown
site), `config_version()` and `simulation_info()` (the `welcome.simulation` object).

How it stays responsive
-----------------------
Everything here runs on one asyncio event loop and every public method must be called
from that loop. Three rules keep the loop free:

* The receive loop of a session never waits for anything but the next frame. What it
  sends (acks, errors) is queued; one writer task per socket owns the sending side, so
  a node that reads slowly delays only itself and is dropped when its queue fills.
* Subscribers never run inside the receive loop. Each has its own delivery task and a
  mailbox. For spectrum frames the mailbox holds one frame per (channel, monitor),
  newest wins, so a slow subscriber sees fewer frames and delays nobody, while one
  that keeps up receives every frame. Status events wait in order and are all
  delivered; only when more than `STATUS_BACKLOG` wait for one subscriber are they
  thinned to the newest of each site.
* The per-frame work is small enough to stay on the loop: parsing a header, checking
  a spectrum frame (131072 bins: about 0.1 ms) or copying one capture chunk (256 KiB:
  tens of microseconds). The receive loop gives the event loop one turn after every
  frame, so frames that arrive back to back never hold it either.

Time. Absolute times are integer UTC nanoseconds of the server clock (`clock`, by
default `time.time_ns`; a demonstration with a fixed simulated start gives the shifted
clock the runtime and the simulated nodes use); intervals are measured on the monotonic
clock.

Close codes. PROTOCOL.md names 1001 (shutdown) and 4001 (replaced). The other ways a
session ends are given codes here: 1008 after a `reject` and when a site is removed
from the configuration, 1002 after three malformed messages in a row, 4002 for
silence (no `hello` in time, or three heartbeat periods without a frame), 1013 when
the node stops reading and 1011 for an internal error.

Readings of PROTOCOL.md that the text leaves open, as implemented here:

* The token is checked before the site is looked up. A site that does not exist and
  has no token is `bad_token`, so only a token holder can learn which sites exist;
  `unknown_site` means a valid token for a site the configuration no longer has.
* A `hello` announcing another protocol version is `protocol_mismatch` even when its
  other fields do not fit version 1.
* Silence does more than mark the node offline: the socket is closed, so that a
  half-open connection cannot hold the site.
* An `assign_ack` counts only when its `config_version` is the one last assigned; a
  channel it does not mention is not ready. After every `assign`, all channels of the
  site wait for the new ack, changed or not.
* Spectrum frames are accepted only for acknowledged channels and assigned windows,
  with the assigned `nfft`: a node cannot feed spectra for a channel of another site,
  and frames produced under an earlier assignment do not reach the pipeline.
* A capture must have exactly the requested number of samples (rate, centre and first
  sample time may differ from the request, as the protocol says).
* A well-formed message that only a server sends, or a second `hello`, is malformed.
* Unknown message types are counted every time and warned about once per type and
  socket.
* A spectrum frame stamped more than `PSD_FUTURE_TOLERANCE_S` ahead of the server clock
  is dropped and counted (`psd_dropped_future`), with one warning per session: one such
  frame would otherwise be the newest time of its window and make the detection
  pipeline drop every correct frame after it.
* A `sim_update` announced while a node had not yet acknowledged its assignment (its
  channels were still opening, and a node hands the update only to open channels) is
  sent to it again once its `assign_ack` arrives. Without that, a scenario replaced in
  the first seconds of a session never reached the node's simulated radios.
"""
from __future__ import annotations

import asyncio
import collections
import inspect
import itertools
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Generic, Literal, TypeVar

import numpy as np
from starlette.websockets import WebSocket, WebSocketDisconnect

from petal_locate.protocol.framing import (
    AssembledCapture,
    CaptureAssembler,
    decode_frame,
    decode_psd,
)
from petal_locate.protocol.messages import (
    BAD_MESSAGE_LIMIT,
    CAPTURE_ERROR_CODES,
    CLOSE_CODE_REPLACED,
    CLOSE_CODE_SHUTDOWN,
    HEARTBEAT_MISS_LIMIT,
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_SAMPLES,
    MAX_CONCURRENT_CAPTURES,
    PROTOCOL_VERSION,
    Assign,
    AssignAck,
    Capabilities,
    CaptureCancel,
    CaptureDataHeader,
    CaptureErrorMessage,
    CaptureRequest,
    ChannelAssignment,
    ClockStatus,
    ControlMessage,
    ErrorMessage,
    Heartbeat,
    HeartbeatAck,
    Hello,
    MonitorAssignment,
    ProtocolError,
    PsdHeader,
    Reject,
    SimulationInfo,
    SimUpdate,
    UnknownMessage,
    Welcome,
    dump_control,
    loads_json_object,
    message_type,
    parse_binary_header,
    parse_control,
)
from petal_locate.server.settings import Settings

__all__ = [
    "CLOSE_CODE_BAD_MESSAGES",
    "CLOSE_CODE_CONGESTED",
    "CLOSE_CODE_INTERNAL",
    "CLOSE_CODE_REJECTED",
    "CLOSE_CODE_SILENT",
    "PSD_FUTURE_TOLERANCE_S",
    "STATUS_BACKLOG",
    "AssignmentsProvider",
    "CaptureFailed",
    "Hub",
    "PsdCallback",
    "PsdFrame",
    "StatusCallback",
    "StatusEvent",
    "Subscription",
    "WindowHealthProvider",
]

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------- constants

CLOSE_CODE_REJECTED: Final[int] = 1008
"""Close code after a `reject`, and when the site of a live session leaves the
configuration (policy violation)."""

CLOSE_CODE_BAD_MESSAGES: Final[int] = 1002
"""Close code after `BAD_MESSAGE_LIMIT` malformed messages in a row (protocol error)."""

CLOSE_CODE_SILENT: Final[int] = 4002
"""Close code for silence: no `hello` in time, or `HEARTBEAT_MISS_LIMIT` heartbeat
periods without any frame."""

CLOSE_CODE_CONGESTED: Final[int] = 1013
"""Close code when the node does not read what the server sends (try again later)."""

CLOSE_CODE_INTERNAL: Final[int] = 1011
"""Close code when the server failed while serving the session (internal error)."""

_CLOSE_CODE_NORMAL: Final[int] = 1000
"""Close code when a socket's receive side ends with no more specific one. In practice
the peer has gone by then and no close frame is sent at all."""

RTT_WINDOW: Final[int] = 10
"""Heartbeat round trips remembered per node; `Hub.rtt_ms` is the worst of them."""

OUTBOUND_QUEUE_LIMIT: Final[int] = 256
"""Control messages that may wait for a node to read them before it is dropped."""

STATUS_BACKLOG: Final[int] = 64
"""Status events that may wait for one subscriber. Beyond it only the newest waiting
event of each site is kept, so a subscriber that has fallen behind still ends on the
latest state of every site."""

CLOSE_TIMEOUT_S: Final[float] = 3.0
"""Longest a socket is given to take its close frame before it is abandoned, s."""

_SLOW_CALLBACK_S: Final[float] = 0.050
"""A synchronous subscriber taking longer than this on the event loop is reported."""

_LOG_TEXT_CHARS: Final[int] = 120
"""Longest piece of peer-supplied text copied into a log line."""

_UNKNOWN_TYPES_REPORTED: Final[int] = 16
"""Distinct unknown message types warned about per socket; the rest are only counted."""

_NS_PER_S: Final[int] = 1_000_000_000

PSD_FUTURE_TOLERANCE_S: Final[float] = 60.0
"""A spectrum frame whose `t_ns` lies further than this ahead of the server clock is
dropped (`psd_dropped_future`), s. Node clocks are disciplined to well under a second;
a minute is far beyond any honest offset and short of anything a glitch produces."""

K = TypeVar("K", bound=Hashable)
T = TypeVar("T")

AssignmentsProvider = Callable[[str], list[ChannelAssignment] | None]
"""`assignments_for(site_id)`: the channels of a site, or None for an unknown site."""

WindowHealthProvider = Callable[[str, str], Mapping[str, Any] | None]
"""`window_health(channel_id, monitor_id)`: what the detection pipeline knows of a
monitor window (`detection_frame_age_s`, `blind`, `blind_reason`, `learning`,
`floor_raised_db`, `note`), or None for a window it does not know. Called on the event
loop for every status report; it must not block."""


# ---------------------------------------------------------------------- public types


class CaptureFailed(Exception):
    """A capture did not produce samples.

    code: why. Either a code the node sent in `capture_error` (`out_of_range`,
        `too_late`, `overflow`, `driver`, `busy`, `cancelled`, or one a newer node
        adds), or one of the server's own:

        - `unknown_channel`: no node seen by this hub was assigned the channel.
        - `offline`: the node of the channel's site is not connected, or went silent
          for three heartbeat periods during the capture.
        - `not_ready`: the node has not acknowledged the channel's assignment, or its
          driver failed to open.
        - `limit`: the node already has `MAX_CONCURRENT_CAPTURES` captures pending.
        - `too_large`: the request exceeds 64 MiB of samples.
        - `deadline`: the last chunk did not arrive by `deadline_ns` (plus the grace
          period), or the deadline had already passed.
        - `disconnected`: the node's socket closed during the capture.
        - `replaced`: a new session of the same node took over during the capture.
        - `reassigned`: an `assign` removed or changed the channel during the capture.
        - `bad_data`: the node's chunks broke the protocol, or announced another
          number of samples than was requested; the capture is void.
        - `shutdown`: the hub is closing.

        Everything except `unknown_channel` and `too_large` (and the node's
        `out_of_range`) can succeed when tried again later.
    message: human-readable detail.
    channel_id: the channel asked for.
    capture_id: the id of the request when one was issued, else None.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        channel_id: str | None = None,
        capture_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code: str = code
        self.message: str = message
        self.channel_id: str | None = channel_id
        self.capture_id: str | None = capture_id


@dataclass(frozen=True, eq=False)
class PsdFrame:
    """One spectrum frame as delivered to `Hub.subscribe_psd` callbacks.

    site_id: the site whose node sent it.
    header: the validated frame header (channel, monitor, `t_ns` labelled first-sample
        time in integer UTC ns, `center_hz`, `span_hz`, `nfft`, `navg`, `seq`).
    psd_db: float32[nfft] in dB, fft-shifted (bin k at center_hz + (k - nfft/2) *
        span_hz / nfft). Read-only and shared by every subscriber: copy before changing.
    received_ns: server clock when the frame arrived, integer UTC ns.
    """

    site_id: str
    header: PsdHeader
    psd_db: np.ndarray
    received_ns: int

    @property
    def channel_id(self) -> str:
        """Channel the frame belongs to."""
        return self.header.channel_id

    @property
    def monitor_id(self) -> str:
        """Monitor window the frame belongs to."""
        return self.header.monitor_id

    @property
    def t_ns(self) -> int:
        """Labelled time of the first sample of the snippet, integer UTC ns."""
        return self.header.t_ns


@dataclass(frozen=True)
class StatusEvent:
    """A change in the state of a node, as delivered to `Hub.subscribe_status`.

    site_id: the site concerned.
    kind: `online` (a session was welcomed), `offline` (it ended), `channels` (an
        assignment was sent or acknowledged, so channel readiness changed) or `clock`
        (the node's clock source or lock state changed).
    reason: what caused it: `hello` for `online`; `disconnected`, `replaced`,
        `offline` (silence), `bad_messages`, `congested`, `removed`, `error` or
        `shutdown` for `offline`; `assign` or `assign_ack` for `channels`; `heartbeat`
        for `clock`.
    status: the node's status at that moment, shaped like `Hub.node_status` entries.
    t_ns: server clock at the change, integer UTC ns.
    """

    site_id: str
    kind: Literal["online", "offline", "channels", "clock"]
    reason: str
    status: dict[str, Any]
    t_ns: int


PsdCallback = Callable[[PsdFrame], Awaitable[None] | None]
"""Spectrum subscriber: a plain function or a coroutine function of one `PsdFrame`."""

StatusCallback = Callable[[StatusEvent], Awaitable[None] | None]
"""Status subscriber: a plain function or a coroutine function of one `StatusEvent`."""


# -------------------------------------------------------------------------- fan-out


class Subscription(Generic[K, T]):
    """One subscriber of the hub; returned by `subscribe_psd` and `subscribe_status`.

    Items are handed to the callback one at a time by a task of the subscription's
    own, never by the session that produced them. While the callback is busy, newer
    items wait in a mailbox that holds one item per key: a newer item replaces an
    older undelivered one of the same key, which is counted in `dropped`.

    Spectrum frames are keyed by (channel, monitor): newest wins per window. Status
    events are keyed by (site, serial number), so they never replace each other and
    arrive in the order they happened, two changes of one site in the same instant
    included (a session replaced: `offline`, then `online`). Only when more than
    `STATUS_BACKLOG` of them wait are they thinned to the newest of each site.

    A coroutine callback may take as long as it likes. A plain function runs on the
    event loop and must return quickly (well under 50 ms); a slower one is reported in
    the log. A callback that raises is logged and counted in `errors`; delivery goes on.

    delivered: items the callback has completed.
    dropped: items replaced by a newer one before they could be delivered.
    errors: calls that raised.
    """

    def __init__(
        self,
        owner: _Fanout[K, T],
        callback: Callable[[T], Awaitable[None] | None],
        name: str,
    ) -> None:
        self._owner = owner
        self._callback = callback
        self._name = name
        self._mailbox: dict[K, T] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active = True
        self._slow_reported = False
        self.delivered: int = 0
        self.dropped: int = 0
        self.errors: int = 0

    @property
    def name(self) -> str:
        """Label of the subscriber in log lines."""
        return self._name

    @property
    def active(self) -> bool:
        """False once `unsubscribe` has been called."""
        return self._active

    @property
    def waiting(self) -> int:
        """Items in the mailbox, not yet handed to the callback."""
        return len(self._mailbox)

    def unsubscribe(self) -> None:
        """Stop delivery. Undelivered items are discarded; calling it twice is fine."""
        if not self._active:
            return
        self._active = False
        self._owner._remove(self)
        self._mailbox.clear()
        self._stop_task()

    def _offer(self, key: K, item: T) -> None:
        """Put an item in the mailbox (newest wins per key) and wake the delivery task.

        Never waits. Must be called on the event loop.
        """
        if not self._active:
            return
        if key in self._mailbox:
            self.dropped += 1
        self._mailbox[key] = item  # a replaced key keeps its place in the queue
        group = self._owner.group
        if group is not None and len(self._mailbox) > self._owner.backlog:
            self._thin(group)
        if self._task is None or self._task.done():
            # A fresh event with each task: after a hub restart the previous task may
            # have run on another event loop, to which its event stays bound.
            self._wake = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(
                self._deliver(self._wake), name=f"petal-hub-{self._owner.what}-{self._name}"
            )
        self._wake.set()

    def _thin(self, group: Callable[[K], Hashable]) -> None:
        """Keep only the newest waiting item of each group, in their order.

        group: maps a mailbox key to what the items must stay complete for (the site
        of a status event). The others are counted in `dropped`.
        """
        newest: dict[Hashable, K] = {}
        for key in self._mailbox:
            newest[group(key)] = key
        keep = set(newest.values())
        waiting = len(self._mailbox)
        self._mailbox = {key: item for key, item in self._mailbox.items() if key in keep}
        self.dropped += waiting - len(self._mailbox)

    def _stop_task(self) -> asyncio.Task[None] | None:
        """Cancel the delivery task; returns it so that a caller can await its end."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            return task
        return None

    async def _deliver(self, wake: asyncio.Event) -> None:
        """Delivery loop: one item at a time, oldest key first.

        wake: set by `_offer` whenever the mailbox gains an item.
        """
        while True:
            await wake.wait()
            while self._mailbox:
                key = next(iter(self._mailbox))
                item = self._mailbox.pop(key)
                started = time.perf_counter()
                try:
                    outcome = self._callback(item)
                    if inspect.isawaitable(outcome):
                        await outcome
                    else:
                        self._note_duration(time.perf_counter() - started)
                except Exception:  # a subscriber's failure must not end delivery
                    self.errors += 1
                    logger.exception(
                        "%s subscriber %s raised",
                        self._owner.what,
                        self._name,
                        extra={"subscriber": self._name, "stream": self._owner.what},
                    )
                else:
                    self.delivered += 1
                # Let the sessions run between two items, however fast the callback is.
                await asyncio.sleep(0)
            wake.clear()

    def _note_duration(self, seconds: float) -> None:
        """Report a plain-function subscriber that held the event loop too long."""
        if seconds <= _SLOW_CALLBACK_S or self._slow_reported:
            return
        self._slow_reported = True
        logger.warning(
            "%s subscriber %s blocked the event loop for %.0f ms; make it a coroutine "
            "or hand the work to a thread (reported once)",
            self._owner.what,
            self._name,
            seconds * 1e3,
            extra={"subscriber": self._name, "stream": self._owner.what},
        )


class _Fanout(Generic[K, T]):
    """The subscribers of one stream; `publish` never waits for any of them."""

    def __init__(
        self,
        what: str,
        *,
        group: Callable[[K], Hashable] | None = None,
        backlog: int = 0,
    ) -> None:
        """
        what: name of the stream in log lines.
        group, backlog: None for a stream whose keys alone bound a mailbox (newest
            wins per key). Otherwise every published key is unique, and a mailbox
            holding more than `backlog` items is thinned to the newest item of each
            `group(key)`.
        """
        self.what = what
        self.group = group
        self.backlog = backlog
        self._subscriptions: list[Subscription[K, T]] = []
        self._names = itertools.count(1)

    def subscribe(
        self, callback: Callable[[T], Awaitable[None] | None], name: str | None
    ) -> Subscription[K, T]:
        """Register a callback; see `Subscription`."""
        if not callable(callback):
            raise TypeError(f"callback: must be callable, got {type(callback).__name__}")
        label = name or getattr(callback, "__qualname__", None) or type(callback).__name__
        subscription: Subscription[K, T] = Subscription(
            self, callback, f"{label}#{next(self._names)}"
        )
        self._subscriptions.append(subscription)
        return subscription

    def publish(self, key: K, item: T) -> None:
        """Offer an item to every subscriber. Constant time per subscriber."""
        for subscription in self._subscriptions:
            subscription._offer(key, item)

    def _remove(self, subscription: Subscription[K, T]) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    async def stop(self) -> None:
        """Stop every delivery task and wait for them to end.

        Subscriptions stay registered: delivery starts again with the next `publish`.
        Items still in a mailbox are discarded and counted in the log.
        """
        tasks: list[asyncio.Task[None]] = []
        undelivered = 0
        for subscription in self._subscriptions:
            undelivered += len(subscription._mailbox)
            subscription._mailbox.clear()
            task = subscription._stop_task()
            if task is not None:
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if undelivered:
            logger.info(
                "%d undelivered %s item(s) discarded at shutdown",
                undelivered,
                self.what,
                extra={"stream": self.what, "undelivered": undelivered},
            )


# ----------------------------------------------------------------------- connection


class _PeerGone(Exception):
    """The node's socket closed (raised by `_Connection.receive`)."""

    def __init__(self, code: int | None) -> None:
        super().__init__(f"socket closed with code {code}")
        self.code = code


class _VoidedCapture(ProtocolError):
    """A `capture_data` chunk broke the protocol and voided its pending capture."""

    def __init__(self, detail: str, capture_id: str) -> None:
        super().__init__(detail, message_type="capture_data")
        self.capture_id = capture_id


class _Connection:
    """One accepted node socket.

    Everything sent to the node goes through `send`, which only queues; `write_loop`
    (one task) owns the sending side of the socket. So frames never interleave, and
    nothing that sends (the receive loop, `Hub.capture`, `Hub.reassign`) ever waits
    for a node that reads slowly. `request_close` queues the close frame behind what
    is already queued and bounds how long the socket may take to go away.
    """

    def __init__(self, websocket: WebSocket, peer: str) -> None:
        self.websocket = websocket
        self.peer = peer
        self.session: _Session | None = None
        self.done = asyncio.Event()
        self.peer_gone = False
        self.congested = False
        self.bad_messages = 0
        self.unknown_types: set[str] = set()
        self.unknown_overflow = False
        self.close_code: int | None = None
        self._queue: collections.deque[str] = collections.deque()
        self._wake = asyncio.Event()
        self._close: tuple[int, str] | None = None
        self._tasks: tuple[asyncio.Task[None], ...] = ()
        self._abort_timer: asyncio.TimerHandle | None = None

    @property
    def closing(self) -> bool:
        """Whether a close has been requested (nothing more is accepted for sending)."""
        return self._close is not None

    def send(self, message: ControlMessage) -> bool:
        """Queue a control message for the node. Never waits.

        Returns False, without queueing, when the socket is closing or when the node
        has let `OUTBOUND_QUEUE_LIMIT` messages pile up; in that case the socket is
        closed with `CLOSE_CODE_CONGESTED` and `congested` is set.
        """
        if self._close is not None:
            return False
        if len(self._queue) >= OUTBOUND_QUEUE_LIMIT:
            self.congested = True
            logger.warning(
                "node %s is not reading: %d messages unsent, closing",
                self.peer,
                len(self._queue),
                extra={"peer": self.peer, "unsent": len(self._queue)},
            )
            self.request_close(CLOSE_CODE_CONGESTED, "node is not reading")
            return False
        self._queue.append(dump_control(message))
        self._wake.set()
        return True

    def request_close(self, code: int, reason: str) -> None:
        """Close the socket once what is queued has been sent. Never waits.

        code, reason: the WebSocket close code and reason (ASCII, at most 123 bytes).
        Only the first request counts. From now on the socket has `CLOSE_TIMEOUT_S` to
        finish; after that its tasks are cancelled and the socket is abandoned to the
        ASGI server, which closes the transport.
        """
        if self._close is not None:
            return
        self._close = (code, reason)
        self.close_code = code
        self._wake.set()
        self._arm_abort()

    def attach(self, *tasks: asyncio.Task[None]) -> None:
        """Name the tasks to cancel when the socket does not close in time."""
        self._tasks = tasks
        if self._close is not None:
            self._arm_abort()

    def release(self) -> None:
        """Forget the close timer (the socket's tasks have ended)."""
        if self._abort_timer is not None:
            self._abort_timer.cancel()
            self._abort_timer = None

    def _arm_abort(self) -> None:
        if self._abort_timer is None:
            loop = asyncio.get_running_loop()
            self._abort_timer = loop.call_later(CLOSE_TIMEOUT_S, self._abort)

    def _abort(self) -> None:
        """The socket did not close in time: stop serving it."""
        stuck = [task for task in self._tasks if not task.done()]
        if stuck:
            logger.warning(
                "node socket %s did not close within %.1f s; abandoning it",
                self.peer,
                CLOSE_TIMEOUT_S,
                extra={"peer": self.peer, "close_code": self.close_code},
            )
        for task in stuck:
            task.cancel()

    async def receive(self, timeout_s: float) -> str | bytes | None:
        """Wait for the next frame.

        timeout_s: longest wait, seconds.
        Returns the text of a text frame, the bytes of a binary frame, or None when
        nothing arrived in time. Raises `_PeerGone` when the socket has closed
        (whichever side closed it).
        """
        try:
            async with asyncio.timeout(max(timeout_s, 0.0)):
                message = await self.websocket.receive()
        except TimeoutError:
            return None
        if message["type"] == "websocket.disconnect":
            self.peer_gone = True
            raise _PeerGone(message.get("code"))
        text = message.get("text")
        if text is not None:
            return str(text)
        data = message.get("bytes")
        return bytes(data) if data is not None else b""

    async def write_loop(self) -> None:
        """Send what `send` queued, in order, then the close frame when one is requested."""
        websocket = self.websocket
        try:
            while True:
                while self._queue and not self.peer_gone:
                    await websocket.send_text(self._queue.popleft())
                if self._close is not None:
                    if not self.peer_gone:
                        code, reason = self._close
                        await websocket.close(code, reason)
                    return
                self._wake.clear()
                await self._wake.wait()
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            # The socket went away under a send (Starlette raises WebSocketDisconnect
            # or RuntimeError, an ASGI server OSError). Nothing more can be sent, so
            # the socket counts as closing from here on; the receive side learns of
            # the disconnect by itself, and the close timer bounds the wait for that.
            self.peer_gone = True
            self._queue.clear()
            logger.debug(
                "send to node %s failed: %s",
                self.peer,
                type(exc).__name__,
                extra={"peer": self.peer},
            )
            self.request_close(_CLOSE_CODE_NORMAL, "")


# -------------------------------------------------------------------------- session


@dataclass(frozen=True)
class _OtherVersion:
    """A `hello` of a protocol version this server does not speak.

    Its other fields need not fit version 1, so only what the log line needs is kept.
    """

    protocol: int
    site_id: str
    node_id: str


@dataclass(eq=False)
class _Pending:
    """A capture that has been requested and not yet resolved."""

    request: CaptureRequest
    assembler: CaptureAssembler
    future: asyncio.Future[AssembledCapture]
    started: float


class _Session:
    """State of one welcomed node; kept after it ends as the site's last known state."""

    def __init__(
        self,
        conn: _Connection,
        hello: Hello,
        channels: Iterable[ChannelAssignment],
        config_version: int,
        heartbeat_s: float,
        now_ns: int,
    ) -> None:
        self.conn = conn
        self.site_id: str = hello.site_id
        self.node_id: str = hello.node_id
        self.software: str = hello.software
        self.driver: str = hello.driver
        self.clock: ClockStatus = hello.clock
        self.connected_since_ns: int = now_ns
        self.heartbeat_s = heartbeat_s
        # A sim_update sent while the assignment was not yet acknowledged may have met
        # no open channel at the node: it is sent again after the assign_ack.
        self.awaiting_ack = True
        self.sim_resend = False
        self.future_warned = False
        self.assigned: dict[str, ChannelAssignment] = {}
        self.monitors: dict[tuple[str, str], MonitorAssignment] = {}
        self.assigned_version = config_version
        self.ready: dict[str, Capabilities] = {}
        self.channel_errors: dict[str, str] = {}
        self.pending: dict[str, _Pending] = {}
        self.rtt_ms: collections.deque[float] = collections.deque(maxlen=RTT_WINDOW)
        self.last_rx = time.monotonic()
        self.ended = False
        self.end_reason: str | None = None
        self.psd_shape_reported: set[tuple[str, str]] = set()
        self.set_assignment(channels, config_version)

    def set_assignment(self, channels: Iterable[ChannelAssignment], config_version: int) -> None:
        """Record a new assignment; no channel is ready until it is acknowledged."""
        self.assigned = {channel.channel_id: channel for channel in channels}
        self.monitors = {
            (channel.channel_id, monitor.monitor_id): monitor
            for channel in self.assigned.values()
            for monitor in channel.monitors
        }
        self.assigned_version = config_version
        self.ready = {}
        self.channel_errors = {}
        self.psd_shape_reported.clear()
        self.awaiting_ack = True

    def log_fields(self) -> dict[str, Any]:
        """Fields identifying the session, for the `extra` of a log record."""
        return {"site_id": self.site_id, "node_id": self.node_id, "peer": self.conn.peer}


_COUNTED_CAPTURE_CODES: Final[frozenset[str]] = CAPTURE_ERROR_CODES | {
    "offline",
    "disconnected",
    "replaced",
    "reassigned",
    "bad_data",
    "shutdown",
}
"""Failure codes of a requested capture that `Hub.counters` tells apart."""

_CAPTURE_CODE_OF_END: Final[dict[str, str]] = {
    "replaced": "replaced",
    "offline": "offline",
    "shutdown": "shutdown",
    "removed": "reassigned",
}
"""Capture failure code for the reason a session ended; anything else: `disconnected`."""


# ------------------------------------------------------------------------- helpers


def _clip(text: Any, limit: int = _LOG_TEXT_CHARS) -> str:
    """Peer-supplied text made safe for a log line: `repr`, cut to `limit` characters."""
    shown = repr(text)
    return shown if len(shown) <= limit else shown[: limit - 3] + "..."


def _peer_label(websocket: WebSocket) -> str:
    """`host:port` of the remote end of a socket, or `?` when it is not known."""
    client = getattr(websocket, "client", None)
    if client is None:
        return "?"
    try:
        host, port = client[0], client[1]
    except (TypeError, IndexError, KeyError):
        return "?"
    return f"{host}:{port}"


def _iso_utc(t_ns: int) -> str:
    """ISO 8601 UTC text (`...Z`) of integer nanoseconds, exact to the nanosecond.

    Whole seconds have no fraction; otherwise 3, 6 or 9 fractional digits, the fewest
    that are exact (the same form as `core.timebase.iso_from_ns`).
    """
    secs, frac = divmod(t_ns, _NS_PER_S)
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs))
    if frac == 0:
        return f"{base}Z"
    if frac % 1_000_000 == 0:
        return f"{base}.{frac // 1_000_000:03d}Z"
    if frac % 1_000 == 0:
        return f"{base}.{frac // 1_000:06d}Z"
    return f"{base}.{frac:09d}Z"


def _site_list(site_ids: Iterable[str]) -> list[str]:
    """The `site_ids` argument of a method as a list without repeats, order kept.

    Raises `ValueError` for a single string, which would otherwise be read as its
    characters.
    """
    if isinstance(site_ids, (str, bytes)):
        raise ValueError("site_ids: must be an iterable of site ids, not a single string")
    return list(dict.fromkeys(site_ids))


def _as_int(value: Any, name: str) -> int:
    """An integer argument (numpy integers included) as a Python int.

    Raises `ValueError` naming the argument for booleans, floats and everything else.
    """
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name}: must be an integer, got {type(value).__name__}")
    return int(value)


def _as_float(value: Any, name: str) -> float:
    """A real-number argument (numpy scalars included) as a Python float.

    Raises `ValueError` naming the argument for booleans, strings and everything else.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"{name}: must be a number, got {type(value).__name__}")
    return float(value)


# ------------------------------------------------------------------------------ hub


class Hub:
    """All node sessions of a server, and the operations the pipeline runs on them.

    Construct it once per application and call `handle` for every socket accepted at
    `/ws/node`. Every method must be called on the event loop that serves the
    sockets; nothing here is thread-safe.
    """

    def __init__(
        self,
        settings: Settings,
        assignments_for: AssignmentsProvider,
        config_version: Callable[[], int],
        simulation_info: Callable[[], Mapping[str, Any]],
        *,
        clock: Callable[[], int] = time.time_ns,
        psd_future_tolerance_s: float = PSD_FUTURE_TOLERANCE_S,
    ) -> None:
        """
        settings: tokens (`check_node_token`), `heartbeat_s`, `hello_timeout_s` and
            `capture_grace_s`.
        assignments_for: `assignments_for(site_id)` returns the channels the node of
            that site must open (channel ids unique), or None when the configuration
            has no such site. Called at every `hello` and by `reassign`.
        config_version: returns the version of the configuration those channels come
            from (a non-negative integer); must change whenever they change.
        simulation_info: returns the `simulation` object of `welcome`:
            `{"enabled": bool, "scenario_url"?: str, "version"?: int}`.
        clock: the server clock, integer UTC ns (`welcome.server_t_ns`, heartbeat
            acks, `received_ns` of spectrum frames, status event times, capture
            deadlines and the check of frames stamped in the future).
        psd_future_tolerance_s: how far ahead of `clock` a spectrum frame may be
            stamped before it is dropped, s.

        The providers are plain functions called on the event loop; they must not block.
        """
        self._settings = settings
        self._assignments_for = assignments_for
        self._config_version = config_version
        self._simulation_info = simulation_info
        self._clock = clock
        self._psd_future_ns = int(round(float(psd_future_tolerance_s) * _NS_PER_S))
        self._last_sim_update: SimUpdate | None = None
        self._window_health: WindowHealthProvider | None = None
        self._sessions: dict[str, _Session] = {}
        self._records: dict[str, _Session] = {}
        self._channel_site: dict[str, str] = {}
        self._last_frame: dict[tuple[str, str], float] = {}
        self._connections: set[_Connection] = set()
        self._psd: _Fanout[tuple[str, str], PsdFrame] = _Fanout("psd")
        self._status: _Fanout[tuple[str, int], StatusEvent] = _Fanout(
            "status", group=lambda key: key[0], backlog=STATUS_BACKLOG
        )
        self._status_serial = itertools.count(1)
        self._closed = False
        self._capture_prefix = secrets.token_hex(4)
        self._capture_seq = itertools.count(1)
        self._counters: collections.Counter[str] = collections.Counter()

    # ------------------------------------------------------------------ lifecycle

    @property
    def closed(self) -> bool:
        """Whether `close` has been called (and `start` not since)."""
        return self._closed

    def start(self) -> None:
        """Accept sessions (again). A new hub is started; this undoes `close`."""
        self._closed = False

    async def close(self) -> None:
        """Shut every session down with close code 1001 and stop delivering to subscribers.

        Pending captures fail with `CaptureFailed("shutdown")`. Sockets that connect
        afterwards are closed with 1001 at once, until `start` is called. Returns when
        every socket has finished, or after `CLOSE_TIMEOUT_S` plus a second for those
        that do not (logged). Calling it again is harmless.
        """
        self._closed = True
        for session in list(self._sessions.values()):
            self._end_session(session, "shutdown", "the server is shutting down")
        connections = list(self._connections)
        for conn in connections:
            conn.request_close(CLOSE_CODE_SHUTDOWN, "server shutting down")
        if connections:
            waits = asyncio.gather(*(conn.done.wait() for conn in connections))
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_S + 1.0):
                    await waits
            except TimeoutError:
                left = sum(1 for conn in connections if not conn.done.is_set())
                logger.warning(
                    "%d node socket(s) still open after hub close", left, extra={"open": left}
                )
        await self._psd.stop()
        await self._status.stop()
        logger.info("hub closed", extra={"sockets_closed": len(connections)})

    # ---------------------------------------------------------------- subscriptions

    def subscribe_psd(
        self, callback: PsdCallback, *, name: str | None = None
    ) -> Subscription[tuple[str, str], PsdFrame]:
        """Deliver every accepted spectrum frame to `callback(frame: PsdFrame)`.

        callback: a plain function or a coroutine function. It never runs inside a
            session's receive loop; while it is busy only the newest undelivered frame
            of each (channel, monitor) is kept. See `Subscription`.
        name: label for log lines; the callback's name when None.
        Frames are delivered only for channels whose current assignment the node has
        acknowledged, and only when `nfft` matches the assigned window.
        Returns the `Subscription`; call its `unsubscribe()` to stop.
        """
        return self._psd.subscribe(callback, name)

    def subscribe_status(
        self, callback: StatusCallback, *, name: str | None = None
    ) -> Subscription[tuple[str, int], StatusEvent]:
        """Deliver every node state change to `callback(event: StatusEvent)`.

        callback: a plain function or a coroutine function, as for `subscribe_psd`.
            Events arrive one at a time in the order they happened, every one of
            them: a session that is replaced gives `offline` (`replaced`) and then
            `online` (`hello`) for the same site. While the callback is busy, events
            wait; when more than `STATUS_BACKLOG` wait, only the newest of each site
            is kept, so a subscriber that fell behind still ends on the latest state
            of every site.
        name: label for log lines; the callback's name when None.
        Returns the `Subscription`; call its `unsubscribe()` to stop.
        """
        return self._status.subscribe(callback, name)

    def set_window_health(self, provider: WindowHealthProvider | None) -> None:
        """Let node status carry what the detection pipeline knows of each window.

        provider: `window_health(channel_id, monitor_id)` (see `WindowHealthProvider`),
            or None to stop. Its fields are added to the window entries of
            `node_status`; a window it reports `blind` makes its channel not `ok`.
        """
        if provider is not None and not callable(provider):
            raise TypeError("set_window_health: provider must be callable or None")
        self._window_health = provider

    # ---------------------------------------------------------------------- queries

    def is_online(self, site_id: str) -> bool:
        """Whether a welcomed node of the site is connected and has been heard from
        within three heartbeat periods."""
        return site_id in self._sessions

    def online_sites(self) -> list[str]:
        """Ids of the sites with a node online, sorted."""
        return sorted(self._sessions)

    def site_of(self, channel_id: str) -> str | None:
        """The site whose node was last assigned the channel.

        None when no node seen by this hub was assigned it; the configuration, not
        the hub, is the authority for sites that never connected.
        """
        return self._channel_site.get(channel_id)

    def channel_ready(self, channel_id: str) -> bool:
        """Whether the channel may be tasked now: its node is online and has
        acknowledged the channel's current assignment with `ok`."""
        return self.channel_capabilities(channel_id) is not None

    def channel_capabilities(self, channel_id: str) -> Capabilities | None:
        """What the channel's driver reported in `assign_ack`, or None when the channel
        is not ready (see `channel_ready`).

        Tasking uses min(configured max_sample_rate_hz, capabilities.max_sample_rate_hz).
        """
        session = self._session_of_channel(channel_id)
        return None if session is None else session.ready.get(channel_id)

    def rtt_ms(self, site_id: str) -> float | None:
        """Worst heartbeat round trip the site's node reported over its last
        `RTT_WINDOW` heartbeats, milliseconds; None when the node is offline or has
        not measured one yet."""
        session = self._sessions.get(site_id)
        if session is None or not session.rtt_ms:
            return None
        return max(session.rtt_ms)

    def last_frame_age_s(self, channel_id: str, monitor_id: str) -> float | None:
        """Seconds since the last accepted spectrum frame of a monitor window.

        None when no frame of that window has been accepted. The age keeps growing
        while the node is offline.
        """
        last = self._last_frame.get((channel_id, monitor_id))
        return None if last is None else max(0.0, time.monotonic() - last)

    def node_status(self, site_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Status of every site this hub has seen a node for, sorted by site id.

        site_ids: when given, the result has exactly these sites, in this order; a
            site the hub has never seen is reported offline with null fields.
        Each entry is JSON-ready and shaped like `NodeStatus` of PROTOCOL.md section B:
        `{site_id, node_id, online, latency_ms, clock, driver, software,
        channels: [{channel_id, ok, last_frame_age_s, windows, detection_problem}],
        connected_since}`.
        `latency_ms` is the latest heartbeat round trip (None offline or before one is
        measured); `clock` the latest `ClockStatus` as a dict; `ok` whether the channel
        is ready and none of its windows is blind to detection; `last_frame_age_s` the
        age of the channel's newest spectrum frame over its monitors (None before
        any); `windows` one entry per assigned monitor window, `{monitor_id,
        last_frame_age_s}` plus the fields of the window-health provider
        (`set_window_health`; null without one); `detection_problem` a sentence naming
        the blind or floor-raised windows (None when there are none);
        `connected_since` ISO 8601 UTC of the `welcome` (None offline). For an offline
        site the other fields are the last known ones.
        """
        if site_ids is None:
            return [self._status_of(self._records[site_id]) for site_id in sorted(self._records)]
        result: list[dict[str, Any]] = []
        for site_id in _site_list(site_ids):
            record = self._records.get(site_id)
            if record is not None:
                result.append(self._status_of(record))
            else:
                result.append(
                    {
                        "site_id": site_id,
                        "node_id": None,
                        "online": False,
                        "latency_ms": None,
                        "clock": None,
                        "driver": None,
                        "software": None,
                        "channels": [],
                        "connected_since": None,
                    }
                )
        return result

    def counters(self) -> dict[str, int]:
        """Running totals for diagnostics: sessions, rejects by reason, malformed
        messages, frames discarded and why, captures by outcome."""
        return dict(self._counters)

    # --------------------------------------------------------------------- captures

    async def capture(
        self,
        channel_id: str,
        t_start_ns: int,
        n_samples: int,
        sample_rate_hz: float,
        center_hz: float,
        deadline_ns: int,
    ) -> AssembledCapture:
        """Ask the channel's node for a time-stamped IQ capture and wait for it.

        channel_id: the channel to read.
        t_start_ns: labelled time of the first sample wanted, integer UTC ns.
        n_samples: complex samples wanted (at least 1).
        sample_rate_hz: complex sample rate, Hz.
        center_hz: tuning centre, Hz.
        deadline_ns: node-clock time by which the node must have sent the last chunk,
            integer UTC ns, not before `t_start_ns`. The server waits until its own
            clock passes it plus `Settings.capture_grace_s`.

        Returns the `AssembledCapture`: `iq` complex64[n_samples] (owned, bit-exact),
        `t_first_ns` (labelled time of `iq[0]`, integer UTC ns), `sample_rate_hz` and
        `center_hz` as the node actually used them (they need not equal the request),
        `overflow` and `clock` (the node's `ClockStatus` at the capture).

        Raises `CaptureFailed(code, message)` when no samples can be had (see its
        codes), `ValueError` naming the argument when one is not a valid request
        (wrong type, non-positive rate, deadline before the start). A capture of more
        than 64 MiB is `CaptureFailed("too_large")`. Cancelling the awaiting task
        withdraws the request (`capture_cancel` is sent).
        """
        n = _as_int(n_samples, "n_samples")
        if n > MAX_CAPTURE_SAMPLES:
            self._counters["capture_refused_too_large"] += 1
            raise CaptureFailed(
                "too_large",
                f"{n} samples are {n * 8} bytes, above the limit of {MAX_CAPTURE_BYTES} "
                "bytes per capture",
                channel_id=channel_id,
            )
        capture_id = f"c{self._capture_prefix}-{next(self._capture_seq)}"
        request = CaptureRequest(
            capture_id=capture_id,
            channel_id=channel_id,
            t_start_ns=_as_int(t_start_ns, "t_start_ns"),
            n_samples=n,
            sample_rate_hz=_as_float(sample_rate_hz, "sample_rate_hz"),
            center_hz=_as_float(center_hz, "center_hz"),
            deadline_ns=_as_int(deadline_ns, "deadline_ns"),
        )
        session = self._taskable_session(channel_id)
        if len(session.pending) >= MAX_CONCURRENT_CAPTURES:
            self._counters["capture_refused_limit"] += 1
            raise CaptureFailed(
                "limit",
                f"the node of site {session.site_id!r} already has "
                f"{MAX_CONCURRENT_CAPTURES} captures pending",
                channel_id=channel_id,
            )
        wait_s = (request.deadline_ns - self._clock()) / _NS_PER_S
        if wait_s <= 0.0:
            self._counters["capture_failed_deadline"] += 1
            raise CaptureFailed(
                "deadline", "deadline_ns has already passed", channel_id=channel_id
            )
        pending = _Pending(
            request=request,
            assembler=CaptureAssembler(capture_id, channel_id, expected_n_samples=n),
            future=asyncio.get_running_loop().create_future(),
            started=time.monotonic(),
        )
        # Sent before it is registered: a send that fails may end the session, which
        # fails what is registered, and this capture reports its own failure. Nothing
        # can answer the request before the next line runs (there is no await between).
        if not self._send(session, request):
            self._counters["capture_failed_disconnected"] += 1
            raise CaptureFailed(
                "disconnected",
                f"the socket of site {session.site_id!r} is closing",
                channel_id=channel_id,
                capture_id=capture_id,
            )
        session.pending[capture_id] = pending
        self._counters["capture_requested"] += 1
        try:
            async with asyncio.timeout(wait_s + self._settings.capture_grace_s):
                result = await pending.future
        except TimeoutError:
            self._withdraw(session, pending)
            self._counters["capture_failed_deadline"] += 1
            logger.warning(
                "capture %s on channel %r missed its deadline (%d of %d samples received)",
                capture_id,
                channel_id,
                pending.assembler.received_samples,
                n,
                extra={
                    **session.log_fields(),
                    "capture_id": capture_id,
                    "channel_id": channel_id,
                    "received_samples": pending.assembler.received_samples,
                },
            )
            raise CaptureFailed(
                "deadline",
                f"{pending.assembler.received_samples} of {n} samples had arrived by the deadline",
                channel_id=channel_id,
                capture_id=capture_id,
            ) from None
        except asyncio.CancelledError:
            self._withdraw(session, pending)
            self._counters["capture_cancelled"] += 1
            raise
        except CaptureFailed as failure:
            # Codes come from the node too; only known ones get a counter of their own.
            known = failure.code in _COUNTED_CAPTURE_CODES
            self._counters[f"capture_failed_{failure.code if known else 'other'}"] += 1
            raise
        self._counters["capture_completed"] += 1
        logger.debug(
            "capture %s complete: %d samples in %d chunk(s), %.3f s",
            capture_id,
            result.n_samples,
            result.n_chunks,
            time.monotonic() - pending.started,
            extra={**session.log_fields(), "capture_id": capture_id, "channel_id": channel_id},
        )
        return result

    def _taskable_session(self, channel_id: str) -> _Session:
        """The live session whose node may be tasked on the channel now.

        Raises `CaptureFailed` with `shutdown`, `unknown_channel`, `offline` or
        `not_ready`.
        """
        if self._closed:
            raise CaptureFailed("shutdown", "the hub is closed", channel_id=channel_id)
        site_id = self._channel_site.get(channel_id)
        if site_id is None:
            self._counters["capture_refused_unknown_channel"] += 1
            raise CaptureFailed(
                "unknown_channel",
                f"no node has been assigned channel {channel_id!r}",
                channel_id=channel_id,
            )
        session = self._sessions.get(site_id)
        if session is None or channel_id not in session.assigned:
            self._counters["capture_refused_offline"] += 1
            raise CaptureFailed(
                "offline", f"the node of site {site_id!r} is not connected", channel_id=channel_id
            )
        if channel_id not in session.ready:
            self._counters["capture_refused_not_ready"] += 1
            error = session.channel_errors.get(channel_id)
            detail = (
                f"the node reported: {error}"
                if error is not None
                else "the node has not acknowledged its assignment yet"
            )
            raise CaptureFailed(
                "not_ready", f"channel {channel_id!r} is not ready: {detail}", channel_id=channel_id
            )
        return session

    def _withdraw(self, session: _Session, pending: _Pending) -> None:
        """Forget a capture nobody waits for any more and tell the node (advisory)."""
        capture_id = pending.request.capture_id
        if session.pending.pop(capture_id, None) is not None and not session.ended:
            self._send(session, CaptureCancel(capture_id=capture_id))

    def _fail_pending(
        self,
        session: _Session,
        code: str,
        message: str,
        channel_ids: set[str] | None = None,
    ) -> int:
        """Fail the session's pending captures (those on `channel_ids` when given).

        Returns how many were failed.
        """
        failed = 0
        for capture_id, pending in list(session.pending.items()):
            channel_id = pending.request.channel_id
            if channel_ids is not None and channel_id not in channel_ids:
                continue
            del session.pending[capture_id]
            if not pending.future.done():
                pending.future.set_exception(
                    CaptureFailed(code, message, channel_id=channel_id, capture_id=capture_id)
                )
                failed += 1
        return failed

    # ---------------------------------------------------------- configuration changes

    async def reassign(self, site_ids: Iterable[str] | None = None) -> list[str]:
        """Send `assign` to nodes after a configuration change.

        site_ids: the sites to bring up to date; None for every site the hub has seen.
        For each of them the providers are asked for the current channels and version:

        - Online and changed (the version or any channel differs from what the node was
          last sent): pending captures on removed or changed channels fail with
          `CaptureFailed("reassigned")`, `assign` is sent, and none of the site's
          channels is ready until the node acknowledges that version. Captures pending
          on unchanged channels go on.
        - Online and unchanged: nothing is sent.
        - Online but no longer in the configuration (`assignments_for` returns None):
          the session is closed with `CLOSE_CODE_REJECTED`; its captures fail.
        - Offline: only `site_of` and `node_status` are brought up to date.

        Returns the ids of the sites an `assign` was sent to, sorted. The method only
        queues the messages; it does not wait for acknowledgements (see
        `channel_ready`). Exceptions raised by a provider propagate.
        """
        targets = sorted(self._records) if site_ids is None else _site_list(site_ids)
        version = _as_int(self._config_version(), "config_version")
        sent: list[str] = []
        for site_id in targets:
            channels = self._assignments_for(site_id)
            session = self._sessions.get(site_id)
            if session is None:
                self._refresh_record(site_id, channels, version)
                continue
            if channels is None:
                logger.warning(
                    "site %r left the configuration; closing its node session",
                    site_id,
                    extra=session.log_fields(),
                )
                self._end_session(session, "removed", "the site left the configuration")
                self._forget_site(site_id)
                session.conn.request_close(CLOSE_CODE_REJECTED, "unknown_site")
                continue
            new = {channel.channel_id: channel for channel in channels}
            if version == session.assigned_version and new == session.assigned:
                continue
            message = Assign(config_version=version, channels=list(channels))
            affected = {
                channel_id
                for channel_id, old in session.assigned.items()
                if new.get(channel_id) != old
            }
            failed = self._fail_pending(
                session,
                "reassigned",
                f"configuration version {version} removed or changed the channel",
                affected,
            )
            session.set_assignment(channels, version)
            self._map_channels(session)
            if not self._send(session, message):
                continue
            sent.append(site_id)
            logger.info(
                "assign sent to site %r: version %d, %d channel(s), %d capture(s) failed",
                site_id,
                version,
                len(new),
                failed,
                extra={**session.log_fields(), "config_version": version, "failed": failed},
            )
            self._publish_status(session, "channels", "assign")
        return sorted(sent)

    async def announce_sim_update(self, version: int, effective_ns: int) -> list[str]:
        """Tell every online node that the simulated world changed (`sim_update`).

        version: version of the new scenario document.
        effective_ns: labelled time from which samples use the new world, integer UTC
            ns (the caller sets it to server time plus `SIM_UPDATE_LEAD_NS`).
        Returns the ids of the sites the message was queued for, sorted. Raises
        `ValueError` naming the argument at fault.

        A node whose assignment is not acknowledged yet may have no channel open to
        take the update (a node hands it only to open channels); it gets the newest
        update again once its `assign_ack` arrives.
        """
        message = SimUpdate(
            version=_as_int(version, "version"), effective_ns=_as_int(effective_ns, "effective_ns")
        )
        self._last_sim_update = message
        sent: list[str] = []
        for session in list(self._sessions.values()):
            if self._send(session, message):
                sent.append(session.site_id)
                if session.awaiting_ack:
                    session.sim_resend = True
        logger.info(
            "sim_update version %d announced to %d node(s)",
            message.version,
            len(sent),
            extra={"sim_version": message.version, "effective_ns": message.effective_ns},
        )
        return sorted(sent)

    # ------------------------------------------------------------------ the session

    async def handle(self, websocket: WebSocket) -> None:
        """Serve one node socket from accept to close.

        websocket: the Starlette/FastAPI WebSocket of `/ws/node`, not yet accepted.
        Returns when the session is over: the node disconnected, was rejected or
        replaced, fell silent, sent three malformed messages in a row, stopped
        reading, or the hub closed. Never raises for anything a node does; cancelling
        the calling task ends the session.
        """
        await websocket.accept()
        conn = _Connection(websocket, _peer_label(websocket))
        self._connections.add(conn)
        if self._closed:
            conn.request_close(CLOSE_CODE_SHUTDOWN, "server shutting down")
        loop = asyncio.get_running_loop()
        reader = loop.create_task(self._read(conn), name=f"petal-hub-read-{conn.peer}")
        writer = loop.create_task(conn.write_loop(), name=f"petal-hub-write-{conn.peer}")
        conn.attach(reader, writer)
        try:
            await asyncio.wait({reader, writer})
        finally:
            conn.release()
            reader.cancel()
            writer.cancel()
            try:
                outcomes = await asyncio.gather(reader, writer, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        logger.error(
                            "node socket task failed", exc_info=outcome, extra={"peer": conn.peer}
                        )
            finally:
                # Whatever happens above (a second cancellation included), the session
                # must not stay registered and `close` must not wait for this socket.
                if conn.session is not None:
                    self._end_session(conn.session, "disconnected", "the node's socket closed")
                self._connections.discard(conn)
                conn.done.set()

    async def _read(self, conn: _Connection) -> None:
        """Receive side of one socket: the handshake, then the session."""
        try:
            session = await self._handshake(conn)
            if session is not None:
                await self._run_session(session)
        except _PeerGone as gone:
            logger.debug(
                "node socket %s closed (code %s)", conn.peer, gone.code, extra={"peer": conn.peer}
            )
        except Exception:  # a bug here or in a provider must not leave the socket half-served
            logger.exception("serving node socket %s failed", conn.peer, extra={"peer": conn.peer})
            self._counters["internal_errors"] += 1
            if conn.session is not None:
                self._end_session(conn.session, "error", "the server failed serving the session")
            conn.request_close(CLOSE_CODE_INTERNAL, "internal error")
        finally:
            # Wake the writer so that it ends too; a no-op when a close is under way.
            conn.request_close(_CLOSE_CODE_NORMAL, "")

    async def _handshake(self, conn: _Connection) -> _Session | None:
        """Wait for a valid `hello` and answer it.

        Returns the session after `welcome`, or None when the socket was rejected or
        is closing.
        """
        deadline = time.monotonic() + self._settings.hello_timeout_s
        while not conn.closing:
            frame = await conn.receive(deadline - time.monotonic())
            if conn.closing:
                return None
            if frame is None:
                self._counters["hello_timeouts"] += 1
                logger.warning(
                    "no hello from %s within %.1f s; closing",
                    conn.peer,
                    self._settings.hello_timeout_s,
                    extra={"peer": conn.peer},
                )
                conn.request_close(CLOSE_CODE_SILENT, "no hello")
                return None
            hello: Hello | _OtherVersion | None = None
            if isinstance(frame, bytes):
                self._bad_message(conn, "binary frame: expected hello first")
            else:
                try:
                    hello = self._parse_hello(conn, frame)
                except ProtocolError as exc:
                    self._bad_message(conn, exc.detail)
            if hello is None:
                await asyncio.sleep(0)  # one turn of the event loop per frame, as below
                continue
            conn.bad_messages = 0
            if isinstance(hello, _OtherVersion):
                self._reject(conn, hello.site_id, hello.node_id, "protocol_mismatch")
                return None
            return self._admit(conn, hello)
        return None

    def _parse_hello(self, conn: _Connection, text: str) -> Hello | _OtherVersion | None:
        """Read the first message of a socket.

        Returns the `Hello` (always of this protocol version), `_OtherVersion` for a
        hello that announces another version (whatever its other fields look like, so
        that it can be answered with `reject protocol_mismatch`), or None for a
        message to ignore (unknown type).
        Raises `ProtocolError` for anything malformed or out of place.
        """
        obj = loads_json_object(text, what="control message")
        mtype = message_type(obj, what="control message")
        if mtype != "hello":
            parsed = parse_control(text)
            if isinstance(parsed, UnknownMessage):
                conn.bad_messages = 0
                self._warn_unknown(conn, None, parsed.type)
                return None
            raise ProtocolError(f"{mtype}: expected hello first", message_type=mtype)
        protocol = obj.get("protocol")
        if type(protocol) is int and protocol != PROTOCOL_VERSION:
            return _OtherVersion(
                protocol=protocol,
                site_id=str(obj.get("site_id", "")),
                node_id=str(obj.get("node_id", "")),
            )
        return Hello.from_wire(obj)

    def _admit(self, conn: _Connection, hello: Hello) -> _Session | None:
        """Decide on a `hello`: reject it, or register the session and send `welcome`.

        Runs without waiting, so two sockets can never both be admitted for one site.
        """
        # The token is checked before the site is looked up, so that only a node that
        # holds a token learns whether a site exists.
        if not self._settings.check_node_token(hello.site_id, hello.token):
            self._reject(conn, hello.site_id, hello.node_id, "bad_token")
            return None
        channels = self._assignments_for(hello.site_id)
        if channels is None:
            self._reject(conn, hello.site_id, hello.node_id, "unknown_site")
            return None
        existing = self._sessions.get(hello.site_id)
        if existing is not None and existing.node_id != hello.node_id:
            self._reject(conn, hello.site_id, hello.node_id, "duplicate_node")
            return None

        version = _as_int(self._config_version(), "config_version")
        simulation = self._simulation_info()
        if not isinstance(simulation, Mapping):
            raise TypeError(
                f"simulation_info: must return a mapping, got {type(simulation).__name__}"
            )
        welcome = Welcome(
            server_t_ns=self._clock(),
            heartbeat_s=self._settings.heartbeat_s,
            config_version=version,
            channels=list(channels),
            simulation=SimulationInfo(**simulation),
        )
        if existing is not None:
            logger.info(
                "node %s of site %r reconnected from %s; replacing its session",
                _clip(hello.node_id),
                hello.site_id,
                conn.peer,
                extra=existing.log_fields(),
            )
            self._counters["sessions_replaced"] += 1
            self._end_session(existing, "replaced", "a new session of the same node took over")
            existing.conn.request_close(CLOSE_CODE_REPLACED, "replaced")

        session = _Session(
            conn, hello, channels, version, self._settings.heartbeat_s, welcome.server_t_ns
        )
        conn.session = session
        self._sessions[session.site_id] = session
        self._records[session.site_id] = session
        self._map_channels(session)
        conn.send(welcome)
        self._counters["sessions_started"] += 1
        logger.info(
            "node online: site %r node %s from %s (%s, driver %s), version %d, %d channel(s)",
            session.site_id,
            _clip(session.node_id),
            conn.peer,
            _clip(session.software),
            _clip(session.driver),
            version,
            len(session.assigned),
            extra={
                **session.log_fields(),
                "config_version": version,
                # Node clock minus server clock at the hello, transit included, ns.
                "hello_clock_minus_server_ns": hello.t_ns - welcome.server_t_ns,
            },
        )
        self._publish_status(session, "online", "hello")
        return session

    def _reject(self, conn: _Connection, site_id: str, node_id: str, reason: str) -> None:
        """Answer a `hello` with `reject` and close the socket.

        site_id, node_id: what the hello claimed, for the log only.
        """
        self._counters[f"rejected_{reason}"] += 1
        logger.warning(
            "rejected hello from %s: %s (site %s, node %s)",
            conn.peer,
            reason,
            _clip(site_id),
            _clip(node_id),
            extra={"peer": conn.peer, "reason": reason},
        )
        conn.send(Reject(reason=reason))
        conn.request_close(CLOSE_CODE_REJECTED, reason)

    async def _run_session(self, session: _Session) -> None:
        """Receive loop of a welcomed session; returns when the session is over."""
        conn = session.conn
        silence_limit_s = HEARTBEAT_MISS_LIMIT * session.heartbeat_s
        while True:
            frame = await conn.receive(session.last_rx + silence_limit_s - time.monotonic())
            if session.ended or conn.closing:
                return
            if frame is None:
                silent_s = time.monotonic() - session.last_rx
                logger.warning(
                    "site %r silent for %.2f s (%d heartbeat periods); marking it offline",
                    session.site_id,
                    silent_s,
                    HEARTBEAT_MISS_LIMIT,
                    extra={**session.log_fields(), "silent_s": silent_s},
                )
                self._counters["sessions_silent"] += 1
                self._end_session(
                    session, "offline", f"the node was silent for {HEARTBEAT_MISS_LIMIT} heartbeats"
                )
                conn.request_close(CLOSE_CODE_SILENT, "heartbeat timeout")
                return
            session.last_rx = time.monotonic()  # any frame counts as liveness
            if isinstance(frame, str):
                self._on_text(session, frame)
            else:
                self._on_binary(session, frame)
            # Frames that arrive back to back never suspend `receive`. One turn of the
            # event loop per frame lets the writer send the reply and the subscribers
            # take the frame before the next one is handled, so a burst neither fills
            # the outbound queue nor overwrites frames a prompt subscriber would take.
            await asyncio.sleep(0)
            if session.ended or conn.closing:
                return

    # ------------------------------------------------------------- incoming frames

    def _on_text(self, session: _Session, text: str) -> None:
        """Handle one text frame of a welcomed session."""
        conn = session.conn
        try:
            message = parse_control(text)
        except ProtocolError as exc:
            self._bad_message(conn, exc.detail)
            return
        if isinstance(message, UnknownMessage):
            conn.bad_messages = 0
            self._warn_unknown(conn, session, message.type)
            return
        if isinstance(message, Heartbeat):
            conn.bad_messages = 0
            self._on_heartbeat(session, message)
        elif isinstance(message, AssignAck):
            conn.bad_messages = 0
            self._on_assign_ack(session, message)
        elif isinstance(message, CaptureErrorMessage):
            conn.bad_messages = 0
            self._on_capture_error(session, message)
        elif isinstance(message, ErrorMessage):
            conn.bad_messages = 0
            self._counters["errors_from_nodes"] += 1
            logger.warning(
                "site %r reports an error in what the server sent: %s %s",
                session.site_id,
                _clip(message.code, 40),
                _clip(message.detail),
                extra=session.log_fields(),
            )
        else:
            # hello after welcome, or a message only a server sends.
            self._bad_message(conn, f"{message.type}: not a message a node sends at this point")

    def _on_binary(self, session: _Session, data: bytes) -> None:
        """Handle one binary frame of a welcomed session."""
        conn = session.conn
        try:
            raw_header, payload = decode_frame(data)
            header = parse_binary_header(raw_header)
            if isinstance(header, UnknownMessage):
                self._warn_unknown(conn, session, header.type)
            elif isinstance(header, PsdHeader):
                self._on_psd(session, header, payload)
            else:
                self._on_capture_data(session, header, payload)
        except ProtocolError as exc:
            self._bad_message(conn, exc.detail)
            if isinstance(exc, _VoidedCapture):
                # Advisory: the node may stop sending the rest of the voided capture.
                self._send(session, CaptureCancel(capture_id=exc.capture_id))
        else:
            conn.bad_messages = 0

    def _on_heartbeat(self, session: _Session, beat: Heartbeat) -> None:
        """Answer a heartbeat and record what it says about the node."""
        clock_changed = (
            beat.clock.locked != session.clock.locked or beat.clock.source != session.clock.source
        )
        session.clock = beat.clock
        if beat.rtt_ms is not None:
            session.rtt_ms.append(beat.rtt_ms)
        self._send(session, HeartbeatAck(seq=beat.seq, server_t_ns=self._clock()))
        if clock_changed:
            logger.info(
                "clock of site %r is now %s, %s",
                session.site_id,
                beat.clock.source,
                "locked" if beat.clock.locked else "unlocked",
                extra=session.log_fields(),
            )
            self._publish_status(session, "clock", "heartbeat")

    def _on_assign_ack(self, session: _Session, ack: AssignAck) -> None:
        """Mark the channels a node has opened as ready."""
        if ack.config_version != session.assigned_version:
            self._counters["assign_ack_stale"] += 1
            logger.info(
                "ignoring assign_ack of version %d from site %r; version %d is assigned",
                ack.config_version,
                session.site_id,
                session.assigned_version,
                extra=session.log_fields(),
            )
            return
        ready: dict[str, Capabilities] = {}
        errors: dict[str, str] = {}
        for channel in ack.channels:
            if channel.channel_id not in session.assigned:
                logger.warning(
                    "assign_ack from site %r names channel %s, which it was not assigned",
                    session.site_id,
                    _clip(channel.channel_id),
                    extra=session.log_fields(),
                )
                continue
            if channel.ok and channel.capabilities is not None:
                ready[channel.channel_id] = channel.capabilities
            else:
                errors[channel.channel_id] = channel.error or "the driver did not open"
        for channel_id in session.assigned:
            if channel_id not in ready and channel_id not in errors:
                errors[channel_id] = "the node's assign_ack did not mention the channel"
        session.ready = ready
        session.channel_errors = errors
        session.awaiting_ack = False
        if session.sim_resend and self._last_sim_update is not None:
            # The channels the ack names are open now; an update that arrived while
            # they were opening met none of them.
            session.sim_resend = False
            if self._send(session, self._last_sim_update):
                self._counters["sim_update_resent"] += 1
                logger.info(
                    "sim_update version %d sent again to site %r after its assign_ack",
                    self._last_sim_update.version,
                    session.site_id,
                    extra={**session.log_fields(), "sim_version": self._last_sim_update.version},
                )
        level = logging.INFO if ack.ok and not errors else logging.WARNING
        logger.log(
            level,
            "assign_ack from site %r for version %d: %d of %d channel(s) ready%s",
            session.site_id,
            ack.config_version,
            len(ready),
            len(session.assigned),
            "" if not errors else "; not ready: " + ", ".join(
                f"{_clip(channel_id, 60)} ({_clip(error, 80)})"
                for channel_id, error in sorted(errors.items())
            ),
            extra={**session.log_fields(), "config_version": ack.config_version},
        )
        self._publish_status(session, "channels", "assign_ack")

    def _on_capture_error(self, session: _Session, error: CaptureErrorMessage) -> None:
        """Fail the capture a node could not serve; discard errors for unknown ids."""
        pending = session.pending.pop(error.capture_id, None)
        if pending is None:
            self._counters["capture_error_stale"] += 1
            logger.debug(
                "discarding capture_error for %s, which is not pending",
                _clip(error.capture_id),
                extra=session.log_fields(),
            )
            return
        if not pending.future.done():
            pending.future.set_exception(
                CaptureFailed(
                    error.code,
                    error.message,
                    channel_id=pending.request.channel_id,
                    capture_id=error.capture_id,
                )
            )

    def _on_capture_data(
        self, session: _Session, header: CaptureDataHeader, payload: memoryview
    ) -> None:
        """Add one chunk to its capture; discard chunks of captures that are not pending.

        A capture whose chunks announce another length than was requested is failed
        with `CaptureFailed("bad_data")` and its chunks are discarded: the frames are
        well-formed, the answer is not the one asked for (this is also how a capture
        above the 64 MiB limit ends).
        Raises `_VoidedCapture` (a `ProtocolError`) when the chunk breaks the protocol;
        the capture has then failed with `CaptureFailed("bad_data")` as well.
        """
        pending = session.pending.get(header.capture_id)
        if pending is None:
            self._counters["capture_data_stale"] += 1
            logger.debug(
                "discarding capture_data for %s, which is not pending",
                _clip(header.capture_id),
                extra=session.log_fields(),
            )
            return
        wanted = pending.request.n_samples
        if header.n_samples_total != wanted:
            del session.pending[header.capture_id]
            self._counters["capture_length_mismatch"] += 1
            detail = f"the node announced {header.n_samples_total} samples, {wanted} were requested"
            logger.warning(
                "capture %s on channel %r void: %s",
                header.capture_id,
                pending.request.channel_id,
                detail,
                extra={**session.log_fields(), "capture_id": header.capture_id},
            )
            if not pending.future.done():
                pending.future.set_exception(
                    CaptureFailed(
                        "bad_data",
                        detail,
                        channel_id=pending.request.channel_id,
                        capture_id=header.capture_id,
                    )
                )
            self._send(session, CaptureCancel(capture_id=header.capture_id))
            return
        try:
            capture = pending.assembler.add(header, payload)
        except ProtocolError as exc:
            del session.pending[header.capture_id]
            if not pending.future.done():
                pending.future.set_exception(
                    CaptureFailed(
                        "bad_data",
                        exc.detail,
                        channel_id=pending.request.channel_id,
                        capture_id=header.capture_id,
                    )
                )
            raise _VoidedCapture(exc.detail, header.capture_id) from None
        if capture is not None:
            del session.pending[header.capture_id]
            if not pending.future.done():
                pending.future.set_result(capture)

    def _on_psd(self, session: _Session, header: PsdHeader, payload: memoryview) -> None:
        """Check a spectrum frame against the assignment and hand it to subscribers.

        Raises `ProtocolError` for a malformed frame. A well-formed frame that does
        not belong (a window the node is not assigned, a channel not acknowledged
        under the current assignment, or the wrong number of bins) is dropped and
        counted: frames are droppable, and these occur around a reassignment. So is a
        frame stamped more than `psd_future_tolerance_s` ahead of the server clock
        (`psd_dropped_future`, warned about once per session); it does not count as
        the window's newest frame.
        """
        header, psd = decode_psd(header, payload)
        key = (header.channel_id, header.monitor_id)
        monitor = session.monitors.get(key)
        if monitor is None:
            self._counters["psd_dropped_unassigned"] += 1
            return
        if header.channel_id not in session.ready:
            self._counters["psd_dropped_not_ready"] += 1
            return
        if header.nfft != monitor.nfft:
            self._counters["psd_dropped_shape"] += 1
            if key not in session.psd_shape_reported:
                session.psd_shape_reported.add(key)
                logger.warning(
                    "site %r sends %d bins for window %s/%s, assigned %d; dropping such frames",
                    session.site_id,
                    header.nfft,
                    _clip(header.channel_id, 60),
                    _clip(header.monitor_id, 60),
                    monitor.nfft,
                    extra=session.log_fields(),
                )
            return
        received_ns = self._clock()
        ahead_ns = header.t_ns - received_ns
        if ahead_ns > self._psd_future_ns:
            # One such frame would become the newest time of its window, and the
            # detection pipeline would drop every correctly stamped frame after it.
            self._counters["psd_dropped_future"] += 1
            if not session.future_warned:
                session.future_warned = True
                logger.warning(
                    "site %r sends spectrum frames stamped %.1f s ahead of the server clock "
                    "(window %s/%s); dropping every frame more than %.0f s ahead (reported "
                    "once per session)",
                    session.site_id,
                    ahead_ns / _NS_PER_S,
                    _clip(header.channel_id, 60),
                    _clip(header.monitor_id, 60),
                    self._psd_future_ns / _NS_PER_S,
                    extra={**session.log_fields(), "ahead_s": round(ahead_ns / _NS_PER_S, 3)},
                )
            return
        self._last_frame[key] = time.monotonic()
        self._counters["psd_frames"] += 1
        self._psd.publish(
            key,
            PsdFrame(site_id=session.site_id, header=header, psd_db=psd, received_ns=received_ns),
        )

    # --------------------------------------------------------------- session helpers

    def _send(self, session: _Session, message: ControlMessage) -> bool:
        """Queue a message for a session's node; ends the session when it is congested."""
        if session.ended:
            return False
        if session.conn.send(message):
            return True
        if session.conn.congested:
            self._counters["sessions_congested"] += 1
            self._end_session(session, "congested", "the node stopped reading")
        return False

    def _bad_message(self, conn: _Connection, detail: str) -> None:
        """Answer a malformed message with `error`; the third in a row closes the socket."""
        conn.bad_messages += 1
        self._counters["bad_messages"] += 1
        logger.warning(
            "malformed message from %s (%d in a row): %s",
            conn.peer,
            conn.bad_messages,
            detail,
            extra={"peer": conn.peer, "detail": detail, "in_a_row": conn.bad_messages},
        )
        conn.send(ErrorMessage.bad_message(detail))
        if conn.bad_messages >= BAD_MESSAGE_LIMIT:
            self._counters["sessions_bad_messages"] += 1
            if conn.session is not None:
                self._end_session(
                    conn.session, "bad_messages", "the node sent malformed messages"
                )
            conn.request_close(CLOSE_CODE_BAD_MESSAGES, "malformed messages")

    def _warn_unknown(self, conn: _Connection, session: _Session | None, mtype: str) -> None:
        """Log a message of a type this version does not define; it is otherwise ignored.

        Every such message is counted. The warning is logged once per type and socket
        (a newer node may send its new message every second), and for at most
        `_UNKNOWN_TYPES_REPORTED` types per socket, so that the log cannot be flooded.
        """
        self._counters["unknown_messages"] += 1
        if mtype in conn.unknown_types:
            return
        source = conn.peer if session is None else f"site {session.site_id!r}"
        fields = {"peer": conn.peer} if session is None else session.log_fields()
        if len(conn.unknown_types) >= _UNKNOWN_TYPES_REPORTED:
            if not conn.unknown_overflow:
                conn.unknown_overflow = True
                logger.warning(
                    "%s sent more than %d unknown message types; further ones are "
                    "ignored without a log line",
                    source,
                    _UNKNOWN_TYPES_REPORTED,
                    extra=fields,
                )
            return
        conn.unknown_types.add(mtype)
        logger.warning(
            "ignoring messages of unknown type %s from %s (reported once)",
            _clip(mtype, 60),
            source,
            extra=fields,
        )

    def _end_session(self, session: _Session, reason: str, message: str) -> None:
        """Mark a session over: unregister it, fail its captures, tell subscribers.

        Only the first call for a session has an effect. Closing the socket is the
        caller's business.
        """
        if session.ended:
            return
        session.ended = True
        session.end_reason = reason
        if self._sessions.get(session.site_id) is session:
            del self._sessions[session.site_id]
        failed = self._fail_pending(
            session, _CAPTURE_CODE_OF_END.get(reason, "disconnected"), message
        )
        session.ready = {}
        self._counters["sessions_ended"] += 1
        logger.info(
            "node offline: site %r node %s (%s), %d pending capture(s) failed",
            session.site_id,
            _clip(session.node_id),
            reason,
            failed,
            extra={**session.log_fields(), "reason": reason, "failed": failed},
        )
        self._publish_status(session, "offline", reason)

    def _session_of_channel(self, channel_id: str) -> _Session | None:
        """The live session whose node is assigned the channel, if any."""
        site_id = self._channel_site.get(channel_id)
        if site_id is None:
            return None
        session = self._sessions.get(site_id)
        if session is None or channel_id not in session.assigned:
            return None
        return session

    def _map_channels(self, session: _Session) -> None:
        """Bring the channel-to-site map and the frame ages in line with an assignment."""
        site_id = session.site_id
        for channel_id in [c for c, s in self._channel_site.items() if s == site_id]:
            if channel_id not in session.assigned:
                del self._channel_site[channel_id]
                for key in [k for k in self._last_frame if k[0] == channel_id]:
                    del self._last_frame[key]
        for channel_id in session.assigned:
            holder = self._channel_site.get(channel_id)
            if holder is not None and holder != site_id:
                # Normal while a configuration change that moves a channel is being
                # applied site by site; a lasting conflict is a configuration error.
                logger.warning(
                    "channel %r moves from site %r to site %r (channel ids must be unique "
                    "across sites)",
                    channel_id,
                    holder,
                    site_id,
                    extra=session.log_fields(),
                )
            self._channel_site[channel_id] = site_id
            for key in [k for k in self._last_frame if k[0] == channel_id]:
                if key not in session.monitors:
                    del self._last_frame[key]

    def _refresh_record(
        self, site_id: str, channels: list[ChannelAssignment] | None, version: int
    ) -> None:
        """Bring the last known state of an offline site in line with the configuration."""
        record = self._records.get(site_id)
        if record is None:
            return
        if channels is None:
            self._forget_site(site_id)
            return
        record.set_assignment(channels, version)
        self._map_channels(record)

    def _forget_site(self, site_id: str) -> None:
        """Drop everything remembered about a site that left the configuration."""
        self._records.pop(site_id, None)
        for channel_id in [c for c, s in self._channel_site.items() if s == site_id]:
            del self._channel_site[channel_id]
            for key in [k for k in self._last_frame if k[0] == channel_id]:
                del self._last_frame[key]

    def _status_of(self, session: _Session) -> dict[str, Any]:
        """`NodeStatus` of a session (live, or the last one of its site)."""
        online = not session.ended and self._sessions.get(session.site_id) is session
        channels: list[dict[str, Any]] = []
        for channel_id, channel in session.assigned.items():
            windows = [self._window_status(channel_id, monitor.monitor_id) for monitor in channel.monitors]
            ages = [w["last_frame_age_s"] for w in windows if w["last_frame_age_s"] is not None]
            blind = [w for w in windows if w["blind"]] if online else []
            raised = [w for w in windows if w["floor_raised_db"] is not None] if online else []
            problems: list[str] = []
            if blind:
                problems.append(
                    "detection is blind on "
                    + ", ".join(f"{w['monitor_id']} ({w['note'] or w['blind_reason']})" for w in blind)
                )
            if raised:
                problems.append(
                    "noise floor raised on "
                    + ", ".join(f"{w['monitor_id']} (+{w['floor_raised_db']:.1f} dB)" for w in raised)
                    + ": a signal may fill the window and be taken for its noise"
                )
            channels.append(
                {
                    "channel_id": channel_id,
                    "ok": online and channel_id in session.ready and not blind,
                    "last_frame_age_s": min(ages) if ages else None,
                    "windows": windows,
                    "detection_problem": "; ".join(problems) if problems else None,
                }
            )
        return {
            "site_id": session.site_id,
            "node_id": session.node_id,
            "online": online,
            "latency_ms": session.rtt_ms[-1] if online and session.rtt_ms else None,
            "clock": session.clock.to_wire(),
            "driver": session.driver,
            "software": session.software,
            "channels": channels,
            "connected_since": _iso_utc(session.connected_since_ns) if online else None,
        }

    def _window_status(self, channel_id: str, monitor_id: str) -> dict[str, Any]:
        """One window entry of `NodeStatus.channels[].windows`: the hub's frame age plus
        what the window-health provider reports (null fields without one)."""
        entry: dict[str, Any] = {
            "monitor_id": monitor_id,
            "last_frame_age_s": self.last_frame_age_s(channel_id, monitor_id),
            "detection_frame_age_s": None,
            "blind": False,
            "blind_reason": None,
            "learning": None,
            "floor_raised_db": None,
            "note": None,
        }
        provider = self._window_health
        if provider is None:
            return entry
        try:
            health = provider(channel_id, monitor_id)
        except Exception:  # status must be reportable whatever the provider does
            self._counters["window_health_errors"] += 1
            logger.exception("window health of %s/%s could not be read", channel_id, monitor_id)
            return entry
        if health:
            for name in ("detection_frame_age_s", "blind", "blind_reason", "learning", "floor_raised_db", "note"):
                if name in health:
                    entry[name] = health[name]
            entry["blind"] = bool(entry["blind"])
        return entry

    def _publish_status(
        self,
        session: _Session,
        kind: Literal["online", "offline", "channels", "clock"],
        reason: str,
    ) -> None:
        """Offer a status event to the status subscribers, in order (see `subscribe_status`)."""
        self._status.publish(
            (session.site_id, next(self._status_serial)),
            StatusEvent(
                site_id=session.site_id,
                kind=kind,
                reason=reason,
                status=self._status_of(session),
                t_ns=self._clock(),
            ),
        )
