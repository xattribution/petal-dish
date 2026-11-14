"""Request dependencies of the console API: authentication and access to the runtime.

- ``require_api_token``: when ``Settings.api_token`` is set, every ``/api`` request must
  carry ``Authorization: Bearer <token>`` (compared in constant time); otherwise HTTP
  401 with ``WWW-Authenticate: Bearer``. ``GET /api/v1/meta`` does not use it, so a
  console can show the marking banner and ask for the token.
- ``ws_token_ok``: the same check for the ``/ws/ui`` handshake, whose token travels as
  ``?token=`` (browsers cannot set headers on a WebSocket).
- ``ws_origin_ok``: the ``/ws/ui`` handshake's ``Origin`` must be the server's own or
  an allowed one (``Settings.allowed_origins``), whether or not a token is set.
- ``get_runtime``: the ``ServerRuntime`` of the application, or HTTP 503 while the
  server is still starting (or was built without one).
- ``bearer_token``: the token of an ``Authorization`` header, used by the node
  backplane route too (which checks node tokens, not the API token).

Nothing here logs a token or puts one in an error message.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from fastapi import HTTPException, Request, WebSocket, status

from petal_locate.server.settings import Settings

if TYPE_CHECKING:
    from petal_locate.server.runtime import ServerRuntime

__all__ = [
    "AUTH_SCHEME",
    "bearer_token",
    "get_runtime",
    "get_settings",
    "require_api_token",
    "ws_origin_ok",
    "ws_token_ok",
]

logger = logging.getLogger(__name__)

AUTH_SCHEME: Final[str] = "Bearer"
"""The HTTP authentication scheme of the API and backplane tokens."""

_UNAUTHORIZED_HEADERS: Final[dict[str, str]] = {"WWW-Authenticate": AUTH_SCHEME}


def bearer_token(authorization: str | None) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header value.

    authorization: the header value, or None when absent.
    Returns the token, or None when the header is absent, uses another scheme or
    carries no token. The scheme name is matched without regard to case (RFC 9110).
    """
    if not authorization:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != AUTH_SCHEME.lower():
        return None
    token = token.strip()
    return token or None


def get_settings(request: Request) -> Settings:
    """The deployment settings of the application serving ``request``."""
    settings: Settings = request.app.state.settings
    return settings


async def require_api_token(request: Request) -> None:
    """Refuse the request with HTTP 401 unless it carries the API token (when one is
    configured). Used as a dependency of every ``/api`` route except ``/meta``."""
    settings = get_settings(request)
    if settings.api_token is None:
        return
    presented = bearer_token(request.headers.get("authorization"))
    if not settings.check_api_token(presented):
        logger.info(
            "API request refused: %s",
            "no API token" if presented is None else "wrong API token",
            extra={"path": request.url.path, "client": request.client.host if request.client else None},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="an API token is required: send Authorization: Bearer <token>",
            headers=_UNAUTHORIZED_HEADERS,
        )


def ws_token_ok(websocket: WebSocket) -> bool:
    """Whether a ``/ws/ui`` handshake may proceed: True when no API token is configured,
    otherwise only when ``?token=`` carries it (constant-time comparison)."""
    settings: Settings = websocket.app.state.settings
    if settings.api_token is None:
        return True
    return settings.check_api_token(websocket.query_params.get("token"))


def ws_origin_ok(websocket: WebSocket) -> bool:
    """Whether the ``Origin`` of a ``/ws/ui`` handshake may open the console socket.

    WebSockets are not covered by the browser's same-origin rules: any page could open
    one to this server and read the live stream. A handshake without ``Origin`` (not
    from a browser) passes; one with it passes when it is the server's own origin
    (``http(s)://`` plus the ``Host`` of the handshake) or among
    ``Settings.allowed_origins`` (``*`` for any)."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    origin = origin.strip().lower().rstrip("/")
    settings: Settings = websocket.app.state.settings
    if "*" in settings.allowed_origins or origin in settings.allowed_origins:
        return True
    host = websocket.headers.get("host", "").strip().lower()
    scheme = "https" if websocket.url.scheme in ("wss", "https") else "http"
    return bool(host) and origin == f"{scheme}://{host}"


def get_runtime(request: Request) -> ServerRuntime:
    """The application's runtime; HTTP 503 when there is none (yet)."""
    runtime: ServerRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the server is starting or has no configuration loaded; try again shortly",
            headers={"Retry-After": "2"},
        )
    return runtime
