"""The console WebSocket ``/ws/ui`` (PROTOCOL.md section B).

Server to browser, text frames: ``status`` about once a second (the newest one at once
on connect), ``event`` with the whole ``Event`` on every change, ``config`` when the
configuration is replaced and ``sim`` when the scenario changes. Binary frames: PLB1
spectrum frames (``protocol.framing.encode_console_psd``) of the windows the browser
selected with ``{"type": "subscribe", "psd": [[channel_id, monitor_id], ...]}``.

Each connection owns a ``ConsoleSubscription`` of the runtime's ``ConsoleBus``: a
mailbox where a newer message of the same kind (status, config, sim, one event id, one
spectrum window) replaces an undelivered older one. A sender task drains it into the
socket; a receiver task reads the browser's messages. A slow browser therefore only
loses intermediate frames of its own windows: nothing waits for it, neither other
browsers nor the pipeline. A browser that stops reading altogether is closed (1013)
after ``SEND_TIMEOUT_S``.

Authentication: when ``Settings.api_token`` is set the handshake must carry
``?token=``; otherwise the socket is accepted and closed at once with 1008 (the
console tells the operator the token was refused). Shutdown: ``UiSockets.close_all``
closes every console socket with 1001 before the server stops.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from petal_locate.server.api import schemas
from petal_locate.server.api.deps import ws_token_ok

if TYPE_CHECKING:
    from petal_locate.server.runtime import ConsoleSubscription, ServerRuntime

__all__ = [
    "CLOSE_GOING_AWAY",
    "CLOSE_NOT_READING",
    "CLOSE_POLICY",
    "CLOSE_TRY_AGAIN",
    "SEND_TIMEOUT_S",
    "UiSockets",
    "ui_router",
]

logger = logging.getLogger(__name__)

SEND_TIMEOUT_S: float = 30.0
"""Longest a single frame may take to leave; a browser slower than that is closed."""

CLOSE_TIMEOUT_S: Final[float] = 3.0
"""Longest the server waits for a close handshake to be written."""

CLOSE_GOING_AWAY: Final[int] = 1001
CLOSE_POLICY: Final[int] = 1008
CLOSE_NOT_READING: Final[int] = 1013
CLOSE_TRY_AGAIN: Final[int] = 1013

_MAX_BAD_MESSAGES_LOGGED: Final[int] = 3

ui_router = APIRouter()
"""The ``/ws/ui`` route."""


class UiSockets:
    """The console sockets of one application (``app.state.ui_sockets``), so that a
    shutdown can close them with 1001 before the server goes away."""

    def __init__(self) -> None:
        self._subscriptions: set[ConsoleSubscription] = set()
        self._handlers: set[asyncio.Task[Any]] = set()
        self._closing = False
        self.accepted = 0
        self.refused = 0

    @property
    def closing(self) -> bool:
        """Whether ``close_all`` has been called (new sockets are refused with 1001)."""
        return self._closing

    @property
    def open(self) -> int:
        """Console sockets being served."""
        return len(self._handlers)

    @property
    def subscriptions(self) -> frozenset[ConsoleSubscription]:
        """The mailboxes of the open console sockets (read-only view)."""
        return frozenset(self._subscriptions)

    def _add(self, subscription: ConsoleSubscription) -> None:
        self._subscriptions.add(subscription)
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)

    def _discard(self, subscription: ConsoleSubscription) -> None:
        self._subscriptions.discard(subscription)
        task = asyncio.current_task()
        if task is not None:
            self._handlers.discard(task)

    async def close_all(self, timeout_s: float = 5.0) -> None:
        """Close every console socket with 1001 and wait (at most ``timeout_s``) for
        their handlers to finish. Idempotent."""
        self._closing = True
        for subscription in list(self._subscriptions):
            subscription.close()
        handlers = [t for t in self._handlers if not t.done()]
        if handlers:
            _done, pending = await asyncio.wait(handlers, timeout=timeout_s)
            if pending:
                logger.warning("%d console socket(s) did not close within %.1f s", len(pending), timeout_s)


def _registry(websocket: WebSocket) -> UiSockets:
    registry: UiSockets | None = getattr(websocket.app.state, "ui_sockets", None)
    if registry is None:
        registry = UiSockets()
        websocket.app.state.ui_sockets = registry
    return registry


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    """Close the socket unless it is already closed; never waits long or raises."""
    if websocket.application_state == WebSocketState.DISCONNECTED or websocket.client_state == WebSocketState.DISCONNECTED:
        return
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), timeout=CLOSE_TIMEOUT_S)
    except (TimeoutError, RuntimeError, OSError, WebSocketDisconnect) as exc:
        logger.debug("console socket close (%d) did not complete: %s", code, exc)


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Accept the handshake only to close at once with ``code`` (so the browser learns
    why, which an HTTP 403 would not tell it)."""
    try:
        await websocket.accept()
    except (RuntimeError, OSError, WebSocketDisconnect):
        return
    await _close(websocket, code, reason)


class _Closed(Exception):
    """The socket must be closed with ``code`` and ``reason``."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def _send_loop(websocket: WebSocket, subscription: ConsoleSubscription) -> None:
    """Drain the mailbox into the socket until the mailbox closes (shutdown) or a frame
    takes longer than ``SEND_TIMEOUT_S`` to leave."""
    from petal_locate.server.runtime import ConsoleClosed

    while True:
        try:
            item = await subscription.get()
        except ConsoleClosed:
            raise _Closed(CLOSE_GOING_AWAY, "server shutting down") from None
        try:
            if isinstance(item, bytes):
                await asyncio.wait_for(websocket.send_bytes(item), timeout=SEND_TIMEOUT_S)
            else:
                await asyncio.wait_for(websocket.send_text(item), timeout=SEND_TIMEOUT_S)
        except TimeoutError:
            raise _Closed(CLOSE_NOT_READING, "console not reading") from None


async def _receive_loop(websocket: WebSocket, subscription: ConsoleSubscription, peer: str) -> None:
    """Read the browser's messages until it disconnects. ``subscribe`` replaces the
    streamed windows; anything else is ignored (logged)."""
    bad = 0
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is None:
            continue  # binary frames from a browser carry nothing we understand
        try:
            data = json.loads(text)
            kind = data.get("type") if isinstance(data, dict) else None
            if kind != "subscribe":
                if bad < _MAX_BAD_MESSAGES_LOGGED:
                    logger.info("console message of type %r ignored", kind, extra={"peer": peer})
                bad += 1
                continue
            request = schemas.WsSubscribe.model_validate(data)
            subscription.set_psd(request.psd)
            logger.debug("console %s streams %d spectrum window(s)", peer, len(request.psd), extra={"peer": peer})
        except (ValueError, ValidationError) as exc:
            if bad < _MAX_BAD_MESSAGES_LOGGED:
                logger.warning("malformed console message ignored: %s", str(exc).splitlines()[0][:200],
                               extra={"peer": peer})
            bad += 1


@ui_router.websocket("/ws/ui")
async def ui_socket(websocket: WebSocket) -> None:
    """One console connection, from handshake to close."""
    registry = _registry(websocket)
    peer = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    if registry.closing:
        await _refuse(websocket, CLOSE_GOING_AWAY, "server shutting down")
        return
    if not ws_token_ok(websocket):
        registry.refused += 1
        logger.info("console socket refused: %s", "no API token" if "token" not in websocket.query_params
                    else "wrong API token", extra={"peer": peer})
        await _refuse(websocket, CLOSE_POLICY, "a valid API token is required (?token=)")
        return
    runtime: ServerRuntime | None = getattr(websocket.app.state, "runtime", None)
    if runtime is None:
        await _refuse(websocket, CLOSE_TRY_AGAIN, "the server is starting; try again shortly")
        return
    await websocket.accept()
    subscription = runtime.console.subscribe()
    registry._add(subscription)
    registry.accepted += 1
    logger.info("console connected", extra={"peer": peer, "consoles": registry.open})
    sender = asyncio.create_task(_send_loop(websocket, subscription), name=f"petal-ui-send {peer}")
    receiver = asyncio.create_task(_receive_loop(websocket, subscription, peer), name=f"petal-ui-recv {peer}")
    code, reason = 1000, ""
    try:
        done, _pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if isinstance(exc, _Closed):
                code, reason = exc.code, exc.reason
            elif isinstance(exc, (WebSocketDisconnect, OSError, RuntimeError)):
                code, reason = 1000, ""  # the browser went away
            elif exc is not None:
                logger.error("console socket failed: %r", exc, exc_info=exc, extra={"peer": peer})
                code, reason = 1011, "internal error"
    finally:
        for task in (sender, receiver):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(sender, receiver, return_exceptions=True)
        subscription.close()
        await _close(websocket, code, reason)
        registry._discard(subscription)
        logger.info(
            "console disconnected (%d%s)",
            code,
            f": {reason}" if reason else "",
            extra={"peer": peer, "delivered": subscription.delivered, "dropped": subscription.dropped},
        )
