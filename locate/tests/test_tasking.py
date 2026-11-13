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
from petal_locate.core.geometry import predict_pair
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
        assert len(env.store.calibrations) == 4
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
        assert len(env.tasking.calibrations()) == 4
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
