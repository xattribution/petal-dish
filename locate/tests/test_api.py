"""Tests of the console REST API (``server/api/rest.py``, ``deps.py``), the node backplane
route and static serving (``server/app.py``), against the real application and runtime
in-process (``httpx.ASGITransport`` with the application's lifespan running).

The configuration is a small synthetic one (two geostationary satellites, three sites)
so that nothing here depends on the shipped example; one test also runs the example.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import math
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import yaml

from petal_locate.config import SystemConfig, dump_config
from petal_locate.core.geodesy import geodesic_distance_m, lla_to_ecef
from petal_locate.core.geometry import predict_tdoa
from petal_locate.core.linkbudget import path_budget
from petal_locate.core.timebase import sec
from petal_locate.core.types import Measurement, PathSpec
from petal_locate.server.api import rest, schemas
from petal_locate.server.app import HubProviders, create_app
from petal_locate.server.pipeline.events import Unit
from petal_locate.server.runtime import SIM_BACKPLANE_PATH, ServerRuntime, StoreError
from petal_locate.server.settings import Marking, Settings
from petal_locate.sim.scenario import Scenario, dump_scenario

ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1"
T_FIXED = "2026-11-13T12:00:00Z"


# --------------------------------------------------------------------------- fixtures


def make_config(name: str = "api-test") -> SystemConfig:
    """Two GEO satellites (140 E, 160 E), three sites; a small region."""

    def transponder(translation: float) -> dict[str, Any]:
        return {"id": "t1", "uplink_center_hz": 300.0e6, "bandwidth_hz": 25e3, "translation_hz": translation,
                "g_over_t_dbk": -14.0, "sfd_dbw_m2": -136.0, "eirp_sat_dbw": 26.0}

    small_dish = {"type": "parabolic", "diameter_m": 0.72}  # 0.62 wavelengths at 260 MHz
    beam = {"type": "beam", "gain_dbi": 10.0, "hpbw_deg": 40.0}
    return SystemConfig.model_validate(
        {
            "name": name,
            "satellites": [
                {"id": "sat-a", "name": "Sat A", "orbit": {"type": "geo", "lon_deg": 140.0},
                 "transponders": [transponder(-40.0e6)]},
                {"id": "sat-b", "name": "Sat B", "orbit": {"type": "geo", "lon_deg": 160.0},
                 "transponders": [transponder(-33.6e6)]},
            ],
            "sites": [
                {"id": "alpha", "name": "Alpha", "lat_deg": 35.0, "lon_deg": 139.0, "channels": [
                    {"id": "alpha-a", "antenna": small_dish, "pointing": {"satellite_id": "sat-a"}},
                    {"id": "alpha-b", "antenna": beam, "pointing": {"satellite_id": "sat-b"}},
                ]},
                {"id": "bravo", "name": "Bravo", "lat_deg": 17.0, "lon_deg": 141.0, "channels": [
                    {"id": "bravo-a", "antenna": beam, "pointing": {"satellite_id": "sat-a"}},
                    {"id": "bravo-b", "antenna": beam, "pointing": {"satellite_id": "sat-b"}},
                ]},
                {"id": "charlie", "name": "Charlie", "lat_deg": -5.0, "lon_deg": 150.0, "channels": [
                    {"id": "charlie-ab", "antenna": {"type": "isotropic"},
                     "pointing": {"az_deg": 0.0, "el_deg": 85.0, "satellite_ids": ["sat-a", "sat-b"]}},
                ]},
            ],
            "carriers": [
                {"id": "ref-1", "name": "Reference", "satellite_id": "sat-a", "transponder_id": "t1",
                 "uplink_center_hz": 299.9925e6, "bandwidth_hz": 5e3,
                 "location": {"lat_deg": 13.4, "lon_deg": 144.8},
                 "emitter": {"eirp_dbw": 13.0, "bandwidth_hz": 5e3}, "use_as_reference": True},
                {"id": "plain", "name": "Plain carrier", "satellite_id": "sat-b", "transponder_id": "t1",
                 "uplink_center_hz": 300.005e6, "bandwidth_hz": 5e3},
            ],
            "processing": {"region": {"lat_min": -30.0, "lat_max": 50.0, "lon_min": 110.0, "lon_max": 180.0}},
        }
    )


def make_scenario(name: str = "base", lat: float = 20.0) -> Scenario:
    return Scenario.model_validate(
        {
            "name": name,
            "description": f"{name} (simulated)",
            "emitters": [
                {"id": "jam-1", "name": "Jammer", "kind": "jammer", "lat_deg": lat, "lon_deg": 135.0,
                 "freq_hz": 300.0e6, "eirp_dbw": 20.0, "waveform": {"type": "noise", "bandwidth_hz": 10e3}},
                {"id": "late", "kind": "jammer", "lat_deg": 0.0, "lon_deg": 150.0, "freq_hz": 300.0e6,
                 "eirp_dbw": 10.0, "waveform": {"type": "cw"}, "t_start": "2099-01-01T00:00:00Z"},
            ],
        }
    )


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    config = tmp_path / "config.yaml"
    config.write_text(dump_config(make_config()))
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(dump_scenario(make_scenario()))
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "north.yaml").write_text(dump_scenario(make_scenario("north", lat=30.0)))
    (presets / "broken.yaml").write_text("name: [unclosed\n")
    return {"config": config, "scenario": scenario, "presets": presets, "data": tmp_path / "data"}


def settings_for(files: dict[str, Path], *, scenario: bool = True, **extra: Any) -> Settings:
    values: dict[str, Any] = {
        "config_path": files["config"],
        "scenario_path": files["scenario"] if scenario else None,
        "data_dir": files["data"],
        "node_tokens": {"alpha": "token-alpha", "bravo": "token-bravo"},
        "marking": Marking(level="UNCLASSIFIED", line="UNCLASSIFIED // TEST"),
    }
    values.update(extra)
    return Settings(**values)


@contextlib.asynccontextmanager
async def serving(settings: Settings, presets: Path | None = None, static_dir: Path | None = None,
                  headers: dict[str, str] | None = None) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    """The whole application with its lifespan running, and a client for it."""
    app = create_app(settings, presets_dir=presets, static_dir=static_dir)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        # the console's own header: a state-changing request without a JSON body needs it
        sent = {"X-Petal-Client": "test", **(headers or {})}
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=sent) as client:
            yield app, client


def runtime_of(app: Any) -> ServerRuntime:
    runtime = app.state.runtime
    assert isinstance(runtime, ServerRuntime)
    return runtime


def no_nan(text: str) -> None:
    assert "NaN" not in text and "Infinity" not in text


def make_event(runtime: ServerRuntime, lo: float = 299.995e6, hi: float = 300.005e6) -> str:
    """An active event with one detection on alpha-a (kept: no window expires it)."""
    now = time.time_ns()
    record = runtime.events.create(space="up", lo_hz=lo, hi_hz=hi, first_seen_ns=now, last_seen_ns=now)
    unit = Unit(
        key=("alpha-a", "sat-a:t1", record.seq, 0, "sat-a"), channel_id="alpha-a", monitor_id="sat-a:t1", site_id="alpha",
        sat_id="sat-a", transponder_id="t1", space="up", lo_hz=lo, hi_hz=hi, down_lo_hz=lo - 40e6,
        down_hi_hz=hi - 40e6, snr_db=12.0, cn0_dbhz=52.0, power_db=12.0, first_seen_ns=now, last_seen_ns=now,
        authorized_carrier_id=None, anomaly=False, received_ns=now,
    )
    record.members[unit.key] = unit
    runtime.correlator.refresh()
    return record.id


# --------------------------------------------------------------------------- meta, status


async def test_meta_status_and_openapi(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (app, client):
        reply = await client.get(f"{API}/meta")
        assert reply.status_code == 200
        meta = reply.json()
        assert meta["marking"] == {"level": "UNCLASSIFIED", "line": "UNCLASSIFIED // TEST"}
        assert meta["simulation"] is True and meta["api_token_required"] is False
        assert meta["config_name"] == "api-test" and meta["version"]
        assert isinstance(meta["server_t_ns"], str) and abs(int(meta["server_t_ns"]) - time.time_ns()) < 60e9

        status = (await client.get(f"{API}/status")).json()
        schemas.Status.model_validate(status)
        assert [n["site_id"] for n in status["nodes"]] == ["alpha", "bravo", "charlie"]
        assert not any(n["online"] for n in status["nodes"])
        assert status["config_version"] == 1 and status["events_active"] == 0
        assert set(status["queue"]) == {"captures_pending", "jobs_running", "jobs_waiting"}

        reply = await client.get("/openapi.json")
        assert reply.status_code == 200
        doc = reply.json()
        expected = [
            "/meta", "/config", "/config/validate", "/config/schema", "/status", "/events", "/events/{event_id}",
            "/events/{event_id}/close", "/events/{event_id}/lop", "/geometry/subpoints", "/geometry/footprints",
            "/geometry/look-angles", "/analysis/accuracy", "/analysis/link-budget", "/sim/scenario",
            "/sim/presets", "/sim/presets/{name}/load", "/sim/reset", "/sim/truth",
        ]
        for path in expected:
            assert f"{API}{path}" in doc["paths"], path
        assert SIM_BACKPLANE_PATH in doc["paths"]
        components = doc["components"]["schemas"]
        for name in ("SystemConfig", "Scenario", "Event", "ConfigEnvelope", "FieldErrorResponse", "LopCollection"):
            assert name in components, name
        ok = doc["paths"][f"{API}/events/{{event_id}}"]["get"]["responses"]["200"]
        assert ok["content"]["application/json"]["schema"]["$ref"].endswith("/Event")
        put = doc["paths"][f"{API}/config"]["put"]
        assert put["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/SystemConfig")
        assert "HTTPValidationError" not in components  # every 422 is documented as FieldErrorResponse


async def test_console_routes_answer_503_without_a_runtime(files: dict[str, Path]) -> None:
    app = create_app(settings_for(files), hub_providers=HubProviders.unconfigured())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            for path in ("/meta", "/status", "/events", "/config"):
                reply = await client.get(f"{API}{path}")
                assert reply.status_code == 503 and reply.headers["retry-after"]
                assert "starting" in reply.json()["detail"]
            assert (await client.get("/healthz")).json()["status"] == "ok"


async def test_unexpected_errors_do_not_leak(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    async with serving(settings_for(files), files["presets"]) as (app, client):

        def boom() -> Any:
            raise RuntimeError("secret internal state at 0xdeadbeef")

        monkeypatch.setattr(runtime_of(app), "status", boom)
        reply = await client.get(f"{API}/status")
        assert reply.status_code == 500
        assert reply.json() == {"detail": "internal error; see the server log"}
        assert "deadbeef" not in reply.text and "Traceback" not in reply.text
        reply = await client.get(f"{API}/no-such-route")
        assert reply.status_code == 404 and reply.json() == {"detail": "Not Found"}

        async def disk_full(*args: Any, **kwargs: Any) -> Any:
            raise StoreError("config_versions: database or disk is full (/srv/data/petal-locate.sqlite3)")

        monkeypatch.setattr(runtime_of(app), "replace_config", disk_full)
        reply = await client.put(f"{API}/config", json={"name": "x"}, headers={"If-Match": "1"})
        assert reply.status_code == 500 and "storage failure" in reply.json()["detail"]
        assert "/srv/data" not in reply.text
        assert (await client.post(f"{API}/no-such-route")).status_code == 405


# --------------------------------------------------------------------------- configuration


async def test_get_config_with_etag_warnings_and_yaml_export(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        reply = await client.get(f"{API}/config")
        assert reply.status_code == 200 and reply.headers["etag"] == '"1"'
        envelope = schemas.ConfigEnvelope.model_validate(reply.json())
        assert envelope.version == 1 and envelope.config.name == "api-test"
        assert set(envelope.derived_monitors) == {"alpha-a", "alpha-b", "bravo-a", "bravo-b", "charlie-ab"}
        assert any(w.loc == "sites.0.channels.0.antenna" and "3 wavelengths" in w.msg for w in envelope.warnings)

        reply = await client.get(f"{API}/config", params={"format": "yaml"})
        assert reply.status_code == 200 and reply.headers["etag"] == '"1"'
        assert reply.headers["content-type"].startswith("application/yaml")
        assert 'filename="api-test-v1.yaml"' in reply.headers["content-disposition"]
        exported = yaml.safe_load(reply.text)
        assert exported["name"] == "api-test" and exported["sites"][0]["channels"][0]["monitors"] == []
        assert SystemConfig.model_validate(exported).sites[0].channels[0].monitors_derived

        reply = await client.get(f"{API}/config", params={"format": "xml"})
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "format"

        schema = (await client.get(f"{API}/config/schema")).json()
        assert schema["title"] == "SystemConfig" and "satellites" in schema["properties"]


async def test_put_config_if_match_conflict_and_field_errors(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (app, client):
        current = (await client.get(f"{API}/config")).json()["config"]

        # No If-Match: 428.
        reply = await client.put(f"{API}/config", json=current)
        assert reply.status_code == 428 and "If-Match" in reply.json()["detail"]

        # JSON body with the right version: accepted, version 2; the body's own version is ignored.
        current["description"] = "edited through the API"
        current["version"] = 99
        reply = await client.put(f"{API}/config", json=current, headers={"If-Match": '"1"'})
        assert reply.status_code == 200, reply.text
        assert reply.headers["etag"] == '"2"' and reply.json()["version"] == 2
        assert runtime_of(app).config.description == "edited through the API"

        # Stale version: 409 with the current version (body and ETag).
        reply = await client.put(f"{API}/config", json=current, headers={"If-Match": '"1"'})
        assert reply.status_code == 409
        assert reply.json()["current_version"] == 2 and reply.headers["etag"] == '"2"'
        reply = await client.put(f"{API}/config", json=current, headers={"If-Match": "banana"})
        assert reply.status_code == 409 and reply.json()["current_version"] == 2

        # YAML body (an exported file) with W/ etag syntax.
        exported = (await client.get(f"{API}/config", params={"format": "yaml"})).text
        reply = await client.put(f"{API}/config", content=exported,
                                 headers={"If-Match": 'W/"2"', "Content-Type": "application/yaml"})
        assert reply.status_code == 200 and reply.json()["version"] == 3

        # The envelope the server returns is accepted back as a body.
        envelope = (await client.get(f"{API}/config")).json()
        reply = await client.put(f"{API}/config", json=envelope, headers={"If-Match": "*"})
        assert reply.status_code == 200 and reply.json()["version"] == 4

        # Field errors: 422 naming each field; nothing changes.
        bad = json.loads(json.dumps(current))
        bad["sites"][0]["lat_deg"] = 123.0
        bad["satellites"][1]["transponders"][0]["bandwidth_hz"] = -1.0
        reply = await client.put(f"{API}/config", json=bad, headers={"If-Match": "4"})
        assert reply.status_code == 422
        body = schemas.FieldErrorResponse.model_validate(reply.json())
        locs = {e.loc for e in body.errors}
        assert "sites.0.lat_deg" in locs and "satellites.1.transponders.0.bandwidth_hz" in locs
        assert body.detail.startswith("the configuration does not validate")
        assert runtime_of(app).config_version == 4

        # A YAML syntax error, an empty body, an unsupported type, a body too large.
        reply = await client.put(f"{API}/config", content="name: [oops\n",
                                 headers={"If-Match": "4", "Content-Type": "text/yaml"})
        assert reply.status_code == 422 and "not valid YAML" in reply.json()["errors"][0]["msg"]
        reply = await client.put(f"{API}/config", content=b"", headers={"If-Match": "4"})
        assert reply.status_code == 422 and reply.json()["errors"][0]["msg"] == "the request has no body"
        reply = await client.put(f"{API}/config", content=b"<xml/>",
                                 headers={"If-Match": "4", "Content-Type": "application/xml"})
        assert reply.status_code == 415
        reply = await client.put(f"{API}/config", content=b"\xff\xfe", headers={"If-Match": "4"})
        assert reply.status_code == 422


async def test_put_config_refuses_large_bodies(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest, "MAX_BODY_BYTES", 1000)
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        reply = await client.put(f"{API}/config", content=b"x" * 2000,
                                 headers={"If-Match": "1", "Content-Type": "application/yaml"})
        assert reply.status_code == 413


async def test_validate_config(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        good = (await client.get(f"{API}/config")).json()["config"]
        result = schemas.ValidateResult.model_validate((await client.post(f"{API}/config/validate", json=good)).json())
        assert result.ok and not result.errors and result.warnings

        bad = dict(good, sites=[dict(good["sites"][0], id="bad id!")])
        reply = await client.post(f"{API}/config/validate", json=bad)
        assert reply.status_code == 200
        result = schemas.ValidateResult.model_validate(reply.json())
        assert not result.ok and result.errors[0].loc == "sites.0.id"

        reply = await client.post(f"{API}/config/validate", content=dump_config(make_config("as-yaml")),
                                  headers={"Content-Type": "application/x-yaml"})
        assert reply.json()["ok"] is True

        reply = await client.post(f"{API}/config/validate", content="{not json", headers={"Content-Type": "application/json"})
        assert reply.json()["ok"] is False and "not valid JSON" in reply.json()["errors"][0]["msg"]


# --------------------------------------------------------------------------- events and lines of position


async def test_events_list_get_close_and_errors(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (app, client):
        runtime = runtime_of(app)
        assert (await client.get(f"{API}/events")).json() == []
        first = make_event(runtime, 299.990e6, 299.995e6)
        second = make_event(runtime, 300.006e6, 300.008e6)
        listed = (await client.get(f"{API}/events")).json()
        assert [e["id"] for e in listed] == [second, first]  # newest first
        for item in listed:
            schemas.EventSummary.model_validate(item)
            assert item["simulated"] is True
        assert len((await client.get(f"{API}/events", params={"limit": 1})).json()) == 1
        assert {e["id"] for e in (await client.get(f"{API}/events", params={"state": "active"})).json()} == {first, second}

        event = (await client.get(f"{API}/events/{first}")).json()
        schemas.Event.model_validate(event)
        assert event["id"] == first and event["uplink_lo_hz"] == 299.990e6 and event["advisory"]["headline"]
        no_nan(json.dumps(event))

        reply = await client.post(f"{API}/events/{first}/close")
        assert reply.status_code == 200 and reply.json()["state"] == "closed"
        assert (await client.post(f"{API}/events/{first}/close")).json()["state"] == "closed"  # idempotent
        closed = (await client.get(f"{API}/events", params={"state": "closed"})).json()
        assert [e["id"] for e in closed] == [first]
        assert [e["id"] for e in (await client.get(f"{API}/events", params={"state": "active"})).json()] == [second]

        reply = await client.get(f"{API}/events/EV-999999")
        assert reply.status_code == 404 and "EV-999999" in reply.json()["detail"]
        assert (await client.post(f"{API}/events/EV-999999/close")).status_code == 404
        assert (await client.get(f"{API}/events/EV-999999/lop")).status_code == 404
        reply = await client.get(f"{API}/events", params={"state": "sleeping"})
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "state"
        reply = await client.get(f"{API}/events", params={"limit": 0})
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "limit"


async def test_lines_of_position_follow_the_measurement_and_are_cached(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with serving(settings_for(files), files["presets"]) as (app, client):
        runtime = runtime_of(app)
        event_id = make_event(runtime)
        assert (await client.get(f"{API}/events/{event_id}/lop")).json() == {"type": "FeatureCollection", "features": []}

        cfg = runtime.config
        truth = (20.0, 135.0)
        t_ns = 1_794_000_000 * 10**9
        a = PathSpec("sat-a", "alpha", 300.0e6, -40.0e6)
        b = PathSpec("sat-b", "alpha", 300.0e6, -33.6e6)
        x = lla_to_ecef(*truth, 0.0)
        value = float(predict_tdoa(x, a, b, sec(t_ns), cfg.orbits(), cfg.sites_xyz()))
        measurement = Measurement(kind="tdoa", value=value, sigma=2e-6, t_ns=t_ns, a=a, b=b)
        row = schemas.MeasurementRow(
            t_ns=t_ns, kind="tdoa",
            pair=schemas.PairRef(a=schemas.ChannelRef(sat_id="sat-a", site_id="alpha", channel_id="alpha-a"),
                                 b=schemas.ChannelRef(sat_id="sat-b", site_id="alpha", channel_id="alpha-b")),
            value=value, sigma=2e-6, residual=None, snr_db=30.0, calibrated=False, used=True, reason=None,
        )
        seq = runtime.events.add_measurement(event_id, row, measurement)
        assert seq == 1

        calls: list[int] = []
        original = rest._lop_features

        def counting(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(rest, "_lop_features", counting)
        reply = await client.get(f"{API}/events/{event_id}/lop")
        assert reply.status_code == 200
        collection = schemas.LopCollection.model_validate(reply.json())
        roles = sorted(f.properties.role for f in collection.features)
        assert roles == ["band", "line"]
        line = next(f for f in collection.features if f.properties.role == "line")
        props = line.properties
        assert (props.a_channel_id, props.b_channel_id, props.seq, props.event_id) == ("alpha-a", "alpha-b", 1, event_id)
        assert props.kind == "tdoa" and props.t_ns == t_ns and not props.calibrated
        assert line.geometry["type"] == "MultiLineString"
        vertices = np.array([v for part in line.geometry["coordinates"] for v in part])
        lon_w = (vertices[:, 0] + 180.0) % 360.0 - 180.0
        nearest = float(np.min(geodesic_distance_m(truth[0], truth[1], vertices[:, 1], lon_w)))
        assert nearest < 40e3, nearest  # the line passes through the emitter (grid 0.25 degrees)
        no_nan(reply.text)

        again = await client.get(f"{API}/events/{event_id}/lop")
        assert again.content == reply.content and len(calls) == 1  # cached per solution and measurement set
        reply = await client.get(f"{API}/events/{event_id}/lop", params={"step_deg": 0.01})
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "step_deg"


def test_lop_step_keeps_the_grid_bounded() -> None:
    from petal_locate.config import Region

    small = Region(lat_min=-30, lat_max=50, lon_min=110, lon_max=180)
    assert rest.lop_step_deg(small) == 0.25
    world = Region()
    step = rest.lop_step_deg(world)
    lat, lon = world.grid(step)
    assert lat.size * lon.size <= rest.LOP_MAX_CELLS and step < 0.6
    wrapped = Region(lat_min=-60, lat_max=60, lon_min=60, lon_max=-120)
    lat, lon = wrapped.grid(rest.lop_step_deg(wrapped))
    assert lat.size * lon.size <= rest.LOP_MAX_CELLS


# --------------------------------------------------------------------------- geometry


async def test_geometry_routes(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        subpoints = (await client.get(f"{API}/geometry/subpoints", params={"t": T_FIXED})).json()
        assert [p["sat_id"] for p in subpoints] == ["sat-a", "sat-b"]
        assert subpoints[0]["lon_deg"] == pytest.approx(140.0, abs=0.01) and abs(subpoints[0]["lat_deg"]) < 0.01
        assert subpoints[1]["alt_m"] == pytest.approx(35_786e3, rel=1e-3)

        rings = (await client.get(f"{API}/geometry/footprints", params={"t": T_FIXED, "min_elevation_deg": 10})).json()
        assert [r["sat_id"] for r in rings] == ["sat-a", "sat-b"]
        ring = rings[0]["ring"]
        assert ring[0] == ring[-1] and rings[0]["min_elevation_deg"] == 10.0
        lats = [p[1] for p in ring]
        assert 60 < max(lats) < 75  # 10 degrees elevation from GEO reaches about 71 degrees
        reply = await client.get(f"{API}/geometry/footprints", params={"min_elevation_deg": 95})
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "min_elevation_deg"

        rows = (await client.get(f"{API}/geometry/look-angles", params={"t": T_FIXED})).json()
        assert len(rows) == 5 * 2  # every channel by every satellite
        by_key = {(r["channel_id"], r["sat_id"]): schemas.LookAngle.model_validate(r) for r in rows}
        own = by_key[("alpha-a", "sat-a")]
        assert own.watched and own.visible and own.off_axis_deg == pytest.approx(0.0, abs=1e-6)
        assert own.electrically_small and own.gain_dbi == pytest.approx(own.peak_gain_dbi)
        other = by_key[("alpha-a", "sat-b")]
        assert not other.watched and other.off_axis_deg > 5.0
        beam = by_key[("bravo-a", "sat-b")]
        assert not beam.electrically_small and beam.gain_dbi < beam.peak_gain_dbi
        assert by_key[("charlie-ab", "sat-a")].watched and by_key[("charlie-ab", "sat-b")].watched
        assert own.freq_hz == 260.0e6 and by_key[("alpha-b", "sat-b")].freq_hz == 266.4e6

        reply = await client.get(f"{API}/geometry/subpoints", params={"t": "yesterday"})
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "t"


def test_look_angles_against_an_independent_calculation() -> None:
    """Elevation of a GEO satellite from a site, from spherical trigonometry."""
    cfg = make_config()
    t_ns = 1_794_000_000 * 10**9
    row = next(r for r in rest.compute_look_angles(cfg, t_ns) if (r.channel_id, r.sat_id) == ("bravo-a", "sat-a"))
    lat, dlon = math.radians(17.0), math.radians(140.0 - 141.0)
    r_ratio = 6378.137 / 42164.17
    cos_g = math.cos(lat) * math.cos(dlon)
    el = math.degrees(math.atan2(cos_g - r_ratio, math.sqrt(1 - cos_g**2)))
    assert row.el_deg == pytest.approx(el, abs=0.3)  # spherical Earth versus WGS84
    assert row.range_km == pytest.approx(42164.17 * math.sqrt(1 + r_ratio**2 - 2 * r_ratio * cos_g), rel=2e-3)


# --------------------------------------------------------------------------- analysis


async def test_link_budget(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (app, client):
        body = {"emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 10e3}, "f_e_hz": 300.0e6, "lat_deg": 20.0,
                "lon_deg": 135.0, "t": T_FIXED}
        reply = await client.post(f"{API}/analysis/link-budget", json=body)
        assert reply.status_code == 200
        response = schemas.LinkBudgetResponse.model_validate(reply.json())
        pairs = {(r.sat_id, r.channel_id) for r in response.rows}
        assert pairs == {("sat-a", "alpha-a"), ("sat-a", "bravo-a"), ("sat-a", "charlie-ab"),
                         ("sat-b", "alpha-b"), ("sat-b", "bravo-b"), ("sat-b", "charlie-ab")}
        cfg = runtime_of(app).config
        row = next(r for r in response.rows if r.channel_id == "bravo-a")
        expected = path_budget(cfg, schemas.LinkBudgetRequest.model_validate(body).emitter, 300.0e6,
                               lla_to_ecef(20.0, 135.0, 0.0), "sat-a", "t1", "bravo-a", sec(response.t_ns))
        assert expected is not None
        assert row.cn0_total_dbhz == pytest.approx(float(expected.cn0_total_dbhz))
        assert row.snr_db == pytest.approx(row.cn0_total_dbhz - 40.0)
        assert row.f_down_hz == 260.0e6 and row.monitor_id == "sat-a:t1"
        rise = 10 * math.log10(1 + 10 ** (row.snr_db / 10))  # 10 kHz is wider than a bin
        assert row.detectable == (rise >= cfg.processing.detection.threshold_db)

        weak = dict(body, emitter={"eirp_dbw": -40.0, "bandwidth_hz": 10e3})
        assert not any(r["detectable"] for r in (await client.post(f"{API}/analysis/link-budget", json=weak)).json()["rows"])
        outside = dict(body, f_e_hz=310.0e6)  # no transponder holds it
        assert (await client.post(f"{API}/analysis/link-budget", json=outside)).json()["rows"] == []
        reply = await client.post(f"{API}/analysis/link-budget", json=dict(body, lat_deg=95.0))
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "lat_deg"
        reply = await client.post(f"{API}/analysis/link-budget", json=dict(body, emitter={"eirp_dbw": 1.0}))
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "emitter.bandwidth_hz"


def stand_in_accuracy(calls: list[Any]) -> Callable[..., dict[str, Any]]:
    """A stand-in for ``core.accuracy.accuracy_map``: values that depend only on the cell
    (so banding cannot change them), null south of the equator, one NaN cell."""

    def accuracy_map(config: Any, pairs: Any, emitter: Any, f_e_hz: float, t_ns: int, region: Any, step_deg: float,
                     kinds: Any = ("tdoa", "fdoa"), reference_carrier_id: Any = None,
                     integration_s: Any = None) -> dict[str, Any]:
        calls.append((region.lat_min, region.lat_max, tuple(kinds), reference_carrier_id))
        lat, lon = region.grid(step_deg)
        semi = [[None if la < 0 else (float("nan") if (la, lo) == (10.0, 120.0) else 1000.0 * la + lo)
                 for lo in lon] for la in lat]
        cep = np.array([[500.0 + la for _ in lon] for la in lat])
        dominant = [[None if la < 0 else "noise" for _ in lon] for la in lat]
        return {"lat_deg": lat, "lon_deg": lon, "semi_major_95_m": semi, "cep50_m": cep, "dominant": dominant}

    return accuracy_map


async def test_accuracy_map_is_banded_cached_and_bounded(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(rest, "_load_accuracy_map", lambda: stand_in_accuracy(calls))
    monkeypatch.setattr(rest, "ACCURACY_BAND_CELLS", 40)
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        body = {"pairs": [["alpha-a", "bravo-b"]], "emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 10e3},
                "f_e_hz": 300.0e6, "step_deg": 5.0, "t": T_FIXED, "reference_carrier_id": "ref-1"}
        reply = await client.post(f"{API}/analysis/accuracy", json=body)
        assert reply.status_code == 200, reply.text
        no_nan(reply.text)
        grid = schemas.AccuracyGrid.model_validate(reply.json())
        assert grid.lat_deg == [-30.0 + 5 * k for k in range(17)]
        assert grid.lon_deg == [110.0 + 5 * k for k in range(15)]
        assert len(calls) > 3 and all(c[3] == "ref-1" for c in calls)  # several bands
        for i, la in enumerate(grid.lat_deg):
            for j, lo in enumerate(grid.lon_deg):
                if la < 0 or (la, lo) == (10.0, 120.0):
                    assert grid.semi_major_95_m[i][j] is None
                else:
                    assert grid.semi_major_95_m[i][j] == pytest.approx(1000.0 * la + lo)
                assert grid.cep50_m[i][j] == pytest.approx(500.0 + la)
                assert grid.dominant[i][j] == (None if la < 0 else "noise")

        before = len(calls)
        again = await client.post(f"{API}/analysis/accuracy", json=body)
        assert again.content == reply.content and len(calls) == before  # served from the cache

        too_fine = dict(body, step_deg=0.25, region={"lat_min": -60, "lat_max": 60, "lon_min": -180, "lon_max": 180})
        reply = await client.post(f"{API}/analysis/accuracy", json=too_fine)
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "step_deg"
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, pairs=[["alpha-a", "nowhere"]]))
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "pairs.0.1"
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, pairs=[["alpha-a", "alpha-a"]]))
        assert reply.json()["errors"][0]["loc"] == "pairs.0"
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, reference_carrier_id="plain"))
        assert reply.status_code == 422 and "not a reference carrier" in reply.json()["errors"][0]["msg"]
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, step_deg=0.01))
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "step_deg"
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, pairs=[]))
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "pairs"


async def test_accuracy_map_uncertainty_override_is_used_for_that_map_only(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``AccuracyRequest.uncertainty`` replaces ``processing.uncertainty`` for one map
    (what-if planning) without touching the configuration in force."""
    seen: list[tuple[float, float]] = []

    def accuracy_map(config: Any, pairs: Any, emitter: Any, f_e_hz: float, t_ns: int, region: Any, step_deg: float,
                     **_: Any) -> dict[str, Any]:
        unc = config.resolved_uncertainty()
        seen.append((unc.tdoa_bias_s, float(unc.sat_pos_ric_m["sat-a"][1])))
        lat, lon = region.grid(step_deg)
        value = unc.tdoa_bias_s * 1e9 + float(unc.sat_pos_ric_m["sat-a"][1])
        return {"lat_deg": lat, "lon_deg": lon, "semi_major_95_m": [[value for _ in lon] for _ in lat],
                "cep50_m": [[value / 2 for _ in lon] for _ in lat], "dominant": [["noise" for _ in lon] for _ in lat]}

    monkeypatch.setattr(rest, "_load_accuracy_map", lambda: accuracy_map)
    async with serving(settings_for(files), files["presets"]) as (app, client):
        body = {"pairs": [["alpha-a", "bravo-b"]], "emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 10e3},
                "f_e_hz": 300.0e6, "step_deg": 10.0, "t": T_FIXED}
        configured = (await client.post(f"{API}/analysis/accuracy", json=body)).json()
        assert configured["semi_major_95_m"][0][0] == pytest.approx(50.0 + 1000.0)  # defaults: 50 ns, 1000 m in-track
        what_if = dict(body, uncertainty={"ephemeris": {"operator": {"pos_m": [10, 20, 30], "vel_mps": [0, 0, 0]}},
                                          "tdoa_bias_ns": 5.0})
        reply = await client.post(f"{API}/analysis/accuracy", json=what_if)
        assert reply.status_code == 200, reply.text
        assert reply.json()["semi_major_95_m"][0][0] == pytest.approx(5.0 + 20.0)
        assert seen[-1] == (pytest.approx(5e-9), 20.0)
        runtime = runtime_of(app)
        assert runtime.config.processing.uncertainty.tdoa_bias_ns == 50.0  # the configuration is untouched
        assert (await client.post(f"{API}/analysis/accuracy", json=body)).json() == configured  # cache keyed apart
        bad = dict(body, uncertainty={"tdoa_bias_ns": -1.0})
        reply = await client.post(f"{API}/analysis/accuracy", json=bad)
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "uncertainty.tdoa_bias_ns"


def test_accuracy_override_against_the_real_engine() -> None:
    """With the real ``core.accuracy`` on the example configuration: removing the
    ephemeris and oscillator uncertainty can only shrink every predicted ellipse, and it
    does shrink an uncalibrated two-pair map dominated by ephemeris error."""
    from petal_locate.config import load_config
    from petal_locate.config.models import UncertaintyConfig
    from petal_locate.core.accuracy import accuracy_map

    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "pacific-uhf.yaml")
    request = schemas.AccuracyRequest(
        pairs=[("naha-pac", "naha-172e"), ("naha-io", "naha-172e")],
        emitter={"eirp_dbw": 25.0, "bandwidth_hz": 8e3},
        f_e_hz=301.005e6,
        step_deg=5.0,
        region={"lat_min": 10.0, "lat_max": 30.0, "lon_min": 120.0, "lon_max": 140.0},
    )
    exact = UncertaintyConfig.model_validate({
        "ephemeris": {"operator": {"pos_m": [0, 0, 0], "vel_mps": [0, 0, 0]}, "tle": {"pos_m": [0, 0, 0], "vel_mps": [0, 0, 0]}},
        "sat_osc_frac": 0.0, "site_osc_frac": 0.0, "tdoa_bias_ns": 0.0})
    t_ns = 1_794_571_200_000_000_000  # 2026-11-13T12:00:00Z
    assert request.region is not None
    base = rest.compute_accuracy(accuracy_map, cfg, request, t_ns)
    ideal = rest.compute_accuracy(accuracy_map, cfg, request.model_copy(update={"uncertainty": exact}), t_ns)
    assert rest.accuracy_config(cfg, request) is cfg
    assert cfg.processing.uncertainty.tdoa_bias_ns == 50.0
    pairs = [(b, i) for rb, ri in zip(base.semi_major_95_m, ideal.semi_major_95_m) for b, i in zip(rb, ri)
             if b is not None and i is not None]
    assert len(pairs) >= 10
    assert all(i <= b * (1 + 1e-9) for b, i in pairs)
    assert sum(i < 0.5 * b for b, i in pairs) >= len(pairs) // 2
    assert {d for row in ideal.dominant for d in row if d is not None} == {"noise"}


async def test_accuracy_map_failures(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"pairs": [["alpha-a", "bravo-b"]], "emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 10e3},
            "f_e_hz": 300.0e6, "step_deg": 5.0, "t": T_FIXED}
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        # This build without core.accuracy: 501.
        monkeypatch.setitem(sys.modules, "petal_locate.core.accuracy", None)
        reply = await client.post(f"{API}/analysis/accuracy", json=body)
        assert reply.status_code == 501 and "not available" in reply.json()["detail"]

        def refuses(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("pair alpha-a/bravo-b shares no transponder")

        monkeypatch.setattr(rest, "_load_accuracy_map", lambda: refuses)
        reply = await client.post(f"{API}/analysis/accuracy", json=body)
        assert reply.status_code == 422 and "shares no transponder" in reply.json()["detail"]

        def wrong_shape(config: Any, pairs: Any, emitter: Any, f: float, t: int, region: Any, step: float,
                        **kw: Any) -> Any:
            return {"lat_deg": [0.0], "lon_deg": [0.0], "semi_major_95_m": [[1.0]], "cep50_m": [[1.0]],
                    "dominant": [["noise"]]}

        monkeypatch.setattr(rest, "_load_accuracy_map", lambda: wrong_shape)
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, step_deg=4.0))
        assert reply.status_code == 500 and "unusable" in reply.json()["detail"]

        calls: list[Any] = []
        monkeypatch.setattr(rest, "_load_accuracy_map", lambda: stand_in_accuracy(calls))
        monkeypatch.setattr(rest, "ACCURACY_TIMEOUT_S", -1.0)
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, step_deg=3.0))
        assert reply.status_code == 504 and "longer than" in reply.json()["detail"]
        assert calls == []  # stopped before the first band


def test_accuracy_bands_cover_the_grid_exactly() -> None:
    from petal_locate.config import Region

    for region, step in [
        (Region(lat_min=-60, lat_max=60, lon_min=60, lon_max=-120), 2.0),
        (Region(lat_min=-1.0, lat_max=1.5, lon_min=0, lon_max=10), 1.0),
        (Region(lat_min=0, lat_max=0.5, lon_min=0, lon_max=10), 1.0),  # a single row
        (Region(), 7.0),
    ]:
        lat, lon, bands = rest.accuracy_bands(region, step, band_cells=50)
        rows = []
        for band in bands:
            band_lat, band_lon = band.grid(step)
            assert np.allclose(band_lon, lon)
            rows.extend(band_lat.tolist())
        assert np.allclose(rows, lat)


# --------------------------------------------------------------------------- simulation


async def test_simulation_routes(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (app, client):
        envelope = (await client.get(f"{API}/sim/scenario")).json()
        assert envelope["version"] == 1 and envelope["scenario"]["name"] == "base"

        presets = (await client.get(f"{API}/sim/presets")).json()
        assert [p["name"] for p in presets] == ["broken", "north"]
        assert presets[0]["description"].startswith("cannot be loaded")
        reply = await client.post(f"{API}/sim/presets/north/load")
        assert reply.status_code == 200 and reply.json()["scenario"]["name"] == "north"
        assert reply.json()["version"] == 2
        assert (await client.post(f"{API}/sim/presets/nothing/load")).status_code == 404
        reply = await client.post(f"{API}/sim/presets/broken/load")
        assert reply.status_code == 422 and reply.json()["errors"]

        scenario = make_scenario("edited", lat=25.0).model_dump(mode="json")
        reply = await client.put(f"{API}/sim/scenario", json=scenario)
        assert reply.status_code == 200 and reply.json()["version"] == 3
        reply = await client.put(f"{API}/sim/scenario", json={"version": 3, "scenario": scenario})
        assert reply.status_code == 200 and reply.json()["version"] == 4
        reply = await client.put(f"{API}/sim/scenario", content=dump_scenario(make_scenario("yaml")),
                                 headers={"Content-Type": "application/yaml"})
        assert reply.status_code == 200 and reply.json()["scenario"]["name"] == "yaml"

        bad = dict(scenario, emitters=[dict(scenario["emitters"][0], pointing={"satellite_id": "sat-z"})])
        reply = await client.put(f"{API}/sim/scenario", json=bad)
        assert reply.status_code == 422 and "sat-z" in reply.json()["errors"][0]["msg"]
        reply = await client.put(f"{API}/sim/scenario", json=dict(scenario, emitters=[{"id": "x"}]))
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"].startswith("emitters.0")

        reply = await client.post(f"{API}/sim/reset")
        assert reply.status_code == 200 and reply.json()["scenario"]["name"] == "base"

        event_id = make_event(runtime_of(app))
        truth = schemas.SimTruth.model_validate((await client.get(f"{API}/sim/truth")).json())
        assert truth.simulated and truth.version == reply.json()["version"]
        emitters = {e.id: e for e in truth.emitters}
        assert set(emitters) == {"jam-1", "late", "ref-1"}  # ref-1: created for the configured carrier
        assert emitters["jam-1"].active and not emitters["late"].active  # "late" starts in 2099
        assert emitters["ref-1"].kind == "carrier" and emitters["ref-1"].eirp_dbw == 13.0
        assert emitters["jam-1"].bandwidth_hz == 10e3 and emitters["jam-1"].waveform == "noise"
        assert [e.event_id for e in truth.events] == [event_id]
        assert truth.events[0].emitter_id == "jam-1" and truth.events[0].miss_m is None


async def test_simulation_routes_404_without_a_scenario(files: dict[str, Path]) -> None:
    async with serving(settings_for(files, scenario=False), files["presets"]) as (_app, client):
        assert (await client.get(f"{API}/meta")).json()["simulation"] is False
        for method, path in [("GET", "/sim/scenario"), ("PUT", "/sim/scenario"), ("GET", "/sim/presets"),
                             ("POST", "/sim/presets/north/load"), ("POST", "/sim/reset"), ("GET", "/sim/truth")]:
            reply = await client.request(method, f"{API}{path}", json=make_scenario().model_dump(mode="json"))
            assert reply.status_code == 404, (method, path)
            assert "not simulating" in reply.json()["detail"]
        reply = await client.get(SIM_BACKPLANE_PATH, headers={"Authorization": "Bearer token-alpha"})
        assert reply.status_code == 404


async def test_backplane_checks_the_node_token(files: dict[str, Path]) -> None:
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        reply = await client.get(SIM_BACKPLANE_PATH)
        assert reply.status_code == 401 and reply.headers["www-authenticate"] == "Bearer"
        for header in ("Bearer token-wrong", "Basic token-alpha", "Bearer", "token-alpha"):
            assert (await client.get(SIM_BACKPLANE_PATH, headers={"Authorization": header})).status_code == 401
        reply = await client.get(SIM_BACKPLANE_PATH, headers={"Authorization": "Bearer token-bravo"})
        assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
        doc = reply.json()
        assert doc["version"] == 1 and isinstance(doc["effective_ns"], str) and int(doc["effective_ns"]) > 0
        assert SystemConfig.model_validate(doc["config"]).name == "api-test"
        assert Scenario.model_validate(doc["scenario"]).name == "base"
        # The API token is not a node token.
        async with serving(settings_for(files, api_token="console-secret"), files["presets"]) as (_a, other):
            reply = await other.get(SIM_BACKPLANE_PATH, headers={"Authorization": "Bearer console-secret"})
            assert reply.status_code == 401


# --------------------------------------------------------------------------- authentication


async def test_api_token(files: dict[str, Path]) -> None:
    async with serving(settings_for(files, api_token="s3cret-token"), files["presets"]) as (_app, client):
        meta = await client.get(f"{API}/meta")  # readable without the token
        assert meta.status_code == 200 and meta.json()["api_token_required"] is True
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic s3cret-token"}):
            reply = await client.get(f"{API}/status", headers=headers)
            assert reply.status_code == 401 and reply.headers["www-authenticate"] == "Bearer"
            assert "s3cret" not in reply.text
        assert (await client.put(f"{API}/config", content="x", headers={"If-Match": "1"})).status_code == 401
        assert (await client.get(f"{API}/sim/truth")).status_code == 401
        for scheme in ("Bearer", "bearer"):
            reply = await client.get(f"{API}/status", headers={"Authorization": f"{scheme} s3cret-token"})
            assert reply.status_code == 200
        assert (await client.get("/healthz")).status_code == 200


# --------------------------------------------------------------------------- static console


async def test_static_console(files: dict[str, Path], tmp_path: Path) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<!doctype html><html><head><script src="./assets/app-AbCd1234.js"></script>'
                                       "</head><body>console</body></html>")
    (static / "assets" / "app-AbCd1234.js").write_text("console.log(1)")
    (static / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("do not serve")
    async with serving(settings_for(files), files["presets"], static_dir=static) as (_app, client):
        reply = await client.get("/")
        assert reply.status_code == 200 and "console" in reply.text and reply.headers["cache-control"] == "no-cache"
        assert "<base" not in reply.text
        reply = await client.get("/assets/app-AbCd1234.js")
        assert reply.status_code == 200 and reply.text == "console.log(1)"
        assert reply.headers["cache-control"] == "public, max-age=31536000, immutable"
        reply = await client.get("/favicon.svg")
        assert reply.status_code == 200 and reply.headers["cache-control"] == "public, max-age=3600"
        reply = await client.get("/watch/EV-000001")  # a client-side route
        assert reply.status_code == 200 and reply.headers["cache-control"] == "no-cache"
        assert '<head><base href="/">' in reply.text
        assert (await client.get("/events")).text.count("<base") == 0  # one level: relative paths still resolve
        assert (await client.get("/assets/missing-AbCd1234.js")).status_code == 404
        assert (await client.get("/%2e%2e/secret.txt")).status_code == 404
        assert (await client.get("/assets/../../secret.txt")).status_code == 404
        reply = await client.get("/api/v1/nothing")
        assert reply.status_code == 404 and reply.json() == {"detail": "Not Found"}
        assert (await client.get("/ws/other")).status_code == 404
        assert (await client.head("/")).status_code == 200


async def test_placeholder_without_a_console_build(files: dict[str, Path], tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    async with serving(settings_for(files), files["presets"], static_dir=empty) as (_app, client):
        for path in ("/", "/index.html", "/watch"):
            reply = await client.get(path)
            assert reply.status_code == 200 and reply.headers["cache-control"] == "no-cache"
            assert "has not been built" in reply.text and "npm run build" in reply.text


# --------------------------------------------------------------------------- the shipped example


async def test_example_configuration_end_to_end(tmp_path: Path) -> None:
    settings = Settings(config_path=ROOT / "config" / "pacific-uhf.yaml",
                        scenario_path=ROOT / "config" / "scenarios" / "pacific-demo.yaml", data_dir=tmp_path / "data")
    async with serving(settings, ROOT / "config" / "scenarios") as (_app, client):
        status = (await client.get(f"{API}/status")).json()
        assert len(status["nodes"]) == 7
        rows = (await client.get(f"{API}/geometry/look-angles")).json()
        assert len(rows) == 19 * 3 and any(r["electrically_small"] for r in rows)
        presets = {p["name"] for p in (await client.get(f"{API}/sim/presets")).json()}
        assert "pacific-demo" in presets and "quiet" in presets
        truth = (await client.get(f"{API}/sim/truth")).json()
        assert {"jammer-philsea", "ref-guam", "ref-sapporo"} <= {e["id"] for e in truth["emitters"]}
        reply = await client.post(f"{API}/analysis/link-budget", json={
            "emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 10e3}, "f_e_hz": 301.005e6, "lat_deg": 20.0,
            "lon_deg": 132.0})
        assert reply.status_code == 200 and len(reply.json()["rows"]) >= 10
        no_nan(reply.text)


# --------------------------------------------------------------------------- event loop


async def test_heavy_routes_keep_the_event_loop_responsive(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Geometry, validation, link budgets, accuracy maps and exports run off the loop,
    and the start-up heap is frozen so that full garbage collections stay short. The
    budget is 50 ms; the bound here is looser because the test machine is shared."""
    calls: list[Any] = []
    monkeypatch.setattr(rest, "_load_accuracy_map", lambda: stand_in_accuracy(calls))
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        assert gc.get_freeze_count() > 0
        exported = (await client.get(f"{API}/config", params={"format": "yaml"})).text
        lags: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            while not stop.is_set():
                start = time.perf_counter()
                await asyncio.sleep(0.005)
                lags.append(time.perf_counter() - start - 0.005)

        task = asyncio.create_task(ticker())
        replies = await asyncio.gather(
            client.get(f"{API}/geometry/look-angles"),
            client.get(f"{API}/geometry/footprints"),
            client.post(f"{API}/config/validate", content=exported, headers={"Content-Type": "application/yaml"}),
            client.post(f"{API}/analysis/link-budget", json={"emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 1e4},
                                                              "f_e_hz": 300.0e6, "lat_deg": 20.0, "lon_deg": 135.0}),
            client.post(f"{API}/analysis/accuracy", json={
                "pairs": [["alpha-a", "bravo-b"]], "emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 1e4},
                "f_e_hz": 300.0e6, "step_deg": 1.0}),
            client.get(f"{API}/config/schema"),
        )
        stop.set()
        await task
        assert [r.status_code for r in replies] == [200] * 6
        assert lags and max(lags) < 0.1, max(lags)
    assert gc.get_freeze_count() == 0  # handed back at shutdown


# --------------------------------------------------------------------------- review hardening


async def test_text_that_is_not_valid_unicode_never_reaches_the_configuration(files: dict[str, Path]) -> None:
    """A configuration whose description held a lone surrogate passed validation, PUT
    answered 500 after applying it, and GET /config answered 500 from then on (M5)."""
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        current = (await client.get(f"{API}/config")).json()["config"]
        bad = dict(current, description="x\ud800")
        body = json.dumps(bad).encode("ascii")  # the escape travels as plain ASCII
        reply = await client.post(f"{API}/config/validate", content=body, headers={"Content-Type": "application/json"})
        assert reply.status_code == 200 and reply.json()["ok"] is False
        assert reply.json()["errors"][0]["loc"] == "description"
        for envelope in (False, True):
            doc = json.dumps({"version": 1, "config": bad} if envelope else bad).encode("ascii")
            reply = await client.put(f"{API}/config", content=doc,
                                     headers={"Content-Type": "application/json", "If-Match": "*"})
            assert reply.status_code == 422, reply.text
            assert "not valid Unicode" in reply.text
        reply = await client.put(f"{API}/config", content='description: "\\ud800"\nname: y\n',
                                 headers={"Content-Type": "application/yaml", "If-Match": "*"})
        assert reply.status_code == 422
        after = await client.get(f"{API}/config")
        assert after.status_code == 200 and after.json()["version"] == 1  # nothing was applied
        scenario = (await client.get(f"{API}/sim/scenario")).json()["scenario"]
        reply = await client.put(f"{API}/sim/scenario", content=json.dumps(dict(scenario, name="s\udce9")).encode("ascii"),
                                 headers={"Content-Type": "application/json"})
        assert reply.status_code == 422 and (await client.get(f"{API}/sim/scenario")).status_code == 200


async def test_malformed_input_is_a_4xx_not_a_500(files: dict[str, Path], caplog: pytest.LogCaptureFixture) -> None:
    """Deep nesting, a NUL in a static path and times outside 1970..2262 gave HTTP 500
    with a traceback each (SRV-5)."""
    deep_json = ('{"a": ' + "[" * 100000 + "]" * 100000 + "}").encode()
    deep_yaml = ("a: " + "[" * 20000 + "]" * 20000).encode()
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        reply = await client.post(f"{API}/config/validate", content=deep_json, headers={"Content-Type": "application/json"})
        assert reply.status_code == 200 and reply.json() == {
            "ok": False, "errors": [{"loc": "(root)", "msg": "the document is nested too deeply"}], "warnings": []}
        reply = await client.post(f"{API}/config/validate", content=deep_yaml, headers={"Content-Type": "application/yaml"})
        assert reply.status_code == 200 and reply.json()["ok"] is False
        reply = await client.put(f"{API}/config", content=deep_json,
                                 headers={"Content-Type": "application/json", "If-Match": "*"})
        assert reply.status_code == 422
        reply = await client.put(f"{API}/sim/scenario", content=deep_json, headers={"Content-Type": "application/json"})
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "(root)"
        for path in ("/index.html%00", "/a%00.js", "/assets/x%00.js"):
            assert (await client.get(path)).status_code == 404, path
        budget = {"emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 1e4}, "f_e_hz": 300.0e6, "lat_deg": 20.0,
                  "lon_deg": 135.0}
        for t in ("2300-01-01T00:00:00Z", "1900-01-01T00:00:00Z"):
            reply = await client.post(f"{API}/analysis/link-budget", json=dict(budget, t=t))
            assert reply.status_code == 422 and reply.json()["errors"] == [
                {"loc": "t", "msg": "outside the times the server represents (1970-01-01 to 2262-04-11 UTC)"}]
            reply = await client.post(f"{API}/analysis/accuracy", json={
                "pairs": [["alpha-a", "bravo-b"]], "emitter": budget["emitter"], "f_e_hz": 300.0e6, "t": t})
            assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "t"
            assert (await client.get(f"{API}/geometry/subpoints", params={"t": t})).status_code == 422
    assert not [r for r in caplog.records if r.exc_info], "a traceback was logged"


async def test_browser_facing_requests_check_host_origin_and_request_kind(files: dict[str, Path]) -> None:
    """Without an API token another local page (or a DNS-rebound name) could read the
    API and change the scenario with a CORS simple request (SRV-3)."""
    async with serving(settings_for(files), files["presets"], headers={"X-Petal-Client": ""}) as (_app, client):
        del client.headers["X-Petal-Client"]
        # DNS rebinding: the request names a host the server does not answer to
        reply = await client.get(f"{API}/config", headers={"Host": "rebind.evil.example"})
        assert reply.status_code == 400 and "--allowed-host" in reply.json()["detail"]
        assert (await client.get("/", headers={"Host": "rebind.evil.example:8080"})).status_code == 400
        for host in ("localhost", "127.0.0.1:8080", "[::1]:8080", "LOCALHOST."):
            assert (await client.get(f"{API}/meta", headers={"Host": host})).status_code == 200, host
        # node-facing paths take any name (nodes dial whatever the network calls the server)
        assert (await client.get("/healthz", headers={"Host": "server.site.example"})).status_code == 200
        # a cross-site simple request: no JSON, no token, no custom header -> refused
        before = (await client.get(f"{API}/sim/scenario")).json()["version"]
        for headers in ({"Content-Type": "text/plain", "Origin": "http://evil.example"}, {},
                        {"Content-Type": "application/x-www-form-urlencoded"}):
            reply = await client.post(f"{API}/sim/presets/north/load", headers=headers)
            assert reply.status_code == 403 and "X-Petal-Client" in reply.json()["detail"], headers
        assert (await client.get(f"{API}/sim/scenario")).json()["version"] == before
        reply = await client.post(f"{API}/config/validate", content="name: x\n", headers={"Content-Type": "text/plain"})
        assert reply.status_code == 403
        # the console's header, a JSON body or a YAML type make it a request no page sends unasked
        assert (await client.post(f"{API}/sim/presets/north/load", headers={"X-Petal-Client": "console"})).status_code == 200
        reply = await client.post(f"{API}/config/validate", content="name: x\n", headers={"Content-Type": "application/yaml"})
        assert reply.status_code == 200
        assert (await client.post(f"{API}/config/validate", json={"name": "x"})).status_code == 200
    # configured names, and '*'
    async with serving(settings_for(files, allowed_hosts=("Console.Example",)), files["presets"]) as (_app, client):
        assert (await client.get(f"{API}/meta", headers={"Host": "console.example:443"})).status_code == 200
        assert (await client.get(f"{API}/meta", headers={"Host": "other.example"})).status_code == 400
    async with serving(settings_for(files, allowed_hosts=("*",)), files["presets"]) as (_app, client):
        assert (await client.get(f"{API}/meta", headers={"Host": "other.example"})).status_code == 200
    # with an API token the Authorization header itself marks the request as not simple
    async with serving(settings_for(files, api_token="tok"), files["presets"],
                       headers={"Authorization": "Bearer tok", "X-Petal-Client": ""}) as (_app, client):
        del client.headers["X-Petal-Client"]
        assert (await client.post(f"{API}/sim/reset")).status_code == 200


async def test_every_api_body_is_capped_and_accuracy_requests_are_bounded(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 100 MB body was read in full before a 422, and 3500 duplicate pairs drove the
    server out of memory (SRV-4, M6)."""
    calls: list[Any] = []
    monkeypatch.setattr(rest, "_load_accuracy_map", lambda: stand_in_accuracy(calls))
    monkeypatch.setattr(rest, "MAX_BODY_BYTES", 4096)
    async with serving(settings_for(files), files["presets"]) as (_app, client):
        big = b'{"pad": "' + b"x" * 10_000 + b'"}'
        for path in ("/analysis/link-budget", "/analysis/accuracy", "/config/validate"):
            reply = await client.post(f"{API}{path}", content=big, headers={"Content-Type": "application/json"})
            assert reply.status_code == 413, path

        async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: counted as it streams
            for _ in range(10):
                yield b" " * 1000

        reply = await client.post(f"{API}/analysis/link-budget", content=chunks(),
                                  headers={"Content-Type": "application/json"})
        assert reply.status_code == 413
        monkeypatch.setattr(rest, "MAX_BODY_BYTES", 4 * 1024 * 1024)
        body = {"pairs": [["alpha-a", "bravo-b"]], "emitter": {"eirp_dbw": 20.0, "bandwidth_hz": 10e3},
                "f_e_hz": 300.0e6, "step_deg": 5.0, "t": T_FIXED}
        reply = await client.post(f"{API}/analysis/accuracy", json=dict(body, pairs=[["alpha-a", "bravo-b"]] * 3500))
        assert reply.status_code == 422 and reply.json()["errors"][0]["loc"] == "pairs"
        assert not calls  # refused before any work
        reply = await client.post(f"{API}/analysis/accuracy",
                                  json=dict(body, pairs=[["alpha-a", "bravo-b"], ["bravo-b", "alpha-a"]]))
        assert reply.status_code == 422
        assert reply.json()["errors"] == [{"loc": "pairs.1", "msg": "the same pair as pairs.0 (each pair counts once)"}]
        monkeypatch.setattr(rest, "ACCURACY_BAND_BYTES", 1000)  # two grid rows no longer fit
        reply = await client.post(f"{API}/analysis/accuracy", json=body)
        assert reply.status_code == 422 and "MiB per band" in reply.json()["errors"][0]["msg"]
        assert not calls
    monkeypatch.undo()
    # bands shrink as the pairs grow, never under two rows
    assert rest.accuracy_band_cells(1, 15) == rest.ACCURACY_BAND_CELLS
    many = rest.accuracy_band_cells(128, 15)
    assert 30 <= many <= rest.ACCURACY_BAND_CELLS
    assert rest.accuracy_band_bytes(128, many) <= rest.ACCURACY_BAND_BYTES or many == 30


async def test_sites_without_a_node_token_are_warned_about(files: dict[str, Path]) -> None:
    """A site added through PUT /config could never connect, and nothing said why
    (ADAPT-3). charlie has no token in these settings."""
    tokens = files["data"].parent / "tokens.yaml"
    tokens.write_text("alpha: token-alpha\nbravo: token-bravo\n")
    settings = settings_for(files, node_tokens={"alpha": "token-alpha", "bravo": "token-bravo"},
                            node_tokens_path=tokens)
    async with serving(settings, files["presets"]) as (_app, client):
        envelope = (await client.get(f"{API}/config")).json()
        notes = [w for w in envelope["warnings"] if "node token" in w["msg"]]
        assert [w["loc"] for w in notes] == ["sites.2"] and "'charlie'" in notes[0]["msg"]
        assert str(tokens) in notes[0]["msg"] and "no restart" in notes[0]["msg"]
        current = envelope["config"]
        current["sites"].append(dict(current["sites"][1], id="delta", name="Delta", channels=[
            dict(current["sites"][1]["channels"][0], id="delta-a")]))
        result = (await client.post(f"{API}/config/validate", json=current)).json()
        assert result["ok"] and {w["loc"] for w in result["warnings"] if "node token" in w["msg"]} == {"sites.2", "sites.3"}
        reply = await client.put(f"{API}/config", json=current, headers={"If-Match": "*"})
        assert reply.status_code == 200
        assert {w["loc"] for w in reply.json()["warnings"] if "node token" in w["msg"]} == {"sites.2", "sites.3"}
        # the operator adds the token to the file: no restart, the warning goes away
        tokens.write_text("alpha: token-alpha\nbravo: token-bravo\ndelta: token-delta\n")
        import os

        os.utime(tokens, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        envelope = (await client.get(f"{API}/config")).json()
        assert [w["loc"] for w in envelope["warnings"] if "node token" in w["msg"]] == ["sites.2"]
        assert settings.check_node_token("delta", "token-delta")


async def test_link_budget_leaves_out_a_satellite_it_cannot_place(files: dict[str, Path]) -> None:
    """An ephemeris table that does not cover t made the whole link budget a 422 (and
    'petal-locate check' crash): the other satellites' rows are given (ADAPT-6)."""
    from petal_locate.core.orbits import orbit_state
    from petal_locate.core.timebase import iso_from_ns, ns_from_iso

    cfg = make_config()
    t_end = ns_from_iso(T_FIXED)
    samples = []
    for k in range(-30, 1):
        t = t_end + k * 60 * 10**9
        r, v = orbit_state(cfg.orbit("sat-b"), sec(t))
        samples.append({"t": iso_from_ns(t), "r_m": [float(x) for x in r], "v_mps": [float(x) for x in v]})
    data = cfg.export_dict()
    data["satellites"][1]["orbit"] = {"type": "table", "samples": samples}
    table_cfg = SystemConfig.model_validate(data)
    emitter = table_cfg.carriers[0].emitter
    assert emitter is not None
    before = rest.compute_link_budget(table_cfg, emitter, 300.0e6, 20.0, 135.0, 0.0, t_end - 60 * 10**9)
    after = rest.compute_link_budget(table_cfg, emitter, 300.0e6, 20.0, 135.0, 0.0, t_end + 3600 * 10**9)
    assert {r.sat_id for r in before} == {"sat-a", "sat-b"}
    assert {r.sat_id for r in after} == {"sat-a"}


async def test_a_simulated_time_offset_moves_the_whole_server_clock(files: dict[str, Path]) -> None:
    """`petal-locate demo --start` (K8): the server's time base carries the offset, and
    so does the hub, while the application runs; the default stays the host clock."""
    from petal_locate.server import hub as hub_module

    offset = -200 * 86400 * 10**9
    settings = settings_for(files, sim_time_offset_ns=offset)
    async with serving(settings, files["presets"]) as (app, client):
        meta = (await client.get(f"{API}/meta")).json()
        assert abs(int(meta["server_t_ns"]) - (time.time_ns() + offset)) < 5 * 10**9
        status = (await client.get(f"{API}/status")).json()
        assert abs(int(status["server_t_ns"]) - (time.time_ns() + offset)) < 5 * 10**9
        # the hub takes the server clock (or, with a hub that has no clock parameter, the
        # application shifts the hub module's time while it runs)
        hub_clock = getattr(app.state.hub, "_clock", None)
        hub_now = hub_clock() if callable(hub_clock) else hub_module.time.time_ns()
        assert abs(hub_now - (time.time_ns() + offset)) < 5 * 10**9
        assert abs(app.state.runtime.store._clock() - (time.time_ns() + offset)) < 5 * 10**9
    assert hub_module.time is time  # put back when the application stops
    async with serving(settings_for(files), files["presets"]) as (app, client):
        assert app.state.clock is time.time_ns and hub_module.time is time


async def test_a_console_address_carrying_the_token_opens_an_authenticated_console(
    files: dict[str, Path], tmp_path: Path
) -> None:
    """`petal-locate demo` prints the console address with its random API token; the
    page served for it hands the token to the console (and drops it from the address)."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><html><head><title>c</title></head><body></body></html>")
    async with serving(settings_for(files, api_token="tok-<&>"), files["presets"], static_dir=static,
                       headers={"Authorization": "Bearer tok-<&>"}) as (_app, client):
        reply = await client.get("/", params={"token": "tok-<&>"})
        assert reply.status_code == 200
        assert 'sessionStorage.setItem("petal.apiToken","tok-\\u003c\\u0026\\u003e")' in reply.text
        assert "history.replaceState" in reply.text and "<&>" not in reply.text
        assert reply.headers["cache-control"] == "no-store" and reply.headers["referrer-policy"] == "no-referrer"
        for params in ({"token": "wrong"}, {}):  # nothing else is ever written into the page
            reply = await client.get("/", params=params)
            assert reply.status_code == 200 and "sessionStorage" not in reply.text and "wrong" not in reply.text
    async with serving(settings_for(files), files["presets"], static_dir=static) as (_app, client):
        assert "sessionStorage" not in (await client.get("/", params={"token": "x"})).text  # no token configured
