"""Link budgets of bent-pipe and direct paths (MATH.md section 4 "Amplitudes", section 7).

Everything is in decibels: powers in dBW, power densities in dBW/Hz, flux densities in
dBW/m^2, gains in dBi, ``G/T`` in dB/K. Positions are ECEF (WGS84) metres, frequencies
hertz, angles degrees. Boresight arguments are unit ECEF vectors; ``None`` means
"omni": no pattern is applied and the boresight value is used in every direction.

The model, per path:

    C/N0_up  = EIRP_e(toward sat) - FSPL(f_e, rho_u) + G/T_sat - K_DBW
    PFD      = EIRP_e(toward sat) - 10 log10(4 pi rho_u^2)
    EIRP_c   = EIRP_sat - (SFD - PFD)                                   linear transponder
    EIRP_n0  = EIRP_sat - SFD + 10 log10(4 pi f_uc^2 / C^2) - G/T_sat + K_DBW     dBW/Hz
    P_tot    = sum of carrier EIRPs + EIRP_n0 * bandwidth_hz  (watts); when it exceeds
               EIRP_sat every carrier and EIRP_n0 are multiplied by EIRP_sat / P_tot
    C_rx     = EIRP_c  - FSPL(f_d, rho_d) + G_site(off-axis) - L_pol
    N0_relay = EIRP_n0 - FSPL(f_d, rho_d) + G_site(off-axis) - L_pol
    N0_site  = K_DBW + 10 log10(T_sys)
    direct:  C_rx = EIRP_e(toward site) - FSPL(f_e, d) + G_site(off-axis to the emitter) - L_pol,
             only inside the radio horizon d < 4.12 km (sqrt(h_e) + sqrt(h_g))

The transponder noise density ``EIRP_n0`` is a property of the transponder alone (it is
defined with no carrier present and equals ``EIRP_c - C/N0_up`` for any carrier at the
uplink centre). The polarisation loss ``L_pol`` is the receive channel's and is applied
where a site receives, never on the uplink.

A satellite leg exists only while the satellite is above the horizon of its ground end
(elevation > 0, WGS84 local vertical, no refraction). The leg functions here report the
elevation and leave the decision to the caller; ``path_budget`` and
``downlink_quality_db`` apply it and return ``None`` for a path that does not exist.

Configuration objects (transponder, channel, antenna, emitter, the system
configuration) are read by attribute; nothing here imports ``petal_locate.config``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .antennas import gain_dbi
from .constants import C, K_DBW
from .geodesy import angle_between_deg, azel_to_ecef, ecef_to_lla
from .orbits import orbit_position

__all__ = [
    "RADIO_HORIZON_M_PER_SQRT_M",
    "UplinkBudget",
    "TransponderOutputs",
    "DownlinkBudget",
    "DirectBudget",
    "PathBudget",
    "fspl_db",
    "transponder_noise_eirp_dbw_hz",
    "uplink_budget",
    "transponder_outputs",
    "downlink_budget",
    "direct_budget",
    "emitter_boresight",
    "path_budget",
    "downlink_quality_db",
    "relay_rise_db",
]

RADIO_HORIZON_M_PER_SQRT_M: float = 4120.0
"""Radio horizon: d < 4.12 km * (sqrt(h_e) + sqrt(h_g)), antenna heights in metres."""

_MISSING: Any = object()


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, eq=False)
class UplinkBudget:
    """Emitter-to-satellite leg. Every field broadcasts over the emitter positions.

    eirp_toward_sat_dbw: EIRP radiated in the direction of the satellite, dBW.
    pfd_dbw_m2: power flux density at the satellite, dBW/m^2.
    cn0_up_dbhz: uplink carrier-to-noise density at the transponder input, dBHz.
    elevation_deg: elevation of the satellite seen from the emitter, degrees
        (the leg exists only when this is > 0).
    """

    eirp_toward_sat_dbw: Any
    pfd_dbw_m2: Any
    cn0_up_dbhz: Any
    elevation_deg: Any


@dataclass(frozen=True, eq=False)
class TransponderOutputs:
    """What a transponder radiates for a set of carriers sharing it.

    eirp_down_dbw: downlink EIRP of each carrier after power robbing, dBW, ``[..., K]``.
    relayed_n0_eirp_dbw_hz: EIRP density of the relayed transponder noise after power
        robbing, dBW/Hz (a float for a 1-D ``pfd_dbw_m2``, otherwise ``[...]``).
    backoff_db: ``EIRP_sat`` minus the total power the linear transponder would
        radiate (all carriers plus its noise over ``bandwidth_hz``), dB. Positive: the
        transponder is linear and runs this far below saturation. Negative: it is
        saturated and every carrier and the noise were reduced by ``-backoff_db``.
    """

    eirp_down_dbw: Any
    relayed_n0_eirp_dbw_hz: Any
    backoff_db: Any

    @property
    def compression_db(self) -> Any:
        """Power-robbing loss applied to every carrier and to the noise, dB (>= 0)."""
        return np.maximum(-np.asarray(self.backoff_db, dtype=np.float64), 0.0)[()]


@dataclass(frozen=True, eq=False)
class DownlinkBudget:
    """Satellite-to-site leg into one receive channel.

    c_rx_dbw: received carrier power, dBW (broadcasts over ``eirp_down_dbw``).
    relayed_n0_dbw_hz: received density of the relayed transponder noise, dBW/Hz.
    site_n0_dbw_hz: the channel's own thermal noise density, dBW/Hz.
    elevation_deg: elevation of the satellite seen from the site, degrees (the leg
        exists only when this is > 0).
    off_axis_deg: angle between the channel boresight and the satellite, degrees.
    """

    c_rx_dbw: Any
    relayed_n0_dbw_hz: Any
    site_n0_dbw_hz: Any
    elevation_deg: Any
    off_axis_deg: Any

    @property
    def n0_total_dbw_hz(self) -> Any:
        """``10 log10(N0_site + N0_relay)`` (linear sum), dBW/Hz."""
        return _db(_lin(self.site_n0_dbw_hz) + _lin(self.relayed_n0_dbw_hz))

    @property
    def relay_rise_db(self) -> Any:
        """Rise of the noise floor inside the transponder passband,
        ``R = 10 log10(1 + N0_relay / N0_site)`` dB (MATH.md section 7)."""
        return _db(1.0 + _lin(np.asarray(self.relayed_n0_dbw_hz) - self.site_n0_dbw_hz))


@dataclass(frozen=True, eq=False)
class DirectBudget:
    """Terrestrial path from an emitter straight into a receive channel.

    c_rx_dbw: received carrier power, dBW; ``-inf`` where the path does not exist.
    in_horizon: whether the emitter is inside the radio horizon of the site (bool, or
        a bool array over the emitter positions).
    """

    c_rx_dbw: Any
    in_horizon: Any


@dataclass(frozen=True, eq=False)
class PathBudget:
    """One bent-pipe path: emitter -> transponder -> receive channel.

    uplink, downlink: the two legs. transponder_eirp_dbw: the carrier's downlink EIRP
    after power robbing, dBW. cn0_total_dbhz: ``C_rx / (N0_site + N0_relay)``, dBHz
    (the cascade of uplink and downlink C/N0, exactly so for a carrier at the
    transponder's uplink centre: the relayed noise density is defined there, which
    differs from ``EIRP_c - C/N0_up`` by ``20 log10(f_e / f_uc)``, under 1e-3 dB for
    the transponders of the example configurations).
    """

    uplink: UplinkBudget
    transponder_eirp_dbw: Any
    downlink: DownlinkBudget
    cn0_total_dbhz: Any

    def snr_db(self, bandwidth_hz: Any) -> Any:
        """Signal-to-noise ratio in a noise bandwidth of ``bandwidth_hz`` (Hz), dB."""
        bandwidth = np.asarray(bandwidth_hz, dtype=np.float64)
        if not np.all(np.isfinite(bandwidth)) or np.any(bandwidth <= 0.0):
            raise ValueError("bandwidth_hz must be finite and positive")
        return (self.cn0_total_dbhz - 10.0 * np.log10(bandwidth))[()]


# --------------------------------------------------------------------------- legs


def fspl_db(dist_m: Any, freq_hz: Any) -> Any:
    """Free-space path loss ``20 log10(4 pi d f / C)``, dB (positive).

    dist_m: path length, m, > 0. freq_hz: frequency, Hz, > 0. They broadcast.
    """
    dist = np.asarray(dist_m, dtype=np.float64)
    freq = np.asarray(freq_hz, dtype=np.float64)
    if not np.all(np.isfinite(dist)) or np.any(dist <= 0.0):
        raise ValueError("dist_m must be finite and positive")
    if not np.all(np.isfinite(freq)) or np.any(freq <= 0.0):
        raise ValueError("freq_hz must be finite and positive")
    return (20.0 * np.log10(4.0 * np.pi * dist * freq / C))[()]


def transponder_noise_eirp_dbw_hz(transponder: Any) -> float:
    """EIRP density of a transponder's own noise with no power robbing, dBW/Hz.

    ``EIRP_sat - SFD + 10 log10(4 pi f_uc^2 / C^2) - G/T + K_DBW`` with
    ``f_uc = uplink_center_hz``. transponder: object with ``eirp_sat_dbw`` (dBW),
    ``sfd_dbw_m2`` (dBW/m^2), ``g_over_t_dbk`` (dB/K) and ``uplink_center_hz`` (Hz).
    """
    f_uc = _positive(_attr(transponder, "uplink_center_hz", "transponder"), "transponder.uplink_center_hz")
    return float(
        _number(_attr(transponder, "eirp_sat_dbw", "transponder"), "transponder.eirp_sat_dbw")
        - _number(_attr(transponder, "sfd_dbw_m2", "transponder"), "transponder.sfd_dbw_m2")
        + 10.0 * np.log10(4.0 * np.pi * f_uc * f_uc / (C * C))
        - _number(_attr(transponder, "g_over_t_dbk", "transponder"), "transponder.g_over_t_dbk")
        + K_DBW
    )


def uplink_budget(
    eirp_dbw: Any,
    antenna: Any,
    boresight: Any,
    x_e: Any,
    sat_r: Any,
    f_e_hz: Any,
    transponder: Any,
) -> UplinkBudget:
    """Emitter-to-satellite leg (no horizon test: see ``elevation_deg`` of the result).

    eirp_dbw: emitter EIRP on boresight, dBW. antenna: the emitter's antenna model
    (``core.antennas``); ignored when ``boresight`` is None. boresight: unit ECEF
    vector(s) ``[..., 3]`` the antenna points along, or None for "omni" (``eirp_dbw``
    in every direction). x_e: emitter phase centre(s), ECEF m, ``[..., 3]``.
    sat_r: satellite position, ECEF m, ``[3]`` (or broadcastable against ``x_e``).
    f_e_hz: uplink carrier frequency, Hz. transponder: object with ``g_over_t_dbk``.

    Returns an ``UplinkBudget`` whose fields broadcast over ``x_e``.
    """
    x = _vec3(x_e, "x_e")
    sat = _vec3(sat_r, "sat_r")
    f_e = _positive_array(f_e_hz, "f_e_hz")
    eirp = _finite_array(eirp_dbw, "eirp_dbw")
    g_over_t = _number(_attr(transponder, "g_over_t_dbk", "transponder"), "transponder.g_over_t_dbk")
    los = sat - x
    rho = np.linalg.norm(los, axis=-1)
    if np.any(rho <= 0.0):
        raise ValueError("x_e coincides with sat_r")
    toward = eirp + _pattern_db(antenna, boresight, los, f_e, "boresight")
    pfd = toward - 10.0 * np.log10(4.0 * np.pi * rho * rho)
    cn0 = toward - fspl_db(rho, f_e) + g_over_t - K_DBW
    elevation = _elevation_deg(x, sat)
    return UplinkBudget(
        eirp_toward_sat_dbw=_scalar(toward),
        pfd_dbw_m2=_scalar(pfd),
        cn0_up_dbhz=_scalar(cn0),
        elevation_deg=_scalar(elevation),
    )


def transponder_outputs(pfd_dbw_m2: Any, transponder: Any) -> TransponderOutputs:
    """Downlink EIRP of the carriers sharing a transponder, with power robbing.

    pfd_dbw_m2: flux density of each carrier at the satellite, dBW/m^2, ``[K]`` (K may
    be 0: noise only) or ``[..., K]`` to evaluate many cases at once (the carriers of
    one case lie along the last axis). ``-inf`` stands for an absent carrier.
    transponder: object with ``eirp_sat_dbw`` (saturated EIRP, dBW), ``sfd_dbw_m2``
    (saturating flux density, dBW/m^2), ``g_over_t_dbk`` (dB/K), ``uplink_center_hz``
    and ``bandwidth_hz`` (Hz).

    Each carrier leaves with ``EIRP_sat - (SFD - PFD)``; the transponder adds its own
    noise ``EIRP_n0`` over ``bandwidth_hz``. When carriers plus noise exceed
    ``EIRP_sat``, all of them (the noise included) are scaled by the same factor so
    that the total equals ``EIRP_sat``.
    """
    pfd = np.atleast_1d(np.asarray(pfd_dbw_m2, dtype=np.float64))
    if np.any(np.isnan(pfd)) or np.any(pfd == np.inf):
        raise ValueError("pfd_dbw_m2 must be finite (or -inf for an absent carrier)")
    eirp_sat = _number(_attr(transponder, "eirp_sat_dbw", "transponder"), "transponder.eirp_sat_dbw")
    sfd = _number(_attr(transponder, "sfd_dbw_m2", "transponder"), "transponder.sfd_dbw_m2")
    bandwidth = _positive(_attr(transponder, "bandwidth_hz", "transponder"), "transponder.bandwidth_hz")
    n0 = transponder_noise_eirp_dbw_hz(transponder)

    carriers_w = _lin(eirp_sat - sfd + pfd)  # [..., K]
    total_w = np.sum(carriers_w, axis=-1) + _lin(n0) * bandwidth  # [...]
    saturated_w = _lin(eirp_sat)
    scale = np.minimum(1.0, saturated_w / total_w)
    eirp_down = _db(carriers_w * scale[..., None])
    relayed_n0 = n0 + _db(scale)
    backoff = eirp_sat - _db(total_w)
    if pfd.ndim == 1:
        return TransponderOutputs(eirp_down, float(relayed_n0), float(backoff))
    return TransponderOutputs(eirp_down, relayed_n0, backoff)


def downlink_budget(
    eirp_down_dbw: Any,
    relayed_n0_eirp_dbw_hz: Any,
    f_d_hz: Any,
    sat_r: Any,
    site_xyz: Any,
    channel: Any,
    boresight: Any,
) -> DownlinkBudget:
    """Satellite-to-site leg into one receive channel (no horizon test: see ``elevation_deg``).

    eirp_down_dbw: carrier EIRP leaving the satellite, dBW (any shape).
    relayed_n0_eirp_dbw_hz: EIRP density of the relayed transponder noise, dBW/Hz.
    f_d_hz: downlink frequency, Hz. sat_r: satellite position, ECEF m, ``[3]``.
    site_xyz: antenna phase centre of the site, ECEF m, ``[3]``. channel: object with
    ``antenna``, ``system_noise_temp_k`` (K) and ``polarization_loss_db`` (dB).
    boresight: unit ECEF vector the channel's antenna points along (None: the
    boresight gain is used whatever the direction).
    """
    eirp = np.asarray(eirp_down_dbw, dtype=np.float64)
    n0_eirp = np.asarray(relayed_n0_eirp_dbw_hz, dtype=np.float64)
    if np.any(np.isnan(eirp)) or np.any(eirp == np.inf):
        raise ValueError("eirp_down_dbw must be finite (or -inf for an absent carrier)")
    if np.any(np.isnan(n0_eirp)) or np.any(n0_eirp == np.inf):
        raise ValueError("relayed_n0_eirp_dbw_hz must be finite (or -inf for none)")
    net, site_n0, elevation, off_axis = _downlink_terms(f_d_hz, sat_r, site_xyz, channel, boresight)
    return DownlinkBudget(
        c_rx_dbw=_scalar(eirp + net),
        relayed_n0_dbw_hz=_scalar(n0_eirp + net),
        site_n0_dbw_hz=site_n0,
        elevation_deg=_scalar(elevation),
        off_axis_deg=_scalar(off_axis),
    )


def _downlink_terms(
    f_d_hz: Any, sat_r: Any, site_xyz: Any, channel: Any, boresight: Any
) -> tuple[Any, float, Any, Any]:
    """Shared pieces of a downlink: ``(G_site - FSPL - L_pol [dB], N0_site [dBW/Hz],
    elevation [deg], off-axis angle [deg])``."""
    sat = _vec3(sat_r, "sat_r")
    site = _vec3(site_xyz, "site_xyz")
    f_d = _positive_array(f_d_hz, "f_d_hz")
    t_sys = _positive(_attr(channel, "system_noise_temp_k", "channel"), "channel.system_noise_temp_k")
    l_pol = _number(_attr(channel, "polarization_loss_db", "channel", 0.0), "channel.polarization_loss_db")
    antenna = _attr(channel, "antenna", "channel")
    los = sat - site
    rho = np.linalg.norm(los, axis=-1)
    if np.any(rho <= 0.0):
        raise ValueError("site_xyz coincides with sat_r")
    off_axis = _off_axis(boresight, los, "boresight")
    net = gain_dbi(antenna, off_axis, f_d) - fspl_db(rho, f_d) - l_pol
    return net, float(K_DBW + 10.0 * np.log10(t_sys)), _elevation_deg(site, sat), off_axis


def direct_budget(
    eirp_dbw: Any,
    antenna: Any,
    boresight: Any,
    x_e: Any,
    h_e_m: Any,
    site_xyz: Any,
    h_g_m: Any,
    channel: Any,
    channel_boresight: Any,
    f_hz: Any,
) -> DirectBudget:
    """Terrestrial path from an emitter straight into a receive channel.

    eirp_dbw: emitter EIRP on boresight, dBW. antenna, boresight: the emitter's
    antenna model and unit ECEF boresight (None = "omni"). x_e: emitter phase
    centre(s), ECEF m, ``[..., 3]``. h_e_m: emitter antenna height above ground, m.
    site_xyz: site phase centre, ECEF m, ``[3]``. h_g_m: site antenna height above
    ground, m. channel: object with ``antenna``, ``polarization_loss_db``.
    channel_boresight: unit ECEF boresight of the channel's antenna (None: boresight
    gain whatever the direction). f_hz: the frequency radiated and received, Hz.

    ``C_rx = EIRP_e(toward site) - FSPL(f, d) + G_site(off-axis to the emitter) - L_pol``
    with ``d = |site_xyz - x_e|``. The path exists only inside the radio horizon
    ``d < 4.12 km * (sqrt(h_e_m) + sqrt(h_g_m))``; elsewhere ``c_rx_dbw`` is ``-inf``
    and ``in_horizon`` is False. The free-space formula needs the far field: an
    emitter closer than one wavelength to the site antenna raises ValueError.
    """
    x = _vec3(x_e, "x_e")
    site = _vec3(site_xyz, "site_xyz")
    freq = _positive_array(f_hz, "f_hz")
    eirp = _finite_array(eirp_dbw, "eirp_dbw")
    h_e = _nonnegative_array(h_e_m, "h_e_m")
    h_g = _nonnegative_array(h_g_m, "h_g_m")
    l_pol = _number(_attr(channel, "polarization_loss_db", "channel", 0.0), "channel.polarization_loss_db")
    site_antenna = _attr(channel, "antenna", "channel")

    los = site - x  # emitter -> site
    dist = np.linalg.norm(los, axis=-1)
    if np.any(dist < C / freq):
        raise ValueError(
            "x_e is closer than one wavelength to site_xyz: the direct-path budget is a far-field formula"
        )
    toward = eirp + _pattern_db(antenna, boresight, los, freq, "boresight")
    site_gain = gain_dbi(site_antenna, _off_axis(channel_boresight, -los, "channel_boresight"), freq)
    c_rx = toward - fspl_db(dist, freq) + site_gain - l_pol
    in_horizon = dist < RADIO_HORIZON_M_PER_SQRT_M * (np.sqrt(h_e) + np.sqrt(h_g))
    c_rx = np.where(in_horizon, c_rx, -np.inf)
    if c_rx.ndim == 0:
        return DirectBudget(c_rx_dbw=float(c_rx), in_horizon=bool(in_horizon))
    return DirectBudget(c_rx_dbw=c_rx, in_horizon=np.broadcast_to(in_horizon, c_rx.shape).copy())


# --------------------------------------------------------------------------- whole paths


def emitter_boresight(emitter: Any, x_e: Any, orbits: Mapping[str, Any], t_s: float) -> Any:
    """Unit ECEF boresight of an emitter's antenna, or None for an "omni" emitter.

    emitter: object with ``pointing``: ``"omni"`` (or absent), ``{satellite_id}`` (aimed
    at that satellite's geometric position at ``t_s``) or ``{az_deg, el_deg}`` (local
    azimuth clockwise from true north and elevation at the emitter, degrees).
    x_e: emitter phase centre(s), ECEF m, ``[..., 3]``. orbits: ``dict[sat_id, Orbit]``.
    t_s: Unix seconds (scalar). Returns ``[..., 3]`` or None.
    """
    x = _vec3(x_e, "x_e")
    t = _scalar_time(t_s)
    pointing = _attr(emitter, "pointing", "emitter", "omni")
    pointing = getattr(pointing, "root", pointing)  # a pydantic RootModel around the union
    if isinstance(pointing, str):
        if pointing != "omni":
            raise ValueError(
                f"emitter.pointing must be 'omni', {{satellite_id}} or {{az_deg, el_deg}}, got {pointing!r}"
            )
        return None
    target_id = _attr(pointing, "satellite_id", "emitter.pointing", None)
    if target_id is not None:
        target = orbit_position(_lookup(orbits, target_id, "emitter.pointing.satellite_id"), t)
        los = target - x
        return los / np.linalg.norm(los, axis=-1, keepdims=True)
    az = _attr(pointing, "az_deg", "emitter.pointing", None)
    el = _attr(pointing, "el_deg", "emitter.pointing", None)
    if az is None or el is None:
        raise ValueError("emitter.pointing must be 'omni', {satellite_id} or {az_deg, el_deg}")
    lat, lon, _ = ecef_to_lla(x)
    return azel_to_ecef(lat, lon, _number(az, "emitter.pointing.az_deg"), _number(el, "emitter.pointing.el_deg"))


def path_budget(
    config: Any,
    emitter: Any,
    f_e_hz: float,
    x_e: Any,
    sat_id: str,
    transponder_id: str,
    channel_id: str,
    t_s: float,
    other_pfd_dbw_m2: Sequence[float] | np.ndarray = (),
) -> PathBudget | None:
    """Budget of the path emitter -> ``transponder_id`` of ``sat_id`` -> ``channel_id``.

    config: the system configuration, read through ``satellite(id)``,
    ``channel(id) -> (site, channel)``, ``site_xyz(site_id)``, ``orbits()`` and
    ``boresight_ecef(channel_id, t_s)``. emitter: an ``EmitterModel``-like object with
    ``eirp_dbw`` (on boresight, dBW), ``antenna`` and ``pointing`` (``"omni"``,
    ``{satellite_id}`` or ``{az_deg, el_deg}``). f_e_hz: carrier frequency radiated,
    Hz. x_e: emitter phase centre, ECEF m, ``[3]`` or ``[..., 3]``. t_s: Unix seconds
    (a scalar; the satellite is placed at its geometric position at ``t_s``).
    other_pfd_dbw_m2: flux densities at the satellite of the other carriers sharing
    the transponder, dBW/m^2, for power robbing.

    Returns ``None`` when the path does not exist: ``f_e_hz`` outside the
    transponder's uplink passband, the satellite not above the site's horizon, or
    (for a single ``x_e``) not above the emitter's horizon. For a batch of emitter
    positions the result fields are arrays over the leading axes of ``x_e``, and the
    positions that do not see the satellite carry ``-inf`` (no power) in every power
    and ratio, with ``uplink.elevation_deg <= 0`` saying why.
    """
    f_e = _positive(f_e_hz, "f_e_hz")
    t = _scalar_time(t_s)
    x = _vec3(x_e, "x_e")
    satellite = config.satellite(sat_id)
    transponder = _transponder(satellite, transponder_id)
    site, channel = config.channel(channel_id)
    f_uc = _positive(_attr(transponder, "uplink_center_hz", "transponder"), "transponder.uplink_center_hz")
    bandwidth = _positive(_attr(transponder, "bandwidth_hz", "transponder"), "transponder.bandwidth_hz")
    translation = _number(_attr(transponder, "translation_hz", "transponder"), "transponder.translation_hz")
    if abs(f_e - f_uc) > 0.5 * bandwidth:
        return None  # not relayed: the carrier centre is outside the uplink passband

    orbits = config.orbits()
    sat_r = orbit_position(_lookup(orbits, sat_id, "sat_id"), t)
    site_xyz = np.asarray(config.site_xyz(_attr(site, "id", "site")), dtype=np.float64)
    net, site_n0, site_elevation, off_axis = _downlink_terms(
        f_e + translation, sat_r, site_xyz, channel, config.boresight_ecef(channel_id, t)
    )
    if not float(site_elevation) > 0.0:
        return None  # the satellite is below the site's horizon

    up = uplink_budget(
        _attr(emitter, "eirp_dbw", "emitter"),
        _attr(emitter, "antenna", "emitter", None),
        emitter_boresight(emitter, x, orbits, t),
        x,
        sat_r,
        f_e,
        transponder,
    )
    visible = np.asarray(up.elevation_deg) > 0.0
    if x.ndim == 1 and not bool(visible):
        return None  # the satellite is below the emitter's horizon

    own_pfd = np.where(visible, up.pfd_dbw_m2, -np.inf)
    others = np.asarray(other_pfd_dbw_m2, dtype=np.float64).reshape(-1)
    if np.any(np.isnan(others)) or np.any(others == np.inf):
        raise ValueError("other_pfd_dbw_m2 must be finite (or -inf for an absent carrier)")
    pfd = np.concatenate(
        [own_pfd[..., None], np.broadcast_to(others, own_pfd.shape + others.shape)], axis=-1
    )
    out = transponder_outputs(pfd, transponder)
    eirp_c = np.asarray(out.eirp_down_dbw)[..., 0]
    down = DownlinkBudget(
        c_rx_dbw=_scalar(eirp_c + net),
        relayed_n0_dbw_hz=_scalar(np.asarray(out.relayed_n0_eirp_dbw_hz) + net),
        site_n0_dbw_hz=site_n0,
        elevation_deg=_scalar(site_elevation),
        off_axis_deg=_scalar(off_axis),
    )
    uplink = UplinkBudget(
        eirp_toward_sat_dbw=_scalar(np.where(visible, up.eirp_toward_sat_dbw, -np.inf)),
        pfd_dbw_m2=_scalar(own_pfd),
        cn0_up_dbhz=_scalar(np.where(visible, up.cn0_up_dbhz, -np.inf)),
        elevation_deg=up.elevation_deg,
    )
    return PathBudget(
        uplink=uplink,
        transponder_eirp_dbw=_scalar(eirp_c),
        downlink=down,
        cn0_total_dbhz=_scalar(np.asarray(down.c_rx_dbw) - down.n0_total_dbw_hz),
    )


def downlink_quality_db(
    config: Any, channel_id: str, sat_id: str, transponder_id: str, t_s: float
) -> float | None:
    """Downlink quality ``q`` of a channel on a transponder (MATH.md section 7), dB.

    ``q = G_site(off-axis toward the satellite) - FSPL(f_d, rho_d) - L_pol
    - 10 log10(N0_site + N0_relay)`` with a linear sum inside the logarithm and
    ``f_d`` the transponder's downlink centre: the C/N0 (dBHz) the channel would see
    per dBW of carrier EIRP leaving the satellite. Differences of ``q`` between
    channels predict how much weaker or stronger the same signal appears.

    config: as for ``path_budget``. t_s: Unix seconds (scalar). Returns ``None`` when
    the satellite is not above the site's horizon.
    """
    down = _quiet_downlink(config, channel_id, sat_id, transponder_id, t_s)
    if down is None:
        return None
    return float(down.c_rx_dbw - down.n0_total_dbw_hz)


def relay_rise_db(config: Any, channel_id: str, sat_id: str, transponder_id: str, t_s: float) -> float | None:
    """Rise of a channel's noise floor inside a transponder's downlink passband, dB.

    ``R = 10 log10(1 + N0_relay / N0_site)`` (MATH.md section 7) with the transponder
    carrying no carriers, evaluated at its downlink centre. config: as for
    ``path_budget``. t_s: Unix seconds (scalar). Returns ``None`` when the satellite is
    not above the site's horizon (its noise does not reach the channel at all).
    """
    down = _quiet_downlink(config, channel_id, sat_id, transponder_id, t_s)
    if down is None:
        return None
    return float(down.relay_rise_db)


def _quiet_downlink(
    config: Any, channel_id: str, sat_id: str, transponder_id: str, t_s: float
) -> DownlinkBudget | None:
    """Downlink of an idle transponder (noise only) into a channel, for a 0 dBW carrier;
    ``None`` when the satellite is not above the site's horizon."""
    t = _scalar_time(t_s)
    satellite = config.satellite(sat_id)
    transponder = _transponder(satellite, transponder_id)
    site, channel = config.channel(channel_id)
    sat_r = orbit_position(_lookup(config.orbits(), sat_id, "sat_id"), t)
    site_xyz = np.asarray(config.site_xyz(_attr(site, "id", "site")), dtype=np.float64)
    f_d = _positive(
        _number(_attr(transponder, "uplink_center_hz", "transponder"), "transponder.uplink_center_hz")
        + _number(_attr(transponder, "translation_hz", "transponder"), "transponder.translation_hz"),
        "transponder downlink center",
    )
    noise = transponder_outputs(np.empty(0), transponder)
    down = downlink_budget(
        0.0,
        noise.relayed_n0_eirp_dbw_hz,
        f_d,
        sat_r,
        site_xyz,
        channel,
        config.boresight_ecef(channel_id, t),
    )
    if not float(down.elevation_deg) > 0.0:
        return None
    return down


# --------------------------------------------------------------------------- internals


def _lin(db: Any) -> Any:
    """Decibels to a linear power ratio."""
    return np.power(10.0, 0.1 * np.asarray(db, dtype=np.float64))


def _db(linear: Any) -> Any:
    """Linear power ratio to decibels (0 maps to -inf without a warning)."""
    with np.errstate(divide="ignore"):
        return 10.0 * np.log10(np.asarray(linear, dtype=np.float64))


def _scalar(value: Any) -> Any:
    """0-d arrays become numpy scalars; other arrays pass through."""
    arr = np.asarray(value)
    return arr[()] if arr.ndim == 0 else arr


def _elevation_deg(x: np.ndarray, target: np.ndarray) -> Any:
    """Elevation of ``target`` above the plane normal to the WGS84 vertical at ``x`` (both
    ECEF m, ``[..., 3]``, broadcast), degrees; geometric, no refraction."""
    lat_deg, lon_deg, _ = ecef_to_lla(x)
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    cos_lat = np.cos(lat)
    up = np.stack([cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)], axis=-1)
    los = target - x
    sine = np.sum(los * up, axis=-1) / np.linalg.norm(los, axis=-1)
    return np.degrees(np.arcsin(np.clip(sine, -1.0, 1.0)))


def _off_axis(boresight: Any, direction: np.ndarray, name: str) -> Any:
    """Angle between a boresight (None = on axis) and ``direction`` (``[..., 3]``), degrees."""
    if boresight is None:
        return np.zeros(direction.shape[:-1])
    axis = _vec3(boresight, name)
    if np.any(np.linalg.norm(axis, axis=-1) <= 0.0):
        raise ValueError(f"{name} must be a non-zero vector")
    return np.asarray(angle_between_deg(axis, direction))


def _pattern_db(antenna: Any, boresight: Any, direction: np.ndarray, freq: Any, name: str) -> Any:
    """Gain toward ``direction`` relative to the boresight gain, dB (0 for "omni")."""
    if boresight is None:
        return np.zeros(direction.shape[:-1])
    if antenna is None:
        raise ValueError("antenna is required when a boresight is given")
    off_axis = _off_axis(boresight, direction, name)
    return gain_dbi(antenna, off_axis, freq) - gain_dbi(antenna, 0.0, freq)


def _transponder(satellite: Any, transponder_id: str) -> Any:
    for transponder in _attr(satellite, "transponders", "satellite"):
        if _attr(transponder, "id", "transponder") == transponder_id:
            return transponder
    raise ValueError(
        f"transponder_id={transponder_id!r} is not a transponder of satellite "
        f"{_attr(satellite, 'id', 'satellite', '?')!r}"
    )


def _lookup(table: Mapping[str, Any], key: Any, key_name: str) -> Any:
    try:
        return table[key]
    except (KeyError, TypeError):
        raise ValueError(f"{key_name}={key!r} is not a satellite of config.orbits()") from None


def _attr(obj: Any, name: str, label: str, default: Any = _MISSING) -> Any:
    """Read ``name`` from a mapping or an attribute object; ValueError naming it when absent."""
    value = obj.get(name, _MISSING) if isinstance(obj, Mapping) else getattr(obj, name, _MISSING)
    if value is _MISSING or (value is None and default is not _MISSING):
        if default is _MISSING:
            raise ValueError(f"{label}.{name} is required")
        return default
    return value


def _number(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None
    if not np.isfinite(out):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def _positive(value: Any, name: str) -> float:
    out = _number(value, name)
    if out <= 0.0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return out


def _finite_array(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite")
    return arr


def _positive_array(value: Any, name: str) -> np.ndarray:
    arr = _finite_array(value, name)
    if np.any(arr <= 0.0):
        raise ValueError(f"{name} must be positive")
    return arr


def _nonnegative_array(value: Any, name: str) -> np.ndarray:
    arr = _finite_array(value, name)
    if np.any(arr < 0.0):
        raise ValueError(f"{name} must be non-negative")
    return arr


def _vec3(value: Any, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 0 or arr.shape[-1] != 3:
        raise ValueError(f"{name} must have shape [..., 3], got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite")
    return arr


def _scalar_time(t_s: Any) -> float:
    t = np.asarray(t_s, dtype=np.float64)
    if t.ndim != 0 or not np.isfinite(t):
        raise ValueError("t_s must be a finite scalar (Unix seconds)")
    return float(t)
