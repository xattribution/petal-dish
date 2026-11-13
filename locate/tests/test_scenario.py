"""Tests of the scenario models (sim/scenario.py) and the shipped scenario files
(config/scenarios/*.yaml) against config/pacific-uhf.yaml."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from petal_locate.config.loader import load_config
from petal_locate.config.models import SystemConfig
from petal_locate.core.geodesy import geodesic_distance_m
from petal_locate.core.linkbudget import RADIO_HORIZON_M_PER_SQRT_M
from petal_locate.sim import (
    Scenario,
    ScenarioError,
    SimWorld,
    dump_scenario,
    load_scenario,
    loads_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "pacific-uhf.yaml"
SCENARIO_DIR = ROOT / "config" / "scenarios"
SCENARIO_FILES = sorted(SCENARIO_DIR.glob("*.yaml"))
REQUIRED = {"pacific-demo", "uplink-cw", "uplink-swept", "local-interferer", "two-jammers", "clock-fault", "quiet"}


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(CONFIG_PATH)


def emitter(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"id": "e1", "lat_deg": 20.0, "lon_deg": 132.0, "freq_hz": 301.005e6, "eirp_dbw": 20.0,
                            "waveform": {"type": "noise", "bandwidth_hz": 10e3}}
    spec.update(overrides)
    return spec


def build(emitters: list[dict[str, Any]], errors: dict[str, Any] | None = None) -> Scenario:
    return Scenario.model_validate({"name": "t", "emitters": emitters, "errors": errors or {}})


# --------------------------------------------------------------------------- shipped files


def test_required_presets_exist() -> None:
    assert REQUIRED <= {p.stem for p in SCENARIO_FILES}


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=[p.stem for p in SCENARIO_FILES])
def test_scenario_file_loads_validates_and_builds(path: Path, cfg: SystemConfig) -> None:
    sc = load_scenario(path)
    sc.validate_against(cfg)
    assert sc.name == path.stem  # the server lists presets by file stem
    assert "\n" not in sc.description.strip() and len(sc.description) > 200  # one paragraph
    assert "simulated" in sc.description.lower()
    assert loads_scenario(dump_scenario(sc)) == sc
    world = SimWorld(cfg, sc)
    assert {e.id for e in world.emitters} >= {"ref-guam", "ref-sapporo"}  # the references radiate
    for e in sc.emitters:
        assert "simulated" in e.name.lower()


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=[p.stem for p in SCENARIO_FILES])
def test_scenario_positions_are_fictional(path: Path, cfg: SystemConfig) -> None:
    """Uplink jammers sit at open-ocean points far from every configured site and
    reference; a local interferer is a generic offset inside exactly one site's radio horizon."""
    sc = load_scenario(path)
    places = [(s.lat_deg, s.lon_deg) for s in cfg.sites] + [
        (c.location.lat_deg, c.location.lon_deg) for c in cfg.carriers if c.location is not None
    ]
    for e in sc.emitters:
        dists = [float(geodesic_distance_m(e.lat_deg, e.lon_deg, la, lo)) for la, lo in places]
        if e.kind == "local":
            horizon = RADIO_HORIZON_M_PER_SQRT_M * 2 * math.sqrt(10.0)
            assert sum(d < horizon for d in dists) == 1
        else:
            assert min(dists) > 500e3, e.id


def test_scenario_specifics(cfg: SystemConfig) -> None:
    sc = {p.stem: load_scenario(p) for p in SCENARIO_FILES}
    demo = sc["pacific-demo"]
    (j,) = demo.emitters
    assert (j.lat_deg, j.lon_deg) == (20.0, 132.0) and j.waveform.type == "noise"
    shared1 = [tp for s in cfg.satellites for tp in s.transponders if tp.relays(j.freq_hz)]
    assert {tp.id for tp in shared1} == {"shared-1"} and len(shared1) == 3  # seen by all three satellites
    # truth errors: ephemeris inside the operator sigmas, a clock offset of tens of ns
    sigma = cfg.processing.uncertainty.ephemeris.operator.pos_m
    for err in demo.errors.ephemeris.values():
        assert all(abs(v) <= s for v, s in zip(err.ric_pos_m, sigma))
        assert err.ric_vel_mps == [0.0, 0.0, 0.0]  # no growing offset: valid at any date
    assert all(10.0 <= abs(v) <= 100.0 for v in demo.errors.site_clock_offset_ns.values())
    assert demo.errors.sat_osc_frac and all(abs(v) < cfg.processing.uncertainty.sat_osc_frac
                                            for v in demo.errors.sat_osc_frac.values())
    assert sc["uplink-cw"].emitters[0].waveform.type == "cw"
    assert sc["uplink-swept"].emitters[0].waveform.type == "chirp"
    assert sc["quiet"].emitters == []
    a, b = sc["two-jammers"].emitters
    tp_a = {tp.id for s in cfg.satellites for tp in s.transponders if tp.relays(a.freq_hz)}
    tp_b = {tp.id for s in cfg.satellites for tp in s.transponders if tp.relays(b.freq_hz)}
    assert tp_a and tp_b and not (tp_a & tp_b)
    fault = sc["clock-fault"]
    assert fault.emitters == demo.emitters and fault.seed == demo.seed
    diff = {k: v for k, v in fault.errors.site_clock_offset_ns.items()
            if demo.errors.site_clock_offset_ns.get(k) != v}
    assert list(diff.values()) == [2000.0]
    local = sc["local-interferer"].emitters[0]
    assert local.kind == "local"
    assert not any(tp.relays(local.freq_hz) for s in cfg.satellites for tp in s.transponders)
    assert any(tp.downlink_lo_hz <= local.freq_hz <= tp.downlink_hi_hz for tp in cfg.satellite("muos-pac").transponders)
    # no jammer overlaps a reference carrier's band
    for name, s in sc.items():
        for e in s.emitters:
            half = 0.5 * (e.waveform.bandwidth_hz if e.waveform else 0.0)
            for c in cfg.carriers:
                assert e.freq_hz + half <= c.uplink_lo_hz or e.freq_hz - half >= c.uplink_hi_hz, (name, e.id, c.id)


# --------------------------------------------------------------------------- validation errors name the field


@pytest.mark.parametrize(
    "emitters, errors, field",
    [
        ([emitter(carrier_id="no-such-carrier")], None, r"emitters\[0\] \(e1\)\.carrier_id"),
        ([emitter(pointing={"satellite_id": "no-such-sat"})], None, r"emitters\[0\] \(e1\)\.pointing\.satellite_id"),
        ([emitter()], {"ephemeris": {"no-such-sat": {"ric_pos_m": [0, 0, 0]}}}, r"errors\.ephemeris"),
        ([emitter()], {"sat_osc_frac": {"no-such-sat": 1e-7}}, r"errors\.sat_osc_frac"),
        ([emitter()], {"site_clock_offset_ns": {"atlantis": 10.0}}, r"errors\.site_clock_offset_ns"),
        ([emitter()], {"site_osc_frac": {"atlantis": 1e-9}}, r"errors\.site_osc_frac"),
        ([emitter(id="ref-guam")], None, r"emitters\[0\] \(ref-guam\)\.id"),
        ([emitter(waveform={"type": "psk", "symbol_rate_hz": 1000, "order": 8})], None,
         r"emitters\[0\] \(e1\)\.waveform\.order"),
    ],
)
def test_validate_against_names_the_field(cfg: SystemConfig, emitters: Any, errors: Any, field: str) -> None:
    with pytest.raises(ScenarioError, match=field):
        build(emitters, errors).validate_against(cfg)


def test_carrier_without_location_or_emitter_needs_explicit_fields(cfg: SystemConfig) -> None:
    data = cfg.export_dict()
    data["carriers"].append({"id": "plain", "name": "Plain carrier", "satellite_id": "muos-pac",
                             "transponder_id": "shared-2", "uplink_center_hz": 301.13e6, "bandwidth_hz": 5e3})
    plain_cfg = SystemConfig.model_validate(data)
    with pytest.raises(ScenarioError, match=r"emitters\[0\] \(x\).*lat_deg and lon_deg"):
        build([{"id": "x", "carrier_id": "plain"}]).validate_against(plain_cfg)
    with pytest.raises(ScenarioError, match=r"emitters\[0\] \(x\).*eirp_dbw and waveform"):
        build([{"id": "x", "carrier_id": "plain", "lat_deg": 1.0, "lon_deg": 150.0}]).validate_against(plain_cfg)
    ok = build([{"id": "x", "carrier_id": "plain", "lat_deg": 1.0, "lon_deg": 150.0, "eirp_dbw": 0.0,
                 "waveform": {"type": "cw"}}])
    ok.validate_against(plain_cfg)
    e = SimWorld(plain_cfg, ok).emitter("x")
    assert e.freq_hz == 301.13e6 and e.alt_m == 0.0 and e.antenna_height_m == 10.0 and e.pointing == "omni"


@pytest.mark.parametrize(
    "text, field",
    [
        ("name: t\nemitters: [{id: a, lon_deg: 1, freq_hz: 3e8, eirp_dbw: 0, waveform: {type: cw}}]", "lat_deg"),
        ("name: t\nemitters: [{id: a, kind: carrier}]", "carrier_id"),
        ("name: t\nemitters: [{id: a, carrier_id: ref-guam, t_start: '2026-11-13T00:00:01Z', "
         "t_stop: '2026-11-13T00:00:00Z'}]", "t_stop"),
        ("name: t\nemitters: [{id: a, carrier_id: ref-guam}, {id: a, carrier_id: ref-sapporo}]", r"emitters\[1\]\.id"),
        ("name: t\nemitters: [{id: a, carrier_id: ref-guam, waveform: {type: noise}}]", "bandwidth_hz"),
        ("name: t\nemitters: [{id: a, carrier_id: ref-guam, waveform: {type: noise, bandwidth_hz: 1e3, duty: 0.5}}]",
         "period_s"),
        ("name: t\nemitters: [{id: a, carrier_id: ref-guam, colour: red}]", "colour"),
        ("name: t\nerrors: {iono: {enabled: true, vtec_tecu: -1}}", "vtec_tecu"),
        ("- just a list", "mapping"),
    ],
)
def test_model_errors_name_the_field(text: str, field: str) -> None:
    with pytest.raises(ScenarioError, match=field):
        loads_scenario(text)


def test_load_scenario_names_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(ScenarioError, match="missing.yaml"):
        load_scenario(missing)
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: t\nemitters: [{id: a}]\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match=r"bad\.yaml.*lat_deg"):
        load_scenario(bad)


def test_round_trip_keeps_every_field() -> None:
    sc = build(
        [emitter(name="n", kind="jammer", alt_m=5.0, antenna_height_m=20.0,
                 antenna={"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 40.0},
                 pointing={"az_deg": 90.0, "el_deg": 30.0},
                 waveform={"type": "psk", "symbol_rate_hz": 2400, "order": 2, "rolloff": 0.25, "seed": 3},
                 enabled=False, t_start="2026-11-13T00:00:00Z")],
        {"ephemeris": {"muos-pac": {"ric_pos_m": [1, 2, 3], "ric_vel_mps": [0.1, 0.2, 0.3],
                                    "t_ref": "2026-11-13T00:00:00Z"}},
         "iono": {"enabled": True, "vtec_tecu": 25.0}},
    )
    again = loads_scenario(dump_scenario(sc))
    assert again == sc
    assert sc.emitter("e1").waveform.seed == 3
    with pytest.raises(ScenarioError, match="emitter_id"):
        sc.emitter("nobody")
    assert np.isclose(again.errors.ephemeris["muos-pac"].ric_vel_mps[2], 0.3)
