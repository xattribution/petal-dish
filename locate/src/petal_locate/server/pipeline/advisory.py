"""Plain-language guidance for operators (ARCHITECTURE.md section 7, ``advisory.py``).

``advise(record, config)`` turns what the pipeline knows about an event into a short
headline, one paragraph of explanation and a list of concrete actions, for operators
who are not RF specialists:

- ``uplink``: the interference reaches the satellite itself, so every user of that
  transponder is affected wherever they are; change channel, transponder or satellite;
  moving or shielding a terminal will not help; report the estimated location, with
  its uncertainty, when there is one.
- ``local``: only that site is affected; most likely a nearby terrestrial source;
  find, filter, shield or relocate locally; other sites are unaffected.
- ``undetermined``: what evidence is missing and what would settle it.

Rules the texts follow:

- Ellipses are always given by their 95 % (``solver.confidence``) semi-axes, the same
  convention as the console's figures; "within about X" and "±X" are the semi-major.
- A position is only reported as a fix when it is one (``SolutionOut.is_fix``: a
  consistent fit with redundancy and a bounded ellipse) and unique. An inconsistent fit
  (``SolutionOut.fit_consistent`` false) or another solution that is not a fix (with its
  ``not_fix_reason``) is described as unreliable and not reported; an unresolved
  ambiguity lists every candidate and says the system
  cannot choose; a position being re-checked (``EventRecord.recheck``) is reported only
  once new measurements confirm it.
- Satellites: before a position exists, satellites the signal was seen through (or
  measured through) are named as affected, the rest of the frequency plan as "may also
  reach"; once one exists the correlator has already left out satellites that cannot see
  it (``EventRecord.not_visible_satellite_ids``), and the advice says so.
- Frequencies: an uplink event is named by its uplink frequency; what a site receives
  (``local`` and ``undetermined``) by the received (downlink) frequency of its ``seen``
  evidence rows, with the site and channels.
- The start time to report is the onset of the current transmission
  (``EventRecord.onset``), given in UTC.
- A signal that cannot be located (``TaskingStatus.unlocatable_reason``) says so and why.
- A fix whose rows go through a satellite the reference calibrations put beyond its
  configured position uncertainty (``EventRecord.doubted_satellite_ids``) is still
  reported, with the warning that its ellipse does not include that error.

Terms are explained the first time they appear; numbers are rounded to what matters.
Simulated events say so in the headline. Pure functions, no I/O.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Final

from petal_locate.server.api import schemas
from petal_locate.server.pipeline.correlator import REOPEN_WINDOW_S
from petal_locate.server.pipeline.events import REACQUIRE_KEEP_S, EventRecord

__all__ = ["LABEL_TEXT", "advise", "format_distance", "format_frequency", "format_position"]

LABEL_TEXT: Final[dict[str, str]] = {
    "cw": "a single unmodulated tone (CW)",
    "swept": "a tone sweeping across the band (swept)",
    "noise": "noise-like energy spread across the band",
    "modulated": "a modulated carrier, like a data or voice signal",
    "pulsed": "a signal that switches on and off (pulsed)",
}
"""How each waveform class is described to an operator (``unknown`` is not described)."""


_MEANINGLESS_ELLIPSE_M: Final[float] = 2.0e6
"""An enlarged ellipse wider than this (semi-major, m) is not quoted: it says nothing."""


# ------------------------------------------------------------------- formatting


def format_frequency(lo_hz: float, hi_hz: float, at_least: bool = False) -> str:
    """``"301.0125 MHz (about 25 kHz wide)"``: center in MHz to 100 Hz, width rounded;
    ``at_least``: the width is a lower bound (the signal filled the window it was
    measured in), ``"(at least 25 kHz wide)"``."""
    centre = 0.5 * (lo_hz + hi_hz)
    width = max(hi_hz - lo_hz, 0.0)
    qualifier = "at least" if at_least else "about"
    if width >= 1e6:
        span = f"{qualifier} {width / 1e6:.1f} MHz wide"
    elif width >= 1e3:
        span = f"{qualifier} {width / 1e3:.0f} kHz wide" if width >= 10e3 else f"{qualifier} {width / 1e3:.1f} kHz wide"
    else:
        span = f"{qualifier} {width:.0f} Hz wide"
    return f"{centre / 1e6:.4f} MHz ({span})"


def format_position(lat_deg: float, lon_deg: float) -> str:
    """``"21.3070° N, 157.8580° W (WGS 84)"``: decimal degrees, hemisphere letters, datum."""
    lon = ((lon_deg + 180.0) % 360.0) - 180.0
    ns = "N" if lat_deg >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat_deg):.4f}° {ns}, {abs(lon):.4f}° {ew} (WGS 84)"


def format_distance(metres: float) -> str:
    """Kilometres rounded to what matters: ``"850 m"``, ``"4.2 km"``, ``"37 km"``, ``"1,250 km"``."""
    if not math.isfinite(metres):
        return "an unknown distance"
    if metres < 1000.0:
        return f"{metres:.0f} m"
    km = metres / 1000.0
    if km < 10.0:
        return f"{km:.1f} km"
    return f"{km:,.0f} km"


def _join(items: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


class _Names:
    """Display names from the configuration, falling back to ids."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def site(self, site_id: str | None) -> str:
        if site_id is None:
            return "an unknown site"
        if self._config is not None:
            try:
                return str(self._config.site(site_id).name)
            except (ValueError, AttributeError):
                pass
        return site_id

    def satellite(self, sat_id: str | None) -> str:
        if sat_id is None:
            return "no satellite"
        if self._config is not None:
            try:
                return str(self._config.satellite(sat_id).name)
            except (ValueError, AttributeError):
                pass
        return sat_id

    def min_separation_km(self) -> float:
        if self._config is not None:
            try:
                return float(self._config.processing.detection.min_site_separation_km)
            except AttributeError:
                pass
        return 200.0

    def max_reduced_chi2(self) -> float | None:
        if self._config is not None:
            try:
                return float(self._config.processing.solver.max_reduced_chi2)
            except AttributeError:
                pass
        return None


# ------------------------------------------------------------------- advice


def advise(record: EventRecord, config: Any, *, simulated: bool = False) -> schemas.Advisory:
    """Guidance for one event.

    record: the event (``pipeline.events.EventRecord``).
    config: the ``SystemConfig`` for site and satellite names (None: ids are used).
    simulated: label the advice as coming from the simulator.
    Returns ``schemas.Advisory(headline, detail, actions)``; never empty, never NaN.
    """
    names = _Names(config)
    if record.merged_into is not None:
        headline = f"Merged into event {record.merged_into}"
        detail = (
            f"This event turned out to be the same signal as event {record.merged_into}, "
            "which now carries all of its detections and evidence."
        )
        actions = [f"Follow event {record.merged_into} instead."]
        return _finish(headline, detail, actions, simulated)

    if record.verdict == "uplink":
        headline, detail, actions = _uplink(record, names)
    elif record.verdict == "local":
        headline, detail, actions = _local(record, names)
    else:
        headline, detail, actions = _undetermined(record, names)

    if record.state == "lost":
        headline = f"No longer detected: {headline[0].lower()}{headline[1:]}"
        stopped = f" at {_utc_text(record.lost_ns)}" if record.lost_ns is not None else ""
        detail = (
            f"The signal is no longer detected at any site{stopped}. If the same frequency is detected again within "
            f"{_minutes_text(REOPEN_WINDOW_S)}, this event reopens: within {REACQUIRE_KEEP_S:g} s it keeps its "
            "position and measurements and checks them against new ones; after a longer break they are set aside "
            "and the signal is located afresh (the history is kept). " + detail
        )
        # what was measured stays worth reporting: the transmitter may start again
        keep = [
            a.replace(" so the transmitter can be found and stopped.",
                      f", saying that it stopped{stopped} and may start again.")
            for a in actions if a.startswith("Report")
        ]
        actions = ["Nothing needs to be moved or changed while the signal is off; act again if it returns."] + keep
    elif record.state == "closed":
        headline = f"Closed: {headline[0].lower()}{headline[1:]}"
        detail = "The operator closed this event. " + detail
        actions = ["Nothing to do: the event is closed. A new event is raised if the signal returns after it stops."]
    return _finish(headline, detail, actions, simulated)


def _finish(headline: str, detail: str, actions: list[str], simulated: bool) -> schemas.Advisory:
    if simulated:
        headline = f"Simulation: {headline}"
        detail = f"{detail} These results come from the simulator, not from live signals."
    return schemas.Advisory(headline=headline, detail=" ".join(detail.split()), actions=actions)


def _frequency(record: EventRecord) -> str:
    return format_frequency(record.lo_hz, record.hi_hz, bool(record.width_lower_bound))


def _minutes_text(seconds: float) -> str:
    """``15 minutes`` (whole minutes), else ``45 s``."""
    return f"{seconds / 60.0:g} minutes" if seconds >= 120.0 and seconds % 60.0 == 0.0 else f"{seconds:g} s"


def _utc_text(t_ns: int) -> str:
    """``2026-11-13 16:56:52Z`` of a UTC ns time."""
    seconds = int(t_ns) // 1_000_000_000
    stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M:%SZ")


def _start_time(record: EventRecord) -> str:
    """``the start time (2026-11-13 16:56:52Z)``: the onset of the current transmission."""
    try:
        return f"the start time ({_utc_text(record.onset)})"
    except (OverflowError, OSError, ValueError):
        return "the start time"


def _ellipse_text(ellipse: schemas.EllipseOut) -> str:
    """``95 % ellipse with semi-axes 4.9 km by 2.4 km``."""
    confidence = round(100.0 * ellipse.confidence)
    return (
        f"{confidence} % ellipse with semi-axes {format_distance(ellipse.semi_major_m)} by "
        f"{format_distance(ellipse.semi_minor_m)}"
    )


def _received_groups(record: EventRecord, names: _Names, *, via: bool) -> list[str]:
    """What the sites that see the signal receive, one phrase per received frequency:
    ``near 260.0050 MHz (about 10 kHz wide) at Busan on busan-pac and busan-io`` (with
    ``via``: ``... through MUOS Pacific``). From the ``seen`` evidence rows' downlink
    ranges (an event outside every passband: its own range)."""
    groups: dict[int, dict[str, Any]] = {}
    for row in record.evidence:
        if row.state != "seen":
            continue
        if row.downlink_lo_hz is not None and row.downlink_hi_hz is not None:
            lo, hi = row.downlink_lo_hz, row.downlink_hi_hz
        elif not record.mapped:
            lo, hi = record.lo_hz, record.hi_hz
        else:  # an older row without its range: the transponder's mapping of the event
            tp = next((t for t in record.transponders if t.sat_id == row.sat_id), None)
            if tp is None:
                continue
            lo, hi = tp.downlink_lo_hz, tp.downlink_hi_hz
        key = int(round(0.5 * (lo + hi) / 1e3))  # one group per kHz of received centre
        group = groups.setdefault(key, {"lo": lo, "hi": hi, "sites": [], "channels": [], "sats": []})
        group["lo"], group["hi"] = min(group["lo"], lo), max(group["hi"], hi)
        for name, value in (("sites", names.site(row.site_id)), ("channels", row.channel_id)):
            if value not in group[name]:
                group[name].append(value)
        if row.sat_id is not None and names.satellite(row.sat_id) not in group["sats"]:
            group["sats"].append(names.satellite(row.sat_id))
    phrases = []
    for group in groups.values():
        text = (
            f"near {format_frequency(group['lo'], group['hi'], bool(record.width_lower_bound))} at "
            f"{_join(group['sites'])} on {_join(group['channels'])}"
        )
        if via and group["sats"]:
            text += f" through {_join(group['sats'])}"
        phrases.append(text)
    return phrases


def _transponder_phrase(record: EventRecord, names: _Names, sat_ids: Sequence[str] | None = None) -> str:
    """``transponder shared-1 of MUOS Pacific`` etc., explained on first use (only those
    of ``sat_ids`` when given and any match)."""
    rows = list(record.transponders)
    if sat_ids is not None:
        rows = [t for t in rows if t.sat_id in sat_ids] or rows
    if not rows:
        return "the affected satellite channel"
    parts = [f"transponder {t.transponder_id} of {names.satellite(t.sat_id)}" for t in rows]
    return f"{_join(parts)} (the satellite channel that relays this frequency)"


def _seen_sites(record: EventRecord, names: _Names) -> list[str]:
    return [names.site(s) for s in record.seen_sites()]


def _seen_satellites(record: EventRecord, names: _Names) -> list[str]:
    out: list[str] = []
    for row in record.evidence:
        if row.state == "seen" and row.sat_id is not None:
            name = names.satellite(row.sat_id)
            if name not in out:
                out.append(name)
    return out


def _measured_satellite_ids(record: EventRecord) -> set[str]:
    """Satellites the signal was seen through or measured through (an accepted pair)."""
    out = {row.sat_id for row in record.evidence if row.state == "seen" and row.sat_id is not None}
    for entry in record.measurements:
        if entry.row.used:
            out.update((entry.row.pair.a.sat_id, entry.row.pair.b.sat_id))
    return out


def _relaying(record: EventRecord) -> tuple[list[str], list[str]]:
    """(satellites known to relay the signal, satellites that may also relay it). Once a
    consistent position exists, ``satellite_ids`` is already limited to satellites that
    can see it, so all are known; before, only those seen or measured through are."""
    listed = list(record.satellite_ids)
    solution = record.solution_out
    if solution is not None and solution.is_fix:
        return listed, []
    measured = _measured_satellite_ids(record)
    known = [s for s in listed if s in measured]
    if not known:
        return listed, []
    return known, [s for s in listed if s not in measured]


def _label_sentence(record: EventRecord) -> str | None:
    if record.classification is None:
        return None
    return LABEL_TEXT.get(record.classification.label)


def _unlocatable(record: EventRecord) -> str | None:
    tasking = record.tasking
    return tasking.unlocatable_reason if tasking is not None else None


class _Position:
    """What the advice says about the position: the detail sentence, the report text (a
    unique, consistent position not under re-check, else None) and flags."""

    def __init__(self, sentence: str, report: str | None, *, ambiguous: bool = False, inconsistent: bool = False,
                 candidates: str | None = None, unlocatable: str | None = None, not_fix: str | None = None) -> None:
        self.sentence = sentence
        self.report = report
        self.ambiguous = ambiguous
        self.inconsistent = inconsistent
        self.not_fix = not_fix  # why a consistent solution is not a fix (no redundancy, a huge ellipse)
        self.candidates = candidates
        self.unlocatable = unlocatable


def _position(record: EventRecord, names: _Names) -> _Position:
    solution = record.solution_out
    if solution is None:
        unlocatable = _unlocatable(record)
        if unlocatable is not None:
            return _Position(f"No position can be computed. {unlocatable}", None, unlocatable=unlocatable)
        relaying = len(record.satellite_ids)
        if relaying < 2:
            sentence = (
                "Only one satellite relays this frequency, so the transmitter cannot be located "
                "from the differences between satellite copies."
            )
            return _Position(sentence, None, unlocatable=sentence)
        sentence = (
            f"No position estimate yet: the system is comparing the copies of the signal "
            f"relayed by {relaying} satellites."
        )
        if record.last_solve_reason:
            sentence += f" The last attempt gave no position ({record.last_solve_reason})."
        if record.recheck:
            sentence += f" {record.recheck}"
        return _Position(sentence, None)
    position = format_position(solution.lat_deg, solution.lon_deg)
    ellipse = solution.ellipse
    confidence = round(100.0 * ellipse.confidence)
    if not solution.fit_consistent:
        limit = names.max_reduced_chi2()
        against = f" against at most {limit:g}" if limit is not None else ""
        enlarged = (
            f" enlarged {solution.ellipse_scale:,.0f} times" if solution.ellipse_scale >= 1.5 else " enlarged"
        )
        if ellipse.semi_major_m > _MEANINGLESS_ELLIPSE_M:
            spread = "the disagreement is so large that no meaningful error ellipse can be given."
        else:
            spread = (
                f"its {_ellipse_text(ellipse)} ({enlarged.strip()} to allow for the disagreement) may still not "
                "contain the transmitter."
            )
        sentence = (
            f"The measurements do not fit a single stationary transmitter (reduced chi-square "
            f"{_chi2_text(solution.reduced_chi2)}{against}): more than one transmitter may share this frequency, "
            "the transmitter may be moving, or a site clock or satellite position may be wrong. The best fit, at "
            f"{position}, is not a reliable position; {spread}"
        )
        return _Position(sentence, None, inconsistent=True)
    if not solution.is_fix:
        reason = solution.not_fix_reason or "it does not meet the conditions of a fix"
        sentence = (
            f"The best fit so far, at {position} ({_ellipse_text(ellipse)}), is not a reliable position: "
            f"{reason}. It is not to be reported until more measurements make it a fix."
        )
        return _Position(sentence, None, not_fix=reason)
    candidates = [solution, *solution.ambiguities]
    if solution.ambiguities:
        listed = "; ".join(
            f"{format_position(c.lat_deg, c.lon_deg)}, {_ellipse_text(c.ellipse)}" for c in candidates
        )
        sentence = (
            f"{len(candidates)} positions fit the measurements equally well, and the system cannot yet choose "
            f"between them: {listed}."
        )
        report_list = "; ".join(
            f"{format_position(c.lat_deg, c.lon_deg)} ±{format_distance(c.ellipse.semi_major_m)}" for c in candidates
        )
        return _Position(sentence, None, ambiguous=True, candidates=f"{report_list} (each at {confidence} % confidence)")
    sentence = (
        f"The transmitter is estimated to be at {position}, within about "
        f"{format_distance(ellipse.semi_major_m)} ({_ellipse_text(ellipse)}, "
        f"major axis {ellipse.azimuth_deg:.0f}° from north)."
    )
    report = f"{position}, ±{format_distance(ellipse.semi_major_m)} at {confidence} % confidence"
    doubted = _doubted(record, names)
    if doubted:
        sentence += (
            f" The reference calibrations put the position of {doubted} wrong beyond its configured uncertainty, "
            "and the ellipse does not include that error: the transmitter may lie outside it."
        )
        report += f"; the ephemeris of {doubted} is in doubt, so the transmitter may lie outside this"
    if record.recheck:
        sentence += f" {record.recheck}"
    return _Position(sentence, report)


def _doubted(record: EventRecord, names: _Names) -> str:
    """The satellites in doubt (``EventRecord.doubted_satellite_ids``) that the published
    position's rows go through, joined for a sentence ("" when none)."""
    if not record.doubted_satellite_ids:
        return ""
    used = set(record.solution_used)
    through: set[str] = set()
    for entry in record.measurements:
        if entry.seq in used:
            through.update((entry.row.pair.a.sat_id, entry.row.pair.b.sat_id))
    return _join([names.satellite(s) for s in record.doubted_satellite_ids if s in through])


def _chi2_text(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "unknown"
    return f"{value:.1e}" if value >= 1e4 else f"{value:.3g}"


def _uplink(record: EventRecord, names: _Names) -> tuple[str, str, list[str]]:
    known, maybe = _relaying(record)
    sats = [names.satellite(s) for s in known] or _seen_satellites(record, names)
    sat_text = _join(sats) or "the satellite"
    frequency = _frequency(record)
    up = frequency.split(" (")[0]
    headline = f"Uplink interference on {sat_text} at {up}"
    position = _position(record, names)
    extra = ""
    if maybe:
        extra += (
            f" It may also reach {_join([names.satellite(s) for s in maybe])}: their transponders relay this "
            "frequency, but no site has seen it through them yet and there is no position yet to tell whether "
            "they can see the transmitter."
        )
    if record.not_visible_satellite_ids:
        hidden = _join([names.satellite(s) for s in record.not_visible_satellite_ids])
        extra += (
            f" {hidden} also relays this frequency but cannot see the transmitter's estimated position, so its "
            "users are not affected."
        )
    detail = (
        f"The interfering signal, near {frequency}, is reaching the satellite's receiver, so the "
        f"satellite relays it to every user of {_transponder_phrase(record, names, known)}, wherever they "
        f"are.{extra} {record.verdict_reason} Moving, re-pointing or shielding a ground terminal will not "
        f"help, because the interference arrives from the satellite itself. {position.sentence}"
    )
    move = f"Move the affected traffic to a channel, transponder or satellite that does not carry {up}."
    if maybe:
        move = move[:-1] + f" (on {_join([names.satellite(s) for s in maybe])} too, until it is known whether they can see the transmitter)."
    actions = [
        move,
        "Do not relocate, re-point or shield terminals to fix this: it will not remove "
        "interference that arrives from the satellite.",
    ]
    authority = "to the satellite operator or the spectrum authority"
    start = _start_time(record)
    if position.report is not None and record.recheck:
        actions.append(
            f"Report the estimated transmitter position ({position.report}), the frequency and {start} "
            f"{authority} once new measurements confirm it: it was measured before the signal changed or broke off."
        )
    elif position.report is not None:
        actions.append(
            f"Report the estimated transmitter position ({position.report}), the frequency and {start} "
            f"{authority} so the transmitter can be found and stopped."
        )
    elif position.ambiguous:
        actions.append(
            f"Report that the transmitter is at one of {1 + len(record.solution_out.ambiguities)} positions that "  # type: ignore[union-attr]
            f"fit equally well ({position.candidates}) and that the system cannot yet choose between them, with "
            f"the frequency and {start}, {authority}."
        )
        actions.append(
            "Do not act on any one of these positions alone: more measurements (another satellite or site, a "
            "reference calibration, or time for the satellites to move) are needed to choose."
        )
    elif position.inconsistent:
        actions.append(
            f"Report the frequency, {start} and the affected satellite {authority}; do not report the computed "
            "position: the measurements are inconsistent with one stationary transmitter."
        )
        actions.append(
            "Check whether more than one transmitter is on this frequency, whether a site's clock or timing "
            "receiver is faulty, or whether a satellite's ephemeris is out of date."
        )
    elif position.not_fix is not None:
        actions.append(
            f"Report the frequency, {start} and the affected satellite {authority}; do not report the computed "
            "position yet: it is not a fix."
        )
    elif position.unlocatable is not None:
        actions.append(
            f"Report the frequency, {start} and the affected satellite {authority}, and say that the transmitter "
            "cannot be located by satellite correlation."
        )
    else:
        actions.append(f"Report the frequency, {start} and the affected satellite {authority}.")
    label = _label_sentence(record)
    if label is not None:
        actions.append(f"Include the signal type in the report: it looks like {label}.")
    return headline, detail, actions


def _local(record: EventRecord, names: _Names) -> tuple[str, str, list[str]]:
    sites = _seen_sites(record, names)
    site_text = _join(sites) or "one site"
    verb = "sees" if len(sites) <= 1 else "see"
    headline = f"Local interference at {site_text}"
    received = _received_groups(record, names, via=False)
    on_air = "; ".join(received) if received else f"near {_frequency(record)}"
    detail = (
        f"Only {site_text} {verb} this signal, received {on_air}. {record.verdict_reason} "
        f"This points to a source on the ground near {site_text}: nearby equipment, another "
        "transmitter, or a fault in the site's own receive chain. Other sites and other users of "
        "the satellite are not affected."
    )
    tune = f" (tune to {received[0].split(' at ')[0].removeprefix('near ')})" if len(received) == 1 else ""
    actions = [
        f"Look for a nearby source around {site_text}{tune}: recently installed equipment, transmitters, "
        "or electrical noise close to the antenna.",
        "Check the site's own receive chain (cables, connectors, amplifiers) for faults.",
        "If the source cannot be switched off: add a filter, shield the antenna, or move the "
        "antenna away from the source.",
        "No change of channel, transponder or satellite is needed for other users.",
    ]
    return headline, detail, actions


def _gap_action(gap: dict[str, Any], names: _Names) -> str | None:
    kind = gap.get("kind")
    site = names.site(gap.get("site_id"))
    sat = names.satellite(gap.get("sat_id"))
    near = names.site(gap.get("near_site_id")) if gap.get("near_site_id") else None
    min_sep = names.min_separation_km()
    if kind == "settling":
        return "Wait a few seconds: the verdict is formed once every site has had time to report."
    if kind == "no_far_site":
        where = f" more than {min_sep:.0f} km from {near}" if near else f" more than {min_sep:.0f} km away"
        through = f" that can see {sat}" if gap.get("sat_id") else " that monitors this frequency"
        return (
            f"Bring a second site online{through}{where}: if it sees the signal too, the "
            "interference is on the uplink; if it clearly does not, it is local."
        )
    if kind == "far_not_sensitive":
        margin = gap.get("margin_db")
        how = (
            f" (it would be only {margin:.0f} dB above its detection threshold; dB, decibels, "
            "compare signal strengths)"
            if isinstance(margin, (int, float)) and math.isfinite(margin)
            else ""
        )
        return (
            f"{site} watches {sat} but is not sensitive enough for its silence to count{how}; a "
            "larger antenna there, or another site, would settle it."
        )
    if kind == "far_stale":
        return f"Restore the spectrum feed from {site}: its node is offline or its data is out of date."
    if kind == "far_unpredictable":
        return (
            f"{site} watches {sat}, but no site sees the signal through {sat} yet, so its "
            "silence cannot be judged."
        )
    if kind == "no_sighting":
        return "No site sees the signal right now; it may have stopped."
    return None


def _undetermined(record: EventRecord, names: _Names) -> tuple[str, str, list[str]]:
    frequency = _frequency(record)
    received = _received_groups(record, names, via=True)
    if len(received) == 1:
        verb, named = "received", received[0].split(" (")[0].removeprefix("near ")  # the received centre
    else:
        verb, named = "detected", frequency.split(" (")[0] + (" uplink" if record.mapped and received else "")
    settling = record.verdict_basis == "settling"
    if settling:
        headline = f"Interference {verb} at {named}: checking where it enters"
    else:
        headline = f"Interference {verb} at {named}: source not yet determined"
    sites, sats = _seen_sites(record, names), _seen_satellites(record, names)
    if received:
        seen = "Received " + "; ".join(received)
    elif sites:  # rows without their received range (older documents)
        seen = f"Seen at {_join(sites)}" + (f" through {_join(sats)}" if sats else "") + f", near {frequency}"
    else:
        seen = f"Detected near {frequency}"
    uplink = (
        f" If it reaches the satellites, it is transmitted near {frequency} on the uplink."
        if record.mapped and received
        else ""
    )
    outside = (
        " It lies outside every transponder passband, so no satellite relays it."
        if not record.mapped
        else ""
    )
    detail = (
        f"{seen}.{uplink}{outside} It is not yet possible to tell whether the "
        "signal reaches the satellite (affecting every user of the transponder, wherever they "
        "are) or comes from near one site (affecting only that site). "
        f"{record.verdict_reason}"
    )
    actions: list[str] = []
    gaps = record.verdict_gaps or ([{"kind": "settling"}] if settling else [])
    for gap in gaps:
        action = _gap_action(gap, names)
        if action is not None and action not in actions:
            actions.append(action)
    if record.mapped:
        actions.append(
            "Ask users of the transponder at other locations whether they also experience the "
            "interference: if they do, it is on the uplink."
        )
    else:
        actions.append(
            "Check for transmitters or equipment near the receiving site: a signal outside the "
            "satellite's channels usually comes from nearby."
        )
    return headline, detail, actions
