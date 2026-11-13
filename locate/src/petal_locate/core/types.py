"""Small shared data types (ARCHITECTURE.md section 5).

`geometry.py` re-exports `PathSpec`; `solver.py` re-exports `CalRef`, `Measurement`
and `Uncertainty`. They live here so modules can be built independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np


@dataclass(frozen=True)
class PathSpec:
    """One bent-pipe path: emitter -> satellite `sat_id` -> site `site_id`.

    f_e_hz: uplink carrier frequency of the emitter, Hz.
    translation_hz: nominal transponder translation, Hz (downlink = uplink + translation).
    tau_x_s: transponder group delay, s.
    """

    sat_id: str
    site_id: str
    f_e_hz: float
    translation_hz: float
    tau_x_s: float = 0.0

    @property
    def f_tune_hz(self) -> float:
        """Nominal downlink (and capture centre) frequency, Hz."""
        return self.f_e_hz + self.translation_hz


@dataclass(frozen=True, eq=False)
class CalRef:
    """A reference-emitter measurement used to calibrate target rows (MATH.md section 9).

    x_ref: ECEF position of the reference emitter's phase centre, m.
    a, b: the reference carrier's own paths (its f_e_hz) on the same satellites and sites.
    t_ns: epoch (t_mid) of the reference measurement.
    sigma: CAF one-sigma of the reference measurement (s for TDOA, Hz for FDOA).
    """

    cal_id: str
    x_ref: np.ndarray
    a: PathSpec
    b: PathSpec
    t_ns: int
    sigma: float


@dataclass
class Measurement:
    """One TDOA (seconds) or FDOA (hertz) row, A minus B (MATH.md section 5).

    sigma is the CAF noise of this measurement only. t_ns is t_mid: the midpoint of
    the channel-B samples. correction is the calibration term c subtracted by the
    solver; cal identifies the reference measurement it came from.
    """

    kind: Literal["tdoa", "fdoa"]
    value: float
    sigma: float
    t_ns: int
    a: PathSpec
    b: PathSpec
    correction: float = 0.0
    cal: CalRef | None = None
    tag: str = ""


@dataclass
class Uncertainty:
    """Resolved one-sigma systematic uncertainties (consider parameters).

    sat_pos_ric_m / sat_vel_ric_mps: per satellite, radial / in-track / cross-track.
    site_clock_s: per site clock offset, s. site_osc_frac: per site fractional
    frequency error. sat_osc_frac: per satellite translation-oscillator fractional
    error. tdoa_bias_s: per-pair TDOA bias, s. Missing keys mean zero.
    """

    sat_pos_ric_m: dict[str, np.ndarray] = field(default_factory=dict)
    sat_vel_ric_mps: dict[str, np.ndarray] = field(default_factory=dict)
    site_clock_s: dict[str, float] = field(default_factory=dict)
    site_osc_frac: dict[str, float] = field(default_factory=dict)
    sat_osc_frac: dict[str, float] = field(default_factory=dict)
    tdoa_bias_s: float = 0.0
