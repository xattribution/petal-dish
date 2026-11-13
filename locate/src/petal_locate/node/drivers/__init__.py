"""Radio drivers of the node agent and their registry.

    driver = get_driver("noise", lookback_s=10.0)

`get_driver(name, **options)` builds an unopened driver; the agent then calls
`open(channel, ctx)` on it. Driver modules are imported only when asked for, so that
a node does not pay for `scipy` or need `SoapySDR` unless its driver does.

| name    | module              | what it is                                              |
|---------|---------------------|---------------------------------------------------------|
| `noise` | `drivers.noise`     | unit-variance noise; protocol tests, bring-up           |
| `sim`   | `drivers.sim`       | the physics simulator behind the simulation backplane   |
| `sigmf` | `drivers.sigmf`     | replay of a SigMF recording with its timestamps         |
| `soapy` | `drivers.soapy`     | SoapySDR hardware (untested on hardware)                |

The interface every driver implements, and the helpers they share, are in
`drivers.base` and re-exported here.
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any, Final, cast

from petal_locate.node.drivers.base import (
    AssignHook,
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    SdrDriver,
    SimUpdateHook,
    WorkerPool,
)

__all__ = [
    "DRIVER_NAMES",
    "AssignHook",
    "Capabilities",
    "Capture",
    "CaptureError",
    "DriverContext",
    "SdrDriver",
    "SimUpdateHook",
    "WorkerPool",
    "driver_names",
    "get_driver",
    "register_driver",
]

logger = logging.getLogger(__name__)

_BUILTIN: Final[dict[str, tuple[str, str]]] = {
    "noise": ("petal_locate.node.drivers.noise", "NoiseDriver"),
    # Slot for the simulation driver, which is a separate piece of work. It is imported
    # on first use and must expose a class (or factory) named `SimDriver` whose
    # keyword arguments are the driver options.
    "sim": ("petal_locate.node.drivers.sim", "SimDriver"),
    "sigmf": ("petal_locate.node.drivers.sigmf", "SigMFDriver"),
    "soapy": ("petal_locate.node.drivers.soapy", "SoapyDriver"),
}
"""Driver name -> (module, attribute). Nothing here is imported until asked for."""

DRIVER_NAMES: Final[tuple[str, ...]] = tuple(_BUILTIN)
"""The built-in driver names, in the order of ARCHITECTURE.md section 6."""

_registered: dict[str, Callable[..., SdrDriver]] = {}


def driver_names() -> tuple[str, ...]:
    """Every name `get_driver` accepts: the built-in ones, then registered ones."""
    return DRIVER_NAMES + tuple(name for name in _registered if name not in _BUILTIN)


def register_driver(name: str, factory: Callable[..., SdrDriver]) -> None:
    """Make `get_driver(name, **options)` call `factory(**options)`.

    For drivers that live outside this package (site-specific hardware, test doubles).
    name: a new name, or a built-in one to replace it for this process.
    Raises `ValueError` for an empty name or a factory that is not callable.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("name: must be a non-empty string")
    if not callable(factory):
        raise ValueError(f"factory: must be callable, got {type(factory).__name__}")
    _registered[name] = factory


def get_driver(name: str, **options: Any) -> SdrDriver:
    """Build the driver called `name` with its options; it is not opened yet.

    name: one of `driver_names()`.
    options: keyword arguments of the driver's constructor (see its class docstring).
    Raises `ValueError` for an unknown name or an option the driver does not take or
    refuses, and `CaptureError("driver")` when the driver's module cannot be loaded
    (for example `sim` before the simulator is installed).
    """
    factory = _registered.get(name) if isinstance(name, str) else None
    if factory is None:
        entry = _BUILTIN.get(name) if isinstance(name, str) else None
        if entry is None:
            raise ValueError(
                f"name: unknown driver {name!r}; choose one of {', '.join(driver_names())}"
            )
        module_name, attribute = entry
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            logger.error(
                "driver module failed to import",
                extra={"driver": name, "module_name": module_name},
                exc_info=True,
            )
            raise CaptureError(
                "driver", f"driver {name!r} is not available: cannot import {module_name} ({exc})"
            ) from exc
        found = getattr(module, attribute, None)
        if not callable(found):
            raise CaptureError(
                "driver", f"driver {name!r} is not available: {module_name} has no {attribute}"
            )
        factory = cast("Callable[..., SdrDriver]", found)
    try:
        return factory(**options)
    except TypeError as exc:
        raise ValueError(f"options: driver {name!r} refused its options ({exc})") from exc
