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
  placeholder page saying how to build it. `/?token=<api token>` serves index.html
  with a script that hands the token to the console (`CONSOLE_TOKEN_KEY`).
- `RequestGuardMiddleware`, before any route: the `Host` header must name an allowed
  host (DNS-rebinding defense), a state-changing `/api` request must not be one a
  foreign page can send unasked (403), and `/api` bodies are capped (413).
- Time: `app.state.clock` is `Settings.clock()` (the host clock, plus the simulation
  offset of `petal-locate demo --start`); the runtime, the store and the hub use it.

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
import json
import logging
import re
import socket
import sys
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
    "CLIENT_HEADER",
    "CONSOLE_TOKEN_KEY",
    "DEFAULT_STATIC_DIR",
    "GRACEFUL_SHUTDOWN_S",
    "HubProviders",
    "HubServer",
    "InboundTextTraceFilter",
    "QueryTokenFilter",
    "RequestGuardMiddleware",
    "allowed_host_names",
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

SERVER_SWITCH_INTERVAL_S: Final[float] = 0.001
"""Python's thread switch interval while the full server runs (default 5 ms), s: how
long the event loop's thread may wait for the GIL behind a busy worker thread."""

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


CONSOLE_TOKEN_KEY: Final[str] = "petal.apiToken"
"""Where the console keeps the API token (``sessionStorage``); a console address that
carries the token (``/?token=<api token>``) is served with a script that stores it
there, so the address ``petal-locate demo`` prints opens an authenticated console."""


def _with_token(content: bytes, token: str) -> bytes:
    """index.html with a script, first in ``<head>``, that hands ``token`` to the console
    and takes it out of the address bar (so that it is not bookmarked or shared)."""
    literal = json.dumps(token).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    script = (
        f'<script>try{{sessionStorage.setItem("{CONSOLE_TOKEN_KEY}",{literal})}}catch(e){{}}'
        'try{history.replaceState(null,"",location.pathname+location.hash)}catch(e){}</script>'
    ).encode("utf-8")
    match = re.search(rb"<head[^>]*>", content, flags=re.IGNORECASE)
    if match is None:
        return script + content
    return content[: match.end()] + script + content[match.end():]


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
    presented = request.query_params.get("token")
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if presented is not None and settings is not None and settings.api_token is not None:
        # Only the server's own token is ever written into the page (never other input).
        if settings.check_api_token(presented):
            content = _with_token(content, settings.api_token)
            headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    return Response(content, media_type="text/html; charset=utf-8", headers=headers)


@static_router.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def static_file(request: Request, path: str) -> Response:
    """A file of the console build; index.html for client-side routes (paths without
    a file extension); the placeholder page when there is no build. A path the file
    system cannot even look up (a NUL character, one too long) is a 404."""
    first = path.split("/", 1)[0]
    if first in _RESERVED_PREFIXES or "\x00" in path:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    static_dir: Path = getattr(request.app.state, "static_dir", DEFAULT_STATIC_DIR)
    if path in ("", "index.html"):
        return _serve_index(request, static_dir, nested=False)
    try:
        root = static_dir.resolve()
        candidate = (static_dir / path).resolve()
        if candidate != root and not candidate.is_relative_to(root):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        is_file = candidate.is_file()
    except (ValueError, OSError):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if is_file:
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
    switch_interval = sys.getswitchinterval()
    try:
        if app.state.manage_runtime:
            # The worker threads run pure-Python stretches of the CAF and the solver while
            # holding the GIL; each time the event loop's thread gives the GIL up (inside
            # numpy, a socket call) it waits up to a whole switch interval to get it back.
            # At the default 5 ms a 1 ms spectrum update on the loop measured 120 ms
            # during a solve; 1 ms cuts such convoys fivefold.
            sys.setswitchinterval(SERVER_SWITCH_INTERVAL_S)
            from petal_locate.server.runtime import ServerRuntime
            from petal_locate.server.store import Store

            settings: Settings = app.state.settings
            store = Store(
                settings.data_dir,
                capture_limit_bytes=settings.capture_limit_bytes,
                history_max_age_s=settings.history_max_age_s,
                history_max_events=settings.history_max_events,
                clock=app.state.clock,
            )
            try:
                owned = await ServerRuntime.from_settings(
                    settings, presets_dir=app.state.presets_dir, store=store, clock=app.state.clock
                )
            except BaseException:
                await store.close()
                raise
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
        sys.setswitchinterval(switch_interval)
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


# --------------------------------------------------------------------------- request guard

CLIENT_HEADER: Final[str] = "x-petal-client"
"""Header a client sends on a state-changing request that carries no JSON or YAML body
(the console sends ``X-Petal-Client: console`` on every POST, PUT and DELETE)."""

_UNSAFE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SIMPLE_TYPES: Final[frozenset[str]] = frozenset(
    {"", "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"}
)
"""Content types a browser sends cross-site without asking the server first (a CORS
"simple request"): a request that carries one of them, no custom header and no
Authorization could come from any web page the operator's browser shows."""

_NODE_PATHS: Final[tuple[str, ...]] = ("/ws/node", "/healthz")
"""Paths outside the browser-facing surface: no Host check (nodes may dial any name)."""


def _host_name(value: str) -> str:
    """The host part of a Host header (port and IPv6 brackets removed), lower case."""
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def allowed_host_names(settings: Settings) -> frozenset[str] | None:
    """Host names the browser-facing routes answer (``Host`` header), or None for any.

    The bound address, ``localhost``, ``127.0.0.1``, ``::1`` and ``Settings.allowed_hosts``;
    when the server binds every interface (``0.0.0.0``, ``::``) also this machine's host
    name, fully qualified name and the addresses they resolve to. ``*`` among the
    allowed hosts disables the check."""
    from petal_locate.server.settings import LOCAL_HOST_NAMES

    extra = {_host_name(h) for h in settings.allowed_hosts}
    if "*" in extra:
        return None
    names = set(LOCAL_HOST_NAMES) | extra | {_host_name(settings.host)}
    if settings.host in ("0.0.0.0", "::", ""):
        try:
            hostname = socket.gethostname()
            names |= {hostname.lower(), socket.getfqdn().lower()}
            names |= {a.lower() for a in socket.gethostbyname_ex(hostname)[2]}
        except OSError:
            pass
    names.discard("")
    return frozenset(names)


class RequestGuardMiddleware:
    """Checks every request before routing (SRV-3, SRV-4 of the review):

    - ``Host``: a browser-facing request (anything but ``/ws/node``, ``/healthz`` and
      the node backplane) must name an allowed host (``allowed_host_names``), else
      HTTP 400 (or a refused WebSocket handshake). This stops DNS rebinding, where a
      page of another site re-points its own name at this server.
    - State-changing ``/api`` requests (POST, PUT, PATCH, DELETE) must carry a
      ``Content-Type`` a browser cannot send cross-site unasked (``application/json``,
      ``application/yaml``, ...), an ``Authorization`` header or the
      ``X-Petal-Client`` header; else HTTP 403. Cross-site requests that could carry
      them need the server's permission (CORS), which it never gives.
    - Every ``/api`` request body is limited to ``rest.MAX_BODY_BYTES`` (4 MiB): a
      larger ``Content-Length``, or more bytes streamed, is answered 413 before any
      route reads it.
    """

    def __init__(self, app: Any, *, allowed_hosts: frozenset[str] | None, exempt: tuple[str, ...]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.exempt = exempt

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", ())}
        if self.allowed_hosts is not None and not any(path == p or path.startswith(p + "/") for p in self.exempt):
            host = headers.get("host", "")
            if host and _host_name(host) not in self.allowed_hosts:
                logger.warning("request for host %r refused (not an allowed host name)", host[:100],
                               extra={"path": path[:200], "host": host[:100]})
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await _plain_error(send, 400, f"the host name {host[:100]!r} is not one this server answers to; "
                                              "start it with --allowed-host NAME to allow it")
                return
        if kind == "http" and (path == "/api" or path.startswith("/api/")):
            method = scope.get("method", "GET").upper()
            if method in _UNSAFE_METHODS:
                ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype in _SIMPLE_TYPES and "authorization" not in headers and CLIENT_HEADER not in headers:
                    await _plain_error(
                        send, 403,
                        "a state-changing request must carry Content-Type application/json or application/yaml, "
                        "an Authorization header, or the header X-Petal-Client (cross-site request defense)",
                    )
                    return
                buffered = await _buffer_body(scope, receive, headers)
                if buffered is None:
                    from petal_locate.server.api import rest

                    await _plain_error(send, 413, f"the request body is larger than {rest.MAX_BODY_BYTES} bytes")
                    return
                receive = buffered
        await self.app(scope, receive, send)


async def _buffer_body(scope: Any, receive: Any, headers: Mapping[str, str]) -> Any | None:
    """Read a request body of at most ``rest.MAX_BODY_BYTES`` and return a ``receive``
    that replays it; None when the body is larger (``Content-Length`` or bytes read)."""
    from petal_locate.server.api import rest

    limit = int(rest.MAX_BODY_BYTES)
    declared = headers.get("content-length")
    if declared is not None and declared.strip().isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            # the client went away: let the route see it
            pending: list[Mapping[str, Any]] = [message]

            async def gone() -> Mapping[str, Any]:
                return pending.pop() if pending else await receive()

            return gone
        body = message.get("body", b"")
        size += len(body)
        if size > limit:
            return None
        chunks.append(body)
        if not message.get("more_body", False):
            break
    replay: list[Mapping[str, Any]] = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

    async def replayed() -> Mapping[str, Any]:
        return replay.pop() if replay else await receive()

    return replayed


async def _plain_error(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


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

    from petal_locate.server.runtime import SIM_BACKPLANE_PATH

    manage_runtime = hub_providers is None
    holder = RuntimeHolder()
    providers = holder.hub_providers() if manage_runtime else hub_providers
    assert providers is not None
    clock = settings.clock()
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
    app.state.clock = clock
    app.state.hub = Hub(
        settings,
        assignments_for=providers.assignments_for,
        config_version=providers.config_version,
        simulation_info=providers.simulation_info,
        clock=clock,  # the server clock (host clock plus any simulation offset)
    )
    app.state.allowed_hosts = allowed_host_names(settings)
    install_error_handlers(app)
    # Added last, so it runs first: nothing is routed (or read) before the checks.
    app.add_middleware(
        RequestGuardMiddleware,
        allowed_hosts=app.state.allowed_hosts,
        exempt=_NODE_PATHS + (SIM_BACKPLANE_PATH,),
    )
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
