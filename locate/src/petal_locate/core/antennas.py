"""Axisymmetric antenna gain patterns (ARCHITECTURE.md section 5).

Every function takes an *antenna model*: any object (or mapping) with the fields of the
configuration ``Antenna`` union, selected by ``type``. Nothing here imports the
configuration package.

    isotropic {gain_dbi=0}
        the same gain in every direction.
    parabolic {diameter_m, efficiency=0.55}
        G0 = efficiency * (pi D / lambda)^2. Main lobe ``G0 - 12 (theta / HPBW)^2`` dB.
        D >= 3 lambda: HPBW = 70 lambda / D degrees; the main lobe is floored by the
        side-lobe envelope ``min(G0, 32 - 25 log10(theta))`` dBi between 1 and 48
        degrees (held at its 1 degree value inside 1 degree) and by -10 dBi beyond.
        D < 3 lambda ("electrically small": the reflector no longer focuses):
        HPBW = min(70 lambda / D, 120) degrees and the floor is G0 - 15 dB.
        Never below -10 dBi in either case.
    beam {gain_dbi, hpbw_deg, front_to_back_db=20}        (yagi, helix, patch)
        ``gain_dbi - 12 (theta / hpbw_deg)^2`` floored at ``gain_dbi - front_to_back_db``.
    table {theta_deg: [...], gain_dbi: [...]}
        linear interpolation in dB over the off-axis angle; the end values are held
        outside the tabulated range.

Angles are degrees off boresight in [0, 180], gains are dBi, frequencies are hertz.
All patterns are symmetric about boresight and never exceed their boresight value
(``table`` excepted: it is whatever was tabulated).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from .constants import C

__all__ = [
    "MIN_GAIN_DBI",
    "SMALL_DISH_WAVELENGTHS",
    "gain_dbi",
    "peak_gain_dbi",
    "hpbw_deg",
    "electrically_small",
]

MIN_GAIN_DBI: float = -10.0
"""Far-out floor of every parabolic pattern, dBi."""

SMALL_DISH_WAVELENGTHS: float = 3.0
"""A parabolic antenna with D below this many wavelengths is electrically small."""

_HPBW_FACTOR_DEG = 70.0  # HPBW = 70 lambda / D
_SMALL_HPBW_MAX_DEG = 120.0
_SMALL_FLOOR_DB = 15.0  # floor of an electrically small dish, dB below G0
_ENVELOPE_REF_DBI = 32.0  # side-lobe envelope 32 - 25 log10(theta) dBi
_ENVELOPE_SLOPE_DB = 25.0
_ENVELOPE_MIN_DEG = 1.0
_ENVELOPE_MAX_DEG = 48.0
_MAIN_LOBE_DB = 12.0  # main lobe: -12 (theta / HPBW)^2 dB, i.e. -3 dB at theta = HPBW / 2
_MISSING: Any = object()
_KINDS = ("isotropic", "parabolic", "beam", "table")


def gain_dbi(antenna_model: Any, off_axis_deg: Any, freq_hz: Any) -> Any:
    """Gain of the antenna ``off_axis_deg`` away from boresight, dBi.

    antenna_model: an ``Antenna`` configuration object (see the module docstring).
    off_axis_deg: angle between boresight and the direction of interest, degrees;
    ``abs`` is taken (the patterns are symmetric), and the magnitude must not exceed
    180. freq_hz: frequency, Hz, > 0 (only ``parabolic`` depends on it). The two
    broadcast; the result has their common shape (a numpy scalar for scalars).
    """
    theta = _angle(off_axis_deg)
    freq = _frequency(freq_hz)
    kind = _kind(antenna_model)
    if kind == "isotropic":
        gain = _number(antenna_model, "gain_dbi", 0.0) + 0.0 * (theta + freq)
    elif kind == "parabolic":
        g0, hpbw, small = _parabolic(antenna_model, freq)
        main = g0 - _MAIN_LOBE_DB * (theta / hpbw) ** 2
        # The envelope is held at its 1 degree value inside 1 degree. At 48 degrees it has
        # fallen to -10.03 dBi, so beyond that the -10 dBi floor below takes over.
        clipped = np.clip(theta, _ENVELOPE_MIN_DEG, _ENVELOPE_MAX_DEG)
        envelope = np.minimum(g0, _ENVELOPE_REF_DBI - _ENVELOPE_SLOPE_DB * np.log10(clipped))
        floor = np.where(small, g0 - _SMALL_FLOOR_DB, envelope)
        gain = np.maximum(np.maximum(main, floor), MIN_GAIN_DBI)
    elif kind == "beam":
        g0, hpbw, front_to_back = _beam(antenna_model)
        gain = np.maximum(g0 - _MAIN_LOBE_DB * (theta / hpbw) ** 2, g0 - front_to_back) + 0.0 * freq
    else:
        tab_theta, tab_gain = _table(antenna_model)
        gain = np.interp(theta, tab_theta, tab_gain) + 0.0 * freq
    gain = np.asarray(gain, dtype=np.float64)
    return gain[()] if gain.ndim == 0 else gain


def peak_gain_dbi(antenna_model: Any, freq_hz: Any) -> Any:
    """Largest gain of the pattern, dBi, at ``freq_hz`` (Hz; scalar or array).

    ``isotropic`` and ``beam``: ``gain_dbi``. ``parabolic``:
    ``10 log10(efficiency (pi D / lambda)^2)``, not below -10 dBi (the pattern floor).
    ``table``: the largest tabulated value (which need not be on boresight; the
    boresight gain is ``gain_dbi(antenna_model, 0.0, freq_hz)``).
    """
    freq = _frequency(freq_hz)
    kind = _kind(antenna_model)
    if kind == "isotropic":
        peak = _number(antenna_model, "gain_dbi", 0.0) + 0.0 * freq
    elif kind == "parabolic":
        peak = _parabolic(antenna_model, freq)[0]
    elif kind == "beam":
        peak = _beam(antenna_model)[0] + 0.0 * freq
    else:
        peak = float(np.max(_table(antenna_model)[1])) + 0.0 * freq
    peak = np.asarray(peak, dtype=np.float64)
    return peak[()] if peak.ndim == 0 else peak


def hpbw_deg(antenna_model: Any, freq_hz: Any) -> Any:
    """Half-power (-3 dB) full beamwidth, degrees, at ``freq_hz`` (Hz; scalar or array).

    ``parabolic``: ``70 lambda / D``, limited to 120 degrees when electrically small.
    ``beam``: ``hpbw_deg``. ``isotropic``: 360 (there is no half-power direction).
    ``table``: twice the smallest off-axis angle at which the interpolated gain is
    3 dB below the boresight gain, or 360 when it never falls that far.
    """
    freq = _frequency(freq_hz)
    kind = _kind(antenna_model)
    if kind == "isotropic":
        width = 360.0 + 0.0 * freq
    elif kind == "parabolic":
        width = _parabolic(antenna_model, freq)[1]
    elif kind == "beam":
        width = _beam(antenna_model)[1] + 0.0 * freq
    else:
        width = _table_hpbw(*_table(antenna_model)) + 0.0 * freq
    width = np.asarray(width, dtype=np.float64)
    return width[()] if width.ndim == 0 else width


def electrically_small(antenna_model: Any, freq_hz: Any) -> Any:
    """True for a ``parabolic`` antenna whose diameter is under 3 wavelengths at ``freq_hz``.

    Such a reflector does not focus: its gain is a few dBi and its beam is tens of
    degrees wide (a 0.72 m dish at 260 MHz is 0.62 wavelengths across). False for
    every other antenna type. freq_hz: Hz, scalar (returns ``bool``) or array (returns
    a bool array).
    """
    freq = _frequency(freq_hz)
    if _kind(antenna_model) != "parabolic":
        small = np.zeros(freq.shape, dtype=bool)
    else:
        small = _parabolic(antenna_model, freq)[2]
    return bool(small) if small.ndim == 0 else small


# --------------------------------------------------------------------------- internals


def _parabolic(model: Any, freq: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Boresight gain (dBi), HPBW (deg) and the electrically-small flag at ``freq`` (Hz)."""
    diameter = _number(model, "diameter_m")
    efficiency = _number(model, "efficiency", 0.55)
    if diameter <= 0.0:
        raise ValueError(f"antenna.diameter_m must be positive, got {diameter!r}")
    if not 0.0 < efficiency <= 1.0:
        raise ValueError(f"antenna.efficiency must be within (0, 1], got {efficiency!r}")
    wavelengths = diameter * freq / C  # D / lambda
    g0 = 10.0 * np.log10(efficiency * (np.pi * wavelengths) ** 2)
    small = wavelengths < SMALL_DISH_WAVELENGTHS
    hpbw = _HPBW_FACTOR_DEG / wavelengths
    hpbw = np.where(small, np.minimum(hpbw, _SMALL_HPBW_MAX_DEG), hpbw)
    return np.maximum(g0, MIN_GAIN_DBI), hpbw, small


def _beam(model: Any) -> tuple[float, float, float]:
    g0 = _number(model, "gain_dbi")
    hpbw = _number(model, "hpbw_deg")
    front_to_back = _number(model, "front_to_back_db", 20.0)
    if not 0.0 < hpbw <= 360.0:
        raise ValueError(f"antenna.hpbw_deg must be within (0, 360], got {hpbw!r}")
    if front_to_back < 0.0:
        raise ValueError(f"antenna.front_to_back_db must be non-negative, got {front_to_back!r}")
    return g0, hpbw, front_to_back


def _table(model: Any) -> tuple[np.ndarray, np.ndarray]:
    theta = np.asarray(_get(model, "theta_deg"), dtype=np.float64)
    gain = np.asarray(_get(model, "gain_dbi"), dtype=np.float64)
    if theta.ndim != 1 or theta.size < 1:
        raise ValueError("antenna.theta_deg must be a non-empty list of angles")
    if gain.shape != theta.shape:
        raise ValueError(
            f"antenna.gain_dbi must have one value per antenna.theta_deg entry "
            f"({theta.size}), got shape {gain.shape}"
        )
    if not (np.all(np.isfinite(theta)) and np.all(np.isfinite(gain))):
        raise ValueError("antenna.theta_deg and antenna.gain_dbi must be finite")
    if theta[0] < 0.0 or theta[-1] > 180.0 or np.any(np.diff(theta) <= 0.0):
        raise ValueError("antenna.theta_deg must be strictly increasing within [0, 180] degrees")
    return theta, gain


def _table_hpbw(theta: np.ndarray, gain: np.ndarray) -> float:
    """Full -3 dB width of a tabulated pattern relative to its boresight gain, degrees."""
    target = float(gain[0]) - 3.0  # the first value is held in to boresight
    below = np.flatnonzero(gain <= target)
    if below.size == 0:
        return 360.0
    k = int(below[0])  # k >= 1 and gain[k - 1] > target >= gain[k]
    frac = (gain[k - 1] - target) / (gain[k - 1] - gain[k])
    return 2.0 * float(theta[k - 1] + frac * (theta[k] - theta[k - 1]))


def _kind(model: Any) -> str:
    if model is None:
        raise ValueError("antenna is required")
    kind = _get(model, "type")
    kind = getattr(kind, "value", kind)  # tolerate enums
    if kind not in _KINDS:
        raise ValueError(f"antenna.type must be one of {', '.join(map(repr, _KINDS))}, got {kind!r}")
    return str(kind)


def _get(model: Any, name: str, default: Any = _MISSING) -> Any:
    """Read ``name`` from a mapping or an attribute object; ValueError naming it when absent."""
    value = model.get(name, _MISSING) if isinstance(model, Mapping) else getattr(model, name, _MISSING)
    if value is _MISSING or value is None:
        if default is _MISSING:
            raise ValueError(f"antenna.{name} is required")
        return default
    return value


def _number(model: Any, name: str, default: Any = _MISSING) -> float:
    value = _get(model, name, default)
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"antenna.{name} must be a number, got {value!r}") from None
    if not np.isfinite(out):
        raise ValueError(f"antenna.{name} must be finite, got {value!r}")
    return out


def _angle(off_axis_deg: Any) -> np.ndarray:
    theta = np.abs(np.asarray(off_axis_deg, dtype=np.float64))
    if not np.all(np.isfinite(theta)) or np.any(theta > 180.0 + 1e-9):
        raise ValueError("off_axis_deg must be finite and within [-180, 180] degrees")
    return np.minimum(theta, 180.0)


def _frequency(freq_hz: Any) -> np.ndarray:
    freq = np.asarray(freq_hz, dtype=np.float64)
    if not np.all(np.isfinite(freq)) or np.any(freq <= 0.0):
        raise ValueError("freq_hz must be finite and positive")
    return freq
