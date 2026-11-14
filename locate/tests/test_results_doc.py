"""docs/RESULTS.md against the study's raw numbers in docs/results/*.json.

Every table of RESULTS.md is rebuilt here from the JSON files and compared row by row,
and the numbers its prose quotes (the short answer first, which README.md repeats) are
recomputed from the same files. A rerun of ``scripts/capability_study.py`` that changes
a number, or an edit of the document that no longer matches the files, fails here and
names the row or the sentence. The configuration the study ran on is pinned too:
editing a value of ``config/pacific-uhf.yaml`` (not just a comment) makes the results
stale, and this test says to rerun the study.

Only the JSON files are read (about 1.7 MB); nothing is simulated, so this runs in the
default suite.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import statistics
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RESULTS_MD = ROOT / "docs" / "RESULTS.md"
DATA = ROOT / "docs" / "results"
FILES = ("accuracy", "detection", "limits", "live", "montecarlo", "references", "scenarios", "sites")

POINT = {"philippine-sea": "Philippine Sea", "south-china-sea": "South China Sea", "sea-of-japan": "Sea of Japan",
         "near-guam": "near Guam", "near-hawaii": "near Hawaii", "korean-peninsula": "Korean peninsula",
         "east-of-japan": "Pacific east of Japan"}
MC_POINT = {**POINT, "east-of-japan": "east of Japan"}
SATELLITE = {"muos-pac": "MUOS Pacific", "uhf-172e": "172 E", "muos-io": "MUOS Indo-Asia"}
DOMINANT = {"ephemeris_position": "ephemeris position", "ephemeris_velocity": "ephemeris velocity",
            "ionosphere": "ionosphere", "troposphere": "troposphere", "reference": "reference", "noise": "noise",
            "clock": "clock", "oscillator": "oscillator", "tdoa_bias": "TDOA bias", None: "-"}
AWAY_FROM_HAWAII = [k for k in POINT if k != "near-hawaii"]
GRADE = {"operator": "operator", "tle": "TLE"}
NUMBER = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]

#: sha256 (first 16 hex digits) of config/pacific-uhf.yaml as parsed, comments and layout
#: ignored, for the file the study ran on (its byte hash, c1dffdc7253968bf, is the
#: ``config_sha256_16`` each results file records).
STUDY_CONFIG_CONTENT_SHA256_16 = "c0cc6fe1f25df5af"


# --------------------------------------------------------------------------- reading


@functools.cache
def load(name: str) -> Any:
    return json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))


@functools.cache
def document() -> str:
    return RESULTS_MD.read_text(encoding="utf-8")


@functools.cache
def prose() -> str:
    """The document with every run of white space made one space (sentences wrap)."""
    return re.sub(r"\s+", " ", document())


def is_separator(line: str) -> bool:
    return line.startswith("|---") and set(line.replace("|", "")) <= {"-", " "}


def tables() -> list[tuple[str, list[str]]]:
    """Every Markdown table (a run of lines starting with '|' whose second line is the
    separator): (the last line before it, its lines from the header on), in order."""
    found: list[tuple[str, list[str]]] = []
    before = ""
    block: list[str] = []
    for line in [*document().splitlines(), ""]:
        if line.startswith("|"):
            block.append(line)
            continue
        if len(block) >= 2 and is_separator(block[1]):
            found.append((before, block))
        elif block:
            before = block[-1].strip()
        block = []
        if line.strip():
            before = line.strip()
    return found


def table(*, header: str, before: str | None = None) -> list[str]:
    """The body rows of the one table whose header row starts with ``header`` (and whose
    preceding line starts with ``before``, when given)."""
    hits = [rows for lead, rows in tables()
            if rows[0].startswith(header) and (before is None or lead.startswith(before))]
    assert len(hits) == 1, f"{len(hits)} tables with header {header!r} after {before!r}"
    return hits[0][2:]


def cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def assert_rows(got: Sequence[str], want: Sequence[str], what: str, *, ordered: bool = True) -> None:
    """The document's rows are the rebuilt ones (in order, unless ``ordered`` is False);
    the message lists the rows that differ."""
    missing = [r for r in want if r not in got]
    extra = [r for r in got if r not in want]
    assert not missing and not extra, (
        f"{what}: RESULTS.md does not match docs/results\n  rebuilt, not in RESULTS.md:\n    "
        + "\n    ".join(missing) + "\n  in RESULTS.md, not rebuilt:\n    " + "\n    ".join(extra))
    assert not ordered or list(got) == list(want), f"{what}: rows in another order"
    assert len(got) == len(want), f"{what}: a row appears twice"


def assert_quoted(claims: Iterable[str]) -> None:
    """Each text (built from the JSON files) appears in the document's prose."""
    absent = [c for c in claims if c not in prose()]
    assert not absent, "RESULTS.md no longer says (from docs/results):\n  " + "\n  ".join(absent)


# --------------------------------------------------------------------------- number formats


def km(m: float | None, digits: int = 2) -> str:
    return "-" if m is None else f"{m / 1e3:.{digits}f}"


def km_scaled(m: float) -> str:
    """Kilometers with two decimals below 10, one below 100, whole with commas above."""
    k = m / 1e3
    return f"{k:.2f}" if k < 10 else f"{k:.1f}" if k < 100 else f"{k:,.0f}"


def km_short(m: float) -> str:
    """Kilometers with two decimals below 1, one below 10, whole (with commas) above."""
    k = m / 1e3
    return f"{k:.2f}" if k < 1 else f"{k:.1f}" if k < 10 else f"{k:,.0f}"


def day_range(values: Sequence[float | None]) -> str:
    """'lo-hi' of a quantity over the epochs, km: two decimals below 10, whole above
    (commas from 1,000); 'unobservable or ...' when some epochs had no estimate."""
    seen = [v for v in values if v is not None]
    if not seen:
        return "unobservable"

    def fmt(k: float) -> str:
        return f"{k:,.0f}" if k >= 100 else f"{k:.0f}" if k >= 10 else f"{k:.2f}"

    text = f"{fmt(min(seen) / 1e3)}-{fmt(max(seen) / 1e3)}"
    return text if len(seen) == len(values) else f"unobservable or {text}"


def span(values: Iterable[float], fmt: str = ".1f", unit: str = "") -> str:
    """'lo to hi' of the values in the given format."""
    vals = list(values)
    return f"{min(vals):{fmt}} to {max(vals):{fmt}}{unit}"


def ns(v: float) -> str:
    return "0" if abs(v) < 0.5 else f"{v:,.0f}"


# --------------------------------------------------------------------------- the files themselves


def test_every_results_file_is_simulated_and_from_the_study_configuration() -> None:
    """All eight files carry the same configuration hash and say they are simulated;
    the configuration as parsed is still the one they were computed on."""
    hashes = {name: load(name)["config_sha256_16"] for name in FILES}
    assert len(set(hashes.values())) == 1, hashes
    assert all(load(name)["simulated"] is True for name in FILES)
    assert all(load(name)["config"].endswith("config/pacific-uhf.yaml") for name in FILES)
    parsed = yaml.safe_load((ROOT / "config" / "pacific-uhf.yaml").read_text(encoding="utf-8"))
    content = hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()[:16]
    assert content == STUDY_CONFIG_CONTENT_SHA256_16, (
        "config/pacific-uhf.yaml no longer holds the values the capability study ran on: rerun "
        "scripts/capability_study.py (RESULTS.md section 8), update RESULTS.md, then this pin")
    media = {json.dumps(load(name)["media_assumed_by_solver"], sort_keys=True) for name in FILES}
    assert media == {json.dumps({"iono_correlation": 0.5, "iono_vtec_tecu": 30.0, "tropo_correlation": 0.9,
                                 "tropo_zenith_m": 2.4}, sort_keys=True)}
    assert_quoted(["30 TECU of vertical electron content (correlation 0.5 between ground ends) and 2.4 m of "
                   "zenith tropospheric delay (correlation 0.9)"])


def test_section_8_timings() -> None:
    def minutes(name: str, digits: int = 1) -> str:
        return f"{load(name)['elapsed_s'] / 60:.{digits}f} min"

    rows = {cells(r)[0]: cells(r)[3] for r in table(header="| file | what | how |")}
    want = {"`detection.json`": minutes("detection"), "`accuracy.json`": minutes("accuracy"),
            "`references.json`": f"{load('references')['elapsed_s']:.0f} s",
            "`montecarlo.json`": minutes("montecarlo") + " (3 workers)", "`limits.json`": minutes("limits"),
            "`sites.json`": minutes("sites"), "`scenarios.json`": minutes("scenarios") + " (3 workers)",
            "`live.json`": minutes("live", 0)}
    assert rows == want


#: The header of every table in RESULTS.md, each rebuilt by a test below: a new table
#: needs its own check here.
CHECKED_TABLES = [
    "| site | satellite | 0.72 m dish 10 kHz |",
    "| point | satellites seeing it | pairs at 03 UTC |",
    "| point | uncalibrated | calibrated, Guam ref |",
    "| lat \\ lon |", "| lat \\ lon |", "| lat \\ lon |",
    "| elevation |",
    "| point | pair | ionosphere |",
    "| map | point | none (0 / 0) |",
    "| point | distance km |",
    "| point | ephemeris | calibration | median miss |",
    "| point | off | drawn |",
    "| epoch | shipped orbits",
    "| site | MUOS Pacific | 172 E | MUOS Indo-Asia |",
    "| site set |",
    "| scenario | emitter | calibration |",
    "| file | what | how |",
]


def test_every_table_of_the_document_is_checked() -> None:
    headers = [rows[0] for _lead, rows in tables()]
    assert len(headers) == len(CHECKED_TABLES), headers
    assert all(h.startswith(c) for h, c in zip(headers, CHECKED_TABLES, strict=True)), headers


# --------------------------------------------------------------------------- 1. detection


def detection_cells() -> dict[tuple[str, str], dict[tuple[str, float], dict[str, Any]]]:
    out: dict[tuple[str, str], dict[tuple[str, float], dict[str, Any]]] = {}
    for row in load("detection")["rows"]:
        out.setdefault((row["site"], row["satellite"]), {})[(row["antenna"], row["bandwidth_hz"])] = row
    return out


ANTENNAS = ("dish-0.72m", "dish-1.2m", "beam-10dBi")


def thresholds(antenna: str, bandwidth_hz: float) -> list[float]:
    return [r["eirp_tracker_full_width_dbw"] for r in load("detection")["rows"]
            if r["antenna"] == antenna and r["bandwidth_hz"] == bandwidth_hz]


def gains_over_small_dish(antenna: str, bandwidth_hz: float) -> list[float]:
    return [v[("dish-0.72m", bandwidth_hz)]["eirp_tracker_full_width_dbw"]
            - v[(antenna, bandwidth_hz)]["eirp_tracker_full_width_dbw"] for v in detection_cells().values()]


def test_section_1_detection_table() -> None:
    want = []
    for (site, sat), by in detection_cells().items():
        values = [f"{by[(a, bw)]['eirp_tracker_full_width_dbw']:.1f}" for a in ANTENNAS for bw in (10e3, 25e3)]
        want.append(f"| {site.capitalize()} | {SATELLITE[sat]} | " + " | ".join(values) + " |")
    assert_rows(table(header="| site | satellite | 0.72 m dish"), want, "section 1")


def test_section_1_and_short_answer_detection_numbers() -> None:
    det = load("detection")
    rows = det["rows"]
    big_10, big_25 = gains_over_small_dish("dish-1.2m", 10e3), gains_over_small_dish("dish-1.2m", 25e3)
    beam_10, beam_25 = gains_over_small_dish("beam-10dBi", 10e3), gains_over_small_dish("beam-10dBi", 25e3)
    small_10, small_25 = thresholds("dish-0.72m", 10e3), thresholds("dish-0.72m", 25e3)
    assert round(statistics.median(small_10)) == 11 and round(statistics.median(small_25)) == 15
    assert all(max(v) - min(v) < 1.0 for a in ANTENNAS for v in (thresholds(a, 10e3), thresholds(a, 25e3)))
    gains = det["antenna_peak_gain_dbi_at_downlink"]

    def gain(prefix: str, key: str) -> list[float]:
        return [v[key] for k, v in gains.items() if k.startswith(prefix)]

    nominal = [r["eirp_nominal_dbw"] - r["eirp_tracker_full_width_dbw"] for r in rows]
    fragments = [r["eirp_nominal_dbw"] - r["eirp_tracker_fragments_dbw"] for r in rows]
    assert_quoted([
        f"about {statistics.median(small_10):.0f} dBW EIRP toward the satellite if it is 10 kHz wide, or about "
        f"{statistics.median(small_25):.0f} dBW if it fills a 25 kHz channel",
        f"A 1.2 m dish detects jammers {span(big_10 + big_25)} dB weaker "
        f"({span(thresholds('dish-1.2m', 10e3))} dBW at 10 kHz), a 10 dBi beam antenna {span(beam_10 + beam_25)} dB "
        f"weaker ({span(thresholds('beam-10dBi', 10e3))} dBW)",
        f"the 1.2 m dish gains {span(big_10)} dB at 10 kHz and {span(big_25)} dB at 25 kHz, the beam "
        f"{span(beam_10)} dB and {span(beam_25)} dB",
        f"reaches {det['per_bin_threshold_db']:.2f} dB",
        f"lies {span(nominal)} dB above the table",
        f"{span(fragments)} dB below the nominal level",
        f"0.72 m dish {span(gain('dish-0.72m', 'gain_dbi'))} dBi with a half-power beam of "
        f"{span(gain('dish-0.72m', 'hpbw_deg'), '.0f')} degrees, 1.2 m dish {span(gain('dish-1.2m', 'gain_dbi'))} "
        f"dBi ({span(gain('dish-1.2m', 'hpbw_deg'), '.0f')} degrees), beam {min(gain('beam', 'gain_dbi')):.0f} dBi "
        f"({min(gain('beam', 'hpbw_deg')):.0f} degrees)",
        f"It behaves as a reflector with {min(gain('dish-0.72m', 'gain_dbi')):.1f} dBi of gain and a beam about "
        f"{round(statistics.mean(gain('dish-0.72m', 'hpbw_deg')), -1):.0f} degrees wide",
        f"adds up to {det['uplink_range_spread_db']:.1f} dB of uplink path loss",
    ])


# --------------------------------------------------------------------------- 2. accuracy maps


def at_03(point: dict[str, Any], key: str) -> dict[str, Any]:
    return point["epochs"][0]["results"][key]


def over_day(point: dict[str, Any], key: str, field: str = "semi_major_95_m") -> list[float | None]:
    return [epoch["results"][key][field] for epoch in point["epochs"]]


def points(keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
    wanted = list(POINT) if keys is None else list(keys)
    by_key = {p["key"]: p for p in load("accuracy")["points"]}
    return [by_key[k] for k in wanted]


def test_section_2_1_operator_grade_table() -> None:
    rows = table(header="| point | satellites seeing it | pairs at 03 UTC |")
    assert len(rows) == len(POINT)

    def cell(r: dict[str, Any]) -> str:
        return "unobservable" if r["semi_major_95_m"] is None else f"{km(r['semi_major_95_m'])} / {km(r['cep50_m'])}"

    for row, p in zip(rows, load("accuracy")["points"], strict=True):
        got = cells(row)
        e0 = p["epochs"][0]
        unc, cal = at_03(p, "operator/uncalibrated"), at_03(p, "operator/calibrated-guam")
        near = p["nearest_whatif_reference"]
        nearest = "same" if near["site"] == "guam" else cell(at_03(p, "operator/calibrated-nearest-whatif"))
        want = [
            str(len(e0["satellites_seeing_emitter"])),
            cell(unc), cell(cal), f"{nearest} ({near['site'].capitalize()}, {near['distance_km']:,.0f} km)",
            f"{DOMINANT[unc['dominant']]} / {DOMINANT[cal['dominant']]}",
            " / ".join(day_range(over_day(p, f"operator/{k}"))
                       for k in ("uncalibrated", "calibrated-guam", "calibrated-nearest-whatif")),
        ]
        lat, lon = p["lat_deg"], p["lon_deg"]
        assert got[0].startswith(f"{POINT[p['key']]} ({abs(lat):g} {'N' if lat >= 0 else 'S'}, "
                                 f"{abs(lon):g} {'E' if lon >= 0 else 'W'}"), got[0]
        # every pair listed is one the tasking rule chose; a pair without a copy is named in words
        listed = re.findall(r"[a-z0-9-]+/[a-z0-9-]+", got[2])
        assert listed and all(tuple(x.split("/")) in {tuple(q) for q in e0["pairs"]} for x in listed), got[2]
        assert [got[1], *got[3:]] == want, f"section 2.1, {POINT[p['key']]}"


def test_section_2_2_tle_grade_table() -> None:
    def cell(r: dict[str, Any]) -> str:
        if r["semi_major_95_m"] is None:
            return "unobservable"
        a, b = r["semi_major_95_m"] / 1e3, r["cep50_m"] / 1e3
        return f"{a:.1f} / {b:.1f}" if a >= 10 else f"{a:.2f} / {b:.2f}"

    want = []
    for p in points():
        near = p["nearest_whatif_reference"]
        nearest = ("same" if near["site"] == "guam" else cell(at_03(p, "tle/calibrated-nearest-whatif")))
        day = " / ".join(day_range(over_day(p, f"tle/{k}"))
                         for k in ("uncalibrated", "calibrated-guam", "calibrated-nearest-whatif"))
        want.append(f"| {POINT[p['key']]} | {cell(at_03(p, 'tle/uncalibrated'))} | "
                    f"{cell(at_03(p, 'tle/calibrated-guam'))} | {nearest} ({near['site'].capitalize()}) | "
                    f"{p['distance_to_guam_reference_km']:,.0f} km | "
                    f"{DOMINANT[at_03(p, 'tle/calibrated-guam')['dominant']]} | {day} |")
    assert_rows(table(header="| point | uncalibrated | calibrated, Guam ref |"), want, "section 2.2")


@pytest.mark.parametrize(("before", "key"), [
    ("Operator-grade, uncalibrated:", "operator/uncalibrated"),
    ("Operator-grade, calibrated against the Guam reference:", "operator/calibrated-guam"),
    ("TLE-grade, calibrated against the Guam reference:", "tle/calibrated-guam"),
])
def test_section_2_3_grids(before: str, key: str) -> None:
    acc = load("accuracy")
    header = "| lat \\ lon | " + " | ".join(f"{lon if lon <= 180 else lon - 360:g}" for lon in acc["grid_lon_deg"]) + " |"
    rows = [lines for lead, lines in tables() if lead == before]
    assert len(rows) == 1 and rows[0][0] == header, before
    want = []
    for grid_row in acc["grid"]:
        values = []
        for c in grid_row:
            r = c.get(key)
            sm = None if r is None else r["semi_major_95_m"]
            values.append("unobs" if sm is None else ">100" if sm > 100e3 else f"{sm / 1e3:.1f}")
        want.append(f"| {grid_row[0]['lat_deg']:.0f} | " + " | ".join(values) + " |")
    assert_rows(rows[0][2:], want, f"section 2.3, {before}")


def test_section_2_4_media_tables() -> None:
    acc = load("accuracy")
    legs = acc["media_leg_delays"]
    labels = {"iono_10tecu_uplink_301.005MHz_ns": "ionosphere, 10 TECU, uplink 301.005 MHz",
              "iono_30tecu_uplink_301.005MHz_ns": "ionosphere, 30 TECU, uplink 301.005 MHz",
              "iono_10tecu_downlink_260.0MHz_ns": "ionosphere, 10 TECU, downlink 260.0 MHz",
              "iono_30tecu_downlink_260.0MHz_ns": "ionosphere, 30 TECU, downlink 260.0 MHz",
              "tropo_2.4m_ns": "troposphere, 2.4 m zenith"}
    header = "| elevation | " + " | ".join(f"{e:g} deg" for e in legs["elevation_deg"]) + " |"
    want = [f"| {label} | " + " | ".join(f"{v:,.0f}" for v in legs[key]) + " |" for key, label in labels.items()]
    assert_rows(table(header=header), want, "section 2.4, delay per leg")

    want = []
    for p in points():
        for m in p["epochs"][0].get("media_tdoa_ns") or []:
            if m.get("emitter_sees_both_satellites", True):
                want.append(f"| {POINT[p['key']]} | {m['pair']} | {ns(m['iono_ns'])} | {ns(m['iono_calibrated_ns'])} | "
                            f"{ns(m['tropo_ns'])} | {ns(m['tropo_calibrated_ns'])} |")
    assert_rows(table(header="| point | pair | ionosphere |"), want, "section 2.4, what the rows carry")

    variants = list(acc["media_variants_tecu_m"])
    assert variants == ["none", "troposphere-only", "iono-10", "configured", "iono-60"]
    kinds = {"operator/uncalibrated": "operator, uncalibrated", "operator/calibrated-guam": "operator, calibrated Guam",
             "tle/calibrated-guam": "TLE, calibrated Guam"}
    want = []
    for kind, label in kinds.items():
        for p in points():
            values = [p["media_variants"][v][kind] for v in variants] if p["media_variants"] else []
            if not values or all(v["semi_major_95_m"] is None for v in values):
                continue
            text = ["unobs" if v["semi_major_95_m"] is None else
                    f"{v['semi_major_95_m'] / 1e3:.1f}" if v["semi_major_95_m"] >= 10e3 else
                    f"{v['semi_major_95_m'] / 1e3:.2f}" for v in values]
            dominant = DOMINANT[values[variants.index("configured")]["dominant"]]
            want.append(f"| {label} | {POINT[p['key']]} | " + " | ".join(text) + f" | {dominant} |")
    assert_rows(table(header="| map | point | none (0 / 0) |"), want, "section 2.4, what it costs")

    def widening(kind: str) -> list[float]:
        out = []
        for p in points(AWAY_FROM_HAWAII):
            a = p["media_variants"]["none"][kind]["semi_major_95_m"]
            b = p["media_variants"]["configured"][kind]["semi_major_95_m"]
            out.append(100 * (b / a - 1))
        return out

    assert max(widening("tle/calibrated-guam")) < 3
    assert_quoted([
        f"widens the operator-grade ellipses by {span(widening('operator/uncalibrated'), '.0f')} % uncalibrated and by "
        f"{span(widening('operator/calibrated-guam'), '.0f')} % calibrated; with TLE-grade ephemeris the satellite "
        "errors dwarf it (under 3 %)",
        "(the ionosphere changes those ellipses by under 3 %, section 2.4)",
    ])


def placement_cells(point: dict[str, Any], grade: str, distance: str) -> dict[str, tuple[float | None, bool]]:
    """Per bearing: the largest semi-major over the epochs (m) and whether every pair was
    calibrated at every epoch."""
    out = {}
    pairs = sum(m.get("emitter_sees_both_satellites", True) for m in point["epochs"][0]["media_tdoa_ns"])
    for bearing, x in point["reference_placement"][grade][distance].items():
        seen = [v for v in x["semi_major_95_m"] if v is not None]
        out[bearing] = (max(seen) if seen else None, all(n == pairs for n in x["tdoa_calibrated_pairs"]))
    return out


BEARINGS = {"0": "N", "45": "NE", "90": "E", "135": "SE", "180": "S", "225": "SW", "270": "W", "315": "NW"}


def test_section_2_5_reference_placement_table() -> None:
    header = "| point | distance km | " + " | ".join(BEARINGS.values()) + " |"
    want = []
    for p in points():
        for distance in p["reference_placement"]["tle"]:
            cell = placement_cells(p, "tle", distance)
            assert list(cell) == list(BEARINGS)
            text = ["-" if v is None else f"{v / 1e3:.1f}" + ("" if full else "*") for v, full in cell.values()]
            want.append(f"| {POINT[p['key']]} | {int(float(distance)):,} | " + " | ".join(text) + " |")
    assert_rows(table(header=header), want, "section 2.5")


def placement_values(grade: str, distances: Sequence[str], bearings: Sequence[str] | None = None, *,
                     full: bool | None = True) -> list[float]:
    """Largest semi-majors (km) over the points away from Hawaii, for the placements
    that calibrated every pair (full=True), those that did not (False) or all (None)."""
    out = []
    for p in points(AWAY_FROM_HAWAII):
        for d in distances:
            for b, (v, ok) in placement_cells(p, grade, d).items():
                if v is not None and (bearings is None or BEARINGS[b] in bearings) and (full is None or ok == full):
                    out.append(v / 1e3)
    return out


def test_section_2_numbers_in_the_prose() -> None:
    three = [p for p in points() if len(p["epochs"][0]["satellites_seeing_emitter"]) == 3]
    unc_minor = [at_03(p, "operator/uncalibrated")["semi_minor_95_m"] for p in three]
    cal_minor = [at_03(p, "operator/calibrated-guam")["semi_minor_95_m"] for p in three]
    upper = [max(v for v in over_day(p, "operator/uncalibrated") if v is not None) for p in three]

    def rounded(values: Iterable[float], fmt: str = ".1f") -> str:
        return span([v / 1e3 for v in values], fmt)

    op650 = placement_values("operator", ["650"])
    op1300 = placement_values("operator", ["1300"])
    starred = placement_values("operator", ["650", "1300"], full=False)
    assert_quoted([
        f"The uncalibrated upper ends ({span([u / 1e3 for u in upper], '.0f')} km)",
        f"At 03 UTC the semi-minor axes are {rounded(unc_minor)} km uncalibrated and {rounded(cal_minor)} km "
        "calibrated against Guam (three-satellite points)",
        f"the same placements give {span(op650)} km at 650 km and {span(op1300)} km at 1,300 km at the six points "
        f"away from Hawaii (up to {max(starred):.1f} km where starred)",
    ])


# --------------------------------------------------------------------------- 3. Monte Carlo


def mc_runs(media: str | None = "drawn") -> list[dict[str, Any]]:
    return [r for r in load("montecarlo")["runs"] if media is None or r["media"] == media]


def mc_run(point: str, ephemeris: str, calibrated: bool, media: str) -> dict[str, Any] | None:
    hits = [r for r in mc_runs(None) if (r["point"], r["ephemeris"], r["calibrated"], r["media"])
            == (point, ephemeris, calibrated, media)]
    assert len(hits) <= 1
    return hits[0]["summary"] if hits else None


def inside(summary: dict[str, Any]) -> int:
    return round(summary["containment_95_total"] * summary["n_solved"])


def test_section_3_monte_carlo_tables() -> None:
    want = []
    for r in mc_runs():
        s = r["summary"]
        assert s["n_solved"] == s["n_trials"] == 40
        want.append(f"| {MC_POINT[r['point']]} | {GRADE[r['ephemeris']]} | {'Guam ref' if r['calibrated'] else 'none'} | "
                    f"{km(s['median_miss_m'])} km | {km(s['p95_miss_m'])} km | "
                    f"{s['containment_95_total']:.3f} ({inside(s)}/{s['n_solved']}) | {s['mean_nees_total']:.2f} | "
                    f"{km(s['cep50_predicted_m'])} km |")
    assert_rows(table(header="| point | ephemeris | calibration | median miss |"), want, "section 3")

    columns = ("off", "drawn", "fixed-10", "fixed-30", "fixed-60", "unmodelled-30")
    want = []
    for point in dict.fromkeys(r["point"] for r in mc_runs() if r["calibrated"]):
        text = []
        for media in columns:
            s = mc_run(point, "operator", True, media)
            text.append("-" if s is None else f"{km(s['median_miss_m'])} / {inside(s)} / {s['mean_nees_total']:.2f}")
        want.append(f"| {MC_POINT[point]} | " + " | ".join(text) + " |")
    assert_rows(table(header="| point | off | drawn | fixed 10 TECU |"), want, "section 3, media")


def test_section_3_numbers_in_the_prose() -> None:
    drawn = mc_runs()
    vtec = [v for r in drawn for v in r["summary"]["truth_vtec_tecu_range"]]
    agree = sum(abs(r["summary"]["cep50_predicted_m"] / r["summary"]["median_miss_m"] - 1) <= 0.25 for r in drawn)
    scs30, ps30 = mc_run("south-china-sea", "operator", True, "fixed-30"), mc_run("philippine-sea", "operator", True,
                                                                                  "fixed-30")
    ps60, scs60 = mc_run("philippine-sea", "operator", True, "fixed-60"), mc_run("south-china-sea", "operator",
                                                                                 True, "fixed-60")
    assert scs30 and ps30 and ps60 and scs60
    uncal = [mc_run(p, "operator", False, m) for p in MC_POINT for m in ("drawn", "fixed-10", "fixed-30")]
    uncal_off = [mc_run(p, "operator", False, "off") for p in MC_POINT]
    uncal_s = [s for s in uncal if s]
    off_s = [s for s in uncal_off if s]
    fixed = [s for p in MC_POINT for m in ("fixed-10", "fixed-30") if (s := mc_run(p, "operator", True, m))]
    exceptions = [r for r in drawn
                  if abs(r["summary"]["cep50_predicted_m"] / r["summary"]["median_miss_m"] - 1) > 0.25]
    assert all(r["summary"]["cep50_predicted_m"] > r["summary"]["median_miss_m"] for r in exceptions)  # cautious
    assert all((t.get("n_ambiguities") or 0) == 0 for r in drawn for t in r["trials"])  # no second candidate
    unmodelled = {p: mc_run(p, "operator", True, "unmodelled-30") for p in ("south-china-sea", "philippine-sea")}
    scs_off = mc_run("south-china-sea", "operator", True, "off")
    assert unmodelled["south-china-sea"] and unmodelled["philippine-sea"] and scs_off
    assert inside(unmodelled["south-china-sea"]) == 0 and inside(unmodelled["philippine-sea"]) < 20
    n = 40
    caf = {p["pair"]: p for p in load("limits")["caf_sigma_check"]["pairs"]}
    co, cross = caf["seoul-pac/seoul-io"], caf["honolulu-pac/naha-172e"]
    assert_quoted([
        f"(drawn VTECs ran from {min(vtec):.1f} to {max(vtec):.0f} TECU)",
        f"within 25 % in {agree} of the {len(drawn)} rows",
        f"the calibrated South China Sea fixes sit {-scs30['mean_error_en_m'][0]:.0f} m west of the truth on "
        f"average (standard error {scs30['stderr_error_en_m'][0]:.0f} m), the Philippine Sea fixes "
        f"{-ps30['mean_error_en_m'][0]:.0f} m west",
        f"{inside(ps60)} and {inside(scs60)} of 40 are inside (mean NEES {ps60['mean_nees_total']:.1f} and "
        f"{scs60['mean_nees_total']:.1f}), and the South China Sea fixes sit {-scs60['mean_error_en_m'][0]:.0f} m west",
        f"keep {min(map(inside, uncal_s))} to {max(map(inside, uncal_s))} of 40 inside and median misses of "
        f"{span([s['median_miss_m'] / 1e3 for s in uncal_s])} km (off: {min(map(inside, off_s))} to "
        f"{max(map(inside, off_s))} of 40, {span([s['median_miss_m'] / 1e3 for s in off_s])} km)",
        f"fixes stay inside their ellipses at 10 and 30 TECU ({min(map(inside, fixed))} to {max(map(inside, fixed))} "
        "of 40)",
        f"it is {co['tdoa_scatter_over_sigma']:.2f} for TDOA and {co['fdoa_scatter_over_sigma']:.2f} for FDOA (mean "
        f"reported TDOA sigma {co['mean_sigma_tdoa_ns']:.0f} ns); on a cross-site pair of different satellites "
        f"(honolulu-pac / naha-172e) {cross['tdoa_scatter_over_sigma']:.2f} and {cross['fdoa_scatter_over_sigma']:.2f} "
        f"({cross['mean_sigma_tdoa_ns']:.0f} ns)",
        f"(1 is ideal; standard error {co['standard_error_of_scatter']:.2f})",
        f"With {n} trials the binomial standard error of a 95 % containment is {(0.95 * 0.05 / n) ** 0.5:.3f}, and the "
        f"standard error of a mean NEES of 2 is {2 / n ** 0.5:.2f}",
        "The two exceptions are cautious: " + " and ".join(
            f"{'TLE-grade ' if r['ephemeris'] == 'tle' else ''}calibrated {MC_POINT[r['point']]}"
            + (f" ({km(r['summary']['median_miss_m'])} km measured, {km(r['summary']['cep50_predicted_m'])} km predicted)"
               if i == 0 else
               f" ({km(r['summary']['median_miss_m'])} km against {km(r['summary']['cep50_predicted_m'])} km)")
            for i, r in enumerate(exceptions)),
        f"its ellipse (mean NEES {unmodelled['south-china-sea']['mean_nees_total']:.0f})",
        f"({inside(scs_off)} of 40, mean NEES {scs_off['mean_nees_total']:.2f}, two standard errors above 2)",
    ])


# --------------------------------------------------------------------------- 4. limits


def test_section_4_1_mirror_table() -> None:
    def place(c: dict[str, Any], digits: int) -> str:
        lat, lon = c["lat_deg"], c["lon_deg"]
        return f"({abs(lat):.{digits}f} {'N' if lat >= 0 else 'S'}, {abs(lon):.{digits}f} {'E' if lon >= 0 else 'W'})"

    rows = table(header="| epoch | shipped orbits")
    epochs = load("limits")["mirror"]["epochs"]
    assert len(rows) == len(epochs)
    for row, e in zip(rows, epochs, strict=True):
        got = cells(row)
        assert got[0] == f"{e['epoch'][11:13]} UTC"
        inclined, flat = e["inclined"], e["zero_inclination"]
        if len(inclined["candidates"]) == 1:
            assert inclined["ambiguity_resolved_by"] == "region"
            assert got[1].startswith("one fix (") and "region" in got[1], got[1]
        else:
            assert inclined["ambiguity_resolved_by"] == "unresolved" and len(inclined["candidates"]) == 2
            other = inclined["candidates"][1]
            chi2 = f"{other['chi2']:.1f}" if other["chi2"] >= 1 else f"{other['chi2']:.2f}"
            assert got[1].startswith(f"unresolved: truth and {place(other, 1)}, chi-square {chi2}"), got[1]
        # with no inclination the second candidate is always the north-south mirror, never resolved
        assert flat["ambiguity_resolved_by"] == "unresolved" and len(flat["candidates"]) == 2
        truth, mirror = flat["candidates"]
        assert abs(mirror["lat_deg"] + truth["lat_deg"]) < 1e-6 and abs(mirror["lon_deg"] - truth["lon_deg"]) < 1e-6
        assert got[2].startswith("unresolved: truth and") and "mirror" in got[2], got[2]
        if "(" in got[2]:
            assert place(mirror, 0) in got[2], got[2]


def test_section_4_tables() -> None:
    vis = load("limits")["visibility_deg"]
    want = [f"| {site.capitalize()} | " + " | ".join(f"{lo:.0f} to {hi:.0f}" for lo, hi in by.values()) + " |"
            for site, by in vis.items()]
    assert all(list(by) == list(SATELLITE) for by in vis.values())
    assert_rows(table(header="| site | MUOS Pacific | 172 E | MUOS Indo-Asia |"), want, "section 4.3")

    sites = load("sites")
    keys = list(sites["all"])
    assert keys == list(POINT)

    def cell(v: dict[str, Any]) -> str:
        c, u = v["calibrated_semi_major_95_m"], v["uncalibrated_semi_major_95_m"]
        cs = "unobs" if c is None else f"{c / 1e3:.2f}" if c < 10e3 else f"{c / 1e3:.1f}"
        return f"{cs} / {'unobs' if u is None else f'{u / 1e3:.1f}'}"

    want = ["| all seven | " + " | ".join(cell(sites["all"][k]) for k in keys) + " |"]
    want += [f"| without {s.capitalize()} | " + " | ".join(cell(v[k]) for k in keys) + " |"
             for s, v in sites["drop_one"].items()]
    want += [f"| only {s.capitalize()} | " + " | ".join(cell(v[k]) for k in keys) + " |"
             for s, v in sites["only_one"].items()]
    assert_rows(table(header="| site set |"), want, "section 4.6")


def relative_changes(sites: dict[str, Any], group: str, which: Sequence[str], field: str) -> list[tuple[float, str, str]]:
    out = []
    for site in which:
        for k in POINT:
            base, other = sites["all"][k][field], sites[group][site][k][field]
            if base is not None and other is not None:
                out.append((abs(other / base - 1), site, k))
    return out


def test_section_4_numbers_in_the_prose() -> None:
    limits = load("limits")
    band = limits["band_overlap"]
    counts = [v["channels_receiving"] for v in band.values()]
    cn0 = [x for v in band.values() for x in v["cn0_dbhz_range"]]
    cw = {p["key"]: p for p in limits["cw_predicted"]["points"]}
    cw_keys = ["philippine-sea", "south-china-sea", "sea-of-japan", "near-guam", "korean-peninsula", "east-of-japan"]
    cw_cal = [cw[k]["calibrated-sapporo"] for k in cw_keys]
    assert all(cw[k]["uncalibrated"]["reason"] == "unobservable" for k in cw)
    assert cw["near-hawaii"]["calibrated-sapporo"]["semi_major_95_m"] is None
    cw_unc, cw_mc = limits["uplink-cw/uncalibrated"], limits["uplink-cw/calibrated"]
    assert cw_unc["failures"] == {"unobservable": cw_unc["n_trials"]} and cw_mc["n_solved"] == cw_mc["n_trials"]
    swept = [limits[f"uplink-swept/{k}"] for k in ("uncalibrated", "calibrated")]
    assert all(s["n_solved"] == 0 for s in swept)
    sites = load("sites")
    asian = ["tokyo", "naha", "seoul", "busan", "manila"]
    alone = relative_changes(sites, "only_one", asian, "calibrated_semi_major_95_m") \
        + relative_changes(sites, "only_one", asian, "uncalibrated_semi_major_95_m")
    dropped = relative_changes(sites, "drop_one", list(sites["drop_one"]), "calibrated_semi_major_95_m") \
        + relative_changes(sites, "drop_one", list(sites["drop_one"]), "uncalibrated_semi_major_95_m")
    worst_alone, worst_drop = max(alone), max(dropped)
    hawaii = [v["calibrated_semi_major_95_m"] / 1e3 for s in ("honolulu", "hilo")
              for v in sites["only_one"][s].values()]
    assert_quoted([
        f"reaches the {counts[0]}, {counts[1]} or {counts[2]} channels watching that satellite at "
        f"{span(cn0)} dB-Hz",
        f"({span(cn0)} dB-Hz for 20 dBW)",
        "the 95 % semi-major is " + " / ".join(f"{c['semi_major_95_m'] / 1e3:.0f}" for c in cw_cal)
        + f" km (CEP50 {min(c['cep50_m'] for c in cw_cal) / 1e3:.1f} to {max(c['cep50_m'] for c in cw_cal) / 1e3:.0f} km)",
        f"all {sum(cw_mc['pair_reasons'].values())} pair correlations report FDOA only",
        f"Uncalibrated, {cw_unc['n_trials']} of {cw_unc['n_trials']} trials are unobservable",
        f"Calibrated, {cw_mc['n_solved']} of {cw_mc['n_trials']} are located: median miss "
        f"{cw_mc['median_miss_m'] / 1e3:.1f} km against a predicted CEP50 of {cw_mc['cep50_predicted_m'] / 1e3:.1f} km, "
        f"95th percentile {cw_mc['p95_miss_m'] / 1e3:.0f} km, {inside(cw_mc)} of {cw_mc['n_trials']} inside their "
        f"ellipses (mean NEES {cw_mc['mean_nees_total']:.1f})",
        f"about {cw_mc['median_miss_m'] / 1e3:.0f} km median miss (95th percentile {cw_mc['p95_miss_m'] / 1e3:.0f} km)",
        f"all {sum(swept[0]['pair_reasons'].values())} pair correlations are \"ambiguous\"",
        f"One Asian site alone predicts at most {worst_alone[0] * 100:.0f} % above the full network "
        f"({worst_alone[1].capitalize()} {POINT[worst_alone[2]]})",
        f"dropping one site changes a prediction by at most {worst_drop[0] * 100:.0f} % "
        f"({worst_drop[1].capitalize()}, uncalibrated)",
        f"predicts within about {5 * round(worst_alone[0] * 20 + 0.49):.0f} % of the full seven-site network, and "
        f"dropping any one site changes the prediction by at most about {5 * round(worst_drop[0] * 20 + 0.49):.0f} %",
        f"The Hawaii sites alone see only two satellites (calibrated {min(hawaii):.1f} to {max(hawaii):.0f} km",
    ])
    assert worst_drop in relative_changes(sites, "drop_one", [worst_drop[1]], "uncalibrated_semi_major_95_m")


# --------------------------------------------------------------------------- 5. scenarios and the live demo


def scenario_groups() -> dict[tuple[str, str, bool], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, bool], list[dict[str, Any]]] = {}
    for r in load("scenarios")["runs"]:
        groups.setdefault((r["scenario"], r["emitter_id"], r["calibrated"]), []).append(r)
    return groups


def test_section_5_scenario_table() -> None:
    media = load("scenarios")["scenario_media_tecu_m"]
    assert media == [30.0, 2.3]
    want = []
    smallest: dict[str, str] = {}
    for (scenario, emitter, calibrated), runs in scenario_groups().items():
        trials = [t for r in runs for t in r["trials"]]
        solved = [t for t in trials if t.get("miss_m") is not None]
        assert len(solved) == len(trials) == 32, (scenario, emitter, calibrated)
        misses = sorted(t["miss_m"] for t in solved)
        median = statistics.median(misses)
        median_text = f"{km_scaled(median)} km"
        smallest[median_text] = f"{median_text} (smallest {km_scaled(min(misses))} km)"
        semi = [t["semi_major_m"] for t in solved]
        name = scenario.replace("+media", " + media")  # pacific-demo with a 30 TECU ionosphere added
        want.append(
            f"| {name} | {emitter} | {'yes' if calibrated else 'no'} | {median_text} | {km_scaled(max(misses))} km | "
            f"{sum(bool(t['inside_95_total']) for t in solved)}/{len(trials)} | "
            f"{sum((t.get('n_ambiguities') or 0) > 0 for t in solved)}/{len(trials)} | "
            f"{km_short(min(semi))}-{km_short(max(semi))} km |")
    got = []
    for row in table(header="| scenario | emitter | calibration |"):
        parts = cells(row)
        if "(smallest" in parts[3]:  # a median far above the closest trial says how close the closest came
            median_text = parts[3].split(" (smallest")[0]
            assert parts[3] == smallest.get(median_text), parts[3]
            row = row.replace(parts[3], median_text)
        got.append(row)
    assert_rows(got, want, "section 5", ordered=False)
    demo = [t for r in scenario_groups()[("pacific-demo", "jammer-philsea", True)] for t in r["trials"]]
    assert_quoted([
        f"against {km_short(min(t['semi_major_m'] for t in demo))} to {km_short(max(t['semi_major_m'] for t in demo))} "
        f"km by {km(min(t['semi_minor_m'] for t in demo))} to {km(max(t['semi_minor_m'] for t in demo))} km here",
    ])


def test_section_5_and_6_numbers_in_the_prose() -> None:
    groups = scenario_groups()
    amb_trials = [r for (s, e, cal), runs in groups.items() if (s, e, cal) == ("pacific-demo", "jammer-philsea", False)
                  for r in runs]
    amb = sum((t.get("n_ambiguities") or 0) > 0 for r in amb_trials for t in r["trials"])
    amb_epochs = sum(any((t.get("n_ambiguities") or 0) > 0 for t in r["trials"]) for r in amb_trials)
    refs: dict[bool, list[float]] = {}  # every calibrated run, by whether the pair goes through seoul-io
    for (_s, _e, cal), runs in groups.items():
        if cal:
            for r in runs:
                for t in r["trials"]:
                    for pair, sigma in zip(r["pairs"], t.get("reference_sigma_tdoa_ns") or [], strict=False):
                        if sigma is not None:
                            refs.setdefault("seoul-io" in json.dumps(pair), []).append(sigma)
    live = load("live")
    demo, eph = live["pacific-demo"], live["ephemeris-error"]
    ref = load("references")
    design = ref["philippine_sea_reference"]["designs"]
    new, five, psd, old = (design[k] for k in ("8kHz_20dBW (configured)", "5kHz_20dBW", "5kHz_equal_psd",
                                                "old_beam_13dBW_5kHz"))
    ratios = [a / b for a, b in zip(five["reference_sigma_tdoa_ns"], new["reference_sigma_tdoa_ns"], strict=True)]
    psd_ratio = statistics.mean(a / b for a, b in zip(psd["reference_sigma_tdoa_ns"], new["reference_sigma_tdoa_ns"],
                                                      strict=True))
    guam = ref["eirp_near_guam"]["by_grade_and_eirp_dbw"]["operator"]
    worst = {eirp: v["ratio_calibrated_over_uncalibrated_max"] for eirp, v in guam.items()}
    designs = ref["configured_design"].values()
    epochs = len(ref["epochs"])
    channels = {d["n_channel_epochs"] // epochs for d in designs}
    assert len(channels) == 1
    (channels,) = channels
    cn0 = [x for d in designs for x in d["cn0_dbhz_range"]]
    alone = [x for d in designs for x in d["backoff_db_alone_range"]]
    beside = [x for d in designs for x in d["backoff_db_with_20dbw_jammer_range"]]
    at30 = [-x for d in designs for x in d["backoff_db_alone_by_eirp_range"]["30"]]
    at40 = [x for d in designs for x in d["backoff_db_alone_by_eirp_range"]["40"]]
    unc = ref["philippine_sea_reference"]["uncalibrated_semi_major_95_m"]
    assert_quoted([
        f"a second, unresolved candidate in {amb} of {sum(len(r['trials']) for r in amb_trials)} uncalibrated "
        f"trials ({NUMBER[amb_epochs]} of {NUMBER[len(amb_trials)]} epochs; `scenarios.json`)",
        f"the reference TDOA noise the correlator measured was {span(refs[False], '.0f')} ns on pairs without a "
        f"co-channel copy and {span(refs[True], '.0f')} ns on pairs through seoul-io",
        f"verdict {demo['verdict']} {demo['seconds_to_verdict']:.1f} s and located {demo['seconds_to_located']:.1f} s "
        "after detection",
        "After 4, 8, 12 and 16 accepted rows the fix was "
        + ", ".join(f"{h['miss_m']:.0f}" for h in demo["history"][:3]) + f" and {demo['history'][3]['miss_m']:.0f} m "
        f"from the truth, always inside a 95 % ellipse shrinking from {demo['history'][0]['semi_major_m']:.0f} by "
        f"{demo['history'][0]['semi_minor_m']:.0f} m to {demo['final']['semi_major_m']:.0f} by "
        f"{demo['final']['semi_minor_m']:.0f} m",
        f"land {demo['uncorrected_rows']['miss_m'] / 1e3:.2f} km off, inside their "
        f"{demo['uncorrected_rows']['semi_major_m'] / 1e3:.1f} km ellipse",
        f"verdict {eph['verdict']} {eph['seconds_to_verdict']:.1f} s and located {eph['seconds_to_located']:.1f} s "
        "after detection. The fix was " + ", ".join(f"{h['miss_m']:.0f}" for h in eph["history"][:3])
        + f" and {eph['history'][3]['miss_m']:.0f} m off, always inside; at 16 rows the ellipse was "
        f"{eph['final']['semi_major_m']:.0f} by {eph['final']['semi_minor_m']:.0f} m",
        f"land {eph['uncorrected_rows']['miss_m'] / 1e3:.1f} km off (NEES {eph['uncorrected_rows']['nees_total']:.0f})",
        f"predicts {old['reference_sigma_tdoa_ns'][0] / 1e3:.2f} and {old['reference_sigma_tdoa_ns'][1] / 1e3:.2f} "
        f"microseconds of reference TDOA noise",
        f"a calibrated 95 % semi-major of {old['calibrated_semi_major_95_m'] / 1e3:.2f} km against {unc / 1e3:.2f} km "
        "uncalibrated",
        f"C/N0 is {span(cn0)} dB-Hz at all {channels} channels",
        f"transponder back-off is {span(alone)} dB with the reference alone, {span(beside)} dB beside a 20 dBW jammer",
        f"Predicted reference TDOA noise is {new['reference_sigma_tdoa_ns'][0]:.0f} and "
        f"{new['reference_sigma_tdoa_ns'][1]:.0f} ns on the two pairs",
        f"the predicted ellipse shrinks about tenfold ({unc / 1e3:.2f} to {new['calibrated_semi_major_95_m'] / 1e3:.2f} "
        "km at 20 N 132 E)",
        f"cuts the reference TDOA noise by {span(ratios)} ({five['reference_sigma_tdoa_ns'][0]:.0f} and "
        f"{five['reference_sigma_tdoa_ns'][1]:.0f} ns at 5 kHz against {new['reference_sigma_tdoa_ns'][0]:.0f} and "
        f"{new['reference_sigma_tdoa_ns'][1]:.0f} ns)",
        f"(5 kHz at {ref['philippine_sea_reference']['equal_psd_eirp_dbw']:.1f} dBW: "
        f"{psd['reference_sigma_tdoa_ns'][0]:.0f} and {psd['reference_sigma_tdoa_ns'][1]:.0f} ns, about "
        f"{psd_ratio:.1f} times)",
        f"the calibrated ellipse is at worst {max(worst['20'], worst['30']):.2f} "
        f"of the uncalibrated one at 20 or 30 dBW, but {worst['40']:.2f} at "
        f"40 dBW (median calibrated semi-major {guam['20']['calibrated_semi_major_95_m_median'] / 1e3:.2f} km at 20 "
        f"dBW, {guam['40']['calibrated_semi_major_95_m_median'] / 1e3:.2f} km at 40 dBW)",
        f"(back-off {max(at40):.1f} to {min(at40):.1f} dB)",
        f"at 30 dBW it is already {span(at30)} dB into saturation",
    ])


# --------------------------------------------------------------------------- the short answer


def test_the_short_answer_quotes_the_tables() -> None:
    three = [p for p in points() if len(p["epochs"][0]["satellites_seeing_emitter"]) == 3]
    unc = [at_03(p, "operator/uncalibrated") for p in three]
    cal = [at_03(p, "operator/calibrated-guam") for p in three]
    cal_day = [v for p in three for v in over_day(p, "operator/calibrated-guam") if v is not None]
    op_none = [r["summary"] for r in mc_runs() if r["ephemeris"] == "operator" and not r["calibrated"]]
    op_cal = [r["summary"] for r in mc_runs() if r["ephemeris"] == "operator" and r["calibrated"]]
    tle_none = [r["summary"] for r in mc_runs() if r["ephemeris"] == "tle" and not r["calibrated"]]
    tle_guam = mc_run("near-guam", "tle", True, "drawn")
    tle_ps = mc_run("philippine-sea", "tle", True, "drawn")
    assert tle_guam and tle_ps
    far = [p for p in points() if abs(p["distance_to_guam_reference_km"] - 3000) < 300]
    far_values = [v for p in far for v in over_day(p, "tle/calibrated-guam") if v is not None]
    by_key = {p["key"]: p for p in points()}
    guam_km = by_key["near-guam"]["distance_to_guam_reference_km"]
    ps_km = by_key["philippine-sea"]["distance_to_guam_reference_km"]
    tle650 = placement_values("tle", ["650"])
    ew1300 = placement_values("tle", ["1300"], ["E", "W"])
    ns1300 = placement_values("tle", ["1300"], ["N", "S"])
    starred = placement_values("tle", ["650", "1300"], full=False)
    op_place = placement_values("operator", ["650", "1300"])
    hawaii = by_key["near-hawaii"]
    hawaii_guam = [v for v in over_day(hawaii, "operator/calibrated-guam") if v is not None]
    hawaii_near = [v for v in over_day(hawaii, "operator/calibrated-nearest-whatif") if v is not None]
    variants = [(p["media_variants"]["none"]["operator/calibrated-guam"]["semi_major_95_m"],
                 p["media_variants"]["configured"]["operator/calibrated-guam"]["semi_major_95_m"])
                for p in points(AWAY_FROM_HAWAII)]
    widen = [b / a - 1 for a, b in variants]
    off_cal = [s for p in MC_POINT if (s := mc_run(p, "operator", True, "off"))]
    fixed = [s for p in MC_POINT for m in ("fixed-10", "fixed-30") if (s := mc_run(p, "operator", True, m))]
    sixty = [s for p in MC_POINT if (s := mc_run(p, "operator", True, "fixed-60"))]
    legs = load("accuracy")["media_leg_delays"]
    iono30 = [v for key in ("iono_30tecu_uplink_301.005MHz_ns", "iono_30tecu_downlink_260.0MHz_ns")
              for e, v in zip(legs["elevation_deg"], legs[key], strict=True) if e >= 10]
    tropo = [v for e, v in zip(legs["elevation_deg"], legs["tropo_2.4m_ns"], strict=True) if e >= 10]
    scs = by_key["south-china-sea"]["epochs"][0]["media_tdoa_ns"]
    ps = by_key["philippine-sea"]
    assert_quoted([
        f"semi-axes of about {min(u['semi_major_95_m'] for u in unc) / 1e3:.0f} to "
        f"{max(u['semi_major_95_m'] for u in unc) / 1e3:.0f} km by {span([u['semi_minor_95_m'] / 1e3 for u in unc])} km "
        "at 03 UTC",
        f"the median miss is {span([s['median_miss_m'] / 1e3 for s in op_none])} km and the 95th percentile "
        f"{span([s['p95_miss_m'] / 1e3 for s in op_none])} km",
        f"semi-axes of {span([c['semi_major_95_m'] / 1e3 for c in cal])} km by "
        f"{span([c['semi_minor_95_m'] / 1e3 for c in cal])} km at 03 UTC ({span([v / 1e3 for v in cal_day])} km over "
        "the day)",
        f"The median miss is then {round(min(s['median_miss_m'] for s in op_cal), -1):.0f} to "
        f"{round(max(s['median_miss_m'] for s in op_cal), -1):.0f} m and the 95th percentile "
        f"{span([s['p95_miss_m'] / 1e3 for s in op_cal], '.2f')}",
        f"uncalibrated fixes are off by about {statistics.mean(s['median_miss_m'] for s in tle_none) / 1e3:.0f} km "
        f"(median; 95th percentile {span([s['p95_miss_m'] / 1e3 for s in tle_none], '.0f')} km)",
        f"{tle_guam['median_miss_m'] / 1e3:.2f} km median near Guam ({round(guam_km, -2):.0f} km from the reference), "
        f"{tle_ps['median_miss_m'] / 1e3:.2f} km in the Philippine Sea ({round(ps_km, -2):,.0f} km away), and a "
        f"predicted 95 % semi-major of {min(far_values) / 1e3:.1f} to {max(far_values) / 1e3:.0f} km at about 3,000 km",
        f"kept the TLE-grade 95 % semi-major to {span(tle650)} km at the six points away from Hawaii",
        f"{span(ew1300)} km due east or west of the area, {min(ns1300):.1f} to {max(ns1300):.0f} km due north or south",
        f"leaves those pairs uncalibrated (up to {max(starred):.0f} km)",
        f"With operator-grade ephemeris the distance matters little ({span(op_place)} km at 650 or 1,300 km)",
        f"roughly {min(iono30) / 1e3:.2f} to {max(iono30) / 1e3:.1f} microseconds",
        f"another {min(tropo):.0f} to {max(tropo):.0f} ns above 10 degrees",
        f"at 30 TECU about {max(abs(m['iono_calibrated_ns']) for m in scs) / 1e3:.1f} microseconds are left on the "
        "South China Sea measurements",
        f"calibrated ellipses are {min(widen) * 100:.0f} to {max(widen) * 100:.0f} % larger than with no ionosphere "
        f"at all ({ps['media_variants']['configured']['operator/calibrated-guam']['semi_major_95_m'] / 1e3:.2f} km "
        f"instead of {ps['media_variants']['none']['operator/calibrated-guam']['semi_major_95_m'] / 1e3:.2f} km in the "
        "Philippine Sea)",
        f"the median calibrated miss is {round(min(s['median_miss_m'] for s in op_cal), -1):.0f} to "
        f"{round(max(s['median_miss_m'] for s in op_cal), -1):.0f} m instead of "
        f"{round(min(s['median_miss_m'] for s in off_cal), -1):.0f} to "
        f"{round(max(s['median_miss_m'] for s in off_cal), -1):.0f} m",
        f"{min(map(inside, op_cal))} to {max(map(inside, op_cal))} of 40 calibrated fixes at every point with a drawn "
        f"ionosphere, {min(map(inside, fixed))} to {max(map(inside, fixed))} of 40 at a fixed 10 or 30 TECU",
        f"too small again ({min(map(inside, sixty))} to {max(map(inside, sixty))} of 40 inside)",
        f"gives {span([v / 1e3 for v in hawaii_guam])} km calibrated against the distant Guam reference, or "
        f"{span([v / 1e3 for v in hawaii_near])} km with a reference near Honolulu",
    ])


# --------------------------------------------------------------------------- README.md and the example configuration


def flat(text: str) -> str:
    """Text with YAML comment markers removed and white space collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"(?m)^\s*#", " ", text))


def test_readme_and_the_example_configuration_quote_the_study() -> None:
    """README.md 'What it can do' and the scenario table, and the reference-carrier
    comment of config/pacific-uhf.yaml, repeat study numbers; they must stay the
    study's (residual R9)."""
    readme = flat((ROOT / "README.md").read_text(encoding="utf-8"))
    config = flat((ROOT / "config" / "pacific-uhf.yaml").read_text(encoding="utf-8"))
    small_10, small_25 = thresholds("dish-0.72m", 10e3), thresholds("dish-0.72m", 25e3)
    big = gains_over_small_dish("dish-1.2m", 10e3) + gains_over_small_dish("dish-1.2m", 25e3)
    beam = gains_over_small_dish("beam-10dBi", 10e3) + gains_over_small_dish("beam-10dBi", 25e3)
    gains = load("detection")["antenna_peak_gain_dbi_at_downlink"]
    small_gain = min(v["gain_dbi"] for k, v in gains.items() if k.startswith("dish-0.72m"))
    small_beam = statistics.mean(v["hpbw_deg"] for k, v in gains.items() if k.startswith("dish-0.72m"))
    three = [p for p in points() if len(p["epochs"][0]["satellites_seeing_emitter"]) == 3]
    unc = [at_03(p, "operator/uncalibrated") for p in three]
    cal = [at_03(p, "operator/calibrated-guam") for p in three]
    cal_day = [v for p in three for v in over_day(p, "operator/calibrated-guam") if v is not None]
    op_none = [r["summary"] for r in mc_runs() if r["ephemeris"] == "operator" and not r["calibrated"]]
    op_cal = [r["summary"] for r in mc_runs() if r["ephemeris"] == "operator" and r["calibrated"]]
    tle_none = [r["summary"] for r in mc_runs() if r["ephemeris"] == "tle" and not r["calibrated"]]
    tle_guam, tle_ps = mc_run("near-guam", "tle", True, "drawn"), mc_run("philippine-sea", "tle", True, "drawn")
    assert tle_guam and tle_ps
    far = [v for p in points() if abs(p["distance_to_guam_reference_km"] - 3000) < 300
           for v in over_day(p, "tle/calibrated-guam") if v is not None]
    legs = load("accuracy")["media_leg_delays"]
    iono30 = [v for key in ("iono_30tecu_uplink_301.005MHz_ns", "iono_30tecu_downlink_260.0MHz_ns")
              for e, v in zip(legs["elevation_deg"], legs[key], strict=True) if e >= 10]
    cw = load("limits")["uplink-cw/calibrated"]
    sites = load("sites")
    alone = max(relative_changes(sites, "only_one", ["tokyo", "naha", "seoul", "busan", "manila"],
                                 "calibrated_semi_major_95_m"))[0]

    def median_miss(runs: list[str], calibrated: bool, emitter: str = "jammer-philsea") -> list[float]:
        return [t["miss_m"] for (s, e, c), group in scenario_groups().items() if s in runs and e == emitter
                and c == calibrated for r in group for t in r["trials"]]

    demo_cal = median_miss(["pacific-demo"], True)
    demo_semi = [t["semi_major_m"] for (s, e, c), group in scenario_groups().items()
                 if (s, e, c) == ("pacific-demo", "jammer-philsea", True) for r in group for t in r["trials"]]
    eph_unc = median_miss(["ephemeris-error"], False)
    assert_text = {
        "README.md": (readme, [
            f"a 10 kHz jammer needs about {statistics.median(small_10):.0f} dBW EIRP toward the satellite "
            f"({statistics.median(small_25):.0f} dBW if it fills a 25 kHz channel); a 1.2 m dish detects jammers "
            f"{span(big)} dB weaker, a 10 dBi beam antenna {span(beam)} dB weaker",
            f"the 0.72 m dish is a {small_gain:.1f} dBi reflector with a beam about {round(small_beam, -1):.0f} degrees",
            f"semi-axes of about {min(u['semi_major_95_m'] for u in unc) / 1e3:.0f} to "
            f"{max(u['semi_major_95_m'] for u in unc) / 1e3:.0f} km by {span([u['semi_minor_95_m'] / 1e3 for u in unc])} "
            f"km; Monte Carlo median miss {span([s['median_miss_m'] / 1e3 for s in op_none])} km",
            f"median miss {round(min(s['median_miss_m'] for s in op_cal), -1):.0f} to "
            f"{round(max(s['median_miss_m'] for s in op_cal), -1):.0f} m",
            f"Uncalibrated about {statistics.mean(s['median_miss_m'] for s in tle_none) / 1e3:.0f} km median miss; "
            f"calibrated median miss {tle_guam['median_miss_m'] / 1e3:.2f} km near the reference and "
            f"{tle_ps['median_miss_m'] / 1e3:.2f} km 1,500 km away, and a predicted 95 % semi-major of "
            f"{min(far) / 1e3:.1f} to {max(far) / 1e3:.0f} km at about 3,000 km",
            f"kept it to {span(placement_values('tle', ['650']))} km",
            f"roughly {min(iono30) / 1e3:.2f} to {max(iono30) / 1e3:.1f} microseconds",
            f"(calibrated, about {cw['median_miss_m'] / 1e3:.0f} km median)",
            f"predicts within about {5 * round(alone * 20 + 0.49):.0f} % of the seven-site network",
            f"a median miss of {km(statistics.median(demo_cal))} km (at most {km(max(demo_cal))} km) inside a 95 % "
            f"ellipse with a semi-major of {km(min(demo_semi))} to {km(max(demo_semi))} km",
            f"about {statistics.median(median_miss(['clock-fault'], False)) / 1e3:.0f} km off (median)",
            f"calibrated {km(statistics.median(median_miss(['ephemeris-error'], True)))} km from the truth (median); "
            f"uncalibrated {min(eph_unc) / 1e3:.0f} to {statistics.median(eph_unc) / 1e3:.0f} km off or worse",
            f"(median miss {km(statistics.median(median_miss(['two-jammers'], True, 'jammer-a')))} km and "
            f"{km(statistics.median(median_miss(['two-jammers'], True, 'jammer-b')))} km)",
            f"(median miss about {cw['median_miss_m'] / 1e3:.0f} km, 95th percentile {cw['p95_miss_m'] / 1e3:.0f} km)",
        ]),
    }
    ref = load("references")
    design = ref["philippine_sea_reference"]["designs"]
    new, five, old = (design[k] for k in ("8kHz_20dBW (configured)", "5kHz_20dBW", "old_beam_13dBW_5kHz"))
    guam = ref["eirp_near_guam"]["by_grade_and_eirp_dbw"]["operator"]
    measured = [s for (_s, _e, c), group in scenario_groups().items() if c for r in group for t in r["trials"]
                for s in (t.get("reference_sigma_tdoa_ns") or []) if s is not None]
    designs = list(ref["configured_design"].values())
    assert_text["config/pacific-uhf.yaml"] = (config, [
        f"its TDOA noise of {min(old['reference_sigma_tdoa_ns']) / 1e3:.1f} to "
        f"{max(old['reference_sigma_tdoa_ns']) / 1e3:.1f} microseconds kept calibrated ellipses at about half the "
        f"uncalibrated size ({old['calibrated_semi_major_95_m'] / 1e3:.1f} against "
        f"{ref['philippine_sea_reference']['uncalibrated_semi_major_95_m'] / 1e3:.1f} km at 20 N 132 E)",
        f"{min(x for d in designs for x in d['cn0_dbhz_range']):.0f} to "
        f"{max(x for d in designs for x in d['cn0_dbhz_range']):.0f} dB-Hz at every channel",
        f"stays {min(x for d in designs for x in d['backoff_db_alone_range']):.0f} to "
        f"{max(x for d in designs for x in d['backoff_db_alone_range']):.0f} dB below saturation with the reference "
        f"alone ({min(x for d in designs for x in d['backoff_db_with_20dbw_jammer_range']):.1f} to "
        f"{max(x for d in designs for x in d['backoff_db_with_20dbw_jammer_range']):.0f} dB with a 20 dBW jammer",
        f"at worst {guam['20']['ratio_calibrated_over_uncalibrated_max']:.2f} of the uncalibrated one at 20 or 30 dBW, "
        f"but {guam['40']['ratio_calibrated_over_uncalibrated_max']:.2f} at 40 dBW",
        "TDOA noise by " + span([a / b for a, b in zip(five["reference_sigma_tdoa_ns"], new["reference_sigma_tdoa_ns"],
                                                     strict=True)]),
        f"predicted {new['reference_sigma_tdoa_ns'][0]:.0f} and {new['reference_sigma_tdoa_ns'][1]:.0f} ns per "
        "one-second reference capture on the two demo pairs",
        f"the correlator measures {span(measured, '.0f')} ns",
        f"against {min(old['reference_sigma_tdoa_ns']) / 1e3:.1f} to {max(old['reference_sigma_tdoa_ns']) / 1e3:.1f} "
        "microseconds with the old beam design",
        f"from {min(u['semi_major_95_m'] for u in unc) / 1e3:.0f} to {max(u['semi_major_95_m'] for u in unc) / 1e3:.0f} "
        f"km to {span([c['semi_major_95_m'] / 1e3 for c in cal])} km (03 UTC; {span([v / 1e3 for v in cal_day])} km "
        "over the day)",
        f"the simulated miss from {span([s['median_miss_m'] / 1e3 for s in op_none])} km to "
        f"{round(min(s['median_miss_m'] for s in op_cal), -1):.0f} to "
        f"{round(max(s['median_miss_m'] for s in op_cal), -1):.0f} m",
        f"median miss {tle_guam['median_miss_m'] / 1e3:.2f} km at 300 km and {tle_ps['median_miss_m'] / 1e3:.2f} km at "
        f"1,500 km; predicted 95 % semi-major {min(far) / 1e3:.1f} to {max(far) / 1e3:.0f} km at 3,000 km",
    ])
    absent = [f"{where}: {claim}" for where, (text, claims) in assert_text.items() for claim in claims
              if claim not in text]
    assert not absent, "no longer says (from docs/results):\n  " + "\n  ".join(absent)
