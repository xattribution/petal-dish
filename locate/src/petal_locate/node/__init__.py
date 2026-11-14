"""The node agent: one process per ground site, next to its software-defined radio.

    from petal_locate.node import NodeAgent

    agent = NodeAgent("wss://server.example", site_id="honolulu", token=token,
                      driver="noise", state_path="/var/lib/petal/node.json")
    await agent.run()          # until agent.stop() / agent.request_stop()

Modules: `agent` (connection, sessions, captures, reconnect), `monitor` (spectrum
windows), `clock` (labelled time and its quality), `drivers` (the radio interface, the
registry `get_driver(name, **options)` and the `noise`, `sim`, `sigmf` and `soapy`
drivers).
"""
from __future__ import annotations

from petal_locate.node.agent import (
    DEFAULT_CHUNK_PAYLOAD_BYTES,
    REJECT_POLICY,
    AgentStats,
    DriverFactory,
    NodeAgent,
    NodeRejected,
    backoff_delay,
    default_state_path,
    load_or_create_node_id,
    normalize_server_url,
)
from petal_locate.node.clock import (
    ClockSource,
    DisciplinedClock,
    ManualClock,
    ReferenceReading,
    SystemClock,
)
from petal_locate.node.drivers import (
    Capabilities,
    Capture,
    CaptureError,
    DriverContext,
    SdrDriver,
    WorkerPool,
    driver_names,
    get_driver,
    register_driver,
)
from petal_locate.node.monitor import FrameMailbox, Monitor, ReadGate, SpectrumFrame

__all__ = [
    "DEFAULT_CHUNK_PAYLOAD_BYTES",
    "REJECT_POLICY",
    "AgentStats",
    "Capabilities",
    "Capture",
    "CaptureError",
    "ClockSource",
    "DisciplinedClock",
    "DriverContext",
    "DriverFactory",
    "FrameMailbox",
    "ManualClock",
    "Monitor",
    "NodeAgent",
    "NodeRejected",
    "ReadGate",
    "ReferenceReading",
    "SdrDriver",
    "SpectrumFrame",
    "SystemClock",
    "WorkerPool",
    "backoff_delay",
    "default_state_path",
    "driver_names",
    "get_driver",
    "load_or_create_node_id",
    "normalize_server_url",
    "register_driver",
]
