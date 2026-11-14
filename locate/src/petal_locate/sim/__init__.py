"""Simulation: scenarios (what is true) and the simulated world (truth plus IQ/PSD synthesis).

Everything produced here is simulated and must be labelled as such wherever it is shown.
"""
from __future__ import annotations

from petal_locate.sim.scenario import (
    EmitterSpec,
    EphemerisError,
    IonoError,
    Scenario,
    ScenarioError,
    TruthErrors,
    WaveformSpec,
    dump_scenario,
    load_scenario,
    loads_scenario,
)
from petal_locate.sim.world import PHASE_FIT_TOL_CYCLES, PathInfo, ResolvedEmitter, SimWorld

__all__ = [
    "PHASE_FIT_TOL_CYCLES",
    "EmitterSpec",
    "EphemerisError",
    "IonoError",
    "PathInfo",
    "ResolvedEmitter",
    "Scenario",
    "ScenarioError",
    "SimWorld",
    "TruthErrors",
    "WaveformSpec",
    "dump_scenario",
    "load_scenario",
    "loads_scenario",
]
