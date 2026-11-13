"""The server application: FastAPI app, its lifespan, and how uvicorn must run it.

`create_app(settings)` builds the app. Today it carries the node interface only:

- `app.state.settings`: the `Settings`.
- `app.state.hub`: the `Hub` (node sessions); the lifespan starts and closes it.
- `/ws/node`: the node WebSocket of PROTOCOL.md section A, served by `Hub.handle`.
- `GET /healthz`: liveness probe, no authentication, nothing sensitive.

Extending it (pipeline, REST API under `/api/v1`, `/ws/ui`, static files): add an
`APIRouter` per concern and include it in `create_app` next to the two below; start
and stop long-lived parts inside `_lifespan`, after `hub.start()` and before
`hub.close()`. Nothing else here needs to change.

Running it: use `run(settings)`, or `build_server(app, settings)` when the caller owns
the event loop. Both apply what PROTOCOL.md requires of uvicorn (no
permessage-deflate, 1 MiB messages) and close the hub *before* uvicorn closes the
sockets, so that nodes see close code 1001 on shutdown rather than uvicorn's 1012.
"""
from __future__ import annotations

import logging
import socket
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import APIRouter, FastAPI, Request, WebSocket

from petal_locate import __version__
from petal_locate.protocol.messages import WS_MAX_SIZE_BYTES, ChannelAssignment
from petal_locate.server.hub import Hub
from petal_locate.server.settings import Settings

__all__ = [
    "GRACEFUL_SHUTDOWN_S",
    "HubProviders",
    "HubServer",
    "InboundTextTraceFilter",
    "build_server",
    "create_app",
    "health_router",
    "node_router",
    "run",
    "uvicorn_config",
]

logger = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN_S: int = 10
"""Longest uvicorn waits for open connections at shutdown before cancelling them, s."""


@dataclass(frozen=True)
class HubProviders:
    """What the hub asks the application (see `Hub.__init__`).

    assignments_for: site_id -> the channels its node must open, None for an unknown
        site.
    config_version: () -> version of the configuration those channels come from.
    simulation_info: () -> the `simulation` object of `welcome`.
    """

    assignments_for: Callable[[str], list[ChannelAssignment] | None]
    config_version: Callable[[], int]
    simulation_info: Callable[[], Mapping[str, Any]]

    @classmethod
    def unconfigured(cls) -> HubProviders:
        """Providers of a server with no configuration: no site is known (every
        `hello` is rejected), version 0, no simulation."""
        return cls(
            assignments_for=lambda site_id: None,
            config_version=lambda: 0,
            simulation_info=lambda: {"enabled": False},
        )


node_router = APIRouter()
"""The node interface (PROTOCOL.md section A)."""

health_router = APIRouter()
"""Probes that need no authentication."""


@node_router.websocket("/ws/node")
async def node_socket(websocket: WebSocket) -> None:
    """One node session, from `hello` to close; everything happens in `Hub.handle`."""
    hub: Hub = websocket.app.state.hub
    await hub.handle(websocket)


@health_router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    """Liveness probe.

    Returns `{status: "ok" | "stopping", version, nodes_online}`: `status` is
    "stopping" once the hub has been closed, `version` the package version and
    `nodes_online` the number of sites with a node online.
    """
    hub: Hub = request.app.state.hub
    return {
        "status": "stopping" if hub.closed else "ok",
        "version": __version__,
        "nodes_online": len(hub.online_sites()),
    }


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop what lives as long as the application: today, the hub."""
    hub: Hub = app.state.hub
    hub.start()
    logger.info("server started", extra={"version": __version__})
    try:
        # Later parts (store, pipeline) start here; they subscribe to the hub.
        yield
    finally:
        # Later parts stop here, before the hub, so that their captures end first.
        await hub.close()
        logger.info("server stopped")


def create_app(settings: Settings, *, hub_providers: HubProviders | None = None) -> FastAPI:
    """Build the FastAPI application.

    settings: the deployment settings; kept as `app.state.settings`.
    hub_providers: what the hub asks the application. None gives a server that knows
        no site (`HubProviders.unconfigured`), which is only useful until the
        configuration is wired in.
    Returns the app with `app.state.hub` already created (so that other parts can
    subscribe before startup); the lifespan starts it and closes it.
    """
    providers = hub_providers if hub_providers is not None else HubProviders.unconfigured()
    app = FastAPI(title="PETAL Locate", version=__version__, lifespan=_lifespan)
    app.state.settings = settings
    app.state.hub = Hub(
        settings,
        assignments_for=providers.assignments_for,
        config_version=providers.config_version,
        simulation_info=providers.simulation_info,
    )
    app.include_router(node_router)
    app.include_router(health_router)
    return app


class HubServer(uvicorn.Server):
    """A uvicorn server that closes the hub before uvicorn closes the node sockets.

    uvicorn's own shutdown closes every WebSocket with code 1012 and runs the
    lifespan shutdown only afterwards, too late for the hub to say 1001 as
    PROTOCOL.md specifies. Closing the hub first does; the lifespan then closes it a
    second time, which is harmless.
    """

    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        super().__init__(config)
        self._app = app

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Close the hub (1001 to every node), then shut uvicorn down as usual."""
        hub: Hub | None = getattr(self._app.state, "hub", None)
        try:
            if hub is not None:
                await hub.close()
        finally:
            await super().shutdown(sockets=sockets)


class InboundTextTraceFilter(logging.Filter):
    """Keeps node tokens out of the ASGI server's debug log.

    With debug logging on, the websockets library behind uvicorn traces every frame
    it receives on the `uvicorn.error` logger and quotes part of its text; the `hello`
    of a node carries the site's bearer token. This filter drops the traces of
    inbound text frames (`< TEXT ...`, and `< CONT ...` for a fragmented one). Nothing
    is lost for diagnosis: the hub logs every message that matters, without values.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.DEBUG:
            return True
        try:
            text = record.getMessage()
        except (TypeError, ValueError):  # a record whose arguments do not fit its format
            return True
        return not text.startswith(("< TEXT", "< CONT"))


_TRACE_FILTER = InboundTextTraceFilter()


def uvicorn_config(app: FastAPI, settings: Settings, **overrides: Any) -> uvicorn.Config:
    """The uvicorn configuration PROTOCOL.md requires for this app.

    app: the application from `create_app`.
    settings: `host` and `port` to bind (port 0 lets the operating system choose).
    overrides: further `uvicorn.Config` arguments (for example `log_config=None`).
    permessage-deflate is off (IQ does not compress and deflate costs the event loop
    139 ms per 4 MiB) and the message size limit is `WS_MAX_SIZE_BYTES` (1 MiB), which
    every frame of the protocol fits. Raises `ValueError` when an override turns
    deflate on or lowers the limit below 1 MiB.
    """
    options: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "ws_per_message_deflate": False,
        "ws_max_size": WS_MAX_SIZE_BYTES,
        "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_S,
    }
    options.update(overrides)
    if options["ws_per_message_deflate"]:
        raise ValueError("ws_per_message_deflate: must stay off (PROTOCOL.md section A)")
    if options["ws_max_size"] < WS_MAX_SIZE_BYTES:
        raise ValueError(
            f"ws_max_size: must be at least {WS_MAX_SIZE_BYTES} bytes, got {options['ws_max_size']}"
        )
    return uvicorn.Config(app, **options)


def build_server(app: FastAPI, settings: Settings, **overrides: Any) -> HubServer:
    """A server for `app`, ready for `await server.serve()` or `server.run()`.

    Arguments as for `uvicorn_config`. Set `server.should_exit = True` to stop it.
    Also installs `InboundTextTraceFilter` on the `uvicorn.error` logger, so that
    debug logging cannot write a node token to the log.
    """
    config = uvicorn_config(app, settings, **overrides)
    # After the Config: building it applies uvicorn's logging configuration.
    logging.getLogger("uvicorn.error").addFilter(_TRACE_FILTER)
    return HubServer(config, app)


def run(settings: Settings, *, hub_providers: HubProviders | None = None) -> None:
    """Create the application and serve it until interrupted (blocks).

    settings, hub_providers: as for `create_app`.
    """
    build_server(create_app(settings, hub_providers=hub_providers), settings).run()
