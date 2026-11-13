"""The console interface of the server (PROTOCOL.md section B).

- ``schemas``: pydantic models of every REST response and WebSocket message (the
  contract the browser console is generated from).
- ``rest``: the routes under ``/api/v1`` and the node simulation backplane route.
- ``ws``: the console WebSocket ``/ws/ui``.
- ``deps``: authentication (API token, node token) and access to the runtime.

Nothing is imported here: ``server.runtime`` imports ``schemas`` while ``rest`` imports
the runtime, so importing ``rest`` from this package's ``__init__`` would be circular.
Import the submodules directly; ``server.app.create_app`` wires them together.
"""
from __future__ import annotations

__all__ = ["deps", "rest", "schemas", "ws"]
