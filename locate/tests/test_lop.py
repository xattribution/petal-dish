"""lop: lines of position as GeoJSON (MATH.md section 11).

The contours are checked against the predictors called directly: the true emitter of a
noise-free row, roots found by bisection along parallels, and point-by-point
classification of the +/- k sigma band. `region` is a small stand-in for the pydantic
model.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest
from scipy.optimize import brentq

from petal_locate.core import geometry as gm
from petal_locate.core import orbits as ob
from petal_locate.core import solver as sv
from petal_locate.core.geodesy import enu_basis, lla_to_ecef
from petal_locate.core.lop import lop_geojson
from petal_locate.core.timebase import ns_from_iso, sec
from petal_locate.core.types import Measurement, PathSpec


class Region:
    """Stand-in for the configuration Region (ARCHITECTURE.md section 3)."""

    def __init__(self, lat_min: float = -70.0, lat_max: float = 70.0, lon_min: float = -180.0, lon_max: float = 180.0) -> None:
        self.lat_min, self.lat_max, self.lon_min, self.lon_max = lat_min, lat_max, lon_min, lon_max

    def lon_span_deg(self) -> float:
        if self.lon_min == -180.0 and self.lon_max == 180.0:
            return 360.0
        return (self.lon_max - self.lon_min) % 360.0

    def contains(self, lat_deg: Any, lon_deg: Any) -> np.ndarray:
        lat, lon = np.asarray(lat_deg, dtype=float), np.asarray(lon_deg, dtype=float)
        in_lat = (lat >= self.lat_min) & (lat <= self.lat_max)
        return in_lat & (np.mod(lon - self.lon_min, 360.0) <= self.lon_span_deg())

    def grid(self, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
        lat = self.lat_min + step_deg * np.arange(int(np.floor((self.lat_max - self.lat_min) / step_deg + 1e-9)) + 1)
        span = self.lon_span_deg()
        n_lon = int(np.floor(span / step_deg + 1e-9)) + (0 if span >= 360.0 else 1)
        return lat, self.lon_min + step_deg * np.arange(n_lon)


class GridOnly:
    """A region exposing nothing but the two documented methods."""

    def __init__(self, inner: Region) -> None:
        self.grid = inner.grid
        self.contains = inner.contains


T_NS = ns_from_iso("2026-01-01T03:00:00Z")
T_S = sec(T_NS)
ORBITS: dict[str, Any] = {
    "sat1": ob.GeoOrbit(-177.0, 5.0, phase_deg=40.0),
    "sat2": ob.GeoOrbit(172.0, 4.0, phase_deg=200.0),
    "sat3": ob.GeoOrbit(75.0, 5.0, phase_deg=310.0),
}
SITES: dict[str, np.ndarray] = {
    "hnl": lla_to_ecef(21.31, -157.86, 15.0),
    "tyo": lla_to_ecef(35.68, 139.77, 50.0),
    "mnl": lla_to_ecef(14.60, 120.98, 20.0),
}
PATH1 = PathSpec("sat1", "hnl", 300e6, -41.0e6)
PATH2 = PathSpec("sat2", "tyo", 300e6, -33.6e6)
PATH3 = PathSpec("sat3", "mnl", 300e6, -41.0e6)
PACIFIC = Region(-60.0, 60.0, 60.0, -120.0)  # wraps the antimeridian: grid longitudes 60 .. 240


def predict(kind: str, lat: Any, lon: Any, a: PathSpec = PATH1, b: PathSpec = PATH2, height: float = 0.0) -> Any:
    f = gm.predict_tdoa if kind == "tdoa" else gm.predict_fdoa
    return f(lla_to_ecef(lat, lon, height), a, b, T_S, ORBITS, SITES)


def row_at(kind: str, lat: float, lon: float, sigma: float | None = None, a: PathSpec = PATH1, b: PathSpec = PATH2, height: float = 0.0) -> Measurement:
    """Noise-free row for an emitter at (lat, lon, height)."""
    if sigma is None:
        sigma = 1e-6 if kind == "tdoa" else 0.05
    return Measurement(kind, float(predict(kind, lat, lon, a, b, height)), sigma, T_NS, a, b, tag=f"{kind}-row")


def feature(collection: dict[str, Any], role: str) -> dict[str, Any] | None:
    found = [f for f in collection["features"] if f["properties"]["role"] == role]
    assert len(found) <= 1
    return found[0] if found else None


def lines_of(collection: dict[str, Any]) -> list[np.ndarray]:
    f = feature(collection, "line")
    return [] if f is None else [np.array(line) for line in f["geometry"]["coordinates"]]


def distance_to_lines_m(lines: list[np.ndarray], lat: float, lon: float, height: float = 0.0) -> float:
    """Shortest distance from a point to a set of polylines, in the point's local east/north plane."""
    origin = lla_to_ecef(lat, lon, height)
    basis = enu_basis(lat, lon)
    best = np.inf
    for line in lines:
        p = ((lla_to_ecef(line[:, 1], line[:, 0], height) - origin) @ basis.T)[:, :2]
        d = p[1:] - p[:-1]
        t = np.clip(-np.sum(p[:-1] * d, axis=1) / np.maximum(np.sum(d * d, axis=1), 1e-30), 0.0, 1.0)
        best = min(best, float(np.min(np.linalg.norm(p[:-1] + t[:, None] * d, axis=1))))
    return best


def inside_multipolygon(polygons: list[list[list[list[float]]]], lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Even-odd point-in-polygon over every ring (holes flip the parity), by ray casting."""
    inside = np.zeros(lon.shape, dtype=bool)
    for rings in polygons:
        for ring in rings:
            v = np.array(ring)
            x0, y0, x1, y1 = v[:-1, 0], v[:-1, 1], v[1:, 0], v[1:, 1]
            for k in range(x0.size):
                if y0[k] == y1[k]:
                    continue
                crosses = (y0[k] > lat) != (y1[k] > lat)
                x_at = x0[k] + (lat - y0[k]) * (x1[k] - x0[k]) / (y1[k] - y0[k])
                inside ^= crosses & (lon < x_at)
    return inside


def ring_area(ring: list[list[float]]) -> float:
    v = np.array(ring)
    return 0.5 * float(np.sum(v[:-1, 0] * v[1:, 1] - v[1:, 0] * v[:-1, 1]))


# ----------------------------------------------------------------------------- the line


@pytest.mark.parametrize("kind", ["tdoa", "fdoa"])
@pytest.mark.parametrize(
    "lat, lon, region",
    [
        (30.37, 140.41, Region()),
        (10.0, 179.9, Region()),  # just west of the antimeridian, at the seam of the default grid
        (-20.0, -179.95, Region()),  # just east of it
        (-41.3, 179.99, Region()),
        (10.0, 179.9, PACIFIC),
        (-20.3, -179.95, PACIFIC),
        (30.37, 140.41, GridOnly(PACIFIC)),
    ],
    ids=["japan", "west-of-180", "east-of-180", "on-the-seam", "wrapped-west", "wrapped-east", "grid-only-region"],
)
def test_zero_contour_passes_through_the_true_emitter(kind: str, lat: float, lon: float, region: Any) -> None:
    collection = lop_geojson(row_at(kind, lat, lon), ORBITS, SITES, region)
    lines = lines_of(collection)
    assert lines, "no line of position"
    assert distance_to_lines_m(lines, lat, lon) < 200.0  # 20 m measured at the default 0.25 degree step
    # and the line really is the locus h = z: every vertex predicts the measured value
    z = row_at(kind, lat, lon)
    scale = 3e-9 if kind == "tdoa" else 3e-7  # predictor gradient, s/m and Hz/m: tolerance of about 300 m
    for line in lines:
        residual = predict(kind, line[:, 1], line[:, 0]) - z.value
        assert np.max(np.abs(residual)) < 300.0 * scale


@pytest.mark.parametrize("kind", ["tdoa", "fdoa"])
def test_contour_agrees_with_roots_found_along_parallels_and_meridians(kind: str) -> None:
    row = row_at(kind, 30.37, 140.41)
    lines = lines_of(lop_geojson(row, ORBITS, SITES, Region(0.0, 60.0, 100.0, 180.0)))

    def visible(lat: Any, lon: Any) -> np.ndarray:
        x = lla_to_ecef(lat, lon, 0.0)
        return np.minimum(gm.elevation_deg(x, ORBITS["sat1"], T_S), gm.elevation_deg(x, ORBITS["sat2"], T_S)) > 1.0

    checked = 0
    lon_scan = np.linspace(100.0, 180.0, 321)
    for lat in (5.13, 17.9, 30.37, 44.44, 55.02):  # roots in longitude along a parallel
        g = predict(kind, lat, lon_scan) - row.value
        ok = visible(lat, lon_scan)
        for k in np.flatnonzero((g[:-1] * g[1:] < 0.0) & ok[:-1] & ok[1:]):
            root = brentq(lambda lon: float(predict(kind, lat, lon)) - row.value, lon_scan[k], lon_scan[k + 1], xtol=1e-10)
            assert distance_to_lines_m(lines, lat, root) < 100.0
            checked += 1
    lat_scan = np.linspace(0.0, 60.0, 241)
    for lon in (112.3, 127.77, 140.41, 155.5, 171.2):  # roots in latitude along a meridian
        g = predict(kind, lat_scan, lon) - row.value
        ok = visible(lat_scan, lon)
        for k in np.flatnonzero((g[:-1] * g[1:] < 0.0) & ok[:-1] & ok[1:]):
            root = brentq(lambda lat: float(predict(kind, lat, lon)) - row.value, lat_scan[k], lat_scan[k + 1], xtol=1e-10)
            assert distance_to_lines_m(lines, root, lon) < 100.0
            checked += 1
    assert checked >= 5


def test_line_at_a_height_above_the_ellipsoid() -> None:
    row = row_at("tdoa", 30.37, 140.41, height=3000.0)
    region = Region(10.0, 50.0, 120.0, 160.0)
    at_height = lines_of(lop_geojson(row, ORBITS, SITES, region, height_m=3000.0))
    at_zero = lines_of(lop_geojson(row, ORBITS, SITES, region, height_m=0.0))
    assert distance_to_lines_m(at_height, 30.37, 140.41, 3000.0) < 50.0
    assert distance_to_lines_m(at_zero, 30.37, 140.41, 0.0) > 250.0  # 3 km of height moves this line 340 m


def test_calibration_correction_is_included() -> None:
    truth = row_at("tdoa", 30.37, 140.41)
    bias = 4.2e-6  # common to target and reference: 1.3 km of range difference
    reference = Measurement("tdoa", float(predict("tdoa", 13.44, 144.79)) + bias, 3e-6, T_NS, PATH1, PATH2)
    c, cal = sv.calibration_correction(reference, lla_to_ecef(13.44, 144.79, 0.0), ORBITS, SITES, "ref-1")
    assert c == pytest.approx(bias, rel=1e-9)
    biased = Measurement("tdoa", truth.value + bias, 1e-6, T_NS, PATH1, PATH2)
    calibrated = Measurement("tdoa", truth.value + bias, 1e-6, T_NS, PATH1, PATH2, correction=c, cal=cal)
    region = Region(10.0, 50.0, 120.0, 160.0)
    raw = lop_geojson(biased, ORBITS, SITES, region)
    fixed = lop_geojson(calibrated, ORBITS, SITES, region)
    assert distance_to_lines_m(lines_of(raw), 30.37, 140.41) > 500.0
    assert distance_to_lines_m(lines_of(fixed), 30.37, 140.41) < 200.0
    props = feature(fixed, "line")["properties"]
    assert props["calibrated"] is True and props["correction"] == c and props["value"] == calibrated.value
    assert props["sigma"] == 1e-6 and props["sigma_total"] == pytest.approx(np.hypot(1e-6, 3e-6))
    assert feature(raw, "line")["properties"]["calibrated"] is False
    assert feature(raw, "line")["properties"]["sigma_total"] == 1e-6


# ----------------------------------------------------------------------------- longitudes


def _all_rings(collection: dict[str, Any]) -> list[np.ndarray]:
    out = lines_of(collection)
    band = feature(collection, "band")
    if band is not None:
        out += [np.array(ring) for polygon in band["geometry"]["coordinates"] for ring in polygon]
    return out


@pytest.mark.parametrize("kind", ["tdoa", "fdoa"])
def test_longitudes_follow_the_region_grid(kind: str) -> None:
    row = row_at(kind, 10.0, 179.9, sigma=20e-6 if kind == "tdoa" else 0.5)
    for region, lon_min in ((Region(), -180.0), (PACIFIC, 60.0), (Region(-30.0, 40.0, 150.0, -150.0), 150.0)):
        collection = lop_geojson(row, ORBITS, SITES, region, step_deg=0.5)
        pieces = _all_rings(collection)
        assert feature(collection, "line") is not None and feature(collection, "band") is not None
        for piece in pieces:
            assert np.all(piece[:, 0] >= lon_min) and np.all(piece[:, 0] < lon_min + 360.0)
            assert np.max(np.abs(np.diff(piece[:, 0]))) < 180.0
            assert np.all(np.abs(piece[:, 1]) <= 90.0)
        lons = np.concatenate([piece[:, 0] for piece in pieces])
        if lon_min > -180.0:  # unwrapped: the antimeridian is crossed without a jump
            assert lons.max() > 180.0 and lons.min() < 180.0
            crossing = [p for p in lines_of(collection) if p[:, 0].min() < 180.0 < p[:, 0].max()]
            assert crossing, "the line through the emitter is one piece across 180 degrees"
            assert distance_to_lines_m(crossing, 10.0, 179.9) < 400.0  # 0.5 degree step


def test_full_circle_grid_is_closed_at_its_seam() -> None:
    """Default region: the grid ends at 179.75 E; the contour must continue to the seam and resume at -180."""
    row = row_at("tdoa", 10.0, 179.9)
    lines = lines_of(lop_geojson(row, ORBITS, SITES, Region()))
    east_ends = [(p[k, 0], p[k, 1]) for p in lines for k in (0, -1) if p[k, 0] > 179.9999]
    west_ends = [(p[k, 0], p[k, 1]) for p in lines for k in (0, -1) if p[k, 0] == -180.0]
    assert east_ends and west_ends
    for lon, lat in east_ends:
        assert lon < 180.0
        assert min(abs(lat - w[1]) for w in west_ends) < 1e-5  # the same point of the seam
    # the last column of cells (179.75 .. 180) is contoured: the emitter at 179.9 E lies on a line
    assert distance_to_lines_m(lines, 10.0, 179.9) < 200.0


def test_region_edges_are_covered_when_the_step_stops_short() -> None:
    region = Region(10.2, 50.7, 120.3, 160.9)  # none of the edges on a 1 degree grid
    lat, lon = region.grid(1.0)
    assert lat[-1] == pytest.approx(50.2) and lon[-1] == pytest.approx(160.3)
    emitter = (50.55, 160.7)  # in the strip beyond the last grid row and column
    row = row_at("tdoa", *emitter)
    lines = lines_of(lop_geojson(row, ORBITS, SITES, region, step_deg=1.0))
    assert distance_to_lines_m(lines, *emitter) < 2000.0  # 1 degree cells: linear interpolation error
    vertices = np.concatenate(lines)
    assert vertices[:, 1].max() <= 50.7 + 1e-9 and vertices[:, 0].max() <= 160.9 + 1e-9
    # a region without lat_max / lon_span_deg is contoured on its bare grid: the strip is not covered
    bare = lines_of(lop_geojson(row, ORBITS, SITES, GridOnly(region), step_deg=1.0))
    assert not bare or np.concatenate(bare)[:, 1].max() <= 50.2 + 1e-9


# ----------------------------------------------------------------------------- elevation mask


def test_cells_below_the_elevation_mask_are_not_contoured() -> None:
    # satellite 3 is 12.3 degrees above the horizon of the emitter
    lat, lon = 30.37, 140.41
    assert float(gm.elevation_deg(lla_to_ecef(lat, lon, 0.0), ORBITS["sat3"], T_S)) == pytest.approx(12.34, abs=0.01)
    row = row_at("tdoa", lat, lon, a=PATH1, b=PATH3)
    region = Region(-60.0, 60.0, 90.0, 180.0)
    open_sky = lines_of(lop_geojson(row, ORBITS, SITES, region, min_elevation_deg=0.0))
    grazing = lines_of(lop_geojson(row, ORBITS, SITES, region, min_elevation_deg=12.0))
    masked = lines_of(lop_geojson(row, ORBITS, SITES, region, min_elevation_deg=14.0))
    assert distance_to_lines_m(open_sky, lat, lon) < 200.0
    assert distance_to_lines_m(grazing, lat, lon) < 200.0  # the emitter is still above a 12 degree mask
    assert masked and distance_to_lines_m(masked, lat, lon) > 300e3  # the line stops where sat3 drops below 14 deg
    for lines, mask in ((open_sky, 0.0), (grazing, 12.0), (masked, 14.0)):
        for line in lines:
            x = lla_to_ecef(line[:, 1], line[:, 0], 0.0)
            lowest = np.minimum(gm.elevation_deg(x, ORBITS["sat1"], T_S), gm.elevation_deg(x, ORBITS["sat3"], T_S))
            assert np.min(lowest) > mask - 0.4  # within one 0.25 degree cell of the mask
    length = lambda lines: sum(len(line) for line in lines)
    assert length(masked) < length(grazing) < length(open_sky) and length(masked) < 0.5 * length(open_sky)
    # the line ends at the mask: its lowest vertex sits on it
    x = lla_to_ecef(np.concatenate(masked)[:, 1], np.concatenate(masked)[:, 0], 0.0)
    lowest = np.minimum(gm.elevation_deg(x, ORBITS["sat1"], T_S), gm.elevation_deg(x, ORBITS["sat3"], T_S))
    assert np.min(lowest) < 14.4
    # nothing is visible above 89 degrees of elevation
    assert lop_geojson(row, ORBITS, SITES, region, min_elevation_deg=89.0)["features"] == []


# ----------------------------------------------------------------------------- the band


@pytest.mark.parametrize("kind, sigma, k_sigma", [("tdoa", 20e-6, 2.0), ("fdoa", 0.4, 2.0), ("tdoa", 40e-6, 1.0)])
def test_band_is_where_the_row_is_within_k_sigma(kind: str, sigma: float, k_sigma: float) -> None:
    lat0, lon0 = 30.37, 140.41
    row = row_at(kind, lat0, lon0, sigma=sigma)
    region = Region(10.0, 50.0, 120.0, 160.0)
    collection = lop_geojson(row, ORBITS, SITES, region, k_sigma=k_sigma)
    band = feature(collection, "band")
    assert band is not None and band["geometry"]["type"] == "MultiPolygon"
    assert band["properties"]["k_sigma"] == k_sigma and band["properties"]["sigma_total"] == sigma
    rng = np.random.default_rng(17)
    lat = rng.uniform(11.0, 49.0, 4000)
    lon = rng.uniform(121.0, 159.0, 4000)
    deviation = (predict(kind, lat, lon) - row.value) / sigma
    clear = np.abs(np.abs(deviation) - k_sigma) > 0.05  # away from the band edge (interpolation within a cell)
    expected = np.abs(deviation) <= k_sigma
    got = inside_multipolygon(band["geometry"]["coordinates"], lon, lat)
    assert np.array_equal(got[clear], expected[clear])
    assert 50 < np.count_nonzero(expected & clear) < 3500  # both classes are well populated
    # the line of position lies inside its band
    for line in lines_of(collection):
        mid = 0.5 * (line[1:] + line[:-1])
        interior = (mid[:, 0] > 120.5) & (mid[:, 0] < 159.5) & (mid[:, 1] > 10.5) & (mid[:, 1] < 49.5)
        assert np.all(inside_multipolygon(band["geometry"]["coordinates"], mid[interior, 0], mid[interior, 1]))
    # ring orientation: exterior counter-clockwise, holes clockwise (GeoJSON right-hand rule); rings closed
    for polygon in band["geometry"]["coordinates"]:
        for i, ring in enumerate(polygon):
            assert ring[0] == ring[-1] and len(ring) >= 4
            assert (ring_area(ring) > 0.0) == (i == 0)


def test_band_of_a_calibrated_row_uses_both_sigmas() -> None:
    lat0, lon0 = 30.37, 140.41
    truth = row_at("tdoa", lat0, lon0)
    reference = Measurement("tdoa", float(predict("tdoa", 13.44, 144.79)), 30e-6, T_NS, PATH1, PATH2)
    c, cal = sv.calibration_correction(reference, lla_to_ecef(13.44, 144.79, 0.0), ORBITS, SITES, "ref-1")
    row = Measurement("tdoa", truth.value, 10e-6, T_NS, PATH1, PATH2, correction=c, cal=cal)
    band = feature(lop_geojson(row, ORBITS, SITES, Region(10.0, 50.0, 120.0, 160.0)), "band")
    assert band is not None
    total = float(np.hypot(10e-6, 30e-6))
    rng = np.random.default_rng(23)
    lat, lon = rng.uniform(11.0, 49.0, 3000), rng.uniform(121.0, 159.0, 3000)
    deviation = np.abs(predict("tdoa", lat, lon) - truth.value)
    got = inside_multipolygon(band["geometry"]["coordinates"], lon, lat)
    clear = np.abs(deviation / total - 2.0) > 0.05
    assert np.array_equal(got[clear], (deviation <= 2.0 * total)[clear])
    # with the row's own sigma alone the band would be a third as wide: many of these points would fall outside
    assert np.count_nonzero(got & (deviation > 2.0 * 10e-6)) > 50


# ----------------------------------------------------------------------------- structure and errors


def test_geojson_structure_and_properties() -> None:
    row = row_at("fdoa", 30.37, 140.41, sigma=0.3)
    collection = lop_geojson(row, ORBITS, SITES, Region(10.0, 50.0, 120.0, 160.0), height_m=0.0, min_elevation_deg=0.0, step_deg=0.5, k_sigma=1.5)
    assert json.loads(json.dumps(collection, allow_nan=False)) == collection
    assert collection["type"] == "FeatureCollection" and len(collection["features"]) == 2
    line, band = feature(collection, "line"), feature(collection, "band")
    assert line["type"] == band["type"] == "Feature"
    assert line["geometry"]["type"] == "MultiLineString" and band["geometry"]["type"] == "MultiPolygon"
    for f in (line, band):
        props = f["properties"]
        assert props["kind"] == "fdoa" and props["tag"] == "fdoa-row"
        assert (props["a_sat_id"], props["a_site_id"], props["b_sat_id"], props["b_site_id"]) == ("sat1", "hnl", "sat2", "tyo")
        assert props["t_ns"] == str(T_NS) and isinstance(props["t_ns"], str)  # nanoseconds never as a JSON number
        assert props["value"] == row.value and props["correction"] == 0.0 and props["calibrated"] is False
        assert props["sigma"] == 0.3 and props["sigma_total"] == 0.3 and props["k_sigma"] == 1.5 and props["height_m"] == 0.0
    for piece in line["geometry"]["coordinates"]:
        assert len(piece) >= 2 and all(len(v) == 2 and all(isinstance(c, float) for c in v) for v in piece)
    first = line["geometry"]["coordinates"][0][0]
    assert 120.0 <= first[0] <= 160.0 and 10.0 <= first[1] <= 50.0  # [lon, lat] order


def test_no_geometry_gives_an_empty_collection() -> None:
    region = Region(10.0, 50.0, 120.0, 160.0)
    far = Measurement("tdoa", 0.5, 1e-6, T_NS, PATH1, PATH2)  # no point on Earth has a TDOA of half a second
    assert lop_geojson(far, ORBITS, SITES, region) == {"type": "FeatureCollection", "features": []}
    # a region from which satellite 1 is below the horizon
    hidden = lop_geojson(row_at("tdoa", 30.37, 140.41), ORBITS, SITES, Region(-10.0, 10.0, 0.0, 40.0))
    assert hidden == {"type": "FeatureCollection", "features": []}
    # inside the band but not on the line: a band feature alone
    lat, lon = 30.0, 140.0
    values = predict("tdoa", np.array([[10.0, 10.0], [50.0, 50.0]]), np.array([[120.0, 160.0], [120.0, 160.0]]))
    just_outside = float(values.max()) + 30e-6
    partial = lop_geojson(Measurement("tdoa", just_outside, 20e-6, T_NS, PATH1, PATH2), ORBITS, SITES, region)
    assert [f["properties"]["role"] for f in partial["features"]] == ["band"]


def test_invalid_arguments_name_the_field() -> None:
    row = row_at("tdoa", 30.37, 140.41)
    region = Region(10.0, 50.0, 120.0, 160.0)
    for kwargs, pattern in (
        ({"step_deg": 0.0}, "step_deg"),
        ({"step_deg": float("nan")}, "step_deg"),
        ({"k_sigma": -1.0}, "k_sigma"),
        ({"min_elevation_deg": 120.0}, "min_elevation_deg"),
        ({"height_m": float("inf")}, "height_m"),
    ):
        with pytest.raises(ValueError, match=pattern):
            lop_geojson(row, ORBITS, SITES, region, **kwargs)
    with pytest.raises(ValueError, match="measurement.kind"):
        lop_geojson(Measurement("range", 0.0, 1e-6, T_NS, PATH1, PATH2), ORBITS, SITES, region)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="measurement.sigma"):
        lop_geojson(Measurement("tdoa", 0.0, 0.0, T_NS, PATH1, PATH2), ORBITS, SITES, region)
    with pytest.raises(ValueError, match=r"measurement\.a\.sat_id"):
        lop_geojson(Measurement("tdoa", 0.0, 1e-6, T_NS, PathSpec("nosat", "hnl", 300e6, -41e6), PATH2), ORBITS, SITES, region)
    with pytest.raises(ValueError, match="measurement"):
        lop_geojson("row", ORBITS, SITES, region)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="region.grid"):
        lop_geojson(row, ORBITS, SITES, object())
