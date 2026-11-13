"""Tests of the server runtime (``server/runtime.py``): configuration replacement with
If-Match and field errors, resuming a data directory, the simulated world and its
backplane, presets, the console bus, the worker pool, and the pipeline wired to a real
``Hub`` (no sockets: frames are handed to the detection pipeline as the hub would).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import pytest_asyncio

from petal_locate.config import SystemConfig, dump_config
from petal_locate.core.spectrum import welch_psd_db
from petal_locate.protocol.messages import PsdHeader
from petal_locate.server.api import schemas
from petal_locate.server.hub import Hub, PsdFrame
from petal_locate.server.pipeline.events import EventRecord, event_truth
from petal_locate.server.runtime import (
    SIM_BACKPLANE_PATH,
    ConfigInvalid,
    ConsoleBus,
    ConsoleClosed,
    EventNotFound,
    NotSimulating,
    PreconditionRequired,
    PresetNotFound,
    RuntimeHolder,
    ScenarioInvalid,
    ServerRuntime,
    VersionConflict,
    WorkPool,
    config_text,
    parse_if_match,
    resolve_truth_emitters,
)
from petal_locate.server.settings import Marking, Settings
from petal_locate.server.store import Store, store_threads
from petal_locate.sim.scenario import Scenario, dump_scenario

SHIFT = -40.0e6


def make_config(name: str = "runtime-test", *, with_carrier: bool = True, far: bool = True) -> SystemConfig:
    tp = {
        "id": "t1",
        "uplink_center_hz": 300.0e6,
        "bandwidth_hz": 25e3,
        "translation_hz": SHIFT,
        "g_over_t_dbk": -14.0,
        "sfd_dbw_m2": -136.0,
        "eirp_sat_dbw": 26.0,
    }
    dish = {"type": "parabolic", "diameter_m": 0.72}  # electrically small at 260 MHz: a warning
    sites = [
        {"id": "alpha", "name": "Alpha", "lat_deg": 35.0, "lon_deg": 139.0,
         "channels": [{"id": "alpha-a", "antenna": dish, "pointing": {"satellite_id": "sat-a"}}]},
    ]
    if far:
        sites.append(
            {"id": "far", "name": "Far", "lat_deg": 17.0, "lon_deg": 139.0,
             "channels": [{"id": "far-a", "antenna": dish, "pointing": {"satellite_id": "sat-a"}}]}
        )
    carriers = []
    if with_carrier:
        carriers.append(
            {"id": "ref-1", "name": "Reference", "satellite_id": "sat-a", "transponder_id": "t1",
             "uplink_center_hz": 299.9925e6, "bandwidth_hz": 5e3,
             "location": {"lat_deg": 13.4, "lon_deg": 144.8},
             "emitter": {"eirp_dbw": 13.0, "bandwidth_hz": 5e3}, "use_as_reference": True}
        )
    return SystemConfig.model_validate(
        {
            "name": name,
            "satellites": [{"id": "sat-a", "name": "Sat A", "orbit": {"type": "geo", "lon_deg": 140.0}, "transponders": [tp]}],
            "sites": sites,
            "carriers": carriers,
        }
    )


def make_scenario(name: str = "base", lat: float = 20.0, carrier: bool = True) -> Scenario:
    emitters: list[dict[str, Any]] = [
        {"id": "jam-1", "kind": "jammer", "lat_deg": lat, "lon_deg": 135.0, "freq_hz": 300.0e6, "eirp_dbw": 20.0,
         "waveform": {"type": "noise", "bandwidth_hz": 10e3}},
    ]
    if carrier:
        emitters.append({"id": "ref-1-tx", "kind": "carrier", "carrier_id": "ref-1"})
    return Scenario.model_validate({"name": name, "description": f"{name} test scenario", "emitters": emitters})


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
    (presets / "misfit.yaml").write_text(
        dump_scenario(Scenario.model_validate({"name": "misfit", "emitters": [
            {"id": "x", "lat_deg": 0.0, "lon_deg": 0.0, "freq_hz": 3e8, "eirp_dbw": 1.0,
             "waveform": {"type": "cw"}, "pointing": {"satellite_id": "no-such-sat"}}]}))
    )
    (presets / "notes.txt").write_text("not a preset")
    return {"config": config, "scenario": scenario, "presets": presets, "data": tmp_path / "data"}


def settings_for(files: dict[str, Path], *, scenario: bool = True) -> Settings:
    return Settings(
        config_path=files["config"],
        scenario_path=files["scenario"] if scenario else None,
        data_dir=files["data"],
        node_tokens={"alpha": "token-alpha", "far": "token-far"},
        marking=Marking(level="UNCLASSIFIED", line="UNCLASSIFIED // TEST"),
    )


class Running:
    """A runtime started against a real hub (no sockets), stopped by ``close``."""

    def __init__(self, runtime: ServerRuntime) -> None:
        self.runtime = runtime
        providers = runtime.hub_providers()
        self.hub = Hub(runtime.settings, providers.assignments_for, providers.config_version, providers.simulation_info)
        self.announced: list[tuple[int, int]] = []
        original = self.hub.announce_sim_update

        async def announce(version: int, effective_ns: int) -> list[str]:
            self.announced.append((version, effective_ns))
            return await original(version, effective_ns)

        self.hub.announce_sim_update = announce  # type: ignore[method-assign]
        self.online: set[str] = set()
        self.hub.is_online = lambda site_id: site_id in self.online  # type: ignore[method-assign]

    async def start(self) -> Running:
        self.hub.start()
        await self.runtime.start(self.hub)
        return self

    async def close(self) -> None:
        await self.runtime.stop()
        await self.hub.close()


@pytest_asyncio.fixture
async def running(files: dict[str, Path]) -> AsyncIterator[Running]:
    runtime = await ServerRuntime.from_settings(settings_for(files), presets_dir=files["presets"])
    r = await Running(runtime).start()
    try:
        yield r
    finally:
        await r.close()
    await asyncio.sleep(0)
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftovers == []
    assert store_threads() == [] and not [t for t in threading.enumerate() if t.name.startswith(("petal-work", "petal-spectrum"))]


def drain(subscription: Any) -> list[Any]:
    out = []
    while (item := subscription.get_nowait()) is not None:
        out.append(json.loads(item) if isinstance(item, str) else item)
    return out


# --------------------------------------------------------------------- start-up


async def test_fresh_data_directory_runs_the_file_configuration(running: Running, files: dict[str, Path]) -> None:
    runtime = running.runtime
    assert runtime.config_source == "file" and runtime.config_version == 1 and runtime.config.version == 1
    latest = await runtime.store.latest_config()
    assert latest is not None and latest.version == 1 and latest.source == "file"
    assert latest.text == config_text(make_config())
    assert runtime.simulating and runtime.scenario_version == 1
    assert runtime.simulation_info() == {"enabled": True, "scenario_url": SIM_BACKPLANE_PATH, "version": 1}
    providers = runtime.hub_providers()
    assert providers.config_version() == 1 and providers.simulation_info()["enabled"]
    channels = providers.assignments_for("alpha")
    assert channels is not None and [c.channel_id for c in channels] == ["alpha-a"]
    monitor = runtime.config.channel("alpha-a")[1].monitors[0]
    (assigned,) = channels[0].monitors
    assert (assigned.monitor_id, assigned.center_hz, assigned.span_hz, assigned.nfft, assigned.navg, assigned.frame_rate_hz) == (
        monitor.id, monitor.center_hz, monitor.span_hz, monitor.nfft, monitor.navg, monitor.frame_rate_hz)
    assert channels[0].antenna_summary == runtime.config.channel("alpha-a")[1].antenna_summary
    assert providers.assignments_for("nowhere") is None
    assert set(runtime.detection.windows) == {("alpha-a", "sat-a:t1"), ("far-a", "sat-a:t1")}
    envelope = runtime.config_envelope()
    assert envelope.version == 1 and envelope.derived_monitors == ["alpha-a", "far-a"]
    dumped = json.loads(envelope.model_dump_json())
    assert dumped["config"]["sites"][0]["channels"][0]["monitors"][0]["id"] == "sat-a:t1"
    assert runtime.node_site_for_token("token-far") == "far"
    assert runtime.node_site_for_token("token-nope") is None and runtime.node_site_for_token(None) is None


async def test_status_meta_and_the_status_message(running: Running) -> None:
    runtime = running.runtime
    subscription = runtime.console.subscribe()
    await asyncio.sleep(0)
    status = runtime.status()
    assert [n.site_id for n in status.nodes] == ["alpha", "far"] and not any(n.online for n in status.nodes)
    assert status.config_version == 1 and status.events_active == 0
    assert status.queue == schemas.QueueStatus(captures_pending=0, jobs_running=0, jobs_waiting=0)
    meta = runtime.meta()
    assert meta.simulation and meta.config_name == "runtime-test" and meta.marking is not None
    assert meta.marking.level == "UNCLASSIFIED" and not meta.api_token_required
    for _ in range(30):
        messages = [m for m in drain(subscription) if isinstance(m, dict) and m["type"] == "status"]
        if messages:
            break
        await asyncio.sleep(0.05)
    message = schemas.WsStatusMessage.model_validate(messages[-1])
    assert message.simulation and message.marking is not None and isinstance(messages[-1]["server_t_ns"], str)
    subscription.close()


# --------------------------------------------------------------------- configuration


async def test_replace_config_needs_a_matching_if_match(running: Running) -> None:
    runtime = running.runtime
    new = make_config("renamed").export_dict()
    with pytest.raises(PreconditionRequired):
        await runtime.replace_config(new, None)
    with pytest.raises(PreconditionRequired):
        await runtime.replace_config(new, "  ")
    with pytest.raises(VersionConflict) as conflict:
        await runtime.replace_config(new, '"7"')
    assert conflict.value.current_version == 1
    with pytest.raises(VersionConflict) as garbage:
        await runtime.replace_config(new, "yesterday")
    assert garbage.value.current_version == 1
    assert runtime.config_version == 1 and runtime.config.name == "runtime-test"
    assert await runtime.store.count_rows("config_versions") == 1


async def test_invalid_configurations_are_refused_with_field_errors(running: Running) -> None:
    runtime = running.runtime
    bad = make_config().export_dict()
    bad["sites"][0]["lat_deg"] = 95.0
    with pytest.raises(ConfigInvalid) as invalid:
        await runtime.replace_config(bad, 1)
    assert {"loc": "sites.0.lat_deg", "msg": invalid.value.errors[0]["msg"]} in invalid.value.errors
    with pytest.raises(ConfigInvalid) as yaml_error:
        await runtime.replace_config("name: [unclosed", 1)
    assert yaml_error.value.errors[0]["loc"] == "(root)"
    with pytest.raises(ConfigInvalid):
        await runtime.replace_config(42, 1)  # type: ignore[arg-type]
    # The running scenario realises carrier ref-1: a configuration without it does not fit.
    with pytest.raises(ConfigInvalid) as misfit:
        await runtime.replace_config(make_config(with_carrier=False).export_dict(), 1)
    assert misfit.value.errors[0]["loc"] == "(scenario)" and "ref-1" in misfit.value.errors[0]["msg"]
    result = await runtime.validate_config(bad)
    assert not result.ok and result.errors[0].loc == "sites.0.lat_deg"
    result = await runtime.validate_config(make_config().export_dict())
    assert result.ok and any("reflector" in w.msg for w in result.warnings)
    assert runtime.config_version == 1


async def test_replace_config_persists_reassigns_and_notifies(running: Running) -> None:
    runtime = running.runtime
    subscription = runtime.console.subscribe()
    before = time.time_ns()
    envelope = await runtime.replace_config(make_config("renamed", far=False).export_dict(), 'W/"1"')
    assert envelope.version == 2 and envelope.config.version == 2 and envelope.config.name == "renamed"
    assert any("reflector" in w.msg for w in envelope.warnings)
    assert runtime.config_version == 2 and runtime.config_source == "api"
    latest = await runtime.store.latest_config()
    assert latest is not None and (latest.version, latest.source) == (2, "api")
    assert runtime.assignments_for("far") is None
    assert set(runtime.detection.windows) == {("alpha-a", "sat-a:t1")}
    # Simulating: the world got a new version, announced 2 s ahead.
    assert runtime.scenario_version == 2
    (version, effective_ns), = running.announced
    assert version == 2 and before + 2_000_000_000 <= effective_ns <= time.time_ns() + 2_000_000_000
    messages = drain(subscription)
    assert {"type": "config", "config_version": 2} in messages
    assert {"type": "sim", "version": 2} in messages
    document = runtime.backplane_document()
    assert document["version"] == 2 and document["effective_ns"] == str(effective_ns)
    assert SystemConfig.model_validate(document["config"]).name == "renamed"
    # "*" matches any version; a stale If-Match after the change conflicts.
    with pytest.raises(VersionConflict):
        await runtime.replace_config(make_config("again").export_dict(), 1)
    assert (await runtime.replace_config(make_config("again"), "*")).version == 3


async def test_concurrent_saves_of_the_same_version_conflict(running: Running) -> None:
    runtime = running.runtime
    results = await asyncio.gather(
        runtime.replace_config(make_config("one").export_dict(), 1),
        runtime.replace_config(make_config("two").export_dict(), 1),
        return_exceptions=True,
    )
    assert sum(isinstance(r, schemas.ConfigEnvelope) for r in results) == 1
    assert sum(isinstance(r, VersionConflict) for r in results) == 1
    assert runtime.config_version == 2


async def test_resume_prefers_the_operator_saved_configuration(files: dict[str, Path]) -> None:
    settings = settings_for(files, scenario=False)
    first = await Running(await ServerRuntime.from_settings(settings)).start()
    await first.runtime.replace_config(make_config("operator-edit").export_dict(), 1)
    await first.close()

    second = await Running(await ServerRuntime.from_settings(settings)).start()
    try:
        assert second.runtime.config_source == "api"
        assert second.runtime.config_version == 2 and second.runtime.config.name == "operator-edit"
        assert await second.runtime.store.count_rows("config_versions") == 2
    finally:
        await second.close()

    # Without an operator save the file wins, under its stored version while unchanged.
    other = files["data"].parent / "data2"
    plain = settings.model_copy(update={"data_dir": other})
    for _ in range(2):
        r = await Running(await ServerRuntime.from_settings(plain)).start()
        assert r.runtime.config_source == "file" and r.runtime.config_version == 1
        await r.close()
    files["config"].write_text(dump_config(make_config("edited-file")))
    r = await Running(await ServerRuntime.from_settings(plain)).start()
    try:
        assert r.runtime.config_version == 2 and r.runtime.config.name == "edited-file"
        latest = await r.runtime.store.latest_config()
        assert latest is not None and latest.source == "file"
    finally:
        await r.close()


async def test_bad_files_fail_start_up_and_leave_nothing_behind(files: dict[str, Path]) -> None:
    files["scenario"].write_text(dump_scenario(Scenario.model_validate(
        {"name": "x", "emitters": [{"id": "e", "carrier_id": "nope"}]})))
    with pytest.raises(ValueError, match="nope"):
        await ServerRuntime.from_settings(settings_for(files))
    files["config"].write_text("name: [")
    with pytest.raises(ValueError):
        await ServerRuntime.from_settings(settings_for(files))
    assert store_threads() == [] and not [t for t in threading.enumerate() if t.name.startswith(("petal-work", "petal-spectrum"))]


# --------------------------------------------------------------------- simulation


async def test_scenario_replacement_presets_and_reset(running: Running) -> None:
    runtime = running.runtime
    subscription = runtime.console.subscribe()
    envelope = runtime.scenario_envelope()
    assert envelope.version == 1 and envelope.scenario.name == "base"
    tasking = runtime.tasking
    assert tasking is not None
    world_changes: list[int] = []
    tasking.world_changed = world_changes.append  # type: ignore[method-assign]
    moved = make_scenario("moved", lat=25.0).model_dump(mode="json")
    before = time.time_ns()
    result = await runtime.replace_scenario(moved)
    assert result.version == 2 and runtime.scenario.name == "moved"
    # tasking drops the old world's calibrations from the moment the new one takes effect
    assert len(world_changes) == 1 and isinstance(world_changes[0], int) and world_changes[0] > before
    assert running.announced[-1][0] == 2
    assert {"type": "sim", "version": 2} in drain(subscription)
    latest = await runtime.store.latest_scenario()
    assert latest is not None and (latest.version, latest.source) == (2, "api")
    document = runtime.backplane_document()
    assert document is runtime.backplane_document()  # cached per version
    assert Scenario.model_validate(document["scenario"]).name == "moved"

    presets = await runtime.presets()
    by_name = {p.name: p.description for p in presets}
    assert set(by_name) == {"north", "broken", "misfit"}
    assert by_name["north"] == "north test scenario" and by_name["broken"].startswith("cannot be loaded")
    loaded = await runtime.load_preset("north")
    assert loaded.version == 3 and loaded.scenario.name == "north"
    assert (await runtime.store.latest_scenario()).source == "preset"  # type: ignore[union-attr]
    for name in ("../config", "missing", "", "a/b"):
        with pytest.raises(PresetNotFound):
            await runtime.load_preset(name)
    with pytest.raises(ScenarioInvalid):
        await runtime.load_preset("broken")
    with pytest.raises(ScenarioInvalid) as misfit:
        await runtime.load_preset("misfit")
    assert "no-such-sat" in str(misfit.value)
    with pytest.raises(ScenarioInvalid) as invalid:
        await runtime.replace_scenario({"name": "x", "emitters": [{"id": "e"}]})
    assert invalid.value.errors and invalid.value.errors[0]["loc"]
    reset = await runtime.reset_scenario()
    assert reset.scenario.name == "base" and reset.version == 4
    assert runtime.scenario_version == 4 and len(running.announced) == 3
    assert len(world_changes) == 3  # replace, preset, reset; refused loads change nothing


async def test_a_server_without_scenario_is_not_simulating(files: dict[str, Path]) -> None:
    r = await Running(await ServerRuntime.from_settings(settings_for(files, scenario=False))).start()
    try:
        runtime = r.runtime
        assert not runtime.simulating and runtime.simulation_info() == {"enabled": False}
        for call in (runtime.scenario_envelope, runtime.backplane_document):
            with pytest.raises(NotSimulating):
                call()
        with pytest.raises(NotSimulating):
            await runtime.replace_scenario(make_scenario())
        with pytest.raises(NotSimulating):
            await runtime.presets()
        with pytest.raises(NotSimulating):
            await runtime.reset_scenario()
        await runtime.replace_config(make_config("x").export_dict(), 1)
        assert r.announced == []
    finally:
        await r.close()


def test_truth_emitters_resolve_carrier_defaults_and_match_events() -> None:
    cfg = make_config()
    emitters = {e.id: e for e in resolve_truth_emitters(make_scenario(), cfg)}
    assert set(emitters) == {"jam-1", "ref-1-tx"}
    assert emitters["ref-1-tx"].lat_deg == 13.4 and emitters["ref-1-tx"].freq_hz == 299.9925e6
    assert emitters["ref-1-tx"].bandwidth_hz == 5e3
    auto = {e.id for e in resolve_truth_emitters(make_scenario(carrier=False), cfg)}
    assert auto == {"jam-1", "ref-1"}  # the simulator realises the unnamed carrier itself
    record = EventRecord(id="EV-1", seq=1, created_ns=0, space="up", lo_hz=299.996e6, hi_hz=300.004e6,
                         first_seen_ns=0, last_seen_ns=0, settle_from_ns=0)
    truth = event_truth(record, list(emitters.values()))
    assert truth is not None and truth.emitter_id == "jam-1" and truth.miss_m is None
    cov = [[1.0e8, 0.0], [0.0, 1.0e8]]  # 10 km sigma each way
    record.solution_out = schemas.SolutionOut.model_validate({
        "lat_deg": 20.05, "lon_deg": 135.0, "height_m": 0.0, "cov_en_m2": cov, "cov_noise_en_m2": cov,
        "cov_consider_en_m2": [[0.0, 0.0], [0.0, 0.0]],
        "ellipse": {"semi_major_m": 24477.0, "semi_minor_m": 24477.0, "azimuth_deg": 0.0, "confidence": 0.95},
        "cep50_m": 11774.0, "chi2": 0.0, "dof": 0, "reduced_chi2": None, "residuals": [], "ambiguities": [],
        "ambiguity_resolved_by": "none", "converged": True, "dominant_error": "noise", "n_measurements": 2})
    truth = event_truth(record, list(emitters.values()))
    assert truth is not None and truth.miss_m == pytest.approx(5535.0, rel=0.01) and truth.inside_ellipse is True
    record.solution_out = record.solution_out.model_copy(update={"lat_deg": 20.5})
    truth = event_truth(record, list(emitters.values()))
    assert truth is not None and truth.inside_ellipse is False
    far_away = EventRecord(id="EV-2", seq=2, created_ns=0, space="up", lo_hz=301e6, hi_hz=301.01e6,
                           first_seen_ns=0, last_seen_ns=0, settle_from_ns=0)
    assert event_truth(far_away, list(emitters.values())) is None


# --------------------------------------------------------------------- events


def _frames(spec: Any, k: int, signal: bool, rng: np.random.Generator) -> PsdFrame:
    n = spec.nfft * spec.navg
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)
    if signal:
        freq = np.fft.fftfreq(n, 1.0 / spec.span_hz) + spec.center_hz
        w = np.fft.fft((rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2))
        w[(freq < 259.997e6) | (freq > 260.003e6)] = 0
        x = x + np.fft.ifft(w) * np.sqrt(30.0)
    psd = welch_psd_db(x.astype(np.complex64), spec.span_hz, spec.nfft, spec.navg)
    t_ns = time.time_ns()
    header = PsdHeader(channel_id=spec.channel_id, monitor_id=spec.monitor_id, t_ns=t_ns, center_hz=spec.center_hz,
                       span_hz=spec.span_hz, nfft=spec.nfft, navg=spec.navg, seq=k)
    return PsdFrame(site_id=spec.site_id, header=header, psd_db=psd, received_ns=t_ns)


async def test_events_are_published_stored_closed_and_resumed_as_history(files: dict[str, Path]) -> None:
    settings = settings_for(files)
    r = await Running(await ServerRuntime.from_settings(settings, presets_dir=files["presets"])).start()
    runtime = r.runtime
    r.online.update({"alpha", "far"})
    subscription = runtime.console.subscribe()
    rng = np.random.default_rng(5)
    specs = dict(runtime.detection.windows)
    for k in range(8):
        for key, spec in specs.items():
            await runtime.detection.on_psd(_frames(spec, k, key[0] == "alpha-a" and k >= 2, rng))
        await asyncio.sleep(0.002)
    (summary,) = runtime.list_events()
    assert summary.state == "detected" and summary.simulated
    assert summary.headline.startswith("Simulation: ")
    event = runtime.get_event(summary.id)
    assert event.simulated and event.truth is not None and event.truth.emitter_id == "jam-1"
    events = [m for m in drain(subscription) if isinstance(m, dict) and m["type"] == "event"]
    assert events and events[-1]["event"]["id"] == summary.id
    schemas.WsEventMessage.model_validate(events[-1])
    assert runtime.list_events(state="active")[0].id == summary.id
    assert runtime.list_events(state="closed") == []
    with pytest.raises(ValueError):
        runtime.list_events(state="burning")
    with pytest.raises(ValueError):
        runtime.list_events(limit=0)
    with pytest.raises(EventNotFound):
        runtime.get_event("EV-424242")
    with pytest.raises(EventNotFound):
        runtime.close_event("EV-424242")
    await r.close()
    rows = await _rows(files["data"], "SELECT state FROM events")
    assert rows == [("detected",)]

    # Restart: the event comes back as inactive history.
    r2 = await Running(await ServerRuntime.from_settings(settings, presets_dir=files["presets"])).start()
    try:
        runtime2 = r2.runtime
        (old,) = runtime2.list_events()
        assert old.id == summary.id and old.state == "lost"
        doc = runtime2.get_event(old.id)
        assert doc.transitions[-1].reason == "server restarted: kept as history"
        assert runtime2.status().events_active == 0
        closed = runtime2.close_event(old.id)
        assert closed.state == "closed" and runtime2.list_events(state="closed")[0].id == old.id
        assert runtime2.events.get(old.id).historic  # type: ignore[union-attr]
        # A new event never reuses an old id.
        record = runtime2.events.create(space="up", lo_hz=1.0, hi_hz=2.0, first_seen_ns=0, last_seen_ns=0)
        assert record.seq == 2
    finally:
        await r2.close()
    states = await _rows(files["data"], "SELECT event_id, state FROM events ORDER BY seq")
    assert states[0] == (summary.id, "closed")
    transitions = await _rows(files["data"], f"SELECT reason FROM event_transitions WHERE event_id = '{summary.id}'")
    assert ("server restarted: kept as history",) in transitions and ("closed by operator",) in transitions


async def test_a_node_coming_online_resets_its_windows_and_status_carries_window_health(running: Running) -> None:
    """SRV-1 wiring: the runtime hands the hub the detection pipeline's window health,
    and a new session of a node forgets the frame times of that site's windows, so a node
    whose clock was corrected back while it was away is not refused."""
    from petal_locate.server.hub import StatusEvent

    runtime, hub = running.runtime, running.hub
    assert hub._window_health == runtime.detection.window_health
    spec = runtime.detection.windows[("alpha-a", "sat-a:t1")]
    rng = np.random.default_rng(9)
    first = _frames(spec, 0, False, rng)
    await runtime.detection.on_psd(first)
    earlier = _frames(spec, 1, False, rng)
    earlier = PsdFrame(site_id=earlier.site_id, header=earlier.header.model_copy(update={"t_ns": first.t_ns - 10**9}),
                       psd_db=earlier.psd_db, received_ns=earlier.received_ns)
    await runtime.detection.on_psd(earlier)  # one second older than the newest: refused
    assert runtime.detection.counters()["out_of_order"] == 1
    runtime._on_node_status(StatusEvent(site_id="alpha", kind="online", reason="hello", status={"node_id": "n"},
                                        t_ns=time.time_ns()))
    await runtime.detection.on_psd(earlier)  # the new session's frame is accepted
    counters = runtime.detection.counters()
    assert counters["accepted"] == 2 and counters["site_resets"] == 1
    health = runtime.detection.window_health("alpha-a", "sat-a:t1")
    assert health is not None and health["blind"] is False and health["learning"] is True


async def _rows(data: Path, sql: str) -> list[tuple[Any, ...]]:
    store = Store(data)
    await store.open()
    try:
        return await store.query(sql)
    finally:
        await store.close()


# --------------------------------------------------------------------- building blocks


def test_parse_if_match_forms() -> None:
    assert parse_if_match(3) == {3}
    assert parse_if_match("3") == {3}
    assert parse_if_match('"3"') == {3}
    assert parse_if_match('W/"3", "4"') == {3, 4}
    assert parse_if_match("*") is None
    for absent in (None, ""):
        with pytest.raises(PreconditionRequired):
            parse_if_match(absent)
    for bad in ("v3", '"3', "3.0", True):
        if bad == '"3':
            assert parse_if_match(bad) == {3}  # a lone quote is tolerated
            continue
        with pytest.raises(VersionConflict):
            parse_if_match(bad)  # type: ignore[arg-type]


async def test_console_bus_newest_wins_per_key_and_filters_spectra() -> None:
    bus = ConsoleBus()
    slow = bus.subscribe()
    fast = bus.subscribe(psd=[("a", "m")])
    for i in range(5):
        bus.publish_text("status", f'{{"n": {i}}}')
    bus.publish_text("event:EV-1", '{"e": 1}')
    bus.publish_psd("a", "m", b"one")
    bus.publish_psd("a", "m", b"two")
    bus.publish_psd("b", "m", b"other")
    assert bus.wants_psd("a", "m") and not bus.wants_psd("b", "m")
    assert await fast.get() == '{"n": 4}'
    assert await fast.get() == '{"e": 1}'
    assert await fast.get() == b"two"
    assert fast.get_nowait() is None and fast.dropped == 5
    assert slow.waiting == 2 and slow.dropped == 4
    fast.set_psd([])
    bus.publish_psd("a", "m", b"three")
    assert fast.get_nowait() is None
    with pytest.raises(ValueError):
        fast.set_psd([("only-one",)])  # type: ignore[list-item]
    waiter = asyncio.create_task(fast.get())
    await asyncio.sleep(0)
    bus.publish_text("config", "{}")
    assert await waiter == "{}"
    assert [slow.get_nowait() for _ in range(3)] == ['{"n": 4}', '{"e": 1}', "{}"]
    closing = asyncio.create_task(slow.get())
    await asyncio.sleep(0)
    slow.close()
    with pytest.raises(ConsoleClosed):
        await closing
    assert bus.subscribers == 1
    seen = []
    bus.publish_text("sim", "x")
    bus.close()
    async for item in fast:
        seen.append(item)
    assert seen == [] and bus.subscribers == 0


async def test_work_pool_limits_concurrency_and_closes_cleanly() -> None:
    pool = WorkPool(2)
    gate = threading.Event()
    active: list[int] = []
    peak = [0]
    lock = threading.Lock()

    def job(i: int) -> int:
        with lock:
            active.append(i)
            peak[0] = max(peak[0], len(active))
        gate.wait(2.0)
        with lock:
            active.remove(i)
        return i * i

    tasks = [asyncio.create_task(pool.run(job, i)) for i in range(5)]
    for _ in range(100):
        if pool.running == 2:
            break
        await asyncio.sleep(0.01)
    assert pool.running == 2 and pool.waiting == 3
    gate.set()
    assert await asyncio.gather(*tasks) == [0, 1, 4, 9, 16]
    assert peak[0] == 2 and pool.running == 0 and pool.waiting == 0
    with pytest.raises(ZeroDivisionError):
        await pool.run(lambda: 1 / 0)
    await pool.close()
    await pool.close()
    with pytest.raises(RuntimeError):
        await pool.run(job, 1)
    assert not [t for t in threading.enumerate() if t.name.startswith(("petal-work", "petal-spectrum"))]
    with pytest.raises(ValueError):
        WorkPool(0)


async def test_direct_construction_stores_its_configuration_at_start(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    await store.open()
    await store.save_config("old: 1\n", "yaml", "file")  # a different document under version 1
    settings = Settings(config_path=tmp_path / "unused.yaml", data_dir=tmp_path / "data")
    runtime = ServerRuntime(settings, make_config(), None, store)
    assert runtime.config_version == 1
    r = await Running(runtime).start()
    try:
        assert runtime.config_version == 2 and runtime.config.version == 2
        assert runtime.config_envelope().version == 2
    finally:
        await r.close()
    with pytest.raises(RuntimeError):
        await runtime.start(r.hub)


# --------------------------------------------------------------------- over real sockets


async def test_runtime_behind_the_real_app_receives_node_spectra_and_records_sessions(files: dict[str, Path]) -> None:
    """The runtime's providers drive a real hub served by uvicorn; a real ``NodeAgent``
    (noise driver) connects, is assigned the configured windows, and its spectrum
    frames reach the detection pipeline and the console; its session is stored."""
    from petal_locate.node.agent import NodeAgent
    from petal_locate.protocol.framing import decode_console_psd, decode_frame
    from petal_locate.server.app import build_server, create_app

    settings = settings_for(files, scenario=False).model_copy(update={"port": 0, "heartbeat_s": 0.5})
    runtime = await ServerRuntime.from_settings(settings)
    app = create_app(settings, hub_providers=runtime.hub_providers())
    server = build_server(app, settings, log_config=None)
    before = asyncio.all_tasks()
    serve = asyncio.create_task(server.serve())
    agent: NodeAgent | None = None
    try:
        for _ in range(400):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        await runtime.start(app.state.hub)
        console = runtime.console.subscribe(psd=[("alpha-a", "sat-a:t1")])
        agent = NodeAgent(f"ws://127.0.0.1:{port}", "alpha", "token-alpha", node_id="node-alpha", driver="noise",
                          proxy=None, backoff_base_s=0.05, backoff_max_s=0.4, close_timeout_s=1.0)
        agent.start()
        await asyncio.wait_for(agent.wait_online(), 8.0)
        for _ in range(800):
            if runtime.detection.counters().get("accepted", 0) >= 3:
                break
            await asyncio.sleep(0.01)
        assert runtime.detection.counters()["accepted"] >= 3
        assert app.state.hub.channel_ready("alpha-a")
        frames = []
        for _ in range(200):
            item = console.get_nowait()
            if isinstance(item, bytes):
                frames.append(item)
                break
            if item is None:
                await asyncio.sleep(0.01)
        header, payload = decode_frame(frames[0])
        console_header, psd, baseline = decode_console_psd(header, payload)
        assert console_header.channel_id == "alpha-a" and psd.shape == (console_header.nfft,)
        status = runtime.status()
        alpha = next(n for n in status.nodes if n.site_id == "alpha")
        assert alpha.online and alpha.driver == "noise" and alpha.channels[0].channel_id == "alpha-a"
        assert runtime.list_events() == []  # unit noise raises nothing
        await agent.stop(5.0)
        agent = None
        for _ in range(200):
            if not app.state.hub.is_online("alpha"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        rows = await runtime.store.query("SELECT site_id, node_id, disconnected_ns IS NOT NULL, driver FROM node_sessions")
        assert rows == [("alpha", "node-alpha", 1, "noise")]
        console.close()
    finally:
        if agent is not None:
            await agent.stop(5.0)
        await runtime.stop()
        server.should_exit = True
        await asyncio.wait_for(serve, 20.0)
    await asyncio.sleep(0.02)
    leftover = [t for t in asyncio.all_tasks() - before if t is not asyncio.current_task() and not t.done()]
    assert not leftover, [t.get_name() for t in leftover]


async def test_runtime_holder_delegates_once_a_runtime_is_attached(files: dict[str, Path]) -> None:
    holder = RuntimeHolder()
    providers = holder.hub_providers()
    assert providers.assignments_for("alpha") is None
    assert providers.config_version() == 0 and providers.simulation_info() == {"enabled": False}
    runtime = await ServerRuntime.from_settings(settings_for(files))
    holder.runtime = runtime
    try:
        assert providers.assignments_for("alpha") == runtime.assignments_for("alpha")
        assert providers.config_version() == 1 and providers.simulation_info()["version"] == 1
    finally:
        await runtime.stop()


# --------------------------------------------------------------------- orbit validity (ADAPT-7)


async def test_status_reports_a_satellite_whose_ephemeris_has_ended(tmp_path: Path) -> None:
    """ADAPT-7: the runtime checks every satellite's orbit at the server time; a table
    that has ended is reported in /status (and the status message) with the table's
    end, and the report clears when the ephemeris is valid again."""
    from petal_locate.core.orbits import orbit_state
    from petal_locate.core.timebase import iso_from_ns, ns_from_iso

    base = make_config()
    now = ns_from_iso("2026-11-13T03:00:00Z")
    end = now - 120 * 1_000_000_000
    orbit = base.orbit("sat-a")
    samples = []
    for k in range(-10, 1):
        t_ns = end + k * 60 * 1_000_000_000
        r, v = orbit_state(orbit, t_ns / 1e9)
        samples.append({"t": iso_from_ns(t_ns), "r_m": [float(x) for x in r], "v_mps": [float(x) for x in v]})
    data = base.model_dump(mode="json")
    data["satellites"][0]["orbit"] = {"type": "table", "samples": samples}
    cfg = SystemConfig.model_validate(data)
    store = Store(tmp_path / "data")
    await store.open()
    clock = {"t": now}
    runtime = ServerRuntime(
        Settings(config_path=tmp_path / "c.yaml", data_dir=tmp_path / "data"), cfg, None, store, clock=lambda: clock["t"]
    )
    try:
        rows = {r.sat_id: r for r in runtime.check_orbits()}
        assert rows["sat-a"].ephemeris_ok is False and rows["sat-a"].unavailable_since_ns == end
        assert rows["sat-a"].message.startswith(f"satellite {cfg.satellite('sat-a').name} (sat-a): ephemeris not available since 2026-11-13T02:58:00")
        assert "its pairs are not tasked" in rows["sat-a"].message
        assert all(r.ephemeris_ok and r.message is None for s, r in rows.items() if s != "sat-a")
        status = runtime.status()
        assert [s.sat_id for s in status.satellites] == [s.id for s in cfg.satellites]
        assert status.site_clocks == []  # no tasking, no calibrations
        message = json.loads(runtime.status_message().model_dump_json())
        assert message["satellites"][0]["ephemeris_ok"] is False
        assert message["satellites"][0]["unavailable_since_ns"] == str(end)
        # back inside the table: available again
        clock["t"] = end - 60 * 1_000_000_000
        rows = {r.sat_id: r for r in runtime.check_orbits()}
        assert rows["sat-a"].ephemeris_ok and rows["sat-a"].unavailable_since_ns is None
    finally:
        await runtime.stop()
