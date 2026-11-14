"""Tests of the correlator: event identity, evidence, capability and verdict (MATH.md section 7).

Evidence is constructed: detections are handed to the correlator as window reports,
exactly as the detection pipeline would after each frame, with a controlled clock.
The configuration is a small real one (two geostationary satellites relaying the same
uplink channel with different translations, sites 100 km and 2000 km apart, one site
with a deliberately poor antenna), so the capability rule runs on real link budgets.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

import pytest

from petal_locate.config import SystemConfig
from petal_locate.core.detect import Classification, Detection
from petal_locate.core.timebase import ns_from_iso
from petal_locate.core.types import Measurement, PathSpec
from petal_locate.server.api import schemas
from petal_locate.server.pipeline.advisory import advise
from petal_locate.server.pipeline.correlator import (
    Correlator,
    QualityTable,
    decide_verdict,
    site_distances_km,
)
from petal_locate.server.pipeline.detection import WindowReport, build_window_specs
from petal_locate.server.pipeline.events import EventBook, EventNotFound, Publication, caf_display

T0 = ns_from_iso("2026-11-13T00:00:00Z")
FRAME_NS = 500_000_000  # monitors run at 2 frames per second

UP = 300.0e6  # the shared uplink channel
SAT_A_SHIFT = -40.0e6  # sat-a translation: uplink 300.000 MHz -> downlink 260.000 MHz
SAT_B_SHIFT = -33.0e6  # sat-b translation: uplink 300.000 MHz -> downlink 267.000 MHz

DISH = {"type": "parabolic", "diameter_m": 2.4}
POOR = {"type": "isotropic", "gain_dbi": -10.0}


def _tp(tid: str, up: float, shift: float) -> dict[str, Any]:
    return {
        "id": tid,
        "uplink_center_hz": up,
        "bandwidth_hz": 25e3,
        "translation_hz": shift,
        "g_over_t_dbk": -14.0,
        "sfd_dbw_m2": -136.0,
        "eirp_sat_dbw": 26.0,
    }


SITES: dict[str, dict[str, Any]] = {
    "alpha": {
        "id": "alpha",
        "name": "Alpha",
        "lat_deg": 35.0,
        "lon_deg": 139.0,
        "channels": [
            {"id": "alpha-a", "antenna": DISH, "pointing": {"satellite_id": "sat-a"}},
            {"id": "alpha-b", "antenna": DISH, "pointing": {"satellite_id": "sat-b"}},
        ],
    },
    "near": {  # about 106 km from alpha
        "id": "near",
        "name": "Near",
        "lat_deg": 35.6,
        "lon_deg": 139.9,
        "channels": [{"id": "near-a", "antenna": DISH, "pointing": {"satellite_id": "sat-a"}}],
    },
    "far": {  # about 1994 km from alpha
        "id": "far",
        "name": "Far",
        "lat_deg": 17.0,
        "lon_deg": 139.0,
        "channels": [
            {"id": "far-a", "antenna": DISH, "pointing": {"satellite_id": "sat-a"}},
            {"id": "far-b", "antenna": DISH, "pointing": {"satellite_id": "sat-b"}},
        ],
    },
    "deaf": {  # about 2082 km from alpha, 30 dB less downlink quality than the dishes
        "id": "deaf",
        "name": "Deaf",
        "lat_deg": 17.0,
        "lon_deg": 145.0,
        "channels": [
            {"id": "deaf-a", "antenna": POOR, "pointing": {"satellite_id": "sat-a"}, "system_noise_temp_k": 5000}
        ],
    },
}


def make_config(sites: tuple[str, ...] = ("alpha", "near", "far", "deaf"), **detection: Any) -> SystemConfig:
    """The test system with the named sites."""
    return SystemConfig.model_validate(
        {
            "name": "correlator-test",
            "satellites": [
                {
                    "id": "sat-a",
                    "name": "Sat A",
                    "orbit": {"type": "geo", "lon_deg": 140.0},
                    "transponders": [_tp("t1", UP, SAT_A_SHIFT), _tp("solo", 300.5e6, SAT_A_SHIFT)],
                },
                {
                    "id": "sat-b",
                    "name": "Sat B",
                    "orbit": {"type": "geo", "lon_deg": 160.0},
                    "transponders": [_tp("t1", UP, SAT_B_SHIFT)],
                },
            ],
            "sites": [SITES[s] for s in sites],
            "processing": {"detection": detection} if detection else {},
        }
    )


class Clock:
    """Server clock under test control (integer ns)."""

    def __init__(self, t_ns: int) -> None:
        self.t = t_ns

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(round(seconds * 1e9))


def det(
    track_id: int,
    up_lo: float,
    up_hi: float,
    shift: float = SAT_A_SHIFT,
    *,
    cn0: float = 70.0,
    snr: float = 20.0,
    first_ns: int = T0,
    last_ns: int | None = None,
    carrier: str | None = None,
    anomaly: bool = False,
) -> Detection:
    """A detection of an uplink range as seen through a satellite with ``shift``."""
    return Detection(
        track_id=track_id,
        f_lo_hz=up_lo + shift,
        f_hi_hz=up_hi + shift,
        power_db=snr,
        snr_db=snr,
        cn0_dbhz=cn0,
        first_seen_ns=first_ns,
        last_seen_ns=last_ns if last_ns is not None else first_ns,
        authorized_carrier_id=carrier,
        anomaly=anomaly,
    )


@dataclass
class Harness:
    """A correlator with its event book, a controlled clock and captured publications."""

    config: SystemConfig
    clock: Clock = field(default_factory=lambda: Clock(T0))
    online: dict[str, bool] = field(default_factory=dict)
    published: list[Publication] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.book = EventBook(clock=self.clock, advisor=lambda r: advise(r, self.config))
        self.book.set_publisher(self.published.append)
        self.specs = build_window_specs(self.config, T0 / 1e9)
        self.quality = QualityTable.build(self.config, T0 / 1e9)
        self.corr = Correlator(
            self.config,
            self.book,
            windows=self.specs,
            quality=self.quality,
            is_online=lambda site: self.online.get(site, True),
            clock=self.clock,
        )

    def frame(self, sightings: dict[tuple[str, str], list[Detection]], *, skip: tuple[str, ...] = ()) -> None:
        """One frame of every window (except channels in ``skip``), with the given
        detections and nothing elsewhere."""
        for key, spec in self.specs.items():
            if key[0] in skip:
                continue
            dets = sightings.get(key, [])
            report = WindowReport(
                spec=spec,
                generation=1,
                t_ns=self.clock(),
                received_ns=self.clock(),
                detections=tuple((1, d) for d in dets),
                threshold_db=6.0,
            )
            self.corr.update(report)

    def run(self, seconds: float, sightings: dict[tuple[str, str], list[Detection]], **kw: Any) -> None:
        """Frames every 0.5 s for ``seconds``, with a correlator tick after each."""
        for _ in range(int(round(seconds / 0.5))):
            self.clock.advance(0.5)
            self.frame(sightings, **kw)
            self.corr.tick()

    def only(self) -> Any:
        records = [r for r in self.book.records() if not r.historic]
        assert len(records) == 1, [r.id for r in records]
        return records[0]

    def rows(self, record: Any) -> dict[tuple[str, str | None], schemas.EvidenceRow]:
        return {(r.channel_id, r.sat_id): r for r in record.evidence}


A1 = ("alpha-a", "sat-a:t1")
B1 = ("alpha-b", "sat-b:t1")
N1 = ("near-a", "sat-a:t1")
FA = ("far-a", "sat-a:t1")
FB = ("far-b", "sat-b:t1")
D1 = ("deaf-a", "sat-a:t1")


# --------------------------------------------------------------------- verdicts


def test_same_signal_through_two_satellites_at_distant_sites_is_uplink() -> None:
    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)], FB: [det(1, UP - 5e3, UP + 5e3, SAT_B_SHIFT)]}
    h.frame(sightings)
    record = h.only()
    assert record.state == "detected" and record.verdict == "undetermined"
    assert record.uplink_lo_hz == pytest.approx(UP - 5e3) and record.uplink_hi_hz == pytest.approx(UP + 5e3)
    assert record.satellite_ids == ["sat-a", "sat-b"]
    assert {(t.sat_id, t.transponder_id) for t in record.transponders} == {("sat-a", "t1"), ("sat-b", "t1")}
    h.run(4.5, sightings)
    assert record.verdict == "undetermined" and record.verdict_basis == "settling"
    h.run(1.0, sightings)
    assert record.verdict == "uplink" and record.verdict_basis == "two_sites"
    assert "Alpha" in record.verdict_reason and "Far" in record.verdict_reason
    rows = h.rows(record)
    assert rows[("alpha-a", "sat-a")].state == "seen"
    assert rows[("far-b", "sat-b")].state == "seen"
    assert rows[("far-b", "sat-b")].downlink_lo_hz == pytest.approx(UP - 5e3 + SAT_B_SHIFT)
    assert [t.verdict for t in record.transitions] == ["undetermined", "uplink"]
    # The verdict sticks when one of the two sightings drops out.
    h.run(2.0, {A1: sightings[A1]})
    assert record.verdict == "uplink" and record.verdict_basis == "kept"
    assert record.verdict_reason.startswith("It was seen at")
    document = h.book.to_schema(record)
    assert "Uplink interference" in document.advisory.headline


def test_seen_at_two_sites_on_one_satellite_is_uplink_but_one_site_on_two_satellites_is_not() -> None:
    h = Harness(make_config())
    one_site = {A1: [det(1, UP - 5e3, UP + 5e3)], B1: [det(1, UP - 5e3, UP + 5e3, SAT_B_SHIFT)]}
    h.frame(one_site)
    h.run(6.0, one_site)
    record = h.only()
    # Alpha sees it through both satellites; Far watches both and would see it, but does not.
    assert record.verdict == "local"
    h2 = Harness(make_config())
    two_sites = {A1: [det(1, UP - 5e3, UP + 5e3)], FA: [det(7, UP - 5e3, UP + 5e3)]}
    h2.frame(two_sites)
    h2.run(6.0, two_sites)
    assert h2.only().verdict == "uplink"


def test_local_when_a_capable_site_2000_km_away_on_the_same_satellite_does_not_see_it() -> None:
    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3, cn0=70.0)]}
    h.frame(sightings)
    h.run(6.0, sightings)
    record = h.only()
    assert record.verdict == "local" and record.verdict_basis == "capable_not_seen"
    rows = h.rows(record)
    seen = rows[("alpha-a", "sat-a")]
    assert seen.state == "seen" and seen.cn0_dbhz == pytest.approx(70.0)
    far = rows[("far-a", "sat-a")]
    assert far.state == "not_seen_capable"
    q = h.quality
    expected = 70.0 + q.get("far-a", "sat-a", "t1") - q.get("alpha-a", "sat-a", "t1") - 10 * math.log10(10e3) - 6.0
    assert far.predicted_margin_db == pytest.approx(expected, abs=1e-6)
    deaf = rows[("deaf-a", "sat-a")]
    assert deaf.state == "not_capable" and deaf.predicted_margin_db is not None and deaf.predicted_margin_db < 6.0
    # Nobody sees it through sat-b, so sat-b rows cannot be judged.
    assert rows[("far-b", "sat-b")].state == "not_capable" and rows[("far-b", "sat-b")].predicted_margin_db is None
    assert "Far" in record.verdict_reason and "1,994 km" in record.verdict_reason
    advisory = h.book.to_schema(record).advisory
    assert advisory.headline == "Local interference at Alpha"
    assert any("not affected" in advisory.detail for _ in [0])


def test_one_site_and_no_capable_second_site_is_undetermined_with_the_reason() -> None:
    h = Harness(make_config(("alpha", "near", "deaf")))
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    h.run(6.0, sightings)
    record = h.only()
    assert record.verdict == "undetermined" and record.verdict_basis == "missing"
    assert record.verdict_gaps[0]["kind"] == "far_not_sensitive"
    assert record.verdict_gaps[0]["site_id"] == "deaf"
    assert "Deaf" in record.verdict_reason and "sensitive" in record.verdict_reason
    advisory = h.book.to_schema(record).advisory
    assert any("Deaf watches Sat A" in a for a in advisory.actions)

    lonely = Harness(make_config(("alpha", "near")))
    lonely.frame(sightings)
    lonely.run(6.0, sightings)
    record = lonely.only()
    assert record.verdict == "undetermined"
    assert record.verdict_gaps == [{"kind": "no_far_site", "sat_id": "sat-a", "near_site_id": "alpha"}]
    assert "no other site more than 200 km away" in record.verdict_reason
    actions = lonely.book.to_schema(record).advisory.actions
    assert any("second site" in a and "Sat A" in a for a in actions)


def test_offline_far_site_is_stale_evidence_and_leaves_the_verdict_undetermined() -> None:
    h = Harness(make_config(("alpha", "far")))
    h.online["far"] = False
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    h.run(6.0, sightings)
    record = h.only()
    rows = h.rows(record)
    assert rows[("far-a", "sat-a")].state == "stale"
    assert record.verdict == "undetermined"
    assert {g["kind"] for g in record.verdict_gaps} == {"far_stale"}
    # The node comes back: its silence now counts.
    h.online["far"] = True
    h.corr.refresh()
    h.run(0.5, sightings)
    assert record.verdict == "local"


def test_verdict_waits_for_the_settle_time() -> None:
    h = Harness(make_config(verdict_settle_s=3.0))
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    record = h.only()
    h.run(2.5, sightings)
    assert record.verdict == "undetermined" and record.verdict_basis == "settling"
    assert record.verdict_gaps == [{"kind": "settling"}]
    assert "checking where it enters" in h.book.to_schema(record).advisory.headline
    h.run(0.5, sightings)
    assert record.verdict == "local"
    verdict_transitions = [t for t in record.transitions if t.verdict == "local"]
    assert verdict_transitions and verdict_transitions[0].t_ns == T0 + 3 * 10**9


# --------------------------------------------------------------------- lifecycle


def test_lost_then_reopened_keeps_the_event_and_its_progress() -> None:
    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    record = h.only()
    assert h.book.mark_characterizing(record.id)
    h.run(6.0, sightings)
    # final recheck: judged local, nothing is characterized or located by satellite any more, and
    # no satellite relays a local signal
    assert record.state == "detected" and record.verdict == "local"
    assert record.transitions[-1].reason == "judged local: nothing to characterize or locate by satellite"
    assert record.satellite_ids == [] and record.transponders == []
    assert h.book.to_schema(record).satellite_ids == []
    h.run(0.5, {})
    assert record.state == "lost" and record.lost_ns == h.clock()
    assert record.transitions[-1].reason == "all detections released"
    # It comes back on another track id within REACQUIRE_KEEP_S: the same event re-opens
    # where it was.
    h.run(20.0, {})
    h.run(0.5, {A1: [det(9, UP - 4e3, UP + 4e3)]})
    assert h.only() is record
    assert record.state == "detected"
    assert record.transitions[-1].reason == "detected again after a break of 20 s"
    assert record.resumed_ns == h.clock() and record.break_s == pytest.approx(20.5)
    assert record.onset_ns is not None and record.recheck is None  # nothing measured to re-check
    assert record.verdict == "local"  # held while the evidence settles again
    assert record.verdict_basis == "capable_not_seen"
    # Lost for longer than the re-open window: a new event.
    h.run(0.5, {})
    assert record.state == "lost"
    h.clock.advance(901.0)
    h.frame({A1: [det(11, UP - 4e3, UP + 4e3)]})
    fresh = [r for r in h.book.records() if r is not record]
    assert len(fresh) == 1 and fresh[0].state == "detected" and fresh[0].id != record.id


def test_window_that_stops_delivering_goes_stale_then_releases_its_detections() -> None:
    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    h.run(6.0, sightings)
    record = h.only()
    assert record.verdict == "local"
    # alpha-a stops sending frames; the others keep going.
    h.run(2.0, {}, skip=("alpha-a",))
    assert h.rows(record)[("alpha-a", "sat-a")].state == "stale"
    assert record.state == "detected"
    h.run(1.5, {}, skip=("alpha-a",))  # 6 frame periods (release_frames) of silence
    assert record.state == "lost"
    assert h.corr.counters()["expired_windows"] == 1


def test_authorised_carrier_becomes_an_event_only_when_anomalous() -> None:
    h = Harness(make_config())
    quiet = {A1: [det(1, UP - 5e3, UP + 5e3, carrier="ref-1", anomaly=False)]}
    h.run(2.0, quiet)
    assert h.book.records() == []
    assert h.corr.counters()["authorized_ignored"] > 0
    loud = {A1: [det(1, UP - 5e3, UP + 5e3, carrier="ref-1", anomaly=True)]}
    h.run(0.5, loud)
    record = h.only()
    assert record.state == "detected"
    h.run(0.5, quiet)  # the anomaly cleared: the carrier no longer holds the event
    assert record.state == "lost"


def test_overlapping_ranges_on_any_channel_are_one_event_and_a_bridge_merges_two() -> None:
    h = Harness(make_config())
    lo = det(1, UP - 2e3, UP + 2e3)
    hi = det(2, UP + 8e3, UP + 12e3)
    h.frame({A1: [lo, hi]})
    first, second = sorted(h.book.records(), key=lambda r: r.seq)
    assert first.id != second.id
    # The same low signal through the other satellite joins the first event.
    h.frame({A1: [lo, hi], FB: [det(5, UP - 1e3, UP + 1e3, SAT_B_SHIFT)]})
    assert len(first.members) == 2 and len(second.members) == 1
    # A wide detection elsewhere bridges both: they merge into the older one.
    bridge = det(3, UP - 1e3, UP + 9e3)
    h.frame({A1: [lo, hi], FB: [det(5, UP - 1e3, UP + 1e3, SAT_B_SHIFT)], FA: [bridge]})
    assert second.state == "closed" and second.merged_into == first.id
    assert second.transitions[-1].reason == f"merged into {first.id}"
    assert first.transitions[-1].reason == f"merged with {second.id}"
    assert first.active and len(first.members) == 4
    assert first.lo_hz == pytest.approx(UP - 2e3) and first.hi_hz == pytest.approx(UP + 12e3)
    assert h.corr.counters()["merged"] == 1
    # Tasking holding the absorbed id is redirected.
    assert h.book.resolve(second.id) is first
    assert h.book.mark_characterizing(second.id) and first.state == "characterizing"
    assert "Merged into" in h.book.to_schema(second).advisory.headline


def test_fragments_of_one_wideband_signal_in_one_update_raise_one_event() -> None:
    """ADAPT-11: a wideband signal near the threshold reaches one window as disjoint
    fragments; another window's single detection joins them. Each fragment raised its
    own event, merged into the first a few milliseconds later and kept in history as a
    closed record. Now they wait for the other windows of the same instant and are
    placed together: one event, nothing merged, nothing else published."""
    h = Harness(make_config())
    h.frame({})  # the windows deliver frames before the signal appears
    fragments = [det(1, UP - 10e3, UP - 7e3), det(2, UP - 5e3, UP - 2e3), det(3, UP, UP + 4e3), det(4, UP + 6e3, UP + 10e3)]
    whole = det(9, UP - 10e3, UP + 10e3)
    h.clock.advance(0.5)
    h.frame({A1: fragments, N1: [whole], FB: [det(5, UP - 10e3, UP + 10e3, SAT_B_SHIFT)]})
    record = h.only()  # one record in the book: no merged fragments in history
    assert record.id == "EV-000001" and record.active and record.merged_into is None
    assert len(record.members) == 6
    assert record.lo_hz == pytest.approx(UP - 10e3) and record.hi_hz == pytest.approx(UP + 10e3)
    counters = h.corr.counters()
    assert counters["created"] == 1 and counters.get("merged", 0) == 0
    assert {p.record.id for p in h.published} == {"EV-000001"}
    assert all("merged" not in (t.reason or "") for t in record.transitions)
    # The event follows the signal as before.
    h.run(1.0, {A1: fragments, N1: [whole]})
    assert h.only() is record and len(record.members) == 5


def test_a_held_detection_raises_its_event_after_the_hold_time_when_a_window_lags() -> None:
    """A window that delivers no frame of the instant does not hold a new event back for
    longer than new_event_hold_s; one that stopped delivering does not hold it at all."""
    h = Harness(make_config())
    h.frame({})
    h.clock.advance(0.5)
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings, skip=("far-b",))  # far-b's frame of this instant is late
    assert [r for r in h.book.records() if not r.historic] == []
    assert h.corr.counters()["held"] == 1
    h.clock.advance(0.5)
    h.corr.tick()
    assert [r for r in h.book.records() if not r.historic] == []  # far-b is still fresh: waiting
    h.clock.advance(0.5)
    h.corr.tick()  # one hold time after the detection was held
    record = h.only()
    assert record.state == "detected" and len(record.members) == 1
    # far-b has now been silent for longer than stale_frames periods: it holds nothing back.
    h.clock.advance(1.5)
    h.frame({**sightings, A1: [det(1, UP - 5e3, UP + 5e3), det(2, UP + 20e3, UP + 22e3)]}, skip=("far-b",))
    assert len([r for r in h.book.records() if not r.historic]) == 2


def test_signals_that_drift_apart_split_into_two_events_after_the_hold_time() -> None:
    h = Harness(make_config())
    a = det(1, UP - 10e3, UP - 4e3)
    b = det(4, UP - 5e3, UP)
    h.frame({A1: [a], N1: [b]})
    record = h.only()
    assert len(record.members) == 2
    apart = {A1: [a], N1: [det(4, UP + 3e3, UP + 9e3)]}
    h.run(4.5, apart)
    assert h.only() is record and record.split_since_ns is not None
    h.run(1.0, apart)
    records = sorted(h.book.records(), key=lambda r: r.seq)
    assert len(records) == 2
    keep, new = records
    assert keep is record and keep.hi_hz == pytest.approx(UP - 4e3)
    assert new.lo_hz == pytest.approx(UP + 3e3) and new.space == "up"
    assert new.transitions[0].reason == f"split from {record.id}"
    assert any(t.reason and t.reason.startswith("split:") for t in record.transitions)
    assert h.corr.counters()["split"] == 1


def test_brief_separation_does_not_split() -> None:
    h = Harness(make_config())
    a = det(1, UP - 10e3, UP - 4e3)
    h.frame({A1: [a], N1: [det(4, UP - 5e3, UP)]})
    h.run(2.0, {A1: [a], N1: [det(4, UP + 3e3, UP + 9e3)]})
    h.run(10.0, {A1: [a], N1: [det(4, UP - 5e3, UP)]})
    record = h.only()
    assert record.split_since_ns is None and len(record.members) == 2


def test_detection_outside_every_passband_is_an_unmapped_event_judged_on_receivers() -> None:
    h = Harness(make_config())
    # 260.0135 MHz: inside the 31.25 kHz monitor window of sat-a:t1, outside its 25 kHz passband.
    unmapped = Detection(1, 260.0130e6, 260.0140e6, 15.0, 15.0, 60.0, T0, T0, None, False)
    h.frame({A1: [unmapped]})
    h.run(6.0, {A1: [unmapped]})
    record = h.only()
    assert record.space == "down" and record.uplink_lo_hz is None
    assert record.downlink_lo_hz == pytest.approx(260.0130e6)
    assert record.satellite_ids == [] and record.transponders == []
    rows = h.rows(record)
    assert rows[("alpha-a", None)].state == "seen"
    assert rows[("far-a", None)].state == "not_seen_capable"
    # margin = cn0 + 10 log10(T_A / T_B) - 10 log10(1 kHz) - threshold
    assert rows[("far-a", None)].predicted_margin_db == pytest.approx(60.0 - 30.0 - 6.0)
    assert rows[("deaf-a", None)].predicted_margin_db == pytest.approx(60.0 + 10 * math.log10(400 / 5000) - 36.0)
    assert record.verdict == "local"
    document = h.book.to_schema(record)
    assert document.uplink_lo_hz is None and document.downlink_hi_hz == pytest.approx(260.0140e6)


def test_closed_event_absorbs_its_signal_until_it_is_released() -> None:
    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    record = h.only()
    h.book.close(record.id)
    assert record.state == "closed" and record.transitions[-1].reason == "closed by operator"
    h.run(3.0, {A1: [det(1, UP - 5e3, UP + 5e3)], FA: [det(2, UP - 5e3, UP + 5e3)]})
    assert h.only() is record  # nothing new while it is still on the air
    h.run(0.5, {})
    assert record.state == "closed" and not record.members
    h.run(0.5, sightings)
    assert len(h.book.records()) == 2
    assert h.book.close(record.id) is record  # closing twice changes nothing


def test_unknown_event_id_raises() -> None:
    h = Harness(make_config())
    with pytest.raises(EventNotFound):
        h.book.close("EV-999999")
    with pytest.raises(EventNotFound):
        h.book.mark_characterizing("nope")


# --------------------------------------------------------------------- tasking API


@dataclass
class FakeSolution:
    """The parts of ``core.solver.Solution`` the event book reads."""

    residuals: list[float]
    converged: bool = True
    lat_deg: float = 20.0
    lon_deg: float = 140.0

    def as_dict(self) -> dict[str, Any]:
        cov = [[4.0e6, 0.0], [0.0, 1.0e6]]
        return {
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "height_m": 0.0,
            "cov_en_m2": cov,
            "cov_noise_en_m2": cov,
            "cov_consider_en_m2": [[0.0, 0.0], [0.0, 0.0]],
            "ellipse": {"semi_major_m": 4895.0, "semi_minor_m": 2448.0, "azimuth_deg": 90.0, "confidence": 0.95},
            "cep50_m": 1900.0,
            "chi2": 1.0,
            "dof": max(len(self.residuals) - 2, 0),
            "reduced_chi2": None,
            "residuals": list(self.residuals),
            "ambiguities": [],
            "ambiguity_resolved_by": "none",
            "converged": self.converged,
            "dominant_error": "noise",
            "n_measurements": len(self.residuals),
        }


def _row(kind: str = "tdoa", used: bool = True, value: float | None = 1e-3) -> schemas.MeasurementRow:
    pair = schemas.PairRef(
        a=schemas.ChannelRef(sat_id="sat-a", site_id="alpha", channel_id="alpha-a"),
        b=schemas.ChannelRef(sat_id="sat-b", site_id="alpha", channel_id="alpha-b"),
    )
    return schemas.MeasurementRow(
        t_ns=T0,
        kind=kind,  # type: ignore[arg-type]
        pair=pair,
        value=value,
        sigma=1e-6 if used else None,
        residual=None,
        snr_db=20.0,
        calibrated=False,
        used=used,
        reason=None if used else "ambiguous",
    )


def test_tasking_mutations_drive_the_lifecycle_and_are_published() -> None:
    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    record = h.only()
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    assert h.book.mark_characterizing(record.id)
    assert not h.book.mark_characterizing(record.id)  # already there
    assert h.book.set_classification(record.id, type("C", (), {"label": "cw", "confidence": 0.9, "features": {"bw": 1.0}})())
    assert not h.book.set_classification(record.id, type("C", (), {"label": "cw", "confidence": math.nan, "features": {}})())
    rejected = h.book.add_measurement(record.id, _row(used=False, value=None))
    assert rejected == 1 and record.state == "characterizing"
    m1 = Measurement("tdoa", 1e-3, 1e-6, T0, path, other)
    m2 = Measurement("fdoa", 0.5, 1e-3, T0, path, other)
    m3 = Measurement("tdoa", 2e-3, 1e-6, T0, path, other)
    s1 = h.book.add_measurement(record.id, _row("tdoa"), m1)
    s2 = h.book.add_measurement(record.id, _row("fdoa", value=0.5), m2)
    s3 = h.book.add_measurement(record.id, _row("tdoa", value=2e-3), m3)
    assert (s1, s2, s3) == (2, 3, 4) and record.state == "locating"
    assert h.book.solver_measurements(record.id) == [m1, m2, m3]
    assert h.book.solver_measurements(record.id, limit=1) == [m3]
    with pytest.raises(ValueError):
        h.book.set_solution(record.id, FakeSolution([0.5, -0.2]), T0, used=[s1])
    assert h.book.set_solution(record.id, None, T0, reason="unobservable")
    assert record.state == "locating" and record.last_solve_reason == "unobservable"
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 1, used=[s1, s2, s3])
    assert record.state == "located" and record.solution_out.is_fix
    residuals = {e.seq: e.row.residual for e in record.measurements}
    assert residuals == {1: None, 2: 0.5, 3: -0.2, 4: 0.1}
    assert record.solution_history[-1].semi_major_m == pytest.approx(4895.0)
    bad = FakeSolution([math.inf, 0.0, 0.0])
    assert not h.book.set_solution(record.id, bad, T0 + 2, used=[s1, s2, s3])

    class Caf:
        surface = [[0.0, -3.0], [-math.inf, 12.0]]
        delay_axis_s = [0.0, 1e-6]
        doppler_axis_hz = [-1.0, 1.0]
        delay_cut = ([0.0, 1e-6], [0.0, 12.0])
        doppler_cut = ([-1.0, 1.0], [math.nan, 12.0])
        tdoa_s = 1e-6
        fdoa_hz = None
        snr_db = 12.0
        detected = True
        reason = None

    display = caf_display(Caf(), _row().pair, T0)
    assert display.surface[1][0] == -3.0 and display.doppler_cut.db[0] == 12.0
    assert h.book.set_caf(record.id, display)
    h.book.flush()
    document = json.loads(h.published[-1].text)
    assert document["state"] == "located" and document["caf"]["peak_tdoa_s"] == 1e-6
    assert document["classification"]["label"] == "cw"
    assert document["solution"]["lat_deg"] == 20.0
    assert [m["residual"] for m in document["measurements"]] == [None, 0.5, -0.2, 0.1]
    assert all(isinstance(t["t_ns"], str) for t in document["transitions"])
    states = [t.state for t in record.transitions]
    assert states == ["detected", "characterizing", "locating", "located"]
    # Closed events ignore late results.
    h.book.close(record.id)
    assert h.book.add_measurement(record.id, _row()) is None
    assert not h.book.set_caf(record.id, display)


def test_event_document_lists_stay_bounded_on_a_long_event() -> None:
    """A solve every 5 s for a day, and a flapping signal, must not grow the published
    document without bound: the first and the newest rows are kept."""
    from petal_locate.server.pipeline.events import MAX_HISTORY_ROWS, MAX_TRANSITION_ROWS

    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    record = h.only()
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    seqs = [h.book.add_measurement(record.id, _row("tdoa"), Measurement("tdoa", 1e-3, 1e-6, T0, path, other)),
            h.book.add_measurement(record.id, _row("tdoa"), Measurement("tdoa", 1e-3, 1e-6, T0, path, other))]
    first_t = T0 + 1
    for k in range(MAX_HISTORY_ROWS + 50):
        assert h.book.set_solution(record.id, FakeSolution([0.1, -0.1]), first_t + k, used=seqs)
    assert len(record.solution_history) == MAX_HISTORY_ROWS
    assert record.solution_history[0].t_ns == first_t
    assert record.solution_history[-1].t_ns == first_t + MAX_HISTORY_ROWS + 49
    for k in range(MAX_TRANSITION_ROWS + 30):
        h.book.mark_lost(record)
        h.book.reopen(record)
    assert len(record.transitions) == MAX_TRANSITION_ROWS
    assert record.transitions[0].state == "detected"
    h.book.flush()
    document = json.loads(h.published[-1].text)
    assert len(document["solution_history"]) == MAX_HISTORY_ROWS


def test_inactive_events_beyond_the_limit_are_dropped_from_memory_only() -> None:
    h = Harness(make_config())
    ids = []
    for k in range(6):
        record = h.book.create(space="up", lo_hz=UP + k * 1e5, hi_hz=UP + k * 1e5 + 1e3, first_seen_ns=T0, last_seen_ns=T0)
        ids.append(record.id)
    h.book.close(ids[0])
    h.book.close(ids[1])
    h.book.mark_lost(h.book.require(ids[2]))  # lost just now: may still re-open
    h.book.mark_lost(h.book.require(ids[3]))
    h.book.flush(force=True)
    assert h.book.prune(900.0, limit=1) == [ids[0]]  # two closed are eligible, one is kept
    assert h.book.get(ids[0]) is None and h.book.get(ids[1]) is not None
    h.clock.advance(901.0)
    assert h.book.prune(900.0, limit=1) == [ids[1], ids[2]]  # the lost ones aged out
    assert h.book.get(ids[3]) is not None and h.book.get(ids[4]) is not None  # active stays
    assert h.book.prune(900.0, limit=1) == []


def test_cross_satellite_detection_makes_the_verdict_uplink() -> None:
    h = Harness(make_config(("alpha", "near")))
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    record = h.only()
    assert h.book.report_cross_satellite_detection(record.id, T0)
    assert record.verdict == "undetermined"  # not before the evidence settles
    h.run(6.0, sightings)
    assert record.verdict == "uplink" and record.verdict_basis == "caf"
    assert "two satellites" in record.verdict_reason


def _locate(h: Harness, record: Any) -> tuple[int, int, int]:
    """Give the event three accepted rows and a converged solution (one more row than
    the two unknowns: a fix); return their seqs."""
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    h.book.mark_characterizing(record.id)
    s1 = h.book.add_measurement(record.id, _row("tdoa"), Measurement("tdoa", 1e-3, 1e-6, T0, path, other))
    s2 = h.book.add_measurement(record.id, _row("fdoa", value=0.5), Measurement("fdoa", 0.5, 1e-3, T0, path, other))
    s3 = h.book.add_measurement(record.id, _row("tdoa", value=2e-3), Measurement("tdoa", 2e-3, 1e-6, T0, path, other))
    assert s1 is not None and s2 is not None and s3 is not None
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), h.clock(), used=[s1, s2, s3])
    assert record.state == "located"
    return s1, s2, s3


def test_detected_again_after_a_long_break_sets_the_old_position_aside() -> None:
    """A signal back after more than REACQUIRE_KEEP_S may be another transmitter on the
    same frequency: the event keeps its identity, but its position, accepted rows and
    verdict are not carried over (a mixed solve or a stale fix would mislead)."""
    from petal_locate.server.pipeline.events import REACQUIRE_KEEP_S, SET_ASIDE_REASON

    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)], FB: [det(1, UP - 5e3, UP + 5e3, SAT_B_SHIFT)]}
    h.frame(sightings)
    record = h.only()
    h.run(6.0, sightings)
    assert record.verdict == "uplink"
    s1, s2, _ = _locate(h, record)
    assert h.book.set_classification(record.id, type("C", (), {"label": "noise", "confidence": 0.9, "features": {}})())
    assert h.book.set_caf(record.id, caf_display(type("Caf", (), {
        "surface": [[0.0]], "delay_axis_s": [0.0], "doppler_axis_hz": [0.0], "delay_cut": ([0.0], [0.0]),
        "doppler_cut": ([0.0], [0.0]), "tdoa_s": 0.0, "fdoa_hz": 0.0, "snr_db": 20.0, "detected": True,
        "reason": None})(), _row().pair, T0))
    h.run(0.5, {})
    assert record.state == "lost"
    h.run(REACQUIRE_KEEP_S + 30.0, {})
    h.run(0.5, sightings)
    assert h.only() is record
    assert record.state == "characterizing" and record.verdict == "undetermined"
    assert record.solution is None and record.solution_out is None and record.solution_t_ns is None
    assert record.classification is None and record.caf is None  # they described the earlier signal
    assert h.book.solver_measurements(record.id) == []
    rows = {e.seq: e.row for e in record.measurements}
    assert not rows[s1].used and rows[s1].reason.startswith(f"{SET_ASIDE_REASON}: measured before the signal was lost at")
    assert rows[s1].value == 1e-3 and rows[s1].residual is None  # kept for the record
    assert "after a break of 1 min" in record.transitions[-1].reason
    assert "position and measurements are set aside" in record.transitions[-1].reason
    assert len(record.solution_history) == 1  # the old ellipse stays in the history
    document = h.book.to_schema(record)
    assert document.solution is None and document.state == "characterizing"
    assert h.book.summary(record).solution is None
    # A solve that started before the set-aside cannot bring the old position back.
    assert not h.book.set_solution(record.id, FakeSolution([0.5, -0.2]), h.clock(), used=[s1, s2])
    assert record.solution is None
    # The verdict is formed again from the new evidence once it has settled.
    h.run(5.5, sightings)
    assert record.verdict == "uplink" and record.verdict_basis == "two_sites"
    # New rows locate it again.
    t1, t2, t3 = _locate(h, record)
    assert [e.seq for e in record.used_measurements()] == [t1, t2, t3]


def test_uplink_verdict_gives_way_when_only_a_local_signal_remains() -> None:
    """The uplink transmitter stops and a local one takes its frequency at Alpha (no
    loss in between, so the event carries on): once the evidence alone has said local
    for LOCAL_OVERRIDE_S without a fresh cross-satellite measurement, the verdict
    becomes local and the uplink position is set aside."""
    from petal_locate.server.pipeline.correlator import CAF_EVIDENCE_S, LOCAL_OVERRIDE_S

    h = Harness(make_config())
    both = {A1: [det(1, UP - 5e3, UP + 5e3)], FB: [det(1, UP - 5e3, UP + 5e3, SAT_B_SHIFT)]}
    h.frame(both)
    record = h.only()
    h.run(6.0, both)
    _locate(h, record)  # its accepted rows are cross-satellite proof (CAF_EVIDENCE_S)
    alone = {A1: both[A1]}
    h.run(LOCAL_OVERRIDE_S + 5.0, alone)
    assert record.verdict == "uplink"  # the cross-satellite proof is still fresh
    h.run(CAF_EVIDENCE_S - LOCAL_OVERRIDE_S - 5.0 + LOCAL_OVERRIDE_S - 1.0, alone)
    assert record.verdict == "uplink" and record.local_since_ns is not None
    h.run(2.0, alone)
    assert record.verdict == "local" and record.verdict_basis == "capable_not_seen"
    assert record.verdict_reason.startswith("Far, 1,994 km away")
    assert record.solution is None and record.state == "detected"
    assert not record.caf_uplink
    assert all(not e.row.used for e in record.measurements)
    reasons = [t.reason or "" for t in record.transitions[-3:]]
    assert reasons[0].startswith("verdict: Far") and "uplink signal seen earlier has stopped" in reasons[0]
    assert reasons[1].startswith("position set aside")
    assert reasons[2] == "judged local: nothing to characterize or locate by satellite"
    assert "Local interference" in h.book.to_schema(record).advisory.headline
    # It stays local while only Alpha sees it.
    h.run(5.0, alone)
    assert record.verdict == "local"


def test_fresh_cross_satellite_measurements_keep_an_uplink_verdict() -> None:
    from petal_locate.server.pipeline.correlator import LOCAL_OVERRIDE_S

    h = Harness(make_config())
    both = {A1: [det(1, UP - 5e3, UP + 5e3)], FB: [det(1, UP - 5e3, UP + 5e3, SAT_B_SHIFT)]}
    h.frame(both)
    record = h.only()
    h.run(6.0, both)
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    for _ in range(int(4 * LOCAL_OVERRIDE_S / 5)):  # a capture round every 5 s
        h.book.add_measurement(record.id, _row("fdoa", value=0.5), Measurement("fdoa", 0.5, 1e-3, T0, path, other))
        h.run(5.0, {A1: both[A1]})
    assert record.verdict == "uplink" and record.verdict_basis == "caf"


def test_evidence_rows_remember_when_a_channel_last_saw_the_event() -> None:
    h = Harness(make_config(("alpha", "far")))
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3, last_ns=T0 + 7 * 10**9)], FB: [det(1, UP - 5e3, UP + 5e3, SAT_B_SHIFT)]}
    h.frame(sightings)
    record = h.only()
    h.run(6.0, sightings)
    assert h.rows(record)[("alpha-a", "sat-a")].last_seen_ns == T0 + 7 * 10**9
    # Alpha's node goes away: its row is stale and still says when it last saw the signal.
    h.online["alpha"] = False
    h.run(2.0, {FB: sightings[FB]}, skip=("alpha-a", "alpha-b"))
    row = h.rows(record)[("alpha-a", "sat-a")]
    assert row.state == "stale" and row.last_seen_ns == T0 + 7 * 10**9
    never = h.rows(record)[("far-a", "sat-a")]
    assert never.last_seen_ns is None


def test_correlation_candidates_and_mirror_positions_reach_the_console() -> None:
    class Caf:
        surface = [[0.0, 1.0], [2.0, 3.0]]
        delay_axis_s = [0.0, 1e-6]
        doppler_axis_hz = [-1.0, 1.0]
        delay_cut = ([0.0, 1e-6], [0.0, 3.0])
        doppler_cut = ([-1.0, 1.0], [0.0, 3.0])
        tdoa_s = None
        fdoa_hz = None
        snr_db = 30.0
        detected = True
        reason = "ambiguous"
        candidates = [(1e-4, 25.0, 30.0), (2e-4, 50.0, 29.5), (math.nan, 0.0, 29.0), (3e-4, math.inf, 1.0)]

    display = caf_display(Caf(), _row().pair, T0)
    assert [(c.tdoa_s, c.fdoa_hz, c.snr_db) for c in display.candidates] == [
        (1e-4, 25.0, 30.0), (2e-4, 50.0, 29.5), (None, 0.0, 29.0)]
    Caf.reason = "narrowband"
    assert all(c.tdoa_s is None for c in caf_display(Caf(), _row().pair, T0).candidates)

    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    record = h.only()
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    seqs = [h.book.add_measurement(record.id, _row("tdoa"), Measurement("tdoa", 1e-3, 1e-6, T0, path, other)) for _ in range(2)]

    class Ambiguous(FakeSolution):
        def as_dict(self) -> dict[str, Any]:
            data = super().as_dict()
            mirror = FakeSolution(list(self.residuals), lat_deg=-20.0).as_dict()
            mirror["ambiguity_resolved_by"] = "unresolved"
            data["ambiguities"] = [mirror]
            data["ambiguity_resolved_by"] = "unresolved"
            return data

    assert h.book.set_solution(record.id, Ambiguous([0.1, 0.2]), T0, used=seqs)
    summary = h.book.summary(record).solution
    assert summary is not None and summary.ambiguous
    assert [(a.lat_deg, a.lon_deg, a.semi_major_m) for a in summary.ambiguities] == [(-20.0, 140.0, 4895.0)]


# --------------------------------------------------------------------- publication


def test_numeric_drift_is_throttled_while_material_changes_publish_at_once() -> None:
    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3, cn0=70.0)]})
    record = h.only()
    assert h.published[0].event.state == "detected"
    # Within the first frame the other windows report one after the other: each one
    # turns its row from stale to fresh, a material change of the evidence.
    h.run(0.5, {A1: [det(1, UP - 5e3, UP + 5e3, cn0=70.0)]})
    count = len(h.published)
    assert 1 <= count <= 1 + len(h.specs)
    for i in range(6):  # SNR wobbles for 3 s: numeric only
        h.run(0.5, {A1: [det(1, UP - 5e3, UP + 5e3, cn0=70.0 + 0.1 * i, snr=20.0 + 0.1 * i)]})
    assert len(h.published) == count
    h.run(3.0, {A1: [det(1, UP - 5e3, UP + 5e3, cn0=71.0)]})  # settle: verdict changes
    assert record.verdict == "local"
    assert len(h.published) <= count + 3
    verdicts = [p.event.verdict for p in h.published]
    assert verdicts[-1] == "local" or "local" in verdicts
    for publication in h.published:
        assert "NaN" not in publication.text and "Infinity" not in publication.text
        schemas.Event.model_validate_json(publication.text)
    assert any(p.new_transitions for p in h.published)
    assert any(p.evidence_changed for p in h.published)


def test_reconfigure_remaps_detections_and_drops_removed_windows() -> None:
    h = Harness(make_config())
    sightings = {A1: [det(1, UP - 5e3, UP + 5e3)], FA: [det(2, UP - 5e3, UP + 5e3)]}
    h.frame(sightings)
    record = h.only()
    assert record.seen_sites() == ["alpha", "far"]
    smaller = make_config(("alpha", "near"))
    specs = build_window_specs(smaller, T0 / 1e9)
    h.corr.reconfigure(smaller, specs, QualityTable.build(smaller, T0 / 1e9))
    assert record.seen_sites() == ["alpha"]
    assert {r.site_id for r in record.evidence} == {"alpha", "near"}
    assert record.active


# --------------------------------------------------------------------- pure verdict


def _ev(site: str, state: str, margin: float | None = None, sat: str | None = "sat-a") -> schemas.EvidenceRow:
    return schemas.EvidenceRow(
        channel_id=f"{site}-ch",
        site_id=site,
        sat_id=sat,
        state=state,  # type: ignore[arg-type]
        snr_db=None,
        cn0_dbhz=None,
        predicted_margin_db=margin,
    )


def test_decide_verdict_rules_against_hand_made_distances() -> None:
    distances = {("a", "b"): 150.0, ("a", "c"): 900.0, ("b", "c"): 800.0}

    def dist(x: str, y: str) -> float:
        return 0.0 if x == y else distances.get((x, y), distances.get((y, x), math.inf))

    # Two seen sites within the separation: not uplink; c capable and far: local.
    d = decide_verdict([_ev("a", "seen"), _ev("b", "seen"), _ev("c", "not_seen_capable", 15.0)], dist, 200.0)
    assert d.verdict == "local"
    # Seen at a and c (900 km): uplink.
    d = decide_verdict([_ev("a", "seen"), _ev("c", "seen")], dist, 200.0)
    assert d.verdict == "uplink" and d.basis == "two_sites"
    # A capable but near site does not make it local.
    d = decide_verdict([_ev("a", "seen"), _ev("b", "not_seen_capable", 20.0)], dist, 200.0)
    assert d.verdict == "undetermined" and d.gaps[0]["kind"] == "no_far_site"
    # Separation larger than every distance: the same evidence is undetermined.
    d = decide_verdict([_ev("a", "seen"), _ev("c", "not_seen_capable", 15.0)], dist, 1000.0)
    assert d.verdict == "undetermined"
    # Hysteresis: a local verdict holds when the witness goes stale.
    d = decide_verdict([_ev("a", "seen"), _ev("c", "stale")], dist, 200.0, previous="local", previous_reason="x")
    assert d.verdict == "local" and d.basis == "kept" and d.reason == "x"
    d = decide_verdict([_ev("a", "seen"), _ev("c", "stale")], dist, 200.0)
    assert d.verdict == "undetermined" and d.gaps[0]["kind"] == "far_stale"
    d = decide_verdict([_ev("a", "seen"), _ev("c", "not_capable", -3.0)], dist, 200.0)
    assert "would not be sensitive enough" in d.reason
    d = decide_verdict([_ev("a", "not_capable")], dist, 200.0)
    assert d.gaps[0]["kind"] == "no_sighting"
    d = decide_verdict([_ev("a", "seen")], dist, 200.0, caf_uplink=True)
    assert d.verdict == "uplink" and d.basis == "caf"


def test_site_distances_are_symmetric_geodesics() -> None:
    distances = site_distances_km(make_config())
    assert distances[("alpha", "far")] == pytest.approx(1994, abs=2)
    assert distances[("alpha", "far")] == distances[("far", "alpha")]
    assert distances[("alpha", "alpha")] == 0.0
    # Independent check: one degree of latitude near 26 N is about 110.9 km.
    one = SystemConfig.model_validate(
        {
            "name": "d",
            "satellites": make_config().model_dump()["satellites"][:1],
            "sites": [
                {**SITES["alpha"], "id": "p", "lat_deg": 25.5, "lon_deg": 139.0, "channels": SITES["alpha"]["channels"][:1]},
                {
                    **SITES["near"],
                    "id": "q",
                    "lat_deg": 26.5,
                    "lon_deg": 139.0,
                    "channels": [{**SITES["near"]["channels"][0], "id": "q-a"}],
                },
            ],
        }
    )
    assert site_distances_km(one)[("p", "q")] == pytest.approx(110.85, abs=0.1)


def test_a_solution_without_redundancy_or_with_an_absurd_ellipse_is_not_a_fix() -> None:
    """R4/R5: live, an event was declared located from 2 rows (no redundancy, dof 0) with a
    12,671 km ellipse 3,143 km from the truth, and an FDOA-only CW fix from 2 rows missed
    by 136 km outside its 84 km ellipse. Only a converged, consistent fit with dof >= 1
    and an ellipse within the sanity bound is a fix; anything else is published with
    is_fix false and its reason, and the event is never located from it."""
    from petal_locate.server.pipeline.events import FIX_MAX_SEMI_MAJOR_M, fix_semi_major_limit_m

    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    record = h.only()
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    h.book.mark_characterizing(record.id)
    s1 = h.book.add_measurement(record.id, _row("fdoa", value=0.5), Measurement("fdoa", 0.5, 1e-3, T0, path, other))
    s2 = h.book.add_measurement(record.id, _row("fdoa", value=0.6), Measurement("fdoa", 0.6, 1e-3, T0, path, other))
    limit = FIX_MAX_SEMI_MAJOR_M
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2]), T0 + 1, used=[s1, s2], max_semi_major_m=limit)
    out = record.solution_out
    assert record.state == "locating" and out is not None and out.fit_consistent and not out.is_fix
    assert out.not_fix_reason.startswith("no redundancy: 2 measurements for the two unknowns of a position")
    assert record.transitions[-1].reason.startswith("the position is not a fix: no redundancy")
    assert h.book.summary(record).solution.is_fix is False
    assert record.solution_history[-1].is_fix is False
    record.verdict, record.verdict_basis, record.verdict_reason = "uplink", "caf", "Seen through two satellites."
    advisory = advise(record, None)
    assert "is not a reliable position: no redundancy" in advisory.detail
    assert not any("Report the estimated transmitter position" in a for a in advisory.actions)
    assert any("do not report the computed position yet: it is not a fix" in a for a in advisory.actions)
    # one more row: a fix
    s3 = h.book.add_measurement(record.id, _row("fdoa", value=0.7), Measurement("fdoa", 0.7, 1e-3, T0, path, other))
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 2, used=[s1, s2, s3],
                               max_semi_major_m=limit)
    assert record.state == "located" and record.solution_out.is_fix and record.solution_out.not_fix_reason is None
    # a consistent, redundant fit whose ellipse is thousands of kilometres long is withdrawn
    huge = FakeSolution([0.5, -0.2, 0.1])
    data = huge.as_dict()
    data["ellipse"] = {**data["ellipse"], "semi_major_m": 12_671_000.0}
    huge.as_dict = lambda: data  # type: ignore[method-assign]
    assert h.book.set_solution(record.id, huge, T0 + 3, used=[s1, s2, s3], max_semi_major_m=limit)
    assert record.state == "locating" and not record.solution_out.is_fix
    assert "the 95 % error ellipse has a semi-major axis of 12,671 km, more than the 1,000 km a fix may have" in (
        record.solution_out.not_fix_reason
    )
    assert record.transitions[-1].reason.startswith("position withdrawn: the position is not a fix: the 95 %")
    # the bound: 1,000 km, or a quarter of a smaller region's smaller extent
    from pathlib import Path

    from petal_locate.config import load_config

    pacific = load_config(Path(__file__).resolve().parents[1] / "config" / "pacific-uhf.yaml")
    assert fix_semi_major_limit_m(pacific.processing.region) == pytest.approx(1.0e6)
    small = type("R", (), {"lat_min": 10.0, "lat_max": 14.0, "lon_span_deg": lambda self: 6.0})()
    assert fix_semi_major_limit_m(small) == pytest.approx(0.25 * 4.0 * 111_195.0)
    assert fix_semi_major_limit_m(None) == FIX_MAX_SEMI_MAJOR_M


def test_a_solution_solved_with_an_ephemeris_allowance_keeps_it_and_says_when_it_changes() -> None:
    """D1: tasking widens the position sigma of a satellite the calibrations put wrong
    beyond its configured uncertainty and hands the allowance to set_solution, with how
    much it widened the 95 % ellipse. The published solution keeps both
    (``solution_allowance``, ``solution_widening``; no widening without an allowance),
    the history says when the allowance widens (with the growth of the ellipse when
    known), shrinks or ends (and nothing when it is the same), and a withdrawn position
    takes them along. The state is never touched by the allowance itself; the fix gate
    judges the widened ellipse like any other."""
    from petal_locate.server.pipeline.events import EphemerisAllowance

    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    record = h.only()
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    h.book.mark_characterizing(record.id)
    seqs = [
        h.book.add_measurement(record.id, _row("fdoa", value=v), Measurement("fdoa", v, 1e-3, T0, path, other))
        for v in (0.5, 0.6, 0.7)
    ]
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 1, used=seqs)
    assert record.state == "located" and record.solution_allowance == ()
    count = len(record.transitions)
    wide = EphemerisAllowance("sat-b", 9.51, (951.0, 9510.0, 9510.0))
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 2, used=seqs, allowance=[wide],
                               widening=1.214)
    assert record.solution_allowance == (wide,) and record.state == "located"
    assert record.solution_widening == pytest.approx(1.214)
    assert len(record.transitions) == count + 1
    assert record.transitions[-1].reason == (
        "ellipse widened: the reference calibrations put the position of sat-b wrong beyond the configured "
        "uncertainty (position sigma taken larger: sat-b 9.5 times; the 95 % ellipse's major axis 21 % longer than "
        "with the configured uncertainty)"
    )
    assert record.transitions[-1].state == "located"
    # the same allowance again: nothing to say (the growth is kept as given: here not known)
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 3, used=seqs, allowance=[wide])
    assert len(record.transitions) == count + 1 and record.solution_widening is None
    # no allowance: no widening kept, whatever is passed
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 3, used=seqs, widening=1.5)
    assert record.solution_widening is None
    assert record.transitions[-1].reason.startswith("ellipse allowance for the position of sat-b removed")
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 3, used=seqs, allowance=[wide],
                               widening=float("nan"))
    assert record.solution_widening is None
    assert record.transitions[-1].reason.endswith("(position sigma taken larger: sat-b 9.5 times)")
    # smaller, then capped, then gone
    small = EphemerisAllowance("sat-b", 4.0, (400.0, 4000.0, 4000.0))
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 4, used=seqs, allowance=[small])
    assert record.transitions[-1].reason == "ellipse allowance for the position of sat-b reduced to 4 times its configured sigma"
    top = EphemerisAllowance("sat-a", 20.0, (2000.0, 20000.0, 20000.0), capped=True)
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 5, used=seqs, allowance=[small, top],
                               widening=2.5)
    assert "the position of sat-a wrong beyond the configured uncertainty (position sigma taken larger: sat-a 20 " \
           "times, at the largest allowed scale; the 95 % ellipse's major axis 2.5 times as long as with the " \
           "configured uncertainty)" in record.transitions[-1].reason
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 6, used=seqs, allowance=[top])
    assert record.transitions[-1].reason == (
        "ellipse allowance for the position of sat-b removed: the configured uncertainty is used again"
    )
    # the fix gate judges the widened ellipse (here 4.9 km against a 1 km bound): not a fix, back to locating
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 6, used=seqs, allowance=[top],
                               widening=1.3, max_semi_major_m=1000.0)
    assert record.state == "locating" and not record.solution_out.is_fix
    assert record.transitions[-1].reason.startswith("position withdrawn: the position is not a fix")
    # a failed solve keeps the published allowance; a withdrawn position takes it along
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 6, used=seqs, allowance=[top],
                               widening=1.3)
    assert record.state == "located"
    assert h.book.set_solution(record.id, None, T0 + 7, reason="unobservable")
    assert record.solution_allowance == (top,) and record.solution_widening == pytest.approx(1.3)
    assert h.book.withdraw_solution(record.id, "test")
    assert record.solution_allowance == () and record.solution_widening is None


def test_a_first_fix_through_a_doubt_not_yet_confirmed_waits_and_a_located_event_keeps_its_fix() -> None:
    """Final pass (live ephemeris-error from 09:00 UTC: the first fix, 4 rows, came while
    the doubt about a satellite it went through was unconfirmed, so no allowance covered
    it; it was located with the truth 415 m off a 449 m ellipse). A solution through such
    a satellite (``unconfirmed_doubt``, from tasking) is published as not a fix while the
    event is not yet located, and so is each ambiguity that would be a fix; the event
    stays locating with the reason in the history. Once the doubt is confirmed (no
    satellite passed) the next fix locates it. A located event is not withdrawn for a new,
    unconfirmed doubt (the advice warns instead), and a withdrawn position forgets the
    hold."""
    from petal_locate.server.pipeline.events import doubt_hold_reason

    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    record = h.only()
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    h.book.mark_characterizing(record.id)
    seqs = [
        h.book.add_measurement(record.id, _row("fdoa", value=v), Measurement("fdoa", v, 1e-3, T0, path, other))
        for v in (0.5, 0.6, 0.7)
    ]

    class Ambiguous(FakeSolution):
        def as_dict(self) -> dict[str, Any]:
            data = super().as_dict()
            data["ambiguities"] = [FakeSolution(list(self.residuals), lat_deg=-20.0).as_dict()]
            data["ambiguity_resolved_by"] = "region"
            return data

    reason = doubt_hold_reason(("sat-b",))
    assert reason == ("the reference calibrations have just put the position of sat-b wrong beyond its configured "
                      "uncertainty, and the ellipse does not include that error yet (it is widened once a later "
                      "round confirms the doubt)")
    assert doubt_hold_reason(("sat-a", "sat-b")).startswith(
        "the reference calibrations have just put the positions of sat-a and sat-b wrong beyond their configured")
    assert h.book.set_solution(record.id, Ambiguous([0.5, -0.2, 0.1]), T0 + 1, used=seqs,
                               unconfirmed_doubt=["sat-b", "sat-b"])
    out = record.solution_out
    assert record.state == "locating" and not out.is_fix and out.not_fix_reason == reason
    assert [(a.is_fix, a.not_fix_reason) for a in out.ambiguities] == [(False, reason)]
    assert record.solution_held == ("sat-b",) and record.solution_history[-1].is_fix is False
    assert record.transitions[-1].reason == f"the position is not a fix: {reason}"
    # the doubt confirmed and widened for: the next fix locates the event
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 2, used=seqs)
    assert record.state == "located" and record.solution_out.is_fix and record.solution_held == ()
    assert record.transitions[-1].reason == "position estimated"
    # located: a new unconfirmed doubt does not withdraw the fix
    count = len(record.transitions)
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 3, used=seqs,
                               unconfirmed_doubt=["sat-a"])
    assert record.state == "located" and record.solution_out.is_fix and record.solution_held == ()
    assert len(record.transitions) == count
    # a solution that is not a fix for another reason keeps that reason
    assert h.book.withdraw_solution(record.id, "test")
    assert record.state == "locating"
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2]), T0 + 4, used=seqs[:2],
                               unconfirmed_doubt=["sat-b"])
    assert record.solution_out.not_fix_reason.startswith("no redundancy") and record.solution_held == ()
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 5, used=seqs,
                               unconfirmed_doubt=["sat-b"])
    assert record.solution_held == ("sat-b",)
    assert h.book.withdraw_solution(record.id, "test") and record.solution_held == ()


def test_the_published_solution_and_event_carry_the_ephemeris_allowance_and_doubt() -> None:
    """Integration of D1: the allowance a solve was widened by and how much it widened
    the ellipse reach the operator in the published solution
    (``SolutionOut.ephemeris_allowance``, ``ellipse_widening``), in the stored solution
    JSON and in the event document, with the satellites in doubt
    (``Event.doubted_satellite_ids``). Without an allowance both are empty, whatever
    widening is passed; an ambiguity never carries them."""
    from petal_locate.server.pipeline.events import EphemerisAllowance

    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    record = h.only()
    path = PathSpec("sat-a", "alpha", UP, SAT_A_SHIFT)
    other = PathSpec("sat-b", "alpha", UP, SAT_B_SHIFT)
    h.book.mark_characterizing(record.id)
    seqs = [
        h.book.add_measurement(record.id, _row("fdoa", value=v), Measurement("fdoa", v, 1e-3, T0, path, other))
        for v in (0.5, 0.6, 0.7)
    ]
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 1, used=seqs, widening=1.4)
    out = record.solution_out
    assert out.ephemeris_allowance == [] and out.ellipse_widening is None
    doc = h.book.to_schema(record)
    assert doc.doubted_satellite_ids == [] and doc.solution.ephemeris_allowance == []

    class Ambiguous(FakeSolution):
        def as_dict(self) -> dict[str, Any]:
            data = super().as_dict()
            mirror = FakeSolution(list(self.residuals), lat_deg=-20.0).as_dict()
            data["ambiguities"] = [mirror]
            data["ambiguity_resolved_by"] = "region"
            return data

    wide = EphemerisAllowance("sat-b", 9.51, (951.0, 9510.0, 9510.0))
    top = EphemerisAllowance("sat-a", 20.0, (2000.0, 20000.0, 20000.0), capped=True)
    assert h.book.set_satellite_doubt(record.id, ["sat-b", "sat-a"])
    assert h.book.set_solution(record.id, Ambiguous([0.5, -0.2, 0.1]), T0 + 2, used=seqs, allowance=[wide, top],
                               widening=1.214)
    out = record.solution_out
    assert [a.model_dump() for a in out.ephemeris_allowance] == [
        {"sat_id": "sat-a", "scale": 20.0, "sigma_ric_m": [2000.0, 20000.0, 20000.0], "capped": True},
        {"sat_id": "sat-b", "scale": 9.51, "sigma_ric_m": [951.0, 9510.0, 9510.0], "capped": False},
    ]
    assert out.ellipse_widening == pytest.approx(1.214)
    assert len(out.ambiguities) == 1
    assert out.ambiguities[0].ephemeris_allowance == [] and out.ambiguities[0].ellipse_widening is None
    stored = json.loads(record.unsaved_solutions[-1][2])
    assert [a["sat_id"] for a in stored["ephemeris_allowance"]] == ["sat-a", "sat-b"]
    assert stored["ellipse_widening"] == pytest.approx(1.214)
    doc = h.book.to_schema(record)
    assert doc.doubted_satellite_ids == ["sat-a", "sat-b"]
    assert doc.solution.ephemeris_allowance == out.ephemeris_allowance
    again = type(doc).model_validate_json(doc.model_dump_json())
    assert again.solution.ellipse_widening == pytest.approx(1.214) and again.doubted_satellite_ids == ["sat-a", "sat-b"]
    # both ended at once: one transition naming both positions
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 3, used=seqs)
    assert record.transitions[-1].reason == (
        "ellipse allowance for the positions of sat-a and sat-b removed: the configured uncertainty is used again"
    )
    assert record.solution_out.ephemeris_allowance == [] and record.solution_out.ellipse_widening is None
    # not finite: the allowance is published, the widening is not known
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2, 0.1]), T0 + 3, used=seqs, allowance=[wide],
                               widening=float("inf"))
    assert [a.sat_id for a in record.solution_out.ephemeris_allowance] == ["sat-b"]
    assert record.solution_out.ellipse_widening is None


def test_a_local_signal_that_takes_over_starts_its_own_transmission() -> None:
    """R2: when the uplink signal stops and a local one holds its frequency at one site,
    the event's onset (the start time the operator reports) and the evidence cells'
    'last saw it' belong to the local signal: the onset is when the uplink was last seen
    at the sites that no longer see anything, not the uplink's first detection, and a
    channel that only saw the uplink no longer shows its last sighting."""
    from petal_locate.server.pipeline.correlator import CAF_EVIDENCE_S, LOCAL_OVERRIDE_S

    h = Harness(make_config())

    def run(seconds: float, far: bool) -> None:
        for _ in range(int(round(seconds / 0.5))):
            h.clock.advance(0.5)
            now = h.clock()
            sightings = {A1: [det(1, UP - 5e3, UP + 5e3, last_ns=now)]}
            if far:
                sightings[FB] = [det(1, UP - 5e3, UP + 5e3, SAT_B_SHIFT, last_ns=now)]
            h.frame(sightings)
            h.corr.tick()

    run(0.5, True)
    record = h.only()
    run(6.0, True)
    _locate(h, record)
    assert record.onset == T0
    run(10.0, True)
    stopped_ns = h.clock()  # the uplink's last frame at Far
    assert h.rows(record)[FB_ROW].last_seen_ns == stopped_ns
    for _ in range(int(2 * (CAF_EVIDENCE_S + LOCAL_OVERRIDE_S + 10.0))):
        run(0.5, False)
        if record.verdict == "local":
            break
    assert record.verdict == "local"
    assert record.onset_ns == stopped_ns  # not the uplink's first detection (T0)
    rows = h.rows(record)
    assert rows[FB_ROW].last_seen_ns is None  # it only ever saw the uplink signal
    assert record.channel_seen_ns.get(FB_ROW) is None
    assert rows[A1_ROW].state == "seen" and rows[A1_ROW].last_seen_ns is not None
    assert int(h.book.to_schema(record).onset_ns) == stopped_ns
    run(5.0, False)  # later evaluations do not bring the uplink's sightings back
    assert h.rows(record)[FB_ROW].last_seen_ns is None


A1_ROW = ("alpha-a", "sat-a")
FB_ROW = ("far-b", "sat-b")


def test_an_uplink_signal_that_takes_over_a_local_event_starts_its_own_transmission() -> None:
    """Final recheck: a local event whose frequency an uplink jammer then takes (seen far
    away through both satellites) kept the local signal's onset, so the advisory gave the
    local signal's start as the jammer's. A local signal does not become an uplink one:
    the onset moves to the first detection at a site that did not see the local signal,
    and what was measured on the local signal (its class) is set aside."""
    h = Harness(make_config())
    local = {A1: [det(1, UP - 5e3, UP + 5e3, cn0=70.0)]}
    h.frame(local)
    h.run(6.0, local)
    record = h.only()
    assert record.verdict == "local" and record.onset == T0
    assert record.local_site_ids == ("alpha",)
    assert h.book.set_classification(record.id, Classification("noise", 0.9, {}))
    h.run(20.0, local)
    start_ns = h.clock() + int(0.5e9)
    both = {
        A1: local[A1],
        FA: [det(2, UP - 5e3, UP + 5e3, first_ns=start_ns)],
        FB: [det(3, UP - 5e3, UP + 5e3, SAT_B_SHIFT, first_ns=start_ns)],
    }
    for _ in range(60):
        h.run(0.5, both)
        if record.verdict == "uplink":
            break
    assert record.verdict == "uplink"
    assert record.onset_ns == start_ns  # the jammer's first detection, not the local signal's (T0)
    assert int(h.book.to_schema(record).onset_ns) == start_ns
    assert record.classification is None  # it described the local signal


def test_event_text_that_utf8_cannot_carry_is_escaped() -> None:
    """SRV-2 defence in depth: text reaching an event (a node's capture error message in a
    row reason, a tasking message, a transition reason) is escaped where UTF-8 cannot
    carry it, so the published document can always be written."""
    h = Harness(make_config())
    h.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    record = h.only()
    bad = "capture_failed: driver: bad \ud800 name"
    seq = h.book.add_measurement(record.id, _row(used=False, value=None).model_copy(update={"reason": bad}))
    assert seq is not None
    status = schemas.TaskingStatus(state="waiting", rounds=1, last_round_ns=T0, next_round_ns=None, channels=["alpha-a"],
                                   message=f"Capture failed on alpha-a (driver: {bad}).", solve_reason=bad)
    assert h.book.set_tasking(record.id, status)
    h.book.note_transition(record, T0, f"note: {bad}", force=True)
    assert h.book.set_solution(record.id, None, T0, reason=bad)
    h.book.flush(force=True)
    text = h.published[-1].text
    text.encode("utf-8")  # writable
    document = json.loads(text)
    assert "\\\\ud800" in json.dumps(document) or "\\ud800" in document["measurements"][-1]["reason"]
    assert document["measurements"][-1]["reason"].endswith("bad \\ud800 name")
    assert record.tasking.message.endswith("(driver: capture_failed: driver: bad \\ud800 name).")
    assert record.transitions[-1].reason == "note: capture_failed: driver: bad \\ud800 name"
    assert record.last_solve_reason.endswith("\\ud800 name")


def test_a_signal_that_fills_its_window_is_at_least_as_wide_as_shown() -> None:
    """ADAPT-5: when every detection of an event reaches both edges of the window it was
    measured in, the signal may be wider than any window shows: the document marks the
    width as a lower bound and the advice says 'at least'."""
    h = Harness(make_config())
    spec = h.specs[A1]
    full = det(1, spec.lo_hz - SAT_A_SHIFT, spec.hi_hz - SAT_A_SHIFT)  # the whole window
    h.frame({A1: [full]})
    record = h.only()
    h.corr.tick()
    assert record.width_lower_bound
    document = h.book.to_schema(record)
    assert document.width_lower_bound and h.book.summary(record).width_lower_bound
    assert "(at least" in advise(record, None).detail
    # a narrower signal inside the window: its width is measured
    h2 = Harness(make_config())
    h2.frame({A1: [det(1, UP - 5e3, UP + 5e3)]})
    narrow = h2.only()
    h2.corr.tick()
    assert not narrow.width_lower_bound and not h2.book.to_schema(narrow).width_lower_bound
    assert "(at least" not in advise(narrow, None).detail and "(about 10 kHz wide)" in advise(narrow, None).detail


@pytest.mark.parametrize(("short_fraction", "lower_bound"), [(0.039, True), (0.07, True), (0.15, False)])
def test_a_signal_that_fills_its_window_up_to_the_receiver_roll_off_is_a_lower_bound(
    short_fraction: float, lower_bound: bool
) -> None:
    """Final recheck of ADAPT-5: a receiver's anti-alias filter rolls off near the window
    edges, so a signal wider than the window is detected only to about 4 % of the span
    short of each edge (live: a 20 MHz jammer showed 4.62 MHz of a 5 MHz window), far more
    than 1.5 bins. The outer WINDOW_EDGE_FRACTION of each side counts as the edge; a
    signal stopping well inside that is measured."""
    h = Harness(make_config())
    spec = h.specs[A1]
    short = short_fraction * spec.span_hz
    h.frame({A1: [det(1, spec.lo_hz + short - SAT_A_SHIFT, spec.hi_hz - short - SAT_A_SHIFT)]})
    record = h.only()
    h.corr.tick()
    assert record.width_lower_bound is lower_bound
    assert ("(at least" in advise(record, None).detail) is lower_bound


def test_a_piece_of_a_wide_signal_does_not_undo_its_lower_bound() -> None:
    """Final recheck (live, a 20 MHz jammer in 5 MHz windows): the flag flickered off when
    the noise broke into a second, narrower detection in the same window. What a window's
    detections cover together decides."""
    h = Harness(make_config())
    spec = h.specs[A1]
    short = 0.04 * spec.span_hz
    whole = det(1, spec.lo_hz + short - SAT_A_SHIFT, spec.hi_hz - short - SAT_A_SHIFT)
    piece = det(2, UP - 2e3, UP + 2e3)
    h.frame({A1: [whole, piece]})
    record = h.only()
    h.corr.tick()
    assert len(record.members) == 2
    assert record.width_lower_bound
