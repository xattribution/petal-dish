"""Tests of the plain-language advisory (ARCHITECTURE.md section 7, ``advisory.py``).

Records are built by hand for each verdict and state; the tests check that the advice
says what an operator needs (and nothing alarmist or unexplained), that names come
from the configuration, and that nothing non-finite or empty is ever produced.
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from petal_locate.config import SystemConfig
from petal_locate.server.api import schemas
from petal_locate.server.pipeline.advisory import (
    LABEL_TEXT,
    advise,
    format_distance,
    format_frequency,
    format_position,
)
from petal_locate.server.pipeline.events import EventRecord

T0 = 1_794_528_000_000_000_000


def _config() -> SystemConfig:
    tp = {
        "id": "t1",
        "uplink_center_hz": 300.0e6,
        "bandwidth_hz": 25e3,
        "translation_hz": -40e6,
        "g_over_t_dbk": -14.0,
        "sfd_dbw_m2": -136.0,
        "eirp_sat_dbw": 26.0,
    }
    channel = {"id": "x", "antenna": {"type": "parabolic", "diameter_m": 2.4}, "pointing": {"satellite_id": "sat-a"}}
    return SystemConfig.model_validate(
        {
            "name": "advisory-test",
            "satellites": [
                {"id": "sat-a", "name": "Sat A", "orbit": {"type": "geo", "lon_deg": 140.0}, "transponders": [tp]},
                {"id": "sat-b", "name": "Sat B", "orbit": {"type": "geo", "lon_deg": 160.0}, "transponders": [tp]},
            ],
            "sites": [
                {"id": "alpha", "name": "Alpha City", "lat_deg": 35.0, "lon_deg": 139.0, "channels": [channel]},
                {
                    "id": "far",
                    "name": "Far Town",
                    "lat_deg": 17.0,
                    "lon_deg": 139.0,
                    "channels": [{**channel, "id": "y"}],
                },
            ],
        }
    )


def _row(site: str, state: str, sat: str | None = "sat-a", margin: float | None = None) -> schemas.EvidenceRow:
    return schemas.EvidenceRow(
        channel_id=f"{site}-ch",
        site_id=site,
        sat_id=sat,
        state=state,  # type: ignore[arg-type]
        snr_db=20.0 if state == "seen" else None,
        cn0_dbhz=70.0 if state == "seen" else None,
        predicted_margin_db=margin,
    )


def _record(**fields: Any) -> EventRecord:
    base: dict[str, Any] = dict(
        id="EV-000001",
        seq=1,
        created_ns=T0,
        space="up",
        lo_hz=299.995e6,
        hi_hz=300.005e6,
        first_seen_ns=T0,
        last_seen_ns=T0,
        settle_from_ns=T0,
    )
    base.update(fields)
    return EventRecord(**base)


def _solution(ambiguous: bool = False) -> schemas.SolutionOut:
    cov = [[4.0e6, 0.0], [0.0, 1.0e6]]
    data: dict[str, Any] = {
        "lat_deg": -12.34567,
        "lon_deg": 190.5,  # unwrapped east of the antimeridian: shown as 169.5 W
        "height_m": 0.0,
        "cov_en_m2": cov,
        "cov_noise_en_m2": cov,
        "cov_consider_en_m2": [[0.0, 0.0], [0.0, 0.0]],
        "ellipse": {"semi_major_m": 4895.0, "semi_minor_m": 2448.0, "azimuth_deg": 90.0, "confidence": 0.95},
        "cep50_m": 1900.0,
        "chi2": 1.0,
        "dof": 2,
        "reduced_chi2": 0.5,
        "residuals": [0.1, -0.1, 0.2, 0.0],
        "ambiguities": [],
        "ambiguity_resolved_by": "none",
        "converged": True,
        "dominant_error": "noise",
        "n_measurements": 4,
    }
    if ambiguous:
        mirror = dict(data, lat_deg=12.34567, ambiguities=[], ambiguity_resolved_by="none")
        data["ambiguities"] = [mirror]
        data["ambiguity_resolved_by"] = "unresolved"
    return schemas.SolutionOut.model_validate(data)


UPLINK = dict(
    verdict="uplink",
    verdict_basis="two_sites",
    verdict_reason="It is seen at Alpha City and Far Town, 1,994 km apart, which no single nearby source could explain.",
    satellite_ids=["sat-a", "sat-b"],
    transponders=[
        schemas.EventTransponder(sat_id="sat-a", transponder_id="t1", downlink_lo_hz=259.995e6, downlink_hi_hz=260.005e6)
    ],
    evidence=[_row("alpha", "seen"), _row("far", "seen", "sat-b")],
)


def _all_text(advisory: schemas.Advisory) -> str:
    return " ".join([advisory.headline, advisory.detail, *advisory.actions])


def test_uplink_with_a_position_says_who_is_affected_what_to_do_and_where() -> None:
    record = _record(**UPLINK, solution_out=_solution(), classification=schemas.ClassificationOut(label="cw", confidence=0.9, features={}))
    advisory = advise(record, _config())
    assert advisory.headline == "Uplink interference on Sat A and Sat B at 300.0000 MHz"
    detail = advisory.detail
    assert "every user of transponder t1 of Sat A (the satellite channel that relays this frequency)" in detail
    assert "wherever they are" in detail
    assert "will not help" in detail
    assert "12.3457° S, 169.5000° W (WGS 84)" in detail
    # one convention everywhere (UX-9): the 95 % semi-axes, as the console shows them
    assert "within about 4.9 km (95 % ellipse with semi-axes 4.9 km by 2.4 km, major axis 90° from north)" in detail
    assert "9.8 km" not in detail
    actions = advisory.actions
    assert actions[0].startswith("Move the affected traffic to a channel, transponder or satellite")
    assert any("Do not relocate, re-point or shield terminals" in a for a in actions)
    assert any(a.startswith("Report the estimated transmitter position (12.3457° S, 169.5000° W (WGS 84), ±4.9 km") for a in actions)
    assert any(LABEL_TEXT["cw"] in a for a in actions)


def test_uplink_without_a_position_explains_why_and_what_is_happening() -> None:
    advisory = advise(_record(**UPLINK), _config())
    assert "No position estimate yet" in advisory.detail and "2 satellites" in advisory.detail
    assert any(
        a.startswith("Report the frequency, the start time (2026-11-13 00:00:00Z) and the affected satellite")
        for a in advisory.actions
    )
    single = dict(UPLINK, satellite_ids=["sat-a"])
    advisory = advise(_record(**single, last_solve_reason="unobservable"), _config())
    assert "Only one satellite relays this frequency" in advisory.detail
    two = advise(_record(**UPLINK, last_solve_reason="unobservable"), _config())
    assert "The last attempt gave no position (unobservable)." in two.detail


def test_uplink_with_an_ambiguous_position_lists_every_candidate_and_reports_no_single_fix() -> None:
    """UX-6: with an unresolved ambiguity the advice names every candidate with its
    ellipse, says the system cannot choose, and never reports one position as a fix."""
    advisory = advise(_record(**UPLINK, solution_out=_solution(ambiguous=True)), _config())
    detail = advisory.detail
    assert "2 positions fit the measurements equally well, and the system cannot yet choose between them" in detail
    assert "12.3457° S, 169.5000° W (WGS 84), 95 % ellipse with semi-axes 4.9 km by 2.4 km" in detail
    assert "12.3457° N, 169.5000° W (WGS 84), 95 % ellipse with semi-axes 4.9 km by 2.4 km" in detail
    assert "The transmitter is estimated to be at" not in detail
    actions = advisory.actions
    assert not any(a.startswith("Report the estimated transmitter position") for a in actions)
    report = next(a for a in actions if a.startswith("Report that the transmitter is at one of 2 positions"))
    assert "12.3457° S, 169.5000° W (WGS 84) ±4.9 km; 12.3457° N, 169.5000° W (WGS 84) ±4.9 km" in report
    assert "(each at 95 % confidence)" in report and "cannot yet choose" in report
    assert any(a.startswith("Do not act on any one of these positions alone") for a in actions)
    # three candidates: all of them are listed
    data = _solution(ambiguous=True).model_dump()
    data["ambiguities"].append(dict(data["ambiguities"][0], lat_deg=0.5))
    three = advise(_record(**UPLINK, solution_out=schemas.SolutionOut.model_validate(data)), _config())
    assert "3 positions fit" in three.detail and "0.5000° N" in three.detail
    assert any(a.startswith("Report that the transmitter is at one of 3 positions") for a in three.actions)


def test_local_says_only_that_site_is_affected_and_how_to_find_the_source() -> None:
    record = _record(
        verdict="local",
        verdict_basis="capable_not_seen",
        verdict_reason="Far Town, 1,994 km away, watches Sat A and would see it with 20 dB to spare (dB, decibels, "
        "compare signal strengths) if it came from the satellite, but does not.",
        evidence=[_row("alpha", "seen"), _row("far", "not_seen_capable", margin=20.0)],
        satellite_ids=["sat-a"],
    )
    advisory = advise(record, _config())
    assert advisory.headline == "Local interference at Alpha City"
    assert "Only Alpha City sees this signal" in advisory.detail
    assert "Other sites and other users of the satellite are not affected" in advisory.detail
    assert "dB, decibels" in advisory.detail  # the unit is explained where it appears
    text = _all_text(advisory)
    for word in ("filter", "shield", "nearby source", "receive chain"):
        assert word in text
    assert "No change of channel, transponder or satellite is needed" in advisory.actions[-1]


def test_undetermined_lists_the_missing_evidence_and_what_would_settle_it() -> None:
    record = _record(
        verdict="undetermined",
        verdict_basis="missing",
        verdict_reason="It is seen only at Alpha City; the spectrum feed of Far Town is offline or out of date.",
        verdict_gaps=[
            {"kind": "far_stale", "site_id": "far", "sat_id": "sat-a"},
            {"kind": "far_not_sensitive", "site_id": "far", "sat_id": "sat-a", "margin_db": 2.4},
            {"kind": "no_far_site", "sat_id": "sat-a", "near_site_id": "alpha"},
            {"kind": "far_unpredictable", "site_id": "far", "sat_id": "sat-b"},
        ],
        evidence=[_row("alpha", "seen"), _row("far", "stale")],
        satellite_ids=["sat-a"],
    )
    advisory = advise(record, _config())
    assert advisory.headline == "Interference detected at 300.0000 MHz: source not yet determined"
    assert "Seen at Alpha City through Sat A" in advisory.detail
    assert "affecting every user of the transponder" in advisory.detail
    actions = advisory.actions
    assert actions[0] == "Restore the spectrum feed from Far Town: its node is offline or its data is out of date."
    assert "only 2 dB above its detection threshold" in actions[1] and "decibels" in actions[1]
    assert "can see Sat A more than 200 km from Alpha City" in actions[2]
    assert "no site sees the signal through Sat B yet" in actions[3]
    assert "Ask users of the transponder at other locations" in actions[-1]


def test_settling_event_says_it_is_still_checking() -> None:
    record = _record(verdict_reason="Gathering evidence.", evidence=[_row("alpha", "seen")])
    advisory = advise(record, _config())
    assert advisory.headline.endswith("checking where it enters")
    assert advisory.actions[0].startswith("Wait a few seconds")


def test_unmapped_event_explains_that_no_satellite_relays_it() -> None:
    record = _record(space="down", lo_hz=260.013e6, hi_hz=260.014e6, evidence=[_row("alpha", "seen", sat=None)],
                     verdict_basis="missing", verdict_gaps=[{"kind": "no_far_site", "sat_id": None, "near_site_id": "alpha"}])
    advisory = advise(record, _config())
    assert "outside every transponder passband, so no satellite relays it" in advisory.detail
    assert any("that monitors this frequency" in a for a in advisory.actions)
    assert "nearby" in advisory.actions[-1]


def test_lost_closed_and_merged_events_say_so() -> None:
    lost = _record(**UPLINK, lost=True, lost_ns=T0 + 90 * 10**9, solution_out=_solution())
    advisory = advise(lost, _config())
    assert advisory.headline.startswith("No longer detected: uplink interference")
    # final recheck: the advice no longer says 'no action is needed' and then 'report it so it can
    # be stopped', and it says what reopening does now (a long break sets the history aside)
    assert advisory.detail.startswith(
        "The signal is no longer detected at any site at 2026-11-13 00:01:30Z. If the same frequency is detected "
        "again within 15 minutes, this event reopens: within 30 s it keeps its position and measurements and checks "
        "them against new ones; after a longer break they are set aside and the signal is located afresh"
    )
    assert "keeps its history" not in advisory.detail
    assert advisory.actions[0] == "Nothing needs to be moved or changed while the signal is off; act again if it returns."
    assert not any("No action is needed" in a or "found and stopped" in a for a in advisory.actions)
    report = next(a for a in advisory.actions if a.startswith("Report the estimated transmitter position"))
    assert report.endswith("saying that it stopped at 2026-11-13 00:01:30Z and may start again.")
    closed = advise(_record(**UPLINK, closed=True), _config())
    assert closed.headline.startswith("Closed: uplink interference") and closed.actions[0].startswith("Nothing to do")
    merged = advise(_record(**UPLINK, closed=True, merged_into="EV-000007"), _config())
    assert merged.headline == "Merged into event EV-000007"
    assert merged.actions == ["Follow event EV-000007 instead."]


def test_simulated_advice_is_labelled_and_ids_stand_in_without_a_configuration() -> None:
    advisory = advise(_record(**UPLINK), None, simulated=True)
    assert advisory.headline.startswith("Simulation: Uplink interference on sat-a and sat-b")
    assert advisory.detail.endswith("These results come from the simulator, not from live signals.")


@pytest.mark.parametrize("verdict", ["uplink", "local", "undetermined"])
@pytest.mark.parametrize("state", [{}, {"lost": True}, {"closed": True}])
def test_every_combination_gives_complete_finite_text(verdict: str, state: dict[str, Any]) -> None:
    record = _record(
        verdict=verdict,
        verdict_basis="missing",
        verdict_reason="Reason.",
        evidence=[_row("alpha", "seen")],
        satellite_ids=["sat-a"],
        **state,
    )
    advisory = advise(record, _config())
    assert advisory.headline and advisory.detail and advisory.actions
    text = _all_text(advisory)
    assert "nan" not in text.lower().split() and "inf" not in text.lower().split()
    assert "None" not in text
    assert "  " not in advisory.detail
    for alarmist in ("!", "URGENT", "DANGER", "attack"):
        assert alarmist not in text


def test_formatters() -> None:
    assert format_frequency(299.9875e6, 300.0125e6) == "300.0000 MHz (about 25 kHz wide)"
    assert format_frequency(14.0e9 - 18e6, 14.0e9 + 18e6) == "14000.0000 MHz (about 36.0 MHz wide)"
    assert format_frequency(300e6, 300e6 + 500) == "300.0002 MHz (about 500 Hz wide)"
    assert format_frequency(300e6, 300e6 + 2500) == "300.0013 MHz (about 2.5 kHz wide)"
    assert format_position(21.307, -157.858) == "21.3070° N, 157.8580° W (WGS 84)"
    assert format_position(-0.5, 359.0) == "0.5000° S, 1.0000° W (WGS 84)"
    assert format_distance(850.0) == "850 m"
    assert format_distance(4200.0) == "4.2 km"
    assert format_distance(1_250_000.0) == "1,250 km"
    assert format_distance(math.inf) == "an unknown distance"


# ----------------------------------------------------------------------------- review fixes


def _seen_down(site: str, channel: str, sat: str | None, lo: float, hi: float, state: str = "seen") -> schemas.EvidenceRow:
    return schemas.EvidenceRow(
        channel_id=channel, site_id=site, sat_id=sat, state=state,  # type: ignore[arg-type]
        snr_db=20.0 if state == "seen" else None, cn0_dbhz=70.0 if state == "seen" else None,
        predicted_margin_db=None, downlink_lo_hz=lo, downlink_hi_hz=hi,
    )


def test_local_advice_quotes_the_received_frequency_site_and_channels() -> None:
    """UX-3: a local source radiates at the downlink frequency the site receives (260 MHz
    here), not at the uplink frequency the event is filed under (300 MHz)."""
    record = _record(
        verdict="local",
        verdict_basis="capable_not_seen",
        verdict_reason="Far Town, 1,994 km away, would see it if it came from the satellite, but does not.",
        evidence=[
            _seen_down("alpha", "alpha-a", "sat-a", 259.995e6, 260.005e6),
            _seen_down("alpha", "alpha-b", "sat-b", 259.995e6, 260.005e6),
            _seen_down("far", "far-a", "sat-a", 259.995e6, 260.005e6, "not_seen_capable"),
        ],
        satellite_ids=["sat-a", "sat-b"],
    )
    advisory = advise(record, _config())
    assert (
        "Only Alpha City sees this signal, received near 260.0000 MHz (about 10 kHz wide) at Alpha City on "
        "alpha-a and alpha-b." in advisory.detail
    )
    assert "300.0000" not in _all_text(advisory)
    assert advisory.actions[0].startswith("Look for a nearby source around Alpha City (tune to 260.0000 MHz")


def test_undetermined_advice_quotes_what_each_site_receives() -> None:
    """M3: an undetermined signal is described by what the sites receive, through which
    satellite, with the uplink frequency only as what it would be on the uplink."""
    record = _record(
        verdict="undetermined",
        verdict_basis="missing",
        verdict_reason="It is seen only at Alpha City.",
        evidence=[_seen_down("alpha", "alpha-a", "sat-a", 259.995e6, 260.005e6)],
        satellite_ids=["sat-a", "sat-b"],
    )
    advisory = advise(record, _config())
    assert advisory.headline == "Interference received at 260.0000 MHz: source not yet determined"
    assert advisory.detail.startswith(
        "Received near 260.0000 MHz (about 10 kHz wide) at Alpha City on alpha-a through Sat A. If it reaches the "
        "satellites, it is transmitted near 300.0000 MHz (about 10 kHz wide) on the uplink."
    )
    # two received frequencies (two satellites with different translations): both, and the uplink headline
    record.evidence.append(_seen_down("far", "far-b", "sat-b", 267.395e6, 267.405e6))
    two = advise(record, _config())
    assert two.headline == "Interference detected at 300.0000 MHz uplink: source not yet determined"
    assert "near 260.0000 MHz" in two.detail and "near 267.4000 MHz (about 10 kHz wide) at Far Town on far-b through Sat B" in two.detail


def test_inconsistent_fit_is_described_as_unreliable_and_not_reported() -> None:
    data = _solution().model_dump()
    data.update(chi2=4.0e4, dof=2, reduced_chi2=2.0e4, fit_consistent=False, fit_p_value=0.0, ellipse_scale=141.4)
    data["ellipse"] = dict(data["ellipse"], semi_major_m=692_000.0, semi_minor_m=346_000.0)
    record = _record(**UPLINK, solution_out=schemas.SolutionOut.model_validate(data))
    advisory = advise(record, _config())
    assert "The measurements do not fit a single stationary transmitter (reduced chi-square 2.0e+04 against at most 5)" in advisory.detail
    assert "is not a reliable position" in advisory.detail and "enlarged 141 times" in advisory.detail
    assert not any(a.startswith("Report the estimated transmitter position") for a in advisory.actions)
    assert any("do not report the computed position" in a for a in advisory.actions)
    assert any(a.startswith("Check whether more than one transmitter is on this frequency") for a in advisory.actions)
    data["ellipse"] = dict(data["ellipse"], semi_major_m=112_000_000.0)
    huge = advise(_record(**UPLINK, solution_out=schemas.SolutionOut.model_validate(data)), _config())
    assert "no meaningful error ellipse can be given" in huge.detail and "112,000 km" not in huge.detail


def test_before_a_fix_only_satellites_seen_through_are_named_as_affected() -> None:
    """ADAPT-1: before a position exists, a satellite of the frequency plan the signal was
    never seen through is only a possible relay."""
    record = _record(**dict(UPLINK, evidence=[_row("alpha", "seen"), _row("far", "seen")]))
    advisory = advise(record, _config())
    assert advisory.headline == "Uplink interference on Sat A at 300.0000 MHz"
    assert "It may also reach Sat B: their transponders relay this frequency, but no site has seen it" in advisory.detail
    assert "(on Sat B too, until it is known whether they can see the transmitter)" in advisory.actions[0]


def test_report_quotes_the_onset_of_the_current_transmission() -> None:
    later = T0 + 3_723 * 1_000_000_000
    record = _record(**UPLINK, solution_out=_solution(), onset_ns=later)
    advisory = advise(record, _config())
    report = next(a for a in advisory.actions if a.startswith("Report the estimated transmitter position"))
    assert "the start time (2026-11-13 01:02:03Z)" in report
    record.recheck = "Detected again after a break of 12 s: the position from before the break is being checked."
    rechecked = advise(record, _config())
    assert record.recheck in rechecked.detail
    assert any("once new measurements confirm it" in a for a in rechecked.actions)


def test_a_fix_through_a_satellite_whose_position_is_in_doubt_says_the_ellipse_may_miss() -> None:
    """Final recheck (ephemeris-error): the calibrations put two satellites' positions
    wrong beyond their configured uncertainty, which the solver does not budget for; the
    event was located with the truth outside its ellipse while the advice said 'Report
    ... ±241 m'. A fix whose rows go through such a satellite is still reported, with the
    warning that the ellipse does not include that error. Satellites the rows do not go
    through do not matter."""
    from petal_locate.server.pipeline.events import MeasurementEntry

    pair = schemas.PairRef(
        a=schemas.ChannelRef(sat_id="sat-a", site_id="alpha", channel_id="x"),
        b=schemas.ChannelRef(sat_id="sat-b", site_id="far", channel_id="y"),
    )
    row = schemas.MeasurementRow(t_ns=T0, kind="tdoa", pair=pair, value=1e-3, sigma=1e-7, residual=None,
                                 snr_db=20.0, calibrated=False, used=True, reason=None)
    entries = [MeasurementEntry(1, row, None)]
    clear = _record(**UPLINK, solution_out=_solution(), measurements=entries, solution_used=(1,))
    assert "may lie outside" not in _all_text(advise(clear, _config()))
    doubted = _record(**UPLINK, solution_out=_solution(), measurements=entries, solution_used=(1,),
                      doubted_satellite_ids=("sat-b",))
    advisory = advise(doubted, _config())
    assert ("The reference calibrations put the position of Sat B wrong beyond its configured uncertainty, and the "
            "ellipse does not include that error yet: the transmitter may lie outside it.") in advisory.detail
    report = next(a for a in advisory.actions if a.startswith("Report the estimated transmitter position"))
    assert "±4.9 km at 95 % confidence; the ephemeris of Sat B is in doubt, so the transmitter may lie outside this)" in report
    other = _record(**UPLINK, solution_out=_solution(), measurements=entries, solution_used=(1,),
                    doubted_satellite_ids=("sat-c",))
    assert "may lie outside" not in _all_text(advise(other, _config()))
    both = _record(**UPLINK, solution_out=_solution(), measurements=entries, solution_used=(1,),
                   doubted_satellite_ids=("sat-a", "sat-b"))
    both_advice = advise(both, _config())
    assert ("The reference calibrations put the positions of Sat A and Sat B wrong beyond their configured "
            "uncertainty, and the ellipse does not include that error yet") in both_advice.detail
    # the report says it in the plural too (it read "the ephemeris of Sat A and Sat B is in doubt")
    report = next(a for a in both_advice.actions if a.startswith("Report the estimated transmitter position"))
    assert "; the ephemerides of Sat A and Sat B are in doubt, so the transmitter may lie outside this)" in report


def test_a_fix_whose_ellipse_includes_an_ephemeris_allowance_says_so_and_drops_the_warning() -> None:
    """D1: once the solve widened the position sigma of the satellite the calibrations put
    wrong beyond its configured uncertainty (``EventRecord.solution_allowance``), the
    ellipse covers that error: the advice says the ellipse is widened, by what factor on
    which satellite's sigma and how much longer the 95 % ellipse's major axis is for it
    (``EventRecord.solution_widening``), and asks for the ephemeris to be checked, with
    no 'may lie outside'. An allowance at its largest scale keeps the warning; so does a
    doubt that came after the solve. Satellites the rows do not go through are not
    mentioned."""
    from petal_locate.server.pipeline.events import EphemerisAllowance, MeasurementEntry

    pair = schemas.PairRef(
        a=schemas.ChannelRef(sat_id="sat-a", site_id="alpha", channel_id="x"),
        b=schemas.ChannelRef(sat_id="sat-b", site_id="far", channel_id="y"),
    )
    row = schemas.MeasurementRow(t_ns=T0, kind="tdoa", pair=pair, value=1e-3, sigma=1e-7, residual=None,
                                 snr_db=20.0, calibrated=True, used=True, reason=None)
    entries = [MeasurementEntry(1, row, None)]
    base = dict(UPLINK, solution_out=_solution(), measurements=entries, solution_used=(1,))
    widened = EphemerisAllowance("sat-b", 9.51, (951.0, 9510.0, 9510.0))
    covered = advise(_record(**base, doubted_satellite_ids=("sat-b",), solution_allowance=(widened,),
                             solution_widening=1.21), _config())
    assert ("The ellipse is widened for the position of Sat B, which the reference calibrations put wrong beyond its "
            "configured uncertainty (its position sigma taken 9.5 times larger, about 9.5 km one sigma); check its "
            "ephemeris. Widened, the 95 % ellipse's major axis is 21 % longer than with the configured "
            "uncertainty.") in covered.detail
    assert "may lie outside" not in _all_text(covered)
    report = next(a for a in covered.actions if a.startswith("Report the estimated transmitter position"))
    assert "±4.9 km at 95 % confidence; the ellipse is widened for the ephemeris of Sat B, which is in doubt)" in report
    # how much longer: not said when not known; 'times as long' from twice the length
    unknown = advise(_record(**base, solution_allowance=(widened,)), _config())
    assert "configured uncertainty (its position sigma taken 9.5 times larger" in unknown.detail
    assert "Widened, the 95 % ellipse" not in unknown.detail
    twice = advise(_record(**base, solution_allowance=(widened,), solution_widening=2.4), _config())
    assert "Widened, the 95 % ellipse's major axis is 2.4 times as long as with the configured uncertainty." in \
        twice.detail
    # the doubt cleared but the published solve still carries the allowance: it is still described
    kept = advise(_record(**base, solution_allowance=(widened,), solution_widening=1.21), _config())
    assert "The ellipse is widened for the position of Sat B" in kept.detail and "may lie outside" not in _all_text(kept)
    # two satellites
    other = EphemerisAllowance("sat-a", 8.0, (800.0, 8000.0, 8000.0))
    two = advise(_record(**base, solution_allowance=(other, widened), solution_widening=1.004), _config())
    assert ("The ellipse is widened for the positions of Sat A and Sat B, which the reference calibrations put wrong "
            "beyond their configured uncertainty (their position sigmas taken 8 and 9.5 times larger, about 8.0 km "
            "and 9.5 km one sigma); check their ephemerides. Widened, the 95 % ellipse's major axis is less than 1 % "
            "longer than with the configured uncertainty.") in two.detail
    # the report says it in the plural (it read "the ellipse is widened for the ephemeris of Sat A and Sat B,
    # which is in doubt")
    report = next(a for a in two.actions if a.startswith("Report the estimated transmitter position"))
    assert "; the ellipse is widened for the ephemerides of Sat A and Sat B, which are in doubt)" in report
    # at the largest scale: the warning stays
    top = EphemerisAllowance("sat-b", 20.0, (2000.0, 20000.0, 20000.0), capped=True)
    capped = advise(_record(**base, doubted_satellite_ids=("sat-b",), solution_allowance=(top,),
                            solution_widening=3.0), _config())
    # capped also means not sized (no evidence, or a fit that failed): the text must hold for both
    assert ("The reference calibrations put the position of Sat B wrong beyond its configured uncertainty, by more "
            "than the largest allowance the system makes or by an amount they cannot size; the ellipse includes that "
            "largest allowance (its position sigma taken 20 times larger, about 20 km one sigma), but the transmitter "
            "may still lie outside it; check its ephemeris. Widened, the 95 % ellipse's major axis is 3 times as long "
            "as with the configured uncertainty.") in capped.detail
    report = next(a for a in capped.actions if a.startswith("Report the estimated transmitter position"))
    assert "; the ephemeris of Sat B is in doubt, so the transmitter may lie outside this" in report
    pair_capped = advise(_record(**base, solution_allowance=(top, EphemerisAllowance("sat-a", 20.0, (2e3, 2e4, 2e4),
                                                                                      capped=True))), _config())
    assert ("put the positions of Sat B and Sat A wrong beyond their configured uncertainty, by more than the largest "
            "allowance the system makes or by amounts they cannot size; the ellipse includes that largest allowance "
            "(their position sigmas taken 20 and 20 times larger") in pair_capped.detail
    report = next(a for a in pair_capped.actions if a.startswith("Report the estimated transmitter position"))
    assert "; the ephemerides of Sat B and Sat A are in doubt, so the transmitter may lie outside this" in report
    # a satellite the rows do not go through: nothing said
    away = EphemerisAllowance("sat-c", 4.0, (400.0, 4000.0, 4000.0))
    assert "widened" not in _all_text(advise(_record(**base, solution_allowance=(away,), solution_widening=1.1),
                                              _config()))
