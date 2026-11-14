"""core/linkbudget.py against MATH.md section 4 "Amplitudes" and section 7.

The reference numbers are hand calculations written out below; power robbing is checked
by brute force in watts; the horizon and pattern geometry use independent routes
(``geodesy.look_angles``, explicit rotations).
"""
from __future__ import annotations

import ast
import math
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import numpy as np
import pytest

from petal_locate.core import linkbudget as lb
from petal_locate.core.antennas import gain_dbi
from petal_locate.core.constants import C, K_DBW, WGS84_A
from petal_locate.core.geodesy import angle_between_deg, azel_to_ecef, lla_to_ecef, look_angles, rz
from petal_locate.core.orbits import GeoOrbit, orbit_position

REPO = Path(__file__).resolve().parents[1]
GEO_RADIUS_M = 42164172.0
T0 = 1767225600.0  # 2026-01-01T00:00:00Z


def transponder(**changes: Any) -> NS:
    fields = dict(
        id="x1",
        uplink_center_hz=301.0e6,
        bandwidth_hz=25e3,
        translation_hz=-41.0e6,
        g_over_t_dbk=-14.0,
        sfd_dbw_m2=-136.0,
        eirp_sat_dbw=26.0,
        group_delay_s=0.0,
    )
    fields.update(changes)
    return NS(**fields)


def beam(gain: float = 10.0, hpbw: float = 52.0, fb: float = 20.0) -> NS:
    return NS(type="beam", gain_dbi=gain, hpbw_deg=hpbw, front_to_back_db=fb)


def channel(**changes: Any) -> NS:
    fields = dict(id="ch", antenna=beam(), system_noise_temp_k=400.0, polarization_loss_db=3.0)
    fields.update(changes)
    return NS(**fields)


class FixedOrbit:
    """A satellite parked at one ECEF position (stand-in for ``core.orbits`` classes)."""

    def __init__(self, r_m: Any) -> None:
        self.r = np.asarray(r_m, dtype=np.float64)

    def state_ecef(self, t_s: Any) -> tuple[np.ndarray, np.ndarray]:
        shape = np.shape(t_s) + (3,)
        return np.broadcast_to(self.r, shape).copy(), np.zeros(shape)


class FakeConfig:
    """The documented ``SystemConfig`` helpers and nothing else (no pydantic, no config package)."""

    def __init__(self, satellites: dict[str, NS], orbits: dict[str, Any], sites: dict[str, NS]) -> None:
        self._satellites, self._orbits, self._sites = satellites, orbits, sites

    def satellite(self, sat_id: str) -> NS:
        return self._satellites[sat_id]

    def channel(self, channel_id: str) -> tuple[NS, NS]:
        for site in self._sites.values():
            for ch in site.channels:
                if ch.id == channel_id:
                    return site, ch
        raise ValueError(f"channel_id {channel_id!r} unknown")

    def site_xyz(self, site_id: str) -> np.ndarray:
        return self._sites[site_id].xyz

    def orbits(self) -> dict[str, Any]:
        return dict(self._orbits)

    def boresight_ecef(self, channel_id: str, t_s: float) -> np.ndarray:
        site, ch = self.channel(channel_id)
        return ch.boresight


# Geometry of the hand calculation: emitter on the equator directly under the satellite,
# site on the equator 20 degrees east, site antenna aimed 10 degrees off the satellite.
X_E = np.array([WGS84_A, 0.0, 0.0])
SAT = np.array([GEO_RADIUS_M, 0.0, 0.0])
SITE = lla_to_ecef(0.0, 20.0, 0.0)
SITE_LOS = (SAT - SITE) / np.linalg.norm(SAT - SITE)
SITE_BORESIGHT = rz(np.radians(10.0)) @ SITE_LOS


def fake_config(tp: NS | None = None, ch: NS | None = None, sat: Any = SAT) -> FakeConfig:
    ch = ch or channel()
    ch.boresight = SITE_BORESIGHT
    site = NS(id="site", xyz=SITE, channels=[ch])
    satellite = NS(id="sat", transponders=[tp or transponder()])
    return FakeConfig({"sat": satellite}, {"sat": FixedOrbit(sat)}, {"site": site})


OMNI = NS(eirp_dbw=20.0, antenna=NS(type="isotropic", gain_dbi=0.0), pointing="omni")


# --------------------------------------------------------------------------- basics


def test_fspl_matches_the_engineering_formula() -> None:
    # 32.4478 + 20 log10(d / km) + 20 log10(f / MHz)
    expected = 32.447783 + 20.0 * math.log10(38000.0) + 20.0 * math.log10(300.0)
    assert lb.fspl_db(38.0e6, 300e6) == pytest.approx(expected, abs=1e-5)
    assert lb.fspl_db(38.0e6, 300e6) == pytest.approx(173.586, abs=1e-3)
    d = np.array([1e3, 1e6])[:, None]
    f = np.array([1e8, 1e9, 1e10])[None, :]
    out = lb.fspl_db(d, f)
    assert out.shape == (2, 3)
    assert out[1, 1] - out[0, 1] == pytest.approx(60.0, abs=1e-9)  # 20 dB per decade of distance
    assert out[0, 2] - out[0, 1] == pytest.approx(20.0, abs=1e-9)  # and of frequency
    with pytest.raises(ValueError, match="dist_m"):
        lb.fspl_db(0.0, 1e9)
    with pytest.raises(ValueError, match="freq_hz"):
        lb.fspl_db(1.0, -1e9)


def test_core_modules_never_import_the_config_package() -> None:
    for name in ("linkbudget.py", "antennas.py", "iono.py"):
        tree = ast.parse((REPO / "src" / "petal_locate" / "core" / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any("config" in alias.name for alias in node.names), name
            elif isinstance(node, ast.ImportFrom):
                assert "config" not in (node.module or ""), name
                assert not any(alias.name == "config" for alias in node.names), name


# --------------------------------------------------------------------------- the hand calculation


def test_path_budget_against_hand_calculation() -> None:
    """20 dBW omni emitter under the satellite, 301 MHz up, 260 MHz down.

    Uplink, rho_u = 42 164 172 - 6 378 137 = 35 786 035 m:
      spreading   10 log10(4 pi rho_u^2) = 10.9921 + 151.0743            = 162.0664 dB(m^2)
      PFD         20 - 162.0664                                          = -142.0664 dBW/m^2
      FSPL_u      20 log10(4 pi * 35 786 035 * 301e6 / c)                = 173.0934 dB
      C/N0_up     20 - 173.0934 + (-14) + 228.6                          = 61.5066 dBHz
    Transponder (SFD -136 dBW/m^2, saturated EIRP 26 dBW, G/T -14 dB/K, 25 kHz):
      EIRP_c      26 - (-136 - (-142.0664)) = 26 - 6.0664                = 19.9336 dBW  (98.48 W)
      EIRP_n0     26 + 136 + 10 log10(4 pi (301e6 / c)^2) + 14 - 228.6
                  = 162 + 11.0270 - 214.6                                = -41.5730 dBW/Hz
      noise power 10^(-4.15730) * 25e3                                   = 1.740 W
      total       98.48 + 1.74 = 100.22 W = 20.0097 dBW, backoff 26 - 20.0097 = 5.9903 dB (linear)
    Downlink to a site 20 degrees east on the equator:
      rho_d       sqrt(R^2 + a^2 - 2 R a cos 20)                         = 36 236 405.6 m
      elevation   atan((cos 20 - R / a) / sin 20)                        = 66.549 deg
      FSPL_d      20 log10(4 pi * 36 236 405.6 * 260e6 / c)              = 171.9302 dB
      G_site      10 - 12 (10 / 52)^2   (beam, 10 degrees off axis)      = 9.5562 dBi
      C_rx        19.9336 - 171.9302 + 9.5562 - 3 (L_pol)                = -145.4403 dBW
      N0_relay    -41.5730 - 171.9302 + 9.5562 - 3                       = -206.9469 dBW/Hz
      N0_site     -228.6 + 10 log10(400)                                 = -202.5794 dBW/Hz
      N0 total    10 log10(10^-20.25794 + 10^-20.69469)                  = -201.2255 dBW/Hz
      C/N0        -145.4403 + 201.2255                                   = 55.7852 dBHz
      SNR in 25 kHz  55.7852 - 43.9794                                   = 11.8058 dB
    """
    budget = lb.path_budget(fake_config(), OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
    assert budget is not None
    tol = 2e-4
    assert budget.uplink.eirp_toward_sat_dbw == pytest.approx(20.0, abs=1e-12)
    assert budget.uplink.pfd_dbw_m2 == pytest.approx(-142.0664, abs=tol)
    assert budget.uplink.cn0_up_dbhz == pytest.approx(61.5066, abs=tol)
    assert budget.uplink.elevation_deg == pytest.approx(90.0, abs=1e-6)
    assert budget.transponder_eirp_dbw == pytest.approx(19.9336, abs=tol)
    assert budget.downlink.c_rx_dbw == pytest.approx(-145.4403, abs=tol)
    assert budget.downlink.relayed_n0_dbw_hz == pytest.approx(-206.9469, abs=tol)
    assert budget.downlink.site_n0_dbw_hz == pytest.approx(-202.5794, abs=tol)
    assert budget.downlink.elevation_deg == pytest.approx(66.549, abs=1e-3)
    assert budget.downlink.off_axis_deg == pytest.approx(10.0, abs=1e-9)
    assert budget.downlink.n0_total_dbw_hz == pytest.approx(-201.2255, abs=tol)
    assert budget.downlink.relay_rise_db == pytest.approx(-201.2255 + 202.5794, abs=2 * tol)
    assert budget.cn0_total_dbhz == pytest.approx(55.7852, abs=tol)
    assert budget.snr_db(25e3) == pytest.approx(11.8058, abs=tol)

    # the same total as the textbook cascade of uplink and downlink C/N0
    cn0_down = budget.downlink.c_rx_dbw - budget.downlink.site_n0_dbw_hz
    cascade = -10.0 * math.log10(10.0 ** (-budget.uplink.cn0_up_dbhz / 10.0) + 10.0 ** (-cn0_down / 10.0))
    assert budget.cn0_total_dbhz == pytest.approx(cascade, abs=1e-9)

    outputs = lb.transponder_outputs([budget.uplink.pfd_dbw_m2], transponder())
    assert outputs.relayed_n0_eirp_dbw_hz == pytest.approx(-41.5730, abs=tol)
    assert outputs.backoff_db == pytest.approx(5.9903, abs=tol)
    assert outputs.compression_db == 0.0


def test_path_budget_is_the_composition_of_the_leg_functions() -> None:
    tp, ch = transponder(), channel()
    up = lb.uplink_budget(20.0, None, None, X_E, SAT, 301.0e6, tp)
    out = lb.transponder_outputs([up.pfd_dbw_m2], tp)
    down = lb.downlink_budget(out.eirp_down_dbw[0], out.relayed_n0_eirp_dbw_hz, 260.0e6, SAT, SITE, ch, SITE_BORESIGHT)
    whole = lb.path_budget(fake_config(tp, ch), OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
    assert whole is not None
    assert whole.uplink.pfd_dbw_m2 == pytest.approx(up.pfd_dbw_m2, abs=1e-12)
    assert whole.transponder_eirp_dbw == pytest.approx(out.eirp_down_dbw[0], abs=1e-12)
    assert whole.downlink.c_rx_dbw == pytest.approx(down.c_rx_dbw, abs=1e-12)
    assert whole.downlink.relayed_n0_dbw_hz == pytest.approx(down.relayed_n0_dbw_hz, abs=1e-12)
    assert whole.downlink.elevation_deg == pytest.approx(down.elevation_deg, abs=1e-12)


# --------------------------------------------------------------------------- transponder noise


def test_transponder_noise_is_a_property_of_the_transponder_alone() -> None:
    tp = transponder()
    # formula written out: EIRP_sat - SFD + 10 log10(4 pi f_uc^2 / c^2) - G/T + k
    expected = 26.0 + 136.0 + 10.0 * math.log10(4.0 * math.pi * (301.0e6 / C) ** 2) + 14.0 + K_DBW
    assert lb.transponder_noise_eirp_dbw_hz(tp) == pytest.approx(expected, abs=1e-12)
    assert lb.transponder_outputs(np.empty(0), tp).relayed_n0_eirp_dbw_hz == pytest.approx(expected, abs=1e-12)
    # no carrier, one weak carrier, several carriers: the density does not move below saturation
    for pfd in ([], [-190.0], [-150.0], [-150.0, -160.0, -170.0]):
        assert lb.transponder_outputs(pfd, tp).relayed_n0_eirp_dbw_hz == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("eirp_dbw", [-10.0, 5.0, 20.0])
@pytest.mark.parametrize("lat_deg,lon_deg", [(0.0, 0.0), (35.0, 40.0), (-60.0, -55.0)])
def test_relayed_noise_equals_carrier_minus_cn0_up_for_any_carrier(eirp_dbw: float, lat_deg: float, lon_deg: float) -> None:
    """EIRP_n0 = EIRP_c - C/N0_up whatever the carrier's EIRP and range (MATH section 4)."""
    tp = transponder()
    x = lla_to_ecef(lat_deg, lon_deg, 30.0)
    up = lb.uplink_budget(eirp_dbw, None, None, x, SAT, tp.uplink_center_hz, tp)
    out = lb.transponder_outputs([up.pfd_dbw_m2], tp)
    assert out.backoff_db > 0.0  # linear
    assert out.eirp_down_dbw[0] - up.cn0_up_dbhz == pytest.approx(out.relayed_n0_eirp_dbw_hz, abs=1e-9)
    assert out.relayed_n0_eirp_dbw_hz == pytest.approx(lb.transponder_noise_eirp_dbw_hz(tp), abs=1e-12)


def test_received_relay_noise_is_independent_of_emitter_eirp_and_range() -> None:
    cfg = fake_config()
    reference = lb.path_budget(cfg, OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
    assert reference is not None
    far = lla_to_ecef(50.0, -45.0, 0.0)  # 3 000 km more range
    assert np.linalg.norm(SAT - far) - np.linalg.norm(SAT - X_E) > 3.0e6
    for eirp, x in [(-10.0, X_E), (5.0, far), (20.0, far), (0.0, lla_to_ecef(-30.0, 60.0, 0.0))]:
        weak = NS(eirp_dbw=eirp, antenna=None, pointing="omni")
        budget = lb.path_budget(cfg, weak, 301.0e6, x, "sat", "x1", "ch", T0)
        assert budget is not None
        assert budget.downlink.relayed_n0_dbw_hz == pytest.approx(reference.downlink.relayed_n0_dbw_hz, abs=1e-12)
        assert budget.downlink.site_n0_dbw_hz == reference.downlink.site_n0_dbw_hz
    # while the carrier itself follows EIRP dB for dB
    quiet = lb.path_budget(cfg, NS(eirp_dbw=-10.0, antenna=None, pointing="omni"), 301.0e6, X_E, "sat", "x1", "ch", T0)
    assert quiet is not None
    assert reference.downlink.c_rx_dbw - quiet.downlink.c_rx_dbw == pytest.approx(30.0, abs=1e-9)


# --------------------------------------------------------------------------- power robbing


def watts(dbw: Any) -> Any:
    return 10.0 ** (np.asarray(dbw, dtype=np.float64) / 10.0)


def test_power_robbing_conserves_total_eirp() -> None:
    tp = transponder()
    saturated_w = watts(tp.eirp_sat_dbw)
    noise_w = watts(lb.transponder_noise_eirp_dbw_hz(tp)) * tp.bandwidth_hz
    pfd = np.array([-133.0, -139.0, -150.0])  # the first alone is 3 dB over SFD
    out = lb.transponder_outputs(pfd, tp)

    # brute force in watts
    linear_w = saturated_w * watts(pfd - tp.sfd_dbw_m2)
    total_w = linear_w.sum() + noise_w
    assert total_w > saturated_w
    scale = saturated_w / total_w
    assert watts(out.eirp_down_dbw) == pytest.approx(linear_w * scale, rel=1e-12)
    assert watts(out.relayed_n0_eirp_dbw_hz) * tp.bandwidth_hz == pytest.approx(noise_w * scale, rel=1e-12)
    # carriers plus noise leave with exactly the saturated EIRP
    assert watts(out.eirp_down_dbw).sum() + watts(out.relayed_n0_eirp_dbw_hz) * tp.bandwidth_hz == pytest.approx(
        saturated_w, rel=1e-12
    )
    # every carrier and the noise lose the same number of dB; ratios are untouched
    loss = -10.0 * math.log10(scale)
    assert out.backoff_db == pytest.approx(-loss, abs=1e-12)
    assert out.compression_db == pytest.approx(loss, abs=1e-12)
    assert out.eirp_down_dbw[0] - out.eirp_down_dbw[2] == pytest.approx(17.0, abs=1e-9)
    assert out.relayed_n0_eirp_dbw_hz == pytest.approx(lb.transponder_noise_eirp_dbw_hz(tp) - loss, abs=1e-12)


def test_no_robbing_below_saturation_and_slight_robbing_exactly_at_sfd() -> None:
    tp = transponder()
    noise_w = watts(lb.transponder_noise_eirp_dbw_hz(tp)) * tp.bandwidth_hz
    low = lb.transponder_outputs([-146.0, -150.0], tp)
    assert low.eirp_down_dbw == pytest.approx([16.0, 12.0], abs=1e-12)  # EIRP_sat - (SFD - PFD)
    assert low.backoff_db == pytest.approx(26.0 - 10.0 * math.log10(watts(16.0) + watts(12.0) + noise_w), abs=1e-12)
    assert low.backoff_db > 0.0 and low.compression_db == 0.0
    # one carrier exactly at SFD: the noise power pushes the total over, so the noise counts
    at_sfd = lb.transponder_outputs([-136.0], tp)
    expected_loss = 10.0 * math.log10(1.0 + noise_w / watts(26.0))
    assert at_sfd.compression_db == pytest.approx(expected_loss, abs=1e-12)
    assert 0.0 < expected_loss < 0.05
    assert at_sfd.eirp_down_dbw[0] == pytest.approx(26.0 - expected_loss, abs=1e-12)


def test_noise_alone_can_saturate_a_transponder() -> None:
    tp = transponder(g_over_t_dbk=-45.0)  # absurdly noisy receiver
    noise_w = watts(lb.transponder_noise_eirp_dbw_hz(tp)) * tp.bandwidth_hz
    assert noise_w > watts(26.0)
    out = lb.transponder_outputs([], tp)
    assert out.eirp_down_dbw.shape == (0,)
    assert watts(out.relayed_n0_eirp_dbw_hz) * tp.bandwidth_hz == pytest.approx(watts(26.0), rel=1e-12)


def test_transponder_outputs_batches_over_leading_axes() -> None:
    tp = transponder()
    rng = np.random.default_rng(7)
    pfd = rng.uniform(-160.0, -130.0, size=(4, 5, 3))
    pfd[0, 0, 1] = -np.inf  # an absent carrier
    out = lb.transponder_outputs(pfd, tp)
    assert out.eirp_down_dbw.shape == (4, 5, 3)
    assert np.shape(out.relayed_n0_eirp_dbw_hz) == (4, 5) and np.shape(out.backoff_db) == (4, 5)
    for i, j in np.ndindex(4, 5):
        single = lb.transponder_outputs(pfd[i, j], tp)
        assert out.eirp_down_dbw[i, j] == pytest.approx(single.eirp_down_dbw, abs=1e-12)
        assert out.relayed_n0_eirp_dbw_hz[i, j] == pytest.approx(single.relayed_n0_eirp_dbw_hz, abs=1e-12)
        assert out.backoff_db[i, j] == pytest.approx(single.backoff_db, abs=1e-12)
    assert out.eirp_down_dbw[0, 0, 1] == -np.inf
    with pytest.raises(ValueError, match="pfd_dbw_m2"):
        lb.transponder_outputs([float("nan")], tp)


def test_other_carriers_rob_power_in_path_budget() -> None:
    cfg = fake_config()
    alone = lb.path_budget(cfg, OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
    shared = lb.path_budget(cfg, OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0, other_pfd_dbw_m2=[-134.0, -140.0])
    assert alone is not None and shared is not None
    tp = transponder()
    noise_w = watts(lb.transponder_noise_eirp_dbw_hz(tp)) * tp.bandwidth_hz
    total_w = watts(26.0) * (watts(alone.uplink.pfd_dbw_m2 + 136.0) + watts(2.0) + watts(-4.0)) + noise_w
    loss = 10.0 * math.log10(total_w / watts(26.0))
    assert loss > 2.0
    assert alone.transponder_eirp_dbw - shared.transponder_eirp_dbw == pytest.approx(loss, abs=1e-9)
    assert alone.downlink.relayed_n0_dbw_hz - shared.downlink.relayed_n0_dbw_hz == pytest.approx(loss, abs=1e-9)
    assert shared.uplink.cn0_up_dbhz == pytest.approx(alone.uplink.cn0_up_dbhz, abs=1e-12)  # robbing is after the receiver
    # carrier and relayed noise drop together, the site noise does not: C/N0 falls by less than the loss
    assert 0.0 < alone.cn0_total_dbhz - shared.cn0_total_dbhz < loss


# --------------------------------------------------------------------------- patterns and polarisation


def test_polarisation_loss_applies_on_the_downlink_only() -> None:
    tp = transponder()
    lossless = lb.path_budget(fake_config(tp, channel(polarization_loss_db=0.0)), OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
    lossy = lb.path_budget(fake_config(tp, channel(polarization_loss_db=3.0)), OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
    assert lossless is not None and lossy is not None
    assert lossless.uplink.cn0_up_dbhz == lossy.uplink.cn0_up_dbhz
    assert lossless.uplink.pfd_dbw_m2 == lossy.uplink.pfd_dbw_m2
    assert lossless.transponder_eirp_dbw == lossy.transponder_eirp_dbw
    assert lossless.downlink.c_rx_dbw - lossy.downlink.c_rx_dbw == pytest.approx(3.0, abs=1e-12)
    assert lossless.downlink.relayed_n0_dbw_hz - lossy.downlink.relayed_n0_dbw_hz == pytest.approx(3.0, abs=1e-12)
    assert lossless.downlink.site_n0_dbw_hz == lossy.downlink.site_n0_dbw_hz
    assert 0.0 < lossless.cn0_total_dbhz - lossy.cn0_total_dbhz < 3.0


def test_uplink_antenna_pattern_and_pointing() -> None:
    tp = transponder()
    los = (SAT - X_E) / np.linalg.norm(SAT - X_E)
    antenna = beam(gain=12.0, hpbw=40.0, fb=25.0)
    on_axis = lb.uplink_budget(20.0, antenna, los, X_E, SAT, 301.0e6, tp)
    assert on_axis.eirp_toward_sat_dbw == pytest.approx(20.0, abs=1e-12)  # eirp_dbw is the boresight EIRP
    for off in (10.0, 20.0, 90.0, 180.0):
        boresight = rz(np.radians(off)) @ los
        tilted = lb.uplink_budget(20.0, antenna, boresight, X_E, SAT, 301.0e6, tp)
        expected = 20.0 - min(12.0 * (off / 40.0) ** 2, 25.0)  # main lobe, floored by the front-to-back ratio
        assert tilted.eirp_toward_sat_dbw == pytest.approx(expected, abs=1e-9)
        assert on_axis.pfd_dbw_m2 - tilted.pfd_dbw_m2 == pytest.approx(20.0 - expected, abs=1e-9)
        assert on_axis.cn0_up_dbhz - tilted.cn0_up_dbhz == pytest.approx(20.0 - expected, abs=1e-9)
    # "omni": the antenna is ignored
    omni = lb.uplink_budget(20.0, antenna, None, X_E, SAT, 301.0e6, tp)
    assert omni.eirp_toward_sat_dbw == 20.0
    with pytest.raises(ValueError, match="antenna"):
        lb.uplink_budget(20.0, None, los, X_E, SAT, 301.0e6, tp)


def test_emitter_boresight_forms() -> None:
    orbits = {"sat": FixedOrbit(SAT), "other": FixedOrbit(rz(np.radians(60.0)) @ SAT)}
    x = lla_to_ecef(25.0, 10.0, 5.0)
    assert lb.emitter_boresight(NS(pointing="omni"), x, orbits, T0) is None
    assert lb.emitter_boresight(NS(), x, orbits, T0) is None
    aimed = lb.emitter_boresight(NS(pointing=NS(satellite_id="other")), x, orbits, T0)
    target = orbits["other"].r - x
    assert np.linalg.norm(aimed) == pytest.approx(1.0, abs=1e-15)
    assert angle_between_deg(aimed, target) == pytest.approx(0.0, abs=1e-6)
    fixed = lb.emitter_boresight({"pointing": {"az_deg": 135.0, "el_deg": 30.0}}, x, orbits, T0)
    assert fixed == pytest.approx(azel_to_ecef(25.0, 10.0, 135.0, 30.0), abs=1e-9)
    az, el, _ = look_angles(x, x + 1000.0 * fixed)
    assert (az, el) == pytest.approx((135.0, 30.0), abs=1e-6)
    with pytest.raises(ValueError, match="emitter.pointing.satellite_id"):
        lb.emitter_boresight(NS(pointing=NS(satellite_id="nope")), x, orbits, T0)
    with pytest.raises(ValueError, match="emitter.pointing"):
        lb.emitter_boresight(NS(pointing="up"), x, orbits, T0)

    # an emitter aimed at another satellite reaches this one through its pattern
    cfg = fake_config()
    cfg._orbits["other"] = orbits["other"]
    emitter = NS(eirp_dbw=20.0, antenna=beam(12.0, 40.0, 25.0), pointing=NS(satellite_id="other"))
    budget = lb.path_budget(cfg, emitter, 301.0e6, X_E, "sat", "x1", "ch", T0)
    off = float(angle_between_deg(orbits["other"].r - X_E, SAT - X_E))
    assert budget is not None and off > 60.0
    assert budget.uplink.eirp_toward_sat_dbw == pytest.approx(
        20.0 + float(gain_dbi(emitter.antenna, off, 301.0e6)) - 12.0, abs=1e-9
    )


def test_pointed_emitters_batch_over_positions() -> None:
    cfg = fake_config()
    cfg._orbits["other"] = FixedOrbit(rz(np.radians(40.0)) @ SAT)
    lat = np.array([-20.0, 0.0, 30.0])[:, None]
    lon = np.array([-30.0, 5.0, 25.0, 50.0])[None, :]
    x = lla_to_ecef(lat, lon, 0.0)
    antenna = beam(14.0, 30.0, 22.0)
    for pointing in (NS(satellite_id="sat"), NS(satellite_id="other"), NS(az_deg=95.0, el_deg=40.0)):
        emitter = NS(eirp_dbw=17.0, antenna=antenna, pointing=pointing)
        batch = lb.path_budget(cfg, emitter, 301.0e6, x, "sat", "x1", "ch", T0)
        assert batch is not None and batch.cn0_total_dbhz.shape == (3, 4)
        for i, j in np.ndindex(3, 4):
            single = lb.path_budget(cfg, emitter, 301.0e6, x[i, j], "sat", "x1", "ch", T0)
            assert single is not None
            assert batch.uplink.eirp_toward_sat_dbw[i, j] == pytest.approx(single.uplink.eirp_toward_sat_dbw, abs=1e-9)
            assert batch.cn0_total_dbhz[i, j] == pytest.approx(single.cn0_total_dbhz, abs=1e-9)
    # aimed at the relaying satellite itself: full EIRP from everywhere
    aimed = lb.path_budget(cfg, NS(eirp_dbw=17.0, antenna=antenna, pointing=NS(satellite_id="sat")), 301.0e6, x, "sat", "x1", "ch", T0)
    assert aimed is not None and aimed.uplink.eirp_toward_sat_dbw == pytest.approx(np.full((3, 4), 17.0), abs=1e-9)
    # a fixed azimuth and elevation hits the satellite squarely only where the geometry agrees
    fixed = lb.path_budget(cfg, NS(eirp_dbw=17.0, antenna=antenna, pointing=NS(az_deg=95.0, el_deg=40.0)), 301.0e6, x, "sat", "x1", "ch", T0)
    assert fixed is not None
    az, el, _ = look_angles(x, SAT)
    off = angle_between_deg(azel_to_ecef(lat, lon, 95.0, 40.0), SAT - x)
    expected = 17.0 - np.minimum(12.0 * (off / 30.0) ** 2, 22.0)
    assert fixed.uplink.eirp_toward_sat_dbw == pytest.approx(expected, abs=1e-9)
    assert np.ptp(fixed.uplink.eirp_toward_sat_dbw) > 10.0


def test_site_pattern_weights_the_downlink() -> None:
    ch = channel(polarization_loss_db=0.0)
    on_axis = lb.downlink_budget(10.0, -50.0, 260e6, SAT, SITE, ch, SITE_LOS)
    for off in (10.0, 30.0, 100.0):
        tilted = lb.downlink_budget(10.0, -50.0, 260e6, SAT, SITE, ch, rz(np.radians(off)) @ SITE_LOS)
        loss = min(12.0 * (off / 52.0) ** 2, 20.0)
        assert tilted.off_axis_deg == pytest.approx(off, abs=1e-9)
        assert on_axis.c_rx_dbw - tilted.c_rx_dbw == pytest.approx(loss, abs=1e-9)
        assert on_axis.relayed_n0_dbw_hz - tilted.relayed_n0_dbw_hz == pytest.approx(loss, abs=1e-9)
        assert tilted.site_n0_dbw_hz == on_axis.site_n0_dbw_hz
    # no boresight: boresight gain whatever the direction
    assert lb.downlink_budget(10.0, -50.0, 260e6, SAT, SITE, ch, None).c_rx_dbw == pytest.approx(on_axis.c_rx_dbw, abs=1e-12)
    # N0_site = k T_sys
    assert on_axis.site_n0_dbw_hz == pytest.approx(K_DBW + 10.0 * math.log10(400.0), abs=1e-12)
    cold = lb.downlink_budget(10.0, -50.0, 260e6, SAT, SITE, channel(system_noise_temp_k=100.0), SITE_LOS)
    assert on_axis.site_n0_dbw_hz - cold.site_n0_dbw_hz == pytest.approx(10.0 * math.log10(4.0), abs=1e-12)
    # arrays of carriers broadcast
    many = lb.downlink_budget(np.array([10.0, 0.0, -np.inf]), -50.0, 260e6, SAT, SITE, ch, SITE_LOS)
    assert many.c_rx_dbw[0] - many.c_rx_dbw[1] == pytest.approx(10.0, abs=1e-12) and many.c_rx_dbw[2] == -np.inf


def test_elevations_agree_with_look_angles() -> None:
    tp = transponder()
    lat = np.array([-70.0, -20.0, 0.0, 35.0, 60.0, 85.0])[:, None]
    lon = np.array([-150.0, -80.0, -20.0, 0.0, 45.0, 100.0, 179.0])[None, :]
    x = lla_to_ecef(lat, lon, 120.0)
    up = lb.uplink_budget(20.0, None, None, x, SAT, 301.0e6, tp)
    assert up.elevation_deg.shape == (6, 7) and up.pfd_dbw_m2.shape == (6, 7)
    assert up.elevation_deg == pytest.approx(look_angles(x, SAT)[1], abs=1e-9)
    assert (up.elevation_deg > 0).any() and (up.elevation_deg < 0).any()
    site = lla_to_ecef(48.0, 11.0, 500.0)
    down = lb.downlink_budget(10.0, -50.0, 260e6, SAT, site, channel(), None)
    assert down.elevation_deg == pytest.approx(float(look_angles(site, SAT)[1]), abs=1e-9)


# --------------------------------------------------------------------------- the horizon rule


def test_path_budget_returns_none_for_paths_that_do_not_exist() -> None:
    cfg = fake_config()
    assert lb.path_budget(cfg, OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0) is not None
    # emitter on the far side of the Earth: the satellite is below its horizon
    antipode = lla_to_ecef(0.0, 180.0, 0.0)
    assert float(look_angles(antipode, SAT)[1]) < 0.0
    assert lb.path_budget(cfg, OMNI, 301.0e6, antipode, "sat", "x1", "ch", T0) is None
    # site that cannot see the satellite, whatever its antenna pattern
    hidden = fake_config(sat=rz(np.radians(150.0)) @ SAT)
    assert float(look_angles(SITE, hidden.orbits()["sat"].r)[1]) < 0.0
    emitter_side = rz(np.radians(150.0)) @ X_E
    assert lb.path_budget(hidden, OMNI, 301.0e6, emitter_side, "sat", "x1", "ch", T0) is None
    assert lb.downlink_quality_db(hidden, "ch", "sat", "x1", T0) is None
    # carrier centre outside the uplink passband: not relayed (edges are inside)
    assert lb.path_budget(cfg, OMNI, 301.0e6 + 12.5e3, X_E, "sat", "x1", "ch", T0) is not None
    assert lb.path_budget(cfg, OMNI, 301.0e6 + 12.6e3, X_E, "sat", "x1", "ch", T0) is None
    assert lb.path_budget(cfg, OMNI, 300.9e6, X_E, "sat", "x1", "ch", T0) is None


def test_path_budget_batches_over_emitter_positions() -> None:
    cfg = fake_config()
    lat = np.linspace(-80.0, 80.0, 9)[:, None]
    lon = np.linspace(-170.0, 170.0, 18)[None, :]
    x = lla_to_ecef(lat, lon, 0.0)
    batch = lb.path_budget(cfg, OMNI, 301.0e6, x, "sat", "x1", "ch", T0, other_pfd_dbw_m2=[-150.0])
    assert batch is not None
    assert batch.cn0_total_dbhz.shape == (9, 18)
    visible = look_angles(x, SAT)[1] > 0.0
    assert visible.any() and (~visible).any()
    for i, j in np.ndindex(9, 18):
        single = lb.path_budget(cfg, OMNI, 301.0e6, x[i, j], "sat", "x1", "ch", T0, other_pfd_dbw_m2=[-150.0])
        if visible[i, j]:
            assert single is not None
            assert batch.cn0_total_dbhz[i, j] == pytest.approx(single.cn0_total_dbhz, abs=1e-9)
            assert batch.uplink.cn0_up_dbhz[i, j] == pytest.approx(single.uplink.cn0_up_dbhz, abs=1e-9)
            assert batch.downlink.c_rx_dbw[i, j] == pytest.approx(single.downlink.c_rx_dbw, abs=1e-9)
        else:
            assert single is None
            assert batch.cn0_total_dbhz[i, j] == -np.inf
            assert batch.uplink.pfd_dbw_m2[i, j] == -np.inf and batch.downlink.c_rx_dbw[i, j] == -np.inf
    # the relayed noise is there whether or not a given emitter position is visible
    assert np.all(np.isfinite(batch.downlink.relayed_n0_dbw_hz))
    assert not np.any(np.isnan(batch.cn0_total_dbhz))


def test_horizon_rule_on_the_pacific_configuration() -> None:
    from petal_locate.config import EmitterModel, load_config

    cfg = load_config(REPO / "config" / "pacific-uhf.yaml")
    emitter = EmitterModel(eirp_dbw=20.0, bandwidth_hz=20e3)
    t_s = T0 + 3 * 3600.0
    near_japan = lla_to_ecef(30.37, 140.41, 10.0)
    assert lb.path_budget(cfg, emitter, 301.0e6, near_japan, "muos-io", "shared-1", "tokyo-io", t_s) is not None
    # Hawaii cannot see 75 E: no downlink there, even into a channel that looks at another satellite
    assert lb.path_budget(cfg, emitter, 301.0e6, near_japan, "muos-io", "shared-1", "honolulu-pac", t_s) is None
    assert lb.downlink_quality_db(cfg, "honolulu-pac", "muos-io", "shared-1", t_s) is None
    # and an emitter in Hawaii is not relayed by 75 E
    honolulu = cfg.site_xyz("honolulu")
    assert lb.path_budget(cfg, emitter, 301.0e6, honolulu, "muos-io", "shared-1", "tokyo-io", t_s) is None
    assert lb.path_budget(cfg, emitter, 301.0e6, honolulu, "muos-pac", "shared-1", "tokyo-pac", t_s) is not None
    # a "solo" channel frequency is not relayed by the shared transponder
    assert lb.path_budget(cfg, emitter, 301.5e6, near_japan, "muos-pac", "shared-1", "tokyo-pac", t_s) is None
    assert lb.path_budget(cfg, emitter, 301.5e6, near_japan, "muos-pac", "solo-pac", "tokyo-pac", t_s) is not None
    with pytest.raises(ValueError, match="transponder_id"):
        lb.path_budget(cfg, emitter, 301.5e6, near_japan, "muos-io", "solo-pac", "tokyo-io", t_s)
    with pytest.raises(ValueError, match="t_s"):
        lb.path_budget(cfg, emitter, 301.0e6, near_japan, "muos-pac", "shared-1", "tokyo-pac", np.array([t_s, t_s + 1]))


# --------------------------------------------------------------------------- downlink quality


def test_downlink_quality_is_the_section_7_formula() -> None:
    from petal_locate.config import load_config

    cfg = load_config(REPO / "config" / "pacific-uhf.yaml")
    t_s = T0 + 7200.0
    for channel_id, sat_id, tp_id in [
        ("tokyo-pac", "muos-pac", "shared-1"),
        ("tokyo-pac", "muos-io", "shared-2"),  # co-channel satellite far off boresight
        ("seoul-pac", "uhf-172e", "solo-172e"),  # beam antenna with polarisation loss
        ("naha-io", "muos-io", "solo-io"),
    ]:
        site, ch = cfg.channel(channel_id)
        sat = cfg.satellite(sat_id)
        tp = sat.transponder(tp_id)
        orbit = GeoOrbit(sat.orbit.lon_deg, sat.orbit.inclination_deg, sat.orbit.epoch, sat.orbit.phase_deg)
        sat_r = orbit_position(orbit, t_s)
        site_xyz = lla_to_ecef(site.lat_deg, site.lon_deg, site.alt_m + site.antenna_height_m)
        tracked = cfg.satellite(ch.pointing.satellite_id).orbit
        tracked_r = orbit_position(GeoOrbit(tracked.lon_deg, tracked.inclination_deg, tracked.epoch, tracked.phase_deg), t_s)
        off_axis = float(angle_between_deg(tracked_r - site_xyz, sat_r - site_xyz))
        f_d = tp.uplink_center_hz + tp.translation_hz
        rho_d = float(np.linalg.norm(sat_r - site_xyz))
        fspl = 20.0 * math.log10(4.0 * math.pi * rho_d * f_d / C)
        net = float(gain_dbi(ch.antenna, off_axis, f_d)) - fspl - ch.polarization_loss_db
        n0_eirp = 26.0 + 136.0 + 10.0 * math.log10(4.0 * math.pi * (tp.uplink_center_hz / C) ** 2) + 14.0 + K_DBW
        n0_site_w = 10.0 ** ((K_DBW + 10.0 * math.log10(ch.system_noise_temp_k)) / 10.0)
        n0_relay_w = 10.0 ** ((n0_eirp + net) / 10.0)
        expected = net - 10.0 * math.log10(n0_site_w + n0_relay_w)
        assert lb.downlink_quality_db(cfg, channel_id, sat_id, tp_id, t_s) == pytest.approx(expected, abs=1e-9)


def test_relay_rise_of_an_idle_transponder() -> None:
    """R = 10 log10(1 + N0_relay / N0_site); in the hand calculation above
    N0_relay - N0_site = -206.9469 + 202.5794 = -4.3675 dB -> R = 10 log10(1 + 0.36581) = 1.3539 dB."""
    cfg = fake_config()
    assert lb.relay_rise_db(cfg, "ch", "sat", "x1", T0) == pytest.approx(1.3539, abs=2e-4)
    budget = lb.path_budget(cfg, OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
    assert budget is not None
    assert lb.relay_rise_db(cfg, "ch", "sat", "x1", T0) == pytest.approx(budget.downlink.relay_rise_db, abs=1e-12)
    # a quieter receiver sees the relayed noise stand out more; a deaf antenna direction sees none of it
    cold = fake_config(ch=channel(system_noise_temp_k=100.0))
    assert lb.relay_rise_db(cold, "ch", "sat", "x1", T0) == pytest.approx(10.0 * math.log10(1.0 + 4.0 * 0.36581), abs=1e-3)
    hidden = fake_config(sat=rz(np.radians(150.0)) @ SAT)
    assert lb.relay_rise_db(hidden, "ch", "sat", "x1", T0) is None
    with pytest.raises(ValueError, match="transponder_id"):
        lb.relay_rise_db(cfg, "ch", "sat", "x9", T0)


def test_quality_difference_predicts_cn0_difference_between_channels() -> None:
    """q_A - q_B is how much stronger the same signal is in channel A than in B (MATH section 7)."""
    from petal_locate.config import EmitterModel, load_config

    cfg = load_config(REPO / "config" / "pacific-uhf.yaml")
    emitter = EmitterModel(eirp_dbw=15.0, bandwidth_hz=10e3)
    x = lla_to_ecef(20.0, 135.0, 0.0)
    t_s = T0 + 5000.0
    watching = [ch.id for _, ch in cfg.channels_watching("uhf-172e")]
    assert len(watching) == 7
    cn0 = {}
    quality = {}
    for channel_id in watching:
        budget = lb.path_budget(cfg, emitter, 301.125e6, x, "uhf-172e", "shared-2", channel_id, t_s)
        assert budget is not None
        cn0[channel_id] = float(budget.cn0_total_dbhz)
        quality[channel_id] = lb.downlink_quality_db(cfg, channel_id, "uhf-172e", "shared-2", t_s)
    first = watching[0]
    spread = [cn0[c] - cn0[first] for c in watching]
    assert max(spread) - min(spread) > 1.0  # the channels really differ
    for channel_id in watching:
        assert cn0[channel_id] - cn0[first] == pytest.approx(quality[channel_id] - quality[first], abs=1e-9)


# --------------------------------------------------------------------------- direct path


def local_point(site_lat: float, site_lon: float, bearing_deg: float, dist_m: float, height_m: float) -> np.ndarray:
    """A point ``dist_m`` along the ground from a site (flat-earth offset in the local ENU frame)."""
    east = dist_m * math.sin(math.radians(bearing_deg))
    north = dist_m * math.cos(math.radians(bearing_deg))
    dlat = math.degrees(north / 6_371_000.0)
    dlon = math.degrees(east / (6_371_000.0 * math.cos(math.radians(site_lat))))
    return lla_to_ecef(site_lat + dlat, site_lon + dlon, height_m)


def test_direct_path_hand_calculation_and_horizon() -> None:
    """260 MHz, 10 m and 10 m antennas: horizon 4.12 km * (sqrt 10 + sqrt 10) = 26.057 km.

    At exactly 5 km: FSPL = 32.4478 + 20 log10(5) + 20 log10(260) = 94.7267 dB;
    C_rx = 13 (EIRP) - 94.7267 + 2 (isotropic site antenna) - 3 (L_pol) = -82.7267 dBW.
    """
    site = lla_to_ecef(35.0, 139.0, 10.0)
    ch = channel(antenna=NS(type="isotropic", gain_dbi=2.0))
    direction = (local_point(35.0, 139.0, 70.0, 5000.0, 10.0) - site)
    emitter_5km = site + 5000.0 * direction / np.linalg.norm(direction)
    budget = lb.direct_budget(13.0, None, None, emitter_5km, 10.0, site, 10.0, ch, None, 260e6)
    assert budget.in_horizon is True
    assert budget.c_rx_dbw == pytest.approx(-82.7267, abs=2e-4)

    horizon_m = 4120.0 * 2.0 * math.sqrt(10.0)
    assert horizon_m == pytest.approx(26057.2, abs=0.1)
    unit = direction / np.linalg.norm(direction)
    inside = lb.direct_budget(13.0, None, None, site + (horizon_m - 1.0) * unit, 10.0, site, 10.0, ch, None, 260e6)
    outside = lb.direct_budget(13.0, None, None, site + (horizon_m + 1.0) * unit, 10.0, site, 10.0, ch, None, 260e6)
    assert inside.in_horizon is True and math.isfinite(inside.c_rx_dbw)
    assert outside.in_horizon is False and outside.c_rx_dbw == -math.inf
    # taller antennas see farther: 100 m and 25 m -> 4.12 * (10 + 5) = 61.8 km
    tall = lb.direct_budget(13.0, None, None, site + 60e3 * unit, 100.0, site, 25.0, ch, None, 260e6)
    assert tall.in_horizon is True
    assert lb.direct_budget(13.0, None, None, site + 62e3 * unit, 100.0, site, 25.0, ch, None, 260e6).in_horizon is False
    # antennas on the ground have no radio horizon at all
    assert lb.direct_budget(13.0, None, None, emitter_5km, 0.0, site, 0.0, ch, None, 260e6).in_horizon is False


def test_direct_path_patterns_and_batching() -> None:
    site = lla_to_ecef(35.0, 139.0, 10.0)
    ch = channel()  # 10 dBi beam, 3 dB polarisation loss
    bearings = np.array([0.0, 90.0, 180.0, 270.0])
    distances = np.array([2e3, 10e3, 40e3])
    x = np.array([[local_point(35.0, 139.0, b, d, 10.0) for d in distances] for b in bearings])
    site_boresight = azel_to_ecef(35.0, 139.0, 90.0, 0.0)  # looking east along the ground
    emitter_antenna = beam(8.0, 60.0, 15.0)
    toward_site = (site - x) / np.linalg.norm(site - x, axis=-1, keepdims=True)
    out = lb.direct_budget(10.0, emitter_antenna, toward_site, x, 10.0, site, 10.0, ch, site_boresight, 260e6)
    assert out.c_rx_dbw.shape == (4, 3) and out.in_horizon.shape == (4, 3)
    assert out.in_horizon.tolist() == [[True, True, False]] * 4
    assert np.all(out.c_rx_dbw[:, 2] == -np.inf)
    for i, j in np.ndindex(4, 2):
        dist = float(np.linalg.norm(site - x[i, j]))
        off = float(angle_between_deg(site_boresight, x[i, j] - site))
        expected = 10.0 - 20.0 * math.log10(4.0 * math.pi * dist * 260e6 / C) + float(gain_dbi(ch.antenna, off, 260e6)) - 3.0
        assert out.c_rx_dbw[i, j] == pytest.approx(expected, abs=1e-9)
    # the emitter to the east is on the site's boresight, the one to the west in its back lobe
    assert out.c_rx_dbw[1, 0] - out.c_rx_dbw[3, 0] == pytest.approx(20.0, abs=0.05)
    # turning the emitter's beam away from the site costs its pattern
    away = lb.direct_budget(10.0, emitter_antenna, -toward_site, x, 10.0, site, 10.0, ch, site_boresight, 260e6)
    assert out.c_rx_dbw[0, 0] - away.c_rx_dbw[0, 0] == pytest.approx(15.0, abs=1e-9)


def test_direct_path_rejects_the_near_field() -> None:
    site = lla_to_ecef(35.0, 139.0, 10.0)
    with pytest.raises(ValueError, match="x_e"):
        lb.direct_budget(0.0, None, None, site + np.array([0.3, 0.0, 0.0]), 10.0, site, 10.0, channel(), None, 260e6)
    with pytest.raises(ValueError, match="h_e_m"):
        lb.direct_budget(0.0, None, None, site + np.array([3000.0, 0.0, 0.0]), -1.0, site, 10.0, channel(), None, 260e6)


# --------------------------------------------------------------------------- errors


def test_invalid_input_names_the_field() -> None:
    tp, ch = transponder(), channel()
    with pytest.raises(ValueError, match="x_e"):
        lb.uplink_budget(20.0, None, None, [1.0, 2.0], SAT, 301e6, tp)
    with pytest.raises(ValueError, match="f_e_hz"):
        lb.uplink_budget(20.0, None, None, X_E, SAT, 0.0, tp)
    with pytest.raises(ValueError, match="eirp_dbw"):
        lb.uplink_budget(float("nan"), None, None, X_E, SAT, 301e6, tp)
    with pytest.raises(ValueError, match="transponder.g_over_t_dbk"):
        lb.uplink_budget(20.0, None, None, X_E, SAT, 301e6, NS())
    with pytest.raises(ValueError, match="transponder.bandwidth_hz"):
        lb.transponder_outputs([-150.0], transponder(bandwidth_hz=0.0))
    with pytest.raises(ValueError, match="transponder.sfd_dbw_m2"):
        lb.transponder_outputs([-150.0], NS(eirp_sat_dbw=26.0, uplink_center_hz=3e8, bandwidth_hz=1e3, g_over_t_dbk=0.0))
    with pytest.raises(ValueError, match="channel.system_noise_temp_k"):
        lb.downlink_budget(10.0, -50.0, 260e6, SAT, SITE, channel(system_noise_temp_k=0.0), None)
    with pytest.raises(ValueError, match="channel.antenna"):
        lb.downlink_budget(10.0, -50.0, 260e6, SAT, SITE, NS(system_noise_temp_k=300.0), None)
    with pytest.raises(ValueError, match="f_d_hz"):
        lb.downlink_budget(10.0, -50.0, -1.0, SAT, SITE, ch, None)
    with pytest.raises(ValueError, match="eirp_down_dbw"):
        lb.downlink_budget(float("nan"), -50.0, 260e6, SAT, SITE, ch, None)
    with pytest.raises(ValueError, match="bandwidth_hz"):
        budget = lb.path_budget(fake_config(), OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0)
        assert budget is not None
        budget.snr_db(0.0)
    with pytest.raises(ValueError, match="other_pfd_dbw_m2"):
        lb.path_budget(fake_config(), OMNI, 301.0e6, X_E, "sat", "x1", "ch", T0, other_pfd_dbw_m2=[float("nan")])
    with pytest.raises(ValueError, match="transponder_id"):
        lb.path_budget(fake_config(), OMNI, 301.0e6, X_E, "sat", "nope", "ch", T0)
