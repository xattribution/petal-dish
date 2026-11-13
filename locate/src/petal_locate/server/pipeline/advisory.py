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

Terms are explained the first time they appear; numbers are rounded to what matters.
Simulated events say so in the headline. Pure functions, no I/O.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Final

from petal_locate.server.api import schemas
from petal_locate.server.pipeline.events import EventRecord

__all__ = ["LABEL_TEXT", "advise", "format_distance", "format_frequency", "format_position"]

LABEL_TEXT: Final[dict[str, str]] = {
    "cw": "a single unmodulated tone (CW)",
    "swept": "a tone sweeping across the band (swept)",
    "noise": "noise-like energy spread across the band",
    "modulated": "a modulated carrier, like a data or voice signal",
    "pulsed": "a signal that switches on and off (pulsed)",
}
"""How each waveform class is described to an operator (``unknown`` is not described)."""


# ------------------------------------------------------------------- formatting


def format_frequency(lo_hz: float, hi_hz: float) -> str:
    """``"301.0125 MHz (about 25 kHz wide)"``: centre in MHz to 100 Hz, width rounded."""
    centre = 0.5 * (lo_hz + hi_hz)
    width = max(hi_hz - lo_hz, 0.0)
    if width >= 1e6:
        span = f"about {width / 1e6:.1f} MHz wide"
    elif width >= 1e3:
        span = f"about {width / 1e3:.0f} kHz wide" if width >= 10e3 else f"about {width / 1e3:.1f} kHz wide"
    else:
        span = f"about {width:.0f} Hz wide"
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
        detail = (
            "The signal is no longer detected at any site. If the same frequency is detected "
            "again soon, this event reopens and keeps its history. " + detail
        )
        keep = [a for a in actions if a.startswith("Report")]
        actions = ["No action is needed unless the signal returns."] + keep
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
    return format_frequency(record.lo_hz, record.hi_hz)


def _transponder_phrase(record: EventRecord, names: _Names) -> str:
    """``transponder shared-1 of MUOS Pacific`` etc., explained on first use."""
    if not record.transponders:
        return "the affected satellite channel"
    parts = [f"transponder {t.transponder_id} of {names.satellite(t.sat_id)}" for t in record.transponders]
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


def _label_sentence(record: EventRecord) -> str | None:
    if record.classification is None:
        return None
    return LABEL_TEXT.get(record.classification.label)


def _solution_text(record: EventRecord) -> tuple[str, str | None, bool]:
    """(sentence for the detail, position text for the report or None, ambiguous)."""
    solution = record.solution_out
    if solution is None:
        relaying = len(record.satellite_ids)
        if relaying < 2:
            sentence = (
                "Only one satellite relays this frequency, so the transmitter cannot be located "
                "from the differences between satellite copies."
            )
        else:
            sentence = (
                f"No position estimate yet: the system is comparing the copies of the signal "
                f"relayed by {relaying} satellites."
            )
            if record.last_solve_reason:
                sentence += f" The last attempt gave no position ({record.last_solve_reason})."
        return sentence, None, False
    position = format_position(solution.lat_deg, solution.lon_deg)
    ellipse = solution.ellipse
    confidence = round(100.0 * ellipse.confidence)
    sentence = (
        f"The transmitter is estimated to be at {position}, within about "
        f"{format_distance(ellipse.semi_major_m)} ({confidence} % confidence; the error ellipse is "
        f"{format_distance(2 * ellipse.semi_major_m)} by {format_distance(2 * ellipse.semi_minor_m)})."
    )
    ambiguous = bool(solution.ambiguities)
    if ambiguous:
        other = solution.ambiguities[0]
        sentence += (
            f" A mirror position at {format_position(other.lat_deg, other.lon_deg)} fits the "
            "measurements equally well and cannot be ruled out yet."
        )
    report = f"{position}, ±{format_distance(ellipse.semi_major_m)} at {confidence} % confidence"
    return sentence, report, ambiguous


def _uplink(record: EventRecord, names: _Names) -> tuple[str, str, list[str]]:
    sats = [names.satellite(s) for s in record.satellite_ids] or _seen_satellites(record, names)
    sat_text = _join(sats) or "the satellite"
    frequency = _frequency(record)
    headline = f"Uplink interference on {sat_text} at {frequency.split(' (')[0]}"
    location, report, ambiguous = _solution_text(record)
    detail = (
        f"The interfering signal, near {frequency}, is reaching the satellite's receiver, so the "
        f"satellite relays it to every user of {_transponder_phrase(record, names)}, wherever they "
        f"are. {record.verdict_reason} Moving, re-pointing or shielding a ground terminal will not "
        f"help, because the interference arrives from the satellite itself. {location}"
    )
    actions = [
        f"Move the affected traffic to a channel, transponder or satellite that does not carry "
        f"{frequency.split(' (')[0]}.",
        "Do not relocate, re-point or shield terminals to fix this: it will not remove "
        "interference that arrives from the satellite.",
    ]
    if report is not None:
        actions.append(
            f"Report the estimated transmitter position ({report}), the frequency and the start "
            "time to the satellite operator or the spectrum authority so the transmitter can be "
            "found and stopped."
        )
    else:
        actions.append(
            "Report the frequency, the start time and the affected satellite to the satellite "
            "operator or the spectrum authority."
        )
    label = _label_sentence(record)
    if label is not None:
        actions.append(f"Include the signal type in the report: it looks like {label}.")
    if ambiguous:
        actions.append("Treat the mirror position as possible until more measurements resolve it.")
    return headline, detail, actions


def _local(record: EventRecord, names: _Names) -> tuple[str, str, list[str]]:
    sites = _seen_sites(record, names)
    site_text = _join(sites) or "one site"
    verb = "sees" if len(sites) <= 1 else "see"
    headline = f"Local interference at {site_text}"
    detail = (
        f"Only {site_text} {verb} this signal, near {_frequency(record)}. {record.verdict_reason} "
        f"This points to a source on the ground near {site_text}: nearby equipment, another "
        "transmitter, or a fault in the site's own receive chain. Other sites and other users of "
        "the satellite are not affected."
    )
    actions = [
        f"Look for a nearby source around {site_text}: recently installed equipment, transmitters, "
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
    settling = record.verdict_basis == "settling"
    if settling:
        headline = f"Interference detected at {frequency.split(' (')[0]}: checking where it enters"
    else:
        headline = f"Interference detected at {frequency.split(' (')[0]}: source not yet determined"
    sites = _seen_sites(record, names)
    sats = _seen_satellites(record, names)
    seen = f"Seen at {_join(sites)}" if sites else "Detected"
    via = f" through {_join(sats)}" if sats else ""
    outside = (
        " It lies outside every transponder passband, so no satellite relays it."
        if not record.mapped
        else ""
    )
    detail = (
        f"{seen}{via}, near {frequency}.{outside} It is not yet possible to tell whether the "
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
