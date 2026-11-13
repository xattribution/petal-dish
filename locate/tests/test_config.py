"""config/models.py, config/loader.py and the shipped example configurations."""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from petal_locate.config import (
    ConfigError,
    EmitterModel,
    Monitor,
    ReceiveChannel,
    Region,
    SystemConfig,
    TrackPointing,
    config_json_schema,
    derive_monitors,
    dump_config,
    field_errors,
    fit_monitor_frame,
    load_config,
    loads_config,
)
from petal_locate.core.antennas import electrically_small
from petal_locate.core.constants import C
from petal_locate.core.geodesy import angle_between_deg, azel_to_ecef, lla_to_ecef, look_angles
from petal_locate.core.orbits import GeoOrbit, KeplerOrbit, TleOrbit, orbit_position
from petal_locate.core.types import PathSpec, Uncertainty

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / "config"
T0 = 1767225600.0  # 2026-01-01T00:00:00Z
SIDEREAL_DAY_S = 86164.1
ISS_L1 = "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
ISS_L2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"
DELETE = object()


def tp(ident: str = "x1", f_up: float = 301.0e6, bw: float = 25e3, translation: float = -41.0e6) -> dict[str, Any]:
    return {
        "id": ident,
        "uplink_center_hz": f_up,
        "bandwidth_hz": bw,
        "translation_hz": translation,
        "g_over_t_dbk": -14.0,
        "sfd_dbw_m2": -136.0,
        "eirp_sat_dbw": 26.0,
    }


def base() -> dict[str, Any]:
    """A small valid configuration as plain data."""
    return {
        "name": "unit",
        "satellites": [
            {
                "id": "sat-a",
                "name": "A",
                "orbit": {"type": "geo", "lon_deg": 140.0, "inclination_deg": 3.0},
                "transponders": [tp("x1", 301.0e6), tp("x2", 301.1e6)],
            },
            {
                "id": "sat-b",
                "name": "B",
                "orbit": {"type": "geo", "lon_deg": 160.0},
                "transponders": [tp("x1", 301.0e6, translation=-33.6e6)],
            },
        ],
        "sites": [
            {
                "id": "north",
                "name": "North",
                "lat_deg": 35.0,
                "lon_deg": 139.0,
                "channels": [
                    {
                        "id": "north-a",
                        "antenna": {"type": "parabolic", "diameter_m": 1.2},
                        "pointing": {"satellite_id": "sat-a"},
                    },
                    {
                        "id": "north-wide",
                        "antenna": {"type": "beam", "gain_dbi": 8.0, "hpbw_deg": 70.0},
                        "pointing": {"az_deg": 170.0, "el_deg": 45.0, "satellite_ids": ["sat-a", "sat-b"]},
                    },
                ],
            },
            {
                "id": "south",
                "name": "South",
                "lat_deg": 14.0,
                "lon_deg": 121.0,
                "alt_m": 50.0,
                "antenna_height_m": 20.0,
                "clock": {"sigma_ns": 80.0},
                "channels": [
                    {"id": "south-b", "antenna": {"type": "isotropic"}, "pointing": {"satellite_id": "sat-b"}},
                ],
            },
        ],
        "carriers": [
            {
                "id": "car-1",
                "name": "Carrier 1",
                "satellite_id": "sat-a",
                "transponder_id": "x1",
                "uplink_center_hz": 301.0e6,
                "bandwidth_hz": 10e3,
                "location": {"lat_deg": 13.5, "lon_deg": 144.7},
                "emitter": {
                    "eirp_dbw": 10.0,
                    "antenna": {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 50.0},
                    "pointing": {"satellite_id": "sat-a"},
                    "bandwidth_hz": 8e3,
                },
                "use_as_reference": True,
            }
        ],
    }


def edited(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    data = base()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


def locations(exc: ValidationError) -> list[str]:
    """Paths of the fields at fault, as they appear in the configuration document."""
    return [error["loc"] for error in field_errors(exc)]


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return SystemConfig.model_validate(base())


@pytest.fixture(scope="module")
def pacific() -> SystemConfig:
    return load_config(CONFIG_DIR / "pacific-uhf.yaml")


@pytest.fixture(scope="module")
def ku() -> SystemConfig:
    return load_config(CONFIG_DIR / "ku-adjacent.yaml")


@pytest.fixture(scope="module")
def leo() -> SystemConfig:
    return load_config(CONFIG_DIR / "leo-example.yaml")


# --------------------------------------------------------------------------- defaults


def test_defaults_match_the_architecture_document(cfg: SystemConfig) -> None:
    assert cfg.version == 1 and cfg.description == "" and cfg.dut1_s == 0.0
    north = cfg.site("north")
    assert (north.alt_m, north.antenna_height_m, north.clock.sigma_ns) == (0.0, 10.0, 30.0)
    channel = north.channels[0]
    assert (channel.name, channel.system_noise_temp_k, channel.polarization_loss_db) == ("", 400.0, 0.0)
    assert channel.max_sample_rate_hz == 2e6
    assert channel.antenna.efficiency == 0.55
    assert north.channels[1].antenna.front_to_back_db == 20.0
    assert cfg.site("south").channels[0].antenna.gain_dbi == 0.0
    orbit = cfg.satellite("sat-b").orbit
    assert (orbit.inclination_deg, orbit.epoch, orbit.phase_deg, orbit.drift_deg_per_day) == (
        0.0,
        "2026-01-01T00:00:00Z",
        0.0,
        0.0,
    )
    assert cfg.satellite("sat-a").transponders[0].group_delay_s == 0.0
    assert cfg.satellite("sat-a").norad_id is None and cfg.satellite("sat-a").notes == ""
    carrier = cfg.carrier("car-1")
    assert (carrier.location.alt_m, carrier.location.antenna_height_m) == (0.0, 10.0)
    assert carrier.emitter.waveform == "noise"

    p = cfg.processing
    assert p.detection.model_dump() == dict(
        threshold_db=6.0, confirm_m=3, confirm_n=5, release_frames=6, merge_bins=2, baseline_tau_s=600.0,
        power_anomaly_db=6.0, min_site_separation_km=200.0, verdict_settle_s=5.0, sensitivity_margin_db=6.0,
    )  # fmt: skip
    assert p.capture.model_dump() == dict(
        duration_s=1.0, lead_s=0.6, interval_s=5.0, oversample=1.25, min_sample_rate_hz=20e3,
        max_samples=262144, timeout_s=15.0,
    )  # fmt: skip
    assert p.caf.model_dump() == dict(min_snr_db=13.0, max_tdoa_sigma_s=5e-4, doppler_margin_hz=20.0, delay_margin_s=20e-6)
    assert p.solver.model_dump() == dict(
        height_m=0.0, min_elevation_deg=0.0, confidence=0.95, grid_step_deg=1.0, n_starts=16,
        chi2_ambiguity=9.0, max_measurements=400, max_reduced_chi2=5.0,
    )  # fmt: skip
    assert p.uncertainty.model_dump() == dict(
        ephemeris=dict(
            operator=dict(pos_m=[100.0, 1000.0, 1000.0], vel_mps=[0.02, 0.02, 0.05]),
            tle=dict(pos_m=[2000.0, 20000.0, 10000.0], vel_mps=[0.3, 0.3, 0.7]),
        ),
        sat_osc_frac=1e-6, site_osc_frac=1e-11, tdoa_bias_ns=50.0,
        iono_vtec_tecu=30.0, iono_correlation=0.5, tropo_zenith_m=2.4, tropo_correlation=0.9,
    )  # fmt: skip
    assert p.calibration.model_dump() == dict(enabled=True, interval_s=30.0, max_age_s=300.0)
    assert p.region.model_dump() == dict(lat_min=-70.0, lat_max=70.0, lon_min=-180.0, lon_max=180.0)

    monitor = Monitor(id="m", center_hz=260e6, span_hz=1e6)
    assert (monitor.nfft, monitor.navg, monitor.frame_rate_hz) == (1024, 8, 2.0)
    emitter = EmitterModel(eirp_dbw=3.0, bandwidth_hz=1e3)
    assert emitter.antenna.type == "isotropic" and emitter.antenna.gain_dbi == 0.0
    assert emitter.pointing == "omni" and emitter.waveform == "noise"


# --------------------------------------------------------------------------- validation names the field


@pytest.mark.parametrize(
    ("path", "value", "loc", "fragment"),
    [
        (("satellites", 0, "transponders", 0, "bandwidth_hz"), 0.0, "satellites.0.transponders.0.bandwidth_hz", "greater than 0"),
        (("satellites", 0, "transponders", 1, "bandwidth_hz"), -5.0, "satellites.0.transponders.1.bandwidth_hz", "greater than 0"),
        (("satellites", 0, "transponders", 0, "uplink_center_hz"), "fast", "satellites.0.transponders.0.uplink_center_hz", "number"),
        (("satellites", 0, "transponders", 0, "translation_hz"), -400e6, "satellites.0.transponders.0", "translation_hz"),
        (("satellites", 0, "transponders", 0, "g_over_t_dbk"), DELETE, "satellites.0.transponders.0.g_over_t_dbk", "required"),
        (("satellites", 0, "bogus"), 1, "satellites.0.bogus", "Extra inputs"),
        (("satellites", 0, "orbit", "type"), "sgp", "satellites.0.orbit", "tle"),
        (("satellites", 0, "orbit", "lon_deg"), 200.0, "satellites.0.orbit.lon_deg", "less than or equal to 180"),
        (("satellites", 0, "orbit", "epoch"), "yesterday", "satellites.0.orbit.epoch", "ISO-8601"),
        (("satellites", 0, "orbit", "drift_deg_per_day"), -400.0, "satellites.0.orbit", "drift_deg_per_day"),
        (("satellites", 0, "ephemeris_sigma"), {"pos_m": [1, 2], "vel_mps": [1, 2, 3]}, "satellites.0.ephemeris_sigma.pos_m", "3 items"),
        (("satellites", 0, "ephemeris_sigma"), {"pos_m": [1, 2, -3], "vel_mps": [1, 2, 3]}, "satellites.0.ephemeris_sigma.pos_m.2", "greater than or equal to 0"),
        (("sites", 0, "lat_deg"), 91.0, "sites.0.lat_deg", "less than or equal to 90"),
        (("sites", 1, "lon_deg"), -181.0, "sites.1.lon_deg", "greater than or equal to -180"),
        (("sites", 1, "clock", "sigma_ns"), -1.0, "sites.1.clock.sigma_ns", "greater than or equal to 0"),
        (("sites", 0, "id"), "bad id", "sites.0.id", "pattern"),
        (("sites", 0, "id"), "../etc", "sites.0.id", "pattern"),
        (("sites", 0, "channels", 0, "system_noise_temp_k"), 0.0, "sites.0.channels.0.system_noise_temp_k", "greater than 0"),
        (("sites", 0, "channels", 0, "max_sample_rate_hz"), float("nan"), "sites.0.channels.0.max_sample_rate_hz", "finite"),
        (("sites", 0, "channels", 0, "polarization_loss_db"), -3.0, "sites.0.channels.0.polarization_loss_db", "greater than or equal to 0"),
        (("sites", 0, "channels", 0, "antenna"), {"type": "parabolic"}, "sites.0.channels.0.antenna.diameter_m", "required"),
        (("sites", 0, "channels", 0, "antenna"), {"type": "dish", "diameter_m": 1.0}, "sites.0.channels.0.antenna", "parabolic"),
        (("sites", 0, "channels", 0, "antenna"), {"type": "table", "theta_deg": [0, 10], "gain_dbi": [3.0]}, "sites.0.channels.0.antenna", "gain_dbi"),
        (("sites", 0, "channels", 0, "antenna"), {"type": "table", "theta_deg": [10, 5], "gain_dbi": [3.0, 1.0]}, "sites.0.channels.0.antenna", "theta_deg"),
        (("sites", 0, "channels", 0, "pointing"), "omni", "sites.0.channels.0.pointing", "satellite_id"),
        (("sites", 0, "channels", 0, "pointing", "satellite_id"), "nope", "sites.0.channels.0.pointing.satellite_id", "unknown satellite id 'nope'"),
        (("sites", 0, "channels", 1, "pointing", "satellite_ids"), ["sat-a", "nope"], "sites.0.channels.1.pointing.satellite_ids.1", "unknown satellite id 'nope'"),
        (("sites", 0, "channels", 1, "pointing", "satellite_ids"), [], "sites.0.channels.1.pointing.satellite_ids", "at least 1"),
        (("sites", 0, "channels", 1, "pointing", "satellite_ids"), ["sat-a", "sat-a"], "sites.0.channels.1.pointing", "satellite_ids"),
        (("sites", 0, "channels", 1, "pointing", "el_deg"), 95.0, "sites.0.channels.1.pointing.el_deg", "less than or equal to 90"),
        (("carriers", 0, "satellite_id"), "nope", "carriers.0.satellite_id", "unknown satellite id 'nope'"),
        (("carriers", 0, "transponder_id"), "x9", "carriers.0.transponder_id", "unknown transponder id 'x9'"),
        (("carriers", 0, "uplink_center_hz"), 305.0e6, "carriers.0.uplink_center_hz", "outside the uplink passband"),
        (("carriers", 0, "location"), DELETE, "carriers.0", "use_as_reference: a reference carrier needs location"),
        (("carriers", 0, "emitter"), DELETE, "carriers.0", "use_as_reference: a reference carrier needs emitter"),
        (("carriers", 0, "emitter", "bandwidth_hz"), 20e3, "carriers.0", "emitter.bandwidth_hz"),
        (("carriers", 0, "emitter", "bandwidth_hz"), 0.0, "carriers.0.emitter", "bandwidth_hz"),
        (("carriers", 0, "emitter", "waveform"), "fm", "carriers.0.emitter.waveform", "'cw', 'noise', 'chirp' or 'psk'"),
        (("carriers", 0, "emitter", "pointing"), "up", "carriers.0.emitter.pointing", "omni"),
        (("carriers", 0, "emitter", "pointing", "satellite_id"), "nope", "carriers.0.emitter.pointing.satellite_id", "unknown satellite id 'nope'"),
        (("carriers", 0, "location", "lat_deg"), -95.0, "carriers.0.location.lat_deg", "greater than or equal to -90"),
        (("name",), "", "name", "at least 1 character"),
        (("dut1_s",), 5.0, "dut1_s", "less than or equal to 1"),
        (("processing",), {"solver": {"grid_step": 1.0}}, "processing.solver.grid_step", "Extra inputs"),
        (("processing",), {"solver": {"confidence": 1.0}}, "processing.solver.confidence", "less than 1"),
        (("processing",), {"detection": {"confirm_m": 6}}, "processing.detection", "confirm_m"),
        (("processing",), {"capture": {"max_samples": 1.5}}, "processing.capture.max_samples", "integer"),
        (("processing",), {"region": {"lon_min": 10.0, "lon_max": 10.0}}, "processing.region", "lon_max"),
        (("processing",), {"region": {"lon_min": 180.0, "lon_max": -180.0}}, "processing.region", "lon_max"),
        (("processing",), {"region": {"lat_min": 20.0, "lat_max": 20.0}}, "processing.region", "lat_max"),
        (("processing",), {"uncertainty": {"sat_osc_frac": -1e-6}}, "processing.uncertainty.sat_osc_frac", "greater than or equal to 0"),
    ],
)
def test_validation_failure_names_the_field(path: tuple[Any, ...], value: Any, loc: str, fragment: str) -> None:
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(edited(path, value))
    assert isinstance(info.value, ValueError)
    assert loc in locations(info.value), locations(info.value)
    assert fragment in str(info.value)


def test_missing_top_level_sections_are_named() -> None:
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate({"name": "x"})
    assert set(locations(info.value)) == {"satellites", "sites"}


def test_several_cross_reference_errors_are_reported_together() -> None:
    data = base()
    data["sites"][0]["channels"][0]["pointing"]["satellite_id"] = "ghost"
    data["carriers"][0]["transponder_id"] = "x9"
    data["sites"][1]["id"] = "north"
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(data)
    assert set(locations(info.value)) == {
        "sites.0.channels.0.pointing.satellite_id",
        "carriers.0.transponder_id",
        "sites.1.id",
    }


def test_orbit_validation_tle_kepler_table() -> None:
    def with_orbit(orbit: dict[str, Any]) -> dict[str, Any]:
        return edited(("satellites", 0, "orbit"), orbit)

    good = SystemConfig.model_validate(with_orbit({"type": "tle", "line1": ISS_L1 + "\n", "line2": ISS_L2 + "  "}))
    assert good.satellite("sat-a").orbit.line1 == ISS_L1  # trailing blanks and newlines are dropped
    assert isinstance(good.orbits()["sat-a"], TleOrbit)

    bad_checksum = ISS_L1[:-1] + "8"
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_orbit({"type": "tle", "line1": bad_checksum, "line2": ISS_L2}))
    assert "satellites.0.orbit.line1" in locations(info.value) and "checksum" in str(info.value)
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_orbit({"type": "tle", "line1": ISS_L1, "line2": ISS_L2[:60]}))
    assert "satellites.0.orbit.line2" in locations(info.value) and "69 characters" in str(info.value)
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_orbit({"type": "tle", "line1": ISS_L2, "line2": ISS_L2}))
    assert "satellites.0.orbit.line1" in locations(info.value)
    other = "2 25545" + ISS_L2[7:68]
    other += str((sum(int(ch) for ch in other if ch.isdigit()) + other.count("-")) % 10)
    with pytest.raises(ValidationError, match="catalogue number"):
        SystemConfig.model_validate(with_orbit({"type": "tle", "line1": ISS_L1, "line2": other}))

    kepler = dict(type="kepler", a_m=7178137.0, e=0.001, inc_deg=60.0, raan_deg=10.0, argp_deg=0.0,
                  mean_anomaly_deg=0.0, epoch="2026-01-01T00:00:00Z")  # fmt: skip
    ok = SystemConfig.model_validate(with_orbit(kepler))
    assert ok.satellite("sat-a").orbit.j2 is True and isinstance(ok.orbits()["sat-a"], KeplerOrbit)
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_orbit({**kepler, "e": 0.2}))  # perigee 5 742 km from the centre
    assert "perigee" in str(info.value) and "satellites.0.orbit" in locations(info.value)
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_orbit({**kepler, "e": 1.0}))
    assert "satellites.0.orbit.e" in locations(info.value)
    missing_epoch = {k: v for k, v in kepler.items() if k != "epoch"}
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_orbit(missing_epoch))
    assert "satellites.0.orbit.epoch" in locations(info.value)

    def sample(t: str) -> dict[str, Any]:
        return {"t": t, "r_m": [42164172.0, 0.0, 0.0], "v_mps": [0.0, 0.0, 0.0]}

    table = {"type": "table", "samples": [sample("2026-01-01T00:00:00Z"), sample("2026-01-01T00:01:00Z")]}
    assert SystemConfig.model_validate(with_orbit(table)).satellite("sat-a").orbit.type == "table"
    for times in (["2026-01-01T00:01:00Z", "2026-01-01T00:00:00Z"], ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"]):
        with pytest.raises(ValidationError) as info:
            SystemConfig.model_validate(with_orbit({"type": "table", "samples": [sample(t) for t in times]}))
        assert "samples[1].t" in str(info.value) and "strictly increasing" in str(info.value)
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_orbit({"type": "table", "samples": [sample("2026-01-01T00:00:00Z")]}))
    assert "satellites.0.orbit.samples" in locations(info.value)


def test_times_are_normalised_including_yaml_timestamps() -> None:
    aware = dt.datetime(2026, 3, 1, 9, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    for given in ("2026-03-01T00:00:00Z", "2026-03-01 09:00:00+09:00", aware, dt.datetime(2026, 3, 1), dt.date(2026, 3, 1)):
        model = SystemConfig.model_validate(edited(("satellites", 0, "orbit", "epoch"), given))
        assert model.satellite("sat-a").orbit.epoch == "2026-03-01T00:00:00Z"
    # an unquoted YAML timestamp arrives as a datetime and still loads
    text = yaml.safe_dump(base()).replace("lon_deg: 160.0", "lon_deg: 160.0\n    epoch: 2026-03-01T00:00:00Z")
    assert "epoch: 2026-03-01T00:00:00Z" in text
    assert loads_config(text).satellite("sat-b").orbit.epoch == "2026-03-01T00:00:00Z"


# --------------------------------------------------------------------------- id uniqueness scopes


def test_ids_unique_across_the_whole_configuration() -> None:
    cases = {
        ("satellites", 1, "id"): ("sat-a", "satellites.1.id", "satellite id 'sat-a' is already used at satellites.0"),
        ("sites", 1, "id"): ("north", "sites.1.id", "site id 'north' is already used at sites.0"),
        # channel ids are unique across sites, not just within one
        ("sites", 1, "channels", 0, "id"): (
            "north-a",
            "sites.1.channels.0.id",
            "channel id 'north-a' is already used at sites.0.channels.0",
        ),
        ("sites", 0, "channels", 1, "id"): ("north-a", "sites.0.channels.1.id", "channel id 'north-a'"),
        ("satellites", 0, "transponders", 1, "id"): ("x1", "satellites.0.transponders.1.id", "transponder"),
    }
    for path, (value, loc, fragment) in cases.items():
        with pytest.raises(ValidationError) as info:
            SystemConfig.model_validate(edited(path, value))
        assert loc in locations(info.value)
        assert fragment in str(info.value)
    data = base()
    data["carriers"].append(copy.deepcopy(data["carriers"][0]))
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(data)
    assert locations(info.value) == ["carriers.1.id"]


def test_transponder_and_monitor_ids_are_scoped_to_their_parent() -> None:
    data = base()
    # the same transponder id on two satellites is the normal case (it is in base())
    assert data["satellites"][0]["transponders"][0]["id"] == data["satellites"][1]["transponders"][0]["id"]
    monitor = {"id": "m1", "center_hz": 260.0e6, "span_hz": 50e3}
    data["sites"][0]["channels"][0]["monitors"] = [dict(monitor)]
    data["sites"][1]["channels"][0]["monitors"] = [dict(monitor)]  # same monitor id in another channel
    model = SystemConfig.model_validate(data)
    assert model.channel("north-a")[1].monitors[0].id == model.channel("south-b")[1].monitors[0].id == "m1"

    data["sites"][0]["channels"][0]["monitors"] = [dict(monitor), {**monitor, "center_hz": 261.0e6}]
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(data)
    assert "sites.0.channels.0" in locations(info.value) and "monitor id 'm1'" in str(info.value)


def test_ids_of_different_kinds_do_not_collide() -> None:
    data = base()
    data["sites"][0]["id"] = "sat-a"  # a site may share an id with a satellite
    data["sites"][1]["channels"][0]["id"] = "south"  # and a channel with its site
    model = SystemConfig.model_validate(data)
    assert model.site("sat-a").name == "North" and model.satellite("sat-a").name == "A"
    assert model.channel("south")[0].id == "south"


@pytest.mark.parametrize("ident", ["a", "MUOS-5", "site_01", "k1.v2", "0x", "a" * 64])
def test_accepted_ids(ident: str) -> None:
    assert SystemConfig.model_validate(edited(("sites", 0, "id"), ident)).sites[0].id == ident


@pytest.mark.parametrize("ident", ["", "-lead", ".hidden", "a|b", "a b", "a/b", "a:b", "é", "a" * 65, 7])
def test_rejected_ids(ident: Any) -> None:
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(edited(("sites", 0, "id"), ident))
    assert "sites.0.id" in locations(info.value)


# --------------------------------------------------------------------------- region


def reference_contains(region: Region, lat: float, lon: float) -> bool:
    """Independent membership test: walk the unit circle from lon_min eastward."""
    if not region.lat_min <= lat <= region.lat_max:
        return False
    if region.lon_min == -180.0 and region.lon_max == 180.0:
        return True
    start = complex(math.cos(math.radians(region.lon_min)), math.sin(math.radians(region.lon_min)))
    stop = complex(math.cos(math.radians(region.lon_max)), math.sin(math.radians(region.lon_max)))
    point = complex(math.cos(math.radians(lon)), math.sin(math.radians(lon)))

    def east_of_start(z: complex) -> float:
        return math.degrees(math.atan2((z / start).imag, (z / start).real)) % 360.0

    return east_of_start(point) <= east_of_start(stop) + 1e-9


def test_default_region_is_the_full_circle() -> None:
    region = Region()
    assert region.is_full_circle and region.lon_span_deg() == 360.0
    assert region.contains(0.0, 123.0) and region.contains(-70.0, -180.0) and region.contains(70.0, 540.0)
    assert not region.contains(70.1, 0.0)
    lat, lon = region.grid(1.0)
    assert lat[0] == -70.0 and lat[-1] == 70.0 and lat.size == 141
    assert lon[0] == -180.0 and lon[-1] == 179.0 and lon.size == 360  # no meridian twice
    lat, lon = region.grid(7.0)
    assert lon.size == 52 and lon[-1] == -180.0 + 51 * 7.0 < 180.0
    assert lat[-1] == -70.0 + 20 * 7.0 == 70.0


def test_pacific_region_wraps_the_antimeridian(pacific: SystemConfig) -> None:
    region = pacific.processing.region
    assert (region.lat_min, region.lat_max, region.lon_min, region.lon_max) == (-60.0, 60.0, 60.0, -120.0)
    assert not region.is_full_circle
    assert region.lon_span_deg() == 180.0  # eastward from 60 E through 180 to 120 W
    inside = [(0.0, 60.0), (0.0, 139.7), (21.3, -157.9), (0.0, 180.0), (0.0, -180.0), (60.0, -120.0), (-60.0, 100.0)]
    outside = [(0.0, 59.9), (0.0, -119.9), (0.0, 0.0), (0.0, -60.0), (60.1, 150.0), (-61.0, 150.0), (10.0, 30.0)]
    for lat, lon in inside:
        assert region.contains(lat, lon), (lat, lon)
        assert region.contains(lat, lon + 360.0) and region.contains(lat, lon - 360.0)  # any 360 degree range
    for lat, lon in outside:
        assert not region.contains(lat, lon), (lat, lon)
        assert not region.contains(lat, lon + 360.0)
    # Honolulu, at 157.858 W, is inside both as -157.858 and unwrapped as 202.142
    assert region.contains(21.307, -157.858) and region.contains(21.307, 202.142)


def test_pacific_region_grid_is_monotonic_and_unwrapped(pacific: SystemConfig) -> None:
    region = pacific.processing.region
    lat, lon = region.grid(1.0)
    assert lat.size == 121 and lat[0] == -60.0 and lat[-1] == 60.0
    assert lon.size == 181 and lon[0] == 60.0 and lon[-1] == 240.0  # 240 = 120 W, not wrapped
    assert np.all(np.diff(lon) > 0.0) and np.all(np.diff(lat) > 0.0)
    assert np.allclose(np.diff(lon), 1.0) and np.max(np.abs(np.diff(lon))) < 180.0
    assert np.array_equal(lon, 60.0 + np.arange(181.0))
    lat2, lon2 = np.meshgrid(lat, lon, indexing="ij")
    assert region.contains(lat2, lon2).all()
    # a step that does not divide the box stops short of the far edges
    lat, lon = region.grid(7.0)
    assert lon[0] == 60.0 and lon[-1] == 60.0 + 25 * 7.0 <= 240.0 and lon.size == 26
    assert lat[-1] == -60.0 + 17 * 7.0 <= 60.0 and lat.size == 18
    lat, lon = region.grid(0.25)
    assert lon.size == 721 and lon[-1] == 240.0 and lat.size == 481 and lat[-1] == 60.0
    for step in (0.0, -1.0, float("nan")):
        with pytest.raises(ValueError, match="step_deg"):
            region.grid(step)


def test_region_contains_matches_an_independent_implementation() -> None:
    rng = np.random.default_rng(11)
    regions = [
        Region(lat_min=-60, lat_max=60, lon_min=60, lon_max=-120),
        Region(lat_min=0, lat_max=60, lon_min=90, lon_max=170),
        Region(lat_min=-10, lat_max=10, lon_min=170, lon_max=-170),
        Region(lat_min=-80, lat_max=80, lon_min=-170, lon_max=170),
        Region(lat_min=-20, lat_max=30, lon_min=-30, lon_max=-40),  # everything but a 10 degree gap
        Region(lat_min=-90, lat_max=90, lon_min=0.0, lon_max=180.0),
        Region(),
    ]
    lat = rng.uniform(-90.0, 90.0, 400)
    lon = rng.uniform(-360.0, 540.0, 400)
    for region in regions:
        got = region.contains(lat, lon)
        assert got.shape == (400,) and got.dtype == bool
        expected = [reference_contains(region, float(a), float(b)) for a, b in zip(lat, lon)]
        assert got.tolist() == expected
        assert 0 < got.sum() < 400 or region.lat_min == -90.0 and region.is_full_circle
    assert regions[2].lon_span_deg() == 20.0 and regions[4].lon_span_deg() == 350.0
    assert regions[5].lon_span_deg() == 180.0 and regions[3].lon_span_deg() == 340.0
    # broadcasting and scalars
    assert regions[0].contains(np.zeros((3, 1)), np.array([100.0, 0.0])).tolist() == [[True, False]] * 3
    assert bool(regions[0].contains(0.0, 100.0)) is True


def test_region_feeds_geometry_measurement_windows(pacific: SystemConfig) -> None:
    from petal_locate.core.geometry import measurement_windows, predict_fdoa, predict_tdoa

    region = pacific.processing.region
    orbits, sites = pacific.orbits(), pacific.sites_xyz()
    a = PathSpec("muos-pac", "tokyo", 301.0e6, -41.0e6)
    b = PathSpec("uhf-172e", "honolulu", 301.0e6, -33.6e6)
    t_s = T0 + 3 * 3600.0
    windows = measurement_windows(a, b, t_s, orbits, sites, region, step_deg=10.0)
    assert windows is not None
    (tdoa_lo, tdoa_hi), (fdoa_lo, fdoa_hi) = windows
    for lat, lon in [(30.37, 140.41), (21.3, -157.9), (-40.0, 175.0), (10.0, 100.0)]:
        assert region.contains(lat, lon)
        x = lla_to_ecef(lat, lon, 0.0)
        assert tdoa_lo <= float(predict_tdoa(x, a, b, t_s, orbits, sites)) <= tdoa_hi
        assert fdoa_lo <= float(predict_fdoa(x, a, b, t_s, orbits, sites)) <= fdoa_hi


# --------------------------------------------------------------------------- monitors


def rule_by_the_book(span_hz: float, frame_rate_hz: float) -> tuple[int, int] | None:
    """The derivation rule, step by step: halve nfft (minimum 64) keeping navg = 8, then
    drop navg one at a time. (ARCHITECTURE section 3 drops navg first; the config keeps
    navg = 8 as long as it can because the detection threshold is calibrated for it.)"""
    nfft, navg = 1024, 8
    while nfft > 64 and nfft * navg / span_hz > 1.0 / frame_rate_hz:
        nfft //= 2
    while navg > 1 and nfft * navg / span_hz > 1.0 / frame_rate_hz:
        navg -= 1
    return (nfft, navg) if nfft * navg / span_hz <= 1.0 / frame_rate_hz else None


def test_fit_monitor_frame_follows_the_reduction_rule() -> None:
    assert fit_monitor_frame(31250.0) == (1024, 8)  # 0.262 s fits 0.5 s
    assert fit_monitor_frame(16384.0) == (1024, 8)  # exactly 0.5 s
    assert fit_monitor_frame(16383.0) == (512, 8)
    assert fit_monitor_frame(6250.0) == (256, 8)  # a 5 kHz transponder
    assert fit_monitor_frame(2048.0) == (128, 8)
    assert fit_monitor_frame(1250.0) == (64, 8)  # a 1 kHz transponder: still 8 averages, 19.5 Hz bins
    assert fit_monitor_frame(1024.0) == (64, 8)  # the narrowest window with 8 averages at 2 frames/s
    assert fit_monitor_frame(1023.0) == (64, 7)  # only now does navg give way
    assert fit_monitor_frame(128.0) == (64, 1)
    with pytest.raises(ValueError, match="too narrow"):
        fit_monitor_frame(127.0)
    assert fit_monitor_frame(1000.0, frame_rate_hz=0.5) == (128, 8)
    assert fit_monitor_frame(2e6, frame_rate_hz=20.0) == (1024, 8)
    rng = np.random.default_rng(3)
    spans = np.concatenate([10.0 ** rng.uniform(1.5, 6.5, 300), [64.0, 128.0, 1024.0, 2048.0, 8192.0, 16384.0]])
    rates = rng.choice([0.5, 1.0, 2.0, 4.0, 10.0], spans.size)
    for span, rate in zip(spans, rates):
        expected = rule_by_the_book(float(span), float(rate))
        if expected is None:
            with pytest.raises(ValueError):
                fit_monitor_frame(float(span), float(rate))
        else:
            assert fit_monitor_frame(float(span), float(rate)) == expected


def test_monitors_are_derived_per_transponder_of_each_watched_satellite(cfg: SystemConfig) -> None:
    _, tracking = cfg.channel("north-a")
    assert tracking.monitors_derived
    assert [m.id for m in tracking.monitors] == ["sat-a:x1", "sat-a:x2"]
    assert [m.center_hz for m in tracking.monitors] == [260.0e6, 260.1e6]  # downlink centres
    assert all(m.span_hz == 31250.0 and (m.nfft, m.navg, m.frame_rate_hz) == (1024, 8, 2.0) for m in tracking.monitors)
    _, fixed = cfg.channel("north-wide")
    assert [m.id for m in fixed.monitors] == ["sat-a:x1", "sat-a:x2", "sat-b:x1"]
    assert fixed.monitors[2].center_hz == 301.0e6 - 33.6e6
    _, south = cfg.channel("south-b")
    assert [m.id for m in south.monitors] == ["sat-b:x1"]
    assert derive_monitors(tracking, cfg.satellites) == tracking.monitors


def test_derived_monitor_span_and_frame_reduction() -> None:
    data = base()
    data["satellites"][0]["transponders"] = [
        tp("wide", 14.25e9, 36e6, -2.3e9),  # wider than the channel can sample
        tp("mid", 14.30e9, 1.8e6, -2.3e9),  # 1.25 * 1.8 MHz exceeds 2 MHz, the bandwidth does not
        tp("std", 14.31e9, 400e3, -2.3e9),
        tp("thin", 14.32e9, 5e3, -2.3e9),
        tp("hair", 14.33e9, 1e3, -2.3e9),
    ]
    data["carriers"] = []
    model = SystemConfig.model_validate(data)
    by_id = {m.id: m for m in model.channel("north-a")[1].monitors}
    assert by_id["sat-a:wide"].span_hz == 2e6 and by_id["sat-a:wide"].center_hz == 11.95e9  # central 2 MHz only
    assert by_id["sat-a:mid"].span_hz == 2e6
    assert by_id["sat-a:std"].span_hz == 500e3
    assert (by_id["sat-a:thin"].span_hz, by_id["sat-a:thin"].nfft, by_id["sat-a:thin"].navg) == (6250.0, 256, 8)
    assert (by_id["sat-a:hair"].span_hz, by_id["sat-a:hair"].nfft, by_id["sat-a:hair"].navg) == (1250.0, 64, 8)
    for monitor in by_id.values():
        assert monitor.nfft * monitor.navg / monitor.span_hz <= 1.0 / monitor.frame_rate_hz
        assert monitor.frame_duration_s == monitor.nfft * monitor.navg / monitor.span_hz
        assert monitor.bin_hz == monitor.span_hz / monitor.nfft
    # a slower radio narrows the windows
    data["sites"][0]["channels"][0]["max_sample_rate_hz"] = 250e3
    slow = {m.id: m for m in SystemConfig.model_validate(data).channel("north-a")[1].monitors}
    assert slow["sat-a:wide"].span_hz == 250e3 and slow["sat-a:std"].span_hz == 250e3
    assert slow["sat-a:thin"].span_hz == 6250.0
    # too narrow to monitor at 2 frames per second at all: the channel is named
    data["satellites"][0]["transponders"].append(tp("dust", 14.34e9, 80.0, -2.3e9))
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(data)
    assert "sites.0.channels.0.monitors" in locations(info.value)
    assert "transponder 'dust'" in str(info.value) and "too narrow" in str(info.value)


def test_identical_windows_of_co_frequency_satellites_are_monitored_once(leo: SystemConfig) -> None:
    _, channel = leo.channel("naha-leo")
    assert channel.watched_satellite_ids == ("leo-a", "leo-b")
    assert [m.id for m in channel.monitors] == ["leo-a:u1", "leo-a:u2"]
    centres = [m.center_hz for m in channel.monitors]
    assert centres == [435.1e6 - 289.2e6, 435.3e6 - 289.2e6]
    for sat_id in ("leo-a", "leo-b"):  # every transponder of both satellites is covered all the same
        for transponder in leo.satellite(sat_id).transponders:
            assert any(m.center_hz == transponder.downlink_center_hz for m in channel.monitors)


def test_explicit_monitors_are_kept_and_validated() -> None:
    data = base()
    explicit = [
        {"id": "low", "center_hz": 259.9e6, "span_hz": 100e3, "nfft": 2048, "navg": 4, "frame_rate_hz": 5.0},
        {"id": "high", "center_hz": 260.1e6, "span_hz": 100e3},
    ]
    data["sites"][0]["channels"][0]["monitors"] = explicit
    model = SystemConfig.model_validate(data)
    _, channel = model.channel("north-a")
    assert not channel.monitors_derived
    assert [m.id for m in channel.monitors] == ["low", "high"]
    assert (channel.monitors[0].nfft, channel.monitors[0].navg, channel.monitors[0].frame_rate_hz) == (2048, 4, 5.0)
    assert model.channel("north-wide")[1].monitors_derived  # the others are still derived

    def with_monitor(**changes: Any) -> dict[str, Any]:
        bad = copy.deepcopy(data)
        bad["sites"][0]["channels"][0]["monitors"] = [{**explicit[1], **changes}]
        return bad

    # span_hz above the channel's sample rate
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_monitor(span_hz=2.5e6))
    assert "monitor 'high'" in str(info.value) and "max_sample_rate_hz" in str(info.value)
    assert "sites.0.channels.0" in locations(info.value)
    # a frame longer than the frame period: 1024 * 8 / 10 kHz = 0.82 s > 0.5 s
    with pytest.raises(ValidationError) as info:
        SystemConfig.model_validate(with_monitor(span_hz=10e3))
    assert "monitor 'high'" in str(info.value) and "frame period" in str(info.value)
    assert "sites.0.channels.0.monitors.0" in locations(info.value)
    assert SystemConfig.model_validate(with_monitor(span_hz=10e3, navg=4)) is not None  # 0.41 s fits
    assert SystemConfig.model_validate(with_monitor(span_hz=16384.0)) is not None  # exactly the frame period
    for field, value in (("nfft", 1), ("nfft", 262144), ("navg", 0), ("frame_rate_hz", 0.0), ("span_hz", -1.0), ("nfft", 512.5)):
        with pytest.raises(ValidationError) as info:
            SystemConfig.model_validate(with_monitor(**{field: value}))
        assert f"sites.0.channels.0.monitors.0.{field}" in locations(info.value)


def test_derived_monitors_follow_the_satellites_without_touching_shared_objects(cfg: SystemConfig) -> None:
    """Building a second configuration from the first one's Site objects re-derives for
    the new transponder plan and leaves the first configuration as it was."""
    before = [m.model_dump() for m in cfg.channel("north-a")[1].monitors]
    moved = [sat.model_copy(deep=True) for sat in cfg.satellites]
    moved[0].transponders[0].uplink_center_hz = 302.0e6
    moved[0].transponders.pop()
    other = SystemConfig(name="other", satellites=moved, sites=cfg.sites)
    _, channel = other.channel("north-a")
    assert channel.monitors_derived
    assert [(m.id, m.center_hz) for m in channel.monitors] == [("sat-a:x1", 261.0e6)]
    assert [m.model_dump() for m in cfg.channel("north-a")[1].monitors] == before
    assert [m.center_hz for m in cfg.channel("north-a")[1].monitors] == [260.0e6, 260.1e6]
    # explicit monitors are carried over untouched
    explicit = cfg.channel("north-a")[1].model_copy(update={"monitors": [Monitor(id="mine", center_hz=250e6, span_hz=1e5)]})
    explicit._monitors_derived = False
    site = cfg.site("north").model_copy(update={"channels": [explicit]})
    third = SystemConfig(name="third", satellites=moved, sites=[site])
    assert [m.id for m in third.channel("north-a")[1].monitors] == ["mine"]
    assert not third.channel("north-a")[1].monitors_derived


def test_a_bare_channel_keeps_its_monitors_until_it_joins_a_system() -> None:
    channel = ReceiveChannel(id="c", antenna={"type": "isotropic"}, pointing={"satellite_id": "sat-a"})
    assert channel.monitors == [] and not channel.monitors_derived
    assert isinstance(channel.pointing, TrackPointing) and channel.watched_satellite_ids == ("sat-a",)
    assert channel.antenna_summary == "isotropic 0 dBi"
    with pytest.raises(ValueError, match="'sat-a'"):
        derive_monitors(channel, [])


# --------------------------------------------------------------------------- loader


def test_yaml_round_trip(cfg: SystemConfig, pacific: SystemConfig, ku: SystemConfig, leo: SystemConfig) -> None:
    for model in (cfg, pacific, ku, leo):
        text = dump_config(model)
        again = loads_config(text)
        assert again == model
        assert dump_config(again) == text
        # derived monitors are not written out; they come back by derivation
        plain = yaml.safe_load(text)
        assert all(channel["monitors"] == [] for site in plain["sites"] for channel in site["channels"])
        assert all(channel.monitors_derived for site in again.sites for channel in site.channels)
        assert "null" not in text


def test_round_trip_keeps_explicit_monitors_and_optional_fields(tmp_path: Path) -> None:
    data = base()
    data["version"] = 7
    data["description"] = "Round trip: ünïcode, \"quotes\" and\nnew lines"
    data["dut1_s"] = -0.123456789
    data["satellites"][0]["norad_id"] = 25544
    data["satellites"][0]["ephemeris_sigma"] = {"pos_m": [1.5, 2.5, 3.5], "vel_mps": [1e-3, 2e-3, 3e-3]}
    data["satellites"][1]["orbit"] = {"type": "tle", "line1": ISS_L1, "line2": ISS_L2}
    data["sites"][0]["channels"][0]["monitors"] = [{"id": "m1", "center_hz": 260.0e6, "span_hz": 123456.789, "navg": 3}]
    data["sites"][0]["channels"][0]["antenna"] = {"type": "table", "theta_deg": [0, 30, 180], "gain_dbi": [6, 0, -9.5]}
    data["carriers"][0]["emitter"]["pointing"] = {"az_deg": 120.0, "el_deg": 33.3}
    data["carriers"].append({"id": "car-2", "name": "Plain", "satellite_id": "sat-b", "transponder_id": "x1",
                             "uplink_center_hz": 301.005e6, "bandwidth_hz": 4e3,
                             "emitter": {"eirp_dbw": 1.0, "bandwidth_hz": 0.0, "waveform": "cw"}})  # fmt: skip
    data["processing"] = {"region": {"lat_min": -60, "lat_max": 60, "lon_min": 60, "lon_max": -120},
                          "uncertainty": {"sat_osc_frac": 3.3e-7, "tdoa_bias_ns": 0.1},
                          "solver": {"grid_step_deg": 0.3}}  # fmt: skip
    model = SystemConfig.model_validate(data)
    again = loads_config(dump_config(model))
    assert again == model
    assert again.dut1_s == -0.123456789 and again.processing.uncertainty.sat_osc_frac == 3.3e-7
    assert again.channel("north-a")[1].monitors[0].span_hz == 123456.789
    assert not again.channel("north-a")[1].monitors_derived and again.channel("north-wide")[1].monitors_derived
    assert again.carrier("car-2").emitter.pointing == "omni" and again.carrier("car-2").location is None
    assert again.carrier("car-1").emitter.pointing.el_deg == 33.3
    assert again.description == data["description"]

    # YAML and JSON files load to the same model
    yaml_path, json_path = tmp_path / "system.yaml", tmp_path / "system.JSON"
    yaml_path.write_text(dump_config(model), encoding="utf-8")
    json_path.write_text(json.dumps(model.export_dict()), encoding="utf-8")
    assert load_config(yaml_path) == model
    assert load_config(json_path) == model
    assert load_config(str(yaml_path)) == model
    (tmp_path / "system.yml").write_text(dump_config(model), encoding="utf-8")
    assert load_config(tmp_path / "system.yml") == model


def test_json_round_trips_through_the_model(cfg: SystemConfig, pacific: SystemConfig) -> None:
    """What the API does: model -> JSON -> model. Derived monitors arrive as explicit ones
    with the same content; ``export_dict`` keeps them derived."""
    for model in (cfg, pacific):
        via_json = SystemConfig.model_validate_json(model.model_dump_json())
        assert via_json.model_dump() == model.model_dump()
        assert not any(channel.monitors_derived for site in via_json.sites for channel in site.channels)
        exported = json.loads(json.dumps(model.export_dict()))
        again = SystemConfig.model_validate(exported)
        assert again == model
        assert all(channel.monitors_derived for site in again.sites for channel in site.channels)
        assert SystemConfig.model_validate(model) is model  # an instance is taken as it is


def test_numbers_written_without_a_decimal_point_or_exponent_sign_still_load() -> None:
    text = dump_config(SystemConfig.model_validate(base()))
    text = text.replace("uplink_center_hz: 301000000.0", "uplink_center_hz: 301e6", 1)
    text = text.replace("bandwidth_hz: 25000.0", "bandwidth_hz: 25000", 1)
    assert "301e6" in text
    model = loads_config(text)
    assert model.satellites[0].transponders[0].uplink_center_hz == 301.0e6
    assert model.satellites[0].transponders[0].bandwidth_hz == 25000.0


def test_loader_failures_name_the_file_and_the_field(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unsupported extension '\.toml'"):
        load_config(tmp_path / "system.toml")
    with pytest.raises(ConfigError, match="missing.yaml.*cannot be read"):
        load_config(tmp_path / "missing.yaml")

    broken = tmp_path / "broken.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="broken.yaml: not valid YAML"):
        load_config(broken)
    with pytest.raises(ConfigError, match="<string>: not valid YAML"):
        loads_config("name: x\nepoch: 2026-13-45T00:00:00Z\n")  # an impossible unquoted timestamp
    bad_json = tmp_path / "broken.json"
    bad_json.write_text('{"name": "x",}', encoding="utf-8")
    with pytest.raises(ConfigError, match="broken.json: not valid JSON"):
        load_config(bad_json)
    listing = tmp_path / "list.yaml"
    listing.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_config(listing)

    # a repeated key would silently drop the first value in plain YAML or JSON loading
    twice = tmp_path / "twice.yaml"
    twice.write_text(dump_config(SystemConfig.model_validate(base())) + "name: other\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'name'"):
        load_config(twice)
    twice_json = tmp_path / "twice.json"
    twice_json.write_text('{"name": "a", "name": "b"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'name'"):
        load_config(twice_json)

    invalid = tmp_path / "invalid.yaml"
    data = base()
    data["sites"][1]["lat_deg"] = 123.0
    data["satellites"][0]["transponders"][0]["bandwidth_hz"] = "wide"
    invalid.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(invalid)
    assert isinstance(info.value, ValueError)
    message = str(info.value)
    assert "invalid.yaml: 2 configuration errors" in message
    assert "sites.1.lat_deg: Input should be less than or equal to 90" in message
    assert "satellites.0.transponders.0.bandwidth_hz: Input should be a valid number" in message
    assert {error["loc"] for error in info.value.errors} == {"sites.1.lat_deg", "satellites.0.transponders.0.bandwidth_hz"}
    assert isinstance(info.value.__cause__, ValidationError)

    # cross-reference problems carry the path of the field as well, without union-member names
    data = base()
    data["carriers"][0]["satellite_id"] = "ghost"
    data["sites"][0]["channels"][1]["pointing"]["satellite_ids"] = ["sat-a", "phantom"]
    invalid.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(invalid)
    assert info.value.errors == [
        {"loc": "sites.0.channels.1.pointing.satellite_ids.1", "msg": "unknown satellite id 'phantom'"},
        {"loc": "carriers.0.satellite_id", "msg": "unknown satellite id 'ghost'"},
    ]
    data = base()
    data["satellites"][0]["orbit"]["lon_deg"] = 400.0
    data["sites"][0]["channels"][0]["antenna"] = {"type": "parabolic", "diameter_m": 0.0}
    data["carriers"][0]["emitter"]["pointing"] = {"az_deg": 0.0, "el_deg": 100.0}
    invalid.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(invalid)
    assert [error["loc"] for error in info.value.errors] == [
        "satellites.0.orbit.lon_deg",
        "sites.0.channels.0.antenna.diameter_m",
        "carriers.0.emitter.pointing.el_deg",
    ]

    with pytest.raises(ConfigError, match="fmt"):
        loads_config("{}", fmt="toml")
    with pytest.raises(ValueError, match="cfg"):
        dump_config(base())  # type: ignore[arg-type]


def test_yaml_merge_keys_are_allowed() -> None:
    text = """
name: anchors
satellites:
  - id: s
    name: S
    orbit: {type: geo, lon_deg: 10.0}
    transponders:
      - &first {id: a, uplink_center_hz: 301.0e+6, bandwidth_hz: 25.0e+3, translation_hz: -41.0e+6,
                g_over_t_dbk: -14.0, sfd_dbw_m2: -136.0, eirp_sat_dbw: 26.0}
      - {<<: *first, id: b, uplink_center_hz: 301.1e+6, eirp_sat_dbw: 20.0}
sites: []
"""
    model = loads_config(text)  # overriding a merged key is not a duplicate
    first, second = model.satellites[0].transponders
    assert (second.id, second.uplink_center_hz, second.eirp_sat_dbw) == ("b", 301.1e6, 20.0)
    assert (second.bandwidth_hz, second.sfd_dbw_m2) == (first.bandwidth_hz, first.sfd_dbw_m2) == (25e3, -136.0)
    with pytest.raises(ConfigError, match="duplicate key 'id'"):
        loads_config(text.replace("id: b,", "id: b, id: c,"))


def test_json_schema() -> None:
    schema = config_json_schema()
    assert schema["title"] == "SystemConfig" and schema["type"] == "object"
    assert set(schema["required"]) == {"name", "satellites", "sites"}
    assert schema["additionalProperties"] is False
    assert {"version", "name", "description", "dut1_s", "satellites", "sites", "carriers", "processing"} == set(
        schema["properties"]
    )
    defs = schema["$defs"]
    for name in ("Satellite", "Transponder", "Site", "ReceiveChannel", "Monitor", "Carrier", "EmitterModel",
                 "ProcessingConfig", "Region", "GeoOrbitModel", "TleOrbitModel", "KeplerOrbitModel",
                 "TableOrbitModel", "ParabolicAntenna", "BeamAntenna", "IsotropicAntenna", "TableAntenna"):  # fmt: skip
        assert name in defs, name
        assert defs[name]["additionalProperties"] is False
    assert defs["Transponder"]["properties"]["bandwidth_hz"]["exclusiveMinimum"] == 0
    orbit = defs["Satellite"]["properties"]["orbit"]
    assert orbit["discriminator"]["propertyName"] == "type"
    assert set(orbit["discriminator"]["mapping"]) == {"tle", "geo", "kepler", "table"}
    json.dumps(schema)  # plain JSON


# --------------------------------------------------------------------------- helper methods


def test_lookups(cfg: SystemConfig) -> None:
    assert cfg.satellite("sat-b").name == "B"
    assert cfg.site("south").lat_deg == 14.0
    site, channel = cfg.channel("north-wide")
    assert site.id == "north" and channel.id == "north-wide"
    assert cfg.carrier("car-1").use_as_reference and [c.id for c in cfg.reference_carriers()] == ["car-1"]
    assert cfg.satellite("sat-a").transponder("x2").uplink_center_hz == 301.1e6
    for call, field in (
        (lambda: cfg.satellite("nope"), "sat_id 'nope'"),
        (lambda: cfg.site("nope"), "site_id 'nope'"),
        (lambda: cfg.channel("nope"), "channel_id 'nope'"),
        (lambda: cfg.carrier("nope"), "carrier_id 'nope'"),
        (lambda: cfg.satellite("sat-b").transponder("x2"), "transponder_id 'x2'"),
        (lambda: cfg.site_xyz("nope"), "site_id 'nope'"),
        (lambda: cfg.channels_watching("nope"), "sat_id 'nope'"),
        (lambda: cfg.uplink_for("nope", 260e6), "sat_id 'nope'"),
        (lambda: cfg.downlink_hz("sat-a", "x9", 301e6), "transponder_id 'x9'"),
        (lambda: cfg.boresight_ecef("nope", T0), "channel_id 'nope'"),
    ):
        with pytest.raises(ValueError, match=field):
            call()


def test_site_xyz_is_the_antenna_phase_centre(cfg: SystemConfig) -> None:
    south = cfg.site("south")
    xyz = cfg.site_xyz("south")
    assert xyz.shape == (3,)
    assert xyz == pytest.approx(lla_to_ecef(14.0, 121.0, 50.0 + 20.0), abs=1e-9)
    # exactly antenna_height_m above the ground point, along the local vertical
    ground = lla_to_ecef(south.lat_deg, south.lon_deg, south.alt_m)
    assert np.linalg.norm(xyz - ground) == pytest.approx(20.0, abs=1e-6)
    _, elevation, distance = look_angles(ground, xyz)
    assert elevation == pytest.approx(90.0, abs=1e-6) and distance == pytest.approx(20.0, abs=1e-6)
    both = cfg.sites_xyz()
    assert list(both) == ["north", "south"]
    assert np.array_equal(both["south"], xyz) and np.array_equal(both["north"], lla_to_ecef(35.0, 139.0, 10.0))
    assert cfg.carrier_xyz("car-1") == pytest.approx(lla_to_ecef(13.5, 144.7, 10.0), abs=1e-9)


def test_orbits_helper_builds_the_numeric_orbits() -> None:
    data = base()
    data["dut1_s"] = 0.25
    data["satellites"].append({"id": "sat-t", "name": "T", "orbit": {"type": "tle", "line1": ISS_L1, "line2": ISS_L2},
                               "transponders": []})  # fmt: skip
    model = SystemConfig.model_validate(data)
    orbits = model.orbits()
    assert list(orbits) == ["sat-a", "sat-b", "sat-t"]
    assert isinstance(orbits["sat-a"], GeoOrbit) and isinstance(orbits["sat-t"], TleOrbit)
    assert orbits["sat-t"].dut1_s == 0.25  # dut1_s reaches the inertial orbit types
    t = T0 + np.array([0.0, 3600.0, 40000.0])
    reference = GeoOrbit(140.0, 3.0)
    assert np.array_equal(orbit_position(orbits["sat-a"], t), orbit_position(reference, t))
    assert np.array_equal(orbit_position(model.orbit("sat-a"), t), orbit_position(reference, t))


def test_boresight_ecef(cfg: SystemConfig) -> None:
    t_s = T0 + 5 * 3600.0
    site_xyz = cfg.site_xyz("north")
    sat_r = orbit_position(GeoOrbit(140.0, 3.0), t_s)
    tracking = cfg.boresight_ecef("north-a", t_s)
    assert tracking.shape == (3,) and np.linalg.norm(tracking) == pytest.approx(1.0, abs=1e-15)
    az, el, _ = look_angles(site_xyz, sat_r)  # an independent route to the same direction
    assert angle_between_deg(tracking, azel_to_ecef(35.0, 139.0, az, el)) < 1e-6
    assert angle_between_deg(tracking, sat_r - site_xyz) < 1e-9
    # the tracked direction follows the inclined satellite through the day
    times = t_s + np.array([0.0, 6 * 3600.0, 12 * 3600.0])
    track = cfg.boresight_ecef("north-a", times)
    assert track.shape == (3, 3) and np.array_equal(track[0], tracking)
    assert angle_between_deg(track[0], track[1]) > 1.0
    # a fixed channel points where it was told, at any time
    fixed = cfg.boresight_ecef("north-wide", t_s)
    assert fixed == pytest.approx(azel_to_ecef(35.0, 139.0, 170.0, 45.0), abs=1e-12)
    az, el, _ = look_angles(site_xyz, site_xyz + 1e3 * fixed)
    assert (az, el) == pytest.approx((170.0, 45.0), abs=1e-6)
    many = cfg.boresight_ecef("north-wide", times)
    assert many.shape == (3, 3) and np.array_equal(many[2], fixed)
    with pytest.raises(ValueError, match="t_s"):
        cfg.boresight_ecef("north-a", float("nan"))


def test_channels_watching(cfg: SystemConfig) -> None:
    assert [(s.id, c.id) for s, c in cfg.channels_watching("sat-a")] == [("north", "north-a"), ("north", "north-wide")]
    assert [(s.id, c.id) for s, c in cfg.channels_watching("sat-b")] == [("north", "north-wide"), ("south", "south-b")]
    data = base()
    data["satellites"].append({"id": "spare", "name": "Spare", "orbit": {"type": "geo", "lon_deg": 0.0}, "transponders": []})
    assert SystemConfig.model_validate(data).channels_watching("spare") == []


def test_transponders_covering_edge_cases(cfg: SystemConfig) -> None:
    def ids(f_lo: float, f_hi: float) -> list[tuple[str, str]]:
        return [(sat.id, transponder.id) for sat, transponder in cfg.transponders_covering(f_lo, f_hi)]

    # x1 is 300.9875 to 301.0125 MHz on both satellites; x2 is 301.0875 to 301.1125 MHz on sat-a only
    assert ids(300.99e6, 301.00e6) == [("sat-a", "x1"), ("sat-b", "x1")]
    assert ids(301.09e6, 301.10e6) == [("sat-a", "x2")]  # only one satellite: nothing to correlate against
    assert ids(300.0e6, 302.0e6) == [("sat-a", "x1"), ("sat-a", "x2"), ("sat-b", "x1")]
    assert ids(301.0120e6, 301.0880e6) == [("sat-a", "x1"), ("sat-a", "x2"), ("sat-b", "x1")]  # clips two channels
    assert ids(301.02e6, 301.08e6) == []  # the gap between the channels
    assert ids(290.0e6, 300.0e6) == []
    # sharing only an edge is not an overlap ...
    assert ids(301.0125e6, 301.0500e6) == []
    assert ids(300.9000e6, 300.9875e6) == []
    assert ids(301.0125e6, 301.0875e6) == []
    # ... but the slightest intrusion is
    assert ids(300.9000e6, 300.9875e6 + 1.0) == [("sat-a", "x1"), ("sat-b", "x1")]
    assert ids(301.0125e6 - 1.0, 301.0500e6) == [("sat-a", "x1"), ("sat-b", "x1")]
    # a single frequency is covered when it lies in the closed passband
    assert ids(301.0e6, 301.0e6) == [("sat-a", "x1"), ("sat-b", "x1")]
    assert ids(301.0125e6, 301.0125e6) == [("sat-a", "x1"), ("sat-b", "x1")]
    assert ids(301.0125e6 + 1.0, 301.0125e6 + 1.0) == []
    with pytest.raises(ValueError, match="f_hi_up"):
        cfg.transponders_covering(301.1e6, 301.0e6)
    with pytest.raises(ValueError, match="finite"):
        cfg.transponders_covering(float("nan"), 301.0e6)


def test_uplink_for_and_downlink_hz_edge_cases(cfg: SystemConfig) -> None:
    transponder, f_up = cfg.uplink_for("sat-a", 260.003e6)
    assert transponder.id == "x1" and f_up == pytest.approx(301.003e6, abs=1e-6)
    transponder, f_up = cfg.uplink_for("sat-a", 260.1e6)
    assert transponder.id == "x2" and f_up == 301.1e6
    # the same downlink frequency maps differently on a satellite with another translation
    transponder, f_up = cfg.uplink_for("sat-b", 267.4e6)
    assert transponder.id == "x1" and f_up == 301.0e6
    assert cfg.uplink_for("sat-b", 260.0e6) is None
    # passband edges are inside, one hertz beyond is not
    assert cfg.uplink_for("sat-a", 260.0125e6)[0].id == "x1"
    assert cfg.uplink_for("sat-a", 259.9875e6)[0].id == "x1"
    assert cfg.uplink_for("sat-a", 260.0125e6 + 1.0) is None
    assert cfg.uplink_for("sat-a", 259.9875e6 - 1.0) is None
    assert cfg.uplink_for("sat-a", 260.05e6) is None  # between the channels
    with pytest.raises(ValueError, match="f_down_hz"):
        cfg.uplink_for("sat-a", float("inf"))

    # inverse of each other
    for sat_id, f_down in (("sat-a", 259.99e6), ("sat-a", 260.11e6), ("sat-b", 267.41e6)):
        transponder, f_up = cfg.uplink_for(sat_id, f_down)
        assert cfg.downlink_hz(sat_id, transponder.id, f_up) == pytest.approx(f_down, abs=1e-6)
    assert cfg.downlink_hz("sat-a", "x1", 301.0e6) == 260.0e6
    assert cfg.downlink_hz("sat-b", "x1", 301.0e6) == 267.4e6
    band = cfg.downlink_hz("sat-a", "x2", np.array([301.09e6, 301.11e6]))
    assert band == pytest.approx([260.09e6, 260.11e6], abs=1e-6)

    # overlapping passbands (and a positive translation): the nearest downlink centre wins
    data = base()
    data["satellites"][1]["transponders"] = [tp("lo", 400.00e6, 100e3, 50e6), tp("hi", 400.08e6, 100e3, 50e6)]
    model = SystemConfig.model_validate(data)
    assert model.uplink_for("sat-b", 450.03e6)[0].id == "lo"
    assert model.uplink_for("sat-b", 450.05e6)[0].id == "hi"
    assert model.uplink_for("sat-b", 450.04e6)[0].id == "lo"  # a tie goes to the earlier one
    assert model.uplink_for("sat-b", 450.03e6)[1] == pytest.approx(400.03e6, abs=1e-6)
    assert model.satellites[1].transponders[0].downlink_center_hz == 450.0e6


def test_transponder_passband_properties(cfg: SystemConfig) -> None:
    x1 = cfg.satellite("sat-b").transponder("x1")
    assert (x1.uplink_lo_hz, x1.uplink_hi_hz) == (300.9875e6, 301.0125e6)
    assert (x1.downlink_center_hz, x1.downlink_lo_hz, x1.downlink_hi_hz) == (267.4e6, 267.3875e6, 267.4125e6)
    assert x1.relays(300.9875e6) and x1.relays(301.0125e6) and not x1.relays(301.0126e6)
    carrier = cfg.carrier("car-1")
    assert (carrier.uplink_lo_hz, carrier.uplink_hi_hz) == (300.995e6, 301.005e6)


def test_resolved_uncertainty() -> None:
    data = base()
    kepler = dict(type="kepler", a_m=7178137.0, e=0.001, inc_deg=60.0, raan_deg=10.0, argp_deg=0.0,
                  mean_anomaly_deg=0.0, epoch="2026-01-01T00:00:00Z")  # fmt: skip
    table = {"type": "table", "samples": [
        {"t": "2026-01-01T00:00:00Z", "r_m": [42164172.0, 0.0, 0.0], "v_mps": [0.0, 0.0, 0.0]},
        {"t": "2026-01-01T01:00:00Z", "r_m": [42164172.0, 0.0, 0.0], "v_mps": [0.0, 0.0, 0.0]},
    ]}  # fmt: skip
    for ident, orbit in (("sat-tle", {"type": "tle", "line1": ISS_L1, "line2": ISS_L2}), ("sat-kep", kepler), ("sat-tab", table)):
        data["satellites"].append({"id": ident, "name": ident, "orbit": orbit, "transponders": []})
    data["satellites"].append({"id": "sat-own", "name": "own", "orbit": {"type": "tle", "line1": ISS_L1, "line2": ISS_L2},
                               "transponders": [],
                               "ephemeris_sigma": {"pos_m": [5.0, 50.0, 30.0], "vel_mps": [0.001, 0.002, 0.003]}})  # fmt: skip
    data["satellites"][1]["ephemeris_sigma"] = {"pos_m": [7.0, 8.0, 9.0], "vel_mps": [0.0, 0.0, 0.0]}  # a geo override
    data["processing"] = {"uncertainty": {"sat_osc_frac": 2e-7, "site_osc_frac": 3e-12, "tdoa_bias_ns": 80.0}}
    unc = SystemConfig.model_validate(data).resolved_uncertainty()
    assert isinstance(unc, Uncertainty)

    operator_pos, operator_vel = [100.0, 1000.0, 1000.0], [0.02, 0.02, 0.05]
    tle_pos, tle_vel = [2000.0, 20000.0, 10000.0], [0.3, 0.3, 0.7]
    expected = {
        "sat-a": (operator_pos, operator_vel),  # geo -> operator
        "sat-b": ([7.0, 8.0, 9.0], [0.0, 0.0, 0.0]),  # per-satellite override of a geo orbit
        "sat-tle": (tle_pos, tle_vel),  # tle -> tle
        "sat-kep": (operator_pos, operator_vel),  # kepler -> operator
        "sat-tab": (operator_pos, operator_vel),  # table -> operator
        "sat-own": ([5.0, 50.0, 30.0], [0.001, 0.002, 0.003]),  # override of a tle orbit
    }
    assert set(unc.sat_pos_ric_m) == set(unc.sat_vel_ric_mps) == set(expected)
    for sat_id, (pos, vel) in expected.items():
        assert isinstance(unc.sat_pos_ric_m[sat_id], np.ndarray) and unc.sat_pos_ric_m[sat_id].shape == (3,)
        assert unc.sat_pos_ric_m[sat_id].tolist() == pos, sat_id
        assert unc.sat_vel_ric_mps[sat_id].tolist() == vel, sat_id
    # per-site clock from Site.clock.sigma_ns (default 30 ns, 80 ns configured for south)
    assert unc.site_clock_s == pytest.approx({"north": 30e-9, "south": 80e-9}, rel=1e-12)
    assert unc.site_osc_frac == {"north": 3e-12, "south": 3e-12}
    assert unc.sat_osc_frac == {sat_id: 2e-7 for sat_id in expected}
    assert unc.tdoa_bias_s == pytest.approx(80e-9, rel=1e-12)

    # changing the defaults moves only the satellites that use them
    data["processing"]["uncertainty"]["ephemeris"] = {"tle": {"pos_m": [1.0, 2.0, 3.0], "vel_mps": [4.0, 5.0, 6.0]}}
    unc = SystemConfig.model_validate(data).resolved_uncertainty()
    assert unc.sat_pos_ric_m["sat-tle"].tolist() == [1.0, 2.0, 3.0]
    assert unc.sat_vel_ric_mps["sat-tle"].tolist() == [4.0, 5.0, 6.0]
    assert unc.sat_pos_ric_m["sat-own"].tolist() == [5.0, 50.0, 30.0]
    assert unc.sat_pos_ric_m["sat-kep"].tolist() == operator_pos


def test_resolved_uncertainty_feeds_geometry_search_windows(pacific: SystemConfig) -> None:
    from petal_locate.core.geometry import search_windows

    unc = pacific.resolved_uncertainty()
    a = PathSpec("muos-pac", "tokyo", 301.0e6, -41.0e6)
    b = PathSpec("uhf-172e", "honolulu", 301.0e6, -33.6e6)
    delay, doppler = search_windows(((0.01, 0.02), (-5.0, 5.0)), a, b, pacific.processing.caf, unc)
    # 20 us + 3 sqrt(30^2 + 30^2 + 50^2) ns; 20 Hz + 3 sqrt((41 Hz)^2 + (33.6 Hz)^2 + two 2.6 mHz site terms)
    pad_delay = 20e-6 + 3.0 * math.sqrt(2 * 30e-9**2 + 50e-9**2)
    pad_doppler = 20.0 + 3.0 * math.sqrt(41.0**2 + 33.6**2 + (1e-11 * 260.0e6) ** 2 + (1e-11 * 267.4e6) ** 2)
    assert delay == pytest.approx((0.01 - pad_delay, 0.02 + pad_delay), abs=1e-12)
    assert doppler == pytest.approx((-5.0 - pad_doppler, 5.0 + pad_doppler), abs=1e-9)


def test_emitter_model_is_independent_of_any_system() -> None:
    emitter = EmitterModel(eirp_dbw=30.0, antenna={"type": "parabolic", "diameter_m": 1.8},
                           pointing={"satellite_id": "anything"}, bandwidth_hz=2e6, waveform="psk")  # fmt: skip
    assert emitter.pointing.satellite_id == "anything" and emitter.antenna.diameter_m == 1.8
    assert EmitterModel(eirp_dbw=0.0, bandwidth_hz=0.0, waveform="cw").bandwidth_hz == 0.0
    fixed = EmitterModel.model_validate({"eirp_dbw": 1.0, "bandwidth_hz": 5.0, "pointing": {"az_deg": 10.0, "el_deg": 5.0}})
    assert (fixed.pointing.az_deg, fixed.pointing.el_deg) == (10.0, 5.0)
    assert EmitterModel.model_validate(fixed.model_dump()) == fixed
    assert EmitterModel.model_validate_json(emitter.model_dump_json()) == emitter
    for bad, field in (
        ({"eirp_dbw": 1.0}, "bandwidth_hz"),
        ({"eirp_dbw": 1.0, "bandwidth_hz": 0.0}, "bandwidth_hz"),
        ({"eirp_dbw": 1.0, "bandwidth_hz": 1.0, "pointing": {"az_deg": 10.0}}, "el_deg"),
        ({"eirp_dbw": 1.0, "bandwidth_hz": 1.0, "pointing": {"satellite_id": "s", "el_deg": 1.0}}, "el_deg"),
        ({"eirp_dbw": 1.0, "bandwidth_hz": 1.0, "pointing": 3}, "pointing"),
        ({"eirp_dbw": 1.0, "bandwidth_hz": 1.0, "gain": 3}, "gain"),
    ):
        with pytest.raises(ValidationError) as info:
            EmitterModel.model_validate(bad)
        assert any(field in loc for loc in locations(info.value)) or field in str(info.value)


def test_warnings(cfg: SystemConfig, pacific: SystemConfig, ku: SystemConfig) -> None:
    notes = {note["loc"]: note["msg"] for note in cfg.warnings()}
    # the 1.2 m dish at 260 MHz, and north-wide's windows at 260 and 267.4 MHz, which no
    # single 2 MHz tuning of its radio can serve
    assert set(notes) == {"sites.0.channels.0.antenna", "sites.0.channels.1.monitors"}
    assert "under 3 wavelengths" in notes["sites.0.channels.0.antenna"]
    assert "span 7.431 MHz" in notes["sites.0.channels.1.monitors"]
    assert "sat-a:x1, sat-a:x2, sat-b:x1" in notes["sites.0.channels.1.monitors"]
    assert ku.warnings(T0) == []  # Ku dishes focus, and every satellite is up
    small = [note["loc"] for note in pacific.warnings(T0)]
    parabolic = [c for s in pacific.sites for c in s.channels if c.antenna.type == "parabolic"]
    assert len(small) == len(parabolic) == 13 and all(loc.endswith(".antenna") for loc in small)
    # a channel pointed at a satellite on the far side of the Earth
    hidden = SystemConfig.model_validate(edited(("satellites", 1, "orbit", "lon_deg"), -30.0))
    below = [note for note in hidden.warnings(T0) if note["loc"].endswith(".pointing")]
    assert [note["loc"] for note in below] == ["sites.0.channels.1.pointing", "sites.1.channels.0.pointing"]
    assert "below the horizon" in below[0]["msg"] and "'sat-b'" in below[0]["msg"]
    assert set(below[0]) == {"loc", "msg"}
    assert not [note for note in hidden.warnings() if note["loc"].endswith(".pointing")]  # needs a time
    # a carrier wider than its transponder is relayed (its centre is inside) but worth a look
    wide = SystemConfig.model_validate(edited(("carriers", 0, "bandwidth_hz"), 40e3))
    spill = [note for note in wide.warnings() if note["loc"] == "carriers.0.bandwidth_hz"]
    assert len(spill) == 1 and "extends beyond the uplink passband of transponder 'x1'" in spill[0]["msg"]
    assert not [note for note in cfg.warnings() if note["loc"].startswith("carriers")]
    exact = SystemConfig.model_validate(edited(("carriers", 0, "bandwidth_hz"), 25e3))  # edge to edge is fine
    assert not [note for note in exact.warnings() if note["loc"].startswith("carriers")]


def test_documented_type_names_are_available() -> None:
    from petal_locate import config
    from petal_locate.config import models

    for name in ("SystemConfig", "Satellite", "Orbit", "Transponder", "Site", "ReceiveChannel", "Pointing",
                 "Monitor", "Antenna", "EmitterModel", "Carrier", "ProcessingConfig", "Region"):  # fmt: skip
        assert hasattr(config, name) and hasattr(models, name), name
    assert set(config.__all__) >= set(models.__all__) - {name for name in models.__all__ if name.isupper()}
    for name in config.__all__:
        assert hasattr(config, name), name


# --------------------------------------------------------------------------- shipped configurations


def geo_elevations(config: SystemConfig, sat_id: str, xyz: np.ndarray, t_s: Any) -> np.ndarray:
    """Elevation of a ``geo`` satellite from a point, rebuilt from the raw orbit fields."""
    orbit = config.satellite(sat_id).orbit
    assert orbit.type == "geo"
    numeric = GeoOrbit(orbit.lon_deg, orbit.inclination_deg, orbit.epoch, orbit.phase_deg, orbit.drift_deg_per_day)
    return np.asarray(look_angles(xyz, orbit_position(numeric, np.asarray(t_s, dtype=np.float64)))[1])


DAY = T0 + np.linspace(0.0, SIDEREAL_DAY_S, 49)

CITIES = {
    "honolulu": (21.307, -157.858),
    "hilo": (19.707, -155.085),
    "tokyo": (35.681, 139.767),
    "naha": (26.212, 127.679),
    "seoul": (37.566, 126.978),
    "busan": (35.180, 129.075),
    "manila": (14.599, 120.984),
    "guam": (13.476, 144.749),
    "sapporo": (43.062, 141.354),
}


@pytest.mark.parametrize("name", ["pacific-uhf", "ku-adjacent", "leo-example"])
def test_shipped_configs_load_and_say_they_are_illustrative(name: str) -> None:
    model = load_config(CONFIG_DIR / f"{name}.yaml")
    assert model.name == name and model.version == 1
    assert "illustrative" in model.description.lower()
    assert "not a real facility" in model.description or "no site or reference location is a real facility" in model.description
    for satellite in model.satellites:
        assert "illustrative" in satellite.notes.lower(), satellite.id
    for carrier in model.carriers:
        assert "illustrative" in carrier.name.lower(), carrier.id
    for site in model.sites:  # neutral city-center names and exactly the city-center coordinates
        assert site.name == f"{site.id.capitalize()} (city center)"
        assert site.time_zone is not None  # the console shows each site's local time
        assert (site.lat_deg, site.lon_deg) == CITIES[site.id]
    raw = (CONFIG_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    assert "EVERYTHING NUMERIC HERE IS ILLUSTRATIVE" in raw
    assert loads_config(dump_config(model)) == model


@pytest.mark.parametrize("name", ["pacific-uhf", "ku-adjacent"])
def test_shipped_channels_point_at_satellites_above_the_horizon(name: str) -> None:
    model = load_config(CONFIG_DIR / f"{name}.yaml")
    assert all(note["loc"].endswith(".antenna") for note in model.warnings(T0))  # nothing below the horizon
    for site in model.sites:
        xyz = lla_to_ecef(site.lat_deg, site.lon_deg, site.alt_m + site.antenna_height_m)
        assert site.channels
        for channel in site.channels:
            assert isinstance(channel.pointing, TrackPointing)
            elevation = geo_elevations(model, channel.pointing.satellite_id, xyz, DAY)
            assert elevation.min() > 5.0, (channel.id, float(elevation.min()))
            # and the boresight really is on that satellite: zero off-axis angle
            sat_r = orbit_position(model.orbits()[channel.pointing.satellite_id], T0)
            assert angle_between_deg(model.boresight_ecef(channel.id, T0), sat_r - xyz) < 1e-9


@pytest.mark.parametrize("name", ["pacific-uhf", "ku-adjacent", "leo-example"])
def test_shipped_monitors_satisfy_their_own_validation(name: str) -> None:
    model = load_config(CONFIG_DIR / f"{name}.yaml")
    for site in model.sites:
        for channel in site.channels:
            assert channel.monitors and channel.monitors_derived
            assert len({m.id for m in channel.monitors}) == len(channel.monitors)
            for monitor in channel.monitors:
                assert Monitor.model_validate(monitor.model_dump()) == monitor
                assert monitor.span_hz <= channel.max_sample_rate_hz
                assert monitor.nfft * monitor.navg / monitor.span_hz <= 1.0 / monitor.frame_rate_hz
                assert 64 <= monitor.nfft <= 1024 and 1 <= monitor.navg <= 8
            # written out as explicit monitors they pass the channel's validation as well
            explicit = ReceiveChannel.model_validate(channel.model_dump())
            assert explicit.monitors == channel.monitors and not explicit.monitors_derived
            # every transponder of every watched satellite has a window on its downlink centre
            for sat_id in channel.watched_satellite_ids:
                for transponder in model.satellite(sat_id).transponders:
                    windows = [m for m in channel.monitors if m.center_hz == transponder.downlink_center_hz]
                    assert len(windows) == 1
                    assert windows[0].span_hz == min(1.25 * transponder.bandwidth_hz, channel.max_sample_rate_hz)


def test_pacific_uhf_frequency_plan(pacific: SystemConfig) -> None:
    assert [(s.id, s.name) for s in pacific.satellites] == [
        ("muos-pac", "MUOS Pacific (nominal 177 W)"),
        ("uhf-172e", "UFO-class legacy UHF (nominal 172 E)"),
        ("muos-io", "MUOS Indo-Asia (nominal 75 E)"),
    ]
    assert [s.orbit.lon_deg for s in pacific.satellites] == [-177.0, 172.0, 75.0]
    assert all(s.orbit.type == "geo" and 1.0 <= s.orbit.inclination_deg <= 6.0 for s in pacific.satellites)
    translations = {s.id: {t.translation_hz for t in s.transponders} for s in pacific.satellites}
    assert translations == {"muos-pac": {-41.0e6}, "uhf-172e": {-33.6e6}, "muos-io": {-41.0e6}}
    uplinks: dict[float, list[str]] = {}
    for satellite in pacific.satellites:
        assert len(satellite.transponders) >= 3
        for transponder in satellite.transponders:
            assert transponder.bandwidth_hz == 25e3
            assert 299e6 < transponder.uplink_center_hz < 303e6  # near 300 MHz
            assert (transponder.g_over_t_dbk, transponder.sfd_dbw_m2, transponder.eirp_sat_dbw) == (-14.0, -136.0, 26.0)
            uplinks.setdefault(transponder.uplink_center_hz, []).append(satellite.id)
    everywhere = [f for f, sats in uplinks.items() if len(sats) == 3]
    solo = {f: sats[0] for f, sats in uplinks.items() if len(sats) == 1}
    assert len(everywhere) == 2  # two channels, same uplink frequencies, on all three satellites
    assert sorted(solo.values()) == ["muos-io", "muos-pac", "uhf-172e"]  # and one of its own on each
    assert len(uplinks) == 5
    # channels do not overlap one another
    edges = sorted((f - 12.5e3, f + 12.5e3) for f in uplinks)
    assert all(hi <= lo for (_, hi), (lo, _) in zip(edges, edges[1:]))
    # the "no second satellite" case: a solo channel is relayed by exactly one satellite
    for f_up, sat_id in solo.items():
        covering = pacific.transponders_covering(f_up - 5e3, f_up + 5e3)
        assert [sat.id for sat, _ in covering] == [sat_id]
    for f_up in everywhere:
        assert [sat.id for sat, _ in pacific.transponders_covering(f_up - 5e3, f_up + 5e3)] == [
            "muos-pac",
            "uhf-172e",
            "muos-io",
        ]
    # muos-pac and muos-io are co-channel on the downlink, about 120 degrees apart seen from Tokyo
    assert pacific.downlink_hz("muos-pac", "shared-1", 301.0e6) == pacific.downlink_hz("muos-io", "shared-1", 301.0e6)
    assert pacific.downlink_hz("uhf-172e", "shared-1", 301.0e6) == 267.4e6
    tokyo = pacific.site_xyz("tokyo")
    orbits = pacific.orbits()
    separation = angle_between_deg(orbit_position(orbits["muos-pac"], T0) - tokyo, orbit_position(orbits["muos-io"], T0) - tokyo)
    assert 115.0 < separation < 125.0
    region = pacific.processing.region
    assert (region.lat_min, region.lat_max, region.lon_min, region.lon_max) == (-60.0, 60.0, 60.0, -120.0)


def test_pacific_uhf_sites_have_one_channel_per_satellite_above_five_degrees(pacific: SystemConfig) -> None:
    assert [site.id for site in pacific.sites] == ["honolulu", "hilo", "tokyo", "naha", "seoul", "busan", "manila"]
    for site in pacific.sites:
        xyz = lla_to_ecef(site.lat_deg, site.lon_deg, site.alt_m + site.antenna_height_m)
        watched = [channel.pointing.satellite_id for channel in site.channels]
        assert len(set(watched)) == len(watched)  # one channel per satellite
        for satellite in pacific.satellites:
            elevation = geo_elevations(pacific, satellite.id, xyz, DAY)
            if satellite.id in watched:
                assert elevation.min() > 5.0, (site.id, satellite.id)
            else:
                assert elevation.max() < 0.0, (site.id, satellite.id)  # never visible, not merely low
    # Hawaii cannot see 75 E
    for site_id in ("honolulu", "hilo"):
        assert sorted(c.pointing.satellite_id for c in pacific.site(site_id).channels) == ["muos-pac", "uhf-172e"]
        assert geo_elevations(pacific, "muos-io", pacific.site_xyz(site_id), DAY).max() < -35.0
    assert sum(len(site.channels) for site in pacific.sites) == 19
    assert len(pacific.channels_watching("muos-pac")) == 7 and len(pacific.channels_watching("muos-io")) == 5


def test_pacific_uhf_antenna_mix_includes_the_electrically_small_case(pacific: SystemConfig) -> None:
    channels = [channel for site in pacific.sites for channel in site.channels]
    dishes = sorted({c.antenna.diameter_m for c in channels if c.antenna.type == "parabolic"})
    beams = {(c.antenna.gain_dbi, c.polarization_loss_db) for c in channels if c.antenna.type == "beam"}
    assert dishes == [0.72, 1.2] and beams == {(10.0, 3.0)}
    assert sum(c.antenna.type == "beam" for c in channels) >= 4
    for channel in channels:
        downlinks = [t.downlink_center_hz for t in pacific.satellite(channel.pointing.satellite_id).transponders]
        assert all(259e6 < f < 269e6 for f in downlinks)
        if channel.antenna.type == "parabolic":
            assert all(electrically_small(channel.antenna, f) for f in downlinks)
            assert channel.antenna.diameter_m / (C / max(downlinks)) < 1.1  # about one wavelength or less


def test_pacific_uhf_reference_carriers(pacific: SystemConfig) -> None:
    """The references are designed to be relayed well by every satellite they calibrate.

    A reference's own TDOA noise enters every calibrated row, so it must be small next to
    the satellite-position error calibration removes (MATH.md section 9). An omni antenna
    gives every satellite above the horizon the full EIRP (a beam aimed at one satellite
    gave the others 15 dB less and 2 to 4 microseconds of reference noise). Checked here
    with the link budget and the Stein bound of MATH.md section 8 in the weakest channel
    of each satellite: under 0.3 microseconds for every pair of satellites.
    """
    from petal_locate.core.crlb import stein_sigmas
    from petal_locate.core.linkbudget import path_budget, transponder_outputs

    references = pacific.reference_carriers()
    assert [c.id for c in references] == ["ref-guam", "ref-sapporo"] and len(pacific.carriers) == 2
    places = {"ref-guam": CITIES["guam"], "ref-sapporo": CITIES["sapporo"]}
    used = set()
    for carrier in references:
        assert (carrier.location.lat_deg, carrier.location.lon_deg) == places[carrier.id]
        emitter = carrier.emitter
        assert emitter.antenna.type == "isotropic" and emitter.pointing == "omni"  # seen equally by every satellite
        assert emitter.bandwidth_hz == carrier.bandwidth_hz == 8.0e3 and emitter.eirp_dbw == 20.0  # 100 W into an omni
        xyz = pacific.carrier_xyz(carrier.id)
        # it sits in a channel that all three satellites relay, so any pair can be calibrated
        covering = pacific.transponders_covering(carrier.uplink_lo_hz, carrier.uplink_hi_hz)
        assert [sat.id for sat, _ in covering] == ["muos-pac", "uhf-172e", "muos-io"]
        assert all(t.uplink_lo_hz <= carrier.uplink_lo_hz and carrier.uplink_hi_hz <= t.uplink_hi_hz for _, t in covering)
        # parked at the low edge of its channel, leaving the upper two thirds to users
        assert carrier.uplink_hi_hz <= covering[0][1].uplink_center_hz - 3.0e3
        used.add(carrier.transponder_id)
        worst_snr: dict[str, float] = {}
        for satellite, transponder in covering:
            # every satellite is above this site's horizon all day (muos-io is the lowest: 5 to 12 deg from Sapporo)
            assert geo_elevations(pacific, satellite.id, xyz, DAY).min() > 4.0, (carrier.id, satellite.id)
            for site, channel in pacific.channels_watching(satellite.id):
                budget = path_budget(pacific, emitter, carrier.uplink_center_hz, xyz, satellite.id,
                                     transponder.id, channel.id, T0)  # fmt: skip
                assert budget is not None
                assert budget.uplink.eirp_toward_sat_dbw == pytest.approx(emitter.eirp_dbw, abs=1e-9)
                # alone it leaves the transponder well below saturation (room for users and interferers)
                backoff = transponder_outputs([float(budget.uplink.pfd_dbw_m2)], transponder).backoff_db
                assert 6.0 < backoff < 7.5, (carrier.id, satellite.id, backoff)
                snr = float(budget.snr_db(emitter.bandwidth_hz))
                assert 11.0 < snr < 18.0, (carrier.id, channel.id, snr)  # clearly visible, like the demo jammer
                worst_snr[satellite.id] = min(worst_snr.get(satellite.id, math.inf), snr)
        integration = pacific.processing.capture.duration_s
        bandwidth = emitter.bandwidth_hz
        for sat_a, sat_b in (("muos-pac", "uhf-172e"), ("muos-pac", "muos-io"), ("uhf-172e", "muos-io")):
            g1, g2 = 10.0 ** (worst_snr[sat_a] / 10.0), 10.0 ** (worst_snr[sat_b] / 10.0)
            coherence = g1 * g2 / ((1.0 + g1) * (1.0 + g2))  # MATH section 8
            rho_db = 10.0 * math.log10(bandwidth * integration * coherence)
            assert rho_db > pacific.processing.caf.min_snr_db + 20.0, (carrier.id, sat_a, sat_b, rho_db)
            sigma_tdoa, _ = stein_sigmas(bandwidth, bandwidth / math.sqrt(12.0), integration, g1, g2)
            assert sigma_tdoa < 0.3e-6, (carrier.id, sat_a, sat_b, sigma_tdoa)
    assert used == {"shared-1", "shared-2"}  # one reference in each shared channel


def test_ku_adjacent_matches_its_description(ku: SystemConfig) -> None:
    assert len(ku.satellites) == 3 and len(ku.sites) == 2 and len(ku.reference_carriers()) == len(ku.carriers) == 1
    longitudes = [s.orbit.lon_deg for s in ku.satellites]
    assert all(2.0 <= b - a <= 3.0 for a, b in zip(longitudes, longitudes[1:]))
    plans = []
    for satellite in ku.satellites:
        assert satellite.orbit.type == "geo" and 0.0 < satellite.orbit.inclination_deg < 0.5
        for transponder in satellite.transponders:
            assert transponder.bandwidth_hz == 36e6
            assert 14.0e9 <= transponder.uplink_lo_hz and transponder.uplink_hi_hz <= 14.5e9
            assert 11.7e9 <= transponder.downlink_lo_hz and transponder.downlink_hi_hz <= 12.2e9
            assert (transponder.g_over_t_dbk, transponder.sfd_dbw_m2, transponder.eirp_sat_dbw) == (3.0, -85.0, 50.0)
        plans.append([(t.id, t.uplink_center_hz, t.translation_hz) for t in satellite.transponders])
    assert plans[0] == plans[1] == plans[2] and len(plans[0]) == 1  # co-channel neighbors
    for site in ku.sites:
        assert sorted({c.antenna.diameter_m for c in site.channels}) == [1.2, 2.4]
        assert sorted(c.pointing.satellite_id for c in site.channels) == ["ku-a", "ku-b", "ku-c"]
        for channel in site.channels:
            assert not electrically_small(channel.antenna, 11.95e9)
            # one window over the whole 36 MHz transponder, inside one tuning of a 45 MS/s radio
            (window,) = channel.monitors
            assert window.span_hz == channel.max_sample_rate_hz == 45e6
            (tp,) = ku.satellite(channel.pointing.satellite_id).transponders
            assert window.center_hz - 0.5 * window.span_hz <= tp.downlink_lo_hz
            assert tp.downlink_hi_hz <= window.center_hz + 0.5 * window.span_hz
    reference = ku.carriers[0]
    assert (reference.location.lat_deg, reference.location.lon_deg) == CITIES["seoul"]
    assert reference.emitter.antenna.type == "parabolic" and reference.emitter.pointing.satellite_id == reference.satellite_id
    # seen from the reference, the neighbours are 2 to 3.5 degrees off its beam: side-lobe uplinks
    xyz = ku.carrier_xyz(reference.id)
    orbits = ku.orbits()
    for other in ("ku-a", "ku-c"):
        off = angle_between_deg(orbit_position(orbits["ku-b"], T0) - xyz, orbit_position(orbits[other], T0) - xyz)
        assert 2.0 < off < 3.5


def test_leo_example_matches_its_description(leo: SystemConfig) -> None:
    assert [s.id for s in leo.satellites] == ["leo-a", "leo-b"] and len(leo.sites) == 1 and leo.carriers == []
    assert all(s.orbit.type == "kepler" for s in leo.satellites)
    assert all(500e3 < s.orbit.a_m - 6378137.0 < 2000e3 for s in leo.satellites)  # low Earth orbit
    plan_a = [t.model_dump() for t in leo.satellites[0].transponders]
    plan_b = [t.model_dump() for t in leo.satellites[1].transponders]
    assert plan_a == plan_b and len(plan_a) == 2  # a shared transponder plan
    site = leo.sites[0]
    (channel,) = site.channels
    assert sorted(channel.watched_satellite_ids) == ["leo-a", "leo-b"]
    assert leo.boresight_ecef(channel.id, T0) == pytest.approx(azel_to_ecef(site.lat_deg, site.lon_deg, 0.0, 90.0), abs=1e-12)
    # both satellites are above the site's horizon from the epoch for 8 minutes, as the file says
    assert all(s.orbit.epoch == "2026-01-01T00:00:00Z" for s in leo.satellites)
    xyz = leo.site_xyz(site.id)
    times = T0 + np.arange(0.0, 481.0, 20.0)
    assert leo.warnings(T0) == [] and leo.warnings(T0 + 480.0) == []
    for satellite in leo.satellites:
        orbit = satellite.orbit
        numeric = KeplerOrbit(orbit.a_m, orbit.e, orbit.inc_deg, orbit.raan_deg, orbit.argp_deg,
                              orbit.mean_anomaly_deg, orbit.epoch, orbit.j2)  # fmt: skip
        elevation = look_angles(xyz, orbit_position(numeric, times))[1]
        assert elevation.min() > 0.0, satellite.id
        assert elevation.max() > 50.0
    late = [note["loc"] for note in leo.warnings(T0 + 3600.0)]
    assert late == ["sites.0.channels.0.pointing"] * 2  # long gone an hour later


# --------------------------------------------------------------------------- site time zone


def test_site_time_zone_is_optional_checked_and_round_trips() -> None:
    """``Site.time_zone`` (display only) takes IANA names from the tz database, is left
    out of exports when unset, and survives YAML and JSON round trips."""
    import zoneinfo
    from datetime import datetime, timezone

    from petal_locate.config import config_json_schema

    plain = SystemConfig.model_validate(base())
    assert all(site.time_zone is None for site in plain.sites)
    assert all("time_zone" not in site for site in plain.export_dict()["sites"])

    data = base()
    data["sites"][0]["time_zone"] = "Asia/Tokyo"
    data["sites"][1]["time_zone"] = "UTC"
    model = SystemConfig.model_validate(data)
    assert [s.time_zone for s in model.sites[:2]] == ["Asia/Tokyo", "UTC"]
    assert loads_config(dump_config(model)) == model
    assert loads_config(json.dumps(model.export_dict()), fmt="json") == model
    # What the accepted name means, independently: Tokyo is nine hours east of UTC.
    offset = datetime(2026, 11, 13, tzinfo=timezone.utc).astimezone(zoneinfo.ZoneInfo("Asia/Tokyo")).utcoffset()
    assert offset is not None and offset.total_seconds() == 9 * 3600

    for bad, fragment in (("Mars/Olympus_Mons", "unknown IANA time zone"), ("Asia/Tokyo/../x", "IANA time zone name"),
                          ("", "IANA time zone name"), ("Asia Tokyo", "IANA time zone name"), (9, "string")):
        data["sites"][0]["time_zone"] = bad
        with pytest.raises(ValidationError) as excinfo:
            SystemConfig.model_validate(data)
        problems = field_errors(excinfo.value)
        assert [p["loc"] for p in problems] == ["sites.0.time_zone"], problems
        assert fragment in problems[0]["msg"], problems
    schema = config_json_schema()
    assert "time_zone" in json.dumps(schema["$defs"]["Site"])


# ------------------------------------------------- hostile documents (review SRV-2, M5, SRV-5, SRV-6)


def test_text_that_is_not_valid_unicode_is_refused_on_every_path() -> None:
    """A lone surrogate (a JSON or YAML escape) used to pass validation, be applied, and
    then break GET /config for good (review M5)."""
    from petal_locate.config.loader import ConfigError

    data = base()
    data["description"] = "x\ud800"
    with pytest.raises(ConfigError) as info:  # JSON text with the escape
        loads_config(json.dumps(data), fmt="json")
    assert info.value.errors == [{"loc": "description", "msg": "text that is not valid Unicode (an unpaired surrogate)"}]
    with pytest.raises(ConfigError) as info:  # YAML double-quoted escape
        loads_config(yaml.safe_dump(base()) + 'description: "y\\udce9"\n', fmt="yaml")
    assert info.value.errors[0]["loc"] == "description"
    with pytest.raises(ValidationError, match="sites.0.name: text that is not valid Unicode"):
        bad = base()
        bad["sites"][0]["name"] = "s\udfff"
        SystemConfig.model_validate(bad)  # a decoded JSON body handed straight to the model
    fine = base()
    fine["description"] = "emoji \U0001F600 and accents é"
    assert SystemConfig.model_validate(fine).description == fine["description"]


def test_yaml_aliases_are_budgeted_and_nesting_is_bounded() -> None:
    """A few kilobytes of aliases stood for millions of validated nodes (review SRV-6);
    deep nesting raised RecursionError, a 500 at the API (review SRV-5)."""
    import time as clock

    from petal_locate.config.loader import MAX_YAML_NODES, ConfigError, YamlLimitError, safe_yaml_load

    tp = ("{id: t, uplink_center_hz: 301.0e+6, bandwidth_hz: 25.0e+3, translation_hz: -41.0e+6, "
          "g_over_t_dbk: -14.0, sfd_dbw_m2: -136.0, eirp_sat_dbw: 26.0}")
    n = 2500
    bomb = "\n".join(
        ["version: 1", "name: bomb", "satellites:",
         f"  - &S {{id: s, orbit: {{type: geo, lon_deg: -177.0}}, transponders: [&T {tp}" + ",*T" * (n - 1) + "]}"]
        + ["  - *S"] * (n - 1) + ["sites: []"]
    )
    started = clock.perf_counter()
    with pytest.raises(ConfigError, match=f"more than {MAX_YAML_NODES} nodes") as info:
        loads_config(bomb)
    assert clock.perf_counter() - started < 2.0  # was 17 s of worker time
    assert info.value.errors[0]["loc"] == "(root)"
    # aliases of a sensible size are fine (a shared antenna, a merge key)
    shared = safe_yaml_load("a: &dish {type: parabolic, diameter_m: 1.2}\nb: *dish\nc: {<<: *dish, diameter_m: 2.4}\n")
    assert shared["b"] == {"type": "parabolic", "diameter_m": 1.2} and shared["c"]["diameter_m"] == 2.4
    with pytest.raises(YamlLimitError, match="recursive alias"):
        safe_yaml_load("a: &x [1, *x]\n")
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        safe_yaml_load("a: 1\na: 2\n")
    deep_yaml = "a: " + "[" * 20000 + "]" * 20000
    with pytest.raises(ConfigError, match="nested too deeply"):
        loads_config(deep_yaml)
    deep_json = '{"a": ' + "[" * 100000 + "]" * 100000 + "}"
    with pytest.raises(ConfigError, match="nested too deeply"):
        loads_config(deep_json, fmt="json")


def test_stale_tle_elements_are_warned_about() -> None:
    """A TLE far from its epoch got the same sigmas and no word (review ADAPT-8)."""
    from petal_locate.config.models import tle_epoch_s

    line1 = "1 99999U 26001A   26001.50000000  .00016717  00000-0  10270-3 0  9000"  # fictional elements
    line2 = "2 99999  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391    00"
    from petal_locate.core.orbits import tle_checksum

    line1 = line1[:68] + str(tle_checksum(line1))
    line2 = line2[:68] + str(tle_checksum(line2))
    epoch = tle_epoch_s(line1)
    assert epoch == dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc).timestamp()
    data = base()
    data["satellites"][0]["orbit"] = {"type": "tle", "line1": line1, "line2": line2}
    cfg = SystemConfig.model_validate(data)

    def tle_notes(t: float) -> list[str]:
        return [n["msg"] for n in cfg.warnings(t) if n["loc"] == "satellites.0.orbit" and "TLE epoch" in n["msg"]]

    assert tle_notes(epoch + 2 * 86400) == []  # a low orbit, 2 days: fresh enough
    (note,) = tle_notes(epoch + 10 * 86400)  # 10 days > 3 days for a 15.7 rev/day orbit
    assert "10 days before the time evaluated" in note and "low or medium" in note and "load current elements" in note
    assert tle_notes(epoch - 5 * 86400)  # before the epoch counts too
    assert all("TLE epoch" not in n["msg"] for n in cfg.warnings())  # no time, no age check
