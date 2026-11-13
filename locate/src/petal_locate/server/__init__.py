"""The central server of PETAL Locate (ARCHITECTURE.md section 7).

- `settings`: `Settings`, node and API tokens, the marking banner.
- `hub`: `Hub`, the node sessions (PROTOCOL.md section A), and `CaptureFailed`.
- `app`: `create_app(settings)` and `run(settings)`.

The names below are loaded on first use, so importing one submodule (the hub from the
pipeline, say) never drags in the whole application:

    from petal_locate.server import Hub, Settings, create_app
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from petal_locate.server.app import HubProviders, build_server, create_app, run
    from petal_locate.server.hub import CaptureFailed, Hub, PsdFrame, StatusEvent, Subscription
    from petal_locate.server.settings import Marking, Settings

__all__ = [
    "CaptureFailed",
    "Hub",
    "HubProviders",
    "Marking",
    "PsdFrame",
    "Settings",
    "StatusEvent",
    "Subscription",
    "build_server",
    "create_app",
    "run",
]

_HOME: dict[str, str] = {
    "CaptureFailed": "hub",
    "Hub": "hub",
    "PsdFrame": "hub",
    "StatusEvent": "hub",
    "Subscription": "hub",
    "HubProviders": "app",
    "build_server": "app",
    "create_app": "app",
    "run": "app",
    "Marking": "settings",
    "Settings": "settings",
}


def __getattr__(name: str) -> Any:
    """Import a public name from its submodule on first use (PEP 562)."""
    module = _HOME.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
