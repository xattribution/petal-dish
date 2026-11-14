"""Propagation media: ionospheric and tropospheric delay of one leg, and their partials.

Neither medium is in the measurement model ``h`` (MATH.md section 3: the predictors use
vacuum light time); the solver and the accuracy map carry them as consider parameters
(MATH.md section 9), one per ground end of a leg, and the simulator can apply them as
truth (``sim.scenario.IonoError`` / ``TropoError``).

Ionosphere (dispersive, thin shell, ``core.iono``): a leg seen at elevation ``el`` from
its ground end, at frequency ``f``, is delayed (envelope) and advanced (carrier phase) by

    delta_iono = K_IONO * TEC_UNIT * VTEC * slant(el) / (f^2 C)     VTEC in TECU

Troposphere (non-dispersive): envelope and carrier phase are both delayed by

    delta_tropo = Z * m(el) / C        Z = zenith delay, m;  m(el) = 1.001 / sqrt(0.002001 + sin^2(el))

(the Black and Eisner mapping function, finite at the horizon: 22.4 at 0 degrees, 1.0
at the zenith).

A path's baseband frequency carries the carrier-phase term, ``-f d(delay)/dt`` for a
delay of the phase: so the ionosphere adds ``+f d(delta_iono)/dt`` and the troposphere
``-f d(delta_tropo)/dt`` to the path's frequency (Hz), with ``f`` the leg's frequency
(the uplink at ``f_e``, the downlink at ``f_e + T``). A TDOA row (path A minus path B)
gets ``+delta`` for path A's legs and ``-delta`` for path B's, an FDOA row likewise with
the frequency terms.

``leg_partials`` returns these per unit parameter (one TECU, one metre of zenith
delay). The elevation is geometric (no light time, no refraction), clipped to
``[0, 90]`` degrees, and its rate a fourth-order central difference over
``+/- 2 * STENCIL_STEP_S``; legs are evaluated at the row's epoch (the uplink's own
epoch differs by the downlink delay, which changes these partials by under 1e-3 even
in low orbit).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from .constants import C
from .geometry import STENCIL_STEP_S, elevation_deg
from .iono import K_IONO, TEC_UNIT, slant_factor
from .orbits import Orbit

__all__ = [
    "MEDIA",
    "tropo_mapping",
    "tropo_delay_s",
    "leg_partials",
    "row_media_partials",
]

MEDIA: tuple[str, ...] = ("ionosphere", "troposphere")
"""The consider categories this module provides (``solver.ERROR_SOURCES`` names)."""

_OFFSETS = np.array([-2.0, -1.0, 1.0, 2.0]) * STENCIL_STEP_S
_WEIGHTS = np.array([1.0, -8.0, 8.0, -1.0]) / (12.0 * STENCIL_STEP_S)


def tropo_mapping(el_deg: Any) -> Any:
    """Tropospheric mapping function ``m(el) = 1.001 / sqrt(0.002001 + sin^2(el))``.

    el_deg: elevation of the satellite seen from the ground end, degrees in [0, 90]
    (values outside are clipped). Returns the slant-to-zenith delay ratio, same shape.
    """
    el = np.clip(np.asarray(el_deg, dtype=np.float64), 0.0, 90.0)
    s = np.sin(np.radians(el))
    out = 1.001 / np.sqrt(0.002001 + s * s)
    return out[()] if out.ndim == 0 else out


def tropo_delay_s(zenith_m: Any, el_deg: Any) -> Any:
    """Tropospheric delay of one leg, seconds: ``zenith_m * m(el) / C`` (``zenith_m`` >= 0)."""
    z = np.asarray(zenith_m, dtype=np.float64)
    if not np.all(np.isfinite(z)) or np.any(z < 0.0):
        raise ValueError("zenith_m must be finite and non-negative")
    out = np.asarray(z * tropo_mapping(el_deg) / C)
    return out[()] if out.ndim == 0 else out


def leg_partials(ground: Any, orbit: Orbit, t_s: Any, freq_hz: Any) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Delay and frequency partials of one leg per unit media parameter.

    ground: ECEF m of the ground end(s), ``[..., 3]``. orbit: the satellite. t_s: epochs,
    Unix s, ``[M]``. freq_hz: the leg's frequency, Hz, scalar or ``[M]``.

    Returns ``{"ionosphere": (delay, freq), "troposphere": (delay, freq)}`` with arrays
    ``[..., M]``: the leg's delay in s and its contribution to the path's baseband
    frequency in Hz, per TECU of vertical electron content and per metre of zenith
    tropospheric delay at that ground end.
    """
    t = np.asarray(t_s, dtype=np.float64).reshape(-1)
    f = np.broadcast_to(np.asarray(freq_hz, dtype=np.float64), t.shape)
    epochs = np.concatenate([t, (t[:, None] + _OFFSETS[None, :]).reshape(-1)])
    el = np.clip(np.asarray(elevation_deg(np.asarray(ground, dtype=np.float64), orbit, epochs)), 0.0, 90.0)
    m = t.size
    lead = el.shape[:-1]
    el0 = el[..., :m]
    el_s = el[..., m:].reshape(lead + (m, _OFFSETS.size))
    iono_k = K_IONO * TEC_UNIT / (f * f * C)  # s per TECU at zenith
    slant0, slant_s = slant_factor(el0), slant_factor(el_s)
    map0, map_s = tropo_mapping(el0), tropo_mapping(el_s)
    iono_delay = iono_k * slant0
    iono_rate = iono_k * (slant_s @ _WEIGHTS)
    tropo_delay = map0 / C
    tropo_rate = (map_s @ _WEIGHTS) / C
    return {
        "ionosphere": (np.asarray(iono_delay), np.asarray(f * iono_rate)),
        "troposphere": (np.asarray(tropo_delay), np.asarray(-f * tropo_rate)),
    }


def row_media_partials(
    kind: str,
    a: Any,
    b: Any,
    t_s: Any,
    emitter: Any,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    emitter_key: Any = "emitter",
    *,
    uplinks: bool = True,
    downlinks: bool = True,
) -> dict[str, dict[Any, np.ndarray]]:
    """Partials of TDOA or FDOA rows of one path pair with respect to every ground end.

    kind: ``"tdoa"`` or ``"fdoa"``. a, b: the ``PathSpec``s (A minus B). t_s: epochs
    ``[M]``. emitter: ECEF m of the emitter position(s), ``[..., 3]`` (the uplinks'
    ground end; unused without ``uplinks``). emitter_key: the key of the emitter end
    in the result (a reference's own key for a reference measurement). uplinks,
    downlinks: which legs to include. Returns ``{medium: {end: array [..., M]}}`` with
    ends ``emitter_key`` (uplinks; leading axes of ``emitter``) and
    ``("site", site_id)`` (downlinks; ``[M]``), in row units per TECU or per metre.
    """
    index = 0 if kind == "tdoa" else 1
    out: dict[str, dict[Any, np.ndarray]] = {medium: {} for medium in MEDIA}
    for path, sign in ((a, 1.0), (b, -1.0)):
        orbit = orbits[path.sat_id]
        legs = []
        if uplinks:
            legs.append((emitter_key, leg_partials(np.asarray(emitter, dtype=np.float64), orbit, t_s, float(path.f_e_hz))))
        if downlinks:
            site = np.asarray(sites_xyz[path.site_id], dtype=np.float64)
            legs.append((("site", path.site_id), leg_partials(site, orbit, t_s, float(path.f_tune_hz))))
        for key, leg in legs:
            for medium in MEDIA:
                values = sign * leg[medium][index]
                table = out[medium]
                table[key] = table[key] + values if key in table else values
    return out
