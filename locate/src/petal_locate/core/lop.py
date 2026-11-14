"""Lines of position for display (MATH.md section 11).

A line of position is where one TDOA or FDOA row is satisfied exactly: the zero contour
of ``h(lat, lon; height_m) - (z - c)`` on the surface at ``height_m``, with ``h`` the
predictor of MATH.md section 5 and ``c`` the row's calibration correction. Around it
lies the band between the contours at ``-k sigma`` and ``+k sigma``.

The field is sampled on ``region.grid(step_deg)``, only where both satellites of the
row are at or above the elevation mask (other cells are masked), and contoured with
``contourpy``. Output is a GeoJSON ``FeatureCollection`` whose longitudes follow the
grid: values in ``[lon_min, lon_min + 360)``, unwrapped (they may exceed 180) and never
jumping by more than 180 degrees between consecutive vertices; consumers wrap for
display.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from contourpy import FillType, LineType, contour_generator

from .geodesy import lla_to_ecef
from .geometry import predict_fdoa, predict_tdoa
from .orbits import Orbit, orbit_position
from .solver import _Rows, _finite, above_mask
from .timebase import sec
from .types import Measurement

__all__ = ["lop_geojson"]

_COORD_DECIMALS = 6  # 0.1 m; the contour itself is linear interpolation within a cell
_SEAM_EPS_DEG = 10.0 ** -_COORD_DECIMALS


def lop_geojson(
    measurement: Measurement,
    orbits: Mapping[str, Orbit],
    sites_xyz: Mapping[str, Any],
    region: Any,
    height_m: float = 0.0,
    min_elevation_deg: float = 0.0,
    step_deg: float = 0.25,
    k_sigma: float = 2.0,
) -> dict[str, Any]:
    """Line of position of one row and its ``+/- k sigma`` band, as GeoJSON.

    measurement: one TDOA (s) or FDOA (Hz) row; its ``correction`` is subtracted from
    its ``value`` before contouring. orbits: ``dict[sat_id, Orbit]`` (ECEF).
    sites_xyz: ``dict[site_id, ndarray[3]]``, ECEF m. region: object with
    ``grid(step_deg) -> (lat_deg[n_lat], lon_deg[n_lon])`` (longitudes increasing,
    unwrapped); when it also exposes ``lat_max`` and ``lon_span_deg()`` its northern
    and eastern edges are added to the grid if the step does not land on them.
    height_m: height of the surface above the WGS84 ellipsoid, m.
    min_elevation_deg: both satellites of the row must be at least this high (at the
    row's epoch) for a cell to be evaluated. step_deg: grid step, degrees.
    k_sigma: half-width of the band in standard deviations.

    The band uses ``sigma`` of the row; for a calibrated row (``measurement.cal``) the
    reference measurement's sigma is added in quadrature, because ``z - c`` carries
    both noises.

    Returns ``{"type": "FeatureCollection", "features": [...]}`` with up to two
    features (a feature with no geometry is omitted):

    - ``MultiLineString``, ``properties.role == "line"``: the zero contour;
    - ``MultiPolygon``, ``properties.role == "band"``: the region between the
      ``-k sigma`` and ``+k sigma`` contours (exterior rings counter-clockwise, holes
      clockwise).

    Coordinates are ``[lon_deg, lat_deg]``, longitudes as described in the module
    docstring. Both features carry ``kind``, ``tag``, ``a_sat_id``, ``a_site_id``,
    ``b_sat_id``, ``b_site_id``, ``t_ns`` (decimal string), ``value``, ``correction``,
    ``calibrated``, ``sigma`` (the row's), ``sigma_total`` (the one the band uses),
    ``k_sigma`` and ``height_m``.
    """
    rows = _Rows([measurement], orbits, sites_xyz, None, label="measurement")
    height = _finite(height_m, "height_m")
    min_el = _finite(min_elevation_deg, "min_elevation_deg")
    if not -90.0 <= min_el < 90.0:
        raise ValueError(f"min_elevation_deg must be within [-90, 90), got {min_elevation_deg!r}")
    step = _finite(step_deg, "step_deg")
    if not step > 0.0:
        raise ValueError(f"step_deg must be positive, got {step_deg!r}")
    k = _finite(k_sigma, "k_sigma")
    if not k > 0.0:
        raise ValueError(f"k_sigma must be positive, got {k_sigma!r}")
    if not callable(getattr(region, "grid", None)):
        raise ValueError("region.grid is required")

    sigma_total = float(measurement.sigma)
    if measurement.cal is not None:
        sigma_total = float(np.hypot(sigma_total, float(measurement.cal.sigma)))
    properties = {
        "kind": measurement.kind,
        "tag": measurement.tag,
        "a_sat_id": measurement.a.sat_id,
        "a_site_id": measurement.a.site_id,
        "b_sat_id": measurement.b.sat_id,
        "b_site_id": measurement.b.site_id,
        "t_ns": str(int(measurement.t_ns)),
        "value": float(measurement.value),
        "correction": float(measurement.correction),
        "calibrated": measurement.cal is not None,
        "sigma": float(measurement.sigma),
        "sigma_total": sigma_total,
        "k_sigma": k,
        "height_m": height,
    }
    collection: dict[str, Any] = {"type": "FeatureCollection", "features": []}

    lat, lon, closed = _grid(region, step)
    if lat.size < 2 or lon.size < 2:
        return collection
    n_lon = lon.size - 1 if closed else lon.size  # distinct meridians
    lat2, lon2 = np.meshgrid(lat, lon[:n_lon], indexing="ij")
    points = lla_to_ecef(lat2, lon2, height)
    t_s = sec(int(measurement.t_ns))
    visible = np.ones(lat2.shape, dtype=bool)
    for sat in dict.fromkeys((measurement.a.sat_id, measurement.b.sat_id)):
        visible &= above_mask(lat2, lon2, points, orbit_position(orbits[sat], t_s), min_el)
    if not np.any(visible):
        return collection
    predict = predict_tdoa if measurement.kind == "tdoa" else predict_fdoa
    predicted = predict(points[visible], measurement.a, measurement.b, t_s, orbits, sites_xyz)
    field = np.full((lat.size, lon.size), np.nan)
    field[:, :n_lon][visible] = (predicted - float(rows.z[0])) / sigma_total
    if closed:
        field[:, -1] = field[:, 0]  # the first meridian again, 360 degrees further east

    generator = contour_generator(
        x=lon,
        y=lat,
        z=np.ma.masked_invalid(field),
        corner_mask=True,
        line_type=LineType.Separate,
        fill_type=FillType.OuterOffset,
    )
    lon_stop = float(lon[0]) + 360.0 - _SEAM_EPS_DEG

    def coordinates(vertices: np.ndarray) -> list[list[float]]:
        out = np.round(vertices, _COORD_DECIMALS)
        out[:, 0] = np.minimum(out[:, 0], lon_stop)  # keep the seam inside [lon_min, lon_min + 360)
        return out.tolist()

    lines = [coordinates(line) for line in generator.lines(0.0) if len(line) >= 2]
    if lines:
        collection["features"].append(
            {
                "type": "Feature",
                "geometry": {"type": "MultiLineString", "coordinates": lines},
                "properties": {**properties, "role": "line"},
            }
        )
    polygons = []
    for vertices, offsets in zip(*generator.filled(-k, k)):
        rings = [coordinates(vertices[start:stop]) for start, stop in zip(offsets[:-1], offsets[1:])]
        if rings:
            polygons.append(rings)
    if polygons:
        collection["features"].append(
            {
                "type": "Feature",
                "geometry": {"type": "MultiPolygon", "coordinates": polygons},
                "properties": {**properties, "role": "band"},
            }
        )
    return collection


def _grid(region: Any, step: float) -> tuple[np.ndarray, np.ndarray, bool]:
    """Grid axes for contouring: ``(lat_deg, lon_deg, closed)``.

    ``region.grid(step)`` with the region's northern and eastern edges appended when
    the region exposes them and the step stops short of them. For a full-circle region
    the first meridian is appended again at ``lon[0] + 360`` (``closed`` is true), so
    contours continue across the seam of the grid.
    """
    lat, lon = (np.asarray(v, dtype=np.float64).reshape(-1) for v in region.grid(step))
    if lat.size == 0 or lon.size == 0:
        return lat, lon, False
    if np.any(np.diff(lat) <= 0.0) or np.any(np.diff(lon) <= 0.0):
        raise ValueError("region.grid must return increasing latitudes and longitudes")
    lat_max = getattr(region, "lat_max", None)
    if lat_max is not None and float(lat_max) - lat[-1] > 1e-9 * step:
        lat = np.append(lat, float(lat_max))
    span_fn = getattr(region, "lon_span_deg", None)
    if callable(span_fn):
        span = float(span_fn())
        closed = span >= 360.0 - 1e-9
        if not closed and (lon[0] + span) - lon[-1] > 1e-9 * step:
            lon = np.append(lon, lon[0] + span)
    else:
        closed = bool((lon[-1] - lon[0]) + step >= 360.0 - 1e-9)
    if closed and lon[-1] >= lon[0] + 360.0 - 1e-9 * step:
        closed = False  # the grid already repeats its first meridian
    if closed:
        lon = np.append(lon, lon[0] + 360.0)
    return lat, lon, closed
