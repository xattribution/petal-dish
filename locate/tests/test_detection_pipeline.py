"""Tests of the detection pipeline (ARCHITECTURE.md section 7, ``pipeline/detection.py``).

Spectrum frames are made the way a node makes them: ``core.spectrum.welch_psd_db`` of
generated IQ (unit thermal noise, relayed noise in the transponder passband at the
rise the link budget predicts, and band-limited noise signals), wrapped in the hub's
``PsdFrame``. The last test runs the whole chain on the demo scenario with
``SimWorld.render_psd`` frames: detection, correlation and an uplink verdict.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from petal_locate.config import SystemConfig, load_config
from petal_locate.core.linkbudget import relay_rise_db
from petal_locate.core.spectrum import welch_psd_db
from petal_locate.core.timebase import ns_from_iso
from petal_locate.protocol.framing import decode_console_psd, decode_frame
from petal_locate.protocol.messages import PsdHeader
from petal_locate.server.hub import PsdFrame
from petal_locate.server.pipeline.correlator import Correlator, QualityTable
from petal_locate.server.pipeline.detection import (
    DetectionPipeline,
    WindowReport,
    WindowSpec,
    build_trackers,
    build_window_specs,
)
from petal_locate.server.pipeline.events import EventBook
from petal_locate.server.runtime import ConsoleBus, WorkPool

ROOT = Path(__file__).resolve().parents[1]
T0 = ns_from_iso("2026-11-13T00:00:00Z")
FRAME_NS = 500_000_000
SHIFT = -40.0e6
KEY = ("alpha-a", "sat-a:t1")


def make_config(threshold_db: float = 6.0, shift: float = SHIFT, tau_s: float = 60.0) -> SystemConfig:
    tp = {
        "id": "t1",
        "uplink_center_hz": 300.0e6,
        "bandwidth_hz": 25e3,
        "translation_hz": shift,
        "g_over_t_dbk": -14.0,
        "sfd_dbw_m2": -136.0,
        "eirp_sat_dbw": 26.0,
    }
    dish = {"type": "parabolic", "diameter_m": 1.2}
    return SystemConfig.model_validate(
        {
            "name": "detection-test",
            "satellites": [{"id": "sat-a", "name": "Sat A", "orbit": {"type": "geo", "lon_deg": 140.0}, "transponders": [tp]}],
            "sites": [
                {"id": "alpha", "name": "Alpha", "lat_deg": 35.0, "lon_deg": 139.0,
                 "channels": [{"id": "alpha-a", "antenna": dish, "pointing": {"satellite_id": "sat-a"}}]},
                {"id": "far", "name": "Far", "lat_deg": 17.0, "lon_deg": 139.0,
                 "channels": [{"id": "far-a", "antenna": dish, "pointing": {"satellite_id": "sat-a"}}]},
                {"id": "antipode", "name": "Antipode", "lat_deg": 0.0, "lon_deg": -40.0,
                 "channels": [{"id": "anti-a", "antenna": dish,
                               "pointing": {"az_deg": 0.0, "el_deg": 45.0, "satellite_ids": ["sat-a"]}}]},
            ],
            "carriers": [
                {"id": "ref-1", "name": "Reference 1", "satellite_id": "sat-a", "transponder_id": "t1",
                 "uplink_center_hz": 299.9925e6, "bandwidth_hz": 5e3}
            ],
            "processing": {"detection": {"threshold_db": threshold_db, "baseline_tau_s": tau_s}},
        }
    )


@dataclass
class Sink:
    """Records what the pipeline hands to the correlator."""

    reports: list[WindowReport] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    def update(self, report: WindowReport) -> None:
        self.reports.append(report)

    def drop_window(self, key: tuple[str, str]) -> None:
        self.dropped.append(key)


class FrameMaker:
    """Welch PSD frames of generated IQ for one window, the way a node computes them."""

    def __init__(self, spec: WindowSpec, seed: int = 1) -> None:
        self.spec = spec
        self.rng = np.random.default_rng(seed)
        self.n = spec.nfft * spec.navg
        self.freq = np.fft.fftfreq(self.n, d=1.0 / spec.span_hz) + spec.center_hz
        self.seq = 0

    def _band(self, lo: float, hi: float, level_db: float) -> np.ndarray:
        white = (self.rng.standard_normal(self.n) + 1j * self.rng.standard_normal(self.n)) / math.sqrt(2.0)
        spectrum = np.fft.fft(white)
        spectrum[(self.freq < lo) | (self.freq > hi)] = 0.0
        return np.fft.ifft(spectrum) * math.sqrt(10.0 ** (level_db / 10.0))

    def psd(self, signals: Iterable[tuple[float, float, float]] = ()) -> np.ndarray:
        """signals: (downlink lo Hz, hi Hz, level dB above the thermal floor)."""
        x = (self.rng.standard_normal(self.n) + 1j * self.rng.standard_normal(self.n)) / math.sqrt(2.0)
        for lo, hi, rise in self.spec.passbands:
            if rise > 0:
                x = x + self._band(lo, hi, 10.0 * math.log10(10.0 ** (rise / 10.0) - 1.0))
        for lo, hi, level in signals:
            x = x + self._band(lo, hi, level)
        return welch_psd_db(x.astype(np.complex64), self.spec.span_hz, self.spec.nfft, self.spec.navg)

    def frame(self, k: int, signals: Iterable[tuple[float, float, float]] = (), *, t_ns: int | None = None,
              center_hz: float | None = None, psd: np.ndarray | None = None) -> PsdFrame:
        spec = self.spec
        data = self.psd(signals) if psd is None else psd
        header = PsdHeader(
            channel_id=spec.channel_id,
            monitor_id=spec.monitor_id,
            t_ns=T0 + k * FRAME_NS if t_ns is None else t_ns,
            center_hz=spec.center_hz if center_hz is None else center_hz,
            span_hz=spec.span_hz,
            nfft=len(data),
            navg=spec.navg,
            seq=self.seq,
        )
        self.seq += 1
        return PsdFrame(site_id=spec.site_id, header=header, psd_db=data, received_ns=header.t_ns)


SIGNAL = (259.9975e6, 260.0025e6, 10.0)  # 5 kHz of noise 10 dB above the floor, uplink 300.000 MHz


def _pipeline(cfg: SystemConfig, sink: Any, **kw: Any) -> tuple[DetectionPipeline, dict[tuple[str, str], WindowSpec]]:
    specs = build_window_specs(cfg, T0 / 1e9)
    pipeline = DetectionPipeline(sink=sink, **kw)
    pipeline.install(cfg.processing.detection, specs)
    return pipeline, specs


def test_window_specs_are_built_as_the_detect_engineer_specified() -> None:
    cfg = make_config()
    t_s = T0 / 1e9
    specs = build_window_specs(cfg, t_s)
    assert set(specs) == {("alpha-a", "sat-a:t1"), ("far-a", "sat-a:t1"), ("anti-a", "sat-a:t1")}
    spec = specs[KEY]
    monitor = cfg.channel("alpha-a")[1].monitors[0]
    assert (spec.center_hz, spec.span_hz, spec.nfft, spec.navg, spec.frame_rate_hz) == (
        monitor.center_hz, monitor.span_hz, monitor.nfft, monitor.navg, monitor.frame_rate_hz)
    (lo, hi, rise), = spec.passbands
    assert (lo, hi) == (259.9875e6, 260.0125e6)  # the downlink passband
    expected = relay_rise_db(cfg, "alpha-a", "sat-a", "t1", t_s)
    assert expected is not None and rise == pytest.approx(expected) and rise > 0
    assert spec.carriers == (("ref-1", 299.990e6 + SHIFT, 299.995e6 + SHIFT),)
    # The satellite is below the antipode's horizon: no relayed noise reaches it.
    assert relay_rise_db(cfg, "anti-a", "sat-a", "t1", t_s) is None
    assert specs[("anti-a", "sat-a:t1")].passbands[0][2] == 0.0
    assert spec.frame_period_s == 0.5 and spec.contains(260.0e6) and not spec.contains(261e6)


def test_detections_are_raised_after_confirmation_and_released_after_the_misses() -> None:
    cfg = make_config()
    sink = Sink()
    pipeline, specs = _pipeline(cfg, sink)
    maker = FrameMaker(specs[KEY])

    async def run() -> None:
        for k in range(10):
            await pipeline.on_psd(maker.frame(k))
        for k in range(10, 20):
            await pipeline.on_psd(maker.frame(k, [SIGNAL]))
        for k in range(20, 30):
            await pipeline.on_psd(maker.frame(k))

    asyncio.run(run())
    per_frame = [len(r.detections) for r in sink.reports]
    assert per_frame[:10] == [0] * 10  # noise alone never raises a detection
    first = per_frame.index(1)
    assert first == 12  # confirmed in 3 of the last 5 frames (confirm_m)
    assert all(n == 1 for n in per_frame[12:20])
    # held for release_frames - 1 frames after the signal ends, then released
    assert per_frame[20:25] == [1] * 5 and per_frame[25:] == [0] * 5
    generation, detection = sink.reports[15].detections[0]
    assert generation == 1 and detection.authorized_carrier_id is None and not detection.anomaly
    bin_hz = specs[KEY].bin_hz
    assert abs(detection.f_lo_hz - SIGNAL[0]) < 3 * bin_hz and abs(detection.f_hi_hz - SIGNAL[1]) < 3 * bin_hz
    assert detection.snr_db == pytest.approx(10.0 * math.log10(1.0 + 10.0 / 10 ** (specs[KEY].passbands[0][2] / 10)) + 0.0, abs=1.5)
    report = sink.reports[15]
    assert report.t_ns == T0 + 15 * FRAME_NS and report.received_ns == report.t_ns
    assert report.threshold_db == pytest.approx(6.0)
    assert pipeline.counters()["accepted"] == 30


def test_bad_frames_are_dropped_and_counted() -> None:
    cfg = make_config()
    sink = Sink()
    pipeline, specs = _pipeline(cfg, sink)
    maker = FrameMaker(specs[KEY])
    nan = maker.psd().copy()
    nan[17] = np.nan
    other = FrameMaker(specs[("far-a", "sat-a:t1")])

    async def run() -> None:
        await pipeline.on_psd(maker.frame(5))
        await pipeline.on_psd(maker.frame(5))  # repeated t_ns
        await pipeline.on_psd(maker.frame(4))  # out of order
        await pipeline.on_psd(maker.frame(6, psd=nan))  # a bin is not finite
        await pipeline.on_psd(maker.frame(7, psd=maker.psd()[:512]))  # wrong nfft
        await pipeline.on_psd(maker.frame(8, center_hz=specs[KEY].center_hz + 1000.0))  # retuned
        await pipeline.on_psd(maker.frame(9, center_hz=specs[KEY].center_hz + 5.0))  # a fraction of a bin: fine
        ghost = maker.frame(10)
        unknown = PsdFrame(site_id="x", header=ghost.header.model_copy(update={"monitor_id": "nope"}),
                           psd_db=ghost.psd_db, received_ns=ghost.received_ns)
        await pipeline.on_psd(unknown)
        await pipeline.on_psd(other.frame(1))

    asyncio.run(run())
    counters = pipeline.counters()
    assert counters["repeated"] == 1
    assert counters["out_of_order"] == 1
    assert counters["non_finite"] == 1
    assert counters["nfft_mismatch"] == 1
    assert counters["retuned"] == 1
    assert counters["unknown_window"] == 1
    assert counters["accepted"] == 3
    assert [r.spec.key for r in sink.reports] == [KEY, KEY, ("far-a", "sat-a:t1")]


def test_a_tracker_that_cannot_be_built_is_logged_and_its_frames_dropped(caplog: pytest.LogCaptureFixture) -> None:
    cfg = make_config()
    specs = build_window_specs(cfg, T0 / 1e9)
    tiny = {KEY: WindowSpec(**{**specs[KEY].__dict__, "nfft": 8})}
    sink = Sink()
    pipeline = DetectionPipeline(sink=sink)
    pipeline.install(cfg.processing.detection, tiny)
    assert pipeline.tracker(KEY) is None
    assert any("cannot be built" in r.getMessage() for r in caplog.records)
    frame = FrameMaker(specs[KEY]).frame(1, psd=np.zeros(8, dtype=np.float32))
    asyncio.run(pipeline.on_psd(frame))
    assert pipeline.counters() == {"no_tracker": 1}


def test_console_frames_carry_psd_baseline_and_detections_for_subscribed_windows_only() -> None:
    cfg = make_config()
    sink = Sink()
    bus = ConsoleBus()
    pipeline, specs = _pipeline(cfg, sink, console=bus)
    maker = FrameMaker(specs[KEY])
    far = FrameMaker(specs[("far-a", "sat-a:t1")])

    async def run() -> list[bytes]:
        subscription = bus.subscribe(psd=[KEY])
        for k in range(16):
            await pipeline.on_psd(maker.frame(k, [SIGNAL] if k >= 3 else []))
            await pipeline.on_psd(far.frame(k))
        frames = []
        while (item := subscription.get_nowait()) is not None:
            frames.append(item)
        subscription.close()
        return frames  # type: ignore[return-value]

    frames = asyncio.run(run())
    assert len(frames) == 1  # newest wins: the client did not read while the frames came
    header, payload = decode_frame(frames[0])
    assert isinstance(header["t_ns"], str) and header["t_ns"] == str(T0 + 15 * FRAME_NS)
    console_header, psd, baseline = decode_console_psd(header, payload)
    assert console_header.channel_id == "alpha-a" and console_header.seq == 15
    assert console_header.has_baseline and baseline is not None and np.isfinite(baseline).all()
    assert psd.shape == (specs[KEY].nfft,)
    assert len(console_header.detections) == 1
    assert console_header.detections[0].authorized_carrier_id is None
    assert pipeline.counters()["console_frames"] == 16  # only the subscribed window was encoded


def test_reinstall_keeps_unchanged_trackers_rebuilds_changed_ones_and_carries_detections() -> None:
    cfg = make_config()
    sink = Sink()
    pipeline, specs = _pipeline(cfg, sink)
    maker = FrameMaker(specs[KEY])
    far_key = ("far-a", "sat-a:t1")
    kept_far = pipeline.tracker(far_key)

    async def run() -> None:
        for k in range(8):
            await pipeline.on_psd(maker.frame(k, [SIGNAL]))
        assert len(sink.reports[-1].detections) == 1
        # alpha-a's inputs change (its carrier list) and anti-a leaves the configuration.
        changed = dict(specs)
        changed[KEY] = WindowSpec(**{**specs[KEY].__dict__, "carriers": ()})
        del changed[("anti-a", "sat-a:t1")]
        assert [s.key for s in pipeline.needs_tracker(cfg.processing.detection, changed)] == [KEY]
        prebuilt = build_trackers(cfg.processing.detection, [changed[KEY]])
        pipeline.install(cfg.processing.detection, changed, prebuilt)
        assert pipeline.tracker(far_key) is kept_far
        assert pipeline.generation(KEY) == 2 and pipeline.tracker(KEY) is prebuilt[KEY]
        assert sink.dropped == [("anti-a", "sat-a:t1")]
        for k in range(8, 16):
            await pipeline.on_psd(maker.frame(k, [SIGNAL]))

    asyncio.run(run())
    generations = [sorted({g for g, _ in r.detections}) for r in sink.reports[8:16]]
    # The old tracker's detection is carried for confirm_n = 5 frames while the new
    # tracker confirms (3 frames), so the window is never without its detection.
    assert generations[:2] == [[1], [1]]
    assert generations[2:5] == [[1, 2]] * 3
    assert generations[5:] == [[2]] * 3
    # A different detection setting rebuilds every tracker (anti-a, removed above, is new again).
    louder = make_config(threshold_db=8.0)
    assert len(pipeline.needs_tracker(louder.processing.detection, build_window_specs(louder, T0 / 1e9))) == 3
    assert pipeline.needs_tracker(cfg.processing.detection, {far_key: specs[far_key]}) == []


def test_authorised_carrier_is_reported_only_when_anomalous_and_then_raises_an_event() -> None:
    cfg = make_config(tau_s=60.0)
    book = EventBook(clock=lambda: clock[0])
    clock = [T0]
    specs = build_window_specs(cfg, T0 / 1e9)
    correlator = Correlator(cfg, book, windows=specs, quality=QualityTable.build(cfg, T0 / 1e9), clock=lambda: clock[0])
    pipeline = DetectionPipeline(sink=correlator)
    pipeline.install(cfg.processing.detection, specs)
    maker = FrameMaker(specs[KEY], seed=3)
    carrier = (299.990e6 + SHIFT, 299.995e6 + SHIFT)
    reports: list[WindowReport] = []
    original = correlator.update

    def spy(report: WindowReport) -> None:
        reports.append(report)
        original(report)

    pipeline._sink = type("S", (), {"update": staticmethod(spy), "drop_window": correlator.drop_window})()  # type: ignore[assignment]

    async def run() -> None:
        for k in range(90):  # the carrier at its normal level, from start-up on
            clock[0] = T0 + k * FRAME_NS
            await pipeline.on_psd(maker.frame(k, [(*carrier, 8.0)]))
        assert all(not r.detections for r in reports)
        assert book.records() == []
        for k in range(90, 100):  # the carrier jumps 10 dB: an anomaly
            clock[0] = T0 + k * FRAME_NS
            await pipeline.on_psd(maker.frame(k, [(*carrier, 18.0)]))

    asyncio.run(run())
    flagged = [d for r in reports for _, d in r.detections]
    assert flagged and all(d.authorized_carrier_id == "ref-1" and d.anomaly for d in flagged)
    (record,) = book.records()
    assert record.uplink_lo_hz == pytest.approx(299.990e6, abs=200) and record.state == "detected"


def test_update_cost_is_measured_and_costly_windows_move_to_the_worker_pool() -> None:
    cfg = make_config()

    async def run(pool: WorkPool | None, offload_cost_s: float) -> tuple[list[int], DetectionPipeline]:
        sink = Sink()
        pipeline, specs = _pipeline(cfg, sink, pool=pool, offload_cost_s=offload_cost_s)
        maker = FrameMaker(specs[KEY], seed=7)
        for k in range(20):
            await pipeline.on_psd(maker.frame(k, [SIGNAL] if 5 <= k < 15 else []))
        return [len(r.detections) for r in sink.reports], pipeline

    async def both() -> tuple[list[int], list[int], DetectionPipeline, DetectionPipeline]:
        inline, p1 = await run(None, 0.0)
        pool = WorkPool(2)
        try:
            offloaded, p2 = await run(pool, 0.0)
        finally:
            await pool.close()
        return inline, offloaded, p1, p2

    inline, offloaded, p1, p2 = asyncio.run(both())
    assert inline == offloaded and max(inline) == 1
    assert not p1.offloaded(KEY) and p2.offloaded(KEY)
    stats = p1.cost_stats()
    assert stats["count"] == 20.0
    assert stats["median_s"] < 0.02  # about 1 ms for 1024 bins; loose for a shared machine


def test_demo_scenario_frames_from_the_simulator_give_an_uplink_event() -> None:
    from petal_locate.sim.scenario import load_scenario
    from petal_locate.sim.world import SimWorld

    cfg = load_config(ROOT / "config" / "pacific-uhf.yaml")
    scenario = load_scenario(ROOT / "config" / "scenarios" / "pacific-demo.yaml")
    world = SimWorld(cfg, scenario)
    clock = [T0]
    book = EventBook(clock=lambda: clock[0])
    specs = build_window_specs(cfg, T0 / 1e9)
    shared = {k: s for k, s in specs.items() if k[1].endswith(":shared-1")}
    correlator = Correlator(cfg, book, windows=specs, quality=QualityTable.build(cfg, T0 / 1e9), clock=lambda: clock[0])
    pipeline = DetectionPipeline(sink=correlator)
    pipeline.install(cfg.processing.detection, specs)

    async def run() -> None:
        for k in range(14):
            clock[0] = T0 + k * FRAME_NS
            for key, spec in shared.items():
                psd = world.render_psd(spec.channel_id, clock[0], spec.center_hz, spec.span_hz, spec.nfft, spec.navg)
                header = PsdHeader(channel_id=key[0], monitor_id=key[1], t_ns=clock[0], center_hz=spec.center_hz,
                                   span_hz=spec.span_hz, nfft=spec.nfft, navg=spec.navg, seq=k)
                await pipeline.on_psd(PsdFrame(site_id=spec.site_id, header=header, psd_db=psd, received_ns=clock[0]))
            correlator.tick()

    asyncio.run(run())
    active = book.active()
    assert len(active) == 1, [(r.id, r.lo_hz, r.hi_hz) for r in active]
    record = active[0]
    # The jammer radiates 10 kHz at 301.005 MHz.
    assert record.lo_hz == pytest.approx(301.000e6, abs=300) and record.hi_hz == pytest.approx(301.010e6, abs=300)
    assert record.satellite_ids == ["muos-pac", "uhf-172e", "muos-io"]
    assert record.verdict == "uplink"
    seen = {r.site_id for r in record.evidence if r.state == "seen"}
    assert {"honolulu", "tokyo", "manila"} <= seen
    assert pipeline.counters().get("accepted") == 14 * len(shared)
