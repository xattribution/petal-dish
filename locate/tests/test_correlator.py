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
from petal_locate.core.detect import Detection
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
    assert record.state == "characterizing" and record.verdict == "local"
    h.run(0.5, {})
    assert record.state == "lost" and record.lost_ns == h.clock()
    assert record.transitions[-1].reason == "all detections released"
    # It comes back on another track id: the same event re-opens.
    h.run(60.0, {})
    h.run(0.5, {A1: [det(9, UP - 4e3, UP + 4e3)]})
    assert h.only() is record
    assert record.state == "characterizing"
    assert record.transitions[-1].reason == "detected again"
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
    s1 = h.book.add_measurement(record.id, _row("tdoa"), m1)
    s2 = h.book.add_measurement(record.id, _row("fdoa", value=0.5), m2)
    assert (s1, s2) == (2, 3) and record.state == "locating"
    assert h.book.solver_measurements(record.id) == [m1, m2]
    assert h.book.solver_measurements(record.id, limit=1) == [m2]
    with pytest.raises(ValueError):
        h.book.set_solution(record.id, FakeSolution([0.5, -0.2]), T0, used=[s1])
    assert h.book.set_solution(record.id, None, T0, reason="unobservable")
    assert record.state == "locating" and record.last_solve_reason == "unobservable"
    assert h.book.set_solution(record.id, FakeSolution([0.5, -0.2]), T0 + 1, used=[s1, s2])
    assert record.state == "located"
    residuals = {e.seq: e.row.residual for e in record.measurements}
    assert residuals == {1: None, 2: 0.5, 3: -0.2}
    assert record.solution_history[-1].semi_major_m == pytest.approx(4895.0)
    bad = FakeSolution([math.inf, 0.0])
    assert not h.book.set_solution(record.id, bad, T0 + 2, used=[s1, s2])

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
    assert [m["residual"] for m in document["measurements"]] == [None, 0.5, -0.2]
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
