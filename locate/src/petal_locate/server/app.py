"""The server application: FastAPI app, its lifespan, and how uvicorn must run it.

`create_app(settings)` builds the whole server (ARCHITECTURE.md section 7):

- `app.state.settings`: the `Settings`.
- `app.state.hub`: the `Hub` (node sessions); the lifespan starts and closes it.
- `app.state.runtime`: the `ServerRuntime` (configuration, scenario, store, event
  pipeline), built by the lifespan from the settings on the serving event loop.
- `/ws/node`: the node WebSocket of PROTOCOL.md section A, served by `Hub.handle`.
- `/api/v1/...` and `/ws/ui`: the console interface of PROTOCOL.md section B
  (`api/rest.py`, `api/ws.py`); OpenAPI at `/openapi.json`, browsable at `/docs`.
- The simulation backplane at the path the runtime advertises in `welcome`
  (`runtime.SIM_BACKPLANE_PATH`), for simulated nodes.
- `GET /healthz`: liveness probe, no authentication, nothing sensitive.
- `/`: the operator console built into `server/static/` (index.html for client-side
  routes, long cache lifetimes for hashed assets, no-cache for index.html), or a
  placeholder page saying how to build it.

`create_app(settings, hub_providers=...)` instead builds an application whose caller
owns the configuration: the lifespan only runs the hub, and the console routes answer
503 until the caller sets `app.state.runtime` (tests of the connection layer do this).

Running it: use `run(settings)`, or `build_server(app, settings)` when the caller owns
the event loop. Both apply what PROTOCOL.md requires of uvicorn (no
permessage-deflate, 1 MiB messages), close the console sockets and the hub *before*
uvicorn closes the sockets (so nodes see close code 1001 on shutdown rather than
uvicorn's 1012), and keep bearer tokens out of uvicorn's log.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import re
import socket
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import uvicorn
from fastapi import APIRouter, FastAPI, Request, Response, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from petal_locate import __version__
from petal_locate.protocol.messages import WS_MAX_SIZE_BYTES, ChannelAssignment
from petal_locate.server.hub import Hub
from petal_locate.server.settings import Settings

__all__ = [
    "DEFAULT_STATIC_DIR",
    "GRACEFUL_SHUTDOWN_S",
    "HubProviders",
    "HubServer",
    "InboundTextTraceFilter",
    "QueryTokenFilter",
    "build_server",
    "create_app",
    "health_router",
    "node_router",
    "placeholder_page",
    "run",
    "static_router",
    "uvicorn_config",
]

logger = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN_S: int = 10
"""Longest uvicorn waits for open connections at shutdown before cancelling them, s."""

DEFAULT_STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"
"""Where the console build lives (``ui/`` builds into it)."""

IMMUTABLE_CACHE: Final[str] = "public, max-age=31536000, immutable"
"""Cache-Control of content-hashed assets (their name changes with their content)."""

NO_CACHE: Final[str] = "no-cache"
"""Cache-Control of index.html and the placeholder: always revalidated."""

SHORT_CACHE: Final[str] = "public, max-age=3600"
"""Cache-Control of other static files (unhashed names)."""

_HASHED_ASSET: Final[re.Pattern[str]] = re.compile(r"[-.][A-Za-z0-9_]{8,}\.[A-Za-z0-9]{1,8}$")
"""A file name carrying a content hash, as Vite writes them (``index-B2x9_kQe.js``)."""

_RESERVED_PREFIXES: Final[tuple[str, ...]] = ("api", "ws", "node")
"""First path segments that never fall back to the console page."""


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

static_router = APIRouter()
"""The operator console's files (added last: it answers every other GET path)."""


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


# --------------------------------------------------------------------------- static console


def placeholder_page() -> str:
    """The page served at ``/`` when no console build is present."""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>PETAL Locate</title>
<style>
:root { --paper: #f4f5f2; --ink: #1d2124; --ink-2: #4a5157; --rule: #c9cdc8; --code: #e6e8e3; }
@media (prefers-color-scheme: dark) {
  :root { --paper: #15191c; --ink: #e3e6e1; --ink-2: #a4aba6; --rule: #343b40; --code: #22282c; }
}
body { margin: 0; background: var(--paper); color: var(--ink);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 44rem; margin: 0 auto; padding: 3rem 1rem; }
h1 { font-size: 1.25rem; margin: 0 0 1rem; }
p { color: var(--ink-2); }
pre, code { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; background: var(--code); }
pre { padding: .75rem 1rem; border: 1px solid var(--rule); overflow-x: auto; }
code { padding: 0 .25rem; }
a { color: inherit; }
</style>
</head>
<body>
<main>
<h1>PETAL Locate: the operator console has not been built</h1>
<p>The server is running, but this installation has no console build in
<code>src/petal_locate/server/static/</code>. Build it once (Node.js required) from the
<code>locate</code> directory:</p>
<pre>cd ui
npm ci
npm run build</pre>
<p>then reload this page. The build is written to <code>src/petal_locate/server/static/</code>
and served from here; nothing else needs to change.</p>
<p>Meanwhile the console API is available under <code>/api/v1</code>; its schema is at
<a href="openapi.json">/openapi.json</a> and can be browsed at <a href="docs">/docs</a>.</p>
</main>
</body>
</html>
"""


_INDEX_CACHE: dict[Path, tuple[int, int, bytes]] = {}


def _index_html(index: Path, base_href: str | None) -> bytes:
    """index.html, read once per modification, with ``<base href>`` inserted when the
    page is served for a nested client route (so relative asset paths still resolve)."""
    stat = index.stat()
    cached = _INDEX_CACHE.get(index)
    if cached is None or cached[:2] != (stat.st_mtime_ns, stat.st_size):
        cached = (stat.st_mtime_ns, stat.st_size, index.read_bytes())
        _INDEX_CACHE[index] = cached
    content = cached[2]
    if base_href is None or b"<base " in content.lower():
        return content
    tag = f'<base href="{base_href}">'.encode()
    match = re.search(rb"<head[^>]*>", content, flags=re.IGNORECASE)
    if match is None:
        return tag + content
    return content[: match.end()] + tag + content[match.end():]


def _serve_index(request: Request, static_dir: Path, nested: bool) -> Response:
    index = static_dir / "index.html"
    headers = {"Cache-Control": NO_CACHE}
    if not index.is_file():
        return HTMLResponse(placeholder_page(), headers=headers)
    root = request.scope.get("root_path", "") or ""
    try:
        content = _index_html(index, f"{root.rstrip('/')}/" if nested else None)
    except OSError as exc:
        logger.error("console index.html cannot be read: %s", exc)
        return HTMLResponse(placeholder_page(), headers=headers)
    return Response(content, media_type="text/html; charset=utf-8", headers=headers)


@static_router.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def static_file(request: Request, path: str) -> Response:
    """A file of the console build; index.html for client-side routes (paths without
    a file extension); the placeholder page when there is no build."""
    first = path.split("/", 1)[0]
    if first in _RESERVED_PREFIXES:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    static_dir: Path = getattr(request.app.state, "static_dir", DEFAULT_STATIC_DIR)
    if path in ("", "index.html"):
        return _serve_index(request, static_dir, nested=False)
    root = static_dir.resolve()
    candidate = (static_dir / path).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if candidate.is_file():
        name = candidate.name
        if name == "index.html":
            return _serve_index(request, static_dir, nested=False)
        hashed = "assets" in candidate.relative_to(root).parts[:-1] or bool(_HASHED_ASSET.search(name))
        return FileResponse(candidate, headers={"Cache-Control": IMMUTABLE_CACHE if hashed else SHORT_CACHE})
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if "." in last:
        return JSONResponse({"detail": "Not Found"}, status_code=404)  # a missing file, not a route
    return _serve_index(request, static_dir, nested="/" in path.strip("/"))


# --------------------------------------------------------------------------- lifespan and app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the hub, then (when the app owns it) build and start the runtime; stop
    them in the reverse order: console sockets, runtime, hub."""
    hub: Hub = app.state.hub
    hub.start()
    owned = None
    frozen = False
    try:
        if app.state.manage_runtime:
            from petal_locate.server.runtime import ServerRuntime

            owned = await ServerRuntime.from_settings(app.state.settings, presets_dir=app.state.presets_dir)
            try:
                await owned.start(hub)
            except BaseException:
                await owned.stop()
                raise
            app.state.runtime = owned
            app.state.runtime_holder.runtime = owned
            try:  # build the OpenAPI document now, off the loop (it takes about 0.4 s)
                await asyncio.to_thread(app.openapi)
            except Exception:  # it is then built on first request instead
                logger.exception("the OpenAPI document could not be prepared")
            frozen = _freeze_heap()
        logger.info("server started", extra={"version": __version__, "runtime": owned is not None})
        yield
    finally:
        ui = getattr(app.state, "ui_sockets", None)
        if ui is not None:
            await ui.close_all()
        if owned is not None:
            app.state.runtime = None
            app.state.runtime_holder.runtime = None
            await owned.stop()
            if frozen:
                gc.unfreeze()
        await hub.close()
        logger.info("server stopped")


def _freeze_heap() -> bool:
    """Exempt everything allocated so far (modules, configuration, models, the OpenAPI
    document) from Python's full garbage collections.

    A full (generation 2) collection walks every tracked object while holding the GIL,
    whichever thread triggers it; with the server's heap that measured 80 to 150 ms,
    which stalls the event loop well past its 50 ms budget. Frozen objects are skipped
    by collections; ``gc.unfreeze()`` at shutdown hands them back. Returns True when
    the heap was frozen."""
    gc.collect()
    gc.freeze()
    logger.info("startup heap frozen for the garbage collector", extra={"objects": gc.get_freeze_count()})
    return True


def create_app(
    settings: Settings,
    *,
    hub_providers: HubProviders | None = None,
    presets_dir: Path | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    settings: the deployment settings; kept as `app.state.settings`.
    hub_providers: None (the normal case) builds the whole server: the lifespan loads
        the configuration (and scenario) of `settings` into a `ServerRuntime`, whose
        providers the hub uses. Given, the caller owns the configuration: the hub uses
        these providers, no runtime is built, and the console routes answer 503 until
        the caller sets `app.state.runtime`.
    presets_dir: directory of preset scenarios (default: `config/scenarios` of a source
        checkout).
    static_dir: directory of the console build (default `server/static`).
    Returns the app with `app.state.hub` already created (so that other parts can
    subscribe before startup); the lifespan starts it and closes it.
    """
    from petal_locate.server.api.rest import (
        ApiState,
        api_router,
        backplane_router,
        install_error_handlers,
        public_router,
    )
    from petal_locate.server.api.ws import UiSockets, ui_router
    from petal_locate.server.runtime import RuntimeHolder

    manage_runtime = hub_providers is None
    holder = RuntimeHolder()
    providers = holder.hub_providers() if manage_runtime else hub_providers
    assert providers is not None
    app = FastAPI(
        title="PETAL Locate",
        version=__version__,
        lifespan=_lifespan,
        description="Console interface of PETAL Locate (PROTOCOL.md section B). Every `*_ns` field is a "
        "decimal string of integer nanoseconds since the Unix epoch.",
    )
    app.state.settings = settings
    app.state.manage_runtime = manage_runtime
    app.state.runtime_holder = holder
    app.state.runtime = None
    app.state.presets_dir = presets_dir
    app.state.static_dir = Path(static_dir) if static_dir is not None else DEFAULT_STATIC_DIR
    app.state.api = ApiState()
    app.state.ui_sockets = UiSockets()
    app.state.hub = Hub(
        settings,
        assignments_for=providers.assignments_for,
        config_version=providers.config_version,
        simulation_info=providers.simulation_info,
    )
    install_error_handlers(app)
    app.include_router(node_router)
    app.include_router(health_router)
    app.include_router(public_router)
    app.include_router(api_router)
    app.include_router(ui_router)
    app.include_router(backplane_router)
    app.include_router(static_router)  # last: it answers every other GET path
    return app


class HubServer(uvicorn.Server):
    """A uvicorn server that closes the console sockets and the hub before uvicorn
    closes the sockets.

    uvicorn's own shutdown closes every WebSocket with code 1012 and runs the
    lifespan shutdown only afterwards, too late for the hub to say 1001 as
    PROTOCOL.md specifies. Closing them first does; the lifespan then closes them a
    second time, which is harmless.
    """

    def __init__(self, config: uvicorn.Config, app: FastAPI, *, capture_signals: bool = True) -> None:
        super().__init__(config)
        self._app = app
        self._capture_signals = capture_signals

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """uvicorn's SIGINT / SIGTERM handling, unless the server was built with
        ``capture_signals=False`` (a caller that handles signals itself, such as
        ``petal-locate demo``, then stops it with ``should_exit``)."""
        if not self._capture_signals:
            yield
            return
        with super().capture_signals():
            yield

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Close the console sockets and the hub (1001), then shut uvicorn down."""
        hub: Hub | None = getattr(self._app.state, "hub", None)
        ui = getattr(self._app.state, "ui_sockets", None)
        try:
            if ui is not None:
                await ui.close_all()
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


_TOKEN_IN_QUERY: Final[re.Pattern[str]] = re.compile(r"(?i)([?&]token=)[^&\s\"']*")


class QueryTokenFilter(logging.Filter):
    """Keeps the console's API token out of uvicorn's log.

    The console's WebSocket handshake carries the API token as ``?token=`` (PROTOCOL.md
    section B), and uvicorn logs the path with its query string (``"WebSocket
    /ws/ui?token=..." [accepted]``, and the request line in debug traces). This filter
    rewrites such records with the value replaced by ``[redacted]``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except (TypeError, ValueError):
            return True
        if "token=" not in text.lower():
            return True
        record.msg = _TOKEN_IN_QUERY.sub(r"\1[redacted]", text)
        record.args = ()
        return True


_TRACE_FILTER = InboundTextTraceFilter()
_QUERY_TOKEN_FILTER = QueryTokenFilter()


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


def build_server(app: FastAPI, settings: Settings, *, capture_signals: bool = True, **overrides: Any) -> HubServer:
    """A server for `app`, ready for `await server.serve()` or `server.run()`.

    Arguments as for `uvicorn_config`. Set `server.should_exit = True` to stop it.
    capture_signals: False leaves SIGINT / SIGTERM to the caller.
    Also installs `InboundTextTraceFilter` and `QueryTokenFilter` on uvicorn's loggers,
    so that neither debug logging nor the console handshake can write a token to the
    log.
    """
    config = uvicorn_config(app, settings, **overrides)
    # After the Config: building it applies uvicorn's logging configuration.
    error_log = logging.getLogger("uvicorn.error")
    error_log.addFilter(_TRACE_FILTER)
    error_log.addFilter(_QUERY_TOKEN_FILTER)
    logging.getLogger("uvicorn.access").addFilter(_QUERY_TOKEN_FILTER)
    return HubServer(config, app, capture_signals=capture_signals)


def run(
    settings: Settings,
    *,
    hub_providers: HubProviders | None = None,
    presets_dir: Path | None = None,
) -> None:
    """Create the application and serve it until interrupted (blocks).

    settings, hub_providers, presets_dir: as for `create_app`.
    """
    app = create_app(settings, hub_providers=hub_providers, presets_dir=presets_dir)
    build_server(app, settings).run()
