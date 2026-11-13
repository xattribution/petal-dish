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
    capture_a, capture_b, noise_share_a, noise_share_b: as ``Measurement``'s, for the
    reference measurement's own captures: two reference measurements correlated from
    one capture share that capture's noise (copied from the reference row by
    ``solver.calibration_correction``).
    """

    cal_id: str
    x_ref: np.ndarray
    a: PathSpec
    b: PathSpec
    t_ns: int
    sigma: float
    capture_a: str = ""
    capture_b: str = ""
    noise_share_a: float = 0.0
    noise_share_b: float = 0.0


@dataclass
class Measurement:
    """One TDOA (seconds) or FDOA (hertz) row, A minus B (MATH.md section 5).

    sigma is the CAF noise of this measurement only. t_ns is t_mid: the midpoint of
    the channel-B samples. correction is the calibration term c subtracted by the
    solver; cal identifies the reference measurement it came from.

    capture_a, capture_b: identifiers of the two captures the row was correlated from
    ("" when unknown or not shared). Rows of different pairs correlated from one
    capture (tasking pairs every partner with the same primary capture) share that
    capture's noise: noise_share_a (noise_share_b) is the fraction of ``sigma**2``
    caused by channel A's (B's) own noise times the other channel's signal
    (``CafResult.noise_share_tdoa`` / ``noise_share_fdoa``). The solver carries the
    shared part as one consider column per (capture, kind) used by two or more rows
    (MATH.md section 9); both are 0 by default, and their sum must be below 1.
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
    capture_a: str = ""
    capture_b: str = ""
    noise_share_a: float = 0.0
    noise_share_b: float = 0.0


@dataclass
class Uncertainty:
    """Resolved one-sigma systematic uncertainties (consider parameters).

    sat_pos_ric_m / sat_vel_ric_mps: per satellite, radial / in-track / cross-track.
    site_clock_s: per site clock offset, s. site_osc_frac: per site fractional
    frequency error. sat_osc_frac: per satellite translation-oscillator fractional
    error. tdoa_bias_s: per-pair TDOA bias, s. Missing keys mean zero.

    iono_vtec_tecu: one-sigma vertical total electron content at each ground end of a
    leg (the emitter, every site, every reference), TECU; the solver applies no
    ionospheric delay, so this is the whole VTEC, not an error about a model. 0 turns
    the ionosphere off. iono_correlation: correlation of that VTEC between any two
    ground ends, in [0, 1] (0: independent, 1: one common value). tropo_zenith_m,
    tropo_correlation: the same for the zenith tropospheric delay, m (MATH.md
    section 9; ``core.media``).
    """

    sat_pos_ric_m: dict[str, np.ndarray] = field(default_factory=dict)
    sat_vel_ric_mps: dict[str, np.ndarray] = field(default_factory=dict)
    site_clock_s: dict[str, float] = field(default_factory=dict)
    site_osc_frac: dict[str, float] = field(default_factory=dict)
    sat_osc_frac: dict[str, float] = field(default_factory=dict)
    tdoa_bias_s: float = 0.0
    iono_vtec_tecu: float = 0.0
    iono_correlation: float = 0.0
    tropo_zenith_m: float = 0.0
    tropo_correlation: float = 0.0
