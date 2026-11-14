"""Tests of capture tasking, correlation, calibration and solving (``pipeline/tasking.py``).

The hub is a stand-in whose ``capture()`` returns IQ rendered by ``sim.world.SimWorld``
for the shipped ``pacific-uhf`` configuration and its scenarios, at exactly the time,
rate and centre requested (or with the offsets a test injects). The server clock is
under test control, so every round is deterministic: same captures, same noise, same
numbers on every run. Truth comes from ``SimWorld.truth_measurement`` (all truth
errors applied) and from the predictors at the true position with the believed orbits.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from petal_locate.config import SystemConfig, dump_config, load_config
from petal_locate.core.detect import Classification
from petal_locate.core.geodesy import geodesic_distance_m
from petal_locate.core.geometry import elevation_deg, predict_pair
from petal_locate.core.solver import Solution
from petal_locate.core.timebase import ns_from_iso
from petal_locate.core.types import Measurement, PathSpec
from petal_locate.protocol.framing import AssembledCapture
from petal_locate.protocol.messages import Capabilities, ClockStatus
from petal_locate.server.api import schemas
from petal_locate.server.hub import CaptureFailed, Hub
from petal_locate.server.pipeline import tasking as tasking_module
from petal_locate.server.pipeline.correlator import QualityTable
from petal_locate.server.pipeline.events import EventBook, EventRecord, Unit, event_truth
from petal_locate.server.pipeline.tasking import (
    MAX_LEAD_S,
    REASON_CAPTURE_FAILED,
    REASON_CLOCK_UNLOCKED,
    REASON_INCONSISTENT,
    REASON_NARROWBAND_CAF,
    REASON_RATE_MISMATCH,
    REASON_UNCONFIRMED,
    PairOutcome,
    RoundReport,
    Tasking,
    truth_emitters,
)
from petal_locate.server.runtime import ServerRuntime, WorkPool
from petal_locate.server.settings import Settings
from petal_locate.server.store import Store, store_threads
from petal_locate.sim.scenario import Scenario, load_scenario
from petal_locate.sim.world import SimWorld

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config" / "pacific-uhf.yaml")
DEMO = load_scenario(ROOT / "config" / "scenarios" / "pacific-demo.yaml")
CW = load_scenario(ROOT / "config" / "scenarios" / "uplink-cw.yaml")
T0 = ns_from_iso("2026-11-13T03:00:00Z")
S = 1_000_000_000

# The demo jammer: 10 kHz of noise at 301.005 MHz in shared-1 (all three satellites).
JAMMER = ("jammer-philsea", 301.000e6, 301.010e6, "shared-1")
SIGHTINGS = (("tokyo-pac", 15.0), ("naha-io", 12.0), ("seoul-172e", 10.0))

_WORLDS: dict[int, SimWorld] = {}


def world_for(scenario: Scenario, config: SystemConfig = CONFIG) -> SimWorld:
    """One SimWorld per (scenario, config) object for the module (they are immutable)."""
    key = id(scenario) ^ (id(config) << 1)
    if key not in _WORLDS:
        _WORLDS[key] = SimWorld(config, scenario)
    return _WORLDS[key]


def with_processing(config: SystemConfig, **sections: dict[str, Any]) -> SystemConfig:
    """A copy of ``config`` with processing sections updated."""
    data = config.model_dump()
    for name, values in sections.items():
        data["processing"][name].update(values)
    return SystemConfig.model_validate(data)


NO_CAL = with_processing(CONFIG, calibration={"enabled": False})


class Clock:
    """Server clock under test control (integer ns)."""

    def __init__(self, t_ns: int) -> None:
        self.t = t_ns

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(round(seconds * S))


@dataclass
class FakeHub:
    """The hub as tasking sees it; captures are rendered by a SimWorld (or fail).

    fail: channel -> codes to raise on its successive calls (None = succeed).
    rate_factor / unlocked / t_offset_ns / fc_offset_hz: what the 'node' returns
    differently from the request for a channel. block: captures wait for this event.
    """

    world: SimWorld | None
    pool: WorkPool
    rtt: Callable[[str], float | None] = lambda site_id: 20.0
    fail: dict[str, list[str | None]] = field(default_factory=dict)
    fail_all: str | None = None
    rate_factor: dict[str, float] = field(default_factory=dict)
    unlocked: set[str] = field(default_factory=set)
    t_offset_ns: dict[str, int] = field(default_factory=dict)
    fc_offset_hz: dict[str, float] = field(default_factory=dict)
    block: asyncio.Event | None = None
    max_hz: float = 6e9
    calls: list[tuple[str, int, int, float, float, int]] = field(default_factory=list)
    pending: int = 0
    cancelled: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)

    async def capture(
        self, channel_id: str, t_start_ns: int, n_samples: int, sample_rate_hz: float, center_hz: float,
        deadline_ns: int,
    ) -> AssembledCapture:
        self.calls.append((channel_id, t_start_ns, n_samples, sample_rate_hz, center_hz, deadline_ns))
        assert isinstance(t_start_ns, int) and isinstance(deadline_ns, int) and deadline_ns > t_start_ns
        self.pending += 1
        self.started.set()
        try:
            if self.block is not None:
                await self.block.wait()
            code = self.fail_all
            if code is None and self.fail.get(channel_id):
                code = self.fail[channel_id].pop(0)
            if code is not None:
                raise CaptureFailed(code, f"simulated {code}", channel_id=channel_id)
            assert self.world is not None
            t_first = t_start_ns + self.t_offset_ns.get(channel_id, 0)
            fc = center_hz + self.fc_offset_hz.get(channel_id, 0.0)
            iq = await self.pool.run(self.world.render_iq, channel_id, t_first, n_samples, sample_rate_hz, fc)
            clock = ClockStatus(
                source="sim", locked=channel_id not in self.unlocked, est_offset_ns=0.0, holdover_s=0.0
            )
            rate = sample_rate_hz * self.rate_factor.get(channel_id, 1.0)
            return AssembledCapture(f"c{len(self.calls)}", channel_id, iq, t_first, rate, fc, False, clock, 1)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.pending -= 1

    def rtt_ms(self, site_id: str) -> float | None:
        return self.rtt(site_id)

    def channel_capabilities(self, channel_id: str) -> Capabilities:
        return Capabilities(min_hz=1e6, max_hz=self.max_hz, max_sample_rate_hz=2e6, lookback_s=30.0, concurrent_reads=4)

    def is_online(self, site_id: str) -> bool:
        return True


class FakeStore:
    """Records what tasking stores."""

    def __init__(self) -> None:
        self.calibrations: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []

    def add_calibration(self, **kwargs: Any) -> None:
        self.calibrations.append(kwargs)

    async def save_capture(self, iq: np.ndarray, **kwargs: Any) -> None:
        assert iq.dtype == np.complex64
        self.captures.append(kwargs)


class SpyPool(WorkPool):
    """A WorkPool that remembers the jobs it ran."""

    def __init__(self, limit: int = 3) -> None:
        super().__init__(limit)
        self.jobs: list[tuple[Any, tuple[Any, ...]]] = []

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        self.jobs.append((fn, args))
        return await super().run(fn, *args, **kwargs)


@dataclass
class Env:
    clock: Clock
    book: EventBook
    pool: SpyPool
    hub: FakeHub
    store: FakeStore
    tasking: Tasking
    world: SimWorld | None


@contextlib.asynccontextmanager
async def environment(
    config: SystemConfig = CONFIG, scenario: Scenario | None = DEMO, **hub_options: Any
) -> AsyncIterator[Env]:
    """Book, pools, fake hub and store, and a Tasking; everything is stopped after."""
    clock = Clock(T0)
    world = world_for(scenario, config) if scenario is not None else None
    book = EventBook(clock=clock, simulated=lambda: True)
    if world is not None:
        emitters = truth_emitters(world)
        book.set_truth(lambda record: event_truth(record, emitters))
    pool = SpyPool(3)
    hub_pool = WorkPool(2)
    hub = FakeHub(world, hub_pool, **hub_options)
    store = FakeStore()

    def sightings(event_id: str) -> list[Unit]:
        record = book.resolve(event_id)
        return [] if record is None else sorted(record.members.values(), key=lambda u: -u.snr_db)

    tasking = Tasking(
        hub=hub, book=book, config=config, pool=pool, sightings=sightings, store=store, clock=clock,
        simulated=lambda: True, tick_s=0.02,
    )
    try:
        yield Env(clock, book, pool, hub, store, tasking, world)
    finally:
        await tasking.stop()
        await pool.close()
        await hub_pool.close()
    await asyncio.sleep(0)
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftovers == [], leftovers
    assert not [t for t in threading.enumerate() if t.name.startswith("petal-work")]


def make_event(
    env: Env,
    lo_hz: float = JAMMER[1],
    hi_hz: float = JAMMER[2],
    transponder_id: str = JAMMER[3],
    sightings: Iterable[tuple[str, float]] = SIGHTINGS,
    config: SystemConfig = CONFIG,
) -> EventRecord:
    """An active uplink event with the given detections (as the correlator would hold)."""
    record = env.book.create(space="up", lo_hz=lo_hz, hi_hz=hi_hz, first_seen_ns=T0, last_seen_ns=T0)
    for track, (channel_id, snr) in enumerate(sightings):
        site, channel = config.channel(channel_id)
        sat_id = channel.watched_satellite_ids[0]
        shift = config.satellite(sat_id).transponder(transponder_id).translation_hz
        key = (channel_id, "m", 0, track, sat_id)
        record.members[key] = Unit(
            key, channel_id, "m", site.id, sat_id, transponder_id, "up", lo_hz, hi_hz, lo_hz + shift, hi_hz + shift,
            snr, 50.0, 10.0, T0, T0, None, False, T0,
        )
    env.book.flush()
    return record


async def rounds(env: Env, record: EventRecord, count: int) -> list[RoundReport]:
    out = []
    for _ in range(count):
        out.append(await env.tasking.run_round(record.id))
        env.clock.advance(5.0)
    env.book.flush(force=True)
    return out


def nominal_truth(world: SimWorld, emitter_id: str, m: Measurement, f_e_hz: float) -> tuple[float, float]:
    """True TDOA / FDOA of a row with both channels at their nominal tuning (what the
    row's value means after the centre correction of MATH.md section 5)."""
    a = PathSpec(m.a.sat_id, m.a.site_id, f_e_hz, m.a.translation_hz, m.a.tau_x_s)
    b = PathSpec(m.b.sat_id, m.b.site_id, f_e_hz, m.b.translation_hz, m.b.tau_x_s)
    return world.truth_measurement(emitter_id, a, b, m.t_ns)


def caf_jobs(env: Env) -> list[Any]:
    return [args[0] for fn, args in env.pool.jobs if fn is tasking_module._correlate]


# ----------------------------------------------------------------------------- full round


async def test_full_round_measures_within_three_sigma_and_locates_the_jammer() -> None:
    # Channel B of one pair starts 0.52 ms late and the other is tuned 3 Hz high: the
    # integer first-sample difference and the centre correction must absorb both.
    async with environment(t_offset_ns={"naha-172e": 520_000}, fc_offset_hz={"tokyo-io": 3.0}) as env:
        record = make_event(env)
        reports = await rounds(env, record, 3)
        first = reports[0]
        assert first.primary_channel_id == "tokyo-pac"
        assert first.sample_rate_hz == 20000.0 and first.n_samples == 20000
        assert first.t_start_ns == T0 + int(0.6 * S)  # lead_s; 3 x 20 ms RTT is less
        partners = {CONFIG.channel(c)[1].watched_satellite_ids[0]: c for c in first.partner_channel_ids}
        assert set(partners) == {"uhf-172e", "muos-io"}
        assert CONFIG.channel(partners["muos-io"])[0].id == "tokyo"  # the primary's site is preferred
        # every capture of a round shares t_start, rate and length; centres are f_e + T
        target = [c for c in env.hub.calls[:6] if c[1] == first.t_start_ns]
        assert len(target) == 3 and {c[2] for c in target} == {20000} and {c[3] for c in target} == {20000.0}
        for channel_id, _, _, _, centre, _ in target:
            site, channel = CONFIG.channel(channel_id)
            sat = channel.watched_satellite_ids[0]
            assert centre == 301.005e6 + CONFIG.satellite(sat).transponder("shared-1").translation_hz

        # first round: detected but unconfirmed, nothing solved, no verdict evidence yet
        assert all(p.reason == REASON_UNCONFIRMED and p.result is not None and p.result.detected for p in first.pairs)
        assert first.solution is None and first.classification == "noise"
        assert len(first.calibrations) == 4 and {c.carrier_id for c in first.calibrations} == {"ref-guam"}
        # the pairs' own calibrations in the first round; same-satellite clock checks (R3) later
        pair_cals = [c for c in env.store.calibrations if c["a_sat_id"] != c["b_sat_id"]]
        assert len(pair_cals) == 4
        later = [c for r in reports[1:] for c in r.calibrations]
        assert later and all(c.key[0] == c.key[2] and c.key[1] != c.key[3] for c in later)
        assert len(env.store.calibrations) == 4 + len(later)
        assert {c["kind"] for c in env.store.calibrations} == {"tdoa", "fdoa"}
        assert all(c["detail"]["simulated"] for c in env.store.calibrations)
        assert len({c.cal.cal_id for c in first.calibrations}) == 4
        await asyncio.sleep(0.05)  # capture writes are background tasks
        assert len(env.store.captures) == len(env.hub.calls)

        # later rounds: confirmed, calibrated, used and solved
        for report in reports[1:]:
            assert [p.status for p in report.pairs] == ["measured", "measured"]
            assert all(p.calibrated == ("tdoa", "fdoa") for p in report.pairs)
            assert report.solution is not None and report.solution.converged
        assert record.caf_uplink

        used = record.used_measurements()
        assert len(used) == 8
        errors: dict[int, float] = {}  # seq -> (raw value - truth) / sigma
        for entry in used:
            m = entry.measurement
            tdoa, fdoa = nominal_truth(env.world, "jammer-philsea", m, 301.005e6)
            errors[entry.seq] = (m.value - (tdoa if m.kind == "tdoa" else fdoa)) / m.sigma
            assert m.cal is not None and m.correction != 0.0
            assert entry.row.calibrated and entry.row.value == pytest.approx(m.value - m.correction)
        # The rows of one full round lie within 3 sigma of truth. (Every row of several
        # rounds within 3 sigma would fail for about 2 % of noise realisations with 8
        # rows; across rounds the bound is 4 sigma and the RMS must look like unit noise.
        # A 16-epoch scratch run against SimWorld gave means within 2 standard errors
        # of 0 and spreads of 0.8 to 1.2.)
        second = reports[1].pairs
        round_two = [s for p in second for s in (p.tdoa_seq, p.fdoa_seq)]
        assert len(round_two) == 4 and all(abs(errors[s]) <= 3.0 for s in round_two), errors
        assert all(abs(e) <= 4.0 for e in errors.values()), errors
        assert math.sqrt(sum(e * e for e in errors.values()) / len(errors)) < 2.0, errors

        doc = env.book.to_schema(record)
        assert doc.state == "located"
        states = [t.state for t in doc.transitions]
        assert states.index("characterizing") < states.index("locating") < states.index("located")
        assert doc.solution is not None and len(doc.solution_history) == 2
        assert doc.truth is not None and doc.truth.emitter_id == "jammer-philsea"
        assert doc.truth.inside_ellipse is True
        assert doc.truth.miss_m < doc.solution.ellipse.semi_major_m
        # rows: residuals for the solved ones, reasons for the first round's
        solved = [r for r in doc.measurements if r.used]
        assert all(r.residual is not None and math.isfinite(r.residual) for r in solved)
        assert {r.reason for r in doc.measurements if not r.used} == {REASON_UNCONFIRMED}
        assert doc.caf is not None and doc.caf.detected and len(doc.caf.surface) > 1
        assert doc.classification is not None and doc.classification.label == "noise"
        assert doc.tasking is not None and doc.tasking.rounds == 3 and doc.tasking.state == "waiting"
        assert doc.tasking.channels[0] == "tokyo-pac" and doc.tasking.solve_reason is None
        doc.model_dump_json()  # the whole document serialises (no NaN anywhere)

        # exclusions and mirror window: the co-channel pair at one site has both
        # satellites heard by both channels, the direct-path point, and a mirror window
        jobs = caf_jobs(env)
        co = next(j for j in jobs if j.path_b.sat_id == "muos-io" and j.f_e_hz == 301.005e6)
        assert {s for s, _ in co.heard} == {"muos-pac", "muos-io"}
        assert co.direct_point and co.mirror_window is not None
        cross = next(j for j in jobs if j.path_b.sat_id == "uhf-172e" and j.f_e_hz == 301.005e6)
        assert cross.heard == () and not cross.direct_point and cross.mirror_window is None
        assert cross.t0_offset_s == pytest.approx(-520e-6)
        assert co.path_b.f_tune_hz == pytest.approx(301.005e6 - 41.0e6 + 3.0)


async def test_calibration_removes_an_injected_common_ephemeris_bias() -> None:
    data = DEMO.model_dump(mode="json")
    data["errors"]["ephemeris"]["muos-pac"] = {"ric_pos_m": [100.0, 6000.0, 0.0], "ric_vel_mps": [0.0, 0.0, 0.0]}
    data["errors"]["ephemeris"]["uhf-172e"] = {"ric_pos_m": [0.0, 0.0, -4000.0], "ric_vel_mps": [0.0, 0.0, 0.0]}
    data["emitters"] += [  # stronger references than configured, so their own noise is small
        {"id": "ref-guam", "kind": "carrier", "carrier_id": "ref-guam", "eirp_dbw": 28.0},
        {"id": "ref-sapporo", "kind": "carrier", "carrier_id": "ref-sapporo", "eirp_dbw": 28.0},
    ]
    biased = Scenario.model_validate(data)
    world = world_for(biased)
    x_true = world.emitter("jammer-philsea").xyz
    orbits, sites = CONFIG.orbits(), CONFIG.sites_xyz()

    misses: dict[str, float] = {}
    async with environment(CONFIG, biased) as env:
        record = make_event(env)
        reports = await rounds(env, record, 2)
        # the two pairs' calibrations (the second round adds same-satellite clock checks, R3)
        assert len([c for c in env.tasking.calibrations() if c.key[0] != c.key[2]]) == 4
        for entry in record.used_measurements():
            m = entry.measurement
            if m.kind != "tdoa":
                continue
            h = float(predict_pair(x_true, m.a, m.b, m.t_ns / S, orbits, sites)[0])
            total = math.hypot(m.sigma, m.cal.sigma)
            assert abs(m.value - h) > 10.0 * m.sigma  # the ephemeris error is in the raw TDOA
            assert abs(m.value - m.correction - h) < 3.0 * total  # and the calibration takes it out
        solution = reports[-1].solution
        assert solution is not None
        misses["calibrated"] = geodesic_distance_m(solution.lat_deg, solution.lon_deg, 20.0, 132.0)
        assert env.book.to_schema(record).truth.inside_ellipse is True
    async with environment(NO_CAL, biased) as env:
        record = make_event(env, config=NO_CAL)
        reports = await rounds(env, record, 2)
        assert env.tasking.calibrations() == [] and env.store.calibrations == []
        assert all(not e.row.calibrated for e in record.used_measurements())
        solution = reports[-1].solution
        assert solution is not None
        misses["uncalibrated"] = geodesic_distance_m(solution.lat_deg, solution.lon_deg, 20.0, 132.0)
    assert misses["uncalibrated"] > 3.0 * misses["calibrated"], misses
    assert misses["calibrated"] < 1500.0, misses


# ----------------------------------------------------------------------------- reasons


async def test_cw_emitter_gives_fdoa_rows_only_with_the_narrowband_reason() -> None:
    f_cw = 301.130e6
    async with environment(NO_CAL, CW) as env:
        # Primary at Honolulu, which does not see muos-io. Against the uhf-172e partner
        # each channel hears exactly one copy of the tone; the muos-io partner's channel
        # also hears muos-pac (an excluded self-peak). A CW has no delay to separate
        # copies by, and a second ridge a few Doppler bins away pulls the tone estimate
        # by up to about 5 reported sigma (a limit of the estimator, measured at Tokyo
        # and Seoul, see the tasking report), so the 3-sigma check is made on the
        # single-copy pair only.
        record = make_event(
            env, f_cw - 50.0, f_cw + 50.0, "shared-2",
            (("honolulu-pac", 25.0), ("naha-io", 22.0), ("seoul-172e", 20.0)), NO_CAL,
        )
        reports = await rounds(env, record, 2)
        assert reports[0].primary_channel_id == "honolulu-pac"
        assert reports[0].classification == "cw"
        rows = env.book.to_schema(record).measurements
        tdoa = [r for r in rows if r.kind == "tdoa"]
        fdoa = [r for r in rows if r.kind == "fdoa"]
        assert tdoa and all(not r.used and r.value is None and r.reason == REASON_NARROWBAND_CAF for r in tdoa)
        assert [r.used for r in fdoa] == [False, False, True, True]
        assert {r.reason for r in fdoa[:2]} == {REASON_UNCONFIRMED}
        used = record.used_measurements()
        assert len(used) == 2 and all(e.measurement.kind == "fdoa" for e in used)
        for entry in used:
            m = entry.measurement
            error = m.value - nominal_truth(env.world, "jammer-cw", m, f_cw)[1]
            bound = 3.0 * m.sigma if m.b.sat_id == "uhf-172e" else 0.1  # Hz for the two-copy pair
            assert abs(error) <= bound, (m.b, error / m.sigma)
        assert all(p.status == "measured" for p in reports[1].pairs)
        # The operator is told why there is no TDOA, in words.
        message = env.book.to_schema(record).tasking.message
        assert message is not None and message.startswith(tasking_module.EXPLAIN_NARROWBAND)


async def test_cw_beside_a_reference_carrier_is_classified_from_the_event_band() -> None:
    """A narrow event's primary capture runs at the 20 kHz minimum rate, so it also holds
    the top 1.5 kHz of the 8 kHz Sapporo reference, 8.5 kHz below the tone. Classified
    as captured, that sliver is taken for the signal and the tone comes out ``noise``
    (tokyo-pac at 03 UTC, SIMULATED); classified from the event's own band it is ``cw``."""
    f_cw = 301.130e6
    async with environment(NO_CAL, CW) as env:
        record = make_event(
            env, f_cw - 50.0, f_cw + 50.0, "shared-2",
            (("tokyo-pac", 25.0), ("naha-io", 22.0), ("seoul-172e", 20.0)), NO_CAL,
        )
        reports = await rounds(env, record, 1)
        assert reports[0].primary_channel_id == "tokyo-pac"
        assert reports[0].classification == "cw"
        features = env.book.to_schema(record).classification.features
        assert abs(features["center_offset_hz"]) < 100.0  # the tone, not the reference 8.7 kHz away


async def test_a_new_simulated_world_drops_the_calibrations_of_the_old_one() -> None:
    """Calibrations made in the clock-fault world (Naha 10 us late, small ephemeris errors)
    must not correct rows measured once ephemeris-error is loaded (two satellites 6 and
    8 km off in-track). Before ``world_changed`` existed they did for up to
    ``calibration.interval_s``: every row of the next event carried them and its fix
    was 1.7 km off, outside a 0.38 km ellipse (SIMULATED)."""
    clock_fault = load_scenario(ROOT / "config" / "scenarios" / "clock-fault.yaml")
    ephemeris_error = load_scenario(ROOT / "config" / "scenarios" / "ephemeris-error.yaml")
    async with environment(CONFIG, clock_fault) as env:
        first = make_event(env)
        await rounds(env, first, 1)
        old = {c.cal.cal_id for c in env.tasking.calibrations()}
        assert old
        env.clock.advance(10.0)  # well inside calibration.interval_s
        env.hub.world = world_for(ephemeris_error)  # the preset load
        env.tasking.world_changed(env.clock())
        assert env.tasking.calibrations() == []
        second = make_event(env)
        reports = await rounds(env, second, 2)
        used = second.used_measurements()
        assert used and all(e.measurement.cal is not None for e in used)
        assert not [e for e in used if e.measurement.cal.cal_id in old]
        solution = reports[-1].solution
        assert solution is not None
        miss = geodesic_distance_m(solution.lat_deg, solution.lon_deg, 17.0, 141.0)
        assert miss < solution.ellipse["semi_major_m"], (miss, solution.ellipse)


EPHEMERIS_ERROR = load_scenario(ROOT / "config" / "scenarios" / "ephemeris-error.yaml")


async def test_a_satellite_the_calibrations_doubt_gets_an_allowance_and_the_ellipse_covers_the_truth() -> None:
    """D1 (ephemeris-error, SIMULATED): muos-pac and uhf-172e are 8 and 6 km off in-track
    against the 1 km the solver budgets for. The clock checks show it from the first
    rounds (the satellites are named), yet the ellipse ignored it: the truth fell outside
    from 42 rows on (offline 160 to 190 m off a 255 to 285 m ellipse) and the advice only
    warned. Now the calibrations size an allowance for each doubted satellite, the next
    solves widen its position sigma by that scale (velocity untouched, other satellites
    untouched), the history says so, the truth stays inside, the event stays located from
    its first fix on (no flapping), and the advice says the ellipse is widened and by how
    much instead of 'may lie outside'.

    The first fix waits for the allowance (final pass): the first solve's rows went
    through uhf-172e, which that very round's clock checks named, yet it was published as
    a fix with the doubt unconfirmed and no allowance ('the ellipse does not include that
    error yet', the truth 415 m off a 449 m ellipse in a live run from 09:00 UTC). Now
    the clock checks run before the solve, and a solution through a doubted satellite
    with no allowance yet is not a fix until the doubt is confirmed and widened for."""
    from petal_locate.core.solver import position_covariance

    async with environment(CONFIG, EPHEMERIS_ERROR) as env:
        record = make_event(env)
        inside: list[bool] = []
        states: list[str] = []
        held: list[str] = []
        uncovered_at_first_fix: set[str] | None = None
        # where tasking believes the event is when it picks reference carriers: a solution held
        # back only by the doubt counts as much as a fix
        plan_references = env.tasking._plan_references
        nears: list[Any] = []

        def spy(*args: Any) -> Any:
            nears.append(args[5] if len(args) > 5 else None)
            return plan_references(*args)

        env.tasking._plan_references = spy  # type: ignore[method-assign]
        after_hold: list[tuple[Any, Any]] = []
        for _ in range(16):
            before = record.solution_out if record.solution_held else None
            await env.tasking.run_round(record.id)
            if before is not None:
                after_hold.append((nears[-1], (before.lat_deg, before.lon_deg)))
            env.clock.advance(5.0)
            env.book.flush(force=True)
            truth = event_truth(record, truth_emitters(env.world))
            if record.solution_out is not None:
                inside.append(bool(truth.inside_ellipse))
                states.append(record.state)
                if record.state != "located":
                    held.append(record.solution_out.not_fix_reason or "")
                elif uncovered_at_first_fix is None:
                    used = set(record.solution_used)
                    through = {s for e in record.measurements if e.seq in used
                               for s in (e.row.pair.a.sat_id, e.row.pair.b.sat_id)}
                    covered = {a.sat_id for a in record.solution_allowance}
                    uncovered_at_first_fix = (set(record.doubted_satellite_ids) & through) - covered
        assert len(record.used_measurements()) >= 46
        assert all(inside), inside  # every published ellipse holds the truth
        # held until the doubt is confirmed, then located from the first fix on (no flapping)
        first = states.index("located")
        assert first >= 1 and set(states[:first]) == {"locating"} and set(states[first:]) == {"located"}, states
        assert uncovered_at_first_fix == set(), uncovered_at_first_fix
        assert held and all(r.startswith("the reference calibrations have just put the position") and
                            r.endswith("and the ellipse does not include that error yet (it is widened once a later "
                                       "round confirms the doubt)") for r in held), held
        assert any((t.reason or "").startswith("the position is not a fix: the reference calibrations have just put")
                   for t in record.transitions)
        assert after_hold and all(near == position for near, position in after_hold), after_hold
        suspects = {s for e in env.tasking.site_clock_estimates() for s in e.suspect_satellite_ids}
        assert suspects == {"muos-pac", "uhf-172e"} == set(record.doubted_satellite_ids)
        allowance = {a.sat_id: a for a in record.solution_allowance}
        assert set(allowance) == {"muos-pac", "uhf-172e"} and record.state == "located"
        configured = CONFIG.resolved_uncertainty()
        for sat, entry in allowance.items():
            assert 4.0 <= entry.scale <= 12.0 and not entry.capped
            assert entry.sigma_ric_m == pytest.approx(tuple(configured.sat_pos_ric_m[sat] * entry.scale))
        assert allowance["muos-pac"].sigma_ric_m[1] >= 8000.0 and allowance["uhf-172e"].sigma_ric_m[1] >= 6000.0
        # the last solve: those two satellites' position sigmas widened, nothing else changed
        job = [args[0] for fn, args in env.pool.jobs if fn is tasking_module._solve][-1]
        assert job.uncertainty is not env.tasking._uncertainty
        for sat in ("muos-pac", "uhf-172e"):
            assert job.uncertainty.sat_pos_ric_m[sat] == pytest.approx(configured.sat_pos_ric_m[sat] * allowance[sat].scale)
            assert np.array_equal(job.uncertainty.sat_vel_ric_mps[sat], configured.sat_vel_ric_mps[sat])
        assert np.array_equal(job.uncertainty.sat_pos_ric_m["muos-io"], configured.sat_pos_ric_m["muos-io"])
        assert job.uncertainty.site_clock_s == configured.site_clock_s
        # how much the allowance widened the ellipse: against the configured uncertainty at the same position
        solution = record.solution
        plain = position_covariance(list(job.measurements), solution.lat_deg, solution.lon_deg, env.tasking._orbits,
                                    env.tasking._sites_xyz, 0.0, configured, 0.95)
        assert record.solution_widening == pytest.approx(solution.ellipse["semi_major_m"]
                                                         / plain["ellipse"]["semi_major_m"], rel=1e-9)
        assert 1.05 < record.solution_widening < 1.6
        # what the operator sees: the published solution and the event document carry both
        published = env.book.to_schema(record)
        assert [(a.sat_id, a.scale, tuple(a.sigma_ric_m), a.capped) for a in published.solution.ephemeris_allowance] == [
            (a.sat_id, a.scale, a.sigma_ric_m, a.capped) for a in record.solution_allowance]
        assert published.solution.ellipse_widening == record.solution_widening
        assert published.doubted_satellite_ids == ["muos-pac", "uhf-172e"]
        growth = f"{(record.solution_widening - 1.0) * 100.0:.0f} % longer than with the configured uncertainty"
        assert any((t.reason or "").startswith("ellipse widened: the reference calibrations put the position")
                   for t in record.transitions)
        message = record.tasking.message
        # the widened sigma as the advice writes it ('8.0 km', it read '8 km' here and '8.0 km' there)
        assert ("The ellipse is widened for the positions of muos-pac (8 times its configured sigma, about 8.0 km) "
                "and uhf-172e (9.5 times its configured sigma, about 9.5 km)") in message
        assert ("which the reference calibrations put wrong beyond the configured uncertainty: the 95 % ellipse's "
                f"major axis is {growth}.") in message
        record.verdict = "uplink"
        advisory = env.book.to_schema(record).advisory
        assert ("The ellipse is widened for the positions of muos-pac and uhf-172e, which the reference calibrations put "
                "wrong beyond their configured uncertainty (their position sigmas taken 8 and ") in advisory.detail
        assert f"check their ephemerides. Widened, the 95 % ellipse's major axis is {growth}." in advisory.detail
        assert "about 8.0 km and 9.5 km one sigma" in advisory.detail
        assert "may lie outside" not in " ".join([advisory.detail, *advisory.actions])


async def test_events_on_two_reference_carriers_through_the_same_channels_are_both_calibrated() -> None:
    """two-jammers: jammer A in shared-1 (calibrated by ref-guam) and jammer B in shared-2
    (ref-sapporo) are tasked through the same channel pairs, rounds alternating. When the
    calibration schedule and the calibrations held were keyed by pair alone, A's Guam
    attempts held B's calibration back and every B row stayed uncalibrated (0 of 16);
    keyed by pair only for what is held, B's Sapporo calibration replaced A's and half
    of A's rows lost theirs (SIMULATED)."""
    two = load_scenario(ROOT / "config" / "scenarios" / "two-jammers.yaml")
    async with environment(CONFIG, two) as env:
        a = make_event(env, 301.000e6, 301.010e6, "shared-1")
        b = make_event(env, 301.126e6, 301.134e6, "shared-2")
        for _ in range(4):
            ra = await env.tasking.run_round(a.id)
            rb = await env.tasking.run_round(b.id)
            assert ra.partner_channel_ids == rb.partner_channel_ids  # the same channel pairs
            env.clock.advance(5.0)
        env.book.flush(force=True)
        for record, carrier in ((a, "ref-guam"), (b, "ref-sapporo")):
            rows = env.book.to_schema(record).measurements
            assert rows and all(r.calibrated and r.reference_carrier_id == carrier for r in rows), (
                carrier, [(r.calibrated, r.reference_carrier_id) for r in rows])


def test_classification_band_keeps_the_event_and_drops_its_neighbours() -> None:
    """Against a closed form: two tones on exact bins of a 20 kHz capture, one 300 Hz
    from the event centre and one 8.6 kHz away. The kept band is centred on the event
    (offset 1 kHz from the capture centre); the near tone comes out as the same complex
    exponential at the reduced rate, the far one is gone."""
    fs, n = 20e3, 20000
    t = np.arange(n) / fs
    near, far, offset = 1300.0, -7600.0, 1000.0  # Hz from the capture centre
    iq = 2.0 * np.exp(2j * np.pi * near * t + 0.3j) + 5.0 * np.exp(2j * np.pi * far * t)
    out, rate = tasking_module._classification_band(iq, fs, offset, 100.0)
    # 4 x 100 Hz is below the 2 x 1024-sample floor, which sets the kept band
    assert rate == pytest.approx(2.0 * 1024 * fs / n, abs=fs / n) and out.size == round(n * rate / fs)
    expected = 2.0 * np.exp(2j * np.pi * (near - offset) * np.arange(out.size) / rate + 0.3j)
    assert np.max(np.abs(out - expected)) < 1e-9
    # wide events and offsets of zero width keep the capture as it is
    same, same_rate = tasking_module._classification_band(iq, fs, 0.0, 10e3)
    assert same_rate == fs and same is iq


def test_round_explanation_names_a_cause_every_pair_shares() -> None:
    from types import SimpleNamespace

    def outcome(reason: str | None, detected: bool = True) -> PairOutcome:
        p = PairOutcome("a", "b", "rejected")
        p.result = SimpleNamespace(reason=reason, detected=detected)  # type: ignore[assignment]
        return p

    explain = tasking_module.round_explanation
    assert explain([outcome("narrowband"), outcome("narrowband")]) == tasking_module.EXPLAIN_NARROWBAND
    assert explain([outcome("ambiguous")]) == tasking_module.EXPLAIN_AMBIGUOUS
    assert explain([outcome("delay-doppler coupled")]) == tasking_module.EXPLAIN_COUPLED
    assert explain([outcome("below threshold", detected=False)]) == tasking_module.EXPLAIN_NOT_DETECTED
    assert explain([outcome(None), outcome(None)]) is None  # complete results need no excuse
    assert explain([outcome("narrowband"), outcome("ambiguous")]) is None  # no single cause
    assert explain([PairOutcome("a", "b", "failed")]) is None  # nothing was correlated


async def test_event_judged_local_gets_an_idle_status_and_no_further_round() -> None:
    async with environment(NO_CAL, None, fail_all="offline") as env:
        record = make_event(env, config=NO_CAL)
        await env.tasking.run_round(record.id)
        env.book.flush(force=True)
        assert env.book.to_schema(record).tasking.state == "waiting"
        record.verdict = "local"  # what the correlator decides
        env.clock.advance(5.0)
        calls = len(env.hub.calls)
        env.tasking._schedule()
        env.book.flush(force=True)
        status = env.book.to_schema(record).tasking
        assert status.state == "idle" and status.next_round_ns is None
        assert "local" in status.message and "nothing to locate" in status.message
        assert env.tasking.rounds_in_flight() == 0 and len(env.hub.calls) == calls


async def test_unlocked_clock_drops_tdoa_between_sites_but_keeps_fdoa() -> None:
    async with environment(NO_CAL, DEMO, unlocked={"naha-172e", "tokyo-io"}) as env:
        record = make_event(env, config=NO_CAL)
        await rounds(env, record, 2)
        rows = env.book.to_schema(record).measurements
        by_partner: dict[str, list[schemas.MeasurementRow]] = {}
        for row in rows:
            by_partner.setdefault(row.pair.b.channel_id, []).append(row)
        far = by_partner["naha-172e"]  # another site: no TDOA from an unlocked capture
        assert all(r.reason == REASON_CLOCK_UNLOCKED and not r.used for r in far if r.kind == "tdoa")
        assert [r.used for r in far if r.kind == "fdoa"] == [False, True]
        near = by_partner["tokyo-io"]  # same site as the primary: the clock error cancels
        assert [r.used for r in near if r.kind == "tdoa"] == [False, True]
        assert all(r.value is not None for r in far if r.kind == "tdoa")  # measured, shown, not used


async def test_rate_mismatch_rejects_the_pair_with_its_reason() -> None:
    async with environment(NO_CAL, DEMO, rate_factor={"naha-172e": 1.001}) as env:
        record = make_event(env, config=NO_CAL)
        report = (await rounds(env, record, 1))[0]
        outcome = {p.b_channel_id: p for p in report.pairs}
        assert outcome["naha-172e"].status == "rejected" and outcome["naha-172e"].reason == REASON_RATE_MISMATCH
        assert outcome["naha-172e"].result is None
        assert outcome["tokyo-io"].reason == REASON_UNCONFIRMED  # the other pair is unaffected
        rows = [r for r in env.book.to_schema(record).measurements if r.pair.b.channel_id == "naha-172e"]
        assert sorted(r.kind for r in rows) == ["fdoa", "tdoa"]
        assert all(r.reason == REASON_RATE_MISMATCH and r.value is None and not r.used for r in rows)


# ----------------------------------------------------------------------------- failures


async def test_retryable_capture_failure_is_retried_and_non_retryable_excludes_the_channel() -> None:
    async with environment(
        NO_CAL, DEMO, fail={"naha-172e": ["disconnected", None, None], "tokyo-io": ["out_of_range"]}
    ) as env:
        record = make_event(env, config=NO_CAL)
        first = (await rounds(env, record, 1))[0]
        message = env.book.to_schema(record).tasking.message
        assert "naha-172e (disconnected); retrying at the next round" in message
        assert "tokyo-io (out_of_range); that channel is no longer used" in message
        second = (await rounds(env, record, 1))[0]
        assert first.failures["naha-172e"][0] == "disconnected"
        assert first.failures["tokyo-io"][0] == "out_of_range"
        outcome = {p.b_channel_id: p for p in first.pairs}
        assert outcome["naha-172e"].status == "failed" and outcome["naha-172e"].tdoa_seq is None
        assert outcome["tokyo-io"].reason.startswith(f"{REASON_CAPTURE_FAILED}: out_of_range")
        rows = env.book.to_schema(record).measurements
        assert not [r for r in rows if r.pair.b.channel_id == "naha-172e" and r.t_ns < T0 + 5 * S]
        failed_rows = [r for r in rows if r.pair.b.channel_id == "tokyo-io"]
        assert len(failed_rows) == 2 and all(r.reason.startswith(REASON_CAPTURE_FAILED) for r in failed_rows)
        # next round: the retryable channel is captured again, the other is replaced
        assert "naha-172e" in second.partner_channel_ids and "tokyo-io" not in second.partner_channel_ids
        replacement = [c for c in second.partner_channel_ids if CONFIG.channel(c)[1].watched_satellite_ids[0] == "muos-io"]
        assert len(replacement) == 1
        assert {p.b_channel_id: p.reason for p in second.pairs}["naha-172e"] == REASON_UNCONFIRMED
        assert second.failures == {}


async def test_closing_the_event_cancels_its_round() -> None:
    block = asyncio.Event()
    async with environment(NO_CAL, DEMO, block=block) as env:
        record = make_event(env, config=NO_CAL)
        task = asyncio.create_task(env.tasking.run_round(record.id))
        await asyncio.wait_for(env.hub.started.wait(), 10.0)
        await asyncio.sleep(0.01)
        assert env.hub.pending == 3 and env.tasking.rounds_in_flight() == 1
        assert env.tasking.captures_in_flight == 3
        env.book.close(record.id)
        report = await asyncio.wait_for(task, 10.0)
        assert report.cancelled
        assert env.hub.cancelled == 3 and env.hub.pending == 0
        assert record.measurements == [] and env.tasking.rounds_in_flight() == 0
        assert env.tasking.captures_in_flight == 0
        assert env.tasking.counters["cancelled_on_close"] == 1
        assert env.book.to_schema(record).state == "closed"
        assert env.tasking.cancel(record.id) is False  # nothing left to cancel


async def test_reconfigure_cancels_the_round_in_flight() -> None:
    block = asyncio.Event()
    async with environment(NO_CAL, DEMO, block=block) as env:
        record = make_event(env, config=NO_CAL)
        task = asyncio.create_task(env.tasking.run_round(record.id))
        await asyncio.wait_for(env.hub.started.wait(), 10.0)
        env.tasking.reconfigure(CONFIG)
        report = await asyncio.wait_for(task, 10.0)
        assert report.cancelled and env.hub.cancelled >= 1
        assert env.tasking.config is CONFIG and env.tasking.counters["reconfigured"] == 1


async def test_scheduling_loop_runs_rounds_per_interval_and_stops_cleanly() -> None:
    async with environment(NO_CAL, None, fail_all="offline") as env:
        record = make_event(env, config=NO_CAL)
        env.tasking.start()
        with pytest.raises(RuntimeError):
            env.tasking.start()

        async def rounds_reach(count: int) -> None:
            while env.tasking.counters.get("rounds", 0) < count:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(rounds_reach(1), 20.0)
        await asyncio.sleep(0.2)  # the interval has not passed on the server clock
        assert env.tasking.counters["rounds"] == 1
        doc = env.book.to_schema(record)
        assert doc.state == "characterizing" and doc.tasking is not None
        assert "retrying at the next round" in (doc.tasking.message or "")
        env.clock.advance(5.0)
        await asyncio.wait_for(rounds_reach(2), 20.0)
        assert env.tasking.counters["capture_failed_offline"] == 6
        await env.tasking.stop()
        assert not env.tasking.running and env.tasking.rounds_in_flight() == 0
        with pytest.raises(RuntimeError):
            await env.tasking.run_round(record.id)


# ----------------------------------------------------------------------------- planning


async def test_lead_time_follows_the_worst_round_trip_with_a_cap() -> None:
    for rtt, lead in ((None, 0.6), (300.0, 0.9), (30_000.0, MAX_LEAD_S)):
        async with environment(NO_CAL, None, fail_all="offline", rtt=lambda site, r=rtt: r) as env:
            record = make_event(env, config=NO_CAL)
            report = await env.tasking.run_round(record.id)
            assert report.t_start_ns == T0 + int(round(lead * S))
            assert {c[1] for c in env.hub.calls} == {report.t_start_ns}
            n, fs = report.n_samples, report.sample_rate_hz
            expected_deadline = report.t_start_ns + int(math.ceil(n / fs * S)) + int(15.0 * S)
            assert {c[5] for c in env.hub.calls} == {expected_deadline}


async def test_partner_choice_prefers_the_primary_site_within_3_db() -> None:
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)
        state = tasking_module._EventState(record.id)

        def table(tokyo_q: float) -> QualityTable:
            values = {(ch, "muos-io", "shared-1"): 0.0 for ch in ("naha-io", "busan-io", "manila-io")}
            values[("seoul-io", "muos-io", "shared-1")] = 10.0
            values[("tokyo-io", "muos-io", "shared-1")] = tokyo_q
            values[("seoul-172e", "uhf-172e", "shared-1")] = 5.0
            return QualityTable(0.0, values)

        for tokyo_q, expected in ((8.0, "tokyo-io"), (6.5, "seoul-io")):
            primary, partners, _ = env.tasking._choose_channels(record, state, table(tokyo_q), 301.005e6)
            assert primary is not None and primary.channel_id == "tokyo-pac"
            chosen = {p.sat_id: p.channel_id for p in partners}
            assert chosen == {"muos-io": expected, "uhf-172e": "seoul-172e"}
        # a channel that cannot tune the frequency, or was excluded, is passed over
        state.excluded["seoul-io"] = "out_of_range: test"
        _, partners, notes = env.tasking._choose_channels(record, state, table(6.5), 301.005e6)
        assert {p.sat_id: p.channel_id for p in partners}["muos-io"] == "tokyo-io"
        assert any("seoul-io excluded" in n for n in notes)
        env.hub.max_hz = 100e6
        primary, partners, notes = env.tasking._choose_channels(record, state, table(6.5), 301.005e6)
        assert primary is None and partners == [] and any("cannot tune" in n for n in notes)


async def test_partners_within_1_db_take_turns_in_odd_rounds_from_the_third() -> None:
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)
        state = tasking_module._EventState(record.id)
        values = {(ch, "muos-io", "shared-1"): 0.0 for ch in ("naha-io", "busan-io", "tokyo-io")}
        values[("seoul-io", "muos-io", "shared-1")] = 10.0
        values[("manila-io", "muos-io", "shared-1")] = 9.5  # within 1 dB of the best
        values[("seoul-172e", "uhf-172e", "shared-1")] = 5.0
        table = QualityTable(0.0, values)
        chosen = []
        for k in range(8):
            state.rounds = k
            _, partners, _ = env.tasking._choose_channels(record, state, table, 301.005e6)
            chosen.append({p.sat_id: p.channel_id for p in partners}["muos-io"])
        assert chosen == ["seoul-io"] * 3 + ["manila-io", "seoul-io", "manila-io", "seoul-io", "manila-io"]


async def test_the_previous_primary_is_held_while_within_3_db_of_the_strongest() -> None:
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)  # tokyo-pac is the strongest sighting
        state = tasking_module._EventState(record.id)
        table = QualityTable(0.0, {})
        primary, _, _ = env.tasking._choose_channels(record, state, table, 301.005e6)
        assert primary is not None and primary.channel_id == "tokyo-pac"
        units = {u.channel_id: u for u in record.members.values()}
        weaker = next(c for c in units if c != "tokyo-pac")
        best = units["tokyo-pac"].snr_db
        units[weaker].snr_db = best + 2.0  # another channel now reads 2 dB stronger
        primary, _, _ = env.tasking._choose_channels(record, state, table, 301.005e6)
        assert primary is not None and primary.channel_id == "tokyo-pac"
        units[weaker].snr_db = best + 4.0  # beyond the hold
        primary, _, _ = env.tasking._choose_channels(record, state, table, 301.005e6)
        assert primary is not None and primary.channel_id == weaker


async def test_event_relayed_by_one_satellite_is_classified_then_left_idle() -> None:
    async with environment(NO_CAL, None, fail_all="offline") as env:
        record = make_event(env, 301.495e6, 301.505e6, "solo-pac", (("tokyo-pac", 15.0),), NO_CAL)
        first = await env.tasking.run_round(record.id)
        assert first.skipped is None and first.partner_channel_ids == []  # a capture for classification only
        assert [c[0] for c in env.hub.calls] == ["tokyo-pac"]
        env.book.set_classification(record.id, schemas.ClassificationOut(label="noise", confidence=0.9, features={}))
        env.clock.advance(5.0)
        second = await env.tasking.run_round(record.id)
        assert second.skipped is not None and "no second satellite" in second.skipped
        env.book.flush(force=True)
        assert env.book.to_schema(record).tasking.state == "idle"


async def test_swept_waveform_is_captured_again_over_five_sweep_periods() -> None:
    async with environment(NO_CAL, None, fail_all="offline") as env:
        record = make_event(env, config=NO_CAL)
        state = tasking_module._EventState(record.id)
        capture = AssembledCapture(
            "c1", "tokyo-pac", np.zeros(20000, np.complex64), T0, 20000.0, 260.005e6, False,
            ClockStatus(source="sim", locked=True, est_offset_ns=0.0, holdover_s=0.0), 1,
        )
        out = tasking_module._CaptureOutcome("tokyo-pac", capture)
        report = RoundReport(record.id, T0)

        def swept(rate: float) -> Classification:
            return Classification("swept", 0.8, {"sweep_rate_hz_per_s": rate, "occupied_bw_hz": 10e3})

        env.tasking._apply_classification(record.id, state, swept(5e5), out, report)  # 20 ms: 1 s holds 50
        assert state.duration_s is None
        env.tasking._apply_classification(record.id, state, swept(1e4), out, report)  # 1 s sweeps
        assert state.duration_s == pytest.approx(5.25) and state.next_due_ns == T0
        env.tasking._apply_classification(record.id, state, swept(500.0), out, report)  # 20 s: capped
        assert state.duration_s == pytest.approx(262144 / 20000.0)
        assert any("capture.max_samples" in n for n in report.notes)
        # the next round asks for that length
        env.tasking._states[record.id] = state
        state.duration_s = 5.25
        await env.tasking.run_round(record.id)
        assert {c[2] for c in env.hub.calls} == {round(5.25 * 20000.0)}


# ----------------------------------------------------------------------------- confirmation


def test_repeat_detections_are_consistent_within_sigmas_plus_drift() -> None:
    det = tasking_module._Detection
    rates = (5e-6, 0.1)  # s/s and Hz/s: GEO-like bounds
    first = det(T0, 2.0e-3, 1e-7, 27.0, 1e-3)
    assert tasking_module._consistent(first, det(T0, 2.0e-3 + 4e-7, 1e-7, 27.003, 1e-3), rates)
    later = T0 + 5 * S  # 5 s later the satellites may have moved the TDOA by up to 25 us
    assert tasking_module._consistent(first, det(later, 2.0e-3 + 20e-6, 1e-7, 27.4, 1e-3), rates)
    assert not tasking_module._consistent(first, det(later, 2.0e-3 + 40e-6, 1e-7, 27.0, 1e-3), rates)
    assert not tasking_module._consistent(first, det(later, 2.0e-3, 1e-7, 28.0, 1e-3), rates)
    assert not tasking_module._consistent(first, det(later, None, None, None, None), rates)
    fdoa_only = det(later, None, None, 27.2, 1e-3)
    assert tasking_module._consistent(first, fdoa_only, rates)


async def test_confirmation_needs_a_repeat_and_recovers_from_a_false_alarm() -> None:
    async with environment(NO_CAL, None) as env:
        state = tasking_module._EventState("EV-x")
        det = tasking_module._Detection
        rates = (5e-6, 0.1)
        key = ("a", "b")
        confirm = env.tasking._confirm
        assert confirm(state, key, det(T0, 1e-3, 1e-7, 5.0, 1e-3), rates) == (False, REASON_UNCONFIRMED)
        assert confirm(state, key, det(T0 + S, 1e-3, 1e-7, 5.0, 1e-3), rates) == (True, None)
        # a false alarm far away is refused against the confirmed track ...
        assert confirm(state, key, det(T0 + 2 * S, 9e-3, 1e-7, -80.0, 1e-3), rates) == (False, REASON_INCONSISTENT)
        # ... and the track goes on
        assert confirm(state, key, det(T0 + 3 * S, 1e-3, 1e-7, 5.0, 1e-3), rates) == (True, None)
        # a genuinely new track is accepted once it repeats
        assert confirm(state, key, det(T0 + 400 * S, 4e-3, 1e-7, 40.0, 1e-3), rates)[0] is False
        assert confirm(state, key, det(T0 + 401 * S, 4e-3, 1e-7, 40.0, 1e-3), rates) == (True, None)


# ----------------------------------------------------------------------------- solve schedule


def fake_solution(lat: float, rchi2: float | None, m: int) -> Solution:
    cov = np.diag([1e6, 4e6])
    return Solution(
        lat_deg=lat, lon_deg=132.0, height_m=0.0, cov_en_m2=cov, cov_noise_en_m2=cov * 0.5,
        cov_consider_en_m2=cov * 0.5,
        ellipse={"semi_major_m": 4900.0, "semi_minor_m": 2450.0, "azimuth_deg": 0.0, "confidence": 0.95},
        cep50_m=1800.0, chi2=(rchi2 or 0.0) * (m - 2), dof=m - 2, reduced_chi2=rchi2, residuals=np.zeros(m),
        ambiguities=[], ambiguity_resolved_by="none", converged=True, dominant_error="noise", n_measurements=m,
    )


class SolvePool:
    """Answers solve jobs from a script and records their starts."""

    def __init__(self, answers: list[tuple[float | None, float | None]]) -> None:
        self.answers = answers
        self.starts: list[Any] = []

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        assert fn is tasking_module._solve
        job = args[0]
        self.starts.append(job.starts)
        lat, rchi2 = self.answers.pop(0)
        if lat is None:
            return None, "not converged"
        return fake_solution(lat, rchi2, len(job.measurements)), None


async def test_solve_schedule_global_first_then_warm_with_global_retries() -> None:
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)
        a = PathSpec("muos-pac", "tokyo", 301.005e6, -41e6)
        b = PathSpec("uhf-172e", "naha", 301.005e6, -33.6e6)
        pair = schemas.PairRef(
            a=schemas.ChannelRef(sat_id="muos-pac", site_id="tokyo", channel_id="tokyo-pac"),
            b=schemas.ChannelRef(sat_id="uhf-172e", site_id="naha", channel_id="naha-172e"),
        )
        for kind, value, sigma in (("tdoa", 2.6e-3, 1e-7), ("fdoa", 27.0, 1e-3)):
            row = schemas.MeasurementRow(
                t_ns=T0, kind=kind, pair=pair, value=value, sigma=sigma, residual=None, snr_db=39.0,
                calibrated=False, used=True, reason=None,
            )
            env.book.add_measurement(record.id, row, Measurement(kind, value, sigma, T0, a, b))
        pool = SolvePool(
            [(20.0, 1.0), (20.1, 5.0), (20.2, 1.0)] + [(20.2, 1.0)] * 7 + [(20.3, 1.0), (None, None), (20.4, 1.0)]
        )
        env.tasking._pool = pool  # type: ignore[assignment]
        state = tasking_module._EventState(record.id)
        for _ in range(11):
            await env.tasking._solve_event(record.id, state, RoundReport(record.id, T0), env.tasking._generation)
        expected_warm = ((20.0, 132.0),)
        assert pool.starts[0] is None  # first solve: global search
        assert pool.starts[1] == expected_warm and pool.starts[2] is None  # reduced chi2 5 > 3: global again
        assert all(s == ((20.2, 132.0),) for s in pool.starts[3:10])
        assert pool.starts[10] is None  # the 10th solve is a global search
        assert pool.starts[11] == ((20.3, 132.0),) and pool.starts[12] is None  # a failed warm start: global
        assert record.solution.lat_deg == 20.4 and state.solves == 11
        assert len(record.solution_history) == 11


async def test_solve_with_too_few_rows_reports_why() -> None:
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)
        state = tasking_module._EventState(record.id)
        report = RoundReport(record.id, T0)
        await env.tasking._solve_event(record.id, state, report, env.tasking._generation)
        assert report.solve_reason == "fewer than two accepted measurements" and state.solves == 0


# ----------------------------------------------------------------------------- runtime wiring


async def test_runtime_runs_and_stops_tasking(tmp_path: Path) -> None:
    config_file = tmp_path / "pacific.yaml"
    config_file.write_text(dump_config(CONFIG), encoding="utf-8")
    settings = Settings(config_path=config_file, data_dir=tmp_path / "data")
    store = Store(tmp_path / "data")
    await store.open()
    runtime = ServerRuntime(settings, CONFIG, None, store)
    providers = runtime.hub_providers()
    hub = Hub(settings, providers.assignments_for, providers.config_version, providers.simulation_info)
    hub.start()
    await runtime.start(hub)
    try:
        tasking = runtime.tasking
        assert tasking is not None and tasking.running
        await runtime.replace_config(NO_CAL, runtime.config_version)
        assert tasking.counters["reconfigured"] == 1 and tasking.config.processing.calibration.enabled is False
    finally:
        await runtime.stop()
        await hub.close()
    assert not tasking.running
    await asyncio.sleep(0)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
    assert store_threads() == []


# ----------------------------------------------------------------------------- another transmitter (K2, UX-5)


def moved_jammer(lat_deg: float, lon_deg: float) -> Scenario:
    """The demo scenario with its jammer at another position, same frequency and waveform:
    another transmitter taking the frequency without a break."""
    data = DEMO.model_dump(mode="json")
    data["emitters"][0].update(lat_deg=lat_deg, lon_deg=lon_deg)
    return Scenario.model_validate(data)


MOVED = moved_jammer(17.0, 141.0)


async def test_another_transmitter_on_the_same_frequency_sets_the_earlier_rows_aside() -> None:
    """K2: the jammer is replaced by another one 1,000 km away on the same frequency, with
    no break. Its detections disagree with the pairs' tracks; once they repeat (a pair's
    track jumps, or a new pair's rows break a fit the earlier rows passed), the old
    position and rows are set aside and the event is located from the new rows only. At
    no point is a position published from the two transmitters' rows mixed."""
    async with environment(NO_CAL) as env:
        record = make_event(env, config=NO_CAL)
        await rounds(env, record, 3)
        assert record.state == "located" and record.solution is not None
        before = {e.seq for e in record.used_measurements()}
        assert len(before) == 8
        assert geodesic_distance_m(record.solution.lat_deg, record.solution.lon_deg, 20.0, 132.0) < 5e3

        env.hub.world = world_for(MOVED, NO_CAL)
        swap_ns = env.clock()
        published: list[schemas.SolutionOut] = []
        for _ in range(4):
            await rounds(env, record, 1)
            if record.solution_out is not None:
                published.append(record.solution_out)
        assert env.tasking.counters["transmission_changes"] == 1
        rows = {e.seq: e.row for e in record.measurements}
        assert all(not rows[s].used and rows[s].reason.startswith("set aside: ") for s in before)
        used = record.used_measurements()
        assert used and all(e.row.t_ns >= swap_ns for e in used)
        assert record.onset_ns is not None and record.onset_ns >= swap_ns
        reasons = [t.reason or "" for t in record.transitions]
        assert any(r.startswith("another transmission: ") for r in reasons)
        solution = record.solution_out
        assert solution is not None and solution.fit_consistent and record.state == "located"
        assert geodesic_distance_m(solution.lat_deg, solution.lon_deg, 17.0, 141.0) < 10e3
        # every consistent position published after the swap was one transmitter's: the
        # earlier one's (until the change was found) or the new one's, never a mix
        for out in published:
            if out.fit_consistent:
                assert min(
                    geodesic_distance_m(out.lat_deg, out.lon_deg, 20.0, 132.0),
                    geodesic_distance_m(out.lat_deg, out.lon_deg, 17.0, 141.0),
                ) < 10e3
        record.verdict = "uplink"
        doc = env.book.to_schema(record)
        assert doc.onset_ns == record.onset_ns
        assert doc.solution_history[0].set_aside and not doc.solution_history[-1].set_aside
        assert any(a.startswith("Report the estimated transmitter position (") for a in doc.advisory.actions)


QUIET = load_scenario(ROOT / "config" / "scenarios" / "quiet.yaml")


async def test_a_waveform_change_with_nothing_measured_before_it_is_another_transmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final recheck: a swept jammer that gave no usable correlation was replaced, with no
    break, by a noise jammer elsewhere. The noise jammer's pairs were confirmed and
    located while the class still said swept (it is measured once a minute), and the
    swept-to-noise re-check then 'passed' by comparing the new jammer with its own
    tracks; the onset stayed the swept jammer's start. Now the class is measured again as
    soon as every measured track began after it, and a re-check that only tracks begun
    after the change agree with starts another transmission: the onset moves to the first
    detection of those tracks, their rows and the new class stay, and it is located."""
    current = {"label": "swept"}

    def classify(*_args: Any) -> Classification:
        return Classification(current["label"], 0.9, {})

    monkeypatch.setattr(tasking_module, "_classify_event", classify)
    async with environment(NO_CAL, QUIET) as env:
        record = make_event(env, config=NO_CAL)
        await rounds(env, record, 2)  # nothing correlates: no track, the class says swept
        assert record.classification is not None and record.classification.label == "swept"
        assert not record.used_measurements() and record.onset == T0

        env.hub.world = world_for(DEMO, NO_CAL)
        swap_ns = env.clock()
        current["label"] = "noise"
        await rounds(env, record, 5)
        reasons = [t.reason or "" for t in record.transitions]
        assert not any(r.startswith("re-check passed") for r in reasons), reasons
        assert any(
            r.startswith("another transmission: its waveform changed from swept to noise, and every pair measured now "
                         "was first detected after that") for r in reasons
        ), reasons
        assert env.tasking.counters["transmission_changes"] == 1
        assert record.onset_ns is not None and swap_ns <= record.onset_ns < env.clock()
        assert record.classification is not None and record.classification.label == "noise"
        used = record.used_measurements()
        assert used and all(e.row.t_ns >= record.onset_ns for e in used)
        assert record.state == "located" and record.solution is not None
        assert geodesic_distance_m(record.solution.lat_deg, record.solution.lon_deg, 20.0, 132.0) < 5e3


async def test_a_recheck_waits_for_a_pair_measured_before_the_change_but_not_for_ever() -> None:
    """While a pair measured before the change is still held but not measured again, the
    agreement of pairs first detected since does not decide the re-check; after
    RECHECK_WAIT_ROUNDS rounds it is cleared, saying the change could not be tested (and
    nothing is set aside)."""
    async with environment(NO_CAL) as env:
        record = make_event(env, config=NO_CAL)
        await rounds(env, record, 3)
        assert record.state == "located"
        state = env.tasking._states[record.id]
        # a pair measured long before, never captured again (as after a change of primary)
        state.tracks[("gone-a", "gone-b")] = tasking_module._PairTrack(
            confirmed=tasking_module._Detection(T0, 0.0, 1e-7, 0.0, 1e-3), since_ns=T0 - 10
        )
        assert env.tasking._mark_recheck(record.id, state, "its level at tokyo-pac rose by 7 dB between rounds", T0 - 1)
        # every other round rotates in new partners (nothing to agree with): two rounds per wait
        reports = await rounds(env, record, 2 * tasking_module.RECHECK_WAIT_ROUNDS + 1)
        reasons = [t.reason or "" for t in record.transitions]
        assert any(r.startswith("re-check passed: pair ") and r.endswith(
            f"no pair measured before the change was measured again in {tasking_module.RECHECK_WAIT_ROUNDS} rounds, "
            "so the change could not be tested") for r in reasons), reasons
        assert record.recheck is None and env.tasking.counters.get("transmission_changes", 0) == 0
        assert record.state == "located" and reports


async def test_an_event_back_after_a_short_break_is_rechecked_before_its_position_is_kept() -> None:
    """UX-5: lost for 13 s, then detected again: the position is kept but re-checked. The
    same transmitter's next detections agree, so it stays; another transmitter's first
    disagreeing detection sets everything aside (no second detection needed)."""
    for scenario, expect_same in ((DEMO, True), (MOVED, False)):
        async with environment(NO_CAL) as env:
            record = make_event(env, config=NO_CAL)
            await rounds(env, record, 3)
            assert record.state == "located"
            members = dict(record.members)
            record.members.clear()
            env.book.mark_lost(record)
            env.clock.advance(13.0)
            record.members.update(members)
            env.book.reopen(record)
            assert record.recheck is not None and record.recheck.startswith("Detected again after a break of 13 s")
            assert record.break_s == pytest.approx(13.0) and record.resumed_ns == env.clock()
            assert record.solution_out is not None and record.state == "located"
            assert "checked against new measurements" in (record.transitions[-1].reason or "")
            record.verdict = "uplink"
            advice = env.book.to_schema(record).advisory
            assert any(a.startswith("Report the estimated transmitter position") and "once new measurements confirm it" in a
                       for a in advice.actions)
            env.hub.world = world_for(scenario, NO_CAL)
            reports = await rounds(env, record, 2)  # the first may use rotated partners without a track yet
            reasons = [t.reason or "" for t in record.transitions]
            if expect_same:
                assert record.recheck is None and any(r.startswith("re-check passed") for r in reasons)
                assert record.solution_out is not None and record.state == "located"
                assert env.tasking.counters.get("transmission_changes", 0) == 0
            else:
                assert env.tasking.counters["transmission_changes"] == 1
                assert record.recheck is None
                assert any("while the event was being re-checked" in r for r in reasons)
                assert all(e.row.t_ns >= reports[0].t_start_ns for e in record.used_measurements())


# ----------------------------------------------------------------------------- fit consistency (UX-1)

_PAIRS = (
    (("muos-pac", "tokyo", "tokyo-pac", -41e6), ("uhf-172e", "naha", "naha-172e", -33.6e6)),
    (("muos-pac", "tokyo", "tokyo-pac", -41e6), ("muos-io", "tokyo", "tokyo-io", -41e6)),
)


def predicted_rows(lat_deg: float, lon_deg: float, t_ns: int) -> list[tuple[schemas.PairRef, Measurement]]:
    """Noise-free TDOA and FDOA rows of an emitter at (lat, lon) for two pairs, as the
    solver would receive them (sigmas 100 ns and 1 mHz)."""
    from petal_locate.core.geodesy import lla_to_ecef

    x = np.asarray(lla_to_ecef(lat_deg, lon_deg, 0.0), dtype=np.float64)
    out = []
    for (sa, sia, ca, ta), (sb, sib, cb, tb) in _PAIRS:
        a, b = PathSpec(sa, sia, 301.005e6, ta), PathSpec(sb, sib, 301.005e6, tb)
        tdoa, fdoa = predict_pair(x, a, b, t_ns / S, CONFIG.orbits(), CONFIG.sites_xyz())
        pair = schemas.PairRef(
            a=schemas.ChannelRef(sat_id=sa, site_id=sia, channel_id=ca),
            b=schemas.ChannelRef(sat_id=sb, site_id=sib, channel_id=cb),
        )
        for kind, value, sigma in (("tdoa", float(np.ravel(tdoa)[0]), 1e-7), ("fdoa", float(np.ravel(fdoa)[0]), 1e-3)):
            out.append((pair, Measurement(kind, value, sigma, t_ns, a, b)))  # type: ignore[arg-type]
    return out


def add_rows(env: Env, record: EventRecord, positions: Iterable[tuple[float, float]], t0_ns: int) -> list[int]:
    seqs = []
    for k, (lat, lon) in enumerate(positions):
        for pair, m in predicted_rows(lat, lon, t0_ns + k * 30 * S):
            row = schemas.MeasurementRow(
                t_ns=m.t_ns, kind=m.kind, pair=pair, value=m.value, sigma=m.sigma, residual=None, snr_db=30.0,
                calibrated=False, used=True, reason=None,
            )
            seq = env.book.add_measurement(record.id, row, m)
            assert seq is not None
            seqs.append(seq)
    return seqs


A_POS, B_POS = (20.0, 132.0), (17.0, 141.0)


async def test_an_inconsistent_fit_is_flagged_never_located_and_never_reported_as_a_fix() -> None:
    """UX-1: rows of two transmitters in one event. The fit cannot be consistent; it is
    published flagged, with its ellipse enlarged by sqrt(reduced chi-square), the event
    goes back to locating, and the advice reports no position."""
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)
        record.verdict = "uplink"
        state = tasking_module._EventState(record.id)
        add_rows(env, record, [A_POS] * 3, T0)
        await env.tasking._solve_event(record.id, state, RoundReport(record.id, T0), env.tasking._generation)
        assert record.state == "located" and record.solution_out.fit_consistent
        assert record.solution_out.ellipse_scale == 1.0 and record.solution_out.fit_p_value > 0.5
        # the next rows interleave both transmitters: they do not fit one even on their own
        env.clock.advance(120.0)
        add_rows(env, record, [B_POS, A_POS, B_POS], T0 + 120 * S)
        report = RoundReport(record.id, T0)
        await env.tasking._solve_event(record.id, state, report, env.tasking._generation)
        out = record.solution_out
        assert out is not None and not out.fit_consistent
        assert out.reduced_chi2 > NO_CAL.processing.solver.max_reduced_chi2 and out.fit_p_value < 1e-3
        assert out.ellipse_scale == pytest.approx(math.sqrt(out.reduced_chi2))
        raw = record.solution.ellipse["semi_major_m"]
        assert out.ellipse.semi_major_m == pytest.approx(raw * out.ellipse_scale)
        assert out.cov_en_m2[0][0] == pytest.approx(float(record.solution.cov_en_m2[0][0]) * out.reduced_chi2)
        assert record.state == "locating"
        assert (record.transitions[-1].reason or "").startswith("position withdrawn: measurements inconsistent")
        assert env.tasking.counters.get("transmission_changes", 0) == 0  # the newest rows alone are mixed too
        assert state.fit_note is not None and "do not fit one stationary transmitter" in state.fit_note
        env.book.flush(force=True)
        doc = env.book.to_schema(record)
        assert doc.state == "locating" and doc.solution_history[-1].fit_consistent is False
        assert env.book.summary(record).solution.fit_consistent is False
        actions = doc.advisory.actions
        assert not any(a.startswith("Report the estimated transmitter position") for a in actions)
        assert any("do not report the computed position" in a for a in actions)
        assert "do not fit a single stationary transmitter" in doc.advisory.detail
        assert "is not a reliable position" in doc.advisory.detail


async def test_the_newest_rows_of_another_transmitter_set_the_earlier_ones_aside() -> None:
    """K2 backstop: the rows added since a consistent fit fit a transmitter of their own
    but not together with the earlier ones: the earlier rows are set aside and the event is
    located from the newest rows (no mixed fix is kept)."""
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)
        state = tasking_module._EventState(record.id)
        old = add_rows(env, record, [A_POS] * 3, T0)
        await env.tasking._solve_event(record.id, state, RoundReport(record.id, T0), env.tasking._generation)
        assert record.state == "located"
        env.clock.advance(120.0)
        new = add_rows(env, record, [B_POS] * 3, T0 + 120 * S)
        report = RoundReport(record.id, T0)
        await env.tasking._solve_event(record.id, state, report, env.tasking._generation)
        assert env.tasking.counters["transmission_changes"] == 1
        out = record.solution_out
        assert out is not None and out.fit_consistent and record.state == "located"
        assert geodesic_distance_m(out.lat_deg, out.lon_deg, *B_POS) < 1e3
        assert report.solution is record.solution and record.solution_used == tuple(new)
        rows = {e.seq: e.row for e in record.measurements}
        assert all(not rows[s].used and rows[s].reason.startswith("set aside: the newest measurements") for s in old)
        assert all(rows[s].used for s in new)
        assert record.onset_ns == T0 + 120 * S
        reasons = [t.reason or "" for t in record.transitions]
        assert any(r.startswith("another transmission: the newest measurements fit one transmitter") for r in reasons)


async def test_the_split_solve_of_another_transmitter_uses_the_allowance_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1 with K2: the solve that tests the newest rows on their own (another transmitter)
    is widened like any other: it gets the allowance in force for its rows' satellites,
    and the solution it publishes carries that allowance and its widening."""
    async with environment(NO_CAL, None) as env:
        forced = {s: (4.0, False) for s in ("muos-pac", "uhf-172e", "muos-io")}
        monkeypatch.setattr(env.tasking, "ephemeris_allowance", lambda: dict(forced))
        record = make_event(env, config=NO_CAL)
        state = tasking_module._EventState(record.id)
        add_rows(env, record, [A_POS] * 3, T0)
        await env.tasking._solve_event(record.id, state, RoundReport(record.id, T0), env.tasking._generation)
        assert record.state == "located" and record.solution_allowance
        env.clock.advance(120.0)
        new = add_rows(env, record, [B_POS] * 3, T0 + 120 * S)
        await env.tasking._solve_event(record.id, state, RoundReport(record.id, T0), env.tasking._generation)
        assert env.tasking.counters["transmission_changes"] == 1 and record.solution_used == tuple(new)
        split = [args[0] for fn, args in env.pool.jobs if fn is tasking_module._solve][-1]
        assert len(split.measurements) == len(new)
        sats = {s for m in split.measurements for s in (m.a.sat_id, m.b.sat_id)}
        configured = NO_CAL.resolved_uncertainty()
        for sat in sats:
            assert split.uncertainty.sat_pos_ric_m[sat] == pytest.approx(configured.sat_pos_ric_m[sat] * 4.0)
        assert {a.sat_id for a in record.solution_allowance} == sats
        assert record.solution_widening is not None and record.solution_widening >= 1.0


# ----------------------------------------------------------------------------- signals that cannot be located (UX-4)

SWEPT = load_scenario(ROOT / "config" / "scenarios" / "uplink-swept.yaml")


async def test_a_periodic_sweep_is_reported_as_unlocatable_with_its_reason() -> None:
    """UX-4: every correlation of a 40 ms sawtooth sweep is ambiguous, so the tasking
    status carries why it cannot be located and the advice says so instead of promising
    a position."""
    async with environment(NO_CAL, SWEPT) as env:
        record = make_event(env, config=NO_CAL)
        record.verdict = "uplink"
        reports = await rounds(env, record, 2)
        assert all(p.reason == tasking_module.REASON_AMBIGUOUS_CAF for r in reports for p in r.pairs if p.result)
        doc = env.book.to_schema(record)
        assert doc.tasking is not None and doc.tasking.unlocatable_reason == tasking_module.UNLOCATABLE_AMBIGUOUS
        assert doc.solution is None
        detail = doc.advisory.detail
        assert "No position can be computed. It cannot be located by comparing satellite copies" in detail
        assert "No position estimate yet" not in detail
        assert any("cannot be located by satellite correlation" in a for a in doc.advisory.actions)


# ----------------------------------------------------------------------------- one satellite's ephemeris ends (M7)


def with_expired_table(config: SystemConfig, sat_id: str, end_ns: int) -> SystemConfig:
    """A copy of ``config`` whose satellite ``sat_id`` follows an ephemeris table (sampled
    from its own orbit) that ends at ``end_ns``."""
    from petal_locate.core.orbits import orbit_state
    from petal_locate.core.timebase import iso_from_ns

    orbit = config.orbit(sat_id)
    samples = []
    for k in range(-20, 1):
        t_ns = end_ns + k * 60 * S
        r, v = orbit_state(orbit, t_ns / S)
        samples.append({"t": iso_from_ns(t_ns), "r_m": [float(x) for x in r], "v_mps": [float(x) for x in v]})
    data = config.model_dump(mode="json")
    for sat in data["satellites"]:
        if sat["id"] == sat_id:
            sat["orbit"] = {"type": "table", "samples": samples}
    return SystemConfig.model_validate(data)


async def test_a_satellite_whose_ephemeris_ended_does_not_stop_pairs_that_do_not_use_it() -> None:
    """M7: muos-io's ephemeris table ended a minute ago. Its pairs cannot be tasked, but
    the round goes on for the pair through the other satellites, and the status says
    which satellite and why."""
    expired = with_expired_table(NO_CAL, "muos-io", T0 - 60 * S)
    async with environment(expired, None) as env:
        env.hub.world = world_for(DEMO, NO_CAL)  # the sky itself is unchanged
        record = make_event(env, config=expired)
        reports = await rounds(env, record, 2)
        for report in reports:
            assert report.skipped is None
            assert [p.b_channel_id for p in report.pairs if p.result is not None]  # a pair was correlated
            assert all(CONFIG.channel(p.b_channel_id)[1].watched_satellite_ids[0] != "muos-io" for p in report.pairs)
            assert any(n.startswith("satellite muos-io: orbit not available (") for n in report.notes)
        assert [p.status for p in reports[1].pairs] == ["measured"]
        doc = env.book.to_schema(record)
        assert doc.tasking is not None and "Satellite muos-io: orbit not available" in (doc.tasking.message or "")
        assert env.tasking.counters.get("round_errors", 0) == 0


def test_geometry_evaluates_each_satellite_on_its_own() -> None:
    expired = with_expired_table(CONFIG, "muos-io", T0 - 60 * S)
    orbits, sites = expired.orbits(), expired.sites_xyz()
    a = tasking_module._Pick("tokyo-pac", "tokyo", "muos-pac", "shared-1", -41e6, 0.0, 2e6, None)
    b = tasking_module._Pick("naha-172e", "naha", "uhf-172e", "shared-1", -33.6e6, 0.0, 2e6, None)
    c = tasking_module._Pick("tokyo-io", "tokyo", "muos-io", "shared-1", -41e6, 0.0, 2e6, None)
    job = tasking_module._GeometryJob(
        pairs=(tasking_module._GeoIn(a, b, 301.005e6, 1e4, 2e4), tasking_module._GeoIn(a, c, 301.005e6, 1e4, 2e4)),
        refs=(), t_mid_s=T0 / S, t_ref_s=T0 / S + 1.0, orbits=orbits, sites_xyz=sites,
        region=expired.processing.region, min_elevation_deg=0.0, height_m=0.0,
        sat_ids=("muos-pac", "uhf-172e", "muos-io"), site_ids=("naha", "tokyo"),
    )
    result = tasking_module._geometry(job)
    assert set(result.unavailable) == {"muos-io"} and "ephemeris table" in result.unavailable["muos-io"]
    assert set(result.kinematics) == {"muos-pac", "uhf-172e"}
    assert ("tokyo", "muos-io") not in result.elevation and ("tokyo", "muos-pac") in result.elevation
    assert result.pairs[0].windows is not None
    assert result.pairs[1].windows is None and result.pairs[1].error.startswith("satellite muos-io: orbit not available")


# ----------------------------------------------------------------------------- dynamics bounds (ADAPT-2)


def test_the_fdoa_rate_bound_holds_for_a_low_orbit() -> None:
    """ADAPT-2: the repeat-detection gate and the capture length rest on a bound of
    |d FDOA / dt|. For the shipped low-orbit example the satellite's acceleration alone
    (the former bound) is exceeded by up to twice; with the geometric term |v|^2 / r_min
    the bound holds over the whole visible pass, for emitters and sites across it."""
    from petal_locate.core.geodesy import lla_to_ecef

    cfg = load_config(ROOT / "config" / "leo-example.yaml")
    orbits = cfg.orbits()
    sites = {
        "naha": lla_to_ecef(26.212, 127.679, 10.0),
        "taipei": lla_to_ecef(25.03, 121.56, 10.0),
        "fukuoka": lla_to_ecef(33.59, 130.40, 10.0),
    }
    emitters = [lla_to_ecef(24.0, 126.0, 10.0), lla_to_ecef(30.0, 135.0, 10.0)]
    f, tr = 435.105e6, -289.2e6
    epoch = ns_from_iso("2026-01-01T00:00:00Z") / S
    worst_new = worst_old = 0.0
    samples = 0
    for t in epoch + np.arange(0.0, 480.0, 8.0):
        job = tasking_module._GeometryJob(
            pairs=(), refs=(), t_mid_s=float(t), t_ref_s=float(t), orbits=orbits, sites_xyz=sites,
            region=cfg.processing.region, min_elevation_deg=0.0, height_m=0.0, sat_ids=("leo-a", "leo-b"),
            site_ids=tuple(sites), ground_height_m=10.0,
        )
        kin = tasking_module._geometry(job).kinematics
        bound = Tasking._rate_bounds(kin, "leo-a", "leo-b", f, tr, tr)[1] / tasking_module.RATE_SAFETY
        old = 2.0 * f * (kin["leo-a"].accel_mps2 + kin["leo-b"].accel_mps2) / tasking_module.C
        for x in emitters:
            for sa in sites:
                for sb in sites:
                    visible = all(
                        float(np.ravel(elevation_deg(p, orbits[s], float(t)))[0]) > 0.0
                        for p, s in ((x, "leo-a"), (x, "leo-b"), (sites[sa], "leo-a"), (sites[sb], "leo-b"))
                    )
                    if not visible:
                        continue
                    a, b = PathSpec("leo-a", sa, f, tr), PathSpec("leo-b", sb, f, tr)
                    f1 = float(np.ravel(predict_pair(x, a, b, float(t) - 1.0, orbits, sites)[1])[0])
                    f2 = float(np.ravel(predict_pair(x, a, b, float(t) + 1.0, orbits, sites)[1])[0])
                    rate = abs(f2 - f1) / 2.0
                    worst_new, worst_old = max(worst_new, rate / bound), max(worst_old, rate / old)
                    samples += 1
    assert samples > 100
    assert worst_old > 1.2  # the former bound (acceleration only) did not hold
    assert worst_new < 1.0  # the bound holds without the safety factor
    # GEO: the geometric term adds nothing that matters
    geo = Tasking._rate_bounds(
        {"g": tasking_module._Kinematics(3.0, 2e-4, 35.7e6)}, "g", "g", 301e6, -41e6, -41e6
    )[1]
    assert geo == pytest.approx(
        tasking_module.RATE_SAFETY * (301e6 * 2 + 260e6 * 2) * (2e-4 + 9.0 / 35.7e6) / tasking_module.C
    )


# ----------------------------------------------------------------------------- re-check triggers (K2)


async def test_a_change_of_waveform_power_or_width_makes_the_event_rechecked() -> None:
    async with environment(NO_CAL, None) as env:
        record = make_event(env, config=NO_CAL)
        state = tasking_module._EventState(record.id)
        primary = tasking_module._Pick("tokyo-pac", "tokyo", "muos-pac", "shared-1", -41e6, 0.0, 2e6, None)
        capture = AssembledCapture(
            "c1", "tokyo-pac", np.zeros(20000, np.complex64), T0, 20000.0, 260.005e6, False,
            ClockStatus(source="sim", locked=True, est_offset_ns=0.0, holdover_s=0.0), 1,
        )
        out = tasking_module._CaptureOutcome("tokyo-pac", capture)
        report = RoundReport(record.id, T0)
        # nothing to re-check before anything is measured
        env.tasking._apply_classification(record.id, state, Classification("noise", 0.9, {}), out, report)
        env.tasking._apply_classification(record.id, state, Classification("cw", 0.9, {}), out, report)
        assert record.recheck is None
        add_rows(env, record, [A_POS] * 2, T0)
        await env.tasking._solve_event(record.id, state, RoundReport(record.id, T0), env.tasking._generation)
        assert record.solution_out is not None
        # an unknown class, or a low confidence, is not a change
        env.tasking._apply_classification(record.id, state, Classification("unknown", 0.9, {}), out, report)
        env.tasking._apply_classification(record.id, state, Classification("noise", 0.3, {}), out, report)
        assert record.recheck is None
        env.tasking._apply_classification(record.id, state, Classification("noise", 0.9, {}), out, report)
        env.tasking._apply_classification(record.id, state, Classification("cw", 0.8, {}), out, report)
        assert record.recheck == "its waveform changed from noise to cw"
        assert (record.transitions[-1].reason or "") == "re-check: its waveform changed from noise to cw"
        # while re-checked, the next round classifies again
        state.classified_ns = record.recheck_since_ns - 1
        assert env.tasking._needs_classification(record, state)
        assert env.book.clear_recheck(record.id, "pair x agrees")
        # power and width steps of the primary sighting between rounds
        unit = next(u for u in record.members.values() if u.channel_id == "tokyo-pac")
        env.tasking._check_level(record, state, primary)
        unit.snr_db += 4.0
        env.tasking._check_level(record, state, primary)
        assert record.recheck is None  # 4 dB: within a fade
        unit.snr_db += 7.0
        env.tasking._check_level(record, state, primary)
        assert record.recheck == "its level at tokyo-pac rose by 7 dB between rounds"
        env.book.clear_recheck(record.id, "agrees")
        unit.down_hi_hz = unit.down_lo_hz + 25e3  # 10 kHz -> 25 kHz
        env.tasking._check_level(record, state, primary)
        assert record.recheck == "its width at tokyo-pac changed from 10.0 kHz to 25.0 kHz"
        doc = env.book.to_schema(record)
        assert doc.recheck == record.recheck


# ----------------------------------------------------------------------------- satellites that cannot see the emitter (ADAPT-1)


def hawaii_fix(config: SystemConfig) -> schemas.SolutionOut:
    """A consistent fix at 18 N 170 W, where muos-io (75 E) is below the horizon."""
    cov = [[1.0e5, 0.0], [0.0, 1.0e5]]
    return schemas.SolutionOut(
        lat_deg=18.0, lon_deg=-170.0, height_m=0.0, cov_en_m2=cov, cov_noise_en_m2=cov,
        cov_consider_en_m2=[[0.0, 0.0], [0.0, 0.0]],
        ellipse=schemas.EllipseOut(semi_major_m=800.0, semi_minor_m=800.0, azimuth_deg=0.0, confidence=0.95),
        cep50_m=370.0, chi2=1.0, dof=4, reduced_chi2=0.25, residuals=[0.0] * 6, ambiguities=[],
        ambiguity_resolved_by="none", converged=True, dominant_error="noise", n_measurements=6,
    )


async def test_a_satellite_that_cannot_see_the_fix_is_not_paired() -> None:
    async with environment(NO_CAL, None) as env:
        record = make_event(env, sightings=(("hilo-pac", 15.0),), config=NO_CAL)
        quality = await env.tasking._quality_table()
        state = tasking_module._EventState(record.id)
        _, partners, _ = env.tasking._choose_channels(record, state, quality, 301.005e6)
        assert "muos-io" in {p.sat_id for p in partners}  # before a fix: the frequency plan
        record.solution_out = hawaii_fix(NO_CAL)
        record.not_visible_satellite_ids = ["muos-io"]  # what the correlator works out
        _, partners, notes = env.tasking._choose_channels(record, state, quality, 301.005e6)
        assert "muos-io" not in {p.sat_id for p in partners} and "uhf-172e" in {p.sat_id for p in partners}
        assert "muos-io cannot see the transmitter's position, so it is not paired" in notes


def test_visible_satellites_uses_the_whole_ellipse() -> None:
    from petal_locate.server.pipeline.events import visible_satellites

    fix = hawaii_fix(CONFIG)
    orbits = CONFIG.orbits()
    assert visible_satellites(fix, orbits, ["muos-pac", "uhf-172e", "muos-io"], T0 / S, 0.0) == ["muos-pac", "uhf-172e"]
    # an ellipse wide enough to reach where muos-io rises keeps it
    wide = fix.model_copy(update={"ellipse": fix.ellipse.model_copy(update={"semi_major_m": 9.0e6, "azimuth_deg": 90.0})})
    assert "muos-io" in visible_satellites(wide, orbits, ["muos-io"], T0 / S, 0.0)
    # an orbit that cannot be evaluated cannot be ruled out
    assert visible_satellites(fix, {}, ["muos-io"], T0 / S, 0.0) == ["muos-io"]


# ----------------------------------------------------------------------------- reference choice (K6)


def with_second_reference(config: SystemConfig) -> SystemConfig:
    """A copy of ``config`` with a second reference carrier in shared-1, at Manila, 8 kHz
    wide at 301.0085 MHz."""
    data = config.model_dump(mode="json")
    guam = next(c for c in data["carriers"] if c["id"] == "ref-guam")
    data["carriers"].append(
        dict(guam, id="ref-manila", name="Reference carrier, Manila (illustrative)", uplink_center_hz=301.0085e6,
             location=dict(guam["location"], lat_deg=14.6, lon_deg=121.0))
    )
    return SystemConfig.model_validate(data)


async def test_the_reference_nearest_the_fix_is_preferred() -> None:
    """K6: with several usable references, the one nearest the event's fix is planned
    (nearest in frequency without a fix)."""
    async with environment(with_second_reference(CONFIG), None) as env:
        a = tasking_module._Pick("tokyo-pac", "tokyo", "muos-pac", "shared-1", -41e6, 0.0, 2e6, None)
        b = tasking_module._Pick("naha-172e", "naha", "uhf-172e", "shared-1", -33.6e6, 0.0, 2e6, None)
        band = (300.999e6, 301.003e6, 301.001e6)  # an event between the two references
        carriers = env.tasking._references_for(a, b, *band)
        assert [c.id for c in carriers] == ["ref-manila", "ref-guam"]  # no fix: nearest in frequency
        for carrier in carriers:
            near = (carrier.location.lat_deg + 0.5, carrier.location.lon_deg)
            assert env.tasking._references_for(a, b, *band, near)[0].id == carrier.id
        plans = env.tasking._plan_references(a, [b], *band, (13.0, 145.0))
        assert [p.carrier_id for p in plans] == ["ref-guam"]
        plans = env.tasking._plan_references(a, [b], *band)
        assert [p.carrier_id for p in plans] == ["ref-manila"]


def test_the_correlator_lists_only_satellites_that_can_see_the_fix() -> None:
    """ADAPT-1: the event's relaying satellites (and the advisory's scope) follow the
    frequency plan until a consistent fix exists, then only the satellites that can see
    it. The README's case: near Hawaii, muos-io (75 E) is below the horizon."""
    from petal_locate.server.pipeline.advisory import advise
    from petal_locate.server.pipeline.correlator import Correlator

    clock = Clock(T0)
    book = EventBook(clock=clock)
    correlator = Correlator(CONFIG, book, clock=clock)
    record = book.create(space="up", lo_hz=JAMMER[1], hi_hz=JAMMER[2], first_seen_ns=T0, last_seen_ns=T0)
    record.verdict, record.verdict_reason = "uplink", "It is seen at Hilo and Honolulu."
    correlator._evaluate(record, T0)
    assert record.satellite_ids == ["muos-pac", "uhf-172e", "muos-io"] and record.not_visible_satellite_ids == []
    record.solution_out, record.solution_t_ns = hawaii_fix(CONFIG), T0
    correlator._evaluate(record, T0)
    assert record.satellite_ids == ["muos-pac", "uhf-172e"]
    assert record.not_visible_satellite_ids == ["muos-io"]
    assert {t.sat_id for t in record.transponders} == {"muos-pac", "uhf-172e"}
    advisory = advise(record, CONFIG)
    assert "MUOS Indo-Asia" not in advisory.headline
    assert "cannot see the transmitter's estimated position, so its users are not affected" in advisory.detail
    doc = book.to_schema(record)
    assert doc.not_visible_satellite_ids == ["muos-io"] and doc.satellite_ids == ["muos-pac", "uhf-172e"]
    # a solution that is not a fix (here an inconsistent one) decides nothing
    record.solution_out = hawaii_fix(CONFIG).model_copy(
        update={"fit_consistent": False, "is_fix": False, "not_fix_reason": "the measurements do not fit"}
    )
    record.solution_t_ns = T0 + S
    correlator._evaluate(record, T0)
    assert record.satellite_ids == ["muos-pac", "uhf-172e", "muos-io"]


# ----------------------------------------------------------------------------- clock offsets from calibrations (UX-10)

CLOCK_FAULT = load_scenario(ROOT / "config" / "scenarios" / "clock-fault.yaml")


def test_site_clock_offsets_find_the_site_that_is_off() -> None:
    """The fit separates site clocks from satellite terms: a satellite error common to
    every site is not blamed on a clock; a site 9.7 us behind is."""
    from petal_locate.server.pipeline.tasking import ClockEdge, site_clock_offsets

    us = 1e-6
    pac, ufo, io = 2.0 * us, -1.5 * us, 0.5 * us  # satellite terms (ephemeris, transponder)
    clock = {"tokyo": 0.0, "naha": -9.7 * us, "busan": 0.1 * us, "manila": -0.1 * us, "seoul": 0.05 * us}
    sat = {"muos-pac": pac, "uhf-172e": ufo, "muos-io": io}

    def edge(sa: str, a: str, sb: str, b: str) -> ClockEdge:
        return ClockEdge(sa, a, sb, b, clock[a] + sat[sa] - clock[b] - sat[sb], 0.1 * us, 3.3 * us, 3.3 * us)

    edges = [
        edge("muos-pac", "tokyo", "muos-io", "tokyo"),  # same site: ties the satellites' terms
        edge("muos-pac", "tokyo", "uhf-172e", "naha"),
        edge("muos-pac", "tokyo", "uhf-172e", "busan"),
        edge("muos-pac", "tokyo", "muos-io", "manila"),
        edge("muos-pac", "tokyo", "muos-io", "seoul"),
        edge("muos-pac", "tokyo", "uhf-172e", "seoul"),
    ]
    rows = {e.site_id: e for e in site_clock_offsets(edges, CONFIG)}
    assert set(rows) == {"tokyo", "naha", "busan", "manila", "seoul"}
    assert rows["naha"].offset_us == pytest.approx(-9.7, abs=0.05) and rows["naha"].caution
    names = {site.id: site.name for site in CONFIG.sites}
    assert rows["naha"].message.startswith(f"Reference calibrations against {names['tokyo']} put the timing of")
    # seen through one satellite only (uhf-172e at Naha): both explanations are named
    assert "about 9.7 µs behind the other sites: either its clock is off" in rows["naha"].message
    assert "or the position of uhf-172e is wrong beyond its configured uncertainty" in rows["naha"].message
    assert not any(rows[s].caution for s in ("tokyo", "busan", "manila", "seoul"))
    assert all(abs(rows[s].offset_us) < 0.2 for s in ("tokyo", "busan", "manila", "seoul"))
    assert rows["naha"].n_calibrations == 1 and rows["naha"].compared_with == ["tokyo"]
    assert rows["naha"].sigma_us is None and rows["tokyo"].sigma_us is not None
    # a large site-dependent ephemeris allowance raises the threshold
    loose = [ClockEdge(e.sat_a, e.site_a, e.sat_b, e.site_b, e.c_s, 5.0 * us, 3.3 * us, 3.3 * us) for e in edges]
    assert not {e.site_id: e for e in site_clock_offsets(loose, CONFIG)}["naha"].caution
    # two sites only: which one is off cannot be told, both are flagged
    pair = site_clock_offsets([ClockEdge("muos-pac", "naha", "muos-pac", "tokyo", -9.7 * us)], CONFIG)
    assert [e.caution for e in pair] == [True, True]
    assert "differs by about 9.7 µs: one of the two clocks may be off" in pair[0].message
    assert "or the position of muos-pac is wrong" in pair[0].message  # one satellite: both explanations
    both = site_clock_offsets(
        [ClockEdge("muos-pac", "naha", "muos-pac", "tokyo", -9.7 * us),
         ClockEdge("uhf-172e", "naha", "uhf-172e", "tokyo", -9.6 * us)], CONFIG)
    assert [e.caution for e in both] == [True, True]
    assert "one of the two clocks is probably off (calibrations through muos-pac and uhf-172e agree" in both[0].message
    assert site_clock_offsets([], CONFIG) == []
    healthy = site_clock_offsets([ClockEdge("muos-pac", "naha", "muos-pac", "tokyo", 0.3 * us)], CONFIG)
    assert not any(e.caution or e.message for e in healthy)


def test_a_clock_fault_that_starts_mid_run_is_not_spread_onto_healthy_sites() -> None:
    """Final recheck of R3: Naha's clock stepped 9.8 us while calibrations from before the
    step still counted. Every edge was a spoke from Honolulu's muos-pac channel; Busan,
    Naha (and later Hilo) were reached through uhf-172e. The fit gives what it cannot
    resolve to the satellite term, which took the uhf-172e sites' mean: healthy Busan was
    flagged at +4.9 us (later Hilo at +3.1 us) with 'either its clock is off or the
    position of uhf-172e is wrong', neither of which was true. The sites whose clock
    differences the edges determine are centered on their own median: with three of
    them only Naha is off; with two, the pair is reported as such."""
    from petal_locate.server.pipeline.tasking import ClockEdge, site_clock_offsets

    us = 1e-6
    names = {site.id: site.name for site in CONFIG.sites}

    def spoke(sat: str, site: str, c_us: float) -> ClockEdge:
        return ClockEdge("muos-pac", "honolulu", sat, site, c_us * us, 0.1 * us, 3.3 * us, 3.3 * us)

    star = [spoke("uhf-172e", "busan", 0.0), spoke("uhf-172e", "naha", 9.8),
            spoke("muos-io", "manila", 0.08), spoke("muos-io", "seoul", -0.08)]
    rows = {e.site_id: e for e in site_clock_offsets(star, CONFIG)}
    # Busan and Naha: only their difference is known, so the message is about the pair
    for site in ("busan", "naha"):
        assert rows[site].caution
        assert rows[site].message.startswith(
            f"Reference calibrations show the timing of {names['busan']} and {names['naha']} differs by about 9.8 µs"
        )
        assert "either its clock is off" not in rows[site].message
    assert not any(rows[s].caution or rows[s].message for s in ("honolulu", "manila", "seoul"))
    # with Hilo also reached through uhf-172e, Naha alone is off
    rows = {e.site_id: e for e in site_clock_offsets(star + [spoke("uhf-172e", "hilo", 0.0)], CONFIG)}
    assert rows["naha"].caution and rows["naha"].offset_us == pytest.approx(-9.8, abs=0.05)
    assert not any(rows[s].caution for s in ("busan", "hilo", "honolulu", "manila", "seoul"))
    assert all(abs(rows[s].offset_us) < 0.2 for s in ("busan", "hilo"))


async def test_a_site_clock_fault_shows_in_the_calibration_estimates() -> None:
    """UX-10: Naha's clock is 10 us late; every calibrated Naha pair carries it in its
    correction, and the estimate flags Naha (calibration still removes it from the fix)."""
    async with environment(CONFIG, CLOCK_FAULT) as env:
        record = make_event(env)
        await rounds(env, record, 10)  # partner rotation brings in enough sites and satellites
        estimates = {e.site_id: e for e in env.tasking.site_clock_estimates()}
        assert len(estimates) >= 4
        naha = estimates["naha"]
        assert naha.offset_us == pytest.approx(-10.0, abs=1.0)
        assert naha.caution and CONFIG.site("naha").name in naha.message and "behind" in naha.message
        assert not any(e.caution for s, e in estimates.items() if s != "naha")
        assert all(abs(e.offset_us) < 0.5 for s, e in estimates.items() if s != "naha")


NAHA_FIRST = (("naha-172e", 15.0), ("tokyo-pac", 12.0), ("seoul-io", 10.0))


async def test_a_clock_fault_at_the_primary_site_is_told_apart_from_its_satellite() -> None:
    """R3: with naha-172e the primary of every round, every cross-satellite calibration
    had uhf-172e at Naha, so Naha's clock could not be told from uhf-172e's satellite term
    and the fit gave the 10 us to the satellite (Naha shown healthy). Same-satellite clock
    checks across sites (one satellite's copy of the reference at two sites: the
    satellite's own error is common to both) measure the clock difference directly."""
    async with environment(CONFIG, CLOCK_FAULT) as env:
        record = make_event(env, sightings=NAHA_FIRST)
        reports = await rounds(env, record, 8)
        assert all(r.primary_channel_id == "naha-172e" for r in reports)
        checks = [c for r in reports for c in r.calibrations if c.key[0] == c.key[2] and c.key[1] != c.key[3]]
        assert {c.key[0] for c in checks} >= {"uhf-172e", "muos-pac"}  # through more than one satellite
        # the round's first calibrations are the pairs' own; clock checks fill later rounds
        assert not any(c.key[0] == c.key[2] for c in reports[0].calibrations)
        estimates = {e.site_id: e for e in env.tasking.site_clock_estimates()}
        naha = estimates["naha"]
        assert naha.offset_us == pytest.approx(-10.0, abs=1.0) and naha.caution
        assert "its clock is probably off" in naha.message and "agree" in naha.message
        assert not any(e.caution for s, e in estimates.items() if s != "naha")
        assert all(abs(e.offset_us) < 0.5 for s, e in estimates.items() if s != "naha")
        # the target pairs were never same-satellite, and the clock checks did not stop them being solved
        assert all(r.pairs and all(p.a_channel_id == "naha-172e" for p in r.pairs) for r in reports)
        assert record.solution_out is not None and record.solution_out.fit_consistent


def test_site_clock_offsets_tell_a_clock_from_a_satellite_position_error() -> None:
    """R3: clock checks (one satellite's copy at two sites) give a site's offset through
    each satellite. A late clock shows alike through all of them; a satellite several km
    off beyond its configured sigma shows through that satellite only (with opposite
    signs at sites on either side of it), so the clock is not blamed and the satellite
    is named; such errors also stop smaller offsets elsewhere from being judged."""
    from petal_locate.server.pipeline.tasking import ClockEdge, site_clock_offsets

    us = 1e-6
    names = {site.id: site.name for site in CONFIG.sites}

    def check(sat: str, a: str, b: str, c_us: float) -> ClockEdge:
        return ClockEdge(sat, a, sat, b, c_us * us, 0.1 * us, 3.3 * us, 3.3 * us)

    # a clock fault at Naha seen through two satellites against three sites
    late = [check(sat, "naha", other, -10.0 + d) for sat, d in (("muos-pac", 0.1), ("uhf-172e", -0.1))
            for other in ("tokyo", "seoul", "manila")]
    rows = {e.site_id: e for e in site_clock_offsets(late, CONFIG)}
    assert rows["naha"].caution and rows["naha"].offset_us == pytest.approx(-10.0, abs=0.2)
    assert "its clock is probably off (calibrations through muos-pac and uhf-172e agree" in rows["naha"].message
    assert not any(rows[s].caution or rows[s].message for s in ("tokyo", "seoul", "manila"))
    # muos-pac 8 km off: Hilo's checks against Asian sites show +4 us through it, nothing through uhf-172e
    eph = [check("muos-pac", "hilo", other, 4.0) for other in ("tokyo", "seoul", "manila")]
    eph += [check("uhf-172e", "hilo", other, 0.1) for other in ("tokyo", "seoul", "manila")]
    rows = {e.site_id: e for e in site_clock_offsets(eph, CONFIG)}
    assert not any(e.caution for e in rows.values())
    note = rows["hilo"].message
    assert note.startswith(f"Reference calibrations put the timing of {names['hilo']} differently through different")
    assert "the clock of" in note and "is probably fine and the position of muos-pac is probably wrong" in note
    # the same, with a 1.6 us offset of another site whose evidence goes through a contaminated one:
    # not judged while satellite errors of 4 us show in the group
    eph.append(check("muos-pac", "honolulu", "hilo", -1.6))
    eph.append(check("uhf-172e", "honolulu", "hilo", -1.6))
    rows = {e.site_id: e for e in site_clock_offsets(eph, CONFIG)}
    assert not rows["honolulu"].caution


# ----------------------------------------------------------------------------- ephemeris allowance (D1)


def synthetic_network(
    offsets: dict[str, tuple[float, float, float]], clocks_ns: dict[str, float] | None = None, seed: int = 7
) -> list[Any]:
    """TDOA calibrations of the pacific-uhf network at T0 against the Guam reference, as
    ``EvidenceEdge``s: same-satellite clock checks from Tokyo to every other site through
    each satellite both see, and cross-satellite pairs at Tokyo, with corrections made
    from the given satellite RIC position offsets (m), site clock offsets (ns),
    arbitrary satellite common delays and 150 ns of noise per calibration."""
    from petal_locate.core.orbits import PerturbedOrbit
    from petal_locate.core.solver import reference_position_partials
    from petal_locate.core.types import CalRef
    from petal_locate.server.pipeline.tasking import EvidenceEdge

    orbits, sites = CONFIG.orbits(), CONFIG.sites_xyz()
    x_ref = CONFIG.carrier_xyz("ref-guam")
    rng = np.random.default_rng(seed)
    common = {"muos-pac": 3e-6, "uhf-172e": -2e-6, "muos-io": 1e-6}
    clocks = clocks_ns or {}
    t_s = T0 / S

    def path(sat: str, site: str) -> PathSpec:
        return PathSpec(sat, site, 301.0e6, CONFIG.satellite(sat).transponder("shared-1").translation_hz)

    def visible(sat: str, site: str) -> bool:
        return float(elevation_deg(sites[site], orbits[sat], t_s)) > 5.0

    pairs = [(sat, "tokyo", sat, site) for sat in common for site in sites if site != "tokyo"]
    pairs += [("muos-pac", "tokyo", "uhf-172e", site) for site in ("naha", "busan", "hilo")]
    pairs += [("muos-pac", "tokyo", "muos-io", site) for site in ("manila", "seoul", "tokyo")]
    perturbed = dict(orbits)
    for sat, ric in offsets.items():
        perturbed[sat] = PerturbedOrbit(orbits[sat], np.asarray(ric, dtype=float), np.zeros(3), t_s)
    edges = []
    for k, (sa, a, sb, b) in enumerate(pairs):
        if not (visible(sa, a) and visible(sb, b)):
            continue
        cal = CalRef(f"c{k}", x_ref, path(sa, a), path(sb, b), T0, 150e-9)
        geometric = float(predict_pair(x_ref, cal.a, cal.b, t_s, perturbed, sites)[0]) - float(
            predict_pair(x_ref, cal.a, cal.b, t_s, orbits, sites)[0]
        )
        c = geometric + (clocks.get(a, 0.0) - clocks.get(b, 0.0)) * 1e-9 + common[sa] - common[sb]
        c += float(rng.normal(0.0, 150e-9))
        partials = reference_position_partials("tdoa", cal, orbits, sites)
        edges.append(EvidenceEdge(sa, a, sb, b, c, 150e-9, tuple((s, tuple(p)) for s, p in partials.items())))
    return edges


def test_the_calibration_network_sizes_a_satellite_position_error_and_shows_none_without_one() -> None:
    """D1: the calibrations held, read as one least-squares fit with free site clocks and
    satellite common delays, against operator-grade sigmas (100 m / 1 km / 1 km):

    - muos-pac 8 km ahead and uhf-172e 6 km behind in-track (ephemeris-error): both
      in-track errors come out within 20 %, and each scale puts its error inside the
      widened one-sigma;
    - muos-pac alone 8 km off: the two co-located satellites see the network alike, so
      the fit shares the error between them (the smallest errors that explain it), but
      their difference, which is what a calibrated pair through both feels, comes out
      right and their combined allowance covers it;
    - no offset: nothing reaches the prior's own size; a 10 us clock fault at one site
      changes nothing at all (a per-site offset is the clock's), so a clock caution can
      never widen an ephemeris."""
    from petal_locate.server.pipeline.tasking import ephemeris_scale, satellite_position_evidence

    sigma = CONFIG.resolved_uncertainty().sat_pos_ric_m
    both = satellite_position_evidence(
        synthetic_network({"muos-pac": (300.0, 8000.0, 400.0), "uhf-172e": (-200.0, -6000.0, 300.0)}), sigma
    )
    assert set(both) == {"muos-pac", "uhf-172e", "muos-io"}
    for sat, truth in (("muos-pac", 8000.0), ("uhf-172e", -6000.0)):
        assert both[sat].error_ric_m[1] == pytest.approx(truth, rel=0.2)
        scale, capped = ephemeris_scale(both[sat], 20.0)
        assert not capped and abs(truth) <= scale * sigma[sat][1] <= 2.0 * abs(truth)
    assert both["muos-pac"].n_edges >= 8 and both["muos-io"].norm < 1.5
    alone = satellite_position_evidence(synthetic_network({"muos-pac": (300.0, 8000.0, 400.0)}), sigma)
    pac, ufo = alone["muos-pac"], alone["uhf-172e"]
    assert pac.error_ric_m[1] - ufo.error_ric_m[1] == pytest.approx(8000.0, rel=0.15)
    assert pac.error_ric_m[1] > 0.0 > ufo.error_ric_m[1]
    combined = math.hypot(ephemeris_scale(pac, 20.0)[0] * sigma["muos-pac"][1], ephemeris_scale(ufo, 20.0)[0] * sigma["uhf-172e"][1])
    assert combined >= 8000.0
    clean = satellite_position_evidence(synthetic_network({}), sigma)
    assert all(e.norm < 1.5 and e.norm_sigma <= 1.0 for e in clean.values()), clean
    faulty = satellite_position_evidence(synthetic_network({}, clocks_ns={"naha": 10_000.0}), sigma)
    for sat, evidence in faulty.items():
        assert evidence.norm == pytest.approx(clean[sat].norm, abs=1e-6)
        assert evidence.error_ric_m == pytest.approx(clean[sat].error_ric_m, abs=1e-3)
    assert satellite_position_evidence([], sigma) == {}


def test_ephemeris_scale_covers_the_evidence_with_its_margin_rounded_up_and_capped() -> None:
    """D1: the scale is max(3, norm + 2 sigma of norm), rounded up to quarter octaves (so a
    small change of the evidence leaves the solve alone) and capped; asking for more than
    the cap, or giving no evidence to size, says so (capped); a cap of 1 turns it off."""
    from petal_locate.server.pipeline.tasking import DOUBT_MIN_SCALE, PositionEvidence, ephemeris_scale

    def evidence(norm: float, spread: float) -> PositionEvidence:
        return PositionEvidence("sat", (0.0, norm * 1000.0, 0.0), norm, spread, 6)

    assert DOUBT_MIN_SCALE == 3.0
    assert ephemeris_scale(evidence(0.2, 1.0), 20.0) == (pytest.approx(2 ** (7 / 4)), False)  # the floor 3 -> 3.36
    assert ephemeris_scale(evidence(6.2, 0.75), 20.0) == (pytest.approx(8.0), False)  # 7.7 -> 8
    assert ephemeris_scale(evidence(6.8, 0.69), 20.0) == (pytest.approx(2 ** (13 / 4)), False)  # 8.2 -> 9.51
    assert ephemeris_scale(evidence(6.0, 1.0), 20.0) == (pytest.approx(8.0), False)  # exactly a step: kept
    assert ephemeris_scale(evidence(19.0, 1.0), 20.0) == (20.0, True)
    assert ephemeris_scale(evidence(17.5, 1.0), 20.0) == (20.0, False)  # 19.5 -> 22.6 rounded, not asked beyond 20
    assert ephemeris_scale(None, 20.0) == (20.0, True)
    assert ephemeris_scale(evidence(float("nan"), 1.0), 12.0) == (12.0, True)
    assert ephemeris_scale(evidence(6.0, 1.0), 1.0) == (1.0, True)


async def test_the_allowance_waits_for_a_confirmed_doubt_rises_at_once_holds_and_ends_when_its_evidence_ages_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1 hysteresis: a satellite named by the clock checks at one round end only is not
    widened; named again at least one capture interval (5 s) later, within
    calibration.max_age_s, its scale applies to the next solve at once; a smaller
    estimate or a cleared doubt does not lower it until the evidence that sized it is
    older than calibration.max_age_s (300 s), then it falls back to the configured
    sigma. With nothing in doubt the solves use the configured Uncertainty object itself.
    A satellite configured with a zero sigma is widened from the operator-grade default.
    Replacing the world or the configuration forgets the allowance and the doubt."""
    from petal_locate.server.pipeline.tasking import DOUBT_CONFIRM_INTERVALS, PositionEvidence

    data = CONFIG.export_dict()
    data["satellites"][2]["ephemeris_sigma"] = {"pos_m": [0.0, 0.0, 0.0], "vel_mps": [0.0, 0.0, 0.0]}
    zero = SystemConfig.model_validate(data)
    assert zero.satellites[2].id == "muos-io"
    async with environment(zero, None) as env:
        tasking = env.tasking
        shown: dict[str, PositionEvidence] = {}
        monkeypatch.setattr(tasking, "satellite_position_evidence", lambda: dict(shown))
        rows = [Measurement("tdoa", 0.0, 1e-7, T0, PathSpec("muos-pac", "tokyo", 301e6, -41e6),
                            PathSpec("muos-io", "naha", 301e6, -41e6))]
        tasking._update_allowance(set())
        assert tasking.ephemeris_allowance() == {}
        assert tasking._solve_uncertainty(rows) == (tasking._uncertainty, ())
        assert tasking._solve_uncertainty(rows)[0] is tasking._uncertainty
        shown["muos-pac"] = PositionEvidence("muos-pac", (0.0, 6000.0, 0.0), 6.2, 0.75, 9)
        # named once, then again 4 s later: not yet confirmed
        tasking._update_allowance({"muos-pac"})
        assert tasking.ephemeris_allowance() == {}
        env.clock.advance(4.0)
        tasking._update_allowance({"muos-pac"})
        assert tasking.ephemeris_allowance() == {}
        assert tasking._solve_uncertainty(rows)[0] is tasking._uncertainty
        env.clock.advance(1.0)  # 5 s after the first: confirmed, sized at once
        tasking._update_allowance({"muos-pac"})
        assert tasking.ephemeris_allowance() == {"muos-pac": (pytest.approx(8.0), False)}
        unc, applied = tasking._solve_uncertainty(rows)
        assert unc.sat_pos_ric_m["muos-pac"] == pytest.approx(np.array([800.0, 8000.0, 8000.0]))
        assert unc.sat_vel_ric_mps is tasking._uncertainty.sat_vel_ric_mps
        assert [(a.sat_id, a.scale, a.capped) for a in applied] == [("muos-pac", pytest.approx(8.0), False)]
        # a smaller estimate 100 s later: the scale holds
        env.clock.advance(100.0)
        shown["muos-pac"] = PositionEvidence("muos-pac", (0.0, 2000.0, 0.0), 2.0, 0.5, 9)
        tasking._update_allowance({"muos-pac"})
        assert tasking.ephemeris_allowance()["muos-pac"][0] == pytest.approx(8.0)
        # the doubt clears: still held until 300 s after the large estimate, then the smaller one
        env.clock.advance(150.0)
        tasking._update_allowance(set())
        assert tasking.ephemeris_allowance()["muos-pac"][0] == pytest.approx(8.0)
        env.clock.advance(60.0)  # 310 s after the large estimate, 210 s after the smaller one
        tasking._update_allowance(set())
        assert tasking.ephemeris_allowance() == {"muos-pac": (pytest.approx(2 ** (7 / 4)), False)}
        env.clock.advance(100.0)
        tasking._update_allowance(set())
        assert tasking.ephemeris_allowance() == {}
        assert tasking._solve_uncertainty(rows)[0] is tasking._uncertainty
        # named twice 200 s apart: confirmed; named again 310 s after a lone sighting: not
        shown["uhf-172e"] = PositionEvidence("uhf-172e", (0.0, 3000.0, 0.0), 3.0, 0.5, 6)
        tasking._update_allowance({"uhf-172e"})
        env.clock.advance(200.0)
        tasking._update_allowance({"uhf-172e"})
        assert set(tasking.ephemeris_allowance()) == {"uhf-172e"}
        shown["muos-io"] = PositionEvidence("muos-io", (0.0, 0.0, 0.0), 0.0, 1.0, 3)
        tasking._update_allowance({"muos-io"})
        env.clock.advance(310.0)
        tasking._update_allowance({"muos-io"})
        assert "muos-io" not in tasking.ephemeris_allowance()
        # a satellite configured with zero sigma: widened from the operator-grade default
        env.clock.advance(5.0)
        tasking._update_allowance({"muos-io"})
        unc, applied = tasking._solve_uncertainty(rows)
        operator = np.asarray(zero.processing.uncertainty.ephemeris.operator.pos_m)
        assert unc.sat_pos_ric_m["muos-io"] == pytest.approx(operator * 2 ** (7 / 4))
        # a satellite the rows do not go through changes nothing
        other = [Measurement("tdoa", 0.0, 1e-7, T0, PathSpec("muos-pac", "tokyo", 301e6, -41e6),
                             PathSpec("uhf-172e", "naha", 301e6, -33.6e6))]
        env.clock.advance(400.0)
        tasking._update_allowance(set())
        assert tasking._solve_uncertainty(other)[0] is tasking._uncertainty
        tasking._update_allowance({"muos-io"})
        env.clock.advance(5.0)
        tasking._update_allowance({"muos-io"})
        assert set(tasking.ephemeris_allowance()) == {"muos-io"}
        tasking.world_changed(env.clock())
        assert tasking.ephemeris_allowance() == {}
        tasking._update_allowance({"muos-io"})  # the doubt was forgotten too: one sighting again
        assert tasking.ephemeris_allowance() == {}
        env.clock.advance(5.0)
        tasking._update_allowance({"muos-io"})
        assert set(tasking.ephemeris_allowance()) == {"muos-io"}
        tasking.reconfigure(zero)
        assert tasking.ephemeris_allowance() == {}
        tasking._update_allowance({"muos-io"})
        assert tasking.ephemeris_allowance() == {}
        # a cap of 1 turns the allowance off
        tasking.reconfigure(with_processing(zero, calibration={"ephemeris_allowance_max_scale": 1.0}))
        for _ in range(3):
            tasking._update_allowance({"muos-io", "muos-pac"})
            env.clock.advance(5.0)
        assert tasking.ephemeris_allowance() == {}
    assert DOUBT_CONFIRM_INTERVALS * zero.processing.capture.interval_s == 5.0  # the 5 s above


async def test_a_doubt_not_yet_confirmed_holds_a_first_fix_only_until_it_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final pass: ``_unconfirmed_doubt`` names the doubted satellites the rows (or their
    calibrations) go through that no allowance covers yet because the doubt is not
    confirmed; those hold a first fix (``EventBook.set_solution(unconfirmed_doubt=...)``).
    The hold ends once the doubt is confirmed (one capture interval later), and never
    starts for a satellite the rows do not go through, one the configuration does not
    know, or with the allowance turned off (it would never come)."""
    from petal_locate.core.types import CalRef
    from petal_locate.server.pipeline.tasking import PositionEvidence

    async with environment(CONFIG, None) as env:
        tasking = env.tasking
        shown = {"uhf-172e": PositionEvidence("uhf-172e", (0.0, 6000.0, 0.0), 6.2, 0.75, 9)}
        monkeypatch.setattr(tasking, "satellite_position_evidence", lambda: dict(shown))
        cal = CalRef("c-1", CONFIG.carrier_xyz("ref-guam"), PathSpec("muos-pac", "tokyo", 301e6, -41e6),
                     PathSpec("uhf-172e", "naha", 301e6, -33.6e6), T0, 1e-7)
        plain = Measurement("tdoa", 0.0, 1e-7, T0, PathSpec("muos-pac", "tokyo", 301e6, -41e6),
                            PathSpec("muos-io", "naha", 301e6, -41e6))
        calibrated = Measurement("tdoa", 0.0, 1e-7, T0, plain.a, plain.b, cal=cal)
        assert tasking._unconfirmed_doubt((), [plain]) == ()
        tasking._update_allowance({"uhf-172e"})  # named once
        assert tasking._unconfirmed_doubt(("uhf-172e",), [plain]) == ()  # not through these rows
        assert tasking._unconfirmed_doubt(("uhf-172e",), [calibrated]) == ("uhf-172e",)  # through their calibration
        assert tasking._unconfirmed_doubt(("uhf-172e", "sat-unknown"), [calibrated]) == ("uhf-172e",)
        env.clock.advance(5.0)
        tasking._update_allowance({"uhf-172e"})  # confirmed and sized: covered, no hold
        assert set(tasking.ephemeris_allowance()) == {"uhf-172e"}
        assert tasking._unconfirmed_doubt(("uhf-172e",), [calibrated]) == ()
        # confirmed but (here, by force) not widened: no hold either, it would never end
        monkeypatch.setattr(tasking, "ephemeris_allowance", lambda: {})
        assert tasking._unconfirmed_doubt(("uhf-172e",), [calibrated]) == ()
        tasking.reconfigure(with_processing(CONFIG, calibration={"ephemeris_allowance_max_scale": 1.0}))
        tasking._update_allowance({"muos-pac"})
        assert tasking._unconfirmed_doubt(("muos-pac",), [plain]) == ()


async def test_a_clock_fault_never_widens_an_ephemeris() -> None:
    """D1: Naha's clock is 10 us late (clock-fault). The clock checks blame the clock, name
    no satellite, and every solve uses the configured Uncertainty object unchanged; the
    network reads no satellite error either (the clocks are free in that fit)."""
    async with environment(CONFIG, CLOCK_FAULT) as env:
        record = make_event(env)
        await rounds(env, record, 10)
        estimates = {e.site_id: e for e in env.tasking.site_clock_estimates()}
        assert estimates["naha"].caution
        assert not any(e.suspect_satellite_ids for e in estimates.values())
        assert record.doubted_satellite_ids == () and record.solution_allowance == ()
        assert env.tasking.ephemeris_allowance() == {}
        solves = [args[0] for fn, args in env.pool.jobs if fn is tasking_module._solve]
        assert solves and all(job.uncertainty is env.tasking._uncertainty for job in solves)
        evidence = env.tasking.satellite_position_evidence()
        assert evidence and all(e.norm < 2.0 for e in evidence.values()), evidence
        assert all(c.position_partials for c in env.tasking.calibrations() if c.key[4] == "tdoa")


def same_uncertainty(a: Any, b: Any) -> bool:
    """Whether two ``Uncertainty`` objects hold exactly the same values (every field;
    per-satellite arrays compared element by element, no tolerance)."""
    import dataclasses

    for f in dataclasses.fields(a):
        x, y = getattr(a, f.name), getattr(b, f.name)
        if isinstance(x, dict):
            if x.keys() != y.keys():
                return False
            for key in x:
                if not np.array_equal(np.asarray(x[key]), np.asarray(y[key])):
                    return False
        elif x != y:
            return False
    return True


@pytest.mark.parametrize("scenario", [DEMO, load_scenario(ROOT / "config" / "scenarios" / "two-jammers.yaml")],
                         ids=["pacific-demo", "two-jammers"])
async def test_with_nothing_in_doubt_every_solve_gets_the_configured_uncertainty_exactly(scenario: Scenario) -> None:
    """D1, no doubt no change: with no satellite named by the clock checks, every solve
    (warm, global and split alike) is handed the configured Uncertainty itself, equal
    value for value to ``config.resolved_uncertainty()``, no widening is worked out, the
    published solution carries no allowance and the history says nothing about one. (The
    working tree and a56a6f9 gave bit-identical solutions over 20 rounds of pacific-demo,
    two-jammers and clock-fault in this harness.)"""
    async with environment(CONFIG, scenario) as env:
        record = make_event(env)
        await rounds(env, record, 10)
        assert record.state == "located" and record.doubted_satellite_ids == ()
        assert env.tasking.ephemeris_allowance() == {}
        solves = [args[0] for fn, args in env.pool.jobs if fn is tasking_module._solve]
        assert len(solves) >= 6
        resolved = CONFIG.resolved_uncertainty()
        for job in solves:
            assert job.uncertainty is env.tasking._uncertainty
            assert same_uncertainty(job.uncertainty, resolved)
        assert not [fn for fn, _ in env.pool.jobs if fn is tasking_module._configured_semi_major]
        assert record.solution_allowance == () and record.solution_widening is None
        assert not any((t.reason or "").startswith(("ellipse widened", "ellipse allowance")) for t in record.transitions)
        assert "widened" not in (record.tasking.message or "")


class _ClockStep:
    """A SimWorld whose renders at one site's channels are ``shift_ns`` late: that
    site's clock stepped (the captures keep their timestamps), with no ``world_changed``."""

    def __init__(self, world: SimWorld, site_id: str) -> None:
        self._world = world
        self._site_id = site_id
        self.shift_ns = 0

    def render_iq(self, channel_id: str, t_ns: int, n: int, rate: float, fc: float) -> Any:
        site, _ = CONFIG.channel(channel_id)
        return self._world.render_iq(channel_id, t_ns + (self.shift_ns if site.id == self._site_id else 0), n, rate, fc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._world, name)


@pytest.mark.slow
async def test_a_clock_that_steps_mid_run_is_not_read_as_a_satellite_error_to_widen() -> None:
    """D1 entry hysteresis (pacific-demo, Naha's clock steps 10 us at the eighth round, as
    real hardware would: no world_changed, calibrations from before the step still count).
    While old and new calibrations mix, one round end saw Naha's views through its
    satellites disagree and named all three satellites; the next blamed the clock again.
    Sized at once, that one round widened all three satellites for the next 300 s (and
    the advice asked for their ephemerides to be checked). A doubt seen at one round end
    is not acted on: no allowance at any round, every solve keeps the configured
    uncertainty."""
    async with environment(CONFIG, DEMO) as env:
        stepped = _ClockStep(env.world, "naha")
        env.hub.world = stepped
        record = make_event(env)
        doubted_rounds: list[int] = []
        for i in range(18):
            if i == 8:
                stepped.shift_ns = 10_000
            await env.tasking.run_round(record.id)
            env.clock.advance(5.0)
            env.book.flush(force=True)
            if record.doubted_satellite_ids:
                doubted_rounds.append(i)
            assert env.tasking.ephemeris_allowance() == {}, i
        assert doubted_rounds == [16]  # the lone round end that named them
        estimates = {e.site_id: e for e in env.tasking.site_clock_estimates()}
        assert estimates["naha"].caution
        solves = [args[0] for fn, args in env.pool.jobs if fn is tasking_module._solve]
        assert solves and all(job.uncertainty is env.tasking._uncertainty for job in solves)
        assert record.solution_allowance == ()


async def test_a_widened_ellipse_faces_the_fix_gate_and_a_new_allowance_is_solved_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1 (pacific-demo, allowance forced): the fix gate judges the widened ellipse (here
    against a 0.8 km bound: the event goes back to locating, and is located again when the
    allowance ends); a change of the allowance in force re-solves the event in the next
    round even when it brings no new measurement; the widening is the widened semi-major
    axis over the configured uncertainty's at the same position; the status message and
    the advice keep the warning for an allowance at the largest scale."""
    from petal_locate.core.solver import position_covariance
    from petal_locate.server.pipeline.events import fix_semi_major_limit_m

    forced: dict[str, tuple[float, bool]] = {}
    async with environment(CONFIG, DEMO) as env:
        tasking = env.tasking
        monkeypatch.setattr(tasking, "ephemeris_allowance", lambda: dict(forced))
        monkeypatch.setattr(tasking_module, "fix_semi_major_limit_m", lambda region: 800.0)
        assert fix_semi_major_limit_m(CONFIG.processing.region) > 800.0
        record = make_event(env)
        await rounds(env, record, 6)
        assert record.state == "located" and record.solution_out.ellipse.semi_major_m < 800.0
        # every satellite widened 20 times (capped), with no capture succeeding: solved anyway
        forced.update({s: (20.0, True) for s in ("muos-pac", "uhf-172e", "muos-io")})
        env.hub.fail_all = "offline"
        before = len([1 for fn, _ in env.pool.jobs if fn is tasking_module._solve])
        await rounds(env, record, 1)
        solves = [args[0] for fn, args in env.pool.jobs if fn is tasking_module._solve]
        assert len(solves) > before
        job = solves[-1]
        configured = CONFIG.resolved_uncertainty()
        for sat in ("muos-pac", "uhf-172e", "muos-io"):
            assert job.uncertainty.sat_pos_ric_m[sat] == pytest.approx(configured.sat_pos_ric_m[sat] * 20.0)
        assert [(a.sat_id, a.scale, a.capped) for a in record.solution_allowance] == [
            ("muos-io", 20.0, True), ("muos-pac", 20.0, True), ("uhf-172e", 20.0, True)]
        solution = record.solution
        plain = position_covariance(list(job.measurements), solution.lat_deg, solution.lon_deg, tasking._orbits,
                                    tasking._sites_xyz, 0.0, configured, 0.95)
        assert record.solution_widening == pytest.approx(solution.ellipse["semi_major_m"]
                                                         / plain["ellipse"]["semi_major_m"], rel=1e-9)
        assert solution.ellipse["semi_major_m"] > 800.0 and record.solution_widening > 2.0
        assert record.state == "locating" and not record.solution_out.is_fix
        reasons = [t.reason or "" for t in record.transitions]
        assert any(r.startswith("position withdrawn: the position is not a fix") for r in reasons)
        assert any(r.startswith("ellipse widened: the reference calibrations put the positions of muos-io, muos-pac "
                                "and uhf-172e wrong") and "at the largest allowed scale" in r for r in reasons)
        message = record.tasking.message
        assert "The ellipse is widened for the positions of muos-io (20 times its configured sigma, about 20 km, the " \
               "largest scale allowed)" in message
        assert "The allowance may still be too small: the transmitter may lie outside the ellipse." in message
        # the same allowance again, still no capture: nothing more to solve
        before = len([1 for fn, _ in env.pool.jobs if fn is tasking_module._solve])
        await rounds(env, record, 1)
        assert len([1 for fn, _ in env.pool.jobs if fn is tasking_module._solve]) == before
        # the allowance ends: re-solved with the configured uncertainty, located again
        forced.clear()
        await rounds(env, record, 1)
        job = [args[0] for fn, args in env.pool.jobs if fn is tasking_module._solve][-1]
        assert job.uncertainty is tasking._uncertainty
        assert record.solution_allowance == () and record.solution_widening is None
        assert record.state == "located"
        assert record.transitions[-1].reason.startswith("ellipse allowance for the positions of muos-io, muos-pac and "
                                                        "uhf-172e removed")


def test_satellite_partials_that_cannot_be_worked_out_cost_only_the_evidence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D1: the satellite partials worked out with each TDOA calibration are optional
    evidence. A predictor's ValueError (numpy's LinAlgError and the propagators' errors
    are ValueErrors) or an arithmetic error leaves the calibration with empty partials
    and a warning; any other exception is a bug and is not swallowed (it fails the
    calibration job, where it is logged)."""
    from petal_locate.core.types import CalRef

    x_ref = CONFIG.carrier_xyz("ref-guam")
    cal = CalRef("c-1", x_ref, PathSpec("muos-pac", "tokyo", 301e6, -41e6), PathSpec("uhf-172e", "naha", 301e6, -33.6e6),
                 T0, 1e-7)
    orbits, sites = CONFIG.orbits(), CONFIG.sites_xyz()
    position, media = tasking_module._reference_partials("tdoa", cal, orbits, sites)
    assert {s for s, _ in position} == {"muos-pac", "uhf-172e"} and media
    assert tasking_module._reference_partials("fdoa", cal, orbits, sites) == ((), ())
    for error in (ValueError("orbit propagation failed"), np.linalg.LinAlgError("singular"), FloatingPointError("x")):
        def broken(*args: Any, error: Exception = error) -> Any:
            raise error

        monkeypatch.setattr(tasking_module, "reference_position_partials", broken)
        caplog.clear()
        assert tasking_module._reference_partials("tdoa", cal, orbits, sites) == ((), ())
        assert any("calibration c-1: satellite partials not worked out" in r.getMessage() for r in caplog.records)

    def bug(*args: Any) -> Any:
        raise TypeError("a bug")

    monkeypatch.setattr(tasking_module, "reference_position_partials", bug)
    with pytest.raises(TypeError):
        tasking_module._reference_partials("tdoa", cal, orbits, sites)


def test_the_evidence_works_for_any_satellite_set_leo_and_a_single_satellite_included() -> None:
    """D1, any constellation: the evidence fit and the scale take whatever satellites,
    sites and orbits the configuration has. Two low-Earth-orbit satellites (the shipped
    leo-example orbits, 800 km) over four sites 400 km apart, leo-a 3 km off in-track
    against an operator-grade 100 m / 1 km / 1 km: its error comes out and its scale
    covers it, leo-b stays near zero. A single satellite (clock checks only, no
    cross-satellite pair) gives finite evidence and no crash; so do one edge, and edges
    naming a satellite with no configured sigma."""
    from petal_locate.core.geodesy import lla_to_ecef
    from petal_locate.core.orbits import PerturbedOrbit
    from petal_locate.core.solver import reference_position_partials
    from petal_locate.core.types import CalRef
    from petal_locate.server.pipeline.tasking import EvidenceEdge, ephemeris_scale, satellite_position_evidence

    leo = load_config(ROOT / "config" / "leo-example.yaml")
    orbits = leo.orbits()
    t_ns = ns_from_iso("2026-01-01T00:02:00Z")
    t_s = t_ns / S
    site = leo.sites[0]
    lat0, lon0 = float(site.lat_deg), float(site.lon_deg)
    sites = {f"s{i}": lla_to_ecef(lat0 + dlat, lon0 + dlon, 0.0)
             for i, (dlat, dlon) in enumerate([(0.0, 0.0), (3.6, 0.0), (0.0, 4.5), (-3.6, -4.5)])}
    x_ref = lla_to_ecef(lat0 + 1.0, lon0 + 1.5, 0.0)
    truth = dict(orbits)
    truth["leo-a"] = PerturbedOrbit(orbits["leo-a"], np.array([0.0, 3000.0, 0.0]), np.zeros(3), t_s)
    rng = np.random.default_rng(3)
    sigma = {s: np.array([100.0, 1000.0, 1000.0]) for s in orbits}

    def edges_for(pairs: list[tuple[str, str, str, str]]) -> list[EvidenceEdge]:
        out = []
        for k, (sa, a, sb, b) in enumerate(pairs):
            cal = CalRef(f"c{k}", x_ref, PathSpec(sa, a, 435.1e6, -289.2e6), PathSpec(sb, b, 435.1e6, -289.2e6),
                         t_ns + k * S, 30e-9)
            tk = cal.t_ns / S
            c = float(predict_pair(x_ref, cal.a, cal.b, tk, truth, sites)[0]
                      - predict_pair(x_ref, cal.a, cal.b, tk, orbits, sites)[0]) + float(rng.normal(0.0, 30e-9))
            partials = reference_position_partials("tdoa", cal, orbits, sites)
            out.append(EvidenceEdge(sa, a, sb, b, c, 30e-9, tuple((s, tuple(p)) for s, p in partials.items())))
        return out

    names = sorted(sites)
    checks = [(sat, "s0", sat, other) for sat in orbits for other in names[1:]]
    cross = [("leo-a", a, "leo-b", b) for a in names for b in names if a != b]
    evidence = satellite_position_evidence(edges_for(checks + cross), sigma)
    assert set(evidence) == {"leo-a", "leo-b"}
    assert evidence["leo-a"].error_ric_m[1] == pytest.approx(3000.0, rel=0.25)
    scale, capped = ephemeris_scale(evidence["leo-a"], 20.0)
    assert not capped and scale * sigma["leo-a"][1] >= 3000.0
    assert evidence["leo-b"].norm < 1.5
    single = satellite_position_evidence(edges_for([("leo-a", "s0", "leo-a", other) for other in names[1:]]), sigma)
    assert set(single) == {"leo-a"} and all(math.isfinite(v) for v in (single["leo-a"].norm, single["leo-a"].norm_sigma))
    one = satellite_position_evidence(edges_for([("leo-a", "s0", "leo-b", "s1")]), sigma)
    assert set(one) == {"leo-a", "leo-b"} and all(e.norm_sigma <= 1.0 + 1e-9 for e in one.values())
    unknown = satellite_position_evidence(edges_for(checks), {})
    assert all(e.norm == 0.0 and e.error_ric_m == (0.0, 0.0, 0.0) for e in unknown.values())


async def test_a_failed_solve_over_more_rows_withdraws_the_published_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """R6: when the rows a published position rests on, with the newest added, admit no
    position at all, that position no longer fits what is measured: it is withdrawn (the
    event goes back to locating and says why) instead of staying published with only
    last_solve_reason set."""
    async with environment() as env:
        record = make_event(env)
        await rounds(env, record, 3)
        assert record.state == "located" and record.solution_out is not None
        before = set(record.solution_used)

        def no_position(job: Any) -> tuple[None, str]:
            return None, "no admissible solution in the region"

        monkeypatch.setattr(tasking_module, "_solve", no_position)
        reports = await rounds(env, record, 2)  # the first may be a rotation round (nothing new confirmed)
        assert any(r.solve_reason == "no admissible solution in the region" for r in reports)
        assert before <= {e.seq for e in record.used_measurements()}  # the rows stay used
        assert record.solution_out is None and record.solution is None and record.state == "locating"
        reason = record.transitions[-1].reason
        assert reason.startswith("position withdrawn: the newest measurements, added to the")
        assert "admit no position (no admissible solution in the region)" in reason
        assert all(e.row.residual is None for e in record.measurements)
        assert env.book.to_schema(record).solution is None
        assert env.tasking.counters["solutions_withdrawn"] == 1


async def test_a_failed_solve_withdraws_the_position_even_past_the_solver_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6 with more rows than solver.max_measurements: the solver sees only the newest N,
    so the rows the published position rests on are no longer all among the rows solved
    (the oldest slid out of the window). The newest rows admitting no position still
    means the published position no longer fits what is measured: it is withdrawn."""
    solver = CONFIG.processing.solver.model_copy(update={"max_measurements": 6})
    config = CONFIG.model_copy(
        update={"processing": CONFIG.processing.model_copy(update={"solver": solver})}
    )
    async with environment(config) as env:
        record = make_event(env)
        await rounds(env, record, 4)
        assert record.state == "located" and record.solution_out is not None
        published = set(record.solution_used)
        assert len(record.used_measurements()) > 6 and len(published) == 6

        def no_position(job: Any) -> tuple[None, str]:
            return None, "no admissible solution in the region"

        monkeypatch.setattr(tasking_module, "_solve", no_position)
        reports = await rounds(env, record, 2)
        assert any(r.solve_reason == "no admissible solution in the region" for r in reports)
        assert published <= {e.seq for e in record.used_measurements()}  # the rows stay used
        assert record.solution_out is None and record.state == "locating"
        reason = record.transitions[-1].reason
        assert reason.startswith("position withdrawn: the newest 6 measurements (the solver window)")
        assert "admit no position (no admissible solution in the region)" in reason
        assert env.tasking.counters["solutions_withdrawn"] == 1


def test_only_a_redundant_consistent_fit_shows_its_rows_fit_one_transmitter() -> None:
    """R4: the K2 split test sets the earlier rows aside as another transmitter's when
    rows added to a fit break it. Two rows for two unknowns (dof 0) always 'fit', so a
    break after them says nothing about another transmitter: only a consistent fit with
    redundancy counts."""
    fix = hawaii_fix(CONFIG)
    assert Tasking._fitted_one_transmitter(fix)
    assert not Tasking._fitted_one_transmitter(fix.model_copy(update={"dof": 0, "n_measurements": 2}))
    assert not Tasking._fitted_one_transmitter(fix.model_copy(update={"fit_consistent": False}))
    assert not Tasking._fitted_one_transmitter(None)
