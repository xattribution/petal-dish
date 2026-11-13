"""Thin-shell ionospheric group delay (MATH.md section 3).

The ionosphere is a single shell at ``IONO_SHELL_HEIGHT_M`` (350 km) above a spherical
Earth of radius ``R_E``. A leg between a ground point and a satellite seen at elevation
``el`` crosses the shell obliquely, which multiplies the vertical electron content by

    slant(el) = 1 / sqrt(1 - (R_E cos(el) / (R_E + 350 km))^2)

and delays the modulation envelope (group delay) by

    delta = 40.3 * STEC / (f^2 C)   seconds,   STEC = vtec_tecu * 1e16 * slant(el)   electrons/m^2

with ``f`` in hertz. The carrier phase is *advanced* by the same amount: the envelope
argument uses ``delay + delta`` and the carrier-phase terms use ``delay - delta``.
``el`` is the elevation of the satellite seen from the ground end of the leg (the
emitter for the uplink at ``f_e``, the site for the downlink at ``f_e + T``).

Check value: 30 TECU, 305 MHz, zenith gives 434 ns.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .constants import C, IONO_SHELL_HEIGHT_M, R_E

__all__ = ["K_IONO", "TEC_UNIT", "slant_factor", "group_delay_s"]

K_IONO: float = 40.3
"""First-order ionospheric refraction constant, m^3/s^2."""

TEC_UNIT: float = 1e16
"""One TEC unit (TECU), electrons/m^2."""


def slant_factor(el_deg: Any) -> Any:
    """Thin-shell obliquity factor STEC / VTEC for a leg seen at elevation ``el_deg``.

    el_deg: elevation of the satellite above the local horizontal at the ground end of
    the leg, degrees, within [0, 90] (a leg below the horizon does not exist; MATH.md
    section 3 "Visibility"). Scalar or array.

    Returns the dimensionless factor, same shape: 1 at the zenith, rising to
    ``(R_E + h) / sqrt((R_E + h)^2 - R_E^2)`` (3.14 for h = 350 km) on the horizon.
    """
    el = _elevation(el_deg)
    ratio = R_E * np.cos(np.radians(el)) / (R_E + IONO_SHELL_HEIGHT_M)
    out = 1.0 / np.sqrt(1.0 - ratio * ratio)
    return out[()] if out.ndim == 0 else out


def group_delay_s(vtec_tecu: Any, el_deg: Any, freq_hz: Any) -> Any:
    """First-order ionospheric group delay of one leg, seconds (always >= 0).

    vtec_tecu: vertical total electron content, TEC units (1 TECU = 1e16 electrons/m^2),
    >= 0. el_deg: elevation of the satellite seen from the ground end of the leg,
    degrees in [0, 90]. freq_hz: carrier frequency of the leg, Hz, > 0. The three
    arguments broadcast.

    Returns ``40.3 * vtec_tecu * 1e16 * slant_factor(el_deg) / (freq_hz^2 * C)``. Add it
    to the delay seen by the envelope and subtract it from the delay seen by the
    carrier phase.
    """
    vtec = np.asarray(vtec_tecu, dtype=np.float64)
    if not np.all(np.isfinite(vtec)) or np.any(vtec < 0.0):
        raise ValueError("vtec_tecu must be finite and non-negative")
    freq = np.asarray(freq_hz, dtype=np.float64)
    if not np.all(np.isfinite(freq)) or np.any(freq <= 0.0):
        raise ValueError("freq_hz must be finite and positive")
    stec = vtec * TEC_UNIT * slant_factor(el_deg)
    out = np.asarray(K_IONO * stec / (freq * freq * C))
    return out[()] if out.ndim == 0 else out


def _elevation(el_deg: Any) -> np.ndarray:
    el = np.asarray(el_deg, dtype=np.float64)
    if not np.all(np.isfinite(el)) or np.any(el < 0.0) or np.any(el > 90.0):
        raise ValueError(
            "el_deg must be finite and within [0, 90] degrees (a leg below the horizon does not exist)"
        )
    return el
